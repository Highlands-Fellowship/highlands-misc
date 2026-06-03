"""
Builds a Sage 50 Payments Journal CSV from card-statement payment rows.

Column order matches the working PAYMENTS.CSV export exactly (39 columns) —
same structure as billpay_payment_formatter.py, but supports multiple
distributions per vendor payment (one row per invoice).

One logical payment per vendor per statement:
  - Number of Distributions  = total invoices for that vendor in the statement
  - Total Paid on Invoice(s) = sum of all those invoices (same on every row)
  - Invoice Paid             = individual invoice number (one per row)
  - Amount                   = individual invoice amount (one per row)

Configurable via env vars:
  CARD_PAYMENT_CASH_ACCOUNT   — bank account debited on payment (default 1003-AB)
  CARD_PAYMENT_AP_ACCOUNT     — AP account credited/cleared   (default 2104-AB)
"""

import csv
import io
import os

_DEFAULT_CASH_ACCOUNT = "1003-AB"
_DEFAULT_AP_ACCOUNT = "2104-AB"

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
    Build a Payments Journal CSV from card-statement payment row dicts.

    Each dict must have:
      vendor_id, vendor_name, check_number, payment_date, memo,
      total_amount, invoice_number, amount, num_distributions, payment_method
    """
    cash_account = os.getenv("CARD_PAYMENT_CASH_ACCOUNT", _DEFAULT_CASH_ACCOUNT)
    ap_account = os.getenv("CARD_PAYMENT_AP_ACCOUNT", _DEFAULT_AP_ACCOUNT)

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(_COLUMNS)

    for row in payment_rows:
        total_str = f"{float(row['total_amount']):.2f}"
        amount_str = f"{float(row['amount']):.2f}"

        writer.writerow([
            row["vendor_id"],
            row["vendor_name"],
            row["vendor_name"],           # Check Name = Vendor Name
            "",                            # Check Address-Line One
            "",                            # Check Address-Line Two
            "",                            # Check City
            "",                            # Check State
            "",                            # Check Zipcode
            "",                            # Check Country
            row["check_number"],
            row["payment_date"],
            row["memo"],
            cash_account,
            total_str,                    # Total Paid on Invoice(s)
            "",                            # Discount Account
            "FALSE",                       # Prepayment
            "FALSE",                       # Customer Payment
            "",                            # AP Date Cleared in Bank Rec
            "Yes",                         # Detailed Payments
            str(row["num_distributions"]),
            row["invoice_number"],
            "0.00",                        # Discount Amount
            "0.00",                        # Quantity
            "",                            # Item ID
            "",                            # Serial Number
            "",                            # Description
            ap_account,                    # G/L Account (AP account to clear)
            "0.00",                        # Unit Price
            "",                            # UPC / SKU
            "0.00",                        # Weight
            amount_str,                   # Amount (individual invoice)
            "",                            # Job ID
            "FALSE",                       # Used for Reimbursable Expense
            "",                            # Transaction Period
            "",                            # Transaction Number
            "",                            # Voided by Transaction
            "0",                           # Recur Number
            "0",                           # Recur Frequency
            row["payment_method"],
        ])

    return buf.getvalue()
