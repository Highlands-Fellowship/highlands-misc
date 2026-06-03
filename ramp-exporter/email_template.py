"""
Branded HTML email templates for Ramp export notifications.
Colors and assets from the Highlands Fellowship brand kit.
"""

# Brand colors
NAVY = "#0d1d41"
TEAL = "#56b6b2"
YELLOW = "#f4b334"
CREAM = "#f2dab2"
LIGHT_GRAY = "#E3E3E3"
MID_GRAY = "#8A8A8A"
NEAR_BLACK = "#1A1A1A"

LOGO_URL = (
    "https://res.cloudinary.com/hfchurch/image/upload"
    "/h_96/Brand%20Guide%20Logos/hf-logo-mark-light.png"
)
WORDMARK_URL = (
    "https://res.cloudinary.com/hfchurch/image/upload"
    "/h_48/Brand%20Guide%20Logos/hf-wordmark-light.png"
)

_BASE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Roboto+Slab:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  body {{ margin:0; padding:0; background:{light_gray}; font-family:'Roboto Slab',Georgia,serif; color:{near_black}; }}
  a {{ color:{teal}; }}
</style>
</head>
<body>
<table width="100%" cellpadding="0" cellspacing="0" role="presentation">

  <!-- Header -->
  <tr><td style="background:{navy}; padding:20px 32px;">
    <table cellpadding="0" cellspacing="0" role="presentation">
      <tr>
        <td style="padding-right:16px; vertical-align:middle;">
          <img src="{logo_url}" height="48" alt="Highlands Fellowship" style="display:block;">
        </td>
        <td style="vertical-align:middle; border-left:1px solid rgba(255,255,255,0.2); padding-left:16px;">
          <img src="{wordmark_url}" height="24" alt="Highlands Fellowship" style="display:block;">
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- Teal accent bar -->
  <tr><td style="background:{teal}; height:4px; font-size:0; line-height:0;">&nbsp;</td></tr>

  <!-- Body card -->
  <tr><td style="padding:24px 16px;">
    <table width="600" align="center" cellpadding="0" cellspacing="0" role="presentation"
           style="background:#ffffff; border-radius:6px; overflow:hidden;
                  box-shadow:0 1px 4px rgba(0,0,0,0.08);">
      <tr><td style="padding:32px 36px;">

        <h2 style="margin:0 0 8px; font-size:20px; font-weight:700; color:{navy};">{heading}</h2>
        <p style="margin:0 0 24px; font-size:15px; line-height:1.6; color:{near_black};">{intro}</p>

        {import_box}

        {skipped_box}

        <p style="margin:24px 0 0; font-size:12px; color:{mid_gray};">Generated: {gen_date}</p>

      </td></tr>
    </table>
  </td></tr>

  <!-- Footer -->
  <tr><td style="padding:0 16px 24px;">
    <table width="600" align="center" cellpadding="0" cellspacing="0" role="presentation">
      <tr><td style="padding:16px 0; text-align:center; border-top:2px solid {teal};">
        <p style="margin:0; font-size:11px; color:{mid_gray}; font-family:'Roboto Slab',Georgia,serif;">
          Highlands Fellowship &bull; Automated Accounting Export
        </p>
      </td></tr>
    </table>
  </td></tr>

</table>
</body>
</html>"""

_IMPORT_BOX = """
<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="margin-bottom:20px;">
  <tr>
    <td style="background:{cream}; border-left:4px solid {teal}; border-radius:4px;
               padding:14px 18px;">
      <p style="margin:0 0 6px; font-size:12px; font-weight:700; color:{navy};
                text-transform:uppercase; letter-spacing:0.5px;">Import path in Sage 50</p>
      <p style="margin:0; font-size:14px; color:{near_black}; line-height:1.5;">{import_path}</p>
    </td>
  </tr>
</table>"""

_SKIPPED_BOX = """
<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="margin-bottom:20px;">
  <tr>
    <td style="background:#fff8e6; border-left:4px solid {yellow}; border-radius:4px;
               padding:14px 18px;">
      <p style="margin:0 0 8px; font-size:12px; font-weight:700; color:{navy};
                text-transform:uppercase; letter-spacing:0.5px;">
        &#9888;&nbsp; {count} Transaction(s) Skipped &mdash; Action Required
      </p>
      <p style="margin:0 0 10px; font-size:13px; color:{near_black};">
        These were <strong>not included</strong> in the attached file.
        Fix the missing fields in Ramp and they will be picked up on the next export.
      </p>
      {rows}
    </td>
  </tr>
</table>"""

_SKIPPED_ROW = (
    '<p style="margin:4px 0; font-size:13px; color:{near_black};">'
    '<strong>{date}&nbsp;&nbsp;{merchant}</strong>'
    '&nbsp;&nbsp;<a href="{ramp_url}" style="font-size:11px; color:{teal}; '
    'text-decoration:none;">Fix in Ramp &#8250;</a><br>'
    '<span style="color:{mid_gray}; font-size:12px;">{reasons}</span></p>'
)


def build_card_email(
    count: int,
    gen_date: str,
    skipped: list[dict],
) -> tuple[str, str]:
    """Return (html, plain_text) for a card transaction export notification."""
    import_path = (
        "File &rsaquo; Select Import/Export &rsaquo; "
        "Accounts Payable &rsaquo; Purchases Journal &rsaquo; Import"
    )
    return _build(
        heading="Card Transactions Ready for Import",
        intro=f"{count} card transaction(s) are ready to import into Sage 50.",
        import_path=import_path,
        gen_date=gen_date,
        skipped=skipped,
    )


def build_billpay_email(
    count: int,
    gen_date: str,
    skipped: list[dict],
) -> tuple[str, str]:
    """Return (html, plain_text) for a bill payment export notification."""
    import_path = (
        "1. <strong>sage_bill_purchases_*.csv</strong> &rsaquo; "
        "File &rsaquo; Select Import/Export &rsaquo; Accounts Payable &rsaquo; Purchases Journal &rsaquo; Import"
        "<br>2. <strong>sage_bill_payments_*.csv</strong> &rsaquo; "
        "File &rsaquo; Select Import/Export &rsaquo; Accounts Payable &rsaquo; Payments Journal &rsaquo; Import"
    )
    return _build(
        heading="Bill Payments Ready for Import",
        intro=f"{count} bill payment(s) are ready to import into Sage 50. Two files are attached.",
        import_path=import_path,
        gen_date=gen_date,
        skipped=skipped,
    )


def build_card_payment_email(
    count: int,
    gen_date: str,
    skipped: list[dict],
) -> tuple[str, str]:
    """Return (html, plain_text) for a card statement payment export notification."""
    import_path = (
        "File &rsaquo; Select Import/Export &rsaquo; "
        "Accounts Payable &rsaquo; Payments Journal &rsaquo; Import"
    )
    return _build(
        heading="Card Payments Ready for Import",
        intro=f"{count} card invoice payment(s) are ready to import into Sage 50.",
        import_path=import_path,
        gen_date=gen_date,
        skipped=skipped,
    )


def build_reimbursement_email(
    count: int,
    gen_date: str,
    skipped: list[dict],
) -> tuple[str, str]:
    """Return (html, plain_text) for a reimbursement export notification."""
    import_path = (
        "File &rsaquo; Select Import/Export &rsaquo; "
        "General Ledger &rsaquo; General Journal &rsaquo; Import"
    )
    return _build(
        heading="Reimbursements Ready for Import",
        intro=f"{count} reimbursement(s) are ready to import into Sage 50.",
        import_path=import_path,
        gen_date=gen_date,
        skipped=skipped,
    )


def _build(
    heading: str,
    intro: str,
    import_path: str,
    gen_date: str,
    skipped: list[dict],
) -> tuple[str, str]:
    fmt = dict(
        navy=NAVY, teal=TEAL, yellow=YELLOW, cream=CREAM,
        light_gray=LIGHT_GRAY, mid_gray=MID_GRAY, near_black=NEAR_BLACK,
        logo_url=LOGO_URL, wordmark_url=WORDMARK_URL,
    )

    import_box = _IMPORT_BOX.format(import_path=import_path, **fmt)

    if skipped:
        rows_html = "".join(
            _SKIPPED_ROW.format(
                date=s["date"],
                merchant=s["merchant"],
                reasons=" &bull; ".join(s["reasons"]),
                ramp_url=s.get("ramp_url", ""),
                **fmt,
            )
            for s in skipped
        )
        skipped_box = _SKIPPED_BOX.format(count=len(skipped), rows=rows_html, **fmt)
    else:
        skipped_box = ""

    html = _BASE.format(
        heading=heading,
        intro=intro,
        import_box=import_box,
        skipped_box=skipped_box,
        gen_date=gen_date,
        **fmt,
    )

    # Plain-text fallback
    plain = f"{intro}\n\nImport path: {import_path.replace('&rsaquo;', '>').replace('&nbsp;', ' ')}\n"
    if skipped:
        plain += f"\nWARNING: {len(skipped)} transaction(s) skipped:\n"
        for s in skipped:
            plain += f"  {s['date']}  {s['merchant']}\n"
            if s.get("ramp_url"):
                plain += f"    {s['ramp_url']}\n"
            for r in s["reasons"]:
                plain += f"    - {r}\n"
    plain += f"\nGenerated: {gen_date}"

    return html, plain
