"""Revocable, write-only BookRamp ingestion and measured listening statistics."""
import collections
import datetime as dt
import hashlib
import json
import math
import secrets
import uuid
from zoneinfo import ZoneInfo

from core import AppError, now
from goodreads import completed_date


class EventConflict(AppError):
    pass


def timestamp(value):
    if not isinstance(value, str) or len(value) > 50:
        raise AppError('Use ISO-8601 timestamps with a timezone offset.')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.year < 2000 or parsed > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10):
            raise ValueError()
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec='microseconds')
    except ValueError:
        raise AppError('Use real ISO-8601 timestamps with a timezone offset, not future dates.') from None


def identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(c) < 32 for c in value):
        raise AppError('Event, session and book identifiers must be nonempty strings up to 200 characters.')
    return value


class Telemetry:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS bookramp_connections (
                id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,last_seen_at TEXT,revoked_at TEXT);
              CREATE TABLE IF NOT EXISTS bookramp_sessions (
                event_id TEXT PRIMARY KEY,session_id TEXT UNIQUE NOT NULL,connection_id TEXT NOT NULL,
                abs_server_id TEXT NOT NULL,abs_item_id TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT NOT NULL,
                source_seconds REAL,wall_seconds REAL,completion_crossed INTEGER NOT NULL,
                completion_timestamp TEXT,payload TEXT NOT NULL,received_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ramp_book ON bookramp_sessions(abs_item_id);
              CREATE INDEX IF NOT EXISTS ramp_date ON bookramp_sessions(started_at);''')
            if not store.meta(db, 'bookramp_server_id'):
                store.set_meta(db, 'bookramp_server_id', uuid.uuid4().hex)

    def status(self):
        with self.store.db() as db:
            return {'service': 'AutomaticBookSelector', 'bookramp_api_version': 1, 'authenticated': True,
                    'abs_server_id': self.store.meta(db, 'bookramp_server_id'),
                    'abs_user_id': self.store.meta(db, 'abs_user_id'), 'max_batch_size': 100}

    def connections(self):
        with self.store.db() as db:
            return {'connections': [dict(r) for r in db.execute('SELECT id,name,created_at,last_seen_at,revoked_at FROM bookramp_connections ORDER BY created_at DESC')],
                    'identity': self.status()}

    def create(self, payload):
        if not isinstance(payload.get('name'), str):
            raise AppError('Give this BookRamp connection a name.')
        name = payload.get('name', '').strip()
        if not name or len(name) > 100:
            raise AppError('Give this BookRamp connection a name up to 100 characters.')
        token = 'br_' + secrets.token_urlsafe(32)
        connection = uuid.uuid4().hex
        with self.store.lock, self.store.db() as db:
            db.execute('INSERT INTO bookramp_connections(id,name,token_hash,created_at) VALUES (?,?,?,?)',
                       (connection, name, hashlib.sha256(token.encode()).hexdigest(), now()))
        return {'id': connection, 'token': token, 'message': 'Shown once. Save it in BookRamp; it is never returned again.'}

    def revoke(self, payload):
        with self.store.lock, self.store.db() as db:
            changed = db.execute('UPDATE bookramp_connections SET revoked_at=? WHERE id=? AND revoked_at IS NULL', (now(), payload.get('id'))).rowcount
        if not changed:
            raise AppError('Active connection not found.')
        return {'revoked': True}

    def authenticate(self, headers):
        value = headers.get('Authorization', '')
        if not value.startswith('Bearer br_') or len(value) > 200:
            return None
        digest = hashlib.sha256(value[7:].encode()).hexdigest()
        with self.store.db() as db:
            row = db.execute('SELECT id FROM bookramp_connections WHERE token_hash=? AND revoked_at IS NULL', (digest,)).fetchone()
        return row['id'] if row else None

    def validate(self, p):
        if not isinstance(p, dict) or p.get('schema_version') != 1 or type(p.get('schema_version')) is not int:
            raise AppError('Each session requires schema_version: 1.')
        identity = self.status()
        if p.get('abs_server_id') != identity['abs_server_id'] or p.get('abs_user_id') != identity['abs_user_id'] or not identity['abs_user_id']:
            raise AppError('This session belongs to a different ABS server/profile. Pair using the status endpoint first.')
        clean = {k: identifier(p.get(k)) for k in ('event_id', 'session_id', 'abs_item_id')}
        clean.update(schema_version=1, abs_server_id=identity['abs_server_id'], abs_user_id=identity['abs_user_id'])
        clean['abs_library_id'] = identifier(p['abs_library_id']) if p.get('abs_library_id') is not None else None
        clean['started_at'], clean['ended_at'] = timestamp(p.get('started_at')), timestamp(p.get('ended_at'))
        if clean['ended_at'] < clean['started_at']:
            raise AppError('Session end cannot precede its start.')
        span = (dt.datetime.fromisoformat(clean['ended_at']) - dt.datetime.fromisoformat(clean['started_at'])).total_seconds()
        for field in ('source_start_seconds', 'source_end_seconds', 'source_seconds_listened', 'wall_clock_seconds', 'average_speed', 'median_speed', 'minimum_speed', 'maximum_speed', 'maximum_sustained_speed'):
            value = p.get(field)
            if value is not None and (type(value) not in (float, int) or not math.isfinite(value) or value < 0 or value > (32 if 'speed' in field else 31_536_000)):
                raise AppError('Listening times and speeds must be finite, nonnegative measured numbers, or null.')
            clean[field] = value
        wall, source = clean['wall_clock_seconds'], clean['source_seconds_listened']
        if wall is not None and wall > span + 2:
            raise AppError('Actual listening time cannot exceed the session duration.')
        if wall == 0 and source:
            raise AppError('Source listening time requires actual listening time above zero.')
        for field in ('again_count', 'large_seek_forward_count', 'large_seek_backward_count', 'chapter_skip_count'):
            value = p.get(field)
            if value is not None and (type(value) is not int or not 0 <= value <= 1_000_000):
                raise AppError('Playback action counts must be nonnegative integers or null.')
            clean[field] = value
        clean['playback_source'] = p.get('playback_source', 'unknown')
        if clean['playback_source'] not in ('streaming', 'downloaded', 'unknown'):
            raise AppError('Unknown playback_source.')
        clean['bookramp_version'] = str(p.get('bookramp_version') or '')[:100]
        crossed = p.get('completion_crossed', False)
        if type(crossed) is not bool:
            raise AppError('completion_crossed must be true or false.')
        clean['completion_crossed'] = crossed
        clean['completion_timestamp'] = timestamp(p.get('completion_timestamp')) if crossed else None
        if crossed and not clean['started_at'] <= clean['completion_timestamp'] <= clean['ended_at']:
            raise AppError('Completion timestamp must fall within the session.')
        buckets = p.get('speed_buckets')
        clean['speed_buckets'] = None
        if buckets is not None:
            if not isinstance(buckets, list) or not 1 <= len(buckets) <= 500 or wall is None:
                raise AppError('Speed buckets require actual listening time and 1–500 measured entries.')
            parsed = []
            for b in buckets:
                if not isinstance(b, dict) or any(type(b.get(k)) not in (float, int) or not math.isfinite(b[k]) for k in ('speed', 'wall_seconds')) or not 0 < b['speed'] <= 32 or b['wall_seconds'] <= 0:
                    raise AppError('Each speed bucket requires positive speed and actual listening seconds.')
                parsed.append({'speed': b['speed'], 'wall_seconds': b['wall_seconds']})
            if abs(sum(b['wall_seconds'] for b in parsed) - wall) > max(2, wall * .01):
                raise AppError('Speed buckets must cover the session listening time.')
            clean['speed_buckets'] = sorted(parsed, key=lambda b: b['speed'])
        with self.store.db() as db:
            book = db.execute('SELECT library_id FROM books WHERE id=?', (clean['abs_item_id'],)).fetchone()
        if book and clean['abs_library_id'] and book['library_id'] != clean['abs_library_id']:
            raise AppError('Library ID does not match this ABS book.')
        return clean

    def ingest(self, p, connection):
        batch = p.get('sessions') if isinstance(p, dict) else None
        if not isinstance(batch, list) or not 1 <= len(batch) <= 100:
            raise AppError('Send a sessions array containing 1–100 finalized sessions.')
        clean = [self.validate(s) for s in batch]
        accepted, duplicate = 0, 0
        with self.store.lock, self.store.db() as db:
            if not db.execute('SELECT 1 FROM bookramp_connections WHERE id=? AND revoked_at IS NULL', (connection,)).fetchone():
                raise AppError('Connection was revoked.')
            for s in clean:
                canonical = json.dumps({k: v for k, v in s.items() if k != 'event_id'}, sort_keys=True, separators=(',', ':'))
                previous = db.execute('SELECT payload FROM bookramp_sessions WHERE event_id=? OR session_id=?', (s['event_id'], s['session_id'])).fetchall()
                if previous:
                    if any(r['payload'] != canonical for r in previous):
                        raise EventConflict('A saved session/event ID was reused with different content. No part of this batch was saved.')
                    duplicate += 1
                    continue
                db.execute('INSERT INTO bookramp_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (s['event_id'], s['session_id'], connection, s['abs_server_id'], s['abs_item_id'], s['started_at'], s['ended_at'],
                     s['source_seconds_listened'], s['wall_clock_seconds'], int(s['completion_crossed']), s['completion_timestamp'], canonical, now()))
                accepted += 1
            db.execute('UPDATE bookramp_connections SET last_seen_at=? WHERE id=?', (now(), connection))
            if accepted and any(s['completion_crossed'] for s in clean):
                self.store.changed()
        return {'accepted': accepted, 'duplicates': duplicate, 'event_ids': [s['event_id'] for s in clean]}

    def stats(self, month):
        try:
            start_date = dt.date.fromisoformat(month + '-01')
            end_date = dt.date(start_date.year + (start_date.month == 12), start_date.month % 12 + 1, 1)
        except (ValueError, TypeError):
            raise AppError('Choose a month as YYYY-MM.') from None
        zone = ZoneInfo('America/Chicago')
        start = dt.datetime.combine(start_date, dt.time(), zone).astimezone(dt.timezone.utc).isoformat(timespec='microseconds')
        end = dt.datetime.combine(end_date, dt.time(), zone).astimezone(dt.timezone.utc).isoformat(timespec='microseconds')
        with self.store.db() as db:
            sessions = [dict(r) for r in db.execute('''SELECT s.*,b.title,b.author FROM bookramp_sessions s
                LEFT JOIN books b ON b.id=s.abs_item_id WHERE s.started_at>=? AND s.started_at<? ORDER BY s.started_at''', (start, end))]
            books = [dict(r) for r in db.execute('''SELECT b.id,b.title,b.author,b.finished,b.finished_at,COALESCE(f.dnf,0) AS dnf,
                (SELECT max(completion_timestamp) FROM bookramp_sessions s WHERE s.abs_item_id=b.id AND s.completion_crossed=1) AS ramp_finished
                FROM books b LEFT JOIN feedback f ON f.book_id=b.id''')]
        completed = {b['id']: b for b in books if not b['dnf'] and start_date.isoformat() <= completed_date(b['ramp_finished'] or (b['finished_at'] if b['finished'] else None)) < end_date.isoformat()}
        pairs = [s for s in sessions if s['source_seconds'] is not None and s['wall_seconds'] is not None]
        source = sum(s['source_seconds'] for s in pairs)
        wall = sum(s['wall_seconds'] for s in pairs)
        buckets, sustained, again = [], [], []
        for s in sessions:
            p = json.loads(s['payload'])
            buckets.extend(p.get('speed_buckets') or [])
            if p.get('maximum_sustained_speed') is not None:
                sustained.append(p['maximum_sustained_speed'])
            if p.get('again_count') is not None:
                again.append(p['again_count'])
        median = None
        if sessions and all(json.loads(s['payload']).get('speed_buckets') is not None for s in sessions):
            half = sum(b['wall_seconds'] for b in buckets) / 2
            cumulative = 0
            for b in sorted(buckets, key=lambda b: b['speed']):
                cumulative += b['wall_seconds']
                if cumulative >= half:
                    median = b['speed']
                    break
        aggregates = {}
        for s in sessions:
            b = aggregates.setdefault(s['abs_item_id'], {'id': s['abs_item_id'], 'title': s['title'] or 'Awaiting ABS catalogue sync', 'author': s['author'] or '', 'sessions': 0, 'source_seconds': 0, 'wall_seconds': 0, 'measured_sessions': 0})
            b['sessions'] += 1
            if s['source_seconds'] is not None and s['wall_seconds'] is not None:
                b['source_seconds'] += s['source_seconds']
                b['wall_seconds'] += s['wall_seconds']
                b['measured_sessions'] += 1
        authors = collections.Counter()
        for b in aggregates.values():
            if b['measured_sessions'] and b['author']:
                authors[b['author']] += b['wall_seconds']
        return {'month': month, 'completed': len(completed), 'completed_books': list(completed.values()),
                'session_count': len(sessions), 'measured_sessions': len(pairs), 'source_hours': source / 3600 if pairs else None,
                'actual_hours': wall / 3600 if pairs else None, 'hours_saved': (source-wall) / 3600 if pairs else None,
                'average_speed': source / wall if wall else None, 'median_speed': median,
                'maximum_sustained_speed': max(sustained) if sustained else None,
                'again_count': sum(again) if again else None, 'top_author': authors.most_common(1)[0][0] if authors else None,
                'books': sorted(aggregates.values(), key=lambda b: -b['wall_seconds']),
                'note': 'Listening totals cover measured sessions started in this month (America/Chicago). Skips are excluded by BookRamp. Replays count as listening. ABS completion is deduplicated with BookRamp; it never estimates listening speed or time.'}
