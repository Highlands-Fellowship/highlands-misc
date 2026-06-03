# Ramp → Sage 50 Import Process

## Overview

Accounting exports from Ramp are handled automatically. Once a week on Wednesday and on the second day of each month, a script runs and emails import-ready files for any new transactions. All you need to do is import the attached file(s) into Sage 50.

---

## What You'll Receive

You will receive up to **four separate emails**, each with a CSV file attached:

### 1. Card Transactions
- **Subject:** Card Transactions Ready for Import
- **Import path:** File › Select Import/Export › Accounts Payable › Purchases Journal › Import

### 2. Card Payments
- **Subject:** Card Payments Ready for Import
- **Import path:** File › Select Import/Export › Accounts Payable › Payments Journal › Import
- Clears the open balance on card transaction invoices imported in step 1.

### 3. Reimbursements
- **Subject:** Reimbursements Ready for Import
- **Import path:** File › Select Import/Export › General Ledger › General Journal › Import

### 4. Bill Payments
- **Subject:** Bill Payments Ready for Import
- **Two files attached** — import in this order:
  1. `sage_bill_purchases_*.csv` → Accounts Payable › Purchases Journal › Import
  2. `sage_bill_payments_*.csv` → Accounts Payable › Payments Journal › Import

> You will only receive an email if there is something new to import for that category. If nothing has changed since the last run, no email is sent.

![SCR_20260527_lmfc.png](/GetFile.ashx?Id=3274226)

---

## Import Steps

1. Open the email and note the import path listed.
2. Save the attached CSV file(s) to your computer.
3. In Sage 50, follow the import path shown in the email.
4. Click the **Options** tab and check **"First Row Contains Headings"**.
5. Select the CSV file you downloaded.
6. Complete the import.
7. Repeat for each attached file (bill payments require two imports).

> **Important:** Do not open the CSV file in Excel before importing. Excel will reformat the data and cause import errors in Sage 50.

![Sage_50___Purchase_Import_Screen.png](/GetFile.ashx?Id=3274224)

---

## Skipped Transactions

If any transactions are missing required accounting fields (such as a Vendor ID or G/L Account), they will **not** be included in the import file. The email will show a warning section listing each skipped item with a direct link to fix it in Ramp.

Once the missing fields are filled in, the transaction will be picked up automatically on the next run — no manual re-export needed.

---

## Questions or Issues

If an email doesn't arrive when expected, or if Sage 50 reports an error during import, contact your system administrator.
