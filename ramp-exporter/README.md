# Ramp -> Sage 50 Exporter

Pulls card transactions and reimbursements from the Ramp API, produces Sage 50-compatible CSVs, and emails them as branded HTML attachments. Runs on a schedule via Windows Task Scheduler.

This replaces a manual workflow: export CSV from Ramp's UI -> run an Excel macro (`FillDistributions.bas`) to fill missing fields -> import into Sage 50.

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
| Amount | `line_items[].amount.amount / minor_unit_conversion_rate` |
| Accounting Department | `card_holder.department_name` |
| Invoice/CM # | Auto-generated as `VendorName.MM.DD.YYYY` (suffix `-2`, `-3` if same vendor has multiple transactions on the same date) |
| Number of Distributions | Count of line items on the transaction |
| Invoice/CM Distribution | 1-based index of the line item within the transaction |

Fixed values (Highlands Fellowship specific): Ship-to address, AP account `2104-AB`, terms `Net Due`, etc.

### Running

```powershell
# Full run
python main.py

# Dry run — builds CSV, skips email and state update
python main.py --dry-run

# Pull from a specific date (ignores exported_ids.json)
python main.py --dry-run --date-from 2026-05-01

# Cap at N transactions for test imports
python main.py --dry-run --limit 1

# Inspect raw API data for a specific merchant
python main.py --dump-raw --merchant "Amazon"
```

Import into Sage 50 via: **File -> Select Import/Export -> Accounts Payable -> Purchases Journal -> Import**

---

## Reimbursements

### How it works

1. Fetches all reimbursements with `sync_status = SYNC_READY`.
2. Skips any reimbursement missing a **G/L Account** and logs a warning.
3. Builds a Sage 50 **General Journal** CSV with two rows per reimbursement:
   - **Debit**: expense G/L account for the reimbursement amount
   - **Credit**: ACH clearing account (configurable via `REIMBURSEMENT_CLEARING_ACCOUNT`, default `2200-00`)
4. Emails the CSV via branded HTML email.
5. Records exported IDs in `exported_reimb_ids.json`.

### Field mapping

| Sage 50 GJ column | Source |
|---|---|
| Date | `created_at` (falls back to `transaction_date`) |
| Reference | `Ramp Reimbursement` (fixed) |
| Description | `Employee FirstName LastName - memo` |
| G/L Account (debit row) | `accounting_field_selections[type=GL_ACCOUNT].external_code` |
| G/L Account (credit row) | `REIMBURSEMENT_CLEARING_ACCOUNT` env var (default `2200-00`) |
| Debit / Credit | Amount in dollars |

### Running

```powershell
# Full run
python reimburse.py

# Dry run
python reimburse.py --dry-run

# Pull from a specific date
python reimburse.py --dry-run --date-from 2026-05-01

# Cap at N reimbursements for test imports
python reimburse.py --dry-run --limit 1

# Inspect raw API data for a specific employee
python reimburse.py --dump-raw --employee "Stuart"
```

Import into Sage 50 via: **File -> Select Import/Export -> General Journal Transactions -> Import**

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
NOTIFY_EMAIL=...                       # Who receives the CSVs
REIMBURSEMENT_CLEARING_ACCOUNT=...     # Optional, default 2200-00
```

**Ramp API setup:** Go to Ramp Settings -> Developers -> Create an API app with the `transactions:read` and `reimbursements:read` scopes. Copy the client ID and secret into `.env`.

**Gmail App Password:** Google Account -> Security -> 2-Step Verification -> App passwords. For a Google Workspace shared mailbox, use the admin console to enable 2SV and generate an App Password for that account.

### 3. Verify API connections

```
python main.py --dump-raw
python reimburse.py --dump-raw
```

### 4. Schedule on Windows

Run once from an elevated PowerShell prompt to register the daily Task Scheduler job:

```
.\setup_task.ps1
```

Edit `setup_task.ps1` to set `$SCRIPT_DIR`, `$PYTHON_EXE`, and `$RUN_HOUR` before running.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Card transactions entry point |
| `reimburse.py` | Reimbursements entry point |
| `ramp_client.py` | Ramp API auth, card transaction fetch, validation |
| `reimbursement_client.py` | Ramp API fetch for reimbursements, journal row expansion |
| `sage_formatter.py` | Builds the Sage 50 vendor-invoice CSV |
| `reimbursement_formatter.py` | Builds the Sage 50 General Journal CSV |
| `emailer.py` | Sends CSV as branded HTML email via Gmail |
| `email_template.py` | Highlands Fellowship branded HTML email templates |
| `setup_task.ps1` | Registers the Windows Task Scheduler job |
| `.env.example` | Secrets template — copy to `.env` |
| `exported_ids.json` | State file for card transaction IDs (auto-created) |
| `exported_reimb_ids.json` | State file for reimbursement IDs (auto-created) |
| `output\` | Generated CSVs (auto-created) |
| `logs\` | Daily log files (auto-created) |

---

## Transactions skipped at export time

Any SYNC_READY transaction missing required fields is skipped and logged — it will not appear in the CSV. It is also listed in the notification email with the reason.

| Warning | Fix in Ramp |
|---|---|
| `missing Vendor ID` | Open the transaction -> set the **Accounting Vendor** field |
| `line item N missing G/L Account` | Open the transaction -> set the **Category/GL Account** for that split |
| `missing G/L Account` (reimbursement) | Open the reimbursement -> set the **Category/GL Account** |

Skipped items will be picked up automatically on the next run once fixed.

---

## Deferred features

- **Mark as synced in Ramp** — `--mark-synced` flag is implemented for card transactions; add same pattern to `reimburse.py` once reimbursement CSV output is validated in Sage
- **Bill Pay** — same pattern as card transactions
