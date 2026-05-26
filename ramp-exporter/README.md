# Ramp → Sage 50 Exporter

Pulls card transactions from the Ramp API and produces a Sage 50 vendor-invoice CSV, then emails it as an attachment. Runs on a schedule via Windows Task Scheduler.

This replaces a manual workflow: export CSV from Ramp's UI → run an Excel macro (`FillDistributions.bas`) to fill missing fields → import into Sage 50.

---

## How it works

1. Authenticates with the Ramp API using OAuth2 client credentials.
2. Fetches all transactions with `sync_status = SYNC_READY` (meaning all accounting fields have been filled in Ramp and the transaction is approved for export).
3. Skips any transaction missing a **Vendor ID** or **G/L Account** and logs a warning so those can be fixed in Ramp.
4. Expands each transaction's **line items** into individual Sage distribution rows. A transaction split across two expense categories becomes two rows with `Number of Distributions = 2`.
5. Builds the Sage 50 vendor-invoice CSV with all required columns and fixed values for Highlands Fellowship.
6. Emails the CSV to the configured recipient via Gmail.
7. Records the exported transaction IDs so they are not exported again on the next run.

---

## Field mapping

| Sage 50 column | Source |
|---|---|
| Vendor ID | `accounting_field_selections[type=MERCHANT].external_id` |
| Date / Date Due / Discount Date | `accounting_date` (falls back to `user_transaction_time`) |
| Description | `CardHolder FirstName LastName - memo` (e.g. `Ledonna Stuart - Lunch meeting`) |
| G/L Account | `line_items[].accounting_field_selections[type=GL_ACCOUNT].external_code` |
| Amount | `line_items[].amount.amount ÷ minor_unit_conversion_rate` |
| Accounting Department | `card_holder.department_name` |
| Invoice/CM # | Auto-generated as `VendorName.MM.DD.YYYY` (suffix `-2`, `-3` if same vendor has multiple transactions on the same date) |
| Number of Distributions | Count of line items on the transaction |
| Invoice/CM Distribution | 1-based index of the line item within the transaction |

Fixed values (Highlands Fellowship specific): Ship-to address, AP account `2104-AB`, terms `Net Due`, etc.

---

## Setup

### 1. Install Python dependencies

```
pip install -r requirements.txt
```

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in:

```
RAMP_CLIENT_ID=...         # From Ramp developer portal
RAMP_CLIENT_SECRET=...     # From Ramp developer portal
GMAIL_USER=...             # Gmail address to send from
GMAIL_APP_PASSWORD=...     # Gmail App Password (not your account password)
NOTIFY_EMAIL=...           # Who receives the CSV
```

**Ramp API setup:** Go to Ramp Settings → Developers → Create an API app with the `transactions:read` scope. Copy the client ID and secret into `.env`.

**Gmail App Password:** Google Account → Security → 2-Step Verification → App passwords. Generate one for this script.

### 3. Verify the API connection

```
python main.py --dump-raw
```

This prints the raw JSON for the first SYNC_READY transaction. Confirm that `accounting_date` is populated and the `accounting_field_selections` fields look correct. Use `--merchant` to find a specific transaction:

```
python main.py --dump-raw --merchant "Walmart"
```

### 4. Test the export

```
# Generate a 1-transaction CSV without sending email
python main.py --dry-run --limit 1
```

The CSV is saved to `output\sage_import_YYYYMMDD.csv`. Import it into a Sage 50 test company via **File → Select Import/Export → Vendor Invoices → Import** to confirm no errors.

### 5. Schedule on Windows

Run once from an elevated PowerShell prompt to register the daily Task Scheduler job:

```
.\setup_task.ps1
```

Edit `setup_task.ps1` to set `$SCRIPT_DIR`, `$PYTHON_EXE`, and `$RUN_HOUR` before running.

---

## Running manually

```powershell
# Full run — exports SYNC_READY transactions, emails CSV, updates state
python main.py

# Dry run — builds CSV and saves to output\ but skips email and state update
python main.py --dry-run

# Pull from a specific date regardless of prior export state
python main.py --dry-run --date-from 2026-05-01

# Cap the export at N transactions (useful for test imports into Sage)
python main.py --dry-run --limit 1

# Inspect raw API data for a specific merchant
python main.py --dump-raw --merchant "Amazon"
```

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Entry point — orchestrates fetch → format → email → state |
| `ramp_client.py` | Ramp API auth, transaction fetch, field extraction, validation |
| `sage_formatter.py` | Builds the Sage 50 CSV from normalised row data |
| `emailer.py` | Sends the CSV as a Gmail attachment |
| `setup_task.ps1` | Registers the Windows Task Scheduler job |
| `.env.example` | Secrets template — copy to `.env` |
| `exported_ids.json` | State file tracking already-exported transaction IDs (auto-created) |
| `output\` | Generated CSVs (auto-created) |
| `logs\` | Daily log files (auto-created) |

---

## Transactions skipped at export time

Any SYNC_READY transaction missing required fields is skipped and logged as a warning — it will not appear in the CSV. Common causes and fixes in Ramp:

| Warning | Fix in Ramp |
|---|---|
| `missing Vendor ID` | Open the transaction → set the **Accounting Vendor** field |
| `line item N missing G/L Account` | Open the transaction → set the **Category/GL Account** for that split |

Skipped transactions will be picked up automatically on the next run once fixed.

---

## Deferred features

- **Mark as synced in Ramp** — once the CSV output is validated in Sage, add `PATCH /transactions/{id}/sync` calls to mark transactions as exported in Ramp
- **Reimbursements** — separate script using the same structure, targeting Sage 50 General Journal
- **Bill Pay** — same pattern as card transactions
