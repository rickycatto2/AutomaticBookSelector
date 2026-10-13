import copy
import csv
import io
import json
import pathlib
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch, MagicMock

from core import AppError, Store, NoRedirect
from goodreads import Goodreads, HEADERS, source_snapshot, valid_isbn
from isbn_catalog import Catalog, ENDPOINTS
from telemetry import Telemetry
from test_core import FakeABS, item

# Synthetic bibliographic fixtures; these ISBNs are not assertions about a real work.
GOOGLE = {'totalItems': 1, 'items': [{'id': 'synthetic-volume', 'volumeInfo': {
    'title': 'One', 'authors': ['Writer'], 'language': 'en',
    'industryIdentifiers': [{'type': 'ISBN_13', 'identifier': '9780306406157'}]}}]}
OPEN = {'numFound': 1, 'docs': [{'key': '/works/SYNTHETIC', 'title': 'One',
    'author_name': ['Writer'], 'language': ['eng'], 'isbn': ['0306406152']}]}
SOURCE = {'title': 'One', 'author': 'Writer', 'language': 'English', 'narrator': 'Voice'}


# Real Google response shape, synthetic bibliographic identity/ISBN only.
GOOGLE_BROAD = {'kind': 'books#volumes', 'totalItems': 2, 'items': [
    {'kind': 'books#volume', 'id': 'synthetic-exact', 'volumeInfo': {
        'title': 'The Synthetic Long Work', 'authors': ['Writer', 'Coauthor'],
        'publisher': 'Synthetic Press', 'publishedDate': '2026', 'language': 'en',
        'printType': 'BOOK', 'industryIdentifiers': [
            {'type': 'ISBN_13', 'identifier': '9780306406157'},
            {'type': 'ISBN_10', 'identifier': '0306406152'}]},
     'saleInfo': {'isEbook': False}, 'accessInfo': {'viewability': 'NO_PAGES'}},
    {'kind': 'books#volume', 'id': 'synthetic-unrelated', 'volumeInfo': {
        'title': 'A Different Work', 'authors': ['Writer', 'Coauthor'],
        'industryIdentifiers': [{'type': 'ISBN_13', 'identifier': '9798217058785'}]}}
]}
BROAD_SOURCE = SOURCE | {'title': 'The Synthetic Long Work', 'author': 'Writer; Coauthor'}


class CatalogTests(unittest.TestCase):
    def resolve(self, google=GOOGLE, library=OPEN, source=SOURCE):
        catalog = Catalog()
        with patch.object(catalog, 'request', side_effect=[google, library]) as request:
            result = catalog.resolve(source)
        return result, request

    def test_google_plain_query_optional_author_and_unchanged_caps(self):
        catalog = Catalog()
        for author, expected in (('Andy Weir', 'Project Hail Mary Andy Weir'),
                                 ('', 'Project Hail Mary')):
            with self.subTest(author=author), patch.object(catalog, 'request', return_value={}) as request:
                catalog.search('Google Books', 'Project Hail Mary', author)
                request.assert_called_once_with('Google Books', {
                    'q': expected, 'maxResults': 10, 'printType': 'books'})
                self.assertNotRegex(expected, r'intitle:|inauthor:')

    def test_google_hostile_metadata_is_plain_words_not_query_syntax(self):
        title = 'A "Title": intitle:"Other"\x00\nOR -isbn:123 &key=not-a-key'
        author = "O'Connor; inauthor:\"Attacker\"\r\tD’Arcy"
        catalog = Catalog()
        with patch.object(catalog, 'request', return_value={}) as request:
            catalog.search('Google Books', title, author)
        params = request.call_args.args[1]
        self.assertEqual(params['q'], 'A Title intitle Other "OR" isbn 123 key "not" a key O Connor inauthor Attacker D Arcy')
        self.assertRegex(params['q'], r'^(?:\w+|"\w+")(?: (?:\w+|"\w+"))*$')
        self.assertNotRegex(params['q'], r'[\x00-\x1f\x7f:\'&=+-]')
        self.assertNotIn('intitle:', params['q'])
        self.assertNotIn('inauthor:', params['q'])
        encoded = urllib.parse.parse_qs(urllib.parse.urlencode(params))
        self.assertEqual(set(encoded), {'q', 'maxResults', 'printType'})
        self.assertEqual(encoded['q'], [params['q']])
        with patch.object(catalog, 'request', return_value={}) as request:
            catalog.search('Google Books', 'Été: 東京', 'José García')
        self.assertEqual(request.call_args.args[1]['q'], 'Été 東京 José García')

    def test_open_library_uses_literal_title_author_parameters_unchanged(self):
        title, author = 'A "Title": Volume 2\n', "O'Connor: inauthor:Other"
        catalog = Catalog()
        with patch.object(catalog, 'request', return_value={'docs': []}) as request:
            self.assertEqual(catalog.search('Open Library', title, author), ([], False))
        request.assert_called_once_with('Open Library', {
            'title': title, 'author': author, 'limit': 10,
            'fields': 'key,title,author_name,isbn,language,editions,editions.key,editions.title,editions.author_name,editions.isbn'})
        params = urllib.parse.parse_qs(urllib.parse.urlencode(request.call_args.args[1]))
        self.assertEqual(params['title'], [title])
        self.assertEqual(params['author'], [author])
        self.assertNotIn('q', params)

    def test_broad_google_payload_requires_exact_work_and_full_author_set(self):
        result, request = self.resolve(google=GOOGLE_BROAD, library={'docs': []}, source=BROAD_SOURCE)
        self.assertTrue(result['high_confidence'])
        self.assertEqual(result['candidate']['reference'], 'synthetic-exact')
        self.assertEqual(request.call_args.args[1]['q'], 'The Synthetic Long Work Writer Coauthor')
        for change, reason in (({'title': 'The Synthetic Long Works'}, 'No exact catalog work'),
                               ({'authors': ['Writer']}, 'author mismatch'),
                               ({'authors': ['Writer', 'Coauthor', 'Voice']}, 'author mismatch'),
                               ({'authors': ['Writer', 'Voice']}, 'author mismatch')):
            google = copy.deepcopy(GOOGLE_BROAD)
            google['items'][0]['volumeInfo'].update(change)
            with self.subTest(change=change):
                result, _ = self.resolve(google=google, library={'docs': []}, source=BROAD_SOURCE)
                self.assertFalse(result['high_confidence'])
                self.assertIsNone(result['candidate'])
                self.assertIn(reason, result['reason'])

    def test_broad_google_payload_subtitle_series_volume_vetoes(self):
        for change in ({'subtitle': 'Volume 2'}, {'subtitle': 'Book 3'},
                       {'subtitle': 'Part 4'}, {'subtitle': 'A Novel'},
                       {'title': 'The Synthetic Long Work (Series #1)'},
                       {'title': 'The Synthetic Long Work (Volume 2)'}):
            google = copy.deepcopy(GOOGLE_BROAD)
            google['items'][0]['volumeInfo'].update(change)
            with self.subTest(change=change):
                result, _ = self.resolve(google=google, library={'docs': []}, source=BROAD_SOURCE)
                self.assertFalse(result['high_confidence'])
                self.assertIsNone(result['candidate'])
                self.assertIn('review required', result['reason'])
        google = copy.deepcopy(GOOGLE_BROAD)
        google['items'][0]['volumeInfo']['subtitle'] = 'Different Subtitle'
        result, _ = self.resolve(google=google, library={'docs': []},
                                 source=BROAD_SOURCE | {'title': 'The Synthetic Long Work: Source Subtitle'})
        self.assertFalse(result['high_confidence'])
        self.assertIn('Conflicting subtitles', result['reason'])

    def test_popular_title_control_truncation_keeps_record_cap_and_auto_confirms(self):
        # Control shape seen for popular titles: totalItems 343, one page of 10,
        # exact work title plus full author set present. Truncation alone is
        # not a wrong-work signal once every matching candidate agrees.
        google = copy.deepcopy(GOOGLE_BROAD)
        google['totalItems'] = 343
        google['items'] *= 6
        catalog = Catalog()
        with patch.object(catalog, 'request', return_value=google):
            candidates, truncated = catalog.search('Google Books', BROAD_SOURCE['title'], BROAD_SOURCE['author'])
        self.assertEqual(len(candidates), 10)
        self.assertTrue(truncated)
        result, _ = self.resolve(google=google, library={'docs': []}, source=BROAD_SOURCE)
        self.assertTrue(result['high_confidence'])
        self.assertEqual(result['candidate']['reference'], 'synthetic-exact')
        self.assertIn('Truncated search, but every matching candidate agrees', result['reason'])
        self.assertIn('unverified', result['reason'])

    def test_truncated_search_single_agreeing_work_identity_still_auto_confirms(self):
        google = copy.deepcopy(GOOGLE_BROAD)
        google['totalItems'] = 40
        result, _ = self.resolve(google=google, library={'docs': []}, source=BROAD_SOURCE)
        self.assertTrue(result['high_confidence'])
        self.assertEqual(result['candidate']['reference'], 'synthetic-exact')
        self.assertIn('Truncated search, but every matching candidate agrees', result['reason'])

    def test_truncated_search_author_conflicting_candidate_forces_review(self):
        google = copy.deepcopy(GOOGLE_BROAD)
        google['totalItems'] = 343
        other = copy.deepcopy(google['items'][0])
        other['id'] = 'synthetic-other-author'
        other['volumeInfo']['authors'] = ['Writer', 'Someone Else']
        google['items'].append(other)
        result, request = self.resolve(google=google, library={'docs': []}, source=BROAD_SOURCE)
        self.assertFalse(result['high_confidence'])
        self.assertIsNone(result['candidate'])
        self.assertIn('author mismatch', result['reason'])
        self.assertEqual(request.call_count, 1)  # no fallback laundering the conflict

    def test_truncated_search_conflicting_subtitles_force_review(self):
        source = BROAD_SOURCE | {'title': 'The Synthetic Long Work: Source Subtitle'}
        google = copy.deepcopy(GOOGLE_BROAD)
        google['totalItems'] = 343
        google['items'][0]['volumeInfo']['title'] = 'The Synthetic Long Work: Source Subtitle'
        bare = copy.deepcopy(google['items'][0])
        bare['id'] = 'synthetic-bare'
        bare['volumeInfo']['title'] = 'The Synthetic Long Work'
        google['items'].append(bare)
        result, request = self.resolve(google=google, library={'docs': []}, source=source)
        self.assertFalse(result['high_confidence'])
        self.assertIsNone(result['candidate'])
        self.assertIn('do not all agree on one work title', result['reason'])
        self.assertEqual(request.call_count, 1)

    def test_truncated_search_candidate_without_usable_isbn_forces_review(self):
        google = copy.deepcopy(GOOGLE_BROAD)
        google['totalItems'] = 343
        noisbn = copy.deepcopy(google['items'][0])
        noisbn['id'] = 'synthetic-no-isbn'
        noisbn['volumeInfo']['industryIdentifiers'] = []
        google['items'].append(noisbn)
        result, request = self.resolve(google=google, library={'docs': []}, source=BROAD_SOURCE)
        self.assertFalse(result['high_confidence'])
        self.assertIsNone(result['candidate'])
        self.assertIn('checksum-valid ISBN', result['reason'])
        self.assertEqual(request.call_count, 1)
        # Not truncated, the same page keeps the existing non-truncated behaviour.
        google['totalItems'] = 6
        result, _ = self.resolve(google=google, library={'docs': []}, source=BROAD_SOURCE)
        self.assertTrue(result['high_confidence'])
        self.assertEqual(result['candidate']['reference'], 'synthetic-exact')

    def test_truncation_review_helper_work_identity_conditions(self):
        from isbn_catalog import truncation_review
        exact = {'title': 'The Synthetic Long Work', 'author': 'Writer; Coauthor'}
        usable = [['9780306406157'], ['9780306406157']]
        self.assertEqual(truncation_review(BROAD_SOURCE['title'], BROAD_SOURCE['author'],
                                           [exact, exact | {'title': 'The Synthetic Long Work'}], usable), '')
        for relevant, fragment in (
                ([exact, exact | {'title': 'The Synthetic Long Works'}], 'one work title'),
                ([exact | {'title': 'The Synthetic Long Works: A Subtitle'}], 'one work title'),
                ([exact, exact | {'author': 'Writer'}], 'full author set'),
                ([exact | {'title': 'The Synthetic Long Work (Unabridged: Volume 2)'}], 'volume or series markers')):
            with self.subTest(relevant=relevant):
                reason = truncation_review(BROAD_SOURCE['title'], BROAD_SOURCE['author'], relevant, usable[:len(relevant)])
                self.assertIn(fragment, reason)
        self.assertIn('checksum-valid ISBN', truncation_review(BROAD_SOURCE['title'], BROAD_SOURCE['author'],
                                                               [exact, exact | {'author': 'Coauthor; Writer'}], usable[:1]))

    def test_exact_full_author_language_isbn(self):
        result, request = self.resolve()
        self.assertTrue(result['high_confidence'])
        self.assertEqual(result['candidate']['isbn'], '9780306406157')
        self.assertIn('unverified', result['reason'])
        self.assertEqual(request.call_count, 1)
        google = copy.deepcopy(GOOGLE)
        google['items'][0]['volumeInfo']['authors'] = ['Writer', 'Coauthor']
        result, _ = self.resolve(google=google, source=SOURCE | {'author': 'Coauthor; Writer'})
        self.assertTrue(result['high_confidence'])

    def test_author_mismatch_ambiguous_title_and_conflicting_work_block(self):
        for change in ({'authors': ['Voice']}, {'authors': ['Writer', 'Coauthor']}):
            google = copy.deepcopy(GOOGLE)
            google['items'][0]['volumeInfo'].update(change)
            result, request = self.resolve(google=google)
            self.assertFalse(result['high_confidence'])
            self.assertEqual(request.call_count, 1)  # no fallback laundering a conflict
        google = copy.deepcopy(GOOGLE)
        other = copy.deepcopy(google['items'][0])
        other['volumeInfo']['authors'] = ['Different Writer']
        google['items'].append(other)
        self.assertFalse(self.resolve(google=google)[0]['high_confidence'])
        google['items'][0]['volumeInfo']['title'] = 'One: A Different Work'
        self.assertFalse(self.resolve(google=google, library={'docs': []})[0]['high_confidence'])
        google['totalItems'] = 20
        self.assertFalse(self.resolve(google=google)[0]['high_confidence'])
        google = copy.deepcopy(GOOGLE)
        other = copy.deepcopy(google['items'][0])
        other['id'] = 'different-volume'
        other['volumeInfo']['industryIdentifiers'][0]['identifier'] = '9798217058785'
        google['items'].append(other)
        self.assertTrue(self.resolve(google=google)[0]['high_confidence'])
        other['volumeInfo']['industryIdentifiers'][0]['identifier'] = '0306406152'
        self.assertTrue(self.resolve(google=google)[0]['high_confidence'])

    def test_fallback_no_isbn_no_match_and_provider_failure(self):
        for google in ({'items': []}, AppError('Unavailable')):
            result, request = self.resolve(google=google)
            self.assertFalse(result['high_confidence'])
            self.assertEqual(result['candidate']['isbn'], '0306406152')
            self.assertEqual(result['provenance'], 'Open Library')
            self.assertEqual(request.call_count, 2)
        google = copy.deepcopy(GOOGLE)
        google['items'][0]['volumeInfo']['industryIdentifiers'][0]['identifier'] = '9780306406158'
        self.assertEqual(self.resolve(google=google)[0]['candidate']['isbn'], '0306406152')
        self.assertFalse(self.resolve(google=google, library={'docs': []})[0]['high_confidence'])
        library = copy.deepcopy(OPEN)
        library['docs'].append(library['docs'][0] | {'key': '/works/OTHER'})
        self.assertFalse(self.resolve(google={'items': []}, library=library)[0]['high_confidence'])
        self.assertEqual(valid_isbn('0000000000000'), '')

    def test_unsafe_source_and_explicit_contributor_roles_never_silently_fixed(self):
        for source in (SOURCE | {'author': 'Writer, Translator - translator'}, SOURCE | {'author': ''},
                       SOURCE | {'title': '=formula'}, SOURCE | {'author': 'x' * 1001}):
            result, request = self.resolve(source=source)
            self.assertFalse(result['high_confidence'])
            request.assert_not_called()

    def test_language_neutral_and_self_narration_requires_corroboration(self):
        for lang in ('', 'de', 'en'):
            google = copy.deepcopy(GOOGLE)
            google['items'][0]['volumeInfo']['language'] = lang
            result, _ = self.resolve(google=google, source=SOURCE | {'language': ''})
            self.assertTrue(result['high_confidence'])
            self.assertEqual(result['score'], 100)
        self.assertTrue(self.resolve(source=SOURCE | {'language': 'German'})[0]['high_confidence'])
        result, _ = self.resolve(source=SOURCE | {'narrator': 'Writer'})
        self.assertTrue(result['high_confidence'])
        self.assertIn('self-narration', result['reason'])
        result, request = self.resolve(source=SOURCE | {'author': 'Voice'})
        self.assertFalse(result['high_confidence'])
        self.assertEqual(request.call_count, 1)

    def test_multiple_editions_deterministic_print_preference(self):
        google = copy.deepcopy(GOOGLE)
        other = copy.deepcopy(google['items'][0])
        other['id'] = 'print'
        other['volumeInfo'].update(publisher='Synthetic publisher', language='de',
                                  industryIdentifiers=[{'identifier': '9798217058785'}])
        google['items'].append(other)
        result, _ = self.resolve(google=google)
        self.assertTrue(result['high_confidence'])
        self.assertEqual(result['candidate']['reference'], 'print')
        google['items'].reverse()
        self.assertEqual(self.resolve(google=google)[0]['candidate'], result['candidate'])

    def test_open_library_linked_edition_only_and_conservative_subtitles(self):
        library = copy.deepcopy(OPEN)
        library['docs'][0]['editions'] = {'docs': [{'key': '/books/SYNTHETIC', 'title': 'One',
                                                  'author_name': ['Writer'], 'isbn': ['0306406152']}]}
        self.assertTrue(self.resolve(google={'items': []}, library=library)[0]['high_confidence'])
        library['docs'][0]['editions']['docs'][0]['author_name'] = ['Voice']
        self.assertFalse(self.resolve(google={'items': []}, library=library)[0]['high_confidence'])
        google = copy.deepcopy(GOOGLE)
        google['items'][0]['volumeInfo']['title'] = 'The Synthetic Long Work: A Subtitle'
        # A catalog-added subtitle never auto-confirms a bare source title.
        result, _ = self.resolve(google=google, library={'docs': []},
                                 source=SOURCE | {'title': 'The Synthetic Long Work'})
        self.assertFalse(result['high_confidence'])
        self.assertIn('review required', result['reason'])
        self.assertFalse(self.resolve(google=google, library={'docs': []},
                         source=SOURCE | {'title': 'The Synthetic Long Work: Different Subtitle'})[0]['high_confidence'])

    def test_series_position_and_catalog_added_subtitles_never_auto_confirm(self):
        # Reported false positive: same author, catalog title names a later volume.
        for suffix, fragment in (('Volume 2', 'volume'), ('Book 3', 'volume'), ('Part 4', 'volume'),
                                 ('#5', 'volume'), ('A Novel', 'subtitle')):
            google = copy.deepcopy(GOOGLE)
            google['items'][0]['volumeInfo']['title'] = 'Long Enough Name: ' + suffix
            result, _ = self.resolve(google=google, library={'docs': []},
                                     source=SOURCE | {'title': 'Long Enough Name'})
            self.assertFalse(result['high_confidence'], suffix)
            self.assertIn('review required', result['reason'])
            self.assertIn(fragment, result['reason'])
        # A source series suffix against a bare catalog title is review, not auto.
        for suffix in ('Volume 2', 'Book 3', '#4'):
            google = copy.deepcopy(GOOGLE)
            google['items'][0]['volumeInfo']['title'] = 'Long Enough Name'
            result, _ = self.resolve(google=google, library={'docs': []},
                                     source=SOURCE | {'title': 'Long Enough Name: ' + suffix})
            self.assertFalse(result['high_confidence'], suffix)
            self.assertIn('Source subtitle names a volume, part or numbered series; review required', result['reason'])

    def test_source_extra_subtitle_auto_confirms_only_without_series_markers(self):
        for suffix, safe in (('A Short Subtitle', True), ('Volume 2', False), ('Book 3', False),
                             ('Part 1', False), ('#2', False)):
            google = copy.deepcopy(GOOGLE)
            google['items'][0]['volumeInfo']['title'] = 'The Synthetic Long Work'
            result, _ = self.resolve(google=google, library={'docs': []},
                                     source=SOURCE | {'title': 'The Synthetic Long Work: ' + suffix})
            self.assertEqual(result['high_confidence'], safe, suffix)
            if safe:
                self.assertEqual(result['score'], 95)
            else:
                self.assertIn('review required', result['reason'])

    def test_trailing_series_marker_is_not_an_edition(self):
        google = copy.deepcopy(GOOGLE)
        google['items'][0]['volumeInfo']['title'] = 'Long Enough Name'
        for suffix, safe in ((' (Series #1)', False), (' (The Book 3)', False), (' (Unabridged)', True)):
            result, _ = self.resolve(google=google, library={'docs': []},
                                     source=SOURCE | {'title': 'Long Enough Name' + suffix})
            self.assertEqual(result['high_confidence'], safe, suffix)

    def test_bounded_transport_cache_rate_limit_and_ssrf(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(GOOGLE).encode()
        opener = MagicMock()
        opener.open.return_value = response
        catalog = Catalog()
        with patch('isbn_catalog.urllib.request.build_opener', return_value=opener) as build:
            params = {'q': 'https://localhost/private?secret=credential'}
            catalog.request('Google Books', params)
            catalog.request('Google Books', params)
            self.assertEqual(opener.open.call_count, 1)
            req = opener.open.call_args.args[0]
            self.assertTrue(req.full_url.startswith(ENDPOINTS['Google Books'] + '?q='))
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)
            response.read.assert_called_once_with(1_000_001)
            self.assertTrue(any(isinstance(h, NoRedirect) for h in build.call_args.args))
            self.assertEqual(req.headers.get('Authorization'), None)
            with self.assertRaises(AppError): catalog.request('Google Books', {'q': 'different'})
            with self.assertRaises(AppError): catalog.request('https://127.0.0.1', {})
        for raw in (b'x' * 1_000_001, b'not JSON', b'[]'):
            response.read.return_value = raw
            with patch('isbn_catalog.urllib.request.build_opener', return_value=opener), self.assertRaises(AppError):
                Catalog().request('Google Books', {})
        with patch('isbn_catalog.urllib.request.build_opener', return_value=opener):
            opener.open.side_effect = OSError('redirect/error')
            with self.assertRaises(AppError): Catalog().request('Google Books', {})
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://localhost/'))


class ExportAutomationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.abs = FakeABS()
        self.abs.items[0]['media']['metadata'].update(language='English', narratorName='Voice')
        self.abs.user['mediaProgress'] = [{'libraryItemId': 'one', 'isFinished': True, 'finishedAt': '2026-10-01T18:00:00Z'}]
        self.store = Store(pathlib.Path(temp.name) / 'test.db', self.abs)
        self.store.sync()
        Telemetry(self.store)
        self.gr = Goodreads(self.store)

    def resolve(self):
        with patch.object(self.gr.catalog, 'request', return_value=GOOGLE):
            return self.gr.resolve({'book_ids': ['one']})['results'][0]

    def test_automatic_export_strict_schema_no_duplicate_and_privacy(self):
        self.store.feedback({'book_id': 'one', 'rating': 2, 'notes': 'PRIVATE SENTINEL'})
        with patch.object(self.gr.catalog, 'request', return_value=GOOGLE) as request:
            self.gr.resolve({'book_ids': ['one']})
        self.assertNotIn('PRIVATE', str(request.call_args_list))
        self.assertNotIn('finished', str(request.call_args_list))
        row = self.gr.review()['books'][0]
        self.assertTrue(row['ready'])
        self.assertTrue(row['automatic'])
        self.assertEqual(row['provenance'], 'Google Books')
        batch = self.gr.export(['one'])
        rows = list(csv.reader(io.StringIO(batch['csv'])))
        self.assertEqual(rows[0], HEADERS)
        self.assertEqual(rows[1], ['One', 'Writer', '9780306406157', '', '2026-10-01', 'read'])
        self.assertNotIn('PRIVATE', batch['csv'])
        with self.assertRaises(AppError): self.gr.export(['one'])
        self.assertEqual(self.abs.writes, [])
        self.gr.reconcile({'batch_id': batch['id'], 'failed_ids': ['one']})
        self.assertFalse(self.resolve()['automatic'])
        self.assertFalse(self.gr.review()['books'][0]['ready'])
        self.assertEqual(self.gr.batch(batch['id'])['csv'], batch['csv'])

    def test_missing_or_future_date_is_never_manufactured(self):
        for stamp in ('', '2999-01-01T00:00:00Z'):
            self.abs.user['mediaProgress'][0]['finishedAt'] = stamp
            self.store.sync()
            self.assertFalse(self.resolve()['automatic'])
            self.assertFalse(self.gr.review()['books'][0]['ready'])
            with self.store.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM gr_matches').fetchone()[0], 0)

    def test_source_changes_and_legacy_confirmations_block_without_deletion(self):
        self.resolve()
        with self.store.db() as db:
            original = dict(db.execute('SELECT * FROM gr_matches').fetchone())
        self.abs.items[0]['media']['metadata']['authorName'] = 'Different Writer'
        self.store.sync()
        row = self.gr.review()['books'][0]
        self.assertTrue(row['source_changed'])
        self.assertFalse(row['ready'])
        with self.store.db() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM gr_matches').fetchone()), original)
            db.execute('DELETE FROM gr_match_context')
        self.assertFalse(self.gr.review()['books'][0]['ready'])
        with self.assertRaises(AppError): self.gr.export(['one'])
        with self.assertRaises(AppError):
            self.gr.save(original | {'confirmed': True, 'ignored': False, 'source_snapshot': source_snapshot(SOURCE)})
        self.gr.save(original | {'confirmed': True, 'ignored': False})
        self.assertTrue(self.gr.review()['books'][0]['ready'])

    def test_same_title_different_authors_remain_and_history_only_excludes_identity(self):
        self.abs.items.append(item('other', 'One', 'Other Writer'))
        self.abs.user['mediaProgress'].append({'libraryItemId': 'other', 'isFinished': True, 'finishedAt': '2026-10-01'})
        self.store.sync()
        self.assertEqual(len(self.gr.review()['books']), 2)
        with self.store.db() as db:
            db.execute("INSERT INTO history VALUES ('123','One','Writer','',0,'read','','','',1,'one','writer')")
        self.assertEqual([b['id'] for b in self.gr.review()['books']], ['other'])

    def test_candidate_isbn_duplicate_history_skips_and_missing_isbn_not_ready(self):
        with self.store.db() as db:
            db.execute("INSERT INTO history VALUES ('123','Different saved edition title','Writer','0306406152',0,'read','','','',1,'different saved edition title','writer')")
        job = self.gr.start_match_batch()['job']
        with patch.object(self.gr.catalog, 'request', return_value=GOOGLE):
            result = self.gr.resolve({'book_ids': ['one'], 'batch_id': job['id']})
        self.assertEqual(result['job']['items'][0]['state'], 'skipped')
        self.assertFalse(self.gr.review()['books'][0]['ready'])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM gr_matches').fetchone()[0], 0)

    def test_unknown_and_different_language_are_export_ready(self):
        for lang in ('', 'German'):
            self.abs.items[0]['media']['metadata']['language'] = lang
            self.store.sync()
            with self.store.db() as db:
                db.execute('DELETE FROM gr_matches')
            self.assertTrue(self.resolve()['automatic'])
            self.assertTrue(self.gr.review()['books'][0]['ready'])

    def test_batch_without_catalog_isbn_stays_review(self):
        google = copy.deepcopy(GOOGLE)
        google['items'][0]['volumeInfo']['industryIdentifiers'] = []
        job = self.gr.start_match_batch()['job']
        with patch.object(self.gr.catalog, 'request', side_effect=[google, {'docs': []}]):
            result = self.gr.resolve({'book_ids': ['one'], 'batch_id': job['id']})
        self.assertEqual(result['job']['items'][0]['state'], 'review')
        self.assertFalse(self.gr.review()['books'][0]['ready'])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM gr_matches').fetchone()[0], 0)

    def test_action_bounds_and_existing_decision_not_overwritten(self):
        for ids in ([], ['one'] * 2, list(map(str, range(11))), 'one', [1]):
            with self.assertRaises(AppError): self.gr.resolve({'book_ids': ids})
        self.gr.save({'book_id': 'one', 'title': 'One', 'author': 'Writer', 'isbn': '0306406152',
                      'completed_date': '2026-10-01', 'confirmed': True})
        self.assertFalse(self.resolve()['automatic'])
        self.assertEqual(self.gr.review()['books'][0]['isbn'], '0306406152')

    def test_metadata_fingerprint_covers_language_isbn_narrator_title(self):
        self.resolve()
        baseline = copy.deepcopy(self.abs.items[0]['media']['metadata'])
        for field, value in (('language', 'German'), ('isbn', '0306406152'),
                             ('narratorName', 'Different Voice'), ('title', 'Other Work')):
            self.abs.items[0]['media']['metadata'] = baseline | {field: value}
            self.store.sync()
            row = self.gr.review()['books'][0]
            self.assertTrue(row['source_changed'])
            self.assertFalse(row['ready'])
        self.abs.items[0]['media']['metadata'] = baseline
        self.store.sync()
        batch = self.gr.export(['one'])
        self.abs.items[0]['media']['metadata'] = baseline | {'title': 'Changed After Export', 'authorName': 'Other Writer'}
        self.store.sync()
        self.assertEqual(self.gr.review()['books'], [])  # ABS export identity persists
        self.assertEqual(self.gr.batch(batch['id'])['csv'], batch['csv'])

    def test_completion_race_does_not_auto_save(self):
        def during_search(*args):
            self.abs.user['mediaProgress'][0]['finishedAt'] = ''
            self.store.sync()
            return GOOGLE
        with patch.object(self.gr.catalog, 'request', side_effect=during_search), self.assertRaises(AppError):
            self.gr.resolve({'book_ids': ['one']})
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM gr_matches').fetchone()[0], 0)

    def test_no_action_auto_prefill_and_no_repeat_or_failed_lookup(self):
        with patch.object(self.gr.catalog, 'request', return_value=GOOGLE) as request:
            self.assertTrue(self.gr.auto_resolve()['results'][0]['automatic'])
            self.assertTrue(self.gr.review()['books'][0]['ready'])
            self.gr.auto_last = -100
            self.assertEqual(self.gr.auto_resolve()['results'], [])
            batch = self.gr.export(['one'])
            self.gr.reconcile({'batch_id': batch['id'], 'failed_ids': ['one']})
            self.assertEqual(self.gr.auto_resolve()['results'], [])
            self.assertEqual(request.call_count, 1)

    def test_auto_uncertainty_cached_budget_pacing_and_source_invalidation(self):
        bad = copy.deepcopy(GOOGLE)
        bad['items'][0]['volumeInfo']['authors'] = ['Other Writer']
        with patch.object(self.gr.catalog, 'request', return_value=bad) as request:
            result = self.gr.auto_resolve()
            self.assertFalse(result['results'][0]['automatic'])
            self.assertIn('author mismatch', self.gr.review()['books'][0]['reason'])
            self.assertTrue(self.gr.auto_resolve()['deferred'])
            self.gr.auto_last = -100
            self.assertEqual(self.gr.auto_resolve()['results'], [])
            self.assertEqual(request.call_count, 1)
            self.abs.items[0]['media']['metadata']['language'] = 'German'
            self.store.sync()
            self.gr.auto_budget *= 10
            self.assertTrue(self.gr.auto_resolve()['deferred'])
            self.gr.auto_budget = []
            self.assertEqual(len(self.gr.auto_resolve()['results']), 1)
            self.assertEqual(request.call_count, 2)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM gr_matches').fetchone()[0], 0)

    def test_incident_narrator_authors_remain_review_without_catalog_corroboration(self):
        for metadata in ({'title': 'The Wildest Dreams Bookshop', 'authorName': 'Gracie Page, Venice Van Someren', 'language': '', 'narratorName': 'Venice Van Someren'},
                         {'title': 'Cleat Cute', 'authorName': 'Dana Varden, Zim Avaltrades', 'language': 'english', 'narratorName': 'Dana Varden, Zim Avaltrades'}):
            self.abs.items[0]['media']['metadata'].update(metadata)
            self.store.sync()
            self.gr.auto_last = -100
            with patch.object(self.gr.catalog, 'request', return_value=GOOGLE) as request:
                self.assertFalse(self.gr.auto_resolve()['results'][0]['automatic'])
                self.assertLessEqual(request.call_count, 2)

    def test_3000_book_queue_never_launches_per_book_catalog_requests(self):
        for index in range(3000):
            book = item('synthetic-' + str(index), 'Synthetic work ' + str(index))
            book['media']['metadata'].update(language='English', narratorName='Voice')
            self.abs.items.append(book)
            self.abs.user['mediaProgress'].append({'libraryItemId': book['id'], 'isFinished': True,
                                                   'finishedAt': '2026-10-01T18:00:00Z'})
        self.store.sync()
        with patch.object(self.gr.catalog, 'request', return_value={'items': [], 'docs': []}) as request:
            for _ in range(12):
                self.gr.auto_last = -100  # simulate independently paced list loads
                self.gr.auto_resolve()
            self.assertEqual(len(self.gr.auto_budget), 10)
            self.assertEqual(request.call_count, 20)  # at most two providers per book
            self.assertEqual(len(self.gr.review()['books']), 3001)

    def test_explicit_batch_25_abs_only_durable_resume_and_outcomes(self):
        for index in range(30):
            book = item('batch-' + str(index), 'Batch work ' + str(index))
            book['media']['metadata'].update(language='English', narratorName='Voice')
            self.abs.items.append(book)
            self.abs.user['mediaProgress'].append({'libraryItemId': book['id'], 'isFinished': True, 'finishedAt': '2026-10-01'})
        self.store.sync()
        with self.store.db() as db:
            db.executemany('INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           [(str(i), 'Seed ' + str(i), 'Seed Writer', '', 0, 'to-read', '', '', '', 0,
                             'seed ' + str(i), 'seed writer') for i in range(3000)])
        job = self.gr.start_match_batch()['job']
        self.assertEqual(len(job['items']), 25)
        self.assertEqual(self.gr.start_match_batch()['job'], job)
        def evidence(source):
            index = int(source['id'].split('-')[1]) if source['id'].startswith('batch-') else 0
            candidate = {'title': source['title'], 'author': source['author'], 'isbn': '9780306406157'}
            return {'candidate': candidate, 'high_confidence': index % 3 == 0,
                    'provider_error': index % 3 == 2, 'provenance': 'Synthetic catalog', 'reason': 'Synthetic evidence'}
        with patch.object(self.gr.catalog, 'resolve', side_effect=evidence) as lookup:
            for entry in job['items'][:7]:
                self.gr.resolve({'book_ids': [entry['id']], 'batch_id': job['id']})
            self.assertEqual(lookup.call_count, 7)
            # Reopening/restarting resumes the same job without repeating attempts.
            restarted = Goodreads(self.store)
            resumed = restarted.start_match_batch()['job']
            self.assertEqual(resumed['id'], job['id'])
            with patch.object(restarted.catalog, 'resolve', side_effect=evidence) as second:
                restarted.resolve({'book_ids': [job['items'][0]['id']], 'batch_id': job['id']})
                self.assertEqual(second.call_count, 0)
                for entry in resumed['items']:
                    if entry['state'] == 'queued':
                        restarted.resolve({'book_ids': [entry['id']], 'batch_id': job['id']})
                self.assertEqual(second.call_count, 18)
        done = restarted.match_job()
        self.assertTrue(all(i['state'] in ('ready', 'review', 'provider_error') for i in done['items']))
        review = restarted.review()['books']
        attempted_ids = {i['id'] for i in done['items']}
        self.assertTrue(all(not b['match_eligible'] for b in review if b['id'] in attempted_ids))
        suggestions = [b for b in review if b.get('suggestion')]
        self.assertTrue(suggestions)
        self.assertTrue(all(not b['confirmed'] and not b['ready'] for b in suggestions))
        self.assertEqual(len(restarted.start_match_batch()['job']['items']), 6)
        self.assertEqual(self.abs.writes, [])
        with self.assertRaises(AppError):
            restarted.resolve({'book_ids': ['123'], 'batch_id': job['id']})
        with self.assertRaises(AppError):
            restarted.resolve({'book_ids': [i['id'] for i in job['items'][:2]], 'batch_id': job['id']})

    def test_batch_changed_decision_skipped_and_interrupted_never_repeated(self):
        job = self.gr.start_match_batch()['job']
        self.gr.save({'book_id': 'one', 'title': 'One', 'author': 'Writer', 'isbn': '0306406152',
                      'completed_date': '2026-10-01', 'confirmed': False})
        with patch.object(self.gr.catalog, 'resolve') as lookup:
            result = self.gr.resolve({'book_ids': ['one'], 'batch_id': job['id']})
            lookup.assert_not_called()
        self.assertEqual(result['job']['items'][0]['state'], 'skipped')
        with self.store.db() as db:
            # Model an interrupted request claim; production never clears attempts.
            snapshot = self.gr.review()['books'][0]['source_snapshot']
            db.execute("UPDATE gr_match_job_items SET state='pending',result=?", (json.dumps({'automatic': False}),))
            db.execute('INSERT INTO gr_catalog_attempts VALUES (?,?,?)',
                       ('one', snapshot, json.dumps({'reason': 'Interrupted', 'provider_error': True})))
        restarted = Goodreads(self.store)
        with patch.object(restarted.catalog, 'resolve') as lookup:
            restarted.resolve({'book_ids': ['one'], 'batch_id': job['id']})
            restarted.auto_resolve()
            lookup.assert_not_called()
        self.assertEqual(restarted.review()['books'][0]['isbn'], '0306406152')

    def test_batch_provider_throttle_is_durable_review_not_retry(self):
        job = self.gr.start_match_batch()['job']
        response, opener = MagicMock(), MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"items": [], "docs": []}'
        opener.open.return_value = response
        self.gr.catalog.last = {'Google Books': 100, 'Open Library': 100}
        with patch('isbn_catalog.time.monotonic', return_value=101), patch('isbn_catalog.urllib.request.build_opener', return_value=opener):
            result = self.gr.resolve({'book_ids': ['one'], 'batch_id': job['id']})
            self.assertEqual(result['job']['items'][0]['state'], 'provider_error')
            self.gr.resolve({'book_ids': ['one'], 'batch_id': job['id']})
            opener.open.assert_not_called()
        self.assertFalse(Goodreads(self.store).review()['books'][0]['match_eligible'])


if __name__ == '__main__':
    unittest.main()
