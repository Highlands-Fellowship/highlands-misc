"""
Builds a Sage 50 Payments Journal CSV from bill payment rows.

Column order matches the working PAYMENTS.CSV export exactly (39 columns).
One row per bill — each Ramp bill has a single invoice number and one payment.

Configurable via env vars:
  BILLPAY_CASH_ACCOUNT   — bank account debited on payment (default 1000-AB)
  BILLPAY_AP_ACCOUNT     — AP clearing account credited (default 2200)
"""

import csv
import io
import os

_DEFAULT_CASH_ACCOUNT = "1000-AB"
_DEFAULT_AP_ACCOUNT = "2200"

_COLUMNS = [
    "Vendor ID",
    "Vendor Name",
    "Check Name",
    "Check Address-Line One",
    "Check Address-Line Two",
    "Check City",
    "Check State",
    "Check Zipcode",
    "Check Country",
    "Check Number",
    "Date",
    "Memo",
    "Cash Account",
    "Total Paid on Invoice(s)",
    "Discount Account",
    "Prepayment",
    "Customer Payment",
    "AP Date Cleared in Bank Rec",
    "Detailed Payments",
    "Number of Distributions",
    "Invoice Paid",
    "Discount Amount",
    "Quantity",
    "Item ID",
    "Serial Number",
    "Description",
    "G/L Account",
    "Unit Price",
    "UPC / SKU",
    "Weight",
    "Amount",
    "Job ID",
    "Used for Reimbursable Expense",
    "Transaction Period",
    "Transaction Number",
    "Voided by Transaction",
    "Recur Number",
    "Recur Frequency",
    "Payment Method",
]


def build_csv(payment_rows: list[dict]) -> str:
    """
    Build a Payments Journal CSV from bill payment row dicts.

    Each dict must have:
      vendor_id, vendor_name, check_number, payment_date, memo,
      total_amount, invoice_number, payment_method
    """
    cash_account = os.getenv("BILLPAY_CASH_ACCOUNT", _DEFAULT_CASH_ACCOUNT)
    ap_account = os.getenv("BILLPAY_AP_ACCOUNT", _DEFAULT_AP_ACCOUNT)

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(_COLUMNS)

    for row in payment_rows:
        amount_str = f"{float(row['total_amount']):.2f}"

        writer.writerow([
            row["vendor_id"],
            row["vendor_name"],
            row["vendor_name"],      # Check Name = Vendor Name
            "",                       # Check Address-Line One
            "",                       # Check Address-Line Two
            "",                       # Check City
            "",                       # Check State
            "",                       # Check Zipcode
            "",                       # Check Country
            row["check_number"],
            row["payment_date"],
            row["memo"],
            cash_account,
            amount_str,              # Total Paid on Invoice(s)
            "",                       # Discount Account
            "FALSE",                  # Prepayment
            "FALSE",                  # Customer Payment
            "",                       # AP Date Cleared in Bank Rec
            "Yes",                    # Detailed Payments
            "1",                      # Number of Distributions (1 invoice per Ramp bill)
            row["invoice_number"],
            "0.00",                   # Discount Amount
            "0.00",                   # Quantity
            "",                       # Item ID
            "",                       # Serial Number
            "",                       # Description
            ap_account,               # G/L Account (AP clearing)
            "0.00",                   # Unit Price
            "",                       # UPC / SKU
            "0.00",                   # Weight
            amount_str,              # Amount
            "",                       # Job ID
            "FALSE",                  # Used for Reimbursable Expense
            "",                       # Transaction Period
            "",                       # Transaction Number
            "",                       # Voided by Transaction
            "0",                      # Recur Number
            "0",                      # Recur Frequency
            row["payment_method"],
        ])

    return buf.getvalue()
