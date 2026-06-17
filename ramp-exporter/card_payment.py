"""
Ramp → Sage 50 card-statement payment export.

Fetches the most recent paid Ramp statement and produces a Sage 50 Payments
Journal CSV that clears the open AP invoices created by the card transaction
Purchases Journal import (main.py).

Behavior:
  - If any transactions are missing a Vendor ID, sends a warning-only email
    (no CSV) and exits.  Fix the Accounting Vendor field in Ramp and the next
    run will check again.
  - If any statement transactions have not yet been exported via the card
    transaction export (main.py), sends a warning-only email and exits.
    This ensures card transactions are in Sage 50 as AP invoices before the
    payment CSV is imported to clear them.
  - Once all checks pass, sends the CSV and records the statement ID in
    exported_statement_ids.json so subsequent daily runs skip it.
  - --include-all bypasses all three checks (recovery use only).

Usage:
  python card_payment.py            # normal run
  python card_payment.py --dry-run  # build CSV / log what would happen, skip email
  python card_payment.py --dump-raw # print raw JSON for most recent paid statement
"""

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import statement_client
import card_payment_formatter
import emailer
import email_template

BASE_DIR = Path(__file__).parent
_STATE_DIR = Path(os.getenv("STATE_DIR", BASE_DIR))
LOG_FILE = BASE_DIR / "logs" / f"run_card_payment_{date.today():%Y%m%d}.log"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
STATE_FILE = _STATE_DIR / "exported_statement_ids.json"
_PURCHASES_STATE_FILE = _STATE_DIR / "exported_ids.json"


def _setup_logging(dry_run: bool) -> None:
    LOG_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    console = logging.StreamHandler(sys.stdout)
    console.stream.reconfigure(encoding="utf-8", errors="replace")
    handlers = [console]
    if not dry_run:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        sys.exit(f"ERROR: {name} is not set in .env")
    return val


def _load_exported_purchase_ids() -> set[str]:
    if not _PURCHASES_STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(_PURCHASES_STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        log = logging.getLogger(__name__)
        log.warning("Could not read %s — treating all transactions as unexported.", _PURCHASES_STATE_FILE)
        return set()


def _load_sent_id() -> str | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("sent_statement_id")
    except Exception:
        return None


def _save_sent_id(stmt_id: str) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"sent_statement_id": stmt_id}, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dump-raw", action="store_true")
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="include transactions missing a Vendor ID using merchant name as fallback; "
             "also bypasses the not-yet-exported check and the already-sent check "
             "(one-time recovery use)",
    )
    args = parser.parse_args()

    _setup_logging(args.dry_run)
    log = logging.getLogger(__name__)

    client_id = _require_env("RAMP_CLIENT_ID")
    client_secret = _require_env("RAMP_CLIENT_SECRET")

    if args.dump_raw:
        import pprint
        stmt, txns = statement_client.dump_raw_statement(client_id, client_secret)
        if stmt is None:
            print("No paid statement found.")
            return
        print("=== Statement (raw) ===")
        pprint.pprint(stmt)
        print(f"\n=== Transactions in period ({len(txns)} total) ===")
        if txns:
            print("-- First transaction --")
            pprint.pprint(txns[0])
            print("\n-- accounting_field_selections (top-level) --")
            for sel in txns[0].get("accounting_field_selections") or []:
                pprint.pprint(sel)
        return

    gmail_user = _require_env("GMAIL_USER")
    gmail_pass = _require_env("GMAIL_APP_PASSWORD")
    notify_email = [e.strip() for e in _require_env("NOTIFY_EMAIL").split(",") if e.strip()]

    log.info("Fetching most recent paid statement from Ramp...")
    payment_rows, stmt_ids, skipped = statement_client.fetch_paid_statements(
        client_id,
        client_secret,
        include_all=args.include_all,
    )

    if not stmt_ids:
        log.info("Nothing to do — no closed statements found.")
        return

    stmt_id = stmt_ids[0]
    today = date.today()

    # ── Hold until all transactions have a Vendor ID ──────────────────────────
    if skipped and not args.include_all:
        log.warning(
            "%d transaction(s) missing Vendor ID — holding export until resolved.",
            len(skipped),
        )
        if args.dry_run:
            log.info("[dry-run] Would send action-required email (no CSV).")
            return

        subject = (
            f"Ramp Card Payments — Action Required: "
            f"{len(skipped)} transaction(s) need Vendor ID ({today:%B %d, %Y})"
        )
        html_body, plain_body = email_template.build_card_payment_pending_email(
            count_skipped=len(skipped),
            gen_date=f"{today:%Y-%m-%d}",
            skipped=skipped,
        )
        log.info("Sending action-required email to %s...", notify_email)
        emailer.send_csv(
            gmail_user=gmail_user,
            gmail_app_password=gmail_pass,
            to_address=notify_email,
            subject=subject,
            body_plain=plain_body,
            body_html=html_body,
        )
        log.info("Email sent.")
        return

    # ── Hold until all statement transactions appear in the Purchases Journal export ──
    if not args.include_all:
        exported_purchase_ids = _load_exported_purchase_ids()
        stmt_tx_ids = {r["tx_id"] for r in payment_rows}
        not_yet_exported = stmt_tx_ids - exported_purchase_ids
        if not_yet_exported:
            not_exported_items = [
                {
                    "merchant": r["vendor_name"] or r["vendor_id"],
                    "date": r["payment_date"],
                    "amount": r["amount"],
                    "id": r["tx_id"],
                    "reasons": [
                        "not yet in Purchases Journal export — "
                        "card transactions must be exported and imported into Sage 50 first"
                    ],
                    "ramp_url": f"https://app.ramp.com/details/list/transactions/{r['tx_id']}",
                }
                for r in payment_rows
                if r["tx_id"] not in exported_purchase_ids
            ]
            log.warning(
                "%d transaction(s) in statement not yet in card transaction export — holding payment CSV.",
                len(not_exported_items),
            )
            if args.dry_run:
                log.info("[dry-run] Would send not-exported warning email (no CSV).")
                return

            subject = (
                f"Ramp Card Payments — Export On Hold: "
                f"{len(not_exported_items)} transaction(s) not yet in Purchases Journal "
                f"({today:%B %d, %Y})"
            )
            html_body, plain_body = email_template.build_card_payment_not_exported_email(
                count=len(not_exported_items),
                gen_date=f"{today:%Y-%m-%d}",
                items=not_exported_items,
            )
            log.info("Sending not-exported warning email to %s...", notify_email)
            emailer.send_csv(
                gmail_user=gmail_user,
                gmail_app_password=gmail_pass,
                to_address=notify_email,
                subject=subject,
                body_plain=plain_body,
                body_html=html_body,
            )
            log.info("Email sent.")
            return

    # ── Skip if this statement was already exported ───────────────────────────
    if not args.include_all:
        sent_id = _load_sent_id()
        if sent_id == stmt_id:
            log.info("Statement %s already exported — nothing to do.", stmt_id)
            return

    # ── Build and send CSV ────────────────────────────────────────────────────
    unique_vendors = len({r["vendor_id"] for r in payment_rows})
    unique_invoices = len({r["invoice_number"] for r in payment_rows})
    log.info(
        "%d statement(s) → %d invoice(s) across %d vendor(s).",
        len(stmt_ids),
        unique_invoices,
        unique_vendors,
    )

    csv_data = card_payment_formatter.build_csv(payment_rows)
    csv_filename = f"sage_card_payments_{today:%Y%m%d}.csv"
    csv_path = OUTPUT_DIR / csv_filename
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write(csv_data)
    log.info("CSV written to %s", csv_path)

    if args.dry_run:
        log.info("[dry-run] Skipping email and state update.")
        return

    subject = (
        f"Ramp Card Payments Ready for Sage 50 — "
        f"{unique_invoices} invoice(s) / {len(stmt_ids)} statement(s) ({today:%B %d, %Y})"
    )
    html_body, plain_body = email_template.build_card_payment_email(
        count=unique_invoices,
        gen_date=f"{today:%Y-%m-%d}",
        skipped=skipped,
    )

    log.info("Sending email to %s...", notify_email)
    emailer.send_csv(
        gmail_user=gmail_user,
        gmail_app_password=gmail_pass,
        to_address=notify_email,
        subject=subject,
        body_plain=plain_body,
        csv_data=csv_data,
        filename=csv_filename,
        body_html=html_body,
    )
    log.info("Email sent.")

    _save_sent_id(stmt_id)
    log.info("Recorded statement %s as exported.", stmt_id)


if __name__ == "__main__":
    main()
