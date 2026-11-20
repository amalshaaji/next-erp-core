# Mobile Shop ERP

Starter implementation for the product described in [PRD.md](PRD.md). Built with Django templates, Supabase Auth, and Supabase-hosted PostgreSQL (SQLite is used when `DATABASE_URL` is blank for local development).

## Current implementation

- Supabase session verification middleware and Admin/Staff shop membership.
- Beginner-friendly overview, product catalog, checkout, and printable receipt screens.
- Quantity and IMEI-tracked inventory, used-phone intake with condition/cost fields, and stock receiving.
- Service items can be billed in POS without changing stock.
- Server-side shop scoping, permission checks, Supabase Auth staff invitations/access management, and stock movement records.
- Receipt-linked partial returns and manually recorded refunds with serialized device disposition.
- Initial shop/Admin provisioning command.
- Admin shop settings for business name, currency, time zone, tax rate, and receipt footer; completed sales retain shop-name, currency, tax-rate, and receipt-footer snapshots.
- Supplier records with archiving, supplier-linked stock receiving, and recent purchase history.
- Date-filtered reports for net sales/refunds, tender totals, low stock, product/service revenue, known costs/margins, and permission-gated CSV export.
- Permission-controlled quantity stocktakes and IMEI-based serialized device status corrections with required reasons and movement history.
- Permission-controlled full-sale voids linked to original receipts, reversing payment records and restoring stock transactionally.
- Staff can see their own sales and receipts; Admins with sales visibility can see shop-wide sales.
- CSV product import includes field validation, duplicate IMEI/SKU checks, a full preview, and a transactional confirm step for opening inventory.
- Admin-only append-oriented audit history for sensitive shop, access, sales, and inventory activity.

Admin can invite Staff through Supabase Auth, select their additional permissions, revoke pending invitations, edit permissions, and deactivate shop access. Returns are linked to original receipts and post manual refunds with stock disposition. Review report methodology and reconcile it against the shop's accounting/closeout process before production use.

## Project layout

```text
config/                 Django project settings, URLs, and WSGI entry point
shop/                   Main domain app
  management/commands/  Shop bootstrap and other Django commands
  migrations/           Versioned PostgreSQL/SQLite schema migrations
  services/             Supabase Auth integration and reporting services
  static/shop/          App-owned CSS and front-end assets
  templates/shop/       App-owned Django templates
  templatetags/         Small presentation-only template helpers
  models.py             Shop, membership, inventory, purchase, sale, and return data
  forms.py              Validated user input forms
  views.py              Server-rendered request handlers
PRD.md                  Product requirements
AGENTS.md               Coding-agent project instructions
```

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and set `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and the server-only `SUPABASE_SERVICE_ROLE_KEY`. The app derives the JWT issuer and JWKS URL from `SUPABASE_URL` unless you provide overrides. Set the `SEED_*` fields for Admin/shop setup; never put a password in `.env`.
4. Leave `DATABASE_URL` blank to use local SQLite, or set it to your Supabase PostgreSQL connection string. Keep database credentials server-side.
5. Run `python manage.py migrate`.
6. Create the first shop and Admin after creating the Supabase Auth user. The command reads its defaults from `SEED_SHOP_NAME`, `SEED_SHOP_CURRENCY`, and `SEED_ADMIN_*`:

   ```sh
   python manage.py bootstrap_shop
   ```

   For an existing one-shop database, run `python manage.py seed_admin` to link or create Admin access idempotently from `SEED_ADMIN_*`.

7. Start Django with `python manage.py runserver` and open `http://127.0.0.1:8000/`.

For production, set `DJANGO_DEBUG=False`, use a unique random `DJANGO_SECRET_KEY` of at least 50 characters, configure exact allowed hosts and HTTPS origins, use production Supabase credentials, and provide a shared Redis `CACHE_URL`. Startup fails closed if required production settings are missing. Choose `DB_CONN_MAX_AGE` and `DB_DISABLE_SERVER_SIDE_CURSORS` for the selected Supabase pooler mode; see [PRODUCTION.md](PRODUCTION.md).

See [Production readiness](PRODUCTION.md) for the launch checklist, environment configuration, backups, and current pilot limitations.
