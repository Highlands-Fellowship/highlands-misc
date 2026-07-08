"""
Ramp -> Sage 50 bill pay export.

Fetches NOT_SYNCED bills that are either PAYMENT_COMPLETED, or PAYMENT_PROCESSING
and paid by check (funds already left the bank), and produces a Sage 50
vendor-invoice CSV (same format as card transactions). See
billpay_client._is_exportable_status() for the exact rule.

Usage:
  python billpay.py                                  # normal run
  python billpay.py --dry-run                        # build CSV, skip email + state
  python billpay.py --dump-raw                       # print raw JSON for first matching bill
  python billpay.py --dump-raw --vendor "Verizon"    # filter --dump-raw by vendor name
  python billpay.py --dump-raw --vendor "Verizon" --any-status  # any sync/payment status; lists all matches
  python billpay.py --dump-raw --bill-id ID          # inspect one specific bill, bypassing all filters
  python billpay.py --date-from 2026-01-01           # pull from a specific date (ignores state)
  python billpay.py --limit 1                        # cap export at N bills (for test imports)
  python billpay.py --mark-synced-ids ID1 ID2 ...    # mark specific IDs synced without re-exporting
  python billpay.py --reexport-ids ID1 ID2 ...       # re-export specific bill IDs regardless of sync status
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

import billpay_client
import sage_formatter
import billpay_payment_formatter
import emailer
import email_template

BASE_DIR = Path(__file__).parent
_STATE_DIR = Path(os.getenv("STATE_DIR", BASE_DIR))
STATE_FILE = _STATE_DIR / "exported_bill_ids.json"
LOG_FILE = BASE_DIR / "logs" / f"billpay_{date.today():%Y%m%d}.log"
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
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
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
    parser.add_argument("--vendor", metavar="NAME", help="filter --dump-raw by vendor name (substring)")
    parser.add_argument("--any-status", action="store_true", help="with --dump-raw: bypass sync_status and status_summary filters (inspect bills in any state)")
    parser.add_argument("--bill-id", metavar="ID", help="with --dump-raw: fetch one specific bill by ID, bypassing all filters")
    parser.add_argument("--date-from", metavar="YYYY-MM-DD")
    parser.add_argument("--limit", metavar="N", type=int, help="cap export at N bills")
    parser.add_argument("--mark-synced", action="store_true", help="mark exported bills and payments as synced in Ramp after emailing")
    parser.add_argument("--mark-synced-ids", metavar="ID", nargs="+", help="mark specific bill IDs as synced (runs BILL_SYNC + BILL_PAYMENT_SYNC) without re-exporting")
    parser.add_argument("--reexport-ids", metavar="ID", nargs="+", help="re-export specific bill IDs regardless of state file or sync status")
    args = parser.parse_args()

    _setup_logging(args.dry_run)
    log = logging.getLogger(__name__)

    client_id = _require_env("RAMP_CLIENT_ID")
    client_secret = _require_env("RAMP_CLIENT_SECRET")

    if args.mark_synced_ids:
        log.info("Marking %d bill(s) as synced in Ramp...", len(args.mark_synced_ids))
        billpay_client.mark_synced(client_id, client_secret, args.mark_synced_ids)
        return

    if args.reexport_ids:
        gmail_user = _require_env("GMAIL_USER")
        gmail_pass = _require_env("GMAIL_APP_PASSWORD")
        notify_email = [e.strip() for e in _require_env("NOTIFY_EMAIL").split(",") if e.strip()]

        log.info("Re-fetching %d specific bill(s) by ID...", len(args.reexport_ids))
        purchase_rows, payment_rows, skipped = billpay_client.fetch_bills_by_ids(
            client_id, client_secret, args.reexport_ids
        )
        if skipped:
            log.warning("%d bill(s) skipped due to missing fields.", len(skipped))
        if not purchase_rows:
            log.info("No valid rows produced — check IDs and Ramp field setup.")
            return

        unique_bills = len({row["id"] for row in purchase_rows})
        today = date.today()

        purchase_csv = sage_formatter.build_csv(purchase_rows)
        purchase_filename = f"sage_bill_purchases_reexport_{today:%Y%m%d}.csv"
        purchase_path = OUTPUT_DIR / purchase_filename
        with open(purchase_path, "w", newline="", encoding="utf-8") as f:
            f.write(purchase_csv)
        log.info("Purchases CSV written to %s", purchase_path)

        payment_csv = billpay_payment_formatter.build_csv(payment_rows)
        payment_filename = f"sage_bill_payments_reexport_{today:%Y%m%d}.csv"
        payment_path = OUTPUT_DIR / payment_filename
        with open(payment_path, "w", newline="", encoding="utf-8") as f:
            f.write(payment_csv)
        log.info("Payments CSV written to %s", payment_path)

        if args.dry_run:
            log.info("[dry-run] Skipping email.")
            return

        subject = f"Ramp Bill Payments Re-export -- {unique_bills} bill(s) ({today:%B %d, %Y})"
        html_body, plain_body = email_template.build_billpay_email(
            count=unique_bills,
            gen_date=f"{today:%Y-%m-%d}",
            skipped=skipped,
        )
        emailer.send_csv(
            gmail_user=gmail_user,
            gmail_app_password=gmail_pass,
            to_address=notify_email,
            subject=subject,
            body_plain=plain_body,
            csv_data=purchase_csv,
            filename=purchase_filename,
            body_html=html_body,
            extra_attachments=[(payment_csv, payment_filename)],
        )
        log.info("Re-export email sent.")
        return

    if args.dump_raw:
        import pprint

        if args.bill_id:
            bill = billpay_client.dump_raw_bill_by_id(client_id, client_secret, args.bill_id)
            if bill is None:
                print(f"No bill found with ID '{args.bill_id}'.")
                return
            candidates = [bill]
        else:
            bill, candidates = billpay_client.dump_raw_bill(
                client_id, client_secret, vendor=args.vendor, any_status=args.any_status
            )
            if bill is None:
                hint = f" matching '{args.vendor}'" if args.vendor else ""
                status_hint = (
                    " (any sync/payment status)" if args.any_status
                    else " (NOT_SYNCED + PAYMENT_COMPLETED only — try --any-status for other states)"
                )
                print(f"No bill found{hint}{status_hint}.")
                return

        if len(candidates) > 1:
            print(f"=== {len(candidates)} matching bill(s) — showing full detail for the oldest ===")
            for b in candidates:
                raw_date = b.get("accounting_date") or b.get("paid_at") or b.get("issued_at") or ""
                payment = b.get("payment") or {}
                print(
                    f"  {b['id']}  {raw_date[:10]}  status_summary={b.get('status_summary')}  "
                    f"payment_method={payment.get('payment_method')}  amount={b.get('amount', {}).get('amount', 0) / 100:.2f}"
                )
            print(f"\nRe-run with --bill-id <ID> to inspect a specific one.\n")

        print("=== Bill (raw) ===")
        pprint.pprint(bill)
        print("\n=== accounting_field_selections (top-level) ===")
        for sel in bill.get("accounting_field_selections") or []:
            pprint.pprint(sel)
        print("\n=== line_items ===")
        for i, item in enumerate(bill.get("line_items") or [], 1):
            print(f"  -- line item {i} --")
            pprint.pprint(item)
        print("\n=== vendor ===")
        pprint.pprint(bill.get("vendor"))
        print("\n=== payment ===")
        pprint.pprint(bill.get("payment"))
        return

    gmail_user = _require_env("GMAIL_USER")
    gmail_pass = _require_env("GMAIL_APP_PASSWORD")
    notify_email = [e.strip() for e in _require_env("NOTIFY_EMAIL").split(",") if e.strip()]

    exported_ids = _load_exported_ids() if not args.date_from else set()

    log.info("Fetching completed bills from Ramp...")
    purchase_rows, payment_rows, skipped = billpay_client.fetch_completed_bills(
        client_id,
        client_secret,
        skip_ids=exported_ids,
        from_date=args.date_from,
    )

    if skipped:
        log.warning("%d bill(s) skipped due to missing fields (see above).", len(skipped))

    if not purchase_rows:
        log.info("Nothing to do -- no new completed bills.")
        return

    unique_bills = len({row["id"] for row in purchase_rows})

    if args.limit:
        seen: list[str] = []
        for row in purchase_rows:
            if row["id"] not in seen:
                seen.append(row["id"])
            if len(seen) >= args.limit:
                break
        purchase_rows = [r for r in purchase_rows if r["id"] in seen]
        payment_rows = [r for r in payment_rows if r["id"] in seen]
        unique_bills = len(seen)
        log.info("--limit %d: export capped at %d bill(s).", args.limit, unique_bills)

    log.info(
        "%d completed bill(s) -> %d purchase distribution row(s), %d payment row(s).",
        unique_bills, len(purchase_rows), len(payment_rows),
    )

    today = date.today()

    purchase_csv = sage_formatter.build_csv(purchase_rows)
    purchase_filename = f"sage_bill_purchases_{today:%Y%m%d}.csv"
    purchase_path = OUTPUT_DIR / purchase_filename
    with open(purchase_path, "w", newline="", encoding="utf-8") as f:
        f.write(purchase_csv)
    log.info("Purchases CSV written to %s", purchase_path)

    payment_csv = billpay_payment_formatter.build_csv(payment_rows)
    payment_filename = f"sage_bill_payments_{today:%Y%m%d}.csv"
    payment_path = OUTPUT_DIR / payment_filename
    with open(payment_path, "w", newline="", encoding="utf-8") as f:
        f.write(payment_csv)
    log.info("Payments CSV written to %s", payment_path)

    if args.dry_run:
        log.info("[dry-run] Skipping email, state update, and sync.")
        return

    subject = f"Ramp Bill Payments Ready for Sage 50 -- {unique_bills} bill(s) ({today:%B %d, %Y})"

    html_body, plain_body = email_template.build_billpay_email(
        count=unique_bills,
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
        csv_data=purchase_csv,
        filename=purchase_filename,
        body_html=html_body,
        extra_attachments=[(payment_csv, payment_filename)],
    )
    log.info("Email sent.")

    new_ids = exported_ids | {row["id"] for row in purchase_rows}
    _save_exported_ids(new_ids)
    log.info("State file updated. %d total exported IDs tracked.", len(new_ids))

    if args.mark_synced:
        bill_ids = list({row["id"] for row in purchase_rows})
        log.info("Marking %d bill(s) as synced in Ramp...", len(bill_ids))
        billpay_client.mark_synced(client_id, client_secret, bill_ids)


if __name__ == "__main__":
    main()
