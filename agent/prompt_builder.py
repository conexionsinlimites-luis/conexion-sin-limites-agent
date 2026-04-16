# agent/prompt_builder.py — Construcción dinámica del system prompt
# Conexión Sin Límites

"""
Arma el system prompt de Valentina combinando:
  1. Prompt base  → config/prompts.yaml (o override en config_json del cliente)
  2. Catálogo     → sección del YAML o reemplazo desde config_json
  3. Objeciones   → sección del YAML o reemplazo desde config_json
  4. Cierres      → sección del YAML o reemplazo desde config_json
  5. Estado lead  → datos frescos de PostgreSQL (no cacheados)
  6. Resumen      → lead_resumen + objeciones actuales del lead

Cache en memoria con TTL de 5 minutos para la config del cliente (config_json).
Los datos del lead se consultan siempre frescos — cambian en cada mensaje.

─────────────────────────────────────────────────────────────────
Esquema esperado en clientes.config_json (todos opcionales):
{
  "prompt_base":   "...",   // reemplaza system_prompt del YAML
  "catalogo":      "...",   // reemplaza la sección CATÁLOGO DEL YAML
  "objeciones":    "...",   // reemplaza la sección MANEJO DE OBJECIONES
  "cierres":       "...",   // reemplaza la sección cierres/reglas absolutas
  "nombre_agente": "...",   // nombre del agente (default: Valentina)
  "tono":          "..."    // descripción del tono (default: humano, cercano, chileno)
}
Si config_json está vacío o no tiene una clave, se usa el YAML como fallback.
─────────────────────────────────────────────────────────────────
"""

import json
import logging
import time
import yaml

from agent.database import get_pool

logger = logging.getLogger("agentkit")

# ── Cache en memoria ───────────────────────────────────────────
# Clave: cliente_slug  |  Valor: (config_dict, timestamp_unix)
_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300  # 5 minutos


# ── Carga del YAML base ────────────────────────────────────────

def _cargar_yaml() -> dict:
    """Lee config/prompts.yaml. Retorna {} si no existe."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("prompt_builder: config/prompts.yaml no encontrado")
        return {}


# ── Acceso a config_json del cliente (con cache) ───────────────

async def _obtener_config_cliente(cliente_slug: str) -> dict:
    """
    Devuelve el dict parseado de clientes.config_json para el slug dado.
    Usa cache de 5 minutos — evita consultar la BD en cada mensaje.
    """
    ahora = time.monotonic()
    entrada = _cache.get(cliente_slug)

    if entrada is not None:
        config, ts = entrada
        if ahora - ts < CACHE_TTL:
            return config
        # TTL expirado → borrar y recargar
        del _cache[cliente_slug]

    config = await _consultar_config_bd(cliente_slug)
    _cache[cliente_slug] = (config, ahora)
    return config


async def _consultar_config_bd(cliente_slug: str) -> dict:
    """Consulta clientes.config_json desde PostgreSQL."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            fila = await conn.fetchrow(
                "SELECT config_json FROM clientes WHERE slug = $1 AND activo = TRUE",
                cliente_slug,
            )

        if not fila or not fila["config_json"]:
            return {}

        raw = fila["config_json"]
        # config_json puede estar guardado como string JSON o como dict (jsonb)
        if isinstance(raw, str):
            return json.loads(raw)
        return dict(raw)

    except Exception as e:
        logger.error(f"prompt_builder: error al leer config_json de '{cliente_slug}': {e}")
        return {}


def invalidar_cache(cliente_slug: str):
    """
    Elimina la entrada cacheada de un cliente para forzar recarga inmediata.
    Útil cuando se actualiza config_json desde el dashboard o scripts.
    """
    _cache.pop(cliente_slug, None)
    logger.info(f"prompt_builder: cache invalidado para '{cliente_slug}'")


# ── Sección dinámica de estado del lead ───────────────────────

def _seccion_estado_lead(lead: dict | None) -> str:
    """
    Genera el bloque de contexto del lead para inyectar al final del prompt.
    Siempre se construye con datos frescos — no se cachea.
    """
    if not lead:
        return (
            "\n\n─────────────────────────────────────────────\n"
            "CONTEXTO DEL LEAD\n"
            "Lead nuevo — primera interacción. No hay historial previo.\n"
            "─────────────────────────────────────────────"
        )

    estado         = (lead.get("estado") or "nuevo").upper()
    score          = lead.get("score") or 0
    msgs_estado    = lead.get("mensajes_en_estado") or 0
    producto       = lead.get("subproducto") or lead.get("producto_principal") or ""
    comuna         = lead.get("comuna") or ""
    resumen        = (lead.get("lead_resumen") or "").strip()
    nombre         = (lead.get("nombre") or "").strip()

    # Objeciones guardadas como JSON array ["precio", "contrato"]
    objeciones_raw = lead.get("objeciones") or "[]"
    try:
        if isinstance(objeciones_raw, str):
            objeciones_list = json.loads(objeciones_raw)
        else:
            objeciones_list = list(objeciones_raw)
    except (json.JSONDecodeError, TypeError):
        objeciones_list = []

    # Tags guardadas como JSON array
    tags_raw = lead.get("tags") or "[]"
    try:
        if isinstance(tags_raw, str):
            tags_list = json.loads(tags_raw)
        else:
            tags_list = list(tags_raw)
    except (json.JSONDecodeError, TypeError):
        tags_list = []

    lineas = [
        "",
        "─────────────────────────────────────────────",
        "CONTEXTO ACTUAL DEL LEAD",
        "─────────────────────────────────────────────",
        f"Estado en CRM : {estado}",
        f"Score         : {score}/100",
        f"Mensajes en '{estado.lower()}': {msgs_estado}",
    ]

    if nombre:
        lineas.append(f"Nombre         : {nombre}")
    if producto:
        lineas.append(f"Producto/Plan  : {producto}")
    if comuna:
        lineas.append(f"Comuna         : {comuna}")
    if objeciones_list:
        lineas.append(f"Objeciones     : {', '.join(objeciones_list)}")
    if tags_list:
        lineas.append(f"Tags           : {', '.join(tags_list)}")
    if resumen:
        lineas.append(f"\nResumen de la conversación:")
        lineas.append(f'"{resumen}"')

    # Instrucciones operativas según estado actual
    instruccion = _instruccion_por_estado(estado, msgs_estado, objeciones_list)
    if instruccion:
        lineas.append(f"\n⚡ INSTRUCCIÓN ACTIVA: {instruccion}")

    lineas.append("─────────────────────────────────────────────")
    return "\n".join(lineas)


def _instruccion_por_estado(estado: str, msgs: int, objeciones: list) -> str:
    """
    Devuelve una instrucción operativa concreta según el estado y
    los mensajes acumulados. Refuerza la máquina de estados del prompt base.
    """
    estado = estado.upper()

    if estado == "DIRECCION_OBTENIDA":
        return "Dirección recibida. Confirmar datos y disparar alerta al supervisor AHORA."

    if estado == "LISTO_PARA_CIERRE":
        return "Lead listo. Enviar mensaje de cierre y confirmar que el supervisor contactará."

    if estado in ("NUEVO", "CONTACTADO") and msgs == 0:
        return "Primera interacción — NO ofrecer nada todavía. Generar confianza primero."

    if estado == "INTERESADO" and msgs >= 3:
        return (
            "ESTANCAMIENTO en INTERESADO. Cambiar estrategia: "
            "hacer UNA pregunta de cierre directa o simplificar la propuesta."
        )

    if estado == "TIBIO" and msgs >= 2:
        return (
            "ESTANCAMIENTO en TIBIO. Simplificar y empujar cierre: "
            "pedir dirección ahora para verificar cobertura."
        )

    if estado == "CALIENTE":
        return "Modo cierre activo — solo pedir dirección. No dar más información."

    if objeciones:
        return f"Resolver objeción '{objeciones[-1]}' antes de avanzar al siguiente paso."

    return ""


# ── Función principal ──────────────────────────────────────────

async def construir_prompt(
    telefono: str,
    cliente_slug: str = "csl",
    lead: dict | None = None,
) -> str:
    """
    Construye y devuelve el system prompt completo para una conversación.

    Combina en orden:
      1. Prompt base        → YAML o override en config_json["prompt_base"]
      2. Catálogo           → config_json["catalogo"] si existe (reemplaza sección del YAML)
      3. Objeciones extra   → config_json["objeciones"] si existe
      4. Cierres extra      → config_json["cierres"] si existe
      5. Estado del lead    → datos frescos (no cacheados)

    Args:
        telefono:     Teléfono del cliente (se usa para identificar el lead si no se pasa)
        cliente_slug: Slug del cliente en tabla `clientes` (default: "csl")
        lead:         Dict con datos del lead ya cargados. Si es None, se consulta la BD.

    Returns:
        String listo para usar como `system=` en la llamada a Claude API.
    """
    # 1. Cargar config del cliente (cacheada 5 min)
    config = await _obtener_config_cliente(cliente_slug)

    # 2. Prompt base: config_json["prompt_base"] tiene prioridad sobre el YAML
    if config.get("prompt_base"):
        prompt_base = config["prompt_base"].strip()
        logger.debug(f"prompt_builder: usando prompt_base de config_json para '{cliente_slug}'")
    else:
        yaml_data   = _cargar_yaml()
        prompt_base = yaml_data.get("system_prompt", "Eres un asistente útil. Responde en español.")
        prompt_base = prompt_base.strip()

    secciones = [prompt_base]

    # 3. Catálogo — reemplaza la sección si viene en config_json
    if config.get("catalogo"):
        secciones.append(
            "\n───────────────────────────────────────────────\n"
            "CATÁLOGO DE PLANES (configuración del cliente)\n"
            "───────────────────────────────────────────────\n"
            + config["catalogo"].strip()
        )
        logger.debug(f"prompt_builder: catálogo personalizado para '{cliente_slug}'")

    # 4. Objeciones extra — agrega o reemplaza
    if config.get("objeciones"):
        secciones.append(
            "\n───────────────────────────────────────────────\n"
            "MANEJO DE OBJECIONES (configuración del cliente)\n"
            "───────────────────────────────────────────────\n"
            + config["objeciones"].strip()
        )

    # 5. Cierres extra — agrega o reemplaza
    if config.get("cierres"):
        secciones.append(
            "\n───────────────────────────────────────────────\n"
            "TÉCNICAS DE CIERRE (configuración del cliente)\n"
            "───────────────────────────────────────────────\n"
            + config["cierres"].strip()
        )

    # 6. Estado del lead — siempre fresco
    if lead is None:
        lead = await _cargar_lead(telefono)

    secciones.append(_seccion_estado_lead(lead))

    return "\n".join(secciones)


async def _cargar_lead(telefono: str) -> dict | None:
    """Carga datos del lead desde PostgreSQL. Retorna None si no existe."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            fila = await conn.fetchrow(
                """
                SELECT estado, score, mensajes_en_estado, nombre,
                       subproducto, producto_principal, comuna,
                       objeciones, tags, lead_resumen
                FROM leads
                WHERE telefono = $1
                """,
                telefono,
            )
        return dict(fila) if fila else None
    except Exception as e:
        logger.error(f"prompt_builder: error al cargar lead '{telefono}': {e}")
        return None
