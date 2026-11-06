"""Tests for portfolio discovery, the JSON holdings cache and retrieval.

Most classes use SimpleTestCase, which fails on any database query. That is what
proves the holdings path works with no database.
"""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch
import zipfile

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from openpyxl import Workbook
import pandas as pd
import requests
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from .adapters import (
    AMC_SOURCES,
    DISCLOSURE_NOT_PUBLISHED,
    DOWNLOAD_BLOCKED,
    DOWNLOAD_FAILED,
    HOLDINGS_AVAILABLE,
    INVALID_WORKBOOK,
    NO_COMMON_MONTH,
    PARSING_FAILED,
    SCHEME_NOT_FOUND,
    SOURCE_NOT_SUPPORTED,
    DirectoryAdapter,
    PortfolioError,
    classify_error,
    is_official_url,
    month_in_text,
    normalise_name,
    registered_domain,
)
from .holdings import _check_workbook_payload, _download_portfolio_file, _holdings_from_link, _link_error_status, _overlap_debug_record, _read_equity_holdings, _reporting_date_from_frame, analyze_holdings_overlap, fetch_fund_holdings, import_portfolio_holdings
from .ingestion import eligible_portfolios, run_ingestion
from .portfolio_store import SOURCE_IMPORT, VERIFIED, _portfolio_dir, clear_read_cache, _write_json_atomic, coverage_report, discovery_due, file_scheme_name, portfolio_key, read_snapshot, record_status, source_status, store_snapshot, stored_links
from .providers import ProviderError, fetch_amfi_disclosure_directory

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'pipeline-tests'}}
HDFC_LARGE = {'scheme_code': '119018', 'scheme_name': 'HDFC Large Cap Fund', 'amc': 'HDFC Mutual Fund', 'plan': 'Direct Plan', 'category': 'Equity Scheme - Large Cap Fund'}
HDFC_LARGE_REGULAR = {**HDFC_LARGE, 'scheme_code': '102000', 'plan': 'Regular Plan'}
SBI_LARGE = {'scheme_code': '119598', 'scheme_name': 'SBI Large Cap Fund', 'amc': 'SBI Mutual Fund', 'plan': 'Direct Plan', 'category': 'Equity Scheme - Large Cap Fund'}
OTHER = {'scheme_code': '900', 'scheme_name': 'Other Flexi Cap Fund', 'amc': 'Other Mutual Fund', 'plan': 'Direct Plan', 'category': 'Equity Scheme - Flexi Cap Fund'}
RECENT = {'2026-10', '2026-09', '2026-08'}


class PortfolioCacheMixin:
    """Give each test its own empty JSON cache, and no real AMFI directory lookups."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cache_dir = folder.name
        for starter in (
            override_settings(FUND_ANALYZER_HOLDINGS_CACHE_DIR=folder.name, CACHES=LOCMEM),
            patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value={}),
        ):
            starter.enable() if hasattr(starter, 'enable') else starter.start()
            self.addCleanup(starter.disable if hasattr(starter, 'disable') else starter.stop)
        cache.clear()
        # Parsed files are remembered per process. Each test starts with none.
        clear_read_cache()

    def month_files(self):
        return sorted(path for path in Path(self.cache_dir).rglob('*.json') if path.name != 'status.json')


def holdings_result(weights, reporting_date='2026-09-30'):
    return {'holdings': weights, 'names': {isin: f'Stock {isin[-2:]}' for isin in weights}, 'industries': {isin: 'Banks' for isin in weights}, 'benchmark': 'NIFTY 500 TRI', 'risk_level': 'Very High', 'portfolio_date': reporting_date}


def portfolio_file(fund_name, rows, reporting_date='September 30, 2026'):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = fund_name[:31]
    sheet.append(['Scheme', fund_name])
    sheet.append([f'Monthly Portfolio Statement as on {reporting_date}'])
    sheet.append(['Instrument', 'ISIN', '% to NAV'])
    sheet.append(['EQUITY & EQUITY RELATED'])
    sheet.append(['Equity Shares'])
    for isin, weight in rows:
        sheet.append(['Stock', isin, weight])
    payload = BytesIO()
    workbook.save(payload)
    return payload.getvalue()


class AdapterRuleTests(SimpleTestCase):
    def test_reporting_month_is_read_from_full_dates(self):
        for text, month in (
            ('October 31, 2026', '2026-10'),
            ('Monthly Portfolio- May 31, 2026.xlsx', '2026-05'),
            ('31st March 2026', '2026-03'),
            ('NIMF-MONTHLY-PORTFOLIO-30-Sep-26.xls', '2026-09'),
            ('September 2026 - All Funds', '2026-09'),
            ('PPFCF_PPFAS_Monthly_Portfolio_Report_September_30_2026.xlsx', '2026-09'),
            ('30 Sept 2026', '2026-09'),
            ('BOBBNPMF_Monthly_Portfolio_30-09-2026_20376.xls', '2026-09'),
            ('no date here', None),
        ):
            self.assertEqual(month_in_text(text), month, text)

    def test_errors_become_structured_statuses(self):
        for error, status in (
            ('403 Client Error: Forbidden for url: x', DOWNLOAD_BLOCKED),
            ('429 Too Many Requests', DOWNLOAD_BLOCKED),
            ('404 Client Error: Not Found for url: x', DISCLOSURE_NOT_PUBLISHED),
            ('No monthly portfolio files found for x', DISCLOSURE_NOT_PUBLISHED),
            ('Read timed out', DOWNLOAD_FAILED),
            ('502 Server Error', DOWNLOAD_FAILED),
            ('No sheet matched fund X', SCHEME_NOT_FOUND),
            ('Monthly portfolio workbook could not be parsed', INVALID_WORKBOOK),
            ('Equity holdings weights exceed 100% for X', PARSING_FAILED),
        ):
            self.assertEqual(classify_error(error), status, error)
        self.assertEqual(classify_error(PortfolioError(INVALID_WORKBOOK, 'bad')), INVALID_WORKBOOK)

    def test_403_on_a_guessed_file_means_not_published(self):
        error = ProviderError('403 Client Error')
        self.assertEqual(_link_error_status({'guessed': True}, error), DISCLOSURE_NOT_PUBLISHED)
        self.assertEqual(_link_error_status({}, error), DOWNLOAD_BLOCKED)

    def test_official_url_checks_the_domain_itself(self):
        self.assertTrue(is_official_url('https://files.hdfcfund.com/a.xlsx', ['hdfcfund.com']))
        self.assertFalse(is_official_url('https://hdfcfund.com.evil.test/a.xlsx', ['hdfcfund.com']))
        self.assertFalse(is_official_url('https://evilhdfcfund.com/a.xlsx', ['hdfcfund.com']))
        self.assertFalse(is_official_url('file:///etc/passwd', ['hdfcfund.com']))
        self.assertEqual(registered_domain('www.miraeassetmf.co.in'), 'miraeassetmf.co.in')
        self.assertEqual(registered_domain('assets-netstorage.growwmf.in'), 'growwmf.in')

    def test_directory_adapter_finds_only_monthly_portfolios(self):
        adapter = DirectoryAdapter({})
        self.assertTrue(adapter.is_candidate('Monthly Portfolio- Sep 30 2026.xlsx', 'https://x.in/Monthly%20Portfolio.xlsx'))
        self.assertTrue(adapter.is_candidate('September 2026 - All Funds', 'https://x.in/file.xlsx'))
        for text in ('Fortnightly Portfolio', 'Half Yearly Portfolio', 'Portfolio Overlap', 'Top 10 Portfolio', 'Proxy Voting Portfolio', 'Portfolio Dashboard'):
            self.assertFalse(adapter.is_candidate(text, 'https://x.in/file.xlsx'), text)
        self.assertFalse(adapter.is_candidate('Monthly Portfolio', 'https://x.in/file.pdf'))
        self.assertFalse(adapter.is_candidate('Download', 'https://x.in/Performance-update-as-a-part-of-portfolio.xlsx'))
        # A folder name must not reject a real portfolio file.
        self.assertTrue(adapter.is_candidate('September 2026 - All Funds', 'https://x.in/FileCDN/FactSheet/18650bf5.xlsx'))

    def test_ampersand_and_and_are_the_same_scheme(self):
        self.assertEqual(normalise_name('HDFC Banking & Financial Services Fund'), normalise_name('HDFC Banking and Financial Services Fund'))

    def test_reporting_date_without_a_space_after_the_comma_is_read(self):
        frame = pd.DataFrame([['Monthly Portfolio Statement as on September 30,2026']])
        self.assertEqual(_reporting_date_from_frame(frame), '2026-09-30')

    def test_directory_adapter_prefers_the_schemes_own_file_and_limits_requests(self):
        links = [
            {'url': f'https://x.in/portfolio-{index}.xlsx', 'text': 'All schemes', 'month': '2026-09'} for index in range(5)
        ] + [
            {'url': 'https://x.in/alpha-fund-portfolio.xlsx', 'text': 'Alpha Fund', 'month': '2026-08'},
            {'url': 'https://x.in/beta-fund-portfolio.xlsx', 'text': 'Beta Fund', 'month': '2026-08'},
            {'url': 'https://x.in/all-08.xlsx', 'text': 'All schemes', 'month': '2026-08'},
        ]
        selected = DirectoryAdapter({}).select_links(links, 'Alpha Fund')
        self.assertEqual(sum(link['month'] == '2026-09' for link in selected), 2)
        self.assertEqual([link['text'] for link in selected if link['month'] == '2026-08'], ['Alpha Fund'])

    def test_workbook_checks(self):
        with self.assertRaises(PortfolioError) as caught:
            _check_workbook_payload(b'<html>Access denied</html>')
        self.assertEqual(caught.exception.status, INVALID_WORKBOOK)
        with self.assertRaises(PortfolioError):
            _check_workbook_payload(b'PK\x03\x04 damaged')
        bomb = BytesIO()
        with zipfile.ZipFile(bomb, 'w') as archive:
            archive.writestr('a', b'x')
        with patch('fund_analyzer.holdings.MAX_WORKBOOK_UNPACKED_BYTES', 0):
            with self.assertRaises(PortfolioError):
                _check_workbook_payload(bomb.getvalue())
        _check_workbook_payload(portfolio_file('Any Fund', [('INE040A01034', 1.0)]))

    def test_placeholder_isin_is_not_treated_as_a_stock(self):
        payload = portfolio_file('Sample Fund', [('INE040A01034', 5.0), ('EQ528501XXXX', 0.1), ('NOT-AN-ISIN', 1.0)])
        self.assertEqual(_read_equity_holdings(payload, 'x.xlsx', 'Sample Fund', 'directory_monthly_excel'), {'INE040A01034': 5.0})


@override_settings(CACHES=LOCMEM)
class DirectoryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_amfi_directory_is_read_from_its_page_data(self):
        page = (
            r'{\"mf_id\":\"64\",\"mf_name\":\"PPFAS Mutual Fund\",\"amc_name\":\"PPFAS\",\"amc_monthly_portfolio_disclosure\":\"https://amc.ppfas.com/p/\"}'
            r'{\"mf_id\":\"1\",\"mf_name\":\"IL\\u0026FS Mutual Fund\",\"amc_monthly_portfolio_disclosure\":\"https://x.in/a?b=1\\u0026c=2\"}'
            r'{\"mf_id\":\"2\",\"mf_name\":\"No Link Fund\",\"amc_monthly_portfolio_disclosure\":\"\"}'
        )
        with patch('fund_analyzer.providers.fetch_url', return_value=MagicMock(text=page)):
            directory = fetch_amfi_disclosure_directory()
        self.assertEqual(directory, {'PPFAS Mutual Fund': 'https://amc.ppfas.com/p/', 'IL&FS Mutual Fund': 'https://x.in/a?b=1&c=2'})

    def test_unreadable_directory_is_an_error_not_an_empty_list(self):
        with patch('fund_analyzer.providers.fetch_url', return_value=MagicMock(text='<html></html>')):
            with self.assertRaises(ProviderError):
                fetch_amfi_disclosure_directory()


@override_settings(CACHES=LOCMEM)
class DownloadSafetyTests(SimpleTestCase):
    def _response(self, *, url='https://files.hdfcfund.com/a.xlsx', chunks=(b'PK\x03\x04',), status=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.url = url
        response.iter_content.return_value = chunks
        response.raise_for_status.side_effect = requests.HTTPError(status) if status else None
        return response

    def test_off_domain_file_is_never_requested(self):
        with patch('fund_analyzer.holdings.requests.get') as get:
            with self.assertRaises(PortfolioError):
                _download_portfolio_file('hdfc', 'https://evil.test/a.xlsx')
        get.assert_not_called()

    def test_redirect_away_from_the_amc_is_rejected(self):
        with patch('fund_analyzer.holdings.requests.get', return_value=self._response(url='https://evil.test/a.xlsx')):
            with self.assertRaises(PortfolioError):
                _download_portfolio_file('hdfc', 'https://files.hdfcfund.com/a.xlsx')

    def test_oversized_file_stops_downloading(self):
        with (
            patch('fund_analyzer.holdings.requests.get', return_value=self._response(chunks=(b'PK\x03\x04', b'x' * 20))),
            patch('fund_analyzer.holdings.MAX_PORTFOLIO_FILE_BYTES', 10),
        ):
            with self.assertRaises(PortfolioError) as caught:
                _download_portfolio_file('hdfc', 'https://files.hdfcfund.com/a.xlsx')
        self.assertEqual(caught.exception.status, INVALID_WORKBOOK)

    def test_html_page_instead_of_a_workbook_is_rejected(self):
        with patch('fund_analyzer.holdings.requests.get', return_value=self._response(chunks=(b'<html>Sign in</html>',))):
            with self.assertRaises(PortfolioError):
                _download_portfolio_file('hdfc', 'https://files.hdfcfund.com/a.xlsx')

    def test_http_403_becomes_blocked(self):
        with patch('fund_analyzer.holdings.requests.get', return_value=self._response(status='403 Client Error: Forbidden')):
            with self.assertRaises(ProviderError) as caught:
                _download_portfolio_file('hdfc', 'https://files.hdfcfund.com/a.xlsx')
        self.assertEqual(classify_error(caught.exception), DOWNLOAD_BLOCKED)


class JsonCacheTests(PortfolioCacheMixin, SimpleTestCase):
    def test_each_month_is_a_separate_file_per_amc_and_portfolio(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        store_snapshot(SBI_LARGE, '119598', '2026-09', holdings_result({'INE040A01034': 3.0}))
        store_snapshot(HDFC_LARGE, '119018', '2026-08', holdings_result({'INE040A01034': 4.0}, '2026-08-31'))
        relative = sorted(path.relative_to(self.cache_dir).as_posix() for path in self.month_files())
        self.assertEqual(relative, [
            'hdfc-mutual-fund/hdfc-large-cap-fund/2026-08.json',
            'hdfc-mutual-fund/hdfc-large-cap-fund/2026-09.json',
            'sbi-mutual-fund/sbi-large-cap-fund/2026-09.json',
        ])

    def test_cache_file_is_structured_json_with_every_field(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}), source_url='https://files.hdfcfund.com/a.xlsx', payload=b'file')
        with open(self.month_files()[0], encoding='utf-8') as file:
            saved = json.load(file)
        self.assertEqual(saved['amc'], 'HDFC Mutual Fund')
        self.assertEqual(saved['portfolio_key'], 'hdfc-mutual-fund:hdfc large cap fund')
        self.assertEqual(saved['scheme_codes'], ['119018'])
        self.assertEqual(saved['reporting_date'], '2026-09-30')
        self.assertEqual(saved['source_url'], 'https://files.hdfcfund.com/a.xlsx')
        self.assertEqual(len(saved['file_hash']), 64)
        self.assertEqual(saved['validation_status'], VERIFIED)
        self.assertEqual(saved['holdings'], [{'isin': 'INE040A01034', 'name': 'Stock 34', 'industry': 'Banks', 'weight': 5.0}])

    def test_cache_folder_is_configurable(self):
        with tempfile.TemporaryDirectory() as other, override_settings(FUND_ANALYZER_HOLDINGS_CACHE_DIR=other):
            store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
            self.assertEqual(len(list(Path(other).rglob('2026-09.json'))), 1)
        self.assertEqual(self.month_files(), [])

    def test_scheme_name_can_not_leave_the_cache_folder(self):
        record = {**HDFC_LARGE, 'amc': '../../evil', 'scheme_name': '..\\..\\windows/system32'}
        folder = _portfolio_dir(record).resolve()
        self.assertTrue(folder.is_relative_to(Path(self.cache_dir).resolve()))
        self.assertNotIn('..', folder.relative_to(Path(self.cache_dir).resolve()).parts)

    def test_failed_write_keeps_the_old_file_and_leaves_no_temporary_file(self):
        target = Path(self.cache_dir) / 'a' / 'x.json'
        _write_json_atomic(target, {'value': 'old'})
        with patch('fund_analyzer.portfolio_store.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                _write_json_atomic(target, {'value': 'new'})
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'value': 'old'})
        self.assertEqual([path.name for path in target.parent.iterdir()], ['x.json'])

    def test_write_retries_when_windows_has_the_file_open(self):
        target = Path(self.cache_dir) / 'a' / 'x.json'
        real_replace = os.replace
        calls = []

        def busy_once(source, destination):
            calls.append(source)
            if len(calls) == 1:
                raise PermissionError('file in use')
            real_replace(source, destination)

        with patch('fund_analyzer.portfolio_store.os.replace', side_effect=busy_once), patch('fund_analyzer.portfolio_store.time.sleep'):
            _write_json_atomic(target, {'value': 'new'})
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'value': 'new'})

    def test_read_retries_while_another_process_is_replacing_the_file(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        real_open = open
        calls = []

        def busy_once(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:
                raise PermissionError('file in use')
            return real_open(*args, **kwargs)

        with patch('builtins.open', side_effect=busy_once), patch('fund_analyzer.portfolio_store.time.sleep'):
            self.assertIsNotNone(read_snapshot(HDFC_LARGE, '2026-09'))
        self.assertEqual(len(calls), 2)

    def test_many_writers_never_leave_a_bad_file_or_lose_a_scheme_code(self):
        def write(index):
            store_snapshot(HDFC_LARGE, str(index), '2026-09', holdings_result({'INE040A01034': 5.0}), payload=b'same')

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(write, range(40)))
        snapshot = read_snapshot(HDFC_LARGE, '2026-09')
        self.assertEqual(len(snapshot['scheme_codes']), 40)
        self.assertEqual(list(self.month_files()[0].parent.glob('*.tmp')), [])

    def test_damaged_or_tampered_file_counts_as_missing(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        path = self.month_files()[0]
        good = json.loads(path.read_text(encoding='utf-8'))
        for bad_text in (
            '{"half written',
            '[]',
            json.dumps({**good, 'month': '2026-08'}),
            json.dumps({**good, 'validation_status': 'UNCHECKED'}),
            json.dumps({**good, 'holdings': []}),
            json.dumps({**good, 'holdings': [{'isin': 'BAD', 'weight': 1}]}),
            json.dumps({**good, 'holdings': [{'isin': 'INE040A01034', 'weight': 'lots'}]}),
            json.dumps({**good, 'holdings': [{'isin': 'INE040A01034', 'weight': 90}, {'isin': 'INE090A01021', 'weight': 90}]}),
        ):
            path.write_text(bad_text, encoding='utf-8')
            self.assertIsNone(read_snapshot(HDFC_LARGE, '2026-09'), bad_text[:40])
            self.assertEqual(stored_links(HDFC_LARGE), [])

    def test_invalid_holdings_are_never_written(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'NOT-AN-ISIN': 5.0}))
        store_snapshot(HDFC_LARGE, '119018', '2026-08', holdings_result({'INE040A01034': 5.0}, '2026-09-30'))
        self.assertEqual(self.month_files(), [])

    def test_disk_failure_does_not_stop_the_overlap(self):
        files = {'HDFC': portfolio_file('HDFC Large Cap Fund', [('INE040A01034', 7.0)]), 'SBI': portfolio_file('SBI Large Cap Fund', [('INE040A01034', 5.0)])}

        def links(_source, name):
            return [{'url': f'https://x.test/{name.split()[0]}.xlsx', 'text': name, 'month': '2026-09'}]

        with (
            patch('fund_analyzer.portfolio_store._write_json_atomic', side_effect=OSError('read-only file system')),
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[HDFC_LARGE, SBI_LARGE]),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=links),
            patch('fund_analyzer.holdings._download_cached', side_effect=lambda _key, _month, url: files[url.rsplit('/', 1)[-1][:-5]]),
        ):
            result = analyze_holdings_overlap(['119018', '119598'])
        self.assertEqual(result['highest_overlap']['percentage'], 5.0)


class OverlapFromCacheTests(PortfolioCacheMixin, SimpleTestCase):
    def _analyze(self, records, *, links=None, download=None, recent=RECENT):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=recent),
            patch('fund_analyzer.holdings._monthly_files_for_fund', **(links or {'side_effect': AssertionError('network used')})) as listing,
            patch('fund_analyzer.holdings._download_cached', **(download or {'side_effect': AssertionError('network used')})) as get,
        ):
            result = analyze_holdings_overlap([record['scheme_code'] for record in records])
        return result, listing, get

    def _mark_checked(self, *records):
        for record in records:
            record_status(record, HOLDINGS_AVAILABLE, latest_month='2026-10')

    def _large_cap_files(self):
        return {
            'https://files.hdfcfund.com/large.xlsx': portfolio_file('HDFC Large Cap Fund', [('INE040A01034', 7.0), ('INE090A01021', 2.0)]),
            'https://www.sbimf.com/large.xlsx': portfolio_file('SBI Large Cap Fund', [('INE040A01034', 5.0), ('INE090A01021', 4.0)]),
        }

    @staticmethod
    def _large_cap_links(_source, name):
        url = 'https://files.hdfcfund.com/large.xlsx' if 'HDFC' in name else 'https://www.sbimf.com/large.xlsx'
        return [{'url': url, 'text': name, 'month': '2026-09'}]

    def test_hdfc_large_119018_vs_sbi_large_119598_downloads_validates_and_caches_json(self):
        files = self._large_cap_files()
        result, _, _ = self._analyze([HDFC_LARGE, SBI_LARGE], links={'side_effect': self._large_cap_links}, download={'side_effect': lambda _key, _month, url: files[url]})
        self.assertEqual(result['highest_overlap']['percentage'], 7.0)
        self.assertEqual(result['highest_overlap']['portfolio_month'], '2026-09')
        self.assertEqual(len(self.month_files()), 2)
        self.assertEqual(result['status'], HOLDINGS_AVAILABLE)

    def test_hdfc_large_119018_vs_sbi_large_119598_second_request_reads_json_only(self):
        files = self._large_cap_files()
        self._analyze([HDFC_LARGE, SBI_LARGE], links={'side_effect': self._large_cap_links}, download={'side_effect': lambda _key, _month, url: files[url]})
        cache.clear()
        again, listing, get = self._analyze([HDFC_LARGE, SBI_LARGE])
        listing.assert_not_called()
        get.assert_not_called()
        self.assertEqual(again['highest_overlap']['percentage'], 7.0)

    def test_hdfc_large_119018_vs_sbi_large_119598_survive_403_from_cached_json(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        store_snapshot(SBI_LARGE, '119598', '2026-09', holdings_result({'INE040A01034': 3.0}))
        cache.clear()
        result, listing, _ = self._analyze([HDFC_LARGE, SBI_LARGE], links={'side_effect': ProviderError('403 Client Error: Forbidden')})
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(result['highest_overlap']['percentage'], 3.0)

    def test_missing_cache_and_403_shows_portfolio_holdings_unavailable(self):
        result, _, _ = self._analyze([HDFC_LARGE, SBI_LARGE], links={'side_effect': ProviderError('403 Client Error: Forbidden')})
        self.assertIsNone(result['highest_overlap']['percentage'])
        self.assertEqual(result['highest_overlap']['message'], 'Portfolio holdings unavailable')
        self.assertEqual(result['pairs'], [])
        self.assertEqual(self.month_files(), [])

    def test_damaged_cache_file_is_downloaded_again(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        self.month_files()[0].write_text('{"half written', encoding='utf-8')
        files = self._large_cap_files()
        result, _, get = self._analyze([HDFC_LARGE, SBI_LARGE], links={'side_effect': self._large_cap_links}, download={'side_effect': lambda _key, _month, url: files[url]})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(result['highest_overlap']['percentage'], 7.0)
        self.assertIsNotNone(read_snapshot(HDFC_LARGE, '2026-09'))

    def test_previous_month_is_used_when_the_latest_is_not_common(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-10', holdings_result({'INE040A01034': 9.0}, '2026-10-31'))
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        store_snapshot(SBI_LARGE, '119598', '2026-09', holdings_result({'INE040A01034': 3.0}))
        store_snapshot(SBI_LARGE, '119598', '2026-08', holdings_result({'INE040A01034': 2.0}, '2026-08-31'))
        self._mark_checked(HDFC_LARGE, SBI_LARGE)
        result, _, _ = self._analyze([HDFC_LARGE, SBI_LARGE])
        self.assertEqual(result['portfolio_month'], '2026-09')
        self.assertEqual(result['highest_overlap']['percentage'], 3.0)

    def test_no_common_month_is_reported_and_is_not_zero_percent(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-10', holdings_result({'INE040A01034': 9.0}, '2026-10-31'))
        store_snapshot(SBI_LARGE, '119598', '2026-08', holdings_result({'INE040A01034': 2.0}, '2026-08-31'))
        self._mark_checked(HDFC_LARGE, SBI_LARGE)
        result, _, _ = self._analyze([HDFC_LARGE, SBI_LARGE])
        self.assertIsNone(result['highest_overlap']['percentage'])
        self.assertEqual(result['status'], NO_COMMON_MONTH)
        self.assertEqual(result['highest_overlap']['message'], 'Portfolio holdings unavailable')

    def test_direct_and_regular_plans_share_one_cache_file(self):
        self.assertEqual(portfolio_key(HDFC_LARGE), portfolio_key(HDFC_LARGE_REGULAR))
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}), payload=b'file')
        store_snapshot(HDFC_LARGE_REGULAR, '102000', '2026-09', holdings_result({'INE040A01034': 5.0}), payload=b'file')
        self.assertEqual(len(self.month_files()), 1)
        self.assertEqual(read_snapshot(HDFC_LARGE, '2026-09')['scheme_codes'], ['102000', '119018'])

    def test_unsupported_amc_is_partial_among_other_funds(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        store_snapshot(SBI_LARGE, '119598', '2026-09', holdings_result({'INE040A01034': 3.0}))
        self._mark_checked(HDFC_LARGE, SBI_LARGE)
        result, _, _ = self._analyze([HDFC_LARGE, SBI_LARGE, OTHER])
        self.assertEqual((result['highest_overlap']['compared_pairs'], result['highest_overlap']['total_pairs']), (1, 3))
        self.assertTrue(result['highest_overlap']['partial_coverage'])
        self.assertEqual({fund['scheme_code']: fund['status'] for fund in result['funds']}, {'119018': HOLDINGS_AVAILABLE, '119598': HOLDINGS_AVAILABLE, '900': SOURCE_NOT_SUPPORTED})

    def test_unsupported_amc_alone_shows_the_required_message(self):
        result, _, _ = self._analyze([OTHER, {**OTHER, 'scheme_code': '901', 'scheme_name': 'Other Mid Cap Fund'}])
        self.assertEqual(result['highest_overlap']['message'], 'Portfolio holdings unavailable')

    def test_cached_json_serves_an_amc_with_no_source(self):
        store_snapshot(OTHER, '900', '2026-09', holdings_result({'INE040A01034': 8.0}))
        store_snapshot(SBI_LARGE, '119598', '2026-09', holdings_result({'INE040A01034': 3.0}))
        self._mark_checked(SBI_LARGE)
        result, _, _ = self._analyze([OTHER, SBI_LARGE])
        self.assertEqual(result['highest_overlap']['percentage'], 3.0)

    def test_new_amc_is_retrieved_live_through_amfis_directory(self):
        file = portfolio_file('Other Flexi Cap Fund', [('INE040A01034', 8.0)])
        store_snapshot(SBI_LARGE, '119598', '2026-09', holdings_result({'INE040A01034': 3.0}))
        self._mark_checked(SBI_LARGE)
        with patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value={'Other Mutual Fund': 'https://www.other.in/portfolio'}):
            result, listing, get = self._analyze(
                [OTHER, SBI_LARGE],
                links={'return_value': [{'url': 'https://www.other.in/all.xlsx', 'text': 'all', 'month': '2026-09'}]},
                download={'return_value': file},
            )
        listing.assert_called_once()
        self.assertEqual(listing.call_args.args[0]['parser'], 'directory_monthly_excel')
        get.assert_called_once()
        self.assertEqual(result['highest_overlap']['percentage'], 3.0)
        self.assertIsNotNone(read_snapshot(OTHER, '2026-09'))

    def test_blocked_source_is_not_asked_again_while_backing_off(self):
        links = {'side_effect': ProviderError('403 Client Error: Forbidden')}
        first, listing, _ = self._analyze([HDFC_LARGE, SBI_LARGE], links=links)
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(first['funds'][0]['status'], DOWNLOAD_BLOCKED)
        self.assertFalse(discovery_due(HDFC_LARGE))
        second, listing, _ = self._analyze([HDFC_LARGE, SBI_LARGE], links=links)
        listing.assert_not_called()
        self.assertEqual(second['funds'][0]['status'], DOWNLOAD_BLOCKED)

    def test_directory_page_with_no_usable_files_is_not_supported_yet(self):
        with patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value={'Other Mutual Fund': 'https://www.other.in/portfolio'}):
            result, _, _ = self._analyze([OTHER, SBI_LARGE], links={'side_effect': ProviderError('No monthly portfolio files found for other-mutual-fund')})
        self.assertEqual(result['funds'][0]['status'], SOURCE_NOT_SUPPORTED)
        self.assertEqual(result['highest_overlap']['message'], 'Portfolio holdings unavailable')

    def test_status_from_an_earlier_check_is_reported_for_a_blocked_amc(self):
        record_status(OTHER, DOWNLOAD_BLOCKED, '403 Client Error: Forbidden')
        result, _, _ = self._analyze([OTHER, SBI_LARGE], links={'return_value': []})
        self.assertEqual(result['funds'][0]['status'], DOWNLOAD_BLOCKED)
        self.assertEqual(result['highest_overlap']['message'], 'Portfolio holdings unavailable')

    def test_file_with_a_changed_name_is_still_accepted_on_its_content(self):
        link = {'url': 'https://files.hdfcfund.com/NEW-NAME-2026.xlsx', 'text': 'renamed', 'month': '2026-09'}
        debug = _overlap_debug_record('119018', HDFC_LARGE)
        with patch('fund_analyzer.holdings._download_cached', return_value=portfolio_file('HDFC Large Cap Fund', [('INE040A01034', 7.0)])):
            result = _holdings_from_link('119018', '2026-09', link, AMC_SOURCES['HDFC Mutual Fund'], 'HDFC Large Cap Fund', debug, {}, {}, HDFC_LARGE)
        self.assertEqual(result['holdings'], {'INE040A01034': 7.0})

    def test_file_for_another_month_or_scheme_is_not_cached(self):
        link = {'url': 'https://files.hdfcfund.com/a.xlsx', 'text': 'a', 'month': '2026-10'}
        source = AMC_SOURCES['HDFC Mutual Fund']
        with patch('fund_analyzer.holdings._download_cached', return_value=portfolio_file('HDFC Large Cap Fund', [('INE040A01034', 7.0)])):
            with self.assertRaisesRegex(ProviderError, 'reports 2026-09-30, not 2026-10'):
                _holdings_from_link('119018', '2026-10', link, source, 'HDFC Large Cap Fund', _overlap_debug_record('119018', HDFC_LARGE), {}, {}, HDFC_LARGE)
        with patch('fund_analyzer.holdings._download_cached', return_value=portfolio_file('HDFC Mid Cap Fund', [('INE040A01034', 7.0)])):
            with self.assertRaisesRegex(ProviderError, 'No sheet matched'):
                _holdings_from_link('119018', '2026-09', {**link, 'month': '2026-09'}, source, 'HDFC Large Cap Fund', _overlap_debug_record('119018', HDFC_LARGE), {}, {}, HDFC_LARGE)
        self.assertEqual(self.month_files(), [])

    def test_invalid_workbook_and_missing_scheme_have_their_own_status(self):
        def links(_source, name):
            return [{'url': f'https://files.hdfcfund.com/{name}.xlsx', 'text': name, 'month': '2026-09'}]

        def download(_key, _month, url):
            return b'<html>not excel</html>' if 'HDFC' in url else portfolio_file('Different Fund', [('INE040A01034', 1.0)])

        result, _, _ = self._analyze([HDFC_LARGE, SBI_LARGE], links={'side_effect': links}, download={'side_effect': download})
        self.assertEqual({fund['scheme_code']: fund['status'] for fund in result['funds']}, {'119018': INVALID_WORKBOOK, '119598': SCHEME_NOT_FOUND})

    def test_renamed_scheme_is_found_through_its_alias(self):
        with patch('fund_analyzer.portfolio_store._read_json', side_effect=lambda path: {'HDFC Mutual Fund': {'HDFC Large Cap Fund': 'HDFC Top 100 Fund'}} if str(path).endswith('scheme_aliases.json') else None):
            self.assertEqual(file_scheme_name(HDFC_LARGE), 'HDFC Top 100 Fund')
            self.assertEqual(file_scheme_name(SBI_LARGE), 'SBI Large Cap Fund')

    def test_staff_upload_is_cached_and_a_download_does_not_replace_it(self):
        with patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[OTHER]):
            import_portfolio_holdings('900', '2026-09-30', 'other.xlsx', portfolio_file('Other Flexi Cap Fund', [('INE040A01034', 6.0)]), 'https://other.test/source.xlsx')
        self.assertEqual(read_snapshot(OTHER, '2026-09')['source_type'], SOURCE_IMPORT)
        store_snapshot(OTHER, '901', '2026-09', holdings_result({'INE090A01021': 1.0}), payload=b'different')
        snapshot = read_snapshot(OTHER, '2026-09')
        self.assertEqual([row['isin'] for row in snapshot['holdings']], ['INE040A01034'])
        self.assertEqual(snapshot['scheme_codes'], ['900', '901'])

    def test_fundamentals_read_the_same_cache(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        self._mark_checked(HDFC_LARGE)
        with (
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=AssertionError('network used')),
        ):
            result = fetch_fund_holdings('102000', HDFC_LARGE_REGULAR)
        self.assertEqual(result['holdings_as_of'], '2026-09-30')
        self.assertEqual(result['riskometer']['level'], 'Very High')
        self.assertEqual(result['benchmark'], 'NIFTY 500 TRI')


class BulkRetrievalTests(PortfolioCacheMixin, SimpleTestCase):
    def _run(self, records, links, download, **options):
        with (
            patch('fund_analyzer.ingestion.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.ingestion._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.ingestion._monthly_files_for_fund', side_effect=links) as listing,
            patch('fund_analyzer.ingestion.source_for_amc', side_effect=lambda amc: {'key': amc, 'url': 'https://x.test', 'parser': 'directory_monthly_excel'} if amc != OTHER['amc'] else None),
            patch('fund_analyzer.holdings._download_cached', side_effect=download) as get,
            patch('fund_analyzer.holdings._cache_parsed_holdings'),
            patch('fund_analyzer.ingestion.time.sleep'),
        ):
            return run_ingestion(pause=0, **options), listing, get

    @staticmethod
    def _links(name):
        return [{'url': f'https://x.test/{name}-{month}.xlsx', 'text': name, 'month': month} for month in ('2026-10', '2026-09', '2026-08')]

    def test_one_download_per_portfolio_and_debt_funds_are_not_eligible(self):
        debt = {**OTHER, 'scheme_code': '5', 'scheme_name': 'Liquid Fund', 'category': 'Debt Scheme - Liquid Fund'}
        self.assertEqual(eligible_portfolios([HDFC_LARGE, HDFC_LARGE_REGULAR, debt]), [HDFC_LARGE])

    def test_stores_newest_months_and_skips_missing_latest(self):
        def download(_key, month, url):
            if month == '2026-10':
                raise ProviderError('404 Client Error: Not Found')
            return portfolio_file('HDFC Large Cap Fund', [('INE040A01034', 7.0)], {'2026-09': 'September 30, 2026', '2026-08': 'August 31, 2026'}[month])

        summary, _, _ = self._run([HDFC_LARGE, HDFC_LARGE_REGULAR], lambda _s, name: self._links(name), download)
        self.assertEqual(summary['results'], {HOLDINGS_AVAILABLE: 1})
        self.assertEqual([path.stem for path in self.month_files()], ['2026-08', '2026-09'])
        self.assertEqual(source_status(HDFC_LARGE), HOLDINGS_AVAILABLE)

    def test_already_cached_month_is_not_downloaded_again(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-10', holdings_result({'INE040A01034': 7.0}, '2026-10-31'))
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 7.0}))
        _, _, get = self._run([HDFC_LARGE], lambda _s, name: self._links(name), AssertionError('downloaded again'))
        get.assert_not_called()

    def test_recently_checked_portfolio_is_skipped_unless_forced(self):
        record_status(HDFC_LARGE, HOLDINGS_AVAILABLE, latest_month='2026-10')
        summary, listing, _ = self._run([HDFC_LARGE], lambda _s, name: [], AssertionError('no'))
        self.assertEqual(summary['due'], 0)
        listing.assert_not_called()
        summary, listing, _ = self._run([HDFC_LARGE], lambda _s, name: [], AssertionError('no'), force=True)
        self.assertEqual(summary['due'], 1)

    def test_blocked_amc_page_is_asked_only_once_per_run(self):
        second = {**HDFC_LARGE, 'scheme_code': '2', 'scheme_name': 'HDFC Mid Cap Fund'}
        summary, listing, _ = self._run([HDFC_LARGE, second], ProviderError('403 Client Error: Forbidden'), AssertionError('no'))
        self.assertEqual(listing.call_count, 1)
        self.assertEqual(summary['results'], {DOWNLOAD_BLOCKED: 2})
        self.assertEqual(summary['amcs_needing_manual_action'], ['HDFC Mutual Fund'])

    def test_temporary_failure_is_retried_a_limited_number_of_times(self):
        calls = []

        def download(_key, month, _url):
            calls.append(month)
            raise ProviderError('Read timed out')

        summary, _, _ = self._run([HDFC_LARGE], lambda _s, name: self._links(name)[:1], download, retries=2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(summary['results'], {DOWNLOAD_FAILED: 1})

    def test_amc_without_a_source_is_reported_not_verified(self):
        summary, listing, _ = self._run([OTHER], lambda _s, name: [], AssertionError('no'))
        listing.assert_not_called()
        self.assertEqual(summary['results'], {SOURCE_NOT_SUPPORTED: 1})
        self.assertEqual(self.month_files(), [])

    def test_older_data_is_preserved_when_the_source_fails(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 7.0}))
        summary, _, _ = self._run([HDFC_LARGE], ProviderError('403 Client Error: Forbidden'), AssertionError('no'))
        self.assertEqual(summary['results'], {HOLDINGS_AVAILABLE: 1})
        self.assertEqual(len(self.month_files()), 1)

    def test_run_stops_when_out_of_time(self):
        second = {**HDFC_LARGE, 'scheme_code': '2', 'scheme_name': 'HDFC Mid Cap Fund'}
        summary, _, _ = self._run([HDFC_LARGE, second], lambda _s, name: [], AssertionError('no'), max_seconds=-1)
        self.assertTrue(summary['timed_out'])
        self.assertEqual(summary['processed'], 0)

    def test_coverage_counts_only_real_cached_files(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        record_status(HDFC_LARGE, HOLDINGS_AVAILABLE, latest_month='2026-09')
        record_status(SBI_LARGE, DOWNLOAD_BLOCKED, 'Listing returned 403')
        report = coverage_report([HDFC_LARGE, HDFC_LARGE_REGULAR, SBI_LARGE, OTHER])
        self.assertEqual(report['total_eligible_schemes'], 3)
        self.assertEqual(report['verified_schemes'], 1)
        by_amc = {entry['amc']: entry for entry in report['amcs']}
        self.assertEqual(by_amc['HDFC Mutual Fund']['latest_month'], '2026-09')
        self.assertEqual(by_amc['Other Mutual Fund']['statuses'], {'NOT_CHECKED': 1})
        self.assertEqual([item['scheme_name'] for item in report['funds_requiring_action']], ['SBI Large Cap Fund'])


class PublicEndpointTests(PortfolioCacheMixin, SimpleTestCase):
    def test_public_overlap_has_no_debug_or_file_locations_and_uses_no_database(self):
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}), source_url='https://files.hdfcfund.com/secret.xlsx')
        store_snapshot(SBI_LARGE, '119598', '2026-09', holdings_result({'INE040A01034': 3.0}))
        record_status(HDFC_LARGE, HOLDINGS_AVAILABLE, latest_month='2026-10')
        record_status(SBI_LARGE, HOLDINGS_AVAILABLE, latest_month='2026-10')
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[HDFC_LARGE, SBI_LARGE]),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
        ):
            response = APIClient().post('/api/overlap', {'scheme_codes': ['119018', '119598']}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('debug', response.data)
        self.assertNotIn('hdfcfund', str(response.data))
        self.assertNotIn(self.cache_dir, str(response.data))
        self.assertEqual(response.data['highest_overlap']['percentage'], 3.0)

    def test_expensive_endpoints_are_rate_limited(self):
        with (
            patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'fund_analysis': '1/min'}),
            patch('fund_analyzer.views.compare_holdings_overlap', return_value={'available': False}),
        ):
            first = APIClient().post('/api/overlap', {'scheme_codes': ['1', '2']}, format='json')
            second = APIClient().post('/api/overlap', {'scheme_codes': ['1', '2']}, format='json')
        self.assertEqual((first.status_code, second.status_code), (200, 429))


class StaffEndpointTests(PortfolioCacheMixin, TestCase):
    """Only staff sign-in touches the database, through the existing accounts app."""

    def _client(self, **fields):
        model = get_user_model()
        client = APIClient()
        client.force_authenticate(model.objects.create(**{model.USERNAME_FIELD: 'person@example.test', **fields}))
        return client

    def test_coverage_is_for_administrators_only(self):
        self.assertEqual(APIClient().get('/api/overlap/coverage').status_code, 401)
        self.assertEqual(self._client().get('/api/overlap/coverage').status_code, 403)

    def test_coverage_report_for_staff(self):
        with patch('fund_analyzer.views.fetch_amfi_latest', return_value=[HDFC_LARGE, SBI_LARGE, OTHER]):
            response = self._client(is_staff=True).get('/api/overlap/coverage')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total_eligible_schemes'], 3)

    def test_import_is_for_administrators_and_rejects_non_web_source(self):
        upload = BytesIO(portfolio_file('Other Flexi Cap Fund', [('INE040A01034', 6.0)]))
        upload.name = 'other.xlsx'
        self.assertEqual(APIClient().post('/api/overlap/import', {'file': upload}, format='multipart').status_code, 401)
        upload.seek(0)
        response = self._client(is_staff=True).post('/api/overlap/import', {'file': upload, 'scheme_code': '900', 'portfolio_date': '2026-09-30', 'source_url': 'file:///etc/passwd'}, format='multipart')
        self.assertEqual(response.status_code, 400)
