# agent/scheduler.py — Scheduler de follow-ups automáticos
# Conexion Sin Limites | Valentina

"""
Revisa cada 5 minutos la tabla followup_programado y envía los mensajes
pendientes por WhatsApp. Respeta la ventana horaria de Chile: 9:00am - 9:00pm.
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import agent.crm as crm

logger = logging.getLogger("agentkit")

ZONA_CHILE     = ZoneInfo("America/Santiago")
HORA_INICIO    = 9   # 9:00am
HORA_FIN       = 21  # 9:00pm
INTERVALO_SECS = 5 * 60  # 5 minutos


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
    Retorna la cantidad de mensajes enviados.
    """
    if not _en_horario_chile():
        ahora_chile = datetime.now(ZONA_CHILE)
        logger.debug(f"Scheduler: fuera de ventana horaria Chile ({ahora_chile.strftime('%H:%M')}), saltando")
        return 0

    pendientes = await crm.obtener_followups_pendientes()
    if not pendientes:
        return 0

    logger.info(f"Scheduler: {len(pendientes)} follow-up(s) pendiente(s)")
    enviados = 0

    for followup in pendientes:
        telefono    = followup["telefono"]
        nombre      = followup.get("nombre", "Cliente")
        subproducto = followup.get("subproducto", "")
        mensaje_raw = followup["mensaje"]
        followup_id = followup["id"]

        mensaje = _rellenar_plantilla(mensaje_raw, nombre, subproducto)

        try:
            ok = await proveedor.enviar_mensaje(telefono, mensaje)
            if ok:
                await crm.marcar_followup_enviado(followup_id)
                enviados += 1
                logger.info(f"Follow-up enviado a {telefono} (tipo: {followup['tipo']})")
            else:
                logger.error(f"Error enviando follow-up a {telefono} — proveedor retornó False")
        except Exception as e:
            logger.error(f"Excepcion enviando follow-up a {telefono}: {e}")

    return enviados


async def iniciar_scheduler(proveedor):
    """
    Loop principal del scheduler. Se ejecuta como tarea asyncio en background.
    Cada 5 minutos revisa y envía follow-ups pendientes dentro del horario Chile.
    """
    logger.info("Scheduler de follow-ups iniciado (intervalo: 5 min | ventana: 9am-9pm Chile)")

    while True:
        try:
            await _procesar_followups(proveedor)
        except Exception as e:
            logger.error(f"Error inesperado en scheduler: {e}")

        await asyncio.sleep(INTERVALO_SECS)
