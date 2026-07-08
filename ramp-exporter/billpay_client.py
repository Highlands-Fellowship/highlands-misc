"""
Ramp API client for bill pay.

Fetches bills with sync_status=NOT_SYNCED and either status_summary=
PAYMENT_COMPLETED, or status_summary=PAYMENT_PROCESSING when paid by check
(funds leave the bank when a check is cut, well before Ramp marks it
PAYMENT_COMPLETED — see _is_exportable_status()). Expands each bill's
line_items into Sage 50 vendor-invoice distribution rows (same structure as
card transactions — reuses sage_formatter.build_csv directly).

Run  python billpay.py --dump-raw  to inspect raw JSON before going live.

Key field locations (verify with --dump-raw against a live bill):
  - Vendor ID:   bill.vendor.remote_id  (fallback: remote_code, name)
  - Invoice #:   bill.invoice_number    (present on bills — no generation needed)
  - Date:        bill.accounting_date   (fallback: paid_at, issued_at)
  - GL Account:  line_items[].accounting_field_selections[type=GL_ACCOUNT].external_code
  - Amount:      line_items[].amount.amount / minor_unit_conversion_rate
  - Department:  accounting_field_selections[type=DEPARTMENT].external_id

Some vendors reuse the same invoice number across unrelated bills, which Sage 50
rejects as a duplicate reference on import. BILLPAY_DEDUPE_VENDORS (comma-separated
vendor IDs) opts specific vendors into a uniquifying suffix on the invoice number —
see _effective_invoice_number().
"""

import datetime
import logging
import os
import re
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


def _clean_text(s: str) -> str:
    """Replace newlines/carriage returns with a space.

    Sage 50's CSV importer does not handle embedded newlines in quoted fields.
    """
    return s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()


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


def _dedupe_vendors() -> set[str]:
    return {v.strip() for v in os.getenv("BILLPAY_DEDUPE_VENDORS", "").split(",") if v.strip()}


def _effective_invoice_number(bill: dict, vendor_id: str) -> str:
    """Invoice number to use on the Sage 50 rows.

    For vendors listed in BILLPAY_DEDUPE_VENDORS, append a suffix derived from the
    Ramp bill ID so bills that reuse the same invoice number don't collide in Sage.
    Deterministic per bill ID, so the same bill always maps to the same value across
    export runs. Must be used identically for both the Purchases and Payments Journal
    rows of a given bill, since Sage matches payments to invoices by this string.
    """
    raw = (bill.get("invoice_number") or "").strip()
    if vendor_id not in _dedupe_vendors():
        return raw
    suffix = bill["id"][-4:]
    return f"{raw[:15]}-{suffix}"[:20]


def _is_exportable_status(bill: dict) -> bool:
    """Whether this bill's status means the payment has actually left the bank.

    PAYMENT_COMPLETED always qualifies. A check payment also qualifies while
    still PAYMENT_PROCESSING — Ramp doesn't flip a bill to PAYMENT_COMPLETED
    until the check clears, but the funds are debited (and the check mailed)
    once the payment is initiated, well before that. ACH/wire payments in
    PAYMENT_PROCESSING are excluded — funds aren't committed until completed.
    """
    status_summary = bill.get("status_summary")
    if status_summary == "PAYMENT_COMPLETED":
        return True
    if status_summary == "PAYMENT_PROCESSING":
        payment_method = ((bill.get("payment") or {}).get("payment_method") or "").upper()
        return payment_method == "CHECK"
    return False


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


def _bill_amount(bill: dict) -> float:
    amt_obj = bill.get("amount") or {}
    if isinstance(amt_obj, dict):
        return amt_obj.get("amount", 0) / amt_obj.get("minor_unit_conversion_rate", 100)
    return float(amt_obj)


def _expand_payment(bill: dict) -> dict:
    """Return one payment row dict for this bill's own invoice.

    "amount" is this bill's own total — NOT payment.amount, which is the total
    across every bill Ramp grouped into the same ACH payment. Rows sharing a
    payment are combined into one multi-distribution entry by _group_payments().
    """
    payment = bill.get("payment") or {}
    vendor = bill.get("vendor") or {}

    raw_date = (
        payment.get("payment_date")
        or payment.get("effective_date")
        or bill.get("paid_at")
        or ""
    )
    payment_date = _format_date(raw_date)

    memo = _clean_text(bill.get("memo") or bill.get("vendor_memo") or "")
    vendor_id = _vendor_id(bill)

    return {
        "id": bill["id"],
        "payment_id": (payment.get("id") or "").strip(),
        "vendor_id": vendor_id,
        "vendor_name": (vendor.get("name") or vendor.get("remote_name") or "").strip(),
        "check_number": (payment.get("customer_friendly_payment_id") or "").strip(),
        "payment_date": payment_date,
        "memo": memo,
        "amount": _bill_amount(bill),
        "invoice_number": _effective_invoice_number(bill, vendor_id),
        "payment_method": _payment_method(bill),
    }


def _group_payments(payment_rows: list[dict]) -> list[dict]:
    """Combine bills paid together in a single Ramp ACH payment into one
    multi-distribution Payments Journal entry.

    Groups by Ramp payment ID (falls back to vendor+check+date if missing).
    Each row keeps its own "amount"; "total_amount" and "num_distributions"
    are set to the group's sum/count so Sage 50 sees one payment covering
    multiple invoices, matching how card statement payments are grouped.
    """
    groups: dict[str, list[dict]] = {}
    for row in payment_rows:
        key = row["payment_id"] or f"{row['vendor_id']}|{row['check_number']}|{row['payment_date']}"
        groups.setdefault(key, []).append(row)

    grouped: list[dict] = []
    for rows in groups.values():
        total = sum(r["amount"] for r in rows)
        for r in rows:
            r["total_amount"] = total
            r["num_distributions"] = len(rows)
            grouped.append(r)
    return grouped


def _expand_bill(bill: dict) -> list[dict]:
    """Return one dict per line_item — same structure as card transaction rows."""
    vendor_id = _vendor_id(bill)
    invoice = _effective_invoice_number(bill, vendor_id)
    raw_date = (
        bill.get("accounting_date")
        or bill.get("paid_at")
        or bill.get("issued_at")
        or ""
    )
    date_str = _format_date(raw_date)
    memo = _clean_text(bill.get("memo") or bill.get("vendor_memo") or "")
    department = _department(bill)
    line_items = bill.get("line_items") or []

    rows = []
    for i, item in enumerate(line_items):
        item_memo = _clean_text(item.get("memo") or "")
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
            if not _is_exportable_status(bill):
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

    return _expand_bills(raw_bills)


def find_unsynced_bills(
    client_id: str,
    client_secret: str,
    from_date: str | None = None,
) -> list[dict]:
    """
    Sweep bills directly from Ramp and return every one that meets our export
    criteria (_is_exportable_status) but isn't fully synced yet
    (sync_status != BILL_AND_PAYMENT_SYNCED).

    Unlike fetch_completed_bills, this ignores exported_bill_ids.json and the
    NOT_SYNCED-only filter — it finds bills stuck in any partial sync state
    (e.g. BILL_SYNC succeeded but BILL_PAYMENT_SYNC didn't), including ones
    from before pending_sync_ids.json existed to track them. Use this for an
    occasional thorough audit; --reconcile's local pending list is the cheap
    day-to-day check.
    """
    token = _get_token(client_id, client_secret)

    params: dict = {"page_size": 100}
    if from_date:
        if len(from_date) == 10:
            from_date = from_date + "T00:00:00Z"
        params["from_date"] = from_date

    found: list[dict] = []
    next_cursor = None

    while True:
        if next_cursor:
            if next_cursor.startswith("http"):
                body = _get(token, {}, url=next_cursor)
            else:
                body = _get(token, {"page_size": 100, "start": next_cursor})
        else:
            body = _get(token, params)

        for bill in body.get("data", []):
            if bill.get("sync_status") == "BILL_AND_PAYMENT_SYNCED":
                continue
            if not _is_exportable_status(bill):
                continue
            found.append({
                "id": bill["id"],
                "vendor": (bill.get("vendor") or {}).get("name") or "unknown vendor",
                "invoice_number": bill.get("invoice_number") or "",
                "amount": _bill_amount(bill),
                "status_summary": bill.get("status_summary"),
                "sync_status": bill.get("sync_status"),
            })

        next_cursor = body.get("page", {}).get("next")
        if not next_cursor:
            break
        params = {}

    found.sort(key=lambda b: (b["vendor"], b["invoice_number"]))
    return found


def _expand_bills(raw_bills: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Validate and expand a list of raw bill dicts into Sage rows.

    Shared by fetch_completed_bills() and fetch_bills_by_ids().
    """
    log = logging.getLogger(__name__)
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
            skipped.append({
                "merchant": vendor,
                "date": date_str,
                "reasons": errors,
                "ramp_url": f"https://app.ramp.com/bill-pay/bills/list/{bill['id']}",
            })
            continue

        purchase_rows.extend(_expand_bill(bill))
        payment_rows.append(_expand_payment(bill))

    return purchase_rows, _group_payments(payment_rows), skipped


def fetch_bills_by_ids(
    client_id: str,
    client_secret: str,
    bill_ids: list[str],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Fetch specific bills by ID and expand into Sage rows. Bypasses the
    NOT_SYNCED filter and the exported_bill_ids.json state file — for
    re-exporting a bill that was already synced (e.g. to verify a config
    change like BILLPAY_DEDUPE_VENDORS took effect, or to recover a bill
    lost during import testing).
    Returns (purchase_rows, payment_rows, skipped).
    """
    log = logging.getLogger(__name__)
    token = _get_token(client_id, client_secret)

    bills = []
    for bid in bill_ids:
        resp = requests.get(
            f"{RAMP_BILLS_URL}/{bid}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if not resp.ok:
            log.error("Could not fetch bill %s: %s %s", bid, resp.status_code, resp.text)
            continue
        bills.append(resp.json())

    return _expand_bills(bills)


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def mark_synced(client_id: str, client_secret: str, bill_ids: list[str]) -> set[str]:
    """Mark a list of bill IDs as synced in Ramp (BILL_SYNC + BILL_PAYMENT_SYNC).

    A check bill exported while still PAYMENT_PROCESSING (see
    _is_exportable_status) isn't yet eligible for BILL_PAYMENT_SYNC on Ramp's
    side — it rejects the whole batch with DEVELOPER_7062 ("not ready for
    sync") if even one bill isn't ready. When that happens, the offending IDs
    are parsed out of the error message and retried without them, so the rest
    of the batch still gets confirmed.

    Returns the set of bill IDs that could not be fully synced (deferred in
    either phase) — these stay exported to Sage but won't show as synced in
    Ramp until a later run, once their check clears. Callers should persist
    this set (see billpay.py's pending_sync_ids.json / --reconcile) since a
    deferred bill is never automatically retried once it's already in
    exported_bill_ids.json — the normal fetch skips it regardless of Ramp's
    sync_status.
    """
    import uuid
    log = logging.getLogger(__name__)
    token = _get_token(client_id, client_secret, write=True)
    deferred: set[str] = set()

    for sync_type in ("BILL_SYNC", "BILL_PAYMENT_SYNC"):
        pending = [b for b in bill_ids if b not in deferred]
        while pending:
            resp = requests.post(
                RAMP_SYNCS_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "idempotency_key": str(uuid.uuid4()),
                    "sync_type": sync_type,
                    "successful_syncs": [
                        {"id": bid, "reference_id": bid}
                        for bid in pending
                    ],
                },
                timeout=30,
            )
            if resp.ok:
                log.info("[%s] Marked %d bill(s) as synced in Ramp.", sync_type, len(pending))
                break

            try:
                body = resp.json()
                error_v2 = body.get("error_v2") or {}
                error_code = body.get("error_code") or error_v2.get("error_code") or ""
                message = error_v2.get("message") or body.get("message") or ""
            except Exception:
                error_code = ""
                message = ""

            if error_code == "DEVELOPER_7062":
                not_ready = set(_UUID_RE.findall(message)) & set(pending)
                if sync_type == "BILL_SYNC":
                    # "Not ready" for BILL_SYNC means it's already done (BILL_SYNC
                    # eligibility doesn't regress) — skip it here and let
                    # BILL_PAYMENT_SYNC make the real ready/not-ready call.
                    skip = not_ready or set(pending)
                    log.info(
                        "[BILL_SYNC] %d bill(s) already synced, skipping: %s",
                        len(skip), ", ".join(sorted(skip)),
                    )
                    pending = [b for b in pending if b not in skip]
                    continue
                if not_ready:
                    log.warning(
                        "[%s] %d bill(s) not ready for sync yet — deferring until a "
                        "later run: %s",
                        sync_type, len(not_ready), ", ".join(sorted(not_ready)),
                    )
                    deferred |= not_ready
                    pending = [b for b in pending if b not in not_ready]
                    continue

            raise RuntimeError(
                f"Ramp sync API ({sync_type}) {resp.status_code}\n"
                f"body: {resp.text}"
            )

    return deferred


def dump_raw_bill_by_id(client_id: str, client_secret: str, bill_id: str) -> dict | None:
    """Fetch one specific bill by ID, bypassing all filters — for inspecting a
    bill you've already identified (e.g. from Ramp's UI or a --dump-raw list)."""
    token = _get_token(client_id, client_secret)
    resp = requests.get(
        f"{RAMP_BILLS_URL}/{bill_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if not resp.ok:
        return None
    return resp.json()


def dump_raw_bill(
    client_id: str,
    client_secret: str,
    vendor: str | None = None,
    any_status: bool = False,
) -> tuple[dict | None, list[dict]]:
    """Return the oldest matching bill for inspection, plus every matching
    bill found — a vendor can have several bills in different states, and
    the oldest one may not be the one you're looking for.

    By default only returns NOT_SYNCED + PAYMENT_COMPLETED bills (same filter
    as the normal export). Pass any_status=True to bypass both the sync_status
    and status_summary filters — useful for inspecting bills in other states
    (e.g. a check payment that's been initiated but not yet completed) or
    already-synced bills (e.g. the payment sub-object after a bill is marked synced).
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
            if not any_status and not _is_exportable_status(bill):
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
        return None, []

    candidates.sort(
        key=lambda b: (
            b.get("accounting_date")
            or b.get("paid_at")
            or b.get("issued_at")
            or ""
        )
    )
    return candidates[0], candidates
