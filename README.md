# Slack Lead Finder 🤖 (v3.0)

This repository is a high-performance refactor of the original work by Alaa Sahraoui, now featuring a full 4-stage funnel automation pipeline with Google Sheets, Brevo CRM, Snov.io enrichment, and Playwright web scraping.

---

## 📌 Overview
Slack_Finder is an autonomous B2B lead generation engine. It transforms Slack mentions into a synchronized multi-stage CRM funnel. By combining Google Gemini (AI), Snov.io, Playwright, and Brevo, the bot extracts, enriches, deduplicates and manages leads across their entire lifecycle — from first contact to active outreach.

---

## 🚀 Key Features (v3.0)

- **4-Stage Funnel:** Leads flow automatically through `Waiting_Room_1 → Waiting_Room_2 → Subscribed`, with `Unsubscribed` as a permanent block list.
- **AI Extraction:** Gemini analyzes Slack messages and returns structured JSON with people, domains, and emails.
- **Multi-layer Enrichment:** Snov.io (by name or domain) → Playwright web scraper → generic inbox patterns as fallback.
- **Brevo CRM Sync:** Leads are pushed to Brevo lists automatically. Email campaigns are sent manually by the team.
- **sent_date Tracking:** The 27-day migration window is measured from the date the email was actually delivered (confirmed by Brevo webhook), not from when the lead was added.
- **Monthly Migration:** APScheduler runs on the 1st of each month, promoting leads that have been in their current stage for ≥27 days since the email was sent. Leads without a confirmed send are never promoted.
- **Brevo Webhook Integration:** Two events are handled:
  - `delivered` → records `sent_date` in Google Sheets, starting the 27-day clock.
  - `unsubscribe` → moves the lead to `Unsubscribed`, blocking them from all future stages.
- **Thread-safe:** All Google Sheets operations are protected by a `threading.Lock`. Snov.io token cache is also lock-protected.
- **Deduplication:** Cross-checked against all active Sheets tabs in real time before inserting.

---

## 🔑 Environment Variables

Create a `.env` file in the root directory (make sure it's in your `.gitignore`):

```
# Slack Connection
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_APP_TOKEN=xapp-your-token

# AI & Prospecting
GEMINI_API_KEY=your-key
GEMINI_MODEL=gemini-3-flash-preview
SNOVIO_CLIENT_ID=your-id
SNOVIO_CLIENT_SECRET=your-secret

# Identity & Cloud
MY_COMPANY=volvero.com
GOOGLE_SHEET_NAME=HojaCalculoPrueba

# Brevo CRM
BREVO_API_KEY=your-key
BREVO_LIST_ID_WR1=your-list-id
BREVO_LIST_ID_WR2=your-list-id
BREVO_LIST_ID_SUBSCRIBED=your-list-id
BREVO_WEBHOOK_SECRET=your-secret
```

> You also need a `google_credentials.json` file in the root directory to enable Google Sheets access.

---

## 🧠 How It Works

### Lead Intake (triggered by Slack mention)
1. User mentions the bot with text containing leads.
2. Gemini extracts people, domains, and emails from the message.
3. For each **person**: Snov.io searches by name + domain. If not found, AI infers and verifies the email, and searches LinkedIn via DuckDuckGo.
4. For each **domain**: Snov.io fetches up to 4 contacts. If empty, Playwright scrapes the website. Last resort: generic inbox patterns (`info@`, `sales@`, etc.).
5. Leads are deduplicated against all Sheets tabs and inserted into `Waiting_Room_1`.
6. Leads are synced to Brevo (WR1 list) so the team can send manual campaigns.
7. A CSV summary is uploaded to the Slack channel.

### Email Delivery Tracking
- Team sends email campaign manually from Brevo.
- Brevo fires a `delivered` webhook for each recipient.
- Bot records `sent_date` (column H) in Google Sheets for that contact.
- The 27-day migration clock starts from this date.

### Monthly Migration (1st of each month, 01:00 AM)
- `Waiting_Room_2 → Subscribed` runs first (reverse cascade to avoid collisions).
- `Waiting_Room_1 → Waiting_Room_2` runs second.
- Only leads with `sent_date` set AND ≥27 days elapsed are promoted.
- Migrated leads are synced to the corresponding Brevo list.

### Unsubscribe Handling
- Brevo fires an `unsubscribe` webhook.
- Bot moves the lead to `Unsubscribed` tab, permanently blocking future funnel stages.

---

## 📂 Project Structure

```
Slack_Finder/
├── README.md
├── google_credentials.json       # (Local) Google Cloud Key
└── Slack_finder_python/
    ├── .env                      # (Local) API Keys
    ├── bot.py                    # Main orchestrator — funnel logic, webhooks, scheduler
    ├── funnel_bot.log            # Runtime log file
    └── src/
        ├── __init__.py
        ├── page_scraper.py       # Playwright email extractor with false-positive filtering
        └── url_utils.py          # Domain normalizer (strips protocol, www, paths)
```

---

## 📊 Google Sheets Structure

Each tab (`Waiting_Room_1`, `Waiting_Room_2`, `Subscribed`, `Unsubscribed`) shares the same column layout:

| A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|
| email | name | role | company_domain | source | added_date | linkedin | sent_date |

> `sent_date` (col H) is filled automatically when Brevo confirms delivery. Migration never runs for rows where this column is empty.

---

## 🔌 Brevo Webhook Setup

In Brevo → Settings → Webhooks, configure a webhook pointing to `http://your-server:5000/brevo-webhook` with the following events enabled:

- ✅ **Entregadas** (delivered) — starts the 27-day migration clock
- ✅ **Suscripción cancelada** (unsubscribe) — moves lead to Unsubscribed

---

## 🚧 Roadmap

### ✅ Completed (v3.0)
- [x] 4-stage funnel with Google Sheets persistence.
- [x] Brevo CRM integration with manual send flow.
- [x] `sent_date` tracking via Brevo `delivered` webhook.
- [x] Migration gated on actual email delivery (not add date).
- [x] Playwright web scraper with false-positive filtering.
- [x] Thread-safe operations throughout.
- [x] HMAC-SHA256 webhook signature validation.

### 🔜 Next Steps
- [ ] Lead Scoring: AI-driven qualification based on role relevance.
- [ ] Dashboard: UI to monitor funnel stage counts and credit consumption.
- [ ] Multi-channel: Extend intake beyond Slack (email forwarding, web form, etc.).

---

## 👨‍💻 Credits
Refactored and enhanced by FormosoCrl.
Based on the original architecture by Alaa Sahraoui.
