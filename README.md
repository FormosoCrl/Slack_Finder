# Slack Lead Finder 🤖 (v2.0 - Professional Edition)

This repository is a high-performance refactor of the original work by Alaa Sahraoui, now featuring Real-Time Cloud Sync, Budget Optimization, and Deep Lead Intelligence.

---

## 📌 Overview
Slack_Finder is an autonomous B2B lead generation engine. It transforms Slack messages into a synchronized database. By combining Google Gemini 3 (AI) with the Snov.io API, it doesn't just extract data—it investigates, verifies, and mirrors it to the cloud.

---

## 🚀 Key New Features (v2.0)

* Mirror Sync System: Bi-directional synchronization between local leads_report.csv and Google Sheets. If a lead is deleted or missing in one place, the bot ensures the cloud stays updated.
* Budget Optimization: Strictly limited to 4 leads per company to maximize Snov.io credit efficiency, saving up to 70% in operational costs.
* Target Intelligence: Dual-step search logic. It attempts a Direct Match for specific names (e.g., "Luke Edis") and intelligently falls back to a team-wide search if the specific email is not found.
* Resilience Engine: Automatic handling of API limits (429 for Gemini) and credit exhaustion (402 for Snov.io) with a graceful fallback to Scraper Patterns.

---

## 🔑 Environment Variables

Create a .env file in the root directory (make sure it's in your .gitignore):

# Slack Connection
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_APP_TOKEN=xapp-your-token

# AI & Prospecting
GEMINI_API_KEY=your-key
SNOVIO_CLIENT_ID=your-id
SNOVIO_CLIENT_SECRET=your-secret

# Identity & Cloud
MY_COMPANY=volvero.com
GOOGLE_SHEET_NAME=HojaCalculoPrueba

> IMPORTANT: You also need a google_credentials.json file in the root directory to enable Google Sheets access.

---

## 🧠 The Intelligence Engine (How It Works)

1. AI Analysis: Gemini 3 analyzes the chat context, extracts corporate domains, and identifies specific targets (People) and their roles.
2. Deep Investigation:
   * Step 1: The bot searches for the "Specific Target" email using name + domain matching.
   * Step 2: It fetches 4 high-value team members from the same domain to enrich the lead pool.
3. Smart Filter: Internal domains (e.g., volvero.com) are strictly ignored via Python logic to prevent data leaks.
4. Cloud Mirroring: Data is deduplicated against the Live Google Sheet in real-time before being uploaded.

---

## 📂 Project Structure

formosocrl-slack_finder/
├── .gitignore               # Multi-layer security for credentials
├── google_credentials.json   # (Local) Google Cloud Key
├── README.md                # Project documentation
└── Slack_finder_python/
    ├── .env                 # (Local) API Keys
    ├── bot.py               # Main Orchestrator Engine
    ├── leads_report.csv     # Local Mirror Database
    └── src/                 # Modular logic (Scrapers, Utils)

---

## 🚧 Roadmap

### ✅ Completed (Phase 2)
- [x] Full Snov.io API Integration.
- [x] Google Sheets Real-Time Mirroring.
- [x] Budget Logic (Credit saving mode).
- [x] Enhanced AI prompt for high-precision extraction.

### 🔜 Phase 3 (Next Steps)
- [ ] Lead Scoring: AI-driven qualification of leads based on role relevance.
- [ ] Automated Outreach: Direct integration with Brevo/Lemlist for drip campaigns.
- [ ] Dashboard: Basic UI to monitor real-time credit consumption and sync status.

---

## 👨‍💻 Credits
Refactored and enhanced by FormosoCrl.
Based on the original architecture by Alaa Sahraoui.