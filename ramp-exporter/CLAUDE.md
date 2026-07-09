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

# Retry sync for bills deferred by a prior run (e.g. checks that have since
# cleared) — combine with the normal run, e.g. for the daily scheduled task:
python billpay.py --mark-synced --reconcile

# Audit: sweep Ramp directly for any bill that should be synced but isn't,
# regardless of local state — for cleanup after a failure predating
# pending_sync_ids.json, or just periodic peace of mind
python billpay.py --audit

# Re-export: rebuild CSVs for specific bill IDs regardless of sync status
python billpay.py --reexport-ids ID1 ID2
```

## Required `.env` file

Copy `.env.example` to `.env`. Required keys: `RAMP_CLIENT_ID`, `RAMP_CLIENT_SECRET`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`. Optional: `OUTPUT_DIR`, `REIMBURSEMENT_BANK_ACCOUNT` (default `1003-AB`), `BILLPAY_CASH_ACCOUNT` (default `1000-AB`), `BILLPAY_AP_ACCOUNT` (default `2200`), `CARD_PAYMENT_CASH_ACCOUNT` (default `1003-AB`), `CARD_PAYMENT_AP_ACCOUNT` (default `2104-AB`).

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
  - GL Account: `line_items[].accounting_field_selections[type="GL_ACCOUNT"].external_code` — `type` appears at the top level of the selection dict (confirmed via `--dump-raw`); it's also duplicated under `category_info.type`, but the code only needs the top-level one
  - Payment date: `payment_processed_at` → `accounting_date` → `transaction_date` → `created_at`
- **Single journal entry per reimbursement** (n+1 rows for n line items), dated `payment_processed_at`:
  - `debit` — one row per line item, expense GL account, positive amount
  - `credit` — one row, bank account (`REIMBURSEMENT_BANK_ACCOUNT`), negative amount (`gl_account=None`, filled by the formatter)
  - No clearing account, no separate expense-date entry — a prior two-entry/clearing-account design (accrual-style, expense dated separately from payment) was replaced with this simpler single-entry model per CFO decision, since expense and payment dates were typically only 1-2 days apart in practice
- 14-column General Journal CSV: single `Amount` column (positive=debit, negative=credit); `Reference` fixed to `"Ramp Reimbursement"` (18 chars, fits Sage's 20-char limit)
- State file: `exported_reimb_ids.json`

### Bill pay (`billpay_client.py` → `sage_formatter.py` + `billpay_payment_formatter.py`)

- Filters: `sync_status == "NOT_SYNCED"` AND `_is_exportable_status(bill)` (bills have no `SYNC_READY` status) — true when `status_summary == "PAYMENT_COMPLETED"`, or `status_summary == "PAYMENT_PROCESSING"` and `payment.payment_method == "CHECK"`. Checks debit the bank when cut/mailed, well before Ramp flips the bill to `PAYMENT_COMPLETED` (which happens on clearing); ACH/wire in `PAYMENT_PROCESSING` is excluded since funds aren't committed yet.
- Key field locations (confirmed from live data):
  - Vendor ID: `vendor.remote_id` → `vendor.remote_code` → `vendor.name`
  - Invoice number: `invoice_number` (always present — no generation needed)
  - GL Account: `line_items[].accounting_field_selections` — check BOTH `sel.get("type")` and `sel.get("category_info", {}).get("type")` for `"GL_ACCOUNT"` (bills may store it under either)
  - Department: top-level `accounting_field_selections[type="DEPARTMENT"].external_id`
  - Date: `accounting_date` → `paid_at` → `issued_at`
  - Payment check number: `payment.customer_friendly_payment_id`
  - Payment date: `payment.payment_date` → `payment.effective_date` → `paid_at`
- `fetch_completed_bills` returns a 3-tuple `(purchase_rows, payment_rows, skipped)`; both it and `fetch_bills_by_ids` (used by `--reexport-ids`) share the validate/expand loop via `_expand_bills()`
- `fetch_bills_by_ids` fetches specific bills directly by ID (`GET /bills/{id}`), bypassing the `NOT_SYNCED` filter and `exported_bill_ids.json` — use it to rebuild CSVs for a bill that's already synced, since a synced bill no longer shows up in a normal fetch
- Purchase rows reuse `sage_formatter.build_csv()` (same 49-column format as card transactions)
- Payment rows go to `billpay_payment_formatter.build_csv()` — 39-column Payments Journal CSV
- **Multi-bill payments:** when Ramp groups several bills into one ACH payment (same `payment.id` — e.g. one utility payment covering 4 meters), `_group_payments()` combines them into a single multi-distribution entry, same pattern as card statement payments: `amount` = each bill's own total (one per row), `total_amount`/`Number of Distributions` = group sum/count (repeated on every row in the group). `_expand_payment()` must use the bill's own `amount`, never `payment.amount` — that field is the *group* total and duplicating it per row was the original bug.
- Both CSVs emailed together as attachments; import Purchases first, then Payments
- **Duplicate invoice numbers:** many bills have no real invoice number — Ramp falls back to the account number, which recurs every billing period and collides in Sage as a duplicate reference. `_effective_invoice_number()` appends a suffix from the last 4 chars of the Ramp bill ID to *every* bill's invoice number (`{invoice[:15]}-{id[-4:]}`, truncated to Sage's 20-char field limit) — this used to be opt-in per vendor via `BILLPAY_DEDUPE_VENDORS`, but the problem kept resurfacing on new vendors, so it's now unconditional. Deterministic per bill ID (stable across re-runs); computed once and shared between the purchase row (`invoice`) and payment row (`invoice_number`) for the same bill, since Sage matches payments to invoices by that exact string.
- **`mark_synced` partial-batch handling:** since a still-`PAYMENT_PROCESSING` check bill can be exported to Sage (see above), Ramp's `BILL_PAYMENT_SYNC` may reject the whole batch with `DEVELOPER_7062` ("not ready for sync") if any bill in it isn't fully complete yet. `mark_synced()` parses the offending IDs out of the error message (`error_v2.message`, extracted via UUID regex) and retries the batch without them, returning the set of bill IDs that ended up genuinely deferred. Deferred bills stay exported to Sage but never resurface through the normal fetch — `exported_bill_ids.json`'s skip-list excludes them regardless of Ramp's `sync_status`, so nothing retries them automatically.
  - **`BILL_SYNC` vs `BILL_PAYMENT_SYNC` "not ready" mean different things.** For a bill already at `sync_status = BILL_SYNCED`, re-attempting `BILL_SYNC` returns the *same* `DEVELOPER_7062` "not ready for sync" message — but here it means "already done," not "genuinely not eligible." `BILL_SYNC` eligibility doesn't regress once granted, so any bill flagged "not ready" during that phase is treated as already-synced and skipped (never added to `deferred`), letting it proceed to `BILL_PAYMENT_SYNC` — the phase where "not ready" is a real, payment-completion-dependent blocker. Getting this backwards (treating every "not ready" as a hard defer) means a bill stuck at `BILL_SYNCED` never even reaches the `BILL_PAYMENT_SYNC` attempt, which is what happened before this distinction existed.
- **`pending_sync_ids.json` + `--reconcile`:** `billpay.py` persists every `mark_synced()` call's deferred set here via `_track_sync_result()` (bills that synced fully get cleared, newly deferred ones get added). `--reconcile` retries sync for everything currently pending and updates the tracker with the outcome — no re-export, no email involved for the pending bills themselves. It's additive, not exclusive: it runs alongside the normal fetch (after "nothing to do", or after the main `--mark-synced` block), so `setup_task.ps1`'s daily Bill task runs `billpay.py --mark-synced --reconcile` to catch up any check payments that have since cleared in the same run.
- **`--audit`:** `billpay_client.find_unsynced_bills()` sweeps bills directly from Ramp (ignoring `exported_bill_ids.json` and the `NOT_SYNCED`-only filter) for anything matching `_is_exportable_status(bill)` where `sync_status != "BILL_AND_PAYMENT_SYNCED"` — catches bills stuck in a partial sync state from before `pending_sync_ids.json` existed to track them (e.g. a batch whose `BILL_PAYMENT_SYNC` failed atomically under the old `mark_synced`, leaving every bill in the batch un-payment-synced even though only some were genuinely not ready). Found bills are merged into `pending_sync_ids.json` and retried immediately via `_reconcile()` unless `--dry-run`. Optionally scoped with `--date-from`.
- State file: `exported_bill_ids.json`

### Shared modules

**`sage_formatter.py`** — 49-column Sage 50 vendor-invoice CSV. `_FIXED` dict holds Highlands Fellowship constants (ship-to address, AP account `2104-AB`, etc.). No grouping logic — distributions pre-computed by client modules.

**`emailer.py`** — `send_csv(gmail_user, gmail_app_password, to_address, subject, body_plain, csv_data, filename, body_html=None, extra_attachments=None)`. Sends `multipart/mixed` with `multipart/alternative` inner part (plain + HTML) plus one or more CSV attachments. `extra_attachments` is a list of `(csv_data, filename)` tuples.

**`email_template.py`** — Highlands Fellowship branded HTML. Three builders: `build_card_email`, `build_reimbursement_email`, `build_billpay_email`. All return `(html, plain_text)`. Import path shown in cream/teal box; skipped transactions in yellow warning box.
