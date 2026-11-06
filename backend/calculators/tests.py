from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

URL = '/api/calculators/inflation/'


class InflationViewTests(APITestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='advisor@example.com', password='pass12345')
        self.client.force_authenticate(user)

    def test_example_from_spec(self):
        response = self.client.get(URL, {'present_cost': 100000, 'rate': 6, 'years': 15})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(round(data['future_cost']), 239656)
        self.assertEqual(round(data['cost_increase']), 139656)
        self.assertEqual(data['times_cost'], 2.40)
        self.assertEqual(data['factor'], 2.3966)
        self.assertEqual(data['half_way_year'], 8)
        self.assertEqual(round(data['half_way_cost']), 159385)
        self.assertEqual(round(data['purchasing_power_of_lakh']), 41727)
        self.assertEqual(len(data['chart']), 16)
        self.assertEqual(data['chart'][0], {'year': 0, 'cost': 100000.0})
        self.assertEqual(round(data['chart'][8]['cost']), 159385)  # a middle year
        self.assertEqual(data['chart'][-1]['cost'], data['future_cost'])

    def test_zero_years_and_zero_rate(self):
        data = self.client.get(URL, {'present_cost': 50000, 'rate': 0, 'years': 0}).json()

        self.assertEqual(data['future_cost'], 50000.0)
        self.assertEqual(data['cost_increase'], 0.0)
        self.assertEqual(data['chart'], [{'year': 0, 'cost': 50000.0}])

    def test_zero_present_cost(self):
        response = self.client.get(URL, {'present_cost': 0, 'rate': 6, 'years': 15})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['future_cost'], 0.0)
        # The else branch: prices still rise 2.40x even when today's cost is 0.
        self.assertEqual(response.json()['times_cost'], 2.40)

    def test_invalid_inputs_return_field_errors(self):
        response = self.client.get(URL, {'present_cost': -1, 'rate': 101, 'years': 101})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {'present_cost', 'rate', 'years'})

    def test_missing_and_non_numeric_inputs(self):
        response = self.client.get(URL, {'present_cost': 'abc', 'rate': 6})

        self.assertEqual(response.status_code, 400)
        self.assertIn('present_cost', response.json())
        self.assertIn('years', response.json())

    def test_requires_login(self):
        self.client.force_authenticate(None)
        response = self.client.get(URL, {'present_cost': 100000, 'rate': 6, 'years': 15})

        self.assertEqual(response.status_code, 401)


GOAL_URL = '/api/calculators/goal/'


class GoalViewTests(APITestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='advisor@example.com', password='pass12345')
        self.client.force_authenticate(user)

    def test_example_from_spec(self):
        response = self.client.get(GOAL_URL, {
            'goal_cost': 2000000, 'inflation': 6, 'years': 10, 'return_rate': 12, 'savings': 200000,
        })

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(round(data['future_goal_value']), 3581695)  # Step 1
        self.assertEqual(round(data['savings_grown']), 621170)       # Step 2
        self.assertEqual(round(data['shortfall']), 2960526)          # Step 3
        self.assertEqual(data['monthly_sip'], 12742)                 # Step 4
        self.assertEqual(data['months'], 120)
        self.assertEqual(data['total_invested'], 1529040)            # Step 5
        self.assertEqual(round(data['growth_earned']), 1852655)
        self.assertEqual(len(data['chart']), 11)
        self.assertEqual(data['chart'][0], {'year': 0, 'corpus': 200000.0, 'goal': 2000000.0})
        # A middle year: SIP for 60 months + savings grown 5 years, against the goal inflated 5 years.
        self.assertEqual(round(data['chart'][5]['corpus']), 1403531)
        self.assertEqual(round(data['chart'][5]['goal']), 2676451)
        # The corpus should reach the inflated goal in the final year.
        self.assertAlmostEqual(data['chart'][-1]['corpus'], data['chart'][-1]['goal'], delta=1)

    def test_second_example(self):
        # Different inputs, so the formula can't be right for one example by luck.
        data = self.client.get(GOAL_URL, {
            'goal_cost': 500000, 'inflation': 7, 'years': 5, 'return_rate': 10, 'savings': 50000,
        }).json()

        self.assertEqual(round(data['future_goal_value']), 701276)
        self.assertEqual(round(data['savings_grown']), 80526)
        self.assertEqual(round(data['shortfall']), 620750)
        self.assertEqual(data['monthly_sip'], 7950)
        self.assertEqual(data['months'], 60)
        self.assertEqual(data['total_invested'], 477000)
        self.assertEqual(round(data['growth_earned']), 174276)

    def test_zero_return_splits_shortfall_evenly(self):
        data = self.client.get(GOAL_URL, {
            'goal_cost': 1200000, 'inflation': 0, 'years': 1, 'return_rate': 0, 'savings': 0,
        }).json()

        self.assertEqual(data['monthly_sip'], 100000)

    def test_savings_already_cover_goal(self):
        data = self.client.get(GOAL_URL, {
            'goal_cost': 100000, 'inflation': 0, 'years': 5, 'return_rate': 10, 'savings': 100000,
        }).json()

        self.assertEqual(data['shortfall'], 0.0)
        self.assertEqual(data['monthly_sip'], 0.0)
        self.assertEqual(data['total_invested'], 0.0)

    def test_zero_years_is_rejected(self):
        response = self.client.get(GOAL_URL, {
            'goal_cost': 2000000, 'inflation': 6, 'years': 0, 'return_rate': 12, 'savings': 0,
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('years', response.json())

    def test_invalid_and_missing_inputs(self):
        response = self.client.get(GOAL_URL, {'goal_cost': -1, 'inflation': 101})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {'goal_cost', 'inflation', 'years', 'return_rate', 'savings'})

    def test_requires_login(self):
        self.client.force_authenticate(None)
        response = self.client.get(GOAL_URL, {
            'goal_cost': 2000000, 'inflation': 6, 'years': 10, 'return_rate': 12, 'savings': 200000,
        })

        self.assertEqual(response.status_code, 401)


SIP_URL = '/api/calculators/sip/'


class SipViewTests(APITestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='advisor@example.com', password='pass12345')
        self.client.force_authenticate(user)

    def test_example_from_spec(self):
        response = self.client.get(SIP_URL, {'monthly_sip': 25000, 'return_rate': 12, 'years': 15})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['months'], 180)                        # Step 1
        self.assertEqual(round(data['maturity_value']), 12614400)    # Step 2
        self.assertEqual(data['total_invested'], 4500000)            # Step 3
        self.assertEqual(round(data['gains']), 8114400)              # Step 4
        self.assertEqual(data['wealth_multiple'], 2.80)              # Step 5
        self.assertEqual(round(data['gains_share_percent'], 1), 64.3)
        self.assertEqual(round(data['gains_percent_of_invested']), 180)
        # Chart: start, a middle year, and the end.
        self.assertEqual(len(data['chart']), 16)
        self.assertEqual(data['chart'][0], {'year': 0, 'value': 0.0, 'invested': 0.0})
        self.assertEqual(round(data['chart'][5]['value']), 2062159)
        self.assertEqual(data['chart'][5]['invested'], 1500000)
        self.assertEqual(data['chart'][-1]['value'], data['maturity_value'])

    def test_second_example(self):
        data = self.client.get(SIP_URL, {'monthly_sip': 10000, 'return_rate': 10, 'years': 20}).json()

        self.assertEqual(round(data['maturity_value']), 7656969)
        self.assertEqual(data['total_invested'], 2400000)
        self.assertEqual(round(data['gains']), 5256969)
        self.assertEqual(data['wealth_multiple'], 3.19)

    def test_zero_return_means_no_gains(self):
        data = self.client.get(SIP_URL, {'monthly_sip': 10000, 'return_rate': 0, 'years': 2}).json()

        self.assertEqual(data['maturity_value'], 240000)
        self.assertEqual(data['gains'], 0)
        self.assertEqual(data['wealth_multiple'], 1.0)

    def test_zero_sip_and_zero_years_are_rejected(self):
        response = self.client.get(SIP_URL, {'monthly_sip': 0, 'return_rate': 12, 'years': 0})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {'monthly_sip', 'years'})

    def test_missing_inputs(self):
        response = self.client.get(SIP_URL, {})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {'monthly_sip', 'return_rate', 'years'})

    def test_requires_login(self):
        self.client.force_authenticate(None)
        response = self.client.get(SIP_URL, {'monthly_sip': 25000, 'return_rate': 12, 'years': 15})

        self.assertEqual(response.status_code, 401)


SWP_URL = '/api/calculators/swp/'


class SwpViewTests(APITestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='advisor@example.com', password='pass12345')
        self.client.force_authenticate(user)

    def test_example_from_spec(self):
        response = self.client.get(SWP_URL, {
            'corpus': 5000000, 'monthly_withdrawal': 30000, 'return_rate': 9, 'years': 20,
        })

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['months'], 240)                          # Step 1
        self.assertEqual(round(data['ending_balance']), 10009152)      # Step 2
        self.assertEqual(data['total_withdrawn'], 7200000)             # Step 3
        self.assertEqual(data['annual_withdrawal'], 360000)            # Step 4
        self.assertEqual(data['withdrawal_rate'], 7.2)
        self.assertEqual(data['ending_multiple'], 2.0)
        self.assertEqual(data['sustainable_withdrawal'], 37500)        # Step 5
        self.assertFalse(data['depleted'])
        self.assertIsNone(data['depletion_month'])
        # Chart: start, a middle year, and the end.
        self.assertEqual(len(data['chart']), 21)
        self.assertEqual(data['chart'][0], {'year': 0, 'balance': 5000000.0})
        self.assertEqual(round(data['chart'][10]['balance']), 6451357)
        self.assertEqual(data['chart'][-1]['balance'], data['ending_balance'])

    def test_corpus_runs_out(self):
        # W = 10,000 is more than P x i = 7,500, so the balance falls to 0 in month 186.
        data = self.client.get(SWP_URL, {
            'corpus': 1000000, 'monthly_withdrawal': 10000, 'return_rate': 9, 'years': 20,
        }).json()

        self.assertTrue(data['depleted'])
        self.assertEqual(data['depletion_month'], 186)
        self.assertEqual(data['ending_balance'], 0)
        # Only what was actually paid out, not W x N = 24,00,000.
        self.assertEqual(round(data['total_withdrawn']), 1855325)
        self.assertEqual(data['ending_multiple'], 0)
        self.assertEqual(round(data['chart'][10]['balance']), 516214)
        # The chart stops at 0 instead of going negative.
        self.assertEqual(data['chart'][16]['balance'], 0)
        self.assertEqual(data['chart'][-1]['balance'], 0)

    def test_zero_return_runs_out_after_corpus_divided_by_withdrawal(self):
        data = self.client.get(SWP_URL, {
            'corpus': 1200000, 'monthly_withdrawal': 10000, 'return_rate': 0, 'years': 20,
        }).json()

        self.assertTrue(data['depleted'])
        self.assertEqual(data['depletion_month'], 120)
        self.assertEqual(data['total_withdrawn'], 1200000)
        self.assertEqual(data['sustainable_withdrawal'], 0)

    def test_second_example(self):
        data = self.client.get(SWP_URL, {
            'corpus': 3000000, 'monthly_withdrawal': 25000, 'return_rate': 8, 'years': 15,
        }).json()

        self.assertEqual(round(data['ending_balance']), 1269809)
        self.assertEqual(data['total_withdrawn'], 4500000)
        self.assertFalse(data['depleted'])

    def test_zero_withdrawal_just_grows(self):
        data = self.client.get(SWP_URL, {
            'corpus': 100000, 'monthly_withdrawal': 0, 'return_rate': 12, 'years': 1,
        }).json()

        self.assertEqual(data['ending_balance'], 112682.5)  # 1,00,000 x 1.01^12 = 1,12,682.503
        self.assertEqual(data['total_withdrawn'], 0)
        self.assertFalse(data['depleted'])

    def test_invalid_inputs(self):
        response = self.client.get(SWP_URL, {
            'corpus': 0, 'monthly_withdrawal': -1, 'return_rate': 101, 'years': 0,
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {'corpus', 'monthly_withdrawal', 'return_rate', 'years'})

    def test_requires_login(self):
        self.client.force_authenticate(None)
        response = self.client.get(SWP_URL, {
            'corpus': 5000000, 'monthly_withdrawal': 30000, 'return_rate': 9, 'years': 20,
        })

        self.assertEqual(response.status_code, 401)


SIP_LUMPSUM_URL = '/api/calculators/sip-lumpsum/'


class SipLumpsumViewTests(APITestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='advisor@example.com', password='pass12345')
        self.client.force_authenticate(user)

    def test_example_from_spec(self):
        response = self.client.get(SIP_LUMPSUM_URL, {
            'monthly_sip': 20000, 'lumpsum': 500000, 'return_rate': 12, 'years': 15,
        })

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['months'], 180)                       # Step 1
        self.assertEqual(round(data['sip_value']), 10091520)        # Step 2
        self.assertEqual(round(data['lumpsum_value']), 2736783)     # Step 3
        self.assertEqual(round(data['maturity_value']), 12828303)   # Step 4
        self.assertEqual(data['sip_invested'], 3600000)             # Step 5
        self.assertEqual(data['total_invested'], 4100000)
        self.assertEqual(round(data['gains']), 8728303)
        self.assertEqual(data['wealth_multiple'], 3.13)             # Step 6
        self.assertEqual(round(data['sip_share_percent'], 1), 78.7)
        self.assertEqual(round(data['lumpsum_share_percent'], 1), 21.3)
        # Chart: start, a middle year, and the end.
        self.assertEqual(len(data['chart']), 16)
        self.assertEqual(data['chart'][0], {'year': 0, 'value': 500000.0, 'invested': 500000.0})
        self.assertEqual(round(data['chart'][8]['value']), 4468513)
        self.assertEqual(data['chart'][8]['invested'], 2420000)
        self.assertEqual(data['chart'][-1]['value'], data['maturity_value'])

    def test_second_example(self):
        data = self.client.get(SIP_LUMPSUM_URL, {
            'monthly_sip': 10000, 'lumpsum': 200000, 'return_rate': 10, 'years': 10,
        }).json()

        self.assertEqual(round(data['sip_value']), 2065520)
        self.assertEqual(round(data['lumpsum_value']), 518748)
        self.assertEqual(round(data['maturity_value']), 2584269)

    def test_lumpsum_only(self):
        data = self.client.get(SIP_LUMPSUM_URL, {
            'monthly_sip': 0, 'lumpsum': 100000, 'return_rate': 12, 'years': 10,
        }).json()

        self.assertEqual(round(data['maturity_value']), 310585)
        self.assertEqual(data['sip_share_percent'], 0)
        self.assertEqual(data['lumpsum_share_percent'], 100)

    def test_sip_and_lumpsum_both_zero_is_rejected(self):
        response = self.client.get(SIP_LUMPSUM_URL, {
            'monthly_sip': 0, 'lumpsum': 0, 'return_rate': 12, 'years': 10,
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'non_field_errors': ['Enter a monthly SIP, a lumpsum, or both.']})

    def test_invalid_inputs(self):
        response = self.client.get(SIP_LUMPSUM_URL, {'monthly_sip': -1, 'return_rate': 101, 'years': 0})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {'monthly_sip', 'lumpsum', 'return_rate', 'years'})

    def test_requires_login(self):
        self.client.force_authenticate(None)
        response = self.client.get(SIP_LUMPSUM_URL, {
            'monthly_sip': 20000, 'lumpsum': 500000, 'return_rate': 12, 'years': 15,
        })

        self.assertEqual(response.status_code, 401)


SIP_LUMPSUM_SWP_URL = '/api/calculators/sip-lumpsum-swp/'
SPEC_INPUTS = {
    'monthly_sip': 20000, 'lumpsum': 500000, 'return_rate': 12, 'years': 15,
    'monthly_withdrawal': 60000, 'withdrawal_return_rate': 9, 'withdrawal_years': 20,
}


class SipLumpsumSwpViewTests(APITestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='advisor@example.com', password='pass12345')
        self.client.force_authenticate(user)

    def test_example_from_spec(self):
        response = self.client.get(SIP_LUMPSUM_SWP_URL, SPEC_INPUTS)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(round(data['corpus_at_retirement']), 12828303)   # Phase 1
        self.assertEqual(round(data['ending_balance']), 37014004)         # Phase 2
        self.assertEqual(data['total_invested'], 4100000)                 # Summary 1
        self.assertEqual(data['total_withdrawn'], 14400000)               # Summary 2
        self.assertEqual(round(data['sustainable_withdrawal']), 96212)    # Summary 3
        self.assertEqual(data['peak_corpus'], data['ending_balance'])     # Summary 4
        self.assertFalse(data['depleted'])
        # Chart: 0..15 accumulation + 16..35 withdrawal = 36 points.
        self.assertEqual(len(data['chart']), 36)
        self.assertEqual(round(data['chart'][2]['corpus']), 1172064)      # spec's t = 2
        self.assertEqual(data['chart'][15]['corpus'], data['corpus_at_retirement'])
        self.assertEqual(round(data['chart'][25]['corpus']), 19835894)    # 10 years into withdrawals
        self.assertEqual(data['chart'][-1], {'year': 35, 'corpus': data['ending_balance']})

    def test_corpus_runs_out(self):
        # W = 1,50,000 is more than C x i2 = 96,212, so the corpus runs out in month 138 of withdrawals.
        data = self.client.get(SIP_LUMPSUM_SWP_URL, {**SPEC_INPUTS, 'monthly_withdrawal': 150000}).json()

        self.assertTrue(data['depleted'])
        self.assertEqual(data['depletion_month'], 138)
        self.assertEqual(data['ending_balance'], 0)
        # Only what was actually paid out, not W x N2 = 3,60,00,000.
        self.assertEqual(round(data['total_withdrawn']), 20588730)
        # Peak is the corpus at retirement (year 15), and the chart stops at 0.
        self.assertEqual(data['peak_corpus'], data['corpus_at_retirement'])
        self.assertEqual(data['chart'][30]['corpus'], 0)
        self.assertEqual(data['chart'][-1]['corpus'], 0)

    def test_inherits_sip_lumpsum_validation(self):
        response = self.client.get(SIP_LUMPSUM_SWP_URL, {**SPEC_INPUTS, 'monthly_sip': 0, 'lumpsum': 0})

        self.assertEqual(response.status_code, 400)
        self.assertIn('non_field_errors', response.json())

    def test_invalid_withdrawal_inputs(self):
        response = self.client.get(SIP_LUMPSUM_SWP_URL, {
            **SPEC_INPUTS, 'monthly_withdrawal': -1, 'withdrawal_return_rate': 101, 'withdrawal_years': 0,
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {'monthly_withdrawal', 'withdrawal_return_rate', 'withdrawal_years'})

    def test_requires_login(self):
        self.client.force_authenticate(None)
        response = self.client.get(SIP_LUMPSUM_SWP_URL, SPEC_INPUTS)

        self.assertEqual(response.status_code, 401)
