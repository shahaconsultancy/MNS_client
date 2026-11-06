from django.urls import path

from .views import FundCompareView, FundDetailView, FundFundamentalsView, FundOverlapImportView, FundOverlapJobStatusView, FundOverlapJobView, FundOverlapView, FundSearchView, PortfolioCoverageView

urlpatterns = [
    path('search', FundSearchView.as_view(), name='fund-search'),
    path('funds/<str:scheme_code>', FundDetailView.as_view(), name='fund-detail'),
    path('compare', FundCompareView.as_view(), name='fund-compare'),
    path('fundamentals', FundFundamentalsView.as_view(), name='fund-fundamentals'),
    path('overlap', FundOverlapView.as_view(), name='fund-overlap'),
    path('overlap/jobs', FundOverlapJobView.as_view(), name='fund-overlap-jobs'),
    path('overlap/jobs/<str:job_id>', FundOverlapJobStatusView.as_view(), name='fund-overlap-job'),
    path('overlap/import', FundOverlapImportView.as_view(), name='fund-overlap-import'),
    path('overlap/coverage', PortfolioCoverageView.as_view(), name='fund-overlap-coverage'),
]
