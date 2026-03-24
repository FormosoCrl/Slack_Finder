import os
import re
import json
import pandas as pd
import requests
import gspread
from datetime import datetime
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai

# --- 1. CONFIGURATION ---
# Load environment variables from .env file
load_dotenv()

# Initialize Slack App and Google Gemini Client
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
client_google = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# Global Constants
CSV_FILE = "leads_report.csv"
MY_COMPANY = os.getenv("MY_COMPANY", "volvero.com")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "HojaCalculoPrueba")


# --- 2. SYNC MODULE (CLOUD & LOCAL DEDUPLICATION) ---

def get_cloud_emails():
    """Fetches all existing emails from the Google Sheet (Column 1) to prevent duplicates."""
    try:
        gc = gspread.service_account(filename="google_credentials.json")
        sh = gc.open(GOOGLE_SHEET_NAME)
        emails = sh.sheet1.col_values(1)
        return set([e.strip().lower() for e in emails if e])
    except Exception as e:
        print(f"⚠️ Cloud Read Error: {e}")
        return set()


def get_local_emails():
    """Fetches all emails from the local CSV backup."""
    if os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            if not df.empty and 'email' in df.columns:
                return set(df['email'].astype(str).str.lower().unique())
        except:
            pass
    return set()


def export_to_google_sheets(leads):
    """Appends new leads to the Google Sheet. Adds headers if the sheet is empty."""
    if not leads: return False
    try:
        gc = gspread.service_account(filename="google_credentials.json")
        sh = gc.open(GOOGLE_SHEET_NAME)
        worksheet = sh.sheet1

        # Check if sheet is empty to add headers
        if not worksheet.get_all_values():
            worksheet.append_row(["Email", "Name", "Role", "Company", "Source", "Date"])

        rows = [[l.get("email"), l.get("name"), l.get("role"),
                 l.get("company_domain"), l.get("source"), l.get("added_date")]
                for l in leads]
        worksheet.append_rows(rows)
        return True
    except Exception as e:
        print(f"❌ Sheets Error: {e}")
        return False


# --- 3. SNOV.IO MODULE (API INTEGRATION) ---

def get_snovio_token():
    """Authenticates with Snov.io API using Client Credentials."""
    cid, sec = os.getenv("SNOVIO_CLIENT_ID"), os.getenv("SNOVIO_CLIENT_SECRET")
    try:
        res = requests.post("https://api.snov.io/v1/oauth/access_token",
                            data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec})
        return res.json().get("access_token")
    except:
        return None


def fetch_snovio_by_domain(domain, token, limit=4):
    """
    Fetches leads from a specific domain.
    Budget Logic: Limit is set to 4 to save credits while providing enough context.
    """
    url = f"https://api.snov.io/v2/domain-emails-with-info?domain={domain}&type=personal&limit={limit}"
    try:
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        # Check for credit exhaustion or rate limits
        if res.status_code in [402, 429]: return [], "snovio credit limit reached, using scraper..."
        data = res.json()
        return [{"email": e['email'], "name": f"{e.get('firstName', '')} {e.get('lastName', '')}".strip(),
                 "role": e.get("position", "N/A"), "company_domain": domain, "source": "Snov.io Domain"}
                for e in data.get("emails", [])], None
    except:
        return [], None


def fetch_snovio_by_person(full_name, domain, token):
    """Attempts to find the specific email address of a named person at a specific domain."""
    url = "https://api.snov.io/v1/get-emails-from-names"
    parts = full_name.split(" ", 1)
    payload = {"firstName": parts[0], "lastName": parts[1] if len(parts) > 1 else "", "domain": domain}
    try:
        res = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
        data = res.json()
        if data.get("success") and data.get("data", {}).get("email"):
            return {"email": data["data"]["email"], "name": full_name, "role": "Direct Target",
                    "company_domain": domain, "source": "Snov.io Name Match"}, None
    except:
        pass
    return None, None


# --- 4. SCRAPER MODULE (FALLBACK PLAN) ---

def run_custom_scraper(domain):
    """Generates standard corporate email patterns if the API has no data."""
    patterns = ["info", "contact", "sales", "support"]
    return [{"email": f"{p}@{domain}", "name": "Auto-Generated", "role": "Corporate Inbox",
             "company_domain": domain, "source": "Scraper Patterns"} for p in patterns]


# --- 5. AI MODULE (EXTRACTION LOGIC) ---

def analyze_text_with_ai(text):
    """Uses Gemini 3 to extract leads, infer domains, and identify specific people."""
    try:
        prompt = f"""
        Act as a Lead Extraction Specialist. Extract business leads with 100% precision.
        Return ONLY a JSON object. No prose.
        Format: {{ "domains": [], "people": [{{ "name": "", "company_domain": "" }}], "emails": [{{ "email": "", "role": "" }}] }}

        STRICT RULES:
        1. Infer domains from company names (e.g., 'Octopus Ventures' -> 'octopusventures.com').
        2. If you see 'Name (Company)', add them to 'people' with the inferred domain.
        3. Exclude any data related to {MY_COMPANY}.
        4. Capture roles (CEO, Founder, Partner, etc.) whenever available.

        Text: {text}
        """
        response = client_google.models.generate_content(model='gemini-3-flash-preview', contents=prompt)
        # Strip potential markdown code blocks from AI response
        clean_json = re.sub(r'```json|```', '', response.text).strip()
        return json.loads(clean_json), None
    except Exception as e:
        if "429" in str(e): return None, "IA credits limit reached"
        return {"domains": [], "people": [], "emails": []}, f"IA Error: {str(e)[:30]}"


# --- 6. CORE PROCESSING LOGIC ---

def process_and_reply(event, client):
    """Main workflow: Analyze -> Search -> Sync -> Notify."""
    text = re.sub(r'<@[A-Z0-9]+>', '', event.get("text", "")).strip()
    if not text: return

    channel = event["channel"]
    client.chat_postMessage(channel=channel, text="🚀 *Deep Search & Smart Sync active...*")

    # A. Analyze text with AI
    data, gemini_error = analyze_text_with_ai(text)
    if data is None:
        client.chat_postMessage(channel=channel, text=f"❌ {gemini_error}");
        return

    token = get_snovio_token()
    raw_found, now, snov_warn_sent = [], datetime.now().strftime("%Y-%m-%d %H:%M"), False
    processed_domains = set()

    # B. Process Specific People (Priority 1)
    for p in data.get("people", []):
        name, domain = p.get("name"), p.get("company_domain")
        if not (token and name and domain): continue

        # 1. Search for the specific individual (Franc-tireur search)
        lead, snov_err = fetch_snovio_by_person(name, domain, token)
        if lead:
            lead["added_date"] = now;
            raw_found.append(lead)

        # 2. ALWAYS fetch 4 additional leads from that domain (The "Team" quota)
        leads, _ = fetch_snovio_by_domain(domain, token, limit=4)
        for l in leads:
            l["added_date"] = now;
            raw_found.append(l)

        processed_domains.add(domain)

    # C. Process Companies mentioned alone
    for domain in data.get("domains", []):
        if domain in processed_domains or MY_COMPANY in domain: continue
        leads, snov_err = fetch_snovio_by_domain(domain, token, limit=4) if token else ([], None)

        if snov_err and not snov_warn_sent:
            client.chat_postMessage(channel=channel, text=f"⚠️ {snov_err}")
            snov_warn_sent = True

        if not leads: leads = run_custom_scraper(domain)
        for l in leads: l["added_date"] = now; raw_found.append(l)

        processed_domains.add(domain)

    # D. Direct Emails found in text
    for e in data.get("emails", []):
        email_val = e['email'] if isinstance(e, dict) else e
        if MY_COMPANY not in email_val:
            role_val = e.get("role", "Extraction") if isinstance(e, dict) else "Extraction"
            raw_found.append({"email": email_val, "name": "N/A", "role": role_val,
                              "company_domain": email_val.split('@')[-1], "source": "Chat", "added_date": now})

    # --- SMART SYNC LOGIC ---
    cloud_emails, local_emails = get_cloud_emails(), get_local_emails()
    leads_to_cloud, leads_to_local = [], []

    for l in raw_found:
        email_clean = l['email'].strip().lower()
        if MY_COMPANY in email_clean: continue

        # Check Cloud Sync status
        if email_clean not in cloud_emails:
            leads_to_cloud.append(l);
            cloud_emails.add(email_clean)

        # Check Local Sync status
        if email_clean not in local_emails:
            leads_to_local.append(l);
            local_emails.add(email_clean)

    # Update Local Backup
    if leads_to_local:
        df_new = pd.DataFrame(leads_to_local)
        if os.path.exists(CSV_FILE):
            df_new = pd.concat([pd.read_csv(CSV_FILE), df_new], ignore_index=True)
        df_new.drop_duplicates(subset=['email'], keep='first').to_csv(CSV_FILE, index=False)

    # Update Google Sheets
    if leads_to_cloud:
        export_to_google_sheets(leads_to_cloud)
        msg = f"✅ Sync Complete: {len(leads_to_cloud)} new leads updated in Cloud."
    else:
        msg = "⚠️ No new unique leads to add to Cloud."

    # Send the final report file back to Slack
    client.files_upload_v2(channel=channel, file=CSV_FILE, title="Leads Report", initial_comment=msg)


# --- 7. EVENT HANDLERS ---

@app.event("app_mention")
def handle_app_mention(event, client, say):
    process_and_reply(event, client)


@app.event("message")
def handle_message(event, client):
    # Only process standard messages (ignore bot messages or subtypes)
    if event.get("subtype") is None:
        process_and_reply(event, client)


# --- APPLICATION ENTRY POINT ---
if __name__ == "__main__":
    print("⚡️ Slack_Finder: Deep Search READY | Security: Active")
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    handler.start()