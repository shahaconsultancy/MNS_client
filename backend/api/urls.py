from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import LoginView, ResetPasswordView

urlpatterns = [
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/reset-password/', ResetPasswordView.as_view(), name='reset-password'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
]


