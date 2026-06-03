"""
Ramp → Sage 50 card-statement payment export.

Fetches paid Ramp statements, groups their transactions by vendor, and
produces a Sage 50 Payments Journal CSV that clears the open AP invoices
created by the card transaction Purchases Journal import (main.py).

Usage:
  python card_payment.py                                  # normal run
  python card_payment.py --dry-run                        # build CSV, skip email + state
  python card_payment.py --dump-raw                       # print raw JSON for most recent paid statement
  python card_payment.py --mark-synced-ids ID1 ID2 ...    # mark specific statement IDs as exported
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
STATE_FILE = BASE_DIR / "exported_statement_ids.json"
LOG_FILE = BASE_DIR / "logs" / f"run_card_payment_{date.today():%Y%m%d}.log"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))


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


def _load_exported_ids() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def _save_exported_ids(ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids), indent=2))


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        sys.exit(f"ERROR: {name} is not set in .env")
    return val


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dump-raw", action="store_true")
    parser.add_argument(
        "--mark-synced-ids",
        metavar="ID",
        nargs="+",
        help="mark specific statement IDs as exported without re-running",
    )
    args = parser.parse_args()

    _setup_logging(args.dry_run)
    log = logging.getLogger(__name__)

    client_id = _require_env("RAMP_CLIENT_ID")
    client_secret = _require_env("RAMP_CLIENT_SECRET")

    # Recovery: mark specific statement IDs as done
    if args.mark_synced_ids:
        exported = _load_exported_ids()
        exported.update(args.mark_synced_ids)
        _save_exported_ids(exported)
        log.info(
            "Marked %d statement ID(s) as exported: %s",
            len(args.mark_synced_ids),
            args.mark_synced_ids,
        )
        return

    # --dump-raw: print most recent paid statement and its transactions, then exit
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

    exported_ids = _load_exported_ids()

    log.info("Fetching paid statements from Ramp...")
    payment_rows, stmt_ids, skipped = statement_client.fetch_paid_statements(
        client_id,
        client_secret,
        skip_ids=exported_ids,
    )

    if skipped:
        log.warning(
            "%d transaction(s) skipped due to missing Vendor ID (see above).", len(skipped)
        )

    if not stmt_ids:
        log.info("Nothing to do — no new paid statements.")
        return

    unique_vendors = len({r["vendor_id"] for r in payment_rows})
    unique_invoices = len({r["invoice_number"] for r in payment_rows})
    log.info(
        "%d statement(s) → %d invoice(s) across %d vendor(s).",
        len(stmt_ids),
        unique_invoices,
        unique_vendors,
    )

    csv_data = card_payment_formatter.build_csv(payment_rows)
    today = date.today()
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

    new_ids = exported_ids | set(stmt_ids)
    _save_exported_ids(new_ids)
    log.info("State file updated. %d total exported statement IDs tracked.", len(new_ids))


if __name__ == "__main__":
    main()
