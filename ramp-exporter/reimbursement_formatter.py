"""
Builds a Sage 50 General Journal CSV from pre-expanded reimbursement rows.

Column order matches the working import template exactly:
  Date, Reference, Date Clear in Bank Rec, Number of Distributions,
  G/L Account, Description, Amount, Job ID, Used for Reimbursable Expenses,
  Transaction Period, Transaction Number, Consolidated Transaction,
  Recur Number, Recur Frequency

Each reimbursement produces 4 rows (or n+3 for n expense line items):
  Expense entry (accounting_date):
    - Debit  expense G/L account(s)    positive amount
    - Credit ACH clearing account      negative amount
  Payment entry (payment_processed_at):
    - Debit  ACH clearing account      positive amount
    - Credit bank/cash account         negative amount

Account codes are read from env vars:
  REIMBURSEMENT_CLEARING_ACCOUNT  (default 2200)
  REIMBURSEMENT_BANK_ACCOUNT      (default 1003-AB)
"""

import csv
import io
import os

_DEFAULT_CLEARING_ACCOUNT = "2200"
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

    row_role is one of: expense_debit, expense_credit, payment_debit, payment_credit.
    gl_account=None rows have their account filled from env vars.
    """
    clearing = os.getenv("REIMBURSEMENT_CLEARING_ACCOUNT", _DEFAULT_CLEARING_ACCOUNT)
    bank = os.getenv("REIMBURSEMENT_BANK_ACCOUNT", _DEFAULT_BANK_ACCOUNT)

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(_COLUMNS)

    for row in rows:
        role = row["row_role"]

        if row["gl_account"] is not None:
            gl = row["gl_account"]
        elif role in ("expense_credit", "payment_debit"):
            gl = clearing
        else:
            gl = bank

        amount_str = f"{float(row['amount']):.2f}"
        reference = (
            "Ramp Reimbursement PYMT"
            if role in ("payment_debit", "payment_credit")
            else "Ramp Reimbursement EXP"
        )

        writer.writerow([
            row["date"],
            reference,
            "",                          # Date Clear in Bank Rec
            row["num_distributions"],
            gl,
            row["description"],
            amount_str,
            "",                          # Job ID
            "False",                     # Used for Reimbursable Expenses
            "",                          # Transaction Period
            "",                          # Transaction Number
            "False",                     # Consolidated Transaction
            "0",                         # Recur Number
            "0",                         # Recur Frequency
        ])

    return buf.getvalue()
