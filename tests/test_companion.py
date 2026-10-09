import copy
import csv
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from core import Store, AppError
from goodreads import Goodreads, book_url, completed_date, page_metadata, valid_isbn
from telemetry import Telemetry, EventConflict
from test_core import FakeABS, item
import test_http


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.abs = FakeABS()
        self.store = Store(pathlib.Path(self.temp.name) / 'test.db', self.abs)
        self.store.sync()
        self.telemetry = Telemetry(self.store)
        self.gr = Goodreads(self.store)

    def finish(self, book='one', when='2026-10-01T18:00:00+00:00'):
        self.abs.user['mediaProgress'].append({'libraryItemId': book, 'isFinished': True, 'progress': 1, 'finishedAt': when})
        self.store.sync()

    def match(self, book='one', **extra):
        self.gr.save({'book_id': book, 'title': book.title(), 'author': 'Writer', 'isbn': '9798217058785',
                      'completed_date': '2026-10-01', 'public_rating': 4, 'confirmed': True, **extra})

    def session(self, **extra):
        identity = self.telemetry.status()
        return {'schema_version': 1, 'event_id': 'event1', 'session_id': 'session1', 'abs_server_id': identity['abs_server_id'],
                'abs_user_id': identity['abs_user_id'], 'abs_item_id': 'one', 'abs_library_id': 'lib',
                'started_at': '2026-10-01T18:00:00Z', 'ended_at': '2026-10-01T19:00:00Z',
                'source_seconds_listened': 7200, 'wall_clock_seconds': 3600, 'again_count': 2,
                'speed_buckets': [{'speed': 2, 'wall_seconds': 3600}], **extra}

    def connection(self):
        token = self.telemetry.create({'name': 'Test phone'})
        return token, self.telemetry.authenticate({'Authorization': 'Bearer ' + token['token']})

    def test_export_has_completion_date_and_independent_public_rating(self):
        self.finish()
        self.store.feedback({'book_id': 'one', 'rating': 1, 'notes': 'Private note'})
        self.match()
        private = self.store.feedback_book('one')['feedback']
        batch = self.gr.export(['one'])
        row = list(csv.DictReader(io.StringIO(batch['csv'])))[0]
        self.assertEqual(row['Date Read'], '2026-10-01')
        self.assertEqual(row['Shelves'], 'read')
        self.assertEqual(row['My Rating'], '4')
        self.assertNotIn('Private note', batch['csv'])
        self.assertNotIn('My Review', batch['csv'])
        self.assertEqual(self.store.feedback_book('one')['feedback'], private)
        self.assertEqual(self.gr.review()['books'], [])
        with self.assertRaises(AppError):
            self.gr.export(['one'])
        self.assertEqual(self.gr.batch(batch['id'])['csv'], batch['csv'])

    def test_abs_dates_are_real_local_dates_and_missing_dates_block_export(self):
        self.assertEqual(completed_date('2026-10-02T02:00:00Z'), '2026-10-01')
        self.assertEqual(completed_date(1790877600000), '2026-10-01')
        self.finish(when='')
        self.assertFalse(self.gr.review()['books'][0]['ready'])
        with self.assertRaises(AppError):
            self.match(completed_date='')
        self.match(completed_date='', ignored=True)
        self.assertEqual(self.gr.review()['books'], [])

    def test_history_read_titles_and_editions_are_excluded_but_to_read_is_not(self):
        self.abs.items.append(item('edition', 'One (Audio Edition)', 'Writer'))
        self.finish()
        self.finish('edition')
        self.assertEqual(len(self.gr.review()['books']), 1)
        with self.store.db() as db:
            db.execute("INSERT INTO history VALUES ('123','One','Writer','',0,'read','','','',1,'one','writer')")
        self.assertEqual(self.gr.review()['books'], [])
        with self.store.db() as db:
            db.execute("UPDATE history SET shelf='to-read',read_count=0")
        self.assertEqual(len(self.gr.review()['books']), 1)

    def test_failed_rows_return_for_review_and_generic_errors_are_not_guessed(self):
        self.finish()
        self.match()
        batch = self.gr.export(['one'])
        result = self.gr.reconcile({'batch_id': batch['id'], 'text': 'Upload failed'})
        self.assertTrue(result['unrecognized'])
        self.assertEqual(self.gr.review()['books'], [])
        result = self.gr.reconcile({'batch_id': batch['id'], 'text': 'Could not import: One by Writer'})
        self.assertEqual(result['flagged'], ['One'])
        self.assertFalse(self.gr.review()['books'][0]['confirmed'])
        with self.assertRaises(AppError):
            self.gr.export(['one'])
        self.match()
        second = self.gr.export(['one'])
        self.assertNotEqual(second['id'], batch['id'])

    def test_failed_manual_selection_and_other_import_confirmation(self):
        self.finish()
        self.finish('two')
        self.match()
        self.match('two')
        batch = self.gr.export(['one', 'two'])
        self.gr.reconcile({'batch_id': batch['id'], 'text': 'unknown error', 'failed_ids': ['one'], 'confirm_remaining': True})
        self.assertEqual([b['id'] for b in self.gr.review()['books']], ['one'])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT state FROM gr_export_items WHERE book_id='two'").fetchone()[0], 'imported')

    def test_isbn_url_and_public_details_validation(self):
        self.assertEqual(valid_isbn('979-8-217-05878-5'), '9798217058785')
        self.assertEqual(valid_isbn('034541005X'), '034541005X')
        self.assertEqual(valid_isbn('9798217058786'), '')
        for url in ('http://www.goodreads.com/book/show/123', 'https://goodreads.com.evil/book/show/123', 'https://www.goodreads.com:999/book/show/123', 'https://www.goodreads.com/book/show/123?secret=yes', 'https://localhost/book/show/123'):
            with self.assertRaises(AppError): book_url(url)
        self.assertEqual(book_url('https://www.goodreads.com/book/show/123-title')[1], '123')
        for values in ({'title': '=danger'}, {'public_rating': True}, {'public_rating': 6}, {'completed_date': 'not-a-date'}, {'title': {}}, {'isbn': '0123'}):
            with self.assertRaises(AppError): self.match(**values)

    def test_extracts_structured_book_isbn_without_running_page_scripts(self):
        body = '<script type="application/ld+json">' + json.dumps({'@type': 'Book', 'name': 'Gouged', 'author': {'name': 'Lindsay Owens'}, 'isbn': '9798217058785'}) + '</script>'
        self.assertEqual(page_metadata(body), {'isbn': '9798217058785', 'title': 'Gouged', 'author': 'Lindsay Owens'})
        with self.assertRaises(AppError): page_metadata('<html>Sign in or solve a challenge</html>')

    def test_tokens_are_hashed_write_only_and_revocable(self):
        token, connection = self.connection()
        self.assertEqual(connection, token['id'])
        with self.store.db() as db:
            self.assertNotEqual(db.execute('SELECT token_hash FROM bookramp_connections').fetchone()[0], token['token'])
        self.assertNotIn(token['token'], json.dumps(self.telemetry.connections()))
        self.telemetry.revoke({'id': token['id']})
        self.assertIsNone(self.telemetry.authenticate({'Authorization': 'Bearer ' + token['token']}))
        self.assertIsNone(self.telemetry.authenticate({'Authorization': 'Bearer fake'}))

    def test_retries_do_not_double_count_and_session_collisions_roll_back_batch(self):
        _, connection = self.connection()
        session = self.session()
        self.assertEqual(self.telemetry.ingest({'sessions': [session]}, connection)['accepted'], 1)
        self.assertEqual(self.telemetry.ingest({'sessions': [session]}, connection)['duplicates'], 1)
        self.assertEqual(self.telemetry.ingest({'sessions': [session | {'event_id': 'retry-new-event'}]}, connection)['duplicates'], 1)
        with self.assertRaises(EventConflict):
            self.telemetry.ingest({'sessions': [self.session(event_id='new', session_id='new'), session | {'again_count': 99}]}, connection)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bookramp_sessions').fetchone()[0], 1)

    def test_invalid_sessions_rejected_atomically_and_unknown_books_retained(self):
        _, connection = self.connection()
        for changes in ({'schema_version': 2}, {'abs_user_id': 'another'}, {'abs_server_id': 'foreign'}, {'wall_clock_seconds': 9999}, {'source_seconds_listened': float('nan')}, {'again_count': True}, {'ended_at': '2026-09-01T00:00:00Z'}, {'completion_crossed': True}, {'abs_library_id': 'wrong'}, {'speed_buckets': [{'speed': 2, 'wall_seconds': 1}]}):
            with self.assertRaises(AppError): self.telemetry.ingest({'sessions': [self.session(), self.session(event_id='two', session_id='two', **changes)]}, connection)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bookramp_sessions').fetchone()[0], 0)
        self.assertEqual(self.telemetry.ingest({'sessions': [self.session(abs_item_id='not-synced-yet')]}, connection)['accepted'], 1)

    def test_measured_stats_weight_times_and_completion_has_one_count(self):
        _, connection = self.connection()
        first = self.session(completion_crossed=True, completion_timestamp='2026-10-01T18:59:59Z', maximum_sustained_speed=2)
        second = self.session(event_id='two', session_id='two', started_at='2026-10-02T18:00:00Z', ended_at='2026-10-02T18:10:00Z', wall_clock_seconds=600, source_seconds_listened=1800, speed_buckets=[{'speed':3, 'wall_seconds':600}], again_count=1)
        self.telemetry.ingest({'sessions': [second, first]}, connection)
        self.finish()
        stats = self.telemetry.stats('2026-10')
        self.assertEqual(stats['completed'], 1)
        self.assertAlmostEqual(stats['average_speed'], 9000/4200)
        self.assertEqual(stats['median_speed'], 2)
        self.assertEqual(stats['again_count'], 3)
        self.assertAlmostEqual(stats['hours_saved'], 4800/3600)
        self.assertEqual(stats['maximum_sustained_speed'], 2)
        self.assertNotIn('one', {b['id'] for b in self.store.recommendations()['books']})

    def test_unknown_metrics_stay_null_and_abs_does_not_invent_listening_hours(self):
        self.finish()
        stats = self.telemetry.stats('2026-10')
        self.assertEqual(stats['completed'], 1)
        self.assertIsNone(stats['source_hours'])
        _, connection = self.connection()
        self.telemetry.ingest({'sessions': [self.session(source_seconds_listened=None, wall_clock_seconds=None, speed_buckets=None, again_count=None)]}, connection)
        stats = self.telemetry.stats('2026-10')
        self.assertIsNone(stats['actual_hours'])
        self.assertIsNone(stats['median_speed'])
        self.assertIsNone(stats['again_count'])

    def test_chicago_month_boundary_uses_session_time_not_upload_time(self):
        _, connection = self.connection()
        event = self.session(started_at='2026-10-01T00:00:00Z', ended_at='2026-10-01T01:00:00Z')
        self.telemetry.ingest({'sessions': [event]}, connection)
        self.assertEqual(self.telemetry.stats('2026-09')['session_count'], 1)
        self.assertEqual(self.telemetry.stats('2026-10')['session_count'], 0)
        with self.assertRaises(AppError): self.telemetry.stats('2026-13')


class ReceiverHTTPTests(test_http.HTTPTests):
    def test_authenticated_card_download_is_a_real_png_attachment(self):
        status, headers, body = self.request('/api/card.png?month=2026-10')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertTrue(body.startswith(b'\x89PNG\r\n\x1a\n'))
        from PIL import Image
        image = Image.open(io.BytesIO(body))
        self.assertEqual(image.size, (1080, 900))
        self.assertEqual(self.request('/api/card.png?month=2026-10', headers={'Host':'attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/card.png?month=../../bad')[0], 400)

    def test_csv_attachment_preserves_utf8_dates_and_public_fields(self):
        from goodreads import Goodreads
        gr = Goodreads(self.store)
        with self.store.db() as db:
            db.execute("UPDATE books SET finished=1,finished_at='2026-10-01T18:00:00Z' WHERE id='one'")
        gr.save({'book_id':'one', 'title':'One, a story', 'author':'Writer', 'isbn':'9798217058785', 'completed_date':'2026-10-01', 'confirmed':True})
        batch = gr.export(['one'])
        status, headers, body = self.request('/api/goodreads/download?id=' + batch['id'])
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        row = list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))[0]
        self.assertEqual(row['Date Read'], '2026-10-01')
        self.assertEqual(row['Title'], 'One, a story')

    def test_machine_token_cannot_read_reviews_or_make_admin_changes(self):
        self.assertEqual(self.request('/api/v1/bookramp/status')[0], 401)
        headers = {'X-Selector-Request': '1'}
        status, _, body = self.request('/api/bookramp/create', {'name': 'Phone'}, headers)
        self.assertEqual(status, 200)
        token = json.loads(body)['token']
        machine = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.request('/api/v1/bookramp/status', headers=machine)[0], 200)
        self.assertEqual(self.request('/api/v1/bookramp/sessions', {'sessions': []}, machine)[0], 400)
        self.assertEqual(self.request('/api/v1/bookramp/sessions', {'sessions': []}, machine | {'Origin':'http://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/bookramp/create', {'name': 'Unauthorized'}, machine)[0], 403)
        public = machine | {'Host': 'selector.example.com'}
        self.assertEqual(self.request('/api/reviews', headers=public)[0], 403)

    def test_new_private_endpoints_require_existing_access_auth(self):
        for path in ('/api/goodreads', '/api/goodreads/batch?id=x', '/api/stats?month=2026-10', '/api/bookramp/connections'):
            self.assertEqual(self.request(path, headers={'Host':'attacker.example'})[0], 403)


if __name__ == '__main__':
    unittest.main()
