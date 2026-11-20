"""Compare with many funds: one fund that cannot be loaded never fails the others.

20 Nov 2026, a client compared 36 funds. AMFI answered the burst with 429 Too Many Requests, and for
UTI Dividend Yield Fund - Regular - Growth (103026) the MFAPI backup holds the IDCW option, which is
rightly refused. That single fund made the whole comparison answer "Fund data is temporarily unavailable".
Three other funds (117560, 104685, 100669) lost their figures over one or two zero NAV rows from 2007/2013.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from . import services
from .providers import ProviderError
from .services import NAV_HISTORY_UNAVAILABLE, _metrics_or_reason, _priced_history, compare_funds

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'compare-resilience'}}


def record(code, name):
    return {'scheme_code': code, 'scheme_name': name, 'amc': 'Test Mutual Fund', 'category': 'Equity Scheme - Flexi Cap Fund', 'plan': 'Regular Plan', 'option': 'Growth', 'nav': 100.0, 'date': '19-Nov-2026', 'isin': 'INF000T01011'}


def history(days=800, start_nav=100.0, step=0.0005):
    first = date(2026, 11, 19) - timedelta(days=days)
    return [{'scheme_code': 'x', 'date': (first + timedelta(days=i)).isoformat(), 'nav': round(start_nav * (1 + step) ** i, 4)} for i in range(days + 1)]


RECORDS = [record('1001', 'Alpha Flexi Cap Fund'), record('1002', 'Beta Flexi Cap Fund'), record('103026', 'UTI - Dividend Yield Fund')]


def fake_fund(code, **kwargs):
    if code == '103026':
        raise ProviderError('Unable to load scheme 103026 from AMFI or MFAPI')
    rows = history()
    result = services._base_detail(next(r for r in RECORDS if r['scheme_code'] == code), source='AMFI', as_of='19-Nov-2026')
    result.update({'ter': None, 'ter_as_of': None, 'aum': None, 'aum_unit': None, 'aum_as_of': None, 'history': rows})
    result['metrics'], result['metrics_unavailable'] = _metrics_or_reason(rows, 0.065, code)
    return result


@override_settings(CACHES=LOCMEM)
class OneFundFailingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        for starter in (
            patch('fund_analyzer.services.fetch_amfi_latest', return_value=RECORDS),
            patch('fund_analyzer.services.fetch_fund_data', side_effect=fake_fund),
            patch('fund_analyzer.services.fetch_amfi_ter_snapshot', return_value={}),
        ):
            starter.start()
            self.addCleanup(starter.stop)

    def test_the_other_funds_are_compared_and_the_failed_one_says_why(self):
        result = compare_funds(['1001', '1002', '103026'])
        funds = {fund['scheme_code']: fund for fund in result['funds']}
        self.assertEqual(list(funds), ['1001', '1002', '103026'])  # order kept, nothing dropped
        self.assertIsNotNone(funds['1001']['metrics'])
        self.assertIsNotNone(funds['1002']['metrics'])
        failed = funds['103026']
        self.assertEqual((failed['metrics'], failed['history'], failed['metrics_unavailable']), (None, [], NAV_HISTORY_UNAVAILABLE))
        self.assertEqual(failed['scheme_name'], 'UTI - Dividend Yield Fund')  # named from the AMFI list
        self.assertIsNone(result['correlation']['103026']['1001'])

    def test_compare_endpoint_answers_200_not_temporarily_unavailable(self):
        reply = APIClient().post('/api/compare', {'scheme_codes': ['1001', '1002', '103026']}, format='json')
        self.assertEqual(reply.status_code, 200, reply.content)
        rows = {fund['scheme_code']: fund for fund in reply.json()['funds']}
        self.assertEqual(rows['103026']['metrics_unavailable'], NAV_HISTORY_UNAVAILABLE)
        self.assertIsNotNone(rows['1001']['metrics'])

    def test_only_when_every_fund_fails_is_the_comparison_unavailable(self):
        with patch('fund_analyzer.services.fetch_fund_data', side_effect=ProviderError('down')):
            with self.assertRaises(ProviderError):
                compare_funds(['1001', '1002'])
            reply = APIClient().post('/api/compare', {'scheme_codes': ['1001', '1002']}, format='json')
        self.assertEqual((reply.status_code, reply.json()['detail']), (503, 'Fund data is temporarily unavailable. Please try again shortly.'))


class ZeroNavRowsTests(SimpleTestCase):
    def test_an_old_zero_row_in_a_live_fund_is_ignored_and_metrics_are_calculated(self):
        rows = history()
        rows[5] = {**rows[5], 'nav': 0.0}  # one bad row years ago, as MFAPI has for 117560, 104685, 100669
        priced = _priced_history(rows, '117560')
        self.assertEqual(len(priced), len(rows) - 1)
        metrics, reason = _metrics_or_reason(priced, 0.065, '117560')
        self.assertIsNotNone(metrics)
        self.assertIsNone(reason)
        # The same figures as a history that never had the bad row.
        self.assertEqual(metrics, _metrics_or_reason([row for i, row in enumerate(rows) if i != 5], 0.065, '117560')[0])

    def test_a_scheme_whose_latest_nav_is_zero_still_gets_no_metrics(self):
        rows = history()
        rows[-1] = {**rows[-1], 'nav': 0.0}  # a segregated portfolio publishes 0
        self.assertEqual(_priced_history(rows, '148304'), rows)
        metrics, reason = _metrics_or_reason(_priced_history(rows, '148304'), 0.065, '148304')
        self.assertIsNone(metrics)
        self.assertIn('zero NAV', reason)


@override_settings(CACHES=LOCMEM)
class AmfiBusyTests(SimpleTestCase):
    """A 429 from AMFI's NAV-history service sends the next funds straight to the backup for a while."""

    def setUp(self):
        cache.clear()

    def refused(self):
        from .providers import SourceFailure

        return SourceFailure('429 Client Error: Too Many Requests', evidence={'http_status': 429, 'retry_after': None})

    def test_after_a_429_other_schemes_skip_amfi_until_the_wait_ends(self):
        from . import providers

        start, end = date(2021, 11, 1), date(2026, 11, 5)
        with patch('fund_analyzer.providers.fetch_url', side_effect=self.refused()) as fetch:
            with self.assertRaises(ProviderError):
                providers.fetch_amfi_history('119018', start, end)
            sent = fetch.call_count
            for code in ('128629', '103026', '140225'):
                with self.assertRaisesRegex(ProviderError, 'busy'):
                    providers.fetch_amfi_history(code, start, end)
            self.assertEqual(fetch.call_count, sent)  # one refusal, no further requests
        cache.delete(providers.AMFI_HISTORY_BUSY_KEY)
        with patch('fund_analyzer.providers.fetch_url', side_effect=self.refused()) as fetch:
            with self.assertRaises(ProviderError):
                providers.fetch_amfi_history('128629', start, end)
            self.assertTrue(fetch.called)  # asked again once the wait is over

    def test_a_cached_history_is_still_served_while_amfi_is_busy(self):
        from . import providers

        start, end = date(2021, 11, 1), date(2026, 11, 5)
        rows = [{'date': '2026-11-05', 'nav': 10.0}]
        cache.set(f'fund-analyzer:amfi:history:119018:{start.isoformat()}:{end.isoformat()}', rows)
        cache.set(providers.AMFI_HISTORY_BUSY_KEY, True)
        self.assertEqual(providers.fetch_amfi_history('119018', start, end), rows)


@override_settings(CACHES=LOCMEM)
class CompareResponseSizeTests(SimpleTestCase):
    """Vercel refuses a response over 4.5 MB. 36 funds were 10.9 MB, almost all of it two unused daily series."""

    def setUp(self):
        cache.clear()
        for starter in (
            patch('fund_analyzer.services.fetch_amfi_latest', return_value=RECORDS),
            patch('fund_analyzer.services.fetch_fund_data', side_effect=fake_fund),
            patch('fund_analyzer.services.fetch_amfi_ter_snapshot', return_value={}),
        ):
            starter.start()
            self.addCleanup(starter.stop)

    def test_compare_leaves_out_the_daily_chart_series_but_keeps_every_figure(self):
        reply = APIClient().post('/api/compare', {'scheme_codes': ['1001', '1002']}, format='json')
        self.assertEqual(reply.status_code, 200)
        metrics = reply.json()['funds'][0]['metrics']
        self.assertNotIn('growthOf10000', metrics)
        self.assertNotIn('rollingReturns', metrics)
        expected = _metrics_or_reason(history(), 0.065, '1001')[0]
        for key in ('r1m', 'r3m', 'r6m', 'r1y', 'r3y', 'r5y', 'volatility', 'sharpe', 'maxDrawdown'):
            self.assertEqual(metrics.get(key), expected.get(key), key)
        self.assertLess(len(reply.content), 100_000)
