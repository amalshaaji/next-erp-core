import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

def environment_bool(name, default):
    value = os.environ.get(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError(f"{name} must be set to True or False.")
    return value == "true"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = environment_bool("DJANGO_DEBUG", "False")
PREVIEW = environment_bool("DJANGO_PREVIEW", "False")
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "whitenoise.runserver_nostatic",
    "shop.apps.ShopConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "shop.middleware.ClerkShopMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.messages.context_processors.messages",
        "shop.context_processors.shop_context",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"

database_url = os.environ.get("DATABASE_URL", "")
database_max_age = int(os.environ.get("DB_CONN_MAX_AGE", "60"))
if database_max_age < 0:
    raise RuntimeError("DB_CONN_MAX_AGE must be zero or greater.")
if database_url:
    import dj_database_url
    DATABASES = {"default": dj_database_url.parse(database_url, conn_max_age=database_max_age, ssl_require=True)}
    DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = environment_bool("DB_DISABLE_SERVER_SIDE_CURSORS", "False")
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("SHOP_TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = not DEBUG
if not DEBUG:
    if SECRET_KEY == "dev-only-change-me":
        raise RuntimeError("Set DJANGO_SECRET_KEY to a unique random secret when DJANGO_DEBUG=False.")
    if len(SECRET_KEY) < 50:
        raise RuntimeError("Use a randomly generated DJANGO_SECRET_KEY with at least 50 characters in production.")
    if len(set(SECRET_KEY)) < 16:
        raise RuntimeError("DJANGO_SECRET_KEY does not appear sufficiently random for production.")
    if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS or any(host in {"localhost", "127.0.0.1", "[::1]"} or host.startswith(".") for host in ALLOWED_HOSTS):
        raise RuntimeError("Set DJANGO_ALLOWED_HOSTS to exact production hostnames; wildcards are not allowed.")
    required_production_settings = {
        "DATABASE_URL": database_url,
        "CLERK_PUBLISHABLE_KEY": os.environ.get("CLERK_PUBLISHABLE_KEY", "").strip(),
        "CLERK_SECRET_KEY": os.environ.get("CLERK_SECRET_KEY", "").strip(),
        "CLERK_FRONTEND_API_URL": os.environ.get("CLERK_FRONTEND_API_URL", "").strip(),
        "CLERK_AUTHORIZED_PARTIES": os.environ.get("CLERK_AUTHORIZED_PARTIES", "").strip(),
        "DJANGO_CSRF_TRUSTED_ORIGINS": os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").strip(),
        "CACHE_URL": os.environ.get("CACHE_URL", "").strip(),
    }
    missing_production_settings = [name for name, value in required_production_settings.items() if not value]
    if missing_production_settings:
        raise RuntimeError("Missing required production settings: " + ", ".join(missing_production_settings))
    authorized_parties = [origin.strip() for origin in required_production_settings["CLERK_AUTHORIZED_PARTIES"].split(",")]
    if any(not origin.startswith("https://") for origin in authorized_parties):
        raise RuntimeError("Production CLERK_AUTHORIZED_PARTIES values must use HTTPS origins.")
    if any(not origin.strip().startswith("https://") for origin in required_production_settings["DJANGO_CSRF_TRUSTED_ORIGINS"].split(",")):
        raise RuntimeError("Production DJANGO_CSRF_TRUSTED_ORIGINS values must use HTTPS origins.")
    if not database_url.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("Production DATABASE_URL must point to PostgreSQL.")
    publishable_prefix = "pk_test_" if PREVIEW else "pk_live_"
    secret_prefix = "sk_test_" if PREVIEW else "sk_live_"
    environment_name = "preview" if PREVIEW else "production"
    if not required_production_settings["CLERK_PUBLISHABLE_KEY"].startswith(publishable_prefix):
        raise RuntimeError(f"{environment_name.title()} requires a {publishable_prefix} Clerk publishable key.")
    if not required_production_settings["CLERK_SECRET_KEY"].startswith(secret_prefix):
        raise RuntimeError(f"{environment_name.title()} requires a {secret_prefix} Clerk secret key.")
    if not required_production_settings["CACHE_URL"].startswith(("redis://", "rediss://")):
        raise RuntimeError("Production CACHE_URL must use redis:// or rediss://.")
    SECURE_HSTS_SECONDS = 15_552_000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = environment_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", "False")
    SECURE_HSTS_PRELOAD = environment_bool("DJANGO_HSTS_PRELOAD", "False")
    if SECURE_HSTS_PRELOAD and not SECURE_HSTS_INCLUDE_SUBDOMAINS:
        raise RuntimeError("DJANGO_HSTS_PRELOAD requires DJANGO_HSTS_INCLUDE_SUBDOMAINS=True.")
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]

CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")
CLERK_FRONTEND_API_URL = os.environ.get("CLERK_FRONTEND_API_URL", "").removeprefix("https://").rstrip("/")
CLERK_JWKS_URL = os.environ.get("CLERK_JWKS_URL", "").strip() or (f"https://{CLERK_FRONTEND_API_URL}/.well-known/jwks.json" if CLERK_FRONTEND_API_URL else "")
CLERK_ISSUER = os.environ.get("CLERK_ISSUER", "").strip() or (f"https://{CLERK_FRONTEND_API_URL}" if CLERK_FRONTEND_API_URL else "")
CLERK_AUTHORIZED_PARTIES = [p.strip() for p in os.environ.get("CLERK_AUTHORIZED_PARTIES", "").split(",") if p.strip()]
CLERK_JWT_PUBLIC_KEY_BASE64 = os.environ.get("CLERK_JWT_PUBLIC_KEY_BASE64", "").strip()

cache_url = os.environ.get("CACHE_URL", "").strip()
if cache_url:
    CACHES = {"default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": cache_url,
        "KEY_PREFIX": os.environ.get("CACHE_KEY_PREFIX", "bill-tech"),
    }}
