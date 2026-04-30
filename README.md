# Volvero Email Finder 🤖 (v3.0)

A Slack-native lead generation engine. Mention the bot with any text containing companies, events or contacts, and it will extract people, enrich them with verified emails, push them into a 4-stage Brevo CRM funnel, and reply in-thread with a downloadable CSV.

> Built on top of an original architecture by Alaa Sahraoui. Refactored, hardened and extended for Volvero's outbound workflow.

---

## 📌 Overview

Volvero Email Finder transforms Slack mentions into a synchronized multi-stage CRM funnel. It combines:

- **Google Gemini** — entity extraction (people, domains, emails)
- **Snov.io** — verified B2B email lookup (by name + domain, or by domain alone)
- **Playwright** — real web scraping when Snov.io has no data
- **DuckDuckGo + Gemini** — LinkedIn profile discovery
- **Google Sheets** — funnel persistence (4 tabs)
- **Brevo** — email campaign list management + delivery/unsubscribe webhooks

---

## 🚀 Key Features

- **4-Stage Funnel** — `Waiting_Room_1 → Waiting_Room_2 → Subscribed`, with `Unsubscribed` as a permanent block list.
- **AI Extraction** — Gemini returns strict JSON with people, domains and emails.
- **Multi-layer Enrichment** — Snov.io (name + domain or domain-only) → Playwright web scraper → generic inbox patterns as last resort.
- **Brevo CRM Sync** — Verified leads are pushed to Brevo lists. AI-inferred (unverified) emails stay in the spreadsheet but are NOT synced to Brevo to avoid polluting the contact list.
- **`sent_date` Tracking** — The 27-day migration window is measured from the date the email was actually delivered (confirmed by Brevo `delivered` webhook), not from when the lead was added.
- **Monthly Migration** — APScheduler runs on the 1st of each month at 01:00. Promotes leads that have been in their stage for ≥27 days *since the email was sent*. Leads without a confirmed delivery are never promoted.
- **Brevo Webhook Integration**:
  - `delivered` → records `sent_date` in Google Sheets, starting the 27-day clock.
  - `unsubscribe` → moves the lead to `Unsubscribed`, blocking them from all future stages.
- **Thread-safe** — All Google Sheets operations and the Snov.io token cache are protected by a `threading.Lock`.
- **Deduplication** — Cross-checked against all active Sheets tabs in real time before inserting.
- **In-thread replies** — The bot replies in a thread on the original mention, keeping channels clean.
- **HMAC signature validation** — Brevo webhooks are verified before being processed.

---

## 🧰 Prerequisites

- Python 3.11+
- A Google account (for Sheets + Service Account)
- A Slack workspace where you can install custom apps
- Accounts on: Google AI Studio (Gemini), Snov.io, Brevo

---

## 🔑 Step-by-step credential setup

This section walks through every credential the bot needs, from zero. Allow ~30–45 minutes the first time.

### 1. Slack — create the App and get the tokens

1. Go to <https://api.slack.com/apps> and click **Create New App → From scratch**.
2. Name it `Volvero_Email_Finder` and pick the target workspace.
3. In the left sidebar, open **Socket Mode** and turn it **on**.
   - Generate an **App-Level Token** with the scope `connections:write`.
   - Copy this token → this is your `SLACK_APP_TOKEN` (starts with `xapp-...`).
4. Open **OAuth & Permissions** and under **Bot Token Scopes** add:
   - `app_mentions:read` — receive mentions
   - `chat:write` — post messages
   - `files:write` — upload the CSV with leads
   - `channels:history`, `groups:history`, `im:history`, `mpim:history` — read thread context
5. Open **Event Subscriptions** and turn it **on**. Under **Subscribe to bot events** add:
   - `app_mention`
6. Open **Install App** → **Install to Workspace** → authorize.
   - Copy the **Bot User OAuth Token** → this is your `SLACK_BOT_TOKEN` (starts with `xoxb-...`).
7. (Optional) Open **Basic Information → Display Information** and set the app name, short description, long description, icon and background color.
8. Invite the bot into the channels where it should listen: `/invite @Volvero_Email_Finder`.

### 2. Google Cloud — Service Account for Sheets API

1. Go to <https://console.cloud.google.com/> and create a new project (or pick an existing one).
2. Open **APIs & Services → Library** and enable:
   - **Google Sheets API**
   - **Google Drive API**
3. Open **APIs & Services → Credentials → Create credentials → Service account**.
   - Give it a name like `volvero-email-finder-bot`.
   - Skip the optional steps and finish.
4. Click the new service account → **Keys → Add key → Create new key → JSON**. A `.json` file is downloaded.
5. Rename it to `google_credentials.json` and place it at the **project root** (next to the `Slack_finder_python/` folder).
6. Open the downloaded JSON and copy the `client_email` value (looks like `xxx@xxx.iam.gserviceaccount.com`).
7. Open the target Google Spreadsheet and **Share** it with that `client_email`, giving it **Editor** rights.
8. Inside the spreadsheet, create four tabs with these exact names: `Waiting_Room_1`, `Waiting_Room_2`, `Subscribed`, `Unsubscribed`.
   - Each tab must have the header row described in [Google Sheets Structure](#-google-sheets-structure).

> ⚠️ The `google_credentials.json` file is in `.gitignore`. **Never** commit it. For cloud deploys, paste its full contents into the `GOOGLE_CREDENTIALS_JSON` environment variable instead — the bot reads from the env var first and falls back to the file if empty.

### 3. Gemini (Google AI Studio)

1. Go to <https://aistudio.google.com/apikey>.
2. Click **Create API key** and select your Cloud project.
3. Copy it → this is your `GEMINI_API_KEY`.
4. The default model is `gemini-3-flash-preview`. You can override it with `GEMINI_MODEL`.

### 4. Snov.io — Client Credentials

1. Sign up at <https://app.snov.io/> (the free tier gives you trial credits).
2. Go to **Profile → API**.
3. Click **Create new app**, fill in any name.
4. Copy the **Client ID** and **Client Secret** → these are your `SNOVIO_CLIENT_ID` and `SNOVIO_CLIENT_SECRET`.

### 5. Brevo — API key, lists and webhook

1. Sign up / log in at <https://app.brevo.com/>.
2. **API key**: Top-right profile menu → **SMTP & API → API Keys → Generate new API key**. Copy it → `BREVO_API_KEY`.
3. **Contact lists**: Go to **Contacts → Lists → Create new list**. Create three lists:
   - `Waiting Room 1` (Month 1) → copy its numeric ID into `BREVO_LIST_ID_WR1`
   - `Waiting Room 2` (Month 2) → `BREVO_LIST_ID_WR2`
   - `Subscribed` (Month 3+) → `BREVO_LIST_ID_SUBSCRIBED`
   - The ID appears in the URL when you open the list (e.g. `/contact/list/61` → ID is `61`).
4. **Webhook**: **Settings → Webhooks → Add a new webhook**.
   - URL: `http://your-public-server:5000/brevo-webhook`
   - Events: enable **delivered** and **unsubscribe**.
   - Add a custom secret string (any random value) — paste the same value into `BREVO_WEBHOOK_SECRET`. The bot verifies every webhook against this secret (plain-token mode or HMAC-SHA256).

> 💡 For local development you can expose port 5000 with `ngrok http 5000` and point Brevo at the temporary HTTPS URL.

---

## 📦 Installation

```bash
git clone https://github.com/FormosoCrl/Slack_Finder.git
cd Slack_Finder/Slack_finder_python

python -m venv .venv
.venv\Scripts\activate     # Windows
# source .venv/bin/activate # macOS / Linux

pip install -r requirements.txt
playwright install chromium
```

Place `google_credentials.json` at the repo root (one level above `Slack_finder_python/`) and create the `.env` file inside `Slack_finder_python/` (see template below).

Run the bot:

```bash
python bot.py
```

You should see:

```
⚡️ Volvero Email Finder starting...
✅ Connection with Google Sheets established.
🕐 Monthly scheduler started (1st of each month, 01:00 AM).
🤖 Volvero Email Finder connected and listening for mentions.
```

---

## 📝 `.env` template

Create `Slack_finder_python/.env` with this exact structure (replace every `your-...` placeholder with the values you collected above):

```env
# --- SLACK & AI ---
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3-flash-preview

# --- LEAD GENERATION (SNOV.IO) ---
SNOVIO_CLIENT_ID=your-snovio-client-id
SNOVIO_CLIENT_SECRET=your-snovio-client-secret

# --- GENERAL CONFIGURATION ---
MY_COMPANY=volvero.com
GOOGLE_SHEET_NAME=Slack bot automatization ( emails )

# --- BREVO (Stage-based sync) ---
BREVO_API_KEY=your-brevo-api-key

# List ID where new leads enter (Month 1)
BREVO_LIST_ID_WR1=61

# List ID for Month 2
BREVO_LIST_ID_WR2=62

# Main / Master list ID (Month 3+)
BREVO_LIST_ID_SUBSCRIBED=63

# --- CLOUD DEPLOY (Optional but recommended for production) ---
# Paste the entire contents of google_credentials.json here as a single line.
# When set, the bot uses this instead of the local file. Required for serverless / containerized deploys.
GOOGLE_CREDENTIALS_JSON=

# --- BREVO WEBHOOK SIGNATURE ---
# Any random string. Must match the value configured in Brevo's webhook settings.
BREVO_WEBHOOK_SECRET=your-random-webhook-secret
```

> The `.env` file is in `.gitignore`. Never commit it.

---

## 🧠 How It Works

### Lead Intake (triggered by Slack mention)
1. User mentions the bot with text containing leads.
2. Gemini extracts people, domains and emails from the message into strict JSON.
3. For each **person**: Snov.io searches by name + domain. If not found, AI infers `firstname@domain`, verifies it via Snov.io, and searches LinkedIn via DuckDuckGo + Gemini.
4. For each **domain**: Snov.io fetches up to 4 contacts. If empty, Playwright scrapes the homepage. Last resort: generic inbox patterns (`info@`, `sales@`, `contact@`, `support@`).
5. Leads are deduplicated against all Sheets tabs and inserted into `Waiting_Room_1`.
6. Verified leads are synced to Brevo (WR1 list). Unverified AI-inferred emails are kept in the sheet but skipped from Brevo.
7. A CSV summary is uploaded **in-thread** to the Slack mention.

### Email Delivery Tracking
- Team sends an email campaign manually from Brevo.
- Brevo fires a `delivered` webhook for each recipient.
- Bot writes `sent_date` (column H) in Google Sheets.
- The 27-day migration clock starts from this date.

### Monthly Migration (1st of each month, 01:00 AM)
- `Waiting_Room_2 → Subscribed` runs first (reverse cascade prevents collisions).
- `Waiting_Room_1 → Waiting_Room_2` runs second.
- Only leads with `sent_date` set AND ≥27 days elapsed are promoted.
- Migrated leads have their `sent_date` reset (the new stage starts a new clock).
- Migrated leads are synced to the corresponding Brevo list.

### Unsubscribe Handling
- Brevo fires an `unsubscribe` webhook.
- Bot moves the lead to `Unsubscribed`, permanently blocking future funnel stages.

---

## 📂 Project Structure

```
Slack_Finder/
├── README.md
├── google_credentials.json       # (Local, gitignored) Google Service Account key
└── Slack_finder_python/
    ├── .env                      # (Local, gitignored) API Keys
    ├── bot.py                    # Main orchestrator — funnel, webhooks, scheduler
    ├── requirements.txt          # Python dependencies (UTF-8)
    ├── funnel_bot.log            # Runtime log file
    └── src/
        ├── __init__.py
        ├── page_scraper.py       # Playwright email extractor + false-positive filter
        └── url_utils.py          # Domain normalizer (strips protocol, www, paths)
```

---

## 📊 Google Sheets Structure

Each tab (`Waiting_Room_1`, `Waiting_Room_2`, `Subscribed`, `Unsubscribed`) must share this exact column layout:

| A     | B    | C    | D              | E      | F          | G        | H         |
|-------|------|------|----------------|--------|------------|----------|-----------|
| email | name | role | company_domain | source | added_date | linkedin | sent_date |

> `sent_date` (column H) is filled automatically when Brevo confirms delivery. Migration never runs for rows where this column is empty.

---

## 🔌 Brevo Webhook Setup

In **Brevo → Settings → Webhooks**, configure a webhook pointing to `http://your-server:5000/brevo-webhook` with these events:

- ✅ **Delivered** — starts the 27-day migration clock
- ✅ **Unsubscribed** — moves lead to Unsubscribed

The webhook secret is validated against `BREVO_WEBHOOK_SECRET`. Both plain-token mode and HMAC-SHA256 are supported.

---

## ☁️ Deploying to a server

For any environment without a persistent filesystem (Render, Railway, Fly.io, Heroku, Docker, etc.):

1. Set every variable from the `.env` template as an environment variable.
2. **Critical:** open `google_credentials.json`, copy the entire contents, and paste them as a single line into `GOOGLE_CREDENTIALS_JSON`. Do NOT upload the file.
3. Expose port 5000 publicly (for the Brevo webhook).
4. Update the Brevo webhook URL to your public server URL.

The bot loads `GOOGLE_CREDENTIALS_JSON` first; if empty, it falls back to `google_credentials.json` on disk.

---

## 🚧 Roadmap

### ✅ Completed (v3.0)
- [x] 4-stage funnel with Google Sheets persistence
- [x] Brevo CRM integration with manual send flow
- [x] `sent_date` tracking via Brevo `delivered` webhook
- [x] Migration gated on actual email delivery (not add date)
- [x] Playwright web scraper with false-positive filtering
- [x] Thread-safe operations throughout
- [x] HMAC-SHA256 webhook signature validation
- [x] In-thread Slack replies
- [x] Brevo sync gated on email verification status

### 🔜 Next Steps
- [ ] Lead Scoring: AI-driven qualification based on role relevance
- [ ] Dashboard: UI to monitor funnel stage counts and credit consumption
- [ ] Multi-channel: Extend intake beyond Slack (email forwarding, web form, etc.)

---

## 👨‍💻 Credits
Refactored and enhanced by FormosoCrl for Volvero.
Based on the original architecture by Alaa Sahraoui.
