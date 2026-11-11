"""AMC-level retrieval: error categories, health, circuit breakers, recovery, listings and archives.

Each confirmed live failure from 10 Nov 2026 has a regression test here:
  ICICI   - AMFI-listed page 404 (moved to a JavaScript site); monthly ZIP of per-scheme workbooks
  DSP     - month-end portfolios published as ZIP files, skipped as 'not supported'
  Bajaj   - Monthly Portfolio list filled by WordPress admin-ajax, no links in the HTML
  Edelweiss - statutory page and its scripts answer 200, but the statutory-menus data API that lists the
              files answers Akamai 403: that route is recorded ACCESS_BLOCKED and never retried
  Invesco - AMFI-listed page redirects to a 404; current page has no file links
  Cloudflare's ordinary challenge-platform script on DSP's page wrongly read as a block
  week-long SOURCE_NOT_SUPPORTED statuses that outlived the fix
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from . import adapters, holdings, listings, portfolio_store as ps, source_health as sh
from .adapters import (
    AMBIGUOUS_MAPPING,
    DOWNLOAD_BLOCKED,
    SCHEME_NOT_FOUND,
    SOURCE_NOT_SUPPORTED,
    PortfolioError,
    classify_error,
    registry_sources,
    source_for_amc,
)
from .holdings import _archive_members, _archive_members_for_scheme, _download_portfolio_file, _holdings_from_link, _load_page_links, _overlap_debug_record
from .portfolio_store import clear_read_cache, discovery_due, record_status
from .providers import ProviderError, SourceFailure
from .tests_pipeline import LOCMEM, portfolio_file

ICICI = {'scheme_code': '120197', 'scheme_name': 'ICICI Prudential Liquid Fund -', 'amc': 'ICICI Prudential Mutual Fund', 'plan': 'Direct Plan', 'category': 'Equity Scheme - Large Cap Fund'}
EDELWEISS = {'scheme_code': '146196', 'scheme_name': 'Edelweiss Small Cap Fund', 'amc': 'Edelweiss Mutual Fund', 'plan': 'Direct Plan', 'category': 'Equity Scheme - Small Cap Fund'}
DSP = {'scheme_code': '119250', 'scheme_name': 'DSP Large Cap Fund', 'amc': 'DSP Mutual Fund', 'plan': 'Direct Plan', 'category': 'Equity Scheme - Large Cap Fund'}
OTHER = {'scheme_code': '900', 'scheme_name': 'Other Flexi Cap Fund', 'amc': 'Other Mutual Fund', 'plan': 'Direct Plan', 'category': 'Equity Scheme - Flexi Cap Fund'}
AKAMAI_DENIED = '<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY><H1>Access Denied</H1>Reference #18.7efd <P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18</P></BODY></HTML>'
# DSP's real listing page loads Cloudflare's bot-detection script on every page. It is not a block.
CLOUDFLARE_NORMAL_PAGE = '<html><head><title>Portfolio Disclosures | DSP</title><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></head><body><form><div class="g-recaptcha"></div></form></body></html>'


class CacheMixin:
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cache_dir = folder.name
        for starter in (
            override_settings(FUND_ANALYZER_HOLDINGS_CACHE_DIR=folder.name, CACHES=LOCMEM),
            patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value={'Other Mutual Fund': 'https://www.othermf.test/portfolio'}),
            patch('fund_analyzer.source_health.time.sleep'),
            patch('fund_analyzer.listings.time.sleep'),
        ):
            starter.enable() if hasattr(starter, 'enable') else starter.start()
            self.addCleanup(starter.disable if hasattr(starter, 'disable') else starter.stop)
        cache.clear()
        clear_read_cache()
        sh._last_request.clear()


def response(status=200, *, text='', url='https://www.othermf.test/portfolio', headers=None, json_body=None, content=b''):
    reply = MagicMock()
    reply.status_code = status
    reply.ok = status < 400
    reply.reason = {403: 'Forbidden', 404: 'Not Found', 429: 'Too Many Requests', 503: 'Service Unavailable'}.get(status, 'OK')
    reply.text = text
    reply.url = url
    reply.history = []
    reply.headers = {'Content-Type': 'application/json' if json_body is not None else 'text/html', **(headers or {})}
    reply.json.return_value = json_body
    reply.content = content
    if status >= 400:
        error = requests.HTTPError(f'{status} Client Error: {reply.reason} for url: {url}')
        error.response = reply
        reply.raise_for_status.side_effect = error
    else:
        reply.raise_for_status.return_value = None
    return reply


def zip_of(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class CategoryTests(SimpleTestCase):
    def test_http_answers_map_to_categories(self):
        cases = [
            ((403, 'listing'), sh.ACCESS_BLOCKED),
            ((451, 'download'), sh.ACCESS_BLOCKED),
            ((404, 'listing'), sh.SOURCE_URL_MOVED),
            ((404, 'download'), sh.DOWNLOAD_FAILED),
            ((429, 'listing'), sh.RATE_LIMITED),
            ((503, 'listing'), sh.SOURCE_TEMPORARILY_UNAVAILABLE),
            ((502, 'download'), sh.SOURCE_TEMPORARILY_UNAVAILABLE),
        ]
        for (status, stage), expected in cases:
            self.assertEqual(sh.category_for_http(status, stage=stage), expected, status)

    def test_a_guessed_file_that_does_not_exist_is_not_a_block(self):
        # HDFC's file host answers 403 for a month not published yet.
        self.assertEqual(sh.category_for_http(403, stage='download', guessed=True), sh.PORTFOLIO_MONTH_UNAVAILABLE)
        self.assertEqual(sh.category_for_http(404, stage='download', guessed=True), sh.PORTFOLIO_MONTH_UNAVAILABLE)

    def test_timeouts_and_connection_errors_are_temporary(self):
        for error in (requests.ReadTimeout('Read timed out'), requests.ConnectionError('Connection reset'), ProviderError('HTTPSConnectionPool: Max retries exceeded')):
            self.assertEqual(sh.category_for_exception(error, stage='listing'), sh.SOURCE_TEMPORARILY_UNAVAILABLE, error)

    def test_status_in_an_error_message_is_read(self):
        self.assertEqual(sh.category_for_exception(ProviderError('404 Client Error: Not Found for url: https://x'), stage='listing'), sh.SOURCE_URL_MOVED)

    def test_access_denied_pages_are_recognised(self):
        self.assertTrue(sh.looks_blocked(AKAMAI_DENIED))
        self.assertTrue(sh.looks_blocked('<html><head><title>Just a moment...</title><script>window._cf_chl_opt={}</script></head></html>'))

    def test_cloudflare_scripts_and_form_captchas_on_a_normal_page_are_not_a_block(self):
        self.assertFalse(sh.looks_blocked(CLOUDFLARE_NORMAL_PAGE))

    def test_retry_after_is_read_as_seconds_or_a_date(self):
        self.assertEqual(sh.retry_after_seconds('120'), 120.0)
        future = (datetime.now(timezone.utc) + timedelta(seconds=90)).strftime('%a, %d %b %Y %H:%M:%S GMT')
        self.assertTrue(60 <= sh.retry_after_seconds(future) <= 91)
        self.assertIsNone(sh.retry_after_seconds(None))

    def test_evidence_never_keeps_a_query_string(self):
        evidence = sh.evidence_from_response(response(403, url='https://x.test/a?token=secret'), 'https://x.test/a?token=secret')
        self.assertNotIn('secret', json.dumps(evidence))
        self.assertEqual(evidence['http_status'], 403)


class RegistryTests(CacheMixin, SimpleTestCase):
    def test_every_registry_entry_is_well_formed(self):
        payload = json.loads(adapters.REGISTRY_FILE.read_text(encoding='utf-8'))
        self.assertEqual(set(payload['amcs']), set(registry_sources()), 'an entry was dropped as invalid')
        for name, entry in payload['amcs'].items():
            host = adapters.urlsplit(entry['url']).hostname
            self.assertTrue(any(host == d or host.endswith('.' + d) for d in entry['domains']), name)
            self.assertIn(entry['parser'], adapters._ADAPTERS, name)
            self.assertTrue(entry.get('verified_at') and entry.get('notes'), name)

    def test_registry_overrides_amfis_out_of_date_page(self):
        source = source_for_amc('ICICI Prudential Mutual Fund')
        self.assertEqual(source['parser'], 'json_downloads_api')
        self.assertNotIn('news-and-media', source['url'])

    def test_restricted_source_sends_no_request_and_reports_a_block(self):
        source = source_for_amc('Edelweiss Mutual Fund')
        with patch('fund_analyzer.holdings.fetch_url') as fetch, patch('fund_analyzer.listings.requests.request') as request:
            with self.assertRaises(SourceFailure) as caught:
                _load_page_links(source['key'], source['url'], source['parser'])
        fetch.assert_not_called()
        request.assert_not_called()
        self.assertEqual(classify_error(caught.exception), DOWNLOAD_BLOCKED)
        self.assertEqual(sh.read_health(source['key'])['state'], sh.STATE_ACCESS_BLOCKED)

    def test_source_without_an_adapter_is_discovery_failed_not_blocked(self):
        source = source_for_amc('Invesco Mutual Fund')
        with patch('fund_analyzer.holdings.fetch_url') as fetch:
            with self.assertRaises(SourceFailure) as caught:
                _load_page_links(source['key'], source['url'], source['parser'])
        fetch.assert_not_called()
        self.assertEqual(classify_error(caught.exception), SOURCE_NOT_SUPPORTED)
        self.assertEqual(sh.read_health(source['key'])['state'], sh.STATE_DISCOVERY_FAILED)

    def test_validated_recovered_page_replaces_amfis_link_only_while_amfi_still_lists_the_old_one(self):
        key = 'other-mutual-fund'
        sh._write_health(key, {'key': key, 'recovered_url': 'https://www.othermf.test/new-portfolio', 'recovered_from': 'https://www.othermf.test/portfolio'})
        self.assertEqual(source_for_amc('Other Mutual Fund')['url'], 'https://www.othermf.test/new-portfolio')
        with patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value={'Other Mutual Fund': 'https://www.othermf.test/fixed-by-amfi'}):
            self.assertEqual(source_for_amc('Other Mutual Fund')['url'], 'https://www.othermf.test/fixed-by-amfi')

    def test_recovered_page_on_another_domain_is_ignored(self):
        key = 'other-mutual-fund'
        sh._write_health(key, {'key': key, 'recovered_url': 'https://evil.test/portfolio', 'recovered_from': 'https://www.othermf.test/portfolio'})
        self.assertEqual(source_for_amc('Other Mutual Fund')['url'], 'https://www.othermf.test/portfolio')


class ListingPageTests(CacheMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.source = source_for_amc('Other Mutual Fund')

    def load(self):
        return _load_page_links(self.source['key'], self.source['url'], self.source['parser'])

    def test_zip_portfolio_links_are_listed(self):
        # DSP: month-end portfolios are ZIP files.
        page = '<a href="/media/dsp-monthend-portfolio-as-on-31-october-2026.zip">Month End Portfolio</a><a href="/media/fund-performance-oct-2026.xlsx">x</a>'
        with patch('fund_analyzer.holdings.fetch_url', return_value=response(text=page)):
            links = self.load()
        self.assertEqual([link['url'].rsplit('/', 1)[-1] for link in links], ['dsp-monthend-portfolio-as-on-31-october-2026.zip'])
        self.assertEqual(sh.read_health(self.source['key'])['state'], sh.WORKING)

    def test_moved_page_is_source_url_moved_with_evidence(self):
        failure = SourceFailure('404 Client Error: Not Found for url: https://www.othermf.test/portfolio', evidence={'http_status': 404, 'content_type': 'text/html'})
        with patch('fund_analyzer.holdings.fetch_url', side_effect=failure):
            with self.assertRaises(ProviderError):
                self.load()
        health = sh.read_health(self.source['key'])
        self.assertEqual((health['state'], health['last_failure']['category'], health['last_failure']['stage']), (sh.STATE_DISCOVERY_FAILED, sh.SOURCE_URL_MOVED, 'listing'))
        self.assertEqual(health['last_failure']['evidence']['http_status'], 404)

    def test_page_that_loads_but_lists_nothing_is_discovery_failed(self):
        with patch('fund_analyzer.holdings.fetch_url', return_value=response(text='<div id="root"></div>')):
            with self.assertRaises(ProviderError):
                self.load()
        self.assertEqual(sh.read_health(self.source['key'])['last_failure']['category'], sh.DISCOVERY_FAILED)

    def test_a_page_listing_only_months_older_than_the_lookback_is_not_working(self):
        # Unifi and Zerodha CASE on 10 Nov 2026: pages answer, newest files are from 2025.
        page = '<a href="/a/monthly-portfolio-april-2025.xlsx">Monthly Portfolio April 2025</a>'
        with patch('fund_analyzer.holdings.fetch_url', return_value=response(text=page)):
            self.load()
        health = sh.read_health(self.source['key'])
        self.assertEqual(health['state'], sh.STATE_DISCOVERY_FAILED)
        self.assertIn('2025-04', health['last_failure']['detail'])

    def test_access_denied_page_served_with_200_is_a_block(self):
        with patch('fund_analyzer.holdings.fetch_url', return_value=response(text=AKAMAI_DENIED)):
            with self.assertRaises(ProviderError) as caught:
                self.load()
        self.assertEqual(classify_error(caught.exception), DOWNLOAD_BLOCKED)
        self.assertEqual(sh.read_health(self.source['key'])['state'], sh.STATE_ACCESS_BLOCKED)

    def test_cloudflare_page_with_files_is_working(self):
        page = CLOUDFLARE_NORMAL_PAGE.replace('</body>', '<a href="/a/monthly-portfolio-october-2026.xlsx">Monthly Portfolio October 2026</a></body>')
        with patch('fund_analyzer.holdings.fetch_url', return_value=response(text=page)):
            self.assertEqual(len(self.load()), 1)

    def test_a_block_opens_the_circuit_and_no_further_request_is_sent(self):
        failure = SourceFailure('403 Client Error: Forbidden for url: https://www.othermf.test/portfolio', evidence={'http_status': 403, 'cdn': 'akamai'})
        with patch('fund_analyzer.holdings.fetch_url', side_effect=failure) as fetch:
            with self.assertRaises(ProviderError):
                self.load()
            cache.clear()
            with self.assertRaises(SourceFailure) as caught:
                self.load()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(classify_error(caught.exception), DOWNLOAD_BLOCKED)
        self.assertIsNotNone(sh.circuit_open(self.source['key']))

    def test_a_circuit_opened_before_a_fix_is_ignored_after_it(self):
        failure = SourceFailure('403 Client Error: Forbidden', evidence={'http_status': 403})
        with patch('fund_analyzer.holdings.fetch_url', side_effect=failure):
            with self.assertRaises(ProviderError):
                self.load()
        cache.clear()
        page = '<a href="/a/monthly-portfolio-october-2026.xlsx">Monthly Portfolio October 2026</a>'
        with patch('fund_analyzer.source_health.RETRIEVAL_VERSION', sh.RETRIEVAL_VERSION + 1), patch('fund_analyzer.holdings.fetch_url', return_value=response(text=page)) as fetch:
            self.assertEqual(len(self.load()), 1)
        fetch.assert_called_once()

    def test_one_slow_answer_does_not_open_the_circuit_but_repeated_ones_do(self):
        for _ in range(2):
            cache.clear()
            with patch('fund_analyzer.holdings.fetch_url', side_effect=SourceFailure('Read timed out. (read timeout=15)')):
                with self.assertRaises(ProviderError):
                    self.load()
            if _ == 0:
                self.assertIsNone(sh.circuit_open(self.source['key']))
        self.assertIsNotNone(sh.circuit_open(self.source['key']))
        self.assertEqual(sh.read_health(self.source['key'])['state'], sh.TEMPORARILY_UNAVAILABLE)

    def test_a_working_listing_closes_the_circuit(self):
        sh.record_failure(self.source['key'], stage='listing', category=sh.SOURCE_TEMPORARILY_UNAVAILABLE)
        sh.record_failure(self.source['key'], stage='listing', category=sh.SOURCE_TEMPORARILY_UNAVAILABLE)
        sh.reset_circuit(self.source['key'])
        page = '<a href="/a/monthly-portfolio-october-2026.xlsx">Monthly Portfolio October 2026</a>'
        with patch('fund_analyzer.holdings.fetch_url', return_value=response(text=page)):
            self.load()
        health = sh.read_health(self.source['key'])
        self.assertEqual((health['state'], health['consecutive_failures'], health['circuit_open_until']), (sh.WORKING, 0, None))


class ListingReaderTests(CacheMixin, SimpleTestCase):
    """ICICI's JSON downloads API and Bajaj's WordPress listing, as their own pages call them."""

    def test_icici_json_listing_resolves_the_category_by_code_and_builds_official_file_urls(self):
        source = source_for_amc('ICICI Prudential Mutual Fund')
        categories = {'data': [{'subCategory': [{'id': 'cat-1', 'title': {'code': 'MONTHLY_PORTFOLIO_DISCSLO_DWND', 'text': 'Monthly Portfolio Disclosures'}}]}]}
        files = {'success': {'data': {'files': [
            {'title': {'text': 'Monthly Portfolio Disclosure October 2026'}, 'url': '/downloads/Files/Monthly Portfolio Disclosures/2026/Oct/Monthly-Portfolio-Disclosure-October-2026.zip', 'isEnabled': True},
            {'title': {'text': 'Monthly Portfolio Disclosure October 2019'}, 'url': '/downloads/old.zip', 'isEnabled': True},
        ]}}}
        answers = [response(json_body=categories, url=source['listing']['categories_url']), response(json_body=files, url=source['listing']['files_url'])]
        with patch('fund_analyzer.listings.requests.request', side_effect=answers) as request:
            links = _load_page_links(source['key'], source['url'], source['parser'])
        self.assertEqual([link['month'] for link in links], ['2026-10'])
        self.assertEqual(links[0]['url'], 'https://www.icicipruamc.com/blob/downloads/Files/Monthly%20Portfolio%20Disclosures/2026/Oct/Monthly-Portfolio-Disclosure-October-2026.zip')
        body = request.call_args_list[1].kwargs['json']
        self.assertEqual((body['categoryId'], body['fileType'], body['page']), ('cat-1', 'All', '1'))
        self.assertEqual(request.call_args_list[1].kwargs['headers']['env'], 'api')

    def test_icici_category_missing_is_discovery_failed(self):
        source = source_for_amc('ICICI Prudential Mutual Fund')
        with patch('fund_analyzer.listings.requests.request', return_value=response(json_body={'data': []}, url=source['listing']['categories_url'])):
            with self.assertRaises(SourceFailure) as caught:
                _load_page_links(source['key'], source['url'], source['parser'])
        self.assertEqual(caught.exception.category, sh.DISCOVERY_FAILED)

    def test_bajaj_wordpress_listing_uses_the_published_nonce_and_section(self):
        source = source_for_amc('Bajaj Finserv Mutual Fund')
        page = ('<script>var bajajDownloads = {"ajaxUrl":"https://www.bajajamc.com/wp-admin/admin-ajax.php","nonce":"abc123"};</script>'
                '<div class="bd-accordion " data-section-id="801" data-filter="year_month"><button><span class="bd-accordion-title">Monthly Portfolio</span></button></div>')
        html = ('<div class="bd-download-row"><span class="bd-download-title">Bajaj Finserv Mutual Fund_Monthly Portfolio as on 31 Oct 2026</span>'
                '<a href="https://media.bajajamc.com/wp-content/uploads/2026/11/Bajaj-Finserv-Mutual-Fund_Monthly-Portfolio-as-on-31-Oct-2026-1.xls">Download</a></div>')

        def request(method, url, **kwargs):
            if method == 'GET':
                return response(text=page, url=url)
            data = kwargs['data']
            body = {'success': True, 'data': {'html': html if data.get('month') == 'October' else ''}}
            return response(json_body=body, url=url)

        with patch('fund_analyzer.listings.requests.request', side_effect=request) as sent:
            links = _load_page_links(source['key'], source['url'], source['parser'])
        self.assertEqual([link['month'] for link in links], ['2026-10'])
        self.assertTrue(links[0]['url'].startswith('https://media.bajajamc.com/'))
        posts = [call.kwargs['data'] for call in sent.call_args_list if call.args[0] == 'POST']
        # The section is found by its title (801), not the configured fallback number.
        self.assertTrue(all(post['nonce'] == 'abc123' and post['section_id'] == '801' for post in posts))
        self.assertIn({'nonce': 'abc123', 'action': 'bajaj_get_downloads', 'section_id': '801', 'year': '2026-27', 'month': 'October'}, posts)

    def test_listing_link_off_the_official_domain_is_dropped(self):
        source = source_for_amc('ICICI Prudential Mutual Fund')
        categories = {'data': [{'id': 'cat-1', 'title': {'code': 'MONTHLY_PORTFOLIO_DISCSLO_DWND'}}]}
        files = {'success': {'data': {'files': [{'title': {'text': 'Monthly Portfolio Disclosure October 2026'}, 'url': 'https://evil.test/October-2026.zip'}]}}}
        answers = [response(json_body=categories, url=source['listing']['categories_url']), response(json_body=files, url=source['listing']['files_url'])]
        with patch('fund_analyzer.listings.requests.request', side_effect=answers):
            with self.assertRaises(ProviderError):
                _load_page_links(source['key'], source['url'], source['parser'])

    def test_rate_limit_waits_a_short_retry_after_then_succeeds(self):
        answers = [response(429, headers={'Retry-After': '2'}, url='https://www.icicipruamc.com/x'), response(json_body={}, url='https://www.icicipruamc.com/x')]
        with patch('fund_analyzer.listings.requests.request', side_effect=answers) as request, patch('fund_analyzer.listings.time.sleep') as sleep:
            listings.source_request('GET', 'https://www.icicipruamc.com/x', amc_key='icici-prudential-mutual-fund')
        self.assertEqual(request.call_count, 2)
        sleep.assert_any_call(2.0)

    def test_long_retry_after_is_not_waited_out_inline(self):
        with patch('fund_analyzer.listings.requests.request', return_value=response(429, headers={'Retry-After': '600'}, url='https://www.icicipruamc.com/x')) as request:
            with self.assertRaises(SourceFailure) as caught:
                listings.source_request('GET', 'https://www.icicipruamc.com/x', amc_key='icici-prudential-mutual-fund')
        self.assertEqual((request.call_count, caught.exception.category), (1, sh.RATE_LIMITED))

    def test_server_error_is_retried_once_but_a_block_never(self):
        with patch('fund_analyzer.listings.requests.request', return_value=response(503, url='https://www.icicipruamc.com/x')) as request:
            with self.assertRaises(SourceFailure):
                listings.source_request('GET', 'https://www.icicipruamc.com/x', amc_key='icici-prudential-mutual-fund')
        self.assertEqual(request.call_count, 2)
        with patch('fund_analyzer.listings.requests.request', return_value=response(403, url='https://www.icicipruamc.com/x')) as request:
            with self.assertRaises(SourceFailure) as caught:
                listings.source_request('GET', 'https://www.icicipruamc.com/x', amc_key='icici-prudential-mutual-fund')
        self.assertEqual((request.call_count, caught.exception.category), (1, sh.ACCESS_BLOCKED))


class DownloadTests(CacheMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        source_for_amc('Other Mutual Fund')  # registers the AMC's official domain

    def download_reply(self, status=200, chunks=(b'',), url='https://www.othermf.test/a.zip'):
        reply = response(status, url=url)
        reply.__enter__.return_value = reply
        reply.iter_content.return_value = chunks
        return reply

    def test_html_served_with_200_is_not_a_workbook(self):
        with patch('fund_analyzer.holdings.requests.get', return_value=self.download_reply(chunks=(AKAMAI_DENIED.encode(),))):
            with self.assertRaises(PortfolioError):
                _download_portfolio_file('other-mutual-fund', 'https://www.othermf.test/a.zip')
        self.assertEqual(sh.read_health('other-mutual-fund')['last_failure']['category'], sh.ACCESS_BLOCKED)

    def test_a_listed_file_blocked_is_recorded_against_the_amc(self):
        with patch('fund_analyzer.holdings.requests.get', return_value=self.download_reply(403)):
            with self.assertRaises(SourceFailure) as caught:
                _download_portfolio_file('other-mutual-fund', 'https://www.othermf.test/a.zip')
        self.assertEqual(caught.exception.category, sh.ACCESS_BLOCKED)
        self.assertEqual(sh.read_health('other-mutual-fund')['state'], sh.STATE_ACCESS_BLOCKED)

    def test_a_guessed_month_that_is_not_published_does_not_mark_the_amc(self):
        with patch('fund_analyzer.holdings.requests.get', return_value=self.download_reply(403)):
            with self.assertRaises(SourceFailure) as caught:
                _download_portfolio_file('other-mutual-fund', 'https://www.othermf.test/a.xlsx', guessed=True)
        self.assertEqual(caught.exception.category, sh.PORTFOLIO_MONTH_UNAVAILABLE)
        self.assertEqual(sh.read_health('other-mutual-fund'), {})

    def test_a_real_file_records_a_working_download(self):
        payload = zip_of({'a.xlsx': portfolio_file('X', [('INE040A01034', 5.0)])})
        with patch('fund_analyzer.holdings.requests.get', return_value=self.download_reply(chunks=(payload,))):
            self.assertEqual(_download_portfolio_file('other-mutual-fund', 'https://www.othermf.test/a.zip'), payload)
        self.assertEqual(sh.read_health('other-mutual-fund')['state'], sh.WORKING)


class ArchiveTests(CacheMixin, SimpleTestCase):
    def test_a_workbook_is_not_mistaken_for_an_archive(self):
        self.assertIsNone(_archive_members(portfolio_file('X', [('INE040A01034', 5.0)])))
        self.assertEqual(_archive_members(zip_of({'a.xlsx': b'x', '__MACOSX/a.xlsx': b'x', 'readme.txt': b'x'})), ['a.xlsx'])

    def test_per_scheme_archive_matches_the_workbook_named_for_the_scheme_despite_amfis_trailing_dash(self):
        members = [f'ICICI Prudential Fund {n}.xlsx' for n in range(10)] + ['ICICI Prudential Liquid Fund.xlsx', 'ICICI Prudential BSE Liquid Rate ETF - Growth.xlsx']
        self.assertEqual(_archive_members_for_scheme(members, 'ICICI Prudential Liquid Fund -'), ['ICICI Prudential Liquid Fund.xlsx'])

    def test_per_scheme_archive_never_reads_a_near_miss(self):
        members = [f'ICICI Prudential Fund {n}.xlsx' for n in range(10)] + ['ICICI Prudential Liquid Fund II.xlsx']
        with self.assertRaises(PortfolioError) as caught:
            _archive_members_for_scheme(members, 'ICICI Prudential Liquid Fund')
        self.assertEqual(caught.exception.status, SCHEME_NOT_FOUND)

    def test_two_workbooks_named_for_the_scheme_are_ambiguous(self):
        with self.assertRaises(PortfolioError) as caught:
            _archive_members_for_scheme(['X Fund.xlsx', 'x-fund.xlsx'], 'X Fund')
        self.assertEqual(caught.exception.status, AMBIGUOUS_MAPPING)

    def test_combined_archive_reads_every_workbook(self):
        self.assertEqual(_archive_members_for_scheme(['DSP ISIN DEBT Portfolio.xlsx', 'DSP Equity FOF ISIN Portfolio.xlsx'], 'DSP Large Cap Fund'), ['DSP ISIN DEBT Portfolio.xlsx', 'DSP Equity FOF ISIN Portfolio.xlsx'])

    def read(self, archive: bytes, record=DSP, month='2026-09'):
        source = {'key': 'dsp-mutual-fund', 'parser': 'directory_monthly_excel'}
        debug = _overlap_debug_record(record['scheme_code'], record)
        with patch('fund_analyzer.holdings._download_cached', return_value=archive):
            return _holdings_from_link(record['scheme_code'], month, {'url': 'https://www.dspim.com/a.zip', 'month': month}, source, record['scheme_name'], debug, {}, {}, record), debug

    def test_holdings_are_read_from_the_matching_workbook_in_a_combined_archive(self):
        archive = zip_of({
            'DSP ISIN DEBT Portfolio as on 30 Sep 2026.xlsx': portfolio_file('DSP Corporate Bond Fund', [('INE001A07RW2', 9.0)]),
            'DSP Equity ISIN Portfolio as on 30 Sep 2026.xlsx': portfolio_file('DSP Large Cap Fund', [('INE040A01034', 6.5), ('INE002A01018', 4.0)]),
        })
        result, debug = self.read(archive)
        self.assertEqual(result['holdings'], {'INE040A01034': 6.5, 'INE002A01018': 4.0})
        self.assertEqual(debug['archive_member'], 'DSP Equity ISIN Portfolio as on 30 Sep 2026.xlsx')
        self.assertEqual(result['source_url'], 'https://www.dspim.com/a.zip')

    def test_the_same_scheme_in_two_combined_workbooks_is_refused(self):
        archive = zip_of({
            'Part A.xlsx': portfolio_file('DSP Large Cap Fund', [('INE040A01034', 6.5)]),
            'Part B.xlsx': portfolio_file('DSP Large Cap Fund', [('INE002A01018', 4.0)]),
        })
        with self.assertRaises(PortfolioError) as caught:
            self.read(archive)
        self.assertEqual(caught.exception.status, AMBIGUOUS_MAPPING)

    def test_scheme_absent_from_the_archive_is_a_mapping_failure(self):
        archive = zip_of({'Debt.xlsx': portfolio_file('DSP Corporate Bond Fund', [('INE001A07RW2', 9.0)])})
        with self.assertRaises(ProviderError) as caught:
            self.read(archive)
        self.assertEqual(classify_error(caught.exception), SCHEME_NOT_FOUND)


class StaleStatusTests(CacheMixin, SimpleTestCase):
    def test_a_failure_recorded_under_an_older_source_configuration_is_retried_at_once(self):
        record_status(OTHER, SOURCE_NOT_SUPPORTED, 'nothing listed')
        self.assertFalse(discovery_due(OTHER))
        with patch('fund_analyzer.source_health.RETRIEVAL_VERSION', sh.RETRIEVAL_VERSION + 1):
            self.assertTrue(discovery_due(OTHER))

    def test_an_amc_wide_failure_waits_briefly_and_is_retried_once_the_amc_recovers(self):
        sh.record_failure('other-mutual-fund', stage='listing', category=sh.ACCESS_BLOCKED)
        record_status(OTHER, DOWNLOAD_BLOCKED, '403')
        status = ps._read_status(OTHER)
        self.assertEqual((status['scope'], status['category']), ('amc', sh.ACCESS_BLOCKED))
        wait = datetime.fromisoformat(status['next_check_at']) - datetime.now(timezone.utc)
        self.assertLessEqual(wait, ps.AMC_WIDE_RETRY_DELAY)
        self.assertFalse(discovery_due(OTHER))
        sh.record_success('other-mutual-fund', stage='listing', months=['2026-10'])
        self.assertTrue(discovery_due(OTHER))

    def test_a_schemes_own_mapping_failure_is_not_blamed_on_the_amc(self):
        sh.record_success('other-mutual-fund', stage='listing', months=['2026-10'])
        record_status(OTHER, SCHEME_NOT_FOUND, 'No sheet matched')
        self.assertEqual(ps._read_status(OTHER)['scope'], 'scheme')
        self.assertFalse(discovery_due(OTHER))

    def test_a_schemes_failure_never_marks_a_working_amc_broken(self):
        sh.record_success('other-mutual-fund', stage='listing', months=['2026-10'])
        sh.record_failure('other-mutual-fund', stage='parse', category=sh.HOLDINGS_PARSE_FAILED, detail='one sheet')
        self.assertEqual(sh.read_health('other-mutual-fund')['state'], sh.WORKING)

    def test_verified_holdings_survive_every_status_change(self):
        from .tests_pipeline import holdings_result

        ps.store_snapshot(OTHER, '900', '2026-09', holdings_result({'INE040A01034': 5.0}))
        record_status(OTHER, DOWNLOAD_BLOCKED, '403')
        with patch('fund_analyzer.source_health.RETRIEVAL_VERSION', sh.RETRIEVAL_VERSION + 1):
            discovery_due(OTHER)
        self.assertEqual([link['month'] for link in ps.stored_links(OTHER)], ['2026-09'])


class DiagnosticsTests(CacheMixin, SimpleTestCase):
    def test_restricted_amc_reports_its_block_with_evidence_and_no_request(self):
        from .diagnostics import diagnose_scheme

        with patch('fund_analyzer.holdings.fetch_url') as fetch, patch('fund_analyzer.listings.requests.request') as request:
            report = diagnose_scheme('146196', live=True, records=[EDELWEISS])
        fetch.assert_not_called()
        request.assert_not_called()
        self.assertFalse(report['available'])
        self.assertEqual(report['source']['access'], 'restricted')
        self.assertEqual(report['amc_health']['state'], sh.STATE_ACCESS_BLOCKED)
        self.assertTrue(report['final_reason'].startswith(sh.ACCESS_BLOCKED))

    def test_stored_holdings_are_reported_available(self):
        from .diagnostics import diagnose_scheme
        from .tests_pipeline import holdings_result

        ps.store_snapshot(EDELWEISS, '146196', '2026-09', holdings_result({'INE040A01034': 5.0}))
        report = diagnose_scheme('146196', records=[EDELWEISS])
        self.assertTrue(report['available'])
        self.assertIn('2026-09', report['final_reason'])

    def test_unknown_code(self):
        from .diagnostics import diagnose_scheme

        self.assertFalse(diagnose_scheme('1', records=[EDELWEISS])['available'])


@override_settings(CACHES=LOCMEM)
class StaffEndpointTests(TestCase):
    def test_health_and_diagnostics_are_staff_only(self):
        client = APIClient()
        for url in ('/api/overlap/sources', '/api/overlap/diagnose/146196'):
            self.assertIn(client.get(url).status_code, (401, 403), url)
        staff = get_user_model().objects.create_user(email='staff@example.com', password='x', is_staff=True)
        client.force_authenticate(staff)
        self.assertEqual(client.get('/api/overlap/sources').status_code, 200)
        with patch('fund_analyzer.diagnostics.fetch_amfi_latest', return_value=[EDELWEISS]):
            reply = client.get('/api/overlap/diagnose/146196')
        self.assertEqual(reply.status_code, 200)
        self.assertNotIn('portfolio_cache', json.dumps(reply.json()))
        self.assertEqual(client.get('/api/overlap/diagnose/abc').status_code, 400)


EDELWEISS_PAGE = 'https://www.edelweissmf.com/statutory'
EDELWEISS_API = 'https://api.edelweissmf.com/edelweissmf/api/v1/mf/statutory-menus'


class EdelweissDataApiTests(CacheMixin, SimpleTestCase):
    """Edelweiss on 10 Nov 2026: the page answers 200, the data API that lists its files answers 403."""

    def test_registry_records_the_page_as_reachable_and_only_the_data_api_as_blocked(self):
        entry = registry_sources()['Edelweiss Mutual Fund']
        evidence = entry['evidence']
        self.assertEqual((entry['access'], entry['restricted_route']), ('restricted', 'data_api'))
        self.assertEqual((evidence['statutory_page']['http_status'], evidence['page_scripts']['http_status']), (200, 200))
        self.assertEqual((evidence['data_api']['url'], evidence['data_api']['http_status'], evidence['data_api']['cdn']), (EDELWEISS_API, 403, 'akamai'))
        self.assertIn('could therefore not be retrieved', entry['notes'])

    def test_no_request_is_sent_and_the_block_names_the_data_api_not_the_website(self):
        source = source_for_amc('Edelweiss Mutual Fund')
        with patch('fund_analyzer.holdings.fetch_url') as fetch, patch('fund_analyzer.listings.requests.request') as request, patch('fund_analyzer.holdings.requests.get') as download:
            with self.assertRaises(SourceFailure) as caught:
                _load_page_links(source['key'], source['url'], source['parser'])
        fetch.assert_not_called()
        request.assert_not_called()
        download.assert_not_called()
        self.assertEqual((caught.exception.category, classify_error(caught.exception)), (sh.ACCESS_BLOCKED, DOWNLOAD_BLOCKED))
        self.assertIn('data API', str(caught.exception))
        self.assertIn('web page loads', str(caught.exception))
        self.assertEqual(sh.read_health(source['key'])['state'], sh.STATE_ACCESS_BLOCKED)

    def test_amfi_route_is_not_used_for_the_blocked_data_api(self):
        from .adapters import amfi_route

        route, reason = amfi_route('Edelweiss Mutual Fund', source_for_amc('Edelweiss Mutual Fund'))
        self.assertIsNone(route)
        self.assertIn('data API', reason)

    def test_page_200_then_data_api_403_is_access_blocked_with_akamai_evidence(self):
        # The generic request path, as any listing reader would meet this pattern on another AMC.
        source_for_amc('Edelweiss Mutual Fund')  # registers edelweissmf.com as the official domain
        page = response(200, text='<html><head><title>Statutory Disclosures</title></head><body><app-root></app-root></body></html>', url=EDELWEISS_PAGE, headers={'Akamai-GRN': '0.1'})
        denied = response(403, text=AKAMAI_DENIED, url=EDELWEISS_API, headers={'Akamai-GRN': '0.2'})
        with patch('fund_analyzer.listings.requests.request', side_effect=[page, denied]) as request:
            self.assertEqual(listings.source_request('GET', EDELWEISS_PAGE, amc_key='edelweiss-mutual-fund').status_code, 200)
            with self.assertRaises(SourceFailure) as caught:
                listings.source_request('GET', EDELWEISS_API, amc_key='edelweiss-mutual-fund', params={'type': 'Statutory', 'fundType': 'MF'})
        self.assertEqual(request.call_count, 2)  # the 403 is not retried
        evidence = caught.exception.evidence
        self.assertEqual((caught.exception.category, evidence['http_status'], evidence['cdn'], evidence['blocked_page']), (sh.ACCESS_BLOCKED, 403, 'akamai', True))
        self.assertEqual(evidence['url'], EDELWEISS_API)


@override_settings(CACHES=LOCMEM)
class EdelweissManualImportTests(TestCase):
    """The staff import is the supported route for 146196. Test files only; no real holdings are created here."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for starter in (
            override_settings(FUND_ANALYZER_HOLDINGS_CACHE_DIR=folder.name),
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[EDELWEISS, OTHER]),
            patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value={'Other Mutual Fund': 'https://www.othermf.test/portfolio'}),
        ):
            starter.enable() if hasattr(starter, 'enable') else starter.start()
            self.addCleanup(starter.disable if hasattr(starter, 'disable') else starter.stop)
        cache.clear()
        clear_read_cache()
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(email='staff@example.com', password='x', is_staff=True))

    def upload(self, rows, reporting_date='October 31, 2026', portfolio_date='2026-10-31'):
        from django.core.files.uploadedfile import SimpleUploadedFile

        workbook = SimpleUploadedFile('Edelweiss-Monthly-Portfolio-October-2026.xlsx', portfolio_file('Edelweiss Small Cap Fund', rows, reporting_date=reporting_date))
        return self.client.post('/api/overlap/import', {'file': workbook, 'scheme_code': '146196', 'portfolio_date': portfolio_date, 'source_url': EDELWEISS_PAGE}, format='multipart')

    def test_staff_upload_for_146196_is_validated_and_stored_as_an_upload(self):
        reply = self.upload([('INE040A01034', 3.5), ('INE002A01018', 2.0)])
        self.assertEqual(reply.status_code, 200, reply.content)
        body = reply.json()
        self.assertEqual((body['scheme_code'], body['holding_count'], body['portfolio_date']), ('146196', 2, '2026-10-31'))
        snapshot = ps.read_snapshot(EDELWEISS, '2026-10')
        self.assertEqual((snapshot['source_type'], snapshot['scheme_codes'], snapshot['source_url']), (ps.SOURCE_IMPORT, ['146196'], EDELWEISS_PAGE))

    def test_uploaded_month_is_used_for_overlap_without_asking_edelweiss(self):
        self.upload([('INE040A01034', 8.0), ('INE090A01021', 6.0), ('INE002A01018', 7.0)])
        other_file = portfolio_file('Other Flexi Cap Fund', [('INE040A01034', 10.0), ('INE090A01021', 5.0), ('INE009A01021', 4.0)], reporting_date='October 31, 2026')
        other_link = [{'url': 'https://www.othermf.test/oct.xlsx', 'text': 'Monthly Portfolio October 2026', 'month': '2026-10', 'portfolio_date': '2026-10-31'}]

        def load(key, url, parser):
            if key.startswith('edelweiss'):
                raise AssertionError('Edelweiss must not be asked')
            return other_link

        with (
            patch('fund_analyzer.holdings.fetch_url', side_effect=AssertionError('no page request expected')),
            patch('fund_analyzer.listings.requests.request', side_effect=AssertionError('no API request expected')),
            patch('fund_analyzer.holdings._load_page_links', side_effect=load),
            patch('fund_analyzer.holdings._download_cached', return_value=other_file),
        ):
            result = holdings.analyze_holdings_overlap(['146196', '900'])
        pair = result['pairs'][0]
        self.assertEqual((pair['portfolio_month'], pair['overlap_pct']), ('2026-10', 13.0))

    def test_a_file_for_another_month_is_rejected_and_nothing_is_stored(self):
        reply = self.upload([('INE040A01034', 3.5)], reporting_date='September 30, 2026', portfolio_date='2026-10-31')
        self.assertEqual(reply.status_code, 400)
        self.assertIsNone(ps.read_snapshot(EDELWEISS, '2026-10'))
