from django.contrib import admin

from .models import (
    AssetItem, Client, ClientProfile, ExpenseItem, FamilyMember, Goal, IncomeItem, InsuranceCover,
    InvestmentItem, LiabilityItem, RetirementPlan,
)

admin.site.register([
    Client, ClientProfile, IncomeItem, ExpenseItem, InvestmentItem, AssetItem, LiabilityItem,
    InsuranceCover, FamilyMember, Goal, RetirementPlan,
])
