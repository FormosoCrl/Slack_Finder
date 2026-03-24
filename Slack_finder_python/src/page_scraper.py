import re
import time


def scrape_site(browser, url, worker_id="Bot", i=1, total=1):
    """
    Navega a la web, extrae el texto y busca patrones de email.
    Adaptado de tu lógica original de worker_func.
    """
    page = browser.new_page()
    emails = set()

    try:
        # Navegamos con un timeout de 30 segundos
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        time.sleep(2)  # Espera pequeña para carga de scripts

        # Obtenemos todo el texto de la página
        content = page.content()

        # Regex para encontrar emails (estándar)
        found = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', content)

        for email in found:
            # Filtro básico para evitar basura común en el HTML
            if not email.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg')):
                emails.add(email.lower())

    except Exception as e:
        print(f"[{worker_id}] Error scrapeando {url}: {e}")
        return "NOT_FOUND"
    finally:
        page.close()

    if emails:
        return ",".join(list(emails))
    return "NOT_FOUND"