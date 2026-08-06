"""
Django settings for cafe project.

Configuration is environment-driven so the SAME code runs in three roles:

  * A local laptop (manager or salesperson) — SQLite, DEBUG on, works offline.
  * The central server — Postgres, DEBUG off, HTTPS hardening on.

Every override has a safe local-development default, so running the project
locally with no environment variables behaves exactly as it did before.
"""

import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# --- Small helpers for reading environment configuration -------------------
def env_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


def env_list(name, default):
    val = os.environ.get(name)
    if not val:
        return default
    return [item.strip() for item in val.split(',') if item.strip()]


# --- Optional .env loading (only if python-dotenv is installed) ------------
# Keeps secrets out of source control. Absence of the package is not an error.
try:  # pragma: no cover - convenience only
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / '.env')
except Exception:
    pass


# SECURITY WARNING: keep the secret key used in production secret!
# In production this MUST be supplied via DJANGO_SECRET_KEY. The literal below
# is only a convenience fallback for local development.
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-bvo+q&+fz*&!n4)!)#%+z5ine_$-8_h*2*td^*4-#jm30s(j0k',
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env_bool('DJANGO_DEBUG', True)

# Locked down in production; permissive only for local development.
ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', ['*'] if DEBUG else [])
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS', [])


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    "panel",
    "django.contrib.humanize",

    'crispy_forms',
    'crispy_tailwind',

    "finance",
    "sync",
    'pwa',

]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    # Requires login for every page except an explicit allowlist, and enforces
    # the manager / salesperson access policy (default-deny for salespeople).
    'cafe.middleware.AccessControlMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Serve static files efficiently in production when WhiteNoise is installed.
# Guarded so a local checkout without the package still boots normally.
try:  # pragma: no cover - depends on deployment environment
    import whitenoise  # noqa: F401

    MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }
except ImportError:
    pass

ROOT_URLCONF = 'cafe.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': ["templates"],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # Exposes `is_manager` / `is_salesperson` to every template so
                # cost/profit data can be hidden from salespeople in markup.
                'panel.permissions.role_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'cafe.wsgi.application'


# Database
# Central server: set POSTGRES_DB (+ related vars) for concurrent multi-user use.
# Laptops / local dev: falls back to SQLite (works offline).

if os.environ.get('POSTGRES_DB'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ['POSTGRES_DB'],
            'USER': os.environ.get('POSTGRES_USER', 'postgres'),
            'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
            'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
            'PORT': os.environ.get('POSTGRES_PORT', '5432'),
            'CONN_MAX_AGE': 60,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            # DJANGO_SQLITE_NAME lets tooling point at an alternate file (e.g. a
            # copy) without editing settings; defaults to the local DB.
            'NAME': os.environ.get('DJANGO_SQLITE_NAME', BASE_DIR / 'db.sqlite3'),
            # Wait instead of failing immediately if the file is briefly locked.
            'OPTIONS': {'timeout': 20},
        }
    }


# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Africa/Khartoum'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.0/howto/static-files/

STATIC_URL = 'static/'
# Destination for `collectstatic` on the server.
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

CRISPY_ALLOWED_TEMPLATE_PACKS = "tailwind"
CRISPY_TEMPLATE_PACK = "tailwind"

# Set default date input format
DATE_INPUT_FORMATS = ['%d/%m/%Y']

# Set display format for templates
DATE_FORMAT = 'd/m/Y'

# Make sure Django doesn't use localization that overrides it
USE_L10N = False


# --- Authentication / access control --------------------------------------
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# Paths reachable without logging in (login page, static assets, PWA plumbing,
# the token-authenticated sync API, and Django admin which has its own login).
LOGIN_EXEMPT_PREFIXES = (
    '/accounts/',
    '/admin/',
    '/static/',
    '/media/',
    '/sync/api/',
    '/serviceworker.js',
    '/manifest.json',
    '/offline',
)


# --- Sync configuration (consumed by the `sync` app) -----------------------
# Role of THIS installation:
#   'server'      - the central authoritative node (no outbound sync)
#   'manager'     - a laptop that pulls everything and pushes admin changes
#   'salesperson' - a laptop that pushes its own sales and pulls reference data
#   'standalone'  - not connected to any server (default; behaves like before)
SYNC_ROLE = os.environ.get('SYNC_ROLE', 'standalone')
# Base URL of the central server, e.g. https://server.example.com
SYNC_SERVER_URL = os.environ.get('SYNC_SERVER_URL', '').rstrip('/')
# Per-node API token issued by the server (identifies + authorizes this laptop).
SYNC_NODE_TOKEN = os.environ.get('SYNC_NODE_TOKEN', '')
# Friendly name for THIS laptop (used only in local sync logs).
SYNC_NODE_NAME = os.environ.get('SYNC_NODE_NAME', 'this-laptop')
# Network timeout (seconds) for outbound sync HTTP calls.
SYNC_HTTP_TIMEOUT = int(os.environ.get('SYNC_HTTP_TIMEOUT', '30'))


# --- Production security hardening (only when DEBUG is off) ----------------
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # Trust the reverse proxy's HTTPS termination (nginx / Caddy).
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    X_FRAME_OPTIONS = 'DENY'


PWA_APP_NAME = 'Equatorial System'
PWA_APP_DESCRIPTION = "Equatorial medical management system"
PWA_APP_THEME_COLOR = '#000000'
PWA_APP_BACKGROUND_COLOR = '#ffffff'
PWA_APP_DISPLAY = 'standalone'
PWA_APP_SCOPE = '/'
PWA_APP_ORIENTATION = 'portrait'
PWA_APP_START_URL = '/'
PWA_APP_ICONS = [
    {
        'src': '/static/icon.png',
        'sizes': '192x192'
    },
    {
        'src': '/static/icon.png',
        'sizes': '512x512'
    }
]
PWA_APP_DIR = 'ltr'
PWA_APP_LANG = 'en-US'
