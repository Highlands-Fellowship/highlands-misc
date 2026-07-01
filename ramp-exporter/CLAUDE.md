# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Pulls card transactions, reimbursements, and bill payments from the Ramp API, formats them as Sage 50-compatible CSVs, and emails the files via Gmail. Runs daily on Windows via Task Scheduler, replacing a manual Ramp UI export + Excel macro workflow.

Four independent entry points, each with its own email:
- `main.py` — card transactions → Sage 50 **Purchases Journal** (`sage_formatter.py`)
- `card_payment.py` — card statement payments → Sage 50 **Payments Journal** (`card_payment_formatter.py`) — clears the open AP invoices created by `main.py`
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
python card_payment.py          # no --mark-synced needed (no Ramp sync for payments)

# Recovery: mark specific IDs as exported without re-running
python main.py --mark-synced-ids ID1 ID2
python reimburse.py --mark-synced-ids ID1
python billpay.py --mark-synced-ids ID1
```

## Required `.env` file

Copy `.env.example` to `.env`. Required keys: `RAMP_CLIENT_ID`, `RAMP_CLIENT_SECRET`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`. Optional: `OUTPUT_DIR`, `REIMBURSEMENT_CLEARING_ACCOUNT` (default `2200`), `REIMBURSEMENT_BANK_ACCOUNT` (default `1003-AB`), `BILLPAY_CASH_ACCOUNT` (default `1000-AB`), `BILLPAY_AP_ACCOUNT` (default `2200`), `CARD_PAYMENT_CASH_ACCOUNT` (default `1003-AB`), `CARD_PAYMENT_AP_ACCOUNT` (default `2104-AB`).

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

### Card statement payments (`statement_client.py` → `card_payment_formatter.py`)

- Always exports the **single most recent** closed statement
- Filters `GET /developer/v1/statements` by `end_date < now` and `CARD_PAYMENT_ENTITY_ID` to exclude Subscription statements
- Fetches transactions via `statement_id` filter on `/transactions` (not a date range)
- **Check order** (each hold sends a warning-only email with no CSV):
  1. Missing Vendor ID → hold; fix Accounting Vendor in Ramp
  2. Any `NOT_SYNCED` → hold; transaction needs coding/approval in Ramp
  3. Any `SYNC_READY` (all others `SYNCED` or `SYNC_READY`) → **auto-export**: call `ramp_client.expand_transactions()` to build a Purchases Journal CSV, email both CSVs with numbered import instructions, mark synced in Ramp, update `exported_ids.json` so `main.py` skips them on its next weekly run
  4. Already sent (statement ID matches `exported_statement_ids.json`) → skip
  5. All `SYNCED` → send Payments Journal CSV only
- `--include-all` bypasses all three hold checks (recovery use)
- Regenerates invoice numbers using the **same stable formula** as card transactions: `{vendor[:9]}.{MMDDYY}.{id[-3:]}` — must match exactly
- Groups transactions by `vendor_id` — one logical payment per vendor per statement
- Check numbers: `RAMP-MMDDYY-001`, `-002`, etc. per vendor (unique per statement, Sage 50 rejects duplicates)
- After sending, records statement ID in `exported_statement_ids.json` — subsequent daily runs skip it
- Produces multi-distribution payment rows: `num_distributions` = invoices per vendor, `total_amount` = vendor subtotal, `amount` = individual invoice amount
- `CARD_PAYMENT_CASH_ACCOUNT` (default `1003-AB`) — bank account debited
- `CARD_PAYMENT_AP_ACCOUNT` (default `2104-AB`) — AP account cleared (must match what Purchases Journal used)
- No Ramp sync call for the statement itself — statements have no sync_status; individual transactions are marked via `TRANSACTION_SYNC` when auto-exported

### Card transactions (`ramp_client.py` → `sage_formatter.py`)

- Filters client-side: `sync_status == "SYNC_READY"` (no server-side filter available)
- Key field locations (confirmed from live data):
  - Vendor ID: `accounting_field_selections[type="MERCHANT"].external_id`
  - GL Account: `line_items[].accounting_field_selections[type="GL_ACCOUNT"].external_code` (use `external_code`, not `external_id`)
  - Department: `card_holder.department_name`
  - Amount: `tx["amount"]` (USD total) distributed proportionally across line items by local-currency ratio — avoids HNL/foreign amounts for international transactions
  - Date: `accounting_date` → `user_transaction_time`
- Invoice numbers auto-generated as `{vendor[:9]}.{MMDDYY}.{id[-3:]}` — max 20 chars (Sage 50 field limit); stable across export runs
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
- **Multi-bill payments:** when Ramp groups several bills into one ACH payment (same `payment.id` — e.g. one utility payment covering 4 meters), `_group_payments()` combines them into a single multi-distribution entry, same pattern as card statement payments: `amount` = each bill's own total (one per row), `total_amount`/`Number of Distributions` = group sum/count (repeated on every row in the group). `_expand_payment()` must use the bill's own `amount`, never `payment.amount` — that field is the *group* total and duplicating it per row was the original bug.
- Both CSVs emailed together as attachments; import Purchases first, then Payments
- **Duplicate invoice numbers:** some vendors reuse the same `invoice_number` across unrelated bills, which Sage 50 rejects on import. `BILLPAY_DEDUPE_VENDORS` (comma-separated vendor IDs) opts specific vendors into `_effective_invoice_number()` — appends a suffix from the last 4 chars of the Ramp bill ID (`{invoice[:15]}-{id[-4:]}`, truncated to 20 chars). Deterministic per bill ID (stable across re-runs); computed once and shared between the purchase row (`invoice`) and payment row (`invoice_number`) for the same bill, since Sage matches payments to invoices by that exact string. Vendors not listed keep their raw Ramp invoice number unchanged.
- State file: `exported_bill_ids.json`

### Shared modules

**`sage_formatter.py`** — 49-column Sage 50 vendor-invoice CSV. `_FIXED` dict holds Highlands Fellowship constants (ship-to address, AP account `2104-AB`, etc.). No grouping logic — distributions pre-computed by client modules.

**`emailer.py`** — `send_csv(gmail_user, gmail_app_password, to_address, subject, body_plain, csv_data, filename, body_html=None, extra_attachments=None)`. Sends `multipart/mixed` with `multipart/alternative` inner part (plain + HTML) plus one or more CSV attachments. `extra_attachments` is a list of `(csv_data, filename)` tuples.

**`email_template.py`** — Highlands Fellowship branded HTML. Three builders: `build_card_email`, `build_reimbursement_email`, `build_billpay_email`. All return `(html, plain_text)`. Import path shown in cream/teal box; skipped transactions in yellow warning box.
