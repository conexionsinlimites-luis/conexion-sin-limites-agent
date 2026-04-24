# agent/scheduler.py — Scheduler de follow-ups automáticos
# Conexion Sin Limites | Valentina

"""
Revisa cada 5 minutos la tabla followup_programado y envía los mensajes
pendientes por WhatsApp. Respeta la ventana horaria de Chile: 9:00am - 9:00pm.

Cadena automática:
  Valentina responde → programa 2h
  Si no hay respuesta en 2h  → envía y programa 24h
  Si no hay respuesta en 24h → envía y programa 3d
  Si no hay respuesta en 3d  → envía y programa 30d
  Si no hay respuesta en 30d → envía y programa 60d
  Si no hay respuesta en 60d → fin de cadena

En cualquier momento que el cliente responde, cancelar_followups() cancela
el pendiente y main.py reinicia la cadena desde 2h.
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import agent.crm as crm
from agent.templates_followup import get_mensaje_followup

logger = logging.getLogger("agentkit")

ZONA_CHILE     = ZoneInfo("America/Santiago")
HORA_INICIO    = 9   # 9:00am Chile
HORA_FIN       = 21  # 9:00pm Chile
INTERVALO_SECS = 5 * 60  # revisar cada 5 minutos

# Cadena de secuencia: qué tipo programar después de enviar cada uno
SIGUIENTE_FOLLOWUP: dict[str, str | None] = {
    "9h":   "24h",
    "24h":  "60h",
    "60h":  "720h",
    "720h": "1440h",
    "1440h": None,
}

TIPO_FOLLOWUP: dict[str, str] = {
    "9h":    "suave",
    "24h":   "medio",
    "60h":   "cierre",
    "720h":  "reactivacion",
    "1440h": "reactivacion",
}  # fin de la cadena


def _en_horario_chile() -> bool:
    """Retorna True si la hora actual en Chile está dentro de la ventana permitida."""
    ahora_chile = datetime.now(ZONA_CHILE)
    return HORA_INICIO <= ahora_chile.hour < HORA_FIN


def _rellenar_plantilla(mensaje: str, nombre: str, subproducto: str) -> str:
    """Reemplaza los placeholders {nombre}, {tema} y {empresa} del mensaje."""
    primer_nombre = (nombre or "").split()[0] if nombre else "Cliente"
    tema          = subproducto or "tu servicio de telecomunicaciones"
    empresa       = subproducto or "tu proveedor actual"
    return (
        mensaje
        .replace("{nombre}", primer_nombre)
        .replace("{tema}", tema)
        .replace("{empresa}", empresa)
    )


async def _procesar_followups(proveedor) -> int:
    """
    Obtiene los follow-ups listos para enviar y los despacha.
    Tras cada envío exitoso, encadena el siguiente tipo en la secuencia.
    Retorna la cantidad de mensajes enviados.
    """
    if not _en_horario_chile():
        ahora_chile = datetime.now(ZONA_CHILE)
        logger.debug(f"Scheduler: fuera de ventana Chile ({ahora_chile.strftime('%H:%M')}), saltando")
        return 0

    pendientes = await crm.obtener_followups_pendientes()
    if not pendientes:
        return 0

    logger.info(f"Scheduler: {len(pendientes)} follow-up(s) listo(s) para enviar")
    enviados = 0

    for followup in pendientes:
        telefono    = followup["telefono"]
        nombre      = followup.get("nombre", "Cliente")
        subproducto = followup.get("subproducto", "")
        mensaje_raw = followup["mensaje"]
        followup_id = followup["id"]
        tipo        = followup["tipo"]

        comuna = followup.get("comuna", "")
        tipo_followup = TIPO_FOLLOWUP.get(tipo, "suave")
        mensaje_template = get_mensaje_followup(tipo_followup, nombre.split()[0] if nombre else "Cliente", comuna or None)
        mensaje = mensaje_template if mensaje_template else _rellenar_plantilla(mensaje_raw, nombre, subproducto)

        try:
            ok = await proveedor.enviar_mensaje(telefono, mensaje)
            if ok:
                await crm.marcar_followup_enviado(followup_id)
                enviados += 1
                logger.info(f"Follow-up [{tipo}] enviado a {telefono}: '{mensaje[:60]}…'")

                # Encadenar el siguiente follow-up si el lead sigue activo
                siguiente = SIGUIENTE_FOLLOWUP.get(tipo)
                if siguiente:
                    try:
                        lead_info  = await crm.obtener_lead(telefono)
                        estado_lead = (lead_info or {}).get("estado", "")
                        if estado_lead not in ("cerrado", "modo_humano"):
                            await crm.programar_followup(telefono, siguiente)
                            logger.info(f"Encadenado follow-up [{siguiente}] para {telefono}")
                        else:
                            logger.info(f"Follow-up [{siguiente}] omitido — lead {telefono} en estado '{estado_lead}'")
                    except Exception as e:
                        logger.error(f"Error encadenando follow-up [{siguiente}] para {telefono}: {e}")
                else:
                    logger.info(f"Cadena de follow-ups completada para {telefono} (último: {tipo})")

            else:
                logger.error(f"Follow-up [{tipo}] a {telefono} falló — proveedor retornó False")
        except Exception as e:
            logger.error(f"Excepción enviando follow-up [{tipo}] a {telefono}: {e}")

    return enviados


async def iniciar_scheduler(proveedor):
    """
    Loop principal del scheduler. Se ejecuta como tarea asyncio en background.
    Cada 5 minutos revisa y envía follow-ups pendientes dentro del horario Chile.
    """
    logger.info("Scheduler de follow-ups iniciado (intervalo: 5 min | ventana: 9am-9pm Chile)")
    logger.info("Cadena: primera respuesta → 9h → 24h → 60h → 30d → 60d")

    while True:
        try:
            await _procesar_followups(proveedor)
        except Exception as e:
            logger.error(f"Error inesperado en scheduler: {e}")

        await asyncio.sleep(INTERVALO_SECS)

