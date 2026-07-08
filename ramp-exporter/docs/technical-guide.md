# Ramp -> Sage 50 Exporter

Pulls card transactions, reimbursements, and bill payments from the Ramp API, produces Sage 50-compatible CSVs, and emails them as branded HTML attachments. Runs on a schedule via Windows Task Scheduler on the CTVW-ACCT server. The code is located in [GitHub](https://github.com/Highlands-Fellowship/highlands-misc/tree/main/ramp-exporter).

This replaces a manual workflow: export CSV from Ramp's UI → run an Excel macro (`FillDistributions.bas`) to fill missing fields → import into Sage 50.

---

## Card Transactions

### How it works

1. Authenticates with the Ramp API using OAuth2 client credentials.
2. Fetches all transactions with `sync_status = SYNC_READY` (all accounting fields filled, approved in Ramp).
3. Skips any transaction missing a **Vendor ID** or **G/L Account** and logs a warning.
4. Expands each transaction's **line items** into individual Sage distribution rows. A transaction split across two expense categories becomes two rows with `Number of Distributions = 2`.
5. Builds the Sage 50 vendor-invoice CSV with all required columns and fixed values for Highlands Fellowship.
6. Emails the CSV to the configured recipient via branded HTML email.
7. Records the exported transaction IDs so they are not exported again on the next run.

### Field mapping

| Sage 50 column | Source |
|---|---|
| Vendor ID | `accounting_field_selections[type=MERCHANT].external_id` |
| Date / Date Due / Discount Date | `accounting_date` (falls back to `user_transaction_time`) |
| Description | `CardHolder FirstName LastName - memo` (e.g. `Ledonna Stuart - Lunch meeting`) |
| G/L Account | `line_items[].accounting_field_selections[type=GL_ACCOUNT].external_code` |
| Amount | `tx["amount"]` (USD) distributed proportionally across line items by local-currency ratio |
| Accounting Department | `card_holder.department_name` |
| Invoice/CM # | Auto-generated as `VendorName.MMDDYY.xxx` (max 20 chars — Sage 50 field limit). Vendor truncated to 9 chars, date as `MMDDYY`, last 3 chars of Ramp transaction ID for uniqueness across export runs |
| Number of Distributions | Count of line items on the transaction |
| Invoice/CM Distribution | 1-based index of the line item within the transaction |

Fixed values (Highlands Fellowship specific): Ship-to address, AP account `2104-AB`, terms `Net Due`, etc.

### Running

```powershell
# Full run (emails CSV, updates exported_ids.json, marks synced in Ramp)
python main.py --mark-synced

# Dry run — builds CSV, skips email and state update
python main.py --dry-run

# Pull from a specific date (ignores exported_ids.json)
python main.py --dry-run --date-from 2026-05-01

# Cap at N transactions for test imports
python main.py --dry-run --limit 1

# Inspect raw API data for a specific merchant
python main.py --dump-raw --merchant "Amazon"

# Mark specific IDs as synced without re-exporting (recovery)
python main.py --mark-synced-ids ID1 ID2 ID3

# Re-export specific transactions regardless of state file or sync status
python main.py --reexport-ids ID1 ID2 ID3
```

Import into Sage 50 via: **File → Select Import/Export → Accounts Payable → Purchases Journal → Import**

---

## Card Payments

### How it works

1. Fetches closed Ramp card statements (`statements:read` scope required).
2. Filters to statements matching `CARD_PAYMENT_ENTITY_ID` — excludes Subscription statements.
3. Selects only the **single most recent** closed statement.
4. Fetches all card transactions in that statement via the `statement_id` filter.
5. Checks `sync_status` on each transaction and takes one of the following actions:
   - **Any transaction missing a Vendor ID** → sends a warning-only email (no CSV). Set the **Accounting Vendor** field in Ramp; the next daily run will retry.
   - **Any transaction `NOT_SYNCED`** (not yet coded or approved in Ramp) → sends a warning-only email and holds. Complete expense coding in Ramp; the next daily run will retry.
   - **All transactions `SYNC_READY` or `SYNCED`, with some `SYNC_READY`** → auto-exports those transactions as a Purchases Journal CSV alongside the Payments Journal CSV, marks them synced in Ramp, and emails **both files** with numbered import instructions. Triggered when `card_payment.py` runs daily but `main.py` runs weekly.
   - **All transactions `SYNCED`** → sends the Payments Journal CSV only.
6. Regenerates invoice numbers using the **same formula** as the card transaction export (`{vendor[:9]}.{MMDDYY}.{id[-3:]}`) so Sage 50 can match payments to existing AP invoices.
7. Groups transactions by vendor. Each vendor gets a unique check number (`RAMP-MMDDYY-001`, `-002`, etc.).
8. Builds a Sage 50 **Payments Journal** CSV — one row per invoice, grouped under the vendor.
9. Emails the CSV(s) and records the statement ID in `exported_statement_ids.json` so subsequent daily runs skip it.

> **When both files are emailed:** Import the Purchases Journal first, then the Payments Journal. The email includes numbered instructions.

> **Important:** Import CSVs directly — do not open them in Excel first. Excel reformats the `Invoice Paid` values, breaking the match to existing AP invoices.

### Field mapping

| Sage 50 column | Source |
|---|---|
| Vendor ID | `accounting_field_selections[type=MERCHANT].external_id` |
| Check Number | `RAMP-MMDDYY-NNN` (unique per vendor per statement) |
| Check Name | `Ramp` (the ACH recipient) |
| Date | `end_date` of the statement (settlement/due date) |
| Memo | `Ramp Card Payment {Mon DD} - {Mon DD}, {YYYY}` (statement period) |
| Cash Account | `CARD_PAYMENT_CASH_ACCOUNT` env var (default `1003-AB`) |
| Invoice Paid | Regenerated invoice number matching the Purchases Journal import |
| G/L Account (AP clearing) | `CARD_PAYMENT_AP_ACCOUNT` env var (default `2104-AB`) |
| Total Paid on Invoice(s) | Sum of all invoices for that vendor in the statement |
| Amount | Individual invoice amount |

### Running

```powershell
# Inspect raw statement and transaction data
python card_payment.py --dump-raw

# Dry run — builds CSV, skips email
python card_payment.py --dry-run

# Full run (emails CSV for most recent closed statement)
python card_payment.py

# Bypass all hold checks and already-sent check (one-time recovery use)
python card_payment.py --dry-run --include-all
```

Import into Sage 50 via: **File → Select Import/Export → Accounts Payable → Payments Journal → Import**

---

## Reimbursements

### How it works

1. Fetches all reimbursements with `sync_status = SYNC_READY`.
2. Skips any reimbursement missing a **G/L Account** and logs a warning.
3. Builds a Sage 50 **General Journal** CSV with **four rows per reimbursement** (two journal entries):
   - **Expense entry** (dated `accounting_date`):
     - Debit: expense G/L account for each line item amount
     - Credit: ACH clearing account (`REIMBURSEMENT_CLEARING_ACCOUNT`, default `2200`)
   - **Payment entry** (dated `payment_processed_at`):
     - Debit: ACH clearing account (`REIMBURSEMENT_CLEARING_ACCOUNT`, default `2200`)
     - Credit: bank/cash account (`REIMBURSEMENT_BANK_ACCOUNT`, default `1003-AB`)
4. Emails the CSV via branded HTML email.
5. Records exported IDs in `exported_reimb_ids.json`.

### Field mapping

| Sage 50 GJ column | Source |
|---|---|
| Date (expense rows) | `accounting_date` (falls back to `transaction_date`) |
| Date (payment rows) | `payment_processed_at` |
| Reference | `Ramp Reimbursement EXP` / `Ramp Reimbursement PYMT` |
| Description | `user_full_name - memo` (e.g. `Melissa Mcfarlane - Hotel stay`) |
| G/L Account (expense debit) | `line_items[].accounting_field_selections[category_info.type=GL_ACCOUNT].external_code` |
| G/L Account (expense credit) | `REIMBURSEMENT_CLEARING_ACCOUNT` env var (default `2200`) |
| G/L Account (payment debit) | `REIMBURSEMENT_CLEARING_ACCOUNT` env var (default `2200`) |
| G/L Account (payment credit) | `REIMBURSEMENT_BANK_ACCOUNT` env var (default `1003-AB`) |
| Amount | Positive = debit, negative = credit (single Amount column) |

### Running

```powershell
# Full run (emails CSV, updates exported_reimb_ids.json, marks synced in Ramp)
python reimburse.py --mark-synced

# Dry run
python reimburse.py --dry-run

# Pull from a specific date
python reimburse.py --dry-run --date-from 2026-05-01

# Cap at N reimbursements for test imports
python reimburse.py --dry-run --limit 1

# Inspect raw API data for a specific employee
python reimburse.py --dump-raw --employee "Stuart"

# Inspect already-synced reimbursements
python reimburse.py --dump-raw --any-status

# Mark specific IDs as synced without re-exporting (recovery)
python reimburse.py --mark-synced-ids ID1 ID2
```

Import into Sage 50 via: **File → Select Import/Export → General Ledger → General Journal → Import**

---

## Bill Pay

### How it works

1. Fetches all bills with `sync_status = NOT_SYNCED` that are either `status_summary = PAYMENT_COMPLETED`, or `status_summary = PAYMENT_PROCESSING` and paid by check — checks debit the bank once mailed, well before Ramp marks the bill fully completed (ACH/wire in `PAYMENT_PROCESSING` is excluded, since those funds aren't committed yet).
2. Skips any bill missing a **Vendor ID**, **invoice number**, or **G/L Account** and logs a warning.
3. Builds **two CSVs**:
   - **Purchases Journal** (`sage_bill_purchases_YYYYMMDD.csv`) — one row per line item, same 49-column format as card transactions. Invoice numbers come directly from Ramp (no auto-generation needed).
   - **Payments Journal** (`sage_bill_payments_YYYYMMDD.csv`) — one row per bill recording the ACH/check payment.
4. Emails both CSVs as attachments via branded HTML email.
5. Records exported bill IDs in `exported_bill_ids.json`.

### Field mapping — Purchases Journal

| Sage 50 column | Source |
|---|---|
| Vendor ID | `vendor.remote_id` (falls back to `remote_code`, then `vendor.name`) |
| Invoice/CM # | `invoice_number` (from Ramp — present on all bills) |
| Date | `accounting_date` (falls back to `paid_at`, `issued_at`) |
| G/L Account | `line_items[].accounting_field_selections[category_info.type=GL_ACCOUNT].external_code` |
| Amount | `line_items[].amount.amount / minor_unit_conversion_rate` |
| Accounting Department | `accounting_field_selections[type=DEPARTMENT].external_id` |
| Number of Distributions | Count of line items on the bill |

### Field mapping — Payments Journal

| Sage 50 column | Source |
|---|---|
| Vendor ID | `vendor.remote_id` |
| Check Number | `payment.customer_friendly_payment_id` |
| Date | `payment.payment_date` (falls back to `payment.effective_date`, `paid_at`) |
| Cash Account | `BILLPAY_CASH_ACCOUNT` env var (default `1000-AB`) |
| Invoice Paid | `invoice_number` (see note below on duplicate invoice numbers) |
| G/L Account (AP clearing) | `BILLPAY_AP_ACCOUNT` env var (default `2200`) |
| Number of Distributions | Count of bills Ramp paid together in the same ACH payment |
| Total Paid on Invoice(s) | Sum of those bills' amounts (same on every row in the group) |
| Amount | This bill's own amount |

> **Multiple bills paid together:** if Ramp settles several bills to the same vendor in one ACH payment (e.g. a utility bill covering multiple meters), they share a `payment.id` and are combined into one multi-distribution Payments Journal entry — one row per bill, each showing its own amount, with the shared total and distribution count repeated on every row.

> **Duplicate invoice numbers:** some vendors reuse the same invoice number across unrelated bills over time, which Sage 50 rejects as a duplicate reference on import. Add the vendor's ID to `BILLPAY_DEDUPE_VENDORS` in `.env` (comma-separated for multiple) to append a short unique suffix to that vendor's invoice numbers only — e.g. `AP-045771` becomes `AP-045771-3f2a`. The suffix is derived from the Ramp bill ID, so it's stable across re-runs and used identically on both the Purchases and Payments Journal rows for a bill. Vendors not listed are unaffected.

### Running

```powershell
# Full run (emails both CSVs, updates exported_bill_ids.json, marks synced in Ramp)
python billpay.py --mark-synced

# Dry run — builds both CSVs, skips email and state update
python billpay.py --dry-run

# Pull from a specific date (ignores exported_bill_ids.json)
python billpay.py --dry-run --date-from 2026-05-01

# Cap at N bills for test imports
python billpay.py --dry-run --limit 1

# Inspect raw API data for a specific vendor
python billpay.py --dump-raw --vendor "Verizon"

# Inspect a vendor's bills in any sync/payment status — lists every match found
python billpay.py --dump-raw --vendor "Verizon" --any-status

# Inspect one specific bill by ID, bypassing all filters
python billpay.py --dump-raw --bill-id ID

# Mark specific IDs as synced without re-exporting (recovery)
python billpay.py --mark-synced-ids ID1 ID2

# Re-export specific bill IDs regardless of sync status (e.g. to verify a
# BILLPAY_DEDUPE_VENDORS change — a synced bill won't show up in a normal run)
python billpay.py --dry-run --reexport-ids ID1

# Full run (state update + sync), but email only you instead of NOTIFY_EMAIL
python billpay.py --mark-synced --to you@highlands.org

# Retry sync for bills deferred by a prior run (e.g. checks that have since
# cleared) — no re-export, no email
python billpay.py --reconcile
```

**Import order matters:**
1. `sage_bill_purchases_*.csv` → **File → Select Import/Export → Accounts Payable → Purchases Journal → Import**
2. `sage_bill_payments_*.csv` → **File → Select Import/Export → Accounts Payable → Payments Journal → Import**

---

## Setup

### 1. Install Python dependencies

```
pip install -r requirements.txt
```

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in:

```
RAMP_CLIENT_ID=...                     # From Ramp developer portal
RAMP_CLIENT_SECRET=...                 # From Ramp developer portal
GMAIL_USER=...                         # Gmail address to send from
GMAIL_APP_PASSWORD=...                 # Gmail App Password (not your account password)
NOTIFY_EMAIL=...                       # Who receives the CSVs (comma-separated for multiple)
```

Optional overrides (defaults shown):

```
REIMBURSEMENT_CLEARING_ACCOUNT=2200    # ACH clearing account for reimbursements
REIMBURSEMENT_BANK_ACCOUNT=1003-AB     # Bank account debited on reimbursement payment
BILLPAY_CASH_ACCOUNT=1000-AB           # Bank account debited on bill payment
BILLPAY_AP_ACCOUNT=2200                # AP clearing account for bill payments
BILLPAY_DEDUPE_VENDORS=                # Comma-separated vendor IDs whose invoice numbers
                                       # need a uniquifying suffix (see Bill Pay section)
CARD_PAYMENT_CASH_ACCOUNT=1003-AB      # Bank account debited on card statement payment
CARD_PAYMENT_AP_ACCOUNT=2104-AB        # AP account cleared by card statement payment
CARD_PAYMENT_ENTITY_ID=                # Entity ID from balance_sections[0].entity_id in
                                       # card_payment.py --dump-raw (excludes Subscription statements)
```

**Ramp API setup:** Go to Ramp Settings → Developers → Create an API app with the `transactions:read`, `reimbursements:read`, `bills:read`, and `statements:read` scopes. Copy the client ID and secret into `.env`.

**Gmail App Password:** Google Account → Security → 2-Step Verification → App passwords.

### 3. Enable Ramp API-based syncing (one-time)

Before `--mark-synced` will work on any script, run this once to register Sage 50 as the accounting connection in Ramp:

```
python setup_accounting_connection.py
```

This calls `POST /developer/v1/accounting/connection` with `{"remote_provider_name": "Sage 50"}`. Only needs to be done once per Ramp organization.

### 4. Verify API connections

```
python main.py --dump-raw
python card_payment.py --dump-raw
python reimburse.py --dump-raw
python billpay.py --dump-raw
```

### 5. Schedule on Windows

Run once from an elevated PowerShell prompt to register Task Scheduler jobs:

```powershell
# Register all four tasks
.\setup_task.ps1

# Register a specific task only
.\setup_task.ps1 -Tasks CardPayment
.\setup_task.ps1 -Tasks Card,Reimb
```

Valid task names: `Card`, `CardPayment`, `Reimb`, `Bill`

Edit `setup_task.ps1` to set `$SCRIPT_DIR`, `$PYTHON_EXE`, and the hour variables before running. Card transactions, reimbursements, and bill payments run with `--mark-synced`. Card payments (`$CARD_PMT_HOUR`, default 10 AM) require no `--mark-synced` flag — statements have no sync status in Ramp.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Card transactions entry point |
| `card_payment.py` | Card statement payments entry point |
| `reimburse.py` | Reimbursements entry point |
| `billpay.py` | Bill pay entry point |
| `ramp_client.py` | Ramp API auth, card transaction fetch, validation |
| `statement_client.py` | Ramp API fetch for card statements and their transactions |
| `reimbursement_client.py` | Ramp API fetch for reimbursements, journal row expansion |
| `billpay_client.py` | Ramp API fetch for bills, purchase/payment row expansion |
| `sage_formatter.py` | Builds the Sage 50 vendor-invoice CSV (used by card and bill pay) |
| `card_payment_formatter.py` | Builds the Sage 50 Payments Journal CSV for card statements |
| `reimbursement_formatter.py` | Builds the Sage 50 General Journal CSV |
| `billpay_payment_formatter.py` | Builds the Sage 50 Payments Journal CSV for bill payments |
| `emailer.py` | Sends CSV(s) as branded HTML email via Gmail |
| `email_template.py` | Highlands Fellowship branded HTML email templates |
| `setup_accounting_connection.py` | One-time Ramp accounting connection setup (run before --mark-synced) |
| `setup_task.ps1` | Registers the Windows Task Scheduler jobs |
| `.env.example` | Secrets template — copy to `.env` |
| `exported_ids.json` | State file for card transaction IDs (auto-created) |
| `exported_statement_ids.json` | State file for card statement IDs (auto-created) |
| `exported_reimb_ids.json` | State file for reimbursement IDs (auto-created) |
| `exported_bill_ids.json` | State file for bill IDs (auto-created) |
| `pending_sync_ids.json` | Bills exported but not yet fully synced in Ramp — retry with `billpay.py --reconcile` (auto-created) |
| `output\` | Generated CSVs (auto-created) |
| `logs\` | Daily log files (auto-created) |

---

## Transactions skipped at export time

Any transaction missing required fields is skipped and logged — it will not appear in the CSV. It is also listed in the notification email with the reason. Skipped items are picked up automatically on the next run once fixed.

| Warning | Fix in Ramp |
|---|---|
| `missing Vendor ID` | Open the transaction → set the **Accounting Vendor** field |
| `line item N missing G/L Account` | Open the transaction → set the **Category/GL Account** for that split |
| `missing G/L Account` (reimbursement) | Open the reimbursement → set the **Category/GL Account** |
| `missing Vendor ID` (bill) | Open the bill → set the vendor's **Remote ID** in Ramp settings |
| `missing invoice number` (bill) | Open the bill → add an invoice number |
| `line item N missing G/L Account` (bill) | Open the bill → set the **GL Account** for that line item |
| `missing Vendor ID` (card payment) | Open the transaction → set the **Accounting Vendor** field; or use `--include-all` for a one-time recovery run |
