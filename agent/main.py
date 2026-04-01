# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor (Whapi, Meta, Twilio) gracias a la capa de providers.
"""

import os
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor

load_dotenv()

# Configuración de logging según entorno
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

# Proveedor de WhatsApp (se configura en .env con WHATSAPP_PROVIDER)
proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))

# Buffer en memoria con los últimos 20 eventos (para /debug)
_eventos = deque(maxlen=20)


def _log(nivel: str, mensaje: str):
    """Loggea y guarda en buffer de debug."""
    ts = datetime.utcnow().strftime("%H:%M:%S")
    entrada = {"ts": ts, "nivel": nivel, "msg": mensaje}
    _eventos.append(entrada)
    if nivel == "ERROR":
        logger.error(mensaje)
    else:
        logger.info(mensaje)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al arrancar el servidor."""
    await inicializar_db()
    _log("INFO", "Base de datos inicializada")
    _log("INFO", f"Servidor AgentKit en puerto {PORT}")
    _log("INFO", f"Proveedor: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="AgentKit — Valentina | Conexion Sin Limites",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway/monitoreo."""
    return {"status": "ok", "agente": "Valentina", "empresa": "Conexion Sin Limites"}


@app.get("/debug")
async def debug():
    """Muestra los últimos eventos del webhook para diagnóstico."""
    return {"eventos": list(_eventos)}


@app.get("/webhook")
@app.head("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET/HEAD del webhook — responde 200 a cualquier proveedor."""
    try:
        resultado = await proveedor.validar_webhook(request)
        if resultado is not None:
            return PlainTextResponse(str(resultado))
    except Exception:
        pass
    return PlainTextResponse("ok")


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.
    Siempre retorna 200 para evitar reintentos de Meta/Whapi.
    """
    # Capturar el body crudo para debug antes de parsearlo
    try:
        body_raw = await request.body()
        _log("INFO", f"Webhook recibido: {body_raw[:300].decode('utf-8', errors='replace')}")
    except Exception:
        pass

    # Parsear webhook
    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:
        _log("ERROR", f"Error parseando webhook: {e}")
        return {"status": "ok"}

    if not mensajes:
        _log("INFO", "Webhook sin mensajes de texto (probablemente status update)")
        return {"status": "ok"}

    for msg in mensajes:
        if msg.es_propio or not msg.texto:
            _log("INFO", f"Mensaje ignorado — es_propio={msg.es_propio} texto='{msg.texto}'")
            continue

        _log("INFO", f"Procesando mensaje de {msg.telefono}: '{msg.texto}'")

        try:
            historial = await obtener_historial(msg.telefono)
            _log("INFO", f"Historial recuperado: {len(historial)} mensajes previos")

            respuesta = await generar_respuesta(msg.texto, historial)
            _log("INFO", f"Respuesta generada: '{respuesta[:100]}'")

            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta)
            if enviado:
                _log("INFO", f"Respuesta enviada OK a {msg.telefono}")
            else:
                _log("ERROR", f"enviar_mensaje falló para {msg.telefono} — revisar token/credenciales en Railway")

        except Exception as e:
            _log("ERROR", f"Error procesando mensaje de {msg.telefono}: {e}")

    return {"status": "ok"}
