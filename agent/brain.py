# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

"""
Lógica de IA del agente. Construye el system prompt dinámicamente
via prompt_builder y genera respuestas usando la API de Anthropic Claude.
"""

import yaml
import logging
from anthropic import AsyncAnthropic
from agent.config import ANTHROPIC_API_KEY
from agent.prompt_builder import construir_prompt

logger = logging.getLogger("agentkit")

# Cliente de Anthropic
client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


def _cargar_yaml() -> dict:
    """Lee config/prompts.yaml para los mensajes de error/fallback."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def obtener_mensaje_error() -> str:
    return _cargar_yaml().get(
        "error_message",
        "Lo siento, estoy teniendo problemas técnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    return _cargar_yaml().get(
        "fallback_message",
        "Disculpa, no entendí tu mensaje. ¿Podrías reformularlo?",
    )


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    nombre_cliente: str = None,
    nombre_recien_capturado: bool = False,
    telefono: str = "",
    cliente_slug: str = "csl",
    cliente_id: int | None = None,
    lead: dict | None = None,
) -> str:
    """
    Genera una respuesta usando Claude API.

    El system prompt se construye dinámicamente via prompt_builder:
      - Ruta A: prompt_base.txt + config_json de la BD (catalogo, objeciones, cierres)
      - Ruta B: prompts.yaml completo como fallback si la BD falla o config_json
                está incompleto.
    En ambas rutas se agrega el bloque de contexto del lead (estado, resumen, etc.).

    Args:
        mensaje:               El mensaje nuevo del usuario.
        historial:             Lista de mensajes anteriores [{"role": ..., "content": ...}].
        nombre_cliente:        Nombre capturado del cliente (None = desconocido).
        nombre_recien_capturado: True si el nombre se detectó en ESTE mensaje.
        telefono:              Teléfono del cliente — identifica el lead en la BD.
        cliente_slug:          Slug del cliente en tabla `clientes` (default: "csl").
        cliente_id:            ID entero del cliente — lookup directo, más rápido que slug.
        lead:                  Dict con datos del lead ya cargados desde main.py.
                               Si se pasa, evita una consulta extra a la BD.

    Returns:
        La respuesta generada por Claude.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    # Construir prompt dinámico (Ruta A o Ruta B según disponibilidad de BD)
    system_prompt = await construir_prompt(
        telefono=telefono,
        cliente_slug=cliente_slug,
        cliente_id=cliente_id,
        lead=lead,
    )

    # Inyectar contexto del nombre para que Valentina lo use o lo pida
    INVALIDOS = {"", "desconocido", "none", "null", "cliente", "unknown"}
    nombre_valido = nombre_cliente and nombre_cliente.strip().lower() not in INVALIDOS
    if nombre_valido:
        if nombre_recien_capturado:
            # El cliente acaba de decir su nombre en ESTE mensaje — usarlo de inmediato
            system_prompt += (
                f"\n\n─────────────────────────────────────────────\n"
                f"CONTEXTO SESIÓN ACTUAL\n"
                f"Nombre del cliente: {nombre_cliente}\n"
                f"⚡ ACABA de decir su nombre por primera vez en este mensaje.\n"
                f"→ Úsalo de forma inmediata y cálida en tu respuesta.\n"
                f"→ Ejemplo natural: '¡Qué gusto, {nombre_cliente}! ...' o\n"
                f"   simplemente intégralo: '{nombre_cliente}, ...' al inicio.\n"
                f"→ No exageres — solo incorpóralo con naturalidad.\n"
                f"─────────────────────────────────────────────"
            )
        else:
            system_prompt += (
                f"\n\n─────────────────────────────────────────────\n"
                f"CONTEXTO SESIÓN ACTUAL\n"
                f"Nombre del cliente: {nombre_cliente}\n"
                f"→ Úsalo naturalmente en la conversación cuando corresponda.\n"
                f"→ NO vuelvas a preguntar el nombre.\n"
                f"─────────────────────────────────────────────"
            )
    else:
        num_mensajes = len(historial)
        if num_mensajes >= 2:
            system_prompt += (
                "\n\n─────────────────────────────────────────────\n"
                "CONTEXTO SESIÓN ACTUAL\n"
                "Nombre del cliente: desconocido.\n"
                "→ En el próximo mensaje natural, pide el nombre de forma cálida.\n"
                "   Ejemplo: '¿Con quién tengo el gusto? 😊'\n"
                "   o bien intégralo: 'Por cierto, ¿cómo te llamas?'\n"
                "→ Solo pregúntalo UNA vez. Si ya lo preguntaste antes, no insistas.\n"
                "─────────────────────────────────────────────"
            )
        else:
            system_prompt += (
                "\n\n─────────────────────────────────────────────\n"
                "CONTEXTO SESIÓN ACTUAL\n"
                "Nombre del cliente: desconocido.\n"
                "→ Es el primer mensaje — NO preguntes el nombre todavía.\n"
                "   Primero responde y genera confianza.\n"
                "─────────────────────────────────────────────"
            )

    # Construir mensajes para la API
    mensajes = []
    for msg in historial:
        mensajes.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    # Agregar el mensaje actual
    mensajes.append({
        "role": "user",
        "content": mensaje
    })

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )

        respuesta = response.content[0].text
        logger.info(f"Respuesta generada ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
