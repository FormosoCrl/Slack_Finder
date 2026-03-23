# Slack_Finder 🤖 (v1.0 - AI Powered)

This app is a refactor from a fork ([FormosoCrl/volvero-emails](https://github.com/FormosoCrl/volvero-emails)) of the original repository by Alaa Sahraoui.

---

## 📌 Overview

**Slack_Finder** is a Slack bot that automates B2B lead generation directly from chat messages. It uses **Google Gemini AI** to analyze text, extract domains and emails, and generate a clean database ready for prospecting.

---

## 🔑 Environment Variables

Create a `.env` file in the root directory (make sure it's in your `.gitignore`):

```bash
SLACK_BOT_TOKEN=your_bot_token
SLACK_APP_TOKEN=your_socket_token
GEMINI_API_KEY=your_gemini_key
MY_COMPANY=volvero.com
```

---

## 📊 Data Schema (CSV)

The `leads_report.csv` file follows this structure to ensure compatibility with CRMs:

| Column | Description | Example |
| :--- | :--- | :--- |
| **email** | Unique email address (avoids duplicates) | `ceo@matrixinternet.ie` |
| **name** | Lead name or "Generic/Auto" | `John Doe` |
| **role** | Detected job title | `CEO` |
| **company_domain** | Domain of the company | `matrixinternet.ie` |
| **source** | Origin (Snovio or Extraction) | `Snovio (Mock)` |
| **added_date** | Capture date | `2026-03-23 17:34` |

---

## 🧠 How It Works (AI + UX)

* **Instant Feedback:** The bot immediately confirms it is processing the message to improve UX.
* **AI Extraction:** Gemini extracts domains and classifies emails by relevance.
* **The "Hard" Filter:** Python code strictly removes any mention of the internal company domain.
* **Auto-Enrichment:** Snov.io simulation to automatically add key contacts.
* **Output:** Elegant report in Slack (Block Kit) + updated CSV download.

---

## 🛡️ Security & Reliability

* **Double Filter:** AI + hardcoded Python filter to block `volvero.com` data.
* **Duplicate Protection:** Pandas prevents duplicate entries in the CSV file.
* **Anti-Error:** Automatic cleanup of inconsistent or malformed AI responses.

---

## 📂 Project Structure 

```text
formosocrl-slack_finder/ 
├── .gitignore             # Credential security 
├── README.md 
└── Slack_finder_python/ 
    ├── .env               # (Local only) 
    ├── bot.py             # Main bot engine 
    └── leads_report.csv   # Local database
```

---

## ▶️ Run the Bot

```bash
pip install -r requirements.txt
python Slack_finder_python/bot.py
```

---

## 🚧 Roadmap

### Phase 1 (Current)
* Functional Slack bot.
* AI extraction.
* CSV local storage.

### Phase 2
* Real integration with **Snov.io API**.
* Advanced web scraping for enrichment.
* Google Sheets real-time sync.

### Phase 3
* Email automation integration (**Brevo**).
* AI-powered lead scoring.
* Full CRM integration.

---

## 👨‍💻 Credits
Refactored by **FormosoCrl**
Based on the original work by **Alaa Sahraoui**
