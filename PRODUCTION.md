# Production readiness

This checklist covers a first production deployment of the mobile shop ERP. Complete each environment-specific item before staff use live customer or stock data.

## 1. Provision the services

- Create a production Supabase project. Configure the Auth email templates, redirect URLs, and allowed site URLs for the deployed web origins.
- Choose the Django hosting platform, public hostname, and supported PostgreSQL connection mode. Use Supabase's direct connection where the host supports IPv6, or its session pooler for persistent application connections. Confirm the provider's current guidance for your host before deployment.
- Create a dedicated PostgreSQL application role with only the privileges required by the application. Do not use a Supabase service role or expose database credentials to browser code.
- Configure TLS from Django to PostgreSQL and HTTPS from browsers to Django. Set the proxy's forwarded-protocol header only when the hosting platform controls it.

## 2. Configure secrets and origins

Set these values in the hosting provider's secret manager, not in source control. The application refuses to start in production when required security settings are missing or weak:

| Variable | Production value |
| --- | --- |
| `DJANGO_SECRET_KEY` | Unique, randomly generated secret; never the development default |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_PREVIEW` | `False` for production; set `True` only for a protected Render preview using a Supabase test project |
| `DJANGO_ALLOWED_HOSTS` | Exact application hostnames, comma-separated |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | HTTPS origins for the Django site, comma-separated |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase public anon/publishable key |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only key for administrative Auth operations |
| `SEED_SHOP_NAME` | Optional one-time shop name for `bootstrap_shop` |
| `SEED_SHOP_CURRENCY` | Optional one-time currency for `bootstrap_shop` (defaults to `INR`) |
| `SEED_ADMIN_SUPABASE_USER_ID` | Optional one-time Supabase user ID for the Admin seed command |
| `SEED_ADMIN_EMAIL` | Optional one-time email for the Admin seed command |
| `SEED_ADMIN_DISPLAY_NAME` | Optional one-time display name for the Admin seed command |
| `DATABASE_URL` | Supabase PostgreSQL URL for the chosen connection mode |
| `CACHE_URL` | Shared Redis URL (`rediss://` preferred) for production invitation/session caches |
| `DB_CONN_MAX_AGE` | Persistent connection lifetime in seconds; use `0` for transaction pooling |
| `DB_DISABLE_SERVER_SIDE_CURSORS` | Set `True` when using transaction pooling |
| `DJANGO_HSTS_INCLUDE_SUBDOMAINS` | Enable only after confirming all subdomains are HTTPS-only |
| `DJANGO_HSTS_PRELOAD` | Enable only after an explicit domain/preload review; requires include-subdomains |
| `CACHE_KEY_PREFIX` | Unique prefix when sharing a Redis instance across environments |

`SUPABASE_ISSUER` and `SUPABASE_JWKS_URL` can be left blank; they are derived from `SUPABASE_URL`. Never put the service-role key, database URL, or Django secret in templates or JavaScript.

## 3. Deploy and bootstrap

1. Install the dependency constraints in `requirements.txt` in the deployment build.
2. Run `python manage.py check --deploy` with production environment variables.
3. Run `python manage.py migrate` as a controlled release step against the production database.
4. Run `python manage.py collectstatic --noinput` and serve static files through the selected host/CDN.
5. Create the first Admin with `python manage.py bootstrap_shop` using the Supabase Auth user ID and intended shop details. For an existing one-shop database, use `python manage.py seed_admin` to link or create the Admin membership. Keep the seed values only as long as needed and never store a password in environment variables.
6. Sign in as the Admin, verify shop settings, and invite one Staff member with only the permissions needed for their work.

### Render web service

Configure the existing Render service with the repository root as its root directory and the **Python 3** runtime:

| Setting | Value |
| --- | --- |
| Build command | `python -m pip install -r requirements.txt && python manage.py collectstatic --noinput` |
| Start command | `python -m gunicorn config.wsgi:application --bind 0.0.0.0:$PORT` |
| Health check path | `/health/` |

Install requirements on every build: Gunicorn is the production WSGI server and must be installed before the start command runs. WhiteNoise serves the collected static files from the web process. The Django project module is `config`; use its existing WSGI application. Set all environment variables from section 2 in Render's environment settings, including `DJANGO_SECRET_KEY` (the app does not read `SECRET_KEY`). Keep `DJANGO_DEBUG=False`. For a temporary Render preview, set `DJANGO_PREVIEW=True` and use a separate Supabase test project; never use that project for production.

Run `python manage.py migrate --noinput` as the controlled release step before routing traffic to the new version. Arrange static file hosting for `staticfiles/` as required above; Gunicorn alone does not serve those assets. Keep the existing Supabase PostgreSQL and Redis services.

The failure notification for commit `9d11cfa64d4fcffdff58196bf57d21d1ac37104b` identifies the failed deploy but contains no error output. Its [deploy logs](https://dashboard.render.com/web/srv-dbuv1hmc6qcc73cqb5i0/deploys/dep-dbuv1i6c6qcc73cqb6i0) require authenticated dashboard access. The repository was missing Gunicorn from `requirements.txt`, so a requirements-only installation could not run the start command. This is a confirmed repository startup blocker; the exact error from that historical deploy remains unverified until the logs are available.

## 4. Verify the live setup

- Confirm unauthenticated requests are redirected to sign-in and that invalid, expired, or wrong-issuer Supabase sessions fail closed.
- Confirm Staff cannot access another member's receipts or shop-wide sales unless the relevant permission is granted. Confirm Admin-only pages reject Staff at the server.
- Import a small sample CSV in a non-production environment first. Verify preview validation, duplicate SKU/IMEI rejection, opening movements, and no writes until confirmation.
- Complete a controlled sale, service-only bill, used-phone intake, return, void, stock adjustment, and CSV import. Reconcile the resulting stock movements, payment records, and audit events.
- Confirm HTTPS, secure session and CSRF cookies, trusted host/origin settings, static asset delivery, and `/health/` readiness checks for PostgreSQL and Redis.
- Django’s deploy check reports HSTS warnings until subdomain and preload settings are explicitly reviewed. Do not enable preload to silence a warning without confirming the whole domain is HTTPS-only.
- Retry the exact same checkout submission and confirm it returns the original receipt without creating duplicate payments or stock movements.

## 5. Operations and recovery

- Use an always-on web compute plan for the live shop. Free Render web services can sleep and share a workspace usage allowance; Free Key Value instances can restart without preserving cache data. The app handles a temporary cache outage by reverifying Supabase identity and checking session audit history in PostgreSQL, rather than allowing Redis failure to crash every protected page.
- Monitor `/health/` from outside the service and enable Render failure notifications. A healthy response is HTTP 200 with `ok`; HTTP 503 with `unavailable` means the database or cache check failed. Safe log messages identify the failing component without printing connection strings. The current live Render service uses TCP health checks; HTTP readiness settings must be reviewed separately so a cache-only outage does not unnecessarily remove a functioning app from service.
- Database and identity-provider outages fail closed with a temporary-unavailability response. Submitted transactions are never automatically replayed. If a connection failed while saving a sale, check for its receipt before trying again; reuse the existing request key when retrying a known checkout submission.
- Enable scheduled Supabase backups/PITR appropriate to the business's recovery objectives. Document who can restore a backup and periodically rehearse restoration into a separate database.
- Send application and platform errors to a monitored logging/alerting service. Keep logs free of session tokens, secrets, and unnecessary customer data. Alert on failed database connections, elevated 5xx rates, and failed scheduled backups.
- Track deployment revisions and migration status. Back up before risky schema or data changes and use a reviewed rollback/forward-fix procedure.
- Review Staff membership and permissions when roles change. Deactivate access promptly when a Staff member leaves.
- Keep dependencies updated and review security advisories as part of routine maintenance.

## Current launch limitations

- This repository does not choose or configure a hosting provider, domain, or Supabase project. Those must be provisioned and verified for the selected environment.
- Reports are operational summaries and require reconciliation against the shop's accounting and tax process before they are used for statutory reporting.
- Audit history is available in the Admin UI and is append-only through normal application flows. It is not a tamper-proof external ledger; restrict database access and include the database in backup and retention controls.
- Production requires a shared Redis cache so session-verification cache behavior is consistent across workers. Set `CACHE_URL` to a TLS-protected Redis URL and monitor connectivity.
- Configure rate limiting and edge protection at the selected host or proxy before public production traffic.
- Production credentials and a hosting platform are not provisioned in this workspace. Code-level readiness checks can run here, but the live deployment checklist still needs to be completed in the selected hosting environment. Rotate any credentials previously pasted into chat before production use.
