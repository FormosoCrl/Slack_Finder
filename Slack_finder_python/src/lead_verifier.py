"""
=============================================================================
Volvero Lead Verifier — Free API Rotation
=============================================================================
Rotates through free email verification APIs:
  1. QuickEmailVerification (100 free/day, no credit card required)
  2. MyEmailVerifier        (100 free/day — add MYEMAILVERIFIER_API_KEY when ready)
  3. BillionVerify          (50  free/day — add BILLIONVERIFY_API_KEY when ready)

Returns Snov.io-compatible strings:
  "valid" | "invalid" | "unknown" | "catch_all"

Each provider is tried in order. If one returns a conclusive answer (valid /
invalid / catch_all) that answer is returned immediately.  If a provider
hits its daily limit (402 / 429) or its key is not configured, the next
one is tried.  Only when ALL providers are exhausted (or all return
inconclusive) does the function return "unknown".
=============================================================================
"""

import os
import logging
import requests
from typing import Optional

log = logging.getLogger("VolveroLeadVerifier")

# ---------------------------------------------------------------------------
# STATUS CONSTANTS  (Snov.io / internal convention)
# ---------------------------------------------------------------------------
STATUS_VALID     = "valid"
STATUS_INVALID   = "invalid"
STATUS_UNKNOWN   = "unknown"
STATUS_CATCH_ALL = "catch_all"

_TIMEOUT = 10          # seconds per API call
_LIMIT_CODES = (402, 429)   # HTTP codes that signal quota exhaustion


class LeadVerifier:
    """
    Rotates through up to three free email-verification APIs.

    Usage:
        lv = LeadVerifier()
        status = lv.verify("john@example.com")
        # "valid" | "invalid" | "unknown" | "catch_all"
    """

    def __init__(self):
        self._qev_key = os.getenv("QUICKEMAILVERIFICATION_API_KEY")
        self._mev_key = os.getenv("MYEMAILVERIFIER_API_KEY")   # optional
        self._bv_key  = os.getenv("BILLIONVERIFY_API_KEY")     # optional

    # -----------------------------------------------------------------------
    # PUBLIC API
    # -----------------------------------------------------------------------

    def verify(self, email: str) -> str:
        """
        Returns "valid" | "invalid" | "unknown" | "catch_all".
        Tries each configured provider in order, skips on quota or missing key.
        """
        email = email.strip().lower()
        for provider in (
            self._check_quickemail,
            self._check_myemailverifier,
            self._check_billionverify,
        ):
            try:
                result = provider(email)
                if result is not None:
                    log.info(f"✅ [{provider.__name__}] '{email}' → {result}")
                    return result
            except requests.RequestException as exc:
                log.warning(f"⚠️ [{provider.__name__}] network error for '{email}': {exc}")
            except Exception as exc:
                log.warning(f"⚠️ [{provider.__name__}] unexpected error for '{email}': {exc}")

        log.warning(f"⚠️ All verifiers exhausted for '{email}' — returning unknown.")
        return STATUS_UNKNOWN

    # -----------------------------------------------------------------------
    # PROVIDER 1 — QuickEmailVerification
    # -----------------------------------------------------------------------

    def _check_quickemail(self, email: str) -> Optional[str]:
        """
        QuickEmailVerification.com  (100 free verifications / day, no CC)
        Docs: https://quickemailverification.com/documents/api
        """
        if not self._qev_key:
            return None   # Not configured — skip

        resp = requests.get(
            "https://api.quickemailverification.com/v1/verify",
            params={"email": email, "apikey": self._qev_key},
            timeout=_TIMEOUT,
        )

        if resp.status_code == 401:
            log.error("❌ QuickEmailVerification: invalid API key.")
            return None
        if resp.status_code in _LIMIT_CODES:
            log.warning("⚠️ QuickEmailVerification: daily limit reached — trying next API.")
            return None
        resp.raise_for_status()

        data   = resp.json()
        result = data.get("result", "").lower()   # "valid" | "invalid" | "unknown"

        # QEV sets catch_all="true" when RCPT accepts any address
        if result == "valid" and str(data.get("catch_all", "")).lower() == "true":
            return STATUS_CATCH_ALL
        if result == "valid":
            return STATUS_VALID
        if result == "invalid":
            return STATUS_INVALID
        return STATUS_UNKNOWN

    # -----------------------------------------------------------------------
    # PROVIDER 2 — MyEmailVerifier
    # -----------------------------------------------------------------------

    def _check_myemailverifier(self, email: str) -> Optional[str]:
        """
        MyEmailVerifier.com  (100 free verifications / day, no CC)
        Add MYEMAILVERIFIER_API_KEY to .env to enable.
        Docs: https://www.myemailverifier.com/api-documentation
        """
        if not self._mev_key:
            return None

        resp = requests.get(
            "https://api.myemailverifier.com/verify",
            params={"secret": self._mev_key, "email": email},
            timeout=_TIMEOUT,
        )

        if resp.status_code == 401:
            log.error("❌ MyEmailVerifier: invalid API key.")
            return None
        if resp.status_code in _LIMIT_CODES:
            log.warning("⚠️ MyEmailVerifier: daily limit reached — trying next API.")
            return None
        resp.raise_for_status()

        data   = resp.json()
        status = data.get("status", "").lower()

        if status == "valid":
            return STATUS_VALID
        if status in ("invalid", "undeliverable"):
            return STATUS_INVALID
        if status in ("catch_all", "accept_all"):
            return STATUS_CATCH_ALL
        return STATUS_UNKNOWN

    # -----------------------------------------------------------------------
    # PROVIDER 3 — BillionVerify
    # -----------------------------------------------------------------------

    def _check_billionverify(self, email: str) -> Optional[str]:
        """
        BillionVerify.com  (50 free verifications / day, no CC)
        Add BILLIONVERIFY_API_KEY to .env to enable.
        Docs: https://app.billionverify.com/api
        """
        if not self._bv_key:
            return None

        resp = requests.get(
            "https://api.billionverify.com/verify",
            params={"apikey": self._bv_key, "email": email},
            timeout=_TIMEOUT,
        )

        if resp.status_code == 401:
            log.error("❌ BillionVerify: invalid API key.")
            return None
        if resp.status_code in _LIMIT_CODES:
            log.warning("⚠️ BillionVerify: daily limit reached.")
            return None
        resp.raise_for_status()

        data   = resp.json()
        status = data.get("status", "").lower()

        if status in ("ok", "valid", "deliverable"):
            return STATUS_VALID
        if status in ("invalid", "undeliverable", "bad"):
            return STATUS_INVALID
        if status in ("catch_all", "accept_all"):
            return STATUS_CATCH_ALL
        return STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# Module-level singleton & convenience wrapper
# ---------------------------------------------------------------------------

_default_verifier = LeadVerifier()


def verify_lead_email(email: str) -> str:
    """
    Convenience wrapper.  Returns "valid" | "invalid" | "unknown" | "catch_all".
    Uses the module-level LeadVerifier (shared across calls, reads env once).
    """
    return _default_verifier.verify(email)
