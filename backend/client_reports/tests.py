from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from .models import Client, ExpenseItem, FamilyMember, IncomeItem


def plan(**over):
    data = {
        'info': {'client_name': 'MR. AJAY', 'spouse_name': 'Asha', 'phone': '9876543210', 'email': 'A@x.com',
                 'address': 'Pune', 'plan_date': '2026-09-05', 'advisor_notes': 'n'},
        'income': [{'key': 'salary', 'label': 'Salary income', 'amount': '100000'}],
        'expenses': [{'key': 'household-0', 'label': 'House rent', 'amount': '30000', 'category': 'household'}],
        'investments': [{'key': 'sip', 'label': 'Mutual fund SIPs', 'amount': '5000'}],
        'assets': [{'key': 'liquid-0', 'label': 'Savings bank', 'amount': '900000', 'asset_class': 'liquid', 'purpose': 'Emergency'}],
        'liabilities': [{'key': 'home', 'label': 'Home loan', 'amount': '400000', 'purpose': ''}],
        'insurance': {'income_replacement_years': 40, 'existing_life_cover': '1000000', 'existing_health_cover': '0',
                      'auto_sum_from_family': False},
        'family': [{'name': 'Asha', 'relationship': 'Spouse', 'date_of_birth': None, 'age': 33, 'is_dependent': True}],
        'goals': [{'name': 'Car', 'cost_today': 1000000, 'years_to_goal': 5, 'inflation_rate': 6, 'expected_return': 10,
                   'current_savings': 0, 'monthly_sip': 5000, 'priority': 'High'}],
        'retirement': {'monthly_expense': 40000, 'existing_corpus': 900, 'current_age': 35, 'retirement_age': 60,
                       'life_expectancy': 85, 'inflation_rate': 6, 'return_before': 12, 'return_after': 8},
    }
    data.update(over)
    return data


class ClientPlanTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='adv@x.com', password='pw')
        self.client_row = Client.objects.create(advisor=self.user, full_name='Ajay Gaikwad', plan_name='Gaikwad Plan')
        self.url = f'/api/clients/{self.client_row.id}/plan/'
        self.client.force_authenticate(self.user)

    def test_empty_plan(self):
        body = self.client.get(self.url).json()
        self.assertIsNone(body['info'])
        self.assertEqual(body['income'], [])

    def test_save_fills_child_tables_and_client_totals(self):
        res = self.client.put(self.url, plan(), format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(IncomeItem.objects.get(client=self.client_row).amount, 100000)
        self.assertEqual(FamilyMember.objects.filter(client=self.client_row).count(), 1)
        self.client_row.refresh_from_db()
        self.assertEqual((self.client_row.net_worth, self.client_row.monthly_surplus, self.client_row.family_members), (500000, 65000, 1))
        self.assertEqual((self.client_row.mobile, self.client_row.email), ('9876543210', 'a@x.com'))
        body = self.client.get(self.url).json()
        self.assertEqual(body['assets'][0]['purpose'], 'Emergency')
        self.assertEqual((body['goals'][0]['name'], body['goals'][0]['priority']), ('Car', 'High'))
        self.assertEqual(body['retirement']['retirement_age'], 60)

    def test_update_replaces_rows(self):
        self.client.put(self.url, plan(), format='json')
        rows = [{'key': 'household-1', 'label': 'Society', 'amount': '2000', 'category': 'household'}]
        self.client.put(self.url, plan(expenses=rows, family=[]), format='json')
        self.assertEqual(list(ExpenseItem.objects.values_list('key', flat=True)), ['household-1'])
        self.assertEqual(FamilyMember.objects.count(), 0)

    def test_other_advisor_cannot_access(self):
        other = get_user_model().objects.create_user(email='o@x.com', password='pw')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_bad_phone_rejected(self):
        info = dict(plan()['info'], phone='123')
        self.assertEqual(self.client.put(self.url, plan(info=info), format='json').status_code, 400)
