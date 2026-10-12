"""Synthetic reports, throwaway DBs only; no external Goodreads access."""
import unittest

from core import AppError
from goodreads import import_report
import test_companion


class ReconcileTests(unittest.TestCase):
    setUp = test_companion.CompanionTests.setUp
    finish = test_companion.CompanionTests.finish
    match = test_companion.CompanionTests.match

    def batch(self, same_title=False):
        for book in ('one', 'two'):
            self.finish(book)
            self.match(book, title='Same' if same_title else book.title(),
                       author='Writer' if book == 'one' else 'Other Writer',
                       isbn='9780306406157' if book == 'one' else '9798217058785')
        return self.gr.export(['one', 'two'])

    def states(self, batch):
        return {b['id']: b['state'] for b in self.gr.batch_items(batch['id'])['items']}

    def test_success_and_mixed_report_preview_then_apply(self):
        batch = self.batch()
        payload = {'batch_id': batch['id'], 'text': 'Imported: 9780306406157\nFailed: 9798217058785', 'confirm_remaining': True}
        result = self.gr.reconcile(payload | {'preview': True})
        self.assertTrue(result['mixed'])
        self.assertEqual(result['flagged'], ['Two'])
        self.assertEqual(self.states(batch), {'one': 'exported', 'two': 'exported'})
        self.gr.reconcile(payload)
        self.assertEqual(self.states(batch), {'one': 'imported', 'two': 'failed'})
        self.assertEqual([b['id'] for b in self.gr.review()['books']], ['two'])
        self.assertEqual(self.gr.batch(batch['id'])['csv'], batch['csv'])

    def test_actual_goodreads_blank_title_report_requeues_without_csv_changes(self):
        batch = self.batch()
        with self.store.db() as db:
            db.execute("UPDATE gr_export_items SET title='Cleat Cute',author='Dana Varden, Zim Avaltrades',isbn='9781250292896' WHERE book_id='one'")
        text = '1 book could not be imported:\nISBN\tTitle\tAuthor\n9781250292896\t\tDana Varden, Zim Avaltrades'
        payload = {'batch_id': batch['id'], 'text': text, 'confirm_remaining': True, 'failed_ids': []}
        preview = self.gr.reconcile(payload | {'preview': True})
        self.assertEqual(preview['flagged'], ['Cleat Cute'])
        self.assertFalse(preview['ambiguous'])
        self.assertEqual(self.states(batch)['one'], 'exported')
        self.gr.reconcile(payload)
        self.assertEqual(self.states(batch), {'one': 'failed', 'two': 'imported'})
        self.assertEqual([b['id'] for b in self.gr.review()['books']], ['one'])
        self.assertFalse(self.gr.review()['books'][0]['ready'])
        self.assertEqual(self.gr.batch(batch['id'])['csv'], batch['csv'])

    def test_table_count_malformed_shared_and_success_rows_are_conservative(self):
        items = [{'book_id': 'one', 'title': 'One', 'author': 'Writer', 'isbn': '9781250292896'}]
        header = '1 book could not be imported:\nISBN\tTitle\tAuthor\n'
        self.assertTrue(import_report(header + '9781250292896\t\tWriter', items)[0])
        for text in (header + 'No errors for ISBN 9781250292896',
                     header + '9781250292896\tWriter',
                     header + 'Imported: 9781250292896'):
            failed, _, _, ambiguous = import_report(text, items)
            self.assertFalse(failed)
            self.assertTrue(ambiguous)
        self.assertTrue(import_report(header.replace('1 book', '2 books') + '9781250292896\t\tWriter', items)[3])
        self.assertFalse(import_report(header + '9781250292896\t\tWriter', items + [items[0] | {'book_id': 'two'}])[0])

    def test_latest_failed_state_not_masked_by_older_export_record(self):
        batch = self.batch()
        with self.store.db() as db:
            db.execute("INSERT INTO gr_exports VALUES ('synthetic-later','2026-10-12','retained')")
            db.execute("INSERT INTO gr_export_items SELECT 'synthetic-later',book_id,title_key,title,author,isbn,'failed','Rejected' FROM gr_export_items WHERE book_id='one'")
        self.assertEqual([b['id'] for b in self.gr.review()['books']], ['one'])
        self.assertEqual(self.states(batch)['one'], 'exported')

    def test_failed_abs_id_kept_visible_ahead_of_newer_duplicate_completion(self):
        self.finish('one')
        self.match('one')
        batch = self.gr.export(['one'])
        self.abs.items[1]['media']['metadata']['title'] = 'One'
        self.finish('two', '2026-10-02T18:00:00Z')
        self.gr.reconcile({'batch_id': batch['id'], 'failed_ids': ['one']})
        self.assertEqual([b['id'] for b in self.gr.review()['books']], ['one'])

    def test_no_context_unknown_empty_and_nonbool_do_not_import(self):
        batch = self.batch()
        for text in ('', '   ', '9780306406157', 'One by Writer', 'Upload failed', 'unknown format',
                     'No errors: 9780306406157', 'No books failed: 9780306406157',
                     'ISBN 9780306406157 had no errors'):
            result = self.gr.reconcile({'batch_id': batch['id'], 'text': text, 'confirm_remaining': True})
            self.assertFalse(result['can_confirm_remaining'])
            self.assertEqual(self.states(batch), {'one': 'exported', 'two': 'exported'})
        for value in ('false', 1, None, [], {}):
            with self.assertRaises(AppError):
                self.gr.reconcile({'batch_id': batch['id'], 'text': 'Failed: 9780306406157', 'confirm_remaining': value})
        with self.assertRaises(AppError):
            self.gr.reconcile({'batch_id': batch['id'], 'preview': 'true'})

    def test_exact_identity_not_substrings_or_isbn_prefixes(self):
        items = [{'book_id': 'short', 'title': 'It', 'author': 'Smith', 'isbn': '9780306406157'}]
        for text in ('Failed: Little by Smithson', 'Failed: It by Smithson',
                     'Failed: It grows by Smith', 'Failed: 97803064061570', 'Failed: 9780306406157 0',
                     'Failed: 9780306406157-extra', 'Failed: x9780306406157'):
            self.assertEqual(import_report(text, items)[0], set(), text)
        self.assertEqual(import_report('Failed: It by Smith', items)[0], {'short'})

    def test_isbn_equivalence_hyphens_checksums_and_sections(self):
        items = [{'book_id': 'one', 'title': 'One', 'author': 'Writer', 'isbn': '9780306406157'}]
        for text in ('Failed: 0306406152', 'Failed: 0-306-40615-2',
                     'Failed: 978-0-306-40615-7', 'Failed books\nOne by Writer'):
            self.assertEqual(import_report(text, items)[0], {'one'}, text)
        self.assertEqual(import_report('Failed: 0306406153', items)[0], set())
        self.assertEqual(import_report('Failed books\nNo errors for ISBN 9780306406157', items)[0], set())
        reverse = [items[0] | {'isbn': '0306406152'}]
        self.assertEqual(import_report('Failed: 9780306406157', reverse)[0], {'one'})
        self.assertEqual(import_report('Failed books\nOne by Writer\nImported books\nOne by Writer', items)[0], set())

    def test_success_never_failed_conflicts_ambiguous(self):
        batch = self.batch()
        for text in ('Imported: 9780306406157', 'Failed: 9780306406157\nImported: 9780306406157',
                     'Failed: 9780306406157; Imported: 9780306406157'):
            result = self.gr.reconcile({'batch_id': batch['id'], 'text': text})
            self.assertEqual(result['flagged'], [])
            self.assertEqual(self.states(batch)['one'], 'exported')
            with self.assertRaises(AppError):
                self.gr.reconcile({'batch_id': batch['id'], 'text': text, 'failed_ids': ['one']})

    def test_duplicate_title_manual_ids_and_atomic_validation(self):
        batch = self.batch(same_title=True)
        for ids in (['one', 'foreign'], ['one', 'one'], [None], [{}]):
            with self.assertRaises(AppError):
                self.gr.reconcile({'batch_id': batch['id'], 'text': 'Failed: 9780306406157', 'failed_ids': ids})
            self.assertEqual(self.states(batch), {'one': 'exported', 'two': 'exported'})
        self.gr.reconcile({'batch_id': batch['id'], 'failed_ids': ['one', 'two']})
        self.assertEqual(self.states(batch), {'one': 'failed', 'two': 'failed'})
        self.assertEqual({b['id'] for b in self.gr.review()['books']}, {'one', 'two'})

    def test_partial_ambiguous_report_blocks_remaining_until_manual(self):
        batch = self.batch()
        payload = {'batch_id': batch['id'], 'text': 'Failed: 9780306406157\nUnknown second row', 'confirm_remaining': True}
        result = self.gr.reconcile(payload)
        self.assertTrue(result['ambiguous'])
        self.assertEqual(self.states(batch), {'one': 'failed', 'two': 'exported'})
        self.gr.reconcile(payload | {'failed_ids': ['one']})
        self.assertEqual(self.states(batch)['two'], 'imported')

    def test_shared_isbn_requires_selection_and_bounded_reasons(self):
        batch = self.batch()
        with self.store.db() as db:
            db.execute('UPDATE gr_export_items SET isbn=? WHERE batch_id=?', ('9780306406157', batch['id']))
        result = self.gr.reconcile({'batch_id': batch['id'], 'text': 'Failed: 9780306406157', 'confirm_remaining': True})
        self.assertTrue(result['ambiguous'])
        self.assertEqual(result['flagged'], [])
        self.gr.reconcile({'batch_id': batch['id'], 'text': 'Failed: 9780306406157 ' + 'x' * 1000, 'failed_ids': ['one']})
        item = self.gr.batch_items(batch['id'])['items'][0]
        self.assertLessEqual(len(item['failure']), 500)
        self.assertNotIn('x' * 100, item['failure'])  # ambiguous report is not persisted wholesale

    def test_retry_identity_and_old_batch_cannot_invalidate_new_export(self):
        batch = self.batch()
        self.gr.reconcile({'batch_id': batch['id'], 'failed_ids': ['one']})
        self.match('one', isbn='9780306406157')
        # Replaying the failed result must not unconfirm the newly reviewed match.
        self.gr.reconcile({'batch_id': batch['id'], 'failed_ids': ['one']})
        self.assertTrue(self.gr.review()['books'][0]['ready'])
        retry = self.gr.export(['one'])
        with self.assertRaises(AppError):
            self.gr.reconcile({'batch_id': batch['id'], 'failed_ids': ['two', 'one']})
        self.assertEqual(self.states(batch)['two'], 'exported')
        self.assertEqual(self.states(retry)['one'], 'exported')
        self.assertEqual(self.gr.batch(batch['id'])['csv'], batch['csv'])


if __name__ == '__main__':
    unittest.main()
