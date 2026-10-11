import csv
import io
import json
import pathlib
import tempfile
import unittest

from core import AppError, Store, canonical_author, isbn, normalize
from goodreads import Goodreads, page_metadata
from telemetry import Telemetry
from test_core import FakeABS, item


class MetadataIntegrityTests(unittest.TestCase):
    def test_known_author_is_not_proof_of_other_contributors_roles(self):
        for authors in ('Veronica Roth; Coauthor Person', 'Veronica Roth, Teresa Englert - translator', 'Veronica Roth & Coauthor Person'):
            self.assertEqual(canonical_author(authors, {normalize('Veronica Roth')}), normalize(authors))

    def test_sync_does_not_turn_bad_isbn_into_valid_identity(self):
        for code in ('0306406153', 'X306406152', '9780306406158', '9798217058786'):
            self.assertEqual(isbn(code), '')
        self.assertEqual(isbn('="0306406152"'), '9780306406157')
        self.assertEqual(isbn('034541005X'), '9780345410054')

    def test_lookup_requires_manual_review_of_multiple_book_nodes(self):
        book = {'@type': 'Book', 'name': 'The Sixth Faction', 'author': [{'name': 'Veronica Roth'}, {'name': 'Coauthor Person'}], 'isbn': '9780306406157'}
        def page(nodes):
            return '<script type="application/ld+json">' + json.dumps(nodes) + '</script>'
        self.assertEqual(page_metadata(page([book, book]))['author'], 'Veronica Roth, Coauthor Person')
        for change in ({'name': 'Related book', 'isbn': '9798217058785'}, {'isbn': '9798217058785'}, {'author': {'name': 'Different Writer'}}):
            with self.subTest(change=change), self.assertRaises(AppError):
                page_metadata(page([book, book | change]))

    def test_abs_sync_review_and_csv_preserve_contributors_until_explicit_review(self):
        with tempfile.TemporaryDirectory() as temp:
            remote = FakeABS()
            remote.items = [item('edition', 'The Sixth Faction', 'Veronica Roth, Coauthor Person')]
            meta = remote.items[0]['media']['metadata']
            meta.update(narratorName='Emma Love, Charlie Thurston, Max Meyers', isbn='9780306406157')
            remote.user['mediaProgress'] = [{'libraryItemId': 'edition', 'isFinished': True, 'progress': 1, 'finishedAt': '2026-10-01T18:00:00Z'}]
            store = Store(pathlib.Path(temp) / 'test.db', remote)
            store.sync()
            Telemetry(store)
            gr = Goodreads(store)
            row = gr.review()['books'][0]
            self.assertEqual(row['original_author'], 'Veronica Roth, Coauthor Person')
            self.assertFalse(row['ready'])
            with self.assertRaises(AppError):
                gr.export(['edition'])
            payload = {'book_id': 'edition', 'title': row['title'], 'author': row['author'], 'isbn': row['isbn'], 'completed_date': '2026-10-01', 'confirmed': True}
            for flag in ('confirmed', 'ignored'):
                for value in ('false', 'true', 0, 1, None):
                    with self.subTest(flag=flag, value=value), self.assertRaises(AppError):
                        gr.save(payload | {flag: value})
            gr.save(payload)
            exported = list(csv.DictReader(io.StringIO(gr.export(['edition'])['csv'])))[0]
            self.assertEqual(exported['Author'], 'Veronica Roth, Coauthor Person')
            self.assertEqual(exported['ISBN'], '9780306406157')
            self.assertNotIn('Emma Love', exported['Author'])
            self.assertEqual(remote.writes, [])
