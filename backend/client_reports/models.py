from django.conf import settings
from django.db import models


class Client(models.Model):
    class Title(models.TextChoices):
        MR = 'Mr', 'Mr.'
        MRS = 'Mrs', 'Mrs.'
        MS = 'Ms', 'Ms.'
        DR = 'Dr', 'Dr.'

    advisor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='clients')
    title = models.CharField(max_length=3, choices=Title.choices, default=Title.MR)
    full_name = models.CharField(max_length=150)
    plan_name = models.CharField(max_length=200)
    mobile = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    net_worth = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    monthly_surplus = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    family_members = models.PositiveIntegerField(default=0)
    advisor_name = models.CharField(max_length=150, default='Money Navigating Solutions')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.plan_name


class ClientProfile(models.Model):
    """Client Info tab (one row per client)."""

    client = models.OneToOneField(Client, on_delete=models.CASCADE, related_name='profile')
    client_name = models.CharField(max_length=150, blank=True)
    spouse_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    plan_date = models.DateField(null=True, blank=True)
    advisor_notes = models.TextField(blank=True)

    def __str__(self):
        return f'Profile of {self.client}'


class PlanItem(models.Model):
    """One entry on a plan tab: a stable `key` from the form, its label, and the amount."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='%(class)s_rows')
    key = models.CharField(max_length=40)
    label = models.CharField(max_length=150)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        abstract = True
        ordering = ['id']

    def __str__(self):
        return f'{self.label}: {self.amount}'


class IncomeItem(PlanItem):
    """Income tab: monthly amount per income source."""

    class Meta(PlanItem.Meta):
        constraints = [models.UniqueConstraint(fields=['client', 'key'], name='uniq_income_item')]


class ExpenseItem(PlanItem):
    """Expenses tab: monthly amount per expense line, grouped by category."""

    category = models.CharField(max_length=30)

    class Meta(PlanItem.Meta):
        constraints = [models.UniqueConstraint(fields=['client', 'key'], name='uniq_expense_item')]


class InvestmentItem(PlanItem):
    """Savings & Investments tab: monthly contribution per investment type."""

    class Meta(PlanItem.Meta):
        constraints = [models.UniqueConstraint(fields=['client', 'key'], name='uniq_investment_item')]


class AssetItem(PlanItem):
    """Assets tab: present value per asset, grouped by asset class."""

    asset_class = models.CharField(max_length=30)
    purpose = models.CharField(max_length=100, blank=True)

    class Meta(PlanItem.Meta):
        constraints = [models.UniqueConstraint(fields=['client', 'key'], name='uniq_asset_item')]


class LiabilityItem(PlanItem):
    """Liabilities & Net Worth tab: outstanding amount per loan type."""

    purpose = models.CharField(max_length=100, blank=True)

    class Meta(PlanItem.Meta):
        constraints = [models.UniqueConstraint(fields=['client', 'key'], name='uniq_liability_item')]


class InsuranceCover(models.Model):
    """Insurance tab inputs (one row per client). Recommended cover is calculated, not stored."""

    client = models.OneToOneField(Client, on_delete=models.CASCADE, related_name='insurance')
    income_replacement_years = models.PositiveSmallIntegerField(default=40)
    existing_life_cover = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    existing_health_cover = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    auto_sum_from_family = models.BooleanField(default=False)

    def __str__(self):
        return f'Insurance of {self.client}'


class FamilyMember(models.Model):
    """Family Members tab: one row per member."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='members')
    position = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=150, blank=True)
    relationship = models.CharField(max_length=20)
    date_of_birth = models.DateField(null=True, blank=True)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    gender = models.CharField(max_length=10, blank=True)
    occupation = models.CharField(max_length=100, blank=True)
    annual_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    life_policy = models.CharField(max_length=150, blank=True)
    life_cover = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    health_policy = models.CharField(max_length=150, blank=True)
    health_cover = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    is_dependent = models.BooleanField(default=False)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return f'{self.name or "Member"} ({self.relationship})'


class Goal(models.Model):
    """Goals tab: one row per life goal. Future target, corpus and shortfall are calculated, not stored."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='goals')
    position = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=60)
    cost_today = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    years_to_goal = models.PositiveSmallIntegerField(default=0)
    inflation_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    expected_return = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    current_savings = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    monthly_sip = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    priority = models.CharField(max_length=10, default='Medium')

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return self.name


class RetirementPlan(models.Model):
    """Retirement tab inputs (one row per client). Corpus required and the SIP are calculated, not stored."""

    client = models.OneToOneField(Client, on_delete=models.CASCADE, related_name='retirement')
    monthly_expense = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    existing_corpus = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    current_age = models.PositiveSmallIntegerField(default=35)
    retirement_age = models.PositiveSmallIntegerField(default=60)
    life_expectancy = models.PositiveSmallIntegerField(default=85)
    inflation_rate = models.DecimalField(max_digits=5, decimal_places=2, default=6)
    return_before = models.DecimalField(max_digits=5, decimal_places=2, default=12)
    return_after = models.DecimalField(max_digits=5, decimal_places=2, default=8)

    def __str__(self):
        return f'Retirement plan of {self.client}'
