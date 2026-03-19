# Slack_Finder

*This App is a refactor from a fork (https://github.com/FormosoCrl/volvero-emails) of https://github.com/alaasahraoui original repository https://github.com/alaasahraoui/volvero-emails.*

## 🚀 What does this project do?

**Slack_Finder** is a Slack-integrated bot designed to automate the entire B2B lead generation and prospecting process without ever leaving the chat. 

The main workflow is very simple:
1. **Input:** The user sends the bot a **link, a company name, or a person's name** via Slack.
2. **Enrichment:** The bot automatically investigates to find the company's domain and its key employees.
3. **Extraction:** It uses a double-barrier system (Snov.io API + Custom Scraper) to obtain real, verified emails.
4. **Storage & Outreach:** It filters out duplicates, saves clean results into Google Sheets (using a double-list system for Prospects and VIPs), and automatically injects them into *cold email* drip campaigns (Brevo/Sendinblue).

Basically, it transforms a simple Slack message into a fully automated sales funnel with *Human in the Loop* supervision.

## 🚧 Development Status

**Current Status: Null / Planning Phase**

The project is currently in the initial stage of logical architecture design and workflow definition. The codebase has not been developed yet. 

**Next Steps (Phase 1):**
- [ ] Configure the App in the Slack Developer Console.
- [ ] Initialize the Python server using `slack_bolt`.
- [ ] Program the interactive Slack menu (Block Kit) to receive user input (Link / Company / Name).