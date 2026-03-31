# agent/tools.py — Herramientas del agente
# Generado por AgentKit para Conexion Sin Limites

"""
Herramientas específicas del negocio.
Valentina puede usar estas funciones para buscar información,
registrar leads y gestionar soporte post-venta.
"""

import os
import yaml
import logging
from datetime import datetime

logger = logging.getLogger("agentkit")

# Compañías disponibles en Conexion Sin Limites
COMPANIAS = ["DirecTV", "Movistar", "Entel", "WOM", "VTR", "Claro"]

# Tipos de servicio disponibles
SERVICIOS = ["Internet", "Televisión", "Dúo (Internet + TV)"]


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atención del negocio."""
    info = cargar_info_negocio()
    horario = info.get("negocio", {}).get("horario", {})
    return {
        "agente_24_7": horario.get("agente", "24/7"),
        "asesores_humanos": horario.get("asesores_humanos", "Lunes a Sábado 8am a 10pm, Domingo 10am a 7pm"),
        "esta_en_horario_humano": _verificar_horario_humano(),
    }


def _verificar_horario_humano() -> bool:
    """Verifica si actualmente hay asesores humanos disponibles."""
    ahora = datetime.now()
    dia_semana = ahora.weekday()  # 0=Lunes, 6=Domingo
    hora = ahora.hour

    if dia_semana <= 5:  # Lunes a Sábado
        return 8 <= hora < 22  # 8am a 10pm
    else:  # Domingo
        return 10 <= hora < 19  # 10am a 7pm


def obtener_companias() -> list[str]:
    """Retorna la lista de compañías con las que trabaja Conexion Sin Limites."""
    return COMPANIAS


def obtener_servicios() -> list[str]:
    """Retorna los tipos de servicio disponibles."""
    return SERVICIOS


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos de /knowledge.
    Retorna el contenido más relevante encontrado.
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


# ════════════════════════════════════════════════════
# HERRAMIENTAS DE CALIFICACIÓN DE LEADS
# ════════════════════════════════════════════════════

def registrar_lead(telefono: str, nombre: str, servicio: str, comuna: str, notas: str = "") -> str:
    """
    Registra un lead interesado en contratar un servicio.
    Guarda la información en un archivo de leads para seguimiento del equipo.

    Returns:
        ID del lead registrado
    """
    lead_id = f"LEAD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{telefono[-4:]}"
    lead = {
        "id": lead_id,
        "telefono": telefono,
        "nombre": nombre,
        "servicio_interes": servicio,
        "comuna": comuna,
        "notas": notas,
        "fecha": datetime.now().isoformat(),
        "estado": "nuevo",
    }

    # Guardar en archivo de leads (en producción usar base de datos)
    leads_file = "knowledge/leads.yaml"
    try:
        leads = []
        if os.path.exists(leads_file):
            with open(leads_file, "r", encoding="utf-8") as f:
                leads = yaml.safe_load(f) or []
        leads.append(lead)
        with open(leads_file, "w", encoding="utf-8") as f:
            yaml.dump(leads, f, allow_unicode=True, default_flow_style=False)
        logger.info(f"Lead registrado: {lead_id}")
    except Exception as e:
        logger.error(f"Error guardando lead: {e}")

    return lead_id


# ════════════════════════════════════════════════════
# HERRAMIENTAS DE SOPORTE POST-VENTA
# ════════════════════════════════════════════════════

def crear_ticket_soporte(telefono: str, problema: str, tipo: str = "consulta") -> str:
    """
    Crea un ticket de soporte post-venta.

    Args:
        telefono: Número del cliente
        problema: Descripción del problema o consulta
        tipo: "consulta", "reclamo", "instalacion", "facturacion"

    Returns:
        ID del ticket creado
    """
    ticket_id = f"TKT-{datetime.now().strftime('%Y%m%d%H%M%S')}-{telefono[-4:]}"
    ticket = {
        "id": ticket_id,
        "telefono": telefono,
        "problema": problema,
        "tipo": tipo,
        "fecha": datetime.now().isoformat(),
        "estado": "abierto",
    }

    tickets_file = "knowledge/tickets.yaml"
    try:
        tickets = []
        if os.path.exists(tickets_file):
            with open(tickets_file, "r", encoding="utf-8") as f:
                tickets = yaml.safe_load(f) or []
        tickets.append(ticket)
        with open(tickets_file, "w", encoding="utf-8") as f:
            yaml.dump(tickets, f, allow_unicode=True, default_flow_style=False)
        logger.info(f"Ticket creado: {ticket_id}")
    except Exception as e:
        logger.error(f"Error creando ticket: {e}")

    return ticket_id


def escalar_a_asesor(telefono: str, contexto: str) -> bool:
    """
    Marca una conversación para ser atendida por un asesor humano.
    Registra el contexto para que el asesor pueda tomar el caso informado.

    Returns:
        True si se escaló correctamente
    """
    escalacion = {
        "telefono": telefono,
        "contexto": contexto,
        "fecha": datetime.now().isoformat(),
        "estado": "pendiente",
        "en_horario": _verificar_horario_humano(),
    }

    escalaciones_file = "knowledge/escalaciones.yaml"
    try:
        escalaciones = []
        if os.path.exists(escalaciones_file):
            with open(escalaciones_file, "r", encoding="utf-8") as f:
                escalaciones = yaml.safe_load(f) or []
        escalaciones.append(escalacion)
        with open(escalaciones_file, "w", encoding="utf-8") as f:
            yaml.dump(escalaciones, f, allow_unicode=True, default_flow_style=False)
        logger.info(f"Escalación registrada para {telefono}")
        return True
    except Exception as e:
        logger.error(f"Error registrando escalación: {e}")
        return False
