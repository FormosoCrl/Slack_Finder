import re


def normalize_url(domain):
    """Limpia el dominio y le añade https:// para Playwright."""
    if not domain: return None
    # Eliminar espacios y protocolos si ya existen
    clean_domain = domain.strip().lower()
    clean_domain = re.sub(r'^https?://', '', clean_domain)
    clean_domain = re.sub(r'^www\.', '', clean_domain)

    # Si tiene un formato válido de dominio, le ponemos https
    if "." in clean_domain:
        return f"https://{clean_domain}"
    return None