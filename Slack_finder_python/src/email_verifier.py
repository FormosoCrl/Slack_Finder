"""
=============================================================================
Volvero Email Verifier
=============================================================================
Pure-Python SMTP-level email verification without sending any emails.

Pipeline:
  1. Syntax check      -- RFC 5321 simplified regex
  2. MX lookup         -- real DNS query for the domain's mail servers
  3. SMTP ping         -- EHLO + MAIL FROM + RCPT TO (no actual email sent)
  4. Catch-all probe   -- random address to detect "accept-all" servers
  5. Auxiliary flags   -- disposable / free provider / role account
  6. Reachability      -- yes | no | unknown | catch_all

Implements standard SMTP protocol (RFC 5321) with no external SaaS dependency.
Only extra library required: dnspython (for MX lookups).
=============================================================================
"""

import re
import uuid
import socket
import smtplib
import logging
import functools
from dataclasses import dataclass
from typing import Optional

try:
    import dns.resolver
    import dns.exception
    _DNS_AVAILABLE = True
except ImportError:
    _DNS_AVAILABLE = False

log = logging.getLogger("VolveroEmailVerifier")

# ---------------------------------------------------------------------------
# REACHABILITY CONSTANTS
# ---------------------------------------------------------------------------
REACHABLE_YES       = "yes"
REACHABLE_NO        = "no"
REACHABLE_UNKNOWN   = "unknown"
REACHABLE_CATCH_ALL = "catch_all"

# ---------------------------------------------------------------------------
# SMTP SETTINGS
# ---------------------------------------------------------------------------
SMTP_TIMEOUT = 10
SMTP_FROM    = "verify@volvero.com"
SMTP_HELO    = "mail.volvero.com"
SMTP_PORT    = 25

# ---------------------------------------------------------------------------
# SYNTAX REGEX  (covers 99% of real-world addresses)
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9!#$%&'*+\-/=?^_`{|}~]"          # first char of local
    r"[a-zA-Z0-9!#$%&'*+\-/=?^_`{|}~.]*"          # rest of local (dots allowed inside)
    r"[a-zA-Z0-9!#$%&'*+\-/=?^_`{|}~]?"           # last char of local (no trailing dot)
    r"@"
    r"[a-zA-Z0-9]"                                 # domain starts with alnum
    r"(?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?"             # optional middle
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)*"  # subdomains
    r"\.[a-zA-Z]{2,}$"                             # TLD
)

# ---------------------------------------------------------------------------
# FREE PROVIDER DOMAINS
# ---------------------------------------------------------------------------
FREE_PROVIDERS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.es", "yahoo.co.uk",
    "hotmail.com", "hotmail.es", "outlook.com", "outlook.es", "live.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "proton.me", "tutanota.com", "zoho.com", "yandex.com", "yandex.ru",
    "mail.com", "gmx.com", "gmx.net", "web.de", "inbox.com",
    "msn.com", "windowslive.com", "fastmail.com", "hushmail.com",
})

# ---------------------------------------------------------------------------
# DISPOSABLE DOMAIN PATTERNS
# ---------------------------------------------------------------------------
DISPOSABLE_PATTERNS = frozenset({
    "mailinator.com", "guerrillamail.com", "guerrillamail.net",
    "trashmail.com", "trashmail.net", "sharklasers.com",
    "guerrillamail.info", "guerrillamail.biz", "guerrillamail.de",
    "guerrillamail.org", "spam4.me", "yopmail.com", "yopmail.fr",
    "dispostable.com", "tempinbox.com", "10minutemail.com",
    "10minutemail.net", "throwam.com", "maildrop.cc",
    "mailnull.com", "tempr.email", "discard.email",
    "mailnesia.com", "getairmail.com", "fakeinbox.com",
})

# ---------------------------------------------------------------------------
# ROLE ACCOUNT PREFIXES
# ---------------------------------------------------------------------------
ROLE_ACCOUNTS = frozenset({
    "info", "contact", "support", "help", "sales", "admin", "administrator",
    "marketing", "hello", "team", "careers", "jobs", "hire", "noreply",
    "no-reply", "donotreply", "do-not-reply", "webmaster", "postmaster",
    "hostmaster", "abuse", "security", "privacy", "legal", "billing",
    "invoice", "invoices", "payments", "finance", "accounting", "press",
    "media", "pr", "feedback", "newsletter", "unsubscribe", "subscriptions",
    "office", "reception", "general", "enquiries", "enquiry", "queries",
    "query", "register", "registration", "dev", "development", "engineer",
    "engineering", "it", "ops", "operations", "hr", "humanresources",
    "recruiting", "recruitment",
})

# ---------------------------------------------------------------------------
# RESULT DATACLASS
# ---------------------------------------------------------------------------
@dataclass
class VerificationResult:
    email:        str
    reachable:    str  = REACHABLE_UNKNOWN
    syntax_ok:    bool = False
    has_mx:       bool = False
    smtp_ok:      Optional[bool] = None   # None = not checked / inconclusive
    catch_all:    bool = False
    disposable:   bool = False
    free:         bool = False
    role_account: bool = False
    mx_host:      str  = ""
    error:        str  = ""

    @property
    def deliverable(self) -> bool:
        return self.reachable == REACHABLE_YES

    def summary(self) -> str:
        """One-line human-readable status for Slack messages."""
        parts = []
        parts.append("syntax OK" if self.syntax_ok else "BAD SYNTAX")
        parts.append("MX OK" if self.has_mx else "NO MX")
        reach_label = {
            REACHABLE_YES:       "deliverable",
            REACHABLE_NO:        "UNDELIVERABLE",
            REACHABLE_UNKNOWN:   "unknown (SMTP blocked)",
            REACHABLE_CATCH_ALL: "catch-all",
        }.get(self.reachable, self.reachable)
        parts.append(reach_label)
        if self.disposable:   parts.append("DISPOSABLE")
        if self.free:         parts.append("free provider")
        if self.role_account: parts.append("role account")
        return " | ".join(parts)

    def to_snov_compat(self) -> str:
        """Drop-in replacement for Snov.io result strings used in bot.py."""
        return {
            REACHABLE_YES:       "valid",
            REACHABLE_NO:        "invalid",
            REACHABLE_CATCH_ALL: "catch_all",
            REACHABLE_UNKNOWN:   "unknown",
        }.get(self.reachable, "unknown")


# ---------------------------------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=512)
def _get_mx_hosts(domain: str) -> tuple:
    """Returns sorted tuple of MX hostnames. Cached to avoid duplicate DNS queries."""
    if not _DNS_AVAILABLE:
        return ()
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        hosts = sorted(answers, key=lambda r: r.preference)
        return tuple(str(r.exchange).rstrip(".") for r in hosts)
    except (dns.exception.DNSException, Exception) as exc:
        log.debug(f"MX lookup failed for '{domain}': {exc}")
        return ()


def _random_probe_address(domain: str) -> str:
    """Generates a UUID-based throwaway address for catch-all detection."""
    return f"volvero-probe-{uuid.uuid4().hex[:10]}@{domain}"


def _smtp_rcpt_check(mx_host: str, email: str, timeout: int = SMTP_TIMEOUT) -> tuple:
    """
    Connects to mx_host:25 and checks whether *email* is accepted via RCPT TO.

    Returns (exists: bool | None, error: str)
      True  -> server accepted  (mailbox likely exists)
      False -> server rejected with 5xx  (mailbox does not exist)
      None  -> inconclusive (timeout, port blocked, greylisting, etc.)
    """
    try:
        with smtplib.SMTP(timeout=timeout) as conn:
            conn.connect(mx_host, SMTP_PORT)
            conn.ehlo(SMTP_HELO)
            code, _ = conn.mail(SMTP_FROM)
            if code not in (250, 251):
                return None, f"MAIL FROM rejected with code {code}"
            code, msg = conn.rcpt(email)
            msg_str = msg.decode(errors="replace") if isinstance(msg, bytes) else str(msg)
            if code in (250, 251):
                return True, ""
            if 500 <= code < 600:
                return False, msg_str
            return None, f"Ambiguous RCPT reply {code}: {msg_str}"

    except smtplib.SMTPConnectError as exc:
        return None, f"Connect error: {exc}"
    except smtplib.SMTPServerDisconnected as exc:
        return None, f"Server disconnected: {exc}"
    except smtplib.SMTPException as exc:
        return None, f"SMTP error: {exc}"
    except (socket.timeout, TimeoutError):
        return None, "Connection timed out (port 25 likely blocked by ISP)"
    except OSError as exc:
        return None, f"Network error: {exc}"


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------

class EmailVerifier:
    """
    Volvero's own SMTP-level email verifier.

    Basic usage:
        vr = EmailVerifier()
        result = vr.verify("john@example.com")
        print(result.reachable)          # yes / no / unknown / catch_all
        print(result.to_snov_compat())   # "valid" / "invalid" / "unknown"
    """

    def __init__(self, smtp_timeout: int = SMTP_TIMEOUT, smtp_enabled: bool = True):
        self.smtp_timeout = smtp_timeout
        self.smtp_enabled = smtp_enabled

    def verify(self, email: str) -> VerificationResult:
        email  = email.strip().lower()
        result = VerificationResult(email=email)

        # Stage 1 — Syntax
        result.syntax_ok = self._check_syntax(email)
        if not result.syntax_ok:
            result.reachable = REACHABLE_NO
            result.error     = "Invalid email syntax"
            return result

        username, domain = email.rsplit("@", 1)

        # Stage 2 — Auxiliary flags (no network)
        result.disposable   = domain  in DISPOSABLE_PATTERNS
        result.free         = domain  in FREE_PROVIDERS
        result.role_account = username.split("+")[0].lower() in ROLE_ACCOUNTS

        # Stage 3 — MX records
        mx_hosts = _get_mx_hosts(domain)
        result.has_mx = bool(mx_hosts)
        if not result.has_mx:
            result.reachable = REACHABLE_NO
            result.error     = f"No MX records found for '{domain}'"
            return result

        result.mx_host = mx_hosts[0]

        # Stage 4 — SMTP check (optional — can be disabled for fast lookups)
        if not self.smtp_enabled:
            result.reachable = REACHABLE_UNKNOWN
            return result

        smtp_ok, err = _smtp_rcpt_check(result.mx_host, email, self.smtp_timeout)

        if smtp_ok is None:
            result.smtp_ok   = None
            result.reachable = REACHABLE_UNKNOWN
            result.error     = err
            return result

        result.smtp_ok = smtp_ok

        if not smtp_ok:
            result.reachable = REACHABLE_NO
            result.error     = err
            return result

        # Stage 5 — Catch-all probe (only reached if SMTP accepted the address)
        probe_ok, _ = _smtp_rcpt_check(result.mx_host, _random_probe_address(domain), self.smtp_timeout)
        if probe_ok is True:
            result.catch_all = True
            result.reachable = REACHABLE_CATCH_ALL
        else:
            result.reachable = REACHABLE_YES

        return result

    def batch_verify(self, emails: list) -> list:
        """Verifies a list of emails sequentially."""
        return [self.verify(e) for e in emails]

    @staticmethod
    def _check_syntax(email: str) -> bool:
        return bool(_EMAIL_RE.match(email))


# ---------------------------------------------------------------------------
# MODULE-LEVEL CONVENIENCE FUNCTIONS
# ---------------------------------------------------------------------------

_default_verifier = EmailVerifier()


def verify_email(email: str) -> VerificationResult:
    """Convenience wrapper around the module-level default verifier."""
    return _default_verifier.verify(email)


def verify_email_status(email: str) -> str:
    """
    Returns a Snov.io-compatible status string.
    Drop-in replacement for verify_email_snovio() in bot.py.
    """
    return verify_email(email).to_snov_compat()
