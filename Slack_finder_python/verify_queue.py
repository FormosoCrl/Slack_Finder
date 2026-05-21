"""
=============================================================================
verify_queue.py — Daily TO_VERIFY Queue Processor
=============================================================================
Run once per day via systemd timer (see deploy notes below).

Pipeline for each lead in the TO_VERIFY queue (processed FIFO by date):
  1. Try free email verification API rotation (QuickEmailVerification → …)
  2a. valid / catch_all   → promote to Waiting_Room_1 + Brevo + remove from queue
  2b. invalid             → discard (remove from queue, no WR1 entry)
  2c. unknown:
        - attempt_count >= MAX_ATTEMPTS → discard (avoid infinite loop)
        - else → AI re-snipe (try alternative email patterns), increment count,
                 update date so this lead moves to the back of the FIFO queue

Daily API budget:  QuickEmailVerification 100 / MyEmailVerifier 100 / BillionVerify 50
                   Combined cap: MAX_PER_RUN (configurable, default 80 to stay safe)

Deploy as a systemd timer:
  /etc/systemd/system/verify-queue.service
  /etc/systemd/system/verify-queue.timer
  (see bottom of this file for unit file content)
=============================================================================
"""

import os
import sys
import re
import json
import time
import logging
import requests
import gspread
from datetime import datetime
from dotenv import load_dotenv

# Allow imports from the project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from src.lead_verifier import LeadVerifier, STATUS_VALID, STATUS_INVALID, STATUS_UNKNOWN, STATUS_CATCH_ALL

# =============================================================================
# CONFIGURATION
# =============================================================================

MAX_ATTEMPTS  = 3     # Discard lead after this many inconclusive attempts
MAX_PER_RUN   = 80    # Max emails to process per daily run (API budget safety)
SLEEP_BETWEEN = 1.5   # Seconds between API calls (rate-limit courtesy)

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Slack bot automatization ( emails )")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
MY_COMPANY        = os.getenv("MY_COMPANY", "volvero.com")

# Brevo
BREVO_API_KEY     = os.getenv("BREVO_API_KEY")
BREVO_LIST_ID_WR1 = os.getenv("BREVO_LIST_ID_WR1", "61")

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("verify_queue.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("VerifyQueue")

# =============================================================================
# GOOGLE SHEETS CONNECTION
# =============================================================================

TAB_VERIFY  = "TO_VERIFY"
TAB_WR1     = "Waiting_Room_1"

_VERIFY_HEADER = ["email", "name", "role", "company_domain", "source",
                  "added_to_queue_date", "attempt_count", "linkedin"]
_WR1_HEADER    = ["email", "name", "role", "company_domain", "source",
                  "added_date", "linkedin", "sent_date"]


def _sheets_client():
    """Returns an authenticated gspread client."""
    env_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if env_creds:
        return gspread.service_account_from_dict(json.loads(env_creds))
    return gspread.service_account(filename="google_credentials.json")


def _open_or_create_tab(sh, title: str, cols: int):
    """Opens a worksheet by name, creating it if absent."""
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=cols)
        log.info(f"✅ Created worksheet '{title}'.")
        return ws


# =============================================================================
# BREVO SYNC (minimal, mirrors bot.py)
# =============================================================================

def _sync_to_brevo(lead: dict) -> bool:
    """Sends a single confirmed lead to Brevo WR1."""
    if not BREVO_API_KEY:
        log.warning("⚠️ BREVO_API_KEY not set — skipping Brevo sync.")
        return False
    try:
        list_id = int(BREVO_LIST_ID_WR1)
    except (TypeError, ValueError):
        log.error(f"❌ Invalid BREVO_LIST_ID_WR1: {BREVO_LIST_ID_WR1}")
        return False

    payload = {
        "email": lead["email"],
        "attributes": {
            "NOMBRE":  lead.get("name", "N/A"),
            "EMPRESA": lead.get("company_domain", ""),
            "CARGO":   lead.get("role", "").replace("⭐ ", ""),
        },
        "listIds":       [list_id],
        "updateEnabled": True,
    }
    try:
        r = requests.post(
            "https://api.brevo.com/v3/contacts",
            json=payload,
            headers={
                "accept":       "application/json",
                "content-type": "application/json",
                "api-key":      BREVO_API_KEY,
            },
            timeout=10,
        )
        if r.status_code not in (200, 201, 204):
            log.warning(f"⚠️ Brevo {r.status_code} for '{lead['email']}': {r.text[:80]}")
        return True
    except requests.RequestException as exc:
        log.error(f"❌ Brevo network error for '{lead['email']}': {exc}")
        return False


# =============================================================================
# AI RE-SNIPE — suggest alternative email patterns
# =============================================================================

def _ai_resnipe(name: str, domain: str) -> list:
    """
    Uses Gemini to generate a ranked list of alternative email patterns to try.
    Returns a list of candidate email addresses (most likely first).
    """
    if not GEMINI_API_KEY:
        return []

    try:
        from google import genai as _genai
        client = _genai.Client(api_key=GEMINI_API_KEY)
    except ImportError:
        log.warning("⚠️ google-genai not available for AI re-snipe.")
        return []

    parts = name.strip().split()
    if len(parts) < 2:
        return []

    first, last = parts[0].lower(), parts[-1].lower()
    prompt = f"""
You are a corporate email pattern expert.
Person: {name}  |  Company domain: {domain}
First name: {first}  |  Last name: {last}

Generate the 6 most likely corporate email addresses for this person, one per line.
Common patterns: firstname@, f.lastname@, firstname.lastname@, flastname@, lastname@, firstname_lastname@
Return ONLY the email addresses, one per line, no extra text.
"""
    try:
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        candidates = [
            line.strip().lower()
            for line in response.text.splitlines()
            if "@" in line and domain in line and MY_COMPANY not in line
        ]
        # De-duplicate while preserving order
        seen = set()
        unique = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique[:6]
    except Exception as exc:
        log.warning(f"⚠️ AI re-snipe failed for '{name}@{domain}': {exc}")
        return []


# =============================================================================
# MAIN PROCESSOR
# =============================================================================

def run():
    log.info("=" * 60)
    log.info(f"🚀 verify_queue.py started at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 60)

    # -------------------------------------------------------------------------
    # 1. Connect to Sheets
    # -------------------------------------------------------------------------
    try:
        gc = _sheets_client()
        sh = gc.open(GOOGLE_SHEET_NAME)
        log.info("✅ Google Sheets connection established.")
    except Exception as exc:
        log.critical(f"❌ Cannot connect to Google Sheets: {exc}")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # 2. Load the TO_VERIFY queue (FIFO by date)
    # -------------------------------------------------------------------------
    try:
        ws_verify = _open_or_create_tab(sh, TAB_VERIFY, len(_VERIFY_HEADER))
        all_rows  = ws_verify.get_all_values()
    except Exception as exc:
        log.critical(f"❌ Cannot read TO_VERIFY: {exc}")
        sys.exit(1)

    if len(all_rows) <= 1:
        log.info("ℹ️ TO_VERIFY queue is empty. Nothing to do.")
        return

    # Parse rows into dicts
    queue = []
    for idx, row in enumerate(all_rows[1:], start=2):
        padded = (row + [""] * len(_VERIFY_HEADER))[:len(_VERIFY_HEADER)]
        d = dict(zip(_VERIFY_HEADER, padded))
        d["_row_index"] = idx
        try:
            d["attempt_count"] = int(d.get("attempt_count", 0) or 0)
        except (ValueError, TypeError):
            d["attempt_count"] = 0
        queue.append(d)

    # Sort FIFO — oldest date first
    def _date_key(r):
        try:
            return datetime.strptime(r["added_to_queue_date"], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return datetime.max

    queue.sort(key=_date_key)
    log.info(f"📋 {len(queue)} leads in TO_VERIFY queue.")

    # -------------------------------------------------------------------------
    # 3. Open WR1 for promoted leads
    # -------------------------------------------------------------------------
    try:
        ws_wr1 = _open_or_create_tab(sh, TAB_WR1, len(_WR1_HEADER))
        # Check if WR1 has a header; if empty, add one
        if not ws_wr1.get_all_values():
            ws_wr1.append_row(_WR1_HEADER)
    except Exception as exc:
        log.error(f"❌ Cannot access Waiting_Room_1: {exc}")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # 4. Process queue (up to MAX_PER_RUN)
    # -------------------------------------------------------------------------
    verifier     = LeadVerifier()
    now_str      = datetime.now().strftime("%Y-%m-%d %H:%M")
    promoted     = []    # confirmed valid → WR1
    discarded    = []    # confirmed invalid OR max attempts exceeded
    still_pending = []   # still unknown after this run (updated rows)
    processed_count = 0

    for lead in queue:
        if processed_count >= MAX_PER_RUN:
            log.info(f"⏸️ Daily limit of {MAX_PER_RUN} reached — stopping.")
            still_pending.append(lead)
            continue

        email  = lead.get("email", "").strip().lower()
        name   = lead.get("name", "")
        domain = lead.get("company_domain", "")
        attempts = lead["attempt_count"]

        if not email:
            log.warning(f"⚠️ Row {lead['_row_index']} has no email — discarding.")
            discarded.append(lead)
            continue

        log.info(f"🔍 [{processed_count + 1}/{min(len(queue), MAX_PER_RUN)}] "
                 f"Verifying '{email}' (attempt #{attempts + 1})…")

        status = verifier.verify(email)
        processed_count += 1
        time.sleep(SLEEP_BETWEEN)

        # ---- Decision tree ----
        if status in (STATUS_VALID, STATUS_CATCH_ALL):
            log.info(f"✅ '{email}' → {status}. Promoting to WR1.")
            promoted.append(lead)

        else:
            # Both 'invalid' and 'unknown' get the re-snipe treatment.
            # - 'invalid': the specific guess doesn't exist, but the person likely
            #   has a different pattern (firstname.lastname@, f.lastname@, ...).
            # - 'unknown': inconclusive — alternative patterns may reach a clearer verdict.
            new_attempts = attempts + 1

            if new_attempts >= MAX_ATTEMPTS:
                log.info(f"⛔ '{email}' → {status} after {new_attempts} attempts. Discarding.")
                discarded.append(lead)
                continue

            # Attempt AI re-snipe: try alternative email patterns
            resnipe_promoted = False
            if name and domain:
                candidates = _ai_resnipe(name, domain)
                for candidate in candidates:
                    if candidate == email:
                        continue   # Don't re-try the same address
                    log.info(f"  🤖 Re-snipe trying '{candidate}'…")
                    cand_status = verifier.verify(candidate)
                    processed_count += 1
                    time.sleep(SLEEP_BETWEEN)

                    if cand_status in (STATUS_VALID, STATUS_CATCH_ALL):
                        log.info(f"  ✅ Re-snipe found valid address: '{candidate}'.")
                        promoted_lead = dict(lead)
                        promoted_lead["email"]  = candidate
                        promoted_lead["source"] = f"AI Re-snipe (attempt {new_attempts})"
                        promoted.append(promoted_lead)
                        resnipe_promoted = True
                        break

                    if processed_count >= MAX_PER_RUN:
                        break

            if resnipe_promoted:
                continue   # Original lead is superseded — don't re-queue it

            # No alternative worked this run.
            # - If original was 'invalid', re-queueing it makes no sense (it's dead).
            #   Discard and let the user know via discarded count.
            # - If original was 'unknown', keep it in the queue for another night.
            if status == STATUS_INVALID:
                log.info(f"🗑️ '{email}' invalid + no resnipe match. Discarding.")
                discarded.append(lead)
            else:
                updated_lead = dict(lead)
                updated_lead["attempt_count"]        = new_attempts
                updated_lead["added_to_queue_date"]  = now_str
                still_pending.append(updated_lead)
                log.info(f"⏳ '{email}' still unknown. Attempt count → {new_attempts}. Re-queued.")

    # Leads that weren't processed this run go back unchanged
    # (already appended to still_pending in the loop above via `continue` path)

    # -------------------------------------------------------------------------
    # 5. Write promoted leads to WR1
    # -------------------------------------------------------------------------
    if promoted:
        log.info(f"📥 Writing {len(promoted)} promoted leads to Waiting_Room_1…")
        wr1_rows = []
        for l in promoted:
            wr1_rows.append([
                l.get("email", ""),
                l.get("name", ""),
                l.get("role", ""),
                l.get("company_domain", ""),
                l.get("source", "Verified Queue"),
                now_str,              # added_date
                l.get("linkedin", "N/A"),
                "",                   # sent_date — filled by Brevo webhook
            ])
        try:
            ws_wr1.append_rows(wr1_rows)
            log.info(f"✅ {len(promoted)} leads added to Waiting_Room_1.")
        except Exception as exc:
            log.error(f"❌ Failed to write to WR1: {exc}")

        # Sync with Brevo
        for l in promoted:
            _sync_to_brevo(l)
            time.sleep(0.1)

    # -------------------------------------------------------------------------
    # 6. Rewrite TO_VERIFY with only still_pending rows
    # -------------------------------------------------------------------------
    try:
        new_data = [_VERIFY_HEADER]
        for r in still_pending:
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
        ws_verify.clear()
        if new_data:
            ws_verify.update("A1", new_data)
        log.info(f"✅ TO_VERIFY queue rewritten. {len(still_pending)} leads remain.")
    except Exception as exc:
        log.error(f"❌ Failed to rewrite TO_VERIFY: {exc}")

    # -------------------------------------------------------------------------
    # 7. Summary
    # -------------------------------------------------------------------------
    log.info("=" * 60)
    log.info(f"📊 Run complete:")
    log.info(f"   ✅ Promoted to WR1 : {len(promoted)}")
    log.info(f"   🗑️ Discarded       : {len(discarded)}")
    log.info(f"   ⏳ Still pending   : {len(still_pending)}")
    log.info("=" * 60)


# =============================================================================
# ENTRYPOINT
# =============================================================================

if __name__ == "__main__":
    run()


# =============================================================================
# SYSTEMD UNIT FILES (copy-paste to deploy on the GCP VM)
# =============================================================================
# Save as /etc/systemd/system/verify-queue.service:
#
# [Unit]
# Description=Volvero TO_VERIFY Queue Daily Processor
# After=network.target
#
# [Service]
# User=david_f
# WorkingDirectory=/home/david_f/Slack_Finder/Slack_finder_python
# ExecStart=/usr/bin/python3 verify_queue.py
# EnvironmentFile=/home/david_f/Slack_Finder/Slack_finder_python/.env
#
# [Install]
# WantedBy=multi-user.target
#
# ---
# Save as /etc/systemd/system/verify-queue.timer:
#
# [Unit]
# Description=Run Volvero queue verifier daily at 03:00 AM
# Requires=verify-queue.service
#
# [Timer]
# OnCalendar=*-*-* 03:00:00
# Persistent=true
#
# [Install]
# WantedBy=timers.target
#
# ---
# Enable with:
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now verify-queue.timer
# =============================================================================
