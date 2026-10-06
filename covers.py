"""Authenticated, bounded ABS cover proxy. No upstream URLs or tokens in the browser."""
import collections
import re
import threading
import time
import urllib.request

class Covers:
    def __init__(self, store):
        self.store = store
        self.cache = collections.OrderedDict()
        self.lock = threading.Lock()

    def get(self, book_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', book_id):
            return None
        with self.store.db() as db:
            if not db.execute('SELECT 1 FROM books WHERE id=? AND available=1', (book_id,)).fetchone():
                return None
        with self.lock:
            cached = self.cache.get(book_id)
            if cached and cached[0] > time.monotonic():
                self.cache.move_to_end(book_id)
                return cached[1]
        result = None
        try:
            req = urllib.request.Request(self.store.abs.url + '/api/items/' + book_id + '/cover?width=400&format=webp',
                headers={'Authorization': 'Bearer ' + self.store.abs.token, 'Accept': 'image/webp,image/jpeg,image/png'})
            with self.store.abs.opener.open(req, timeout=15) as response:
                data = response.read(2_000_001)
            if 0 < len(data) <= 2_000_000:
                kind = 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else None
                if kind:
                    result = data, kind
        except Exception:
            pass  # Missing covers and upstream failures use the UI placeholder.
        with self.lock:
            self.cache[book_id] = (time.monotonic() + (3600 if result else 60), result)
            self.cache.move_to_end(book_id)
            while len(self.cache) > 64:
                self.cache.popitem(last=False)
        return result
