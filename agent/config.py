# agent/config.py — Lectura centralizada de variables de entorno
# Maneja nombres en inglés Y español (Railway traduce automáticamente algunas variables)

import os
from dotenv import load_dotenv

load_dotenv()


def _get(*nombres: str, default: str = "") -> str:
    """Intenta cada nombre en orden y retorna el primero que tenga valor."""
    for nombre in nombres:
        val = os.getenv(nombre, "").strip()
        if val:
            return val
    return default


# ── Anthropic ──────────────────────────────────────────────
ANTHROPIC_API_KEY = _get(
    "ANTHROPIC_API_KEY",
    "CLAVE_API_DE_ANTHROPIC",
    "CLAVE_ANTHROPIC",
    "ANTHROPIC_KEY",
)

# ── OpenAI (Whisper) ───────────────────────────────────────
OPENAI_API_KEY = _get(
    "OPENAI_API_KEY",
    "CLAVE_API_DE_OPENAI",
    "CLAVE_OPENAI",
    "OPENAI_KEY",
)

# ── Meta Cloud API ─────────────────────────────────────────
META_ACCESS_TOKEN = "".join(_get(
    "META_ACCESS_TOKEN",
    "TOKEN_DE_ACCESO_META",
    "TOKEN_ACCESO_META",
    "META_TOKEN",
).split())

META_PHONE_NUMBER_ID = "".join(_get(
    "META_PHONE_NUMBER_ID",
    "ID_NUMERO_TELEFONO_META",
    "ID_DE_NUMERO_DE_TELEFONO_DE_META",
    "PHONE_NUMBER_ID",
).split())

META_VERIFY_TOKEN = "".join(_get(
    "META_VERIFY_TOKEN",
    "TOKEN_VERIFICACION_META",
    "TOKEN_DE_VERIFICACION_DE_META",
    "VERIFY_TOKEN",
    default="agentkit-verify",
).split())

# ── Whapi ──────────────────────────────────────────────────
WHAPI_TOKEN = _get(
    "WHAPI_TOKEN",
    "TOKEN_WHAPI",
    "TOKEN_DE_WHAPI",
)

# ── Twilio ─────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = _get(
    "TWILIO_ACCOUNT_SID",
    "SID_CUENTA_TWILIO",
)
TWILIO_AUTH_TOKEN = _get(
    "TWILIO_AUTH_TOKEN",
    "TOKEN_AUTH_TWILIO",
    "TOKEN_AUTENTICACION_TWILIO",
)
TWILIO_PHONE_NUMBER = _get(
    "TWILIO_PHONE_NUMBER",
    "NUMERO_TELEFONO_TWILIO",
)

# ── Servidor ───────────────────────────────────────────────
WHATSAPP_PROVIDER = _get(
    "WHATSAPP_PROVIDER",
    "PROVEEDOR_WHATSAPP",
    "PROVEEDOR_DE_WHATSAPP",
    default="whapi",
).lower()

PORT = int(_get("PORT", "PUERTO", default="8000"))

ENVIRONMENT = _get(
    "ENVIRONMENT",
    "ENTORNO",
    "ENV",
    default="development",
).lower()

# ── Base de datos ──────────────────────────────────────────
DATABASE_URL = _get(
    "DATABASE_URL",
    "URL_BASE_DE_DATOS",
    "URL_DE_BASE_DE_DATOS",
    default="sqlite+aiosqlite:///./agentkit.db",
)

DB_PATH = _get(
    "DB_PATH",
    "RUTA_BASE_DE_DATOS",
    default="agentkit.db",
)
