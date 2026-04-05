# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

"""
Lógica de IA del agente. Lee el system prompt de prompts.yaml
y genera respuestas usando la API de Anthropic Claude.
"""

import yaml
import logging
from anthropic import AsyncAnthropic
from agent.config import ANTHROPIC_API_KEY

logger = logging.getLogger("agentkit")

# Cliente de Anthropic
client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """Lee el system prompt desde config/prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("system_prompt", "Eres un asistente útil. Responde en español.")


def obtener_mensaje_error() -> str:
    """Retorna el mensaje de error configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("error_message", "Lo siento, estoy teniendo problemas técnicos. Por favor intenta de nuevo en unos minutos.")


def obtener_mensaje_fallback() -> str:
    """Retorna el mensaje de fallback configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("fallback_message", "Disculpa, no entendí tu mensaje. ¿Podrías reformularlo?")


async def generar_respuesta(mensaje: str, historial: list[dict], nombre_cliente: str = None) -> str:
    """
    Genera una respuesta usando Claude API.

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]
        nombre_cliente: Nombre del cliente si ya fue capturado (None = aún desconocido)

    Returns:
        La respuesta generada por Claude
    """
    # Si el mensaje es muy corto o vacío, usar fallback
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    # Inyectar contexto del nombre para que Valentina lo use o lo pida
    INVALIDOS = {"", "desconocido", "none", "null", "cliente", "unknown"}
    nombre_valido = nombre_cliente and nombre_cliente.strip().lower() not in INVALIDOS
    if nombre_valido:
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
