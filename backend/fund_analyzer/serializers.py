from datetime import date

from rest_framework import serializers

MAX_FUNDS_PER_REQUEST = 100


class SourceSerializer(serializers.Serializer):
    source = serializers.CharField()
    asOfDate = serializers.CharField(allow_null=True, required=False)


class FundSearchResultSerializer(SourceSerializer):
    scheme_code = serializers.CharField()
    scheme_name = serializers.CharField()
    amc = serializers.CharField(allow_null=True, required=False)
    category = serializers.CharField(allow_null=True, required=False)
    scheme_type = serializers.CharField(allow_null=True, required=False)
    plan = serializers.CharField(allow_null=True, required=False)
    option = serializers.CharField(allow_null=True, required=False)
    isin = serializers.CharField(allow_null=True, required=False)
    nav = serializers.FloatField(allow_null=True, required=False)


class FundSearchResponseSerializer(SourceSerializer):
    results = FundSearchResultSerializer(many=True)


class MetricSetSerializer(serializers.Serializer):
    r1m = serializers.FloatField(allow_null=True, required=False)
    r3m = serializers.FloatField(allow_null=True, required=False)
    r6m = serializers.FloatField(allow_null=True, required=False)
    r1y = serializers.FloatField(allow_null=True, required=False)
    r3y = serializers.FloatField(allow_null=True, required=False)
    r5y = serializers.FloatField(allow_null=True, required=False)
    volatility = serializers.FloatField(allow_null=True, required=False)
    sharpe = serializers.FloatField(allow_null=True, required=False)
    maxDrawdown = serializers.FloatField(allow_null=True, required=False)
    rollingReturns = serializers.ListField(required=False)
    calendarYearReturns = serializers.ListField(required=False)
    growthOf10000 = serializers.ListField(required=False)
    sip = serializers.DictField(allow_null=True, required=False)


class FundDetailSerializer(SourceSerializer):
    scheme_code = serializers.CharField()
    scheme_name = serializers.CharField()
    amc = serializers.CharField(allow_null=True, required=False)
    category = serializers.CharField(allow_null=True, required=False)
    scheme_type = serializers.CharField(allow_null=True, required=False)
    plan = serializers.CharField(allow_null=True, required=False)
    option = serializers.CharField(allow_null=True, required=False)
    isin = serializers.CharField(allow_null=True, required=False)
    nav = serializers.FloatField(allow_null=True, required=False)
    nav_date = serializers.CharField(allow_null=True, required=False)
    scheme_status = serializers.CharField(allow_null=True, required=False)
    last_nav_date = serializers.CharField(allow_null=True, required=False)
    ter =serializers.FloatField(allow_null=True, required=False)
    ter_as_of = serializers.CharField(allow_null=True, required=False)
    aum = serializers.FloatField(allow_null=True, required=False)
    aum_unit = serializers.CharField(allow_null=True, required=False)
    aum_as_of = serializers.CharField(allow_null=True, required=False)
    holdings = serializers.ListField(child=serializers.DictField(), required=False)
    holdings_as_of = serializers.CharField(allow_null=True, required=False)
    holdings_source = serializers.CharField(allow_null=True, required=False)
    benchmark = serializers.CharField(allow_null=True, required=False)
    allocation = serializers.DictField(allow_null=True, required=False)
    sectors = serializers.ListField(child=serializers.DictField(), required=False)
    riskometer = serializers.DictField(allow_null=True, required=False)
    asset_class = serializers.CharField(allow_null=True, required=False)
    metrics = MetricSetSerializer(allow_null=True, required=False)


class CompareRequestSerializer(serializers.Serializer):
    # A client can pick as many funds as they like. The ceiling only stops one request from
    # asking for thousands: every fund adds a download, and every pair adds a comparison.
    scheme_codes = serializers.ListField(child=serializers.CharField(), min_length=1, max_length=MAX_FUNDS_PER_REQUEST)
    risk_free_rate = serializers.FloatField(allow_null=True, required=False, min_value=0, max_value=1)
    # Historical overlap: compare every fund on this one reporting month, as YYYY-MM.
    portfolio_month = serializers.RegexField(r'^\d{4}-(0[1-9]|1[0-2])$', allow_null=True, required=False)

    def validate_portfolio_month(self, value):
        if value and value > date.today().strftime('%Y-%m'):
            raise serializers.ValidationError('portfolio_month cannot be in the future.')
        return value

    def validate_scheme_codes(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError('scheme_codes must contain unique scheme codes.')
        return value


class CompareResponseSerializer(SourceSerializer):
    funds = FundDetailSerializer(many=True)
    correlation = serializers.DictField()
    summary = serializers.DictField()
