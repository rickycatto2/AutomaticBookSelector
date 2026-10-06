import copy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from ai import AI, AppError, Rejected, LIMIT, grounded_pick
from core import Store
from covers import Covers
from test_core import FakeABS, export

class FakeClient:
    configured = True
    def __init__(self):
        self.calls = []
        self.bad_id = False
        self.failure = None

    def request(self, body):
        request = json.loads(body)
        self.calls.append(request)
        if self.failure:
            raise self.failure
        if request['text']['format']['name'] == 'reading_taste':
            value = {'lasting': 'Enjoys intimacy, not sentimental endings.', 'current': '', 'seek': ['relationships'], 'avoid': ['tidy ending']}
        else:
            context = json.loads(request['input'])
            value = {'summary': 'Some possibilities grounded in your taste.', 'picks': [
                {'id': 'invented' if self.bad_id else b['id'], 'reason': 'Intimate relationships match your preference.', 'cautions': [], 'fit': 'lasting'}
                for b in context['candidates'][:2]]}
        return {'status': 'completed', 'usage': {'input_tokens': 1000, 'output_tokens': 200},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(value)}]}]}

class AITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(pathlib.Path(temp.name) / 'db.sqlite3', FakeABS())
        self.store.sync()
        self.client = FakeClient()
        self.ai = AI(self.store, self.client)

    def test_manual_generation_then_reloads_do_not_spend(self):
        self.assertEqual(self.ai.recommendations('lib')['source'], 'local')
        self.assertEqual(self.client.calls, [])
        self.ai.generate('lib')
        spent = self.ai.status()['spent_usd']
        for _ in range(3):
            self.assertEqual(self.ai.recommendations('lib')['source'], 'ai')
        restarted = AI(self.store, self.client)
        self.assertEqual(restarted.recommendations('lib')['source'], 'ai')
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(restarted.status()['spent_usd'], spent)
        self.ai.generate('lib')
        self.assertEqual(len(self.client.calls), 3)  # Taste analysis reused.

    def test_saved_set_survives_taste_changes_until_requested(self):
        self.ai.generate('lib')
        profile = copy.deepcopy(self.store.profile())
        profile['likes'] = 'A different mood'
        self.store.profile(profile)
        result = self.ai.recommendations('lib')
        self.assertTrue(result['stale'])
        self.assertEqual(result['source'], 'ai')
        self.assertEqual(len(self.client.calls), 2)

    def test_finished_books_removed_without_a_paid_call(self):
        self.ai.generate('lib')
        self.store.abs.user['mediaProgress'] = [{'libraryItemId': 'one', 'isFinished': True, 'progress': 1}]
        self.store.sync()
        result = self.ai.recommendations('lib')
        self.assertNotIn('one', [b['id'] for b in result['books']])
        self.assertEqual(len(self.client.calls), 2)

    def test_feedback_scope_and_expired_kick_are_excluded_from_context(self):
        self.store.kick({'text': 'cozy romance', 'duration_days': 21})
        self.store.feedback({'book_id': 'one', 'scope': 'current', 'notes': 'Current secret mood'})
        self.store.feedback({'book_id': 'two', 'scope': 'none', 'notes': 'Do not use this'})
        self.assertEqual(len(self.ai.context()['feedback']), 1)
        self.store.kick({'text': '', 'duration_days': 21})
        self.assertEqual(self.ai.context()['feedback'], [])
        self.assertEqual(self.ai.context()['kick'], {'active': False})

    def test_explicit_review_supersedes_goodreads_seed(self):
        self.store.import_csv(export([{'Book Id': '1', 'Title': 'One', 'Author': 'Writer', 'My Rating': '5'}]))
        self.store.feedback({'book_id': 'one', 'rating': 2, 'scope': 'lasting', 'notes': 'Changed my mind'})
        self.assertEqual(self.ai.context()['goodreads_seed'], [])
        self.assertEqual(self.ai.context()['feedback'][0]['rating'], 2)

    def test_invalid_ids_never_enter_recommendations_but_usage_counted(self):
        self.client.bad_id = True
        with self.assertRaises(AppError):
            self.ai.generate('lib')
        self.assertEqual(self.ai.recommendations('lib')['source'], 'local')
        self.assertGreater(self.ai.status()['spent_usd'], 0)

    def test_budget_reservation_blocks_even_before_exact_limit(self):
        with self.store.db() as db:
            db.execute('INSERT INTO ai_calls VALUES (?,?,?,?,?,?,?)', ('used', self.ai.month(), 'accounted', LIMIT-1, '', 1, 1))
        with self.assertRaises(AppError):
            self.ai.generate('lib')
        self.assertEqual(self.client.calls, [])

    def test_unknown_delivery_keeps_reservation_across_restart(self):
        self.client.failure = AppError('Uncertain result')
        with self.assertRaises(AppError):
            self.ai.generate('lib')
        spent = self.ai.status()['spent_usd']
        self.assertGreater(spent, 0)
        self.assertEqual(AI(self.store, self.client).status()['spent_usd'], spent)
        self.assertEqual(len(self.client.calls), 1)

    def test_rejected_credentials_release_reservation_and_redact(self):
        self.client.failure = Rejected(401)
        with self.assertRaisesRegex(AppError, 'Check your OpenAI'):
            self.ai.generate('lib')
        self.assertEqual(self.ai.status()['spent_usd'], 0)

    def test_busy_generation_does_not_make_another_call(self):
        self.ai.lock.acquire()
        try:
            with self.assertRaises(AppError):
                self.ai.generate('lib')
        finally:
            self.ai.lock.release()
        self.assertEqual(self.client.calls, [])

    def test_requests_have_no_storage_tools_or_credentials(self):
        self.ai.generate('lib')
        for request in self.client.calls:
            self.assertFalse(request['store'])
            self.assertNotIn('tools', request)
            self.assertNotIn('fake-token', json.dumps(request))
            self.assertLessEqual(request['max_output_tokens'], 3500)

    def test_queer_genre_alone_does_not_support_sapphic_claim(self):
        book = {'description': 'Jesse and Lulu reunite in a friendship study.', 'genres': ['LGBTQ+', 'Romance']}
        pick = {'id': 'one', 'reason': 'A warm sapphic romance.', 'fit': 'current', 'cautions': []}
        checked = grounded_pick(book, pick)
        self.assertEqual(checked['fit'], 'exploratory')
        self.assertIn('does not establish', checked['reason'])
        self.assertEqual(checked['id'], 'one')
        self.assertEqual(pick['fit'], 'current')

    def test_explicit_description_supports_claim_and_negative_claim_is_preserved(self):
        pick = {'id': 'one', 'reason': 'A warm sapphic romance.', 'fit': 'current', 'cautions': []}
        self.assertEqual(grounded_pick({'description': 'A cozy sapphic romance.'}, pick), pick)
        negative = pick | {'reason': 'Not sapphic, but a possible romance alternative.'}
        self.assertEqual(grounded_pick({'description': 'A romance about Jesse and Lulu.'}, negative), negative)

    def test_covers_reject_unknown_ids_path_traversal_and_html(self):
        covers = Covers(self.store)
        self.assertIsNone(covers.get('../secret'))
        self.assertIsNone(covers.get('missing'))
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'<html>not an image</html>'
        self.store.abs.opener = type('Opener', (), {'open': lambda *a, **kw: Response()})()
        self.assertIsNone(covers.get('one'))

    def test_cover_cache_keeps_token_off_browser_and_no_redirects(self):
        calls = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'RIFF1234WEBPimage'
        def opened(request, **kwargs):
            calls.append(request)
            return Response()
        self.store.abs.opener = type('Opener', (), {})()
        self.store.abs.opener.open = opened
        covers = Covers(self.store)
        self.assertEqual(covers.get('one')[1], 'image/webp')
        self.assertEqual(covers.get('one')[1], 'image/webp')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('fake-token', calls[0].full_url)

if __name__ == '__main__':
    unittest.main()
