"""
Ramp API client for card statements.

Fetches closed statements and re-derives the card-transaction invoice numbers
(using the same formula as ramp_client.py) so the Payments Journal CSV can
reference them and clear the open AP invoices created by the Purchases Journal
import.

Run  python card_payment.py --dump-raw  to inspect raw JSON before going live.

Key field locations (confirmed from live API):
  - Statement ID:        statement["id"]
  - Period start:        statement["start_date"]      (ISO datetime)
  - Period end:          statement["end_date"]         (ISO datetime, = due/payment date)
  - Transactions:        statement["statement_lines"][]["id"]  (type="CARD_TRANSACTION")
  - Entity ID (filter):  statement["balance_sections"][0]["entity_id"]

Filtering:
  - Closed statements: end_date < now  (no payment_status field in the API)
  - Card program:      set CARD_PAYMENT_ENTITY_ID in .env to the entity_id from
                       your Ramp Card statements to exclude Subscription statements.
                       Find it in --dump-raw output under balance_sections[0].entity_id.
"""

import datetime
import logging
import os
import requests

RAMP_TOKEN_URL = "https://api.ramp.com/developer/v1/token"
RAMP_STATEMENTS_URL = "https://api.ramp.com/developer/v1/statements"
RAMP_TRANSACTIONS_URL = "https://api.ramp.com/developer/v1/transactions"


def _get_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        RAMP_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": "statements:read transactions:read"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _get(token: str, url: str, params: dict | None = None) -> dict:
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ramp API {resp.status_code} on GET {url}\n"
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


def _is_closed(stmt: dict) -> bool:
    """Return True if the statement period has ended (end_date is in the past)."""
    end_raw = stmt.get("end_date") or stmt.get("period_end") or ""
    if not end_raw:
        return False
    try:
        dt = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        return dt < datetime.datetime.now(datetime.timezone.utc)
    except Exception:
        return False


def _matches_entity_filter(stmt: dict) -> bool:
    """
    If CARD_PAYMENT_ENTITY_ID is set, only include statements whose
    balance_sections contain that entity_id.  Use this to exclude
    Subscription statements from the Ramp Card payment export.

    Set CARD_PAYMENT_ENTITY_ID to the value shown in --dump-raw under
    balance_sections[0].entity_id for a Ramp Card statement.
    """
    entity_filter = os.getenv("CARD_PAYMENT_ENTITY_ID", "").strip()
    if not entity_filter:
        return True
    for section in stmt.get("balance_sections") or []:
        if section.get("entity_id") == entity_filter:
            return True
    return False


def _statement_tx_ids(stmt: dict) -> set[str]:
    """Extract CARD_TRANSACTION IDs directly from statement_lines."""
    return {
        line["id"]
        for line in (stmt.get("statement_lines") or [])
        if line.get("type") == "CARD_TRANSACTION"
    }


def _statement_payment_date(stmt: dict) -> str:
    """Return the statement payment date.  end_date = due/settlement date."""
    raw = stmt.get("end_date") or stmt.get("period_end") or stmt.get("due_date") or ""
    return _format_date(raw)


def _statement_check_prefix(stmt: dict) -> str:
    """
    Return a short date-based prefix for check numbers: RAMP-MMDDYY
    Each vendor in the statement gets RAMP-MMDDYY-001, -002, etc.
    Max length: 14 chars for prefix alone, 18 with -NNN suffix (within Sage 50's 20-char limit).
    """
    end_raw = stmt.get("end_date") or stmt.get("period_end") or ""
    date_str = _format_date(end_raw)
    m, d, y = (date_str.split("/") + ["", "", ""])[:3]
    mmddyy = f"{m}{d}{y[2:]}" if m and d and y else "000000"
    return f"RAMP-{mmddyy}"


def _vendor_id(tx: dict) -> str:
    """Vendor ID from top-level accounting_field_selections (type MERCHANT)."""
    for sel in tx.get("accounting_field_selections") or []:
        if sel.get("type") == "MERCHANT":
            return (sel.get("external_id") or "").strip()
    return ""


def _vendor_name(tx: dict) -> str:
    return (tx.get("merchant_name") or "").strip()


def _tx_amount(tx: dict) -> float:
    """
    Total transaction amount in USD display units.

    Uses the top-level 'amount' field (already in USD display units per Ramp API).
    line_items[].amount can be in the merchant's local currency for international
    transactions (e.g. HNL for Honduras), so it cannot be used reliably here.
    """
    return float(tx.get("amount", 0))


def _invoice_number(tx: dict) -> str:
    """Regenerate the same invoice number that main.py produces."""
    vendor = _vendor_id(tx)
    raw_date = tx.get("accounting_date") or tx.get("user_transaction_time") or ""
    date_str = _format_date(raw_date)
    m, d, y = (date_str.split("/") + ["", "", ""])[:3]
    date_compact = f"{m}{d}{y[2:]}"
    short_id = tx["id"][-3:]
    return f"{vendor[:9]}.{date_compact}.{short_id}"


def _fetch_transactions_for_statement(token: str, stmt: dict) -> list[dict]:
    """
    Fetch all transactions belonging to this statement.

    Filters by statement_id (confirmed present on transaction objects from live API),
    which is more reliable than a date-range fetch since it won't miss transactions
    whose accounting_date falls slightly outside the statement period.
    """
    stmt_id = stmt.get("id") or ""
    if not stmt_id:
        return []

    params: dict = {"page_size": 100, "statement_id": stmt_id}
    fetched: list[dict] = []
    next_url = None

    while True:
        if next_url:
            body = _get(token, next_url)
        else:
            body = _get(token, RAMP_TRANSACTIONS_URL, params=params)

        fetched.extend(body.get("data", []))

        next_url = body.get("page", {}).get("next")
        if not next_url:
            break

    return fetched


def _build_payment_rows(stmt: dict, txns: list[dict], include_all: bool = False) -> list[dict]:
    """
    Convert a statement + its transactions into Payments Journal row dicts,
    grouped by vendor.  One logical payment per vendor, N rows (one per invoice).
    """
    payment_date = _statement_payment_date(stmt)
    check_prefix = _statement_check_prefix(stmt)

    start_raw = stmt.get("start_date") or ""
    end_raw_memo = stmt.get("end_date") or ""
    try:
        dt_start = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        dt_end = datetime.datetime.fromisoformat(end_raw_memo.replace("Z", "+00:00"))
        memo = (
            f"Ramp Card Payment "
            f"{dt_start.strftime('%b')} {dt_start.day} - "
            f"{dt_end.strftime('%b')} {dt_end.day}, {dt_end.year}"
        )
    except Exception:
        memo = "Ramp Card Payment"

    log = logging.getLogger(__name__)

    # Group by vendor_id; collect transactions missing a vendor ID or not yet synced.
    # sync_ready_txns: SYNC_READY transactions — card_payment.py can auto-export these.
    # blocked:         NOT_SYNCED (or unknown) transactions — need coding in Ramp first.
    by_vendor: dict[str, list[dict]] = {}
    skipped: list[dict] = []
    sync_ready_txns: list[dict] = []
    blocked: list[dict] = []
    for tx in txns:
        vid = _vendor_id(tx)
        raw_date = tx.get("accounting_date") or tx.get("user_transaction_time") or ""
        merchant = tx.get("merchant_name") or "unknown merchant"
        if not vid:
            if include_all:
                # Fall back to merchant name as vendor ID so the row is included
                vid = merchant
                log.warning(
                    "NO VENDOR ID %s  %s  %s  $%.2f -- using merchant name as fallback",
                    tx["id"], merchant, _format_date(raw_date), _tx_amount(tx),
                )
            else:
                skipped.append({
                    "merchant": merchant,
                    "date": _format_date(raw_date),
                    "amount": _tx_amount(tx),
                    "id": tx["id"],
                    "reasons": ["missing Vendor ID (set Accounting Vendor in Ramp)"],
                    "ramp_url": f"https://app.ramp.com/details/list/transactions/{tx['id']}",
                })
                log.warning(
                    "SKIPPED %s  %s  %s  $%.2f -- missing Vendor ID",
                    tx["id"], merchant, _format_date(raw_date), _tx_amount(tx),
                )
                continue
        tx_sync = tx.get("sync_status", "")
        if tx_sync != "SYNCED" and not include_all:
            if tx_sync == "SYNC_READY":
                # Ready to export — card_payment.py will auto-include these
                sync_ready_txns.append(tx)
                log.info(
                    "SYNC_READY %s  %s  %s  $%.2f -- will auto-export with payment",
                    tx["id"], merchant, _format_date(raw_date), _tx_amount(tx),
                )
            else:
                # NOT_SYNCED or unknown — needs coding/approval in Ramp first
                blocked.append({
                    "merchant": merchant,
                    "date": _format_date(raw_date),
                    "amount": _tx_amount(tx),
                    "id": tx["id"],
                    "reasons": [
                        f"sync_status: {tx_sync or 'unknown'} — "
                        "transaction needs to be coded and approved in Ramp"
                    ],
                    "ramp_url": f"https://app.ramp.com/details/list/transactions/{tx['id']}",
                })
                log.warning(
                    "BLOCKED %s  %s  %s  $%.2f -- sync_status=%s",
                    tx["id"], merchant, _format_date(raw_date), _tx_amount(tx), tx_sync,
                )
        by_vendor.setdefault(vid, []).append(tx)

    rows = []
    for seq, (vid, vendor_txns) in enumerate(by_vendor.items(), start=1):
        # Unique check number per vendor: RAMP-MMDDYY-001, -002, etc.
        check_number = f"{check_prefix}-{seq:03d}"

        vendor_txns.sort(
            key=lambda t: t.get("accounting_date") or t.get("user_transaction_time") or ""
        )
        invoices = [
            {
                "invoice_number": _invoice_number(t),
                "amount": _tx_amount(t),
                "vendor_name": _vendor_name(t),
                "tx_id": t["id"],
            }
            for t in vendor_txns
        ]
        total = sum(inv["amount"] for inv in invoices)
        vname = invoices[0]["vendor_name"] if invoices else ""
        num_dist = len(invoices)

        for inv in invoices:
            rows.append({
                "vendor_id": vid,
                "vendor_name": vname,
                "check_number": check_number,
                "payment_date": payment_date,
                "memo": memo,
                "total_amount": total,
                "invoice_number": inv["invoice_number"],
                "amount": inv["amount"],
                "num_distributions": num_dist,
                "payment_method": "Check",
                "tx_id": inv["tx_id"],
            })

    return rows, skipped, sync_ready_txns, blocked


def fetch_paid_statements(
    client_id: str,
    client_secret: str,
    include_all: bool = False,
) -> tuple[list[dict], list[str], list[dict], list[dict], list[dict]]:
    """
    Fetch the single most recent closed statement and expand into Payments Journal rows.
    Returns (payment_rows, statement_ids, skipped, sync_ready_txns, blocked).

    skipped          — transactions missing a Vendor ID (can't build a payment row)
    sync_ready_txns  — raw transaction dicts with sync_status=SYNC_READY; card_payment.py
                       can auto-export these as a Purchases Journal CSV so both can be
                       imported together when main.py hasn't run yet
    blocked          — summary dicts for transactions with sync_status=NOT_SYNCED (or
                       unknown); these need to be coded/approved in Ramp first

    Always exports only the most recently closed statement — no local state file needed.
    Sage 50's duplicate check number rejection (RAMP-MMDDYY-NNN) prevents accidental
    double-imports if the script is run twice before a new statement closes.
    """
    log = logging.getLogger(__name__)
    token = _get_token(client_id, client_secret)

    statements: list[dict] = []
    next_url = None
    params: dict = {"page_size": 100}

    while True:
        if next_url:
            body = _get(token, next_url)
        else:
            body = _get(token, RAMP_STATEMENTS_URL, params=params)

        for stmt in body.get("data", []):
            if not _is_closed(stmt):
                continue
            if not _matches_entity_filter(stmt):
                continue
            statements.append(stmt)

        next_url = body.get("page", {}).get("next")
        if not next_url:
            break
        params = {}

    # Take only the most recently closed statement
    statements.sort(
        key=lambda s: s.get("end_date") or s.get("period_end") or "",
        reverse=True,
    )
    statements = statements[:1]

    all_rows: list[dict] = []
    all_skipped: list[dict] = []
    all_sync_ready: list[dict] = []
    all_blocked: list[dict] = []
    stmt_ids: list[str] = []

    for stmt in statements:
        txns = _fetch_transactions_for_statement(token, stmt)
        if not txns:
            log.warning("Statement %s: no transactions found — skipping.", stmt["id"])
            continue

        rows, skipped, sync_ready_txns, blocked = _build_payment_rows(
            stmt, txns, include_all=include_all
        )
        all_skipped.extend(skipped)
        all_sync_ready.extend(sync_ready_txns)
        all_blocked.extend(blocked)

        if not rows:
            log.warning(
                "Statement %s: all transactions missing vendor ID — skipping.", stmt["id"]
            )
            continue

        unique_vendors = len({r["vendor_id"] for r in rows})
        log.info(
            "Statement %s (%s - %s): %d transaction(s), %d vendor(s), "
            "%d skipped, %d sync_ready, %d blocked, payment date %s",
            stmt["id"],
            _format_date(stmt.get("start_date") or ""),
            _format_date(stmt.get("end_date") or ""),
            len(txns),
            unique_vendors,
            len(skipped),
            len(sync_ready_txns),
            len(blocked),
            rows[0]["payment_date"] if rows else "?",
        )
        all_rows.extend(rows)
        stmt_ids.append(stmt["id"])

    return all_rows, stmt_ids, all_skipped, all_sync_ready, all_blocked


def dump_raw_statement(client_id: str, client_secret: str) -> tuple[dict | None, list[dict]]:
    """Return the most recent closed statement and its transactions for inspection."""
    token = _get_token(client_id, client_secret)
    params: dict = {"page_size": 100}
    candidates: list[dict] = []
    next_url = None

    while True:
        if next_url:
            body = _get(token, next_url)
        else:
            body = _get(token, RAMP_STATEMENTS_URL, params=params)

        for stmt in body.get("data", []):
            if _is_closed(stmt):
                candidates.append(stmt)

        next_url = body.get("page", {}).get("next")
        if not next_url:
            break
        params = {}

    if not candidates:
        return None, []

    candidates.sort(
        key=lambda s: s.get("start_date") or s.get("period_start") or "",
        reverse=True,
    )
    stmt = candidates[0]
    txns = _fetch_transactions_for_statement(token, stmt)
    return stmt, txns
