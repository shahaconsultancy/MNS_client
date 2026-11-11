"""Retrieval priority: validated cache -> AMFI-listed location -> direct AMC route -> backup -> unavailable.

AMFI's portfolio-disclosure page links each AMC to its own monthly-portfolio page; it hosts no
portfolio files (checked live 10 Nov 2026). So the AMFI route finds files on the AMC's own site,
which are downloaded from the AMC and validated exactly like any other official file. Finding a
link is never success: only validated holdings are.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import holdings, source_health as sh
from .adapters import VIA_AMC_PAGE, VIA_AMFI_DIRECTORY, amfi_route, source_for_amc
from .holdings import _discover_files, _holdings_from_link, _overlap_debug_record, analyze_holdings_overlap, calculate_overlap
from .portfolio_store import _portfolio_dir, read_snapshot, store_snapshot, stored_links
from .providers import ProviderError, SourceFailure
from .tests_pipeline import holdings_result, portfolio_file
from .tests_source_health import CacheMixin

DSP = {'scheme_code': '119250', 'scheme_name': 'DSP Large Cap Fund', 'amc': 'DSP Mutual Fund', 'plan': 'Direct Plan', 'option': 'Growth', 'category': 'Equity Scheme - Large Cap Fund', 'date': '07-Nov-2026', 'nav': 500.0}
OTHER = {'scheme_code': '900', 'scheme_name': 'Other Flexi Cap Fund', 'amc': 'Other Mutual Fund', 'plan': 'Direct Plan', 'option': 'Growth', 'category': 'Equity Scheme - Flexi Cap Fund', 'date': '07-Nov-2026', 'nav': 100.0}
DSP_AMFI_PAGE = 'https://www.dspim.com/about-us/mandatory-disclosure/portfolio-disclosures'
DIRECTORY = {
    'DSP Mutual Fund': DSP_AMFI_PAGE,
    'HDFC Mutual Fund': 'https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio',
    'Edelweiss Mutual Fund': 'https://www.edelweissmf.com/statutory#Portfolio-of-Schemes',
    'Other Mutual Fund': 'https://www.othermf.test/portfolio',
}
OCT_ZIP = 'https://www.dspim.com/media/dsp-monthend-portfolio-as-on-31-october-2026.zip'
DIRECT_OCT = 'https://www.dspim.com/media/direct/oct-2026.xlsx'
HDFCB, ICICIB, REL, INFY = 'INE040A01034', 'INE090A01021', 'INE002A01018', 'INE009A01021'


def oct_link(url=OCT_ZIP):
    return {'url': url, 'text': 'Month End Portfolio October 2026', 'month': '2026-10', 'portfolio_date': '2026-10-31'}


@override_settings(FUND_ANALYZER_AMFI_FALLBACK_ENABLED=True)
class PriorityTests(CacheMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        directory = patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value=DIRECTORY)
        self.directory = directory.start()
        self.addCleanup(directory.stop)
        self.dsp = source_for_amc('DSP Mutual Fund')

    def routes(self, *, amfi=None, direct=None):
        """The AMFI-listed page (via _load_page_links) and the direct route (the direct lister), answering separately."""
        calls = []

        def load(amc_key, page_url, parser_key):
            calls.append(('amfi', page_url))
            if isinstance(amfi, Exception):
                raise amfi
            return amfi

        def lister(source, fund_name):
            calls.append(('direct', source['url']))
            if isinstance(direct, Exception):
                raise direct
            return direct

        return load, lister, calls

    def discover(self, source, amc, *, amfi=None, direct=None):
        load, lister, calls = self.routes(amfi=amfi, direct=direct)
        trail = {}
        with patch.object(holdings, '_load_page_links', side_effect=load):
            try:
                links = _discover_files(source, 'DSP Large Cap Fund', amc_name=amc, trail=trail, direct=lister)
            except ProviderError as exc:
                return exc, calls, trail
        return links, calls, trail

    # --- the order of the routes ---------------------------------------------------------------
    def test_amfi_listed_location_is_asked_before_the_direct_route(self):
        links, calls, trail = self.discover(self.dsp, 'DSP Mutual Fund', amfi=[oct_link()], direct=[oct_link(DIRECT_OCT)])
        self.assertEqual([kind for kind, _ in calls], ['amfi', 'direct'])
        self.assertEqual(calls[0][1], DSP_AMFI_PAGE)
        # Within a month, the file found through AMFI is tried first; the direct one stays as the fallback.
        self.assertEqual([(link['url'], link.get('via')) for link in links], [(OCT_ZIP, VIA_AMFI_DIRECTORY), (DIRECT_OCT, None)])
        self.assertEqual(links[0]['source_key'], 'dsp-mutual-fund--amfi')
        self.assertEqual((trail['amfi_discovery']['result'], trail['direct_listing']['result']), ('ok', 'ok'))

    def test_direct_route_is_used_when_the_amfi_listed_page_fails(self):
        moved = SourceFailure('404 Client Error: Not Found', category=sh.SOURCE_URL_MOVED)
        links, calls, trail = self.discover(self.dsp, 'DSP Mutual Fund', amfi=moved, direct=[oct_link(DIRECT_OCT)])
        self.assertEqual([kind for kind, _ in calls], ['amfi', 'direct'])
        self.assertEqual([link['url'] for link in links], [DIRECT_OCT])
        self.assertEqual((trail['amfi_discovery']['category'], trail['direct_listing']['result']), (sh.SOURCE_URL_MOVED, 'ok'))

    def test_dynamic_amfi_listed_page_with_no_file_falls_through_to_the_direct_route(self):
        # Invesco on 10 Nov 2026: AMFI's link answers, but the page renders its list with JavaScript.
        empty = SourceFailure('No monthly portfolio files found', category=sh.DISCOVERY_FAILED)
        links, _, trail = self.discover(self.dsp, 'DSP Mutual Fund', amfi=empty, direct=[oct_link(DIRECT_OCT)])
        self.assertEqual([link['url'] for link in links], [DIRECT_OCT])
        self.assertEqual(trail['amfi_discovery']['category'], sh.DISCOVERY_FAILED)

    def test_both_routes_failing_reports_the_direct_failure_and_finds_nothing(self):
        moved = SourceFailure('404 Client Error: Not Found', category=sh.SOURCE_URL_MOVED)
        direct_failure = SourceFailure('No monthly portfolio files found for dsp-mutual-fund', category=sh.DISCOVERY_FAILED)
        result, _, trail = self.discover(self.dsp, 'DSP Mutual Fund', amfi=moved, direct=direct_failure)
        self.assertIs(result, direct_failure)
        self.assertEqual((trail['amfi_discovery']['result'], trail['direct_listing']['result']), ('error', 'error'))

    def test_amfi_files_are_kept_when_only_the_direct_route_fails(self):
        result, _, trail = self.discover(self.dsp, 'DSP Mutual Fund', amfi=[oct_link()], direct=SourceFailure('503', category=sh.SOURCE_TEMPORARILY_UNAVAILABLE))
        self.assertEqual([link['url'] for link in result], [OCT_ZIP])
        self.assertEqual(trail['direct_listing']['category'], sh.SOURCE_TEMPORARILY_UNAVAILABLE)

    # --- no duplicate or pointless requests ------------------------------------------------------
    def test_same_destination_on_both_routes_is_requested_once(self):
        # 46 AMCs: the page we read is the one AMFI lists. One request serves both routes.
        other = source_for_amc('Other Mutual Fund')
        self.directory.reset_mock()
        load, lister, calls = self.routes(amfi=AssertionError('the AMFI page must not be fetched separately'), direct=[oct_link('https://www.othermf.test/oct.xlsx')])
        trail = {}
        with patch.object(holdings, '_load_page_links', side_effect=load):
            links = _discover_files(other, 'Other Flexi Cap Fund', amc_name='Other Mutual Fund', trail=trail, direct=lister)
        self.assertEqual(calls, [('direct', 'https://www.othermf.test/portfolio')])
        self.directory.assert_not_called()  # the directory was already read to find this page
        self.assertEqual(links[0]['via'], VIA_AMFI_DIRECTORY)
        self.assertEqual((trail['amfi_discovery']['same_as_direct'], trail['direct_listing']['result']), (True, 'not_repeated'))

    def test_amfi_link_that_redirects_to_the_direct_page_is_one_destination(self):
        # DSP on 10 Nov 2026: AMFI's link redirects to the page the registry already uses.
        sh.record_success('dsp-mutual-fund--amfi', stage='listing', months=['2026-10'], evidence={'final_url': self.dsp['url']})
        links, calls, trail = self.discover(self.dsp, 'DSP Mutual Fund', amfi=AssertionError('not fetched separately'), direct=[oct_link(DIRECT_OCT)])
        self.assertEqual([kind for kind, _ in calls], ['direct'])
        self.assertEqual((trail['amfi_discovery']['same_as_direct'], trail['direct_listing']['result'], links[0]['via']), (True, 'not_repeated', VIA_AMFI_DIRECTORY))

    def test_a_failed_same_destination_is_not_requested_again(self):
        other = source_for_amc('Other Mutual Fund')
        load, lister, calls = self.routes(direct=SourceFailure('404 Client Error: Not Found', category=sh.SOURCE_URL_MOVED))
        with patch.object(holdings, '_load_page_links', side_effect=load):
            with self.assertRaises(SourceFailure):
                _discover_files(other, 'Other Flexi Cap Fund', amc_name='Other Mutual Fund', direct=lister)
        self.assertEqual(len(calls), 1)

    def test_hdfc_file_pattern_is_credited_to_the_amc_route_not_amfi(self):
        # AMFI lists HDFC's listing page, which answers 403; HDFC's published file-name pattern supplies the files.
        hdfc = source_for_amc('HDFC Mutual Fund')
        guessed = {**oct_link('https://files.hdfcfund.com/s3fs-public/2026-11/Monthly%20HDFC%20Mid%20Cap%20Fund.xlsx'), 'guessed': True}
        load, lister, calls = self.routes(direct=[guessed])
        trail = {}
        with patch.object(holdings, '_load_page_links', side_effect=load):
            links = _discover_files(hdfc, 'HDFC Mid Cap Fund', amc_name='HDFC Mutual Fund', trail=trail, direct=lister)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('via', links[0])
        self.assertEqual((trail['amfi_discovery']['result'], trail['direct_listing']['method']), ('error', 'published file-name pattern'))

    def test_a_url_found_on_both_routes_is_listed_once_and_credited_to_amfi(self):
        links, _, _ = self.discover(self.dsp, 'DSP Mutual Fund', amfi=[oct_link()], direct=[oct_link(), oct_link(DIRECT_OCT)])
        self.assertEqual([(link['url'], link.get('via')) for link in links], [(OCT_ZIP, VIA_AMFI_DIRECTORY), (DIRECT_OCT, None)])

    def test_amfi_page_on_a_host_that_is_blocking_us_is_not_requested(self):
        sh.record_failure('dsp-mutual-fund', stage='listing', category=sh.ACCESS_BLOCKED, evidence={'url': 'https://www.dspim.com/mandatory-disclosures/portfolio-disclosures', 'http_status': 403})
        route, reason = amfi_route('DSP Mutual Fund', self.dsp)
        self.assertIsNone(route)
        self.assertIn('www.dspim.com is refusing', reason)

    def test_amfi_link_off_the_amcs_official_domain_is_refused(self):
        with patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value={**DIRECTORY, 'DSP Mutual Fund': 'https://dsp-portfolios.example/monthly'}):
            links, calls, trail = self.discover(self.dsp, 'DSP Mutual Fund', amfi=[oct_link()], direct=[oct_link(DIRECT_OCT)])
        self.assertEqual([kind for kind, _ in calls], ['direct'])
        self.assertIn('not an official domain', trail['amfi_discovery']['reason'])

    def test_restricted_amc_is_not_asked_on_either_route(self):
        route, reason = amfi_route('Edelweiss Mutual Fund', source_for_amc('Edelweiss Mutual Fund'))
        self.assertIsNone(route)
        self.assertIn('data API refuses', reason)

    def test_switch_off_uses_the_direct_route_only(self):
        with override_settings(FUND_ANALYZER_AMFI_FALLBACK_ENABLED=False):
            links, calls, trail = self.discover(self.dsp, 'DSP Mutual Fund', amfi=[oct_link()], direct=[oct_link(DIRECT_OCT)])
        self.assertEqual([kind for kind, _ in calls], ['direct'])
        self.assertEqual(trail['amfi_discovery']['result'], 'skipped')

    # --- storage, attribution and validation ----------------------------------------------------
    def test_file_found_through_amfi_is_downloaded_from_the_amc_and_attributed(self):
        payload = portfolio_file('DSP Large Cap Fund', [(HDFCB, 8.0), (ICICIB, 6.0), (REL, 7.0)], reporting_date='October 31, 2026')
        link = {**oct_link('https://www.dspim.com/media/oct-2026.xlsx'), 'via': VIA_AMFI_DIRECTORY, 'source_key': 'dsp-mutual-fund--amfi', 'discovery_page': DSP_AMFI_PAGE}
        with patch.object(holdings, '_download_cached', return_value=payload) as download:
            result = _holdings_from_link('119250', '2026-10', link, self.dsp, 'DSP Large Cap Fund', _overlap_debug_record('119250', DSP), {}, {}, DSP)
        self.assertEqual(download.call_args.args[0], 'dsp-mutual-fund--amfi')
        self.assertEqual(result['discovered_via'], VIA_AMFI_DIRECTORY)
        snapshot = read_snapshot(DSP, '2026-10')
        self.assertEqual(
            {key: snapshot[key] for key in ('source_type', 'discovered_via', 'discovery_page', 'download_host', 'source_url', 'reporting_date')},
            {'source_type': 'download', 'discovered_via': VIA_AMFI_DIRECTORY, 'discovery_page': DSP_AMFI_PAGE, 'download_host': 'www.dspim.com', 'source_url': 'https://www.dspim.com/media/oct-2026.xlsx', 'reporting_date': '2026-10-31'},
        )
        self.assertEqual(len(snapshot['file_hash']), 64)

    def test_a_verified_month_is_not_replaced_by_another_routes_file(self):
        for via, weight, url, payload in ((VIA_AMFI_DIRECTORY, 8.0, OCT_ZIP, b'amfi route'), (VIA_AMC_PAGE, 9.0, DIRECT_OCT, b'direct route')):
            store_snapshot(DSP, '119250', '2026-10', holdings_result({HDFCB: weight}, '2026-10-31'), source_url=url, payload=payload, discovery={'discovered_via': via, 'discovery_page': DSP_AMFI_PAGE, 'download_host': 'www.dspim.com'})
        snapshot = read_snapshot(DSP, '2026-10')
        self.assertEqual((snapshot['discovered_via'], snapshot['holdings'][0]['weight'], snapshot['source_url']), (VIA_AMFI_DIRECTORY, 8.0, OCT_ZIP))
        self.assertEqual(len([path for path in _portfolio_dir(DSP).iterdir() if path.name != 'status.json']), 1)

    def test_the_same_file_twice_is_one_record(self):
        for _ in range(2):
            store_snapshot(DSP, '119250', '2026-10', holdings_result({HDFCB: 8.0}, '2026-10-31'), source_url=OCT_ZIP, payload=b'same bytes', discovery={'discovered_via': VIA_AMFI_DIRECTORY, 'discovery_page': DSP_AMFI_PAGE, 'download_host': 'www.dspim.com'})
        self.assertEqual([link['month'] for link in stored_links(DSP)], ['2026-10'])

    def test_duplicate_isin_rows_are_added_up_and_counted(self):
        payload = portfolio_file('DSP Large Cap Fund', [(HDFCB, 5.0), (HDFCB, 3.0), (REL, 7.0)], reporting_date='October 31, 2026')
        with patch.object(holdings, '_download_cached', return_value=payload):
            result = _holdings_from_link('119250', '2026-10', oct_link('https://www.dspim.com/media/oct-2026.xlsx'), self.dsp, 'DSP Large Cap Fund', _overlap_debug_record('119250', DSP), {}, {}, DSP)
        self.assertEqual(result['holdings'][HDFCB], 8.0)
        self.assertEqual(read_snapshot(DSP, '2026-10')['duplicate_isin_rows'], 1)

    def test_invalid_isin_or_wrong_month_is_rejected(self):
        link = {**oct_link('https://www.dspim.com/media/oct-2026.xlsx'), 'via': VIA_AMFI_DIRECTORY, 'source_key': 'dsp-mutual-fund--amfi'}
        for payload in (
            portfolio_file('DSP Large Cap Fund', [('NOT-AN-ISIN', 8.0)], reporting_date='October 31, 2026'),
            portfolio_file('DSP Large Cap Fund', [(HDFCB, 8.0)], reporting_date='September 30, 2026'),
        ):
            with patch.object(holdings, '_download_cached', return_value=payload):
                with self.assertRaises(ProviderError):
                    _holdings_from_link('119250', '2026-10', link, self.dsp, 'DSP Large Cap Fund', _overlap_debug_record('119250', DSP), {}, {}, DSP)
        self.assertEqual(stored_links(DSP), [])

    # --- end to end through the overlap analysis ------------------------------------------------
    def analyze(self, files, *, amfi, direct):
        other_link = {'url': 'https://www.othermf.test/oct-2026.xlsx', 'text': 'Monthly Portfolio October 2026', 'month': '2026-10', 'portfolio_date': '2026-10-31'}
        files = {**files, other_link['url']: portfolio_file('Other Flexi Cap Fund', [(HDFCB, 10.0), (ICICIB, 5.0), (INFY, 4.0)], reporting_date='October 31, 2026')}
        downloads = []

        def load(amc_key, page_url, parser_key):
            if amc_key == 'dsp-mutual-fund--amfi':
                if isinstance(amfi, Exception):
                    raise amfi
                return amfi
            if amc_key == 'dsp-mutual-fund':
                if isinstance(direct, Exception):
                    raise direct
                return direct
            return [other_link]

        def download(key, month, url, **kwargs):
            downloads.append(url)
            return files[url]

        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[DSP, OTHER]),
            patch.object(holdings, '_load_page_links', side_effect=load),
            patch.object(holdings, '_download_cached', side_effect=download),
        ):
            return analyze_holdings_overlap(['119250', '900']), downloads

    def status(self):
        return json.loads((_portfolio_dir(DSP) / 'status.json').read_text(encoding='utf-8'))['sources']

    def test_overlap_uses_the_file_found_through_amfi_first_the_13_percent_example(self):
        files = {OCT_ZIP: portfolio_file('DSP Large Cap Fund', [(HDFCB, 8.0), (ICICIB, 6.0), (REL, 7.0)], reporting_date='October 31, 2026'), DIRECT_OCT: b'never needed'}
        result, downloads = self.analyze(files, amfi=[oct_link()], direct=[oct_link(DIRECT_OCT)])
        pair = result['pairs'][0]
        self.assertEqual((pair['portfolio_month'], pair['overlap_pct'], pair['left_portfolio_date'], pair['right_portfolio_date']), ('2026-10', 13.0, '2026-10-31', '2026-10-31'))
        self.assertEqual(pair['left_source'], 'official_amc')  # found via AMFI, still the AMC's own file
        self.assertNotIn(DIRECT_OCT, downloads)
        sources = self.status()
        self.assertEqual((sources['amfi_discovery']['result'], sources['selected']['discovered_via'], sources['attempts'][0]['route']), ('ok', VIA_AMFI_DIRECTORY, VIA_AMFI_DIRECTORY))

    def test_amfi_file_that_fails_validation_falls_back_to_the_direct_file_for_the_same_month(self):
        files = {
            OCT_ZIP: portfolio_file('DSP Large Cap Fund', [('NOT-AN-ISIN', 8.0)], reporting_date='October 31, 2026'),
            DIRECT_OCT: portfolio_file('DSP Large Cap Fund', [(HDFCB, 8.0), (ICICIB, 6.0), (REL, 7.0)], reporting_date='October 31, 2026'),
        }
        result, downloads = self.analyze(files, amfi=[oct_link()], direct=[oct_link(DIRECT_OCT)])
        self.assertEqual(downloads.index(OCT_ZIP) < downloads.index(DIRECT_OCT), True)
        self.assertEqual((result['pairs'][0]['portfolio_month'], result['pairs'][0]['overlap_pct']), ('2026-10', 13.0))
        attempts = self.status()['attempts']
        self.assertEqual([(attempt['route'], attempt['result']) for attempt in attempts], [(VIA_AMFI_DIRECTORY, 'error'), (VIA_AMC_PAGE, 'ok')])
        self.assertEqual(self.status()['selected']['discovered_via'], VIA_AMC_PAGE)

    def test_validated_cache_is_used_before_any_route(self):
        store_snapshot(DSP, '119250', '2026-10', holdings_result({HDFCB: 8.0, ICICIB: 6.0, REL: 7.0}, '2026-10-31'), source_url=OCT_ZIP, payload=b'x')
        sh.record_success('dsp-mutual-fund', stage='listing', months=['2026-10'])
        from .portfolio_store import record_status

        record_status(DSP, 'HOLDINGS_AVAILABLE', '', '2026-10')  # checked just now: no check is due
        result, downloads = self.analyze({}, amfi=AssertionError('AMFI route must not run'), direct=AssertionError('direct route must not run'))
        self.assertEqual(result['pairs'][0]['overlap_pct'], 13.0)
        self.assertNotIn(OCT_ZIP, downloads)

    def test_all_official_routes_failing_gives_no_overlap_figure_never_zero(self):
        result, _ = self.analyze({}, amfi=SourceFailure('No monthly portfolio files found', category=sh.DISCOVERY_FAILED), direct=SourceFailure('404 Client Error: Not Found', category=sh.SOURCE_URL_MOVED))
        self.assertEqual(result['pairs'], [])
        self.assertIsNone(result['highest_overlap']['percentage'])
        self.assertFalse(next(fund for fund in result['funds'] if fund['scheme_code'] == '119250')['available'])
        sources = self.status()
        self.assertEqual((sources['amfi_discovery']['result'], sources['direct_listing']['result'], sources['selected'], sources['backup']['enabled']), ('error', 'error', None, False))


class FormulaTests(SimpleTestCase):
    def test_the_13_percent_example(self):
        self.assertEqual(calculate_overlap({HDFCB: 8, ICICIB: 6, REL: 7}, {HDFCB: 10, ICICIB: 5, INFY: 4}), 13)


@override_settings(FUND_ANALYZER_AMFI_FALLBACK_ENABLED=True)
class ConcurrentUsersTests(CacheMixin, SimpleTestCase):
    def test_many_users_reading_one_cached_month_get_the_same_holdings_and_no_download(self):
        store_snapshot(DSP, '119250', '2026-10', holdings_result({HDFCB: 8.0, REL: 7.0}, '2026-10-31'), source_url=OCT_ZIP, payload=b'x')
        results, errors = [], []

        def read():
            try:
                link = stored_links(DSP)[0]
                results.append(_holdings_from_link('119250', '2026-10', link, source_for_amc('DSP Mutual Fund'), 'DSP Large Cap Fund', _overlap_debug_record('119250', DSP), {}, {}, DSP)['holdings'])
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        with patch.object(holdings, '_download_cached', side_effect=AssertionError('no download expected')):
            threads = [threading.Thread(target=read) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, [{HDFCB: 8.0, REL: 7.0}] * 12)


class DisplayStateTests(SimpleTestCase):
    def test_labels_staff_read(self):
        from .source_monitor import display_state

        cases = [
            ({'state': 'WORKING', 'discovery_method': 'directory_monthly_excel'}, 'WORKING'),
            ({'state': 'ACCESS_BLOCKED', 'discovery_method': 'manual_import'}, 'ACCESS_BLOCKED'),
            ({'state': 'DISCOVERY_FAILED', 'discovery_method': 'manual_import'}, 'UNSUPPORTED'),
            ({'state': 'DISCOVERY_FAILED', 'discovery_method': 'directory_monthly_excel', 'last_failure': {'category': 'SOURCE_URL_MOVED'}}, 'SOURCE_CHANGED'),
            ({'state': 'DISCOVERY_FAILED', 'discovery_method': 'json_downloads_api', 'source_url': 'https://a.test/x', 'amfi_listed_url': 'https://a.test/y'}, 'SOURCE_CHANGED'),
            ({'state': 'DISCOVERY_FAILED', 'discovery_method': 'directory_monthly_excel', 'source_url': 'https://a.test/x/', 'amfi_listed_url': 'https://a.test/x'}, 'UNSUPPORTED'),
            ({'state': 'PARSER_ISSUE', 'discovery_method': 'directory_monthly_excel'}, 'PARSER_FAILED'),
            ({'state': 'TEMPORARILY_UNAVAILABLE'}, 'TEMPORARILY_UNAVAILABLE'),
        ]
        for row, expected in cases:
            self.assertEqual(display_state(row), expected, row)


from . import charlie_alpha as ca  # noqa: E402
from .tests_charlie import FakeProvider, complete, make_isin, payload  # noqa: E402

DSP_C = {**DSP, 'isin': make_isin('INF740K0179')}
OTHER_C = {**OTHER, 'isin': make_isin('INF000O0001')}
CHARLIE_LABEL = 'Third-party API — Charlie Alpha'


@override_settings(FUND_ANALYZER_AMFI_FALLBACK_ENABLED=True)
class CharlieAfterBothOfficialRoutesTests(CacheMixin, SimpleTestCase):
    """cache -> AMFI-listed location -> direct AMC route -> Charlie Alpha -> unavailable."""

    def setUp(self):
        super().setUp()
        directory = patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value=DIRECTORY)
        directory.start()
        self.addCleanup(directory.stop)
        ca.clear_memory()
        self.addCleanup(ca.clear_memory)

    def analyze(self, provider, *, dsp_official=None):
        """DSP: both official routes fail unless dsp_official gives files. OTHER: its official file works."""
        other_link = {'url': 'https://www.othermf.test/oct-2026.xlsx', 'text': 'Monthly Portfolio October 2026', 'month': '2026-10', 'portfolio_date': '2026-10-31'}
        files = {other_link['url']: portfolio_file('Other Flexi Cap Fund', [(HDFCB, 10.0), (ICICIB, 5.0), (INFY, 4.0)], reporting_date='October 31, 2026')}
        files.update((dsp_official or {}).get('files', {}))
        routes = []

        def load(amc_key, page_url, parser_key):
            routes.append(amc_key)
            if amc_key == 'dsp-mutual-fund--amfi':
                raise SourceFailure('No monthly portfolio files found', category=sh.DISCOVERY_FAILED)
            if amc_key == 'dsp-mutual-fund':
                if dsp_official:
                    return dsp_official['links']
                raise SourceFailure('404 Client Error: Not Found', category=sh.SOURCE_URL_MOVED)
            return [other_link]

        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[DSP_C, OTHER_C]),
            patch.object(holdings, '_load_page_links', side_effect=load),
            patch.object(holdings, '_download_cached', side_effect=lambda key, month, url, **kw: files[url]),
            patch.object(ca, '_get_json', provider),
        ):
            result = analyze_holdings_overlap(['119250', '900'])
        return result, routes

    def dsp_backup(self, as_of='2026-10-31', weights=None):
        # A credible equity fund: the three shared stocks of the 13% example plus stocks only DSP holds.
        # (The backup's own validation refuses an "equity" fund that is mostly cash.)
        only_dsp = {make_isin(f'INE77{n}A0101'): 9.25 for n in range(8)}
        weights = weights or {HDFCB: 8.0, ICICIB: 6.0, REL: 7.0, **only_dsp}
        return FakeProvider({'119250': {as_of: payload(DSP_C, as_of, complete(weights))}})

    def fund(self, result, code):
        return next(item for item in result['funds'] if item['scheme_code'] == code)

    def status(self, record):
        return json.loads((_portfolio_dir(record) / 'status.json').read_text(encoding='utf-8'))['sources']

    @override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True)
    def test_charlie_alpha_fills_only_the_fund_both_official_routes_could_not_supply(self):
        provider = self.dsp_backup()
        result, routes = self.analyze(provider)
        # Both official routes were tried, AMFI-listed first, before the backup.
        self.assertEqual(routes[:2], ['dsp-mutual-fund--amfi', 'dsp-mutual-fund'])
        self.assertEqual(provider.codes_asked(), {'119250'})  # only the missing fund
        dsp, other = self.fund(result, '119250'), self.fund(result, '900')
        self.assertEqual((dsp['holdings_source'], dsp['holdings_source_label']), ('charlie_alpha', CHARLIE_LABEL))
        self.assertEqual((other['holdings_source'], other['holdings_source_label']), ('official_amc', 'Official AMC'))
        pair = result['pairs'][0]
        # Same reporting month on both sides; the formula is unchanged (the 13% example).
        self.assertEqual((pair['portfolio_month'], pair['overlap_pct'], pair['left_portfolio_date'], pair['right_portfolio_date']), ('2026-10', 13.0, '2026-10-31', '2026-10-31'))
        self.assertEqual((pair['holdings_source'], pair['holdings_source_label']), ('mixed', 'Official AMC and ' + CHARLIE_LABEL))
        snapshot = read_snapshot(DSP_C, '2026-10')
        self.assertEqual((snapshot['source_type'], snapshot['third_party'], snapshot['source_label']), ('charlie_alpha', True, CHARLIE_LABEL))
        self.assertEqual(read_snapshot(OTHER_C, '2026-10')['source_type'], 'download')
        sources = self.status(DSP_C)
        self.assertEqual(
            (sources['amfi_discovery']['result'], sources['direct_listing']['result'], sources['backup']['attempted'], sources['selected']['source_label']),
            ('error', 'error', True, CHARLIE_LABEL),
        )

    @override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True)
    def test_official_file_that_fails_validation_falls_back_to_charlie_for_that_month(self):
        bad = 'https://www.dspim.com/media/oct-2026.xlsx'
        provider = self.dsp_backup()
        result, _ = self.analyze(provider, dsp_official={
            'links': [{'url': bad, 'text': 'Month End Portfolio October 2026', 'month': '2026-10', 'portfolio_date': '2026-10-31'}],
            'files': {bad: portfolio_file('DSP Large Cap Fund', [('NOT-AN-ISIN', 8.0)], reporting_date='October 31, 2026')},
        })
        self.assertEqual(self.fund(result, '119250')['holdings_source'], 'charlie_alpha')
        self.assertEqual(result['pairs'][0]['overlap_pct'], 13.0)

    @override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True)
    def test_charlie_never_replaces_a_verified_official_month(self):
        store_snapshot(DSP_C, '119250', '2026-10', holdings_result({HDFCB: 9.0, REL: 1.0}, '2026-10-31'), source_url=OCT_ZIP, payload=b'official')
        from .portfolio_store import record_status

        record_status(DSP_C, 'HOLDINGS_AVAILABLE', '', '2026-10')
        provider = self.dsp_backup()
        result, _ = self.analyze(provider)
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.fund(result, '119250')['holdings_source'], 'official_amc')
        self.assertEqual(read_snapshot(DSP_C, '2026-10')['source_type'], 'download')

    @override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True)
    def test_charlie_month_outside_its_four_month_lookback_is_not_used(self):
        provider = self.dsp_backup(as_of='2026-06-30')  # within the official 6 months, outside the backup's 4
        result, _ = self.analyze(provider)
        self.assertEqual(result['pairs'], [])
        self.assertIsNone(result['highest_overlap']['percentage'])
        self.assertFalse(self.fund(result, '119250')['available'])

    @override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True)
    def test_invalid_charlie_portfolio_is_refused_and_shows_unavailable_not_zero(self):
        provider = self.dsp_backup(weights={HDFCB: 80.0, ICICIB: 60.0})  # adds up to more than 100%
        result, _ = self.analyze(provider)
        self.assertEqual(result['pairs'], [])
        self.assertIsNone(result['highest_overlap']['percentage'])
        dsp = self.fund(result, '119250')
        self.assertFalse(dsp['available'])
        self.assertTrue(dsp.get('backup_attempted'))
        self.assertIsNone(read_snapshot(DSP_C, '2026-10'))

    @override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True)
    def test_charlie_without_the_fund_shows_unavailable(self):
        result, _ = self.analyze(FakeProvider({}))
        self.assertEqual(result['pairs'], [])
        self.assertFalse(self.fund(result, '119250')['available'])

    def test_disabled_flag_means_no_request_and_holdings_unavailable(self):
        provider = self.dsp_backup()
        self.assertFalse(ca.enabled())
        result, _ = self.analyze(provider)
        self.assertEqual(provider.calls, [])
        self.assertEqual(result['pairs'], [])
        self.assertFalse(self.fund(result, '119250')['available'])


@override_settings(FUND_ANALYZER_AMFI_FALLBACK_ENABLED=True, FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True)
class BackupOnlyForTheMissingFundTests(CacheMixin, SimpleTestCase):
    """10 Nov 2026, Edelweiss + SBI + HDFC: Edelweiss exists only in the backup, for September. SBI and HDFC
    had only October cached and were checked recently, so their listings were skipped and the backup was
    used for all three. Their official September files must be found and used instead."""

    def setUp(self):
        super().setUp()
        directory = patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value=DIRECTORY)
        directory.start()
        self.addCleanup(directory.stop)
        ca.clear_memory()
        self.addCleanup(ca.clear_memory)

    def test_recently_checked_official_fund_is_listed_for_the_older_month_instead_of_using_the_backup(self):
        from .portfolio_store import record_status

        store_snapshot(OTHER_C, '900', '2026-10', holdings_result({HDFCB: 9.0}, '2026-10-31'), source_url='https://www.othermf.test/oct.xlsx', payload=b'oct')
        record_status(OTHER_C, 'HOLDINGS_AVAILABLE', '', '2026-10')  # checked just now: its listing is not due
        sep_url = 'https://www.othermf.test/sep-2026.xlsx'
        sep_file = portfolio_file('Other Flexi Cap Fund', [(HDFCB, 10.0), (ICICIB, 5.0), (INFY, 4.0)], reporting_date='September 30, 2026')
        only_dsp = {make_isin(f'INE77{n}A0101'): 9.25 for n in range(8)}
        provider = FakeProvider({
            '119250': {'2026-09-30': payload(DSP_C, '2026-09-30', complete({HDFCB: 8.0, ICICIB: 6.0, REL: 7.0, **only_dsp}))},
            '900': {'2026-09-30': payload(OTHER_C, '2026-09-30', complete({HDFCB: 1.0, **only_dsp}))},
        })

        def load(amc_key, page_url, parser_key):
            if amc_key.startswith('dsp'):
                raise SourceFailure('404 Client Error: Not Found', category=sh.SOURCE_URL_MOVED)
            return [{'url': sep_url, 'text': 'Monthly Portfolio September 2026', 'month': '2026-09', 'portfolio_date': '2026-09-30'}]

        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[DSP_C, OTHER_C]),
            patch.object(holdings, '_load_page_links', side_effect=load),
            patch.object(holdings, '_download_cached', side_effect=lambda key, month, url, **kw: {sep_url: sep_file}[url]),
            patch.object(ca, '_get_json', provider),
        ):
            result = analyze_holdings_overlap(['119250', '900'])
        funds = {fund['scheme_code']: fund for fund in result['funds']}
        self.assertEqual((funds['119250']['holdings_source'], funds['900']['holdings_source']), ('charlie_alpha', 'official_amc'))
        self.assertEqual(provider.codes_asked(), {'119250'})  # the official fund never reached the backup
        pair = result['pairs'][0]
        self.assertEqual((pair['portfolio_month'], pair['overlap_pct'], pair['right_source']), ('2026-09', 13.0, 'official_amc'))
        self.assertEqual(read_snapshot(OTHER_C, '2026-09')['source_type'], 'download')
