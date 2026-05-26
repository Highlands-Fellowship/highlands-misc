"""
Ramp → Sage 50 card-transaction export.

Usage:
  python main.py                          # normal run
  python main.py --dry-run                # build CSV, skip email + state
  python main.py --dump-raw               # print raw JSON for first SYNC_READY transaction
  python main.py --date-from 2026-01-01   # pull from a specific date (ignores state)
  python main.py --limit 1                # cap export at N transactions
  python main.py --limit 1 --mark-synced  # full test: email + mark synced in Ramp
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

import ramp_client
import sage_formatter
import emailer

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "exported_ids.json"
LOG_FILE = BASE_DIR / "logs" / f"run_{date.today():%Y%m%d}.log"
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
    parser.add_argument("--merchant", metavar="NAME", help="filter --dump-raw by merchant name (substring)")
    parser.add_argument("--date-from", metavar="YYYY-MM-DD")
    parser.add_argument("--limit", metavar="N", type=int, help="cap export at N transactions (for test imports)")
    parser.add_argument("--mark-synced", action="store_true", help="mark exported transactions as synced in Ramp after emailing")
    args = parser.parse_args()

    _setup_logging(args.dry_run)
    log = logging.getLogger(__name__)

    client_id = _require_env("RAMP_CLIENT_ID")
    client_secret = _require_env("RAMP_CLIENT_SECRET")

    # --dump-raw: print first matching SYNC_READY transaction and exit
    if args.dump_raw:
        import pprint
        tx, full_body = ramp_client.dump_raw_transaction(client_id, client_secret, merchant=args.merchant)
        if tx is None:
            hint = f" matching '{args.merchant}'" if args.merchant else ""
            print(f"No SYNC_READY transaction found{hint}.")
        else:
            print("=== Transaction (raw) ===")
            pprint.pprint(tx)
            print("\n=== accounting_field_selections (top-level) ===")
            for sel in tx.get("accounting_field_selections") or []:
                pprint.pprint(sel)
            print("\n=== line_items ===")
            for i, item in enumerate(tx.get("line_items") or [], 1):
                print(f"  -- line item {i} --")
                pprint.pprint(item)
        return

    gmail_user = _require_env("GMAIL_USER")
    gmail_pass = _require_env("GMAIL_APP_PASSWORD")
    notify_email = _require_env("NOTIFY_EMAIL")

    exported_ids = _load_exported_ids() if not args.date_from else set()

    log.info("Fetching sync-ready transactions from Ramp...")
    rows, skipped = ramp_client.fetch_sync_ready_transactions(
        client_id,
        client_secret,
        skip_ids=exported_ids,
        from_date=args.date_from,
    )

    if skipped:
        log.warning("%d transaction(s) skipped due to missing fields (see above).", len(skipped))

    if not rows:
        log.info("Nothing to do — no new sync-ready transactions.")
        return

    if args.limit:
        # Keep only the first N unique transactions (all their line items)
        seen_ids: list[str] = []
        for row in rows:
            if row["id"] not in seen_ids:
                seen_ids.append(row["id"])
            if len(seen_ids) >= args.limit:
                break
        rows = [r for r in rows if r["id"] in seen_ids]
        log.info("--limit %d: export capped at %d transaction(s).", args.limit, len(seen_ids))

    unique_txns = len({row["id"] for row in rows})
    log.info(
        "%d SYNC_READY transaction(s) passed validation -> %d distribution row(s). "
        "Check above for any SKIPPED warnings.",
        unique_txns, len(rows),
    )

    csv_data = sage_formatter.build_csv(rows)
    today = date.today()
    csv_filename = f"sage_card_transactions_{today:%Y%m%d}.csv"
    csv_path = OUTPUT_DIR / csv_filename
    # Use newline="" so Python doesn't translate \r\n → \r\r\n on Windows
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write(csv_data)
    log.info("CSV written to %s", csv_path)

    if args.dry_run:
        log.info("[dry-run] Skipping email, state update, and sync.")
        return

    subject = f"Ramp Card Transactions Ready for Sage 50 — {unique_txns} transaction(s) ({today:%B %d, %Y})"

    skipped_section = ""
    if skipped:
        lines = [f"\n\nWARNING: {len(skipped)} transaction(s) were skipped and are NOT in the attached file."]
        lines.append("Fix these in Ramp and they will be included in the next export:\n")
        for s in skipped:
            lines.append(f"  {s['date']}  {s['merchant']}")
            for reason in s["reasons"]:
                lines.append(f"    - {reason}")
        skipped_section = "\n".join(lines)

    body = (
        f"{unique_txns} card transaction(s) are ready to import into Sage 50."
        f"{skipped_section}\n\n"
        f"Import into Sage 50 via:\n"
        f"  File > Select Import/Export > Accounts Payable > Purchases Journal > Import\n\n"
        f"Generated: {today:%Y-%m-%d}"
    )

    log.info("Sending email to %s...", notify_email)
    emailer.send_csv(
        gmail_user=gmail_user,
        gmail_app_password=gmail_pass,
        to_address=notify_email,
        subject=subject,
        body=body,
        csv_data=csv_data,
        filename=csv_filename,
    )
    log.info("Email sent.")

    new_ids = exported_ids | {row["id"] for row in rows}
    _save_exported_ids(new_ids)
    log.info("State file updated. %d total exported IDs tracked.", len(new_ids))

    if args.mark_synced:
        tx_ids = list({row["id"] for row in rows})
        log.info("Marking %d transaction(s) as synced in Ramp...", len(tx_ids))
        ramp_client.mark_synced(client_id, client_secret, tx_ids)


if __name__ == "__main__":
    main()
