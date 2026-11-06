"""Tests for the Charlie Alpha backup source. They never touch the network and use no database."""
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from . import charlie_alpha as ca
from .charlie_alpha import CharlieError, normalise_portfolio
from .holdings import analyze_holdings_overlap
from .portfolio_store import SOURCE_CHARLIE, read_snapshot, store_snapshot, stored_links
from .tests_pipeline import PortfolioCacheMixin, holdings_result

RECENT = {'2026-10', '2026-09', '2026-08'}
ALL_SOURCES = 'Portfolio holdings unavailable for a matching reporting month.'


def make_isin(prefix: str) -> str:
    """A real-shaped ISIN with a correct check digit, so tests do not depend on any particular company."""
    return next(prefix + str(digit) for digit in range(10) if ca.isin_is_valid(prefix + str(digit)))


E1, E2, E3, E4 = (make_isin(f'INE00{n}A0101') for n in (1, 2, 3, 4))
BOND = make_isin('INE004A0701')
STATE_SEC = make_isin('IN202025001')
INVIT = make_isin('INE005A2301')
UNIT_1, UNIT_2 = make_isin('INF179K0101'), make_isin('INF204K0101')
CDMDF = make_isin('INF0RQ62202')


def fund(code, name, amc='HDFC Mutual Fund', category='Equity Scheme - Large Cap Fund'):
    return {'scheme_code': code, 'scheme_name': name, 'amc': amc, 'plan': 'Direct Plan', 'category': category, 'isin': f'INF000A0{code[-4:]}'}


A = fund('1001', 'Alpha Large Cap Fund')
B = fund('1002', 'Beta Large Cap Fund', amc='Nowhere Mutual Fund')   # no official source exists for this AMC
C = fund('1003', 'Gamma Large Cap Fund')


def row(isin, pct, label='equity', name=None):
    return {'isin': isin, 'name': name or f'Company {str(isin)[-4:]}', 'industry': 'Banks', 'holding_type': label, 'holding_pct': pct, 'quantity': 10.0}


def complete(weights, extra=()):
    """A whole portfolio: the stocks, anything extra, and a cash row that brings it to 100%."""
    rows = [row(isin, pct) for isin, pct in weights.items()] + list(extra)
    rows.append(row(None, round(100 - sum(r['holding_pct'] for r in rows), 4), 'cash', 'Cash'))
    return rows


def payload(record, as_of, rows, **changes):
    body = {
        'amfi_code': record['scheme_code'], 'isin': record['isin'], 'scheme_name': f"{record['scheme_name']} - Growth Option - Direct Plan",
        'category': record['category'], 'as_of': as_of, 'total_holdings': len(rows), 'holdings': rows,
    }
    return {**body, **changes}


class FakeProvider:
    """Stands in for the API. portfolios maps scheme code -> {as_of: payload}. It records every request."""

    def __init__(self, portfolios=None, error=None):
        self.portfolios, self.error, self.calls = portfolios or {}, error, []

    def __call__(self, path, params=None):
        self.calls.append((path, (params or {}).get('as_of')))
        if self.error:
            raise self.error
        code = path.rsplit('/', 1)[-1]
        if path.startswith('/mf/holdings-periods/'):
            if code not in self.portfolios:
                raise CharlieError(ca.NOT_FOUND)
            return {'amfi_code': code, 'periods': [{'as_of': as_of, 'holdings_count': 1} for as_of in sorted(self.portfolios[code], reverse=True)]}
        if path.startswith('/mf/holdings/'):
            if code not in self.portfolios or params['as_of'] not in self.portfolios[code]:
                raise CharlieError(ca.NOT_FOUND)
            return self.portfolios[code][params['as_of']]
        raise AssertionError(f'unexpected request {path}')

    def codes_asked(self):
        return {path.rsplit('/', 1)[-1] for path, _ in self.calls}


def official(record, months):
    """Verified official holdings already saved for these months: {month: {isin: weight}}."""
    day = {'2026-10': '31', '2026-09': '30', '2026-08': '31', '2026-07': '31', '2026-06': '30', '2026-05': '31'}
    for month, weights in months.items():
        store_snapshot(record, record['scheme_code'], month, holdings_result(weights, f'{month}-{day[month]}'))


class BackupTestCase(PortfolioCacheMixin, SimpleTestCase):
    """The backup is on, its memory is empty, and nothing can reach the real API."""

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True))
        ca.clear_memory()
        self.addCleanup(ca.clear_memory)

    def analyze(self, records, provider, links=None, download=None, progress=None, codes=None):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', **(links or {'return_value': []})),
            patch('fund_analyzer.holdings._download_cached', **(download or {'side_effect': AssertionError('no official download expected')})),
            patch.object(ca, '_get_json', provider),
        ):
            return analyze_holdings_overlap(codes or [record['scheme_code'] for record in records], progress=progress)

    def fund_of(self, result, code):
        return next(item for item in result['funds'] if item['scheme_code'] == code)


# --- Validation: what is accepted and what is refused -------------------------------------------------

class ValidationTests(SimpleTestCase):
    def accept(self, record=A, rows=None, **changes):
        rows = rows if rows is not None else complete({E1: 40.0, E2: 20.0})
        return normalise_portfolio(record, payload(record, '2026-09-30', rows, **changes), requested_as_of='2026-09-30', asset_class='equity', payload_asset_class='equity')

    def refuse(self, code, rows=None, record=A, asset_class='equity', payload_class='equity', as_of='2026-09-30', **changes):
        rows = rows if rows is not None else complete({E1: 40.0, E2: 20.0})
        with self.assertRaises(CharlieError) as caught:
            normalise_portfolio(record, payload(record, as_of, rows, **changes), requested_as_of='2026-09-30', asset_class=asset_class, payload_asset_class=payload_class)
        self.assertEqual(caught.exception.code, code)

    def test_a_correct_portfolio_becomes_the_internal_format(self):
        result = self.accept()
        self.assertEqual(result['holdings'], {E1: 40.0, E2: 20.0})
        self.assertEqual(result['security_classes'], {E1: 'equity', E2: 'equity'})
        self.assertEqual(result['classes_read'], ['equity', 'debt'])
        self.assertEqual(result['portfolio_date'], '2026-09-30')
        self.assertEqual(result['other_holdings'], [{'name': 'Cash', 'weight': 40.0, 'instrument_type': 'Cash & Margin'}])
        self.assertEqual((result['validation']['status'], result['validation']['provider']), ('VERIFIED', 'charlie_alpha'))

    def test_the_isin_decides_the_class_not_the_providers_label(self):
        # The provider calls a bond, a state security and an InvIT 'equity'. Their ISINs say otherwise.
        rows = complete({E1: 40.0}, [row(BOND, 10.0, 'equity'), row(STATE_SEC, 5.0, 'equity'), row(INVIT, 3.0, 'equity')])
        result = self.accept(rows=rows)
        self.assertEqual(result['security_classes'], {E1: 'equity', BOND: 'debt', STATE_SEC: 'debt'})
        self.assertNotIn(INVIT, result['holdings'])
        self.assertIn(INVIT, [item.get('isin') for item in result['other_holdings']])
        self.assertEqual((result['validation']['label_mismatch_rows'], result['validation']['label_mismatch_weight']), (2, 15.0))

    def test_cash_derivatives_and_fund_units_are_never_security_holdings(self):
        rows = complete({E1: 40.0}, [row(E2, 2.0, 'derivative'), row(UNIT_1, 1.0, 'fund_unit'), row(CDMDF, 0.4, 'fund_unit', 'Corporate Debt Market Development Fund')])
        result = self.accept(rows=rows)
        self.assertEqual(list(result['holdings']), [E1])
        kinds = {item['instrument_type'] for item in result['other_holdings']}
        self.assertTrue({'Derivatives', 'Mutual Fund Units / ETFs', 'Alternative Investment Fund Units', 'Cash & Margin'} <= kinds)

    def test_a_repeated_isin_is_counted_once_with_its_weights_added(self):
        result = self.accept(rows=complete({E1: 30.0}, [row(E1, 10.0)]))
        self.assertEqual(result['holdings'], {E1: 40.0})
        self.assertEqual(result['validation']['duplicate_isin_rows_merged'], 1)

    def test_a_zero_weight_row_is_not_a_holding(self):
        result = self.accept(rows=complete({E1: 40.0, E3: 0.0}))
        self.assertEqual(result['holdings'], {E1: 40.0})

    def test_the_wrong_scheme_is_refused(self):
        self.refuse(ca.SCHEME_MISMATCH, amfi_code='999999')
        self.refuse(ca.SCHEME_MISMATCH, isin='INF999Z01234')
        self.refuse(ca.SCHEME_MISMATCH, scheme_name='Totally Different Mid Cap Fund')
        self.refuse(ca.SCHEME_MISMATCH, payload_class='debt')

    def test_the_wrong_date_is_refused(self):
        self.refuse(ca.DATE_MISMATCH, as_of='2026-08-31')
        self.refuse(ca.DATE_MISMATCH, as_of='not a date')
        self.refuse(ca.DATE_MISMATCH, as_of='2999-01-01')

    def test_a_partial_portfolio_is_refused(self):
        # A top-10 list adds up to far less than the scheme.
        self.refuse(ca.INCOMPLETE, rows=[row(E1, 9.0), row(E2, 8.0)])
        self.refuse(ca.INCOMPLETE, rows=complete({E1: 40.0}), total_holdings=99)
        self.refuse(ca.INCOMPLETE, rows=[row(E1, 80.0), row(E2, 40.0)])

    def test_bad_weights_are_refused(self):
        self.refuse(ca.BAD_WEIGHT, rows=[row(E1, 99.0), {**row(E2, 1.0), 'holding_pct': 'lots'}])
        self.refuse(ca.BAD_WEIGHT, rows=[row(E1, 99.0), {**row(E2, 1.0), 'holding_pct': None}])
        self.refuse(ca.BAD_WEIGHT, rows=[row(E1, 130.0), row(None, -30.0, 'cash', 'Cash')])
        self.refuse(ca.BAD_WEIGHT, rows=[row(E1, 105.0)], total_holdings=1)

    def test_an_unverifiable_isin_is_never_matched_on(self):
        bad = 'INE001A01019'      # wrong check digit
        self.assertFalse(ca.isin_is_valid(bad))
        small = self.accept(rows=complete({E1: 40.0}, [row(bad, 0.5)]))
        self.assertNotIn(bad, small['holdings'])
        self.assertEqual(small['validation']['unverifiable_isin_weight'], 0.5)
        self.refuse(ca.INVALID_ISIN, rows=complete({E1: 40.0}, [row(bad, 5.0)]))

    def test_a_portfolio_must_look_like_its_kind_of_fund(self):
        self.refuse(ca.WRONG_CLASS, rows=complete({}, [row(BOND, 90.0, 'debt')]))   # an equity scheme holding bonds
        stocks_in_a_debt_fund = complete({E1: 50.0, E2: 20.0})
        self.refuse(ca.WRONG_CLASS, rows=stocks_in_a_debt_fund, asset_class='debt', payload_class='debt')
        self.refuse(ca.WRONG_CLASS, rows=complete({E1: 40.0}), asset_class='fof', payload_class='fof')

    def test_an_empty_or_malformed_response_is_refused(self):
        self.refuse(ca.NO_HOLDINGS, rows=[], total_holdings=0)
        self.refuse(ca.INVALID_JSON, rows=['not a row'], total_holdings=1)

    def test_a_fund_of_funds_keeps_only_its_fund_units(self):
        fof = fund('1009', 'Omega Active FoF', category='Other Scheme - FoF Domestic')
        rows = [row(UNIT_1, 60.0, 'fund_unit'), row(UNIT_2, 30.0, 'fund_unit'), row(CDMDF, 0.3, 'fund_unit', 'Corporate Debt Market Development Fund'), row(None, 9.7, 'cash', 'Cash')]
        result = normalise_portfolio(fof, payload(fof, '2026-09-30', rows), requested_as_of='2026-09-30', asset_class='fof', payload_asset_class='fof')
        self.assertEqual(result['holdings'], {UNIT_1: 60.0, UNIT_2: 30.0})
        self.assertEqual(set(result['security_classes'].values()), {'fund_unit'})
        self.assertEqual(result['classes_read'], ['fund_unit'])
        self.assertIn(CDMDF, [item.get('isin') for item in result['other_holdings']])

    def test_check_digits(self):
        self.assertTrue(ca.isin_is_valid('INE090A01021'))
        self.assertFalse(ca.isin_is_valid('INE090A01022'))
        self.assertFalse(ca.isin_is_valid('short'))


# --- The client: bounded, polite and strict --------------------------------------------------------------

def reply(status=200, body=None, headers=None, bad_json=False):
    response = MagicMock(status_code=status, headers=headers or {})
    if bad_json:
        response.json.side_effect = ValueError('not json')
    else:
        response.json.return_value = body if body is not None else {}
    return response


class ClientTests(SimpleTestCase):
    def setUp(self):
        ca.clear_memory()
        self.addCleanup(ca.clear_memory)
        self.enterContext(override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True, FUND_ANALYZER_CHARLIE_ALPHA_MAX_REQUESTS=40))
        self.enterContext(patch('fund_analyzer.charlie_alpha.time.sleep'))
        ca._budget.set(None)

    def get(self, *answers):
        return patch('fund_analyzer.charlie_alpha.requests.get', side_effect=list(answers))

    def test_a_missing_scheme_is_not_retried(self):
        with self.get(reply(404)) as http:
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/mf/holdings/1')
        self.assertEqual((caught.exception.code, http.call_count), (ca.NOT_FOUND, 1))

    def test_throttling_is_retried_then_succeeds(self):
        with self.get(reply(429, headers={'Retry-After': '1'}), reply(200, {'ok': True})) as http:
            self.assertEqual(ca._get_json('/x'), {'ok': True})
        self.assertEqual(http.call_count, 2)

    def test_endless_throttling_gives_up_after_a_few_attempts(self):
        with self.get(*[reply(429)] * 5) as http:
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/x')
        self.assertEqual((caught.exception.code, http.call_count), (ca.RATE_LIMITED, ca.MAX_ATTEMPTS))

    def test_timeouts_and_server_errors_are_retried_a_bounded_number_of_times(self):
        with self.get(requests.Timeout('slow'), reply(500), reply(200, {'ok': 1})) as http:
            self.assertEqual(ca._get_json('/x'), {'ok': 1})
        self.assertEqual(http.call_count, 3)
        with self.get(*[requests.Timeout('slow')] * 5) as http:
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/x')
        self.assertEqual((caught.exception.code, http.call_count), (ca.TIMEOUT, ca.MAX_ATTEMPTS))
        ca.clear_memory()   # the outage above opened the breaker. Each case here starts fresh.
        with self.get(*[reply(503)] * 5):
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/x')
        self.assertEqual(caught.exception.code, ca.HTTP_ERROR)

    def test_after_an_outage_the_provider_is_not_asked_again_for_a_while(self):
        with self.get(*[requests.Timeout('slow')] * 3) as http:
            with self.assertRaises(CharlieError):
                ca._get_json('/a')
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/b')
        # Three attempts for the first question. The second is answered at once, without the network.
        self.assertEqual((caught.exception.code, http.call_count), (ca.UNAVAILABLE, 3))

    def test_a_missing_scheme_does_not_trip_the_breaker(self):
        with self.get(reply(404), reply(200, {'ok': 1})):
            with self.assertRaises(CharlieError):
                ca._get_json('/a')
            self.assertEqual(ca._get_json('/b'), {'ok': 1})

    def test_a_refusal_is_not_retried(self):
        with self.get(reply(403)) as http:
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/x')
        self.assertEqual((caught.exception.code, http.call_count), (ca.HTTP_ERROR, 1))

    def test_invalid_json_is_refused(self):
        with self.get(reply(200, bad_json=True)):
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/x')
        self.assertEqual(caught.exception.code, ca.INVALID_JSON)
        with self.get(reply(200, ['a list'])):
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/x')
        self.assertEqual(caught.exception.code, ca.INVALID_JSON)

    def test_one_analysis_cannot_send_unlimited_requests(self):
        self.enterContext(override_settings(FUND_ANALYZER_CHARLIE_ALPHA_MAX_REQUESTS=2))
        with self.get(*[reply(200, {'ok': 1})] * 5) as http:
            ca.begin_analysis()
            ca._get_json('/a')
            ca._get_json('/b')
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/c')
        self.assertEqual((caught.exception.code, http.call_count), (ca.BUDGET_EXHAUSTED, 2))

    def test_the_local_limiter_stays_under_the_providers_limit(self):
        with patch.object(ca, 'RATE_LIMIT_REQUESTS', 2), patch.object(ca, 'MAX_RATE_WAIT', 0.0), self.get(*[reply(200, {'ok': 1})] * 5) as http:
            ca._get_json('/a')
            ca._get_json('/b')
            with self.assertRaises(CharlieError) as caught:
                ca._get_json('/c')
        self.assertEqual((caught.exception.code, http.call_count), (ca.RATE_LIMITED, 2))

    def test_a_repeated_question_is_answered_from_memory(self):
        periods = {'amfi_code': '1001', 'periods': [{'as_of': '2026-09-30'}, {'as_of': '2026-08-31'}]}
        with self.get(reply(200, periods)) as http:
            self.assertEqual(ca.list_periods(A), ['2026-09-30', '2026-08-31'])
            self.assertEqual(ca.list_periods(A), ['2026-09-30', '2026-08-31'])
        self.assertEqual(http.call_count, 1)

    def test_periods_for_another_scheme_future_dates_and_none_at_all(self):
        with self.get(reply(200, {'amfi_code': '5555', 'periods': [{'as_of': '2026-09-30'}]})):
            with self.assertRaises(CharlieError) as caught:
                ca.list_periods(A)
        self.assertEqual(caught.exception.code, ca.SCHEME_MISMATCH)
        ca.clear_memory()
        with self.get(reply(200, {'amfi_code': '1001', 'periods': [{'as_of': '2999-01-01'}, {'as_of': 'junk'}]})):
            with self.assertRaises(CharlieError) as caught:
                ca.list_periods(A)
        self.assertEqual(caught.exception.code, ca.NO_PERIODS)

    def test_the_providers_own_overlap_is_available_only_as_a_cross_check(self):
        with self.get(reply(200, {'overlap': 12.3})) as http:
            self.assertEqual(ca.cross_check_overlap(['1', '2']), {'overlap': 12.3})
        self.assertEqual(http.call_args.args[0], 'https://api.charliealpha.in/v1/mf/overlap')
        self.assertEqual(http.call_args.kwargs['params']['schemes'], '1,2')


# --- The workflow: official first, backup only for the fund that needs it ------------------------------------

class WorkflowTests(BackupTestCase):
    def test_with_the_backup_switched_off_nothing_changes_and_nothing_is_requested(self):
        official(A, {'2026-09': {E1: 40.0}})
        provider = FakeProvider({B['scheme_code']: {'2026-09-30': payload(B, '2026-09-30', complete({E1: 30.0}))}})
        with override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=False):
            result = self.analyze([A, B], provider)
        self.assertEqual(provider.calls, [])
        self.assertEqual(result['pairs'], [])
        fund_b = self.fund_of(result, '1002')
        self.assertEqual((fund_b['available'], fund_b['reason_code'], fund_b['status']), (False, 'SOURCE_NOT_SUPPORTED', 'SOURCE_NOT_SUPPORTED'))
        self.assertFalse(result['backup_enabled'])

    def test_official_holdings_never_trigger_the_backup(self):
        official(A, {'2026-09': {E1: 40.0}})
        official(C, {'2026-09': {E1: 30.0}})
        provider = FakeProvider()
        result = self.analyze([A, C], provider)
        self.assertEqual(provider.calls, [])
        self.assertEqual((result['pairs'][0]['overlap_pct'], result['pairs'][0]['holdings_source']), (30.0, 'official_amc'))

    def test_an_unsupported_fund_uses_the_backup_and_overlap_is_our_own_calculation(self):
        official(A, {'2026-09': {E1: 40.0, E2: 20.0, E3: 10.0}})
        provider = FakeProvider({'1002': {'2026-09-30': payload(B, '2026-09-30', complete({E1: 30.0, E2: 25.0, E4: 5.0}))}})
        result = self.analyze([A, B], provider)
        pair = result['pairs'][0]
        # min(40, 30) + min(20, 25). Nothing from the provider's own overlap is used.
        self.assertEqual(pair['overlap_pct'], 50.0)
        self.assertEqual((pair['common_count'], pair['portfolio_month'], pair['portfolio_date']), (2, '2026-09', '2026-09-30'))
        self.assertEqual((pair['left_source'], pair['right_source'], pair['holdings_source']), ('official_amc', 'charlie_alpha', 'mixed'))
        self.assertEqual((self.fund_of(result, '1001')['holdings_source'], self.fund_of(result, '1002')['holdings_source']), ('official_amc', 'charlie_alpha'))
        self.assertEqual((result['highest_overlap']['holdings_source'], result['holdings_sources']), ('mixed', ['charlie_alpha', 'official_amc']))
        self.assertFalse([path for path, _ in provider.calls if 'overlap' in path])

    def test_only_the_fund_that_needs_the_backup_is_requested(self):
        official(A, {'2026-09': {E1: 40.0}})
        official(C, {'2026-09': {E1: 35.0}})
        provider = FakeProvider({'1002': {'2026-09-30': payload(B, '2026-09-30', complete({E1: 30.0}))}})
        result = self.analyze([A, B, C], provider)
        self.assertEqual(provider.codes_asked(), {'1002'})
        by_pair = {(p['left']['scheme_code'], p['right']['scheme_code']): (p['overlap_pct'], p['holdings_source']) for p in result['pairs']}
        self.assertEqual(by_pair, {('1001', '1002'): (30.0, 'mixed'), ('1001', '1003'): (35.0, 'official_amc'), ('1002', '1003'): (30.0, 'mixed')})
        self.assertEqual((result['compared_pairs'], result['total_pairs']), (3, 3))

    def test_holdings_are_requested_only_for_the_month_needed(self):
        official(A, {'2026-09': {E1: 40.0}})
        provider = FakeProvider({'1002': {d: payload(B, d, complete({E1: 30.0})) for d in ('2026-10-31', '2026-09-30', '2026-08-31')}})
        self.analyze([A, B], provider)
        holdings_requests = [as_of for path, as_of in provider.calls if path.startswith('/mf/holdings/')]
        self.assertEqual(holdings_requests, ['2026-09-30'])

    def test_a_failed_official_file_falls_back_for_that_fund(self):
        official(A, {'2026-09': {E1: 40.0}})
        official(C, {'2026-09': {E1: 35.0}})
        broken = fund('1004', 'Delta Large Cap Fund')

        def links(_source, name):
            return [{'url': 'https://x.test/delta.xlsx', 'text': name, 'month': '2026-09'}] if name == broken['scheme_name'] else []

        provider = FakeProvider({'1004': {'2026-09-30': payload(broken, '2026-09-30', complete({E1: 20.0, E3: 50.0}))}})
        result = self.analyze([A, broken, C], provider, links={'side_effect': links}, download={'return_value': b'this is not a workbook'})
        self.assertEqual(self.fund_of(result, '1004')['holdings_source'], 'charlie_alpha')
        self.assertEqual(provider.codes_asked(), {'1004'})
        self.assertEqual(len(result['pairs']), 3)

    def test_the_backup_stage_is_reported(self):
        official(A, {'2026-09': {E1: 40.0}})
        stages = []
        provider = FakeProvider({'1002': {'2026-09-30': payload(B, '2026-09-30', complete({E1: 30.0}))}})
        self.analyze([A, B], provider, progress=stages.append)
        self.assertEqual(stages, ['fetching', 'backup', 'validating', 'calculating'])

    def test_no_stage_is_reported_when_official_data_is_enough(self):
        official(A, {'2026-09': {E1: 40.0}})
        official(C, {'2026-09': {E1: 35.0}})
        stages = []
        self.analyze([A, C], FakeProvider(), progress=stages.append)
        self.assertNotIn('backup', stages)


class MonthTests(BackupTestCase):
    def test_a_month_the_provider_lacks_is_never_replaced_by_another_inside_a_pair(self):
        official(A, {'2026-09': {E1: 40.0}})
        provider = FakeProvider({'1002': {d: payload(B, d, complete({E1: 30.0})) for d in ('2026-10-31', '2026-08-31')}})
        result = self.analyze([A, B], provider)
        self.assertEqual(result['pairs'], [])
        self.assertIsNone(result['highest_overlap']['percentage'])
        gap = result['pair_reasons'][0]
        self.assertEqual((gap['reason_code'], gap['left_portfolio_month'], gap['right_portfolio_month']), ('NO_COMMON_REPORTING_MONTH', '2026-09', '2026-10'))

    def test_another_mutually_available_month_is_found(self):
        official(A, {'2026-09': {E1: 40.0}, '2026-08': {E1: 25.0}})
        provider = FakeProvider({'1002': {d: payload(B, d, complete({E1: 30.0 if d.startswith('2026-08') else 99.0})) for d in ('2026-10-31', '2026-08-31')}})
        pair = self.analyze([A, B], provider)['pairs'][0]
        self.assertEqual((pair['portfolio_month'], pair['overlap_pct'], pair['holdings_source']), ('2026-08', 25.0, 'mixed'))

    def test_the_month_common_to_all_funds_is_used_across_mixed_sources(self):
        official(A, {'2026-10': {E1: 50.0}, '2026-09': {E1: 40.0}})
        official(C, {'2026-09': {E1: 20.0}, '2026-08': {E1: 5.0}})
        provider = FakeProvider({'1002': {'2026-09-30': payload(B, '2026-09-30', complete({E1: 30.0})), '2026-08-31': payload(B, '2026-08-31', complete({E1: 8.0, E3: 60.0}))}})
        result = self.analyze([A, B, C], provider)
        self.assertEqual({p['portfolio_month'] for p in result['pairs']}, {'2026-09'})
        self.assertEqual((result['common_month'], result['pairs_different_months']), ('2026-09', False))

    def test_pairs_use_different_months_when_no_month_is_common(self):
        official(A, {'2026-10': {E1: 50.0}})
        official(C, {'2026-09': {E1: 20.0}})
        provider = FakeProvider({'1002': {'2026-10-31': payload(B, '2026-10-31', complete({E1: 45.0})), '2026-09-30': payload(B, '2026-09-30', complete({E1: 10.0, E3: 60.0}))}})
        result = self.analyze([A, B, C], provider)
        months = {(p['left']['scheme_code'], p['right']['scheme_code']): (p['portfolio_month'], p['overlap_pct']) for p in result['pairs']}
        self.assertEqual(months, {('1001', '1002'): ('2026-10', 45.0), ('1002', '1003'): ('2026-09', 10.0)})
        self.assertTrue(result['pairs_different_months'])
        self.assertEqual((result['compared_pairs'], result['total_pairs'], result['partial_coverage']), (2, 3, True))


class RealRunRegressionTests(BackupTestCase):
    """Two faults found by running against the live API, kept so they cannot return."""

    def test_official_holdings_are_kept_when_only_a_newer_official_month_failed(self):
        # HDFC-like fund: official September is valid, but its October file is not published yet.
        # The backup has October. October third-party data must not displace official September.
        official(C, {'2026-09': {E1: 35.0}})
        links = {'side_effect': lambda _s, name: [{'url': 'https://x.test/gamma-oct.xlsx', 'text': name, 'month': '2026-10'}] if name == C['scheme_name'] else []}
        provider = FakeProvider({
            '1002': {d: payload(B, d, complete({E1: 30.0, E3: 40.0})) for d in ('2026-10-31', '2026-09-30')},
            '1003': {'2026-10-31': payload(C, '2026-10-31', complete({E1: 99.0}))},
        })
        result = self.analyze([B, C], provider, links=links, download={'return_value': b'not published'})
        pair = result['pairs'][0]
        self.assertEqual((pair['portfolio_month'], pair['overlap_pct'], pair['right_source']), ('2026-09', 30.0, 'official_amc'))
        self.assertNotIn('1003', provider.codes_asked())

    def test_mid_month_snapshots_are_never_used(self):
        # The provider also keeps fortnightly (15th) snapshots. Only month-end portfolios are compared.
        official(A, {'2026-10': {E1: 50.0}, '2026-09': {E1: 40.0}})
        provider = FakeProvider({'1002': {
            '2026-10-15': payload(B, '2026-10-15', complete({E1: 45.0})),
            '2026-09-30': payload(B, '2026-09-30', complete({E1: 30.0, E3: 40.0})),
        }})
        result = self.analyze([A, B], provider)
        pair = result['pairs'][0]
        self.assertEqual((pair['portfolio_month'], pair['right_portfolio_date'], pair['overlap_pct']), ('2026-09', '2026-09-30', 30.0))
        self.assertNotIn('2026-10-15', [as_of for _, as_of in provider.calls])


class FailureTests(BackupTestCase):
    def test_both_sources_unavailable_is_unavailable_never_zero(self):
        official(A, {'2026-09': {E1: 40.0}})
        result = self.analyze([A, B], FakeProvider({}))
        fund_b = self.fund_of(result, '1002')
        self.assertEqual((fund_b['available'], fund_b['message'], fund_b['backup_attempted']), (False, ALL_SOURCES, True))
        self.assertEqual((result['pairs'], result['highest_overlap']['percentage'], result['highest_overlap']['message']), ([], None, ALL_SOURCES))
        self.assertEqual(result['pair_reasons'][0]['message'], ALL_SOURCES)

    def test_a_provider_outage_or_rate_limit_is_survived(self):
        official(A, {'2026-09': {E1: 40.0}})
        for error in (CharlieError(ca.RATE_LIMITED), CharlieError(ca.TIMEOUT), CharlieError(ca.HTTP_ERROR), CharlieError(ca.BUDGET_EXHAUSTED)):
            result = self.analyze([A, B], FakeProvider(error=error))
            self.assertEqual((result['pairs'], result['highest_overlap']['percentage'], result['highest_overlap']['message']), ([], None, ALL_SOURCES), error.code)
            self.assertTrue(self.fund_of(result, '1001')['available'])

    def test_an_outage_costs_one_fund_not_every_fund(self):
        official(A, {'2026-09': {E1: 40.0}})
        other = fund('1005', 'Epsilon Large Cap Fund', amc='Nowhere Mutual Fund')
        with patch('fund_analyzer.charlie_alpha.requests.get', side_effect=requests.Timeout('slow')) as http, patch('fund_analyzer.charlie_alpha.time.sleep'):
            with (
                patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[A, B, other]),
                patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
                patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
            ):
                result = analyze_holdings_overlap(['1001', '1002', '1005'])
        self.assertEqual(http.call_count, ca.MAX_ATTEMPTS)
        self.assertEqual([self.fund_of(result, code)['message'] for code in ('1002', '1005')], [ALL_SOURCES, ALL_SOURCES])

    def test_data_that_fails_validation_is_refused_saved_nowhere_and_never_zero(self):
        official(A, {'2026-09': {E1: 40.0}})
        top_ten_only = [row(E1, 9.0), row(E2, 8.0)]
        result = self.analyze([A, B], FakeProvider({'1002': {'2026-09-30': payload(B, '2026-09-30', top_ten_only)}}))
        self.assertEqual((result['pairs'], result['highest_overlap']['percentage']), ([], None))
        self.assertEqual(self.fund_of(result, '1002')['message'], ALL_SOURCES)
        self.assertIsNone(read_snapshot(B, '2026-09'))

    def test_a_portfolio_for_the_wrong_scheme_is_refused(self):
        official(A, {'2026-09': {E1: 40.0}})
        wrong = payload(B, '2026-09-30', complete({E1: 30.0}), amfi_code='4242')
        result = self.analyze([A, B], FakeProvider({'1002': {'2026-09-30': wrong}}))
        self.assertEqual(result['pairs'], [])
        self.assertIsNone(read_snapshot(B, '2026-09'))

    def test_rows_from_two_providers_are_never_combined(self):
        # Official rows exist for A in September but only a top-10 list is on offer from the backup. Nothing is merged.
        official(A, {'2026-09': {E1: 40.0}})
        result = self.analyze([A, B], FakeProvider({'1002': {'2026-09-30': payload(B, '2026-09-30', [row(E1, 20.0)])}}))
        self.assertEqual(result['pairs'], [])


class CacheTests(BackupTestCase):
    def setUp(self):
        super().setUp()
        official(A, {'2026-09': {E1: 40.0, E2: 20.0}})
        self.provider = FakeProvider({'1002': {'2026-09-30': payload(B, '2026-09-30', complete({E1: 30.0, E2: 25.0}))}})

    def test_a_backup_portfolio_is_saved_as_identifiable_third_party_json(self):
        self.analyze([A, B], self.provider)
        saved = read_snapshot(B, '2026-09')
        self.assertEqual((saved['source_type'], saved['third_party'], saved['reporting_date'], saved['validation_status']), (SOURCE_CHARLIE, True, '2026-09-30', 'VERIFIED'))
        self.assertTrue(saved['source_url'].startswith('https://api.charliealpha.in/v1/mf/holdings/1002'))
        self.assertEqual((saved['validation']['status'], saved['validation']['provider']), ('VERIFIED', 'charlie_alpha'))
        self.assertIn('1002', saved['scheme_codes'])
        self.assertTrue(saved['portfolio_key'] and saved['imported_at'])
        self.assertEqual({row['isin'] for row in saved['holdings']}, {E1, E2})
        self.assertTrue(stored_links(B)[0]['third_party'])

    def test_the_saved_copy_answers_the_next_analysis_without_the_api(self):
        self.analyze([A, B], self.provider)
        ca.clear_memory()
        again = FakeProvider()
        result = self.analyze([A, B], again)
        self.assertEqual(again.calls, [])
        self.assertEqual((result['pairs'][0]['overlap_pct'], self.fund_of(result, '1002')['holdings_source']), (50.0, 'charlie_alpha'))

    def test_a_saved_third_party_copy_is_not_served_when_the_backup_is_off(self):
        self.analyze([A, B], self.provider)
        with override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=False):
            result = self.analyze([A, B], FakeProvider())
        self.assertEqual(result['pairs'], [])
        self.assertFalse(self.fund_of(result, '1002')['available'])

    def test_third_party_data_never_replaces_official_data_for_a_month(self):
        official(B, {'2026-09': {E1: 77.0}})
        store_snapshot(B, '1002', '2026-09', {**holdings_result({E1: 5.0}, '2026-09-30'), 'asset_class': 'equity'}, source_type=SOURCE_CHARLIE, validation={'status': 'VERIFIED'})
        saved = read_snapshot(B, '2026-09')
        self.assertEqual((saved['source_type'], saved['holdings'][0]['weight']), ('download', 77.0))
        self.assertNotIn('third_party', saved)

    def test_official_data_replaces_a_saved_third_party_copy(self):
        store_snapshot(B, '1002', '2026-09', {**holdings_result({E1: 5.0}, '2026-09-30'), 'asset_class': 'equity'}, source_type=SOURCE_CHARLIE, validation={'status': 'VERIFIED'})
        self.assertEqual(read_snapshot(B, '2026-09')['source_type'], SOURCE_CHARLIE)
        official(B, {'2026-09': {E1: 77.0}})
        saved = read_snapshot(B, '2026-09')
        self.assertEqual((saved['source_type'], saved['holdings'][0]['weight']), ('download', 77.0))
        self.assertNotIn('third_party', saved)

    def test_official_files_are_tried_before_a_saved_third_party_copy_of_the_same_month(self):
        store_snapshot(B, '1002', '2026-09', {**holdings_result({E1: 5.0}, '2026-09-30'), 'asset_class': 'equity'}, source_type=SOURCE_CHARLIE, validation={'status': 'VERIFIED'})
        from .tests_pipeline import portfolio_file
        workbook = portfolio_file(B['scheme_name'], [(E1, 30.0), (E2, 25.0)])
        links = {'side_effect': lambda _s, name: [{'url': 'https://x.test/beta.xlsx', 'text': name, 'month': '2026-09'}] if name == B['scheme_name'] else []}
        hdfc_like = fund('1002', 'Beta Large Cap Fund')           # an AMC the official reader supports
        result = self.analyze([A, hdfc_like], FakeProvider(), links=links, download={'return_value': workbook})
        self.assertEqual((self.fund_of(result, '1002')['holdings_source'], result['pairs'][0]['holdings_source']), ('official_amc', 'official_amc'))


class FundamentalsTests(BackupTestCase):
    def test_fundamentals_label_the_source_and_use_the_backup_for_an_unsupported_fund(self):
        from .holdings import fetch_fund_holdings
        provider = FakeProvider({'1002': {'2026-09-30': payload(B, '2026-09-30', complete({E1: 30.0, E2: 25.0}))}})
        with patch.object(ca, '_get_json', provider), patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT):
            details = fetch_fund_holdings('1002', B)
        self.assertEqual((details['holdings_source'], details['holdings_as_of']), ('charlie_alpha', '2026-09-30'))
        self.assertEqual([item['isin'] for item in details['holdings']], [E1, E2])

    def test_fundamentals_for_an_official_fund_never_ask_the_backup(self):
        from .holdings import fetch_fund_holdings
        official(A, {'2026-09': {E1: 40.0}})
        provider = FakeProvider()
        with patch.object(ca, '_get_json', provider), patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT), patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]):
            details = fetch_fund_holdings('1001', A)
        self.assertEqual((provider.calls, details['holdings_source']), ([], 'official_amc'))


class FlagTests(SimpleTestCase):
    def test_the_backup_is_off_unless_it_is_switched_on(self):
        from django.conf import settings
        self.assertFalse(settings.FUND_ANALYZER_CHARLIE_ALPHA_ENABLED)
        self.assertFalse(ca.enabled())
