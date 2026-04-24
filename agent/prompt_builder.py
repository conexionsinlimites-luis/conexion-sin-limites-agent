# agent/prompt_builder.py — Construcción dinámica del system prompt
# Conexión Sin Límites

"""
Construye el system prompt para cada mensaje entrante combinando:

  RUTA A — BD disponible y config_json completo:
    prompt_base.txt  +  {catalogo}  +  {objeciones}  +  {cierres}
    tomados de clientes.config_json en PostgreSQL.

  RUTA B — Fallback (BD falla o config_json sin secciones):
    config/prompts.yaml  completo, sin modificaciones.

  En ambas rutas se agrega al final el bloque CONTEXTO DEL LEAD
  con datos frescos de la tabla leads (estado, score, resumen, etc.).

Cache en memoria con TTL de 5 minutos, indexado por cliente_id cuando
está disponible o por cliente_slug en caso contrario.
Los datos del lead nunca se cachean — cambian en cada mensaje.

─────────────────────────────────────────────────────────────────
Esquema esperado en clientes.config_json:
{
  "nombre_agente": "Valentina",
  "tono":          "cercano, vendedor, seguro, humano",
  "catalogo":      "MOVISTAR FIBRA...",
  "objeciones":    "\"Está caro\":\\n→ ...",
  "cierres":       "Cierre suave — ..."
}
Si faltan "catalogo", "objeciones" o "cierres" se usa el YAML completo.
─────────────────────────────────────────────────────────────────
"""

import json
import logging
import time
import yaml

from agent.database import get_pool

logger = logging.getLogger("agentkit")

# ── Cache en memoria ───────────────────────────────────────────
# Clave: "id:<cliente_id>" o "slug:<cliente_slug>"
# Valor: (config_dict, timestamp_monotonic)
_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300  # 5 minutos

# Secciones que deben estar todas presentes en config_json
# para usar la Ruta A. Si falta alguna se cae a Ruta B (YAML).
_SECCIONES_REQUERIDAS = ("catalogo", "objeciones", "cierres")


# ── Lectura de archivos de configuración ──────────────────────

def _cargar_yaml() -> dict:
    """Lee config/prompts.yaml. Retorna {} si no existe."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("prompt_builder: config/prompts.yaml no encontrado")
        return {}


def _cargar_prompt_base_txt() -> str:
    """
    Lee config/prompt_base.txt — template con placeholders:
    {agente_nombre}, {tono}, {catalogo}, {objeciones}, {cierres},
    {estado}, {resumen}.
    Retorna None si no existe.
    """
    try:
        with open("config/prompt_base.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        import os
        logger.warning(f"prompt_builder: config/prompt_base.txt no encontrado — cwd={os.getcwd()}")
        return None


# ── Cache y acceso a config_json del cliente ──────────────────

def _cache_key(cliente_id: int | None, cliente_slug: str) -> str:
    """Genera la clave de cache. Prefiere id cuando está disponible."""
    if cliente_id is not None:
        return f"id:{cliente_id}"
    return f"slug:{cliente_slug}"


async def _obtener_config_cliente(
    cliente_slug: str,
    cliente_id: int | None = None,
) -> dict:
    """
    Devuelve el dict de clientes.config_json para el cliente indicado.
    Usa cache de 5 minutos para no consultar la BD en cada mensaje.
    En caso de error de BD retorna {} (activando el fallback al YAML).
    """
    key   = _cache_key(cliente_id, cliente_slug)
    ahora = time.monotonic()

    entrada = _cache.get(key)
    if entrada is not None:
        config, ts = entrada
        if ahora - ts < CACHE_TTL:
            return config
        del _cache[key]

    config = await _consultar_config_bd(cliente_slug, cliente_id)
    _cache[key] = (config, ahora)
    return config


async def _consultar_config_bd(
    cliente_slug: str,
    cliente_id: int | None = None,
) -> dict:
    """Consulta clientes.config_json. Usa id si está disponible, sino slug."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if cliente_id is not None:
                fila = await conn.fetchrow(
                    "SELECT config_json FROM clientes WHERE id = $1 AND activo = TRUE",
                    cliente_id,
                )
            else:
                fila = await conn.fetchrow(
                    "SELECT config_json FROM clientes WHERE slug = $1 AND activo = TRUE",
                    cliente_slug,
                )

        if not fila or not fila["config_json"]:
            return {}

        raw = fila["config_json"]
        if isinstance(raw, str):
            return json.loads(raw)
        return dict(raw)

    except Exception as e:
        logger.error(
            f"prompt_builder: error al leer config_json "
            f"(id={cliente_id}, slug={cliente_slug!r}): {e}"
        )
        return {}   # Ruta B — fallback al YAML


def invalidar_cache(cliente_slug: str = "", cliente_id: int | None = None):
    """
    Elimina la entrada cacheada para forzar recarga inmediata.
    Acepta cliente_slug, cliente_id, o ambos.
    """
    removed = 0
    for key in (f"id:{cliente_id}", f"slug:{cliente_slug}"):
        if _cache.pop(key, None) is not None:
            removed += 1
    if removed:
        logger.info(f"prompt_builder: cache invalidado (id={cliente_id}, slug={cliente_slug!r})")


# ── Bloque de contexto del lead (siempre fresco) ──────────────

def _estado_inline(lead: dict) -> str:
    """
    Genera una representación compacta del estado del lead
    para rellenar el placeholder {estado} de prompt_base.txt.
    """
    estado      = (lead.get("estado") or "nuevo").upper()
    score       = lead.get("score") or 0
    msgs        = lead.get("mensajes_en_estado") or 0
    producto    = lead.get("subproducto") or lead.get("producto_principal") or ""
    comuna      = lead.get("comuna") or ""

    objeciones_raw = lead.get("objeciones") or "[]"
    try:
        obs = json.loads(objeciones_raw) if isinstance(objeciones_raw, str) else list(objeciones_raw)
    except (json.JSONDecodeError, TypeError):
        obs = []

    lineas = [f"Estado: {estado} | Score: {score}/100 | Mensajes en estado: {msgs}"]
    if producto:
        lineas.append(f"Producto/Plan: {producto}")
    if comuna:
        lineas.append(f"Comuna: {comuna}")
    if obs:
        lineas.append(f"Objeciones detectadas: {', '.join(obs)}")

    instruccion = _instruccion_por_estado(estado, msgs, obs)
    if instruccion:
        lineas.append(f"INSTRUCCION ACTIVA: {instruccion}")

    lineas.append("\nREGLA OBLIGATORIA: Cuando el cliente entregue una direccion completa (calle, numero y comuna), incluye AL FINAL de tu respuesta este marcador exacto (el cliente NO lo ve): [ALERTA_SUPERVISOR|nombre=NOMBRE_REAL|tel=TELEFONO_REAL|dir=DIRECCION_COMPLETA] Reemplaza con los datos reales del lead. OBLIGATORIO sin excepcion.")

    return "\n".join(lineas)


def _seccion_estado_lead(
    lead: dict | None,
    estado_override: str | None = None,
    resumen_override: str | None = None,
) -> str:
    """
    Genera el bloque completo CONTEXTO DEL LEAD para anexar al prompt.
    Se usa en la Ruta B (YAML fallback) o cuando se quiere el bloque completo.
    estado_override y resumen_override permiten pasar valores ya extraídos.
    """
    if not lead and not estado_override:
        return (
            "\n\n─────────────────────────────────────────────\n"
            "CONTEXTO DEL LEAD\n"
            "Lead nuevo — primera interacción. No hay historial previo.\n"
            "─────────────────────────────────────────────"
        )

    if lead:
        estado   = estado_override or (lead.get("estado") or "nuevo").upper()
        score    = lead.get("score") or 0
        msgs     = lead.get("mensajes_en_estado") or 0
        producto = lead.get("subproducto") or lead.get("producto_principal") or ""
        comuna   = lead.get("comuna") or ""
        resumen  = resumen_override or (lead.get("lead_resumen") or "").strip()
        nombre   = (lead.get("nombre") or "").strip()

        tags_raw = lead.get("tags") or "[]"
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else list(tags_raw)
        except (json.JSONDecodeError, TypeError):
            tags = []

        objeciones_raw = lead.get("objeciones") or "[]"
        try:
            obs = json.loads(objeciones_raw) if isinstance(objeciones_raw, str) else list(objeciones_raw)
        except (json.JSONDecodeError, TypeError):
            obs = []
    else:
        estado   = (estado_override or "nuevo").upper()
        score    = 0
        msgs     = 0
        producto = ""
        comuna   = ""
        resumen  = resumen_override or ""
        nombre   = ""
        tags     = []
        obs      = []

    lineas = [
        "",
        "─────────────────────────────────────────────",
        "CONTEXTO ACTUAL DEL LEAD",
        "─────────────────────────────────────────────",
        f"Estado en CRM : {estado}",
        f"Score         : {score}/100",
        f"Mensajes en '{estado.lower()}': {msgs}",
    ]
    if nombre:
        lineas.append(f"Nombre         : {nombre}")
    if producto:
        lineas.append(f"Producto/Plan  : {producto}")
    if comuna:
        lineas.append(f"Comuna         : {comuna}")
    if obs:
        lineas.append(f"Objeciones     : {', '.join(obs)}")
    if tags:
        lineas.append(f"Tags           : {', '.join(tags)}")
    if resumen:
        lineas.append(f'\nResumen: "{resumen}"')

    instruccion = _instruccion_por_estado(estado, msgs, obs)
    if instruccion:
        lineas.append(f"\nINSTRUCCION ACTIVA: {instruccion}")

    lineas.append("─────────────────────────────────────────────")
    return "\n".join(lineas)


def _instruccion_por_estado(estado: str, msgs: int, objeciones: list) -> str:
    """Instrucción operativa concreta según el estado y mensajes acumulados."""
    estado = estado.upper()

    if estado == "DIRECCION_OBTENIDA":
        return "Dirección recibida. Confirmar datos al cliente y en la MISMA respuesta incluir OBLIGATORIAMENTE este marcador exacto al final (invisible para el cliente):\n[ALERTA_SUPERVISOR|nombre=NOMBRE|tel=TELEFONO|dir=DIRECCION]\nReemplaza NOMBRE, TELEFONO y DIRECCION con los datos reales del lead."
    if estado == "LISTO_PARA_CIERRE":
        return ("Lead ya cerrado. Si el cliente vuelve a escribir, NO lo trates como nuevo. "
                "Salúdalo por su nombre, pregúntale cómo le fue con el supervisor o si necesita algo más. "
                "NUNCA vuelvas a pedir datos que ya tienes.")
    if estado in ("NUEVO", "CONTACTADO") and msgs == 0:
        return "Primera interacción — NO ofrecer nada todavía. Generar confianza primero."
    if estado == "INTERESADO" and msgs >= 3:
        return (
            "ESTANCAMIENTO en INTERESADO. Hacer UNA pregunta de cierre directa "
            "o simplificar la propuesta."
        )
    if estado == "TIBIO" and msgs >= 2:
        return "ESTANCAMIENTO en TIBIO. Pedir dirección ahora para verificar cobertura."
    if estado == "CALIENTE":
        return "Modo cierre activo — solo pedir dirección. No dar más información."
    if objeciones:
        return f"Resolver objeción '{objeciones[-1]}' antes de avanzar al siguiente paso."
    return ""


# ── Función principal ──────────────────────────────────────────

async def construir_prompt(
    telefono: str,
    cliente_slug: str = "csl",
    cliente_id: int | None = None,
    lead: dict | None = None,
) -> str:
    """
    Construye el system prompt completo para una conversación entrante.

    Ruta A — config_json completo en BD:
      Rellena los placeholders de config/prompt_base.txt con los valores
      de clientes.config_json (catalogo, objeciones, cierres, agente_nombre, tono)
      y agrega el bloque de contexto del lead al final.

    Ruta B — Fallback (BD falla o config_json incompleto):
      Usa config/prompts.yaml completo (system_prompt) y agrega el bloque
      de contexto del lead al final. El comportamiento es idéntico al
      sistema anterior, garantizando continuidad del servicio.

    Args:
        telefono:     Teléfono del cliente. Se usa para cargar el lead si
                      no se pasa el parámetro `lead`.
        cliente_slug: Slug del cliente en tabla `clientes` (default: "csl").
        cliente_id:   ID entero del cliente — lookup más rápido que por slug.
                      Si se provee, tiene prioridad sobre cliente_slug en la BD.
        lead:         Dict con datos del lead ya cargados desde main.py.
                      Si es None, se consulta la tabla leads por teléfono.

    Returns:
        String listo para usar como parámetro `system=` en la API de Claude.
    """
    # ── 1. Cargar datos del lead (frescos, sin cache) ──────────
    if lead is None:
        lead = await _cargar_lead(telefono)

    estado  = (lead.get("estado") or "nuevo")   if lead else "nuevo"
    resumen = (lead.get("lead_resumen") or "")   if lead else ""

    # ── 2. Cargar config del cliente (cacheada 5 min) ──────────
    config = await _obtener_config_cliente(cliente_slug, cliente_id)

    tiene_secciones = all(config.get(s) for s in _SECCIONES_REQUERIDAS)

    # ── 3A. RUTA A — BD disponible y config_json completo ──────
    if tiene_secciones:
        template = _cargar_prompt_base_txt()

        if template:
            try:
                prompt = template.format(
                    agente_nombre = config.get("nombre_agente", "Valentina"),
                    tono          = config.get("tono", "cercano, vendedor, seguro, humano"),
                    catalogo      = config["catalogo"].strip(),
                    objeciones    = config["objeciones"].strip(),
                    cierres       = config["cierres"].strip(),
                    estado        = _estado_inline(lead) if lead else f"Estado: {estado.upper()}",
                    resumen       = resumen or "(sin resumen disponible aún)",
                )
                # Agregar bloque completo de contexto al final
                supervisor_inst = config.get("supervisor_instruccion", "")
                if supervisor_inst:
                    prompt += f"\n\nSUPERVISOR: {supervisor_inst}"
                prompt += _seccion_estado_lead(lead, estado, resumen)
                logger.debug(
                    f"prompt_builder: Ruta A — template+config_json "
                    f"(id={cliente_id}, slug={cliente_slug!r})"
                )
                return prompt
            except KeyError as e:
                logger.warning(
                    f"prompt_builder: placeholder faltante en prompt_base.txt: {e} "
                    f"— cayendo a Ruta B"
                )
        else:
            logger.warning(
                "prompt_builder: prompt_base.txt no encontrado — cayendo a Ruta B"
            )

    # ── 3B. RUTA B — Fallback al YAML completo ─────────────────
    logger.debug(
        f"prompt_builder: Ruta B — YAML fallback "
        f"(tiene_secciones={tiene_secciones}, id={cliente_id}, slug={cliente_slug!r})"
    )
    yaml_data = _cargar_yaml()
    base = yaml_data.get(
        "system_prompt",
        "Eres un asistente útil de ventas. Responde en español.",
    ).strip()

    # Agregar bloque de contexto del lead al final del YAML
    base += _seccion_estado_lead(lead, estado, resumen)
    return base


async def _cargar_lead(telefono: str) -> dict | None:
    """Carga datos del lead desde PostgreSQL. Retorna None si no existe."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            fila = await conn.fetchrow(
                """
                SELECT estado, score, mensajes_en_estado, nombre,
                       subproducto, producto_principal, comuna,
                       objeciones, tags, lead_resumen, cliente_id
                FROM leads
                WHERE telefono = $1
                """,
                telefono,
            )
        return dict(fila) if fila else None
    except Exception as e:
        logger.error(f"prompt_builder: error al cargar lead '{telefono}': {e}")
        return None
