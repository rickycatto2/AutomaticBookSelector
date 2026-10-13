"""Bounded public bibliographic suggestions. Never Goodreads acceptance proof."""
import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

from core import AppError, NoRedirect, normalize

ENDPOINTS = {'Google Books': 'https://www.googleapis.com/books/v1/volumes',
             'Open Library': 'https://openlibrary.org/search.json'}


class ProviderFailure(AppError):
    """Only controlled metadata crosses the provider boundary."""
    def __init__(self, provider, status, message):
        self.failure = {'provider': provider, 'status': status, 'message': message}
        super().__init__(f'{provider} [{status}]: {message}')


def retryable_provider_result(result):
    return bool(result.get('provider_error') and not result.get('candidate') and not result.get('candidates')
                and any(f.get('status') in ('daily_quota', 'transport') for f in result.get('provider_failures', [])))


# Explicit series position markers. They identify a different work, not an edition.
SERIES_MARK = re.compile(r'(?i)\b(?:volume|vol|book|part|issue|chapter|series|saga|trilogy|number)\b|#|\d')


def work_title(value):
    return normalize(re.sub(r'\s*\((?:audio(?:book)? edition|unabridged|large print|ebook|[^()]+(?:\bbook|\bvolume|#)\s*\d+)\)\s*$', '', value or '', flags=re.I))


def parenthetical(value):
    match = re.search(r'\(([^()]*)\)\s*$', value or '')
    return match.group(1) if match else ''


def volume_marker(value):
    """Trailing volume/part/numbered-series marker; '' when there is none."""
    tail = parenthetical(value)
    return normalize(tail) if SERIES_MARK.search(tail) else ''


def truncation_review(title, author, relevant, usable):
    """Truncation alone is not evidence of a wrong work.

    Provider page limits make totalItems > 10 ordinary for popular titles, so
    a truncated result can still be confirmed automatically when every
    relevant candidate agrees on one work identity: the exact source work
    title key, the full source author set, no conflicting subtitle or volume
    marker, and a checksum-valid ISBN on every candidate. Any disagreement
    among them, or a candidate without a usable ISBN, keeps the book in
    review. Returns '' only when that agreement holds.
    """
    keys = {work_title(c['title']) for c in relevant}
    if len(keys) > 1 or keys != {work_title(title)}:
        return 'Truncated search, but matching candidates do not all agree on one work title; review required.'
    if {volume_marker(c['title']) for c in relevant} - {volume_marker(title)}:
        return 'Truncated search, but matching candidates carry conflicting volume or series markers; review required.'
    if {authors(c['author']) for c in relevant} != {authors(author)}:
        return 'Truncated search, but matching candidates disagree on the full author set; review required.'
    if len(usable) != len(relevant):
        return 'Truncated search, and a matching candidate lacks a checksum-valid ISBN; review required.'
    return ''


def title_evidence(left, right):
    """Points plus reason for a catalog title (left) against the source (right).

    Only an exact normalized work title, or a source that adds a non-series
    subtitle to a bare catalog title, can reach the automatic threshold. Every
    volume/part/numbered-series or conflicting added subtitle returns 0 so the
    book stays in review with that reason.
    """
    if work_title(left) == work_title(right):
        if normalize(left) == normalize(right):
            return 40, 'Exact normalized work title'
        # work_title may drop a trailing series designator; that is not an edition.
        tail_left, tail_right = parenthetical(left), parenthetical(right)
        if tail_left != tail_right and (SERIES_MARK.search(tail_left) or SERIES_MARK.search(tail_right)):
            return 0, 'Trailing series position marker differs between source and catalog; review required'
        return 40, 'Exact work title after an edition marker'
    # No fuzzy cross-work matching: bases must be identical, specific and long.
    a, b = left.split(':', 1), right.split(':', 1)
    base = work_title(a[0])
    if base != work_title(b[0]) or len(base.split()) < 3 or len(base) < 12:
        return 0, 'Work title not established'
    if len(a) == 1 and len(b) == 2:
        extra = work_title(b[1])
        if SERIES_MARK.search(extra):
            return 0, 'Source subtitle names a volume, part or numbered series; review required'
        return 35, 'Conservative source-subtitle-insensitive work title'
    if len(a) == 2 and len(b) == 1:
        extra = work_title(a[1])
        if SERIES_MARK.search(extra):
            return 0, 'Catalog title names a volume, part or numbered series the source lacks; review required'
        return 0, 'Catalog adds a subtitle the source lacks; review required'
    return 0, 'Conflicting subtitles may identify different works; review required'


def authors(value):
    parts = re.split(r'\s*(?:,|;| & | and )\s*', value or '', flags=re.I)
    return tuple(sorted(normalize(p) for p in parts if p.strip()))


def language(value):
    key = normalize(value or '')
    return {'english': 'en', 'eng': 'en', 'en': 'en', 'german': 'de', 'deu': 'de', 'ger': 'de', 'de': 'de',
            'french': 'fr', 'fra': 'fr', 'fre': 'fr', 'fr': 'fr', 'spanish': 'es', 'spa': 'es', 'es': 'es'}.get(key, '')


def google_query(title, author):
    """Plain search words; metadata cannot introduce query syntax.

    Punctuation (including quotes, colons and apostrophes) and controls become
    word separators. Boolean words are quoted so they stay literal, without
    imposing phrase adjacency on titles or multi-author lists.
    This is retrieval only: identity checks use the unchanged
    source title/author, never these stripped query words.
    """
    words = re.findall(r'[^\W_]+', title + ' ' + author)
    return ' '.join('"' + word + '"' if word.upper() in {'AND', 'OR', 'NOT'} else word
                    for word in words)


class Catalog:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = {}
        self.cache = {}
        self.cooldowns = {}
        self.config = {}

    def request(self, provider, params):
        # Endpoint cannot be supplied by a browser, provider result, or catalogue metadata.
        if provider not in ENDPOINTS:
            raise AppError('Unsupported catalog endpoint.')
        params = dict(params)
        key = os.environ.get('GOOGLE_BOOKS_API_KEY', '') if provider == 'Google Books' else ''
        if key:
            params['key'] = key
        else:
            params.pop('key', None)
        config = hashlib.sha256(key.encode()).digest()
        # Cache identity never retains a credential-bearing URL.
        cache_key = (provider, config, urllib.parse.urlencode({k: v for k, v in params.items() if k != 'key'}))
        with self.lock:
            stamp = time.monotonic()
            if self.config.get(provider) != config:
                self.config[provider] = config
                self.cooldowns.pop(provider, None)
            cooldown = self.cooldowns.get(provider)
            if cooldown and time.time() < cooldown[0]:
                raise ProviderFailure(provider, cooldown[1], cooldown[2])
            self.cooldowns.pop(provider, None)
            cached = self.cache.get(cache_key)
            if cached and stamp - cached[0] < 86400:
                return cached[1]
            if stamp - self.last.get(provider, -100) < 5:
                raise ProviderFailure(provider, 'throttle', 'Please wait five seconds between uncached searches for this provider.')
            self.last[provider] = stamp
            try:
                url = ENDPOINTS[provider] + '?' + urllib.parse.urlencode(params)
                req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AutomaticBookSelector/1.0'})
                # Disable environmental proxies; no cookies or redirects.
                with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=10) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError('oversized')
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('shape')
                # Defend against a provider reflecting the credential into metadata.
                if key:
                    def redact(value):
                        if isinstance(value, str):
                            return value.replace(key, '[redacted]')
                        if isinstance(value, list):
                            return [redact(v) for v in value]
                        if isinstance(value, dict):
                            return {redact(k): redact(v) for k, v in value.items()}
                        return value
                    data = redact(data)
            except urllib.error.HTTPError as exc:
                status = 'daily_quota' if exc.code == 429 else 'http_' + str(exc.code)
                message = 'Daily quota exceeded (HTTP 429); lookup could not run.' if exc.code == 429 else 'Provider returned HTTP ' + str(exc.code) + '; lookup could not run.'
                if exc.code == 429:
                    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
                    self.cooldowns[provider] = (dt.datetime.combine(tomorrow, dt.time(), dt.timezone.utc).timestamp(), status, message)
                raise ProviderFailure(provider, status, message) from None
            except (ValueError, TypeError, UnicodeError):
                raise ProviderFailure(provider, 'invalid_response', 'Invalid or oversized catalog response; lookup could not run.') from None
            except Exception:
                message = 'Transport failure or timeout; lookup could not run.'
                self.cooldowns[provider] = (time.time() + 60, 'transport', message)
                raise ProviderFailure(provider, 'transport', message) from None
            self.cooldowns.pop(provider, None)
            if len(self.cache) >= 128:
                self.cache.pop(next(iter(self.cache)))
            self.cache[cache_key] = (stamp, data)
            return data

    def search(self, provider, title, author):
        if provider == 'Google Books':
            data = self.request(provider, {'q': google_query(title, author), 'maxResults': 10, 'printType': 'books'})
            candidates = []
            for item in (data.get('items') or [])[:10]:
                v = item.get('volumeInfo') or {}
                candidates.append({'title': v.get('title', '') + (': ' + v['subtitle'] if v.get('subtitle') else ''),
                                   'author': ', '.join(v.get('authors') or []), 'languages': [v.get('language', '')],
                                   'isbns': [i.get('identifier', '') for i in v.get('industryIdentifiers', [])],
                                    'reference': str(item.get('id', '')), 'publisher': v.get('publisher', ''),
                                    'print': not bool((item.get('saleInfo') or {}).get('isEbook'))})
            return candidates, data.get('totalItems', 0) > 10
        data = self.request(provider, {'title': title, 'author': author, 'limit': 10,
                                       'fields': 'key,title,author_name,isbn,language,editions,editions.key,editions.title,editions.author_name,editions.isbn'})
        return [{'title': v.get('title', ''), 'author': ', '.join(v.get('author_name') or []),
                 'languages': v.get('language') or [], 'isbns': (v.get('isbn') or [])[:100],
                  'reference': str(v.get('key', '')), 'editions': (v.get('editions') or {}).get('docs', [])[:10]}
                for v in (data.get('docs') or [])[:10]], data.get('numFound', 0) > 10

    def resolve(self, source):
        from goodreads import valid_isbn
        title, author = source['title'], source['author']
        base = {'provenance': '', 'reason': '', 'candidate': None, 'high_confidence': False,
                'score': 0, 'threshold': 90, 'evidence': [], 'goodreads_isbn_url': ''}
        if any(not isinstance(v, str) or not v.strip() or len(v) > 1000 or v.lstrip()[0] in '=+-@' for v in (title, author)):
            return base | {'reason': 'Unsafe or missing title/author fields; manual review required.'}
        if not work_title(title) or not authors(author):
            return base | {'reason': 'Missing work or full author; manual review required.'}
        if re.search(r'\b(?:translator|narrator|translated|narrated)\b', author, re.I):
            return base | {'reason': 'Contributor roles are uncertain; correct/review authors without silently dropping names.'}
        errors, failures = [], []
        genuine_outcome = False
        for provider in ENDPOINTS:
            try:
                candidates, truncated = self.search(provider, title, author)
            except ProviderFailure as exc:
                failures.append(exc.failure)
                errors.append(str(exc))
                continue
            except (AppError, ValueError, TypeError, AttributeError):
                failure = ProviderFailure(provider, 'invalid_response', 'Invalid catalog response; lookup could not run.')
                failures.append(failure.failure)
                errors.append(str(failure))
                continue
            relevant, conflict = [], ''
            for c in candidates:
                points, why = title_evidence(c['title'], title)
                if points:
                    relevant.append(c)
                elif why != 'Work title not established' and not conflict:
                    conflict = why
            if conflict:
                genuine_outcome = True
                errors.append(provider + ': ' + conflict)
            if not relevant:
                continue
            genuine_outcome = True
            result = base | {'provenance': provider, 'candidates': relevant}
            if any(not isinstance(v, str) or not v.strip() or len(v) > 1000 or v.lstrip()[0] in '=+-@'
                   for c in relevant for v in (c['title'], c['author'])):
                return result | {'reason': 'Unsafe catalog title/author fields; manual review required.'}
            if any(authors(c['author']) != authors(author) for c in relevant):
                return result | {'reason': 'Same-title author mismatch or ambiguous works; review required.'}
            subtitles = {work_title(c['title']) for c in relevant if ':' in c['title']}
            if len(subtitles) > 1:
                return result | {'reason': 'Conflicting subtitles may identify different works; review required.'}
            codes = [(c, sorted({valid_isbn(i) for i in c['isbns']} - {''})) for c in relevant]
            usable = [(c, values) for c, values in codes if values]
            # Distinct Open Library work IDs cannot be assumed to be duplicate editions.
            if provider == 'Open Library' and (len({c['reference'] for c in relevant}) > 1
                                             or not all(re.fullmatch(r'/works/[A-Za-z0-9]+', c['reference']) for c in relevant)):
                return result | {'reason': 'Multiple distinct catalog works; review required.'}
            # A truncated page is review only when its candidates disagree; a
            # unanimous exact work identity is relaxed, never a wrong work.
            blocked = truncation_review(title, author, relevant, usable) if truncated else ''
            if blocked:
                return result | {'reason': blocked}
            if not usable:
                errors.append(provider + ': no valid ISBN')
                continue
            # Prefer publisher/print evidence, then ISBN-13 and a stable ISBN/ID
            # ordering. No invented or source-derived identifier is introduced.
            choices = [(c, i) for c, values in usable for i in values]
            c, code = min(choices, key=lambda pair: (not bool(pair[0].get('publisher')),
                          not pair[0].get('print', False), len(pair[1]) != 13, pair[1], pair[0]['reference']))
            linked = provider == 'Google Books'
            if provider == 'Open Library':
                editions = [(e, valid_isbn(i)) for e in c.get('editions', [])
                            if str(e.get('key', '')).startswith('/books/')
                            and title_evidence(e.get('title', ''), title)[0]
                            and authors(', '.join(e.get('author_name') or [])) == authors(author)
                            for i in e.get('isbn', []) if valid_isbn(i)]
                if editions:
                    e, code = min(editions, key=lambda pair: (len(pair[1]) != 13, pair[1], pair[0]['key']))
                    linked = True
            points, title_why = title_evidence(c['title'], title)
            evidence = [title_why + f' (+{points})', 'Independent catalog corroborates full author set (+35)',
                        'Checksum-valid catalog ISBN (+15)', 'Linked edition identity (+10)' if linked else 'Work-index ISBN lacks edition/author linkage (+0)']
            score = points + 35 + 15 + (10 if linked else 0)
            same_language = language(source.get('language')) and {language(v) for v in c['languages']} == {language(source.get('language'))}
            evidence.append('Edition difference: unknown/different language is neutral (+0)' if not same_language else 'Matching language (neutral +0)')
            if authors(author) == authors(source.get('narrator', '')):
                evidence.append('Possible self-narration: full author set independently corroborated')
            if truncated:
                evidence.append('Truncated search, but every matching candidate agrees on the same work title and full author set (neutral +0)')
            # Automatic confirmation needs an exact work title, or a clean one-sided
            # source subtitle with no conflicting catalog title in the same results.
            title_ok = points == 40 or not conflict
            high = linked and score >= base['threshold'] and title_ok
            candidate = {'title': c['title'], 'author': c['author'], 'isbn': code, 'reference': c['reference']}
            return result | {'candidate': candidate, 'score': score, 'evidence': evidence,
                             'goodreads_isbn_url': 'https://www.goodreads.com/book/isbn/' + code,
                             'high_confidence': high, 'reason': f'Score {score}/100; auto threshold 90 with linked ISBN evidence. ' + '; '.join(evidence) +
                             ('. ' if high else ('. ' + conflict + ' ' if conflict and not title_ok else '. Edition evidence insufficient; review required. ')) +
                             'Goodreads import acceptance is unverified.'}
        return base | {'provider_error': bool(failures) and not genuine_outcome, 'provider_failures': failures,
                        'reason': '; '.join(errors) or 'No exact catalog work with a valid ISBN found; review required.'}
