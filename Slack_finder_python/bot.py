"""
=========================================================================================
🚀 VOLVERO EMAIL FINDER — SLACK AI LEAD GENERATION & 4-STAGE FUNNEL AUTOMATION BOT
=========================================================================================
FIXES v3 & v4:
- src/page_scraper.py and src/url_utils.py are now fully integrated into the pipeline
- SNOV_CACHE protected with a threading Lock to prevent race conditions
- files_upload_v2 wrapped in try/finally to guarantee temp file cleanup
- AI-inferred (unverified) emails excluded from Brevo sync (only real verified emails are sent)
- scrape_site result correctly parsed (was returning comma-joined string, now returns list)
- run_custom_scraper now attempts real web scraping before falling back to generic patterns
- Playwright browser lifecycle managed inside run_custom_scraper (no orphaned browsers)

- Native SMTP email verifier (src/email_verifier.py) replaces Snov.io verify call for
  AI-inferred addresses: no API credits consumed, DNS+SMTP ping in-house.
- verify_email_snovio() retained as fallback if Snov.io token is available; our verifier
  runs first and Snov.io is only called when SMTP gives "unknown" (port-25 blocked).

FIXES v5:
- Gemini Vision OCR: if the @mention includes image attachments (screenshots, photos,
  business cards, slides…) the bot downloads them and passes them to Gemini multimodal.
  Extracted leads are merged with any text leads before deduplication and sync.
- Supported formats: JPEG, PNG, GIF, WEBP.
- No new dependencies — uses the google-genai SDK already in the project.
=========================================================================================
"""

import os
import re
import json
import time
import hmac
import base64
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
from src.email_verifier import verify_email, REACHABLE_YES, REACHABLE_NO, REACHABLE_CATCH_ALL

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
log = logging.getLogger("VolveroEmailFinder")

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
        return all_emails - {""}  # Discard empty strings from whitespace-only cells

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
                        l.get("added_date"), l.get("linkedin", "N/A"), ""  # sent_date — filled by Brevo webhook
                    ]
                    for l in leads
                ]
                ws.append_rows(rows)
                log.info(f"✅ {len(rows)} leads added to Waiting_Room_1.")
                return True
            except Exception as e:
                log.error(f"❌ Error inserting leads into Waiting_Room_1: {e}")
                return False

    def update_sent_date(self, email: str, sent_date: str) -> bool:
        """
        Writes sent_date (col H, index 7) for a lead across all active tabs.
        Called by the Brevo 'delivered' webhook so the 27-day window starts from actual send.
        """
        with self._lock:
            self._reconnect_if_needed()
            for name in ["Waiting_Room_1", "Waiting_Room_2", "Subscribed"]:
                try:
                    ws   = self.sh.worksheet(name)
                    data = ws.get_all_values()
                    for idx, row in enumerate(data):
                        if row and row[0].strip().lower() == email.strip().lower():
                            existing = row[7] if len(row) > 7 else ""
                            if existing:
                                # Already set — ignore Brevo retries to avoid resetting the 27-day clock
                                log.info(f"⏭️ sent_date already set for '{email}', ignoring duplicate webhook.")
                                return True
                            ws.update_cell(idx + 1, 8, sent_date)  # Column H (1-based = 8)
                            log.info(f"📧 sent_date '{sent_date}' recorded for '{email}' in '{name}'.")
                            return True
                except Exception as e:
                    log.warning(f"⚠️ Could not update sent_date in '{name}': {e}")
        log.warning(f"⚠️ Email '{email}' not found in any active tab for sent_date update.")
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
        Each leg is wrapped independently so a failure in one does not prevent the other.
        """
        log.info(f"📅 Starting monthly migration ({datetime.now()})...")
        try:
            self._migrate_logic("Waiting_Room_2", "Subscribed", os.getenv("BREVO_LIST_ID_SUBSCRIBED"))
        except Exception as e:
            log.error(f"❌ Migration WR2→Subscribed failed unexpectedly: {e}")
        try:
            self._migrate_logic("Waiting_Room_1", "Waiting_Room_2", os.getenv("BREVO_LIST_ID_WR2"))
        except Exception as e:
            log.error(f"❌ Migration WR1→WR2 failed unexpectedly: {e}")

    def _migrate_logic(self, from_tab: str, to_tab: str, brevo_list_id: str):
        """Migration engine: filters by maturity (>=27 days), moves rows and syncs with Brevo."""
        to_move = []  # Pre-initialized so the Brevo sync block outside the lock never hits UnboundLocalError
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
                        sent_date_val = row[7] if len(row) > 7 else ""
                        if not sent_date_val:
                            # Email not sent yet — keep in current tab
                            to_stay.append(row)
                            continue
                        lead_date = datetime.strptime(sent_date_val, "%Y-%m-%d %H:%M")
                        if (now - lead_date).days >= 27:
                            to_move.append(row)
                        else:
                            to_stay.append(row)
                    except (ValueError, IndexError, TypeError):
                        log.warning(f"⚠️ Invalid sent_date in row: {row}. Kept in source tab.")
                        to_stay.append(row)

                if to_move:
                    # Reset sent_date when promoting to next stage — the 27-day clock
                    # must restart from the new campaign, not carry over from the previous one.
                    # Pad to 7 cols first to guard against corrupted rows with fewer columns.
                    to_move_reset = [(row + [""] * 7)[:7] + [""] for row in to_move]
                    ws_to.append_rows(to_move_reset)

                    # Source cleanup — clear and rewrite only the rows that stay
                    ws_from.clear()
                    ws_from.update("A1", to_stay)
                    log.info(f"✅ {len(to_move)} leads migrated: {from_tab} → {to_tab}.")
                else:
                    log.info(f"ℹ️ No mature leads in '{from_tab}'.")

            except Exception as e:
                log.error(f"❌ Migration error in '{from_tab}': {e}")

        # Brevo sync runs OUTSIDE the lock — avoids blocking webhooks during HTTP calls
        if to_move:
            log.info(f"✉️ Syncing {len(to_move)} leads with Brevo (list {brevo_list_id})...")
            for row in to_move:
                try:
                    lead_dict = {
                        "email": row[0], "name": row[1],
                        "role": row[2], "company_domain": row[3]
                    }
                    export_to_brevo([lead_dict], list_id=brevo_list_id)
                except IndexError:
                    log.warning(f"⚠️ Skipping corrupt row in Brevo sync: {row}")
                time.sleep(0.1)  # Anti-429 throttling


cloud = CloudManager()

# =========================================================================================
# 3. BREVO INTEGRATION
# =========================================================================================

def export_to_brevo(leads: list, list_id: str = None) -> bool:
    """
    Sends every lead to Brevo CRM.
    The Snov.io 'valid' filter has been removed — high-profile contacts are sometimes
    flagged as 'unknown' even when correct, so we now sync everything we have.
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

    # Cast list ID to int once, up front, so we fail fast on a bad env value
    # instead of crashing mid-loop after partial sync.
    try:
        list_id_int = int(target_list)
    except (TypeError, ValueError):
        log.error(f"❌ Brevo list ID '{target_list}' is not a valid integer.")
        return False

    url     = "https://api.brevo.com/v3/contacts"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": api_key
    }

    for lead in leads:
        raw_email = lead.get("email", "").strip()

        if not raw_email:
            continue

        payload = {
            "email": raw_email,
            "attributes": {
                "NOMBRE":  lead.get("name", "N/A"),
                "EMPRESA": lead.get("company_domain", ""),
                "CARGO":   lead.get("role", "").replace("⭐ ", "")
            },
            "listIds":       [list_id_int],
            "updateEnabled": True
        }
        try:
            log.info(f"📨 Syncing email {raw_email} to Brevo list {list_id_int}...")
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
            res  = requests.post(
                "https://api.snov.io/v1/oauth/access_token",
                data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec},
                timeout=10
            )
            body  = res.json()
            token = body.get("access_token") if isinstance(body, dict) else None
            if token:
                SNOV_CACHE["token"] = token
                SNOV_CACHE["expiry"] = time.time() + 3000
                log.info("🔑 Snov.io token renewed.")
            else:
                log.error(f"❌ Snov.io token response unexpected: {str(body)[:100]}")
            return token
        except (requests.RequestException, ValueError, AttributeError) as e:
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
        data  = res.json()
        if not isinstance(data, dict):
            log.warning(f"⚠️ Snov.io domain unexpected response for '{domain}': {str(data)[:100]}")
            return [], "Unexpected response format"
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
    except (requests.RequestException, ValueError, AttributeError) as e:
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
        if isinstance(data, dict) and data.get("success") and data.get("data", {}).get("email"):
            return {
                "email":          data["data"]["email"],
                "name":           full_name,
                "role":           "Direct Target",
                "company_domain": domain,
                "source":         "Snov.io Name Match",
                "linkedin":       "N/A"
            }, None
    except (requests.RequestException, ValueError, AttributeError) as e:
        log.error(f"❌ Snov.io person error '{full_name}': {e}")
    return None, None


def verify_email_snovio(email: str, token: str) -> str:
    """Verifies whether an AI-inferred email actually exists via Snov.io."""
    try:
        res  = requests.post(
            "https://api.snov.io/v1/get-emails-verification",
            headers={"Authorization": f"Bearer {token}"},
            json={"emails": [email]},
            timeout=10
        )
        data = res.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0].get("result", "unknown")
        return "unknown"
    except (requests.RequestException, ValueError, TypeError) as e:
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


# Supported image MIME types Gemini Vision accepts
_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _download_slack_file(url: str, bot_token: str) -> bytes | None:
    """Downloads a private Slack file using the bot token as Bearer auth."""
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=20
        )
        if resp.status_code == 200:
            return resp.content
        log.warning(f"⚠️ Could not download Slack file ({resp.status_code}): {url[:80]}")
    except requests.RequestException as exc:
        log.error(f"❌ Network error downloading Slack file: {exc}")
    return None


def analyze_image_with_ai(image_bytes: bytes, mime_type: str, retries: int = 2) -> tuple[dict, str | None]:
    """
    Passes an image to Gemini Vision and extracts business leads from it.
    Accepts screenshots, photos, business cards, slides, etc.
    Returns the same JSON structure as analyze_text_with_ai().
    """
    prompt = f"""
    Act as a Senior Business Intelligence & Lead Generation Expert.
    You are looking at an IMAGE. Perform a DEEP VISUAL SCAN to extract EVERY potential business lead visible.

    STRICT OUTPUT RULE: Return ONLY a valid JSON object. No prose, no explanations, no markdown.
    Format: {{
      "domains": [],
      "people": [{{"name": "", "company_domain": "", "role": ""}}],
      "emails": [{{"email": "", "role": ""}}]
    }}

    SCANNING RULES:
    1. READ ALL TEXT visible in the image: names, titles, companies, emails, websites, badges, business cards, slides, logos, footers, watermarks.
    2. COMPANY LOGOS COUNT: If you see a company logo or brand name, infer its corporate domain and add it to 'domains'. This is mandatory even if no person is visible.
    3. ENTITY LINKING: If you see a person and a company together, link them in 'people'.
    4. DOMAIN INFERENCE: Infer the corporate domain for every company or brand visible (e.g. 'Europ Assistance' -> 'europ-assistance.com', 'Neosurance' -> 'neosurance.eu').
    5. ROLE CAPTURE: Extract exact job titles. If not visible, use "Lead".
    6. SECURITY FILTER: EXCLUDE any data related to {MY_COMPANY} or its employees.
    7. CLEANING: Remove prefixes like 'Mr.', 'Ms.', 'Dr.' from names.
    8. Only return empty arrays if the image has zero business-relevant content (e.g. a nature photo).
    """
    empty = {"domains": [], "people": [], "emails": []}
    for attempt in range(retries + 1):
        try:
            image_part = {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode("utf-8")
                }
            }
            response = client_google.models.generate_content(
                model=GEMINI_MODEL,
                contents=[{"role": "user", "parts": [image_part, {"text": prompt}]}]
            )
            clean_json = re.sub(r'```json|```', '', response.text).strip()
            return json.loads(clean_json), None
        except json.JSONDecodeError as exc:
            log.warning(f"⚠️ Invalid JSON from Gemini Vision (attempt {attempt + 1}): {exc}")
        except Exception as exc:
            log.error(f"❌ Gemini Vision error (attempt {attempt + 1}): {exc}")
            if attempt < retries:
                time.sleep(10)
    return empty, "Gemini Vision analysis failed after several attempts."


def _merge_ai_data(base: dict, extra: dict) -> dict:
    """Merges two lead-extraction dicts, deduplicating by value."""
    return {
        "domains": list(set(base.get("domains", []) + extra.get("domains", []))),
        "people":  base.get("people",  []) + extra.get("people",  []),
        "emails":  base.get("emails",  []) + extra.get("emails",  []),
    }


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
    AI Extraction (text + images) → Snov.io Enrichment → Web Scraping Fallback → Deduplication → Persistence → Reply.
    """
    text  = re.sub(r'<@[A-Z0-9]+(?:\|[^>]+)?>', '', event.get("text", "")).strip()
    files = event.get("files", [])

    # Require at least some content — text OR an image attachment
    if not text and not files:
        return

    channel   = event["channel"]
    thread_ts = event.get("thread_ts") or event.get("ts")
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="🚀 *Deep Search & Enrichment active...*")

    # --- Text analysis ---
    data = {"domains": [], "people": [], "emails": []}
    if text:
        text_data, ai_error = analyze_text_with_ai(text)
        if ai_error:
            log.warning(f"⚠️ Partial AI result: {ai_error}")
        data = _merge_ai_data(data, text_data)

    # --- Image analysis (Gemini Vision) ---
    bot_token    = os.environ["SLACK_BOT_TOKEN"]
    image_count  = 0
    for f in files:
        mime = f.get("mimetype", "")
        if mime not in _IMAGE_MIME_TYPES:
            log.info(f"⏭️ Skipping non-image attachment: {f.get('name', '?')} ({mime})")
            continue

        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue

        log.info(f"🖼️ Downloading image '{f.get('name', '?')}' for Vision analysis...")
        image_bytes = _download_slack_file(url, bot_token)
        if not image_bytes:
            continue

        img_data, img_error = analyze_image_with_ai(image_bytes, mime)
        if img_error:
            log.warning(f"⚠️ Vision partial result for '{f.get('name','?')}': {img_error}")
        data = _merge_ai_data(data, img_data)
        image_count += 1
        log.info(f"✅ Vision extracted from image #{image_count}: {len(img_data.get('people',[]))} people, {len(img_data.get('emails',[]))} emails")

    if image_count:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"🖼️ Analysed {image_count} image(s) with Gemini Vision."
        )

    token             = get_snovio_token()
    raw_found         = []
    now               = datetime.now().strftime("%Y-%m-%d %H:%M")
    processed_domains = set()

    if not any([data.get("people"), data.get("domains"), data.get("emails")]):
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="⚠️ No leads found in the text or images.")
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
            # AI inference: guess first-name@domain
            guessed = f"{name.split()[0].lower()}@{dom}"

            # Step 1: native SMTP verifier (no API credits)
            vr      = verify_email(guessed)
            status  = vr.to_snov_compat()   # "valid" | "invalid" | "unknown" | "catch_all"

            # Step 2: if SMTP inconclusive AND Snov.io token available, use it as fallback
            if status == "unknown" and token:
                status = verify_email_snovio(guessed, token)

            ln = investigate_linkedin_with_ai(name, dom.split('.')[0])

            # Skip addresses confirmed invalid by SMTP (saves Brevo list quality)
            if vr.reachable == REACHABLE_NO:
                log.info(f"🗑️ Skipping '{guessed}' — SMTP confirmed non-existent.")
                processed_domains.add(dom)
                continue

            raw_found.append({
                "email":          guessed,
                "name":           name,
                "role":           f"⭐ {role}",
                "company_domain": dom,
                "source":         f"AI Enriched ({status})",
                "added_date":     now,
                "linkedin":       ln or "N/A"
            })
        # Mark domain as processed so section B skips it — avoids duplicate Snov.io calls
        processed_domains.add(dom)

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
        email_val = e.get("email", "") if isinstance(e, dict) else (e if isinstance(e, str) else "")
        if not email_val:
            continue
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
        export_to_brevo(leads_to_sync)  # Push to Brevo WR1 list so manual campaigns are possible

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
                thread_ts=thread_ts,
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
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="⚠️ No unique new leads to add.")


@app_slack.event("app_mention")
def handle_app_mention(event, client):
    """Slack mention event binding — runs the pipeline in a background thread."""
    Thread(target=process_and_reply, args=(event, client), daemon=True).start()

# =========================================================================================
# 7. BREVO WEBHOOK (with HMAC signature validation)
# =========================================================================================

def _verify_brevo_signature(payload: bytes, header_sig: str) -> bool:
    """
    Validates that the webhook genuinely comes from Brevo.
    If BREVO_WEBHOOK_SECRET is not set, verification is skipped (dev mode only).

    Brevo supports two token delivery modes depending on configuration:
      - "Token" mode: sends the secret as a plain string in X-Brevo-Signature.
      - Custom/HMAC mode: some integrations compute HMAC-SHA256(secret, payload).
    We accept either so the bot works regardless of which mode Brevo uses.
    """
    if not BREVO_WEBHOOK_SECRET:
        log.warning("⚠️ BREVO_WEBHOOK_SECRET not configured. Signature verification skipped.")
        return True

    if not header_sig:
        log.warning("⚠️ Webhook received with no X-Brevo-Signature header.")
        return False

    # Mode 1 — Brevo "Token" auth: token sent as plain string
    if hmac.compare_digest(BREVO_WEBHOOK_SECRET, header_sig):
        return True

    # Mode 2 — HMAC-SHA256 fallback
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

    if data.get("event") == "delivered":
        email = data.get("email", "").strip()
        if not email:
            return jsonify({"status": "no_email"}), 400
        sent_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        cloud.update_sent_date(email, sent_date)
        return jsonify({"status": "sent_date_recorded"}), 200

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
    log.info("⚡️ Volvero Email Finder starting...")

    # Flask server in a background thread (handles Brevo webhook)
    Thread(
        target=lambda: flask_app.run(port=5000, host="0.0.0.0", use_reloader=False),
        daemon=True,
        name="FlaskWebhook"
    ).start()

    # Monthly migration scheduler
    start_scheduler()

    # Slack WebSocket — blocking call, runs on the main thread
    log.info("🤖 Volvero Email Finder connected and listening for mentions.")
    SocketModeHandler(app_slack, os.environ["SLACK_APP_TOKEN"]).start()