"""Single-user, local reading companion. No runtime third-party dependencies."""
import collections
import contextlib
import csv
import datetime
import hashlib
import html
import io
import json
import math
import os
import pathlib
import re
import sqlite3
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load_env(path):
    """Read local dotenv without executing values or replacing process configuration."""
    if pathlib.Path(path).exists():
        for line in pathlib.Path(path).read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                key = key.strip()
                if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                    os.environ.setdefault(key, value.strip().strip('\"\''))


def normalize(value):
    value = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.findall(r'[a-z0-9]+', value))


def identity(title, author):
    # Goodreads often appends series/edition details; subtitles are ignored only
    # together with an exact author match. No title-only fuzzy exclusions.
    return normalize(re.sub(r'\([^)]*\)', '', title).split(':')[0]), normalize(author)


def author_parts(author):
    return {normalize(part) for part in re.split(r',|;|\s+&\s+|\s+and\s+', author or '') if normalize(part)}


def canonical_author(author, known):
    # Some ABS metadata includes narrators in authorName. Match an exact known
    # Goodreads/preferred author component, never a surname or fuzzy name.
    matches = author_parts(author) & known
    return next(iter(matches)) if len(matches) == 1 else normalize(author)


def isbn(value):
    code = re.sub(r'[^0-9Xx]', '', str(value or '')).upper()
    if len(code) == 10:
        body = '978' + code[:9]
        code = body + str((10 - sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(body)) % 10) % 10)
    return code if len(code) == 13 and code.isdigit() else ''


def plain(value):
    return html.unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))


STOP = set('the a an and or to of in on for with by is it as at from that this her his their she he they you your our are was were be has have had not but into about novel book audiobook story new one two first life time woman man people'.split())


def tokens(value):
    return {t for t in normalize(plain(value)).split() if len(t) > 2 and t not in STOP}


DIMENSIONS = {
    'voice': ('Distinctive voice', 'voice witty wit observant sharp intimate incisive narrated narrator'),
    'intimacy': ('Emotional intimacy', 'intimate emotional grief love friendship desire vulnerability marriage relationships family'),
    'reinvention': ('Reinvention', 'reinvention identity transformation crisis change midlife middle age self discovery unravel destabilization'),
    'humor': ('Wit and dark humor', 'humor humour funny comic comedy satire satirical hilarious dark witty absurd'),
    'strangeness': ('A little strangeness', 'strange surreal surrealism weird uncanny experimental speculative magical fantastical'),
    'literary': ('Literary fiction', 'literary contemporary fiction literary fiction prize psychological character'),
}
DEFAULT_PROFILE = {
    'likes': 'Contemporary literary fiction, strong voice, emotional intimacy, messy complicated adults, identity and relationships, wit, dark humor, destabilization and reinvention.',
    'avoids': '',
    'preferred_authors': 'Naima Brown, Nathan Hill, Miranda July, Hattie Williams, Anelise Chen, V. E. Schwab, Taylor Jenkins Reid, Alison Espach',
    'author_preference': 'Primarily female writers. Author gender is not inferred from names; add preferred authors to prioritize them.',
    'max_hours': 0,
    'weights': {'voice': 3, 'intimacy': 3, 'reinvention': 3, 'humor': 3, 'strangeness': 2, 'literary': 3},
}


class AppError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward Authorization to a redirected host.


class ABS:
    def __init__(self, url, token):
        self.url = url.rstrip('/')
        self.token = token
        parsed = urllib.parse.urlsplit(self.url)
        if self.url and (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise AppError('ABS_URL must be an http(s) server URL without credentials or query parameters.')
        if os.getenv('IN_DOCKER') == '1' and parsed.hostname in ('localhost', '127.0.0.1', '::1'):
            host = 'host.docker.internal' + (':' + str(parsed.port) if parsed.port else '')
            self.url = urllib.parse.urlunsplit(parsed._replace(netloc=host))
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, payload=None):
        if not self.url or not self.token:
            raise AppError('Set ABS_URL and ABS_TOKEN in your local .env file, then restart.')
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json', 'Accept': 'application/json',
        })
        try:
            with self.opener.open(req, timeout=45) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                raise AppError('ABS redirected the request. Set ABS_URL to its final server address.') from None
            raise AppError(f'ABS returned HTTP {error.code}. Check the token, permissions and server address.') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AppError('Could not reach ABS. Check the server address and connection.') from None
        except (ValueError, UnicodeError):
            raise AppError('ABS returned an unexpected response. Check the server address and version.') from None

    def get(self, path):
        return self.request('GET', path)

    def pages(self, path):
        results, page = [], 0
        while True:
            response = self.get(path + ('&' if '?' in path else '?') + f'limit=200&page={page}')
            batch = response.get('results', [])
            if not isinstance(batch, list) or not isinstance(response.get('total'), int):
                raise AppError('Unexpected ABS catalogue response; sync was not saved.')
            results.extend(batch)
            if len(results) >= response['total']:
                return results
            if not batch:
                raise AppError('ABS pagination stopped early; sync was not saved.')
            page += 1
            if page > 10000:
                raise AppError('ABS catalogue is too large for this sync.')


class Store:
    def __init__(self, path, abs_client):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.abs = abs_client
        self.lock = threading.RLock()
        self.sync_state = {'running': False, 'phase': 'Waiting for first sync', 'error': None}
        self.revision = 0
        self.cache = None
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, isbn TEXT,
                rating INTEGER, shelf TEXT, date_read TEXT, shelves TEXT, review TEXT,
                read_count INTEGER, title_key TEXT, author_key TEXT);
              CREATE TABLE IF NOT EXISTS books (
                id TEXT PRIMARY KEY, library_id TEXT, title TEXT, author TEXT, isbn TEXT,
                description TEXT, genres TEXT, tags TEXT, duration REAL, narrator TEXT,
                available INTEGER, title_key TEXT, author_key TEXT, progress REAL DEFAULT 0,
                finished INTEGER DEFAULT 0, finished_at TEXT);
              CREATE TABLE IF NOT EXISTS feedback (
                book_id TEXT PRIMARY KEY, rating INTEGER, notes TEXT, enjoyed TEXT, avoid TEXT,
                dnf INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0, updated_at TEXT);
              CREATE TABLE IF NOT EXISTS completion_questions (
                book_id TEXT PRIMARY KEY, finished_at TEXT, dismissed INTEGER DEFAULT 0);
              CREATE TABLE IF NOT EXISTS queue (book_id TEXT, playlist_id TEXT, added_at TEXT,
                PRIMARY KEY (book_id, playlist_id));
              CREATE INDEX IF NOT EXISTS books_identity ON books(title_key, author_key);
            ''')
            if not self.meta(db, 'profile'):
                self.set_meta(db, 'profile', DEFAULT_PROFILE)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def meta(self, db, key, default=None):
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, db, key, value):
        db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))

    def changed(self):
        self.revision += 1
        self.cache = None

    def import_csv(self, raw, explicit=False):
        if len(raw) > 20 * 1024 * 1024:
            raise AppError('CSV exceeds the 20 MB import limit.')
        try:
            reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
            required = {'Book Id', 'Title', 'Author', 'My Rating', 'Exclusive Shelf'}
            if not required.issubset(reader.fieldnames or []):
                raise AppError('Please select the original Goodreads library export CSV.')
            rows = list(reader)
            if len(rows) > 100000:
                raise AppError('CSV contains too many rows.')
            values = []
            for row in rows:
                if not row.get('Book Id') or not row.get('Title') or not row.get('Author'):
                    raise AppError('CSV has a row missing book ID, title or author. No rows were imported.')
                rating_value = float(row.get('My Rating') or 0)
                count_value = float(row.get('Read Count') or 0)
                if not rating_value.is_integer() or not count_value.is_integer():
                    raise ValueError()
                rating = int(rating_value)
                count = int(count_value)
                if rating not in range(6) or count < 0:
                    raise ValueError()
                key = identity(row['Title'], row['Author'])
                values.append((row['Book Id'], row['Title'], row['Author'], isbn(row.get('ISBN13') or row.get('ISBN')), rating,
                               row.get('Exclusive Shelf'), row.get('Date Read'), row.get('Bookshelves'), row.get('My Review'), count, *key))
        except (UnicodeError, csv.Error, ValueError, TypeError):
            raise AppError('Could not read this Goodreads CSV. Check encoding and rating columns; no rows were imported.') from None
        if not values:
            raise AppError('The CSV contains no books.')
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock, self.db() as db:
            if self.meta(db, 'goodreads_import') and not explicit:
                return {'skipped': True, 'reason': 'Goodreads already bootstrapped'}
            if self.meta(db, 'goodreads_hash') == digest:
                return {'skipped': True, 'reason': 'This export was already imported'}
            db.executemany('''INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, author=excluded.author,
                isbn=excluded.isbn, rating=excluded.rating, shelf=excluded.shelf, date_read=excluded.date_read,
                shelves=excluded.shelves, review=excluded.review, read_count=excluded.read_count,
                title_key=excluded.title_key, author_key=excluded.author_key''', values)
            self.set_meta(db, 'goodreads_import', {'at': now(), 'rows': len(values)})
            self.set_meta(db, 'goodreads_hash', digest)
            self.changed()
        return {'imported': len(values)}

    def bootstrap(self, folder):
        with self.db() as db:
            if self.meta(db, 'goodreads_import'):
                return
        files = sorted(pathlib.Path(folder).glob('*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            self.import_csv(files[0].read_bytes())

    def sync(self):
        with self.lock:
            if self.sync_state['running']:
                return {'already_running': True}
            self.sync_state = {'running': True, 'phase': 'Connecting to your library', 'error': None}
        try:
            me = self.abs.get('/api/me')
            user_id = me.get('id')
            if not user_id:
                raise AppError('ABS did not return a user identity.')
            with self.db() as db:
                if self.meta(db, 'abs_user_id', user_id) != user_id:
                    raise AppError('This database belongs to another ABS user. Use a separate DATA_DIR for a different account.')
                known_authors = {r[0] for r in db.execute('SELECT DISTINCT author_key FROM history')}
                known_authors |= {normalize(a) for a in self.meta(db, 'profile')['preferred_authors'].split(',') if a.strip()}
            libs = self.abs.get('/api/libraries').get('libraries', [])
            wanted = {s.strip() for s in os.getenv('ABS_LIBRARY_IDS', '').split(',') if s.strip()}
            libs = [l for l in libs if l.get('mediaType') == 'book' and (not wanted or l['id'] in wanted)]
            if not libs or (wanted and wanted - {l['id'] for l in libs}):
                raise AppError('No matching book libraries, or one configured library is inaccessible.')
            incoming = []
            for lib in libs:
                self.sync_state['phase'] = f'Reading {lib["name"]}'
                for item in self.abs.pages('/api/libraries/' + urllib.parse.quote(lib['id'], safe='') + '/items?minified=0'):
                    if item.get('mediaType') != 'book':
                        continue
                    media = item.get('media', {})
                    meta = media.get('metadata', {})
                    author = meta.get('authorName') or ', '.join(a['name'] for a in meta.get('authors', []))
                    title = meta.get('title') or 'Untitled'
                    audio = media.get('numAudioFiles', media.get('numTracks', 0))
                    available = bool(audio and not item.get('isMissing') and not item.get('isInvalid'))
                    incoming.append((item['id'], lib['id'], title, author, isbn(meta.get('isbn')), plain(meta.get('description')),
                                     json.dumps(meta.get('genres') or []), json.dumps(media.get('tags') or []), media.get('duration') or 0,
                                     meta.get('narratorName') or '', int(available), identity(title, author)[0], canonical_author(author, known_authors)))
            progress = me.get('mediaProgress')
            if progress is None:
                progress = self.abs.get('/api/me/progress').get('mediaProgress')
            if not isinstance(progress, list):
                raise AppError('ABS did not return progress data; sync was not saved.')
            with self.lock, self.db() as db:
                # All network reads finish before changing the snapshot.
                db.execute('UPDATE books SET available=0')
                db.executemany('''INSERT INTO books
                  (id,library_id,title,author,isbn,description,genres,tags,duration,narrator,available,title_key,author_key)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                  library_id=excluded.library_id,title=excluded.title,author=excluded.author,isbn=excluded.isbn,
                  description=excluded.description,genres=excluded.genres,tags=excluded.tags,duration=excluded.duration,
                  narrator=excluded.narrator,available=excluded.available,title_key=excluded.title_key,author_key=excluded.author_key''', incoming)
                db.execute('UPDATE books SET progress=0,finished=0,finished_at=NULL')
                for entry in progress:
                    if entry.get('episodeId') or not entry.get('libraryItemId'):
                        continue
                    finished = bool(entry.get('isFinished'))
                    db.execute('UPDATE books SET progress=?, finished=?, finished_at=? WHERE id=?',
                               (max(0, min(1, float(entry.get('progress') or 0))), int(finished), str(entry.get('finishedAt') or ''), entry['libraryItemId']))
                    if finished:
                        db.execute('INSERT OR IGNORE INTO completion_questions(book_id,finished_at) VALUES (?,?)',
                                   (entry['libraryItemId'], str(entry.get('finishedAt') or '')))
                self.set_meta(db, 'libraries', [{'id': l['id'], 'name': l['name']} for l in libs])
                self.set_meta(db, 'abs_user_id', user_id)
                self.set_meta(db, 'abs_username', me.get('username', ''))
                self.set_meta(db, 'last_sync', now())
                self.changed()
            self.sync_state = {'running': False, 'phase': 'Library up to date', 'error': None}
            return {'synced': len(incoming), 'progress_records': len(progress)}
        except Exception as error:
            message = str(error) if isinstance(error, AppError) else 'Sync failed unexpectedly; your previous library snapshot is retained.'
            self.sync_state = {'running': False, 'phase': 'Sync needs attention', 'error': message}
            raise AppError(message) from None

    def profile(self, payload=None):
        with self.lock, self.db() as db:
            if payload is not None:
                cleaned = {}
                for key in ('likes', 'avoids', 'preferred_authors', 'author_preference'):
                    if not isinstance(payload.get(key), str) or len(payload[key]) > 5000:
                        raise AppError('Profile text must be shorter than 5,000 characters per field.')
                    cleaned[key] = payload[key].strip()
                try:
                    cleaned['max_hours'] = float(payload.get('max_hours', 0))
                    cleaned['weights'] = {k: int(payload['weights'][k]) for k in DIMENSIONS}
                    if not 0 <= cleaned['max_hours'] <= 200 or any(not -3 <= v <= 3 for v in cleaned['weights'].values()):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise AppError('Invalid profile weights or listening length.') from None
                self.set_meta(db, 'profile', cleaned)
                self.changed()
            return self.meta(db, 'profile')

    def feedback(self, payload):
        book_id = payload.get('book_id')
        rating = payload.get('rating')
        if rating is not None and (type(rating) is not int or rating not in range(1, 6)):
            raise AppError('Rating must be 1 to 5, or left blank.')
        if any(not isinstance(payload.get(k, ''), str) or len(payload.get(k, '')) > 5000 for k in ('notes', 'enjoyed', 'avoid')):
            raise AppError('Feedback text is too long or invalid.')
        with self.lock, self.db() as db:
            if not db.execute('SELECT id FROM books WHERE id=?', (book_id,)).fetchone():
                raise AppError('Book was not found in your synced library.')
            db.execute('''INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
              rating=excluded.rating,notes=excluded.notes,enjoyed=excluded.enjoyed,avoid=excluded.avoid,
              dnf=excluded.dnf,dismissed=excluded.dismissed,updated_at=excluded.updated_at''',
                       (book_id, rating, payload.get('notes', ''), payload.get('enjoyed', ''), payload.get('avoid', ''),
                        int(bool(payload.get('dnf'))), int(bool(payload.get('dismissed'))), now()))
            db.execute('UPDATE completion_questions SET dismissed=1 WHERE book_id=?', (book_id,))
            self.changed()
        return {'saved': True}

    def dismiss_question(self, book_id):
        with self.lock, self.db() as db:
            db.execute('UPDATE completion_questions SET dismissed=1 WHERE book_id=?', (book_id,))
        return {'saved': True}

    def snapshot(self):
        with self.db() as db:
            stats = {
                'catalogue': db.execute('SELECT count(*) FROM books WHERE available=1').fetchone()[0],
                'history': db.execute('SELECT count(*) FROM history').fetchone()[0],
                'abs_finished': db.execute('SELECT count(*) FROM books WHERE finished=1').fetchone()[0],
                'in_progress': db.execute('SELECT count(*) FROM books WHERE finished=0 AND progress>0').fetchone()[0],
                'feedback': db.execute('SELECT count(*) FROM feedback').fetchone()[0],
            }
            questions = [dict(r) for r in db.execute('''SELECT b.id,b.title,b.author,b.finished_at,
              EXISTS(SELECT 1 FROM queue WHERE book_id=b.id) AS recommended
              FROM books b JOIN completion_questions q ON b.id=q.book_id
              WHERE b.finished=1 AND q.dismissed=0 AND NOT EXISTS(SELECT 1 FROM feedback f WHERE f.book_id=b.id)
              ORDER BY recommended DESC, b.finished_at DESC LIMIT 30''')]
            return {'stats': stats, 'sync': dict(self.sync_state), 'last_sync': self.meta(db, 'last_sync'),
                    'goodreads_import': self.meta(db, 'goodreads_import'), 'libraries': self.meta(db, 'libraries', []),
                    'username': self.meta(db, 'abs_username'), 'questions': questions, 'profile': self.meta(db, 'profile'),
                    'dimensions': {k: v[0] for k, v in DIMENSIONS.items()}, 'configured': bool(self.abs.url and self.abs.token)}

    def recommendations(self, library_id='', search='', limit=36):
        with self.lock:
            if self.cache is None:
                self.cache = self._rank()
            rows = [r for r in self.cache if (not library_id or r['library_id'] == library_id) and
                    (not search or normalize(search) in normalize(r['title'] + ' ' + r['author']))]
            return {'books': rows[:limit], 'eligible': len(rows)}

    def _rank(self):
        with self.db() as db:
            books = [dict(r) for r in db.execute('SELECT * FROM books WHERE available=1')]
            history = [dict(r) for r in db.execute('SELECT * FROM history')]
            feedback = {r['book_id']: dict(r) for r in db.execute('SELECT * FROM feedback')}
            queued = {r[0] for r in db.execute('SELECT book_id FROM queue')}
            profile = self.meta(db, 'profile')
        # Match seed history by ISBN or title AND author; never by title alone.
        by_key, by_isbn = {}, {}
        read_keys, read_isbns = set(), set()
        author_values = collections.defaultdict(list)
        for h in history:
            key = (h['title_key'], h['author_key'])
            by_key[key] = h
            if h['isbn']:
                by_isbn[h['isbn']] = h
            if h['shelf'] == 'read' or h['date_read'] or h['read_count']:
                read_keys.add(key)
                if h['isbn']:
                    read_isbns.add(h['isbn'])
            if h['rating']:
                author_values[h['author_key']].append((h['rating'] - 3) / 2)
        known_authors = {h['author_key'] for h in history} | {normalize(a) for a in profile['preferred_authors'].split(',') if a.strip()}
        for b in books:
            b['author_key'] = canonical_author(b['author'], known_authors)
        documents = {b['id']: tokens(' '.join((b['title'], b['description'], b['genres'], b['tags']))) for b in books}
        frequency = collections.Counter(t for terms in documents.values() for t in terms)
        idf = {t: math.log((len(books) + 1) / (n + 1)) + 1 for t, n in frequency.items()}
        positive, negative = collections.Counter(), collections.Counter()
        for term in tokens(profile['likes']):
            positive[term] += 4
        for term in tokens(profile['avoids']):
            negative[term] += 5
        trained = set()
        # Count each work once when multiple audiobook editions match.
        for b in books:
            key = (b['title_key'], b['author_key'])
            h = by_isbn.get(b['isbn']) if b['isbn'] else None
            h = h or by_key.get(key)
            f = feedback.get(b['id'], {})
            rating = f.get('rating') if f.get('rating') is not None else (h or {}).get('rating')
            if rating and key not in trained:
                target = positive if rating > 3 else negative
                weight = abs(rating - 3)
                for term in documents[b['id']]:
                    target[term] += weight
                trained.add(key)
            if f.get('rating'):
                author_values[b['author_key']].append((f['rating'] - 3) / 2)
            for term in tokens(f.get('enjoyed', '')):
                positive[term] += 6
            for term in tokens(f.get('avoid', '')):
                negative[term] += 6
        # Off-catalogue Goodreads shelves can add weak topical hints. Reviews are
        # stored, but not blindly interpreted as positive text (they may be critical).
        for h in history:
            if h['rating'] >= 4:
                for term in tokens(h['shelves']):
                    positive[term] += .5
        pref_authors = {normalize(a) for a in profile['preferred_authors'].split(',') if a.strip()}
        positives = {t: min(8, w) * idf.get(t, 1) for t, w in positive.items() if t in idf}
        negatives = {t: min(8, w) * idf.get(t, 1) for t, w in negative.items() if t in idf}
        pos_norm = math.sqrt(sum(v * v for v in positives.values())) or 1
        neg_norm = math.sqrt(sum(v * v for v in negatives.values())) or 1
        read_feedback_keys = {(b['title_key'], b['author_key']) for b in books if feedback.get(b['id'], {}).get('rating') or feedback.get(b['id'], {}).get('dnf')}
        abs_finished_keys = {(b['title_key'], b['author_key']) for b in books if b['finished']}
        results = []
        for b in books:
            key = (b['title_key'], b['author_key'])
            f = feedback.get(b['id'], {})
            if b['finished'] or b['progress'] > 0 or key in read_keys or key in abs_finished_keys or key in read_feedback_keys or (b['isbn'] and b['isbn'] in read_isbns) or f.get('dismissed') or f.get('dnf'):
                continue
            if profile['max_hours'] and b['duration'] > profile['max_hours'] * 3600:
                continue
            terms = documents[b['id']]
            norm = math.sqrt(sum(idf[t] ** 2 for t in terms)) or 1
            score = 100 * sum(positives.get(t, 0) * idf[t] for t in terms) / (pos_norm * norm)
            score -= 70 * sum(negatives.get(t, 0) * idf[t] for t in terms) / (neg_norm * norm)
            reasons, cautions = [], []
            if b['author_key'] in pref_authors:
                score += 22
                reasons.append('By an author you explicitly want to read more of')
            affinity = author_values.get(b['author_key'], [])
            if affinity:
                avg = sum(affinity) / len(affinity)
                score += avg * 16
                if avg > .25:
                    reasons.append('You rated other books by this author highly')
                elif avg < -.25:
                    cautions.append('Your past ratings for this author were lower')
            for dimension, (label, words) in DIMENSIONS.items():
                matched = terms & tokens(words)
                weight = profile['weights'][dimension]
                if matched:
                    score += weight * min(3, len(matched)) * 1.5
                    if weight > 1:
                        reasons.append(label + ': catalogue mentions ' + ', '.join(sorted(matched)[:3]))
            overlap = sorted(terms & positive.keys(), key=lambda t: positives.get(t, 0), reverse=True)[:3]
            if overlap and not reasons:
                reasons.append('Matches themes in your profile or highly rated books: ' + ', '.join(overlap))
            avoid = sorted(terms & tokens(profile['avoids']))[:3]
            if avoid:
                cautions.append('Also mentions themes you want less of: ' + ', '.join(avoid))
            if not reasons:
                reasons.append('An exploratory pick from your unread library; little preference evidence yet')
            if not b['description']:
                cautions.append('No description available; confidence is limited')
            h = by_isbn.get(b['isbn']) if b['isbn'] else None
            h = h or by_key.get(key)
            if h and h['shelf'] == 'to-read':
                score += 3
                reasons.append('Already on your Goodreads to-read shelf')
            results.append({k: b[k] for k in ('id', 'library_id', 'title', 'author', 'description', 'duration', 'narrator')} |
                           {'genres': json.loads(b['genres']), 'score': round(score, 1), 'reasons': reasons[:3], 'cautions': cautions,
                            'queued': b['id'] in queued})
        results.sort(key=lambda b: (-b['score'], b['title'], b['id']))
        # Prefer one edition per work in discovery while keeping exact ABS IDs.
        seen, unique = set(), []
        for b in results:
            key = identity(b['title'], b['author'])
            if key not in seen:
                unique.append(b)
                seen.add(key)
        return unique

    def playlists(self, library_id):
        with self.db() as db:
            libraries = self.meta(db, 'libraries', [])
            if library_id not in {l['id'] for l in libraries}:
                raise AppError('Select a synced book library first.')
        payload = self.abs.get('/api/libraries/' + urllib.parse.quote(library_id, safe='') + '/playlists')
        rows = payload.get('results', payload.get('playlists', [])) if isinstance(payload, dict) else payload
        return [{'id': p['id'], 'name': p['name'], 'count': len(p.get('items', []))} for p in rows]

    def queue_books(self, payload):
        ids = payload.get('book_ids', [])
        library_id = payload.get('library_id')
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) for i in ids):
            raise AppError('Choose up to 100 books.')
        ids = list(dict.fromkeys(ids))
        # This lock also serializes retries so one server process never creates two
        # same-named playlists or sends the same additions concurrently.
        with self.lock, self.db() as db:
            if library_id not in {l['id'] for l in self.meta(db, 'libraries', [])}:
                raise AppError('Select a synced library first.')
            for book_id in ids:
                row = db.execute('SELECT available,library_id FROM books WHERE id=?', (book_id,)).fetchone()
                if not row or not row['available'] or row['library_id'] != library_id:
                    raise AppError('Every selected book must be available in the same library.')
            playlist_id = payload.get('playlist_id', '')
            if playlist_id:
                pl = self.abs.get('/api/playlists/' + urllib.parse.quote(playlist_id, safe=''))
                if pl.get('libraryId') != library_id or pl.get('userId') != self.meta(db, 'abs_user_id'):
                    raise AppError('The playlist must belong to your listening account and selected library.')
            else:
                name = str(payload.get('name') or 'AutomaticBookSelector').strip()
                if not name or len(name) > 100:
                    raise AppError('Playlist name must be 1 to 100 characters.')
                matches = [p for p in self.playlists(library_id) if p['name'] == name]
                if len(matches) > 1:
                    raise AppError('Multiple playlists have this name. Select the intended playlist explicitly.')
                if matches:
                    pl = self.abs.get('/api/playlists/' + urllib.parse.quote(matches[0]['id'], safe=''))
                else:
                    pl = self.abs.request('POST', '/api/playlists', {'libraryId': library_id, 'name': name,
                                           'description': 'Books selected with AutomaticBookSelector for Bookramp.', 'items': []})
            playlist_id = pl['id']
            existing = {i.get('libraryItemId') for i in pl.get('items', [])}
            additions = [i for i in ids if i not in existing]
            if additions:
                result = self.abs.request('POST', '/api/playlists/' + urllib.parse.quote(playlist_id, safe='') + '/batch/add',
                                          {'items': [{'libraryItemId': i} for i in additions]})
                if not set(ids).issubset({i.get('libraryItemId') for i in result.get('items', [])}):
                    raise AppError('ABS did not confirm all selected books. Refresh playlists before retrying.')
            # Record confirmations, including books already present, so retries
            # after a response interruption remain harmless.
            db.executemany('INSERT OR IGNORE INTO queue VALUES (?,?,?)', [(i, playlist_id, now()) for i in ids])
            self.changed()
            return {'playlist_id': playlist_id, 'name': pl.get('name'), 'added': len(additions), 'already_present': len(ids) - len(additions)}
