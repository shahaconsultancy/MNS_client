"""Scheme-to-sheet identification: spelling differences, ambiguity, and retrying failures from an older matcher."""
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
from django.test import SimpleTestCase, override_settings

from . import portfolio_store as ps
from .adapters import AMBIGUOUS_MAPPING, classify_error, comparable_name, normalise_name, scheme_link_matches
from .holdings import ProviderError, _locate_holdings_table, _scheme_title, _sheet_name_score
from .portfolio_store import MATCHER_VERSION, _portfolio_dir, _write_json_atomic, discovery_due

SBI_SMALL = {'amc': 'SBI Mutual Fund', 'scheme_name': 'SBI SMALL CAP FUND', 'scheme_code': '125497'}


def sheet(title):
    rows = [
        [None, None, None, None],
        ['AMC', '1', None, None],
        ['SCHEME NAME :', title, None, None],
        ['PORTFOLIO STATEMENT AS ON :', '2026-10-31', None, None],
        [None, None, None, None],
        ['Name of the Instrument / Issuer', 'ISIN', 'Rating / Industry^', '% to AUM'],
        ['Ather Energy Ltd.', 'INE0LEZ01016', 'Automobiles', 4.48],
        ['City Union Bank Ltd.', 'INE491A01021', 'Banks', 2.86],
    ]
    return pd.DataFrame(rows)


def read(sheets, name='SBI SMALL CAP FUND', diagnostics=None):
    return _locate_holdings_table(b'', 'https://x.test/a.xlsx', name, diagnostics, (sheets, {}))


class NameComparisonTests(SimpleTestCase):
    def test_spelling_variants_of_the_same_scheme_compare_equal(self):
        pairs = [
            ('SBI SMALL CAP FUND', 'SBI Smallcap Fund'), ('HDFC Mid Cap Fund', 'HDFC Midcap Fund'),
            ('HDFC Mid-Cap Fund', 'HDFC Mid Cap Fund'), ('ICICI Prudential Large Cap Fund', 'ICICI Prudential Largecap Fund'),
            ('DSP Flexi Cap Fund', 'DSP Flexicap Fund'), ('Kotak Multi Cap Fund', 'Kotak Multicap Fund'),
            ('Nippon India Small Cap Fund', 'NIPPON INDIA SMALL-CAP FUND'),
            ('Invesco India Banking & PSU Fund', 'Invesco India Banking and PSU Fund'),
        ]
        for left, right in pairs:
            with self.subTest(left=left):
                self.assertEqual(comparable_name(left), comparable_name(right))

    def test_plan_and_option_words_are_not_part_of_the_name(self):
        self.assertEqual(comparable_name('SBI Small Cap Fund - Direct Plan - Growth'), comparable_name('SBI Smallcap Fund'))
        self.assertEqual(comparable_name('HDFC Mid Cap Fund - Regular Plan - IDCW'), comparable_name('HDFC Mid Cap Fund'))

    def test_meaningful_differences_are_kept(self):
        different = [
            ('SBI Small Cap Fund', 'SBI Nifty Smallcap 250 Index Fund'),
            ('SBI Banking & PSU Fund', 'SBI Banking and Financial Services Fund'),
            ('Nippon India Small Cap Fund', 'Nippon India Large Cap Fund'), ('Axis Small Cap Fund', 'Axis Midcap Fund'),
        ]
        for left, right in different:
            with self.subTest(left=left, right=right):
                self.assertNotEqual(comparable_name(left), comparable_name(right))
                self.assertFalse(scheme_link_matches(left, right) and scheme_link_matches(right, left))

    def test_cache_keys_still_use_the_original_normaliser(self):
        self.assertEqual(normalise_name('HDFC Large Cap Fund'), 'hdfc large cap fund')


class SheetSelectionTests(SimpleTestCase):
    def test_amfi_name_finds_the_amc_spelling(self):
        self.assertEqual(_sheet_name_score('SSCF', sheet('SBI Smallcap Fund'), 'SBI SMALL CAP FUND'), 3.5)
        diagnostics = {}
        read({'SSCF': sheet('SBI Smallcap Fund')}, diagnostics=diagnostics)
        self.assertEqual(diagnostics['matched_sheet_name'], 'SSCF')

    def test_a_different_scheme_with_a_similar_name_is_not_chosen(self):
        self.assertLess(_sheet_name_score('SNS250IF', sheet('SBI Nifty Smallcap 250 Index Fund'), 'SBI SMALL CAP FUND'), 2.5)

    def test_scheme_title_reads_the_scheme_name_row(self):
        self.assertEqual(_scheme_title(sheet('SBI Smallcap Fund')), 'sbi smallcap fund')

    def test_exact_title_beats_a_longer_name_that_contains_it(self):
        exact = _sheet_name_score('A1', sheet('HDFC Mid Cap Fund'), 'HDFC Mid Cap Fund')
        longer = _sheet_name_score('A2', sheet('HDFC Mid Cap Fund Series 2'), 'HDFC Mid Cap Fund')
        self.assertGreater(exact, longer)

    def test_two_different_schemes_at_the_same_level_are_refused_not_guessed(self):
        sheets = {'X1': sheet('HDFC Mid Cap Fund Series 1'), 'X2': sheet('HDFC Mid Cap Fund Series 2')}
        diagnostics = {}
        with self.assertRaises(ProviderError) as caught:
            read(sheets, 'HDFC Mid Cap Fund', diagnostics)
        self.assertEqual(classify_error(caught.exception), AMBIGUOUS_MAPPING)
        self.assertEqual(sorted(diagnostics['ambiguous_candidates']), ['X1', 'X2'])

    def test_the_same_scheme_on_two_sheets_is_not_ambiguous(self):
        read({'S1': sheet('SBI Smallcap Fund'), 'S2': sheet('SBI Smallcap Fund')})

    def test_a_workbook_that_never_names_the_scheme_is_still_refused(self):
        with self.assertRaises(ProviderError):
            read({'Z': sheet('Axis Bluechip Fund')})


class StaleFailureTests(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        override = override_settings(FUND_ANALYZER_HOLDINGS_CACHE_DIR=self.dir.name)
        override.enable()
        self.addCleanup(override.disable)

    def write(self, status='SCHEME_NOT_FOUND', **extra):
        now = datetime.now(timezone.utc)
        _write_json_atomic(_portfolio_dir(SBI_SMALL) / 'status.json', {
            'status': status, 'next_check_at': (now + timedelta(days=1)).isoformat(), **extra,
        })

    def test_failure_from_an_older_matcher_is_retried_immediately(self):
        self.write()
        self.assertTrue(discovery_due(SBI_SMALL))

    def test_failure_from_the_current_matcher_still_waits(self):
        self.write(matcher_version=MATCHER_VERSION)
        self.assertFalse(discovery_due(SBI_SMALL))

    def test_recording_a_failure_stamps_the_matcher_version(self):
        ps.record_status(SBI_SMALL, 'SCHEME_NOT_FOUND', 'x')
        self.assertFalse(discovery_due(SBI_SMALL))

    def test_success_replaces_the_failure_status(self):
        self.write()
        ps.record_status(SBI_SMALL, 'HOLDINGS_AVAILABLE', '', '2026-10')
        self.assertEqual(ps.source_status(SBI_SMALL), 'HOLDINGS_AVAILABLE')

    def test_a_network_failure_is_not_retried_just_because_of_the_matcher(self):
        self.write(status='DOWNLOAD_BLOCKED')
        self.assertFalse(discovery_due(SBI_SMALL))
