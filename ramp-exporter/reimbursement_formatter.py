"""
Builds a Sage 50 General Journal CSV from pre-expanded reimbursement rows.

Column order matches the working import template exactly:
  Date, Reference, Date Clear in Bank Rec, Number of Distributions,
  G/L Account, Description, Amount, Job ID, Used for Reimbursable Expenses,
  Transaction Period, Transaction Number, Consolidated Transaction,
  Recur Number, Recur Frequency

Each reimbursement produces one journal entry (n+1 rows for n line items),
dated payment_processed_at:
  - Debit  expense G/L account(s)    positive amount
  - Credit bank/cash account         negative amount

Bank account code is read from an env var:
  REIMBURSEMENT_BANK_ACCOUNT  (default 1003-AB)
"""

import csv
import io
import os

_DEFAULT_BANK_ACCOUNT = "1003-AB"

_COLUMNS = [
    "Date",
    "Reference",
    "Date Clear in Bank Rec",
    "Number of Distributions",
    "G/L Account",
    "Description",
    "Amount",
    "Job ID",
    "Used for Reimbursable Expenses",
    "Transaction Period",
    "Transaction Number",
    "Consolidated Transaction",
    "Recur Number",
    "Recur Frequency",
]


def build_csv(rows: list[dict]) -> str:
    """
    Build a General Journal CSV string from pre-expanded reimbursement rows.

    Each row dict must have:
      id, date, description, gl_account (or None), amount, num_distributions, row_role.

    row_role is one of: debit, credit. gl_account=None on the credit row —
    filled from the env-configured bank account.
    """
    bank = os.getenv("REIMBURSEMENT_BANK_ACCOUNT", _DEFAULT_BANK_ACCOUNT)

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(_COLUMNS)

    for row in rows:
        gl = row["gl_account"] if row["gl_account"] is not None else bank
        amount_str = f"{float(row['amount']):.2f}"

        writer.writerow([
            row["date"],
            "Ramp Reimbursement",        # Reference (Sage 20-char field limit; 19 chars)
            "",                           # Date Clear in Bank Rec
            row["num_distributions"],
            gl,
            row["description"],
            amount_str,
            "",                           # Job ID
            "False",                      # Used for Reimbursable Expenses
            "",                           # Transaction Period
            "",                           # Transaction Number
            "False",                      # Consolidated Transaction
            "0",                          # Recur Number
            "0",                          # Recur Frequency
        ])

    return buf.getvalue()
