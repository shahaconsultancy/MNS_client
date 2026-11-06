import re
from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from .models import (
    AssetItem, Client, ClientProfile, ExpenseItem, FamilyMember, Goal, IncomeItem, InsuranceCover,
    InvestmentItem, LiabilityItem, RetirementPlan,
)


class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = [
            'id', 'title', 'full_name', 'plan_name', 'mobile', 'email',
            'net_worth', 'monthly_surplus', 'family_members', 'advisor_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'net_worth', 'monthly_surplus', 'family_members',
                            'advisor_name', 'created_at', 'updated_at']
        extra_kwargs = {'plan_name': {'required': False, 'allow_blank': True}}

    def validate_full_name(self, value):
        value = ' '.join(value.split())
        if len(value) < 2:
            raise serializers.ValidationError('Enter the client full name.')
        return value

    def validate_mobile(self, value):
        digits = re.sub(r'\D', '', value)
        if digits and len(digits) != 10:
            raise serializers.ValidationError('Enter a valid 10-digit mobile number.')
        return digits

    def validate_email(self, value):
        return value.strip().lower()

    def validate(self, attrs):
        if not attrs.get('plan_name', '').strip():
            surname = attrs['full_name'].split()[-1]
            attrs['plan_name'] = f'{surname.title()} Family Financial Plan'
        return attrs


# ---- Financial plan: every saved tab in one request -------------------------------------------

def _row(model, fields):
    meta = type('Meta', (), {'model': model, 'fields': fields, 'extra_kwargs': {'amount': {'min_value': 0}}})
    return type(f'{model.__name__}Row', (serializers.ModelSerializer,), {'Meta': meta})


# Line-item tables, keyed by the name used in the request / response.
ROWS = {
    'income': (IncomeItem, _row(IncomeItem, ['key', 'label', 'amount'])),
    'expenses': (ExpenseItem, _row(ExpenseItem, ['key', 'label', 'amount', 'category'])),
    'investments': (InvestmentItem, _row(InvestmentItem, ['key', 'label', 'amount'])),
    'assets': (AssetItem, _row(AssetItem, ['key', 'label', 'amount', 'asset_class', 'purpose'])),
    'liabilities': (LiabilityItem, _row(LiabilityItem, ['key', 'label', 'amount', 'purpose'])),
}


class InfoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientProfile
        exclude = ['id', 'client']

    def validate_phone(self, value):
        digits = re.sub(r'\D', '', value)
        if digits and len(digits) != 10:
            raise serializers.ValidationError('Enter a valid 10-digit mobile number.')
        return digits


class InsuranceSerializer(serializers.ModelSerializer):
    class Meta:
        model = InsuranceCover
        exclude = ['id', 'client']


class MemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = FamilyMember
        exclude = ['id', 'client', 'position']


class GoalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Goal
        exclude = ['id', 'client', 'position']


class RetirementSerializer(serializers.ModelSerializer):
    class Meta:
        model = RetirementPlan
        exclude = ['id', 'client']


class PlanSerializer(serializers.Serializer):
    info = InfoSerializer()
    income = ROWS['income'][1](many=True)
    expenses = ROWS['expenses'][1](many=True)
    investments = ROWS['investments'][1](many=True)
    assets = ROWS['assets'][1](many=True)
    liabilities = ROWS['liabilities'][1](many=True)
    insurance = InsuranceSerializer()
    family = MemberSerializer(many=True)
    goals = GoalSerializer(many=True)
    retirement = RetirementSerializer()


def plan_data(client):
    """The saved plan in the shape PUT accepts. `info`, `insurance` and `retirement` are null until first saved."""
    profile = ClientProfile.objects.filter(client=client).first()
    cover = InsuranceCover.objects.filter(client=client).first()
    retirement = RetirementPlan.objects.filter(client=client).first()
    data = {
        'info': InfoSerializer(profile).data if profile else None,
        'insurance': InsuranceSerializer(cover).data if cover else None,
        'retirement': RetirementSerializer(retirement).data if retirement else None,
        'family': MemberSerializer(client.members.all(), many=True).data,
        'goals': GoalSerializer(client.goals.all(), many=True).data,
    }
    for name, (model, row) in ROWS.items():
        data[name] = row(model.objects.filter(client=client), many=True).data
    return data


@transaction.atomic
def save_plan(client, data):
    """Replace every plan table of this client with the submitted rows, then refresh the list-page figures."""
    ClientProfile.objects.update_or_create(client=client, defaults=data['info'])
    InsuranceCover.objects.update_or_create(client=client, defaults=data['insurance'])
    RetirementPlan.objects.update_or_create(client=client, defaults=data['retirement'])
    client.goals.all().delete()
    Goal.objects.bulk_create(Goal(client=client, position=i, **g) for i, g in enumerate(data['goals']))
    for name, (model, _) in ROWS.items():
        model.objects.filter(client=client).delete()
        model.objects.bulk_create(model(client=client, **row) for row in data[name])
    client.members.all().delete()
    FamilyMember.objects.bulk_create(FamilyMember(client=client, position=i, **m) for i, m in enumerate(data['family']))

    def total(name):
        return sum((r['amount'] for r in data[name]), Decimal(0))

    client.mobile = data['info']['phone']
    client.email = data['info']['email'].strip().lower()
    client.net_worth = total('assets') - total('liabilities')
    client.monthly_surplus = total('income') - total('expenses') - total('investments')
    client.family_members = len(data['family'])
    client.save(update_fields=['mobile', 'email', 'net_worth', 'monthly_surplus', 'family_members', 'updated_at'])
