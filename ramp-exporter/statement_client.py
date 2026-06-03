"""
Ramp API client for card statements.

Fetches paid statements and re-derives the card-transaction invoice numbers
(using the same formula as ramp_client.py) so the Payments Journal CSV can
reference them and clear the open AP invoices created by the Purchases Journal
import.

Run  python card_payment.py --dump-raw  to inspect raw JSON before going live.

Key field locations (verify with --dump-raw against a live statement):
  - Statement ID:     statement["id"]
  - Payment status:   statement["payment_status"]  ("PAID" / "UNPAID" / "OVERDUE")
  - Period start:     statement["period_start"]     (ISO datetime)
  - Period end:       statement["period_end"]        (ISO datetime)
  - Payment date:     statement["paid_at"]           (fallback: due_date)
  - Statement total:  statement["total_due"]["amount"] / minor_unit_conversion_rate

Transactions are fetched separately via the /transactions endpoint filtered
by the statement's period (from_date / to_date).  If the Ramp API exposes a
direct statement→transactions relationship (e.g. a statement_id filter or an
embedded list), switch to that — it will be visible in --dump-raw output.
"""

import datetime
import logging
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


def _vendor_id(tx: dict) -> str:
    """Vendor ID from top-level accounting_field_selections (type MERCHANT)."""
    for sel in tx.get("accounting_field_selections") or []:
        if sel.get("type") == "MERCHANT":
            return (sel.get("external_id") or "").strip()
    return ""


def _vendor_name(tx: dict) -> str:
    return (tx.get("merchant_name") or "").strip()


def _tx_amount(tx: dict) -> float:
    amt = tx.get("amount", 0)
    if isinstance(amt, dict):
        return amt.get("amount", 0) / amt.get("minor_unit_conversion_rate", 100)
    return float(amt)


def _invoice_number(tx: dict) -> str:
    """Regenerate the same invoice number that main.py / sage_formatter would produce."""
    vendor = _vendor_id(tx)
    raw_date = tx.get("accounting_date") or tx.get("user_transaction_time") or ""
    date_str = _format_date(raw_date)
    m, d, y = (date_str.split("/") + ["", "", ""])[:3]
    date_compact = f"{m}{d}{y[2:]}"
    short_id = tx["id"][-3:]
    return f"{vendor[:9]}.{date_compact}.{short_id}"


def _statement_payment_date(stmt: dict) -> str:
    """Return the statement payment date, falling back to due_date."""
    raw = (
        stmt.get("paid_at")
        or stmt.get("payment_date")
        or stmt.get("due_date")
        or ""
    )
    return _format_date(raw)


def _statement_check_number(stmt: dict) -> str:
    """Return a short, stable check-number for the statement payment."""
    sid = stmt.get("id") or ""
    # Use last 20 chars of the statement ID to fit Sage 50's Check Number field
    return sid[-20:] if sid else ""


def _fetch_transactions_for_statement(token: str, stmt: dict) -> list[dict]:
    """
    Fetch all card transactions that fall within the statement's period.

    Tries the date-range approach (from_date / to_date) which is reliable
    across API versions.  If the statements API embeds a 'transactions' list
    or exposes a statement_id filter, switch to that instead — verify with
    --dump-raw.
    """
    period_start = stmt.get("period_start") or stmt.get("start_date") or ""
    period_end = stmt.get("period_end") or stmt.get("end_date") or ""

    if not period_start:
        return []

    # Normalise to full ISO datetime strings as Ramp requires
    if len(period_start) == 10:
        period_start = period_start + "T00:00:00Z"
    if period_end and len(period_end) == 10:
        period_end = period_end + "T23:59:59Z"

    params: dict = {"page_size": 100, "from_date": period_start}
    if period_end:
        params["to_date"] = period_end

    txns: list[dict] = []
    next_url = None

    while True:
        if next_url:
            body = _get(token, next_url)
        else:
            body = _get(token, RAMP_TRANSACTIONS_URL, params=params)

        txns.extend(body.get("data", []))
        next_url = body.get("page", {}).get("next")
        if not next_url:
            break

    return txns


def _build_payment_rows(stmt: dict, txns: list[dict]) -> list[dict]:
    """
    Convert a statement + its transactions into Payments Journal row dicts,
    grouped by vendor.  One logical "check" per vendor, N rows (one per invoice).
    """
    payment_date = _statement_payment_date(stmt)
    check_number = _statement_check_number(stmt)
    period = stmt.get("period_start") or ""
    # Human-readable memo: "Ramp Card Payment May 2026"
    try:
        dt = datetime.datetime.fromisoformat(period.replace("Z", "+00:00"))
        memo = f"Ramp Card Payment {dt.strftime('%B %Y')}"
    except Exception:
        memo = "Ramp Card Payment"

    # Build per-invoice records keyed by vendor
    by_vendor: dict[str, list[dict]] = {}
    for tx in txns:
        vid = _vendor_id(tx)
        if not vid:
            continue  # no vendor ID → can't apply payment to an invoice
        by_vendor.setdefault(vid, []).append(tx)

    rows = []
    for vid, vendor_txns in by_vendor.items():
        # Sort transactions within vendor by date for consistent ordering
        vendor_txns.sort(
            key=lambda t: t.get("accounting_date") or t.get("user_transaction_time") or ""
        )
        invoices = [
            {
                "invoice_number": _invoice_number(t),
                "amount": _tx_amount(t),
                "vendor_name": _vendor_name(t),
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
            })

    return rows


def fetch_paid_statements(
    client_id: str,
    client_secret: str,
    skip_ids: set[str],
) -> tuple[list[dict], list[str]]:
    """
    Fetch paid statements not yet exported, expand into Payments Journal rows.
    Returns (payment_rows, statement_ids).

    payment_rows — passed to card_payment_formatter.build_csv()
    statement_ids — IDs of statements included, for state-file tracking
    """
    log = logging.getLogger(__name__)
    token = _get_token(client_id, client_secret)

    # Collect all paid statements not already exported
    statements: list[dict] = []
    next_url = None
    params: dict = {"page_size": 100}

    while True:
        if next_url:
            body = _get(token, next_url)
        else:
            body = _get(token, RAMP_STATEMENTS_URL, params=params)

        for stmt in body.get("data", []):
            # Filter: must be paid and not already exported
            status = (stmt.get("payment_status") or stmt.get("status") or "").upper()
            if status != "PAID":
                continue
            if stmt["id"] in skip_ids:
                continue
            statements.append(stmt)

        next_url = body.get("page", {}).get("next")
        if not next_url:
            break
        params = {}

    # Sort oldest-first by period start
    statements.sort(key=lambda s: s.get("period_start") or s.get("start_date") or "")

    all_rows: list[dict] = []
    stmt_ids: list[str] = []

    for stmt in statements:
        txns = _fetch_transactions_for_statement(token, stmt)
        if not txns:
            log.warning("Statement %s has no transactions — skipping.", stmt["id"])
            continue

        rows = _build_payment_rows(stmt, txns)
        if not rows:
            log.warning(
                "Statement %s: all transactions missing vendor ID — skipping.", stmt["id"]
            )
            continue

        unique_txns = len(txns)
        unique_vendors = len({r["vendor_id"] for r in rows})
        log.info(
            "Statement %s: %d transaction(s), %d vendor(s), payment date %s",
            stmt["id"],
            unique_txns,
            unique_vendors,
            rows[0]["payment_date"] if rows else "?",
        )
        all_rows.extend(rows)
        stmt_ids.append(stmt["id"])

    return all_rows, stmt_ids


def dump_raw_statement(client_id: str, client_secret: str) -> tuple[dict | None, list[dict]]:
    """
    Return the most recent paid statement and its transactions for inspection.
    Used by  card_payment.py --dump-raw.
    """
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
            status = (stmt.get("payment_status") or stmt.get("status") or "").upper()
            if status == "PAID":
                candidates.append(stmt)

        next_url = body.get("page", {}).get("next")
        if not next_url:
            break
        params = {}

    if not candidates:
        return None, []

    # Return the most recent paid statement
    candidates.sort(
        key=lambda s: s.get("period_start") or s.get("start_date") or "",
        reverse=True,
    )
    stmt = candidates[0]
    txns = _fetch_transactions_for_statement(token, stmt)
    return stmt, txns
