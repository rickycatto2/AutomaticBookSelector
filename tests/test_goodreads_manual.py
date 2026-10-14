"""Manual-assisted workflow: synthetic ABS, isolated database, no live providers."""
import csv
import io
import unittest
from unittest.mock import patch

from core import AppError
import test_goodreads_identifiers as fixtures


class ManualWorkflowTests(unittest.TestCase):
    setUp = fixtures.IdentifierTests.setUp
    save = fixtures.IdentifierTests.save

    def test_identifiers_and_formula_guard(self):
        for code in ('B0CHYSM5M4', '9780306406157', '0306406152', '978-0-306-40615-7'):
            self.save(code)
            batch = self.gr.export(['one'])
            rows = list(csv.reader(io.StringIO(batch['csv'])))
            self.assertEqual(len(rows[0]), 6)
            self.assertEqual(len(rows[1]), 6)
            self.assertEqual(rows[1][2], code.replace('-', ''))
        for code in ('9780306406158', 'b0CHYSM5M4', 'B0CHYSM5M!', 'B0CHYSM5', '=1+1', 'B0CHYSM5M4\n'):
            with self.subTest(code=code), self.assertRaisesRegex(AppError, 'ISBN-10/13.*ASIN'):
                self.save(code)

    def test_five_public_stars_never_affect_taste_or_recommendations(self):
        self.store.feedback({'book_id': 'one', 'rating': 1, 'notes': 'Private dislike'})
        before_profile = self.store.profile()
        before_rank = self.store._rank()  # force actual computation, not cached recommendations
        before_feedback = self.store.feedback_book('one')
        with self.store.db() as db:
            before_history = [tuple(r) for r in db.execute('SELECT * FROM history')]
        self.save(public_rating=5)
        self.assertEqual(self.store.profile(), before_profile)
        self.assertEqual(self.store._rank(), before_rank)
        self.store.cache = None
        self.assertEqual(self.store.recommendations()['books'], before_rank)
        self.assertEqual(self.store.feedback_book('one'), before_feedback)
        with self.store.db() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM history')], before_history)
        self.assertEqual(list(csv.DictReader(io.StringIO(self.gr.export(['one'])['csv'])))[0]['My Rating'], '5')

    def test_unconfirmed_exports_stay_queued_until_explicit_confirmation(self):
        self.save()
        old = self.gr.export(['one'])
        self.assertEqual(self.gr.review()['books'][0]['import_state'], 'exported-unconfirmed')
        newer = self.gr.export(['one'])
        with self.assertRaises(AppError):
            self.gr.confirm_imported({'batch_id': old['id'], 'book_id': 'one'})
        self.gr.confirm_imported({'batch_id': newer['id'], 'book_id': 'one'})
        self.assertEqual(self.gr.review()['books'], [])
        self.assertEqual(self.gr.batch(old['id'])['csv'], old['csv'])

    def test_failed_isbn_and_asin_return_with_reason(self):
        for code in ('9780306406157', 'B0CHYSM5M4'):
            self.save(code)
            batch = self.gr.export(['one'])
            report = 'Failed: ' + code
            self.gr.reconcile({'batch_id': batch['id'], 'text': report})
            book = self.gr.review()['books'][0]
            self.assertEqual(book['import_state'], 'import-failed')
            self.assertEqual(book['failure'], report)
            self.assertFalse(book['ready'])

    def test_individual_return_from_old_history_preserves_saved_details_and_rows(self):
        self.save(title='Edited One', author='Full Author, Coauthor', public_rating=5,
                  url='https://www.goodreads.com/book/show/123')
        old = self.gr.export(['one'])
        newer = self.gr.export(['one'])
        with self.store.db() as db:
            before = dict(db.execute("SELECT * FROM gr_matches WHERE book_id='one'").fetchone())
        self.gr.return_to_review({'batch_id': old['id'], 'book_id': 'one'})
        with self.store.db() as db:
            after = dict(db.execute("SELECT * FROM gr_matches WHERE book_id='one'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM gr_exports').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM gr_export_items').fetchone()[0], 2)
        self.assertEqual(after, before | {'confirmed': 0})
        self.assertEqual(self.gr.review()['books'][0]['import_state'], 'returned-to-review')
        self.assertEqual(self.gr.batch(old['id'])['csv'], old['csv'])
        self.assertEqual(self.gr.batch(newer['id'])['csv'], newer['csv'])
        self.assertEqual(self.gr.batch_items(old['id'])['items'][0]['state'], 'exported')

    def test_return_one_title_does_not_reset_batch_and_rejects_foreign_id(self):
        self.save()
        with self.store.db() as db:
            db.execute("UPDATE books SET finished=1,finished_at='2026-10-01' WHERE id='two'")
        self.gr.save({'book_id': 'two', 'title': 'Two', 'author': 'Writer', 'isbn': '9780306406157',
                      'completed_date': '2026-10-01', 'confirmed': True})
        batch = self.gr.export(['one', 'two'])
        self.gr.return_to_review({'batch_id': batch['id'], 'book_id': 'one'})
        self.assertEqual({b['id']: b['state'] for b in self.gr.batch_items(batch['id'])['items']},
                         {'one': 'review', 'two': 'exported'})
        with self.assertRaises(AppError):
            self.gr.return_to_review({'batch_id': batch['id'], 'book_id': 'foreign'})

    def test_manual_identifier_wins_over_bounded_matching(self):
        job = self.gr.start_match_batch()['job']
        self.save('9780306406157')
        with patch.object(self.gr.catalog, 'resolve', side_effect=AssertionError('No provider call')):
            result = self.gr.resolve({'book_ids': ['one'], 'batch_id': job['id']})
        self.assertEqual(result['job']['items'][0]['state'], 'skipped')
        self.assertEqual(self.gr.review()['books'][0]['isbn'], '9780306406157')

    def test_success_and_cross_batch_safety_retained(self):
        self.save()
        batch = self.gr.export(['one'])
        with self.assertRaises(AppError):
            self.gr.reconcile({'batch_id': batch['id'], 'text': 'Imported: B0CHYSM5M4', 'failed_ids': ['one']})
        with self.assertRaises(AppError):
            self.gr.reconcile({'batch_id': batch['id'], 'failed_ids': ['foreign']})
        self.gr.reconcile({'batch_id': batch['id'], 'text': 'unrecognised prose', 'confirm_remaining': True})
        self.assertEqual(self.gr.batch_items(batch['id'])['items'][0]['state'], 'exported')
