import os
import re
import json
import time  # ---> NUEVO: Librería para las pausas
import pandas as pd
import requests
import gspread
from datetime import datetime
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai
from ddgs import DDGS

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
            worksheet.append_row(["Email", "Name", "Role", "Company", "Source", "Date", "LinkedIn"])

        rows = [[l.get("email"), l.get("name"), l.get("role"),
                 l.get("company_domain"), l.get("source"), l.get("added_date"), l.get("linkedin", "N/A")]
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
                 "role": e.get("position", "N/A"), "company_domain": domain, "source": "Snov.io Domain",
                 "linkedin": "N/A"}
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
                    "company_domain": domain, "source": "Snov.io Name Match", "linkedin": "N/A"}, None
    except:
        pass
    return None, None


def verify_email_snovio(email, token):
    """Verifies if an email exists using Snov.io Verifier."""
    url = "https://api.snov.io/v1/get-emails-verification"
    try:
        res = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json={"emails": [email]})
        data = res.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0].get("result", "unknown")
    except:
        pass
    return "unknown"


# --- 4. SCRAPER MODULE (FALLBACK PLAN) ---

def run_custom_scraper(domain):
    """Generates standard corporate email patterns if the API has no data."""
    patterns = ["info", "contact", "sales", "support"]
    return [{"email": f"{p}@{domain}", "name": "Auto-Generated", "role": "Corporate Inbox",
             "company_domain": domain, "source": "Scraper Patterns", "linkedin": "N/A"} for p in patterns]


# --- 5. AI MODULES (EXTRACTION & INVESTIGATION) ---

def analyze_text_with_ai(text, retries=2):
    """Uses Gemini 3 to extract leads, infer domains, and identify specific people."""
    prompt = f"""
    Act as a Senior Business Intelligence & Lead Generation Expert. 
    Your goal is to perform a DEEP SCAN of the provided text to extract EVERY potential business lead.

    STRICT OUTPUT RULE: Return ONLY a valid JSON object. No prose, no explanations.
    Format: {{ 
      "domains": [], 
      "people": [{{ "name": "", "company_domain": "", "role": "" }}], 
      "emails": [{{ "email": "", "role": "" }}] 
    }}

    SCANNING RULES:
    1. EXHAUSTIVE SEARCH: Scan the entire text, including signatures, speaker lists, event agendas, and footers. Do not stop after the first lead.
    2. ENTITY LINKING: If you find a person and a company nearby (e.g., 'Jeff Sheridan - Founder (Matrix Internet)'), link them in the 'people' array.
    3. DOMAIN INFERENCE: You MUST infer the corporate domain for every company name found (e.g., 'Matrix Internet' -> 'matrixinternet.ie', 'Digital SME Alliance' -> 'digitalsme.eu').
    4. ROLE CAPTURE: Extract the exact job title (CEO, Project Manager, Founder, etc.). If not explicitly stated, use "Lead".
    5. SECURITY FILTER: Absolutely EXCLUDE any data related to {MY_COMPANY} or its employees (like Marco Filippi).
    6. CLEANING: Remove prefixes like 'Mr.', 'Ms.', or 'Dr.' from names.

    Text to analyze: 
    {text}
    """

    # ---> NUEVO: Lógica de reintento para la API gratuita
    for attempt in range(retries + 1):
        try:
            response = client_google.models.generate_content(model='gemini-3-flash-preview', contents=prompt)
            clean_json = re.sub(r'```json|```', '', response.text).strip()
            return json.loads(clean_json), None
        except Exception as e:
            if "429" in str(e) and attempt < retries:
                print(f"⏳ Límite de API alcanzado en extracción. Esperando 10s... (Intento {attempt + 1}/{retries})")
                time.sleep(10)
            else:
                return {"domains": [], "people": [], "emails": []}, f"IA Error: {str(e)[:50]}"


def investigate_linkedin_with_ai(name, company, retries=2):
    """Agent: Uses DuckDuckGo to search for a person and Gemini to verify the LinkedIn URL."""
    try:
        search_query = f'site:linkedin.com/in/ "{name}" "{company}"'
        print(f"🔍 Buscando en DDG: {search_query}")

        try:
            results_list = DDGS().text(search_query, max_results=3)
            raw_results = str(results_list) if results_list else ""
        except Exception as e:
            print(f"⚠️ Error al conectar con DDG: {e}")
            raw_results = ""

        print(f"📦 Resultados DDG: {raw_results[:200]}...")

        if not raw_results or raw_results == "[]":
            print("⚠️ DDG no devolvió resultados útiles.")
            return None

        verification_prompt = f"""
        You are a Data Verification Agent.
        Target Person: {name}
        Target Company: {company}

        Below are search results from DuckDuckGo. Your job is to find the SINGLE correct LinkedIn profile URL for the Target Person working at the Target Company.

        Search Results:
        {raw_results}

        STRICT RULES:
        1. If a result clearly matches the person AND company, return ONLY the URL (e.g., https://www.linkedin.com/in/luke-edis). No prose.
        2. If none of the results confidently match BOTH the name and the company, return the exact word: NONE
        """

        # ---> NUEVO: Lógica de reintento para la API gratuita
        for attempt in range(retries + 1):
            try:
                response = client_google.models.generate_content(model='gemini-3-flash-preview',
                                                                 contents=verification_prompt)
                url = response.text.strip()
                print(f"🧠 Gemini decidió que la URL es: {url}")

                if url == "NONE" or not url.startswith("http"):
                    return None

                return url
            except Exception as e:
                if "429" in str(e) and attempt < retries:
                    print(
                        f"⏳ Límite de API alcanzado en verificación de {name}. Esperando 15s... (Intento {attempt + 1}/{retries})")
                    time.sleep(15)
                else:
                    raise e  # Lanzamos el error final si fallan los reintentos

    except Exception as e:
        print(f"⚠️ Agent Investigation Error: {e}")
        return None


# --- 6. CORE PROCESSING LOGIC ---

def process_and_reply(event, client):
    """Main workflow: Analyze -> Search -> Sync -> Notify."""
    text = re.sub(r'<@[A-Z0-9]+>', '', event.get("text", "")).strip()
    if not text: return

    channel = event["channel"]
    client.chat_postMessage(channel=channel, text="🚀 *Deep Search & Verification active...*")

    # A. Analyze text with AI
    data, gemini_error = analyze_text_with_ai(text)
    if data is None or not any([data.get("people"), data.get("domains"), data.get("emails")]):
        client.chat_postMessage(channel=channel, text=f"❌ {gemini_error}")
        return

    token = get_snovio_token()
    raw_found, now, snov_warn_sent = [], datetime.now().strftime("%Y-%m-%d %H:%M"), False
    processed_domains = set()

    # B. Process Specific People (Priority 1)
    for p in data.get("people", []):
        name, domain = p.get("name"), p.get("company_domain")
        role = p.get("role", "Target (Manual Search Needed)")  # Extrae el rol si la IA lo encontró
        if not (token and name and domain): continue

        # 1. Search for the specific individual (Franc-tireur search)
        lead, snov_err = fetch_snovio_by_person(name, domain, token)
        if lead:
            lead["added_date"] = now
            if lead["role"] in ["Specific Target", "Direct Target"] and role:
                lead["role"] = role
            raw_found.append(lead)
        else:
            # VIP FEATURE: Sniping + Verification
            guessed_email = f"{name.split()[0].lower()}@{domain}"
            status = verify_email_snovio(guessed_email, token)

            if status == "valid":
                raw_found.append({
                    "email": guessed_email, "name": name, "role": role,
                    "company_domain": domain, "source": "Verified Sniping", "added_date": now, "linkedin": "N/A"
                })
            else:
                # NEW AGENT FEATURE: LinkedIn Investigation if Sniping fails
                # ---> NUEVO: Pausa preventiva entre búsquedas intensivas
                time.sleep(2)
                linkedin_url = investigate_linkedin_with_ai(name, domain.split('.')[0])

                role_display = f"⭐ {role}"
                link_display = linkedin_url if linkedin_url else "N/A"

                marker_email = f"[PENDING] {guessed_email}"
                raw_found.append({
                    "email": marker_email, "name": name, "role": role_display,
                    "company_domain": domain, "source": f"AI Inference ({status})", "added_date": now,
                    "linkedin": link_display
                })

        # 2. ALWAYS fetch 4 additional leads from that domain (The "Team" quota)
        if domain not in processed_domains:
            leads, _ = fetch_snovio_by_domain(domain, token, limit=4)
            for l in leads:
                l["added_date"] = now
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
        for l in leads:
            l["added_date"] = now
            raw_found.append(l)

        processed_domains.add(domain)

    # D. Direct Emails found in text
    for e in data.get("emails", []):
        email_val = e['email'] if isinstance(e, dict) else e
        if MY_COMPANY not in email_val:
            role_val = e.get("role", "Extraction") if isinstance(e, dict) else "Extraction"
            raw_found.append({"email": email_val, "name": "N/A", "role": role_val,
                              "company_domain": email_val.split('@')[-1], "source": "Chat", "added_date": now,
                              "linkedin": "N/A"})

    # --- SMART SYNC LOGIC ---
    cloud_emails, local_emails = get_cloud_emails(), get_local_emails()
    leads_to_cloud, leads_to_local = [], []

    for l in raw_found:
        email_clean = l['email'].strip().lower()
        if MY_COMPANY in email_clean: continue

        # Check Cloud Sync status
        if email_clean not in cloud_emails:
            leads_to_cloud.append(l)
            cloud_emails.add(email_clean)

        # Check Local Sync status
        if email_clean not in local_emails:
            leads_to_local.append(l)
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
    if os.path.exists(CSV_FILE):
        client.files_upload_v2(channel=channel, file=CSV_FILE, title="Leads Report", initial_comment=msg)
    else:
        client.chat_postMessage(channel=channel, text=f"{msg}\n(Aún no se ha generado ningún archivo CSV local).")


# --- 7. EVENT HANDLERS ---

# ---> NUEVO: Hemos quitado @app.event("message") temporalmente para evitar que
# Slack dispare dos procesos paralelos cuando etiquetas al bot.
@app.event("app_mention")
def handle_app_mention(event, client, say):
    process_and_reply(event, client)


# --- APPLICATION ENTRY POINT ---
if __name__ == "__main__":
    print("⚡️ Slack_Finder: Deep Search & Verification READY | Security: Active")
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    handler.start()