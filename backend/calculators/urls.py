from django.urls import path

from .views import GoalView, InflationView, SipLumpsumSwpView, SipLumpsumView, SipView, SwpView

urlpatterns = [
    path('calculators/inflation/', InflationView.as_view(), name='calculator-inflation'),
    path('calculators/goal/', GoalView.as_view(), name='calculator-goal'),
    path('calculators/sip/', SipView.as_view(), name='calculator-sip'),
    path('calculators/swp/', SwpView.as_view(), name='calculator-swp'),
    path('calculators/sip-lumpsum/', SipLumpsumView.as_view(), name='calculator-sip-lumpsum'),
    path('calculators/sip-lumpsum-swp/', SipLumpsumSwpView.as_view(), name='calculator-sip-lumpsum-swp'),
]
