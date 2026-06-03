"""
Ramp API client — fetches card transactions that are SYNC_READY and expands
each transaction's line_items into individual Sage distribution rows.

Run  python main.py --dump-raw  to inspect the raw JSON before going live.
"""

import datetime
import os
import requests

RAMP_TOKEN_URL = "https://api.ramp.com/developer/v1/token"
RAMP_TRANSACTIONS_URL = "https://api.ramp.com/developer/v1/transactions"
RAMP_SYNCS_URL = "https://api.ramp.com/developer/v1/accounting/syncs"


def _get_token(client_id: str, client_secret: str, write: bool = False) -> str:
    scope = "transactions:read accounting:write" if write else "transactions:read"
    resp = requests.post(
        RAMP_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": scope},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def mark_synced(client_id: str, client_secret: str, transaction_ids: list[str]) -> None:
    """Mark a list of transaction IDs as synced in Ramp."""
    import logging
    import uuid
    log = logging.getLogger(__name__)

    token = _get_token(client_id, client_secret, write=True)
    resp = requests.post(
        RAMP_SYNCS_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "idempotency_key": str(uuid.uuid4()),
            "sync_type": "TRANSACTION_SYNC",
            "successful_syncs": [
                {"id": tid, "reference_id": tid}
                for tid in transaction_ids
            ],
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ramp sync API {resp.status_code}\n"
            f"body: {resp.text}"
        )
    log.info("Marked %d transaction(s) as synced in Ramp.", len(transaction_ids))


def _get(token: str, params: dict, url: str = RAMP_TRANSACTIONS_URL) -> dict:
    """GET transactions. Pass a full next-page URL directly to avoid double-encoding."""
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params if url == RAMP_TRANSACTIONS_URL else None,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ramp API {resp.status_code} on GET /transactions\n"
            f"url:    {resp.url}\n"
            f"body:   {resp.text}"
        )
    return resp.json()


def _vendor_id(tx: dict) -> str:
    """Vendor ID from top-level accounting_field_selections (type MERCHANT)."""
    for sel in tx.get("accounting_field_selections") or []:
        if sel.get("type") == "MERCHANT":
            return (sel.get("external_id") or "").strip()
    return ""


def _gl_account(item: dict) -> str:
    """GL account code from a line item's accounting_field_selections (type GL_ACCOUNT).
    Sage wants the account code (external_code), not the display name (external_id).
    """
    for sel in item.get("accounting_field_selections") or []:
        if sel.get("type") == "GL_ACCOUNT":
            return (sel.get("external_code") or sel.get("external_id") or "").strip()
    return ""


def _line_item_amount(item: dict) -> float:
    amt = item.get("amount") or {}
    if isinstance(amt, dict):
        raw = amt.get("amount", 0)
        rate = amt.get("minor_unit_conversion_rate", 100)
        return raw / rate
    return float(amt)


def _clean_text(s: str) -> str:
    """Replace newlines/carriage returns with a space.

    Sage 50's CSV importer does not handle embedded newlines in quoted fields.
    """
    return s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()


def _format_date(raw: str) -> str:
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return raw[:10]


def _expand_transaction(tx: dict, invoice: str) -> list[dict]:
    """Return one dict per line_item (= one Sage distribution row)."""
    vendor_id = _vendor_id(tx)
    # Sage "Date" column maps to Accounting Date; fall back to transaction time
    date_str = _format_date(tx.get("accounting_date") or tx.get("user_transaction_time") or "")
    holder = tx.get("card_holder") or {}
    first = (holder.get("first_name") or "").strip()
    last = (holder.get("last_name") or "").strip()
    cardholder = f"{first} {last}".strip()
    tx_memo = _clean_text(tx.get("memo") or tx.get("merchant_name") or "")
    if cardholder:
        tx_memo = f"{cardholder} - {tx_memo}" if tx_memo else cardholder
    department = holder.get("department_name") or ""

    line_items = tx.get("line_items") or []

    # Transaction with no line items — treat as a single distribution
    if not line_items:
        return [{
            "id": tx["id"],
            "vendor_id": vendor_id,
            "invoice": invoice,
            "date": date_str,
            "memo": tx_memo,
            "gl_account": "",
            "department": department,
            "amount": f"{float(tx.get('amount', 0)):.2f}",
            "num_distributions": 1,
            "dist_number": 1,
        }]

    rows = []
    for i, item in enumerate(line_items):
        item_memo = _clean_text(item.get("memo") or "")
        if item_memo:
            memo = f"{cardholder} - {item_memo}" if cardholder else item_memo
        else:
            memo = tx_memo
        rows.append({
            "id": tx["id"],
            "vendor_id": vendor_id,
            "invoice": invoice,
            "date": date_str,
            "memo": memo,
            "gl_account": _gl_account(item),
            "department": department,
            "amount": f"{_line_item_amount(item):.2f}",
            "num_distributions": len(line_items),
            "dist_number": i + 1,
        })
    return rows


def fetch_sync_ready_transactions(
    client_id: str,
    client_secret: str,
    skip_ids: set[str],
    from_date: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Pull all SYNC_READY card transactions and expand into per-line-item rows.
    Returns (rows, skipped) where skipped is a list of dicts with keys:
      merchant, date, reasons  — for surfacing in the email body.
    """
    token = _get_token(client_id, client_secret)

    params: dict = {"page_size": 100}
    if from_date:
        # Ramp requires a full ISO datetime, not just a date
        if len(from_date) == 10:
            from_date = from_date + "T00:00:00Z"
        params["from_date"] = from_date

    raw_txns: list[dict] = []
    next_url = None
    while True:
        body = _get(token, params, url=next_url or RAMP_TRANSACTIONS_URL)
        for tx in body.get("data", []):
            if tx.get("sync_status") != "SYNC_READY":
                continue
            if tx["id"] in skip_ids:
                continue
            raw_txns.append(tx)

        next_url = body.get("page", {}).get("next")
        if not next_url:
            break
        params = {}  # params are already baked into next_url

    return _assign_invoices_and_expand(raw_txns)


def _validate(tx: dict) -> list[str]:
    """Return a list of validation error strings; empty list means the transaction is ok."""
    errors = []

    if not _vendor_id(tx):
        errors.append("missing Vendor ID (set Accounting Vendor in Ramp)")

    line_items = tx.get("line_items") or []
    if line_items:
        for i, item in enumerate(line_items, 1):
            if not _gl_account(item):
                errors.append(f"line item {i} missing G/L Account")
    else:
        errors.append("no line items (transaction has no expense splits)")

    return errors


def _assign_invoices_and_expand(txns: list[dict]) -> tuple[list[dict], list[dict]]:
    """Validate, generate unique invoice numbers, then expand each transaction to rows.
    Transactions are sorted oldest-first by accounting_date so --limit N picks the
    earliest unsynced transactions and imports into Sage in chronological order.
    Returns (rows, skipped) where each skipped entry has merchant/date/reasons keys.
    """
    import logging
    log = logging.getLogger(__name__)

    txns = sorted(
        txns,
        key=lambda t: t.get("accounting_date") or t.get("user_transaction_time") or "",
    )

    rows: list[dict] = []
    skipped: list[dict] = []

    for tx in txns:
        errors = _validate(tx)
        if errors:
            merchant = tx.get("merchant_name") or "unknown merchant"
            date_str = _format_date(tx.get("accounting_date") or tx.get("user_transaction_time") or "")
            log.warning("SKIPPED %s  %s  %s -- %s", tx["id"], merchant, date_str, "; ".join(errors))
            skipped.append({
                "merchant": merchant,
                "date": date_str,
                "reasons": errors,
                "ramp_url": f"https://app.ramp.com/details/list/transactions/{tx['id']}",
            })
            continue

        vendor = _vendor_id(tx)
        date_str = _format_date(tx.get("accounting_date") or tx.get("user_transaction_time") or "")
        # Sage 50 Invoice/CM # field limit: 20 characters.
        # Format: {vendor[:9]}.{MMDDYY}.{id[:3]}  = 9+1+6+1+3 = 20 chars max.
        # The 3-char transaction ID suffix ensures uniqueness across export runs;
        # same transaction always produces the same invoice number.
        m, d, y = (date_str.split("/") + ["", "", ""])[:3]
        date_compact = f"{m}{d}{y[2:]}"   # e.g. "050426" for 05/04/2026
        short_id = tx["id"][-3:]
        invoice = f"{vendor[:9]}.{date_compact}.{short_id}"

        rows.extend(_expand_transaction(tx, invoice))

    return rows, skipped


def fetch_transactions_by_ids(
    client_id: str,
    client_secret: str,
    transaction_ids: list[str],
) -> tuple[list[dict], list[dict]]:
    """
    Fetch specific transactions by ID and expand into Sage distribution rows.
    Bypasses sync_status filter and state file — for re-exporting specific
    transactions that were missed or lost during import testing.
    Returns (rows, skipped).
    """
    import logging
    log = logging.getLogger(__name__)
    token = _get_token(client_id, client_secret)

    txns = []
    for tid in transaction_ids:
        resp = requests.get(
            f"{RAMP_TRANSACTIONS_URL}/{tid}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if not resp.ok:
            log.error("Could not fetch transaction %s: %s %s", tid, resp.status_code, resp.text)
            continue
        txns.append(resp.json())

    return _assign_invoices_and_expand(txns)


def dump_raw_transaction(
    client_id: str, client_secret: str, merchant: str | None = None
) -> tuple[dict | None, dict]:
    """
    Return the oldest matching SYNC_READY transaction (same ordering as the export).
    Pass merchant (case-insensitive substring) to find a specific one.
    """
    token = _get_token(client_id, client_secret)
    params: dict = {"page_size": 100}
    next_url = None
    candidates: list[dict] = []
    last_body: dict = {}

    while True:
        last_body = _get(token, params, url=next_url or RAMP_TRANSACTIONS_URL)
        for tx in last_body.get("data", []):
            if tx.get("sync_status") != "SYNC_READY":
                continue
            if merchant:
                name = (tx.get("merchant_name") or "").lower()
                if merchant.lower() not in name:
                    continue
            candidates.append(tx)

        next_url = last_body.get("page", {}).get("next")
        if not next_url:
            break
        params = {}

    if not candidates:
        return None, last_body

    candidates.sort(
        key=lambda t: t.get("accounting_date") or t.get("user_transaction_time") or ""
    )
    return candidates[0], last_body
