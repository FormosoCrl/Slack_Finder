import re
import time


# Valid TLD suffixes used to filter out CSS/JS false positives from the email regex
_VALID_TLDS = {
    "com", "net", "org", "io", "co", "uk", "de", "fr", "es", "it",
    "nl", "be", "ch", "at", "au", "ca", "us", "eu", "info", "biz",
    "me", "tv", "ai", "app", "dev", "tech", "email", "online", "site"
}

# Minimum length for the local part of an email (filters out tokens like "a@b.co")
_MIN_LOCAL_LENGTH = 3


def _is_valid_email(email: str) -> bool:
    """
    Validates an extracted email candidate beyond the basic regex.
    Filters out common false positives found in HTML/CSS/JS source code.
    """
    if not email or "@" not in email:
        return False

    local, _, domain = email.partition("@")

    # Filter out image/media file false positives
    if email.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico')):
        return False

    # Filter out CSS/JS noise (e.g. "2x@media", "@keyframes", version strings)
    if len(local) < _MIN_LOCAL_LENGTH:
        return False

    # Must have a dot in the domain part
    if "." not in domain:
        return False

    # TLD must be a known suffix
    tld = domain.rsplit(".", 1)[-1].lower()
    if tld not in _VALID_TLDS:
        return False

    # Reject numeric-only local parts (common in minified JS)
    if local.replace(".", "").replace("-", "").replace("_", "").isdigit():
        return False

    return True


def scrape_site(browser, url: str, worker_id: str = "Bot", i: int = 1, total: int = 1) -> str:
    """
    Navigates to a URL using the provided Playwright browser instance,
    extracts all text content, and finds valid email addresses.

    Args:
        browser:   A Playwright browser instance (already launched by the caller).
        url:       The fully-qualified URL to scrape (e.g. https://example.com).
        worker_id: Label used in log messages for identification.
        i:         Current item index (for progress logging).
        total:     Total items being processed (for progress logging).

    Returns:
        A comma-joined string of unique email addresses found, or "NOT_FOUND".
    """
    page   = browser.new_page()
    emails = set()

    try:
        print(f"[{worker_id}] ({i}/{total}) Scraping {url}...")

        # Navigate with a 30-second timeout; wait only for DOM, not full network idle
        page.goto(url, timeout=30000, wait_until="domcontentloaded")

        # Short wait to allow client-side scripts to render contact info
        time.sleep(2)

        # Extract the full rendered HTML source
        content = page.content()

        # Standard email regex — cast a wide net, then filter
        raw_matches = re.findall(
            r'[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+',
            content
        )

        for email in raw_matches:
            clean = email.lower().strip(".")
            if _is_valid_email(clean):
                emails.add(clean)

    except Exception as e:
        print(f"[{worker_id}] Error scraping {url}: {e}")
        return "NOT_FOUND"
    finally:
        page.close()

    if emails:
        print(f"[{worker_id}] Found {len(emails)} email(s) at {url}.")
        return ",".join(sorted(emails))

    return "NOT_FOUND"