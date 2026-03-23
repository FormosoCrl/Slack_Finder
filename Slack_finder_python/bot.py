import os
import re
import json
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import google.generativeai as genai

# 1. Configuración
load_dotenv()
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

CSV_FILE = "leads_report.csv"
MY_COMPANY = "volvero.com"


# --- SIMULADOR DE SNOVIO ---
def simulate_snovio_api(domain):
    return [
        {"email": f"ceo@{domain}", "name": "John Doe", "role": "CEO", "source": "Snovio_Mock"},
        {"email": f"tech@{domain}", "name": "Jane Smith", "role": "CTO", "source": "Snovio_Mock"}
    ]


# --- INTELIGENCIA ARTIFICIAL ---
def analyze_text_with_ai(text):
    try:
        model = genai.GenerativeModel('gemini-flash-latest')
        prompt = f"""
        Extract entities from this text in JSON format.
        RULES:
        1. "domains": List of strings (e.g. ["matrixinternet.ie"]). EXCLUDE '{MY_COMPANY}'.
        2. "emails": List of objects {{"email": "...", "relevance": "High/Low", "role": "..."}}.
        Text: {text}
        """
        response = model.generate_content(prompt)
        clean_response = response.text.strip()
        if clean_response.startswith("```"):
            clean_response = re.sub(r'```json|```', '', clean_response).strip()
        return json.loads(clean_response)
    except Exception as e:
        print(f"⚠️ Error en IA: {e}")
        return {"domains": [], "emails": []}


# --- GESTIÓN DEL CSV (FILTRO TOTAL) ---
def update_leads_csv(new_leads_list):
    df_new = pd.DataFrame(new_leads_list)

    if os.path.exists(CSV_FILE):
        try:
            df_old = pd.read_csv(CSV_FILE)
            df_final = pd.concat([df_old, df_new], ignore_index=True)
        except:
            df_final = df_new
    else:
        df_final = df_new

    # --- LA PURGA FINAL ---
    # Limpiamos todo el DataFrame antes de guardar por si había basura de pruebas anteriores
    df_final = df_final[~df_final['email'].str.contains(MY_COMPANY, case=False, na=False)]
    if 'company_domain' in df_final.columns:
        df_final = df_final[~df_final['company_domain'].str.contains(MY_COMPANY, case=False, na=False)]

    # Eliminar duplicados y guardar
    df_final.drop_duplicates(subset=['email'], keep='first', inplace=True)
    df_final.to_csv(CSV_FILE, index=False)
    return CSV_FILE


# --- PROCESAMIENTO ---
def process_and_reply(event, client):
    channel_id = event["channel"]
    user_id = event["user"]
    raw_text = event["text"]
    text_clean = re.sub(r'<@[A-Z0-9]+>', '', raw_text).strip()

    if not text_clean: return

    # --- MENSAJE INICIAL DE CARGA ---
    client.chat_postMessage(
        channel=channel_id,
        text=f"👋 Hola <@{user_id}>, he recibido el texto. Dame unos segundos para analizarlo... 🧠"
    )

    # 1. IA analiza
    data = analyze_text_with_ai(text_clean)

    # 2. Filtrado manual de seguridad
    raw_domains = [str(d).lower() for d in data.get("domains", []) if MY_COMPANY not in str(d).lower()]
    raw_emails = [e for e in data.get("emails", []) if MY_COMPANY not in str(e.get('email', '')).lower()]

    # --- REPORTE DE DEBUG ELEGANTE ---
    email_summary = "".join([f"\n• `{e['email']}`" for e in raw_emails]) or "\n• _Ninguno_"
    domain_summary = ", ".join([f"`{d}`" for d in raw_domains]) or "_Ninguno_"

    report_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📊 Reporte de Análisis Inteligente"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Empresas externas detectadas:*\n{domain_summary}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Correos extraídos del texto:*{email_summary}"}},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": f"🛡️ *Filtro de Seguridad:* Cualquier dato relacionado con `{MY_COMPANY}` ha sido purgado."}]}
    ]
    client.chat_postMessage(channel=channel_id, blocks=report_blocks, text="Reporte de Análisis")

    # 3. Preparar Leads
    leads_to_save = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    for domain in raw_domains:
        results = simulate_snovio_api(domain)
        for r in results:
            leads_to_save.append({
                "email": r["email"], "name": r["name"], "role": r["role"],
                "company_domain": domain, "source": "Snovio (Mock)", "added_date": now
            })

    for e in raw_emails:
        if e.get("relevance") == "Low":
            leads_to_save.append({
                "email": e["email"], "name": "Generic/Auto", "role": e.get("role", "Unknown"),
                "company_domain": e["email"].split('@')[-1], "source": "Direct Email", "added_date": now
            })

    # 4. Guardar y enviar
    csv_path = update_leads_csv(leads_to_save)

    if leads_to_save or os.path.exists(CSV_FILE):
        client.files_upload_v2(
            channel=channel_id,
            file=csv_path,
            title="Base de Datos de Leads",
            initial_comment=f"📂 He actualizado y limpiado el archivo, <@{user_id}>. Ya puedes descargarlo."
        )
    else:
        client.chat_postMessage(channel=channel_id, text="✅ Proceso terminado. No se han encontrado datos nuevos.")


# --- EVENTOS ---
@app.event("app_mention")
def handle_app_mention(event, client): process_and_reply(event, client)


@app.event("message")
def handle_message(event, client):
    if event.get("subtype") is None: process_and_reply(event, client)


if __name__ == "__main__":
    print("⚡️ Slack_Finder: FULL ENGINE STARTING...")
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    handler.start()