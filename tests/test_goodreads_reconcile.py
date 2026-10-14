"""Synthetic reports, throwaway DBs only; no external Goodreads access."""
import unittest

from core import AppError
from goodreads import import_report
import test_companion
from test_core import item


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

    def test_observed_17_row_export_16_blank_title_rejections(self):
        # Observed report shape/identifiers; other identifiers and personal
        # titles/authors are synthetic. No user library or network access.
        identifiers = ['B0FS8Y2G2T', '0593214218', '0306406152'] + [
            f'B0TEST{i:04d}' for i in range(13)]
        ids = [f'rejected-{i}' for i in range(16)] + ['accepted']
        self.abs.items.extend(item(book, book.title(), 'Writer ' + str(i))
                              for i, book in enumerate(ids))
        self.store.sync()
        for i, book in enumerate(ids):
            self.finish(book)
            self.match(book, author='Writer ' + str(i),
                       isbn=identifiers[i] if i < 16 else '9798217058785')
        batch = self.gr.export(ids)
        self.assertEqual(batch['count'], 17)
        for separator in ('\t\t', '  ', ' '):
            # Goodreads can report ISBN-13 for an exported ISBN-10.
            reported = identifiers[:]
            reported[2] = '9780306406157'
            text = '16 books could not be imported:\nISBN Title Author\n' + '\n'.join(
                code + separator + 'Writer ' + str(i) for i, code in enumerate(reported))
            text += '\nImported: Accepted by Writer 16'
            payload = {'batch_id': batch['id'], 'text': text, 'confirm_remaining': True}
            preview = self.gr.reconcile(payload | {'preview': True})
            self.assertEqual(len(preview['flagged']), 16, separator)
            self.assertFalse(preview['ambiguous'], separator)
            self.assertTrue(preview['can_confirm_remaining'])
            self.assertTrue(preview['mixed'])
            self.assertEqual(set(self.states(batch).values()), {'exported'})
        self.gr.reconcile(payload)
        self.assertEqual(self.states(batch), {book: 'failed' for book in ids[:-1]} | {'accepted': 'imported'})
        review = self.gr.review()['books']
        self.assertEqual({b['id'] for b in review}, set(ids[:-1]))
        self.assertTrue(all(not b['ready'] and not b['confirmed'] for b in review))
        self.assertEqual(self.gr.batch(batch['id'])['csv'], batch['csv'])

    def test_asin_context_boundaries_success_and_ambiguity(self):
        entry = {'book_id': 'one', 'title': 'One', 'author': 'Writer', 'isbn': 'B0FS8Y2G2T'}
        header = '1 book could not be imported:\nISBN Title Author\n'
        for text in ('Failed: B0FS8Y2G2T', 'Failed books\nASIN: B0FS8Y2G2T',
                     header + 'B0FS8Y2G2T\t\tWriter', header + 'B0FS8Y2G2T  Writer'):
            self.assertEqual(import_report(text, [entry])[0], {'one'}, text)
        for text in ('B0FS8Y2G2T', 'Failed: xB0FS8Y2G2T', 'Failed: B0FS8Y2G2T-extra',
                     'Failed: b0fs8y2g2t', header + 'B0FS8Y2G2T no errors for Writer',
                     header + 'B0FS8Y2G2T Imported: Writer', header + 'B0FOREIGN1  Writer'):
            failed, _, _, ambiguous = import_report(text, [entry])
            self.assertFalse(failed, text)
            self.assertTrue(ambiguous, text)
        for text in ('Imported: B0FS8Y2G2T', header + 'Imported: B0FS8Y2G2T',
                     'Failed: B0FS8Y2G2T\nImported: B0FS8Y2G2T',
                     'Failed: B0FS8Y2G2T; Imported: B0FS8Y2G2T'):
            failed, succeeded, _, _ = import_report(text, [entry])
            self.assertFalse(failed)
            self.assertEqual(succeeded, {'one'})
        self.assertFalse(import_report(header + 'B0FS8Y2G2T  Writer',
                                      [entry, entry | {'book_id': 'two'}])[0])

    def test_asin_unknown_foreign_success_and_partial_report_gates(self):
        batch = self.batch()
        with self.store.db() as db:
            db.execute("UPDATE gr_export_items SET isbn='B0FS8Y2G2T' WHERE book_id='one'")
        for text in ('B0FS8Y2G2T', 'Failed: B0FOREIGN1', 'unknown text'):
            result = self.gr.reconcile({'batch_id': batch['id'], 'text': text, 'confirm_remaining': True})
            self.assertFalse(result['can_confirm_remaining'])
            self.assertEqual(set(self.states(batch).values()), {'exported'})
        with self.assertRaises(AppError):
            self.gr.reconcile({'batch_id': batch['id'], 'text': 'Imported: B0FS8Y2G2T', 'failed_ids': ['one']})
        payload = {'batch_id': batch['id'], 'text': 'Failed: B0FS8Y2G2T\nunknown row', 'confirm_remaining': True}
        result = self.gr.reconcile(payload)
        self.assertTrue(result['ambiguous'])
        self.assertEqual(self.states(batch), {'one': 'failed', 'two': 'exported'})
        self.gr.reconcile(payload | {'failed_ids': ['one']})
        self.assertEqual(self.states(batch), {'one': 'failed', 'two': 'imported'})

    def test_latest_failed_state_not_masked_by_older_export_record(self):
        batch = self.batch()
        with self.store.db() as db:
            db.execute("INSERT INTO gr_exports VALUES ('synthetic-later','2026-10-12','retained')")
            db.execute("INSERT INTO gr_export_items SELECT 'synthetic-later',book_id,title_key,title,author,isbn,'failed','Rejected' FROM gr_export_items WHERE book_id='one'")
        self.assertEqual([b['id'] for b in self.gr.review()['books']], ['one', 'two'])
        self.assertEqual(self.states(batch)['one'], 'exported')

    def test_failed_abs_id_kept_visible_ahead_of_newer_duplicate_completion(self):
        self.finish('one')
        self.match('one')
        batch = self.gr.export(['one'])
        self.abs.items[1]['media']['metadata']['title'] = 'One'
        self.finish('two', '2026-10-02T18:00:00Z')
        self.gr.reconcile({'batch_id': batch['id'], 'failed_ids': ['one']})
        self.assertEqual([b['id'] for b in self.gr.review()['books']], ['one', 'two'])

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
