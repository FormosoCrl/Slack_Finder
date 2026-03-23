# Slack_Finder 🤖 (v1.0 - AI Powered)

This App is a refactor from a fork (FormosoCrl/volvero-emails) of the
original repository by Alaa Sahraoui.

------------------------------------------------------------------------

## 📌 Overview

Slack_Finder es un bot de Slack que automatiza la generación de leads
B2B directamente desde mensajes de chat.\
Utiliza Google Gemini AI para analizar textos, extraer dominios y
correos, y generar una base de datos limpia lista para prospección.

------------------------------------------------------------------------

## 🔑 Environment Variables

Crea un archivo `.env` en la raíz (asegúrate de que esté en tu
`.gitignore`):

``` bash
SLACK_BOT_TOKEN=your_bot_token
SLACK_APP_TOKEN=your_socket_token
GEMINI_API_KEY=your_gemini_key
MY_COMPANY=volvero.com
```

------------------------------------------------------------------------

## 📊 Data Schema (CSV)

El archivo `leads_report.csv` sigue esta estructura para asegurar la
compatibilidad con CRMs:

  -----------------------------------------------------------------------------
  Columna          Descripción                          Ejemplo
  ---------------- ------------------------------------ -----------------------
  email            Email único (evita duplicados)       ceo@matrixinternet.ie

  name             Nombre del lead o "Generic/Auto"     John Doe

  role             Cargo detectado                      CEO

  company_domain   Dominio de la empresa                matrixinternet.ie

  source           Origen (Snovio o Extracción)         Snovio (Mock)

  added_date       Fecha de captura                     2026-03-23 17:34
  -----------------------------------------------------------------------------

------------------------------------------------------------------------

## 🧠 How It Works (AI + UX)

-   **Instant Feedback:** El bot confirma inmediatamente que está
    procesando el mensaje.
-   **AI Extraction:** Gemini extrae dominios y clasifica correos por
    relevancia.
-   **The "Hard" Filter:** El código de Python elimina cualquier mención
    a la propia empresa.
-   **Auto-Enrichment:** Simulación de Snov.io para añadir contactos
    clave.
-   **Output:** Reporte elegante en Slack (Block Kit) + descarga del
    CSV.

------------------------------------------------------------------------

## 🛡️ Security & Reliability

-   **Double Filter:** IA + filtro en código para bloquear
    `volvero.com`.
-   **Duplicate Protection:** Pandas evita duplicados en el CSV.
-   **Anti-Error:** Limpieza automática ante respuestas inconsistentes
    de la IA.

------------------------------------------------------------------------

## 📂 Project Structure

    formosocrl-slack_finder/
    ├── .gitignore             # Seguridad de credenciales
    ├── README.md
    └── Slack_finder_python/
        ├── .env               # (Local only)
        ├── bot.py             # Motor principal del bot
        └── leads_report.csv   # Base de datos local

------------------------------------------------------------------------

## ▶️ Run the Bot

``` bash
pip install -r requirements.txt
python Slack_finder_python/bot.py
```

------------------------------------------------------------------------

## 🚧 Roadmap

### Phase 1

-   Slack bot funcional
-   AI extraction
-   CSV storage

### Phase 2

-   Integración real con Snov.io
-   Scraping avanzado
-   Google Sheets sync

### Phase 3

-   Automatización de emails (Brevo)
-   Lead scoring con IA
-   Integración con CRM

------------------------------------------------------------------------

## ⚠️ Notes

-   Snov.io actualmente está simulado (mock)
-   La calidad depende del input del usuario

------------------------------------------------------------------------

## 👨‍💻 Credits

Refactor por FormosoCrl\
Basado en el trabajo original de Alaa Sahraoui
