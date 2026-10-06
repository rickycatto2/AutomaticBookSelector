"""Loopback-only web app; Docker publishes its port to 127.0.0.1 only."""
import argparse
import json
import mimetypes
import os
import pathlib
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import ABS, AppError, Store, load_env
from access import AccessPolicy
from notifications import Notifications

ROOT = pathlib.Path(__file__).resolve().parent


def handler(store, port, access_policy=None, notifications=None):
    access_policy = access_policy or AccessPolicy.from_env(port)
    notifications = notifications or Notifications(store)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # No request payloads, credentials, server URL or upstream response body.
            pass

        def respond(self, code, payload, content_type='application/json'):
            data = json.dumps(payload).encode() if content_type == 'application/json' else payload
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return access_policy.authorize(self.headers)

        def do_GET(self):
            if not self.authorized():
                self.respond(403, {'error': 'Sign in through Cloudflare Access, or use the local app address.'})
                return
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            try:
                if parsed.path == '/api/health':
                    self.respond(200, {'ok': True})
                elif parsed.path == '/api/state':
                    self.respond(200, store.snapshot() | {'email': notifications.settings()})
                elif parsed.path == '/api/book':
                    self.respond(200, store.feedback_book(params.get('id', [''])[0]))
                elif parsed.path == '/api/recommendations':
                    self.respond(200, store.recommendations(params.get('library', [''])[0], params.get('search', [''])[0]))
                elif parsed.path == '/api/playlists':
                    self.respond(200, {'playlists': store.playlists(params.get('library', [''])[0])})
                elif parsed.path in ('/', '/app.js', '/style.css'):
                    filename = 'index.html' if parsed.path == '/' else parsed.path.lstrip('/')
                    kind = mimetypes.guess_type(filename)[0] or 'text/plain'
                    self.respond(200, (ROOT / 'static' / filename).read_bytes(), kind + '; charset=utf-8')
                else:
                    self.respond(404, {'error': 'Page not found.'})
            except AppError as error:
                self.respond(400, {'error': str(error)})
            except Exception:
                self.respond(500, {'error': 'The app could not complete this request.'})

        def do_POST(self):
            if not self.authorized() or self.headers.get('X-Selector-Request') != '1':
                self.respond(403, {'error': 'Sign in to the app before making changes.'})
                return
            if not access_policy.valid_origin(self.headers):
                self.respond(403, {'error': 'Cross-origin requests are not allowed.'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                maximum = 20 * 1024 * 1024 if self.path == '/api/import' else 64 * 1024
                if not 0 <= length <= maximum:
                    self.respond(413, {'error': 'Request is too large.'})
                    return
                raw = self.rfile.read(length)
                if self.path == '/api/import':
                    result = store.import_csv(raw, explicit=True)
                else:
                    payload = json.loads(raw or b'{}')
                    if not isinstance(payload, dict):
                        raise AppError('Request must be an object.')
                    if self.path == '/api/sync':
                        if store.sync_state['running']:
                            result = {'already_running': True}
                        else:
                            threading.Thread(target=quiet_sync, args=(store,), daemon=True).start()
                            result = {'started': True}
                    elif self.path == '/api/profile':
                        result = store.profile(payload)
                    elif self.path == '/api/kick':
                        result = store.kick(payload)
                    elif self.path == '/api/email/settings':
                        result = notifications.settings(payload)
                    elif self.path == '/api/email/test':
                        result = notifications.test()
                    elif self.path == '/api/feedback':
                        result = store.feedback(payload)
                    elif self.path == '/api/questions/dismiss':
                        result = store.dismiss_question(payload.get('book_id'))
                    elif self.path == '/api/queue':
                        result = store.queue_books(payload)
                    else:
                        self.respond(404, {'error': 'Action not found.'})
                        return
                self.respond(200, result)
            except (ValueError, UnicodeError):
                self.respond(400, {'error': 'Request could not be read.'})
            except AppError as error:
                self.respond(400, {'error': str(error)})
            except Exception:
                self.respond(500, {'error': 'The app could not complete this action. Refresh before retrying.'})
    return Handler


def quiet_sync(store):
    try:
        store.sync()
    except AppError:
        print('ABS sync needs attention. See the status in the web app.', flush=True)


def main():
    load_env(ROOT / '.env')
    parser = argparse.ArgumentParser()
    parser.add_argument('--bootstrap-only', action='store_true', help='Import newest CSV, sync ABS, and exit')
    args = parser.parse_args()
    store = Store(pathlib.Path(os.getenv('DATA_DIR', str(ROOT / 'data'))) / 'selector.sqlite3', ABS(os.getenv('ABS_URL', ''), os.getenv('ABS_TOKEN', '')))
    try:
        store.bootstrap(os.getenv('GOODREADS_IMPORT_DIR', str(ROOT)))
    except AppError as error:
        print(str(error), flush=True)
    if args.bootstrap_only:
        result = store.sync()
        print(json.dumps(result), flush=True)
        return
    interval = max(60, int(os.getenv('SYNC_INTERVAL_SECONDS', '900')))
    def poll():
        while True:
            quiet_sync(store)
            threading.Event().wait(interval)
    threading.Thread(target=poll, daemon=True).start()
    notifications = Notifications(store)
    def email_poll():
        while True:
            try:
                notifications.process()
            except Exception:
                print('Email reminders need attention. Check the app settings.', flush=True)
            threading.Event().wait(60)
    threading.Thread(target=email_poll, daemon=True).start()
    port = int(os.getenv('PORT', '5077'))
    server = ThreadingHTTPServer((os.getenv('HOST', '127.0.0.1'), port), handler(store, port, notifications=notifications))
    server.daemon_threads = True
    print(f'AutomaticBookSelector: http://localhost:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
