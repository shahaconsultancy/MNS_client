import hmac

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.exceptions import AuthenticationFailed, Throttled, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import LoginSerializer, ResetPasswordSerializer


def _client_ip(request):
    # Behind Vercel's proxy the visitor is the first address of X-Forwarded-For.
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return forwarded.split(',')[0].strip() or request.META.get('REMOTE_ADDR', '')


class LoginView(APIView):
    """Signs in. After LOGIN_MAX_ATTEMPTS wrong passwords from one IP for one email, it is locked for LOGIN_LOCK_SECONDS."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        email = str(request.data.get('email', '')).strip().lower()
        counter = f'login-failed:{_client_ip(request)}:{email}'
        if cache.get(counter, 0) >= settings.LOGIN_MAX_ATTEMPTS:
            minutes = max(1, settings.LOGIN_LOCK_SECONDS // 60)
            raise Throttled(
                wait=settings.LOGIN_LOCK_SECONDS,
                detail=f'Too many failed attempts. Try again in {minutes} minute{"s" if minutes != 1 else ""}.',
            )

        serializer = LoginSerializer(data=request.data, context={'request': request})
        try:
            serializer.is_valid(raise_exception=True)
        except AuthenticationFailed:
            cache.add(counter, 0, settings.LOGIN_LOCK_SECONDS)  # the lock starts with the first failure
            cache.incr(counter)
            raise
        cache.delete(counter)
        user = serializer.validated_data['user']

        refresh = RefreshToken.for_user(user)
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': {
                'id': user.id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
            },
        })


class ResetPasswordView(APIView):
    """Sets a new password when the secret key from PASSWORD_RESET_KEY is given with the account's email."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'password_reset'

    def post(self, request):
        if not settings.PASSWORD_RESET_KEY:
            raise ValidationError({'detail': 'Password reset is not enabled.'})

        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        key_ok = hmac.compare_digest(data['key'].encode(), settings.PASSWORD_RESET_KEY.encode())
        user = get_user_model().objects.filter(email=data['email'].strip().lower(), is_active=True).first()
        if not key_ok or user is None:
            # One message for a wrong key and an unknown email, so the form doesn't reveal which accounts exist.
            raise ValidationError({'detail': 'The key or email is not valid.'})

        serializer.check_password_rules(user, data['new_password'])
        user.set_password(data['new_password'])
        user.save(update_fields=['password'])
        return Response({'detail': 'Password updated. Sign in with your new password.'})
