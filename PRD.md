# Mobile Shop ERP PRD

**Status:** MVP baseline

**Audience:** One mobile phone shop, managed by an Admin and operated by Staff

**Product:** Next, a server-rendered Django application with Supabase Auth and Supabase PostgreSQL

## 1. Purpose

Give a small mobile shop one beginner-friendly place to record sales, stock, supplier details, returns, expenses, and operating reports. The MVP prioritizes accurate bills and stock counts, clear access controls, and a fast checkout flow.

## 2. Users and access

- **Admin:** Manages the shop, staff access, settings, products, costs, reports, suppliers, expenses, and sales corrections.
- **Staff:** Uses checkout and only the additional capabilities granted by the Admin.
- Supabase Auth handles sign-in, sessions, Admin-created accounts, and password recovery. An Admin can create an Admin or Staff account with an email and temporary password, then share the password directly with that person. Admin accounts receive full access; Staff accounts receive only selected extra permissions. The app does not email invitations. Public registration and shared user accounts are not supported.
- Existing legacy memberships are linked to Supabase on first sign-in only when the confirmed Supabase email matches one active membership.
- Protected pages refresh the Django session cookie from the Supabase client session during navigation and form entry.
- Django resolves the verified Supabase identity to an active shop membership and enforces every permission on the server. The MVP is isolated to one shop per deployment.

## 3. Product scope

### Catalog and inventory

- Products have a name, optional SKU/category/model/supplier, kind, second-hand flag, price, optional cost, reorder level, and active state.
- Product creation counts stock in **pieces**. The catalog does not offer an IMEI tracking choice. A starting quantity creates a stock movement.
- Services can be billed in POS without changing stock.
- The existing serialized-device model supports previously recorded/imported IMEI inventory and checkout. The current Add Product form creates quantity-tracked products.
- Stock receives, opening quantities, sales, returns, voids, and count corrections are represented by stock movements. Posted movements are not silently overwritten.
- The Inventory header offers Add product; Receive stock is accessed from Suppliers & purchases.
- Supplier selection is shop-scoped. Supplier records show assigned products, summarized receipt activity, and the date added from their creation audit record. Older records without that event display “Not recorded”.

### Checkout and receipts

- Staff can find products and services, prepare a bill, choose cash, UPI, card, bank transfer, or another tender, and post a receipt.
- Prices, tax, totals, and receipt snapshots are calculated on the server with decimal arithmetic.
- Printed bills use a compact thermal receipt layout and follow the paper size selected in the printer settings.
- Admins can set a shop phone number in Shop settings. New bills print it below the shop address and snapshot it with the sale so existing receipts retain their original contact details.
- A repeated checkout submission with the same request key returns the original receipt instead of posting a second sale, payment, or stock movement.
- Services do not decrement inventory. Missing service costs do not block an estimate; service costs are excluded unless recorded.

### Returns, corrections, and expenses

- Returns reference the original sale, record a reason and disposition, create a linked refund, and restore stock only when returned as sellable. Sellable stock is the default disposition; staff can choose inspection or damaged/write-off when needed.
- Voids and expense corrections create linked reversal records.
- Shop expenses reduce estimated profit for their recorded date.

### Overview and reports

- The application brand shown in navigation and page titles is **Next**. The supplied blue “n” image is used for the favicon and the navigation and sign-in logos.
- The UI uses a white canvas, horizontal top navigation, blue primary actions, and restrained colored metric accents. The Overview places the seven-day trend beside low stock, followed by recent sales and the existing category charts.
- The Overview summarizes today's activity, low stock, recent sales, a seven-day sales graph, sales category mix, and current stock category mix.
- Reports show date-filtered sales, returns, tax, tender, stock, product, expense, and profit summaries. Statutory accounting remains outside the MVP and reports must be reconciled to the shop's accounting process.
- The report summary includes a separate Expenses card for the selected dates, after linked corrections, with the same access requirements as estimated profit.
- Cost and profit information is permission-controlled.

## 4. Core acceptance criteria

- An unauthenticated or invalid Supabase session cannot access protected shop data.
- Only an Admin can create Admin or Staff Auth accounts; the temporary password is sent only to Supabase Auth over TLS and is never stored in application data or logs.
- Staff permissions are checked on the server; one shop's supplier/product data cannot be selected by another shop.
- A valid checkout posts one sale, payment, and matching stock movement in a database transaction. Retrying the same request does not duplicate them.
- A return is linked to its original receipt and cannot exceed the unreturned quantity or paid amount.
- Quantity stock changes are explainable from their movement history.
- Services do not change stock. Reports distinguish missing stock costs from service costs that are not recorded.
- A public health endpoint verifies database and cache availability without returning infrastructure details.

## 5. Technical and operational requirements

- Backend and server-rendered UI: Python and Django templates.
- Authentication: Supabase Auth; authorization and shop isolation: Django.
- Production database: Supabase PostgreSQL over TLS, with Django ORM migrations as the schema source of truth.
- Production requires `DEBUG=False`, a strong unique Django secret, exact hosts and HTTPS origins, Supabase Auth keys, PostgreSQL, and a shared Redis cache.
- Hosted previews may use `DEBUG=False` with `DJANGO_PREVIEW=True` and Supabase test-project keys.
- Production operations require managed HTTPS, static file hosting, monitored errors, database backups, and a rehearsed restore process.
- Deployment provider, production domain, Redis service, backup policy, and alerting provider are environment decisions and are not provisioned by this repository.

## 6. Out of scope

- Multi-shop tenancy in one deployment, public sign-up, custom roles, and a JavaScript SPA.
- A full repair/work-order system, payment terminal integration, automatic payment processing, accounting ledger, or statutory tax filing.
- Production hosting or live service provisioning from this repository.
