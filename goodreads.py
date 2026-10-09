"""Reviewed public exports, independent of private recommendation feedback."""
import csv
import datetime as dt
import html
import io
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from core import AppError, NoRedirect, identity, normalize, now

ZONE = ZoneInfo('America/Chicago')
HEADERS = ['Title', 'Author', 'ISBN', 'My Rating', 'Date Read', 'Shelves']


def completed_date(value):
    if not value:
        return ''
    try:
        if isinstance(value, (float, int)) or str(value).isdigit():
            number = float(value)
            stamp = dt.datetime.fromtimestamp(number / 1000 if number > 1e11 else number, dt.timezone.utc)
        else:
            stamp = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                return stamp.date().isoformat()
        return stamp.astimezone(ZONE).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return ''


def valid_isbn(value):
    code = re.sub(r'[\s-]', '', str(value or '')).upper()
    if re.fullmatch(r'\d{13}', code):
        return code if sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(code)) % 10 == 0 else ''
    if re.fullmatch(r'\d{9}[\dX]', code):
        return code if sum((10-i) * (10 if n == 'X' else int(n)) for i, n in enumerate(code)) % 11 == 0 else ''
    return ''


def book_url(value):
    parsed = urllib.parse.urlsplit(str(value).strip())
    match = re.fullmatch(r'/(?:en/)?book/show/(\d+)(?:[.\-][A-Za-z0-9_.%\-]*)?/?', parsed.path)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.goodreads.com', 'goodreads.com') or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.query or parsed.fragment or not match:
        raise AppError('Paste an HTTPS Goodreads book/show link without extra query parameters.')
    return 'https://www.goodreads.com/book/show/' + match[1], match[1]


class PageData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.collect = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and (attrs.get('type') == 'application/ld+json' or attrs.get('id') == '__NEXT_DATA__'):
            self.collect, self.parts = True, []

    def handle_data(self, data):
        if self.collect:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.collect:
            self.scripts.append(''.join(self.parts))
            self.collect = False


def page_metadata(body):
    parser = PageData()
    parser.feed(body)
    books = []
    def walk(node):
        if isinstance(node, dict):
            if node.get('@type') == 'Book' or node.get('__typename') == 'Book':
                books.append(node)
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)
    for script in parser.scripts:
        try:
            walk(json.loads(script))
        except (ValueError, RecursionError):
            continue
    for book in books:
        details = book.get('details') or {}
        code = valid_isbn(book.get('isbn') or details.get('isbn13') or details.get('isbn'))
        if code:
            authors = book.get('author') or []
            if isinstance(authors, dict):
                authors = [authors]
            author = ', '.join(a.get('name', '') for a in authors if isinstance(a, dict)) if isinstance(authors, list) else str(authors)
            return {'isbn': code, 'title': book.get('name') or book.get('title') or '', 'author': author}
    raise AppError('This page did not provide a usable ISBN. Open Book details & editions and paste an ISBN from a print edition below.')


class Goodreads:
    def __init__(self, store):
        self.store = store
        self.lookup_lock = threading.Lock()
        self.last_lookup = 0
        with store.lock, store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS gr_matches (
                book_id TEXT PRIMARY KEY,title TEXT NOT NULL,author TEXT NOT NULL,isbn TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',gr_id TEXT NOT NULL DEFAULT '',confirmed INTEGER NOT NULL DEFAULT 0,
                public_rating INTEGER,completed_date TEXT NOT NULL DEFAULT '',ignored INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS gr_exports (id TEXT PRIMARY KEY,created_at TEXT NOT NULL,csv TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS gr_export_items (batch_id TEXT NOT NULL,book_id TEXT NOT NULL,
                title_key TEXT NOT NULL,title TEXT NOT NULL,author TEXT NOT NULL,isbn TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'exported',failure TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(batch_id,book_id));''')

    def review(self):
        with self.store.db() as db:
            history = [dict(r) for r in db.execute('SELECT * FROM history')]
            read_titles = {h['title_key'] for h in history if h['shelf'] == 'read' or h['date_read'] or h['read_count']}
            seeds = {h['title_key']: h for h in history}
            matches = {r['book_id']: dict(r) for r in db.execute('SELECT * FROM gr_matches')}
            exports = {r['title_key'] for r in db.execute("SELECT title_key FROM gr_export_items WHERE state IN ('exported','imported')")}
            failures = {r['book_id']: r['failure'] for r in db.execute("SELECT book_id,failure FROM gr_export_items WHERE state='failed' ORDER BY rowid")}
            rows = [dict(r) for r in db.execute('''SELECT b.*,COALESCE(f.dnf,0) AS dnf,
                (SELECT max(completion_timestamp) FROM bookramp_sessions s WHERE s.abs_item_id=b.id AND s.completion_crossed=1) AS ramp_finished
                FROM books b LEFT JOIN feedback f ON f.book_id=b.id
                WHERE b.finished=1 OR b.id IN (SELECT abs_item_id FROM bookramp_sessions WHERE completion_crossed=1)''')]
            batches = [dict(r) for r in db.execute('''SELECT e.id,e.created_at,
                count(i.book_id) AS count,sum(i.state='failed') AS failed,sum(i.state='imported') AS imported
                FROM gr_exports e JOIN gr_export_items i ON i.batch_id=e.id GROUP BY e.id ORDER BY e.created_at DESC LIMIT 20''')]
        seen, books, excluded = set(), [], 0
        for b in sorted(rows, key=lambda b: str(b['finished_at'] or b['ramp_finished'] or ''), reverse=True):
            key = identity(b['title'], b['author'])[0]
            match = matches.get(b['id'], {})
            if b['dnf'] or key in read_titles or key in exports or key in seen or match.get('ignored'):
                excluded += 1
                continue
            seen.add(key)
            seed = seeds.get(key, {})
            saved = {'book_id': b['id'], 'title': seed.get('title', b['title']), 'author': seed.get('author', b['author']),
                     'isbn': seed.get('isbn', '') or b['isbn'], 'url': 'https://www.goodreads.com/book/show/' + seed['id'] if seed else '',
                     'confirmed': 0, 'public_rating': None, 'completed_date': completed_date(b['ramp_finished'] or b['finished_at'])} | match
            saved.update(id=b['id'], original_title=b['title'], original_author=b['author'], audio_isbn=b['isbn'], failure=failures.get(b['id'], ''))
            saved['ready'] = bool(saved['confirmed'] and valid_isbn(saved['isbn']) and saved['completed_date'] and saved['title'] and saved['author'])
            books.append(saved)
        return {'books': books, 'excluded': excluded, 'batches': batches}

    def save(self, p):
        if any(not isinstance(p.get(k, ''), str) for k in ('title', 'author', 'isbn', 'url', 'completed_date')):
            raise AppError('Book details must be text values.')
        title, author = p.get('title', '').strip(), p.get('author', '').strip()
        if not title or not author or max(len(title), len(author)) > 1000 or title[0] in '=+-@' or author[0] in '=+-@':
            raise AppError('Enter the correct title and primary author (up to 1,000 characters each).')
        code = str(p.get('isbn', '')).strip()
        if code and not valid_isbn(code):
            raise AppError('That ISBN has an invalid format or checksum. Use 10 or 13 digits, preserving leading zeros.')
        rating = p.get('public_rating')
        if rating is not None and (type(rating) is not int or rating not in range(1, 6)):
            raise AppError('Choose a Goodreads rating from 1 to 5, or leave it blank.')
        date = p.get('completed_date', '')
        try:
            if not (p.get('ignored') and not date):
                parsed_date = dt.date.fromisoformat(date)
                if parsed_date.isoformat() != date or parsed_date > dt.datetime.now(ZONE).date():
                    raise ValueError()
        except (ValueError, TypeError):
            raise AppError('Enter the actual date completed as YYYY-MM-DD. Every export needs it.') from None
        url, gr_id = book_url(p['url']) if p.get('url') else ('', '')
        with self.store.lock, self.store.db() as db:
            if not db.execute('SELECT 1 FROM books WHERE id=?', (p.get('book_id'),)).fetchone():
                raise AppError('Book not found in your synced catalogue.')
            db.execute('''INSERT INTO gr_matches VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
                title=excluded.title,author=excluded.author,isbn=excluded.isbn,url=excluded.url,gr_id=excluded.gr_id,
                confirmed=excluded.confirmed,public_rating=excluded.public_rating,completed_date=excluded.completed_date,
                ignored=excluded.ignored,updated_at=excluded.updated_at''',
                (p['book_id'], title, author, valid_isbn(code), url, gr_id, int(bool(p.get('confirmed'))), rating, date, int(bool(p.get('ignored'))), now()))
        return {'saved': True}

    def lookup(self, p):
        url, gr_id = book_url(p.get('url', ''))
        with self.lookup_lock:
            if time.monotonic() - self.last_lookup < 5:
                raise AppError('Please wait five seconds between Goodreads lookups.')
            self.last_lookup = time.monotonic()
        # No cookies, credentials, redirects, proxy rotation or challenge bypass.
        req = urllib.request.Request(url, headers={'User-Agent': 'AutomaticBookSelector/1.0 (personal bibliographic lookup)', 'Accept': 'text/html'})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ValueError()
            result = page_metadata(body.decode('utf-8'))
        except AppError:
            raise
        except Exception:
            raise AppError('Goodreads did not allow this lookup. Open the link and copy the print-edition ISBN from Book details & editions. Nothing was changed.') from None
        return result | {'url': url, 'gr_id': gr_id}

    def export(self, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise AppError('Select between 1 and 500 distinct books.')
        with self.store.lock:
            eligible = {b['id']: b for b in self.review()['books']}
            if any(i not in eligible or not eligible[i]['ready'] for i in ids):
                raise AppError('Save and confirm each selected match, ISBN and completion date before exporting. Already exported books are excluded.')
            output = io.StringIO(newline='')
            writer = csv.writer(output, lineterminator='\r\n')
            writer.writerow(HEADERS)
            batch = uuid.uuid4().hex
            entries = []
            for i in ids:
                b = eligible[i]
                writer.writerow([b['title'], b['author'], b['isbn'], b['public_rating'] if b['public_rating'] is not None else '', b['completed_date'], 'read'])
                entries.append((batch, i, identity(b['original_title'], b['original_author'])[0], b['title'], b['author'], b['isbn']))
            data = output.getvalue()
            with self.store.db() as db:
                db.execute('INSERT INTO gr_exports VALUES (?,?,?)', (batch, now(), data))
                db.executemany('INSERT INTO gr_export_items(batch_id,book_id,title_key,title,author,isbn) VALUES (?,?,?,?,?,?)', entries)
        return {'id': batch, 'filename': 'goodreads-' + dt.datetime.now(ZONE).date().isoformat() + '-' + batch[:8] + '.csv', 'csv': data, 'count': len(ids)}

    def batch(self, batch_id):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM gr_exports WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise AppError('Export batch not found.')
        return {'id': batch_id, 'filename': 'goodreads-' + batch_id[:8] + '.csv', 'csv': row['csv']}

    def batch_items(self, batch_id):
        with self.store.db() as db:
            return {'items': [dict(r) for r in db.execute('SELECT book_id AS id,title,state FROM gr_export_items WHERE batch_id=? ORDER BY rowid', (batch_id,))]}

    def reconcile(self, p):
        batch = p.get('batch_id')
        text = p.get('text', '')
        if not isinstance(text, str) or len(text) > 50000:
            raise AppError('Paste Goodreads output shorter than 50,000 characters.')
        with self.store.lock, self.store.db() as db:
            items = [dict(r) for r in db.execute('SELECT * FROM gr_export_items WHERE batch_id=?', (batch,))]
            if not items:
                raise AppError('Choose an export batch first.')
            marked = []
            lines = [normalize(html.unescape(re.sub('<[^>]*>', ' ', line))) for line in text.splitlines() if line.strip()]
            for item in items:
                title, author = normalize(item['title']), normalize(item['author'])
                # A single generic 'failed' is insufficient to guess which book failed.
                if any((item['isbn'] and item['isbn'] in line.replace(' ', '')) or (title and title in line and author and author in line) for line in lines):
                    db.execute("UPDATE gr_export_items SET state='failed',failure=? WHERE batch_id=? AND book_id=?", (text[:4000], batch, item['book_id']))
                    db.execute('UPDATE gr_matches SET confirmed=0 WHERE book_id=?', (item['book_id'],))
                    marked.append(item['title'])
            selected = p.get('failed_ids', [])
            if not isinstance(selected, list) or any(i not in {b['book_id'] for b in items} for i in selected):
                raise AppError('Select failed books from this batch only.')
            for item in items:
                if item['book_id'] in selected and item['title'] not in marked:
                    db.execute("UPDATE gr_export_items SET state='failed',failure=? WHERE batch_id=? AND book_id=?", (text[:4000] or 'Marked failed manually', batch, item['book_id']))
                    db.execute('UPDATE gr_matches SET confirmed=0 WHERE book_id=?', (item['book_id'],))
                    marked.append(item['title'])
            if p.get('confirm_remaining'):
                db.execute("UPDATE gr_export_items SET state='imported' WHERE batch_id=? AND state='exported'", (batch,))
        return {'flagged': marked, 'unrecognized': bool(text.strip()) and not marked,
                'items': [{'id': b['book_id'], 'title': b['title']} for b in items]}
