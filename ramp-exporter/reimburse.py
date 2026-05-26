"""
Ramp -> Sage 50 reimbursement export.

Usage:
  python reimburse.py                                    # normal run
  python reimburse.py --dry-run                          # build CSV, skip email + state
  python reimburse.py --dump-raw                         # print raw JSON for first SYNC_READY reimbursement
  python reimburse.py --date-from 2026-01-01             # pull from a specific date (ignores state)
  python reimburse.py --limit 1                          # cap export at N reimbursements (for test imports)
  python reimburse.py --mark-synced-ids ID1 ID2 ...      # mark specific IDs synced in Ramp without re-exporting
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

import reimbursement_client
import reimbursement_formatter
import emailer
import email_template

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "exported_reimb_ids.json"
LOG_FILE = BASE_DIR / "logs" / f"reimb_{date.today():%Y%m%d}.log"
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
    parser.add_argument("--employee", metavar="NAME", help="filter --dump-raw by employee name (substring)")
    parser.add_argument("--date-from", metavar="YYYY-MM-DD")
    parser.add_argument("--limit", metavar="N", type=int, help="cap export at N reimbursements")
    parser.add_argument("--mark-synced", action="store_true", help="mark exported reimbursements as synced in Ramp after emailing")
    parser.add_argument("--mark-synced-ids", metavar="ID", nargs="+", help="mark specific reimbursement IDs as synced without re-exporting")
    args = parser.parse_args()

    _setup_logging(args.dry_run)
    log = logging.getLogger(__name__)

    client_id = _require_env("RAMP_CLIENT_ID")
    client_secret = _require_env("RAMP_CLIENT_SECRET")

    if args.mark_synced_ids:
        log.info("Marking %d reimbursement(s) as synced in Ramp...", len(args.mark_synced_ids))
        reimbursement_client.mark_synced(client_id, client_secret, args.mark_synced_ids)
        return

    if args.dump_raw:
        import pprint
        reimb, full_body = reimbursement_client.dump_raw_reimbursement(
            client_id, client_secret, employee=args.employee
        )
        if reimb is None:
            hint = f" matching '{args.employee}'" if args.employee else ""
            print(f"No SYNC_READY reimbursement found{hint}.")
        else:
            print("=== Reimbursement (raw) ===")
            pprint.pprint(reimb)
            print("\n=== accounting_field_selections ===")
            for sel in reimb.get("accounting_field_selections") or []:
                pprint.pprint(sel)
        return

    gmail_user = _require_env("GMAIL_USER")
    gmail_pass = _require_env("GMAIL_APP_PASSWORD")
    notify_email = _require_env("NOTIFY_EMAIL")

    exported_ids = _load_exported_ids() if not args.date_from else set()

    log.info("Fetching sync-ready reimbursements from Ramp...")
    rows, skipped = reimbursement_client.fetch_sync_ready_reimbursements(
        client_id,
        client_secret,
        skip_ids=exported_ids,
        from_date=args.date_from,
    )

    if skipped:
        log.warning("%d reimbursement(s) skipped due to missing fields (see above).", len(skipped))

    if not rows:
        log.info("Nothing to do -- no new sync-ready reimbursements.")
        return

    unique_reimbs = len({row["id"] for row in rows})

    if args.limit:
        seen: list[str] = []
        for row in rows:
            if row["id"] not in seen:
                seen.append(row["id"])
            if len(seen) >= args.limit:
                break
        rows = [r for r in rows if r["id"] in seen]
        unique_reimbs = len(seen)
        log.info("--limit %d: export capped at %d reimbursement(s).", args.limit, unique_reimbs)

    log.info(
        "%d SYNC_READY reimbursement(s) -> %d journal rows.",
        unique_reimbs, len(rows),
    )

    csv_data = reimbursement_formatter.build_csv(rows)
    today = date.today()
    csv_filename = f"sage_reimbursements_{today:%Y%m%d}.csv"
    csv_path = OUTPUT_DIR / csv_filename
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write(csv_data)
    log.info("CSV written to %s", csv_path)

    if args.dry_run:
        log.info("[dry-run] Skipping email, state update.")
        return

    subject = f"Ramp Reimbursements Ready for Sage 50 -- {unique_reimbs} reimbursement(s) ({today:%B %d, %Y})"

    html_body, plain_body = email_template.build_reimbursement_email(
        count=unique_reimbs,
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

    new_ids = exported_ids | {row["id"] for row in rows}
    _save_exported_ids(new_ids)
    log.info("State file updated. %d total exported IDs tracked.", len(new_ids))

    if args.mark_synced:
        reimb_ids = list({row["id"] for row in rows})
        log.info("Marking %d reimbursement(s) as synced in Ramp...", len(reimb_ids))
        reimbursement_client.mark_synced(client_id, client_secret, reimb_ids)


if __name__ == "__main__":
    main()
