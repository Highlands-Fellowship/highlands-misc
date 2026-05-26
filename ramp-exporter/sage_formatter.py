"""
Build a Sage 50 vendor-invoice CSV from rows produced by ramp_client.

Each row is one line-item (one Sage distribution).  num_distributions and
dist_number are pre-computed by ramp_client from the transaction's line_items
array, so no grouping logic is needed here.
"""

import csv
import io

SAGE_HEADERS = [
    "Vendor ID",
    "Invoice/CM #",
    "Apply to Invoice Number",
    "Credit Memo",
    "Date",
    "Drop Ship",
    "Customer SO #",
    "Waiting on Bill",
    "Customer ID",
    "Customer Invoice #",
    "Ship to Name",
    "Ship to Address-Line One",
    "Ship to Address-Line Two",
    "Ship to City",
    "Ship to State",
    "Ship to Zipcode",
    "Ship to Country",
    "Date Due",
    "Discount Date",
    "Discount Amount",
    "Accounts Payable Account",
    "Ship Via",
    "P.O. Note",
    "Note Prints After Line Items",
    "Beginning Balance Transaction",
    "Applied To Purchase Order",
    "Number of Distributions",
    "Invoice/CM Distribution",
    "Apply to Invoice Distribution",
    "PO Number",
    "PO Distribution",
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
    "Displayed Terms",
    "Return Authorization",
    "Recur Number",
    "Recur Frequency",
    "Accounting Department",
]

_FIXED = {
    "Credit Memo": "FALSE",
    "Drop Ship": "FALSE",
    "Waiting on Bill": "FALSE",
    "Ship to Name": "HIGHLANDS FELLOWSHIP",
    "Ship to Address-Line One": "P.O. Box 553",
    "Ship to City": "Abingdon",
    "Ship to State": "VA",
    "Ship to Zipcode": "24212",
    "Discount Amount": "0",
    "Accounts Payable Account": "2104-AB",
    "Note Prints After Line Items": "FALSE",
    "Beginning Balance Transaction": "FALSE",
    "Applied To Purchase Order": "FALSE",
    "Apply to Invoice Distribution": "0",
    "PO Distribution": "0",
    "Quantity": "0",
    "Unit Price": "0",
    "Weight": "0",
    "Used for Reimbursable Expense": "FALSE",
    "Displayed Terms": "Net Due",
    "Recur Number": "0",
}


def build_csv(rows: list[dict]) -> str:
    """Return the Sage 50 vendor-invoice CSV as a string."""
    if not rows:
        raise ValueError("No rows to format")

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(SAGE_HEADERS)

    for row in rows:
        record = dict(_FIXED)
        record["Vendor ID"] = row["vendor_id"]
        record["Invoice/CM #"] = row["invoice"]
        record["Date"] = row["date"]
        record["Date Due"] = row["date"]
        record["Discount Date"] = row["date"]
        record["Number of Distributions"] = str(row["num_distributions"])
        record["Invoice/CM Distribution"] = str(row["dist_number"])
        record["Description"] = row["memo"]
        record["G/L Account"] = row["gl_account"]
        record["Amount"] = row["amount"]
        record["Accounting Department"] = row["department"]

        writer.writerow([record.get(h, "") for h in SAGE_HEADERS])

    return buf.getvalue()
