"""
Ramp API client for bill pay.

Fetches bills with sync_status=NOT_SYNCED and status_summary=PAYMENT_COMPLETED,
then expands each bill's line_items into Sage 50 vendor-invoice distribution rows
(same structure as card transactions — reuses sage_formatter.build_csv directly).

Run  python billpay.py --dump-raw  to inspect raw JSON before going live.

Key field locations (verify with --dump-raw against a live bill):
  - Vendor ID:   bill.vendor.remote_id  (fallback: remote_code, name)
  - Invoice #:   bill.invoice_number    (present on bills — no generation needed)
  - Date:        bill.accounting_date   (fallback: paid_at, issued_at)
  - GL Account:  line_items[].accounting_field_selections[type=GL_ACCOUNT].external_code
  - Amount:      line_items[].amount.amount / minor_unit_conversion_rate
  - Department:  accounting_field_selections[type=DEPARTMENT].external_id
"""

import datetime
import logging
import requests

RAMP_TOKEN_URL = "https://api.ramp.com/developer/v1/token"
RAMP_BILLS_URL = "https://api.ramp.com/developer/v1/bills"
RAMP_SYNCS_URL = "https://api.ramp.com/developer/v1/accounting/syncs"


def _get_token(client_id: str, client_secret: str, write: bool = False) -> str:
    scope = "bills:read accounting:write" if write else "bills:read"
    resp = requests.post(
        RAMP_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": scope},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _get(token: str, params: dict, url: str = RAMP_BILLS_URL) -> dict:
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params if url == RAMP_BILLS_URL else None,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ramp API {resp.status_code} on GET /bills\n"
            f"url:  {resp.url}\n"
            f"body: {resp.text}"
        )
    return resp.json()


def _format_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return raw[:10]


def _vendor_id(bill: dict) -> str:
    vendor = bill.get("vendor") or {}
    return (
        (vendor.get("remote_id") or "").strip()
        or (vendor.get("remote_code") or "").strip()
        or (vendor.get("name") or "").strip()
    )


def _gl_account(item: dict) -> str:
    for sel in item.get("accounting_field_selections") or []:
        # Check top-level type and category_info.type (bills may use either)
        sel_type = sel.get("type") or (sel.get("category_info") or {}).get("type") or ""
        if sel_type == "GL_ACCOUNT":
            return (sel.get("external_code") or sel.get("external_id") or "").strip()
    return ""


def _department(bill: dict) -> str:
    for sel in bill.get("accounting_field_selections") or []:
        sel_type = sel.get("type") or (sel.get("category_info") or {}).get("type") or ""
        if sel_type == "DEPARTMENT":
            return (sel.get("external_id") or "").strip()
    return ""


def _line_item_amount(item: dict) -> float:
    amt = item.get("amount") or {}
    if isinstance(amt, dict):
        raw = amt.get("amount", 0)
        rate = amt.get("minor_unit_conversion_rate", 100)
        return raw / rate
    return float(amt)


def _validate(bill: dict) -> list[str]:
    errors = []
    if not _vendor_id(bill):
        errors.append("missing Vendor ID (set vendor remote_id in Ramp)")
    if not bill.get("invoice_number"):
        errors.append("missing invoice number")
    line_items = bill.get("line_items") or []
    if not line_items:
        errors.append("no line items")
    else:
        for i, item in enumerate(line_items, 1):
            if not _gl_account(item):
                errors.append(f"line item {i} missing G/L Account")
    return errors


def _payment_method(bill: dict) -> str:
    method = ((bill.get("payment") or {}).get("payment_method") or "").upper()
    # Sage 50 Payments Journal uses "Check" for all electronic and check payments
    return "Check"


def _expand_payment(bill: dict) -> dict:
    """Return one payment row dict for the Payments Journal."""
    payment = bill.get("payment") or {}
    vendor = bill.get("vendor") or {}

    raw_date = (
        payment.get("payment_date")
        or payment.get("effective_date")
        or bill.get("paid_at")
        or ""
    )
    payment_date = _format_date(raw_date)

    amt_obj = payment.get("amount") or bill.get("amount") or {}
    if isinstance(amt_obj, dict):
        total = amt_obj.get("amount", 0) / amt_obj.get("minor_unit_conversion_rate", 100)
    else:
        total = float(amt_obj)

    memo = (bill.get("memo") or bill.get("vendor_memo") or "").strip()

    return {
        "id": bill["id"],
        "payment_id": (payment.get("id") or "").strip(),
        "vendor_id": _vendor_id(bill),
        "vendor_name": (vendor.get("name") or vendor.get("remote_name") or "").strip(),
        "check_number": (payment.get("customer_friendly_payment_id") or "").strip(),
        "payment_date": payment_date,
        "memo": memo,
        "total_amount": total,
        "invoice_number": bill.get("invoice_number") or "",
        "payment_method": _payment_method(bill),
    }


def _expand_bill(bill: dict) -> list[dict]:
    """Return one dict per line_item — same structure as card transaction rows."""
    vendor_id = _vendor_id(bill)
    invoice = bill.get("invoice_number") or ""
    raw_date = (
        bill.get("accounting_date")
        or bill.get("paid_at")
        or bill.get("issued_at")
        or ""
    )
    date_str = _format_date(raw_date)
    memo = (bill.get("memo") or bill.get("vendor_memo") or "").strip()
    department = _department(bill)
    line_items = bill.get("line_items") or []

    rows = []
    for i, item in enumerate(line_items):
        item_memo = (item.get("memo") or "").strip()
        rows.append({
            "id": bill["id"],
            "vendor_id": vendor_id,
            "invoice": invoice,
            "date": date_str,
            "memo": item_memo or memo,
            "gl_account": _gl_account(item),
            "department": department,
            "amount": f"{_line_item_amount(item):.2f}",
            "num_distributions": len(line_items),
            "dist_number": i + 1,
        })
    return rows


def fetch_completed_bills(
    client_id: str,
    client_secret: str,
    skip_ids: set[str],
    from_date: str | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Pull all NOT_SYNCED + PAYMENT_COMPLETED bills and expand into rows.
    Returns (purchase_rows, payment_rows, skipped).
      purchase_rows — for Sage 50 Purchases Journal (sage_formatter)
      payment_rows  — for Sage 50 Payments Journal (billpay_payment_formatter)
    """
    log = logging.getLogger(__name__)
    token = _get_token(client_id, client_secret)

    params: dict = {"page_size": 100}
    if from_date:
        if len(from_date) == 10:
            from_date = from_date + "T00:00:00Z"
        params["from_date"] = from_date

    raw_bills: list[dict] = []
    next_cursor = None

    while True:
        if next_cursor:
            # Bills pagination may use a cursor token rather than a full URL
            if next_cursor.startswith("http"):
                body = _get(token, {}, url=next_cursor)
            else:
                params_page = {"page_size": 100, "start": next_cursor}
                body = _get(token, params_page)
        else:
            body = _get(token, params)

        for bill in body.get("data", []):
            if bill.get("sync_status") != "NOT_SYNCED":
                continue
            if bill.get("status_summary") != "PAYMENT_COMPLETED":
                continue
            if bill["id"] in skip_ids:
                continue
            raw_bills.append(bill)

        next_cursor = body.get("page", {}).get("next")
        if not next_cursor:
            break
        params = {}

    # Sort oldest-first by accounting_date
    raw_bills.sort(
        key=lambda b: (
            b.get("accounting_date")
            or b.get("paid_at")
            or b.get("issued_at")
            or ""
        )
    )

    purchase_rows: list[dict] = []
    payment_rows: list[dict] = []
    skipped: list[dict] = []

    for bill in raw_bills:
        errors = _validate(bill)
        if errors:
            vendor = (bill.get("vendor") or {}).get("name") or "unknown vendor"
            raw_date = bill.get("accounting_date") or bill.get("paid_at") or bill.get("issued_at") or ""
            date_str = _format_date(raw_date)
            log.warning(
                "SKIPPED %s  %s  %s -- %s",
                bill["id"], vendor, date_str, "; ".join(errors),
            )
            skipped.append({"merchant": vendor, "date": date_str, "reasons": errors})
            continue

        purchase_rows.extend(_expand_bill(bill))
        payment_rows.append(_expand_payment(bill))

    return purchase_rows, payment_rows, skipped


def mark_synced(client_id: str, client_secret: str, bill_ids: list[str]) -> None:
    """Mark a list of bill IDs as synced in Ramp."""
    import uuid
    log = logging.getLogger(__name__)
    token = _get_token(client_id, client_secret, write=True)
    resp = requests.post(
        RAMP_SYNCS_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "idempotency_key": str(uuid.uuid4()),
            "sync_type": "BILL_SYNC",
            "successful_syncs": [
                {"id": bid, "reference_id": bid}
                for bid in bill_ids
            ],
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ramp sync API {resp.status_code}\n"
            f"body: {resp.text}"
        )
    log.info("Marked %d bill(s) as synced in Ramp.", len(bill_ids))


def mark_payments_synced(client_id: str, client_secret: str, payment_ids: list[str]) -> None:
    """Attempt to mark bill payment IDs as synced in Ramp via BILL_SYNC.

    Ramp does not expose a PAYMENT_SYNC type. This tries BILL_SYNC with the
    payment IDs — Ramp may accept payment UUIDs through the same sync type.
    If it fails, the payment will need to be dismissed manually in Ramp's GUI.
    """
    import uuid
    log = logging.getLogger(__name__)
    payment_ids = [pid for pid in payment_ids if pid]
    if not payment_ids:
        log.info("No payment IDs to mark synced.")
        return
    token = _get_token(client_id, client_secret, write=True)
    resp = requests.post(
        RAMP_SYNCS_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "idempotency_key": str(uuid.uuid4()),
            "sync_type": "BILL_SYNC",
            "successful_syncs": [
                {"id": pid, "reference_id": pid}
                for pid in payment_ids
            ],
        },
        timeout=30,
    )
    if not resp.ok:
        log.warning(
            "Could not mark payment(s) as synced via Ramp API (status %d): %s\n"
            "Ramp does not expose a payment sync type — dismiss these manually in "
            "Ramp's accounting export UI.",
            resp.status_code, resp.text,
        )
        return
    log.info("Marked %d payment(s) as synced in Ramp.", len(payment_ids))


def dump_raw_bill(
    client_id: str,
    client_secret: str,
    vendor: str | None = None,
    any_status: bool = False,
) -> tuple[dict | None, dict]:
    """Return the oldest matching PAYMENT_COMPLETED bill for inspection.

    By default only returns NOT_SYNCED bills (same filter as the normal export).
    Pass any_status=True to include already-synced bills — useful for inspecting
    the payment sub-object structure after a bill has been marked synced.
    """
    token = _get_token(client_id, client_secret)
    params: dict = {"page_size": 100}
    candidates: list[dict] = []
    last_body: dict = {}
    next_cursor = None

    while True:
        if next_cursor:
            if next_cursor.startswith("http"):
                last_body = _get(token, {}, url=next_cursor)
            else:
                last_body = _get(token, {"page_size": 100, "start": next_cursor})
        else:
            last_body = _get(token, params)

        for bill in last_body.get("data", []):
            if not any_status and bill.get("sync_status") != "NOT_SYNCED":
                continue
            if bill.get("status_summary") != "PAYMENT_COMPLETED":
                continue
            if vendor:
                name = ((bill.get("vendor") or {}).get("name") or "").lower()
                if vendor.lower() not in name:
                    continue
            candidates.append(bill)

        next_cursor = last_body.get("page", {}).get("next")
        if not next_cursor:
            break
        params = {}

    if not candidates:
        return None, last_body

    candidates.sort(
        key=lambda b: (
            b.get("accounting_date")
            or b.get("paid_at")
            or b.get("issued_at")
            or ""
        )
    )
    return candidates[0], last_body
