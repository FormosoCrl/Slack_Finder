import re


def normalize_url(domain: str) -> str | None:
    """
    Cleans a raw domain string and prepends https:// for use with Playwright.

    Handles the following input formats:
        - "example.com"
        - "www.example.com"
        - "http://example.com"
        - "https://www.example.com/some/path"
        - "  Example.COM  " (with whitespace or mixed case)

    Returns:
        A normalized URL string (e.g. "https://example.com"), or None if the
        input does not look like a valid domain.
    """
    if not domain:
        return None

    # Strip whitespace and lowercase
    clean = domain.strip().lower()

    # Remove any existing protocol prefix
    clean = re.sub(r'^https?://', '', clean)

    # Remove leading www.
    clean = re.sub(r'^www\.', '', clean)

    # Remove any trailing path, query string, or fragment
    clean = clean.split('/')[0].split('?')[0].split('#')[0]

    # A valid domain must contain at least one dot and no spaces
    if "." not in clean or " " in clean:
        return None

    return f"https://{clean}"