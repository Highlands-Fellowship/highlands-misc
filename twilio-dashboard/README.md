# Twilio SMS Dashboard — Rock RMS HTML Block

An HTML/Lava block for Rock RMS 18.2 that displays Twilio SMS statistics pulled
directly from the Twilio REST API. Shows aggregate KPIs and a per-phone-number
breakdown for 30, 60, or 90-day windows, plus a flagged-message table for
identifying outbound messages with unusually high segment counts or cost.

---

## Features

- **Aggregate KPIs** — Messages Sent, Messages Received, Segments Used, Total Cost
- **Per-number cards** — Sent, Received, Segments, and Cost for each active Twilio number with a proportional activity bar
- **Messages to Review** — table of outbound messages that hit configurable segment/cost thresholds, sorted worst-first
- **30 / 60 / 90-day tabs** — switch date ranges without a page reload
- **Hide Inactive toggle** — inactive numbers (zero activity) are hidden by default; one click reveals them
- **GSAP animations** — count-up KPIs, staggered card entrance, animated progress bars, spinning loader
- **No proxy required** — Twilio's REST API supports browser `fetch()` with Basic Auth; credentials are injected server-side via Lava
- **Tabler icons** throughout (`ti ti-*`), consistent with Rock RMS's current direction

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Rock RMS 18.2+ | Uses `SystemPhoneNumber` table (added in Rock 16) |
| Twilio Communication Transport | Active in Admin Tools → Communications → Transport |
| HTML Content block with Lava + SQL enabled | See setup below |

---

## Setup

### Step 1 — Add the block to a Rock page

1. Create a new internal page or use an existing admin page.
2. Add an **HTML Content** block to the page zone.
3. Open **Block Settings**:
   - Check **Enable Lava**.
   - Under **Advanced Settings → Enabled Lava Commands**, ensure `sql` is listed (leave blank to allow all commands).
4. Paste the entire contents of `block.lava` into the HTML editor and save.

> **Security:** Restrict this page to roles that should see SMS billing data (e.g., Finance, Communications Admin). Twilio credentials are rendered server-side into the page HTML, so anyone who can open DevTools on the page can read them.

### Step 2 — Verify Twilio credentials in Rock

1. Go to **Admin Tools → Communications → Transport**.
2. Confirm a Twilio transport is active with **SID** and **Token** values saved.
3. The Lava block reads these directly — no additional configuration is needed.

### Step 3 — Label your phone numbers

1. Go to **Admin Tools → Communications → System Phone Numbers**.
2. Set a **Name** on each Twilio number (e.g., `Main Campus`, `Missions Line`).
3. Numbers without a name fall back to displaying the raw E.164 number.

> **Important:** Only numbers listed in System Phone Numbers appear in the dashboard. If a number is missing, add it there and mark it active. Also verify the **Number** field is in E.164 format (`+1XXXXXXXXXX`) — this must match exactly what Twilio has on record.

---

## Dashboard Sections

### KPI Row

| KPI | What it counts |
|---|---|
| Messages Sent | Outbound messages from your Twilio numbers |
| Messages Received | Inbound messages to your Twilio numbers |
| Segments Used | Outbound SMS segments (each 160-char block is 1 segment) |
| Total Cost | Sum of outbound message costs including A2P carrier surcharges |

> **Why does cost differ from the Twilio console?** The Twilio console widget typically excludes A2P 10DLC carrier surcharges from its cost display. This dashboard sums the `price` field on each message, which includes those surcharges. The dashboard total will be slightly higher than the console widget but matches your actual invoice.

### By Phone Number

Each card shows Sent, Received, Segments, and Cost for one System Phone Number. Cards are sorted by outbound activity (most active first). The colored bar at the top of each card is proportional to that number's share of total activity.

Numbers with zero sent and zero received are considered inactive and hidden by default. Use the **Show All** button (top right of the tab bar) to reveal them.

### Messages to Review

A table of individual outbound messages that exceeded either threshold:

| Threshold | Default | What it catches |
|---|---|---|
| Segments | 3+ | Messages using 3 or more SMS segments (~320+ characters) |
| Cost | $0.10+ | Any single message costing more than 10 cents |

Columns:
- **Number** — The System Phone Number name + raw number (e.g., `Main Campus` / `+15551234567`)
- **Dir** — Always `Sent` (only outbound messages are flagged)
- **Date** — Date the message was sent
- **Recipient / Sender** — The recipient's phone number
- **Segs** — Segment count badge (yellow = 3–4, red = 5+)
- **Cost** — Cost of that individual message
- **Message Preview** — First 70 characters of the body; hover for full text

If no messages hit either threshold for the selected period, a green confirmation message appears instead.

To change the thresholds, edit these two constants near the top of the `<script>` block in `block.lava`:

```javascript
var SEGMENT_FLAG = 3;    // flag outbound messages with this many segments or more
var COST_FLAG    = 0.10; // flag messages costing this much or more (USD)
```

---

## How It Works

```
Page load — server-side (Lava)
  ├── SQL  →  read SID + Token from Communication Transport AttributeValues
  ├── SQL  →  read active numbers from SystemPhoneNumber (IsActive = 1)
  └── Inject window._tdCfg = { sid, tok, numbers[] } into the page

Page load — client-side (JavaScript)
  └── For each number, two parallel Twilio API requests:
        GET /Accounts/{SID}/Messages.json?From={number}&DateSent>=…&DateSent<=…
        GET /Accounts/{SID}/Messages.json?To={number}&DateSent>=…&DateSent<=…
      Results are deduplicated by message SID, then split by Twilio's
      direction field (outbound-api/outbound-reply → Sent, inbound → Received).
      Aggregate KPIs = sum of all per-number stats.
      Flagged messages = outbound messages exceeding SEGMENT_FLAG or COST_FLAG.
```

Using Twilio's `direction` field (rather than inferring direction from the API
filter used) ensures messages are labeled correctly even when a System Phone
Number entry does not correspond to a Twilio-owned number.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Credentials not found" error | Verify the Twilio Communication Transport is active in Admin Tools → Communications |
| HTTP 401 from Twilio | SID or Token is wrong — check the Transport settings in Rock |
| HTTP 403 from Twilio | Account permission issue — verify sub-account access at twilio.com/console |
| No per-number cards appear | Check that SystemPhoneNumber rows exist with IsActive = 1 |
| A number shows 0 Sent and 0 Received | Confirm the Number field is in E.164 format (`+1XXXXXXXXXX`) and matches Twilio exactly |
| A non-Twilio number appears in the list | Remove or deactivate it in Admin Tools → Communications → System Phone Numbers |
| 90-day tab is slow | High message volumes trigger API pagination — this is expected; each page is 1,000 messages |
| Dashboard cost is higher than Twilio console | Expected — this dashboard includes A2P carrier surcharges; the Twilio console widget excludes them |
