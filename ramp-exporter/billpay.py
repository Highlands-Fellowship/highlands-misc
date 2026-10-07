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
  python billpay.py --mark-synced --to you@x.com     # full run, email only you instead of NOTIFY_EMAIL
  python billpay.py --reexport-ids ID1 ID2 ...       # re-export specific bill IDs regardless of sync status
  python billpay.py --mark-synced --reconcile        # daily task: normal run + retry any previously deferred syncs
  python billpay.py --audit                          # sweep Ramp for any bill not fully synced yet, then retry
  python billpay.py --audit --dry-run                # same, but only list findings, no retry
  python billpay.py --audit --date-from 2026-06-01   # limit the audit sweep to a date range
  python billpay.py --check-vendor-ids               # preview Vendor IDs if export switched to Ramp's External ID
  python billpay.py --check-payments                 # only run the payment-changed-after-export check
  python billpay.py --check-payments --dry-run       # same, but report only (no email, no state update)
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
PENDING_SYNC_FILE = _STATE_DIR / "pending_sync_ids.json"
PAYMENT_STATE_FILE = _STATE_DIR / "exported_bill_payments.json"
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


def _load_pending_sync_ids() -> set[str]:
    if PENDING_SYNC_FILE.exists():
        try:
            return set(json.loads(PENDING_SYNC_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_pending_sync_ids(ids: set[str]) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_SYNC_FILE.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


def _track_sync_result(attempted_ids, deferred_ids) -> None:
    """After a mark_synced() call, update pending_sync_ids.json: bills that
    were attempted but not deferred are now fully synced and get cleared;
    newly deferred bills are added so --reconcile can retry them later."""
    pending = _load_pending_sync_ids()
    pending -= (set(attempted_ids) - set(deferred_ids))
    pending |= set(deferred_ids)
    _save_pending_sync_ids(pending)


def _load_exported_payments() -> dict[str, dict]:
    """bill ID -> the payment Sage has for it: {payment_id, ref, date}, plus
    "alerted": true once we've emailed that it was cancelled, until a
    completed replacement comes through."""
    if PAYMENT_STATE_FILE.exists():
        try:
            return json.loads(PAYMENT_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_exported_payments(tracked: dict[str, dict]) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    PAYMENT_STATE_FILE.write_text(json.dumps(tracked, indent=2, sort_keys=True), encoding="utf-8")


def _record_exported_payments(payment_rows: list[dict]) -> None:
    """Remember which Ramp payment was just sent to Sage for each bill, so a
    later run can tell if Ramp cancels or replaces it."""
    tracked = _load_exported_payments()
    for row in payment_rows:
        tracked[row["id"]] = {
            "payment_id": row["payment_id"],
            "ref": row["check_number"],
            "date": row["payment_date"],
        }
    _save_exported_payments(tracked)


def _check_payment_changes(
    client_id: str,
    client_secret: str,
    log: logging.Logger,
    dry_run: bool,
    gmail_user: str,
    gmail_pass: str,
    notify_email: list[str],
) -> None:
    """Email an alert when a bill already exported to Sage has its Ramp
    payment cancelled, reversed, or replaced afterward.

    Ramp keeps such a bill marked synced, so the normal fetch never sees it
    again — e.g. a check exported in July, cancelled and reversed in
    September, then re-paid by ACH: Sage kept the voided check and never got
    the ACH. Two kinds of alert, each sent once:
      - cancelled: the payment Sage has is no longer active and nothing has
        replaced it yet -> void it in Sage.
      - replaced: a different payment has now left the bank -> void the
        original (if not already) and import the attached Payments CSV.

    Bills exported before this tracking existed are seeded with their current
    Ramp payment on first sight (no alert) — changes that happened before
    that can't be detected.
    """
    exported_ids = _load_exported_ids()
    tracked = _load_exported_payments()
    bills = {b["id"]: b for b in billpay_client.fetch_all_bills(client_id, client_secret)}

    seeded = 0
    for bill_id in exported_ids - tracked.keys():
        if bill_id in bills:
            snap = billpay_client.payment_snapshot(bills[bill_id])
            tracked[bill_id] = {"payment_id": snap["payment_id"], "ref": snap["ref"], "date": snap["date"]}
            seeded += 1
    if seeded:
        log.info("Payment check: started tracking %d previously exported bill(s).", seeded)

    replaced: list[tuple[dict, dict, dict]] = []
    cancelled: list[tuple[dict, dict, dict]] = []
    for bill_id, entry in tracked.items():
        bill = bills.get(bill_id)
        if bill is None:
            continue
        snap = billpay_client.payment_snapshot(bill)
        if snap["payment_id"] == entry["payment_id"]:
            continue
        if snap["payment_id"]:
            replaced.append((bill, entry, snap))
        elif not entry.get("alerted"):
            cancelled.append((bill, entry, snap))

    if not replaced and not cancelled:
        log.info("Payment check: no exported bill payments have changed.")
        if seeded and not dry_run:
            _save_exported_payments(tracked)
        return

    items = []
    for bill, entry, snap in replaced + cancelled:
        info = billpay_client.bill_summary(bill)
        in_sage = f"In Sage: payment {entry['ref'] or '(reference not tracked)'} dated {entry['date'] or '?'}"
        if snap["payment_id"]:
            now = f"Ramp now: replaced by payment {snap['ref']} dated {snap['date']} -- in the attached CSV"
        else:
            now = (
                f"Ramp now: no completed payment (status {snap['status'] or 'unknown'}) -- "
                "void the Sage payment; a replacement will be emailed once it completes"
            )
        log.warning(
            "Payment changed after export: %s  %s  invoice %s  $%.2f -- %s; %s",
            bill["id"], info["vendor"], info["invoice"], info["amount"], in_sage, now,
        )
        items.append({
            "date": entry["date"],
            "merchant": f"{info['vendor']} -- invoice {info['invoice']} (${info['amount']:,.2f})",
            "reasons": [in_sage, now],
            "ramp_url": f"https://app.ramp.com/bill-pay/bills/list/{bill['id']}",
        })

    today = date.today()
    attachments = []
    if replaced:
        payment_csv = billpay_payment_formatter.build_csv(
            billpay_client.build_payment_rows([bill for bill, _, _ in replaced])
        )
        payment_filename = f"sage_bill_payments_changed_{today:%Y%m%d}.csv"
        payment_path = OUTPUT_DIR / payment_filename
        with open(payment_path, "w", newline="", encoding="utf-8") as f:
            f.write(payment_csv)
        log.info("Replacement Payments CSV written to %s", payment_path)
        attachments.append((payment_csv, payment_filename))

    if dry_run:
        log.info("[dry-run] Payment check: skipping alert email and state update.")
        return

    count = len(replaced) + len(cancelled)
    html_body, plain_body = email_template.build_billpay_payment_changed_email(
        count=count,
        gen_date=f"{today:%Y-%m-%d}",
        items=items,
        has_csv=bool(replaced),
    )
    emailer.send_csv(
        gmail_user=gmail_user,
        gmail_app_password=gmail_pass,
        to_address=notify_email,
        subject=f"Ramp Bill Payments Changed After Export -- {count} bill(s) ({today:%B %d, %Y})",
        body_plain=plain_body,
        csv_data=attachments[0][0] if attachments else None,
        filename=attachments[0][1] if attachments else None,
        body_html=html_body,
    )
    log.info("Payment-changed alert sent for %d bill(s).", count)

    for bill, _, snap in replaced:
        tracked[bill["id"]] = {"payment_id": snap["payment_id"], "ref": snap["ref"], "date": snap["date"]}
    for bill, entry, snap in cancelled:
        entry["alerted"] = True
    _save_exported_payments(tracked)


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        sys.exit(f"ERROR: {name} is not set in .env")
    return val


def _reconcile(client_id: str, client_secret: str, log: logging.Logger, dry_run: bool = False) -> None:
    """Retry sync for bills a prior run deferred (e.g. checks that have since
    cleared). No re-export, no email — just the Ramp sync confirmation call."""
    pending = _load_pending_sync_ids()
    if not pending:
        log.info("Reconcile: nothing pending.")
        return
    if dry_run:
        log.info("Reconcile: [dry-run] would retry sync for %d pending bill(s): %s", len(pending), ", ".join(sorted(pending)))
        return
    log.info("Reconcile: retrying sync for %d pending bill(s): %s", len(pending), ", ".join(sorted(pending)))
    deferred = billpay_client.mark_synced(client_id, client_secret, list(pending))
    _track_sync_result(pending, deferred)
    resolved = pending - deferred
    if resolved:
        log.info("Reconcile: resolved %d bill(s): %s", len(resolved), ", ".join(sorted(resolved)))
    if deferred:
        log.warning(
            "Reconcile: %d bill(s) still not ready — still pending: %s",
            len(deferred), ", ".join(sorted(deferred)),
        )
    else:
        log.info("Reconcile: all pending bills are now fully synced.")


def _print_vendor_id_check(r: dict) -> None:
    active = r["total"] - r["inactive"]
    print(f"=== Vendor ID check: {active} active vendor(s) ({r['inactive']} inactive skipped) ===")
    print("Current = what the export sends today (linked accounting vendor ID)")
    print("New     = what it would send if it used Ramp's External ID field first\n")

    print(f"WILL CHANGE ({len(r['will_change'])}) -- External ID set and different from current value")
    print("  Verify each New value matches a Sage 50 Vendor ID exactly before switching.")
    if r["will_change"]:
        print(f"  {'Ramp name':<36} {'Current':<36} New")
        for row in r["will_change"]:
            print(f"  {row['name'][:36]:<36} {row['current'][:36]:<36} {row['proposed']}")
    else:
        print("  (none)")

    print(f"\nTOO LONG FOR SAGE ({len(r['too_long'])}) -- would export more than 20 chars after the switch")
    if r["too_long"]:
        print(f"  {'Ramp name':<36} {'Len':>4}  Value")
        for row in r["too_long"]:
            print(f"  {row['name'][:36]:<36} {len(row['value']):>4}  {row['value']}")
    else:
        print("  (none)")

    print(f"\nNO ID AT ALL ({len(r['no_id'])}) -- both blank; export falls back to the display name")
    for row in r["no_id"]:
        print(f"  {row['name']}")
    if not r["no_id"]:
        print("  (none)")

    print(f"\nUnchanged by the switch: {r['unchanged']} vendor(s)")


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
    parser.add_argument("--to", metavar="EMAIL", help="override NOTIFY_EMAIL for this run only (e.g. to test a full run without emailing the full distribution list)")
    parser.add_argument("--reconcile", action="store_true", help="also retry syncing bills deferred by a prior run (see pending_sync_ids.json) — runs alongside the normal fetch, e.g. combine with --mark-synced for the daily task")
    parser.add_argument("--audit", action="store_true", help="sweep Ramp directly for any bill that should be synced but isn't (ignores local state); combine with --date-from to limit the range")
    parser.add_argument("--check-payments", action="store_true", help="only run the check for exported bills whose Ramp payment was later cancelled, reversed, or replaced (also runs automatically on every normal run)")
    parser.add_argument("--check-vendor-ids", action="store_true", help="read-only: list vendors whose exported Vendor ID would change if the export used Ramp's External ID field, or that exceed Sage's 20-char limit (needs vendors:read scope)")
    args = parser.parse_args()

    _setup_logging(args.dry_run)
    log = logging.getLogger(__name__)

    client_id = _require_env("RAMP_CLIENT_ID")
    client_secret = _require_env("RAMP_CLIENT_SECRET")

    if args.mark_synced_ids:
        log.info("Marking %d bill(s) as synced in Ramp...", len(args.mark_synced_ids))
        deferred = billpay_client.mark_synced(client_id, client_secret, args.mark_synced_ids)
        _track_sync_result(args.mark_synced_ids, deferred)
        return

    if args.check_vendor_ids:
        _print_vendor_id_check(billpay_client.check_vendor_ids(client_id, client_secret))
        return

    if args.audit:
        log.info("Auditing Ramp for bills that should be synced but aren't...")
        found = billpay_client.find_unsynced_bills(client_id, client_secret, from_date=args.date_from)
        if not found:
            log.info("Audit: nothing found -- everything eligible is already fully synced.")
            return
        for b in found:
            log.info(
                "  %s  %-30s  %-25s  $%10.2f  status_summary=%s sync_status=%s",
                b["id"], b["vendor"][:30], b["invoice_number"][:25], b["amount"],
                b["status_summary"], b["sync_status"],
            )
        pending = _load_pending_sync_ids() | {b["id"] for b in found}
        log.info("Audit: found %d bill(s) not fully synced.", len(found))
        if args.dry_run:
            log.info("[dry-run] Skipping pending_sync_ids.json update and retry.")
            return
        _save_pending_sync_ids(pending)
        _reconcile(client_id, client_secret, log)
        return

    if args.reexport_ids:
        gmail_user = _require_env("GMAIL_USER")
        gmail_pass = _require_env("GMAIL_APP_PASSWORD")
        notify_email = [args.to] if args.to else [e.strip() for e in _require_env("NOTIFY_EMAIL").split(",") if e.strip()]

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

        purchase_csv = sage_formatter.build_csv(
            purchase_rows, ap_account=os.getenv("BILLPAY_AP_ACCOUNT", "2200")
        )
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
        _record_exported_payments(payment_rows)
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
    notify_email = [args.to] if args.to else [e.strip() for e in _require_env("NOTIFY_EMAIL").split(",") if e.strip()]

    if args.check_payments:
        _check_payment_changes(client_id, client_secret, log, args.dry_run, gmail_user, gmail_pass, notify_email)
        return

    # Runs before the normal export and never blocks it — a failure here is
    # logged, and the day's new bills still go out.
    try:
        _check_payment_changes(client_id, client_secret, log, args.dry_run, gmail_user, gmail_pass, notify_email)
    except Exception:
        log.exception("Payment check failed -- continuing with the normal export.")

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
        if args.reconcile:
            _reconcile(client_id, client_secret, log, dry_run=args.dry_run)
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

    purchase_csv = sage_formatter.build_csv(
        purchase_rows, ap_account=os.getenv("BILLPAY_AP_ACCOUNT", "2200")
    )
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
    _record_exported_payments(payment_rows)
    log.info("State file updated. %d total exported IDs tracked.", len(new_ids))

    if args.mark_synced:
        bill_ids = list({row["id"] for row in purchase_rows})
        log.info(
            "Marking %d bill(s) as synced in Ramp: %s",
            len(bill_ids), ", ".join(sorted(bill_ids)),
        )
        deferred = billpay_client.mark_synced(client_id, client_secret, bill_ids)
        _track_sync_result(bill_ids, deferred)
        if deferred:
            log.warning(
                "%d bill(s) not yet fully synced — tracked in pending_sync_ids.json, "
                "retry later with --reconcile: %s",
                len(deferred), ", ".join(sorted(deferred)),
            )

    if args.reconcile:
        _reconcile(client_id, client_secret, log)


if __name__ == "__main__":
    main()
