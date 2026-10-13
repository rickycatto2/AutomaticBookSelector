"""Synthetic credentials and mocked providers only; never load .env."""
import copy
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

from isbn_catalog import Catalog, ProviderFailure, ENDPOINTS
from goodreads import Goodreads, source_snapshot, LEGACY_CATALOG_BLOCK
from test_core import item
import test_isbn_catalog as fixtures
from test_isbn_catalog import GOOGLE, SOURCE


class ProviderTests(unittest.TestCase):
    def opener(self, data):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(data).encode()
        opener = MagicMock()
        opener.open.return_value = response
        return opener

    def test_key_google_only_and_anonymous_default_no_reflection(self):
        sentinel = 'SYNTHETIC-KEY-not-a-real-credential'
        data = copy.deepcopy(GOOGLE)
        data['items'][0]['volumeInfo']['publisher'] = sentinel
        opener = self.opener(data)
        with patch.dict(os.environ, {'GOOGLE_BOOKS_API_KEY': sentinel}), patch('isbn_catalog.urllib.request.build_opener', return_value=opener):
            catalog = Catalog()
            result = catalog.resolve(SOURCE)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(opener.open.call_args.args[0].full_url).query)
            self.assertEqual(query['key'], [sentinel])
            self.assertNotIn(sentinel, json.dumps(result))
            self.assertNotIn(sentinel, repr(catalog.cache))
            catalog.request('Open Library', {'title': 'One', 'key': sentinel})
            self.assertNotIn('key', urllib.parse.parse_qs(urllib.parse.urlsplit(opener.open.call_args.args[0].full_url).query))
        with patch.dict(os.environ, {'GOOGLE_BOOKS_API_KEY': ''}), patch('isbn_catalog.urllib.request.build_opener', return_value=opener):
            Catalog().request('Google Books', {})
            self.assertNotIn('key', urllib.parse.parse_qs(urllib.parse.urlsplit(opener.open.call_args.args[0].full_url).query))

    def test_quota_cooldown_config_change_and_safe_errors(self):
        opener = self.opener({})
        opener.open.side_effect = urllib.error.HTTPError('https://secret.invalid/?key=SYNTHETIC', 429, 'SYNTHETIC body', {}, None)
        with patch.dict(os.environ, {'GOOGLE_BOOKS_API_KEY': 'SYNTHETIC'}), patch('isbn_catalog.urllib.request.build_opener', return_value=opener), patch('isbn_catalog.time.monotonic', return_value=100):
            catalog = Catalog()
            for _ in range(20):
                with self.assertRaises(ProviderFailure) as caught:
                    catalog.request('Google Books', {'q': 'One'})
                self.assertEqual(caught.exception.failure['status'], 'daily_quota')
                self.assertNotIn('SYNTHETIC', str(caught.exception))
                self.assertNotIn('https:', str(caught.exception))
            self.assertEqual(opener.open.call_count, 1)
            opener.open.side_effect = None
            with patch.dict(os.environ, {'GOOGLE_BOOKS_API_KEY': 'CHANGED-SYNTHETIC'}), patch('isbn_catalog.time.monotonic', return_value=106):
                self.assertEqual(catalog.request('Google Books', {}), {})
            self.assertNotIn('Google Books', catalog.cooldowns)
            self.assertEqual(opener.open.call_count, 2)

    def test_transport_backoff_expires_and_throttle_remains(self):
        opener = self.opener({})
        opener.open.side_effect = TimeoutError('secret URL/body')
        with patch('isbn_catalog.urllib.request.build_opener', return_value=opener), patch('isbn_catalog.time.time', return_value=1000), patch('isbn_catalog.time.monotonic', return_value=100):
            catalog = Catalog()
            for _ in range(20):
                with self.assertRaises(ProviderFailure) as caught:
                    catalog.request('Open Library', {})
                self.assertEqual(caught.exception.failure['status'], 'transport')
            self.assertEqual(opener.open.call_count, 1)
            opener.open.side_effect = None
            with patch('isbn_catalog.time.time', return_value=1061), patch('isbn_catalog.time.monotonic', return_value=161):
                catalog.request('Open Library', {})
                with self.assertRaises(ProviderFailure) as caught:
                    catalog.request('Open Library', {'title': 'other'})
                self.assertEqual(caught.exception.failure['status'], 'throttle')
            self.assertNotIn('Open Library', catalog.cooldowns)

    def test_http_invalid_and_resolve_statuses(self):
        opener = self.opener([])
        with patch('isbn_catalog.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(ProviderFailure) as caught:
                Catalog().request('Open Library', {})
            self.assertEqual(caught.exception.failure['status'], 'invalid_response')
            opener.open.side_effect = urllib.error.HTTPError('secret', 503, 'body', {}, None)
            with self.assertRaises(ProviderFailure) as caught:
                Catalog().request('Open Library', {})
            self.assertEqual(caught.exception.failure['status'], 'http_503')
        catalog = Catalog()
        with patch.object(catalog, 'search', side_effect=lambda provider, *_: (_ for _ in ()).throw(ProviderFailure(provider, 'daily_quota', 'Daily quota exceeded (HTTP 429).'))):
            result = catalog.resolve(SOURCE)
        self.assertTrue(result['provider_error'])
        self.assertEqual(len(result['provider_failures']), 2)
        self.assertIn('Google Books [daily_quota]', result['reason'])
        self.assertNotIn('review required', result['reason'].lower())


class RetryTests(unittest.TestCase):
    setUp = fixtures.ExportAutomationTests.setUp

    def persist(self, book_id, result):
        with self.store.db() as db:
            source = dict(db.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone())
            db.execute('INSERT OR REPLACE INTO gr_catalog_attempts VALUES (?,?,?)', (book_id, source_snapshot(source), json.dumps(result)))

    def blocked(self):
        return {'high_confidence': False, 'candidate': None, 'provider_error': True, 'reason': 'Google Books [daily_quota]: Daily quota exceeded (HTTP 429).',
                'provider_failures': [{'provider': 'Google Books', 'status': 'daily_quota', 'message': 'Daily quota exceeded (HTTP 429).'}]}

    def test_only_provider_failed_books_queued_capped_and_durable_claim(self):
        for i in range(30):
            self.abs.items.append(item('retry-' + str(i), 'Retry ' + str(i), 'Writer'))
            self.abs.user['mediaProgress'].append({'libraryItemId': 'retry-' + str(i), 'isFinished': True, 'finishedAt': '2026-10-01'})
        self.store.sync()
        for i in range(30):
            self.persist('retry-' + str(i), self.blocked())
        self.persist('one', self.blocked() | {'candidate': {'title': 'One', 'author': 'Writer', 'isbn': '0306406152'}})
        job = self.gr.retry_provider_books()['job']
        self.assertEqual(len(job['items']), 25)
        self.assertNotIn('one', [i['id'] for i in job['items']])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM gr_catalog_attempts').fetchone()[0], 31)
        target = job['items'][0]['id']
        def resolve(source):
            with self.store.db() as db:
                state = db.execute('SELECT state FROM gr_match_job_items WHERE job_id=? AND book_id=?', (job['id'], target)).fetchone()[0]
                self.assertEqual(state, 'pending')
                pending = json.loads(db.execute('SELECT result FROM gr_catalog_attempts WHERE book_id=?', (target,)).fetchone()[0])
                self.assertIn('in flight', pending['reason'])
            return self.blocked()
        with patch.object(self.gr.catalog, 'resolve', side_effect=resolve) as lookup:
            response = self.gr.resolve({'batch_id': job['id'], 'book_ids': [target]})
            self.gr.resolve({'batch_id': job['id'], 'book_ids': [target]})
            self.assertEqual(lookup.call_count, 1)
        self.assertEqual(sum(i['state'] == 'queued' for i in response['job']['items']), 24)
        with self.assertRaises(Exception):
            self.gr.resolve({'batch_id': job['id'], 'book_ids': [target, 'one']})
        self.assertTrue(next(b for b in self.gr.review()['books'] if b['id'] == target)['provider_error'])

    def test_review_decision_and_import_failure_never_requeued(self):
        self.persist('one', self.blocked() | {'provider_error': False, 'reason': 'Genuine review outcome'})
        self.assertIsNone(self.gr.retry_provider_books()['job'])
        self.persist('one', self.blocked())
        book = self.gr.review()['books'][0]
        self.gr.save({'book_id': 'one', 'title': book['title'], 'author': book['author'], 'isbn': '', 'completed_date': book['completed_date'], 'confirmed': False})
        self.assertIsNone(self.gr.retry_provider_books()['job'])
        with self.store.db() as db:
            db.execute('DELETE FROM gr_matches')
            db.execute("INSERT INTO gr_exports VALUES ('synthetic','2026-10-01','')")
            db.execute("INSERT INTO gr_export_items VALUES ('synthetic','one','one','One','Writer','','failed','Test failure')")
        self.assertIsNone(self.gr.retry_provider_books()['job'])

    def test_retry_does_not_resume_unrelated_eligible_queue(self):
        self.gr.start_match_batch()
        with self.assertRaises(Exception):
            self.gr.retry_provider_books()


class LegacyAttemptTests(unittest.TestCase):
    setUp = fixtures.ExportAutomationTests.setUp
    persist = RetryTests.persist
    blocked = RetryTests.blocked
    def legacy(self):
        return {'provider_error': True, 'candidate': None, 'reason': 'Recorded: ' + LEGACY_CATALOG_BLOCK,
                'evidence': ['original evidence'], 'high_confidence': False}

    def raw_attempts(self):
        with self.store.db() as db:
            return [tuple(r) for r in db.execute('SELECT * FROM gr_catalog_attempts ORDER BY book_id,source_snapshot')]

    def test_legacy_upgrade_first_use_preserves_reason_and_second_run(self):
        original = self.legacy()
        self.persist('one', original)
        # Simulate reopening an existing database; migration runs on first use.
        self.gr = Goodreads(self.store)
        book = self.gr.review()['books'][0]
        self.assertTrue(book['retry_eligible'])
        self.assertTrue(book['provider_error'])
        self.assertFalse(book['ready'])
        self.assertEqual(book['reason'], original['reason'])
        self.assertIn('blocked', book['lookup_status'])
        upgraded = json.loads(self.raw_attempts()[0][2])
        self.assertEqual(upgraded.pop('provider_failures')[0]['provider'], 'catalog')
        self.assertEqual(upgraded, original)
        before = self.raw_attempts()
        self.assertEqual(self.gr.migrate_legacy_catalog_attempts(), 0)
        self.assertEqual(self.raw_attempts(), before)

    def test_conservative_and_byte_unchanged_exclusions(self):
        cases = [self.blocked(), self.legacy() | {'provider_failures': []},
                 self.legacy() | {'candidate': {'title': 'One'}},
                 self.legacy() | {'candidates': [{'title': 'One'}]},
                 self.legacy() | {'reason': 'Genuine review required'},
                 self.legacy() | {'provider_error': False},
                 self.legacy() | {'automatic': True}, self.legacy() | {'high_confidence': True}]
        with self.store.db() as db:
            for i, result in enumerate(cases):
                db.execute('INSERT INTO gr_catalog_attempts VALUES (?,?,?)', ('case-' + str(i), 'snapshot', json.dumps(result, indent=2)))
        before = self.raw_attempts()
        self.assertEqual(self.gr.migrate_legacy_catalog_attempts(), 0)
        self.assertEqual(self.gr.migrate_legacy_catalog_attempts(), 0)
        self.assertEqual(self.raw_attempts(), before)

    def test_zero_rows_and_other_tables_untouched(self):
        self.assertEqual(self.gr.migrate_legacy_catalog_attempts(), 0)
        self.gr.start_match_batch()
        with self.store.db() as db:
            db.execute("INSERT INTO gr_exports VALUES ('sentinel','2026-10-01','original csv')")
            db.execute("INSERT INTO gr_export_items VALUES ('sentinel','other','other','Other','Writer','','exported','')")
            db.execute("INSERT INTO gr_matches VALUES ('other','Other','Writer','','','',0,NULL,'',0,'original')")
        tables = ['gr_matches', 'gr_exports', 'gr_export_items', 'gr_match_jobs', 'gr_match_job_items']
        def snapshot():
            with self.store.db() as db:
                return {t: [tuple(r) for r in db.execute('SELECT * FROM ' + t)] for t in tables}
        before = snapshot()
        self.persist('one', self.legacy())
        self.assertEqual(self.gr.migrate_legacy_catalog_attempts(), 1)
        self.assertEqual(snapshot(), before)

    def test_both_batch_actions_queue_legacy_set_without_provider_calls(self):
        for i in range(19):
            book_id = 'legacy-' + str(i)
            self.abs.items.append(item(book_id, 'Legacy ' + str(i), 'Writer'))
            self.abs.user['mediaProgress'].append({'libraryItemId': book_id, 'isFinished': True, 'finishedAt': '2026-10-01'})
        self.store.sync()
        self.persist('one', {'provider_error': False, 'reason': 'Genuine review'})
        for i in range(19):
            self.persist('legacy-' + str(i), self.legacy())
        with patch.object(self.gr.catalog, 'resolve', side_effect=AssertionError('No provider calls')) as lookup:
            general = self.gr.start_match_batch()['job']
            self.assertEqual(len(general['items']), 19)
            with self.store.db() as db:
                results = [json.loads(r[0]) for r in db.execute('SELECT result FROM gr_match_job_items WHERE job_id=?', (general['id'],))]
                self.assertTrue(all(r['provider_retry'] for r in results))
                # Synthetic terminal states, not a live lookup; retain the queue.
                db.execute("UPDATE gr_match_job_items SET state='provider_error' WHERE job_id=?", (general['id'],))
            retry = self.gr.retry_provider_books()['job']
            self.assertEqual(len(retry['items']), 19)
            self.assertEqual({i['id'] for i in retry['items']}, {i['id'] for i in general['items']})
            lookup.assert_not_called()
        self.assertEqual(len(self.raw_attempts()), 20)
