# Agent Instructions

## Product source of truth

- Read [PRD.md](PRD.md) before planning or changing product behavior.
- Treat the PRD as the product baseline. If implementation details are undecided there, inspect the existing code and choose the smallest consistent change. Record meaningful product decisions in the PRD.
- Keep the MVP focused on a single mobile shop with two account roles: **Admin** and **Staff**.

## Product requirements to preserve

- Use **Supabase Auth** for identity and authentication, including sign-in, invitations, password recovery, and session handling.
- Enforce shop membership, Admin/Staff access, and individual Staff permissions on the server. Do not trust client-only role state or hidden UI controls as authorization.
- Do not add public sign-up, shared accounts, or custom roles unless the user explicitly changes the product requirements.
- Track serialized phones by unique IMEI/serial. Track second-hand phones individually with condition, acquisition source/date/cost, and linked sales history.
- Support service items in POS as non-stock billable lines. Service lines must not change inventory. A full repair/work-order system is outside the MVP.
- Keep checkout, inventory, purchasing, used-phone intake, and reports understandable to users with basic computer skills. Prefer plain labels, guided flows, useful empty states, and clear validation.
- Preserve transaction and stock-movement audit history. Correct posted records with linked reversal/refund/adjustment events rather than silently overwriting history.

## Approved technology baseline

- **Backend:** Python with Django.
- **Frontend:** Django templates and HTML rendered by Django. Keep the UI server-rendered unless the user approves a different frontend architecture.
- **Database:** Supabase-hosted PostgreSQL. Django is the application database client and source of schema migrations.
- **Authentication:** Supabase Auth. Do not replace Supabase Auth with another provider.
- **Authorization:** Django validates Supabase identity/session context, resolves active shop membership, and enforces Admin/Staff permissions on the server.
- Use Django Admin only for trusted back-office maintenance. Build the Staff checkout as a separate, beginner-friendly Django template flow.
- Use Django ORM and Django migrations for application data. Do not make schema changes through ad hoc dashboard edits that bypass migrations.
- Keep Supabase database credentials server-side, use TLS, and follow Supabase's recommended connection mode for the selected deployment environment. Never expose a database connection string or service credential to browser code.
- Configure and monitor database connections for the selected Supabase pooling mode and hosting environment.
- Do not add a JavaScript SPA framework, separate API service, or additional hosted backend without a clear product need and user approval.

## Implementation guidance

- Before editing, inspect the existing project structure, framework, dependencies, and local conventions. Do not introduce a new stack or replace existing architecture without a clear need.
- Keep changes focused and consistent with the product scope. Avoid adding features excluded from the MVP in the PRD.
- Keep Supabase service-role keys and private credentials out of source control and client-side code. Use the repository's established environment configuration patterns.
- Validate Supabase sessions server-side and resolve the current active shop membership and permissions for protected operations. Fail closed if identity or authorization cannot be verified.
- Validate Supabase session signature, issuer, audience, and expiry. Prefer cached public key/JWKS verification when supported; verify HS256 sessions through the Supabase Auth user endpoint.
- Treat sales, payments, returns, and inventory movements as linked records. Prevent duplicate posting on retries and preserve historical snapshots used by completed transactions.
- Keep used-phone intake connected to resale: the same IMEI record carries condition and acquisition cost into checkout and the sale history.
- Do not expose password/session tokens, sensitive customer data, or protected cost data in logs or unauthorized views.
- Follow existing accessibility and responsive design conventions; prioritize keyboard and tablet use for point of sale.

## Tools and skills

Use the tools already present in the repository before introducing new dependencies. The implementation will need these capabilities:

- **Web application development:** build accessible, responsive forms, tables, navigation, and a fast point-of-sale flow.
- **Supabase Auth integration:** configure Supabase Auth sign-in in Django templates, verify sessions on the server, manage invitations, and handle identity lifecycle events securely.
- **Database and migrations:** use a relational data model with schema migrations for shops, users/memberships, products, serialized devices, stock movements, purchases, sales, payments, returns, services, and audit events.
- **Authorization:** implement and review server-side permission checks and shop data isolation for every protected route and operation.
- **Data validation:** validate user input and CSV imports at the server boundary; give users actionable field-level errors.
- **Financial and inventory integrity:** use safe decimal arithmetic, transactional posting, idempotency, and linked reversal records.
- **Operational readiness:** configure environment variables/secrets, logging, monitoring, backups, and deployment using the hosting platform selected for the project.
- **Verification:** use the repository's established formatting, linting, type-checking, and test tools. Add coverage for permissions, IMEI uniqueness, checkout retries, returns, and stock movements when implementation begins.

The application stack above is approved. Still inspect the repository before choosing exact package versions, deployment settings, or optional libraries. Prefer official Django and Supabase documentation for integration details.

## Working and verification

- Make the requested change directly when the requirements are clear. Ask only when a decision materially affects product behavior, security, data integrity, or external services and cannot be resolved from the PRD or codebase.
- Before finishing implementation work, review the diff for scope, permission enforcement, and accidental secret or data exposure.
- Run relevant checks when the user asks for verification or when repository instructions require them; report exactly what was run and any remaining issue.
- Update [PRD.md](PRD.md) when a user-approved product decision changes scope or behavior.
