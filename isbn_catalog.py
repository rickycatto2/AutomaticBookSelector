"""Anonymous, bounded public bibliographic suggestions. Never Goodreads acceptance proof."""
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from core import AppError, NoRedirect, normalize, isbn as canonical_isbn

ENDPOINTS = {'Google Books': 'https://www.googleapis.com/books/v1/volumes',
             'Open Library': 'https://openlibrary.org/search.json'}


def work_title(value):
    # Only explicit format decorations, not subtitles/series/work numbers.
    return normalize(re.sub(r'\s*\((?:audio(?:book)? edition|unabridged|large print|ebook)\)\s*$', '', value or '', flags=re.I))


def authors(value):
    parts = re.split(r'\s*(?:,|;| & | and )\s*', value or '', flags=re.I)
    return tuple(sorted(normalize(p) for p in parts if p.strip()))


def language(value):
    key = normalize(value or '')
    return {'english': 'en', 'eng': 'en', 'en': 'en', 'german': 'de', 'deu': 'de', 'ger': 'de', 'de': 'de',
            'french': 'fr', 'fra': 'fr', 'fre': 'fr', 'fr': 'fr', 'spanish': 'es', 'spa': 'es', 'es': 'es'}.get(key, '')


class Catalog:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = {}
        self.cache = {}

    def request(self, provider, params):
        # Endpoint cannot be supplied by a browser, provider result, or catalogue metadata.
        if provider not in ENDPOINTS:
            raise AppError('Unsupported catalog endpoint.')
        url = ENDPOINTS[provider] + '?' + urllib.parse.urlencode(params)
        with self.lock:
            stamp = time.monotonic()
            cached = self.cache.get(url)
            if cached and stamp - cached[0] < 86400:
                return cached[1]
            if stamp - self.last.get(provider, -100) < 5:
                raise AppError('Please wait five seconds between uncached searches for this provider.')
            self.last[provider] = stamp
            req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AutomaticBookSelector/1.0'})
            try:
                # Disable environmental proxies; no cookies, API key, credentials or redirects.
                with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=10) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError('oversized')
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('shape')
            except Exception:
                raise AppError('Public catalog unavailable or response invalid; review manually.') from None
            if len(self.cache) >= 128:
                self.cache.pop(next(iter(self.cache)))
            self.cache[url] = (stamp, data)
            return data

    def search(self, provider, title, author):
        if provider == 'Google Books':
            data = self.request(provider, {'q': 'intitle:"' + title + '" inauthor:"' + author + '"', 'maxResults': 10, 'printType': 'books'})
            candidates = []
            for item in (data.get('items') or [])[:10]:
                v = item.get('volumeInfo') or {}
                candidates.append({'title': v.get('title', '') + (': ' + v['subtitle'] if v.get('subtitle') else ''),
                                   'author': ', '.join(v.get('authors') or []), 'languages': [v.get('language', '')],
                                   'isbns': [i.get('identifier', '') for i in v.get('industryIdentifiers', [])],
                                   'reference': str(item.get('id', ''))})
            return candidates, data.get('totalItems', 0) > 10
        data = self.request(provider, {'title': title, 'author': author, 'limit': 10,
                                      'fields': 'key,title,author_name,isbn,language'})
        return [{'title': v.get('title', ''), 'author': ', '.join(v.get('author_name') or []),
                 'languages': v.get('language') or [], 'isbns': (v.get('isbn') or [])[:100],
                 'reference': str(v.get('key', ''))} for v in (data.get('docs') or [])[:10]], data.get('numFound', 0) > 10

    def resolve(self, source):
        from goodreads import valid_isbn
        title, author = source['title'], source['author']
        base = {'provenance': '', 'reason': '', 'candidate': None, 'high_confidence': False}
        if any(not isinstance(v, str) or not v.strip() or len(v) > 1000 or v.lstrip()[0] in '=+-@' for v in (title, author)):
            return base | {'reason': 'Unsafe or missing title/author fields; manual review required.'}
        if not work_title(title) or not authors(author) or not language(source.get('language')):
            return base | {'reason': 'Missing work, full author or known source language; manual review required.'}
        if re.search(r'\b(?:translator|narrator|translated|narrated)\b', author, re.I) or authors(author) == authors(source.get('narrator', '')):
            return base | {'reason': 'Contributor roles are uncertain; correct/review authors without silently dropping names.'}
        errors = []
        for provider in ENDPOINTS:
            try:
                candidates, truncated = self.search(provider, title, author)
            except (AppError, ValueError, TypeError, AttributeError) as exc:
                errors.append(str(exc))
                continue
            relevant = [c for c in candidates if work_title(c['title']) == work_title(title)]
            if not relevant:
                continue
            result = base | {'provenance': provider, 'candidates': relevant}
            if truncated:
                return result | {'reason': 'Search is truncated; multiple works may exist. Review required.'}
            if any(authors(c['author']) != authors(author) for c in relevant):
                return result | {'reason': 'Same-title author mismatch or ambiguous works; review required.'}
            if any(not c['languages'] or {language(v) for v in c['languages']} != {language(source['language'])} for c in relevant):
                return result | {'reason': 'Unknown or conflicting edition language; review required.'}
            codes = [(c, sorted({valid_isbn(i) for i in c['isbns']} - {''})) for c in relevant]
            usable = [(c, values) for c, values in codes if values]
            # Distinct Open Library work IDs cannot be assumed to be duplicate editions.
            if provider == 'Open Library' and len({c['reference'] for c in relevant}) > 1:
                return result | {'reason': 'Multiple distinct catalog works; review required.'}
            if provider == 'Google Books' and len(relevant) > 1:
                # Volume IDs are editions, not work IDs. Without shared identifiers we
                # cannot prove that same-named volumes are editions of one work.
                shared = set(canonical_isbn(i) for i in codes[0][1])
                for _, values in codes[1:]:
                    shared &= {canonical_isbn(i) for i in values}
                if not shared:
                    return result | {'reason': 'Multiple catalog volumes without shared edition identifiers/work linkage; review required.'}
            if not usable:
                errors.append(provider + ': no valid ISBN')
                continue
            c, values = usable[0]
            if provider == 'Google Books' and len(relevant) > 1:
                values = [i for i in values if canonical_isbn(i) in shared]
            if provider == 'Open Library':
                # Search returns work-level aggregated ISBNs/languages, not a linked edition.
                # Do not pretend this establishes the language/authors of the selected ISBN.
                return result | {'candidate': {'title': c['title'], 'author': c['author'], 'isbn': values[0], 'reference': c['reference']},
                                 'reason': 'Open Library work-index ISBN suggestion only; edition-level language/author linkage needs manual review. Goodreads acceptance is unverified.'}
            return result | {'candidate': {'title': c['title'], 'author': c['author'], 'isbn': values[0], 'reference': c['reference']},
                             'high_confidence': True, 'reason': 'Exact work/full authors/language and checksum-valid catalog ISBN. Goodreads import acceptance is unverified.'}
        return base | {'reason': '; '.join(errors) or 'No exact catalog work with a valid ISBN found; review required.'}
