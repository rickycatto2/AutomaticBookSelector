import csv
import io
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from core import Store, AppError
from goodreads import Goodreads, valid_asin, valid_identifier, valid_isbn
from telemetry import Telemetry
from test_core import FakeABS


class IdentifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / 'test.db'
        self.abs = FakeABS()
        self.abs.items[0]['media']['metadata']['asin'] = 'B0CHYSM5M4'
        self.abs.user['mediaProgress'] = [{'libraryItemId': 'one', 'isFinished': True, 'progress': 1, 'finishedAt': '2026-10-01T18:00:00Z'}]
        self.store = Store(self.path, self.abs)
        self.store.sync()
        Telemetry(self.store)
        self.gr = Goodreads(self.store)

    def save(self, code='B0CHYSM5M4', **extra):
        self.gr.save({'book_id': 'one', 'title': 'One', 'author': 'Writer', 'isbn': code,
                      'completed_date': '2026-10-01', 'confirmed': True, **extra})

    def test_migration_and_blank_resync(self):
        with self.store.db() as db:
            db.execute('ALTER TABLE books DROP COLUMN asin')
        migrated = Store(self.path, self.abs)
        with migrated.db() as db:
            self.assertEqual(db.execute("SELECT asin FROM books WHERE id='one'").fetchone()[0], '')
        migrated.sync()
        for value in ('', '   ', None, 123):
            self.abs.items[0]['media']['metadata']['asin'] = value
            migrated.sync()
            with migrated.db() as db:
                self.assertEqual(db.execute("SELECT asin FROM books WHERE id='one'").fetchone()[0], 'B0CHYSM5M4')

    def test_syntax_and_unchanged_checksums(self):
        self.assertEqual(valid_identifier('B0CHYSM5M4'), 'B0CHYSM5M4')
        for code in ('B0CHYSM5M', 'B0CHYSM5M44', 'b0CHYSM5M4', ' B0CHYSM5M4', 'B0CHYSM5M4\n', 'B0CHYSM5M!', 'B0CHYSM\x005M4'):
            self.assertEqual(valid_asin(code), '')
            self.assertEqual(valid_identifier(code), '')
        for code in ('9780306406157', '0306406152'):
            self.assertEqual(valid_identifier(code), code)
        for code in ('9780306406158', '0306406153', 'B0CHYSM5M4'):
            self.assertEqual(valid_isbn(code), '')

    def test_review_save_export_six_columns_and_confirmation(self):
        row = self.gr.review()['books'][0]
        self.assertEqual(row['isbn'], 'B0CHYSM5M4')
        self.assertIn('ABS-supplied ASIN', row['provenance'])
        self.assertFalse(row['ready'])
        self.save(confirmed=False)
        self.assertFalse(self.gr.review()['books'][0]['ready'])
        self.save()
        self.assertTrue(self.gr.review()['books'][0]['ready'])
        rows = list(csv.reader(io.StringIO(self.gr.export(['one'])['csv'])))
        self.assertEqual(rows[0], ['Title', 'Author', 'ISBN', 'My Rating', 'Date Read', 'Shelves'])
        self.assertEqual(rows[1], ['One', 'Writer', 'B0CHYSM5M4', '', '2026-10-01', 'read'])

    def test_asin_auto_resolves_without_provider(self):
        with patch.object(self.gr.catalog, 'resolve', side_effect=AssertionError('No provider needed')):
            self.assertTrue(self.gr.resolve({'book_ids': ['one']})['results'][0]['automatic'])
        self.assertFalse(self.gr.review()['books'][0]['ready'])  # suggestion requires explicit Save

    def test_saved_isbn_never_overwritten(self):
        self.save('9780306406157')
        candidate = {'title': 'One', 'author': 'Writer', 'isbn': '9798217058785'}
        with patch.object(self.gr.catalog, 'resolve', return_value={'candidate': candidate, 'high_confidence': True, 'reason': 'Synthetic', 'provenance': 'Catalog'}):
            self.assertFalse(self.gr.resolve({'book_ids': ['one'], 'title': 'One', 'author': 'Writer'})['results'][0]['automatic'])
        self.assertEqual(self.gr.review()['books'][0]['isbn'], '9780306406157')

    def test_asin_preferred_over_prior_automatic_catalog_isbn(self):
        self.save('9780306406157')
        with self.store.db() as db:
            db.execute("UPDATE gr_match_context SET automatic=1 WHERE book_id='one'")
        self.assertEqual(self.gr.review()['books'][0]['isbn'], 'B0CHYSM5M4')
        # The stored catalog decision is retained, not destructively migrated.
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT isbn FROM gr_matches WHERE book_id='one'").fetchone()[0], '9780306406157')

    def test_asin_duplicate_history_and_export(self):
        with self.store.db() as db:
            db.execute("INSERT INTO history VALUES ('123','Different','Other','B0CHYSM5M4',0,'read','','','',1,'different','other')")
        self.assertEqual(self.gr.review()['books'], [])
        with self.store.db() as db:
            db.execute('DELETE FROM history')
        self.save()
        self.gr.export(['one'])
        with self.store.db() as db:
            db.execute("UPDATE books SET finished=1,finished_at='2026-10-01',asin='B0CHYSM5M4' WHERE id='two'")
        self.assertEqual({b['id'] for b in self.gr.review()['books']}, {'one', 'two'})

    def test_formula_guard_and_missing_date(self):
        for code in ('=B0CHYSM5M4', '+B0CHYSM5M4', '@B0CHYSM5M4', '-B0CHYSM5M4'):
            with self.assertRaises(AppError):
                self.save(code)
        with self.assertRaises(AppError):
            self.save(completed_date='')

    def test_explicit_edited_query_and_batch_source(self):
        result = {'high_confidence': False, 'candidate': None, 'reason': 'Synthetic', 'provenance': 'Catalog'}
        with self.store.db() as db:
            db.execute("UPDATE books SET asin='' WHERE id='one'")
        with patch.object(self.gr.catalog, 'resolve', return_value=result) as lookup:
            self.gr.resolve({'book_ids': ['one'], 'title': ' Edited title ', 'author': ' Edited author '})
            self.assertEqual(lookup.call_args.args[0]['title'], 'Edited title')
            self.assertEqual(lookup.call_args.args[0]['author'], 'Edited author')
            self.assertEqual(self.gr.review()['books'][0]['title'], 'One')
            job = self.gr.recheck_books()['job']
            self.gr.resolve({'book_ids': ['one'], 'batch_id': job['id'], 'title': 'Ignored', 'author': 'Ignored'})
            self.assertEqual(lookup.call_args.args[0]['title'], 'One')
            self.assertEqual(lookup.call_args.args[0]['author'], 'Writer')
        for title in ('=Formula', 'bad\x00title', ''):
            with self.assertRaises(AppError):
                self.gr.resolve({'book_ids': ['one'], 'title': title, 'author': 'Writer'})

    def test_asin_source_change_invalidates_confirmation(self):
        self.save()
        with self.store.db() as db:
            db.execute("UPDATE books SET asin='B012345678' WHERE id='one'")
        row = self.gr.review()['books'][0]
        self.assertTrue(row['source_changed'])
        self.assertFalse(row['ready'])
        self.assertEqual(row['isbn'], 'B0CHYSM5M4')

    def test_asin_failed_import_table_returns_for_review(self):
        self.save()
        batch = self.gr.export(['one'])
        result = self.gr.reconcile({'batch_id': batch['id'], 'text': '1 book could not be imported:\nISBN\tTitle\tAuthor\nB0CHYSM5M4\t\tWriter'})
        self.assertEqual(result['flagged'], ['One'])
        self.assertFalse(self.gr.review()['books'][0]['ready'])
