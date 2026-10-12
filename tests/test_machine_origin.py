"""Native hostname boundary tests; temporary DBs only, no tunnel/network identity calls."""
import http.client
import json
import pathlib
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from access import AccessPolicy
from core import Store
from server import handler
from telemetry import Telemetry
from test_core import FakeABS


STATUS = '/api/v1/bookramp/status'
SESSIONS = '/api/v1/bookramp/sessions'


class MachineConfigTests(unittest.TestCase):
    def test_default_disabled_and_env_opt_in(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(AccessPolicy.from_env(5077).host_kind('native.example.test'))
        with patch.dict('os.environ', {'BOOKRAMP_MACHINE_ORIGIN': 'https://native.example.test'}, clear=True):
            self.assertEqual(AccessPolicy.from_env(5077).host_kind('NATIVE.EXAMPLE.TEST:443'), 'machine')

    def test_invalid_origins_and_same_hostname_fail_closed(self):
        invalid = ('http://native.example.test', 'https://native.example.test/',
                   'https://native.example.test/path', 'https://native.example.test?',
                   'https://native.example.test#', 'https://native.example.test:80',
                   'https://native.example.test:444', 'https://native.example.test:',
                   'https://user@native.example.test', 'https://127.0.0.1', 'https://[::1]',
                   'https://localhost', 'https://native.example.test.', 'https://bad_host.example.test',
                   'https://-bad.example.test', 'https://native.example.test\n',
                   'https://SELECTOR.example.test', 'https://selector.example.test:443')
        for origin in invalid:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                AccessPolicy(5077, public_origin='https://selector.example.test', machine_origin=origin)
        with self.assertRaises(ValueError):
            AccessPolicy(5077, public_origin='https://selector.example.test.', machine_origin='https://selector.example.test')


class MachineHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.temp.name) / 'test.db', FakeABS())
        self.store.sync()
        self.telemetry = Telemetry(self.store)
        self.connection = self.telemetry.create({'name': 'Test phone'})
        self.policy = AccessPolicy(5077, 'https://selector.example.test',
                                   'https://test.cloudflareaccess.com', 'test-audience', 'test@example.test',
                                   'https://native.example.test')
        self.policy.verify = lambda token: token == 'valid-access-test-assertion'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.store, 5077, self.policy))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        identity = self.telemetry.status()
        self.session = {'schema_version': 1, 'event_id': 'e1', 'session_id': 's1',
                        'abs_server_id': identity['abs_server_id'], 'abs_user_id': identity['abs_user_id'],
                        'abs_item_id': 'one', 'started_at': '2026-10-01T18:00:00Z',
                        'ended_at': '2026-10-01T19:00:00Z'}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, headers=(), body=b'', token=True, host='native.example.test'):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader('Host', host)
            if token:
                conn.putheader('Authorization', 'Bearer ' + self.connection['token'])
            for key, value in headers:
                conn.putheader(key, value)
            if not any(k.lower() == 'content-length' for k, _ in headers):
                conn.putheader('Content-Length', str(len(body)))
            if not any(k.lower() == 'content-type' for k, _ in headers):
                conn.putheader('Content-Type', 'application/json')
            conn.endheaders(body)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_exact_method_path_matrix_even_with_browser_credentials(self):
        paths = [STATUS, SESSIONS, '/', '/api/health', '/api/state', '/api/import', '/api/reviews',
                 '/api/bookramp/create', '/api/bookramp/revoke', '/api/bookramp/connections',
                 '/api/stats', '/api/card.png', '/app.js', '/companion.js', '/style.css', '/other']
        for route in (STATUS, SESSIONS):
            paths.extend((route + '/', route + '?', route + '?x=1', route + '#fragment',
                          route.replace('/bookramp/', '/BOOKRAMP/'), route.replace('bookramp', '%62ookramp'),
                          '/' + route, '/api/v1/../v1/bookramp/' + route.rsplit('/', 1)[1],
                          'https://native.example.test' + route))
        forged = [('Cf-Access-Jwt-Assertion', 'valid-access-test-assertion'),
                  ('Cookie', 'CF_Authorization=forged'), ('Cf-Access-Authenticated-User-Email', 'test@example.test'),
                  ('X-Selector-Request', '1')]
        for method in ('GET', 'POST', 'HEAD', 'OPTIONS', 'PUT', 'PATCH', 'DELETE', 'TRACE', 'CONNECT', 'CUSTOM'):
            for path in paths:
                with self.subTest(method=method, path=path):
                    code, _ = self.request(method, path, forged, b'{"sessions":[]}')
                    expected = 200 if (method, path) == ('GET', STATUS) else 400 if (method, path) == ('POST', SESSIONS) else 404
                    self.assertEqual(code, expected)

    def test_wrong_host_case_and_port(self):
        for host in ('native.example.test', 'NATIVE.EXAMPLE.TEST', 'native.example.test:443'):
            self.assertEqual(self.request('GET', STATUS, host=host)[0], 200)
        for host in ('wrong.example.test', 'native.example.test:80', 'native.example.test:5077',
                     'native.example.test.', 'native.example.test/path', 'native.example.test:0443',
                     'native.example.test@wrong.example.test', 'native.example.test,wrong.example.test', ''):
            for method, path in (('GET', STATUS), ('POST', SESSIONS)):
                with self.subTest(host=host, method=method):
                    self.assertEqual(self.request(method, path, host=host)[0], 401)

    def test_bearer_is_only_machine_auth_and_revocation_applies_to_both_routes(self):
        browser = [('Cf-Access-Jwt-Assertion', 'valid-access-test-assertion'),
                   ('Cookie', 'CF_Authorization=valid'), ('Cf-Access-Authenticated-User-Email', 'test@example.test')]
        for method, path in (('GET', STATUS), ('POST', SESSIONS)):
            self.assertEqual(self.request(method, path, browser, token=False)[0], 401)
            self.assertEqual(self.request(method, path, [('Authorization', 'Bearer br_wrong')], token=False)[0], 401)
        self.telemetry.revoke({'id': self.connection['id']})
        for method, path in (('GET', STATUS), ('POST', SESSIONS)):
            self.assertEqual(self.request(method, path)[0], 401)

    def test_browser_host_requires_access_even_on_native_routes(self):
        for method, path in (('GET', STATUS), ('POST', SESSIONS)):
            self.assertEqual(self.request(method, path, host='selector.example.test')[0], 401)
            self.assertEqual(self.request(method, path, [('Cf-Access-Jwt-Assertion', 'forged')], host='selector.example.test')[0], 401)
            self.assertEqual(self.request(method, path, [('Cf-Access-Jwt-Assertion', 'valid-access-test-assertion')], token=False, host='selector.example.test')[0], 401)
        self.assertEqual(self.request('GET', '/', host='selector.example.test')[0], 403)
        self.assertEqual(self.request('GET', '/', [('Cf-Access-Jwt-Assertion', 'valid-access-test-assertion')], host='selector.example.test')[0], 200)
        self.assertEqual(self.request('GET', STATUS, [('Cf-Access-Jwt-Assertion', 'valid-access-test-assertion')], host='selector.example.test')[0], 200)
        self.assertEqual(self.request('POST', SESSIONS, [('Cf-Access-Jwt-Assertion', 'valid-access-test-assertion')], b'{"sessions":[]}', host='selector.example.test')[0], 400)

    def test_disabled_machine_origin_denies_all_native_requests(self):
        self.server.RequestHandlerClass = handler(self.store, 5077, AccessPolicy(5077))
        for method, path in (('GET', STATUS), ('POST', SESSIONS)):
            self.assertEqual(self.request(method, path)[0], 401)
        self.assertEqual(self.request('GET', '/')[0], 403)

    def test_all_origin_headers_denied_on_both_routes(self):
        for origin in ('', 'null', 'https://native.example.test', 'https://selector.example.test', 'http://evil.example.test'):
            for method, path in (('GET', STATUS), ('POST', SESSIONS)):
                self.assertEqual(self.request(method, path, [('Origin', origin)])[0], 403)
        for path in ('/', '/api/health', '/api/state', '/api/import', '/api/reviews'):
            self.assertEqual(self.request('GET', path, [('Origin', 'https://native.example.test')])[0], 404)

    def test_size_framing_json_duplicates_and_content_type(self):
        for length in ('0', '1048577'):
            self.assertEqual(self.request('POST', SESSIONS, [('Content-Length', length)])[0], 413)
        for length in ('bogus', '-1', '+1', '1_0'):
            self.assertEqual(self.request('POST', SESSIONS, [('Content-Length', length)])[0], 400)
        self.assertEqual(self.request('POST', SESSIONS, [('Transfer-Encoding', 'chunked')])[0], 400)
        self.assertEqual(self.request('POST', SESSIONS, [('Content-Type', 'text/plain')], b'{}')[0], 400)
        for body in (b'{"sessions":[],"sessions":[]}', b'{"sessions":[{"schema_version":1,"schema_version":1}]}', b'[]', b'{'):
            self.assertEqual(self.request('POST', SESSIONS, body=body)[0], 400)
        self.assertEqual(self.request('POST', SESSIONS, body=json.dumps({'sessions': [self.session] * 101}).encode())[0], 400)
        body = b'{"sessions":[]}' + b' ' * (1048576 - len(b'{"sessions":[]}'))
        self.assertEqual(self.request('POST', SESSIONS, body=body)[0], 400)  # size accepted, invalid empty batch
        for name in ('Host', 'Authorization', 'Origin', 'Content-Length', 'Content-Type', 'Cf-Access-Jwt-Assertion', 'X-Selector-Request'):
            extra = [(name, 'a'), (name.lower(), 'b')]
            for method, path in (('GET', STATUS), ('POST', SESSIONS)):
                self.assertEqual(self.request(method, path, extra)[0], 400)

    def test_valid_upload_retry_and_conflict_are_atomic(self):
        body = json.dumps({'sessions': [self.session]}).encode()
        code, result = self.request('POST', SESSIONS, body=body)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(result)['accepted'], 1)
        code, result = self.request('POST', SESSIONS, body=body)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(result)['duplicates'], 1)
        conflict = {'sessions': [self.session | {'event_id': 'e2', 'session_id': 's2'}, self.session | {'again_count': 1}]}
        self.assertEqual(self.request('POST', SESSIONS, body=json.dumps(conflict).encode())[0], 409)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bookramp_sessions').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
