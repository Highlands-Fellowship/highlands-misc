"""
Ramp API client for reimbursements.

Each SYNC_READY reimbursement expands into a single journal entry, dated
payment_processed_at (n+1 rows for n line_items):

  Row 1..n  — debit each expense G/L account (one per line_item)
  Row n+1   — credit bank/cash account (total amount, negative)

No separate expense-date entry or clearing account — approved by CFO in
favor of the simpler single-entry model (see git history for the prior
two-entry/clearing-account design and the trade-offs discussed).

Run  python reimburse.py --dump-raw  to inspect raw JSON before going live.

Key field locations (confirmed from live API):
  - GL Account:    line_items[].accounting_field_selections[type=GL_ACCOUNT].external_code
  - Amount:        line_items[].amount.amount / minor_unit_conversion_rate
  - Payment date:  payment_processed_at (fallback: accounting_date, transaction_date)
  - Employee:      user_full_name
"""

import datetime
import logging
import requests

RAMP_TOKEN_URL = "https://api.ramp.com/developer/v1/token"
RAMP_REIMBURSEMENTS_URL = "https://api.ramp.com/developer/v1/reimbursements"
RAMP_SYNCS_URL = "https://api.ramp.com/developer/v1/accounting/syncs"


def _get_token(client_id: str, client_secret: str, write: bool = False) -> str:
    scope = "reimbursements:read accounting:write" if write else "reimbursements:read"
    resp = requests.post(
        RAMP_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": scope},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def mark_synced(client_id: str, client_secret: str, reimbursement_ids: list[str]) -> None:
    """Mark a list of reimbursement IDs as synced in Ramp via the /accounting/syncs endpoint."""
    import uuid
    log = logging.getLogger(__name__)
    token = _get_token(client_id, client_secret, write=True)
    resp = requests.post(
        RAMP_SYNCS_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "idempotency_key": str(uuid.uuid4()),
            "sync_type": "REIMBURSEMENT_SYNC",
            "successful_syncs": [
                {"id": rid, "reference_id": rid}
                for rid in reimbursement_ids
            ],
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ramp sync API {resp.status_code}\n"
            f"body: {resp.text}"
        )
    log.info("Marked %d reimbursement(s) as synced in Ramp.", len(reimbursement_ids))


def _get(token: str, params: dict, url: str = RAMP_REIMBURSEMENTS_URL) -> dict:
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params if url == RAMP_REIMBURSEMENTS_URL else None,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ramp API {resp.status_code} on GET /reimbursements\n"
            f"url:  {resp.url}\n"
            f"body: {resp.text}"
        )
    return resp.json()


def _clean_text(s: str) -> str:
    """Replace newlines and carriage returns with a space.

    Sage 50's CSV importer does not handle embedded newlines in quoted fields —
    it treats them as record separators and fails with a Date parse error.
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


def _gl_account(item: dict) -> str:
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


def _validate(reimb: dict) -> list[str]:
    errors = []
    line_items = reimb.get("line_items") or []
    if not line_items:
        errors.append("no line items")
        return errors
    for i, item in enumerate(line_items, 1):
        if not _gl_account(item):
            errors.append(f"line item {i} missing G/L Account (set Category/GL Account in Ramp)")
    return errors


def _expand_reimbursement(reimb: dict) -> list[dict]:
    """
    Return one journal entry per reimbursement (n+1 rows for n line items),
    dated payment_processed_at:
      - n debit rows  (expense GL accounts, one per line_item)
      - 1 credit row  (bank account)

    gl_account is None on the credit row — the formatter substitutes the
    env-configured bank account code at write time.
    """
    reimb_id = reimb["id"]
    employee_name = _clean_text(reimb.get("user_full_name") or "")
    memo = _clean_text(reimb.get("memo") or "")

    # "Employee Name - memo", same pattern as card transactions (ramp_client.py)
    # — the memo alone is often generic/unhelpful on its own.
    if employee_name:
        description = f"{employee_name} - {memo}" if memo else employee_name
    else:
        description = memo or "Ramp Reimbursement"

    raw_date = (
        reimb.get("payment_processed_at")
        or reimb.get("accounting_date")
        or reimb.get("transaction_date")
        or reimb.get("created_at")
        or ""
    )
    date_str = _format_date(raw_date)

    line_items = reimb.get("line_items") or []
    total_amount = sum(_line_item_amount(item) for item in line_items)
    num_dist = len(line_items) + 1  # n expense debits + 1 bank credit

    rows = []

    for item in line_items:
        rows.append({
            "id": reimb_id,
            "date": date_str,
            "description": description,
            "gl_account": _gl_account(item),
            "amount": _line_item_amount(item),
            "num_distributions": num_dist,
            "row_role": "debit",
        })

    rows.append({
        "id": reimb_id,
        "date": date_str,
        "description": description,
        "gl_account": None,          # Bank account — filled by formatter
        "amount": -total_amount,
        "num_distributions": num_dist,
        "row_role": "credit",
    })

    return rows


def fetch_sync_ready_reimbursements(
    client_id: str,
    client_secret: str,
    skip_ids: set[str],
    from_date: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Pull all SYNC_READY reimbursements and expand into journal rows.
    Returns (rows, skipped).
    """
    log = logging.getLogger(__name__)
    token = _get_token(client_id, client_secret)

    params: dict = {"page_size": 100}
    if from_date:
        if len(from_date) == 10:
            from_date = from_date + "T00:00:00Z"
        params["from_date"] = from_date

    raw_reimbs: list[dict] = []
    next_url = None
    while True:
        body = _get(token, params, url=next_url or RAMP_REIMBURSEMENTS_URL)
        for reimb in body.get("data", []):
            if reimb.get("sync_status") != "SYNC_READY":
                continue
            if reimb["id"] in skip_ids:
                continue
            raw_reimbs.append(reimb)

        next_url = body.get("page", {}).get("next")
        if not next_url:
            break
        params = {}

    raw_reimbs.sort(
        key=lambda r: (
            r.get("accounting_date")
            or r.get("transaction_date")
            or r.get("created_at")
            or ""
        )
    )

    rows: list[dict] = []
    skipped: list[dict] = []

    for reimb in raw_reimbs:
        errors = _validate(reimb)
        if errors:
            name = (reimb.get("user_full_name") or "unknown").strip()
            raw_date = (
                reimb.get("accounting_date")
                or reimb.get("transaction_date")
                or reimb.get("created_at")
                or ""
            )
            date_str = _format_date(raw_date)
            log.warning(
                "SKIPPED %s  %s  %s -- %s",
                reimb["id"], name, date_str, "; ".join(errors),
            )
            skipped.append({
                "merchant": name,
                "date": date_str,
                "reasons": errors,
                "ramp_url": f"https://app.ramp.com/details/list/reimbursement/{reimb['id']}/review",
            })
            continue

        rows.extend(_expand_reimbursement(reimb))

    return rows, skipped


def dump_raw_reimbursement(
    client_id: str,
    client_secret: str,
    employee: str | None = None,
    any_status: bool = False,
) -> tuple[dict | None, dict]:
    """Return the oldest matching reimbursement for inspection.

    By default only returns SYNC_READY reimbursements (same filter as the export).
    Pass any_status=True to include already-synced reimbursements — useful for
    re-running an export after reimbursements have already been marked synced.
    """
    token = _get_token(client_id, client_secret)
    params: dict = {"page_size": 100}
    next_url = None
    candidates: list[dict] = []
    last_body: dict = {}

    while True:
        last_body = _get(token, params, url=next_url or RAMP_REIMBURSEMENTS_URL)
        for reimb in last_body.get("data", []):
            if not any_status and reimb.get("sync_status") != "SYNC_READY":
                continue
            if employee:
                full_name = (reimb.get("user_full_name") or "").lower()
                if employee.lower() not in full_name:
                    continue
            candidates.append(reimb)

        next_url = last_body.get("page", {}).get("next")
        if not next_url:
            break
        params = {}

    if not candidates:
        return None, last_body

    candidates.sort(
        key=lambda r: (
            r.get("accounting_date")
            or r.get("transaction_date")
            or r.get("created_at")
            or ""
        )
    )
    return candidates[0], last_body
