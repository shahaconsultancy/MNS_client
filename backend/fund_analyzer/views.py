from __future__ import annotations

import logging

from urllib.parse import urlsplit

from django.conf import settings
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .holdings import import_portfolio_holdings
from .jobs import get_job, start_job
from .portfolio_store import coverage_report
from .providers import ProviderError, fetch_amfi_latest
from .serializers import CompareRequestSerializer, CompareResponseSerializer, FundDetailSerializer, FundSearchResponseSerializer, FundSearchResultSerializer
from .services import compare_funds, compare_holdings_overlap, fetch_fund_data, fetch_fundamentals, search_funds


logger = logging.getLogger(__name__)


def _unavailable(message: str, exc: Exception, **extra) -> Response:
    """A plain message for the client. The provider's own text, which can name hosts and URLs, goes to the log."""
    logger.warning('Fund Analyzer provider error: %s', exc)
    return Response({'detail': message, **extra}, status=503)


class FundSearchView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fund_read'

    def get(self, request):
        query = str(request.query_params.get('q', '')).strip()
        if len(query) < 2:
            return Response({'source': 'AMFI', 'asOfDate': None, 'results': []})
        try:
            payload = search_funds(query)
            payload['results'] = [FundSearchResultSerializer({'source': payload['source'], 'asOfDate': payload['asOfDate'], **row}).data for row in payload['results']]
            return Response(FundSearchResponseSerializer(payload).data)
        except ProviderError as exc:
            return _unavailable('Fund search is temporarily unavailable. Please try again shortly.', exc, source='AMFI', asOfDate=None)


class FundDetailView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fund_read'

    def get(self, request, scheme_code: str):
        try:
            known = {str(record.get('scheme_code')) for record in fetch_amfi_latest()}
        except ProviderError:
            known = None
        if known is not None and str(scheme_code) not in known:
            return Response({'detail': 'Scheme not found', 'source': 'AMFI', 'asOfDate': None}, status=404)
        try:
            payload = fetch_fund_data(scheme_code, include_history=False)
            return Response(FundDetailSerializer(payload).data)
        except ProviderError as exc:
            return _unavailable('Fund data is temporarily unavailable. Please try again shortly.', exc, source='MFAPI (backup)', asOfDate=None)


class FundCompareView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fund_analysis'

    def post(self, request):
        request_serializer = CompareRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response({'detail': request_serializer.errors, 'source': 'AMFI', 'asOfDate': None}, status=400)
        try:
            validated = request_serializer.validated_data
            risk_free_rate = validated.get('risk_free_rate')
            payload = compare_funds(validated['scheme_codes'], risk_free_rate if risk_free_rate is not None else settings.FUND_ANALYZER_RISK_FREE_RATE)
            return Response(CompareResponseSerializer(payload).data)
        except ProviderError as exc:
            return _unavailable('Fund data is temporarily unavailable. Please try again shortly.', exc, source='MFAPI (backup)', asOfDate=None)


class FundFundamentalsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fund_analysis'

    def post(self, request):
        request_serializer = CompareRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response({'detail': request_serializer.errors, 'source': 'AMFI'}, status=400)
        try:
            payload = fetch_fundamentals(request_serializer.validated_data['scheme_codes'])
            payload['funds'] = [FundDetailSerializer(fund).data for fund in payload['funds']]
            return Response(payload)
        except ProviderError as exc:
            return _unavailable('Fund fundamentals are temporarily unavailable. Please try again shortly.', exc, source='AMFI')


class FundOverlapView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fund_analysis'

    def post(self, request):
        request_serializer = CompareRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response({'detail': request_serializer.errors, 'available': False}, status=400)
        try:
            payload = compare_holdings_overlap(request_serializer.validated_data['scheme_codes'], request_serializer.validated_data.get('portfolio_month'))
        except ProviderError:
            return Response({'detail': 'Portfolio holdings unavailable', 'available': False}, status=503)
        # File locations and error details are for the internal coverage report only.
        payload.pop('debug', None)
        return Response(payload)


class FundOverlapJobView(APIView):
    """Start (or join) an overlap job. Poll FundOverlapJobStatusView for its stage and result."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fund_analysis'

    def post(self, request):
        request_serializer = CompareRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response({'detail': request_serializer.errors, 'available': False}, status=400)
        job = start_job(request_serializer.validated_data['scheme_codes'], request_serializer.validated_data.get('portfolio_month'))
        return Response(_job_summary(job), status=202)


class FundOverlapJobStatusView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fund_read'

    def get(self, request, job_id):
        job = get_job(job_id)
        if job is None:
            return Response({'detail': 'Overlap job not found or expired', 'status': 'missing'}, status=404)
        return Response(_job_summary(job))


def _job_summary(job):
    summary = {key: job.get(key) for key in ('job_id', 'status', 'stage', 'message', 'error')}
    if job.get('status') == 'done':
        summary['result'] = job.get('result')
    return summary


class FundOverlapImportView(APIView):
    permission_classes = [IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fund_import'

    def post(self, request):
        uploaded = request.FILES.get('file')
        if not uploaded or not request.data.get('scheme_code') or not request.data.get('portfolio_date'):
            return Response({'detail': 'file, scheme_code and portfolio_date are required'}, status=400)
        if uploaded.size > 15 * 1024 * 1024:
            return Response({'detail': 'Portfolio disclosure file must be 15 MB or smaller'}, status=400)
        # Kept as a note of where the file came from. This address is never downloaded.
        source_url = str(request.data.get('source_url') or '')
        if source_url and (len(source_url) > 1000 or urlsplit(source_url).scheme not in ('http', 'https')):
            return Response({'detail': 'source_url must be a web address'}, status=400)
        try:
            result = import_portfolio_holdings(
                str(request.data['scheme_code']),
                str(request.data['portfolio_date']),
                uploaded.name,
                uploaded.read(),
                source_url,
            )
            return Response(result)
        except ProviderError as exc:
            return Response({'detail': str(exc)}, status=400)


class PortfolioCoverageView(APIView):
    """Internal report of which schemes have verified holdings, and what needs attention."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        try:
            return Response(coverage_report(fetch_amfi_latest()))
        except ProviderError as exc:
            return Response({'detail': str(exc)}, status=503)


