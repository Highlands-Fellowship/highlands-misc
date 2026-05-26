# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Pulls card transactions from the Ramp API that are marked `SYNC_READY`, formats them as a Sage 50 vendor-invoice CSV, and emails the file via Gmail. Intended to run daily on Windows via Task Scheduler, replacing a manual Ramp UI export + Excel macro workflow.

## Running the script

```
# Install dependencies (one-time)
pip install -r requirements.txt

# Inspect raw Ramp API output for a transaction — do this first to verify field names
python main.py --dump-raw

# Test the full pipeline without sending email or updating state
python main.py --dry-run

# Pull transactions from a specific date (ignores exported_ids.json state)
python main.py --dry-run --date-from 2026-01-01

# Production run (emails CSV, updates exported_ids.json)
python main.py
```

## Required `.env` file

Copy `.env.example` to `.env`. Required keys:
- `RAMP_CLIENT_ID` / `RAMP_CLIENT_SECRET` — OAuth2 client credentials from Ramp developer portal (Basic Auth on token endpoint, scope `transactions:read`)
- `GMAIL_USER` / `GMAIL_APP_PASSWORD` — Gmail app password (not account password)
- `NOTIFY_EMAIL` — recipient for the CSV attachment

Optional: `OUTPUT_DIR` (defaults to `./output/`)

## Architecture

**Data flow:** `main.py` → `ramp_client.py` → `sage_formatter.py` → `emailer.py`

**`ramp_client.py`** handles all Ramp API interaction:
- OAuth2 client credentials via Basic Auth (`auth=(id, secret)` on POST to `/developer/v1/token`)
- Paginates `GET /developer/v1/transactions` — Ramp's `page.next` is a full URL, so subsequent pages are fetched by URL directly (not as a query parameter)
- Filters client-side on `sync_status == "SYNC_READY"` (no server-side filter exists for this)
- `from_date` must be a full ISO datetime (`2026-01-01T00:00:00Z`), not just a date
- Each transaction is expanded into one dict per `line_item` — these become Sage distribution rows
- `num_distributions` = `len(line_items)`, `dist_number` = 1-based index within the transaction
- Invoice numbers generated as `VendorName.MM.DD.YYYY`; same vendor+date gets `-2`, `-3` suffix

**Key Ramp API field mapping** (confirmed from live data):
- Vendor ID → `accounting_field_selections[type="MERCHANT"].external_id`
- GL Account code → `line_items[].accounting_field_selections[type="GL_ACCOUNT"].external_code` (use `external_code`, not `external_id`)
- Department → `card_holder.department_name`
- Amount → `line_items[].amount.amount / minor_unit_conversion_rate` (stored in cents)
- Top-level `amount` field is already in dollars; line item amounts are in minor units

**`sage_formatter.py`** maps pre-computed row dicts to the 49-column Sage 50 vendor-invoice CSV. Fixed values specific to Highlands Fellowship (ship-to address, AP account `2104-AB`, etc.) are in the `_FIXED` dict. No grouping logic — distributions are already computed by `ramp_client`.

**`exported_ids.json`** — state file tracking Ramp transaction IDs already exported. Prevents duplicates on subsequent runs. Skipped when `--date-from` is passed.

## Reimbursements

**`reimbursement_client.py`** fetches from `GET /developer/v1/reimbursements`, same pagination/filter pattern as card transactions. Scope: `reimbursements:read`. `fetch_sync_ready_reimbursements` returns `(rows, skipped)`. Expands each reimbursement into two rows (debit/credit pair): `entry_type = "debit"` uses the expense G/L account, `entry_type = "credit"` uses `REIMBURSEMENT_CLEARING_ACCOUNT` env var (default `2200-00`). Date: `created_at` fallback to `transaction_date`.

**`reimbursement_formatter.py`** builds a 13-column Sage 50 General Journal CSV (Date, Reference, Description, G/L Account, Debit, Credit, plus fixed metadata columns). Debit rows get a positive Amount in the Debit column; credit rows get a positive Amount in the Credit column. Reference is fixed as "Ramp Reimbursement".

**`reimburse.py`** entry point — same flags as `main.py` except `--employee` instead of `--merchant` for `--dump-raw` filter. State file: `exported_reimb_ids.json`.

**`email_template.py`** — Highlands Fellowship branded HTML email. `build_card_email(count, gen_date, skipped)` and `build_reimbursement_email(count, gen_date, skipped)` both return `(html, plain_text)`. Skipped transactions appear in a yellow warning box; import path appears in a cream/teal box.

**`emailer.py`** — `send_csv` now accepts optional `body_html` param. When provided, sends `multipart/mixed` with a `multipart/alternative` inner part (plain + HTML) plus the CSV attachment.

## Deferred features

- Marking reimbursements as synced in Ramp — add same `--mark-synced` pattern from `main.py` once reimbursement CSV output is validated against Sage
- Bill Pay transaction type — same structure as card transactions
