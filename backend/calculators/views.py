import math
from decimal import ROUND_HALF_UP, Decimal

from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import (
    GoalSerializer, InflationSerializer, SipLumpsumSerializer, SipLumpsumSwpSerializer, SipSerializer,
    SwpSerializer,
)


def to_number(value, places=2):
    """Round a Decimal and return it as a JSON number."""
    return float(round(value, places))


class InflationView(APIView):
    """Future cost of something that costs `present_cost` today."""

    def get(self, request):
        serializer = InflationSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        present_cost = serializer.validated_data['present_cost']
        rate = serializer.validated_data['rate']
        years = serializer.validated_data['years']

        # Future cost = present cost x (1 + r/100)^n
        growth = 1 + rate / 100
        factor = growth ** years
        future_cost = present_cost * factor

        # Times today's cost = Future cost ÷ Present cost
        if present_cost > 0:
            times_cost = future_cost / present_cost
        else:
            times_cost = factor  # avoid dividing by 0

        half_way_year = math.ceil(years / 2)
        chart = [
            {'year': year, 'cost': to_number(present_cost * growth ** year)}
            for year in range(years + 1)
        ]

        return Response({
            'present_cost': to_number(present_cost),
            'rate': to_number(rate),
            'years': years,
            'factor': to_number(factor, 4),
            'future_cost': to_number(future_cost),
            'cost_increase': to_number(future_cost - present_cost),
            'times_cost': to_number(times_cost),
            'half_way_year': half_way_year,
            'half_way_cost': to_number(present_cost * growth ** half_way_year),
            'purchasing_power_of_lakh': to_number(Decimal('100000') / factor),
            'chart': chart,
        })


class GoalView(APIView):
    """Monthly SIP needed to reach a goal, after inflation and existing savings."""

    def get(self, request):
        serializer = GoalSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        goal_cost = serializer.validated_data['goal_cost']        # C
        inflation = serializer.validated_data['inflation'] / 100  # f
        years = serializer.validated_data['years']                # n
        return_rate = serializer.validated_data['return_rate'] / 100  # r
        savings = serializer.validated_data['savings']            # P

        # Step 1: future goal value, G = C x (1 + f)^n
        future_goal_value = goal_cost * (1 + inflation) ** years

        # Step 2: existing savings grown, S = P x (1 + r)^n
        savings_grown = savings * (1 + return_rate) ** years

        # Step 3: shortfall to fund by SIP, A = G - S (0 if savings already cover the goal)
        shortfall = max(future_goal_value - savings_grown, Decimal('0'))

        # Step 4: required monthly SIP, paid at the start of each month
        # SIP = A x i / ([(1 + i)^N - 1] x (1 + i))
        monthly_rate = return_rate / 12  # i
        months = years * 12              # N
        if monthly_rate > 0:
            sip = shortfall * monthly_rate / (((1 + monthly_rate) ** months - 1) * (1 + monthly_rate))
        else:
            sip = shortfall / months  # no growth: split the shortfall evenly
        monthly_sip = sip.quantize(Decimal('1'), rounding=ROUND_HALF_UP)

        # Step 5: summary figures, based on the rounded SIP shown to the user
        total_invested = monthly_sip * months
        growth_earned = future_goal_value - total_invested - savings

        # Year-by-year: SIP value so far + savings grown so far, against the inflated goal.
        chart = []
        for year in range(years + 1):
            months_so_far = year * 12
            if monthly_rate > 0:
                sip_value = sip * ((1 + monthly_rate) ** months_so_far - 1) / monthly_rate * (1 + monthly_rate)
            else:
                sip_value = sip * months_so_far
            chart.append({
                'year': year,
                'corpus': to_number(sip_value + savings * (1 + return_rate) ** year),
                'goal': to_number(goal_cost * (1 + inflation) ** year),
            })

        return Response({
            'goal_cost': to_number(goal_cost),
            'inflation': to_number(inflation * 100),
            'years': years,
            'return_rate': to_number(return_rate * 100),
            'savings': to_number(savings),
            'future_goal_value': to_number(future_goal_value),
            'savings_grown': to_number(savings_grown),
            'shortfall': to_number(shortfall),
            'monthly_sip': to_number(monthly_sip),
            'months': months,
            'total_invested': to_number(total_invested),
            'growth_earned': to_number(growth_earned),
            'chart': chart,
        })


class SipView(APIView):
    """What a fixed monthly investment grows to."""

    def get(self, request):
        serializer = SipSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        monthly_sip = serializer.validated_data['monthly_sip']          # M
        return_rate = serializer.validated_data['return_rate'] / 100    # r
        years = serializer.validated_data['years']                      # n

        # Step 1: monthly rate and number of instalments
        monthly_rate = return_rate / 12  # i
        months = years * 12              # N

        # Step 2: maturity value, paid at the start of each month
        # FV = M x [((1 + i)^N - 1) / i] x (1 + i)
        if monthly_rate > 0:
            maturity_value = monthly_sip * ((1 + monthly_rate) ** months - 1) / monthly_rate * (1 + monthly_rate)
        else:
            maturity_value = monthly_sip * months  # no growth

        # Step 3 and 4: total invested and estimated gains
        total_invested = monthly_sip * months
        gains = maturity_value - total_invested

        # Step 5: chart, one point per year t
        chart = []
        for year in range(years + 1):
            months_so_far = year * 12
            if monthly_rate > 0:
                value = monthly_sip * ((1 + monthly_rate) ** months_so_far - 1) / monthly_rate * (1 + monthly_rate)
            else:
                value = monthly_sip * months_so_far
            chart.append({
                'year': year,
                'value': to_number(value),
                'invested': to_number(monthly_sip * months_so_far),
            })

        return Response({
            'monthly_sip': to_number(monthly_sip),
            'return_rate': to_number(return_rate * 100),
            'years': years,
            'months': months,
            'maturity_value': to_number(maturity_value),
            'total_invested': to_number(total_invested),
            'gains': to_number(gains),
            # Step 5 footer metrics. The serializer's minimums keep both divisors above 0.
            'wealth_multiple': to_number(maturity_value / total_invested),
            'gains_share_percent': to_number(gains / maturity_value * 100),
            'gains_percent_of_invested': to_number(gains / total_invested * 100),
            'chart': chart,
        })


def swp_balance(corpus, withdrawal, monthly_rate, months):
    """Balance after `months` withdrawals taken at the end of each month (can go below 0)."""
    if monthly_rate == 0:
        return corpus - withdrawal * months
    growth = (1 + monthly_rate) ** months
    # B = P x (1 + i)^N - W x [((1 + i)^N - 1) / i]
    return corpus * growth - withdrawal * (growth - 1) / monthly_rate


def run_swp(corpus, withdrawal, monthly_rate, months):
    """Ending balance, total withdrawn and the month the corpus runs out (None if it lasts)."""
    # When does the corpus run out? Only possible if W is more than the monthly interest (P x i).
    months_until_depletion = None
    if withdrawal > corpus * monthly_rate:
        if monthly_rate > 0:
            # -ln(1 - P x i / W) / ln(1 + i)
            months_until_depletion = -(1 - corpus * monthly_rate / withdrawal).ln() / (1 + monthly_rate).ln()
        else:
            months_until_depletion = corpus / withdrawal

    if months_until_depletion is None or months_until_depletion > months:
        return swp_balance(corpus, withdrawal, monthly_rate, months), withdrawal * months, None

    full_withdrawals = math.floor(months_until_depletion)
    # The last month pays out whatever is left after that month's interest.
    last_withdrawal = swp_balance(corpus, withdrawal, monthly_rate, full_withdrawals) * (1 + monthly_rate)
    total_withdrawn = withdrawal * full_withdrawals + last_withdrawal
    return Decimal('0'), total_withdrawn, math.ceil(months_until_depletion)


class SwpView(APIView):
    """How a corpus holds up under a fixed monthly withdrawal."""

    def get(self, request):
        serializer = SwpSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        corpus = serializer.validated_data['corpus']                    # P
        withdrawal = serializer.validated_data['monthly_withdrawal']    # W
        return_rate = serializer.validated_data['return_rate'] / 100    # r
        years = serializer.validated_data['years']                      # n

        # Step 1: monthly rate and number of withdrawals
        monthly_rate = return_rate / 12  # i
        months = years * 12              # N

        # Step 5: sustainable withdrawal, the monthly interest alone
        sustainable_withdrawal = corpus * monthly_rate

        # Steps 2 and 3: ending balance and total withdrawn (handles the corpus running out)
        ending_balance, total_withdrawn, depletion_month = run_swp(corpus, withdrawal, monthly_rate, months)
        depleted = depletion_month is not None

        # Chart: balance at each year t, stopping at 0 instead of going negative.
        chart = []
        for year in range(years + 1):
            balance = swp_balance(corpus, withdrawal, monthly_rate, year * 12)
            chart.append({'year': year, 'balance': to_number(max(balance, Decimal('0')))})

        return Response({
            'corpus': to_number(corpus),
            'monthly_withdrawal': to_number(withdrawal),
            'return_rate': to_number(return_rate * 100),
            'years': years,
            'months': months,
            'ending_balance': to_number(ending_balance),
            'total_withdrawn': to_number(total_withdrawn),
            # Step 4: summary figures
            'annual_withdrawal': to_number(withdrawal * 12),
            'withdrawal_rate': to_number(withdrawal * 12 / corpus * 100),
            'ending_multiple': to_number(ending_balance / corpus),
            'sustainable_withdrawal': to_number(sustainable_withdrawal),
            'depleted': depleted,
            'depletion_month': depletion_month,
            'chart': chart,
        })


def sip_future_value(monthly_sip, monthly_rate, months):
    """Value of a monthly SIP paid at the start of each month: M x [((1 + i)^N - 1) / i] x (1 + i)."""
    if monthly_rate == 0:
        return monthly_sip * months
    return monthly_sip * ((1 + monthly_rate) ** months - 1) / monthly_rate * (1 + monthly_rate)


class SipLumpsumView(APIView):
    """A one-time investment plus a monthly SIP, grown together."""

    def get(self, request):
        serializer = SipLumpsumSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        monthly_sip = serializer.validated_data['monthly_sip']          # M
        lumpsum = serializer.validated_data['lumpsum']                  # L
        return_rate = serializer.validated_data['return_rate'] / 100    # r
        years = serializer.validated_data['years']                      # n

        # Step 1: monthly rate and number of instalments
        monthly_rate = return_rate / 12  # i
        months = years * 12              # N

        # Step 2: SIP future value (start of month)
        sip_value = sip_future_value(monthly_sip, monthly_rate, months)

        # Step 3: lumpsum future value (compounded yearly), L x (1 + r)^n
        lumpsum_value = lumpsum * (1 + return_rate) ** years

        # Step 4: maturity value
        maturity_value = sip_value + lumpsum_value

        # Step 5: invested amount and gains
        sip_invested = monthly_sip * months
        total_invested = sip_invested + lumpsum
        gains = maturity_value - total_invested

        # Chart: one point per year t
        chart = []
        for year in range(years + 1):
            value = sip_future_value(monthly_sip, monthly_rate, year * 12) + lumpsum * (1 + return_rate) ** year
            chart.append({
                'year': year,
                'value': to_number(value),
                'invested': to_number(monthly_sip * year * 12 + lumpsum),
            })

        return Response({
            'monthly_sip': to_number(monthly_sip),
            'lumpsum': to_number(lumpsum),
            'return_rate': to_number(return_rate * 100),
            'years': years,
            'months': months,
            'sip_value': to_number(sip_value),
            'lumpsum_value': to_number(lumpsum_value),
            'maturity_value': to_number(maturity_value),
            'sip_invested': to_number(sip_invested),
            'total_invested': to_number(total_invested),
            'gains': to_number(gains),
            # Step 6: footer metrics. validate() makes sure SIP or lumpsum is above 0, so these never divide by 0.
            'wealth_multiple': to_number(maturity_value / total_invested),
            'sip_share_percent': to_number(sip_value / maturity_value * 100),
            'lumpsum_share_percent': to_number(lumpsum_value / maturity_value * 100),
            'chart': chart,
        })


class SipLumpsumSwpView(APIView):
    """Build a corpus with SIP + lumpsum, then draw a monthly income from it."""

    def get(self, request):
        serializer = SipLumpsumSwpSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        monthly_sip = serializer.validated_data['monthly_sip']                          # M
        lumpsum = serializer.validated_data['lumpsum']                                  # L
        return_rate = serializer.validated_data['return_rate'] / 100                    # r1
        years = serializer.validated_data['years']                                      # n1
        withdrawal = serializer.validated_data['monthly_withdrawal']                    # W
        withdrawal_return_rate = serializer.validated_data['withdrawal_return_rate'] / 100  # r2
        withdrawal_years = serializer.validated_data['withdrawal_years']                # n2

        # Phase 1: corpus at retirement, C = FV_SIP + FV_Lump
        monthly_rate = return_rate / 12  # i1
        months = years * 12              # N1
        corpus = sip_future_value(monthly_sip, monthly_rate, months) + lumpsum * (1 + return_rate) ** years

        # Phase 2: ending balance after withdrawals (handles the corpus running out)
        withdrawal_monthly_rate = withdrawal_return_rate / 12  # i2
        withdrawal_months = withdrawal_years * 12              # N2
        ending_balance, total_withdrawn, depletion_month = run_swp(
            corpus, withdrawal, withdrawal_monthly_rate, withdrawal_months,
        )

        # Chart: accumulation years 0..n1, then withdrawal years n1+1..n1+n2 (stopping at 0).
        chart = []
        for year in range(years + 1):
            value = sip_future_value(monthly_sip, monthly_rate, year * 12) + lumpsum * (1 + return_rate) ** year
            chart.append({'year': year, 'corpus': to_number(value)})
        for s in range(1, withdrawal_years + 1):
            balance = swp_balance(corpus, withdrawal, withdrawal_monthly_rate, s * 12)
            chart.append({'year': years + s, 'corpus': to_number(max(balance, Decimal('0')))})

        return Response({
            'monthly_sip': to_number(monthly_sip),
            'lumpsum': to_number(lumpsum),
            'return_rate': to_number(return_rate * 100),
            'years': years,
            'monthly_withdrawal': to_number(withdrawal),
            'withdrawal_return_rate': to_number(withdrawal_return_rate * 100),
            'withdrawal_years': withdrawal_years,
            'corpus_at_retirement': to_number(corpus),
            'ending_balance': to_number(ending_balance),
            # Summary figures
            'total_invested': to_number(monthly_sip * months + lumpsum),
            'total_withdrawn': to_number(total_withdrawn),
            'sustainable_withdrawal': to_number(corpus * withdrawal_monthly_rate),
            # The balance only rises or only falls within each phase, so the highest yearly point is the peak.
            'peak_corpus': max(point['corpus'] for point in chart),
            'depleted': depletion_month is not None,
            'depletion_month': depletion_month,
            'chart': chart,
        })
