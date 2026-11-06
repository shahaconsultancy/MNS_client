from datetime import date, datetime
from io import BytesIO
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlImage
import pandas as pd
from PIL import Image, ImageDraw
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from .analytics import cagr, max_drawdown, sharpe, volatility
from .holdings import AMC_SOURCES, _asset_allocation, _benchmark_from_frame, _clean_holding_name, _common_holdings, _monthly_files_for_fund, _sector_allocation, _read_equity_holdings, analyze_holdings_overlap, calculate_overlap, fetch_fund_holdings, import_portfolio_holdings
from .adapters import is_official_url
from .providers import ProviderError, _aum_quarters, fetch_amfi_aum, fetch_amfi_ter_snapshot
from .riskometer import RISK_LEVELS, read_risk_level, read_scheme_riskometer
from .services import compare_funds
from .tests_pipeline import PortfolioCacheMixin


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.nav = [
            {'date': date(2021, 1, 1).isoformat(), 'nav': 110},
            {'date': date(2022, 1, 1).isoformat(), 'nav': 100},
            {'date': date(2023, 1, 1).isoformat(), 'nav': 120},
            {'date': date(2024, 1, 1).isoformat(), 'nav': 90},
            {'date': date(2025, 1, 1).isoformat(), 'nav': 150},
            {'date': date(2026, 1, 1).isoformat(), 'nav': 180},
        ]

    def test_cagr(self):
        self.assertAlmostEqual(cagr(self.nav, 5), ((180 / 110) ** 0.2) - 1, places=8)

    def test_volatility_is_annualised(self):
        self.assertIsNotNone(volatility(self.nav, years=3))

    def test_sharpe_uses_configured_risk_free_rate(self):
        self.assertAlmostEqual(sharpe(self.nav, 0), cagr(self.nav, 3) / volatility(self.nav, 3), places=8)

    def test_drawdown(self):
        self.assertAlmostEqual(max_drawdown(self.nav, years=3), 90 / 120 - 1, places=8)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'ter-tests'}})
class AmfiTerTests(SimpleTestCase):
    HEADERS = ['NSDL Scheme Code', 'Scheme Name', 'Scheme Type', 'Scheme Category', 'TER Date',
               'Regular Plan - Base Expense Ratio (BER) (%)', 'Regular Plan - Total TER (%)',
               'Direct Plan - Base Expense Ratio (BER) (%)', 'Direct Plan - Total TER (%)']
    DIRECT = {'scheme_code': '1', 'scheme_name': 'Sample Flexi Cap Fund', 'amc': 'Sample Mutual Fund', 'plan': 'Direct Plan', 'option': 'Growth'}
    REGULAR = {'scheme_code': '2', 'scheme_name': 'Sample Flexi Cap Fund', 'amc': 'Sample Mutual Fund', 'plan': 'Regular Plan', 'option': 'Growth'}

    def setUp(self):
        cache.clear()

    @classmethod
    def _workbook(cls, rows):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(cls.HEADERS)
        for row in rows:
            sheet.append(row)
        payload = BytesIO()
        workbook.save(payload)
        return payload.getvalue()

    @staticmethod
    def _row(day, regular, direct, name='Sample Flexi Cap Fund', code='SMPL/O/E/FCF/1'):
        return [code, name, 'Open Ended', 'Equity Scheme', datetime(2026, 11, day), 9.9, regular, 8.8, direct]

    def _snapshot(self, tables, records=None):
        """tables maps AMFI's MM-YYYY month to workbook rows; a missing month has no rows."""
        def fetch(_url, *, params=None, **_kwargs):
            return SimpleNamespace(content=self._workbook(tables.get(params['Month'], [])))

        with (
            patch('fund_analyzer.providers._ter_amc_ids', return_value={'Sample Mutual Fund': '7'}),
            patch('fund_analyzer.providers._ter_months', return_value=['11-2026', '10-2026']),
            patch('fund_analyzer.providers.fetch_url', side_effect=fetch),
        ):
            return fetch_amfi_ter_snapshot(['1', '2'], records or [self.DIRECT, self.REGULAR])

    def test_each_plan_gets_its_own_total_ter_from_the_latest_date(self):
        result = self._snapshot({'11-2026': [self._row(1, 1.5, 0.5), self._row(4, 1.25, 0.75), self._row(2, 1.1, 0.1)]})
        self.assertEqual(result['1'], (0.75, '2026-11-04'))
        self.assertEqual(result['2'], (1.25, '2026-11-04'))

    def test_scheme_missing_from_newest_month_uses_older_month_and_keeps_its_date(self):
        rows = [[*self._row(30, 1.5, 0.5)[:4], datetime(2026, 10, 31), 9.9, 1.5, 8.8, 0.5]]
        self.assertEqual(self._snapshot({'10-2026': rows})['1'], (0.5, '2026-10-31'))

    def test_blank_or_zero_total_is_unavailable_not_zero_percent(self):
        for value in (None, '', 0, '0.0000', '-'):
            cache.clear()
            result = self._snapshot({'11-2026': [self._row(1, 1.5, value)]})
            self.assertEqual(result['1'], (None, None))
            self.assertEqual(result['2'], (1.5, '2026-11-01'))

    def test_funds_of_one_amc_share_a_single_download(self):
        requests_made = []

        def fetch(_url, *, params=None, **_kwargs):
            requests_made.append(params['Month'])
            return SimpleNamespace(content=self._workbook([self._row(1, 1.5, 0.5)]))

        with (
            patch('fund_analyzer.providers._ter_amc_ids', return_value={'Sample Mutual Fund': '7'}),
            patch('fund_analyzer.providers._ter_months', return_value=['11-2026', '10-2026']),
            patch('fund_analyzer.providers.fetch_url', side_effect=fetch),
        ):
            result = fetch_amfi_ter_snapshot(['1', '2'], [self.DIRECT, self.REGULAR])
        self.assertEqual(requests_made, ['11-2026'])
        self.assertEqual(result, {'1': (0.5, '2026-11-01'), '2': (1.5, '2026-11-01')})

    def test_throttled_download_is_retried_once(self):
        answers = [ProviderError('429 Client Error'), SimpleNamespace(content=self._workbook([self._row(1, 1.5, 0.5)]))]
        with (
            patch('fund_analyzer.providers._ter_amc_ids', return_value={'Sample Mutual Fund': '7'}),
            patch('fund_analyzer.providers._ter_months', return_value=['11-2026']),
            patch('fund_analyzer.providers.fetch_url', side_effect=answers),
            patch('fund_analyzer.providers.sleep') as wait,
        ):
            result = fetch_amfi_ter_snapshot(['1'], [self.DIRECT])
        self.assertEqual(result['1'], (0.5, '2026-11-01'))
        wait.assert_called_once()

    def test_short_row_and_footnote_do_not_break_the_workbook(self):
        short_row = self._row(1, 1.5, 0.5)[:7]
        footnote = ['For months May to March, the reference period is April 1.']
        result = self._snapshot({'11-2026': [short_row, footnote]})
        self.assertEqual(result['1'], (None, None))
        self.assertEqual(result['2'], (1.5, '2026-11-01'))

    def test_other_fund_name_is_not_matched(self):
        result = self._snapshot({'11-2026': [self._row(1, 1.5, 0.5, name='Sample Flexi Cap Fund II')]})
        self.assertEqual(result['1'], (None, None))

    def test_record_without_a_plan_is_unavailable(self):
        record = {**self.DIRECT, 'plan': None}
        result = self._snapshot({'11-2026': [self._row(1, 1.5, 0.5)]}, [record, self.REGULAR])
        self.assertEqual(result['1'], (None, None))

    def test_two_schemes_with_one_name_are_not_guessed(self):
        rows = [self._row(1, 1.5, 0.5), self._row(1, 1.6, 0.6, code='SMPL/O/E/FCF/2')]
        self.assertEqual(self._snapshot({'11-2026': rows})['1'], (None, None))

    def test_amfi_failure_is_unavailable_and_serves_stale_table_when_cached(self):
        self.assertEqual(self._snapshot({'11-2026': [self._row(1, 1.5, 0.5)]})['1'], (0.5, '2026-11-01'))
        cache.delete('fund-analyzer:amfi:ter:7:11-2026')
        with (
            patch('fund_analyzer.providers._ter_amc_ids', return_value={'Sample Mutual Fund': '7'}),
            patch('fund_analyzer.providers._ter_months', return_value=['11-2026']),
            patch('fund_analyzer.providers.fetch_url', side_effect=ProviderError('429')),
            patch('fund_analyzer.providers.sleep'),
        ):
            self.assertEqual(fetch_amfi_ter_snapshot(['1'], [self.DIRECT])['1'], (0.5, '2026-11-01'))
            cache.clear()
            self.assertEqual(fetch_amfi_ter_snapshot(['1'], [self.DIRECT])['1'], (None, None))

    def test_compare_returns_ter_and_survives_a_ter_failure(self):
        fund = {'scheme_code': '1', 'source': 'AMFI', 'history': [], 'metrics': None, 'asOfDate': None}
        with (
            patch('fund_analyzer.services.fetch_amfi_latest', return_value=[self.DIRECT]),
            patch('fund_analyzer.services.fetch_fund_data', side_effect=lambda *_args, **_kwargs: dict(fund)),
            patch('fund_analyzer.services.fetch_amfi_ter_snapshot', return_value={'1': (0.75, '2026-11-04')}),
        ):
            self.assertEqual(compare_funds(['1'])['funds'][0]['ter'], 0.75)
        with (
            patch('fund_analyzer.services.fetch_amfi_latest', return_value=[self.DIRECT]),
            patch('fund_analyzer.services.fetch_fund_data', side_effect=lambda *_args, **_kwargs: dict(fund)),
            patch('fund_analyzer.services.fetch_amfi_ter_snapshot', side_effect=RuntimeError('boom')),
        ):
            result = compare_funds(['1'])['funds'][0]
        self.assertIsNone(result['ter'])
        self.assertIsNone(result['ter_as_of'])


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'aum-tests'}})
class AmfiAumTests(SimpleTestCase):
    REPORT = '''<table><tr><td>Sample Mutual Fund</td></tr><tr><td>Equity</td></tr>
        <tr><td>Sample Flexi Fund - Growth Option - Direct Plan</td><td>1,000.50</td><td>0</td></tr>
        <tr><td>Sample Flexi Fund - Growth Plan</td><td>2000</td><td>0.50</td></tr>
        <tr><td>Sample Flexi Fund-Direct Plan-IDCW</td><td>100</td><td>0</td></tr>
        <tr><td>Sample Flexi Fund II - Direct Plan</td><td>9999</td><td>0</td></tr>
        <tr><td>Sample &amp; Co Fund - Direct Plan</td><td>50</td><td>0</td></tr></table>'''

    def setUp(self):
        cache.clear()

    def _aum(self, name='Sample Flexi Fund', amc='Sample Mutual Fund', reports=None):
        pages = reports if reports is not None else [self.REPORT]

        def fetch(_url, *, params=None, **_kwargs):
            self.params.append(params)
            return SimpleNamespace(text=pages.pop(0))

        self.params = []
        quarters = [(date(2026, 7, 1), date(2026, 9, 30)), (date(2026, 4, 1), date(2026, 6, 30))]
        with (
            patch('fund_analyzer.providers._ter_amc_ids', return_value={'Sample Mutual Fund': '7'}),
            patch('fund_analyzer.providers._aum_quarters', return_value=quarters),
            patch('fund_analyzer.providers.fetch_url', side_effect=fetch),
        ):
            return fetch_amfi_aum(name, amc)

    def test_scheme_aum_adds_every_plan_and_requests_one_amc(self):
        self.assertEqual(self._aum(), (3101.0, '2026-09-30'))
        self.assertEqual(self.params[0]['mf'], '7')
        self.assertEqual(self.params[0]['qt'], 'July - September 2026')

    def test_similar_scheme_name_is_not_added(self):
        self.assertEqual(self._aum(name='Sample Flexi Fund II'), (9999.0, '2026-09-30'))
        cache.clear()
        self.assertEqual(self._aum(name='Sample Flexi'), (None, None))

    def test_plan_names_without_a_hyphen_are_added(self):
        report = '''<table><tr><td>Sample Mutual Fund</td></tr>
            <tr><td>Sample Large Cap Fund Direct Plan Growth</td><td>10</td><td>0</td></tr>
            <tr><td>SAMPLE LARGE CAP FUND REGULAR GROWTH</td><td>20</td><td>0</td></tr>
            <tr><td>Sample Large Cap Fund (Direct) - IDCW</td><td>30</td><td>0</td></tr>
            <tr><td>Sample Large Cap Fund (formerly Old Fund) - Regular Plan</td><td>40</td><td>0</td></tr>
            <tr><td>Sample Large Cap Fund Series 2 - Direct Plan</td><td>999</td><td>0</td></tr></table>'''
        self.assertEqual(self._aum(name='Sample Large Cap Fund', reports=[report]), (100.0, '2026-09-30'))

    def test_report_for_another_amc_is_rejected(self):
        report = self.REPORT.replace('Sample Mutual Fund', 'Other Mutual Fund')
        self.assertEqual(self._aum(reports=[report, report]), (None, None))

    def test_empty_newest_quarter_uses_previous_quarter_date(self):
        self.assertEqual(self._aum(reports=['<table></table>', self.REPORT]), (3101.0, '2026-06-30'))

    def test_unknown_amc_or_failure_is_unavailable(self):
        self.assertEqual(self._aum(amc='Unknown Mutual Fund'), (None, None))
        with (
            patch('fund_analyzer.providers._ter_amc_ids', return_value={'Sample Mutual Fund': '7'}),
            patch('fund_analyzer.providers.fetch_url', side_effect=ProviderError('timed out')),
        ):
            self.assertEqual(fetch_amfi_aum('Sample Flexi Fund', 'Sample Mutual Fund'), (None, None))

    def test_quarters_are_completed_and_cross_the_year(self):
        with patch('fund_analyzer.providers.date', wraps=date) as fake_date:
            fake_date.today.return_value = date(2026, 2, 10)
            self.assertEqual(_aum_quarters(), [(date(2025, 10, 1), date(2025, 12, 31)), (date(2025, 7, 1), date(2025, 9, 30))])


class FundHoldingsTests(PortfolioCacheMixin, SimpleTestCase):
    RECORD = {'scheme_code': '1', 'scheme_name': 'Sample Fund', 'amc': 'HDFC Mutual Fund', 'category': 'Equity Scheme - Flexi Cap Fund'}

    def _holdings(self, record, loader):
        links = [{'url': f'https://example.test/{month}.xlsx', 'text': 'Sample', 'month': month} for month in ('2026-09', '2026-10')]
        with (
            patch('fund_analyzer.holdings._imported_links', return_value=[]),
            patch('fund_analyzer.holdings._parsed_links', return_value=[]),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=links),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value={'2026-10', '2026-09'}),
            patch('fund_analyzer.holdings._holdings_from_link', side_effect=loader),
        ):
            return fetch_fund_holdings('1', record)

    def test_falls_back_to_previous_month_and_sorts_largest_first(self):
        def loader(_code, month, *_args):
            if month == '2026-10':
                raise ProviderError('October file not published')
            return {
                'holdings': {'INE002A01018': 3.456, 'INE040A01034': 7.0, 'US0231351067': 2.0},
                'names': {'INE040A01034': 'HDFC Bank'},
                'industries': {'INE040A01034': 'Banks', 'INE002A01018': 'Petroleum Products'},
                'benchmark': 'NIFTY 500 TRI',
                'risk_level': 'Moderately High',
                'portfolio_date': '2026-09-30',
            }

        self.assertEqual(self._holdings(self.RECORD, loader), {
            'holdings': [
                {'isin': 'INE040A01034', 'name': 'HDFC Bank', 'weight': 7.0},
                {'isin': 'INE002A01018', 'name': 'INE002A01018', 'weight': 3.46},
                {'isin': 'US0231351067', 'name': 'US0231351067', 'weight': 2.0},
            ],
            'holdings_as_of': '2026-09-30',
            'holdings_source': 'official_amc',
            'benchmark': 'NIFTY 500 TRI',
            'allocation': {'indian_equity': 10.46, 'international_equity': 2.0, 'other': 87.54},
            'sectors': [
                {'name': 'Banks', 'weight': 7.0},
                {'name': 'Petroleum Products', 'weight': 3.46},
                {'name': 'Others', 'weight': 2.0},
            ],
            'asset_class': 'equity',
            'riskometer': {'level': 'Moderately High', 'as_of': '2026-09-30', 'source': 'Official AMC Disclosure'},
        })

    def test_sectors_group_the_rest_as_others_and_need_industry_data(self):
        weights = {f'INE00000000{index}': float(10 - index) for index in range(8)}
        industries = {isin: f'Sector {index}' for index, isin in enumerate(weights)}
        rows = _sector_allocation(weights, industries)
        self.assertEqual([row['name'] for row in rows], ['Sector 0', 'Sector 1', 'Sector 2', 'Sector 3', 'Sector 4', 'Sector 5', 'Others'])
        self.assertEqual(rows[-1]['weight'], 7.0)
        self.assertEqual(sum(row['weight'] for row in rows), sum(weights.values()))
        self.assertEqual(_sector_allocation(weights, {}), [])

    def test_allocation_never_reports_negative_other(self):
        self.assertEqual(_asset_allocation({'INE040A01034': 100.4})['other'], 0.0)

    def test_parser_reads_industry_and_benchmark_without_leaking_them_into_debug(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Sample Fund'
        for row in (
            ['Scheme', 'Sample Fund'],
            ['Monthly Portfolio Statement as on September 30, 2026'],
            ['Name of the Instrument', 'ISIN', 'Industry / Rating', '% to NAV'],
            ['EQUITY & EQUITY RELATED'],
            ['Equity Shares'],
            ['HDFC Bank Ltd.', 'INE040A01034', 'Banks', 7.0],
            ['Note'],
            ['Benchmark Name : BSE 500 TRI'],
        ):
            sheet.append(row)
        payload = BytesIO()
        workbook.save(payload)
        diagnostics = {}
        _read_equity_holdings(payload.getvalue(), 'https://example.test/sample.xlsx', 'Sample Fund', 'sbi_monthly_excel', diagnostics)
        self.assertEqual(diagnostics['holding_industries'], {'INE040A01034': 'Banks'})
        self.assertEqual(diagnostics['benchmark'], 'BSE 500 TRI')

    def test_benchmark_forms_used_by_amc_files(self):
        import pandas as pd
        for text, expected in (
            ('9) Riskometer based on Scheme Portfolio and Portfolio Benchmark "NIFTY 500 TRI" as on Sep 30, 2026', 'NIFTY 500 TRI'),
            ('BENCHMARK NAME - NIFTY SMALLCAP 250 TRI', 'NIFTY SMALLCAP 250 TRI'),
            ('Standard Deviation( Benchmark )', None),
        ):
            self.assertEqual(_benchmark_from_frame(pd.DataFrame([[text]])), expected)

    def test_no_valid_month_returns_nothing(self):
        def loader(*_args):
            raise ProviderError('Bad file')

        empty = {'holdings': [], 'holdings_as_of': None, 'benchmark': None, 'allocation': None, 'sectors': [], 'riskometer': None, 'asset_class': 'equity'}
        self.assertEqual(self._holdings(self.RECORD, loader), empty)
        self.assertEqual(fetch_fund_holdings('1', None), empty)

    def test_fundamentals_endpoint_returns_holdings_and_survives_their_failure(self):
        record = {**self.RECORD, 'nav': 10.0, 'date': '04-Nov-2026'}
        with (
            patch('fund_analyzer.services.fetch_amfi_latest', return_value=[record]),
            patch('fund_analyzer.services.fetch_amfi_ter_snapshot', return_value={'1': (0.75, '2026-10-31')}),
            patch('fund_analyzer.services.fetch_amfi_aum', return_value=(3101.0, '2026-09-30')),
            patch('fund_analyzer.services.fetch_fund_holdings', return_value={
                'holdings': [{'isin': 'INE040A01034', 'name': 'HDFC Bank', 'weight': 7.0}], 'holdings_as_of': '2026-09-30',
                'benchmark': 'NIFTY 500 TRI', 'allocation': {'indian_equity': 7.0, 'international_equity': 0.0, 'other': 93.0},
                'sectors': [{'name': 'Banks', 'weight': 7.0}],
                'riskometer': {'level': 'Very High', 'as_of': '2026-09-30', 'source': 'Official AMC Disclosure'},
            }),
        ):
            fund = APIClient().post('/api/fundamentals', {'scheme_codes': ['1']}, format='json').data['funds'][0]
        self.assertEqual(fund['riskometer'], {'level': 'Very High', 'as_of': '2026-09-30', 'source': 'Official AMC Disclosure'})
        self.assertEqual(fund['holdings'][0]['name'], 'HDFC Bank')
        self.assertEqual(fund['benchmark'], 'NIFTY 500 TRI')
        self.assertEqual(fund['allocation']['other'], 93.0)
        self.assertEqual(fund['sectors'][0]['weight'], 7.0)
        self.assertEqual(fund['holdings_as_of'], '2026-09-30')
        self.assertEqual((fund['ter'], fund['aum'], fund['aum_as_of']), (0.75, 3101.0, '2026-09-30'))
        with (
            patch('fund_analyzer.services.fetch_amfi_latest', return_value=[record]),
            patch('fund_analyzer.services.fetch_amfi_ter_snapshot', return_value={}),
            patch('fund_analyzer.services.fetch_amfi_aum', return_value=(None, None)),
            patch('fund_analyzer.services.fetch_fund_holdings', side_effect=RuntimeError('boom')),
        ):
            response = APIClient().post('/api/fundamentals', {'scheme_codes': ['1']}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['funds'][0]['holdings'], [])
        self.assertIsNone(response.data['funds'][0]['riskometer'])


class RiskometerTests(unittest.TestCase):
    COLOURS = ((20, 160, 80), (150, 205, 60), (245, 235, 50), (195, 140, 40), (250, 140, 30), (235, 30, 40))

    @classmethod
    def _dial(cls, needle_degrees, colours=None):
        """Draw a riskometer with the needle at this angle; 180 is far left, 0 is far right."""
        colours = colours or cls.COLOURS
        image = Image.new('RGB', (300, 220), 'white')
        draw = ImageDraw.Draw(image)
        box = (30, 20, 270, 260)
        step = 180 / len(colours)
        for index, colour in enumerate(colours):
            draw.pieslice(box, 180 + index * step, 180 + (index + 1) * step, fill=colour)
        draw.ellipse((75, 65, 225, 215), fill='white')
        centre = (150, 140)
        tip = (centre[0] + 72 * math.cos(math.radians(needle_degrees)), centre[1] - 72 * math.sin(math.radians(needle_degrees)))
        draw.line((centre, tip), fill=(30, 30, 30), width=5)
        draw.rectangle((20, 140, 280, 165), fill='black')
        payload = BytesIO()
        image.save(payload, 'PNG')
        return payload.getvalue()

    def test_every_official_level_is_read_from_the_needle(self):
        for index, level in enumerate(RISK_LEVELS):
            self.assertEqual(read_risk_level(self._dial(180 - 15 - index * 30)), level)

    def test_dial_stretched_into_an_oval_is_still_read(self):
        stretched = BytesIO()
        Image.open(BytesIO(self._dial(165))).resize((450, 220)).save(stretched, 'PNG')
        self.assertEqual(read_risk_level(stretched.getvalue()), 'Low')

    def test_needle_on_a_segment_edge_is_not_guessed(self):
        self.assertIsNone(read_risk_level(self._dial(90)))

    def test_unclear_pictures_are_not_guessed(self):
        blank = BytesIO()
        Image.new('RGB', (300, 220), 'white').save(blank, 'PNG')
        for picture in (b'', b'not an image', blank.getvalue(), self._dial(45, self.COLOURS[:5])):
            self.assertIsNone(read_risk_level(picture))

    def _workbook(self, rows, pictures):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Sample'
        for row in rows:
            sheet.append(row)
        for cell, data in pictures.items():
            sheet.add_image(XlImage(BytesIO(data)), cell)
        payload = BytesIO()
        workbook.save(payload)
        return payload.getvalue(), pd.DataFrame(rows)

    def test_scheme_picture_is_read_and_not_the_benchmark(self):
        rows = [['x']] * 2 + [['Scheme Riskometer:']] + [['x']] * 10 + [['Benchmark Riskometer:']]
        for scheme_angle, level in ((165, 'Low'), (15, 'Very High')):
            payload, frame = self._workbook(rows, {'A4': self._dial(scheme_angle), 'A15': self._dial(105)})
            self.assertEqual(read_scheme_riskometer(payload, 'Sample', frame), level)

    def test_side_by_side_labels_choose_the_scheme_picture(self):
        rows = [['x', 'Scheme Riskometer', 'x', 'x', 'Benchmark Riskometer']]
        payload, frame = self._workbook(rows, {'B2': self._dial(105), 'E2': self._dial(15)})
        self.assertEqual(read_scheme_riskometer(payload, 'Sample', frame), 'Moderate')

    def test_missing_label_picture_or_zip_is_unavailable(self):
        no_label, frame = self._workbook([['x']], {'A2': self._dial(15)})
        self.assertIsNone(read_scheme_riskometer(no_label, 'Sample', frame))
        no_picture, frame = self._workbook([['Scheme Riskometer:']], {})
        self.assertIsNone(read_scheme_riskometer(no_picture, 'Sample', frame))
        self.assertIsNone(read_scheme_riskometer(b'old binary xls', 'Sample', frame))

    def test_ambiguous_picture_position_is_unavailable(self):
        rows = [['x', 'x', 'Scheme Riskometer', 'x', 'x']]
        payload, frame = self._workbook(rows, {'B2': self._dial(15), 'D2': self._dial(165)})
        self.assertIsNone(read_scheme_riskometer(payload, 'Sample', frame))

    def test_riskometer_failure_does_not_stop_the_holdings(self):
        file = HoldingsOverlapTests._portfolio_file('Sample Fund', [('INE040A01034', 5.0)])
        diagnostics = {}
        with patch('fund_analyzer.holdings.read_scheme_riskometer', side_effect=RuntimeError('boom')):
            holdings = _read_equity_holdings(file, 'https://example.test/sample.xlsx', 'Sample Fund', 'sbi_monthly_excel', diagnostics)
        self.assertEqual(holdings, {'INE040A01034': 5.0})
        self.assertIsNone(diagnostics['risk_level'])


class HoldingsOverlapTests(PortfolioCacheMixin, SimpleTestCase):
    @staticmethod
    def _portfolio_file(fund_name, rows, reporting_date='2026-09-30'):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = fund_name[:31]
        sheet.append(['Scheme', fund_name])
        sheet.append([f"Monthly Portfolio Statement as on {date.fromisoformat(reporting_date).strftime('%B %d, %Y')}"])
        sheet.append(['Instrument', 'ISIN', '% to NAV'])
        sheet.append(['EQUITY & EQUITY RELATED'])
        sheet.append(['Equity Shares'])
        for isin, weight in rows:
            sheet.append(['Stock', isin, weight])
        payload = BytesIO()
        workbook.save(payload)
        return payload.getvalue()

    def test_equity_parser_aggregates_duplicate_isins_and_excludes_debt(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'SBI Contra Fund'
        sheet.append(['Scheme', 'SBI Contra Fund'])
        sheet.append(['Instrument', 'ISIN', 'Market value', '% to AUM'])
        sheet.append(['EQUITY & EQUITY RELATED'])
        sheet.append(['Equity Shares'])
        sheet.append(['HDFC Bank', 'INE040A01034', 100, 4.79])
        sheet.append(['HDFC Bank', 'INE040A01034', 20, 1.21])
        sheet.append(['(b) Units issued by ReIT'])
        sheet.append(['REIT unit', 'INE0REI01000', 20, 2.29])
        sheet.append(['DEBT INSTRUMENTS'])
        sheet.append(['Bond', 'INE001A01000', 100, 20])
        payload = BytesIO()
        workbook.save(payload)

        holdings = _read_equity_holdings(payload.getvalue(), 'contra.xlsx', 'SBI Contra Fund', 'sbi_monthly_excel')
        self.assertEqual(holdings, {'INE040A01034': 6.0})

    def test_hdfc_public_excel_host_is_used_when_listing_is_forbidden(self):
        with (
            patch('fund_analyzer.holdings._load_page_links', side_effect=ProviderError('403 Forbidden')),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value={'2026-10', '2026-09'}),
        ):
            links = _monthly_files_for_fund(AMC_SOURCES['HDFC Mutual Fund'], 'HDFC Flexi Cap Fund - Direct Plan - Growth Option')
        self.assertEqual([link['month'] for link in links], ['2026-10', '2026-09'])
        self.assertEqual(links[1]['url'], 'https://files.hdfcfund.com/s3fs-public/2026-10/Monthly%20HDFC%20Flexi%20Cap%20Fund%20-%2030%20September%202026.xlsx')
        self.assertEqual(links[1]['listing_error'], '403 Forbidden')

    def test_any_hdfc_scheme_gets_a_limited_set_of_candidates_when_listing_is_forbidden(self):
        months = {'2026-10', '2026-09', '2026-08'}
        with (
            patch('fund_analyzer.holdings._load_page_links', side_effect=ProviderError('403 Forbidden')),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=months),
        ):
            links = _monthly_files_for_fund(AMC_SOURCES['HDFC Mutual Fund'], 'HDFC Large Cap Fund')
            shouting = _monthly_files_for_fund(AMC_SOURCES['HDFC Mutual Fund'], 'HDFC DIVIDEND YIELD FUND')
        self.assertEqual(len(links), 3)
        self.assertTrue(all(link['guessed'] for link in links))
        self.assertIn('Monthly%20HDFC%20Large%20Cap%20Fund%20-%2030%20September%202026.xlsx', links[1]['url'])
        self.assertEqual(len(shouting), 6)
        self.assertIn('HDFC%20Dividend%20Yield%20Fund', shouting[1]['url'])
        self.assertTrue(all(is_official_url(link['url'], ['hdfcfund.com']) for link in links))

    def test_hdfc_file_that_does_not_name_the_scheme_is_rejected(self):
        payload = self._portfolio_file('HDFC Mid Cap Fund', [('INE040A01034', 5.0)])
        with self.assertRaisesRegex(ProviderError, 'No sheet matched'):
            _read_equity_holdings(payload, 'large.xlsx', 'HDFC Large Cap Fund', 'hdfc_xlsx_by_scheme')

    def test_csv_disclosure_excludes_non_equity_rows(self):
        payload = (
            'SBI Contra Fund,,\nInstrument,ISIN,% to NAV\nEQUITY & EQUITY RELATED,,\n'
            'HDFC Bank,INE040A01034,5\nDEBT INSTRUMENTS,,\nBond,INE001A01000,20\n'
        ).encode('utf-8')
        holdings = _read_equity_holdings(payload, 'contra.csv', 'SBI Contra Fund', 'import_by_code')
        self.assertEqual(holdings, {'INE040A01034': 5.0})

    def test_import_rejects_workbook_for_another_fund(self):
        payload = self._portfolio_file('Different Equity Fund', [('INE040A01034', 5.0)])
        with self.assertRaisesRegex(ProviderError, 'No sheet matched fund'):
            _read_equity_holdings(payload, 'wrong.xlsx', 'SBI Contra Fund', 'import_by_code')

    def test_ppfas_prefers_its_fund_file_over_consolidated_file(self):
        links = [
            {'url': 'https://amc.ppfas.com/PPFAS_Monthly_Portfolio_Report_September_30_2026.xls', 'text': 'Consolidated', 'month': '2026-09'},
            {'url': 'https://amc.ppfas.com/PPTSF_PPFAS_Monthly_Portfolio_Report_September_30_2026.xlsx', 'text': 'PPTSF', 'month': '2026-09'},
        ]
        with patch('fund_analyzer.holdings._load_page_links', return_value=links):
            selected = _monthly_files_for_fund(AMC_SOURCES['PPFAS Mutual Fund'], 'Parag Parikh ELSS Tax Saver Fund')
        self.assertEqual(selected, [links[1]])

    def test_xlsx_content_with_xls_filename_is_parsed(self):
        payload = self._portfolio_file('SBI Contra Fund', [('INE040A01034', 4.79)])
        holdings = _read_equity_holdings(payload, 'contra.xls', 'SBI Contra Fund', 'sbi_monthly_excel')
        self.assertEqual(holdings, {'INE040A01034': 4.79})

    def test_import_rejects_date_that_differs_from_workbook(self):
        record = {'scheme_code': '119835', 'scheme_name': 'SBI Contra Fund', 'amc': 'SBI Mutual Fund', 'category': 'Equity Scheme - Contra Fund'}
        payload = self._portfolio_file('SBI Contra Fund', [('INE040A01034', 4.79)])
        with patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[record]):
            with self.assertRaisesRegex(ProviderError, 'file reports 2026-09-30'):
                import_portfolio_holdings('119835', '2026-10-31', 'sbi.xlsx', payload)

    def test_failed_newer_file_falls_back_to_latest_valid_common_month(self):
        records = [
            {'scheme_code': '119835', 'scheme_name': 'SBI Contra Fund', 'amc': 'SBI Mutual Fund', 'category': 'Equity Scheme - Contra Fund'},
            {'scheme_code': '118955', 'scheme_name': 'HDFC Flexi Cap Fund', 'amc': 'HDFC Mutual Fund', 'category': 'Equity Scheme - Flexi Cap Fund'},
        ]
        files = {}
        for record in records:
            for month, reporting_date in [('2026-10', '2026-10-31'), ('2026-09', '2026-09-30')]:
                files[(record['scheme_name'], month)] = self._portfolio_file(
                    record['scheme_name'], [('INE040A01034', 5.0 if record['scheme_code'] == '119835' else 7.0)], reporting_date,
                )

        def links(_source, fund_name):
            return [
                {'url': f'https://example.test/{fund_name}/{month}.xlsx', 'text': fund_name, 'month': month}
                for month in ('2026-10', '2026-09')
            ]

        def download(_amc, month, url):
            fund_name = url.split('/')[-2]
            if fund_name == 'HDFC Flexi Cap Fund' and month == '2026-10':
                raise ProviderError('October file timed out')
            return files[(fund_name, month)]

        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value={'2026-10', '2026-09'}),
            patch('fund_analyzer.holdings._imported_links', return_value=[]),
            patch('fund_analyzer.holdings._parsed_links', return_value=[]),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=links),
            patch('fund_analyzer.holdings._download_cached', side_effect=download),
            patch('fund_analyzer.holdings._cache_parsed_holdings'),
        ):
            result = analyze_holdings_overlap(['119835', '118955'])
        self.assertEqual(result['portfolio_month'], '2026-09')
        self.assertEqual(result['highest_overlap']['percentage'], 5.0)
        self.assertEqual(result['highest_overlap']['portfolio_date'], '2026-09-30')
        self.assertIn('October file timed out', result['debug'][1]['errors_by_month']['2026-10'])

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'overlap-stale-tests'}})
    def test_cached_hdfc_holdings_survive_blocked_page(self):
        records = [
            {'scheme_code': '119835', 'scheme_name': 'SBI Contra Fund', 'amc': 'SBI Mutual Fund', 'category': 'Equity Scheme - Contra Fund'},
            {'scheme_code': '118955', 'scheme_name': 'HDFC Flexi Cap Fund', 'amc': 'HDFC Mutual Fund', 'category': 'Equity Scheme - Flexi Cap Fund'},
        ]
        cache.clear()
        cache_key = 'fund-analyzer:portfolio-parsed:118955:2026-09'
        cache.set(cache_key, {'holdings': {'INE040A01034': 7.0}, 'portfolio_date': '2026-09-30'})
        cached_link = {'url': 'https://files.hdfcfund.com/hdfc.xlsx', 'cache_key': cache_key, 'month': '2026-09', 'cached': True}

        def links(_source, fund_name):
            if fund_name == 'HDFC Flexi Cap Fund':
                raise ProviderError('403 Forbidden')
            return [{'url': 'https://www.sbimf.com/sbi.xlsx', 'month': '2026-09', 'text': fund_name}]

        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value={'2026-09'}),
            patch('fund_analyzer.holdings._imported_links', return_value=[]),
            patch('fund_analyzer.holdings._parsed_links', side_effect=lambda code: [cached_link] if code == '118955' else []),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=links),
            patch('fund_analyzer.holdings._download_cached', return_value=self._portfolio_file('SBI Contra Fund', [('INE040A01034', 5.0)])),
        ):
            result = analyze_holdings_overlap(['119835', '118955'])
        self.assertEqual(result['highest_overlap']['percentage'], 5.0)
        self.assertEqual(result['portfolio_month'], '2026-09')
        self.assertEqual(result['debug'][1]['error'], '403 Forbidden')

    def _analyze_month_schedule(self, schedule, weights):
        records = [
            {'scheme_code': str(index), 'scheme_name': name, 'amc': 'HDFC Mutual Fund', 'category': 'Equity Scheme - Flexi Cap Fund'}
            for index, name in enumerate(schedule, start=1)
        ]

        def links(_source, name):
            return [{'url': f'https://example.test/{name}/{month}.xlsx', 'month': month, 'text': name} for month in schedule[name]]

        def parsed(_payload, url, name, _parser, diagnostics, _workbook):
            month = url.rsplit('/', 1)[-1][:7]
            diagnostics['reporting_date'] = f'{month}-30' if month == '2026-09' else f'{month}-31'
            return {'INE040A01034': weights[(name, month)]}

        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value={'2026-10', '2026-09', '2026-08'}),
            patch('fund_analyzer.holdings._imported_links', return_value=[]),
            patch('fund_analyzer.holdings._parsed_links', return_value=[]),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=links),
            patch('fund_analyzer.holdings._download_cached', return_value=b'workbook'),
            patch('fund_analyzer.holdings._workbook_sheets', return_value=({}, {})),
            patch('fund_analyzer.holdings._read_equity_holdings', side_effect=parsed),
            patch('fund_analyzer.holdings._cache_parsed_holdings'),
        ):
            return analyze_holdings_overlap([record['scheme_code'] for record in records])

    def test_three_funds_prefer_one_common_month_for_all_pairs(self):
        schedule = {'Fund A': ['2026-10', '2026-09'], 'Fund B': ['2026-10', '2026-09'], 'Fund C': ['2026-09']}
        weights = {('Fund A', '2026-10'): 8, ('Fund B', '2026-10'): 9,
                   ('Fund A', '2026-09'): 2, ('Fund B', '2026-09'): 3, ('Fund C', '2026-09'): 4}
        result = self._analyze_month_schedule(schedule, weights)
        self.assertEqual(result['portfolio_month'], '2026-09')
        self.assertEqual(len(result['pairs']), 3)
        self.assertTrue(all(pair['portfolio_month'] == '2026-09' for pair in result['pairs']))
        self.assertEqual(result['highest_overlap']['percentage'], 3.0)

    def test_fallback_keeps_going_until_latest_month_common_to_all_funds(self):
        schedule = {'Fund A': ['2026-10', '2026-09', '2026-08'], 'Fund B': ['2026-09', '2026-08'], 'Fund C': ['2026-10', '2026-08']}
        weights = {('Fund A', '2026-10'): 8, ('Fund A', '2026-09'): 7, ('Fund A', '2026-08'): 2,
                   ('Fund B', '2026-09'): 9, ('Fund B', '2026-08'): 3,
                   ('Fund C', '2026-10'): 9, ('Fund C', '2026-08'): 4}
        result = self._analyze_month_schedule(schedule, weights)
        self.assertEqual(result['portfolio_month'], '2026-08')
        self.assertTrue(all(pair['portfolio_month'] == '2026-08' for pair in result['pairs']))
        self.assertFalse(result['pairs_different_months'])
        self.assertEqual(result['highest_overlap']['percentage'], 3.0)

    def test_two_funds_use_october_when_both_monthly_files_work(self):
        schedule = {'Fund A': ['2026-10', '2026-09'], 'Fund B': ['2026-10', '2026-09']}
        weights = {('Fund A', '2026-10'): 8, ('Fund B', '2026-10'): 9,
                   ('Fund A', '2026-09'): 2, ('Fund B', '2026-09'): 3}
        result = self._analyze_month_schedule(schedule, weights)
        self.assertEqual(result['portfolio_month'], '2026-10')
        self.assertEqual(result['highest_overlap']['portfolio_date'], '2026-10-31')
        self.assertEqual(result['highest_overlap']['percentage'], 8.0)
        self.assertEqual(result['debug'][0]['attempted_months'], ['2026-10'])

    def test_partial_pairs_report_when_they_use_different_months(self):
        schedule = {'Fund A': ['2026-10', '2026-09'], 'Fund B': ['2026-10'], 'Fund C': ['2026-09']}
        weights = {('Fund A', '2026-10'): 8, ('Fund B', '2026-10'): 9,
                   ('Fund A', '2026-09'): 2, ('Fund C', '2026-09'): 4}
        result = self._analyze_month_schedule(schedule, weights)
        self.assertEqual({pair['portfolio_month'] for pair in result['pairs']}, {'2026-10', '2026-09'})
        self.assertTrue(result['pairs_different_months'])
        self.assertTrue(result['highest_overlap']['partial_coverage'])
        self.assertEqual(result['highest_overlap']['portfolio_month'], '2026-10')

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'overlap-import-tests'}})
    def test_imported_monthly_files_feed_highest_overlap(self):
        records = [
            {'scheme_code': '119835', 'scheme_name': 'SBI Contra Fund', 'amc': 'SBI Mutual Fund', 'category': 'Equity Scheme - Contra Fund'},
            {'scheme_code': '118955', 'scheme_name': 'HDFC Flexi Cap Fund', 'amc': 'HDFC Mutual Fund', 'category': 'Equity Scheme - Flexi Cap Fund'},
        ]
        cache.clear()
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=ProviderError('403 Forbidden')),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value={'2026-09'}),
        ):
            import_portfolio_holdings('119835', '2026-09-30', 'sbi.xlsx', self._portfolio_file('SBI Contra Fund', [
                ('INE040A01034', 5.0), ('INE090A01021', 4.0), ('INE002A01018', 3.0),
            ]))
            import_portfolio_holdings('118955', '2026-09-30', 'hdfc.xlsx', self._portfolio_file('HDFC Flexi Cap Fund', [
                ('INE040A01034', 7.0), ('INE090A01021', 6.0), ('INE009A01021', 2.0),
            ]))
            result = analyze_holdings_overlap(['119835', '118955'])
            endpoint_response = APIClient().post('/api/overlap', {'scheme_codes': ['119835', '118955']}, format='json')
        self.assertEqual(result['highest_overlap']['percentage'], 9.0)
        self.assertEqual(result['highest_overlap']['portfolio_date'], '2026-09-30')
        self.assertFalse(result['highest_overlap']['partial_coverage'])
        self.assertEqual(endpoint_response.status_code, 200)
        self.assertEqual(endpoint_response.data['highest_overlap']['percentage'], 9.0)
        self.assertEqual(result['pairs'][0]['common_holdings'], [
            {'isin': 'INE040A01034', 'name': 'Stock', 'left_weight': 5.0, 'right_weight': 7.0, 'overlap_weight': 5.0, 'security_class': 'equity'},
            {'isin': 'INE090A01021', 'name': 'Stock', 'left_weight': 4.0, 'right_weight': 6.0, 'overlap_weight': 4.0, 'security_class': 'equity'},
        ])
        self.assertTrue(all('holding_names' not in item for item in result['debug']))

    def test_common_holdings_explain_the_overlap_and_tolerate_missing_names(self):
        left = {'holdings': {'INE040A01034': 2.5, 'INE090A01021': 8.0, 'INE002A01018': 3.0}, 'names': {'INE090A01021': 'ICICI Bank'}}
        right = {'holdings': {'INE040A01034': 4.0, 'INE090A01021': 6.0}}
        rows = _common_holdings(left, right)
        self.assertEqual([row['name'] for row in rows], ['ICICI Bank', 'INE040A01034'])
        self.assertEqual(sum(min(row['left_weight'], row['right_weight']) for row in rows), calculate_overlap(left['holdings'], right['holdings']))

    def test_holding_names_drop_footnote_marks(self):
        self.assertEqual(_clean_holding_name('  HDFC Bank  Ltd.£ '), 'HDFC Bank Ltd.')
        self.assertEqual(_clean_holding_name('Reliance Industries Limited**'), 'Reliance Industries Limited')
        self.assertEqual(_clean_holding_name(None), '')

    def _analyze(self, holdings_by_name, dates=None, categories=None):
        categories = categories or {}
        records = [
            {'scheme_code': str(index), 'scheme_name': name, 'amc': 'HDFC Mutual Fund', 'category': categories.get(name, 'Equity Scheme - Flexi Cap Fund')}
            for index, name in enumerate(holdings_by_name, start=1)
        ]
        dates = dates or {}

        def links_for_fund(_source, name):
            return [{
                'url': f'https://example.test/{name}.xlsx',
                'text': name,
                'month': '2026-09',
                'portfolio_date': dates.get(name, '2026-09-30'),
            }]

        def parsed_holdings(_payload, _url, name, *_args):
            value = holdings_by_name[name]
            if isinstance(value, Exception):
                raise value
            _args[1]['reporting_date'] = dates.get(name, '2026-09-30')
            return value

        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._imported_links', return_value=[]),
            patch('fund_analyzer.holdings._parsed_links', return_value=[]),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value={'2026-09'}),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=links_for_fund),
            patch('fund_analyzer.holdings._download_cached', return_value=b'workbook'),
            patch('fund_analyzer.holdings._workbook_sheets', return_value=({}, {})),
            patch('fund_analyzer.holdings._read_equity_holdings', side_effect=parsed_holdings),
        ):
            return analyze_holdings_overlap([row['scheme_code'] for row in records])

    def test_eight_funds_produce_twenty_eight_pairs(self):
        holdings = {f'Fund {index}': {'INE040A01034': float(index)} for index in range(1, 9)}
        result = self._analyze(holdings)
        self.assertEqual(len(result['pairs']), 28)
        self.assertEqual(result['highest_overlap']['percentage'], 7.0)
        self.assertFalse(result['highest_overlap']['partial_coverage'])

    def test_zero_overlap_is_available(self):
        result = self._analyze({'Fund A': {'INE040A01034': 5.0}, 'Fund B': {'INE002A01018': 7.0}})
        self.assertTrue(result['highest_overlap']['available'])
        self.assertEqual(result['highest_overlap']['percentage'], 0.0)

    def test_debt_fund_without_files_says_holdings_unavailable(self):
        records = [
            {'scheme_code': '1', 'scheme_name': 'Bond Fund', 'amc': 'SBI Mutual Fund', 'category': 'Debt Scheme - Corporate Bond Fund'},
            {'scheme_code': '2', 'scheme_name': 'Equity Fund', 'amc': 'HDFC Mutual Fund', 'category': 'Equity Scheme - Flexi Cap Fund'},
        ]
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._imported_links', return_value=[]),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
        ):
            result = analyze_holdings_overlap(['1', '2'])
        self.assertFalse(result['highest_overlap']['available'])
        self.assertEqual(result['highest_overlap']['message'], 'Holdings unavailable')

    def test_missing_holdings_are_excluded_and_coverage_is_partial(self):
        result = self._analyze({
            'Fund A': {'INE040A01034': 5.0},
            'Fund B': {'INE040A01034': 3.0},
            'Fund C': ProviderError('Portfolio file unavailable'),
        })
        self.assertEqual(result['highest_overlap']['percentage'], 3.0)
        self.assertEqual(result['highest_overlap']['compared_pairs'], 1)
        self.assertTrue(result['highest_overlap']['partial_coverage'])

    def test_debt_fund_beside_common_month_funds_is_partial_not_a_crash(self):
        result = self._analyze(
            {'Fund A': {'INE040A01034': 5.0}, 'Fund B': {'INE040A01034': 3.0}, 'Bond Fund': {}},
            categories={'Bond Fund': 'Debt Scheme - Corporate Bond Fund'},
        )
        self.assertEqual(result['portfolio_month'], '2026-09')
        self.assertEqual(result['highest_overlap']['percentage'], 3.0)
        self.assertEqual(result['highest_overlap']['compared_pairs'], 1)
        self.assertEqual(result['highest_overlap']['total_pairs'], 3)
        self.assertTrue(result['highest_overlap']['partial_coverage'])

    def test_no_valid_common_month_reports_holdings_unavailable(self):
        result = self._analyze({'Fund A': ProviderError('Bad file'), 'Fund B': ProviderError('Bad file')})
        self.assertFalse(result['highest_overlap']['available'])
        self.assertEqual(result['highest_overlap']['message'], 'Portfolio holdings unavailable')

    def test_same_month_with_different_reporting_dates_has_no_exact_date(self):
        result = self._analyze(
            {'Fund A': {'INE040A01034': 5.0}, 'Fund B': {'INE040A01034': 3.0}},
            dates={'Fund A': '2026-09-29', 'Fund B': '2026-09-30'},
        )
        self.assertEqual(result['portfolio_month'], '2026-09')
        self.assertIsNone(result['highest_overlap']['portfolio_date'])
