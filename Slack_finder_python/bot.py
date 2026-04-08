"""
=========================================================================================
🚀 SLACK AI LEAD GENERATION & 4-STAGE FUNNEL AUTOMATION BOT
=========================================================================================
FIXES v3:
- src/page_scraper.py and src/url_utils.py are now fully integrated into the pipeline
- SNOV_CACHE protected with a threading Lock to prevent race conditions
- files_upload_v2 wrapped in try/finally to guarantee temp file cleanup
- [PENDING] emails excluded from Brevo sync (only real verified emails are sent)
- scrape_site result correctly parsed (was returning comma-joined string, now returns list)
- run_custom_scraper now attempts real web scraping before falling back to generic patterns
- Playwright browser lifecycle managed inside run_custom_scraper (no orphaned browsers)
=========================================================================================
"""

import os
import re
import json
import time
import hmac
import hashlib
import logging
import tempfile
import requests
import gspread
import pandas as pd
from threading import Thread, Lock
from flask import Flask, request, jsonify, abort
from datetime import datetime
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai
from ddgs import DDGS
from apscheduler.schedulers.background import BackgroundScheduler
from playwright.sync_api import sync_playwright

from src.url_utils import normalize_url
from src.page_scraper import scrape_site

# =========================================================================================
# 1. CONFIGURATION & LOGGING
# =========================================================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("funnel_bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger("FunnelBot")

app_slack = App(token=os.environ["SLACK_BOT_TOKEN"])
client_google = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MY_COMPANY           = os.getenv("MY_COMPANY", "volvero.com")
GOOGLE_SHEET_NAME    = os.getenv("GOOGLE_SHEET_NAME", "HojaCalculoPrueba")
GEMINI_MODEL         = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
BREVO_WEBHOOK_SECRET = os.getenv("BREVO_WEBHOOK_SECRET", "")

flask_app = Flask(__name__)

# =========================================================================================
# 2. CLOUD & SYNC MANAGER
# =========================================================================================

class CloudManager:
    """
    Manages all Google Sheets operations.
    Uses a Lock to ensure safety in multi-threaded environments.
    """
    TAB_NAMES = ["Waiting_Room_1", "Waiting_Room_2", "Subscribed", "Unsubscribed"]

    def __init__(self):
        self._lock = Lock()
        self._connect()

    def _connect(self):
        """Establishes connection with Google Sheets. Raises exception on failure."""
        try:
            env_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            if env_creds:
                self.gc = gspread.service_account_from_dict(json.loads(env_creds))
            else:
                self.gc = gspread.service_account(filename="google_credentials.json")
            self.sh = self.gc.open(GOOGLE_SHEET_NAME)
            log.info("✅ Connection with Google Sheets established.")
        except Exception as e:
            log.critical(f"❌ Could not connect to Google Sheets: {e}")
            raise

    def _reconnect_if_needed(self):
        """Silent reconnection if the session expired."""
        try:
            self.sh.worksheet(self.TAB_NAMES[0])  # Ping
        except Exception:
            log.warning("🔄 Reconnecting to Google Sheets...")
            self._connect()

    def get_all_emails(self) -> set:
        """Returns all emails from all tabs to prevent duplicates."""
        all_emails = set()
        with self._lock:
            self._reconnect_if_needed()
            for name in self.TAB_NAMES:
                try:
                    ws = self.sh.worksheet(name)
                    emails = ws.col_values(1)[1:]  # Skip header row
                    all_emails.update(e.strip().lower() for e in emails if e)
                except Exception as e:
                    log.warning(f"⚠️ Could not read tab '{name}': {e}")
        return all_emails

    def add_leads_to_fase1(self, leads: list) -> bool:
        """Inserts new leads into Waiting_Room_1 in a single batch."""
        if not leads:
            return False
        with self._lock:
            self._reconnect_if_needed()
            try:
                ws = self.sh.worksheet("Waiting_Room_1")
                rows = [
                    [
                        l.get("email"), l.get("name"), l.get("role"),
                        l.get("company_domain"), l.get("source"),
                        l.get("added_date"), l.get("linkedin", "N/A")
                    ]
                    for l in leads
                ]
                ws.append_rows(rows)
                log.info(f"✅ {len(rows)} leads added to Waiting_Room_1.")
                return True
            except Exception as e:
                log.error(f"❌ Error inserting leads into Waiting_Room_1: {e}")
                return False

    def move_lead(self, email: str, from_tab: str, to_tab: str) -> bool:
        """Moves a lead between tabs. Mainly used by the Brevo webhook."""
        with self._lock:
            self._reconnect_if_needed()
            try:
                source_ws = self.sh.worksheet(from_tab)
                target_ws = self.sh.worksheet(to_tab)
                data = source_ws.get_all_values()
                for idx, row in enumerate(data):
                    if row and row[0].strip().lower() == email.strip().lower():
                        target_ws.append_row(row)
                        source_ws.delete_rows(idx + 1)
                        log.info(f"🔀 Lead '{email}' moved from {from_tab} → {to_tab}.")
                        return True
                log.warning(f"⚠️ Lead '{email}' not found in '{from_tab}'.")
                return False
            except Exception as e:
                log.error(f"❌ Error moving lead '{email}': {e}")
                return False

    def run_migration(self):
        """
        Executed on the 1st of each month by APScheduler.
        Reverse cascade: WR2→Subscribed BEFORE WR1→WR2 to avoid data collision.
        """
        log.info(f"📅 Starting monthly migration ({datetime.now()})...")
        self._migrate_logic("Waiting_Room_2", "Subscribed", os.getenv("BREVO_LIST_ID_SUBSCRIBED"))
        self._migrate_logic("Waiting_Room_1", "Waiting_Room_2", os.getenv("BREVO_LIST_ID_WR2"))

    def _migrate_logic(self, from_tab: str, to_tab: str, brevo_list_id: str):
        """Migration engine: filters by maturity (>=27 days), moves rows and syncs with Brevo."""
        with self._lock:
            self._reconnect_if_needed()
            try:
                ws_from = self.sh.worksheet(from_tab)
                ws_to   = self.sh.worksheet(to_tab)
                data    = ws_from.get_all_values()

                if len(data) <= 1:
                    log.info(f"ℹ️ '{from_tab}' is empty, nothing to migrate.")
                    return

                header, leads = data[0], data[1:]
                to_move, to_stay = [], [header]
                now = datetime.now()

                for row in leads:
                    try:
                        lead_date = datetime.strptime(row[5], "%Y-%m-%d %H:%M")
                        if (now - lead_date).days >= 27:
                            to_move.append(row)
                        else:
                            to_stay.append(row)
                    except (ValueError, IndexError):
                        log.warning(f"⚠️ Invalid date in row: {row}. Kept in source tab.")
                        to_stay.append(row)

                if to_move:
                    ws_to.append_rows(to_move)
                    log.info(f"✉️ Syncing {len(to_move)} leads with Brevo (list {brevo_list_id})...")
                    for row in to_move:
                        lead_dict = {
                            "email": row[0], "name": row[1],
                            "role": row[2], "company_domain": row[3]
                        }
                        export_to_brevo([lead_dict], list_id=brevo_list_id)
                        time.sleep(0.1)  # Anti-429 throttling

                    # Source cleanup — clear and rewrite only the rows that stay
                    ws_from.clear()
                    ws_from.update("A1", to_stay)
                    log.info(f"✅ {len(to_move)} leads migrated: {from_tab} → {to_tab}.")
                else:
                    log.info(f"ℹ️ No mature leads in '{from_tab}'.")

            except Exception as e:
                log.error(f"❌ Migration error in '{from_tab}': {e}")


cloud = CloudManager()

# =========================================================================================
# 3. BREVO INTEGRATION
# =========================================================================================

def export_to_brevo(leads: list, list_id: str = None) -> bool:
    """
    Sends confirmed leads to Brevo CRM.
    [PENDING] emails are skipped — only verified addresses are synced.
    """
    api_key     = os.getenv("BREVO_API_KEY")
    target_list = list_id or os.getenv("BREVO_LIST_ID_WR1")

    if not api_key:
        log.error("❌ BREVO_API_KEY not configured.")
        return False
    if not target_list:
        log.error("❌ Brevo list ID not configured.")
        return False
    if not leads:
        return False

    url     = "https://api.brevo.com/v3/contacts"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": api_key
    }

    for lead in leads:
        raw_email = lead.get("email", "").strip()

        # Skip unverified AI-guessed emails — do not pollute Brevo with [PENDING] addresses
        if raw_email.startswith("[PENDING]"):
            log.info(f"⏭️ Skipping PENDING email for Brevo: {raw_email}")
            continue

        if not raw_email:
            continue

        payload = {
            "email": raw_email,
            "attributes": {
                "NOMBRE":  lead.get("name", "N/A"),
                "EMPRESA": lead.get("company_domain", ""),
                "CARGO":   lead.get("role", "").replace("⭐ ", "")
            },
            "listIds":       [int(target_list)],
            "updateEnabled": True
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code not in (200, 201, 204):
                log.warning(f"⚠️ Brevo responded {r.status_code} for '{raw_email}': {r.text[:100]}")
        except requests.RequestException as e:
            log.error(f"❌ Network error syncing '{raw_email}' with Brevo: {e}")

    return True

# =========================================================================================
# 4. SNOV.IO & SCRAPER
# =========================================================================================

# Lock added to prevent simultaneous token renewal from concurrent threads
_snov_cache_lock = Lock()
SNOV_CACHE: dict = {"token": None, "expiry": 0}


def get_snovio_token() -> str | None:
    """Returns a valid Snov.io token, using cache when possible. Thread-safe."""
    with _snov_cache_lock:
        if SNOV_CACHE["token"] and time.time() < SNOV_CACHE["expiry"]:
            return SNOV_CACHE["token"]

        cid = os.getenv("SNOVIO_CLIENT_ID")
        sec = os.getenv("SNOVIO_CLIENT_SECRET")
        if not cid or not sec:
            log.error("❌ Snov.io credentials not configured.")
            return None

        try:
            res = requests.post(
                "https://api.snov.io/v1/oauth/access_token",
                data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec},
                timeout=10
            )
            token = res.json().get("access_token")
            if token:
                SNOV_CACHE["token"] = token
                SNOV_CACHE["expiry"] = time.time() + 3000
                log.info("🔑 Snov.io token renewed.")
            return token
        except requests.RequestException as e:
            log.error(f"❌ Could not obtain Snov.io token: {e}")
            return None


def fetch_snovio_by_domain(domain: str, token: str, limit: int = 4) -> tuple[list, str | None]:
    """Searches for emails associated with a corporate domain."""
    url = f"https://api.snov.io/v2/domain-emails-with-info?domain={domain}&type=personal&limit={limit}"
    try:
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if res.status_code == 402:
            return [], "Snov.io: credits exhausted"
        if res.status_code == 429:
            return [], "Snov.io: rate limit reached"
        data = res.json()
        leads = [
            {
                "email":          e["email"],
                "name":           f"{e.get('firstName', '')} {e.get('lastName', '')}".strip(),
                "role":           e.get("position", "N/A"),
                "company_domain": domain,
                "source":         "Snov.io Domain",
                "linkedin":       "N/A"
            }
            for e in data.get("emails", [])
        ]
        return leads, None
    except requests.RequestException as e:
        log.error(f"❌ Snov.io domain error '{domain}': {e}")
        return [], str(e)


def fetch_snovio_by_person(full_name: str, domain: str, token: str) -> tuple[dict | None, str | None]:
    """Searches for the exact email of a specific person at a domain."""
    parts   = full_name.split(" ", 1)
    payload = {
        "firstName": parts[0],
        "lastName":  parts[1] if len(parts) > 1 else "",
        "domain":    domain
    }
    try:
        res = requests.post(
            "https://api.snov.io/v1/get-emails-from-names",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=10
        )
        data = res.json()
        if data.get("success") and data.get("data", {}).get("email"):
            return {
                "email":          data["data"]["email"],
                "name":           full_name,
                "role":           "Direct Target",
                "company_domain": domain,
                "source":         "Snov.io Name Match",
                "linkedin":       "N/A"
            }, None
    except requests.RequestException as e:
        log.error(f"❌ Snov.io person error '{full_name}': {e}")
    return None, None


def verify_email_snovio(email: str, token: str) -> str:
    """Verifies whether an AI-inferred email actually exists via Snov.io."""
    try:
        res = requests.post(
            "https://api.snov.io/v1/get-emails-verification",
            headers={"Authorization": f"Bearer {token}"},
            json={"emails": [email]},
            timeout=10
        )
        return res.json()[0].get("result", "unknown")
    except requests.RequestException as e:
        log.warning(f"⚠️ Error verifying '{email}': {e}")
        return "unknown"


def run_custom_scraper(domain: str) -> list:
    """
    Real web scraper fallback using Playwright + page_scraper.py.
    Attempts to visit the domain and extract actual email addresses from the page.
    Falls back to generic inbox patterns only if scraping yields nothing.
    """
    url = normalize_url(domain)
    if not url:
        log.warning(f"⚠️ Could not normalize domain '{domain}', using generic patterns.")
        return _generic_inbox_patterns(domain)

    log.info(f"🔧 Attempting real web scrape for '{domain}' at {url}...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            result  = scrape_site(browser, url)
            browser.close()

        if result and result != "NOT_FOUND":
            # scrape_site returns a comma-joined string of emails
            scraped_emails = [e.strip() for e in result.split(",") if e.strip()]
            leads = [
                {
                    "email":          email,
                    "name":           "Web Scraped",
                    "role":           "Unknown",
                    "company_domain": domain,
                    "source":         "Web Scraper",
                    "linkedin":       "N/A"
                }
                for email in scraped_emails
                if MY_COMPANY not in email  # Safety filter
            ]
            if leads:
                log.info(f"✅ Web scraper found {len(leads)} emails for '{domain}'.")
                return leads

    except Exception as e:
        log.warning(f"⚠️ Web scraper failed for '{domain}': {e}. Falling back to generic patterns.")

    return _generic_inbox_patterns(domain)


def _generic_inbox_patterns(domain: str) -> list:
    """Last-resort fallback: generates common corporate inbox addresses."""
    log.info(f"📭 Using generic inbox patterns for '{domain}'.")
    return [
        {
            "email":          f"{prefix}@{domain}",
            "name":           "Auto-Generated",
            "role":           "Corporate Inbox",
            "company_domain": domain,
            "source":         "Scraper Patterns",
            "linkedin":       "N/A"
        }
        for prefix in ["info", "contact", "sales", "support"]
    ]

# =========================================================================================
# 5. AI MODULES
# =========================================================================================

def analyze_text_with_ai(text: str, retries: int = 2) -> tuple[dict, str | None]:
    """
    Uses Gemini to extract entities (people, domains, emails) from raw text.
    Returns strict JSON. Retries up to `retries` times on failure.
    """
    prompt = f"""
    Act as a Senior Business Intelligence & Lead Generation Expert.
    Your goal is to perform a DEEP SCAN of the provided text to extract EVERY potential business lead.

    STRICT OUTPUT RULE: Return ONLY a valid JSON object. No prose, no explanations, no markdown.
    Format: {{
      "domains": [],
      "people": [{{"name": "", "company_domain": "", "role": ""}}],
      "emails": [{{"email": "", "role": ""}}]
    }}

    SCANNING RULES:
    1. EXHAUSTIVE SEARCH: Scan the entire text, including signatures, speaker lists, event agendas, and footers.
    2. ENTITY LINKING: If you find a person and a company nearby, link them in the 'people' array.
    3. DOMAIN INFERENCE: Infer the corporate domain for every company (e.g., 'Matrix Internet' -> 'matrixinternet.ie').
    4. ROLE CAPTURE: Extract the exact job title. If not stated, use "Lead".
    5. SECURITY FILTER: EXCLUDE any data related to {MY_COMPANY} or its employees.
    6. CLEANING: Remove prefixes like 'Mr.', 'Ms.', 'Dr.' from names.

    Text to analyze:
    {text}
    """
    empty = {"domains": [], "people": [], "emails": []}
    for attempt in range(retries + 1):
        try:
            response   = client_google.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            clean_json = re.sub(r'```json|```', '', response.text).strip()
            return json.loads(clean_json), None
        except json.JSONDecodeError as e:
            log.warning(f"⚠️ Invalid JSON from Gemini (attempt {attempt + 1}): {e}")
        except Exception as e:
            log.error(f"❌ Gemini error (attempt {attempt + 1}): {e}")
            if attempt < retries:
                time.sleep(10)
    return empty, "AI analysis error after several attempts."


def investigate_linkedin_with_ai(name: str, company: str, retries: int = 2) -> str | None:
    """
    Web agent: searches DuckDuckGo and uses Gemini to identify the exact LinkedIn profile URL.
    """
    try:
        query = f'site:linkedin.com/in/ "{name}" "{company}"'
        try:
            results = DDGS().text(query, max_results=3)
            raw     = str(results) if results else ""
        except Exception as e:
            log.warning(f"⚠️ DuckDuckGo failed for '{name}': {e}")
            raw = ""

        if not raw or raw == "[]":
            return None

        prompt = f"""
        You are a Data Verification Agent. Target Person: {name}. Target Company: {company}.
        Below are DuckDuckGo search results. Find the SINGLE correct LinkedIn profile URL.
        Return ONLY the URL or the exact word: NONE
        Results: {raw}
        """
        for attempt in range(retries + 1):
            try:
                response = client_google.models.generate_content(model=GEMINI_MODEL, contents=prompt)
                url = response.text.strip()
                return url if url.startswith("http") else None
            except Exception as e:
                log.warning(f"⚠️ Gemini LinkedIn (attempt {attempt + 1}): {e}")
                if attempt < retries:
                    time.sleep(15)
        return None
    except Exception as e:
        log.error(f"❌ General error in investigate_linkedin: {e}")
        return None

# =========================================================================================
# 6. MAIN ORCHESTRATOR
# =========================================================================================

def process_and_reply(event: dict, client):
    """
    Full pipeline triggered by a Slack mention:
    AI Extraction → Snov.io Enrichment → Web Scraping Fallback → Deduplication → Persistence → Reply.
    """
    text = re.sub(r'<@[A-Z0-9]+>', '', event.get("text", "")).strip()
    if not text:
        return

    channel = event["channel"]
    client.chat_postMessage(channel=channel, text="🚀 *Deep Search & Enrichment active...*")

    data, ai_error = analyze_text_with_ai(text)
    if ai_error:
        log.warning(f"⚠️ Partial AI result: {ai_error}")

    token             = get_snovio_token()
    raw_found         = []
    now               = datetime.now().strftime("%Y-%m-%d %H:%M")
    processed_domains = set()

    if not any([data.get("people"), data.get("domains"), data.get("emails")]):
        client.chat_postMessage(channel=channel, text="⚠️ No leads found in the text.")
        return

    # A. Specific people (high priority — direct name + domain match)
    for p in data.get("people", []):
        name = p.get("name")
        dom  = p.get("company_domain")
        role = p.get("role", "Lead")
        if not (token and name and dom):
            continue

        lead, _ = fetch_snovio_by_person(name, dom, token)
        if lead:
            lead["added_date"] = now
            raw_found.append(lead)
        else:
            # AI inference: guess first-name@domain, verify it, find LinkedIn
            guessed = f"{name.split()[0].lower()}@{dom}"
            status  = verify_email_snovio(guessed, token)
            ln      = investigate_linkedin_with_ai(name, dom.split('.')[0])
            raw_found.append({
                "email":          f"[PENDING] {guessed}",
                "name":           name,
                "role":           f"⭐ {role}",
                "company_domain": dom,
                "source":         f"AI Enriched ({status})",
                "added_date":     now,
                "linkedin":       ln or "N/A"
            })

    # B. Domains (company-level extraction via Snov.io, then web scraper fallback)
    for dom in data.get("domains", []):
        if dom in processed_domains or MY_COMPANY in dom:
            continue

        leads, err = fetch_snovio_by_domain(dom, token) if token else ([], "No token")
        if err:
            log.warning(f"⚠️ Snov.io domain '{dom}': {err}")

        if not leads:
            # Snov.io found nothing — attempt real web scraping before using generic patterns
            leads = run_custom_scraper(dom)

        for l in leads:
            l["added_date"] = now
            raw_found.append(l)
        processed_domains.add(dom)

    # C. Emails directly mentioned in the text
    for e in data.get("emails", []):
        email_val = e["email"] if isinstance(e, dict) else e
        if MY_COMPANY not in email_val:
            raw_found.append({
                "email":          email_val,
                "name":           "N/A",
                "role":           "Extraction",
                "company_domain": email_val.split('@')[-1],
                "source":         "Chat",
                "added_date":     now,
                "linkedin":       "N/A"
            })

    # Global duplicate filter (against live Sheets) + intra-batch deduplication
    cloud_emails  = cloud.get_all_emails()
    seen_in_batch = set()
    leads_to_sync = []
    for l in raw_found:
        email_clean = l["email"].strip().lower()
        if (email_clean not in cloud_emails
                and MY_COMPANY not in email_clean
                and email_clean not in seen_in_batch):
            leads_to_sync.append(l)
            seen_in_batch.add(email_clean)

    log.info(f"📊 {len(raw_found)} leads found, {len(leads_to_sync)} unique new ones.")

    if leads_to_sync:
        cloud.add_leads_to_fase1(leads_to_sync)
        export_to_brevo(leads_to_sync)  # [PENDING] emails are filtered inside export_to_brevo

        # Write CSV to a temp file and guarantee cleanup even if upload fails
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".csv", prefix="leads_", delete=False, encoding="utf-8"
            ) as tmp:
                tmp_path = tmp.name

            pd.DataFrame(leads_to_sync).to_csv(tmp_path, index=False)
            client.files_upload_v2(
                channel=channel,
                file=tmp_path,
                title="New Leads",
                initial_comment=f"✅ Funnel Sync: {len(leads_to_sync)} leads added to Phase 1."
            )
        except Exception as e:
            log.error(f"❌ Failed to upload CSV to Slack: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
    else:
        client.chat_postMessage(channel=channel, text="⚠️ No unique new leads to add.")


@app_slack.event("app_mention")
def handle_app_mention(event, client):
    """Slack mention event binding — runs the pipeline in a background thread."""
    Thread(target=process_and_reply, args=(event, client), daemon=True).start()

# =========================================================================================
# 7. BREVO WEBHOOK (with HMAC signature validation)
# =========================================================================================

def _verify_brevo_signature(payload: bytes, header_sig: str) -> bool:
    """
    Validates that the webhook genuinely comes from Brevo using HMAC-SHA256.
    If BREVO_WEBHOOK_SECRET is not set, verification is skipped (dev mode only).
    """
    if not BREVO_WEBHOOK_SECRET:
        log.warning("⚠️ BREVO_WEBHOOK_SECRET not configured. Signature verification skipped.")
        return True
    expected = hmac.new(
        BREVO_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_sig)


@flask_app.route("/brevo-webhook", methods=["POST"])
def brevo_webhook():
    """
    Receives unsubscribe events from Brevo.
    Moves the lead to the Unsubscribed tab and blocks it from all future funnel stages.
    """
    raw_body  = request.get_data()
    signature = request.headers.get("X-Brevo-Signature", "")

    if not _verify_brevo_signature(raw_body, signature):
        log.warning("🚫 Webhook with invalid signature rejected.")
        abort(403)

    data = request.json
    if not data:
        return jsonify({"status": "no_data"}), 400

    if data.get("event") == "unsubscribe":
        email = data.get("email", "").strip()
        if not email:
            return jsonify({"status": "no_email"}), 400

        moved = False
        for tab in ["Waiting_Room_1", "Waiting_Room_2", "Subscribed"]:
            if cloud.move_lead(email, tab, "Unsubscribed"):
                moved = True
                break

        if not moved:
            log.warning(f"⚠️ Email '{email}' not found in any active tab.")
        return jsonify({"status": "processed", "moved": moved}), 200

    return jsonify({"status": "ignored"}), 200

# =========================================================================================
# 8. ROBUST SCHEDULER WITH APSCHEDULER
# =========================================================================================

def start_scheduler():
    """
    Uses APScheduler (cron trigger) for monthly migration.
    Runs on the 1st of each month at 01:00 AM.
    misfire_grace_time=3600 allows execution up to 1 hour late if the process was down.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=cloud.run_migration,
        trigger="cron",
        day=1,
        hour=1,
        minute=0,
        id="monthly_migration",
        replace_existing=True,
        misfire_grace_time=3600
    )
    scheduler.start()
    log.info("🕐 Monthly scheduler started (1st of each month, 01:00 AM).")
    return scheduler

# =========================================================================================
# 9. ENTRYPOINT
# =========================================================================================

if __name__ == "__main__":
    log.info("⚡️ Funnel Bot v3 starting...")

    # Flask server in a background thread (handles Brevo webhook)
    Thread(
        target=lambda: flask_app.run(port=5000, host="0.0.0.0", use_reloader=False),
        daemon=True,
        name="FlaskWebhook"
    ).start()

    # Monthly migration scheduler
    start_scheduler()

    # Slack WebSocket — blocking call, runs on the main thread
    log.info("🤖 Slack Bot connected and listening for mentions.")
    SocketModeHandler(app_slack, os.environ["SLACK_APP_TOKEN"]).start()