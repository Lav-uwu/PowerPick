# PowerPick

A modern electrical-supplies store for electricians in **India**, built with readable HTML, CSS, JavaScript, Python Flask and a **JSON database**. No frontend build step or SQL database is required.

## Features and modules

- Catalog: 12 sample electrical products, generated images, specifications, stock, search, category/budget filters and sorting.
- Accounts: signup/login, hashed passwords, login throttling, CSRF protection, protected admin role and private order access.
- Shopping: persistent session basket, server-side prices/stock, INR, GST-inclusive totals, domestic delivery fees and Indian address validation.
- Checkout: working **demo gateway** (approve, decline, retry, cancel), cash on delivery and optional Razorpay integration.
- After ordering: history, fulfillment timeline, carrier tracking links, cancellations, return requests, refunds and verified-purchase reviews.
- Support: contact form, FAQs, administrator replies, customer message inbox and optional SMTP order emails with a retryable outbox.
- Admin: add/edit/hide products, stock control, fulfillment, tracking, return decisions, refund actions, support and communications.
- Policies: shipping, returns/refunds, warranty, privacy and terms. Generated brand icon is also the favicon.

## Codebase

```text
app.py                    Local / Vercel Flask entry point
powerpick/
  __init__.py             Configuration, sessions, CSRF and formatting
  database.py             Atomic local JSON / private Vercel Blob persistence
  utils.py                India choices, dates and record views
  cli.py                  Initialize JSON and create administrators
  routes/                 Store, accounts, admin and payment endpoints
  services/               Cart pricing, order rules, notifications and Razorpay
  templates/              Jinja HTML pages, admin screens and shared partials
public/
  css/styles.css          Responsive design tokens, layouts and states
  js/                     Progressive shopping UI and optional live checkout
  assets/                 Generated icon, hero and catalog image sheet
data/products.json        Seed catalog; edits in admin update the private database
instance/powerpick.json   Local runtime database (created locally, ignored by Git)
tests/test_store.py       Isolated workflow checks using temporary JSON
docs/DESIGN.md            Visual direction, generated assets and prompts
.env.example              Settings template; real secrets belong in .env
vercel.json               Vercel function settings
```

**Libraries:** Flask/Jinja/Werkzeug, python-dotenv and filelock. JSON, HMAC, SMTP, HTTP and testing use Python's standard library. There is no JavaScript framework or icon dependency. Vercel Blob and Razorpay use small server-side HTTP adapters.

## Run locally

Requires Python 3.11+ and pip. From the project folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Create .env only if one does not already exist:
Copy-Item .env.example .env
```

Set `SECRET_KEY` in `.env` to a long random value. A generated local `.env` may already be present; keep its secret. Keep `BLOB_READ_WRITE_TOKEN` empty to use local JSON.

```powershell
.\.venv\Scripts\python.exe -m flask --app app init-db
.\.venv\Scripts\python.exe -m flask --app app create-admin
.\.venv\Scripts\python.exe app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). `create-admin` prompts for a name, email and password; no default admin password exists. Sign in normally, then visit `/admin`. Customers can register at `/signup`. On macOS/Linux, use `.venv/bin/python` instead.

**Try checkout:** add an item → create/sign into an account → enter an Indian address → select Demo checkout → simulate approval, decline or cancellation. Demo orders never charge money. Use admin to pack, ship, deliver and review return requests. Set `DEMO_PAYMENTS=false` when you want to remove demo checkout.

## Deploy on Vercel

1. Push this folder to a Git repository and import it into Vercel. Its Flask support discovers `app.py`. Leave the output directory and build command unset; static assets live in `public/`.
2. Under **Storage**, create/connect a **private Vercel Blob store**. Vercel supplies `BLOB_READ_WRITE_TOKEN`. The database remains JSON; do not use a public Blob store for customer records.
3. Set environment variables: `SECRET_KEY` (random), `APP_URL` (your HTTPS domain), `COOKIE_SECURE=true`, `JSON_BLOB_PATH=powerpick/production.json`, and `DEMO_PAYMENTS=true` for the demo. Use a separate private store/path for preview deployments.
4. To initialize the hosted JSON and administrator, temporarily set the same Blob token/path in your local `.env`, then run `init-db` and `create-admin` above. These commands preserve existing records. Remove the token locally afterward to return to the local file database.
5. Deploy/redeploy in Vercel. Visit the site and sign in to `/admin`. Local `instance/` files are not deployed as the hosted database.

Vercel cannot persist runtime writes in a project JSON file. `database.py` stores one **private JSON blob**, bypasses its read cache and uses ETag conditional writes with bounded retries. Local writes use a cross-process file lock and atomic replacement. This simple whole-file design suits a small store/demo; large catalogs or heavy concurrent traffic need a more scalable JSON document store. Back up the private JSON regularly.

See [Vercel Flask documentation](https://vercel.com/docs/frameworks/backend/flask) and [Vercel Blob storage](https://vercel.com/docs/vercel-blob).

## Optional services and store settings

- **Live payments:** add Razorpay key ID/secret and webhook secret in environment variables. Enable automatic capture in Razorpay and configure `https://YOUR-DOMAIN/payments/webhook` for `payment.captured`, `order.paid` and `refund.processed`. Use test keys before live keys. Server verification checks HMAC, order, currency and captured amount. [Integration guide](https://razorpay.com/docs/payments/payment-gateway/web-integration/standard/integration-steps/).
- **Email:** configure the SMTP variables in `.env.example`. Messages always appear in the account inbox. SMTP sends a queued copy after successful form actions; admin can retry the next queued email. Without SMTP, guest support replies cannot be emailed.
- **Delivery:** defaults are ₹79, free over ₹1,499. Configure environment values in paise. Carrier links/statuses are entered by admin; no live courier API is connected.
- **GST:** sample catalog rates are 18%, included in product prices. Configure actual SKU rates and merchant details before launch; this is not an automated GST invoicing/filing system. Shipping is stored as the final configured delivery charge.
- **Reservations:** unpaid holds expire after 30 minutes on checkout/order visits. `flask --app app expire-payments` can also run through an external scheduler.
- **Launch content:** replace sample inventory/prices/illustrations, set `SUPPORT_EMAIL`, and complete/review policies with the actual business details and service terms. Real Razorpay, SMTP and hosted Blob require your own service accounts.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Checks cover filtering/assets, sessions/CSRF/admin access, pricing and tax, Indian addresses, competing baskets, demo outcomes, order privacy, tracking, returns, reviews, notifications and concurrent local JSON writes. External services are not called.
