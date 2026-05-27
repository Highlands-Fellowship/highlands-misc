# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Pulls card transactions, reimbursements, and bill payments from the Ramp API, formats them as Sage 50-compatible CSVs, and emails the files via Gmail. Runs daily on Windows via Task Scheduler, replacing a manual Ramp UI export + Excel macro workflow.

Three independent entry points, each with its own state file and email:
- `main.py` — card transactions → Sage 50 **Purchases Journal** (`sage_formatter.py`)
- `reimburse.py` — reimbursements → Sage 50 **General Journal** (`reimbursement_formatter.py`)
- `billpay.py` — bill payments → Sage 50 **Purchases Journal** + **Payments Journal** (`sage_formatter.py` + `billpay_payment_formatter.py`)

## Running the scripts

```powershell
# Inspect raw Ramp API output — do this first when verifying field names
python main.py --dump-raw
python reimburse.py --dump-raw --employee "LastName"
python billpay.py --dump-raw --vendor "VendorName"

# Dry run — build CSV(s), skip email/state/sync
python main.py --dry-run
python reimburse.py --dry-run
python billpay.py --dry-run

# Pull from a specific date (ignores state file)
python main.py --dry-run --date-from 2026-05-01

# Production runs (email, state update, mark synced in Ramp)
python main.py --mark-synced
python reimburse.py --mark-synced
python billpay.py --mark-synced

# Recovery: mark specific IDs synced without re-exporting
python main.py --mark-synced-ids ID1 ID2
python reimburse.py --mark-synced-ids ID1
python billpay.py --mark-synced-ids ID1
```

## Required `.env` file

Copy `.env.example` to `.env`. Required keys: `RAMP_CLIENT_ID`, `RAMP_CLIENT_SECRET`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`. Optional: `OUTPUT_DIR`, `REIMBURSEMENT_CLEARING_ACCOUNT` (default `2200`), `REIMBURSEMENT_BANK_ACCOUNT` (default `1003-AB`), `BILLPAY_CASH_ACCOUNT` (default `1000-AB`), `BILLPAY_AP_ACCOUNT` (default `2200`).

## One-time setup: Ramp accounting connection

Before `--mark-synced` works on any script, run once:
```
python setup_accounting_connection.py
```
This calls `POST /developer/v1/accounting/connection` with `{"remote_provider_name": "Sage 50"}`. Without this, the sync endpoint returns a 400 `DEVELOPER_7089` error.

## Architecture

### Ramp API patterns (shared across all three pipelines)

- **Auth:** OAuth2 client credentials — `POST /developer/v1/token` with `auth=(client_id, client_secret)`. Separate token calls for read-only vs. write (scope includes `accounting:write` when `--mark-synced`).
- **Pagination:** `page.next` in the response body is either a full URL or a cursor string. All three clients handle both forms.
- **Sync endpoint:** `POST /developer/v1/accounting/syncs` (plural, not `/sync`). Body requires `idempotency_key` (UUID), `sync_type` (`TRANSACTION_SYNC` / `REIMBURSEMENT_SYNC` / `BILL_SYNC`), and `successful_syncs[{id, reference_id}]`.
- `from_date` params must be full ISO datetimes (`2026-01-01T00:00:00Z`), not date-only strings.

### Card transactions (`ramp_client.py` → `sage_formatter.py`)

- Filters client-side: `sync_status == "SYNC_READY"` (no server-side filter available)
- Key field locations (confirmed from live data):
  - Vendor ID: `accounting_field_selections[type="MERCHANT"].external_id`
  - GL Account: `line_items[].accounting_field_selections[type="GL_ACCOUNT"].external_code` (use `external_code`, not `external_id`)
  - Department: `card_holder.department_name`
  - Amount: `line_items[].amount.amount / minor_unit_conversion_rate` (minor units)
  - Date: `accounting_date` → `user_transaction_time`
- Invoice numbers auto-generated as `VendorName.MM.DD.YYYY.xxxxxx` (last 6 chars of `tx["id"]`); stable across export runs — same transaction always produces the same invoice number
- State file: `exported_ids.json`

### Reimbursements (`reimbursement_client.py` → `reimbursement_formatter.py`)

- Filters: `sync_status == "SYNC_READY"` on `GET /developer/v1/reimbursements`
- Key field locations (confirmed from live data):
  - Employee name: `user_full_name` (top-level string — `employee.first_name/last_name` are `None`)
  - GL Account: `line_items[].accounting_field_selections[category_info.type="GL_ACCOUNT"].external_code` (type is under `category_info`, not at top level)
  - Expense date: `accounting_date` → `transaction_date` → `created_at`
  - Payment date: `payment_processed_at`
- **4 rows per reimbursement** (two journal entries, `row_role` field drives account substitution):
  - `expense_debit` — expense GL, positive amount, dated `accounting_date`
  - `expense_credit` — clearing account (`REIMBURSEMENT_CLEARING_ACCOUNT`), negative, dated `accounting_date`
  - `payment_debit` — clearing account, positive, dated `payment_processed_at`
  - `payment_credit` — bank account (`REIMBURSEMENT_BANK_ACCOUNT`), negative, dated `payment_processed_at`
- 14-column General Journal CSV: single `Amount` column (positive=debit, negative=credit)
- State file: `exported_reimb_ids.json`

### Bill pay (`billpay_client.py` → `sage_formatter.py` + `billpay_payment_formatter.py`)

- Filters: `sync_status == "NOT_SYNCED"` AND `status_summary == "PAYMENT_COMPLETED"` (bills have no `SYNC_READY` status)
- Key field locations (confirmed from live data):
  - Vendor ID: `vendor.remote_id` → `vendor.remote_code` → `vendor.name`
  - Invoice number: `invoice_number` (always present — no generation needed)
  - GL Account: `line_items[].accounting_field_selections` — check BOTH `sel.get("type")` and `sel.get("category_info", {}).get("type")` for `"GL_ACCOUNT"` (bills may store it under either)
  - Department: top-level `accounting_field_selections[type="DEPARTMENT"].external_id`
  - Date: `accounting_date` → `paid_at` → `issued_at`
  - Payment check number: `payment.customer_friendly_payment_id`
  - Payment date: `payment.payment_date` → `payment.effective_date` → `paid_at`
- `fetch_completed_bills` returns a 3-tuple `(purchase_rows, payment_rows, skipped)`
- Purchase rows reuse `sage_formatter.build_csv()` (same 49-column format as card transactions)
- Payment rows go to `billpay_payment_formatter.build_csv()` — 39-column Payments Journal CSV
- Both CSVs emailed together as attachments; import Purchases first, then Payments
- State file: `exported_bill_ids.json`

### Shared modules

**`sage_formatter.py`** — 49-column Sage 50 vendor-invoice CSV. `_FIXED` dict holds Highlands Fellowship constants (ship-to address, AP account `2104-AB`, etc.). No grouping logic — distributions pre-computed by client modules.

**`emailer.py`** — `send_csv(gmail_user, gmail_app_password, to_address, subject, body_plain, csv_data, filename, body_html=None, extra_attachments=None)`. Sends `multipart/mixed` with `multipart/alternative` inner part (plain + HTML) plus one or more CSV attachments. `extra_attachments` is a list of `(csv_data, filename)` tuples.

**`email_template.py`** — Highlands Fellowship branded HTML. Three builders: `build_card_email`, `build_reimbursement_email`, `build_billpay_email`. All return `(html, plain_text)`. Import path shown in cream/teal box; skipped transactions in yellow warning box.
