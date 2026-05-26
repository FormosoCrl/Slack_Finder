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
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai
from ddgs import DDGS
from apscheduler.schedulers.background import BackgroundScheduler
from playwright.sync_api import sync_playwright

from src.url_utils import normalize_url
from src.page_scraper import scrape_site
from src.email_verifier import verify_email, REACHABLE_YES, REACHABLE_NO, REACHABLE_CATCH_ALL, ROLE_ACCOUNTS
from src.lead_verifier import verify_lead_email

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
MIGRATION_STATE_FILE = Path(os.getenv("MIGRATION_STATE_FILE", "/var/lib/volvero/last_migration.json"))

flask_app = Flask(__name__)

# =========================================================================================
# 2. CLOUD & SYNC MANAGER
# =========================================================================================

class CloudManager:
    """
    Manages all Google Sheets operations.
    Uses a Lock to ensure safety in multi-threaded environments.
    """
    TAB_NAMES        = ["Waiting_Room_1", "Waiting_Room_2", "Subscribed", "Unsubscribed"]
    TAB_VERIFY_QUEUE = "TO_VERIFY"

    # TO_VERIFY column layout (1-based in Sheets, 0-based in Python list)
    # Col A: email | B: name | C: role | D: company_domain | E: source
    # Col F: added_to_queue_date | G: attempt_count | H: linkedin
    _VERIFY_HEADER = ["email", "name", "role", "company_domain", "source",
                      "added_to_queue_date", "attempt_count", "linkedin"]

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
        """Returns all emails from all tabs (including TO_VERIFY) to prevent duplicates."""
        all_emails = set()
        all_tabs   = self.TAB_NAMES + [self.TAB_VERIFY_QUEUE]
        with self._lock:
            self._reconnect_if_needed()
            for name in all_tabs:
                try:
                    ws     = self.sh.worksheet(name)
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

    # ------------------------------------------------------------------
    # TO_VERIFY QUEUE  —  pending emails awaiting daily verification
    # ------------------------------------------------------------------

    def add_leads_to_verify_queue(self, leads: list) -> bool:
        """
        Appends unknown leads to the TO_VERIFY queue tab.
        Creates the sheet + header row automatically if it doesn't exist yet.
        """
        if not leads:
            return False
        with self._lock:
            self._reconnect_if_needed()
            try:
                # Create the tab if it doesn't exist yet
                try:
                    ws = self.sh.worksheet(self.TAB_VERIFY_QUEUE)
                except gspread.exceptions.WorksheetNotFound:
                    ws = self.sh.add_worksheet(
                        title=self.TAB_VERIFY_QUEUE, rows=1000, cols=len(self._VERIFY_HEADER)
                    )
                    ws.append_row(self._VERIFY_HEADER)
                    log.info(f"✅ TO_VERIFY tab created automatically.")

                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                rows = [
                    [
                        l.get("email", ""),
                        l.get("name", ""),
                        l.get("role", ""),
                        l.get("company_domain", ""),
                        l.get("source", ""),
                        l.get("added_date", now),
                        0,                           # attempt_count starts at 0
                        l.get("linkedin", "N/A"),
                    ]
                    for l in leads
                ]
                ws.append_rows(rows)
                log.info(f"✅ {len(rows)} unknown leads added to TO_VERIFY queue.")
                return True
            except Exception as exc:
                log.error(f"❌ Error inserting leads into TO_VERIFY: {exc}")
                return False

    def get_verify_queue(self) -> list:
        """
        Returns all rows from TO_VERIFY as a list of dicts,
        sorted by added_to_queue_date ascending (FIFO).
        Each dict also carries '_row_index' (1-based Sheets row number).
        """
        with self._lock:
            self._reconnect_if_needed()
            try:
                ws   = self.sh.worksheet(self.TAB_VERIFY_QUEUE)
                data = ws.get_all_values()
            except gspread.exceptions.WorksheetNotFound:
                return []
            except Exception as exc:
                log.error(f"❌ Could not read TO_VERIFY: {exc}")
                return []

        if len(data) <= 1:
            return []

        header = self._VERIFY_HEADER
        rows   = []
        for idx, row in enumerate(data[1:], start=2):   # start=2 → Sheets 1-based + skip header
            # Pad row to full width in case some trailing cells are empty
            padded = (row + [""] * len(header))[:len(header)]
            d = dict(zip(header, padded))
            d["_row_index"] = idx
            try:
                d["attempt_count"] = int(d.get("attempt_count", 0) or 0)
            except (ValueError, TypeError):
                d["attempt_count"] = 0
            rows.append(d)

        # FIFO: oldest date first; rows without a date go last
        def _date_key(r):
            try:
                return datetime.strptime(r["added_to_queue_date"], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                return datetime.max

        return sorted(rows, key=_date_key)

    def rewrite_verify_queue(self, rows: list) -> bool:
        """
        Replaces the entire TO_VERIFY sheet content with the provided rows.
        Call after processing to persist deletions and attempt-count updates.
        `rows` should be a list of dicts with keys matching _VERIFY_HEADER.
        """
        with self._lock:
            self._reconnect_if_needed()
            try:
                try:
                    ws = self.sh.worksheet(self.TAB_VERIFY_QUEUE)
                except gspread.exceptions.WorksheetNotFound:
                    ws = self.sh.add_worksheet(
                        title=self.TAB_VERIFY_QUEUE, rows=1000, cols=len(self._VERIFY_HEADER)
                    )

                new_data = [self._VERIFY_HEADER]
                for r in rows:
                    new_data.append([
                        r.get("email", ""),
                        r.get("name", ""),
                        r.get("role", ""),
                        r.get("company_domain", ""),
                        r.get("source", ""),
                        r.get("added_to_queue_date", ""),
                        str(r.get("attempt_count", 0)),
                        r.get("linkedin", "N/A"),
                    ])

                ws.clear()
                if new_data:
                    ws.update("A1", new_data)
                log.info(f"✅ TO_VERIFY queue rewritten ({len(rows)} rows remaining).")
                return True
            except Exception as exc:
                log.error(f"❌ Error rewriting TO_VERIFY: {exc}")
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
        Executed on the 1st of each month by APScheduler (and at startup if a
        migration was missed while the bot was down).
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
        _save_last_migration(datetime.now())

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
    Real web scraper using Playwright + page_scraper.py.
    Visits the domain and extracts actual email addresses found on the page.

    Returns an empty list when nothing is found. The previous version fell back
    to _generic_inbox_patterns() (info@, contact@, sales@, support@) but those
    are now filtered out at the dedup stage anyway, so the fallback is disabled
    to keep behaviour explicit and skip an unnecessary Playwright spin-up.
    """
    url = normalize_url(domain)
    if not url:
        log.warning(f"⚠️ Could not normalize domain '{domain}'.")
        return []

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
        log.warning(f"⚠️ Web scraper failed for '{domain}': {e}.")

    return []


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
    2. CURRENT EMPLOYER ONLY: For each person, extract ONLY the CURRENT company/role. Explicitly IGNORE:
       - Universities, schools and any educational institution (even if shown next to the person).
       - Past employers, previous positions, "ex-" companies.
       - Secondary affiliations, sponsors, partners, or any logo that is not the person's primary current employer.
    3. ENTITY LINKING: When a person is the clear subject of the image (e.g. LinkedIn profile, business card), link them to their CURRENT company in 'people'. Do NOT add unrelated companies visible in the background to 'domains'.
    4. DOMAIN INFERENCE: Infer the corporate domain only for the CURRENT employer of an extracted person, or for companies that are themselves the subject of the image (e.g. an event sponsor list where no specific person is highlighted).
    5. ROLE CAPTURE: Extract exact CURRENT job title. If not visible, use "Lead".
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


def investigate_domain_with_ai(company_name: str, retries: int = 1) -> str | None:
    """
    Resolves a corporate domain from a bare company name via DuckDuckGo + Gemini.
    Used when Gemini Vision extracts a brand without a TLD (e.g. 'fundingloop' instead
    of 'fundingloop.ch'). Returns a bare domain like 'fundingloop.ch' or None.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return None
    try:
        try:
            results = DDGS().text(f'"{company_name}" official site', max_results=3)
            raw = str(results) if results else ""
        except Exception as e:
            log.warning(f"⚠️ DuckDuckGo failed for domain '{company_name}': {e}")
            raw = ""

        if not raw or raw == "[]":
            return None

        prompt = f"""
        You are a Data Verification Agent. Target Company: {company_name}.
        Below are web search results. Identify the SINGLE official corporate website.
        Return ONLY the bare domain (e.g. 'fundingloop.ch' or 'apple.com'), or the exact word: NONE
        Reject social-media URLs (linkedin.com, twitter.com, facebook.com, crunchbase.com, etc.) — those are profiles, not the official site.
        Results: {raw}
        """
        for attempt in range(retries + 1):
            try:
                response = client_google.models.generate_content(model=GEMINI_MODEL, contents=prompt)
                domain = response.text.strip().lower()
                # Strip protocol, www, paths, quotes that Gemini sometimes adds
                domain = re.sub(r'^https?://', '', domain)
                domain = re.sub(r'^www\.', '', domain)
                domain = domain.split('/')[0].strip("'\"`")
                if domain and domain != "none" and "." in domain and " " not in domain:
                    return domain
                return None
            except Exception as e:
                log.warning(f"⚠️ Gemini domain resolver (attempt {attempt + 1}): {e}")
                if attempt < retries:
                    time.sleep(5)
        return None
    except Exception as e:
        log.error(f"❌ General error in investigate_domain: {e}")
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
    leads_failed      = []   # Specific people the AI found but whose email couldn't be verified
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

        # If Gemini extracted a bare brand name without a TLD (e.g. "fundingloop"),
        # resolve it to a real domain via DuckDuckGo before any Snov.io / SMTP work.
        # Without this, downstream calls fail silently with NXDOMAIN.
        if "." not in dom:
            log.info(f"🔍 Resolving domain for bare brand '{dom}' via DDG...")
            resolved = investigate_domain_with_ai(dom)
            if resolved:
                log.info(f"   → resolved to '{resolved}'")
                dom = resolved
            else:
                log.warning(f"⚠️ Could not resolve domain for '{dom}'. Skipping person '{name}'.")
                leads_failed.append({
                    "name":   name,
                    "domain": dom,
                    "tried":  "—",
                    "reason": "Could not resolve corporate domain from brand name",
                })
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

            # Routing for sniped guesses:
            #   valid / catch_all → WR1 directly (we know it exists)
            #   unknown           → TO_VERIFY (nightly free APIs may have more info)
            #   NO (SMTP reject)  → TO_VERIFY too: the specific guess is dead but
            #                        the person likely uses a different pattern
            #                        (firstname.lastname@, f.lastname@, ...).
            #                        The nightly resnipe will try alternatives.
            # Only confirmed-existing addresses skip TO_VERIFY.
            _pending = status not in ("valid", "catch_all")
            if vr.reachable == REACHABLE_NO:
                log.info(f"📤 '{guessed}' rejected by SMTP — queued for nightly resnipe.")
                status = "unknown"   # forces resnipe path in verify_queue.py

            raw_found.append({
                "email":          guessed,
                "name":           name,
                "role":           f"⭐ {role}",
                "company_domain": dom,
                "source":         f"AI Enriched ({status})",
                "added_date":     now,
                "linkedin":       ln or "N/A",
                "_pending":       _pending,   # True → TO_VERIFY queue instead of WR1
            })
        # Mark domain as processed so section B skips it — avoids duplicate Snov.io calls
        processed_domains.add(dom)

    # B. Domains (company-level extraction via Snov.io, then web scraper fallback)
    for dom in data.get("domains", []):
        # Resolve bare brand names (no TLD) to real domains before processing.
        if "." not in dom:
            log.info(f"🔍 Resolving bare brand '{dom}' via DDG...")
            resolved = investigate_domain_with_ai(dom)
            if resolved:
                log.info(f"   → resolved to '{resolved}'")
                dom = resolved
            else:
                log.warning(f"⚠️ Could not resolve domain for '{dom}'. Skipping.")
                continue

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

    # Global duplicate filter (against live Sheets + TO_VERIFY) + intra-batch deduplication
    cloud_emails    = cloud.get_all_emails()
    seen_in_batch   = set()
    leads_confirmed = []    # status known → go to Waiting_Room_1 + Brevo immediately
    leads_pending   = []    # status unknown → go to TO_VERIFY queue for daily re-check
    skipped_generic = 0     # role-account inboxes filtered at dedup time

    for l in raw_found:
        email_clean = l["email"].strip().lower()

        # Block generic role-account inboxes (info@, support@, sales@, contact@…)
        # at every entry point. They waste Brevo slots and campaign sends, and the
        # ones Gemini hallucinates from a bare domain aren't even verified to exist.
        # "+" aliases (info+foo@…) also count as role accounts.
        local_part = email_clean.split("@", 1)[0].split("+", 1)[0]
        if local_part in ROLE_ACCOUNTS:
            skipped_generic += 1
            continue

        if (email_clean not in cloud_emails
                and MY_COMPANY not in email_clean
                and email_clean not in seen_in_batch):
            seen_in_batch.add(email_clean)
            if l.pop("_pending", False):
                leads_pending.append(l)
            else:
                # Strip internal flag if somehow set by other sections
                l.pop("_pending", None)
                leads_confirmed.append(l)

    if skipped_generic:
        log.info(f"🚫 Filtered {skipped_generic} generic role-account inbox(es) from intake.")

    total_new = len(leads_confirmed) + len(leads_pending)
    log.info(
        f"📊 {len(raw_found)} raw leads → "
        f"{len(leads_confirmed)} confirmed, {len(leads_pending)} queued for verification."
    )

    # --- Persist confirmed leads → WR1 + Brevo ---
    if leads_confirmed:
        cloud.add_leads_to_fase1(leads_confirmed)
        export_to_brevo(leads_confirmed)

    # --- Persist unknown leads → TO_VERIFY queue ---
    if leads_pending:
        cloud.add_leads_to_verify_queue(leads_pending)

    if total_new:
        # Build CSV from all new leads for Slack summary
        all_new = leads_confirmed + leads_pending
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".csv", prefix="leads_", delete=False, encoding="utf-8"
            ) as tmp:
                tmp_path = tmp.name

            pd.DataFrame(all_new).to_csv(tmp_path, index=False)

            summary_lines = []
            if leads_confirmed:
                summary_lines.append(f"✅ *{len(leads_confirmed)}* leads added to Phase 1 (WR1).")
            if leads_pending:
                summary_lines.append(
                    f"⏳ *{len(leads_pending)}* emails queued in `TO_VERIFY` "
                    f"(unknown status — daily API rotation will confirm tonight)."
                )
            if leads_failed:
                summary_lines.append(
                    f"❌ *{len(leads_failed)}* target(s) found but discarded (email unreachable):"
                )
                for f in leads_failed:
                    summary_lines.append(f"   • {f['name']} @ `{f['domain']}` — tried `{f['tried']}`")

            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=tmp_path,
                title="New Leads",
                initial_comment="\n".join(summary_lines),
            )
        except Exception as e:
            log.error(f"❌ Failed to upload CSV to Slack: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
    elif leads_failed:
        # No leads added, but the bot did find specific people — surface that so the user
        # knows the issue is email verification, not extraction.
        lines = [f"❌ *{len(leads_failed)}* target(s) found but discarded (email unreachable):"]
        for f in leads_failed:
            lines.append(f"   • {f['name']} @ `{f['domain']}` — tried `{f['tried']}`")
        lines.append("_Tip: re-send with the exact corporate domain to retry._")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(lines))
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

# =========================================================================================
# 8b. MIGRATION STATE PERSISTENCE
# =========================================================================================

def _load_last_migration() -> datetime | None:
    """
    Reads the timestamp of the last successful migration from disk.
    Returns None if the file doesn't exist or is unreadable.
    """
    try:
        if MIGRATION_STATE_FILE.exists():
            data = json.loads(MIGRATION_STATE_FILE.read_text())
            return datetime.fromisoformat(data["last_migration"])
    except Exception as e:
        log.warning(f"⚠️ Could not read migration state file: {e}")
    return None


def _save_last_migration(ts: datetime):
    """Persists the timestamp of the last successful migration to disk."""
    try:
        MIGRATION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MIGRATION_STATE_FILE.write_text(json.dumps({"last_migration": ts.isoformat()}))
        log.info(f"💾 Migration state saved: {ts.strftime('%Y-%m-%d %H:%M')}.")
    except Exception as e:
        log.warning(f"⚠️ Could not save migration state: {e}")


def check_missed_migration():
    """
    Called once at bot startup. Detects if the bot was down when the 1st-of-month
    migration should have fired (and APScheduler's 1-hour grace window has expired).
    If a missed migration is detected, runs it immediately.

    First-run behaviour: if no state file exists yet, records today as the baseline
    without triggering a migration (we have no prior reference point).
    """
    now  = datetime.now()
    last = _load_last_migration()

    if last is None:
        log.info("📋 No migration history found. Recording today as baseline — no migration triggered on first run.")
        _save_last_migration(now)
        return

    # Compute the 1st of the month immediately following the last migration
    if last.month == 12:
        next_expected = last.replace(year=last.year + 1, month=1, day=1,
                                     hour=1, minute=0, second=0, microsecond=0)
    else:
        next_expected = last.replace(month=last.month + 1, day=1,
                                     hour=1, minute=0, second=0, microsecond=0)

    # Include the same 1-hour grace window that APScheduler uses
    grace_deadline = next_expected + timedelta(hours=1)

    if now >= grace_deadline:
        log.warning(
            f"⚠️ Missed migration detected! "
            f"Last ran: {last.strftime('%Y-%m-%d')} | "
            f"Expected: {next_expected.strftime('%Y-%m-%d %H:%M')} | "
            f"Running catch-up migration now..."
        )
        cloud.run_migration()   # run_migration() calls _save_last_migration() internally
    else:
        log.info(f"✅ Migration up to date. Last ran: {last.strftime('%Y-%m-%d')}.")


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

    # Check if a migration was missed while the bot was down
    check_missed_migration()

    # Monthly migration scheduler
    start_scheduler()

    # Slack WebSocket — blocking call, runs on the main thread
    log.info("🤖 Volvero Email Finder connected and listening for mentions.")
    SocketModeHandler(app_slack, os.environ["SLACK_APP_TOKEN"]).start()