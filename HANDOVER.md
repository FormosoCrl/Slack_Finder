# 📕 Volvero Email Finder — Operations & Handover Guide

> **Purpose of this document.** This is a complete knowledge-transfer guide written for whoever inherits the *Volvero Email Finder* Slack bot. It explains, from zero, **what the bot does**, **how every piece fits together**, **which third-party services it depends on and how to obtain their keys**, **where everything is configured**, and **how to keep it running, deploy updates, and recover it if the server dies**.
>
> Read the [Executive Summary](#1-executive-summary) first, then the [Operations Runbook](#9-operations-runbook-daily-tasks) — those two sections cover 90% of day-to-day needs. The rest is reference material.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture](#2-system-architecture)
3. [The Complete Lead Flow (end to end)](#3-the-complete-lead-flow-end-to-end)
4. [Third-Party Services & API Keys — how to get each one](#4-third-party-services--api-keys--how-to-get-each-one)
5. [Environment Variables (`.env`) — full reference](#5-environment-variables-env--full-reference)
6. [Google Sheets Structure](#6-google-sheets-structure)
7. [Hosting & Infrastructure (Google Cloud VM)](#7-hosting--infrastructure-google-cloud-vm)
8. [The Two Background Services (systemd)](#8-the-two-background-services-systemd)
9. [Operations Runbook (daily tasks)](#9-operations-runbook-daily-tasks)
10. [Troubleshooting](#10-troubleshooting)
11. [Disaster Recovery — if the VM dies](#11-disaster-recovery--if-the-vm-dies)
12. [Costs, Quotas & Scaling Limits](#12-costs-quotas--scaling-limits)
13. [Project File Map](#13-project-file-map)
14. [Glossary](#14-glossary)

---

## 1. Executive Summary

**What it is:** A Slack-native lead-generation engine. A team member @-mentions the bot in Slack with any text (or screenshot) that contains companies, events, or contacts. The bot extracts the people and companies, finds their corporate emails, verifies those emails, and pushes the good ones into a **time-based email-nurturing funnel** managed in Google Sheets and Brevo (the email marketing platform).

**The funnel has 4 stages**, and leads move through them automatically, one stage per month:

```
        (new lead enters here)
              │
              ▼
   ┌──────────────────┐   +1 month   ┌──────────────────┐   +1 month   ┌──────────────┐
   │  Waiting_Room_1  │ ───────────▶ │  Waiting_Room_2  │ ───────────▶ │  Subscribed  │
   └──────────────────┘              └──────────────────┘              └──────────────┘
              │                              │                                │
              └──────────────────────────────┴────────────────────────────────┘
                                      │ (if the person clicks "unsubscribe")
                                      ▼
                              ┌──────────────────┐
                              │   Unsubscribed   │  ← permanent block list
                              └──────────────────┘
```

**Key concept — the clock starts when the email is *delivered*, not when the lead is added.** Each stage lasts ~27 days, measured from the moment Brevo confirms the marketing email was actually delivered to that person. A lead that never received an email never advances.

**Three things run continuously:**

| Component | What it does | When |
|---|---|---|
| **Slack listener** | Listens for @-mentions, runs the enrichment pipeline, replies in-thread with a CSV | Always on |
| **Webhook server** | Receives "delivered" and "unsubscribe" events from Brevo | Always on (port 5000) |
| **Monthly migration** | Promotes mature leads to the next funnel stage | 1st of each month, 01:00 |
| **Nightly verifier** | Re-checks "uncertain" emails and rescues the good ones | Every night, 03:00 |

**Where it lives:** A Google Cloud VM (Debian Linux) inside a GCP project named **`Slack bot`**. It runs as two `systemd` services so it survives reboots and crashes.

---

## 2. System Architecture

```
                          ┌─────────────────────────────────────────────┐
                          │              GOOGLE CLOUD VM                  │
                          │         (project: "Slack bot")                │
                          │                                               │
   Slack workspace        │   ┌─────────────────────────────────────┐   │
   ───── @mention ───────▶│   │  bot.py  (volvero-bot.service)       │   │
                          │   │  • Slack Socket-Mode listener        │   │
   ◀──── CSV reply ───────│   │  • Flask webhook server  :5000       │   │
                          │   │  • APScheduler monthly migration     │   │
                          │   └─────────────────────────────────────┘   │
   Brevo                  │   ┌─────────────────────────────────────┐   │
   ──── webhook ─────────▶│   │  verify_queue.py (verify-queue.timer)│   │
   (delivered/unsub)      │   │  • Runs nightly @ 03:00              │   │
                          │   │  • Re-verifies the TO_VERIFY queue   │   │
                          │   └─────────────────────────────────────┘   │
                          └───────────────┬───────────────────────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────┐
        ▼                ▼                 ▼                ▼                 ▼
  ┌───────────┐    ┌───────────┐    ┌───────────┐   ┌───────────┐    ┌────────────┐
  │  Gemini   │    │  Snov.io  │    │   Brevo   │   │  Google   │    │  Free email│
  │  (AI: text│    │ (B2B email│    │ (email    │   │  Sheets   │    │  verifiers │
  │  + vision)│    │  lookup)  │    │  campaigns│   │ (database)│    │  (nightly) │
  └───────────┘    └───────────┘    └───────────┘   └───────────┘    └────────────┘
```

**Why Google Sheets as the database?** It's free, the team can read/edit leads by hand, and it needs no separate hosting. The four funnel stages are four tabs in one spreadsheet.

**Why Brevo?** It's the email-marketing platform where the team actually composes and sends the campaigns. The bot only *syncs contacts into Brevo lists* and *listens for delivery/unsubscribe events* — it does **not** send emails itself.

---

## 3. The Complete Lead Flow (end to end)

### Step 1 — Intake (a Slack @-mention)

A user mentions the bot: `@Volvero_Email_Finder here are the speakers at the fintech summit: [text or screenshot]`.

1. The Slack listener fires `handle_app_mention` and processes the request in a background thread (so Slack doesn't time out).
2. **Gemini** reads the message text **and any attached images** (screenshots, business cards, slides — multimodal vision). It returns strict JSON with three sections:
   - **A — People** (name + role + company)
   - **B — Domains** (companies with no specific person)
   - **C — Emails** (addresses already written in the message)

### Step 2 — Enrichment (finding the emails)

For each **person** found:
- If the company is a bare brand name with no domain (e.g. "FundingLoop"), the bot resolves it to a real domain (`fundingloop.ch`) using **DuckDuckGo search + Gemini**.
- **Snov.io** is queried by *name + domain* to find the verified corporate email.
- If Snov.io has nothing, the bot lets Gemini *guess* a likely address (`firstname@domain`). Because a guess is unproven, it is **not trusted yet** → it goes to the **TO_VERIFY queue** (see Step 4).

For each **domain** found (no specific person):
- **Snov.io** fetches up to 4 contacts at that company.
- If empty, **Playwright** scrapes the company homepage for published addresses.

### Step 3 — Filtering & Deduplication (quality gates)

Before any lead is accepted, three filters run:
- **Generic-inbox filter:** addresses like `info@`, `support@`, `sales@`, `contact@`, `noreply@` (~50 prefixes) are **rejected** — they waste the limited Snov.io quota and aren't real people.
- **Own-company filter (`MY_COMPANY`):** anything at `volvero.com` is rejected — we don't email ourselves.
- **Deduplication:** the email is checked against **all** spreadsheet tabs; if it already exists anywhere in the funnel, it's skipped.

Surviving leads are split:
- **Verified email** (Snov.io says valid, or our SMTP check passes) → written to **`Waiting_Room_1`** *and* synced to the Brevo WR1 list.
- **Unverified guess** → written to the **`TO_VERIFY`** queue, *not* synced to Brevo (we don't pollute the contact list with addresses that might bounce).

Finally, the bot uploads a **CSV summary in-thread** to the original Slack mention.

### Step 4 — Nightly verification (`verify_queue.py`, 03:00)

Every night a separate script works through the `TO_VERIFY` queue (oldest first):
1. It re-checks each uncertain email using a rotation of **free email-verification APIs** (no credits burned).
2. **Valid / catch-all** → promoted to `Waiting_Room_1` + synced to Brevo.
3. **Invalid or unknown** → the bot asks Gemini for **6 alternative patterns** (`f.lastname@`, `firstname.lastname@`, …) and verifies each. If one works → promoted. If none work after **3 nightly attempts** → discarded.

This is how a Gemini *guess* eventually becomes a confirmed lead — or is dropped if the person genuinely can't be reached.

### Step 5 — Delivery tracking (the clock starts)

The team sends a marketing campaign **manually from Brevo** to a funnel list. For each recipient, Brevo fires a **`delivered`** webhook → the bot writes the timestamp into the spreadsheet's **`sent_date`** column (H). **This timestamp is what the 27-day migration clock counts from.** Duplicate webhooks are ignored so the clock never resets accidentally.

### Step 6 — Monthly migration (1st of each month, 01:00)

The scheduler promotes mature leads to the next stage:
- **`Waiting_Room_2 → Subscribed`** runs **first**, then **`Waiting_Room_1 → Waiting_Room_2`**. (This reverse order prevents a lead from skipping two stages in one night.)
- Only leads whose `sent_date` is set **and** ≥ 27 days old are promoted.
- On promotion, `sent_date` is cleared so the next stage starts a fresh clock, and the lead is synced to the next Brevo list.
- **Missed-migration safety net:** if the VM was down on the 1st, the bot detects this on its next startup (using `/var/lib/volvero/last_migration.json`) and runs the catch-up migration automatically.

### Step 7 — Unsubscribe

If a recipient clicks "unsubscribe", Brevo fires an **`unsubscribe`** webhook → the bot moves that lead to the **`Unsubscribed`** tab, permanently blocking them from all future stages.

---

## 4. Third-Party Services & API Keys — how to get each one

> Everything below is stored in a single `.env` file on the server (see [Section 5](#5-environment-variables-env--full-reference)). This table tells you **what each service is for, how to obtain the credential, and where it goes.**

### 4.1 Slack (REQUIRED — this is how the bot is triggered)

| | |
|---|---|
| **Why** | Receive @-mentions and post replies/CSVs |
| **Cost** | Free |
| **Where to get it** | <https://api.slack.com/apps> |

Steps:
1. **Create New App → From scratch**, name it `Volvero_Email_Finder`, pick the workspace.
2. **Socket Mode** → turn ON → generate an **App-Level Token** with scope `connections:write` → this is **`SLACK_APP_TOKEN`** (`xapp-…`).
3. **OAuth & Permissions → Bot Token Scopes**, add: `app_mentions:read`, `chat:write`, `files:write`, `channels:history`, `groups:history`, `im:history`, `mpim:history`.
4. **Event Subscriptions** → ON → subscribe to bot event `app_mention`.
5. **Install App → Install to Workspace** → copy the **Bot User OAuth Token** → this is **`SLACK_BOT_TOKEN`** (`xoxb-…`).
6. Invite the bot into the channel: `/invite @Volvero_Email_Finder`.

### 4.2 Google Cloud Service Account (REQUIRED — the database connection)

| | |
|---|---|
| **Why** | Read/write the Google Sheet that stores all leads |
| **Cost** | Free |
| **Where to get it** | <https://console.cloud.google.com/> (use the **`Slack bot`** project) |

Steps:
1. In the `Slack bot` project, enable **Google Sheets API** and **Google Drive API** (APIs & Services → Library).
2. **Credentials → Create credentials → Service account** → name it e.g. `volvero-email-finder-bot`.
3. Open the service account → **Keys → Add key → Create new key → JSON**. A `.json` file downloads.
4. Either: place it next to `bot.py` as **`google_credentials.json`**, **or** paste its entire contents into the **`GOOGLE_CREDENTIALS_JSON`** env var (the bot reads the env var first). The current server uses the file on disk.
5. Open the `.json`, copy the **`client_email`** (`…@….iam.gserviceaccount.com`).
6. Open the Google Sheet → **Share** → add that `client_email` with **Editor** rights. *(If you skip this, the bot can't see the sheet.)*

### 4.3 Gemini — Google AI Studio (REQUIRED — the "brain")

| | |
|---|---|
| **Why** | Extract people/companies from text & images; guess email patterns; resolve brand names |
| **Cost** | Has a free tier; check current limits |
| **Where to get it** | <https://aistudio.google.com/apikey> |

Steps: **Create API key** → select the `Slack bot` Cloud project → copy → this is **`GEMINI_API_KEY`**. Model is set by **`GEMINI_MODEL`** (currently `gemini-3-flash-preview`).

> ⚠️ Gemini model names change over time. If the bot logs Gemini errors after a while, the model name may have been retired — check the current model list at AI Studio and update `GEMINI_MODEL`.

### 4.4 Snov.io (REQUIRED — the primary email finder)

| | |
|---|---|
| **Why** | Look up verified B2B emails by name+domain or by domain |
| **Cost** | Paid credits (free trial credits to start). **This is the scarce resource** — that's why generic inboxes are filtered out. |
| **Where to get it** | <https://app.snov.io/> → **Profile → API → Create new app** |

Copy the **Client ID** → **`SNOVIO_CLIENT_ID`**, and **Client Secret** → **`SNOVIO_CLIENT_SECRET`**.

### 4.5 Brevo (REQUIRED — the email platform + webhooks)

| | |
|---|---|
| **Why** | Holds the contact lists, sends the campaigns (done manually by the team), and fires delivery/unsubscribe webhooks |
| **Cost** | Has a free tier |
| **Where to get it** | <https://app.brevo.com/> |

Steps:
1. **API key:** profile menu → **SMTP & API → API Keys → Generate new API key** → **`BREVO_API_KEY`**.
2. **Lists:** Contacts → Lists → create three lists. The numeric ID is in each list's URL (`/contact/list/61` → `61`):
   - Waiting Room 1 → **`BREVO_LIST_ID_WR1`**
   - Waiting Room 2 → **`BREVO_LIST_ID_WR2`**
   - Subscribed → **`BREVO_LIST_ID_SUBSCRIBED`**
3. **Webhook:** Settings → Webhooks → Add a webhook:
   - URL: `http://<VM-public-IP>:5000/brevo-webhook`
   - Events: **delivered** + **unsubscribe**
   - Set a custom secret string → paste the same value into **`BREVO_WEBHOOK_SECRET`** (the bot validates every webhook against it).

### 4.6 Free email-verification APIs (OPTIONAL but recommended — the nightly rescue)

| | |
|---|---|
| **Why** | The nightly `verify_queue.py` uses these to re-check uncertain emails **without** spending Snov.io credits |
| **Cost** | Free daily quotas |
| **Used in rotation** | tried in order; if one is out of quota, the next is used |

| Service | Free/day | Env var | Get it at |
|---|---|---|---|
| QuickEmailVerification | 100 | `QUICKEMAILVERIFICATION_API_KEY` | <https://quickemailverification.com> |
| MyEmailVerifier | 100 | `MYEMAILVERIFIER_API_KEY` | <https://www.myemailverifier.com> |
| BillionVerify | 50 | `BILLIONVERIFY_API_KEY` | <https://app.billionverify.com> |

> If none of these keys are set, the nightly verifier still runs but can only return "unknown" for guesses, so fewer leads get rescued. Setting at least `QUICKEMAILVERIFICATION_API_KEY` is strongly recommended.

---

## 5. Environment Variables (`.env`) — full reference

The bot reads everything from a single file: **`/home/david_f/Slack_Finder/Slack_finder_python/.env`**.

> ⚠️ This file contains all secrets and is **gitignored** — it is **not** in the repository and will **not** survive a VM rebuild. **Make a private backup of it now** (e.g. a password manager or a secure note). Without it, the bot cannot start.

```env
# --- SLACK & AI ---
SLACK_BOT_TOKEN=xoxb-...           # 4.1 — Bot User OAuth Token
SLACK_APP_TOKEN=xapp-...           # 4.1 — App-Level Token (Socket Mode)
GEMINI_API_KEY=...                 # 4.3 — Google AI Studio key
GEMINI_MODEL=gemini-3-flash-preview

# --- LEAD GENERATION (SNOV.IO) ---
SNOVIO_CLIENT_ID=...               # 4.4
SNOVIO_CLIENT_SECRET=...           # 4.4

# --- GENERAL CONFIGURATION ---
MY_COMPANY=volvero.com             # own-domain filter (don't email ourselves)
GOOGLE_SHEET_NAME=Slack bot automatization ( emails )   # exact spreadsheet name

# --- BREVO ---
BREVO_API_KEY=...                  # 4.5
BREVO_LIST_ID_WR1=61               # Month 1 list ID
BREVO_LIST_ID_WR2=62               # Month 2 list ID
BREVO_LIST_ID_SUBSCRIBED=63        # Month 3+ list ID
BREVO_WEBHOOK_SECRET=...           # must match the secret set in Brevo's webhook

# --- GOOGLE CREDENTIALS (only if NOT using the .json file on disk) ---
GOOGLE_CREDENTIALS_JSON=           # paste full service-account JSON as one line

# --- FREE EMAIL VERIFIERS (optional, nightly queue) ---
QUICKEMAILVERIFICATION_API_KEY=... # 4.6 (recommended)
MYEMAILVERIFIER_API_KEY=...        # 4.6 (optional)
BILLIONVERIFY_API_KEY=...          # 4.6 (optional)

# --- OPTIONAL OVERRIDES ---
MIGRATION_STATE_FILE=/var/lib/volvero/last_migration.json   # default; rarely changed
```

> ⚠️ **`GOOGLE_SHEET_NAME` must be identical** in the environment for both `bot.py` and `verify_queue.py` — they open the same spreadsheet by name. The default values hard-coded in each file differ, so the `.env` value is what actually matters; keep it set.

---

## 6. Google Sheets Structure

One spreadsheet (its name = `GOOGLE_SHEET_NAME`) shared with the service-account email. It has these tabs, each with the **exact** header row below.

**Funnel tabs** — `Waiting_Room_1`, `Waiting_Room_2`, `Subscribed`, `Unsubscribed`:

| A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|
| email | name | role | company_domain | source | added_date | linkedin | sent_date |

**Queue tab** — `TO_VERIFY` (auto-created if missing):

| A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|
| email | name | role | company_domain | source | added_to_queue_date | attempt_count | linkedin |

> `sent_date` (column H of the funnel tabs) is written by the Brevo `delivered` webhook. **Migration never promotes a row whose `sent_date` is empty.**

---

## 7. Hosting & Infrastructure (Google Cloud VM)

| Item | Value |
|---|---|
| **Cloud** | Google Cloud Platform |
| **GCP Project** | **`Slack bot`** |
| **VM hostname** | `instance-20260430-075446` |
| **OS** | Debian GNU/Linux 13 (kernel 6.12, x86-64) |
| **Linux user** | `david_f` |
| **App directory** | `/home/david_f/Slack_Finder/Slack_finder_python` |
| **Python env** | virtualenv at `.venv/` (Python 3.13) |
| **Migration state file** | `/var/lib/volvero/last_migration.json` |
| **Log files** | `funnel_bot.log` (bot) and `verify_queue.log` (nightly), inside the app dir |
| **Open port** | 5000 (Brevo webhook) — must be reachable from the internet |

**How to connect:** from the Google Cloud Console, open the VM in **Compute Engine → VM instances** and click **SSH**, or use the CLI:

```bash
gcloud compute ssh instance-20260430-075446 --project="<project-id-of-Slack-bot>"
```

> Find the exact project ID in the Cloud Console (the display name is "Slack bot"; the ID is a slug like `slack-bot-123456`).

---

## 8. The Two Background Services (systemd)

The bot survives reboots and crashes because it runs as `systemd` services. There are **two**, and **the bot must be supervised ONLY by `systemd`** — see [§9 — ghost-process check](#-make-sure-only-one-bot-is-running-ghost-process-check) for why this matters.

### 8.1 `volvero-bot.service` — the always-on bot

Runs `bot.py` (Slack listener + webhook server + monthly migration scheduler). Auto-restarts on crash and on reboot.

```ini
# /etc/systemd/system/volvero-bot.service   (already installed on the VM)
[Unit]
Description=Volvero Slack Bot
After=network.target

[Service]
User=david_f
WorkingDirectory=/home/david_f/Slack_Finder/Slack_finder_python
ExecStart=/home/david_f/Slack_Finder/Slack_finder_python/.venv/bin/python3 bot.py
EnvironmentFile=/home/david_f/Slack_Finder/Slack_finder_python/.env
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 8.2 `verify-queue.timer` — the nightly verifier

Runs `verify_queue.py` once a day at 03:00. The unit files are documented at the bottom of `verify_queue.py` itself. Summary:

```ini
# /etc/systemd/system/verify-queue.timer
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true        # if the VM was off at 03:00, run on next boot

# /etc/systemd/system/verify-queue.service → ExecStart=python3 verify_queue.py
```

**Useful commands:**

```bash
# See timer schedule / last run
systemctl list-timers verify-queue.timer

# Manually trigger a nightly run right now (for testing)
sudo systemctl start verify-queue.service
```

---

## 9. Operations Runbook (daily tasks)

> All commands run on the VM, from `/home/david_f/Slack_Finder/Slack_finder_python` unless noted.

### ✅ Is the bot alive?

```bash
sudo systemctl status volvero-bot.service
```

Look for `active (running)`. The healthy startup log shows:

```
✅ Connection with Google Sheets established.
🕐 Monthly scheduler started (1st of each month, 01:00 AM).
🤖 Volvero Email Finder connected and listening for mentions.
⚡️ Bolt app is running!
```

### 📜 Read the logs

```bash
# Live tail of the bot
sudo journalctl -u volvero-bot.service -f

# Last 50 lines
sudo journalctl -u volvero-bot.service -n 50 --no-pager

# Nightly verifier log
sudo journalctl -u verify-queue.service -n 50 --no-pager
# (or:  tail -n 50 verify_queue.log )
```

### 🔄 Deploy a code update (the standard workflow)

Code changes are pushed to GitHub, then pulled on the VM:

```bash
cd ~/Slack_Finder
git pull
sudo systemctl restart volvero-bot.service
sudo journalctl -u volvero-bot.service -n 20 --no-pager   # confirm clean startup
```

> If `git pull` says *"Already up to date"* but you expected changes, you're probably on the wrong branch — the production branch is **`main`**. Run `git branch` to check.

### 🔁 Restart / Stop / Start

```bash
sudo systemctl restart volvero-bot.service
sudo systemctl stop volvero-bot.service
sudo systemctl start volvero-bot.service
```

### 📦 After changing dependencies (`requirements.txt`)

```bash
cd ~/Slack_Finder/Slack_finder_python
source .venv/bin/activate
pip install -r requirements.txt
deactivate
sudo systemctl restart volvero-bot.service
```

### 🧹 Clear Python bytecode cache (if a deploy seems not to take effect)

Python keeps compiled `.pyc` files in `__pycache__/` directories. In rare cases (mostly when `git pull` preserves file timestamps), the running process can hold on to stale bytecode. If a code change clearly landed on disk but the running bot still behaves like the old version, nuke the caches and restart:

```bash
sudo find ~/Slack_Finder -name "__pycache__" -type d -prune -exec rm -rf {} +
sudo find ~/Slack_Finder -name "*.pyc" -delete
sudo systemctl restart volvero-bot.service
```

> If the old behaviour persists **after** this, the cause is almost certainly a duplicate process — jump to the next subsection.

### 🕵️ Make sure ONLY one bot is running (ghost-process check)

> **Why this matters — a real production incident.** For 13 days, two copies of the bot ran in parallel on the VM: the `systemd` service (with current code) and a leftover **PM2** instance from before the migration to `systemd` (with months-old code). The PM2 ghost was intercepting some Slack mentions and logging stale behaviour, making it look as if recent code fixes weren't being deployed at all. Both consumed memory; only one was actually correct. **The bot must be managed by `systemd` only.**

#### How to check for ghosts

```bash
# 1. Is PM2 running anything?  (If pm2 isn't installed, this errors — that's OK)
pm2 list 2>/dev/null

# 2. How many bot.py Python processes are alive?
ps aux | grep -i "[p]ython.*bot.py"
```

A **healthy** state shows:
- `pm2 list` either errors with *"command not found"* or shows an **empty** list
- Exactly **one** Python process running `bot.py` (the one launched by systemd)

If `pm2 list` shows an entry, or if `ps` shows two or more `bot.py` processes, you have a ghost.

#### How to clean up if you find a ghost

```bash
# Stop ALL PM2 processes and kill the PM2 daemon
pm2 kill

# Disable PM2 from auto-starting on reboot — PM2 prints an exact sudo command, run it
pm2 unstartup systemd

# Restart the systemd bot so it becomes the only running instance
sudo systemctl restart volvero-bot.service

# Verify only one Python process is alive now
ps aux | grep -i "[p]ython.*bot.py"
```

> **Golden rule.** This bot must be supervised only by `systemd`. If you ever find another manager running a copy (PM2, `supervisord`, `nohup`, `screen`, `tmux`, a cron job, a Docker container, …), kill it and disable its auto-start. Two processes connected to the same Slack app and the same Google Sheet *will* cause silent races and stale behaviour.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot doesn't respond to mentions | Service down, or Slack tokens expired | `systemctl status`; check logs for `SlackApiError`; re-issue tokens if needed |
| `Permission denied: '/var/lib/volvero'` on startup | State directory missing | `sudo mkdir -p /var/lib/volvero && sudo chown david_f:david_f /var/lib/volvero`, then restart |
| Gemini errors / no leads extracted | `GEMINI_MODEL` retired, or quota exhausted | Check AI Studio for current model name; update `GEMINI_MODEL` in `.env` |
| No emails found for anyone | Snov.io out of credits | Check Snov.io dashboard; top up credits |
| `sent_date` never gets written | Brevo webhook not reaching the VM | Verify port 5000 is open; check the webhook URL/secret in Brevo matches `.env` |
| Webhooks return 403 | `BREVO_WEBHOOK_SECRET` mismatch | Make the `.env` value identical to Brevo's webhook secret |
| Leads stuck, never migrate | `sent_date` empty (email never delivered), or <27 days old | Expected behaviour — migration is delivery-gated |
| Migration didn't run on the 1st | VM was down | It self-heals on next startup (catch-up migration) — check logs for `Missed migration detected` |
| Sheets API `429` errors | Too many requests (high volume) | See [Scaling Limits](#12-costs-quotas--scaling-limits) |
| **Recent code changes don't seem to take effect in production** | A second copy of the bot is running (PM2 ghost, `screen` session, leftover `nohup`, etc.) intercepting requests with stale code | See **[§9 — Ghost-process check](#-make-sure-only-one-bot-is-running-ghost-process-check)** |

---

## 11. Disaster Recovery — if the VM dies

If the VM is deleted, corrupted, or you need to rebuild it elsewhere, here's the full recipe. **None of the lead data is lost** — it all lives in Google Sheets and Brevo, not on the VM. Only the *running process* and the `.env` need to be restored.

### What you need before starting
1. The **`.env`** file (your private backup — see the warning in [Section 5](#5-environment-variables-env--full-reference)).
2. The **`google_credentials.json`** service-account key (or the `GOOGLE_CREDENTIALS_JSON` value).
3. Access to the GitHub repo: `https://github.com/FormosoCrl/Slack_Finder`.

### Rebuild steps

```bash
# 1. Create/boot a Linux VM (Debian/Ubuntu), then SSH in.

# 2. Install Python + git
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# 3. Clone the repo
cd ~ && git clone https://github.com/FormosoCrl/Slack_Finder.git
cd Slack_Finder/Slack_finder_python

# 4. Create the virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium

# 5. Restore secrets (copy your backups into this folder)
#    - .env
#    - google_credentials.json   (if using the file method)

# 6. Create the migration state dir
sudo mkdir -p /var/lib/volvero && sudo chown $USER:$USER /var/lib/volvero

# 7. Re-create the two systemd units (see Section 8), then:
sudo systemctl daemon-reload
sudo systemctl enable --now volvero-bot.service
sudo systemctl enable --now verify-queue.timer

# 8. Update the Brevo webhook URL to the NEW VM's public IP:5000

# 9. (Important) Make sure NO other process manager (PM2, etc.) is set up on
#    this VM — see §9 ghost-process check.
```

### Choosing the VM — Google Cloud free tier

Google Cloud offers an **"Always Free" tier** that includes a small VM at no cost, which is enough to run this bot. **However, the exact specs and eligible regions change over time**, so do **not** assume any fixed numbers from this document.

> **➡️ When rebuilding, ask an AI assistant (or check the official page) for the *current* GCP Always-Free VM requirements**, e.g.:
> *"What are the current Google Cloud Always Free tier VM specs (machine type, regions, disk size) as of [today's date]?"*
>
> Then pick a machine type and region that fall within those current free limits. As a historical reference only (verify before relying on it): the free tier has typically been a single small shared-core VM in specific US regions with a modest standard persistent disk — but **confirm the live requirements at the moment you provision**.

The bot is lightweight (peak ~200 MB RAM, ~9 min CPU/day), so the smallest free-tier machine is sufficient for the current and projected volume.

---

## 12. Costs, Quotas & Scaling Limits

### What costs money vs. what's free

| Service | Free tier | Paid? |
|---|---|---|
| Slack | ✅ Free | No |
| Google Sheets / Drive API | ✅ Free | No |
| Gemini (AI Studio) | ✅ Free tier (verify current limits) | Only at high volume |
| **Snov.io** | Trial credits | **Yes — the main recurring cost.** Top up as credits run low |
| Brevo | ✅ Free tier (with daily send caps) | Paid plans for higher send volume |
| Free email verifiers | ✅ Free daily quotas | No |
| GCP VM | ✅ Always-Free tier (see §11) | Only if you exceed free limits |

### Scaling limits (current code)

The codebase was optimised to handle growth. Current ballpark: **~300 leads today, designed to scale to ~8,000.**

| Total leads | Status |
|---|---|
| 0 – 2,000 | All green |
| 2,000 – 5,000 | Fine; high-delivery days get a little slower |
| 5,000 – 8,000 | Comfortable (the two hot paths were already optimised to use targeted `find()` calls instead of full-sheet reads) |
| 8,000+ | Monitor Google Sheets API `429` rate-limit errors; if they appear, batch the Brevo sync calls and read only the needed columns |

The two performance-critical operations — recording `sent_date` (per delivered email) and moving a lead on unsubscribe — were rewritten to be O(1) targeted operations rather than full-sheet scans, specifically so the funnel scales to several thousand leads without hitting Google's rate limits.

---

## 13. Project File Map

```
Slack_Finder/                          ← git repo root
├── README.md                          ← original technical setup guide
├── HANDOVER.md                        ← this document
└── Slack_finder_python/
    ├── .env                           ← (gitignored) ALL secrets — back this up!
    ├── google_credentials.json        ← (gitignored) GCP service-account key
    ├── bot.py                         ← MAIN: Slack listener, webhook server, scheduler, migration
    ├── verify_queue.py                ← nightly TO_VERIFY processor (systemd timer)
    ├── requirements.txt               ← Python dependencies
    ├── funnel_bot.log                 ← bot runtime log
    ├── verify_queue.log               ← nightly verifier log
    └── src/
        ├── email_verifier.py          ← native SMTP verifier + ROLE_ACCOUNTS (generic-inbox list)
        ├── lead_verifier.py           ← free-API rotation (QuickEmailVerification, etc.)
        ├── page_scraper.py            ← Playwright homepage email scraper
        └── url_utils.py               ← domain normaliser
```

**Key functions to know in `bot.py`:**
- `handle_app_mention()` → entry point for Slack mentions
- `process_and_reply()` → the full enrichment pipeline
- `analyze_image_with_ai()` / Gemini calls → AI extraction
- `brevo_webhook()` → handles `delivered` + `unsubscribe` events
- `CloudManager.run_migration()` / `_migrate_logic()` → monthly funnel migration
- `check_missed_migration()` → startup catch-up safety net
- `export_to_brevo()` → syncs a lead into a Brevo list

---

## 14. Glossary

| Term | Meaning |
|---|---|
| **Funnel** | The 4-stage lead journey: WR1 → WR2 → Subscribed (+ Unsubscribed block list) |
| **WR1 / WR2** | `Waiting_Room_1` / `Waiting_Room_2` — months 1 and 2 of the nurture sequence |
| **`sent_date`** | Timestamp Brevo confirmed the email was delivered; the 27-day clock counts from here |
| **TO_VERIFY queue** | Holding area for unproven AI-guessed emails, re-checked nightly |
| **Snipe / re-snipe** | Guessing a likely email pattern (`f.lastname@…`) and verifying it |
| **Role account / generic inbox** | `info@`, `support@`, etc. — filtered out because they aren't real people and waste Snov.io credits |
| **Catch-all** | A mail server that accepts any address; treated as "good enough" to keep |
| **Migration** | The monthly promotion of mature leads to the next funnel stage |
| **Socket Mode** | Slack's connection method that needs no public inbound URL for the bot itself |
| **Ghost process** | A second copy of the bot left running by an old supervisor (PM2, `nohup`, etc.) that silently intercepts traffic with stale code — see §9 |

---

*Document maintained alongside the codebase. When you change a flow, a key, or the infrastructure, update the relevant section here so the next person isn't left guessing.*
