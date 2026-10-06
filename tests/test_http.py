import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from core import Store
from access import AccessPolicy
from server import handler
from test_core import FakeABS


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.temp.name) / 'http.sqlite3', FakeABS())
        self.store.sync()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.store, 0))
        self.port = self.server.server_port
        self.server.RequestHandlerClass = handler(self.store, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}' + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers, error.read()

    def test_page_and_state_do_not_expose_credentials(self):
        status, headers, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'Your next good listen', body)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        status, _, body = self.request('/api/state')
        self.assertEqual(status, 200)
        self.assertNotIn(b'fake-token', body)

    def test_host_validation_blocks_dns_rebinding(self):
        status, _, _ = self.request('/api/state', headers={'Host': 'attacker.example'})
        self.assertEqual(status, 403)

    def test_write_requires_custom_header_and_local_origin(self):
        payload = {'book_id': 'one', 'rating': 5}
        self.assertEqual(self.request('/api/feedback', payload)[0], 403)
        self.assertEqual(self.request('/api/feedback', payload, {'X-Selector-Request': '1', 'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/feedback', payload, {'X-Selector-Request': '1', 'Origin': f'http://127.0.0.1:{self.port}'})[0], 200)

    def test_sensitive_files_and_traversal_are_not_served(self):
        for path in ('/.env', '/core.py', '/data/selector.sqlite3', '/../.env'):
            self.assertEqual(self.request(path)[0], 404)

    def test_saved_review_can_be_found_and_dnf_corrected_without_losing_notes(self):
        original = {'book_id': 'one', 'rating': 4, 'scope': 'none', 'notes': 'My note',
                    'enjoyed': 'Voice', 'avoid': 'Slow ending', 'dnf': True}
        self.store.feedback(original)
        self.store.feedback({'book_id': 'two', 'dismissed': True})
        status, _, body = self.request('/api/reviews?search=ONE')
        self.assertEqual(status, 200)
        saved = json.loads(body)['books']
        self.assertEqual([b['id'] for b in saved], ['one'])
        self.assertEqual(saved[0]['feedback']['dnf'], 1)
        status, _, body = self.request('/api/book?id=one')
        correction = json.loads(body)['feedback'] | {'dnf': False}
        self.assertEqual(self.request('/api/feedback', correction, {'X-Selector-Request': '1'})[0], 200)
        corrected = self.store.feedback_book('one')['feedback']
        self.assertEqual(corrected['dnf'], 0)
        for key in ('rating', 'notes', 'enjoyed', 'avoid', 'scope'):
            self.assertEqual(corrected[key], original[key])
        self.assertEqual([b['id'] for b in self.store.reviews()['books']], ['one'])
        self.assertEqual(self.request('/api/reviews?search=%25')[0], 200)
        self.assertEqual(self.store.reviews('%')['books'], [])

    def test_invalid_json_rejected(self):
        self.assertEqual(self.request('/api/profile', [], {'X-Selector-Request': '1'})[0], 400)

    def test_public_api_and_page_require_valid_access_identity(self):
        policy = AccessPolicy(self.port, 'https://selector.example.com', 'https://reader.cloudflareaccess.com', 'audience', 'reader@example.com')
        policy.verify = lambda token: token == 'valid-test-token'
        self.server.RequestHandlerClass = handler(self.store, self.port, policy)
        for path in ('/', '/api/state', '/api/reviews', '/api/recommendations', '/api/cover?id=one'):
            self.assertEqual(self.request(path, headers={'Host': 'selector.example.com'})[0], 403)
            self.assertEqual(self.request(path, headers={'Host': 'selector.example.com', 'Cf-Access-Jwt-Assertion': 'invalid'})[0], 403)
            self.assertEqual(self.request(path, headers={'Host': 'selector.example.com', 'Cf-Access-Jwt-Assertion': 'valid-test-token'})[0], 404 if path.startswith('/api/cover') else 200)

    def test_paid_refresh_requires_authentication_and_matching_origin(self):
        payload = {'library_id': 'lib'}
        self.assertEqual(self.request('/api/ai/generate', payload)[0], 403)
        self.assertEqual(self.request('/api/ai/generate', payload,
            {'X-Selector-Request': '1', 'Origin': 'https://attacker.example'})[0], 403)

    def test_remote_feedback_requires_authentication_and_matching_origin(self):
        policy = AccessPolicy(self.port, 'https://selector.example.com', 'https://reader.cloudflareaccess.com', 'audience', 'reader@example.com')
        policy.verify = lambda token: token == 'valid-test-token'
        self.server.RequestHandlerClass = handler(self.store, self.port, policy)
        headers = {'Host': 'selector.example.com', 'Cf-Access-Jwt-Assertion': 'valid-test-token', 'X-Selector-Request': '1'}
        self.assertEqual(self.request('/api/feedback', {'book_id': 'one', 'rating': 5}, headers)[0], 403)
        headers['Origin'] = 'https://selector.example.com'
        self.assertEqual(self.request('/api/feedback', {'book_id': 'one', 'rating': 5}, headers)[0], 200)


if __name__ == '__main__':
    unittest.main()
