# agent/make_integration.py — Integración con Make.com
# Conexion Sin Limites | Valentina

"""
Envía datos del lead al webhook de Make.com cada vez que Valentina
recibe un mensaje nuevo o actualiza el estado de un lead.
Fire-and-forget: no bloquea el flujo principal.
"""

import logging
from datetime import datetime

import httpx

logger = logging.getLogger("agentkit")

MAKE_WEBHOOK_URL = "https://hook.us2.make.com/ob7kw9569i28s62ujw7ie998zpjuudbp"


async def enviar_a_make(
    telefono: str,
    nombre: str,
    estado: str,
    score: int | float,
    producto: str,
    ultimo_mensaje: str,
    intencion: str,
    fecha: str | None = None,
) -> None:
    """
    Envía los datos del lead al webhook de Make.com.
    No lanza excepciones — los errores se loggean y se ignoran.
    """
    payload = {
        "telefono": telefono,
        "nombre": nombre or "",
        "estado": estado or "",
        "score": score or 0,
        "producto": producto or "",
        "ultimo_mensaje": ultimo_mensaje or "",
        "intencion": intencion or "",
        "fecha": fecha or datetime.utcnow().isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(MAKE_WEBHOOK_URL, json=payload)
            if r.status_code in (200, 201, 202, 204):
                logger.info(f"Make.com: lead {telefono} enviado (estado={estado})")
            else:
                logger.warning(f"Make.com: respuesta inesperada {r.status_code} para {telefono}")
    except Exception as e:
        logger.error(f"Make.com: error enviando lead {telefono}: {e}")
