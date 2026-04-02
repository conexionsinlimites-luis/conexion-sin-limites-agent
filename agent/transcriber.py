# agent/transcriber.py — Transcripción de audios con OpenAI Whisper
# Generado por AgentKit

"""
Recibe bytes de audio desde Meta API y los transcribe usando Whisper-1 de OpenAI.
Soporta los formatos de audio que envía WhatsApp: ogg/opus, mp4, etc.
"""

import io
import logging
from openai import AsyncOpenAI
from agent.config import OPENAI_API_KEY

logger = logging.getLogger("agentkit")

# Cliente de OpenAI (Whisper)
_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Extensión por mime_type para que Whisper identifique el formato
_MIME_A_EXT = {
    "audio/ogg": ".ogg",
    "audio/ogg; codecs=opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".mp4",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/aac": ".aac",
}


async def transcribir(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """
    Transcribe un audio usando OpenAI Whisper-1 en español.

    Args:
        audio_bytes: Contenido binario del archivo de audio
        mime_type: MIME type del audio (ej: "audio/ogg; codecs=opus")

    Returns:
        Texto transcrito, o mensaje de error si falla
    """
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY no configurada — no se puede transcribir")
        return ""

    # Determinar extensión según mime_type
    mime_base = mime_type.split(";")[0].strip().lower()
    extension = _MIME_A_EXT.get(mime_base, ".ogg")
    nombre_archivo = f"audio{extension}"

    try:
        # Whisper necesita un file-like object con nombre de archivo
        archivo = io.BytesIO(audio_bytes)
        archivo.name = nombre_archivo

        response = await _client.audio.transcriptions.create(
            model="whisper-1",
            file=archivo,
            language="es",
        )
        texto = response.text.strip()
        logger.info(f"Whisper transcribió: '{texto[:80]}'")
        return texto

    except Exception as e:
        logger.error(f"Error en transcripción Whisper: {e}")
        return ""
