"""Tests for debt fund holdings and overlap. These use no database."""
from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase
from openpyxl import Workbook

from .holdings import _debt_issuer, _read_debt_holdings, _read_equity_holdings, analyze_holdings_overlap, fetch_fund_holdings, import_portfolio_holdings
from .portfolio_store import read_snapshot, store_snapshot
from .tests_pipeline import HDFC_LARGE, PortfolioCacheMixin, holdings_result

HDFC_DEBT = {'scheme_code': '128629', 'scheme_name': 'HDFC Banking and PSU Debt Fund', 'amc': 'HDFC Mutual Fund', 'plan': 'Direct Plan', 'category': 'Debt Scheme - Banking and PSU Fund'}
SBI_DEBT = {'scheme_code': '146215', 'scheme_name': 'SBI Corporate Bond Fund', 'amc': 'SBI Mutual Fund', 'plan': 'Direct Plan', 'category': 'Debt Scheme - Corporate Bond Fund'}
RECENT = {'2026-10', '2026-09', '2026-08'}


def sheet_bytes(rows, *, title='Sheet'):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title[:31]
    for row in rows:
        sheet.append(row)
    payload = BytesIO()
    workbook.save(payload)
    return payload.getvalue()


def hdfc_style_file(name='HDFC Banking and PSU Debt Fund', date='September 30, 2026', ncd_weight=5.41, net_current_assets=True):
    """Layout of HDFC's real file: ISIN first, instrument types as heading rows."""
    return sheet_bytes([
        [name],
        [f'Monthly Portfolio Statement as on {date}'],
        ['ISIN', 'Coupon (%)', 'Name Of the Instrument', 'Industry+ /Rating', 'Quantity', 'Market/ Fair Value (Rs. in Lacs.)', '% to NAV'],
        ['DEBT INSTRUMENTS'],
        ['(a) Listed / awaiting listing on Stock Exchanges'],
        ['Government Securities (Central/State)'],
        ['IN0020210152', 6.67, '6.67% GOI MAT 151235^', 'Sovereign', 8000000, 7730.53, 1.53],
        ['Non-Convertible debentures / Bonds'],
        ['INE053F08387', 7.46, 'Indian Railways Finance Corp. Ltd.^', 'CRISIL - AAA', 27500, 27357.91, ncd_weight],
        ['INE296A07SV1', 7.1, 'Bajaj Finance Ltd.', 'CRISIL - AAA', 100, 10110.92, 2.0],
        ['Sub Total', 438615.47, 86.77],
        ['(c) Securitized Debt Instruments'],
        ['Securitized Debt Instruments'],
        ['INE2I7F15012', None, 'Siddhivinayak Securitisation Trust (Originator: X)', 'CRISIL - AAA(SO)', 50, 4780.19, 0.95],
        ['Sub Total', 11942.85, 2.37],
        ['MONEY MARKET INSTRUMENTS'],
        ['Certificate Of Deposit (CD)'],
        ['INE692A16KU1', None, 'Union Bank of India', 'ICRA - A1+', 2000, 9801.5, 1.94],
        ['TREPS - Tri-party Repo'],
        ['TREPS - Tri-party Repo', None, None, None, None, 6622.13, 1.31],
        ['OTHERS'],
        ['Alternative Investment Fund Units'],
        ['INF0RQ622028', None, 'Corporate Debt Market Development Fund', None, 15349, 1842.89, 0.36],
        ['Net Current Assets'],
        *([['Net Current Assets', None, None, None, None, 15953.99, 3.14]] if net_current_assets else []),
        ['Grand Total', None, None, None, None, 505545.85, 100],
        ['Portfolio Classification by Asset Class(%)'],
        ['Alternative Investment Fund Units', 0.36],
        ['Cash, Cash Equivalents and Net Current Assets', 4.45],
    ])


def sbi_style_file(rows=None, date='September 30, 2026', name='SBI Corporate Bond Fund'):
    """Layout of SBI's real file, which also has an empty equity section first."""
    body = rows if rows is not None else [
        ['Non Convertible Debenture'],
        [None, 704769, 'Bajaj Finance Ltd.', 'INE296A07SV1', 'CRISIL AAA', 113000, 112847.9, 5.18],
        [None, 800, 'Bajaj Finance Ltd.', 'INE296A07XX5', 'CRISIL AAA', 10, 10, 3.0],
        ['Total', None, None, None, None, None, 1360676.99, 62.41],
        ['Central Government Securities'],
        [None, 900162, '6.67% GOI 2035', 'IN0020210152', 'Sovereign', 25000000, 25167.63, 1.15],
        ['State Government Securities'],
        [None, 1906767, '7.63% State Government of Gujarat 2036', 'IN1520260051', 'Sovereign', 19908500, 19577.34, 0.9],
        ['Treasury Bills'],
        [None, 1801501, '91 DAY T-BILL 12.11.26', 'IN002026X198', 'Sovereign', 11000000, 10933.57, 0.5],
    ]
    return sheet_bytes([
        ['SBI Mutual Fund'],
        ['SCHEME NAME :', name],
        [f'PORTFOLIO STATEMENT AS ON : {date}'],
        ['', '', 'Name of the Instrument / Issuer', 'ISIN', 'Rating / Industry^', 'Quantity', 'Market value', '% to AUM'],
        ['EQUITY & EQUITY RELATED'],
        ['a) Listed/awaiting listing on Stock Exchange', None, None, 'NIL', 'NIL'],
        ['DEBT INSTRUMENTS'],
        ['a) Listed/awaiting listing on the stock exchanges'],
        *body,
        ['OTHERS'],
        ['Alternative Investment Funds'],
        [None, 6400002, 'Corporate Debt Market Development Fund-A2', 'INF0RQ622028', 'CDMDF', 62093, 7455.18, 0.34],
        ['TREPS / Reverse Repo Investments'],
        [None, 712261017, 'Reverse Repo', 'GSECREPO1125', 'Sovereign', None, 70964.33, 0.92],
        ['Other Current Assets / (Liabilities)'],
        ['Net Receivable / Payable', None, None, None, None, None, -48887.78, -0.6],
    ])


class DebtParserTests(SimpleTestCase):
    def _read(self, payload, name='HDFC Banking and PSU Debt Fund'):
        diagnostics = {}
        return _read_debt_holdings(payload, 'x.xlsx', name, 'hdfc_xlsx_by_scheme', diagnostics), diagnostics

    def test_all_debt_instruments_are_read_as_percent_of_nav(self):
        holdings, _ = self._read(hdfc_style_file())
        self.assertEqual(holdings, {'IN0020210152': 1.53, 'INE053F08387': 5.41, 'INE296A07SV1': 2.0, 'INE2I7F15012': 0.95, 'INE692A16KU1': 1.94})

    def test_fund_units_repo_and_cash_are_not_debt_securities(self):
        holdings, _ = self._read(hdfc_style_file())
        self.assertNotIn('INF0RQ622028', holdings)
        self.assertLess(sum(holdings.values()), 100)

    def test_other_rows_keep_their_own_classification(self):
        _, details = self._read(hdfc_style_file())
        rows = {row['name']: row for row in details['other_holdings']}
        self.assertEqual(rows['Corporate Debt Market Development Fund']['instrument_type'], 'Alternative Investment Fund Units')
        self.assertEqual(rows['Corporate Debt Market Development Fund']['isin'], 'INF0RQ622028')
        self.assertEqual(rows['TREPS - Tri-party Repo'], {'name': 'TREPS - Tri-party Repo', 'weight': 1.31, 'instrument_type': 'TREPS / Repo'})
        self.assertEqual(rows['Net Current Assets']['instrument_type'], 'Net Current Assets')

    def test_summary_rows_after_the_grand_total_are_not_read_twice(self):
        _, details = self._read(hdfc_style_file())
        self.assertEqual(len(details['other_holdings']), 3)

    def test_negative_net_current_assets_keep_their_sign(self):
        _, details = self._read(sbi_style_file(), 'SBI Corporate Bond Fund')
        rows = {row['name']: row for row in details['other_holdings']}
        self.assertEqual(rows['Net Receivable / Payable']['weight'], -0.6)

    def test_repo_deal_code_is_not_kept_as_an_isin(self):
        # SBI's TREP01102026 style codes have the same shape as a real ISIN.
        holdings, details = self._read(sbi_style_file(), 'SBI Corporate Bond Fund')
        self.assertNotIn('GSECREPO1125', holdings)
        repo = next(row for row in details['other_holdings'] if row['name'] == 'Reverse Repo')
        self.assertEqual((repo['instrument_type'], repo.get('isin')), ('TREPS / Repo', None))

    def test_fund_units_under_a_debt_heading_are_still_not_debt(self):
        payload = sbi_style_file(rows=[['Non Convertible Debenture'], [None, 1, 'Bajaj Finance Ltd.', 'INE296A07SV1', 'CRISIL AAA', 1, 1, 5.0], [None, 2, 'Some Liquid Fund Units', 'INF200K01VS0', '', 1, 1, 1.0]])
        holdings, details = self._read(payload, 'SBI Corporate Bond Fund')
        self.assertEqual(holdings, {'INE296A07SV1': 5.0})
        unit = next(row for row in details['other_holdings'] if row.get('isin') == 'INF200K01VS0')
        self.assertEqual(unit['instrument_type'], 'Mutual Fund Units / ETFs')

    def test_weights_on_a_different_basis_are_rejected(self):
        # A weight that is not market value over grand total cannot be compared with other funds.
        with self.assertRaisesRegex(Exception, 'do not match market value'):
            self._read(hdfc_style_file(ncd_weight=7.41))

    def test_rows_that_do_not_add_up_to_100_are_logged(self):
        with self.assertLogs('fund_analyzer.holdings', level='WARNING') as logs:
            self._read(hdfc_style_file(net_current_assets=False))
        self.assertIn('add up to', logs.output[0])

    def test_instrument_type_and_issuer_come_from_the_file(self):
        _, details = self._read(hdfc_style_file())
        types, issuers = details['instrument_types'], details['issuers']
        self.assertEqual(types['IN0020210152'], 'Government Securities')
        self.assertEqual(types['INE053F08387'], 'Non-Convertible Debentures / Bonds')
        self.assertEqual(types['INE2I7F15012'], 'Securitised Debt')
        self.assertEqual(types['INE692A16KU1'], 'Certificate of Deposit')
        self.assertEqual(issuers['IN0020210152'], 'Government of India')
        self.assertEqual(issuers['INE053F08387'], 'Indian Railways Finance Corp. Ltd.')
        self.assertEqual(issuers['INE2I7F15012'], 'Siddhivinayak Securitisation Trust')
        self.assertEqual(details['holding_industries']['INE053F08387'], 'CRISIL - AAA')

    def test_credit_rating_keeps_its_plus_sign(self):
        _, details = self._read(hdfc_style_file())
        self.assertEqual(details['holding_industries']['INE692A16KU1'], 'ICRA - A1+')

    def test_second_amc_layout_with_empty_equity_section_and_state_and_tbills(self):
        holdings, details = self._read(sbi_style_file(), 'SBI Corporate Bond Fund')
        self.assertEqual(holdings, {'INE296A07SV1': 5.18, 'INE296A07XX5': 3.0, 'IN0020210152': 1.15, 'IN1520260051': 0.9, 'IN002026X198': 0.5})
        self.assertEqual(details['instrument_types']['IN1520260051'], 'State Government Securities')
        self.assertEqual(details['instrument_types']['IN002026X198'], 'Treasury Bills')
        self.assertEqual(details['issuers']['IN1520260051'], 'State Government of Gujarat')
        self.assertEqual(details['issuers']['IN002026X198'], 'Government of India')

    def test_type_is_read_from_the_name_when_one_heading_covers_several(self):
        payload = sbi_style_file(rows=[
            [None, 1, '8.95% Bharti Telecom NCD Ser XVII (MD 01/01/2028)', 'INE403D08207', 'CRISIL AAA', 1, 1, 2.2],
            [None, 2, '7.39% Haryana SDL (MD 09/11/2026)', 'IN1620160227', 'Sovereign', 1, 1, 0.48],
        ])
        _, details = self._read(payload, 'SBI Corporate Bond Fund')
        self.assertEqual(details['instrument_types']['INE403D08207'], 'Non-Convertible Debentures / Bonds')
        self.assertEqual(details['instrument_types']['IN1620160227'], 'State Government Securities')
        self.assertEqual(details['issuers']['INE403D08207'], 'Bharti Telecom')
        self.assertEqual(details['issuers']['IN1620160227'], 'State Government of Haryana')

    def test_weights_can_pass_100_when_net_assets_are_negative(self):
        rows = [['Certificate of Deposit'], [None, 1, 'Canara Bank', 'INE476A16L21', 'A1+', 1, 1, 60.0], [None, 2, 'Bank of Baroda', 'INE028A16LG5', 'A1+', 1, 1, 41.08]]
        holdings, _ = self._read(sbi_style_file(rows=rows), 'SBI Corporate Bond Fund')
        self.assertAlmostEqual(sum(holdings.values()), 101.08)

    def test_wrong_scheme_and_equity_only_file_are_rejected(self):
        with self.assertRaisesRegex(Exception, 'No sheet matched'):
            self._read(hdfc_style_file('HDFC Liquid Fund'))
        with self.assertRaisesRegex(Exception, 'No ISIN debt holdings'):
            self._read(sbi_style_file(rows=[]), 'SBI Corporate Bond Fund')

    def test_equity_parser_still_ignores_debt_rows(self):
        payload = sheet_bytes([
            ['Sample Fund'], ['Monthly Portfolio Statement as on September 30, 2026'],
            ['Name of the Instrument', 'ISIN', '% to NAV'],
            ['EQUITY & EQUITY RELATED'], ['Equity Shares'], ['HDFC Bank', 'INE040A01034', 7.0],
            ['DEBT INSTRUMENTS'], ['Some Bond', 'INE296A07SV1', 3.0],
        ])
        self.assertEqual(_read_equity_holdings(payload, 'x.xlsx', 'Sample Fund', 'sbi_monthly_excel'), {'INE040A01034': 7.0})

    def test_issuer_is_left_out_rather_than_guessed(self):
        self.assertIsNone(_debt_issuer('', 'Sovereign', 'IN1520260051'))
        self.assertEqual(_debt_issuer('6.17% CGL 2028', 'Sovereign', 'IN0020210160'), 'Government of India')


class DebtOverlapTests(PortfolioCacheMixin, SimpleTestCase):
    def _analyze(self, records, *, links=None, download=None):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', **(links or {'return_value': []})),
            patch('fund_analyzer.holdings._download_cached', **(download or {'side_effect': AssertionError('no download expected')})),
        ):
            return analyze_holdings_overlap([record['scheme_code'] for record in records])

    def _live(self, files):
        def links(_source, name):
            return [{'url': f'https://x.test/{name}.xlsx', 'text': name, 'month': '2026-09'}]

        return {'side_effect': links}, {'side_effect': lambda _key, _month, url: files[url.rsplit('/', 1)[-1][:-5]]}

    def test_128629_vs_146215_calculates_overlap_from_common_isins_only(self):
        links, download = self._live({'HDFC Banking and PSU Debt Fund': hdfc_style_file(), 'SBI Corporate Bond Fund': sbi_style_file()})
        result = self._analyze([HDFC_DEBT, SBI_DEBT], links=links, download=download)
        high = result['highest_overlap']
        # Common: INE296A07SV1 min(2.0, 5.18) and IN0020210152 min(1.53, 1.15).
        self.assertEqual(high['percentage'], 3.15)
        self.assertEqual(high['portfolio_month'], '2026-09')
        self.assertIsNone(high['message'])
        self.assertEqual([row['isin'] for row in result['pairs'][0]['common_holdings']], ['INE296A07SV1', 'IN0020210152'])

    def test_fund_units_are_a_separate_figure_and_never_in_the_primary_overlap(self):
        links, download = self._live({'HDFC Banking and PSU Debt Fund': hdfc_style_file(), 'SBI Corporate Bond Fund': sbi_style_file()})
        result = self._analyze([HDFC_DEBT, SBI_DEBT], links=links, download=download)
        pair = result['pairs'][0]
        self.assertEqual(pair['overlap_pct'], 3.15)
        self.assertEqual(result['highest_overlap']['percentage'], 3.15)
        self.assertNotIn('INF0RQ622028', [row['isin'] for row in pair['common_holdings']])
        # 3.15 plus min(0.36, 0.34) for the Corporate Debt Market Development Fund units.
        self.assertEqual(pair['including_fund_units']['overlap_pct'], 3.49)
        self.assertEqual(pair['including_fund_units']['fund_units'], [{'isin': 'INF0RQ622028', 'name': 'Corporate Debt Market Development Fund', 'left_weight': 0.36, 'right_weight': 0.34}])

    def test_fund_unit_figure_survives_the_json_cache(self):
        links, download = self._live({'HDFC Banking and PSU Debt Fund': hdfc_style_file(), 'SBI Corporate Bond Fund': sbi_style_file()})
        self._analyze([HDFC_DEBT, SBI_DEBT], links=links, download=download)
        with patch('fund_analyzer.holdings.cache.get', return_value=None):
            result = self._analyze([HDFC_DEBT, SBI_DEBT])
        self.assertEqual(result['pairs'][0]['overlap_pct'], 3.15)
        self.assertEqual(result['pairs'][0]['including_fund_units']['overlap_pct'], 3.49)

    def test_repo_is_never_in_either_overlap_figure(self):
        # Two SBI files can carry the same deal code. Matching on it would be meaningless.
        repo = [{'name': 'TREPS', 'weight': 7.0, 'instrument_type': 'TREPS / Repo'}]
        for record, code in ((HDFC_DEBT, '128629'), (SBI_DEBT, '146215')):
            store_snapshot(record, code, '2026-09', {**holdings_result({'INE296A07SV1': 5.0}), 'asset_class': 'debt', 'other_holdings': repo})
        pair = self._analyze([HDFC_DEBT, SBI_DEBT])['pairs'][0]
        self.assertEqual(pair['overlap_pct'], 5.0)
        self.assertNotIn('including_fund_units', pair)

    def test_equity_pairs_have_no_fund_unit_figure(self):
        other = {**HDFC_LARGE, 'scheme_code': '2', 'scheme_name': 'HDFC Mid Cap Fund'}
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        store_snapshot(other, '2', '2026-09', holdings_result({'INE040A01034': 4.0}))
        self.assertNotIn('including_fund_units', self._analyze([HDFC_LARGE, other])['pairs'][0])

    def test_allocation_shows_negative_other_when_securities_pass_100(self):
        result = {**holdings_result({'INE296A07SV1': 60.0, 'INE476A16L21': 46.47}), 'asset_class': 'debt', 'other_holdings': [{'name': 'Net Current Assets', 'weight': -6.47, 'instrument_type': 'Net Current Assets'}]}
        store_snapshot(HDFC_DEBT, '128629', '2026-09', result)
        with (
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
        ):
            details = fetch_fund_holdings('128629', HDFC_DEBT)
        self.assertEqual(details['allocation'], {'debt_securities': 106.47, 'other': -6.47})
        self.assertEqual(details['sectors'][-1], {'name': 'Net Current Assets', 'weight': -6.47})
        self.assertNotIn('Net Current Assets', [row['name'] for row in details['holdings']])

    def test_damaged_other_rows_make_the_cache_file_count_as_missing(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', {**holdings_result({'INE296A07SV1': 5.0}), 'asset_class': 'debt', 'other_holdings': [{'name': 'TREPS', 'instrument_type': 'TREPS / Repo'}]})
        self.assertIsNone(read_snapshot(HDFC_DEBT, '2026-09'))

    def test_two_bonds_from_one_issuer_are_not_matched(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', {**holdings_result({'INE296A07SV1': 5.0}), 'asset_class': 'debt'})
        store_snapshot(SBI_DEBT, '146215', '2026-09', {**holdings_result({'INE296A07XX5': 4.0}), 'asset_class': 'debt'})
        high = self._analyze([HDFC_DEBT, SBI_DEBT])['highest_overlap']
        self.assertEqual(high['percentage'], 0.0)
        self.assertEqual((high['message'], high['reason_code']), ('No common securities', 'NO_COMMON_SECURITIES'))

    def test_equity_with_no_common_stocks_keeps_a_plain_zero(self):
        other = {**HDFC_LARGE, 'scheme_code': '2', 'scheme_name': 'HDFC Mid Cap Fund'}
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        store_snapshot(other, '2', '2026-09', holdings_result({'INE090A01021': 4.0}))
        high = self._analyze([HDFC_LARGE, other])['highest_overlap']
        self.assertEqual(high['percentage'], 0.0)
        self.assertIsNone(high['message'])

    def test_holdings_unavailable(self):
        high = self._analyze([HDFC_DEBT, SBI_DEBT])['highest_overlap']
        self.assertIsNone(high['percentage'])
        self.assertEqual((high['message'], high['reason_code']), ('Holdings unavailable', 'HOLDINGS_UNAVAILABLE'))

    def test_parser_unsupported(self):
        empty = sbi_style_file(rows=[])
        links, download = self._live({'HDFC Banking and PSU Debt Fund': hdfc_style_file(), 'SBI Corporate Bond Fund': empty})
        high = self._analyze([HDFC_DEBT, SBI_DEBT], links=links, download=download)['highest_overlap']
        self.assertIsNone(high['percentage'])
        self.assertEqual((high['message'], high['reason_code']), ('Parser unsupported', 'PARSER_UNSUPPORTED'))

    def test_no_common_reporting_month(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-10', {**holdings_result({'INE296A07SV1': 5.0}, '2026-10-31'), 'asset_class': 'debt'})
        store_snapshot(SBI_DEBT, '146215', '2026-08', {**holdings_result({'INE296A07SV1': 4.0}, '2026-08-31'), 'asset_class': 'debt'})
        high = self._analyze([HDFC_DEBT, SBI_DEBT])['highest_overlap']
        self.assertIsNone(high['percentage'])
        self.assertEqual((high['message'], high['reason_code']), ('No common reporting month', 'NO_COMMON_REPORTING_MONTH'))

    def test_latest_common_month_is_used_for_debt(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-10', {**holdings_result({'INE296A07SV1': 9.0}, '2026-10-31'), 'asset_class': 'debt'})
        store_snapshot(HDFC_DEBT, '128629', '2026-09', {**holdings_result({'INE296A07SV1': 5.0}), 'asset_class': 'debt'})
        store_snapshot(SBI_DEBT, '146215', '2026-09', {**holdings_result({'INE296A07SV1': 4.0}), 'asset_class': 'debt'})
        high = self._analyze([HDFC_DEBT, SBI_DEBT])['highest_overlap']
        self.assertEqual((high['percentage'], high['portfolio_month']), (4.0, '2026-09'))

    def test_debt_weight_above_100_survives_the_json_cache(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', {**holdings_result({'INE296A07SV1': 60.0, 'INE476A16L21': 41.08}), 'asset_class': 'debt'})
        self.assertIsNotNone(read_snapshot(HDFC_DEBT, '2026-09'))
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 60.0, 'INE090A01021': 41.08}))
        self.assertIsNone(read_snapshot(HDFC_LARGE, '2026-09'))

    def test_fundamentals_show_securities_with_type_and_issuer(self):
        links, download = self._live({'HDFC Banking and PSU Debt Fund': hdfc_style_file()})
        with (
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', **links),
            patch('fund_analyzer.holdings._download_cached', **download),
        ):
            result = fetch_fund_holdings('128629', HDFC_DEBT)
        self.assertEqual(result['asset_class'], 'debt')
        self.assertEqual(result['holdings'][0], {'isin': 'INE053F08387', 'name': 'Indian Railways Finance Corp. Ltd.', 'weight': 5.41, 'instrument_type': 'Non-Convertible Debentures / Bonds', 'issuer': 'Indian Railways Finance Corp. Ltd.', 'rating': 'CRISIL - AAA'})
        self.assertEqual(result['allocation'], {'debt_securities': 11.83, 'other': 88.17})
        units = next(row for row in result['holdings'] if row.get('isin') == 'INF0RQ622028')
        self.assertEqual(units, {'isin': 'INF0RQ622028', 'name': 'Corporate Debt Market Development Fund', 'weight': 0.36, 'instrument_type': 'Alternative Investment Fund Units', 'excluded_from_overlap': True})
        names = [row['name'] for row in result['holdings']]
        self.assertIn('TREPS - Tri-party Repo', names)
        self.assertNotIn('Net Current Assets', names)
        sectors = {row['name']: row['weight'] for row in result['sectors']}
        self.assertEqual((sectors['Alternative Investment Fund Units'], sectors['TREPS / Repo'], sectors['Net Current Assets']), (0.36, 1.31, 3.14))
        self.assertEqual(result['sectors'][0], {'name': 'Non-Convertible Debentures / Bonds', 'weight': 7.41})

    def test_cached_debt_details_are_read_back_without_downloading(self):
        links, download = self._live({'HDFC Banking and PSU Debt Fund': hdfc_style_file()})
        with (
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', **links),
            patch('fund_analyzer.holdings._download_cached', **download),
        ):
            fetch_fund_holdings('128629', HDFC_DEBT)
        with (
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
            patch('fund_analyzer.holdings._download_cached', side_effect=AssertionError('downloaded again')),
        ):
            result = fetch_fund_holdings('128629', HDFC_DEBT)
        self.assertEqual(result['holdings'][0]['instrument_type'], 'Non-Convertible Debentures / Bonds')
        self.assertEqual(result['holdings'][0]['issuer'], 'Indian Railways Finance Corp. Ltd.')

    def test_staff_can_upload_a_debt_file(self):
        with patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[HDFC_DEBT]):
            import_portfolio_holdings('128629', '2026-09-30', 'debt.xlsx', hdfc_style_file())
        self.assertEqual(read_snapshot(HDFC_DEBT, '2026-09')['asset_class'], 'debt')
