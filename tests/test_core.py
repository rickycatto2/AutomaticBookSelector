import copy
import csv
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from core import ABS, AppError, DEFAULT_PROFILE, Store, identity, isbn


def export(rows):
    output = io.StringIO()
    keys = ['Book Id', 'Title', 'Author', 'My Rating', 'Exclusive Shelf', 'ISBN13', 'Read Count', 'Date Read', 'Bookshelves']
    writer = csv.DictWriter(output, fieldnames=keys)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue().encode('utf-8-sig')


def item(book_id, title, author='Writer', description='Contemporary literary fiction about identity and intimate relationships.', **kwargs):
    return {'id': book_id, 'libraryId': 'lib', 'mediaType': 'book', 'media': {'metadata': {'title': title, 'authorName': author, 'description': description, 'genres': ['Fiction']}, 'numAudioFiles': 1, 'duration': 36000}, **kwargs}


class FakeABS:
    url, token = 'http://fake', 'fake-token'

    def __init__(self):
        self.user = {'id': 'user', 'username': 'reader', 'mediaProgress': []}
        self.items = [item('one', 'One'), item('two', 'Two'), item('three', 'Three')]
        self.remote = {}
        self.writes = []
        self.fail = False

    def get(self, path):
        if self.fail:
            raise AppError('Simulated offline server')
        if path == '/api/me':
            return copy.deepcopy(self.user)
        if path == '/api/libraries':
            return {'libraries': [{'id': 'lib', 'name': 'Audiobooks', 'mediaType': 'book'}]}
        if path.endswith('/playlists'):
            return {'results': copy.deepcopy(list(self.remote.values())), 'total': len(self.remote)}
        if path.startswith('/api/playlists/'):
            return copy.deepcopy(self.remote[path.split('/')[-1]])
        raise AssertionError(path)

    def pages(self, path):
        if self.fail:
            raise AppError('Simulated offline server')
        return copy.deepcopy(self.items)

    def request(self, method, path, payload):
        self.writes.append((method, path, copy.deepcopy(payload)))
        if path == '/api/playlists':
            pl = {'id': 'playlist', 'name': payload['name'], 'userId': 'user', 'libraryId': payload['libraryId'], 'items': []}
            self.remote[pl['id']] = pl
        else:
            pl = self.remote[path.split('/')[3]]
            pl['items'].extend(payload['items'])
        return copy.deepcopy(pl)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.abs = FakeABS()
        self.store = Store(pathlib.Path(self.temp.name) / 'test.sqlite3', self.abs)
        self.store.sync()

    def test_bootstrap_is_once_and_import_is_idempotent(self):
        raw = export([{'Book Id': '1', 'Title': 'One', 'Author': 'Writer', 'My Rating': '4', 'Exclusive Shelf': 'read'}])
        self.assertEqual(self.store.import_csv(raw)['imported'], 1)
        self.assertTrue(self.store.import_csv(raw, explicit=True)['skipped'])
        newer = export([{'Book Id': '2', 'Title': 'Two', 'Author': 'Writer', 'My Rating': '1', 'Exclusive Shelf': 'read'}])
        self.assertTrue(self.store.import_csv(newer)['skipped'])
        self.assertEqual(self.store.snapshot()['stats']['history'], 1)

    def test_bad_csv_is_atomic(self):
        raw = export([{'Book Id': '1', 'Title': 'One', 'Author': 'Writer', 'My Rating': '4'}, {'Book Id': '2', 'Title': 'Two', 'Author': 'Writer', 'My Rating': '99'}])
        with self.assertRaises(AppError):
            self.store.import_csv(raw)
        self.assertEqual(self.store.snapshot()['stats']['history'], 0)

    def test_goodreads_integral_decimal_ratings(self):
        raw = export([{'Book Id': '1', 'Title': 'One', 'Author': 'Writer', 'My Rating': '4.0', 'Read Count': '1.0', 'Exclusive Shelf': 'read'}])
        self.assertEqual(self.store.import_csv(raw)['imported'], 1)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT rating FROM history').fetchone()[0], 4)
        fractional = export([{'Book Id': '2', 'Title': 'Two', 'Author': 'Writer', 'My Rating': '3.5'}])
        with self.assertRaises(AppError):
            self.store.import_csv(fractional, explicit=True)

    def test_finished_and_progress_and_seed_are_excluded(self):
        self.store.import_csv(export([{'Book Id': '1', 'Title': 'One (Series #1)', 'Author': 'Writer', 'My Rating': '5', 'Exclusive Shelf': 'read'}]))
        self.abs.user['mediaProgress'] = [{'libraryItemId': 'two', 'isFinished': True, 'progress': 1, 'finishedAt': 123}, {'libraryItemId': 'three', 'isFinished': False, 'progress': .2}]
        self.store.sync()
        self.assertEqual(self.store.recommendations()['books'], [])
        self.assertEqual(len(self.store.snapshot()['questions']), 1)

    def test_completion_does_not_invent_positive_feedback_and_can_be_reset(self):
        self.abs.user['mediaProgress'] = [{'libraryItemId': 'one', 'isFinished': True, 'progress': 1}]
        self.store.sync()
        self.assertEqual(self.store.snapshot()['stats']['feedback'], 0)
        self.abs.user['mediaProgress'] = []
        self.store.sync()
        self.assertIn('one', [b['id'] for b in self.store.recommendations()['books']])
        self.assertEqual(self.store.snapshot()['questions'], [])

    def test_reimport_never_overwrites_abs_or_feedback(self):
        self.store.feedback({'book_id': 'one', 'rating': 1, 'notes': 'Too much plot', 'avoid': 'thriller'})
        self.abs.user['mediaProgress'] = [{'libraryItemId': 'two', 'isFinished': True, 'progress': 1}]
        self.store.sync()
        self.store.import_csv(export([{'Book Id': '2', 'Title': 'Two', 'Author': 'Writer', 'My Rating': '5', 'Exclusive Shelf': 'to-read'}]), explicit=True)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT rating FROM feedback WHERE book_id="one"').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT finished FROM books WHERE id="two"').fetchone()[0], 1)

    def test_sync_failure_keeps_snapshot_and_rejects_other_user(self):
        original = self.store.snapshot()['last_sync']
        self.abs.fail = True
        with self.assertRaises(AppError):
            self.store.sync()
        self.assertEqual(self.store.snapshot()['last_sync'], original)
        self.assertEqual(self.store.snapshot()['stats']['catalogue'], 3)
        self.abs.fail = False
        self.abs.user['id'] = 'another-user'
        with self.assertRaises(AppError):
            self.store.sync()
        self.assertEqual(self.store.snapshot()['last_sync'], original)

    def test_removed_and_ebook_only_and_duplicate_editions(self):
        duplicate = item('edition', 'One', 'Writer')
        ebook = item('ebook', 'Ebook')
        ebook['media']['numAudioFiles'] = 0
        self.abs.items.extend([duplicate, ebook])
        self.store.sync()
        self.assertEqual(len(self.store.recommendations()['books']), 3)
        self.abs.items = [item('one', 'One')]
        self.store.sync()
        self.assertEqual(self.store.snapshot()['stats']['catalogue'], 1)

    def test_feedback_shifts_themes_and_dislike_excludes_work(self):
        self.abs.items = [item('one', 'Read', description='surreal uncanny grief'), item('two', 'Surreal', description='surreal uncanny grief'), item('three', 'Thriller', description='detective crime thriller')]
        self.store.sync()
        profile = copy.deepcopy(DEFAULT_PROFILE)
        profile.update(likes='', avoids='', preferred_authors='')
        profile['weights'] = {key: 0 for key in profile['weights']}
        self.store.profile(profile)
        self.store.feedback({'book_id': 'one', 'rating': 5, 'enjoyed': 'surreal uncanny grief'})
        self.assertEqual(self.store.recommendations()['books'][0]['id'], 'two')
        self.store.feedback({'book_id': 'two', 'rating': 1})
        self.assertEqual([b['id'] for b in self.store.recommendations()['books']], ['three'])

    def test_same_title_different_author_is_not_excluded(self):
        self.store.import_csv(export([{'Book Id': '1', 'Title': 'One', 'Author': 'Different Writer', 'My Rating': '4', 'Exclusive Shelf': 'read'}]))
        self.assertIn('one', [b['id'] for b in self.store.recommendations()['books']])

    def test_seed_matches_author_component_with_extra_abs_names(self):
        self.abs.items = [item('one', 'One', author='Writer, Narrator Person'), item('two', 'Two', author='Writer, Narrator Person')]
        self.store.sync()
        self.store.import_csv(export([{'Book Id': '1', 'Title': 'One', 'Author': 'Writer', 'My Rating': '5', 'Exclusive Shelf': 'read'}]))
        picks = self.store.recommendations()['books']
        self.assertEqual([b['id'] for b in picks], ['two'])
        self.assertIn('You rated other books by this author highly', picks[0]['reasons'])

    def test_queue_creates_adds_then_retry_deduplicates(self):
        payload = {'library_id': 'lib', 'book_ids': ['one', 'one', 'two']}
        first = self.store.queue_books(payload)
        second = self.store.queue_books(payload)
        self.assertEqual(first['added'], 2)
        self.assertEqual(second['added'], 0)
        self.assertEqual(len(self.abs.writes), 2)
        self.assertEqual(self.abs.writes[1][2], {'items': [{'libraryItemId': 'one'}, {'libraryItemId': 'two'}]})

    def test_queue_rejects_wrong_library_and_owner(self):
        with self.assertRaises(AppError):
            self.store.queue_books({'library_id': 'lib', 'book_ids': ['unknown']})
        self.abs.remote['someone'] = {'id': 'someone', 'userId': 'other', 'libraryId': 'lib', 'name': 'Other', 'items': []}
        with self.assertRaises(AppError):
            self.store.queue_books({'library_id': 'lib', 'book_ids': ['one'], 'playlist_id': 'someone'})
        self.assertEqual(self.abs.writes, [])

    def test_empty_playlist_creation(self):
        result = self.store.queue_books({'library_id': 'lib', 'book_ids': []})
        self.assertEqual(result['added'], 0)
        self.assertEqual(self.store.playlists('lib')[0]['name'], 'AutomaticBookSelector')

    def test_question_dismissal_survives_sync(self):
        self.abs.user['mediaProgress'] = [{'libraryItemId': 'one', 'isFinished': True, 'progress': 1}]
        self.store.sync()
        self.store.dismiss_question('one')
        self.store.sync()
        self.assertEqual(self.store.snapshot()['questions'], [])


class ProtocolTests(unittest.TestCase):
    def test_docker_routes_localhost_to_host(self):
        with patch.dict('os.environ', {'IN_DOCKER': '1'}):
            self.assertEqual(ABS('http://localhost:13378/abs', 'token').url, 'http://host.docker.internal:13378/abs')
            self.assertEqual(ABS('https://server.example/abs', 'token').url, 'https://server.example/abs')

    def test_pagination_fetches_all_and_rejects_truncated(self):
        client = ABS('http://fake', 'token')
        with patch.object(client, 'get', side_effect=[{'results': [{'id': 1}], 'total': 2}, {'results': [{'id': 2}], 'total': 2}]) as get:
            self.assertEqual(len(client.pages('/api/items')), 2)
            self.assertIn('page=1', get.call_args[0][0])
        with patch.object(client, 'get', side_effect=[{'results': [{'id': 1}], 'total': 2}, {'results': [], 'total': 2}]):
            with self.assertRaises(AppError):
                client.pages('/api/items')

    def test_isbn_excel_format_and_title_identity(self):
        self.assertEqual(isbn('="0306406152"'), '9780306406157')
        self.assertEqual(identity('One (Series #1)', 'Míra July'), ('one', 'mira july'))

    def test_credentials_never_embedded_in_url(self):
        with self.assertRaises(AppError):
            ABS('https://user:secret@example.com', 'token')
        with self.assertRaises(AppError):
            ABS('https://example.com?token=secret', 'token')


if __name__ == '__main__':
    unittest.main()
