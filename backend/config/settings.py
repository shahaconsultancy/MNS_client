import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'dev-insecure-key')
DEBUG = os.getenv('DJANGO_DEBUG', 'False') == 'True'
ALLOWED_HOSTS = [h.strip() for h in os.getenv('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'corsheaders',
    'rest_framework',
    'accounts',
    'api',
    'client_reports',
    'calculators',
    'fund_analyzer',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': dj_database_url.config(
        default=os.getenv('DATABASE_URL', 'postgres://postgres:root@localhost:5432/mns_db'),
        conn_max_age=60,
    )
}

AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

CORS_ALLOWED_ORIGINS = [o.strip() for o in os.getenv('CORS_ALLOWED_ORIGINS', 'http://localhost:5173').split(',') if o.strip()]
FUND_ANALYZER_RISK_FREE_RATE = float(os.getenv('FUND_ANALYZER_RISK_FREE_RATE', '0.065'))
FUND_ANALYZER_PORTFOLIO_LOOKBACK_MONTHS = max(1, int(os.getenv('FUND_ANALYZER_PORTFOLIO_LOOKBACK_MONTHS', '6')))
# Backup holdings source: Charlie Alpha, an independent third-party API. OFF unless switched on, because
# its terms say "education and research" and grant no commercial redistribution rights. Turn it on only
# after permission and accuracy have been verified. The official AMC reader works the same either way.
FUND_ANALYZER_CHARLIE_ALPHA_ENABLED = os.getenv('FUND_ANALYZER_CHARLIE_ALPHA_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
FUND_ANALYZER_CHARLIE_ALPHA_URL = os.getenv('FUND_ANALYZER_CHARLIE_ALPHA_URL', 'https://api.charliealpha.in/v1').rstrip('/')
# Upper bound on backup requests for one Update Analysis, so a large selection cannot flood the provider.
# The backup source may supply only the newest of these completed months (October back to July on 6 November 2026).
FUND_ANALYZER_CHARLIE_ALPHA_LOOKBACK_MONTHS = max(1, int(os.getenv('FUND_ANALYZER_CHARLIE_ALPHA_LOOKBACK_MONTHS', '4')))
# Cache retention, used only by `manage.py cleanup_portfolio_cache`. Never below the lookbacks above.
FUND_ANALYZER_CACHE_OFFICIAL_RETENTION_MONTHS = max(1, int(os.getenv('FUND_ANALYZER_CACHE_OFFICIAL_RETENTION_MONTHS', '12')))
FUND_ANALYZER_CACHE_BACKUP_RETENTION_MONTHS = max(1, int(os.getenv('FUND_ANALYZER_CACHE_BACKUP_RETENTION_MONTHS', '6')))
FUND_ANALYZER_CACHE_TEMP_FILE_HOURS = max(1, int(os.getenv('FUND_ANALYZER_CACHE_TEMP_FILE_HOURS', '24')))
FUND_ANALYZER_CACHE_STATUS_RETENTION_DAYS = max(1, int(os.getenv('FUND_ANALYZER_CACHE_STATUS_RETENTION_DAYS', '30')))
# Where official files past retention are moved. Without it they are kept, because they may not be downloadable again.
FUND_ANALYZER_CACHE_ARCHIVE_DIR = os.getenv('FUND_ANALYZER_CACHE_ARCHIVE_DIR', '')
FUND_ANALYZER_CHARLIE_ALPHA_MAX_REQUESTS = max(1, int(os.getenv('FUND_ANALYZER_CHARLIE_ALPHA_MAX_REQUESTS', '40')))
# Verified holdings are JSON files here, not database rows. Vercel can only write to
# its temporary folder, which is wiped between runs, so set this on a server with a disk.
FUND_ANALYZER_HOLDINGS_CACHE_DIR = os.getenv('FUND_ANALYZER_HOLDINGS_CACHE_DIR') or str(
    Path(tempfile.gettempdir()) / 'mns-portfolio-cache' if os.getenv('VERCEL') else BASE_DIR / 'portfolio_cache'
)
if 'test' in sys.argv:
    # Tests must never touch the real cache.
    FUND_ANALYZER_HOLDINGS_CACHE_DIR = tempfile.mkdtemp(prefix='mns-test-portfolio-cache-')

REDIS_URL = os.getenv('REDIS_URL')
if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'CONNECTION_POOL_KWARGS': {'socket_connect_timeout': 1, 'socket_timeout': 1},
            },
            'KEY_PREFIX': 'mns',
            'TIMEOUT': 300,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'mns-fund-analyzer',
            'TIMEOUT': 300,
        }
    }

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ('rest_framework_simplejwt.authentication.JWTAuthentication',),
    'DEFAULT_PERMISSION_CLASSES': ('rest_framework.permissions.IsAuthenticated',),
    'DEFAULT_THROTTLE_RATES': {
        'fund_read': '120/min',
        'fund_analysis': '30/min',
        'fund_import': '30/hour',
        'password_reset': '10/hour',
    },
}

# Production hardening (live server only: DEBUG off). Vercel terminates HTTPS and says so in X-Forwarded-Proto.
if not DEBUG and 'test' not in sys.argv:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = os.getenv('DJANGO_SECURE_SSL_REDIRECT', 'True') == 'True'
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv('DJANGO_HSTS_SECONDS', '86400'))  # raise once HTTPS is confirmed everywhere
    SECURE_CONTENT_TYPE_NOSNIFF = True

# Sign-in lock: this many wrong passwords from one IP for one email, then locked for LOGIN_LOCK_SECONDS.
LOGIN_MAX_ATTEMPTS = int(os.getenv('LOGIN_MAX_ATTEMPTS', '3'))
LOGIN_LOCK_SECONDS = int(os.getenv('LOGIN_LOCK_SECONDS', '300'))

# Secret typed in the login page's "Forgot password?" pop-up. Empty switches the feature off.
PASSWORD_RESET_KEY = os.getenv('PASSWORD_RESET_KEY', '')

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
}
