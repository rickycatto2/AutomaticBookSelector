"""Reviewed public exports, independent of private recommendation feedback."""
import csv
import datetime as dt
import html
import hashlib
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

from core import AppError, NoRedirect, identity, normalize, now, isbn as canonical_isbn
from isbn_catalog import Catalog, authors, work_title

ZONE = ZoneInfo('America/Chicago')
HEADERS = ['Title', 'Author', 'ISBN', 'My Rating', 'Date Read', 'Shelves']


def import_report(text, items):
    """Deliberately narrow grammar: exact ISBN or whole 'title by author' row.

    Unknown formats stay local for explicit selection; never infer failures from
    a title/author substring or from a success row in a mixed report.
    """
    failed, succeeded, reasons = set(), set(), {}
    ambiguous, section, table = False, '', False
    expected_failures, table_rows = None, 0
    failure = r'(?:could not import|unable to import|failed(?: to import)?|not imported|error|rejected)'
    success = r'(?:successfully imported|imported successfully|imported|success|successful)'
    for raw in text.splitlines():
        line = html.unescape(re.sub('<[^>]*>', ' ', raw)).strip()
        if not line:
            continue
        clean = normalize(line)
        if re.fullmatch(r'\d+ books? could not be imported\s*:', line, re.I):
            if expected_failures is not None:
                ambiguous = True
            expected_failures = int(line.split()[0])
            section, table = 'failed', False
            continue
        if section == 'failed' and re.fullmatch(r'ISBN\s+Title\s+Author', line, re.I):
            table = True
            continue
        # Goodreads supplies tab-delimited rows, including a blank Title.
        # Do not inherit failure context into arbitrary prose or success rows.
        cells = line.split('\t')
        table_row = bool(table and len(cells) == 3 and valid_isbn(cells[0].strip()))
        if table_row:
            table_rows += 1
        if clean in ('failed books', 'failed imports', 'errors', 'rejected books'):
            section = 'failed'
            continue
        if clean in ('successfully imported books', 'imported books', 'successful imports'):
            section = 'success'
            continue
        # A status must lead the row; a title containing "Error" or prose such
        # as "no books failed" is not failure evidence.
        negative = bool(re.match(r'^' + failure + r'\s*[:\-]', line, re.I))
        # Remove negative phrases before checking positive status ("not imported").
        positive = bool(re.search(r'\b' + success + r'\b', re.sub(failure, '', line, flags=re.I), re.I))
        if table_row:
            positive = False
        status = 'success' if positive else 'failed' if negative else section
        payload = re.sub(r'^\s*(?:' + failure + '|' + success + r')\s*[:\-]\s*', '', line, flags=re.I)
        if section and not negative and not positive:
            # Section inheritance is only safe for bare identity rows, not
            # arbitrary prose mentioning an ISBN (e.g. "no errors for ISBN").
            bare_code = valid_isbn(re.sub(r'^ISBN\s*:\s*', '', payload, flags=re.I))
            bare_work = any(normalize(payload) == normalize(b['title'] + ' by ' + b['author']) for b in items)
            if not bare_code and not bare_work and not table_row:
                status = ''
        codes = {canonical_isbn(valid_isbn(m.group())) for m in re.finditer(r'(?<![\w-])(?:\d[ -]?){9,}[\dXx](?![\w-])', line)} - {''}
        if table_row:
            codes = {canonical_isbn(valid_isbn(cells[0].strip()))}
        matches = [b for b in items if (valid_isbn(b['isbn']) and canonical_isbn(b['isbn']) in codes)
                   or normalize(payload) == normalize(b['title'] + ' by ' + b['author'])]
        if len(matches) != 1 or not status or (negative and positive):
            ambiguous = True
            # Still protect identifiable successes from automatic/manual failure.
            if positive:
                succeeded.update(b['book_id'] for b in matches)
            continue
        book_id = matches[0]['book_id']
        if status == 'success':
            succeeded.add(book_id)
        else:
            failed.add(book_id)
            reasons[book_id] = line[:500]
    if failed & succeeded or (expected_failures is not None and table_rows != expected_failures):
        ambiguous = True
    failed -= succeeded
    return failed, succeeded, reasons, ambiguous


def source_snapshot(book):
    return hashlib.sha256(json.dumps({k: book.get(k, '') for k in ('id', 'title', 'author', 'isbn', 'language', 'narrator')}, sort_keys=True).encode()).hexdigest()


def work_identity(book):
    return work_title(book['title']), authors(book['author'])


def genuine_date(value):
    try:
        date = dt.date.fromisoformat(value)
        return date.isoformat() == value and date <= dt.datetime.now(ZONE).date()
    except (ValueError, TypeError):
        return False


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
    if re.fullmatch(r'97[89]\d{10}', code):
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
    candidates = []
    for book in books:
        details = book.get('details') or {}
        code = valid_isbn(book.get('isbn') or details.get('isbn13') or details.get('isbn'))
        if code:
            authors = book.get('author') or []
            if isinstance(authors, dict):
                authors = [authors]
            author = ', '.join(a.get('name', '') for a in authors if isinstance(a, dict)) if isinstance(authors, list) else str(authors)
            candidate = {'isbn': code, 'title': book.get('name') or book.get('title') or '', 'author': author}
            if candidate not in candidates:
                candidates.append(candidate)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise AppError('This page contains multiple books or ISBN editions. Open the intended edition and copy its title, author and ISBN for manual review.')
    raise AppError('This page did not provide a usable ISBN. Audiobook pages often list only an ASIN. Use Find print/ebook edition to locate the work, open Book details & editions on a print or ebook edition, and paste its ISBN below — or try Find work ISBN (free catalogs) first.')


class Goodreads:
    def __init__(self, store):
        self.store = store
        self.lookup_lock = threading.Lock()
        self.last_lookup = 0
        self.catalog = Catalog()
        self.auto_lock = threading.Lock()
        self.auto_attempts = {}
        self.auto_budget = []
        self.auto_last = -100
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
            db.execute('''CREATE TABLE IF NOT EXISTS gr_match_context (
                book_id TEXT PRIMARY KEY,source_snapshot TEXT NOT NULL,provenance TEXT NOT NULL,
                reason TEXT NOT NULL,evidence TEXT NOT NULL,automatic INTEGER NOT NULL DEFAULT 0)''')
            db.executescript('''CREATE TABLE IF NOT EXISTS gr_catalog_attempts (
                book_id TEXT NOT NULL,source_snapshot TEXT NOT NULL,result TEXT NOT NULL,
                PRIMARY KEY(book_id,source_snapshot));
              CREATE TABLE IF NOT EXISTS gr_match_jobs (id TEXT PRIMARY KEY,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS gr_match_job_items (
                job_id TEXT NOT NULL,book_id TEXT NOT NULL,source_snapshot TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued',result TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(job_id,book_id));''')

    def review(self):
        with self.store.db() as db:
            history = [dict(r) for r in db.execute('SELECT * FROM history')]
            read_titles = {work_identity(h) for h in history if h['shelf'] == 'read' or h['date_read'] or h['read_count']}
            read_isbns = {canonical_isbn(h['isbn']) for h in history if h['shelf'] == 'read' or h['date_read'] or h['read_count']} - {''}
            seeds = {work_identity(h): h for h in history}
            matches = {r['book_id']: dict(r) for r in db.execute('SELECT * FROM gr_matches')}
            exported = [dict(r) for r in db.execute("""SELECT i.* FROM gr_export_items i
                JOIN gr_exports e ON e.id=i.batch_id WHERE i.state IN ('exported','imported')
                AND NOT EXISTS (SELECT 1 FROM gr_export_items newer JOIN gr_exports ne ON ne.id=newer.batch_id
                    WHERE newer.book_id=i.book_id AND ne.rowid>e.rowid)""")]
            exports = {work_identity(r) for r in exported}
            export_isbns = {canonical_isbn(r['isbn']) for r in exported} - {''}
            exported_ids = {r['book_id'] for r in exported}
            contexts = {r['book_id']: dict(r) for r in db.execute('SELECT * FROM gr_match_context')}
            attempts = {(r['book_id'], r['source_snapshot']): json.loads(r['result'])
                        for r in db.execute('SELECT * FROM gr_catalog_attempts')}
            failures = {r['book_id']: r['failure'] for r in db.execute("SELECT book_id,failure FROM gr_export_items WHERE state='failed' ORDER BY rowid")}
            rows = [dict(r) for r in db.execute('''SELECT b.*,COALESCE(f.dnf,0) AS dnf,
                (SELECT max(completion_timestamp) FROM bookramp_sessions s WHERE s.abs_item_id=b.id AND s.completion_crossed=1) AS ramp_finished
                FROM books b LEFT JOIN feedback f ON f.book_id=b.id
                WHERE b.finished=1 OR b.id IN (SELECT abs_item_id FROM bookramp_sessions WHERE completion_crossed=1)''')]
            batches = [dict(r) for r in db.execute('''SELECT e.id,e.created_at,
                count(i.book_id) AS count,sum(i.state='failed') AS failed,sum(i.state='imported') AS imported
                FROM gr_exports e JOIN gr_export_items i ON i.batch_id=e.id GROUP BY e.id ORDER BY e.created_at DESC LIMIT 20''')]
        seen, books, excluded = set(), [], 0
        # Keep the actual rejected ABS ID visible ahead of duplicate editions.
        for b in sorted(rows, key=lambda b: (b['id'] in failures, str(b['finished_at'] or b['ramp_finished'] or '')), reverse=True):
            key = work_identity(b)
            match = matches.get(b['id'], {})
            if b['dnf'] or key in read_titles or key in exports or canonical_isbn(b['isbn']) in read_isbns | export_isbns or b['id'] in exported_ids or key in seen or match.get('ignored'):
                excluded += 1
                continue
            seen.add(key)
            seed = seeds.get(key, {})
            saved = {'book_id': b['id'], 'title': seed.get('title', b['title']), 'author': seed.get('author', b['author']),
                     'isbn': seed.get('isbn', '') or b['isbn'], 'url': 'https://www.goodreads.com/book/show/' + seed['id'] if seed else '',
                     'confirmed': 0, 'public_rating': None, 'completed_date': completed_date(b['ramp_finished'] or b['finished_at'])} | match
            saved.update(id=b['id'], original_title=b['title'], original_author=b['author'], audio_isbn=b['isbn'], failure=failures.get(b['id'], ''))
            context = contexts.get(b['id'], {})
            saved['source_snapshot'] = source_snapshot(b)
            saved['source_changed'] = bool(match and context.get('source_snapshot') != source_snapshot(b))
            saved['provenance'] = context.get('provenance', 'Unreviewed source metadata')
            saved['reason'] = context.get('reason', 'Review work, authors, language, ISBN and genuine completion date.')
            if saved['source_changed']:
                saved['reason'] = 'Source metadata changed or prior confirmation has no source snapshot. Re-review required; saved details retained.'
            saved['ready'] = bool(not saved['source_changed'] and saved['confirmed'] and valid_isbn(saved['isbn']) and genuine_date(saved['completed_date']) and saved['title'] and saved['author'])
            saved['automatic'] = bool(context.get('automatic'))
            persisted = attempts.get((b['id'], saved['source_snapshot']))
            evidence = persisted or json.loads(context.get('evidence') or '{}')
            saved['score'] = evidence.get('score', 0)
            saved['threshold'] = evidence.get('threshold', 90)
            saved['evidence'] = evidence.get('evidence', [])
            saved['match_eligible'] = not bool(match or saved['failure'] or persisted)
            if persisted and not match:
                saved['reason'] = persisted.get('reason', 'Previous lookup interrupted; use per-book review.')
                saved['provenance'] = persisted.get('provenance') or 'Free catalog check'
                saved['suggestion'] = persisted.get('candidate')
                saved['candidates'] = persisted.get('candidates', [])
            attempt = self.auto_attempts.get(saved['source_snapshot'])
            if attempt and time.monotonic() - attempt[0] < 86400 and not match and not persisted:
                suggestion = attempt[1]
                saved['reason'] = suggestion['reason']
                saved['provenance'] = suggestion.get('provenance') or 'Automatic catalog check'
                saved['suggestion'] = suggestion.get('candidate')
            books.append(saved)
        return {'books': books, 'excluded': excluded, 'batches': batches, 'match_job': self.match_job()}

    def match_job(self, job_id=None):
        with self.store.db() as db:
            job = db.execute('SELECT * FROM gr_match_jobs WHERE id=?', (job_id,)).fetchone() if job_id else db.execute(
                'SELECT * FROM gr_match_jobs ORDER BY rowid DESC LIMIT 1').fetchone()
            if not job:
                return None
            items = [dict(r) for r in db.execute(
                'SELECT book_id AS id,state FROM gr_match_job_items WHERE job_id=? ORDER BY rowid', (job['id'],))]
        return {'id': job['id'], 'items': items}

    def start_match_batch(self):
        """Create/resume a durable queue only; no network or background execution."""
        with self.store.lock, self.store.db() as db:
            job = self.match_job()
            if job and any(i['state'] == 'queued' for i in job['items']):
                return {'job': job}
            books = [b for b in self.review()['books'] if b['match_eligible']][:25]
            if not books:
                return {'job': job}
            job_id = uuid.uuid4().hex
            db.execute('INSERT INTO gr_match_jobs VALUES (?,?)', (job_id, now()))
            db.executemany('INSERT INTO gr_match_job_items(job_id,book_id,source_snapshot) VALUES (?,?,?)',
                           [(job_id, b['id'], b['source_snapshot']) for b in books])
        return {'job': self.match_job(job_id)}

    def resolve_batch_item(self, p):
        ids, job_id = p.get('book_ids'), p.get('batch_id')
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or not isinstance(job_id, str):
            raise AppError('A matching batch resolves one queued ABS book per request.')
        book_id = ids[0]
        with self.store.lock, self.store.db() as db:
            row = db.execute('SELECT * FROM gr_match_job_items WHERE job_id=? AND book_id=?', (job_id, book_id)).fetchone()
            if not row:
                raise AppError('Book is not in this bounded matching batch.')
            if row['state'] != 'queued':
                return {'results': [json.loads(row['result']) | {'id': book_id}], 'job': self.match_job(job_id)}
            book = next((b for b in self.review()['books'] if b['id'] == book_id), None)
            if not book or not book['match_eligible'] or book['source_snapshot'] != row['source_snapshot']:
                result = {'id': book_id, 'automatic': False, 'reason': 'Skipped: decision, source or export state changed.'}
                db.execute("UPDATE gr_match_job_items SET state='skipped',result=? WHERE job_id=? AND book_id=?",
                           (json.dumps(result), job_id, book_id))
                # Return after transaction commit below.
                skipped = True
            else:
                skipped = False
                result = {'id': book_id, 'automatic': False, 'provider_error': True,
                          'reason': 'Lookup in flight or interrupted. No automatic retry; use per-book review if needed.'}
                db.execute("UPDATE gr_match_job_items SET state='pending',result=? WHERE job_id=? AND book_id=?",
                           (json.dumps(result), job_id, book_id))
                db.execute('INSERT INTO gr_catalog_attempts VALUES (?,?,?)',
                           (book_id, row['source_snapshot'], json.dumps(result)))
        if not skipped:
            try:
                result = self.resolve({'book_ids': ids, '_batch_snapshot': row['source_snapshot']})['results'][0]
            except Exception:
                result = {'id': book_id, 'automatic': False, 'provider_error': True,
                          'reason': 'Lookup failed or state changed. Nothing overwritten; review this book explicitly.'}
            state = 'skipped' if result.get('skipped') else 'ready' if result.get('automatic') else 'provider_error' if result.get('provider_error') else 'review'
            with self.store.lock, self.store.db() as db:
                db.execute('UPDATE gr_match_job_items SET state=?,result=? WHERE job_id=? AND book_id=?',
                           (state, json.dumps(result), job_id, book_id))
                db.execute('INSERT OR REPLACE INTO gr_catalog_attempts VALUES (?,?,?)',
                           (book_id, row['source_snapshot'], json.dumps(result)))
        return {'results': [result], 'job': self.match_job(job_id)}

    def save(self, p):
        if any(k in p and type(p[k]) is not bool for k in ('confirmed', 'ignored')):
            raise AppError('Match confirmation and skip flags must be boolean values.')
        if any(not isinstance(p.get(k, ''), str) for k in ('title', 'author', 'isbn', 'url', 'completed_date')):
            raise AppError('Book details must be text values.')
        title, author = p.get('title', '').strip(), p.get('author', '').strip()
        if not title or not author or max(len(title), len(author)) > 1000 or title[0] in '=+-@' or author[0] in '=+-@':
            raise AppError('Enter the correct title and author(s) (up to 1,000 characters each).')
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
            source = db.execute('SELECT * FROM books WHERE id=?', (p.get('book_id'),)).fetchone()
            if not source:
                raise AppError('Book not found in your synced catalogue.')
            if p.get('source_snapshot') and p['source_snapshot'] != source_snapshot(dict(source)):
                raise AppError('Source changed while this form was open. Reload and re-review before saving.')
            db.execute('''INSERT INTO gr_matches VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
                title=excluded.title,author=excluded.author,isbn=excluded.isbn,url=excluded.url,gr_id=excluded.gr_id,
                confirmed=excluded.confirmed,public_rating=excluded.public_rating,completed_date=excluded.completed_date,
                ignored=excluded.ignored,updated_at=excluded.updated_at''',
                 (p['book_id'], title, author, valid_isbn(code), url, gr_id, int(bool(p.get('confirmed'))), rating, date, int(bool(p.get('ignored'))), now()))
            db.execute('''INSERT INTO gr_match_context VALUES (?,?,?,?,?,0) ON CONFLICT(book_id) DO UPDATE SET
                source_snapshot=excluded.source_snapshot,provenance=excluded.provenance,reason=excluded.reason,evidence=excluded.evidence,automatic=0''',
                (p['book_id'], source_snapshot(dict(source)), 'Manual review (Goodreads acceptance unverified)',
                 'User-reviewed work, authors, language, ISBN and actual completion date.', '{}'))
        return {'saved': True}

    def resolve(self, p):
        if 'batch_id' in p:
            return self.resolve_batch_item(p)
        ids = p.get('book_ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise AppError('Choose 1–10 distinct completed books for this explicit action.')
        results = []
        for book_id in ids:
            with self.store.lock:
                eligible = {b['id']: b for b in self.review()['books']}
                if book_id not in eligible:
                    raise AppError('Only completed, not-yet-exported review entries can be resolved.')
                with self.store.db() as db:
                    source = dict(db.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone())
                    prior = db.execute('SELECT 1 FROM gr_matches WHERE book_id=?', (book_id,)).fetchone()
                if p.get('_batch_snapshot') and (prior or eligible[book_id]['failure']
                                                or p['_batch_snapshot'] != source_snapshot(source)):
                    results.append({'id': book_id, 'automatic': False, 'skipped': True,
                                    'reason': 'Skipped: source or saved decision changed before lookup.'})
                    continue
            result = self.catalog.resolve(source)
            # Saved/manual/failed/stale decisions are never overwritten by automation.
            automatic = bool(result['high_confidence'] and not prior and not eligible[book_id]['failure']
                             and genuine_date(eligible[book_id]['completed_date']))
            with self.store.lock, self.store.db() as db:
                current = dict(db.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone())
                latest = {b['id']: b for b in self.review()['books']}.get(book_id)
                if not latest or latest['completed_date'] != eligible[book_id]['completed_date']:
                    raise AppError('Completion/export state changed during lookup. Reload and review.')
                if source_snapshot(current) != source_snapshot(source):
                    raise AppError('Source changed during lookup; nothing saved for this book. Reload and review.')
                if db.execute('SELECT 1 FROM gr_matches WHERE book_id=?', (book_id,)).fetchone() or latest['failure']:
                    automatic = False
                candidate = result.get('candidate')
                if candidate:
                    prior_reads = [dict(r) for r in db.execute("SELECT title,author,isbn FROM history WHERE shelf='read' OR date_read<>'' OR read_count>0")]
                    prior_exports = [dict(r) for r in db.execute("SELECT title,author,isbn FROM gr_export_items WHERE state IN ('exported','imported')")]
                    if any(work_identity(candidate) == work_identity(r) or canonical_isbn(candidate['isbn']) == canonical_isbn(r['isbn'])
                           for r in prior_reads + prior_exports):
                        automatic = False
                        result = result | {'high_confidence': False, 'skipped': True,
                                           'reason': 'Skipped: candidate work/ISBN already in Goodreads read history or an existing export.'}
                if automatic:
                    candidate = result['candidate']
                    db.execute('INSERT INTO gr_matches VALUES (?,?,?,?,?,?,1,NULL,?,0,?)',
                               (book_id, candidate['title'], candidate['author'], candidate['isbn'], '', '', eligible[book_id]['completed_date'], now()))
                    db.execute('INSERT OR REPLACE INTO gr_match_context VALUES (?,?,?,?,?,1)',
                                (book_id, source_snapshot(source), result['provenance'], result['reason'], json.dumps(result)))
                db.execute('INSERT OR REPLACE INTO gr_catalog_attempts VALUES (?,?,?)',
                           (book_id, source_snapshot(source), json.dumps(result)))
            results.append(result | {'id': book_id, 'automatic': automatic,
                                     'reason': result['reason'] + ('' if automatic else ' Not auto-saved: review candidate, existing decision and completion date.')})
        return {'results': results}

    def auto_resolve(self):
        """One book per list load, ten per rolling day/process; never a bulk job.

        Fingerprint attempts (including uncertainty/errors) are retained durably.
        Existing decisions and failed imports are never sent or overwritten.
        """
        if not self.auto_lock.acquire(blocking=False):
            return {'results': [], 'deferred': True}
        try:
            stamp = time.monotonic()
            self.auto_budget = [s for s in self.auto_budget if stamp - s < 86400]
            self.auto_attempts = {k: v for k, v in self.auto_attempts.items() if stamp - v[0] < 86400}
            if len(self.auto_budget) >= 10 or stamp - self.auto_last < 5:
                return {'results': [], 'deferred': True}
            with self.store.lock, self.store.db() as db:
                saved_ids = {r[0] for r in db.execute('SELECT book_id FROM gr_matches')}
                book = next((b for b in self.review()['books'] if b['match_eligible'] and b['id'] not in saved_ids
                             and not b['failure'] and b['source_snapshot'] not in self.auto_attempts), None)
            if not book:
                return {'results': [], 'deferred': False}
            self.auto_budget.append(stamp)
            self.auto_last = stamp
            result = self.resolve({'book_ids': [book['id']]})
            self.auto_attempts[book['source_snapshot']] = (stamp, result['results'][0])
            return result | {'deferred': False}
        finally:
            self.auto_lock.release()

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
            return {'items': [dict(r) for r in db.execute('SELECT book_id AS id,title,author,isbn,state,substr(failure,1,500) AS failure FROM gr_export_items WHERE batch_id=? ORDER BY rowid', (batch_id,))]}

    def reconcile(self, p):
        batch = p.get('batch_id')
        text = p.get('text', '')
        if not isinstance(text, str) or len(text) > 50000:
            raise AppError('Paste Goodreads output shorter than 50,000 characters.')
        if any(k in p and type(p[k]) is not bool for k in ('confirm_remaining', 'preview')):
            raise AppError('Import confirmation and preview must be boolean values.')
        with self.store.lock, self.store.db() as db:
            items = [dict(r) for r in db.execute('SELECT * FROM gr_export_items WHERE batch_id=?', (batch,))]
            if not items:
                raise AppError('Choose an export batch first.')
            selected = p.get('failed_ids', [])
            if not isinstance(selected, list) or any(not isinstance(i, str) or i not in {b['book_id'] for b in items} for i in selected) or len(set(selected)) != len(selected):
                raise AppError('Select failed books from this batch only.')
            detected, succeeded, reasons, ambiguous = import_report(text, items)
            if set(selected) & succeeded:
                raise AppError('Selected book has a success line. Remove that line or correct the selection before marking a failure.')
            manual = bool(selected)
            # A manual/empty selection cannot silently erase explicit failures
            # and then confirm those rejected books as imported.
            chosen = set(selected) | detected
            unresolved = ambiguous and not manual
            # Empty input is always a no-op, even with remaining confirmation.
            no_op = not text.strip() and not selected
            apply = not p.get('preview', False) and not no_op
            marked = []
            for item in items:
                if item['book_id'] in chosen:
                    marked.append(item['title'])
                    if apply:
                        newer = db.execute('''SELECT 1 FROM gr_export_items i JOIN gr_exports e ON e.id=i.batch_id
                            WHERE i.book_id=? AND e.rowid>(SELECT rowid FROM gr_exports WHERE id=?) LIMIT 1''', (item['book_id'], batch)).fetchone()
                        if newer:
                            raise AppError('This book has a newer export. Reconcile the latest batch; nothing was changed.')
                        reason = reasons.get(item['book_id'], 'Marked failed by explicit per-book selection')
                        db.execute("UPDATE gr_export_items SET state='failed',failure=? WHERE batch_id=? AND book_id=?", (reason, batch, item['book_id']))
                        if item['state'] != 'failed':
                            db.execute('UPDATE gr_matches SET confirmed=0 WHERE book_id=?', (item['book_id'],))
            can_confirm = not unresolved and not no_op and bool(chosen or succeeded)
            if apply and p.get('confirm_remaining') and can_confirm:
                db.execute("UPDATE gr_export_items SET state='imported',failure='' WHERE batch_id=? AND state='exported'", (batch,))
            current = [dict(r) for r in db.execute('SELECT book_id AS id,title,author,isbn,state,substr(failure,1,500) AS failure FROM gr_export_items WHERE batch_id=? ORDER BY rowid', (batch,))]
        return {'flagged': marked, 'unrecognized': bool(text.strip()) and not detected and not succeeded and not manual,
                'ambiguous': unresolved, 'mixed': bool(succeeded and detected), 'can_confirm_remaining': can_confirm,
                'preview': bool(p.get('preview')), 'no_op': no_op,
                'items': [b | {'proposed_failed': b['id'] in chosen, 'reported_success': b['id'] in succeeded,
                              'reason': reasons.get(b['id'], '')} for b in current]}
