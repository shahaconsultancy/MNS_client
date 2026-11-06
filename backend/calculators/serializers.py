from decimal import Decimal

from rest_framework import serializers


class InflationSerializer(serializers.Serializer):
    present_cost = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0'))
    rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'),
    )
    years = serializers.IntegerField(min_value=0, max_value=100)


class GoalSerializer(serializers.Serializer):
    goal_cost = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0'))
    inflation = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'),
    )
    # At least 1 year: a goal can't be funded with zero months of SIP.
    years = serializers.IntegerField(min_value=1, max_value=100)
    return_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'),
    )
    savings = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0'))


class SipSerializer(serializers.Serializer):
    # At least ₹1 and 1 year, so invested and maturity value are never 0.
    monthly_sip = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('1'))
    return_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'),
    )
    years = serializers.IntegerField(min_value=1, max_value=100)


class SwpSerializer(serializers.Serializer):
    # At least ₹1, because withdrawal rate and ending multiple divide by the corpus.
    corpus = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('1'))
    monthly_withdrawal = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0'))
    return_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'),
    )
    years = serializers.IntegerField(min_value=1, max_value=100)


class SipLumpsumSerializer(serializers.Serializer):
    monthly_sip = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0'))
    lumpsum = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0'))
    return_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'),
    )
    years = serializers.IntegerField(min_value=1, max_value=100)

    def validate(self, attrs):
        # Either one can be 0, but not both: the SIP and lumpsum shares divide by the maturity value.
        if attrs['monthly_sip'] == 0 and attrs['lumpsum'] == 0:
            raise serializers.ValidationError('Enter a monthly SIP, a lumpsum, or both.')
        return attrs


class SipLumpsumSwpSerializer(SipLumpsumSerializer):
    """The SIP + lumpsum inputs (accumulation phase), plus the withdrawal phase."""

    monthly_withdrawal = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0'))
    withdrawal_return_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'),
    )
    withdrawal_years = serializers.IntegerField(min_value=1, max_value=100)
