# agent/dashboard.py — Dashboard web del CRM Valentina
# Conexion Sin Limites

"""
Expone tres rutas:
  GET /dashboard         → página HTML del dashboard
  GET /api/stats         → KPIs y distribución por estado
  GET /api/leads         → últimos 20 leads
  GET /api/messages      → últimos 30 mensajes del historial CRM
"""

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import secrets
import time
import traceback
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from agent.config import TELEFONO_OWNER, DASHBOARD_USER, DASHBOARD_PASSWORD
from agent.database import get_pool
import agent.crm as _crm
import agent.campanas as _campanas
from agent.memory import guardar_mensaje as _guardar_memoria

logger = logging.getLogger("agentkit")

# ── Autenticación por cookie firmada ──────────────────────────────────────────
_COOKIE_NAME = "vcrm_session"
_COOKIE_DAYS = 30


def _firmar_token(ts: str) -> str:
    """HMAC-SHA256 del timestamp usando DASHBOARD_PASSWORD como clave."""
    return hmac.new(
        DASHBOARD_PASSWORD.encode("utf-8"),
        ts.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _generar_cookie() -> str:
    """Genera un token firmado: '{timestamp}.{hmac}'."""
    ts = str(int(time.time()))
    return f"{ts}.{_firmar_token(ts)}"


def _es_sesion_valida(token: str) -> bool:
    """Verifica firma y expiración (30 días) del token de sesión."""
    if not DASHBOARD_PASSWORD or not token:
        return False
    try:
        ts, sig = token.split(".", 1)
        if time.time() - int(ts) > _COOKIE_DAYS * 86400:
            return False
        return hmac.compare_digest(sig, _firmar_token(ts))
    except Exception:
        return False


def _verificar_auth(request: Request):
    """
    Dependency para rutas protegidas.
    - API paths (/api/*): devuelve 401 JSON si no hay sesión válida.
    - HTML paths: redirige a /login con 307.
    """
    if not DASHBOARD_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard no disponible: configura DASHBOARD_PASSWORD en Railway.",
        )
    token = request.cookies.get(_COOKIE_NAME, "")
    if not _es_sesion_valida(token):
        if request.url.path.startswith("/api/"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sesión expirada. Recarga el dashboard.",
            )
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )


# Router protegido (todas las rutas requieren sesión válida)
router = APIRouter(dependencies=[Depends(_verificar_auth)])

# Router público (login / logout — sin autenticación)
public_router = APIRouter()

# ── SSE broadcast system ───────────────────────────────────────────────────────
_sse_queues: set[asyncio.Queue] = set()


async def broadcast_event(data: dict):
    """Emite un evento SSE a todos los clientes conectados al Live Chat."""
    global _sse_queues
    if not _sse_queues:
        return
    payload = json.dumps(data, ensure_ascii=False)
    muertos: set[asyncio.Queue] = set()
    for q in _sse_queues:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            muertos.add(q)
    _sse_queues -= muertos


# ── Proveedor WhatsApp (lazy init para el dashboard) ─────────────────────────
_proveedor_wa = None


def _get_proveedor():
    global _proveedor_wa
    if _proveedor_wa is None:
        from agent.providers import obtener_proveedor
        _proveedor_wa = obtener_proveedor()
    return _proveedor_wa

# ── Prioridad visual por estado y score ───────────────────────────────────────
def calcular_prioridad(estado: str, score: int) -> str:
    """🔴 caliente  🟡 tibio  ⚪ frío  🟣 modo_humano — basado en estado y score."""
    if estado == "modo_humano":
        return "🟣"
    if estado in ("caliente", "listo_para_cierre", "direccion_obtenida") or score >= 70:
        return "🔴"
    if estado in ("tibio", "interesado") or score >= 40:
        return "🟡"
    return "⚪"


# ── Colores por estado (para el frontend) ─────────────────────────────────────
COLOR_ESTADO = {
    "nuevo":               "#555555",
    "contactado":          "#3498db",
    "interesado":          "#9b59b6",
    "tibio":               "#e67e22",
    "caliente":            "#e74c3c",
    "direccion_obtenida":  "#1abc9c",
    "listo_para_cierre":   "#c9a227",
    "cerrado":             "#2ecc71",
    "seguimiento":         "#7f8c8d",
    "modo_humano":         "#a855f7",
}

# ── API: estadísticas ──────────────────────────────────────────────────────────

@router.get("/api/stats")
async def api_stats():
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_leads = await conn.fetchval("SELECT COUNT(*) FROM leads")

        leads_calientes = await conn.fetchval(
            "SELECT COUNT(*) FROM leads WHERE estado IN ('caliente','listo_para_cierre')"
        )
        leads_cerrados = await conn.fetchval(
            "SELECT COUNT(*) FROM leads WHERE estado = 'cerrado'"
        )
        score_raw = await conn.fetchval(
            "SELECT ROUND(AVG(score::numeric), 1) FROM leads"
        )
        score_promedio = float(score_raw) if score_raw else 0

        rows = await conn.fetch(
            "SELECT estado, COUNT(*) AS total FROM leads GROUP BY estado ORDER BY total DESC"
        )
        por_estado = [
            {"estado": r["estado"], "total": r["total"], "color": COLOR_ESTADO.get(r["estado"], "#888")}
            for r in rows
        ]

        try:
            followups_pendientes = await conn.fetchval(
                "SELECT COUNT(*) FROM followup_programado WHERE enviado=0 AND cancelado=0"
            ) or 0
        except Exception:
            followups_pendientes = 0

        hoy_dt = datetime.combine(date.today(), datetime.min.time())
        mensajes_hoy = await conn.fetchval(
            "SELECT COUNT(*) FROM historial_mensajes WHERE timestamp >= $1", hoy_dt
        )

        # Tasas de conversión (excluye modo_humano)
        r = await conn.fetchrow("""
            SELECT
              COUNT(CASE WHEN estado IN (
                'contactado','interesado','tibio','caliente',
                'direccion_obtenida','listo_para_cierre','cerrado'
              ) THEN 1 END) AS n_contactado,
              COUNT(CASE WHEN estado IN (
                'interesado','tibio','caliente',
                'direccion_obtenida','listo_para_cierre','cerrado'
              ) THEN 1 END) AS n_interesado,
              COUNT(CASE WHEN estado IN (
                'caliente','direccion_obtenida','listo_para_cierre','cerrado'
              ) THEN 1 END) AS n_caliente,
              COUNT(CASE WHEN estado IN ('listo_para_cierre','cerrado') THEN 1 END) AS n_cierre,
              COUNT(CASE WHEN estado = 'cerrado' THEN 1 END) AS n_cerrado
            FROM leads WHERE estado != 'modo_humano'
        """)
        n_contactado = r["n_contactado"] or 0
        n_interesado = r["n_interesado"] or 0
        n_caliente   = r["n_caliente"]   or 0
        n_cierre     = r["n_cierre"]     or 0

        def tasa(num, den):
            return round(num / den * 100, 1) if den else 0

        conversion = {
            "contactado_interesado": {
                "label": "Contactado → Interesado",
                "pct": tasa(n_interesado, n_contactado),
                "num": n_interesado, "den": n_contactado,
            },
            "interesado_caliente": {
                "label": "Interesado → Caliente",
                "pct": tasa(n_caliente, n_interesado),
                "num": n_caliente, "den": n_interesado,
            },
            "caliente_cierre": {
                "label": "Caliente → Cierre",
                "pct": tasa(n_cierre, n_caliente),
                "num": n_cierre, "den": n_caliente,
            },
        }

    # Contador "Sin Respuesta":
    # Caso A — historial existe y el último mensaje fue del agente
    # Caso B — estado='contactado' sin ningún mensaje en historial (envio_masivo)
    try:
        sin_respuesta_count = await conn.fetchval("""
            SELECT
              (SELECT COUNT(DISTINCT SPLIT_PART(REPLACE(tel,' ',''),'@',1))
               FROM (
                   SELECT DISTINCT ON (SPLIT_PART(REPLACE(telefono,' ',''),'@',1))
                       SPLIT_PART(REPLACE(telefono,' ',''),'@',1) AS tel,
                       rol
                   FROM historial_mensajes
                   ORDER BY SPLIT_PART(REPLACE(telefono,' ',''),'@',1), timestamp DESC
               ) last_msg
               LEFT JOIN leads l
                 ON SPLIT_PART(REPLACE(l.telefono,' ',''),'@',1) = last_msg.tel
               WHERE last_msg.rol = 'assistant'
                 AND (l.estado IS NULL OR l.estado NOT IN ('cerrado','modo_humano'))
                 AND (l.tags IS NULL OR l.tags NOT LIKE '%Incontactable%')
              )
              +
              (SELECT COUNT(*) FROM leads l
               WHERE l.estado = 'contactado'
                 AND (l.tags IS NULL OR l.tags NOT LIKE '%Incontactable%')
                 AND NOT EXISTS (
                     SELECT 1 FROM historial_mensajes hm
                     WHERE SPLIT_PART(REPLACE(hm.telefono,' ',''),'@',1)
                         = SPLIT_PART(REPLACE(l.telefono,' ',''),'@',1)
                 )
              )
        """) or 0
    except Exception:
        sin_respuesta_count = 0

    return JSONResponse({
        "total_leads":          total_leads,
        "leads_calientes":      leads_calientes,
        "leads_cerrados":       leads_cerrados,
        "score_promedio":       score_promedio,
        "por_estado":           por_estado,
        "followups_pendientes": followups_pendientes,
        "mensajes_hoy":         mensajes_hoy,
        "conversion":           conversion,
        "sin_respuesta_count":  int(sin_respuesta_count),
        "actualizado":          datetime.now().strftime("%H:%M:%S"),
    })


# ── API: estadísticas de campañas ─────────────────────────────────────────────

FUNNEL_ORDER = [
    "nuevo", "contactado", "interesado", "tibio", "caliente",
    "direccion_obtenida", "listo_para_cierre", "cerrado", "seguimiento",
]

@router.get("/api/stats/campanas")
async def api_stats_campanas():
    pool = await get_pool()
    async with pool.acquire() as conn:

        # ── Embudo por estado en orden de funnel ──────────────────────────────
        estado_rows = await conn.fetch(
            "SELECT estado, COUNT(*) AS total FROM leads GROUP BY estado"
        )
        conteo = {r["estado"]: r["total"] for r in estado_rows}
        total_leads = sum(conteo.values()) or 1
        embudo = [
            {
                "estado": e,
                "total":  conteo.get(e, 0),
                "pct_total": round(conteo.get(e, 0) / total_leads * 100, 1),
                "color": COLOR_ESTADO.get(e, "#888"),
            }
            for e in FUNNEL_ORDER if conteo.get(e, 0) > 0
        ]

        # ── Tasa de respuesta a follow-ups ────────────────────────────────────
        try:
            fu_rows = await conn.fetch("""
                WITH enviados AS (
                    SELECT id, telefono, tipo, programado_para
                    FROM followup_programado WHERE enviado = 1
                ),
                respondidos AS (
                    SELECT DISTINCT e.id
                    FROM enviados e
                    JOIN historial_mensajes hm
                      ON hm.telefono = e.telefono
                     AND hm.rol = 'user'
                     AND hm.timestamp > e.programado_para
                )
                SELECT
                    e.tipo,
                    COUNT(*)           AS enviados,
                    COUNT(r.id)        AS respondidos
                FROM enviados e
                LEFT JOIN respondidos r ON r.id = e.id
                GROUP BY e.tipo
                ORDER BY e.tipo
            """)
            tipo_orden = ["2h", "24h", "3d", "30d", "60d"]
            por_tipo = []
            total_env = total_resp = 0
            tipo_data = {r["tipo"]: dict(r) for r in fu_rows}
            for t in tipo_orden:
                if t not in tipo_data:
                    continue
                td = tipo_data[t]
                env  = td["enviados"]  or 0
                resp = td["respondidos"] or 0
                total_env  += env
                total_resp += resp
                por_tipo.append({
                    "tipo": t,
                    "enviados":    env,
                    "respondidos": resp,
                    "tasa": round(resp / env * 100, 1) if env else 0,
                })
            followups = {
                "total_enviados":    total_env,
                "total_respondidos": total_resp,
                "tasa": round(total_resp / total_env * 100, 1) if total_env else 0,
                "por_tipo": por_tipo,
            }
        except Exception:
            followups = {"total_enviados": 0, "total_respondidos": 0, "tasa": 0, "por_tipo": []}

        # ── Leads por día (últimos 14 días) ───────────────────────────────────
        dia_rows = await conn.fetch("""
            SELECT
                (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Santiago')::date AS dia,
                COUNT(*) AS total
            FROM leads
            WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '14 days'
            GROUP BY dia
            ORDER BY dia
        """)
        # Completar días sin leads con 0 para que el gráfico no tenga huecos
        from datetime import timedelta
        hoy_chile = datetime.now().date()
        dia_map = {str(r["dia"]): r["total"] for r in dia_rows}
        leads_por_dia = [
            {"dia": str(hoy_chile - timedelta(days=13 - i)),
             "total": dia_map.get(str(hoy_chile - timedelta(days=13 - i)), 0)}
            for i in range(14)
        ]

        # ── Top productos de interés ──────────────────────────────────────────
        prod_rows = await conn.fetch("""
            SELECT COALESCE(NULLIF(TRIM(subproducto), ''), 'Sin especificar') AS producto,
                   COUNT(*) AS total
            FROM leads
            GROUP BY producto
            ORDER BY total DESC
            LIMIT 8
        """)
        top_productos = [{"producto": r["producto"], "total": r["total"]} for r in prod_rows]

    return JSONResponse({
        "embudo":        embudo,
        "followups":     followups,
        "leads_por_dia": leads_por_dia,
        "top_productos": top_productos,
    })


# ── API: campañas ─────────────────────────────────────────────────────────────

@router.get("/api/campanas")
async def api_listar_campanas():
    """Lista todas las campañas ordenadas por fecha de creación."""
    campanas = await _campanas.listar_campanas()
    return JSONResponse({"campanas": campanas})


@router.post("/api/campanas")
async def api_crear_campana(request: Request):
    """Crea una campaña con sus destinatarios (estado: borrador)."""
    body = await request.json()
    nombre  = (body.get("nombre") or "").strip()[:120]
    mensaje = (body.get("mensaje") or "").strip()[:2000]
    if not nombre or not mensaje:
        raise HTTPException(status_code=400, detail="nombre y mensaje son requeridos")
    filtros = {
        "tag":       (body.get("tag") or "").strip(),
        "estado":    (body.get("estado") or "").strip(),
        "score_min": body.get("score_min") or 0,
        "comuna":    (body.get("comuna") or "").strip(),
        "desde":     (body.get("desde") or "").strip(),
        "hasta":     (body.get("hasta") or "").strip(),
    }
    campana_id = await _campanas.crear_campana(nombre, mensaje, filtros)
    return JSONResponse({"ok": True, "id": campana_id})


@router.get("/api/campanas/preview")
async def api_preview_campana(
    tag: str = "", estado: str = "", score_min: int = 0,
    comuna: str = "", desde: str = "", hasta: str = "",
):
    """Vista previa del número de leads que recibirán la campaña."""
    filtros = {
        "tag": tag, "estado": estado, "score_min": score_min,
        "comuna": comuna, "desde": desde, "hasta": hasta,
    }
    data = await _campanas.preview_destinatarios(filtros)
    return JSONResponse(data)


@router.get("/api/campanas/{campana_id}")
async def api_obtener_campana(campana_id: int):
    """Detalle completo de una campaña."""
    campana = await _campanas.obtener_campana(campana_id)
    if not campana:
        raise HTTPException(status_code=404, detail="Campaña no encontrada")
    return JSONResponse(campana)



@router.post("/api/campanas/{campana_id}/pausar")
async def api_pausar_campana(campana_id: int):
    resultado = await _campanas.pausar_campana(campana_id)
    return JSONResponse(resultado)

@router.post("/api/campanas/{campana_id}/reanudar")
async def api_reanudar_campana(campana_id: int):
    resultado = await _campanas.reanudar_campana(campana_id)
    return JSONResponse(resultado)

@router.get("/api/campanas/{campana_id}/progreso")
async def api_progreso_campana(campana_id: int):
    resultado = await _campanas.progreso_campana(campana_id)
    return JSONResponse(resultado)

@router.post("/api/campanas/subir-excel")
async def api_subir_excel(request: Request):
    """Sube un Excel/CSV y crea leads en la DB."""
    import io, csv
    try:
        form = await request.form()
        archivo = form.get("archivo")
        if not archivo:
            return JSONResponse({"ok": False, "error": "No se recibió archivo"}, status_code=400)
        contenido = await archivo.read()
        texto = contenido.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(texto))
        pool = await get_pool()
        insertados = 0
        duplicados = 0
        errores = 0
        async with pool.acquire() as conn:
            for row in reader:
                try:
                    tel = str(row.get("telefono") or row.get("Telefono") or row.get("TELEFONO") or "").strip().replace(" ","").replace("+","")
                    if not tel or len(tel) < 8:
                        errores += 1
                        continue
                    nombre = str(row.get("nombre") or row.get("Nombre") or row.get("NOMBRE") or "").strip()
                    comuna = str(row.get("comuna") or row.get("Comuna") or row.get("COMUNA") or "").strip()
                    existing = await conn.fetchval("SELECT id FROM leads WHERE REPLACE(telefono,' ','') = $1", tel)
                    if existing:
                        duplicados += 1
                        continue
                    await conn.execute("""
                        INSERT INTO leads (telefono, nombre, comuna, estado, cliente_id, producto_principal)
                        VALUES ($1, $2, $3, 'nuevo', 1, 'telecom')
                    """, tel, nombre, comuna)
                    insertados += 1
                except Exception:
                    errores += 1
        return JSONResponse({"ok": True, "insertados": insertados, "duplicados": duplicados, "errores": errores})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@router.get("/api/campanas/{campana_id}/destinatarios")
async def api_destinatarios(campana_id: int):
    """Lista de destinatarios de una campaña con estado de envío."""
    dests = await _campanas.obtener_destinatarios(campana_id)
    return JSONResponse({"destinatarios": dests})


@router.post("/api/campanas/{campana_id}/enviar")
async def api_enviar_campana(campana_id: int):
    """Dispara el envío de la campaña en background."""
    campana = await _campanas.obtener_campana(campana_id)
    if not campana:
        raise HTTPException(status_code=404, detail="Campaña no encontrada")
    if campana["estado"] not in ("borrador",):
        raise HTTPException(status_code=400, detail=f"No se puede enviar: estado actual es '{campana['estado']}'")
    asyncio.create_task(_campanas.enviar_campana_bg(campana_id, _get_proveedor()))
    return JSONResponse({"ok": True, "message": "Envío iniciado en background"})


@router.post("/api/campanas/{campana_id}/cancelar")
async def api_cancelar_campana(campana_id: int):
    """Cancela una campaña en estado borrador."""
    await _campanas.cancelar_campana(campana_id)
    return JSONResponse({"ok": True})


# ── API: leads recientes ───────────────────────────────────────────────────────

@router.get("/api/leads")
async def api_leads():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT nombre, telefono, estado, score, subproducto,
                   ultima_interaccion, objeciones, lead_resumen, notas,
                   tags, created_at
            FROM leads
            ORDER BY ultima_interaccion DESC
            LIMIT 500
        """)
        leads = []
        for r in rows:
            try:
                tags = json.loads(r["tags"] or "[]")
            except Exception:
                tags = []
            leads.append({
                "nombre":             r["nombre"] or "Desconocido",
                "telefono":           r["telefono"],
                "estado":             r["estado"],
                "score":              r["score"] or 0,
                "subproducto":        r["subproducto"] or "—",
                "ultima_interaccion": str(r["ultima_interaccion"]) if r["ultima_interaccion"] else "",
                "created_at":         str(r["created_at"]) if r["created_at"] else "",
                "color":              COLOR_ESTADO.get(r["estado"], "#888"),
                "prioridad":          calcular_prioridad(r["estado"], r["score"] or 0),
                "resumen":            r["lead_resumen"] or "",
                "notas":              r["notas"] or "",
                "tags":               tags,
            })
    return JSONResponse({"leads": leads})


# ── API: estadísticas por comuna (Make.com → Google Sheets) ───────────────────

@router.get("/api/leads/comunas/stats")
async def api_comunas_stats():
    """
    Devuelve conteo de leads agrupados por comuna.
    Usado por Make.com para actualizar el mapa de calor en Google Sheets.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                COALESCE(NULLIF(TRIM(comuna), ''), 'Sin especificar') AS comuna,
                COUNT(*)                                               AS total,
                COUNT(CASE WHEN estado IN ('caliente','listo_para_cierre','cerrado') THEN 1 END) AS calientes,
                ROUND(AVG(score::numeric), 1)                          AS score_promedio
            FROM leads
            GROUP BY COALESCE(NULLIF(TRIM(comuna), ''), 'Sin especificar')
            ORDER BY total DESC
        """)
    return JSONResponse({
        "comunas": [
            {
                "comuna":         r["comuna"],
                "total":          r["total"],
                "calientes":      r["calientes"] or 0,
                "score_promedio": float(r["score_promedio"]) if r["score_promedio"] else 0,
            }
            for r in rows
        ],
        "actualizado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# ── API: exportar leads a CSV ──────────────────────────────────────────────────

@router.get("/api/leads/export-csv")
async def export_leads_csv(
    request: Request,
    estado: str = "",
    tag: str = "",
    prioridad: str = "",
    fecha_desde: str = "",
    fecha_hasta: str = "",
):
    """
    Descarga leads como CSV con filtros opcionales.
    Parámetros: estado, tag, prioridad (alta/media/baja), fecha_desde, fecha_hasta (YYYY-MM-DD).
    """
    conditions = []
    params: list = []
    idx = 1

    if estado:
        conditions.append(f"l.estado = ${idx}")
        params.append(estado); idx += 1

    if fecha_desde:
        conditions.append(f"l.created_at >= ${idx}::date")
        params.append(fecha_desde); idx += 1

    if fecha_hasta:
        conditions.append(f"l.created_at < (${idx}::date + interval '1 day')")
        params.append(fecha_hasta); idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT l.nombre, l.telefono, l.estado, l.score,
                   l.subproducto, l.tags, l.notas, l.lead_resumen,
                   l.direccion, l.comuna, l.origen,
                   l.created_at, l.ultima_interaccion,
                   l.mensajes_en_estado,
                   (SELECT mensaje FROM historial_mensajes hh
                    WHERE SPLIT_PART(REPLACE(hh.telefono,' ',''),'@',1)
                        = SPLIT_PART(REPLACE(l.telefono,' ',''),'@',1)
                    ORDER BY hh.timestamp DESC LIMIT 1) AS ultimo_mensaje,
                   COUNT(h.id) AS total_mensajes
            FROM leads l
            LEFT JOIN historial_mensajes h
                   ON SPLIT_PART(REPLACE(h.telefono,' ',''),'@',1)
                    = SPLIT_PART(REPLACE(l.telefono,' ',''),'@',1)
            {where}
            GROUP BY l.id, l.nombre, l.telefono, l.estado, l.score,
                     l.subproducto, l.tags, l.notas, l.lead_resumen,
                     l.direccion, l.comuna, l.origen,
                     l.created_at, l.ultima_interaccion, l.mensajes_en_estado
            ORDER BY l.ultima_interaccion DESC NULLS LAST
        """, *params)

    def _prioridad(estado: str, score: int) -> str:
        if estado in ("caliente", "listo_para_cierre", "cerrado"):
            return "🔴 Alta"
        if score >= 60 or estado in ("interesado", "seguimiento"):
            return "🟡 Media"
        return "⚪ Baja"

    def _prioridad_clave(estado: str, score: int) -> str:
        p = _prioridad(estado, score)
        if p.startswith("🔴"): return "alta"
        if p.startswith("🟡"): return "media"
        return "baja"

    # Filtros post-query (tag y prioridad no son fáciles de filtrar en SQL con el esquema actual)
    resultado = []
    for r in rows:
        try:
            tags_list = json.loads(r["tags"] or "[]")
        except Exception:
            tags_list = []
        score_val = r["score"] if r["score"] is not None else 0
        estado_val = r["estado"] or ""
        if tag and tag not in tags_list:
            continue
        if prioridad and _prioridad_clave(estado_val, score_val) != prioridad.lower():
            continue
        resultado.append((r, tags_list, score_val, estado_val))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Nombre", "Teléfono", "Estado", "Score", "Prioridad",
        "Tags", "Producto", "Resumen IA",
        "Último mensaje", "Total mensajes",
        "Dirección", "Comuna", "Origen",
        "Fecha de creación", "Última interacción",
        "Notas",
    ])
    for r, tags_list, score_val, estado_val in resultado:
        tags_str = ", ".join(tags_list)
        created  = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
        ultima   = r["ultima_interaccion"].strftime("%Y-%m-%d %H:%M") if r["ultima_interaccion"] else ""
        writer.writerow([
            r["nombre"] or "",
            r["telefono"] or "",
            estado_val,
            score_val,
            _prioridad(estado_val, score_val),
            tags_str,
            r["subproducto"] or "",
            r["lead_resumen"] or "",
            r["ultimo_mensaje"] or "",
            r["total_mensajes"] or 0,
            r["direccion"] or "",
            r["comuna"] or "",
            r["origen"] or "",
            created,
            ultima,
            r["notas"] or "",
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    filename = f"leads_exportados_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── API: tags de un lead ──────────────────────────────────────────────────────

@router.patch("/api/leads/{telefono}/tags")
async def actualizar_tags_lead(telefono: str, request: Request):
    """Reemplaza la lista de tags de un lead."""
    body = await request.json()
    tags = body.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    tags_clean = [str(t).strip()[:50] for t in tags if str(t).strip()][:20]
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE leads SET tags = $1 WHERE telefono = $2",
            json.dumps(tags_clean, ensure_ascii=False), telefono
        )
    return JSONResponse({"ok": True, "tags": tags_clean})


# ── API: guardar notas de un lead ──────────────────────────────────────────────

@router.patch("/api/leads/{telefono}/notas")
async def actualizar_notas_lead(telefono: str, request: Request):
    """Actualiza las notas internas de un lead."""
    body = await request.json()
    notas = str(body.get("notas", ""))[:2000]  # límite razonable
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE leads SET notas = $1 WHERE telefono = $2",
            notas, telefono
        )
    return JSONResponse({"ok": True})


# ── API: notas internas por lead (tabla lead_notas) ───────────────────────────

@router.get("/api/leads/{telefono}/notas-internas")
async def listar_notas_internas(telefono: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, contenido, created_at FROM lead_notas "
            "WHERE telefono=$1 ORDER BY created_at DESC",
            telefono
        )
    return JSONResponse({"notas": [
        {"id": r["id"], "contenido": r["contenido"],
         "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M")}
        for r in rows
    ]})


@router.post("/api/leads/{telefono}/notas-internas")
async def crear_nota_interna(telefono: str, request: Request):
    body = await request.json()
    contenido = str(body.get("contenido", "")).strip()[:1000]
    if not contenido:
        raise HTTPException(status_code=400, detail="Contenido vacío")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO lead_notas(telefono, contenido) VALUES($1,$2) "
            "RETURNING id, created_at",
            telefono, contenido
        )
    return JSONResponse({"ok": True, "id": row["id"],
                         "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M")})


@router.delete("/api/leads/notas-internas/{nota_id}")
async def eliminar_nota_interna(nota_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM lead_notas WHERE id=$1", nota_id)
    return JSONResponse({"ok": True})


# ── API: detalle completo de un lead ──────────────────────────────────────────

@router.get("/api/leads/{telefono}/detail")
async def lead_detail(telefono: str):
    """Devuelve todos los campos de un lead para el modal de detalle."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT nombre, telefono, estado, score, subproducto,
                   notas, lead_resumen, direccion, comuna,
                   objeciones, mensajes_en_estado, tags,
                   ultima_interaccion, created_at
            FROM leads WHERE telefono = $1
        """, telefono)
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            objeciones = json.loads(row["objeciones"] or "[]")
        except Exception:
            objeciones = []
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:
            tags = []
        return JSONResponse({
            "nombre":             row["nombre"] or "Desconocido",
            "telefono":           row["telefono"],
            "estado":             row["estado"] or "nuevo",
            "score":              row["score"] or 0,
            "subproducto":        row["subproducto"] or "—",
            "notas":              row["notas"] or "",
            "resumen":            row["lead_resumen"] or "",
            "direccion":          row["direccion"] or "—",
            "comuna":             row["comuna"] or "—",
            "objeciones":         objeciones,
            "tags":               tags,
            "mensajes_en_estado": row["mensajes_en_estado"] or 0,
            "ultima_interaccion": str(row["ultima_interaccion"]) if row["ultima_interaccion"] else "—",
            "created_at":         str(row["created_at"]) if row["created_at"] else "—",
            "color":              COLOR_ESTADO.get(row["estado"] or "nuevo", "#888"),
            "prioridad":          calcular_prioridad(row["estado"] or "nuevo", row["score"] or 0),
        })


# ── API: regenerar resumen IA de un lead ──────────────────────────────────────

@router.post("/api/leads/{telefono}/resumen")
async def regenerar_resumen_lead(telefono: str):
    """Dispara la regeneración del resumen IA en background y responde inmediatamente."""
    import asyncio as _asyncio
    _asyncio.create_task(_crm.actualizar_resumen_lead(telefono))
    return JSONResponse({"ok": True, "message": "regeneración iniciada en background"})


# ── API: tomar / liberar lead (modo humano) ────────────────────────────────────

@router.post("/api/leads/{telefono}/tomar")
async def tomar_lead(telefono: str):
    """Activa modo_humano: pausa las respuestas de Valentina y cancela follow-ups."""
    nombre = "Cliente"
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT nombre FROM leads WHERE telefono = $1", telefono
            )
            if row and row["nombre"]:
                nombre = row["nombre"]
            await conn.execute(
                "UPDATE leads SET estado = 'modo_humano', ultima_interaccion = CURRENT_TIMESTAMP WHERE telefono = $1",
                telefono
            )
            await conn.execute(
                "UPDATE followup_programado SET cancelado = 1 WHERE telefono = $1 AND enviado = 0 AND cancelado = 0",
                telefono
            )
    await broadcast_event({"type": "mode_change", "telefono": telefono, "modo_humano": True})

    # Notificar al dueño por WhatsApp que tomó el control de este lead
    try:
        tel_limpio = telefono.replace("+", "").replace(" ", "")
        wa_link = f"https://wa.me/{tel_limpio}"
        mensaje = (
            f"🟣 *MODO HUMANO ACTIVADO*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *{nombre}* (+{tel_limpio})\n"
            f"💬 Abrir chat: {wa_link}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"El bot está pausado. Responde desde el dashboard."
        )
        await _get_proveedor().enviar_mensaje(TELEFONO_OWNER, mensaje)
    except Exception:
        pass

    return JSONResponse({"ok": True, "telefono": telefono, "estado": "modo_humano"})


@router.post("/api/leads/{telefono}/liberar")
async def liberar_lead(telefono: str):
    """Desactiva modo_humano: devuelve el lead a seguimiento para que Valentina retome."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE leads SET estado = 'seguimiento', ultima_interaccion = CURRENT_TIMESTAMP WHERE telefono = $1",
            telefono
        )
    # Inyectar mensaje interno para que Valentina retome el contexto
    try:
        pass
    except Exception:
        pass
    await broadcast_event({"type": "mode_change", "telefono": telefono, "modo_humano": False})
    return JSONResponse({"ok": True, "telefono": telefono, "estado": "seguimiento"})


# ── API: SSE stream para Live Chat ────────────────────────────────────────────

@router.get("/api/events")
async def sse_events(request: Request):
    """Server-Sent Events — notifica nuevos mensajes al Live Chat en tiempo real."""
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _sse_queues.add(q)

    async def generar():
        try:
            yield 'data: {"type":"connected"}\n\n'
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _sse_queues.discard(q)

    return StreamingResponse(
        generar(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── API: lista de conversaciones (Live Chat) ───────────────────────────────────

@router.get("/api/conversations")
async def api_conversations():
    """Lista de conversaciones ordenada por última actividad, máx. 50."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Fix 1: contar en historial_mensajes (fuente real), no en mensajes
            total_en_historial = await conn.fetchval("SELECT COUNT(*) FROM historial_mensajes")
            logger.info(f"[api/conversations] historial_mensajes total={total_en_historial}")

            if not total_en_historial:
                return JSONResponse({"conversaciones": [], "debug": "historial_mensajes vacío"})

            # Fix 2: limpiar @s.whatsapp.net además de espacios con SPLIT_PART
            filas_mem = await conn.fetch("""
                SELECT * FROM (
                    SELECT DISTINCT ON (SPLIT_PART(REPLACE(telefono, ' ', ''), '@', 1))
                        SPLIT_PART(REPLACE(telefono, ' ', ''), '@', 1) AS telefono,
                        timestamp                                       AS ultima_actividad,
                        mensaje                                         AS ultimo_mensaje,
                        rol                                             AS ultimo_rol,
                        COUNT(*) OVER (
                            PARTITION BY SPLIT_PART(REPLACE(telefono, ' ', ''), '@', 1)
                        ) AS total_mensajes
                    FROM historial_mensajes
                    ORDER BY SPLIT_PART(REPLACE(telefono, ' ', ''), '@', 1), timestamp DESC
                ) latest
                ORDER BY ultima_actividad DESC
                LIMIT 50
            """)

            logger.info(f"[api/conversations] filas={len(filas_mem)} tels={[f['telefono'] for f in filas_mem]}")

            if not filas_mem:
                return JSONResponse({"conversaciones": [], "debug": "query devolvió 0 filas"})

            telefonos = [f["telefono"] for f in filas_mem]

            # Fix 2b: también normalizar teléfono en leads para el lookup
            crm_rows = await conn.fetch("""
                SELECT SPLIT_PART(REPLACE(telefono, ' ', ''), '@', 1) AS telefono,
                       nombre, estado, score, tags
                FROM leads
                WHERE SPLIT_PART(REPLACE(telefono, ' ', ''), '@', 1) = ANY($1)
            """, telefonos)
            logger.info(f"[api/conversations] leads CRM={len(crm_rows)}")
            info_lead = {r["telefono"]: dict(r) for r in crm_rows}

            # Timestamp del último mensaje del lead (rol='user') por teléfono
            ultimo_user_rows = await conn.fetch("""
                SELECT SPLIT_PART(REPLACE(telefono,' ',''),'@',1) AS telefono,
                       MAX(timestamp) AS ultimo_user_ts
                FROM historial_mensajes
                WHERE rol = 'user'
                  AND SPLIT_PART(REPLACE(telefono,' ',''),'@',1) = ANY($1)
                GROUP BY SPLIT_PART(REPLACE(telefono,' ',''),'@',1)
            """, telefonos)
            ultimo_user_ts = {r["telefono"]: r["ultimo_user_ts"] for r in ultimo_user_rows}

        conversaciones = []
        for f in filas_mem:
            tel    = f["telefono"]
            lead   = info_lead.get(tel, {})
            estado = lead.get("estado") or "nuevo"
            score  = int(lead.get("score") or 0)
            _n     = (lead.get("nombre") or "").strip()
            nombre = _n if (_n and _n.lower() not in ("desconocido", "cliente", "unknown", "")) else tel
            try:
                tags = json.loads(lead.get("tags") or "[]")
            except Exception:
                tags = []
            uts = ultimo_user_ts.get(tel)
            conversaciones.append({
                "telefono":         tel,
                "nombre":           nombre,
                "estado":           estado,
                "score":            score,
                "tags":             tags,
                "ultima_actividad": str(f["ultima_actividad"]),
                "ultimo_mensaje":   str(f["ultimo_mensaje"] or ""),
                "ultimo_rol":       str(f["ultimo_rol"] or "user"),
                "ultimo_user_ts":   str(uts) if uts else "",
                "total_mensajes":   int(f["total_mensajes"]),
                "modo_humano":      estado == "modo_humano",
                "color":            COLOR_ESTADO.get(estado, "#888"),
                "prioridad":        calcular_prioridad(estado, score),
            })

        logger.info(f"[api/conversations] retornando {len(conversaciones)} conversaciones")
        return JSONResponse({"conversaciones": conversaciones})

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"[api/conversations] ERROR: {e}\n{tb}")
        return JSONResponse({"conversaciones": [], "error": str(e), "traceback": tb}, status_code=500)


# ── API: diagnóstico de tablas ─────────────────────────────────────────────────

@router.get("/api/debug/tables")
async def api_debug_tables():
    """Cuenta filas en cada tabla para diagnosticar si los datos llegan a la DB."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        counts = {}
        for tabla in ("mensajes", "leads", "historial_mensajes", "followup_programado"):
            try:
                counts[tabla] = await conn.fetchval(f"SELECT COUNT(*) FROM {tabla}")
            except Exception as e:
                counts[tabla] = f"ERROR: {e}"

        # Últimas 5 filas de mensajes para verificar formato de timestamp y teléfono
        try:
            muestra = await conn.fetch(
                "SELECT REPLACE(telefono, ' ', '') AS telefono, role, timestamp FROM mensajes ORDER BY timestamp DESC LIMIT 5"
            )
            counts["mensajes_muestra"] = [
                {"tel": r["telefono"], "role": r["role"], "ts": str(r["timestamp"])}
                for r in muestra
            ]
        except Exception as e:
            counts["mensajes_muestra"] = f"ERROR: {e}"

    logger.info(f"[debug/tables] {counts}")
    return JSONResponse(counts)


@router.get("/api/debug/leads")
async def api_debug_leads():
    """Muestra todos los leads en la tabla leads para diagnóstico."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT telefono, nombre, estado, score, ultima_interaccion FROM leads ORDER BY ultima_interaccion DESC LIMIT 20"
        )
        leads = [
            {
                "telefono": r["telefono"],
                "nombre": r["nombre"],
                "estado": r["estado"],
                "score": r["score"],
                "ultima_interaccion": str(r["ultima_interaccion"]) if r["ultima_interaccion"] else None,
            }
            for r in rows
        ]
    return JSONResponse({"total": len(leads), "leads": leads})


@router.get("/api/debug/conversations")
async def api_debug_conversations():
    """Muestra los resultados crudos de cada paso de api/conversations para diagnóstico."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM mensajes")

            # Paso 1: las primeras 5 filas crudas de mensajes
            muestra_raw = await conn.fetch(
                "SELECT REPLACE(telefono, ' ', '') AS telefono, role, content, timestamp FROM mensajes ORDER BY timestamp DESC LIMIT 5"
            )
            paso1 = [{"tel": r["telefono"], "role": r["role"], "ts": str(r["timestamp"]),
                      "content_start": str(r["content"])[:40]} for r in muestra_raw]

            # Paso 2: probar el DISTINCT ON directamente
            try:
                distinct_rows = await conn.fetch("""
                    SELECT DISTINCT ON (REPLACE(telefono, ' ', ''))
                        REPLACE(telefono, ' ', '') AS telefono,
                        timestamp AS ts, role,
                        COUNT(*) OVER (PARTITION BY REPLACE(telefono, ' ', '')) AS cnt
                    FROM mensajes
                    ORDER BY REPLACE(telefono, ' ', ''), timestamp DESC
                """)
                paso2 = [{"tel": r["telefono"], "ts": str(r["ts"]), "role": r["role"],
                          "cnt": int(r["cnt"])} for r in distinct_rows]
            except Exception as e2:
                paso2 = f"ERROR: {e2}"

            # Paso 3: probar el ANY($1) con la lista de teléfonos
            try:
                tels = [r["telefono"] for r in muestra_raw]
                leads_rows = await conn.fetch(
                    "SELECT telefono, nombre, estado, score FROM leads WHERE telefono = ANY($1)", tels
                )
                paso3 = [dict(r) for r in leads_rows]
            except Exception as e3:
                paso3 = f"ERROR: {e3}"

        return JSONResponse({"total_mensajes": total, "paso1_muestra": paso1,
                             "paso2_distinct_on": paso2, "paso3_leads": paso3})
    except Exception as e:
        return JSONResponse({"error": str(e), "traceback": traceback.format_exc()}, status_code=500)


# ── API: historial de un contacto — /api/chat/{tel} ───────────────────────────

@router.get("/api/chat/{telefono}")
async def api_chat_historial(telefono: str):
    """Retorna el historial completo de mensajes de un contacto."""
    tel = telefono.replace(" ", "").strip().lstrip("+")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            filas = await conn.fetch(
                "SELECT rol AS role, mensaje AS content, timestamp, estado_lead FROM historial_mensajes WHERE REPLACE(telefono, ' ', '') = $1 ORDER BY timestamp ASC",
                tel
            )
        mensajes = [
            {
                "role": f["role"],
                "content": f["content"],
                "timestamp": str(f["timestamp"]),
                "estado_lead": f["estado_lead"] or "",
            }
            for f in filas
        ]
        logger.info(f"[api/chat] telefono={tel} mensajes={len(mensajes)}")
        return JSONResponse({"mensajes": mensajes, "telefono": tel})
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"[api/chat] ERROR telefono={tel}: {e}\n{tb}")
        return JSONResponse({"mensajes": [], "telefono": tel, "error": str(e)}, status_code=500)


# ── API: enviar mensaje desde el dashboard — /api/chat/{tel}/send ─────────────

@router.post("/api/chat/{telefono}/send")
async def enviar_mensaje_dashboard(telefono: str, request: Request):
    """Envía un mensaje al contacto vía WhatsApp y lo guarda en historial."""
    import logging as _log_mod
    _logger = _log_mod.getLogger("agentkit")
    tel = telefono.lstrip("+")
    try:
        body = await request.json()
        texto = (body.get("mensaje") or "").strip()
    except Exception:
        return JSONResponse({"ok": False, "error": "Body inválido"}, status_code=400)

    if not texto:
        return JSONResponse({"ok": False, "error": "Mensaje vacío"}, status_code=400)

    ts = datetime.utcnow().isoformat()

    # 1) Guardar en historial primero — el dashboard lo verá aunque WA falle
    try:
        await _guardar_memoria(tel, "assistant", texto)
        await _crm.guardar_mensaje(tel, "assistant", texto, "modo_humano", None)
    except Exception as e:
        _logger.error(f"Error guardando mensaje dashboard: {e}")

    # 2) Notificar Live Chat vía SSE — actualiza dashboard inmediatamente
    await broadcast_event({
        "type": "new_message", "telefono": tel,
        "role": "assistant", "content": texto, "ts": ts,
    })

    # 3) Enviar por WhatsApp (puede fallar sin romper la respuesta)
    enviado = False
    wa_error = None
    try:
        enviado = await _get_proveedor().enviar_mensaje(tel, texto)
        if not enviado:
            wa_error = "El proveedor rechazó el mensaje (revisar token/número)"
    except Exception as e:
        wa_error = str(e)
        _logger.error(f"Error WA en envío dashboard para {tel}: {e}")

    if wa_error:
        return JSONResponse({"ok": False, "guardado": True, "error": wa_error})
    return JSONResponse({"ok": True, "guardado": True})


# ── API: mensajes recientes ────────────────────────────────────────────────────

@router.get("/api/messages")
async def api_messages():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT h.telefono, h.rol, h.mensaje, h.timestamp,
                   h.estado_lead, h.intencion_detectada, l.nombre
            FROM historial_mensajes h
            LEFT JOIN leads l ON h.telefono = l.telefono
            ORDER BY h.timestamp DESC
            LIMIT 30
        """)
    mensajes = []
    for r in rows:
        _n = (r["nombre"] or "").strip()
        _nombre = _n if (_n and _n.lower() not in ("desconocido", "cliente", "unknown", "")) else r["telefono"]
        mensajes.append({
            "telefono":  r["telefono"],
            "nombre":    _nombre,
            "rol":       r["rol"],
            "mensaje":   r["mensaje"][:100] + ("…" if len(r["mensaje"]) > 100 else ""),
            "timestamp": str(r["timestamp"]),
            "estado":    r["estado_lead"] or "—",
            "intencion": r["intencion_detectada"] or "—",
        })
    return JSONResponse({"mensajes": mensajes})


# ── API: KPI detail endpoints ────────────────────────────────────────────────

@router.get("/api/kpi/leads")
async def kpi_leads():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT telefono, nombre, estado, score, subproducto, ultima_interaccion
            FROM leads ORDER BY ultima_interaccion DESC NULLS LAST
        """)
    return JSONResponse({"items": [
        {"telefono": r["telefono"], "nombre": r["nombre"] or "Desconocido",
         "estado": r["estado"] or "nuevo", "score": r["score"] or 0,
         "subproducto": r["subproducto"] or "—",
         "ts": str(r["ultima_interaccion"]) if r["ultima_interaccion"] else None}
        for r in rows
    ]})


@router.get("/api/kpi/leads-calientes")
async def kpi_leads_calientes():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT telefono, nombre, estado, score, direccion, subproducto, ultima_interaccion
            FROM leads WHERE estado IN ('caliente','listo_para_cierre')
            ORDER BY score DESC NULLS LAST
        """)
    return JSONResponse({"items": [
        {"telefono": r["telefono"], "nombre": r["nombre"] or "Desconocido",
         "estado": r["estado"], "score": r["score"] or 0,
         "direccion": r["direccion"] or "—", "subproducto": r["subproducto"] or "—",
         "ts": str(r["ultima_interaccion"]) if r["ultima_interaccion"] else None}
        for r in rows
    ]})


@router.get("/api/kpi/conversiones")
async def kpi_conversiones():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT telefono, nombre, score, subproducto, ultima_interaccion
            FROM leads WHERE estado = 'cerrado'
            ORDER BY ultima_interaccion DESC NULLS LAST
        """)
    return JSONResponse({"items": [
        {"telefono": r["telefono"], "nombre": r["nombre"] or "Desconocido",
         "score": r["score"] or 0, "subproducto": r["subproducto"] or "—",
         "ts": str(r["ultima_interaccion"]) if r["ultima_interaccion"] else None}
        for r in rows
    ]})


@router.get("/api/kpi/top-score")
async def kpi_top_score():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT telefono, nombre, estado, score, subproducto
            FROM leads WHERE score IS NOT NULL
            ORDER BY score DESC NULLS LAST LIMIT 10
        """)
        max_score = await conn.fetchval("SELECT MAX(score) FROM leads") or 100
    return JSONResponse({"items": [
        {"telefono": r["telefono"], "nombre": r["nombre"] or "Desconocido",
         "estado": r["estado"] or "nuevo", "score": r["score"] or 0,
         "subproducto": r["subproducto"] or "—",
         "pct": round((r["score"] or 0) / max(max_score, 1) * 100)}
        for r in rows
    ]})


@router.get("/api/kpi/mensajes-hoy")
async def kpi_mensajes_hoy():
    pool = await get_pool()
    async with pool.acquire() as conn:
        hoy_dt = datetime.combine(date.today(), datetime.min.time())
        rows = await conn.fetch("""
            SELECT h.telefono, h.rol, h.mensaje, h.timestamp, l.nombre
            FROM historial_mensajes h
            LEFT JOIN leads l ON h.telefono = l.telefono
            WHERE h.timestamp >= $1
            ORDER BY h.timestamp DESC LIMIT 50
        """, hoy_dt)
    return JSONResponse({"items": [
        {"telefono": r["telefono"],
         "nombre": (r["nombre"] or "").strip() or r["telefono"],
         "rol": r["rol"], "mensaje": r["mensaje"],
         "ts": str(r["timestamp"])}
        for r in rows
    ]})


@router.get("/api/kpi/followups")
async def kpi_followups():
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT f.telefono, f.tipo, f.mensaje, f.programado_para, l.nombre
                FROM followup_programado f
                LEFT JOIN leads l ON f.telefono = l.telefono
                WHERE f.enviado = 0 AND f.cancelado = 0
                ORDER BY f.programado_para ASC
            """)
        except Exception:
            rows = []
    return JSONResponse({"items": [
        {"telefono": r["telefono"],
         "nombre": (r["nombre"] or "").strip() or r["telefono"],
         "tipo": r["tipo"], "mensaje": r["mensaje"],
         "programado_para": str(r["programado_para"])}
        for r in rows
    ]})


# ── API: Sin Respuesta ────────────────────────────────────────────────────────

_SIN_RESPUESTA_QUERY = """
    WITH
    -- Caso A: pasaron por el webhook, el último mensaje fue del agente (nunca respondieron)
    ultimos AS (
        SELECT DISTINCT ON (SPLIT_PART(REPLACE(telefono,' ',''),'@',1))
            SPLIT_PART(REPLACE(telefono,' ',''),'@',1) AS telefono,
            rol           AS ultimo_rol,
            timestamp     AS ultima_actividad
        FROM historial_mensajes
        ORDER BY SPLIT_PART(REPLACE(telefono,' ',''),'@',1), timestamp DESC
    ),
    primeros AS (
        SELECT DISTINCT ON (SPLIT_PART(REPLACE(telefono,' ',''),'@',1))
            SPLIT_PART(REPLACE(telefono,' ',''),'@',1) AS telefono,
            timestamp AS primer_contacto
        FROM historial_mensajes
        WHERE rol = 'assistant'
        ORDER BY SPLIT_PART(REPLACE(telefono,' ',''),'@',1), timestamp ASC
    ),
    caso_a AS (
        SELECT
            u.telefono,
            COALESCE(l.nombre,  u.telefono)  AS nombre,
            COALESCE(l.comuna,  '')           AS ciudad,
            COALESCE(l.estado,  'nuevo')      AS estado,
            COALESCE(l.subproducto, '')       AS subproducto,
            COALESCE(l.tags,    '[]')         AS tags,
            p.primer_contacto                 AS fecha_envio,
            u.ultima_actividad,
            ROUND(
                EXTRACT(EPOCH FROM (NOW() - u.ultima_actividad)) / 86400.0, 1
            )::float                          AS dias_sin_respuesta
        FROM ultimos u
        JOIN primeros p ON u.telefono = p.telefono
        LEFT JOIN leads l
          ON SPLIT_PART(REPLACE(l.telefono,' ',''),'@',1) = u.telefono
        WHERE u.ultimo_rol = 'assistant'
          AND (l.estado IS NULL OR l.estado NOT IN ('cerrado','modo_humano'))
          AND (l.tags IS NULL OR l.tags NOT LIKE '%Incontactable%')
    ),
    -- Caso B: estado='contactado' sin ningún mensaje en historial (envio_masivo)
    caso_b AS (
        SELECT
            SPLIT_PART(REPLACE(l.telefono,' ',''),'@',1) AS telefono,
            COALESCE(l.nombre, l.telefono)               AS nombre,
            COALESCE(l.comuna, '')                        AS ciudad,
            l.estado,
            COALESCE(l.subproducto, '')                   AS subproducto,
            COALESCE(l.tags, '[]')                        AS tags,
            l.ultima_interaccion                          AS fecha_envio,
            l.ultima_interaccion                          AS ultima_actividad,
            ROUND(
                EXTRACT(EPOCH FROM (NOW() - l.ultima_interaccion)) / 86400.0, 1
            )::float                                      AS dias_sin_respuesta
        FROM leads l
        WHERE l.estado = 'contactado'
          AND (l.tags IS NULL OR l.tags NOT LIKE '%Incontactable%')
          AND NOT EXISTS (
              SELECT 1 FROM historial_mensajes hm
              WHERE SPLIT_PART(REPLACE(hm.telefono,' ',''),'@',1)
                  = SPLIT_PART(REPLACE(l.telefono,' ',''),'@',1)
          )
    )
    SELECT * FROM caso_a
    UNION ALL
    SELECT * FROM caso_b
    ORDER BY ultima_actividad ASC
    LIMIT 500
"""


@router.get("/api/sin-respuesta")
async def api_sin_respuesta():
    """Lista de leads que recibieron mensaje pero nunca respondieron."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SIN_RESPUESTA_QUERY)
        resultado = []
        for r in rows:
            try:
                tags = json.loads(r["tags"] or "[]")
            except Exception:
                tags = []
            resultado.append({
                "telefono":           r["telefono"],
                "nombre":             r["nombre"],
                "ciudad":             r["ciudad"],
                "estado":             r["estado"],
                "subproducto":        r["subproducto"],
                "tags":               tags,
                "fecha_envio":        str(r["fecha_envio"]),
                "ultima_actividad":   str(r["ultima_actividad"]),
                "dias_sin_respuesta": float(r["dias_sin_respuesta"] or 0),
            })
        return JSONResponse({"leads": resultado, "total": len(resultado)})
    except Exception as e:
        return JSONResponse({"leads": [], "total": 0, "error": str(e)}, status_code=500)


@router.post("/api/sin-respuesta/{telefono}/reactivar")
async def reactivar_lead(telefono: str, request: Request):
    """Envía mensaje de reactivación manual al lead."""
    tel = telefono.lstrip("+").replace(" ", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    mensaje = (body.get("mensaje") or "").strip()
    if not mensaje:
        # Mensaje de reactivación predeterminado
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT nombre FROM leads WHERE REPLACE(telefono,' ','') = $1", tel
            )
        nombre = (row["nombre"] or "").strip() if row else ""
        nombre_fmt = nombre.split()[0].title() if nombre and nombre.lower() not in ("desconocido","cliente","unknown","") else ""
        saludo = f"Hola {nombre_fmt}! " if nombre_fmt else "Hola! "
        mensaje = (
            f"{saludo}Te escribo nuevamente desde Conexión Sin Límites. "
            f"Quedamos pendientes con tu consulta. "
            f"¿Tienes un momento para que podamos ayudarte? 😊"
        )
    ts = datetime.utcnow().isoformat()
    try:
        await _crm.guardar_mensaje(tel, "assistant", mensaje, "seguimiento", None)
        await _guardar_memoria(tel, "assistant", mensaje)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Error guardando: {e}"}, status_code=500)
    enviado = False
    try:
        from agent.providers import obtener_proveedor as _get_prov
        enviado = await _get_prov().enviar_mensaje(tel, mensaje)
    except Exception:
        pass
    await broadcast_event({
        "type": "new_message", "telefono": tel,
        "role": "assistant", "content": mensaje, "ts": ts,
    })
    return JSONResponse({"ok": True, "enviado": enviado, "mensaje": mensaje})


@router.post("/api/sin-respuesta/{telefono}/incontactable")
async def marcar_incontactable(telefono: str):
    """Agrega tag 'Incontactable' al lead y lo mueve a seguimiento."""
    tel = telefono.lstrip("+").replace(" ", "")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tags FROM leads WHERE REPLACE(telefono,' ','') = $1", tel
        )
        if not row:
            return JSONResponse({"ok": False, "error": "Lead no encontrado"}, status_code=404)
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:
            tags = []
        if "Incontactable" not in tags:
            tags.append("Incontactable")
        await conn.execute(
            "UPDATE leads SET tags = $1, estado = 'seguimiento', ultima_interaccion = CURRENT_TIMESTAMP "
            "WHERE REPLACE(telefono,' ','') = $2",
            json.dumps(tags), tel
        )
    return JSONResponse({"ok": True, "telefono": tel, "tags": tags})


@router.get("/api/sin-respuesta/export.csv")
async def exportar_sin_respuesta_csv():
    """Descarga CSV con todos los leads sin respuesta."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SIN_RESPUESTA_QUERY)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Nombre", "Telefono", "Ciudad", "Region",
        "Dias_sin_respuesta", "Fecha_ultimo_intento",
        "Fecha_primer_contacto", "Estado", "Promocion_original",
    ])
    for r in rows:
        dias = float(r["dias_sin_respuesta"] or 0)
        writer.writerow([
            r["nombre"],
            r["telefono"],
            r["ciudad"],
            "",   # región no está en la BD, dejar vacío
            f"{dias:.1f}",
            str(r["ultima_actividad"])[:19],
            str(r["fecha_envio"])[:19],
            r["estado"],
            r["subproducto"],
        ])
    output.seek(0)
    filename = f"sin_respuesta_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── HTML del dashboard ─────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=HTML_DASHBOARD)


HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Valentina CRM — Conexion Sin Limites</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Space+Grotesk:wght@400;600;700&display=swap" rel="stylesheet" crossorigin>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  /* CONEXION SIN LIMITES - Dark Futurista Premium
     #000000 | #00D4FF neon | #FF2233 rojo estrella */
  :root {
    --neon:       #00D4FF;
    --neon-dim:   rgba(0, 212, 255, 0.15);
    --neon-glow:  rgba(0, 212, 255, 0.4);
    --red:        #FF2233;
    --red-glow:   rgba(255, 34, 51, 0.4);
    --bg:         #000000;
    --txt:        #FFFFFF;
    --txt2:       rgba(255,255,255,0.45);
    --txt3:       rgba(255,255,255,0.2);
    --glass:      rgba(255,255,255,0.03);
    --glass-h:    rgba(0,212,255,0.06);
    --border:     rgba(0,212,255,0.18);
    --border-h:   rgba(0,212,255,0.5);
    --green:      #00FF88;
    --orange:     #FF8C00;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--txt);
    font-family: 'Space Grotesk', system-ui, sans-serif;
    overflow-x: hidden;
  }

  /* ── fondo animado ── */
  body::before {
    content: '';
    position: fixed; inset: 0; z-index: 0;
    background:
      radial-gradient(ellipse 80% 40% at 50% -10%, rgba(0,212,255,0.07) 0%, transparent 70%),
      radial-gradient(ellipse 40% 30% at 100% 100%, rgba(255,34,51,0.04) 0%, transparent 60%);
    pointer-events: none;
  }

  /* ── header ── */
  header {
    position: sticky; top: 0; z-index: 100;
    background: rgba(0,0,0,0.88);
    backdrop-filter: blur(24px);
    border-bottom: 1px solid var(--border);
    box-shadow: 0 2px 48px rgba(0,212,255,0.10), 0 1px 0 rgba(0,212,255,0.08);
    padding: 0 2.5rem;
    min-height: 80px;
    display: flex; align-items: center; gap: 1.5rem;
    justify-content: space-between;
  }

  /* ── Logo + marca ── */
  .logo { display: flex; align-items: center; gap: 1.1rem; flex-shrink: 0; }

  .logo-icon {
    width: 46px; height: 46px;
    border: 2px solid var(--neon);
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, rgba(0,212,255,.12) 0%, rgba(0,212,255,.04) 100%);
    box-shadow: 0 0 18px var(--neon-glow), inset 0 0 14px rgba(0,212,255,0.07);
    font-family: 'Orbitron', sans-serif;
    font-size: 1.25rem; font-weight: 900;
    color: var(--neon);
    text-shadow: 0 0 12px var(--neon);
    flex-shrink: 0;
    position: relative;
  }
  /* Pulso suave detrás del ícono */
  .logo-icon::before {
    content: '';
    position: absolute; inset: -4px; border-radius: 16px;
    border: 1px solid rgba(0,212,255,.2);
    animation: logoPulse 3s ease-in-out infinite;
  }
  @keyframes logoPulse {
    0%, 100% { opacity: .5; transform: scale(1);   }
    50%       { opacity: 1;  transform: scale(1.06);}
  }

  .logo-text-wrap { display: flex; flex-direction: column; gap: 2px; }

  .logo-name {
    font-family: 'Orbitron', sans-serif;
    font-size: 1.08rem; font-weight: 800;
    letter-spacing: .13em; line-height: 1;
    color: var(--txt);
    text-shadow: 0 0 24px rgba(255,255,255,0.25);
    white-space: nowrap;
  }

  .logo-x {
    color: var(--red);
    text-shadow: 0 0 10px var(--red), 0 0 22px var(--red-glow);
    position: relative;
    display: inline-block;
  }
  .logo-x::after {
    content: '*';
    position: absolute;
    top: -5px; right: -5px;
    font-size: .4em;
    color: var(--red);
    text-shadow: 0 0 8px var(--red);
    font-family: Arial, sans-serif;
  }

  .logo-slogan {
    font-size: .68rem; font-weight: 500;
    font-family: 'Space Grotesk', sans-serif;
    color: rgba(0,212,255,.75);
    letter-spacing: .18em;
    text-transform: uppercase;
    font-style: italic;
  }

  /* ── Indicador de sistema ── */
  .sys-status {
    display: flex; align-items: center; gap: .45rem;
    padding: .32rem .85rem;
    border-radius: 20px;
    border: 1px solid rgba(0,255,136,.3);
    background: rgba(0,255,136,.05);
    font-size: .68rem; font-weight: 600;
    color: var(--green); letter-spacing: .05em;
    font-family: 'Space Grotesk', sans-serif;
    white-space: nowrap; flex-shrink: 0;
    transition: border-color .4s, color .4s, background .4s;
  }
  .sys-status.error {
    border-color: rgba(239,68,68,.35);
    background: rgba(239,68,68,.06);
    color: #f87171;
  }
  .sys-status.error .sys-dot { background: #f87171; box-shadow: 0 0 6px #f87171; animation: none; }
  .sys-dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 7px var(--green);
    animation: blink 1.8s ease-in-out infinite;
    flex-shrink: 0;
  }

  .header-right {
    display: flex; align-items: center; gap: 1.25rem;
    flex-shrink: 0;
  }

  .live-badge {
    display: flex; align-items: center; gap: .45rem;
    border: 1px solid var(--green);
    border-radius: 20px; padding: .35rem 1rem;
    font-size: .7rem; font-weight: 700;
    color: var(--green); letter-spacing: .08em;
    background: rgba(0,255,136,0.05);
    box-shadow: 0 0 10px rgba(0,255,136,0.15);
  }
  .live-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
    animation: blink 1.4s infinite;
  }

  #last-update {
    font-size: .68rem; color: var(--txt3);
    font-family: 'Space Grotesk', sans-serif;
    letter-spacing: .04em;
  }

  /* ── layout ── */
  main { position: relative; z-index: 1; padding: 2rem 2.5rem; max-width: 1440px; margin: 0 auto; }

  .section-label {
    font-family: 'Orbitron', sans-serif;
    font-size: .6rem; font-weight: 700;
    letter-spacing: .25em; color: var(--neon);
    text-transform: uppercase; margin-bottom: 1rem;
    display: flex; align-items: center; gap: .75rem;
    opacity: .7;
  }
  .section-label::before { content: '//'; opacity: .5; font-family: Arial, monospace; }
  .section-label::after  { content: ''; flex: 1; height: 1px; background: linear-gradient(90deg, var(--border) 0%, transparent 100%); }

  /* ── KPI grid ── */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 1rem; margin-bottom: 2rem;
  }
  @media(max-width:1200px) { .kpi-grid { grid-template-columns: repeat(3,1fr); } }
  @media(max-width:600px)  { .kpi-grid { grid-template-columns: repeat(2,1fr); } }

  .kpi-card {
    background: var(--glass);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 1.4rem 1.2rem;
    backdrop-filter: blur(12px);
    transition: border-color .25s, box-shadow .25s, transform .2s;
    cursor: pointer;
    position: relative; overflow: hidden;
  }
  .kpi-card .kpi-hint {
    position: absolute; top: .6rem; right: .7rem;
    font-size: .55rem; color: var(--txt3); opacity: 0;
    transition: opacity .2s; letter-spacing: .04em;
    font-family: 'Space Grotesk', sans-serif;
  }
  .kpi-card:hover .kpi-hint { opacity: 1; }
  .kpi-card:active { transform: translateY(0) scale(.98); }
  .kpi-card::before {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent, var(--neon-glow), transparent);
    opacity: 0; transition: opacity .3s;
  }
  .kpi-card:hover {
    border-color: var(--border-h);
    box-shadow: 0 0 30px var(--neon-dim), inset 0 0 20px rgba(0,212,255,0.03);
    transform: translateY(-2px);
  }
  .kpi-card:hover::before { opacity: 1; }
  .kpi-card.primary {
    border-color: rgba(0,212,255,0.35);
    background: rgba(0,212,255,0.05);
    box-shadow: 0 0 25px rgba(0,212,255,0.1);
  }

  .kpi-label {
    font-size: .6rem; font-weight: 600;
    color: var(--txt2); text-transform: uppercase;
    letter-spacing: .12em; margin-bottom: .5rem;
  }
  .kpi-value {
    font-family: 'Arial Black', 'Arial', 'Helvetica Neue', sans-serif;
    font-size: 2.6rem; font-weight: 900; line-height: 1;
    transition: text-shadow .3s;
    letter-spacing: -.02em;
  }
  .kpi-value.neon  { color: var(--neon);  text-shadow: 0 0 20px var(--neon-glow), 0 0 40px rgba(0,212,255,0.2); }
  .kpi-value.red   { color: var(--red);   text-shadow: 0 0 20px var(--red-glow),  0 0 40px rgba(255,34,51,0.2); }
  .kpi-value.green { color: var(--green); text-shadow: 0 0 20px rgba(0,255,136,.5); }
  .kpi-value.white { color: var(--txt);   text-shadow: 0 0 15px rgba(255,255,255,.3); }
  .kpi-sub { font-size: .65rem; color: var(--txt3); margin-top: .45rem; letter-spacing: .03em; }

  /* ── main grid ── */
  .main-grid { display: grid; grid-template-columns: 1fr 1.65fr; gap: 1.25rem; margin-bottom: 1.25rem; }
  @media(max-width:960px) { .main-grid { grid-template-columns: 1fr; } }

  /* ── campañas grid ── */
  .campanas-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; margin-bottom: 2rem; }
  @media(max-width:960px) { .campanas-grid { grid-template-columns: 1fr; } }
  .campanas-chart-wrap { position: relative; height: 200px; }
  .campanas-embudo-row { margin-bottom: .55rem; }
  .campanas-embudo-label {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: .18rem;
  }
  .campanas-embudo-name { font-size: .68rem; color: var(--txt2); text-transform: capitalize; }
  .campanas-embudo-val  { font-family: 'Orbitron', sans-serif; font-size: .65rem; font-weight: 700; }
  .campanas-embudo-pct  { font-size: .55rem; color: var(--txt3); margin-left: .2rem; }
  .campanas-bar-track   { height: 6px; border-radius: 3px; background: rgba(255,255,255,.06); overflow: hidden; }
  .campanas-bar-fill    { height: 100%; border-radius: 3px; transition: width .7s cubic-bezier(.4,0,.2,1); }
  .fu-rate-headline     { display: flex; align-items: baseline; gap: .5rem; margin-bottom: 1rem; }
  .fu-rate-num          { font-family: 'Orbitron', sans-serif; font-size: 2rem; font-weight: 900; line-height: 1; }
  .fu-rate-sub          { font-size: .68rem; color: var(--txt2); }
  .fu-tipo-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: .32rem 0; border-bottom: 1px solid rgba(255,255,255,.04);
    font-size: .68rem;
  }
  .fu-tipo-tag  { font-family: 'Orbitron', sans-serif; font-size: .58rem; color: var(--neon); min-width: 2.5rem; }
  .fu-tipo-cnt  { color: var(--txt2); }
  .fu-tipo-pct  { font-weight: 700; min-width: 3rem; text-align: right; }

  /* ── mapa de calor por comuna ── */
  .heatmap-table {
    width: 100%; border-collapse: collapse; font-size: .76rem;
  }
  .heatmap-table th {
    font-size: .6rem; font-weight: 700; color: var(--txt3);
    text-transform: uppercase; letter-spacing: .07em;
    padding: .35rem .6rem; text-align: left;
    border-bottom: 1px solid var(--border);
  }
  .heatmap-table th:not(:first-child) { text-align: right; }
  .heatmap-table td {
    padding: .42rem .6rem; border-bottom: 1px solid rgba(255,255,255,.03);
    vertical-align: middle;
  }
  .heatmap-table td:not(:first-child) { text-align: right; }
  .heatmap-table tr:last-child td { border-bottom: none; }
  .heatmap-table tr:hover td { background: rgba(255,255,255,.03); }
  .hm-comuna { font-weight: 600; color: var(--txt); }
  .hm-total  { font-family: 'Orbitron', sans-serif; font-weight: 700; font-size: .72rem; }
  .hm-hot    { color: #00D4FF; }
  .hm-score  { color: var(--txt2); font-size: .7rem; }
  .hm-bar-wrap { width: 90px; display: inline-block; vertical-align: middle; }
  .hm-bar-track { height: 4px; border-radius: 2px; background: rgba(255,255,255,.07); overflow: hidden; }
  .hm-bar-fill  { height: 100%; border-radius: 2px; transition: width .6s cubic-bezier(.4,0,.2,1); }
  .hm-rank-hot  { color: var(--red); }
  .hm-rank-warm { color: #FFAA00; }
  .hm-rank-cold { color: var(--txt3); }
  .heatmap-search {
    width: 100%; background: rgba(255,255,255,.04); border: 1px solid var(--border);
    border-radius: 8px; color: var(--txt); font-family: 'Space Grotesk', sans-serif;
    font-size: .78rem; padding: .5rem .8rem; outline: none;
    transition: border-color .2s; margin-bottom: 1rem;
  }
  .heatmap-search:focus { border-color: var(--neon); }
  .hm-pagination {
    display: flex; align-items: center; justify-content: space-between;
    margin-top: .9rem; font-size: .68rem; color: var(--txt3);
  }
  .hm-pagination button {
    background: rgba(255,255,255,.06); border: 1px solid var(--border);
    border-radius: 6px; color: var(--txt2); font-size: .65rem; font-weight: 600;
    padding: .28rem .75rem; cursor: pointer; transition: all .2s;
  }
  .hm-pagination button:hover:not(:disabled) { border-color: var(--neon); color: var(--neon); }
  .hm-pagination button:disabled { opacity: .3; cursor: default; }

  /* ── glass cards ── */
  .card {
    background: var(--glass);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 1.5rem 1.75rem;
    backdrop-filter: blur(12px);
    overflow: hidden;
    position: relative;
  }
  .card::after {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent 0%, var(--neon-glow) 50%, transparent 100%);
    opacity: .4;
  }

  .card-title {
    font-family: 'Orbitron', sans-serif;
    font-size: .6rem; font-weight: 700;
    color: var(--neon); text-transform: uppercase;
    letter-spacing: .2em; margin-bottom: 1.25rem;
    display: flex; align-items: center; gap: .5rem;
    opacity: .85;
  }
  .card-title::before { content: '>'; font-size: 1em; font-family: Arial, sans-serif; }

  /* ── chart ── */
  .chart-wrap { position: relative; height: 260px; }

  /* ── leads list ── */
  .leads-list { display: flex; flex-direction: column; gap: .5rem; max-height: 320px; overflow-y: auto; }

  .lead-row {
    display: grid; grid-template-columns: 1.4rem 1fr auto auto auto auto auto auto;
    align-items: center; gap: .75rem;
    background: rgba(255,255,255,0.02);
    border-radius: 10px; padding: .65rem 1rem;
    border-left: 2px solid transparent;
    transition: background .2s, border-color .2s, box-shadow .2s;
  }
  .lead-priority { font-size: 1rem; line-height: 1; flex-shrink: 0; }

  .btn-tomar {
    font-size: .58rem; font-weight: 700;
    padding: .22rem .7rem; border-radius: 20px;
    border: 1px solid rgba(0,212,255,0.35);
    background: rgba(0,212,255,0.07);
    color: var(--neon); cursor: pointer;
    text-transform: uppercase; letter-spacing: .07em;
    white-space: nowrap; font-family: 'Space Grotesk', sans-serif;
    transition: background .2s, box-shadow .2s;
  }
  .btn-tomar:hover:not(:disabled) {
    background: rgba(0,212,255,0.18);
    box-shadow: 0 0 10px rgba(0,212,255,0.3);
  }
  .btn-tomar:disabled { opacity: .45; cursor: default; }

  .btn-liberar {
    font-size: .58rem; font-weight: 700;
    padding: .22rem .7rem; border-radius: 20px;
    border: 1px solid rgba(168,85,247,0.4);
    background: rgba(168,85,247,0.1);
    color: #c084fc; cursor: pointer;
    text-transform: uppercase; letter-spacing: .07em;
    white-space: nowrap; font-family: 'Space Grotesk', sans-serif;
    transition: background .2s, box-shadow .2s;
  }
  .btn-liberar:hover {
    background: rgba(168,85,247,0.2);
    box-shadow: 0 0 10px rgba(168,85,247,0.3);
  }
  .lead-row:hover {
    background: var(--glass-h);
    box-shadow: inset 0 0 20px rgba(0,212,255,0.04);
  }
  .lead-name  { font-size: .84rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .lead-phone { font-size: .68rem; color: var(--txt2); margin-top: 2px; font-family: monospace; letter-spacing: .03em; }
  .lead-resumen {
    font-size: .62rem; color: var(--txt3); margin-top: 3px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    max-width: 340px; letter-spacing: .01em; line-height: 1.4;
  }
  .lead-score {
    font-family: 'Orbitron', sans-serif;
    font-size: .75rem; font-weight: 700;
    color: var(--neon);
    text-shadow: 0 0 8px var(--neon-glow);
    white-space: nowrap;
  }

  .estado-badge {
    font-size: .58rem; font-weight: 700; padding: .22rem .65rem;
    border-radius: 20px; white-space: nowrap;
    text-transform: uppercase; letter-spacing: .07em;
    font-family: 'Space Grotesk', sans-serif;
  }

  /* (tabla de mensajes reemplazada por tarjetas — ver .msg-card-list) */

  .tag-user      { color: var(--neon);  font-weight: 700; font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; }
  .tag-assistant { color: var(--txt2);  font-weight: 600; font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; }
  .tag-alta   { color: var(--red);    font-weight: 700; font-size: .72rem; text-shadow: 0 0 8px var(--red-glow); }
  .tag-media  { color: var(--orange); font-weight: 700; font-size: .72rem; }
  .tag-baja   { color: var(--txt3);   font-size: .72rem; }
  .msg-text   { color: rgba(255,255,255,.8); line-height: 1.45; max-width: 420px; }
  .msg-time   { font-size: .62rem; color: var(--txt3); white-space: nowrap; font-family: monospace; letter-spacing: .04em; }

  /* ── scrollbar ── */
  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(0,212,255,0.25); border-radius: 10px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--neon); box-shadow: 0 0 6px var(--neon); }

  /* ── animaciones ── */
  @keyframes blink { 0%,100%{opacity:1;box-shadow:0 0 6px var(--green)} 50%{opacity:.3;box-shadow:none} }
  @keyframes fadeUp { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
  @keyframes scanline {
    0%   { transform: translateY(-100%); }
    100% { transform: translateY(100vh); }
  }
  .fade-in { animation: fadeUp .35s ease forwards; }

  /* ── empty state ── */
  .empty {
    text-align: center; padding: 3rem 1rem;
    color: var(--txt3); font-size: .8rem;
    letter-spacing: .05em;
  }
  .empty::before { content: ''; }
  .empty::after  { content: ''; }

  /* ── embudo de conversión ── */
  .funnel-grid {
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 1rem; margin-bottom: 2rem;
  }
  @media(max-width:720px) { .funnel-grid { grid-template-columns: 1fr; } }

  .funnel-card {
    background: var(--glass);
    border: 1px solid var(--border);
    border-radius: 14px; padding: 1.4rem 1.5rem;
    backdrop-filter: blur(12px);
    position: relative; overflow: hidden;
    transition: border-color .25s, box-shadow .25s;
  }
  .funnel-card:hover {
    border-color: var(--border-h);
    box-shadow: 0 0 24px var(--neon-dim);
  }
  .funnel-card::after {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent, var(--neon-glow), transparent);
    opacity: .4;
  }

  .funnel-label {
    font-size: .58rem; font-weight: 700;
    color: var(--txt2); text-transform: uppercase;
    letter-spacing: .12em; margin-bottom: .6rem;
  }
  .funnel-arrow { color: var(--neon); margin: 0 .3em; opacity: .7; }

  .funnel-pct {
    font-family: 'Arial Black', sans-serif;
    font-size: 2.8rem; font-weight: 900; line-height: 1;
    letter-spacing: -.02em; margin-bottom: .6rem;
  }

  .funnel-bar-track {
    height: 4px; border-radius: 4px;
    background: rgba(255,255,255,0.06);
    overflow: hidden; margin-bottom: .55rem;
  }
  .funnel-bar-fill {
    height: 100%; border-radius: 4px;
    transition: width 1s cubic-bezier(.4,0,.2,1);
    width: 0%;
  }

  .funnel-counts {
    font-size: .65rem; color: var(--txt3);
    letter-spacing: .03em;
  }
  .funnel-counts strong { color: var(--txt2); }

  /* ── scan line decorativa en header ── */
  .scan-line {
    position: absolute; bottom: -1px; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent 0%, var(--neon) 50%, transparent 100%);
    opacity: .6;
  }

  /* ── Live Chat ─────────────────────────────────────────────────────────── */
  .chat-container {
    display: grid;
    grid-template-columns: 300px 1fr;
    height: 620px;
    border: 1px solid var(--border);
    border-radius: 16px;
    overflow: hidden;
    background: var(--glass);
    backdrop-filter: blur(12px);
    margin-bottom: 2.5rem;
    position: relative;
  }
  .chat-container::after {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent 0%, var(--neon-glow) 50%, transparent 100%);
    opacity: .4; pointer-events: none;
  }

  .chat-sidebar {
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    background: rgba(0,0,0,0.2);
  }
  .chat-sidebar-header {
    padding: .9rem 1.2rem;
    border-bottom: 1px solid var(--border);
    font-size: .6rem; font-weight: 700;
    color: var(--neon); text-transform: uppercase; letter-spacing: .18em;
    display: flex; justify-content: space-between; align-items: center;
    font-family: 'Orbitron', sans-serif;
  }
  .conv-list { flex: 1; overflow-y: auto; min-height: 0; -webkit-overflow-scrolling: touch; overscroll-behavior: contain; }

  .conv-item {
    padding: .65rem 1rem;
    cursor: pointer;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    border-left: 3px solid transparent;
    transition: background .15s, border-color .15s;
  }
  .conv-item:hover { background: var(--glass-h); }
  .conv-item.active {
    background: rgba(0,212,255,0.07);
    border-left-color: var(--neon);
  }
  .conv-item-top { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: .25rem; }
  .conv-item-name { font-size: .82rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 160px; }
  .conv-item-time { font-size: .58rem; color: var(--txt3); font-family: monospace; flex-shrink: 0; }
  .conv-item-bottom { display: flex; justify-content: space-between; align-items: center; gap: .5rem; }
  .conv-item-preview { font-size: .67rem; color: var(--txt3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }

  .modo-badge {
    font-size: .55rem; font-weight: 700;
    padding: .15rem .5rem; border-radius: 20px;
    text-transform: uppercase; letter-spacing: .07em; flex-shrink: 0;
    font-family: 'Space Grotesk', sans-serif;
  }
  .modo-badge.humano { background: rgba(168,85,247,0.15); border: 1px solid rgba(168,85,247,0.4); color: #c084fc; }
  .modo-badge.bot    { background: rgba(0,212,255,0.08);  border: 1px solid rgba(0,212,255,0.25); color: var(--neon); }

  .chat-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }

  .chat-main-header {
    padding: .85rem 1.4rem;
    border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
    min-height: 54px; gap: 1rem;
    background: rgba(0,0,0,0.1);
  }
  .chat-contact-info { min-width: 0; }
  .chat-contact-name { font-size: .9rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chat-contact-phone { font-size: .65rem; color: var(--txt2); font-family: monospace; margin-top: 2px; }
  .chat-header-actions { display: flex; gap: .5rem; align-items: center; flex-shrink: 0; }

  .chat-messages {
    flex: 1; overflow-y: auto;
    padding: 1.2rem 1.4rem;
    display: flex; flex-direction: column; gap: .5rem;
    min-height: 0;
    -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain;
  }

  .msg-bubble-wrap { display: flex; flex-direction: column; }
  .msg-bubble-wrap.user     { align-items: flex-start; }
  .msg-bubble-wrap.assistant { align-items: flex-end; }

  .msg-bubble {
    max-width: 72%; padding: .6rem .95rem;
    border-radius: 12px; font-size: .83rem; line-height: 1.45;
    word-break: break-word; white-space: pre-wrap;
  }
  .msg-bubble.user {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.1);
    border-bottom-left-radius: 4px;
  }
  .msg-bubble.assistant {
    background: rgba(0,212,255,0.1);
    border: 1px solid rgba(0,212,255,0.22);
    border-bottom-right-radius: 4px;
    color: var(--txt);
  }
  .msg-bubble-time {
    font-size: .58rem; color: var(--txt3);
    margin-top: .2rem; font-family: monospace;
  }

  .chat-input-row {
    padding: .7rem 1rem;
    border-top: 1px solid var(--border);
    display: flex; gap: .6rem; align-items: flex-end;
    background: rgba(0,0,0,0.15);
  }
  .chat-input {
    flex: 1; background: rgba(255,255,255,0.04);
    border: 1px solid var(--border); border-radius: 8px;
    color: var(--txt); padding: .55rem .9rem;
    font-family: 'Space Grotesk', sans-serif; font-size: .83rem;
    resize: none; outline: none; transition: border-color .2s;
    min-height: 38px; max-height: 110px; overflow-y: auto;
  }
  .chat-input:focus { border-color: var(--neon); box-shadow: 0 0 8px var(--neon-dim); }
  .chat-input:disabled { opacity: .35; }
  .chat-send-btn {
    background: rgba(0,212,255,0.1); border: 1px solid rgba(0,212,255,0.35);
    color: var(--neon); padding: .52rem 1.1rem; border-radius: 8px;
    font-family: 'Space Grotesk', sans-serif; font-size: .78rem; font-weight: 700;
    cursor: pointer; transition: background .2s, box-shadow .2s; white-space: nowrap;
  }
  .chat-send-btn:hover:not(:disabled) {
    background: rgba(0,212,255,0.2);
    box-shadow: 0 0 12px rgba(0,212,255,0.3);
  }
  .chat-send-btn:disabled { opacity: .35; cursor: default; }

  .conv-new-msg {
    animation: convFlash .8s ease;
  }
  @keyframes convFlash {
    0%   { background: rgba(0,212,255,0.18); }
    100% { background: transparent; }
  }

  .btn-ver-chat {
    background: none; border: none; cursor: pointer;
    font-size: .8rem; padding: .1rem .2rem; opacity: .5;
    transition: opacity .2s; line-height: 1; border-radius: 4px;
  }
  .btn-ver-chat:hover { opacity: 1; background: rgba(0,212,255,0.1); }
  .btn-detail {
    background: none; border: none; cursor: pointer;
    font-size: .82rem; padding: .1rem .2rem; opacity: .4;
    transition: opacity .2s; line-height: 1; border-radius: 4px;
  }
  .btn-detail:hover { opacity: 1; background: rgba(168,85,247,.12); }
  .ld-section { margin-bottom: 1rem; }
  .ld-section-title {
    font-family: 'Orbitron', sans-serif; font-size: .55rem;
    font-weight: 700; letter-spacing: .12em; color: var(--txt3);
    text-transform: uppercase; margin-bottom: .45rem;
  }
  .ld-resumen-block {
    background: rgba(0,212,255,.04); border: 1px solid rgba(0,212,255,.15);
    border-radius: 10px; padding: 1rem 1.1rem;
    font-size: .8rem; line-height: 1.75; color: var(--txt);
    white-space: pre-wrap; font-family: 'Space Grotesk', sans-serif;
  }
  .ld-resumen-empty {
    background: rgba(255,255,255,.03); border: 1px dashed rgba(255,255,255,.1);
    border-radius: 10px; padding: .85rem 1.1rem;
    font-size: .72rem; color: var(--txt3); text-align: center;
  }
  .ld-meta-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: .5rem .75rem;
  }
  .ld-meta-item { }
  .ld-meta-label { font-size: .58rem; color: var(--txt3); letter-spacing: .06em; text-transform: uppercase; margin-bottom: .15rem; }
  .ld-meta-value { font-size: .78rem; color: var(--txt); font-weight: 600; }
  .ld-objeciones { display: flex; flex-wrap: wrap; gap: .35rem; }
  .ld-obj-tag {
    background: rgba(255,34,51,.1); border: 1px solid rgba(255,34,51,.3);
    color: #ff6b7a; border-radius: 6px; padding: .15rem .55rem;
    font-size: .65rem; font-weight: 700; letter-spacing: .04em;
  }
  .ld-regenerar-btn {
    background: rgba(168,85,247,.08); border: 1px solid rgba(168,85,247,.3);
    color: #c084fc; font-family: 'Space Grotesk', sans-serif;
    font-size: .65rem; font-weight: 700; padding: .3rem .8rem;
    border-radius: 8px; cursor: pointer; transition: background .15s;
  }
  .ld-regenerar-btn:hover { background: rgba(168,85,247,.18); }
  .btn-notas {
    background: none; border: none; cursor: pointer;
    font-size: .85rem; padding: .1rem .2rem; opacity: .3;
    transition: opacity .2s; line-height: 1; border-radius: 4px;
  }
  .btn-notas:hover  { opacity: .9; background: rgba(168,85,247,0.12); }
  .btn-notas.activo { opacity: .85; }
  .btn-tags {
    background: none; border: none; cursor: pointer;
    font-size: .8rem; padding: .1rem .2rem; opacity: .3;
    transition: opacity .2s; line-height: 1; border-radius: 4px;
  }
  .btn-tags:hover  { opacity: .9; background: rgba(245,158,11,.12); }
  .btn-tags.activo { opacity: .9; }

  /* ── Tag chips ── */
  .tag-chip {
    display: inline-flex; align-items: center;
    padding: .18rem .55rem; border-radius: 20px;
    font-size: .62rem; font-weight: 700; letter-spacing: .04em;
    white-space: nowrap; cursor: default;
  }
  .tag-chip-sm {
    display: inline-flex; align-items: center;
    padding: .1rem .38rem; border-radius: 20px;
    font-size: .55rem; font-weight: 700; letter-spacing: .03em;
    white-space: nowrap;
  }
  .lead-tags { display: flex; flex-wrap: wrap; gap: .25rem; margin-top: .25rem; }

  /* ── Filter bar ── */
  .filter-bar {
    display: flex; flex-wrap: wrap; gap: .4rem;
    padding: .55rem 0 .4rem; margin-bottom: .4rem;
    border-bottom: 1px solid rgba(255,255,255,.06);
  }
  .filter-input, .filter-select, .filter-date {
    background: rgba(255,255,255,.04); border: 1px solid var(--border);
    border-radius: 8px; color: var(--txt); padding: .3rem .6rem;
    font-family: 'Space Grotesk', sans-serif; font-size: .68rem;
    outline: none; transition: border-color .2s;
  }
  .filter-input  { flex: 1; min-width: 140px; }
  .filter-select { min-width: 120px; cursor: pointer; }
  .filter-date   { min-width: 110px; }
  .filter-score  {
    background: rgba(255,255,255,.04); border: 1px solid var(--border);
    border-radius: 8px; color: var(--txt); padding: .3rem .5rem;
    font-family: 'Space Grotesk', sans-serif; font-size: .68rem;
    width: 68px; outline: none; transition: border-color .2s;
  }
  .filter-input:focus, .filter-select:focus,
  .filter-date:focus, .filter-score:focus { border-color: rgba(0,212,255,.5); }
  .filter-select option { background: #111; }
  .filter-clear {
    background: rgba(255,34,51,.07); border: 1px solid rgba(255,34,51,.2);
    color: rgba(255,80,80,.8); border-radius: 8px; padding: .3rem .65rem;
    font-size: .65rem; font-weight: 700; cursor: pointer;
    font-family: 'Space Grotesk', sans-serif; transition: background .15s;
    white-space: nowrap;
  }
  .filter-clear:hover { background: rgba(255,34,51,.15); }
  .filter-count { font-size: .6rem; color: var(--txt3); align-self: center; white-space: nowrap; }

  /* ── Tags modal ── */
  .tags-predefined { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1rem; }
  .tag-toggle {
    padding: .28rem .75rem; border-radius: 20px; cursor: pointer;
    font-size: .7rem; font-weight: 700; letter-spacing: .04em;
    transition: opacity .15s, transform .1s; border: 1px solid transparent;
    opacity: .4;
  }
  .tag-toggle.activo { opacity: 1; transform: scale(1.05); }
  .tag-toggle:hover  { opacity: .85; }

  /* ── Modal: chat completo ── */
  .modal-overlay {
    display: none; position: fixed; inset: 0; z-index: 2000;
    background: rgba(0,0,0,0.88); backdrop-filter: blur(10px);
    align-items: flex-start; justify-content: center;
    padding: 2rem 1rem; overflow-y: auto;
  }
  .modal-box {
    width: 100%; max-width: 740px;
    background: #080808;
    border: 1px solid var(--border);
    border-radius: 20px; overflow: hidden;
    box-shadow: 0 0 60px rgba(0,212,255,0.08);
    position: relative;
  }
  .modal-box::after {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent, var(--neon-glow), transparent);
    opacity: .5;
  }
  .modal-header {
    padding: 1.2rem 1.75rem;
    border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
    background: rgba(0,212,255,0.03);
  }
  .modal-close {
    background: rgba(255,255,255,0.04); border: 1px solid var(--border);
    color: var(--txt2); padding: .4rem 1rem; border-radius: 8px;
    cursor: pointer; font-size: .75rem; font-family: 'Space Grotesk', sans-serif;
    transition: background .2s, color .2s;
  }
  .modal-close:hover { background: rgba(255,34,51,0.12); color: var(--red); border-color: var(--red); }
  .modal-messages {
    padding: 1.5rem 1.75rem; display: flex; flex-direction: column;
    gap: .9rem; max-height: 72vh; overflow-y: auto;
  }

  @media(max-width:860px) {
    .chat-container { grid-template-columns: 1fr; height: auto; }
    .chat-sidebar   { height: 220px; border-right: none; border-bottom: 1px solid var(--border); }
    .chat-main      { height: 440px; }
  }

  /* ── MOBILE RESPONSIVE ────────────────────────────────────── */
  @media(max-width:768px) {
    header {
      padding: max(.6rem, env(safe-area-inset-top)) max(.85rem, env(safe-area-inset-right))
               .6rem max(.85rem, env(safe-area-inset-left));
      min-height: auto;
      height: auto;
      /* 2 filas: logo arriba, nav + live abajo */
      flex-direction: column;
      align-items: flex-start;
      gap: .55rem;
      justify-content: flex-start;
    }
    /* Fila 1: logo a la izquierda, sistema a la derecha */
    .logo {
      width: 100%; justify-content: space-between; align-items: center;
    }
    .logo-icon { width: 34px; height: 34px; font-size: .92rem; border-radius: 8px; }
    .logo-name { font-size: .82rem; letter-spacing: .08em; }
    .logo-slogan { font-size: .58rem; letter-spacing: .12em; }
    /* Sys-status: en móvil se muestra dentro del logo row */
    #sys-status-desktop { display: none !important; }
    #sys-status-mobile  { display: flex !important; margin-left: auto; }
    .sys-status { padding: .25rem .6rem; font-size: .6rem; }
    /* Ocultar live badge y timestamp en móvil */
    .header-right { display: none; }
    /* Fila 2: tabs + live ocupan todo el ancho */
    .tab-nav { flex-shrink: 0; }
    .tab-btn { padding: .28rem .6rem; font-size: .64rem; }
    .btn-live { padding: .28rem .75rem; font-size: .68rem; flex-shrink: 0; }

    main {
      padding: 1rem;
      padding-bottom: max(1rem, env(safe-area-inset-bottom));
    }

    .section-label { margin-bottom: .75rem; font-size: .55rem; }

    .kpi-grid { gap: .55rem; margin-bottom: 1.25rem; }
    .kpi-card { padding: 1rem .9rem; border-radius: 10px; }
    .kpi-value { font-size: 2rem; }
    .kpi-label { font-size: .55rem; }
    .kpi-sub   { font-size: .58rem; }

    .funnel-grid { gap: .6rem; margin-bottom: 1.25rem; }
    .funnel-card { padding: 1rem 1.1rem; border-radius: 10px; }
    .funnel-pct  { font-size: 2.2rem; }
    .funnel-label { font-size: .55rem; }

    .main-grid { gap: .8rem; margin-bottom: 1rem; }
    .card { padding: 1.1rem 1.1rem; border-radius: 12px; }
    .chart-wrap { height: 200px; }

    .leads-list { max-height: 220px; }
    .lead-row   {
      grid-template-columns: 1.2rem 1fr auto auto auto;
      gap: .4rem; padding: .55rem .7rem;
    }
    .lead-row .lead-score { display: none; }
    .lead-resumen { display: none; }
    .lead-name  { font-size: .78rem; }
    .lead-phone { font-size: .62rem; }
    .btn-tomar, .btn-liberar { font-size: .54rem; padding: .2rem .55rem; }

    /* Mensajes tarjetas en móvil */
    .msg-card { grid-template-columns: 34px 1fr auto; gap: .5rem; padding: .55rem .65rem; }
    .msg-avatar { width: 34px; height: 34px; font-size: .72rem; }
    .msg-card-name { font-size: .78rem; }
    .msg-card-preview { font-size: .68rem; }

    /* Live Chat móvil */
    .chat-container { margin-bottom: 1.5rem; }
    .chat-sidebar   { height: 185px; }
    .chat-main      { height: 390px; }
    .chat-main-header { padding: .65rem 1rem; }
    .chat-contact-name  { font-size: .82rem; }
    .chat-contact-phone { font-size: .6rem; }
    .chat-header-actions { gap: .3rem; }
    .chat-messages { padding: .9rem 1rem; }
    .msg-bubble { font-size: .78rem; padding: .5rem .8rem; max-width: 85%; }
    .chat-input-row { padding: .5rem .7rem; gap: .4rem; }
    .chat-input { font-size: .78rem; padding: .48rem .75rem; }
    .chat-send-btn { font-size: .72rem; padding: .48rem .85rem; }

    /* Modal responsive */
    .modal-overlay { padding: .3rem; align-items: flex-end; }
    .modal-box     { border-radius: 18px 18px 0 0; max-width: 100%; }
    .modal-header  { padding: .9rem 1.2rem; }
    .modal-messages { padding: 1rem 1.1rem; max-height: 76vh; }
  }

  /* Ajustes extra para pantallas muy pequeñas (< 400px) */
  @media(max-width:400px) {
    .kpi-grid { grid-template-columns: repeat(2,1fr); gap: .4rem; }
    .kpi-value { font-size: 1.75rem; }
    .logo-name { font-size: .65rem; }
    .chat-sidebar { height: 160px; }
    .chat-main    { height: 360px; }
  }

  /* ── Mensajes recientes — tarjetas ────────────────────────────────────── */
  .msg-card-list {
    display: flex; flex-direction: column; gap: .35rem;
    max-height: 520px; overflow-y: auto;
    -webkit-overflow-scrolling: touch; overscroll-behavior: contain;
  }
  .msg-card {
    display: grid;
    grid-template-columns: 40px 1fr auto;
    gap: .75rem; align-items: center;
    padding: .65rem .9rem; border-radius: 10px;
    background: rgba(255,255,255,.02);
    border: 1px solid transparent;
    cursor: pointer;
    transition: background .15s, border-color .15s;
  }
  .msg-card:hover {
    background: var(--glass-h);
    border-color: var(--border);
  }
  .msg-avatar {
    width: 40px; height: 40px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: .82rem; font-weight: 700; flex-shrink: 0;
    font-family: 'Space Grotesk', sans-serif; letter-spacing: -.01em;
  }
  .msg-card-body { min-width: 0; }
  .msg-card-name {
    font-size: .84rem; font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    display: flex; align-items: center; gap: .4rem;
    margin-bottom: .18rem;
  }
  .msg-card-preview {
    font-size: .72rem; color: var(--txt2);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    line-height: 1.4;
  }
  .msg-card-preview.bot-msg { color: rgba(0,212,255,.6); }
  .msg-card-right {
    display: flex; flex-direction: column;
    align-items: flex-end; gap: .3rem; flex-shrink: 0;
  }
  .msg-card-time { font-size: .6rem; color: var(--txt3); font-family: monospace; white-space: nowrap; }
  .msg-role-dot {
    width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
  }

  /* ── TABS ──────────────────────────────────────────────────── */
  :root { --app-h: 100vh; }
  /* dvh = dynamic viewport height: sigue la barra del navegador en móvil */
  @supports (height: 100dvh) { :root { --app-h: 100dvh; } }
  /* Layout de app pantalla completa via flexbox — sin cálculos de altura */
  html { height: var(--app-h); overflow: hidden; }
  body { overflow: hidden; height: 100%; display: flex; flex-direction: column; }
  header { flex-shrink: 0; }
  .tab-nav {
    display: flex; gap: .2rem;
    background: rgba(0,0,0,.45); border: 1px solid var(--border);
    border-radius: 22px; padding: .22rem;
  }
  .tab-btn {
    padding: .32rem 1.1rem; border-radius: 18px; border: none;
    background: transparent; color: var(--txt2);
    font-size: .72rem; font-weight: 600; cursor: pointer;
    font-family: 'Space Grotesk', sans-serif; letter-spacing: .04em;
    transition: all .2s; white-space: nowrap;
  }
  .tab-btn.active { background: rgba(0,212,255,.15); color: var(--neon); box-shadow: 0 0 10px rgba(0,212,255,.2); }
  .tab-btn:hover:not(.active) { color: var(--txt); background: rgba(255,255,255,.06); }
  .btn-live {
    padding: .32rem 1rem; border-radius: 18px; border: 1px solid var(--border);
    background: transparent; color: var(--txt2); font-size: .72rem; font-weight: 600;
    cursor: pointer; font-family: 'Space Grotesk', sans-serif; letter-spacing: .04em;
    transition: all .2s; white-space: nowrap;
  }
  .btn-live.active {
    background: rgba(0,212,255,.15); color: var(--neon);
    border-color: var(--neon); box-shadow: 0 0 10px rgba(0,212,255,.25);
  }
  .btn-live:hover:not(.active) { color: var(--txt); background: rgba(255,255,255,.06); }

  /* ── PANELS ─────────────────────────────────────────────────── */
  #panel-metrics {
    flex: 1; min-height: 0;
    overflow-y: auto; overflow-x: hidden;
    -webkit-overflow-scrolling: touch;
    padding-bottom: env(safe-area-inset-bottom);
  }
  #panel-chat {
    flex: 1; min-height: 0;
    display: none; flex-direction: column;
  }

  /* ── WHATSAPP WEB LAYOUT ─────────────────────────────────────── */
  .wa-layout { display: flex; flex: 1; min-height: 0; overflow: hidden; }

  /* Sidebar */
  .wa-sidebar {
    width: 320px; flex-shrink: 0;
    display: flex; flex-direction: column;
    border-right: 1px solid var(--border);
    background: rgba(0,0,0,.25); overflow: hidden;
  }
  .wa-sidebar-hdr {
    padding: .8rem 1.1rem; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
    background: rgba(0,212,255,.03); flex-shrink: 0;
    font-family: 'Orbitron', sans-serif;
    font-size: .6rem; font-weight: 700; letter-spacing: .18em;
    color: var(--neon); text-transform: uppercase;
  }
  .wa-search-wrap { padding: .6rem .9rem .5rem; border-bottom: 1px solid rgba(255,255,255,.05); flex-shrink: 0; }
  .wa-search-input {
    width: 100%; background: rgba(255,255,255,.06);
    border: 1px solid rgba(255,255,255,.1); border-radius: 20px;
    padding: .46rem .9rem; color: var(--txt);
    font-size: .8rem; outline: none;
    font-family: 'Space Grotesk', sans-serif; transition: border-color .2s;
  }
  .wa-search-input:focus { border-color: rgba(0,212,255,.4); }
  .wa-search-input::placeholder { color: var(--txt3); }
  .wa-tag-filter-wrap {
    padding: .45rem .9rem .55rem; border-bottom: 1px solid rgba(255,255,255,.05);
    flex-shrink: 0;
  }
  .wa-tag-filter-select {
    width: 100%; background: rgba(255,255,255,.04);
    border: 1px solid rgba(255,255,255,.08); border-radius: 8px;
    padding: .38rem .75rem; color: var(--txt2);
    font-size: .72rem; outline: none; cursor: pointer;
    font-family: 'Space Grotesk', sans-serif; transition: border-color .2s;
    appearance: none; -webkit-appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23666'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right .65rem center;
    padding-right: 1.8rem;
  }
  .wa-tag-filter-select:focus { border-color: rgba(0,212,255,.35); color: var(--txt); }
  .wa-tag-filter-select option { background: #111; color: var(--txt); }
  /* Indicador tiempo sin respuesta */
  .wa-sin-resp {
    display: inline-flex; align-items: center; gap: .18rem;
    font-size: .58rem; font-weight: 700; border-radius: 8px;
    padding: .08rem .35rem; white-space: nowrap; flex-shrink: 0;
  }
  .wa-sin-resp.verde  { background: rgba(34,197,94,.15);  color: #4ade80; }
  .wa-sin-resp.amarillo { background: rgba(245,158,11,.15); color: #fbbf24; }
  .wa-sin-resp.rojo   { background: rgba(239,68,68,.15);  color: #f87171; }

  /* Badge respondió / sin respuesta en sidebar */
  .wa-resp-badge {
    display: inline-flex; align-items: center; gap: .18rem;
    font-size: .58rem; font-weight: 700; border-radius: 8px;
    padding: .1rem .4rem; white-space: nowrap; flex-shrink: 0;
    letter-spacing: .04em;
  }
  .wa-resp-badge.respondio   { background: rgba(34,197,94,.18);  color: #4ade80; border: 1px solid rgba(34,197,94,.35); }
  .wa-resp-badge.sin-resp    { background: rgba(100,100,120,.15); color: #9ca3af; border: 1px solid rgba(100,100,120,.3); }

  /* Filtros de estado (tabs) */
  .wa-status-filter-wrap {
    padding: .45rem .9rem .5rem; border-bottom: 1px solid rgba(255,255,255,.05);
    flex-shrink: 0; display: flex; gap: .3rem; flex-wrap: wrap;
  }
  .wa-sf-btn {
    flex: 1; min-width: 0; padding: .28rem .5rem; border-radius: 8px;
    border: 1px solid rgba(255,255,255,.1);
    background: rgba(255,255,255,.04); color: var(--txt2);
    font-family: 'Space Grotesk', sans-serif; font-size: .62rem; font-weight: 600;
    cursor: pointer; transition: all .15s; white-space: nowrap; text-align: center;
  }
  .wa-sf-btn:hover { background: rgba(255,255,255,.09); color: var(--txt); }
  .wa-sf-btn.active {
    background: rgba(0,212,255,.15); color: var(--neon);
    border-color: rgba(0,212,255,.45); box-shadow: 0 0 8px rgba(0,212,255,.15);
  }
  .wa-sf-btn.active.manual {
    background: rgba(239,68,68,.15); color: #f87171;
    border-color: rgba(239,68,68,.45); box-shadow: 0 0 8px rgba(239,68,68,.15);
  }

  /* Indicador "atendido por" en sidebar */
  .wa-atendido {
    font-size: .58rem; color: var(--txt3); white-space: nowrap; flex-shrink: 0;
    letter-spacing: .02em;
  }
  .wa-atendido.valentina { color: rgba(0,212,255,.7); }
  .wa-atendido.yo        { color: rgba(34,197,94,.8); }

  .wa-conv-list {
    flex: 1; overflow-y: auto; min-height: 0;
    -webkit-overflow-scrolling: touch; overscroll-behavior: contain;
  }

  /* Contact items */
  .wa-conv-item {
    display: flex; align-items: center; gap: .72rem;
    padding: .72rem 1rem; cursor: pointer;
    border-bottom: 1px solid rgba(255,255,255,.04);
    border-left: 3px solid transparent;
    transition: background .12s, border-color .12s;
  }
  .wa-conv-item:hover { background: rgba(0,212,255,.05); }
  .wa-conv-item.active { background: rgba(0,212,255,.1); border-left-color: var(--neon); }
  .wa-conv-avatar {
    width: 44px; height: 44px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: .9rem; font-weight: 700; font-family: 'Space Grotesk', sans-serif;
  }
  .wa-conv-info { flex: 1; min-width: 0; }
  .wa-conv-name-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: .2rem; gap: .25rem; }
  .wa-conv-priority { font-size: .75rem; flex-shrink: 0; line-height: 1; }
  .wa-conv-name { font-size: .84rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }
  .wa-conv-time { font-size: .6rem; color: var(--txt3); font-family: monospace; flex-shrink: 0; }
  .wa-conv-preview-row { display: flex; align-items: center; gap: .4rem; }
  .wa-conv-preview { font-size: .72rem; color: var(--txt2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
  .wa-conv-score { font-size: .6rem; font-family: 'Orbitron', monospace; font-weight: 700; flex-shrink: 0; }
  .wa-conv-badges { display: flex; gap: .3rem; align-items: center; flex-shrink: 0; }

  /* Chat panel */
  .wa-chat-panel { flex: 1; min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
  .wa-empty { flex: 1; display: flex; align-items: center; justify-content: center; background: rgba(0,0,0,.1); }
  .wa-empty-inner { text-align: center; }
  .wa-active { flex: 1; display: flex; flex-direction: column; min-height: 0; }

  /* Chat header */
  .wa-chat-hdr {
    flex-shrink: 0; display: flex; align-items: flex-start; gap: .85rem;
    padding: .7rem 1.25rem; border-bottom: 1px solid var(--border);
    background: rgba(0,0,0,.2); min-height: 62px;
  }
  .wa-chat-hdr-avatar {
    width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: .88rem; font-weight: 700; font-family: 'Space Grotesk', sans-serif;
  }
  .wa-chat-hdr-info { flex: 1; min-width: 0; padding-top: .1rem; }
  .wa-chat-hdr-name { font-size: .92rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .wa-chat-hdr-sub  { font-size: .65rem; color: var(--txt2); margin-top: 2px; font-family: monospace; }
  .wa-chat-hdr-actions { display: flex; gap: .45rem; align-items: center; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; padding-top: .1rem; }

  /* Tags inline en el chat */
  .wa-tags-bar { display: flex; flex-wrap: wrap; align-items: center; gap: .28rem; margin-top: .4rem; }
  .wa-tag-chip {
    display: inline-flex; align-items: center; gap: .22rem;
    border-radius: 10px; padding: .15rem .5rem;
    font-size: .62rem; font-weight: 600; cursor: default;
    transition: opacity .15s;
  }
  .wa-tag-chip .wa-tag-x {
    cursor: pointer; opacity: .6; font-size: .65rem; line-height: 1;
    padding: 0 .05rem; transition: opacity .15s;
  }
  .wa-tag-chip .wa-tag-x:hover { opacity: 1; }
  .wa-tag-add-btn {
    display: inline-flex; align-items: center; gap: .2rem;
    background: rgba(255,255,255,.05); border: 1px dashed rgba(255,255,255,.18);
    border-radius: 10px; padding: .15rem .5rem; font-size: .62rem;
    color: var(--txt3); cursor: pointer; transition: all .15s; white-space: nowrap;
  }
  .wa-tag-add-btn:hover { border-color: var(--neon); color: var(--neon); background: rgba(0,212,255,.07); }
  /* Popover de predefinidos */
  .wa-tags-popover {
    position: absolute; z-index: 200;
    background: #111; border: 1px solid var(--border);
    border-radius: 10px; padding: .65rem .75rem;
    box-shadow: 0 8px 32px rgba(0,0,0,.6);
    display: flex; flex-wrap: wrap; gap: .35rem;
    max-width: 260px; top: 100%; left: 0; margin-top: .3rem;
  }
  .wa-tag-pre {
    border-radius: 10px; padding: .18rem .55rem;
    font-size: .63rem; font-weight: 600; cursor: pointer;
    transition: all .15s; white-space: nowrap;
  }
  .wa-tag-pre.on  { opacity: 1; transform: scale(1.04); }
  .wa-tag-pre.off { opacity: .5; }
  .wa-tag-pre:hover { opacity: 1; }

  /* Notas internas en el chat */
  .wa-notas-panel {
    flex-shrink: 0; border-top: 1px solid rgba(255,255,255,.06);
    background: rgba(0,0,0,.18); display: flex; flex-direction: column;
    max-height: 260px; transition: max-height .25s ease;
  }
  .wa-notas-panel.collapsed { max-height: 36px; overflow: hidden; }
  .wa-notas-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: .45rem .9rem; cursor: pointer; flex-shrink: 0;
    user-select: none;
  }
  .wa-notas-title {
    font-size: .62rem; font-weight: 700; color: var(--txt3);
    text-transform: uppercase; letter-spacing: .09em;
    display: flex; align-items: center; gap: .4rem;
  }
  .wa-notas-count {
    background: rgba(0,212,255,.15); color: var(--neon);
    border-radius: 8px; padding: .05rem .35rem;
    font-size: .58rem; font-weight: 700;
  }
  .wa-notas-toggle { font-size: .65rem; color: var(--txt3); transition: transform .2s; }
  .wa-notas-panel.collapsed .wa-notas-toggle { transform: rotate(-90deg); }
  .wa-notas-body {
    flex: 1; overflow-y: auto; padding: 0 .9rem .5rem;
    display: flex; flex-direction: column; gap: .45rem;
  }
  .wa-nota-item {
    background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.07);
    border-radius: 8px; padding: .5rem .65rem;
    display: flex; align-items: flex-start; gap: .5rem;
  }
  .wa-nota-texto { flex: 1; font-size: .75rem; color: var(--txt); line-height: 1.45; word-break: break-word; }
  .wa-nota-meta  { font-size: .58rem; color: var(--txt3); margin-top: .25rem; }
  .wa-nota-del   {
    flex-shrink: 0; background: none; border: none; cursor: pointer;
    color: var(--txt3); font-size: .75rem; padding: .1rem .2rem;
    opacity: .5; transition: opacity .15s; line-height: 1;
  }
  .wa-nota-del:hover { opacity: 1; color: var(--red); }
  .wa-nota-ver-todas {
    font-size: .65rem; color: var(--neon); cursor: pointer;
    text-align: center; padding: .3rem; opacity: .8;
    transition: opacity .15s;
  }
  .wa-nota-ver-todas:hover { opacity: 1; }
  .wa-notas-input-row {
    display: flex; gap: .45rem; padding: .5rem .9rem .6rem; flex-shrink: 0;
    border-top: 1px solid rgba(255,255,255,.05);
  }
  .wa-nota-input {
    flex: 1; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.1);
    border-radius: 8px; color: var(--txt); font-family: 'Space Grotesk', sans-serif;
    font-size: .75rem; padding: .42rem .7rem; outline: none; resize: none;
    transition: border-color .2s; line-height: 1.4;
  }
  .wa-nota-input:focus { border-color: rgba(0,212,255,.4); }
  .wa-nota-input::placeholder { color: var(--txt3); }
  .wa-nota-send {
    flex-shrink: 0; background: rgba(0,212,255,.12); border: 1px solid rgba(0,212,255,.3);
    color: var(--neon); border-radius: 8px; font-size: .7rem; font-weight: 700;
    padding: .42rem .8rem; cursor: pointer; transition: background .15s; white-space: nowrap;
  }
  .wa-nota-send:hover { background: rgba(0,212,255,.25); }
  .wa-nota-send:disabled { opacity: .4; cursor: default; }

  /* Messages */
  .wa-messages {
    flex: 1; overflow-y: scroll; min-height: 0;
    padding: 1.1rem 1.25rem; display: flex; flex-direction: column; gap: .38rem;
    -webkit-overflow-scrolling: touch; overscroll-behavior: contain;
    background: radial-gradient(ellipse 80% 50% at 50% 100%, rgba(0,212,255,.03) 0%, transparent 70%), rgba(0,0,0,.1);
  }
  .wa-messages::-webkit-scrollbar { width: 6px; }
  .wa-messages::-webkit-scrollbar-track { background: #1a1a2e; }
  .wa-messages::-webkit-scrollbar-thumb { background: #00d4ff; border-radius: 3px; }
  .wa-messages { scrollbar-width: thin; scrollbar-color: #00d4ff #1a1a2e; }

  /* Bubbles */
  .wa-bubble-wrap { display: flex; flex-direction: column; max-width: 74%; }
  .wa-bubble-wrap.user      { align-self: flex-start; }
  .wa-bubble-wrap.assistant { align-self: flex-end; }
  .wa-bubble {
    padding: .55rem .95rem; border-radius: 12px;
    font-size: .84rem; line-height: 1.48; word-break: break-word; white-space: pre-wrap;
  }
  .wa-bubble.user {
    background: rgba(255,255,255,.08); border: 1px solid rgba(255,255,255,.12);
    border-bottom-left-radius: 3px; color: var(--txt);
  }
  .wa-bubble.assistant {
    background: rgba(0,212,255,.13); border: 1px solid rgba(0,212,255,.25);
    border-bottom-right-radius: 3px; color: var(--txt);
  }
  .wa-bubble.assistant.error { opacity: .5; border-color: rgba(255,34,51,.4); background: rgba(255,34,51,.07); }
  /* Burbujas del operador humano (Yo) — verde */
  .wa-bubble-wrap.owner { align-self: flex-end; }
  .wa-bubble.owner {
    background: rgba(34,197,94,.14); border: 1px solid rgba(34,197,94,.28);
    border-bottom-right-radius: 3px; color: var(--txt);
  }
  /* Etiqueta de remitente encima de la burbuja */
  .wa-bubble-sender {
    font-size: .58rem; font-weight: 700; letter-spacing: .04em;
    margin-bottom: .18rem; opacity: .75;
  }
  .wa-bubble-wrap.user     .wa-bubble-sender { color: #9ca3af; text-align: left; }
  .wa-bubble-wrap.assistant .wa-bubble-sender { color: rgba(0,212,255,.85); text-align: right; }
  .wa-bubble-wrap.owner    .wa-bubble-sender { color: rgba(34,197,94,.85); text-align: right; }
  .wa-bubble-time { font-size: .58rem; color: var(--txt3); margin-top: .15rem; font-family: monospace; }
  .wa-bubble-wrap.user .wa-bubble-time      { text-align: left; }
  .wa-bubble-wrap.assistant .wa-bubble-time { text-align: right; }
  .wa-bubble-wrap.owner .wa-bubble-time     { text-align: right; }

  /* Date separator */
  .wa-date-sep {
    align-self: center; font-size: .65rem; color: var(--txt3);
    background: rgba(255,255,255,.06); border: 1px solid var(--border);
    border-radius: 20px; padding: .22rem .85rem; margin: .4rem 0; letter-spacing: .04em;
  }

  /* Input row */
  .wa-input-row {
    flex-shrink: 0; display: flex; flex-direction: column;
    padding: .65rem 1rem; border-top: 1px solid var(--border);
    gap: .3rem; background: rgba(0,0,0,.2);
  }
  .wa-input-main { display: flex; gap: .6rem; align-items: flex-end; }
  .wa-human-hint { font-size: .65rem; color: #c084fc; letter-spacing: .04em; display: none; }
  .wa-input {
    flex: 1; background: rgba(255,255,255,.05);
    border: 1px solid var(--border); border-radius: 22px;
    color: var(--txt); padding: .58rem 1.1rem;
    font-family: 'Space Grotesk', sans-serif; font-size: .84rem;
    resize: none; outline: none; min-height: 40px; max-height: 120px;
    overflow-y: auto; transition: border-color .2s; -webkit-overflow-scrolling: touch;
    line-height: 1.45;
  }
  .wa-input:focus { border-color: var(--neon); box-shadow: 0 0 8px var(--neon-dim); }
  .wa-input:disabled { opacity: .35; cursor: default; }
  .wa-input.modo-humano { border-color: rgba(168,85,247,.5); }
  .wa-input.modo-humano:focus { border-color: rgba(168,85,247,.8); box-shadow: 0 0 8px rgba(168,85,247,.3); }

  .wa-send-btn {
    width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0;
    border: 1px solid rgba(0,212,255,.35); cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    background: rgba(0,212,255,.15); color: var(--neon);
    transition: background .2s, box-shadow .2s, transform .1s;
  }
  .wa-send-btn:hover:not(:disabled) { background: rgba(0,212,255,.28); box-shadow: 0 0 14px rgba(0,212,255,.35); transform: scale(1.06); }
  .wa-send-btn:disabled { opacity: .35; cursor: default; }
  .wa-send-btn.modo-humano { background: rgba(168,85,247,.15); color: #c084fc; border-color: rgba(168,85,247,.4); }
  .wa-send-btn.modo-humano:hover:not(:disabled) { background: rgba(168,85,247,.28); box-shadow: 0 0 14px rgba(168,85,247,.3); }

  /* ── Banner MODO MANUAL (rojo, visible en el chat) ── */
  .wa-manual-banner {
    display: none; align-items: center; justify-content: center; gap: .6rem;
    background: rgba(239,68,68,.18); border-bottom: 2px solid rgba(239,68,68,.6);
    padding: .55rem 1.25rem; flex-shrink: 0;
    animation: pulseRed 2s ease-in-out infinite;
  }
  .wa-manual-banner.visible { display: flex; }
  .wa-manual-banner-text {
    font-family: 'Orbitron', sans-serif; font-size: .7rem; font-weight: 700;
    color: #f87171; letter-spacing: .12em; text-transform: uppercase;
  }
  .wa-manual-banner-sub {
    font-size: .65rem; color: rgba(248,113,113,.75); letter-spacing: .04em;
  }
  @keyframes pulseRed {
    0%, 100% { background: rgba(239,68,68,.18); }
    50%       { background: rgba(239,68,68,.28); }
  }

  /* ── Botón toggle Tomar Lead / Liberar IA ── */
  .btn-toggle-lead {
    display: flex; align-items: center; gap: .4rem;
    padding: .55rem 1.2rem; border-radius: 22px; border: none; cursor: pointer;
    font-family: 'Space Grotesk', sans-serif; font-size: .78rem; font-weight: 700;
    letter-spacing: .05em; text-transform: uppercase; white-space: nowrap;
    transition: background .2s, box-shadow .2s, transform .1s;
    background: rgba(0,212,255,.15); color: var(--neon);
    border: 1.5px solid rgba(0,212,255,.5);
    box-shadow: 0 0 12px rgba(0,212,255,.2);
  }
  .btn-toggle-lead:hover:not(:disabled) {
    background: rgba(0,212,255,.28); box-shadow: 0 0 20px rgba(0,212,255,.4);
    transform: translateY(-1px);
  }
  .btn-toggle-lead.activo {
    background: rgba(239,68,68,.2); color: #f87171;
    border-color: rgba(239,68,68,.6);
    box-shadow: 0 0 14px rgba(239,68,68,.3);
    animation: pulseRed 2s ease-in-out infinite;
  }
  .btn-toggle-lead.activo:hover:not(:disabled) {
    background: rgba(239,68,68,.32); box-shadow: 0 0 24px rgba(239,68,68,.45);
  }
  .btn-toggle-lead:disabled { opacity: .5; cursor: default; transform: none; animation: none; }
  .btn-toggle-icon { font-size: .88rem; line-height: 1; }

  /* Mobile WA */
  @media(max-width:640px) {
    .wa-sidebar { position: absolute; left: 0; top: 0; bottom: 0; z-index: 10; transform: translateX(0); transition: transform .25s; width: 100%; }
    .wa-chat-panel { width: 100%; }
    .wa-layout.chat-abierto .wa-sidebar { transform: translateX(-100%); }
    #btn-wa-back { display: flex !important; }
    .wa-input-row {
      padding-bottom: max(.65rem, env(safe-area-inset-bottom));
    }
    .wa-chat-hdr-actions { gap: .3rem; }
    .btn-tomar, .btn-liberar { font-size: .56rem; padding: .22rem .6rem; }
  }
  #btn-wa-back { display: none; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 50%; border: 1px solid var(--border); background: transparent; color: var(--txt2); cursor: pointer; font-size: 1.1rem; flex-shrink: 0; }
  #btn-wa-back:hover { background: rgba(255,255,255,.06); color: var(--txt); }

  /* ── Panel Sin Respuesta ── */
  #panel-sin-respuesta { display: none; }
  .sr-toolbar {
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: .75rem;
    padding: 1.4rem 0 .9rem;
  }
  .sr-toolbar-left { display: flex; align-items: center; gap: .75rem; }
  .sr-title {
    font-family: 'Orbitron', sans-serif; font-size: .78rem; font-weight: 700;
    color: var(--neon); letter-spacing: .12em; text-transform: uppercase;
  }
  .sr-count-badge {
    background: rgba(239,68,68,.18); border: 1px solid rgba(239,68,68,.4);
    color: #f87171; border-radius: 14px; padding: .18rem .65rem;
    font-size: .68rem; font-weight: 700; font-family: 'Space Grotesk', sans-serif;
  }
  .sr-export-btn {
    display: flex; align-items: center; gap: .35rem;
    padding: .42rem 1rem; border-radius: 18px;
    border: 1px solid rgba(0,212,255,.35); background: rgba(0,212,255,.08);
    color: var(--neon); font-size: .72rem; font-weight: 600;
    cursor: pointer; transition: all .2s; white-space: nowrap;
    font-family: 'Space Grotesk', sans-serif; letter-spacing: .04em;
    text-decoration: none;
  }
  .sr-export-btn:hover { background: rgba(0,212,255,.18); box-shadow: 0 0 12px rgba(0,212,255,.25); }

  .sr-table-wrap {
    overflow-x: auto;
    overflow-y: auto;
    max-height: 70vh; border-radius: 12px;
    border: 1px solid var(--border);
    background: rgba(0,0,0,.18);
    -webkit-overflow-scrolling: touch;
  }
  .sr-table {
    width: 100%; border-collapse: collapse;
    font-family: 'Space Grotesk', sans-serif;
  }
  .sr-table th {
    padding: .65rem 1rem; text-align: left;
    font-size: .62rem; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; color: var(--txt3);
    border-bottom: 1px solid var(--border);
    background: rgba(0,0,0,.2); white-space: nowrap;
  }
  .sr-table td {
    padding: .72rem 1rem; font-size: .82rem; color: var(--txt);
    border-bottom: 1px solid rgba(255,255,255,.04); vertical-align: middle;
  }
  .sr-table tr:last-child td { border-bottom: none; }
  .sr-table tr:hover td { background: rgba(0,212,255,.04); }
  .sr-days {
    display: inline-flex; align-items: center; gap: .3rem;
    font-weight: 700; font-family: 'Orbitron', monospace; font-size: .78rem;
  }
  .sr-days.urgent { color: #f87171; }
  .sr-days.warn   { color: #fbbf24; }
  .sr-days.ok     { color: #4ade80; }
  .sr-phone { font-family: monospace; font-size: .78rem; color: var(--txt2); }
  .sr-promo {
    display: inline-block; background: rgba(168,85,247,.12);
    border: 1px solid rgba(168,85,247,.3); color: #c084fc;
    border-radius: 10px; padding: .1rem .5rem; font-size: .7rem; font-weight: 600;
  }
  .sr-promo.empty { background: none; border: none; color: var(--txt3); }
  .sr-actions { display: flex; gap: .4rem; align-items: center; flex-wrap: wrap; }
  .sr-btn {
    padding: .28rem .7rem; border-radius: 14px; border: none;
    font-size: .68rem; font-weight: 700; cursor: pointer; white-space: nowrap;
    font-family: 'Space Grotesk', sans-serif; letter-spacing: .03em;
    transition: all .18s;
  }
  .sr-btn.reactivar    { background: rgba(0,212,255,.14); color: var(--neon); border: 1px solid rgba(0,212,255,.35); }
  .sr-btn.reactivar:hover { background: rgba(0,212,255,.26); box-shadow: 0 0 10px rgba(0,212,255,.25); }
  .sr-btn.incontactable { background: rgba(239,68,68,.1); color: #f87171; border: 1px solid rgba(239,68,68,.3); }
  .sr-btn.incontactable:hover { background: rgba(239,68,68,.22); }
  .sr-btn.ver-chat     { background: rgba(255,255,255,.06); color: var(--txt2); border: 1px solid rgba(255,255,255,.12); }
  .sr-btn.ver-chat:hover { background: rgba(255,255,255,.12); color: var(--txt); }
  .sr-btn:disabled { opacity: .45; cursor: default; }
  .sr-empty {
    text-align: center; padding: 3.5rem 1rem;
    color: var(--txt3); font-size: .88rem;
  }
  .sr-loading { text-align: center; padding: 3rem 1rem; color: var(--txt3); }

  /* ── Panel campañas ── */
  #panel-campanas { display: none; }
  .cpn-toolbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; flex-wrap: wrap;
    margin: 1.5rem 0 .75rem;
  }
  .cpn-toolbar-title {
    font-family: 'Orbitron', sans-serif; font-size: .8rem; font-weight: 700;
    color: var(--neon); letter-spacing: .12em; text-transform: uppercase;
  }
  .cpn-new-btn {
    background: rgba(0,212,255,.1); border: 1px solid rgba(0,212,255,.4);
    color: var(--neon); font-family: 'Space Grotesk', sans-serif;
    font-size: .72rem; font-weight: 700; letter-spacing: .06em;
    padding: .45rem 1.1rem; border-radius: 10px; cursor: pointer;
    transition: background .15s, box-shadow .2s;
  }
  .cpn-new-btn:hover { background: rgba(0,212,255,.2); box-shadow: 0 0 14px rgba(0,212,255,.25); }

  .cpn-list { display: flex; flex-direction: column; gap: .55rem; }
  .cpn-row {
    display: grid; grid-template-columns: auto 1fr auto auto auto;
    align-items: center; gap: .85rem;
    background: rgba(255,255,255,.02); border: 1px solid rgba(255,255,255,.06);
    border-radius: 12px; padding: .8rem 1.1rem;
    cursor: pointer; transition: background .18s, border-color .2s;
    border-left: 3px solid transparent;
  }
  .cpn-row:hover { background: rgba(0,212,255,.04); border-color: rgba(0,212,255,.18); }
  .cpn-row-estado {
    width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
  }
  .cpn-row-info { min-width: 0; }
  .cpn-row-nombre { font-size: .82rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cpn-row-meta   { font-size: .65rem; color: var(--txt3); margin-top: 2px; }
  .cpn-estado-badge {
    font-size: .6rem; font-weight: 700; letter-spacing: .06em;
    padding: .2rem .6rem; border-radius: 20px; white-space: nowrap;
    text-transform: uppercase;
  }
  .cpn-metrics { text-align: right; min-width: 80px; }
  .cpn-metrics-num { font-size: .8rem; font-weight: 700; font-family: 'Orbitron', sans-serif; color: var(--neon); }
  .cpn-metrics-sub { font-size: .6rem; color: var(--txt3); }
  .cpn-action-btn {
    background: none; border: 1px solid rgba(255,255,255,.1); color: var(--txt2);
    font-size: .62rem; font-weight: 700; letter-spacing: .05em;
    padding: .3rem .7rem; border-radius: 8px; cursor: pointer;
    transition: all .15s; white-space: nowrap;
    font-family: 'Space Grotesk', sans-serif; text-transform: uppercase;
  }
  .cpn-action-btn:hover { background: rgba(255,255,255,.08); color: var(--txt); }
  .cpn-action-btn.enviar { border-color: rgba(0,255,136,.35); color: var(--green); }
  .cpn-action-btn.enviar:hover { background: rgba(0,255,136,.1); box-shadow: 0 0 8px rgba(0,255,136,.2); }

  /* Progress bar en fila de campaña */
  .cpn-progress { height: 3px; border-radius: 2px; background: rgba(255,255,255,.08); margin-top: 5px; overflow: hidden; }
  .cpn-progress-fill { height: 100%; border-radius: 2px; transition: width .6s ease; }

  /* Modal nueva campaña */
  .ncpn-section { margin-bottom: 1.1rem; }
  .ncpn-label {
    font-size: .63rem; font-weight: 700; letter-spacing: .08em; color: var(--txt3);
    text-transform: uppercase; margin-bottom: .4rem;
  }
  .ncpn-input, .ncpn-select, .ncpn-textarea {
    width: 100%; background: rgba(255,255,255,.04); border: 1px solid var(--border);
    border-radius: 10px; color: var(--txt); padding: .55rem .85rem;
    font-family: 'Space Grotesk', sans-serif; font-size: .8rem; outline: none;
    transition: border-color .2s; box-sizing: border-box;
  }
  .ncpn-textarea { min-height: 110px; resize: vertical; line-height: 1.5; }
  .ncpn-input:focus, .ncpn-select:focus, .ncpn-textarea:focus { border-color: rgba(0,212,255,.5); }
  .ncpn-select option { background: #111; }
  .ncpn-filtros-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: .6rem;
  }
  .ncpn-preview-box {
    background: rgba(0,212,255,.05); border: 1px solid rgba(0,212,255,.2);
    border-radius: 10px; padding: .75rem 1rem; min-height: 60px;
  }
  .ncpn-preview-num {
    font-family: 'Orbitron', sans-serif; font-size: 1.6rem; font-weight: 700;
    color: var(--neon); line-height: 1.1;
  }
  .ncpn-preview-sub { font-size: .68rem; color: var(--txt3); margin-top: 3px; }
  .ncpn-preview-list { margin-top: .6rem; display: flex; flex-direction: column; gap: .25rem; }
  .ncpn-preview-item { font-size: .68rem; color: var(--txt2); display: flex; gap: .5rem; }
  .ncpn-hint {
    font-size: .65rem; color: var(--txt3); margin-top: .35rem; line-height: 1.4;
  }

  /* Modal detalle campaña */
  .cpnd-header-grid {
    display: grid; grid-template-columns: 1fr 1fr 1fr 1fr;
    gap: .75rem; margin-bottom: 1.1rem;
  }
  .cpnd-stat { background: rgba(255,255,255,.03); border-radius: 10px; padding: .65rem .85rem; }
  .cpnd-stat-label { font-size: .6rem; color: var(--txt3); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
  .cpnd-stat-val   { font-size: 1.1rem; font-weight: 700; font-family: 'Orbitron', sans-serif; margin-top: 3px; }
  .cpnd-dest-row {
    display: grid; grid-template-columns: auto 1fr auto;
    align-items: center; gap: .6rem;
    padding: .45rem .65rem; border-radius: 8px; font-size: .72rem;
    background: rgba(255,255,255,.02);
  }
  .cpnd-dest-rows { display: flex; flex-direction: column; gap: .3rem; max-height: 50vh; overflow-y: auto; }
  .cpnd-envio-ok  { color: var(--green); font-size: .65rem; font-weight: 700; }
  .cpnd-envio-err { color: var(--red);   font-size: .65rem; font-weight: 700; }
  .cpnd-envio-pen { color: var(--txt3);  font-size: .65rem; }

  /* ── Botones rápidos en sidebar ── */
  .wa-quick-tomar, .wa-quick-liberar {
    width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: .65rem; cursor: pointer; transition: all .15s;
    line-height: 1;
  }
  .wa-quick-tomar {
    background: rgba(0,212,255,.1); border: 1px solid rgba(0,212,255,.3); color: var(--neon);
  }
  .wa-quick-tomar:hover { background: rgba(0,212,255,.25); box-shadow: 0 0 8px rgba(0,212,255,.3); }
  .wa-quick-liberar {
    background: rgba(168,85,247,.12); border: 1px solid rgba(168,85,247,.35); color: #c084fc;
  }
  .wa-quick-liberar:hover { background: rgba(168,85,247,.25); box-shadow: 0 0 8px rgba(168,85,247,.3); }
</style>
</head>
<body>

<header style="position:relative">
  <!-- Fila 1 (en móvil): marca + sistema -->
  <div class="logo">
    <!-- Ícono con inicial -->
    <div class="logo-icon">C</div>
    <!-- Nombre + slogan -->
    <div class="logo-text-wrap">
      <div class="logo-name">
        CONE<span class="logo-x">X</span>I&Oacute;N SIN L&Iacute;MITES
      </div>
      <div class="logo-slogan">Tu se&ntilde;al. Tu mundo.</div>
    </div>
    <!-- Indicador de sistema — visible solo en móvil dentro del logo row -->
    <div class="sys-status" id="sys-status-mobile" style="display:none">
      <div class="sys-dot"></div>
      <span>Activo</span>
    </div>
  </div>

  <!-- Fila 2 (en móvil): nav + live -->
  <nav class="tab-nav">
    <button class="tab-btn active" id="tab-metricas"    onclick="switchTab('metricas')">&#128200; M&eacute;tricas</button>
    <button class="tab-btn"        id="tab-sin-resp"    onclick="switchTab('sin-respuesta')">&#9200; Sin Respuesta <span id="tab-sr-count" style="display:none;background:rgba(239,68,68,.25);color:#f87171;border-radius:10px;padding:.05rem .42rem;font-size:.6rem;margin-left:.25rem;font-family:'Space Grotesk',sans-serif">0</span></button>
    <button class="tab-btn"        id="tab-campanas"    onclick="switchTab('campanas')">&#128226; Campa&ntilde;as</button>
  </nav>
  <button class="btn-live" id="btn-live" onclick="toggleLive()">&#128172; Live</button>

  <!-- Desktop derecha: sistema + EN VIVO + timestamp + salir -->
  <div class="header-right">
    <div class="sys-status" id="sys-status-desktop">
      <div class="sys-dot"></div>
      <span>Sistema activo</span>
    </div>
    <div class="live-badge"><div class="live-dot"></div>EN VIVO</div>
    <div id="last-update">Iniciando...</div>
    <a href="/logout" style="color:var(--txt2);font-size:.65rem;font-weight:600;letter-spacing:.06em;text-decoration:none;text-transform:uppercase;padding:.3rem .7rem;border:1px solid var(--border);border-radius:14px;transition:color .2s" onmouseover="this.style.color='var(--red)'" onmouseout="this.style.color='var(--txt2)'">&#x2715; Salir</a>
  </div>
  <div class="scan-line"></div>
</header>

<div id="panel-metrics">
<main>

  <!-- KPIs -->
  <div class="section-label" style="margin-top:1.5rem">Resumen del sistema</div>
  <div class="kpi-grid">
    <div class="kpi-card primary" onclick="abrirKpiModal('total')">
      <span class="kpi-hint">ver detalle ›</span>
      <div class="kpi-label">Total Leads</div>
      <div class="kpi-value neon" id="k-total">0</div>
      <div class="kpi-sub">registros activos</div>
    </div>
    <div class="kpi-card" onclick="abrirKpiModal('calientes')">
      <span class="kpi-hint">ver detalle ›</span>
      <div class="kpi-label">Leads Calientes</div>
      <div class="kpi-value red" id="k-hot">0</div>
      <div class="kpi-sub">caliente + listo cierre</div>
    </div>
    <div class="kpi-card" onclick="abrirKpiModal('cerrados')">
      <span class="kpi-hint">ver detalle ›</span>
      <div class="kpi-label">Conversiones</div>
      <div class="kpi-value green" id="k-closed">0</div>
      <div class="kpi-sub">leads cerrados</div>
    </div>
    <div class="kpi-card" onclick="abrirKpiModal('score')">
      <span class="kpi-hint">ver detalle ›</span>
      <div class="kpi-label">Score Promedio</div>
      <div class="kpi-value white" id="k-score">0</div>
      <div class="kpi-sub">sobre 100 pts</div>
    </div>
    <div class="kpi-card" onclick="abrirKpiModal('msgs')">
      <span class="kpi-hint">ver detalle ›</span>
      <div class="kpi-label">Mensajes Hoy</div>
      <div class="kpi-value white" id="k-msgs">0</div>
      <div class="kpi-sub">en historial CRM</div>
    </div>
    <div class="kpi-card" onclick="abrirKpiModal('followups')">
      <span class="kpi-hint">ver detalle ›</span>
      <div class="kpi-label">Follow-ups</div>
      <div class="kpi-value neon" id="k-followups">0</div>
      <div class="kpi-sub">pendientes de envio</div>
    </div>
  </div>

  <!-- Embudo de conversión -->
  <div class="section-label">Embudo de conversi&oacute;n</div>
  <div class="funnel-grid" id="funnel-grid">
    <div class="funnel-card">
      <div class="funnel-label">Contactado <span class="funnel-arrow">→</span> Interesado</div>
      <div class="funnel-pct" id="f-pct-0" style="color:var(--neon)">—</div>
      <div class="funnel-bar-track"><div class="funnel-bar-fill" id="f-bar-0" style="background:var(--neon)"></div></div>
      <div class="funnel-counts" id="f-cnt-0">sin datos</div>
    </div>
    <div class="funnel-card">
      <div class="funnel-label">Interesado <span class="funnel-arrow">→</span> Caliente</div>
      <div class="funnel-pct" id="f-pct-1" style="color:var(--orange)">—</div>
      <div class="funnel-bar-track"><div class="funnel-bar-fill" id="f-bar-1" style="background:var(--orange)"></div></div>
      <div class="funnel-counts" id="f-cnt-1">sin datos</div>
    </div>
    <div class="funnel-card">
      <div class="funnel-label">Caliente <span class="funnel-arrow">→</span> Cierre</div>
      <div class="funnel-pct" id="f-pct-2" style="color:var(--green)">—</div>
      <div class="funnel-bar-track"><div class="funnel-bar-fill" id="f-bar-2" style="background:var(--green)"></div></div>
      <div class="funnel-counts" id="f-cnt-2">sin datos</div>
    </div>
  </div>

  <!-- Chart + Leads recientes -->
  <div class="section-label">An&aacute;lisis de pipeline</div>
  <div class="main-grid">

    <div class="card">
      <div class="card-title">Leads por estado</div>
      <div class="chart-wrap">
        <canvas id="chart-estados"></canvas>
      </div>
    </div>

    <div class="card" style="overflow:visible">
      <div class="card-title" style="display:flex;align-items:center;justify-content:space-between;gap:.75rem">
        <span>Leads <span id="leads-count-badge" style="font-size:.6rem;color:var(--txt3);font-weight:400;margin-left:.3rem"></span></span>
        <button onclick="abrirExportModal()" id="btn-export-csv" style="background:rgba(0,212,255,.07);border:1px solid rgba(0,212,255,.3);color:var(--neon);font-family:'Space Grotesk',sans-serif;font-size:.62rem;font-weight:700;letter-spacing:.06em;padding:.3rem .7rem;border-radius:8px;cursor:pointer;transition:background .15s;white-space:nowrap" onmouseover="this.style.background='rgba(0,212,255,.18)'" onmouseout="this.style.background='rgba(0,212,255,.07)'">&#8659; Exportar CSV</button>
      </div>
      <!-- Filtros + búsqueda -->
      <div class="filter-bar">
        <input  type="text"   id="f-buscar"  class="filter-input"  placeholder="&#128269; Buscar nombre o teléfono..." oninput="filtrarLeads()">
        <select id="f-estado" class="filter-select" onchange="filtrarLeads()">
          <option value="">Todos los estados</option>
          <option value="nuevo">Nuevo</option>
          <option value="contactado">Contactado</option>
          <option value="interesado">Interesado</option>
          <option value="tibio">Tibio</option>
          <option value="caliente">Caliente</option>
          <option value="direccion_obtenida">Dirección obtenida</option>
          <option value="listo_para_cierre">Listo cierre</option>
          <option value="cerrado">Cerrado</option>
          <option value="seguimiento">Seguimiento</option>
        </select>
        <select id="f-tag" class="filter-select" onchange="filtrarLeads()">
          <option value="">Todos los tags</option>
        </select>
        <input  type="number" id="f-score" class="filter-score" placeholder="Score ≥" min="0" max="100" oninput="filtrarLeads()">
        <input  type="date"   id="f-desde" class="filter-date"  onchange="filtrarLeads()" title="Desde (primer contacto)">
        <input  type="date"   id="f-hasta" class="filter-date"  onchange="filtrarLeads()" title="Hasta (primer contacto)">
        <button class="filter-clear" onclick="limpiarFiltros()">&#10005; Limpiar</button>
        <span class="filter-count" id="f-count"></span>
      </div>
      <div class="leads-list" id="leads-list">
        <div class="empty">Sin datos</div>
      </div>
    </div>

  </div>

  <!-- Mapa de calor por comuna -->
  <div class="section-label">Mapa de calor por comuna</div>
  <div class="card" style="margin-bottom:2rem">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;flex-wrap:wrap;gap:.5rem">
      <div class="card-title" style="margin-bottom:0">Leads por comuna <span id="hm-total-label" style="font-size:.65rem;font-weight:400;color:var(--txt3);margin-left:.4rem"></span></div>
      <div style="font-size:.65rem;color:var(--txt3)">
        <span style="color:var(--red)">&#9679;</span> Top 3 &nbsp;
        <span style="color:#FFAA00">&#9679;</span> Alto &nbsp;
        <span style="color:var(--txt3)">&#9679;</span> Bajo
      </div>
    </div>
    <input class="heatmap-search" id="hm-search" type="text" placeholder="&#128269; Buscar comuna..." oninput="hmFiltrar()" autocomplete="off">
    <div style="overflow-x:auto">
      <table class="heatmap-table">
        <thead>
          <tr>
            <th style="width:1.5rem">#</th>
            <th></th>
            <th>Comuna</th>
            <th>Leads</th>
            <th>Calientes</th>
            <th>Score prom.</th>
            <th style="min-width:100px">Distribuci&oacute;n</th>
          </tr>
        </thead>
        <tbody id="hm-tbody">
          <tr><td colspan="7" class="empty" style="padding:1.5rem;text-align:center">Cargando...</td></tr>
        </tbody>
      </table>
    </div>
    <div class="hm-pagination">
      <span id="hm-pag-info"></span>
      <div style="display:flex;gap:.4rem">
        <button id="hm-prev" onclick="hmPaginar(-1)" disabled>&#8592; Ant</button>
        <button id="hm-next" onclick="hmPaginar(1)">Sig &#8594;</button>
      </div>
    </div>
  </div>

  <!-- Actividad reciente -->
  <div class="section-label">Actividad reciente</div>
  <div class="card" style="margin-bottom:2rem">
    <div class="card-title">&#218;ltimos mensajes por contacto</div>
    <div class="msg-card-list" id="msgs-list">
      <div class="empty">Sin datos</div>
    </div>
  </div>

  <!-- Estadísticas de campañas -->
  <div class="section-label" style="margin-top:1rem">Estad&iacute;sticas de campa&ntilde;as</div>
  <div class="campanas-grid">

    <!-- Embudo de estados -->
    <div class="card">
      <div class="card-title">Distribuci&oacute;n de estados</div>
      <div id="campanas-embudo"><div class="empty">Cargando...</div></div>
    </div>

    <!-- Tasa de respuesta follow-ups -->
    <div class="card">
      <div class="card-title">Tasa de respuesta — follow-ups</div>
      <div id="campanas-followups"><div class="empty">Cargando...</div></div>
    </div>

    <!-- Leads por día -->
    <div class="card">
      <div class="card-title">Leads por d&iacute;a <span style="font-size:.58rem;color:var(--txt3);font-weight:400">(&#250;ltimas 2 semanas)</span></div>
      <div class="campanas-chart-wrap">
        <canvas id="chart-leads-dia"></canvas>
      </div>
    </div>

    <!-- Top productos -->
    <div class="card">
      <div class="card-title">Top productos de inter&eacute;s</div>
      <div class="campanas-chart-wrap">
        <canvas id="chart-productos"></canvas>
      </div>
    </div>

  </div>

</main>
</div><!-- /panel-metrics -->

<!-- ── Panel Sin Respuesta ───────────────────────────────────────────────────── -->
<div id="panel-sin-respuesta">
<main>
  <div class="sr-toolbar">
    <div class="sr-toolbar-left">
      <span class="sr-title">&#9200; Sin Respuesta</span>
      <span class="sr-count-badge" id="sr-total-badge">0 leads</span>
    </div>
    <a class="sr-export-btn" href="/api/sin-respuesta/export.csv" download>
      &#8681; Exportar CSV
    </a>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <div class="sr-table-wrap">
      <table class="sr-table">
        <thead>
          <tr>
            <th>Nombre</th>
            <th>Tel&eacute;fono</th>
            <th>Ciudad</th>
            <th>Fecha env&iacute;o</th>
            <th>Sin respuesta</th>
            <th>Promoci&oacute;n</th>
            <th>Acci&oacute;n</th>
          </tr>
        </thead>
        <tbody id="sr-tbody">
          <tr><td colspan="7" class="sr-loading">Cargando...</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</main>
</div><!-- /panel-sin-respuesta -->

<!-- ── Panel Live Chat — WhatsApp Web ───────────────────────────────────────── -->
<div id="panel-chat">
  <div class="wa-layout" id="wa-layout">

    <!-- Sidebar izquierdo -->
    <div class="wa-sidebar">
      <div class="wa-sidebar-hdr">
        <span>Conversaciones</span>
        <span id="wa-conv-count" style="font-family:'Space Grotesk',sans-serif;font-size:.7rem;color:var(--txt3);font-weight:500;letter-spacing:.04em">—</span>
      </div>
      <div class="wa-search-wrap">
        <input type="text" id="wa-search" class="wa-search-input" placeholder="&#128269; Buscar contacto..." oninput="filtrarContactos(this.value)">
      </div>
      <!-- Filtros de estado -->
      <div class="wa-status-filter-wrap">
        <button class="wa-sf-btn active" id="sf-todos"       onclick="filtrarPorEstado('')">Todos</button>
        <button class="wa-sf-btn"        id="sf-respondio"   onclick="filtrarPorEstado('respondio')">Respondieron</button>
        <button class="wa-sf-btn"        id="sf-sin-resp"    onclick="filtrarPorEstado('sin-resp')">Sin resp.</button>
        <button class="wa-sf-btn manual" id="sf-manual"      onclick="filtrarPorEstado('manual')">Manual</button>
      </div>
      <div class="wa-tag-filter-wrap">
        <select id="wa-tag-filter" class="wa-tag-filter-select" onchange="filtrarPorTag(this.value)">
          <option value="">&#127991; Todos los tags</option>
          <option value="Interesado">Interesado</option>
          <option value="Sin cobertura">Sin cobertura</option>
          <option value="Tiene contrato">Tiene contrato</option>
          <option value="Precio alto">Precio alto</option>
          <option value="Llamar despu&#233;s">Llamar despu&#233;s</option>
          <option value="No contesta">No contesta</option>
          <option value="Cerrado">Cerrado</option>
        </select>
      </div>
      <div class="wa-conv-list" id="wa-conv-list">
        <div class="empty" style="padding:2.5rem 1rem;text-align:center">Cargando...</div>
      </div>
    </div>

    <!-- Panel derecho -->
    <div class="wa-chat-panel">

      <!-- Estado vacío -->
      <div class="wa-empty" id="wa-empty">
        <div class="wa-empty-inner">
          <div style="font-size:4rem;opacity:.2;margin-bottom:1.25rem">&#128172;</div>
          <div style="font-size:1rem;font-weight:600;color:var(--txt2);margin-bottom:.4rem">Live Chat</div>
          <div style="font-size:.82rem;color:var(--txt3)">Selecciona una conversaci&oacute;n para empezar</div>
        </div>
      </div>

      <!-- Chat activo -->
      <div class="wa-active" id="wa-active" style="display:none">

        <!-- Header -->
        <div class="wa-chat-hdr" id="wa-chat-hdr">
          <button id="btn-wa-back" onclick="history.back()" title="Volver">&#8592;</button>
        </div>

        <!-- Banner MODO MANUAL -->
        <div class="wa-manual-banner" id="wa-manual-banner">
          <span style="font-size:1.1rem">&#128683;</span>
          <div>
            <div class="wa-manual-banner-text">MODO MANUAL &mdash; Valentina pausada</div>
            <div class="wa-manual-banner-sub">Est&aacute;s respondiendo t&uacute; directamente. Valentina no enviar&aacute; mensajes.</div>
          </div>
        </div>

        <!-- Notas internas -->
        <div class="wa-notas-panel collapsed" id="wa-notas-panel">
          <div class="wa-notas-header" onclick="toggleNotasPanel()">
            <div class="wa-notas-title">
              &#128221; Notas internas
              <span class="wa-notas-count" id="wa-notas-count" style="display:none">0</span>
            </div>
            <span class="wa-notas-toggle">&#9660;</span>
          </div>
          <div class="wa-notas-body" id="wa-notas-body"></div>
          <div class="wa-notas-input-row">
            <textarea class="wa-nota-input" id="wa-nota-input" rows="1"
              placeholder="Escribe una nota interna..."
              oninput="this.style.height='auto';this.style.height=Math.min(this.scrollHeight,80)+'px'"
              onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();guardarNotaInterna();}"></textarea>
            <button class="wa-nota-send" id="wa-nota-send-btn" onclick="guardarNotaInterna()">+ Nota</button>
          </div>
        </div>

        <!-- Burbujas -->
        <div class="wa-messages" id="wa-messages">
          <div class="empty" style="margin:auto;padding:2rem;text-align:center">Selecciona un contacto</div>
        </div>

        <!-- Input -->
        <div class="wa-input-row">
          <div class="wa-human-hint" id="wa-human-hint">&#128163; Modo humano &mdash; enviando directo a WhatsApp</div>
          <div class="wa-input-main">
            <textarea class="wa-input" id="wa-input" placeholder="Selecciona una conversaci&oacute;n para escribir..." rows="1" disabled></textarea>
            <button class="wa-send-btn" id="wa-send-btn" onclick="waSend()" disabled>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
            </button>
          </div>
        </div>

      </div><!-- /wa-active -->
    </div><!-- /wa-chat-panel -->
  </div><!-- /wa-layout -->
</div><!-- /panel-chat -->

<!-- ── Panel Campañas ──────────────────────────────────────────────────────── -->
<div id="panel-campanas">
<main>
  <div class="cpn-toolbar">
    <div class="cpn-toolbar-title">&#128226; Campa&ntilde;as de WhatsApp</div>
    <button class="cpn-new-btn" onclick="abrirNuevaCampana()">+ Nueva Campa&ntilde;a</button>
  </div>
  
<div class="card" style="margin-bottom:1rem">
  <div class="card-title">📤 Subir nueva base de datos (CSV)</div>
  <div style="display:flex;align-items:center;gap:1rem;flex-wrap:wrap;padding:.5rem 0">
    <input type="file" id="excel-input" accept=".csv,.xlsx" style="color:var(--txt);background:#111;border:1px solid #333;border-radius:8px;padding:.4rem .6rem;font-size:.78rem;flex:1;min-width:200px">
    <button id="btn-subir-excel" onclick="subirExcel()" style="padding:.5rem 1.25rem;background:#00D4FF;color:#000;border:none;border-radius:8px;font-weight:700;cursor:pointer;white-space:nowrap">📤 Subir CSV</button>
  </div>
  <div style="font-size:.7rem;color:#666;margin-top:.25rem">Formato: columnas telefono, nombre, comuna (separadas por coma)</div>
</div>
<div class="card">
    <div class="card-title" style="display:flex;align-items:center;justify-content:space-between">
      <span>Historial de campa&ntilde;as</span>
      <span id="cpn-count" style="font-size:.62rem;color:var(--txt3);font-weight:400"></span>
    </div>
    <div class="cpn-list" id="cpn-list">
      <div class="empty">Cargando...</div>
    </div>
  </div>
</main>
</div><!-- /panel-campanas -->

<!-- Modal: KPI Detail -->
<div class="modal-overlay" id="modal-kpi" onclick="if(event.target===this)cerrarKpiModal()">
  <div class="modal-box" style="max-width:640px">
    <div class="modal-header">
      <div>
        <div id="kpi-modal-title" style="font-family:'Orbitron',sans-serif;font-size:.85rem;font-weight:700;color:var(--neon);letter-spacing:.1em"></div>
        <div id="kpi-modal-sub" style="font-size:.65rem;color:var(--txt3);margin-top:.25rem"></div>
      </div>
      <button class="modal-close" onclick="cerrarKpiModal()">&#10005;&nbsp; Cerrar</button>
    </div>
    <div id="kpi-modal-body" class="modal-messages" style="max-height:65vh;gap:.5rem">
      <div class="empty">Cargando...</div>
    </div>
  </div>
</div>

<!-- Modal: Ver chat completo -->
<div class="modal-overlay" id="modal-chat" onclick="if(event.target===this)cerrarModal()">
  <div class="modal-box">
    <div class="modal-header">
      <div>
        <div id="modal-nombre" style="font-family:'Orbitron',sans-serif;font-size:.85rem;font-weight:700;color:var(--neon);letter-spacing:.1em"></div>
        <div id="modal-tel" style="font-size:.68rem;color:var(--txt2);font-family:monospace;margin-top:.3rem"></div>
      </div>
      <button class="modal-close" onclick="cerrarModal()">&#10005;&nbsp; Cerrar</button>
    </div>
    <div class="modal-messages" id="modal-messages">
      <div class="empty">Cargando...</div>
    </div>
  </div>
</div>

<!-- Modal: Notas del lead -->
<div class="modal-overlay" id="modal-notas" onclick="if(event.target===this)cerrarNotasModal()">
  <div class="modal-box" style="max-width:500px">
    <div class="modal-header">
      <div>
        <div id="notas-modal-title" style="font-family:'Orbitron',sans-serif;font-size:.85rem;font-weight:700;color:var(--neon);letter-spacing:.1em">NOTAS</div>
        <div id="notas-modal-sub" style="font-size:.65rem;color:var(--txt3);margin-top:.25rem">Notas internas — no visibles para el cliente</div>
      </div>
      <button class="modal-close" onclick="cerrarNotasModal()">&#10005;&nbsp; Cerrar</button>
    </div>
    <div style="padding:1.4rem 1.75rem;display:flex;flex-direction:column;gap:1rem">
      <textarea id="notas-textarea"
        placeholder="Escribe notas internas sobre este lead&#10;(historial, acuerdos, recordatorios...)"
        style="width:100%;min-height:160px;background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:10px;color:var(--txt1);font-family:'Space Grotesk',sans-serif;font-size:.82rem;line-height:1.5;padding:.85rem 1rem;resize:vertical;outline:none;transition:border-color .2s;box-sizing:border-box"
        onfocus="this.style.borderColor='rgba(0,212,255,.5)'"
        onblur="this.style.borderColor='var(--border)'"
      ></textarea>
      <div style="display:flex;gap:.75rem;justify-content:flex-end">
        <button onclick="cerrarNotasModal()" style="background:rgba(255,255,255,.05);border:1px solid var(--border);color:var(--txt2);font-family:'Space Grotesk',sans-serif;font-size:.75rem;padding:.5rem 1.1rem;border-radius:8px;cursor:pointer">Cancelar</button>
        <button id="notas-save-btn" onclick="guardarNotas()" style="background:rgba(0,212,255,.12);border:1px solid rgba(0,212,255,.4);color:var(--neon);font-family:'Space Grotesk',sans-serif;font-size:.75rem;font-weight:700;padding:.5rem 1.4rem;border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='rgba(0,212,255,.25)'" onmouseout="this.style.background='rgba(0,212,255,.12)'">Guardar notas</button>
      </div>
    </div>
  </div>
</div>

<!-- Modal: Tags del lead -->
<div class="modal-overlay" id="modal-tags" onclick="if(event.target===this)cerrarTagsModal()">
  <div class="modal-box" style="max-width:500px">
    <div class="modal-header">
      <div>
        <div id="tags-modal-title" style="font-family:'Orbitron',sans-serif;font-size:.85rem;font-weight:700;color:var(--neon);letter-spacing:.1em">TAGS</div>
        <div style="font-size:.65rem;color:var(--txt3);margin-top:.25rem">Haz clic para activar / desactivar</div>
      </div>
      <button class="modal-close" onclick="cerrarTagsModal()">&#10005;&nbsp; Cerrar</button>
    </div>
    <div style="padding:1.4rem 1.75rem;display:flex;flex-direction:column;gap:1rem">
      <div>
        <div style="font-size:.65rem;color:var(--txt3);font-weight:700;letter-spacing:.08em;margin-bottom:.55rem">SUGERIDOS</div>
        <div class="tags-predefined" id="tags-predefined"></div>
      </div>
      <div>
        <div style="font-size:.65rem;color:var(--txt3);font-weight:700;letter-spacing:.08em;margin-bottom:.4rem">AGREGAR TAG PERSONALIZADO</div>
        <div style="display:flex;gap:.5rem">
          <input id="tags-custom-input" type="text" maxlength="50"
            placeholder="Escribe un tag..."
            style="flex:1;background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:8px;color:var(--txt);padding:.4rem .75rem;font-family:'Space Grotesk',sans-serif;font-size:.78rem;outline:none"
            onfocus="this.style.borderColor='rgba(0,212,255,.5)'"
            onblur="this.style.borderColor='var(--border)'"
            onkeydown="if(event.key==='Enter')agregarTagPersonalizado()">
          <button onclick="agregarTagPersonalizado()" style="background:rgba(0,212,255,.1);border:1px solid rgba(0,212,255,.3);color:var(--neon);font-family:'Space Grotesk',sans-serif;font-size:.72rem;font-weight:700;padding:.4rem .9rem;border-radius:8px;cursor:pointer">+ Agregar</button>
        </div>
      </div>
      <div style="display:flex;gap:.75rem;justify-content:flex-end;border-top:1px solid rgba(255,255,255,.06);padding-top:.75rem">
        <button onclick="cerrarTagsModal()" style="background:rgba(255,255,255,.05);border:1px solid var(--border);color:var(--txt2);font-family:'Space Grotesk',sans-serif;font-size:.75rem;padding:.5rem 1.1rem;border-radius:8px;cursor:pointer">Cancelar</button>
        <button id="tags-save-btn" onclick="guardarTags()" style="background:rgba(0,212,255,.12);border:1px solid rgba(0,212,255,.4);color:var(--neon);font-family:'Space Grotesk',sans-serif;font-size:.75rem;font-weight:700;padding:.5rem 1.4rem;border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='rgba(0,212,255,.25)'" onmouseout="this.style.background='rgba(0,212,255,.12)'">Guardar tags</button>
      </div>
    </div>
  </div>
</div>

<!-- Modal: Detalle del lead -->
<div class="modal-overlay" id="modal-lead-detail" onclick="if(event.target===this)cerrarLeadDetail()">
  <div class="modal-box" style="max-width:580px">
    <div class="modal-header">
      <div>
        <div id="ld-title" style="font-family:'Orbitron',sans-serif;font-size:.85rem;font-weight:700;color:var(--neon);letter-spacing:.1em">DETALLE DEL LEAD</div>
        <div id="ld-sub" style="font-size:.65rem;color:var(--txt3);margin-top:.25rem"></div>
      </div>
      <div style="display:flex;gap:.6rem;align-items:center">
        <button id="ld-btn-chat" onclick="" style="background:rgba(0,212,255,.08);border:1px solid rgba(0,212,255,.3);color:var(--neon);font-family:'Space Grotesk',sans-serif;font-size:.68rem;font-weight:700;padding:.38rem .9rem;border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='rgba(0,212,255,.18)'" onmouseout="this.style.background='rgba(0,212,255,.08)'">&#8594; Chat</button>
        <button class="modal-close" onclick="cerrarLeadDetail()">&#10005;&nbsp; Cerrar</button>
      </div>
    </div>
    <div id="ld-body" class="modal-messages" style="max-height:72vh;gap:.75rem;padding:1.4rem 1.75rem">
      <div class="empty">Cargando...</div>
    </div>
  </div>
</div>

<!-- Modal: Nueva Campaña -->
<div class="modal-overlay" id="modal-nueva-campana" onclick="if(event.target===this)cerrarNuevaCampana()">
  <div class="modal-box" style="max-width:600px">
    <div class="modal-header">
      <div>
        <div style="font-family:'Orbitron',sans-serif;font-size:.85rem;font-weight:700;color:var(--neon);letter-spacing:.1em">NUEVA CAMPA&Ntilde;A</div>
        <div style="font-size:.65rem;color:var(--txt3);margin-top:.25rem">Crea y segmenta tu mensaje masivo de WhatsApp</div>
      </div>
      <button class="modal-close" onclick="cerrarNuevaCampana()">&#10005;&nbsp; Cerrar</button>
    </div>
    <div style="padding:1.4rem 1.75rem;display:flex;flex-direction:column;gap:.85rem;overflow-y:auto;max-height:80vh">

      <div class="ncpn-section">
        <div class="ncpn-label">Nombre de la campa&ntilde;a *</div>
        <input id="ncpn-nombre" class="ncpn-input" type="text" maxlength="120" placeholder="Ej: Promo diciembre — leads tibios">
      </div>

      <div class="ncpn-section">
        <div class="ncpn-label">Mensaje *</div>
        <textarea id="ncpn-mensaje" class="ncpn-textarea" placeholder="Hola {{nombre}}, te escribo de Conexión Sin Límites..."></textarea>
        <div class="ncpn-hint">Usa <strong>{{nombre}}</strong> para personalizar con el nombre de cada cliente.</div>
      </div>

      <div class="ncpn-section">
        <div class="ncpn-label">Segmentaci&oacute;n de destinatarios <span style="font-weight:400;text-transform:none;letter-spacing:0">(opcional — sin filtros = todos los leads)</span></div>
        <div class="ncpn-filtros-grid">
          <div>
            <div class="ncpn-label" style="margin-top:.3rem">Tag</div>
            <input id="ncpn-tag" class="ncpn-input" type="text" maxlength="50" placeholder="Ej: DirecTV">
          </div>
          <div>
            <div class="ncpn-label" style="margin-top:.3rem">Estado del lead</div>
            <select id="ncpn-estado" class="ncpn-select">
              <option value="">Todos los estados</option>
              <option value="nuevo">Nuevo</option>
              <option value="contactado">Contactado</option>
              <option value="interesado">Interesado</option>
              <option value="tibio">Tibio</option>
              <option value="caliente">Caliente</option>
              <option value="cerrado">Cerrado</option>
              <option value="seguimiento">Seguimiento</option>
            </select>
          </div>
          <div>
            <div class="ncpn-label" style="margin-top:.3rem">Score m&iacute;nimo</div>
            <input id="ncpn-score" class="ncpn-input" type="number" min="0" max="100" placeholder="Ej: 30">
          </div>
          <div>
            <div class="ncpn-label" style="margin-top:.3rem">Comuna</div>
            <input id="ncpn-comuna" class="ncpn-input" type="text" maxlength="80" placeholder="Ej: Santiago">
          </div>
          <div>
            <div class="ncpn-label" style="margin-top:.3rem">Desde (primer contacto)</div>
            <input id="ncpn-desde" class="ncpn-input" type="date">
          </div>
          <div>
            <div class="ncpn-label" style="margin-top:.3rem">Hasta</div>
            <input id="ncpn-hasta" class="ncpn-input" type="date">
          </div>
        </div>
        <button id="ncpn-preview-btn" onclick="previewDestinatarios()" style="margin-top:.75rem;background:rgba(255,255,255,.05);border:1px solid var(--border);color:var(--txt2);font-family:'Space Grotesk',sans-serif;font-size:.72rem;font-weight:700;padding:.4rem 1rem;border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='rgba(255,255,255,.1)'" onmouseout="this.style.background='rgba(255,255,255,.05)'">&#128065; Ver destinatarios</button>
      </div>

      <div class="ncpn-section">
        <div class="ncpn-label">Vista previa</div>
        <div class="ncpn-preview-box" id="ncpn-preview-box">
          <div class="ncpn-preview-sub">Haz clic en "Ver destinatarios" para previsualizar</div>
        </div>
      </div>

      <div style="display:flex;gap:.75rem;justify-content:flex-end;border-top:1px solid rgba(255,255,255,.06);padding-top:.75rem">
        <button onclick="cerrarNuevaCampana()" style="background:rgba(255,255,255,.05);border:1px solid var(--border);color:var(--txt2);font-family:'Space Grotesk',sans-serif;font-size:.75rem;padding:.5rem 1.1rem;border-radius:8px;cursor:pointer">Cancelar</button>
        <button id="ncpn-save-btn" onclick="crearCampana()" style="background:rgba(0,212,255,.12);border:1px solid rgba(0,212,255,.4);color:var(--neon);font-family:'Space Grotesk',sans-serif;font-size:.75rem;font-weight:700;padding:.5rem 1.6rem;border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='rgba(0,212,255,.25)'" onmouseout="this.style.background='rgba(0,212,255,.12)'">Crear campa&ntilde;a</button>
      </div>
    </div>
  </div>
</div>

<!-- Modal: Detalle Campaña -->
<div class="modal-overlay" id="modal-campana-detail" onclick="if(event.target===this)cerrarDetalleCampana()">
  <div class="modal-box" style="max-width:680px">
    <div class="modal-header">
      <div>
        <div id="cpnd-title" style="font-family:'Orbitron',sans-serif;font-size:.85rem;font-weight:700;color:var(--neon);letter-spacing:.1em">DETALLE DE CAMPA&Ntilde;A</div>
        <div id="cpnd-sub" style="font-size:.65rem;color:var(--txt3);margin-top:.25rem"></div>
      </div>
      <button class="modal-close" onclick="cerrarDetalleCampana()">&#10005;&nbsp; Cerrar</button>
    </div>
    <div id="cpnd-body" class="modal-messages" style="max-height:75vh;gap:.6rem;padding:1.4rem 1.75rem">
      <div class="empty">Cargando...</div>
    </div>
  </div>
</div>

<!-- ── Modal exportar CSV ─────────────────────────────────────────────────── -->
<div class="modal-overlay" id="modal-export-csv" onclick="if(event.target===this)cerrarExportModal()">
  <div class="modal-box" style="max-width:460px">
    <div class="modal-header">
      <div>
        <div style="font-family:'Orbitron',sans-serif;font-size:.85rem;font-weight:700;color:var(--neon);letter-spacing:.1em">EXPORTAR LEADS</div>
        <div style="font-size:.65rem;color:var(--txt3);margin-top:.25rem">Aplica filtros opcionales antes de descargar</div>
      </div>
      <button class="modal-close" onclick="cerrarExportModal()">&#10005;&nbsp; Cerrar</button>
    </div>
    <div style="padding:1.4rem 1.75rem;display:flex;flex-direction:column;gap:1rem">

      <!-- Estado -->
      <div>
        <label style="display:block;font-size:.65rem;font-weight:700;color:var(--txt3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:.4rem">Estado</label>
        <select id="exp-estado" class="filter-select" style="width:100%">
          <option value="">Todos los estados</option>
          <option value="nuevo">Nuevo</option>
          <option value="contactado">Contactado</option>
          <option value="interesado">Interesado</option>
          <option value="seguimiento">Seguimiento</option>
          <option value="caliente">Caliente</option>
          <option value="listo_para_cierre">Listo para cierre</option>
          <option value="cerrado">Cerrado</option>
          <option value="perdido">Perdido</option>
          <option value="sin_cobertura">Sin cobertura</option>
        </select>
      </div>

      <!-- Tag -->
      <div>
        <label style="display:block;font-size:.65rem;font-weight:700;color:var(--txt3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:.4rem">Tag</label>
        <select id="exp-tag" class="filter-select" style="width:100%">
          <option value="">Todos los tags</option>
          <option value="Interesado">Interesado</option>
          <option value="Sin cobertura">Sin cobertura</option>
          <option value="Tiene contrato">Tiene contrato</option>
          <option value="Precio alto">Precio alto</option>
          <option value="Llamar después">Llamar después</option>
          <option value="No contesta">No contesta</option>
          <option value="Cerrado">Cerrado</option>
        </select>
      </div>

      <!-- Prioridad -->
      <div>
        <label style="display:block;font-size:.65rem;font-weight:700;color:var(--txt3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:.4rem">Prioridad visual</label>
        <select id="exp-prioridad" class="filter-select" style="width:100%">
          <option value="">Todas las prioridades</option>
          <option value="alta">&#128308; Alta</option>
          <option value="media">&#128993; Media</option>
          <option value="baja">&#9898; Baja</option>
        </select>
      </div>

      <!-- Rango de fechas -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:.75rem">
        <div>
          <label style="display:block;font-size:.65rem;font-weight:700;color:var(--txt3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:.4rem">Desde</label>
          <input id="exp-desde" type="date" class="filter-select" style="width:100%;color-scheme:dark">
        </div>
        <div>
          <label style="display:block;font-size:.65rem;font-weight:700;color:var(--txt3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:.4rem">Hasta</label>
          <input id="exp-hasta" type="date" class="filter-select" style="width:100%;color-scheme:dark">
        </div>
      </div>

      <!-- Info de leads a exportar -->
      <div id="exp-preview" style="background:rgba(0,212,255,.06);border:1px solid rgba(0,212,255,.2);border-radius:8px;padding:.65rem .9rem;font-size:.72rem;color:var(--txt2);min-height:2.2rem"></div>

      <!-- Botones -->
      <div style="display:flex;gap:.75rem;justify-content:flex-end;margin-top:.25rem">
        <button onclick="cerrarExportModal()" style="background:rgba(255,255,255,.05);border:1px solid var(--border);color:var(--txt2);font-family:'Space Grotesk',sans-serif;font-size:.75rem;padding:.5rem 1.1rem;border-radius:8px;cursor:pointer">Cancelar</button>
        <button id="exp-download-btn" onclick="ejecutarExportCSV()" style="background:rgba(0,212,255,.12);border:1px solid rgba(0,212,255,.4);color:var(--neon);font-family:'Space Grotesk',sans-serif;font-size:.75rem;font-weight:700;padding:.5rem 1.4rem;border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='rgba(0,212,255,.25)'" onmouseout="this.style.background='rgba(0,212,255,.12)'">&#8659; Descargar CSV</button>
      </div>
    </div>
  </div>
</div>

<script>
// =========================================================================
// CHART
// =========================================================================
let chartEstados = null;
function initChart(labels, data, colors) {
  const ctx = document.getElementById('chart-estados').getContext('2d');
  if (chartEstados) chartEstados.destroy();
  chartEstados = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors.map(c => c + '33'),
        borderColor:     colors,
        borderWidth: 1, borderRadius: 6,
        hoverBackgroundColor: colors.map(c => c + '66'),
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(0,0,0,0.9)', borderColor: '#00D4FF', borderWidth: 1,
          titleColor: '#00D4FF', bodyColor: '#ffffff',
          titleFont: { family: 'Orbitron', size: 11 },
          bodyFont:  { family: 'Space Grotesk', size: 12 }, padding: 12,
        }
      },
      scales: {
        x: {
          ticks: { color: 'rgba(255,255,255,0.4)', font: { family: 'Space Grotesk', size: 10 } },
          grid:  { color: 'rgba(255,255,255,0.04)' },
          border:{ color: 'rgba(0,212,255,0.15)' }
        },
        y: {
          ticks: { color: 'rgba(255,255,255,0.4)', font: { size: 10 }, stepSize: 1 },
          grid:  { color: 'rgba(255,255,255,0.04)' },
          border:{ color: 'rgba(0,212,255,0.15)' },
          beginAtZero: true
        }
      }
    }
  });
}

// =========================================================================
// HELPERS
// =========================================================================
function esc(str) {
  return String(str||'')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/\\n/g,'<br>');
}
function fmtTime(ts) {
  if (!ts) return '\u2014';
  const d = new Date(ts.replace(' ','T'));
  if (isNaN(d)) return ts.slice(10,16) || ts;
  return d.toLocaleTimeString('es-CL', { hour:'2-digit', minute:'2-digit' });
}

function fmtSinRespuesta(ts) {
  /* Devuelve { texto, clase } según tiempo transcurrido desde el último mensaje del lead. */
  if (!ts) return null;
  const d = new Date(ts.replace(' ','T'));
  if (isNaN(d)) return null;
  const seg = Math.floor((Date.now() - d.getTime()) / 1000);
  if (seg < 60)   return { texto: `${seg}s`,  clase: 'verde' };
  const min = Math.floor(seg / 60);
  if (min < 60)   return { texto: `${min}min`, clase: 'verde' };
  const hrs = Math.floor(min / 60);
  if (hrs < 24)   return { texto: `${hrs}h`,  clase: hrs < 1 ? 'verde' : 'amarillo' };
  const dias = Math.floor(hrs / 24);
  const hRest = hrs % 24;
  const texto = hRest > 0 ? `${dias}d ${hRest}h` : `${dias}d`;
  return { texto, clase: 'rojo' };
}
function fmtDateLabel(ts) {
  if (!ts) return '';
  const d = new Date(ts.replace(' ','T'));
  if (isNaN(d)) return '';
  const hoy  = new Date();
  const ayer = new Date(hoy); ayer.setDate(ayer.getDate()-1);
  const same = (a,b) => a.getDate()===b.getDate() && a.getMonth()===b.getMonth() && a.getFullYear()===b.getFullYear();
  if (same(d,hoy)) return 'Hoy';
  if (same(d,ayer)) return 'Ayer';
  return d.toLocaleDateString('es-CL', { day:'numeric', month:'long' });
}
function avatarColor(tel) {
  const p = ['#00D4FF','#FF2233','#00FF88','#FF8C00','#c084fc','#F59E0B','#10B981','#3B82F6','#EC4899'];
  let h = 0; for (let i=0;i<tel.length;i++) h=(Math.imul(31,h)+tel.charCodeAt(i))|0;
  return p[Math.abs(h)%p.length];
}
function estadoBadge(estado, color) {
  return `<span class="estado-badge" style="background:${color}1a;color:${color};border:1px solid ${color}55;box-shadow:0 0 6px ${color}33">${estado}</span>`;
}
function intencionTag(v) {
  const cls = v==='alta'?'tag-alta':v==='media'?'tag-media':'tag-baja';
  return `<span class="${cls}">${v}</span>`;
}
function prioridadLabel(emoji) {
  return {'\\uD83D\\uDD34':'Caliente','\\uD83D\\uDFE1':'Tibio','\\u26AA':'Fr\\u00edo','\\uD83D\\uDFE3':'En atenci\\u00f3n humana'}[emoji] || '';
}
function botonAccion(lead) {
  if (lead.estado === 'modo_humano') return `<button class="btn-liberar" onclick="liberarLead('${lead.telefono}')">Liberar IA</button>`;
  return `<button class="btn-tomar" onclick="tomarLead('${lead.telefono}',this)">Tomar lead</button>`;
}
async function tomarLead(telefono, btn) {
  btn.disabled = true; btn.textContent = '...';
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(telefono) + '/tomar', { method:'POST' });
    if (r.ok) await actualizarLeads(); else { btn.disabled=false; btn.textContent='Tomar lead'; }
  } catch(e) { btn.disabled=false; btn.textContent='Tomar lead'; }
}
async function liberarLead(telefono) {
  await fetch('/api/leads/' + encodeURIComponent(telefono) + '/liberar', { method:'POST' });
  await actualizarLeads();
}

// =========================================================================
// TABS
// =========================================================================
function switchTab(tab) {
  // Cerrar Live si está abierto (sin llamar a switchTab de nuevo)
  const pc  = document.getElementById('panel-chat');
  const btnLive = document.getElementById('btn-live');
  if (pc && pc.style.display === 'flex') {
    pc.style.display = 'none'; pc.style.flexDirection = '';
    if (btnLive) btnLive.classList.remove('active');
  }

  const pm  = document.getElementById('panel-metrics');
  const pk  = document.getElementById('panel-campanas');
  const psr = document.getElementById('panel-sin-respuesta');
  const bm  = document.getElementById('tab-metricas');
  const bk  = document.getElementById('tab-campanas');
  const bsr = document.getElementById('tab-sin-resp');

  // Ocultar todos
  pm.style.display  = 'none';
  pk.style.display  = 'none';
  psr.style.display = 'none';
  bm.classList.remove('active');
  bk.classList.remove('active');
  if (bsr) bsr.classList.remove('active');

  if (tab === 'metricas') {
    pm.style.display = '';
    bm.classList.add('active');
  } else if (tab === 'campanas') {
    pk.style.display = 'block';
    bk.classList.add('active');
    actualizarCampanasList();
  } else if (tab === 'sin-respuesta') {
    psr.style.display = 'block';
    if (bsr) bsr.classList.add('active');
    actualizarSinRespuesta();
  }
}

let _tabAnterior = 'metricas';

function _abrirLivePanel() {
  const pc  = document.getElementById('panel-chat');
  const pm  = document.getElementById('panel-metrics');
  const pk  = document.getElementById('panel-campanas');
  const psr = document.getElementById('panel-sin-respuesta');
  const btn = document.getElementById('btn-live');
  // Guardar qué tab estaba activo
  _tabAnterior = pk && pk.style.display !== 'none'
    ? 'campanas'
    : (psr && psr.style.display !== 'none' ? 'sin-respuesta' : 'metricas');
  pm.style.display  = 'none';
  pk.style.display  = 'none';
  if (psr) psr.style.display = 'none';
  pc.style.display = 'flex';
  pc.style.flexDirection = 'column';
  btn.classList.add('active');
  actualizarConversaciones();
}

function _cerrarLivePanel() {
  const pc  = document.getElementById('panel-chat');
  const btn = document.getElementById('btn-live');
  pc.style.display = 'none';
  pc.style.flexDirection = '';
  btn.classList.remove('active');
  // Restaurar tab anterior
  switchTab(_tabAnterior);
}

function toggleLive() {
  const pc = document.getElementById('panel-chat');
  if (pc.style.display === 'flex') {
    history.back(); // deja que popstate maneje el cierre
  } else {
    _abrirLivePanel();
    history.pushState({ view: 'live' }, '');
  }
}

// =========================================================================
// METRICS
// =========================================================================
async function actualizarStats() {
  try {
    const r = await fetch('/api/stats');
    if (!r.ok) {
      const txt = await r.text();
      console.error('api/stats error', r.status, txt);
      document.getElementById('last-update').textContent = 'ERR ' + r.status;
      return;
    }
    const d = await r.json();
    console.log('api/stats:', d);
    document.getElementById('k-total').textContent     = d.total_leads    ?? '?';
    document.getElementById('k-hot').textContent       = d.leads_calientes ?? '?';
    document.getElementById('k-closed').textContent    = d.leads_cerrados  ?? '?';
    document.getElementById('k-score').textContent     = d.score_promedio  ?? '?';
    document.getElementById('k-msgs').textContent      = d.mensajes_hoy    ?? '?';
    document.getElementById('k-followups').textContent = d.followups_pendientes ?? '?';
    document.getElementById('last-update').textContent = d.actualizado;
    // Indicadores de estado del sistema
    _setSysStatus(true);
    // Actualizar contador "Sin Respuesta" en el tab
    const srCount = d.sin_respuesta_count ?? 0;
    const srTabBadge = document.getElementById('tab-sr-count');
    if (srTabBadge) {
      srTabBadge.textContent = srCount;
      srTabBadge.style.display = srCount > 0 ? 'inline' : 'none';
    }
    if (d.por_estado && d.por_estado.length) {
      initChart(d.por_estado.map(e=>e.estado), d.por_estado.map(e=>e.total), d.por_estado.map(e=>e.color));
    }
    renderEmbudo(d.conversion);
  } catch(e) {
    console.error('actualizarStats excepción:', e);
    document.getElementById('last-update').textContent = 'ERR JS';
    _setSysStatus(false);
  }
}

function _setSysStatus(ok) {
  const isMobile = window.matchMedia('(max-width:768px)').matches;
  const desk  = document.getElementById('sys-status-desktop');
  const mob   = document.getElementById('sys-status-mobile');
  // Mostrar el correcto según viewport
  if (desk) desk.style.display = '';   // header-right ya lo oculta en mobile via CSS
  if (mob)  mob.style.display  = isMobile ? 'flex' : 'none';
  [desk, mob].forEach(el => {
    if (!el) return;
    if (ok) {
      el.classList.remove('error');
      el.querySelector('span').textContent = el === mob ? 'Activo' : 'Sistema activo';
    } else {
      el.classList.add('error');
      el.querySelector('span').textContent = 'Sin conexión';
    }
  });
}

function renderEmbudo(conv) {
  if (!conv) return;
  [conv.contactado_interesado, conv.interesado_caliente, conv.caliente_cierre].forEach((e,i) => {
    document.getElementById(`f-pct-${i}`).textContent = e.den>0 ? e.pct+'%' : '\u2014';
    setTimeout(() => { document.getElementById(`f-bar-${i}`).style.width = e.den>0 ? e.pct+'%' : '0%'; }, 80+i*60);
    document.getElementById(`f-cnt-${i}`).innerHTML = e.den>0 ? `<strong>${e.num}</strong> de ${e.den} leads` : 'sin datos suficientes';
  });
}
let _leadsData = [];

async function actualizarLeads() {
  const r = await fetch('/api/leads'); const d = await r.json();
  _leadsData = d.leads || [];
  // Poblar select de tags con opciones únicas
  const tagSelect = document.getElementById('f-tag');
  if (tagSelect) {
    const allTags = [...new Set(_leadsData.flatMap(l => l.tags || []))].sort();
    const curVal  = tagSelect.value;
    tagSelect.innerHTML = '<option value="">Todos los tags</option>' +
      allTags.map(t => `<option value="${esc(t)}"${t===curVal?' selected':''}>${esc(t)}</option>`).join('');
  }
  filtrarLeads();
}

function filtrarLeads() {
  const buscar = (document.getElementById('f-buscar')?.value || '').toLowerCase().trim();
  const estado = document.getElementById('f-estado')?.value || '';
  const tag    = document.getElementById('f-tag')?.value    || '';
  const scoreMin = parseInt(document.getElementById('f-score')?.value || '0', 10) || 0;
  const desde  = document.getElementById('f-desde')?.value || '';
  const hasta  = document.getElementById('f-hasta')?.value || '';

  const filtrados = _leadsData.filter(l => {
    if (buscar && !( (l.nombre||'').toLowerCase().includes(buscar) || l.telefono.includes(buscar) )) return false;
    if (estado && l.estado !== estado) return false;
    if (tag    && !(l.tags||[]).includes(tag)) return false;
    if (scoreMin > 0 && (l.score || 0) < scoreMin) return false;
    if (desde && l.created_at && l.created_at.slice(0,10) < desde) return false;
    if (hasta && l.created_at && l.created_at.slice(0,10) > hasta) return false;
    return true;
  });

  const countEl = document.getElementById('f-count');
  if (countEl) countEl.textContent = filtrados.length < _leadsData.length
    ? `${filtrados.length} de ${_leadsData.length}`
    : `${_leadsData.length} leads`;

  const el = document.getElementById('leads-list');
  if (!filtrados.length) { el.innerHTML = '<div class="empty">Sin leads que coincidan</div>'; return; }

  const TAG_COLORS = ['#00D4FF','#c084fc','#f59e0b','#22c55e','#ef4444','#3b82f6','#f97316','#10b981'];
  function tagColor(t) { let h=0; for(let i=0;i<t.length;i++) h=(Math.imul(31,h)+t.charCodeAt(i))|0; return TAG_COLORS[Math.abs(h)%TAG_COLORS.length]; }

  el.innerHTML = filtrados.map(l => {
    const safeTel    = l.telefono.replace(/['"<>&]/g, '');
    const safeNombre = esc(l.nombre);
    const notasIcon  = l.notas
      ? `<button class="btn-notas activo" title="Ver/editar notas" onclick="abrirNotasModal('${safeTel}','${safeNombre}',this.dataset.notas)" data-notas="${esc(l.notas)}">&#128221;</button>`
      : `<button class="btn-notas"        title="Agregar nota"     onclick="abrirNotasModal('${safeTel}','${safeNombre}','')"                                              >&#128221;</button>`;
    const detailIcon = `<button class="btn-detail" title="Ver resumen IA" onclick="abrirLeadDetail('${safeTel}')">&#128270;</button>`;
    const tagsIcon   = `<button class="btn-tags${l.tags&&l.tags.length?' activo':''}" title="Editar tags" onclick="abrirTagsModal('${safeTel}','${safeNombre}')">&#127991;</button>`;
    const tagsHtml   = l.tags && l.tags.length
      ? `<div class="lead-tags">${l.tags.map(t=>`<span class="tag-chip-sm" style="background:${tagColor(t)}22;color:${tagColor(t)};border:1px solid ${tagColor(t)}55">${esc(t)}</span>`).join('')}</div>`
      : '';
    return `
    <div class="lead-row fade-in" style="border-left-color:${l.color};box-shadow:inset 2px 0 8px ${l.color}22">
      <div class="lead-priority" title="${prioridadLabel(l.prioridad)}">${l.prioridad}</div>
      <div style="min-width:0">
        <div class="lead-name">${esc(l.nombre)}</div>
        <div class="lead-phone">${l.telefono} &middot; ${l.subproducto}</div>
        ${tagsHtml}
        ${l.resumen ? `<div class="lead-resumen" title="${esc(l.resumen)}">${esc(l.resumen)}</div>` : ''}
      </div>
      ${estadoBadge(l.estado,l.color)}
      <div class="lead-score">${l.score}<span style="font-size:.55rem;opacity:.6">pts</span></div>
      ${detailIcon}
      ${notasIcon}
      ${tagsIcon}
      ${botonAccion(l)}
    </div>`;
  }).join('');
}

function limpiarFiltros() {
  ['f-buscar','f-score'].forEach(id => { const el=document.getElementById(id); if(el) el.value=''; });
  ['f-estado','f-tag'].forEach(id => { const el=document.getElementById(id); if(el) el.value=''; });
  ['f-desde','f-hasta'].forEach(id => { const el=document.getElementById(id); if(el) el.value=''; });
  filtrarLeads();
}

// =========================================================================
// MODAL — Tags del lead
// =========================================================================
const TAGS_PREDEFINIDOS = [
  'Interesado','Sin cobertura','Tiene contrato','Precio alto',
  'Llamar después','No contesta','Cerrado',
];
const TAG_COLORS_MODAL = ['#00D4FF','#c084fc','#f59e0b','#22c55e','#ef4444','#3b82f6','#f97316','#10b981'];
function _tagColorModal(t){let h=0;for(let i=0;i<t.length;i++)h=(Math.imul(31,h)+t.charCodeAt(i))|0;return TAG_COLORS_MODAL[Math.abs(h)%TAG_COLORS_MODAL.length];}

let _tagsTelActivo = '';
let _tagsActivos   = [];

function abrirTagsModal(telefono, nombre) {
  _tagsTelActivo = telefono;
  // Obtener tags actuales del lead en _leadsData
  const lead = _leadsData.find(l => l.telefono === telefono);
  _tagsActivos = lead ? [...(lead.tags || [])] : [];
  document.getElementById('tags-modal-title').textContent = 'TAGS — ' + nombre;
  document.getElementById('tags-custom-input').value = '';
  renderTagsPredefinidos();
  document.getElementById('modal-tags').style.display = 'flex';
}

function cerrarTagsModal() {
  document.getElementById('modal-tags').style.display = 'none';
  _tagsTelActivo = '';
}

function renderTagsPredefinidos() {
  // Combinar predefinidos con tags personalizados activos
  const todos = [...new Set([...TAGS_PREDEFINIDOS, ..._tagsActivos])];
  const el = document.getElementById('tags-predefined');
  el.innerHTML = todos.map(t => {
    const c      = _tagColorModal(t);
    const activo = _tagsActivos.includes(t);
    return `<button class="tag-toggle${activo?' activo':''}"
      style="background:${c}${activo?'33':'11'};color:${c};border:1px solid ${c}${activo?'66':'33'}"
      onclick="toggleTag('${esc(t)}')">${esc(t)}</button>`;
  }).join('');
}

function toggleTag(tag) {
  if (_tagsActivos.includes(tag)) {
    _tagsActivos = _tagsActivos.filter(t => t !== tag);
  } else {
    if (_tagsActivos.length >= 20) return;
    _tagsActivos.push(tag);
  }
  renderTagsPredefinidos();
}

function agregarTagPersonalizado() {
  const input = document.getElementById('tags-custom-input');
  const val   = (input.value || '').trim().slice(0, 50);
  if (!val || _tagsActivos.includes(val) || _tagsActivos.length >= 20) { input.value=''; return; }
  _tagsActivos.push(val);
  input.value = '';
  renderTagsPredefinidos();
}

async function guardarTags() {
  if (!_tagsTelActivo) return;
  const btn = document.getElementById('tags-save-btn');
  btn.disabled = true; btn.textContent = 'Guardando...';
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(_tagsTelActivo) + '/tags', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tags: _tagsActivos }),
    });
    if (r.ok) {
      const telGuardado = _tagsTelActivo;
      cerrarTagsModal();
      await actualizarLeads();
      // Si el lead está abierto en el Live Chat, refrescar el header para mostrar los nuevos tags
      if (contactoActivo && contactoActivo === telGuardado) {
        const conv = conversaciones.find(c => c.telefono === contactoActivo);
        if (conv) renderChatHeader(contactoActivo, conv.modo_humano || false);
      }
    } else {
      btn.textContent = 'Error — reintentar'; btn.disabled = false;
    }
  } catch(_) { btn.textContent = 'Error — reintentar'; btn.disabled = false; }
}
let _expListenersOk = false;
function abrirExportModal() {
  document.getElementById('modal-export-csv').style.display = 'flex';
  // Registrar listeners de preview solo la primera vez
  if (!_expListenersOk) {
    ['exp-estado','exp-tag','exp-prioridad','exp-desde','exp-hasta'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('change', actualizarExportPreview);
    });
    _expListenersOk = true;
  }
  actualizarExportPreview();
}

function cerrarExportModal() {
  document.getElementById('modal-export-csv').style.display = 'none';
}

function actualizarExportPreview() {
  const estado    = (document.getElementById('exp-estado')?.value    || '').toLowerCase();
  const tag       = (document.getElementById('exp-tag')?.value       || '');
  const prioridad = (document.getElementById('exp-prioridad')?.value || '').toLowerCase();
  const desde     = document.getElementById('exp-desde')?.value  || '';
  const hasta     = document.getElementById('exp-hasta')?.value  || '';
  const prev      = document.getElementById('exp-preview');
  if (!prev) return;

  function _prioridadKey(l) {
    const e = (l.estado || '').toLowerCase();
    const s = l.score || 0;
    if (['caliente','listo_para_cierre','cerrado'].includes(e)) return 'alta';
    if (s >= 60 || ['interesado','seguimiento'].includes(e))   return 'media';
    return 'baja';
  }

  let count = _leadsData.length;
  let filtrado = _leadsData.filter(l => {
    if (estado    && l.estado !== estado)                     return false;
    if (tag       && !(l.tags||[]).includes(tag))             return false;
    if (prioridad && _prioridadKey(l) !== prioridad)          return false;
    if (desde     && l.created_at && l.created_at < desde)   return false;
    if (hasta     && l.created_at && l.created_at > hasta+'T23:59:59') return false;
    return true;
  });

  const filtros = [estado, tag, prioridad, desde, hasta].filter(Boolean).length;
  if (filtros === 0) {
    prev.textContent = `Se exportarán ${count} leads (todos)`;
  } else {
    prev.textContent = `Se exportarán ≈${filtrado.length} leads con los filtros aplicados`;
  }
}

function ejecutarExportCSV() {
  const btn    = document.getElementById('exp-download-btn');
  const estado = document.getElementById('exp-estado')?.value    || '';
  const tag    = document.getElementById('exp-tag')?.value       || '';
  const prio   = document.getElementById('exp-prioridad')?.value || '';
  const desde  = document.getElementById('exp-desde')?.value     || '';
  const hasta  = document.getElementById('exp-hasta')?.value     || '';

  const params = new URLSearchParams();
  if (estado) params.set('estado',      estado);
  if (tag)    params.set('tag',         tag);
  if (prio)   params.set('prioridad',   prio);
  if (desde)  params.set('fecha_desde', desde);
  if (hasta)  params.set('fecha_hasta', hasta);

  const url = '/api/leads/export-csv' + (params.toString() ? '?' + params.toString() : '');

  if (btn) { btn.textContent = '⏳ Generando...'; btn.disabled = true; }
  const a = document.createElement('a');
  a.href = url; a.download = '';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => {
    if (btn) { btn.textContent = '↙ Descargar CSV'; btn.disabled = false; }
    cerrarExportModal();
  }, 1800);
}
let _notasTelActivo = '';
function abrirNotasModal(telefono, nombre, notas) {
  _notasTelActivo = telefono;
  document.getElementById('notas-modal-title').textContent = 'NOTAS — ' + nombre;
  document.getElementById('notas-textarea').value = notas || '';
  document.getElementById('modal-notas').style.display = 'flex';
  setTimeout(() => document.getElementById('notas-textarea').focus(), 80);
}
function cerrarNotasModal() {
  document.getElementById('modal-notas').style.display = 'none';
  _notasTelActivo = '';
}
async function guardarNotas() {
  if (!_notasTelActivo) return;
  const btn   = document.getElementById('notas-save-btn');
  const notas = document.getElementById('notas-textarea').value;
  btn.disabled = true; btn.textContent = 'Guardando...';
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(_notasTelActivo) + '/notas', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notas }),
    });
    if (r.ok) {
      cerrarNotasModal();
      actualizarLeads();  // refresca la lista para que el ícono se actualice
    } else {
      btn.textContent = 'Error — reintentar';
      btn.disabled = false;
    }
  } catch(_) { btn.textContent = 'Error — reintentar'; btn.disabled = false; }
}
async function actualizarMensajes() {
  const r = await fetch('/api/messages'); const d = await r.json();
  const el = document.getElementById('msgs-list');
  if (!d.mensajes.length) { el.innerHTML = '<div class="empty">Sin mensajes</div>'; return; }
  const seen = new Set(); const list = [];
  for (const m of d.mensajes) { if (!seen.has(m.telefono)) { seen.add(m.telefono); list.push(m); } }
  el.innerHTML = list.map(m => {
    const nombre = m.nombre||m.telefono;
    const inicial = nombre.replace(/[^a-zA-Z0-9]/g,'').charAt(0).toUpperCase()||'#';
    const color   = avatarColor(m.telefono);
    const esBot   = m.rol==='assistant';
    const safeTel = m.telefono.replace(/['"<>&]/g,'');
    return `
    <div class="msg-card fade-in" onclick="irAlChat('${safeTel}')">
      <div class="msg-avatar" style="background:${color}22;color:${color};border:1.5px solid ${color}55">${inicial}</div>
      <div class="msg-card-body">
        <div class="msg-card-name"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(nombre)}</span>${m.estado!=='\u2014'?estadoBadge(m.estado,'#00D4FF'):''}</div>
        <div class="msg-card-preview${esBot?' bot-msg':''}">${esBot?'\u21A9 ':''}${esc(m.mensaje)}</div>
      </div>
      <div class="msg-card-right">
        <div class="msg-card-time">${fmtTime(m.timestamp)}</div>
        ${m.intencion!=='\u2014'?intencionTag(m.intencion):''}
      </div>
    </div>`;
  }).join('');
}
function irAlChat(telefono) {
  const pc = document.getElementById('panel-chat');
  if (pc.style.display !== 'flex') {
    _abrirLivePanel();
    history.pushState({ view: 'live' }, '');
  }
  setTimeout(() => seleccionarContacto(telefono), 80);
}
// =========================================================================
// MODAL — Detalle del lead (resumen IA)
// =========================================================================
let _ldTelActivo = '';

async function abrirLeadDetail(telefono) {
  _ldTelActivo = telefono;
  const modal = document.getElementById('modal-lead-detail');
  const body  = document.getElementById('ld-body');
  document.getElementById('ld-title').textContent   = 'DETALLE DEL LEAD';
  document.getElementById('ld-sub').textContent     = '+' + telefono;
  document.getElementById('ld-btn-chat').onclick    = () => { cerrarLeadDetail(); irAlChat(telefono); };
  body.innerHTML = '<div class="empty">Cargando...</div>';
  modal.style.display = 'flex';
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(telefono) + '/detail');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    renderLeadDetail(d);
  } catch(err) {
    body.innerHTML = `<div class="empty">Error cargando detalle (${err.message})</div>`;
  }
}

function cerrarLeadDetail() {
  document.getElementById('modal-lead-detail').style.display = 'none';
  _ldTelActivo = '';
}

function renderLeadDetail(d) {
  const body   = document.getElementById('ld-body');
  const color  = d.color || '#888';
  document.getElementById('ld-title').textContent = esc(d.nombre).toUpperCase();
  document.getElementById('ld-sub').textContent   = d.prioridad + ' ' + d.estado + '  ·  +' + d.telefono;

  // Resumen IA
  const resumenHtml = d.resumen
    ? `<div class="ld-resumen-block">${esc(d.resumen)}</div>`
    : `<div class="ld-resumen-empty">Sin resumen generado todavía — se genera automáticamente después de cada mensaje</div>`;

  // Objeciones
  const objHtml = d.objeciones && d.objeciones.length
    ? `<div class="ld-objeciones">${d.objeciones.map(o => `<span class="ld-obj-tag">${esc(o)}</span>`).join('')}</div>`
    : `<span style="font-size:.72rem;color:var(--txt3)">Ninguna registrada</span>`;

  body.innerHTML = `
    <div class="ld-section">
      <div class="ld-section-title" style="display:flex;align-items:center;justify-content:space-between">
        <span>Resumen IA</span>
        <button class="ld-regenerar-btn" onclick="regenerarResumen('${d.telefono.replace(/['"<>&]/g,'')}')">&#9881; Regenerar</button>
      </div>
      ${resumenHtml}
    </div>

    <div class="ld-section">
      <div class="ld-section-title">Datos del lead</div>
      <div class="ld-meta-grid">
        <div class="ld-meta-item">
          <div class="ld-meta-label">Estado</div>
          <div class="ld-meta-value"><span style="color:${color}">${d.estado}</span></div>
        </div>
        <div class="ld-meta-item">
          <div class="ld-meta-label">Score</div>
          <div class="ld-meta-value" style="color:${d.score>=70?'#ff4d6d':d.score>=40?'#f4a261':'#888'}">${d.score} / 100</div>
        </div>
        <div class="ld-meta-item">
          <div class="ld-meta-label">Producto</div>
          <div class="ld-meta-value">${esc(d.subproducto)}</div>
        </div>
        <div class="ld-meta-item">
          <div class="ld-meta-label">Dirección</div>
          <div class="ld-meta-value">${esc(d.direccion)}${d.comuna!=='—'?' · '+esc(d.comuna):''}</div>
        </div>
        <div class="ld-meta-item">
          <div class="ld-meta-label">Última interacción</div>
          <div class="ld-meta-value" style="font-size:.7rem;font-weight:400">${fmtDateLabel(d.ultima_interaccion) || d.ultima_interaccion}</div>
        </div>
        <div class="ld-meta-item">
          <div class="ld-meta-label">Lead creado</div>
          <div class="ld-meta-value" style="font-size:.7rem;font-weight:400">${fmtDateLabel(d.created_at) || d.created_at}</div>
        </div>
      </div>
    </div>

    <div class="ld-section">
      <div class="ld-section-title">Objeciones detectadas</div>
      ${objHtml}
    </div>

    ${d.tags && d.tags.length ? `
    <div class="ld-section">
      <div class="ld-section-title" style="display:flex;align-items:center;justify-content:space-between">
        <span>Tags</span>
        <button class="ld-regenerar-btn" onclick="cerrarLeadDetail();abrirTagsModal('${d.telefono.replace(/['"<>&]/g,'')}','${(d.nombre||'').replace(/['"<>&]/g,'')}')">&#9998; Editar</button>
      </div>
      <div class="lead-tags" style="margin-top:.4rem">${d.tags.map(t=>{const c=(['#00D4FF','#c084fc','#f59e0b','#22c55e','#ef4444','#3b82f6','#f97316','#10b981'])[Math.abs([...t].reduce((h,ch)=>(Math.imul(31,h)+ch.charCodeAt(0))|0,0))%8];return`<span class="tag-chip" style="background:${c}22;color:${c};border:1px solid ${c}55">${esc(t)}</span>`}).join('')}</div>
    </div>` : `
    <div class="ld-section">
      <div class="ld-section-title" style="display:flex;align-items:center;justify-content:space-between">
        <span>Tags</span>
        <button class="ld-regenerar-btn" onclick="cerrarLeadDetail();abrirTagsModal('${d.telefono.replace(/['"<>&]/g,'')}','${(d.nombre||'').replace(/['"<>&]/g,'')}')">+ Agregar</button>
      </div>
      <div style="font-size:.72rem;color:var(--txt3)">Sin tags asignados</div>
    </div>`}

    ${d.notas ? `
    <div class="ld-section">
      <div class="ld-section-title">Notas internas</div>
      <div style="background:rgba(168,85,247,.05);border:1px solid rgba(168,85,247,.2);border-radius:10px;padding:.85rem 1rem;font-size:.78rem;color:var(--txt);line-height:1.6;white-space:pre-wrap">${esc(d.notas)}</div>
    </div>` : ''}
  `;
}

async function regenerarResumen(telefono) {
  const btn = document.querySelector('.ld-regenerar-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Generando...'; }
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(telefono) + '/resumen', { method: 'POST' });
    if (r.ok) {
      // Esperar 1s para que la tarea background termine y luego recargar el modal
      await new Promise(res => setTimeout(res, 1000));
      await abrirLeadDetail(telefono);
    } else {
      if (btn) { btn.disabled = false; btn.textContent = '⚙ Regenerar'; }
    }
  } catch(_) {
    if (btn) { btn.disabled = false; btn.textContent = '⚙ Regenerar'; }
  }
}

// =========================================================================
// ESTADÍSTICAS DE CAMPAÑAS
// =========================================================================
const COLOR_ESTADO_JS = {
  nuevo:'#555555', contactado:'#3498db', interesado:'#9b59b6', tibio:'#e67e22',
  caliente:'#e74c3c', direccion_obtenida:'#1abc9c', listo_para_cierre:'#c9a227',
  cerrado:'#2ecc71', seguimiento:'#7f8c8d', modo_humano:'#a855f7',
};
let chartLeadsDia = null;
let chartProductos = null;

async function actualizarCampanas() {
  try {
    const r = await fetch('/api/stats/campanas');
    if (!r.ok) return;
    const d = await r.json();
    renderEmbudoCampanas(d.embudo   || []);
    renderFollowupRate  (d.followups || {});
    renderChartLeadsDia (d.leads_por_dia || []);
    renderChartProductos(d.top_productos  || []);
  } catch(e) { console.error('[Campanas]', e); }
}

function renderEmbudoCampanas(embudo) {
  const el = document.getElementById('campanas-embudo');
  if (!embudo.length) { el.innerHTML = '<div class="empty">Sin datos aún</div>'; return; }
  const max = Math.max(...embudo.map(e => e.total), 1);
  el.innerHTML = embudo.map(e => {
    const pct   = Math.round(e.total / max * 100);
    const color = e.color || '#888';
    return `<div class="campanas-embudo-row">
      <div class="campanas-embudo-label">
        <span class="campanas-embudo-name">${e.estado}</span>
        <span class="campanas-embudo-val" style="color:${color}">${e.total}<span class="campanas-embudo-pct">${e.pct_total}%</span></span>
      </div>
      <div class="campanas-bar-track">
        <div class="campanas-bar-fill" style="width:${pct}%;background:${color}"></div>
      </div>
    </div>`;
  }).join('');
}

function renderFollowupRate(f) {
  const el = document.getElementById('campanas-followups');
  if (!f || !f.total_enviados) {
    el.innerHTML = '<div class="empty">Aún no se han enviado follow-ups</div>';
    return;
  }
  const tColor = f.tasa >= 50 ? 'var(--green)' : f.tasa >= 25 ? 'var(--orange)' : 'var(--red)';
  const rows = (f.por_tipo || []).map(t => {
    const c = t.tasa >= 50 ? 'var(--green)' : t.tasa >= 25 ? 'var(--orange)' : 'var(--txt3)';
    const barW = Math.round(t.tasa);
    return `<div class="fu-tipo-row">
      <span class="fu-tipo-tag">${t.tipo}</span>
      <div style="flex:1;margin:0 .75rem;height:4px;border-radius:2px;background:rgba(255,255,255,.06)">
        <div style="height:100%;width:${barW}%;background:${c};border-radius:2px;transition:width .6s ease"></div>
      </div>
      <span class="fu-tipo-cnt">${t.respondidos}/${t.enviados}</span>
      <span class="fu-tipo-pct" style="color:${c}">${t.tasa}%</span>
    </div>`;
  }).join('');
  el.innerHTML = `
    <div class="fu-rate-headline">
      <span class="fu-rate-num" style="color:${tColor}">${f.tasa}%</span>
      <span class="fu-rate-sub">${f.total_respondidos} de ${f.total_enviados} respondidos</span>
    </div>
    ${rows || '<div style="font-size:.68rem;color:var(--txt3)">Sin datos por tipo todavía</div>'}`;
}

function renderChartLeadsDia(data) {
  const ctx = document.getElementById('chart-leads-dia');
  if (!ctx) return;
  if (chartLeadsDia) { chartLeadsDia.destroy(); chartLeadsDia = null; }
  const labels = data.map(d => d.dia.slice(5));  // MM-DD
  const values = data.map(d => d.total);
  chartLeadsDia = new Chart(ctx.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: '#00D4FF', backgroundColor: 'rgba(0,212,255,.07)',
        borderWidth: 2, pointRadius: 3, pointBackgroundColor: '#00D4FF',
        fill: true, tension: .35,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor:'rgba(0,0,0,.9)', borderColor:'#00D4FF', borderWidth:1,
          titleColor:'#00D4FF', bodyColor:'#fff',
          titleFont:{family:'Orbitron',size:9}, bodyFont:{family:'Space Grotesk',size:12}, padding:10 }
      },
      scales: {
        x: { ticks:{color:'rgba(255,255,255,.35)', font:{family:'Space Grotesk',size:8}, maxRotation:45},
             grid:{color:'rgba(255,255,255,.04)'}, border:{color:'rgba(0,212,255,.15)'} },
        y: { ticks:{color:'rgba(255,255,255,.4)', font:{size:10}, stepSize:1},
             grid:{color:'rgba(255,255,255,.04)'}, border:{color:'rgba(0,212,255,.15)'}, beginAtZero:true }
      }
    }
  });
}

function renderChartProductos(data) {
  const ctx = document.getElementById('chart-productos');
  if (!ctx) return;
  if (chartProductos) { chartProductos.destroy(); chartProductos = null; }
  if (!data.length) {
    const wrap = ctx.closest('.campanas-chart-wrap');
    if (wrap) wrap.innerHTML = '<div class="empty" style="padding-top:3rem">Sin datos de productos</div>';
    return;
  }
  const PROD_COLORS = ['#00D4FF','#c084fc','#f59e0b','#22c55e','#ef4444','#3b82f6','#1abc9c','#e67e22'];
  const labels = data.map(d => d.producto.length > 22 ? d.producto.slice(0,20)+'\u2026' : d.producto);
  const values = data.map(d => d.total);
  chartProductos = new Chart(ctx.getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: PROD_COLORS.map(c => c + '22'),
        borderColor:     PROD_COLORS,
        borderWidth: 1, borderRadius: 5,
      }]
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor:'rgba(0,0,0,.9)', borderColor:'rgba(0,212,255,.3)', borderWidth:1,
          titleColor:'#00D4FF', bodyColor:'#fff',
          titleFont:{family:'Space Grotesk',size:11}, bodyFont:{family:'Space Grotesk',size:12}, padding:10 }
      },
      scales: {
        x: { ticks:{color:'rgba(255,255,255,.4)', font:{size:10}, stepSize:1},
             grid:{color:'rgba(255,255,255,.04)'}, border:{color:'rgba(0,212,255,.15)'}, beginAtZero:true },
        y: { ticks:{color:'rgba(255,255,255,.55)', font:{family:'Space Grotesk',size:9}},
             grid:{display:false}, border:{display:false} }
      }
    }
  });
}

// ── Mapa de calor por comuna ───────────────────────────────────────────────
let _hmData      = [];   // todos los datos originales
let _hmFiltrado  = [];   // filtrado por búsqueda
let _hmPage      = 0;
const _HM_PAGE_SIZE = 15;

async function actualizarHeatmap() {
  try {
    const r = await fetch('/api/leads/comunas/stats');
    if (!r.ok) return;
    const d = await r.json();
    _hmData = d.comunas || [];
    _hmFiltrado = [..._hmData];
    _hmPage = 0;
    hmRender();
  } catch(e) { console.warn('heatmap error:', e); }
}

function hmFiltrar() {
  const q = (document.getElementById('hm-search').value || '').toLowerCase().trim();
  _hmFiltrado = q
    ? _hmData.filter(c => c.comuna.toLowerCase().includes(q))
    : [..._hmData];
  _hmPage = 0;
  hmRender();
}

function hmPaginar(dir) {
  const maxPage = Math.ceil(_hmFiltrado.length / _HM_PAGE_SIZE) - 1;
  _hmPage = Math.max(0, Math.min(_hmPage + dir, maxPage));
  hmRender();
}

function hmRender() {
  const tbody = document.getElementById('hm-tbody');
  if (!tbody) return;

  const total = _hmFiltrado.length;
  const start = _hmPage * _HM_PAGE_SIZE;
  const page  = _hmFiltrado.slice(start, start + _HM_PAGE_SIZE);
  const maxTotal = _hmData.length > 0 ? _hmData[0].total : 1;

  // Label total
  const lbl = document.getElementById('hm-total-label');
  if (lbl) lbl.textContent = `(${_hmData.length} comunas)`;

  if (page.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty" style="padding:1.5rem;text-align:center">Sin resultados</td></tr>';
    document.getElementById('hm-pag-info').textContent = '';
    document.getElementById('hm-prev').disabled = true;
    document.getElementById('hm-next').disabled = true;
    return;
  }

  tbody.innerHTML = page.map((c, i) => {
    const globalRank = start + i;   // posición real (0-based) en el dataset completo
    const pct   = maxTotal > 0 ? Math.round((c.total / maxTotal) * 100) : 0;
    const hotPct = c.total > 0 ? Math.round((c.calientes / c.total) * 100) : 0;

    // Color de la barra según posición real
    let barColor, rankIcon, rankClass;
    if (globalRank < 3) {
      barColor = '#FF2233'; rankIcon = '🔴'; rankClass = 'hm-rank-hot';
    } else if (globalRank < Math.ceil(_hmData.length * 0.3)) {
      barColor = '#FFAA00'; rankIcon = '🟡'; rankClass = 'hm-rank-warm';
    } else {
      barColor = 'rgba(255,255,255,.25)'; rankIcon = '⚪'; rankClass = 'hm-rank-cold';
    }

    return `<tr>
      <td style="color:var(--txt3);font-size:.65rem">${globalRank + 1}</td>
      <td style="font-size:1rem;line-height:1">${rankIcon}</td>
      <td class="hm-comuna">${esc(c.comuna)}</td>
      <td class="hm-total ${rankClass}">${c.total}</td>
      <td style="font-size:.72rem">
        ${c.calientes > 0
          ? `<span class="hm-hot">${c.calientes}</span> <span style="color:var(--txt3);font-size:.62rem">(${hotPct}%)</span>`
          : `<span style="color:var(--txt3)">—</span>`}
      </td>
      <td class="hm-score">${c.score_promedio > 0 ? c.score_promedio.toFixed(1) : '—'}</td>
      <td>
        <div class="hm-bar-wrap">
          <div class="hm-bar-track">
            <div class="hm-bar-fill" style="width:${pct}%;background:${barColor}"></div>
          </div>
        </div>
      </td>
    </tr>`;
  }).join('');

  // Paginación
  const totalPages = Math.ceil(total / _HM_PAGE_SIZE);
  document.getElementById('hm-pag-info').textContent =
    total > _HM_PAGE_SIZE ? `Página ${_hmPage + 1} de ${totalPages} · ${total} comunas` : `${total} comunas`;
  document.getElementById('hm-prev').disabled = (_hmPage === 0);
  document.getElementById('hm-next').disabled = (_hmPage >= totalPages - 1);
}

async function refresh() {
  try { await Promise.all([actualizarStats(), actualizarLeads(), actualizarMensajes(), actualizarCampanas(), actualizarHeatmap()]); }
  catch(e) { document.getElementById('last-update').textContent = 'ERROR'; }
}
refresh();
setInterval(refresh, 30_000);

// =========================================================================
// SSE
// =========================================================================
let _sse = null;
function conectarSSE() {
  if (_sse) { try { _sse.close(); } catch(_){} }
  _sse = new EventSource('/api/events');
  _sse.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.type === 'new_message') {
        actualizarConversaciones();
        if (contactoActivo && contactoActivo.replace(/\\s/g,'') === d.telefono.replace(/\\s/g,'')) {
          if (!_chatUltimoTS || d.ts > _chatUltimoTS) {
            waAgregarBurbuja(d.role, d.content, d.ts, d.estado_lead || '');
            waScrollAbajo();
            _chatUltimoTS = d.ts;
          }
        } else { flashConvWA(d.telefono); }
      } else if (d.type === 'conversations_update') {
        actualizarConversaciones();
      } else if (d.type === 'mode_change') {
        actualizarConversaciones();
        if (contactoActivo && contactoActivo.replace(/\\s/g,'') === d.telefono.replace(/\\s/g,'')) renderChatHeader(d.telefono, d.modo_humano);
      }
    } catch(_) {}
  };
  _sse.onerror = () => { _sse.close(); setTimeout(conectarSSE, 4000); };
}

// Reconectar SSE cuando el tab vuelve a ser visible (sobrevive redeploy/sleep)
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && (!_sse || _sse.readyState === 2)) {
    conectarSSE();
  }
});

// =========================================================================
// LIVE CHAT STATE
// =========================================================================
let contactoActivo = null;
let conversaciones = [];
let _chatUltimoTS  = '';
let _searchQuery   = '';

async function actualizarConversaciones() {
  try {
    const r = await fetch('/api/conversations');
    if (!r.ok) { console.error('[LiveChat] /api/conversations HTTP', r.status); return; }
    const d = await r.json();
    conversaciones = d.conversaciones || [];
    console.log('[LiveChat] conversaciones cargadas:', conversaciones.length);
    renderConvList();
  } catch(err) { console.error('[LiveChat] actualizarConversaciones error:', err); }
}

let _tagFilter    = '';
let _statusFilter = '';   // '' | 'respondio' | 'sin-resp' | 'manual'
function filtrarContactos(q) { _searchQuery = q.toLowerCase(); renderConvList(); }
function filtrarPorTag(tag) { _tagFilter = tag; renderConvList(); }
function filtrarPorEstado(estado) {
  _statusFilter = estado;
  // Activar botón correcto
  ['sf-todos','sf-respondio','sf-sin-resp','sf-manual'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.classList.remove('active');
  });
  const mapa = { '':'sf-todos', 'respondio':'sf-respondio', 'sin-resp':'sf-sin-resp', 'manual':'sf-manual' };
  const btn = document.getElementById(mapa[estado] || 'sf-todos');
  if (btn) btn.classList.add('active');
  renderConvList();
}

function renderConvList() {
  const el = document.getElementById('wa-conv-list');
  if (!el) return;

  let lista = conversaciones;
  if (_searchQuery) {
    lista = lista.filter(c => {
      const n = (c.nombre||c.telefono).toLowerCase();
      return n.includes(_searchQuery) || c.telefono.includes(_searchQuery);
    });
  }
  if (_tagFilter) {
    lista = lista.filter(c => (c.tags||[]).includes(_tagFilter));
  }
  if (_statusFilter === 'respondio') {
    lista = lista.filter(c => c.ultimo_rol === 'user');
  } else if (_statusFilter === 'sin-resp') {
    lista = lista.filter(c => c.ultimo_rol === 'assistant');
  } else if (_statusFilter === 'manual') {
    lista = lista.filter(c => c.modo_humano);
  }

  // Actualizar contador con el número filtrado
  const cnt = document.getElementById('wa-conv-count');
  const hayFiltro = _tagFilter || _searchQuery || _statusFilter;
  if (cnt) cnt.textContent = hayFiltro
    ? `${lista.length} / ${conversaciones.length}`
    : conversaciones.length;
  if (!lista.length) {
    el.innerHTML = `<div class="empty" style="padding:2.5rem 1rem;text-align:center">${hayFiltro?'Sin resultados':'Sin conversaciones'}</div>`;
    return;
  }
  el.innerHTML = lista.map(c => {
    const activo     = c.telefono===contactoActivo?' active':'';
    const nombre     = c.nombre||c.telefono;
    const inicial    = nombre.replace(/[^a-zA-Z0-9]/g,'').charAt(0).toUpperCase()||'#';
    const color      = avatarColor(c.telefono);
    const safeTel    = c.telefono.replace(/['"<>&]/g,'');
    const score      = c.score || 0;
    const prioridad  = c.prioridad || '\u26AA';
    const scoreColor = score>=70?'var(--red)':score>=40?'var(--orange)':'var(--txt3)';

    // Badge respondió / sin respuesta (según quién habló último)
    const respondio = c.ultimo_rol === 'user';
    const respBadge = respondio
      ? '<span class="wa-resp-badge respondio">&#10003; Respondi\u00f3</span>'
      : '<span class="wa-resp-badge sin-resp">Sin respuesta</span>';

    // Indicador "atendido por"
    let atendidoHtml = '';
    if (c.modo_humano) {
      atendidoHtml = '<span class="wa-atendido yo">&#128100; Atendido por ti</span>';
    } else if (c.ultimo_rol === 'assistant') {
      atendidoHtml = '<span class="wa-atendido valentina">&#129302; Valentina</span>';
    }

    // Badge modo
    const badge = c.modo_humano
      ? '<span class="modo-badge humano">Manual</span>'
      : '<span class="modo-badge bot">Bot</span>';
    const toggleBtn = c.modo_humano
      ? `<button class="wa-quick-liberar" title="Liberar IA" onclick="event.stopPropagation();quickLiberar('${safeTel}')">&#9646;&#9646;</button>`
      : `<button class="wa-quick-tomar"   title="Tomar lead" onclick="event.stopPropagation();quickTomar('${safeTel}',this)">&#128100;</button>`;

    // Preview: prefijo según quién habló último
    const previewPfx = c.ultimo_rol==='assistant' ? '🤖 ' : '👤 ';
    const preview = previewPfx + esc((c.ultimo_mensaje||'').slice(0,48));

    const TC=['#00D4FF','#c084fc','#f59e0b','#22c55e','#ef4444','#3b82f6','#f97316','#10b981'];
    const tc=t=>{let h=0;for(let i=0;i<t.length;i++)h=(Math.imul(31,h)+t.charCodeAt(i))|0;return TC[Math.abs(h)%TC.length];};
    const tagsHtml = c.tags && c.tags.length
      ? `<div class="lead-tags" style="margin-top:.2rem">${c.tags.slice(0,3).map(t=>`<span class="tag-chip-sm" style="background:${tc(t)}22;color:${tc(t)};border:1px solid ${tc(t)}55">${esc(t)}</span>`).join('')}${c.tags.length>3?`<span class="tag-chip-sm" style="background:rgba(255,255,255,.05);color:var(--txt3)">+${c.tags.length-3}</span>`:''}</div>`
      : '';
    const sinResp = fmtSinRespuesta(c.ultimo_user_ts);
    const sinRespHtml = sinResp
      ? `<span class="wa-sin-resp ${sinResp.clase}" title="Tiempo sin respuesta">${sinResp.texto}</span>`
      : '';
    return `
    <div class="wa-conv-item${activo}" id="wconv-${safeTel}" onclick="seleccionarContacto('${safeTel}')">
      <div class="wa-conv-avatar" style="background:${color}22;color:${color};border:1.5px solid ${color}44">${inicial}</div>
      <div class="wa-conv-info">
        <div class="wa-conv-name-row">
          <span class="wa-conv-priority" title="${score} pts">${prioridad}</span>
          <span class="wa-conv-name" title="${esc(nombre)}">${esc(nombre)}</span>
          <span class="wa-conv-time">${fmtTime(c.ultima_actividad)}</span>
        </div>
        <div class="wa-conv-preview-row">
          <span class="wa-conv-preview">${preview}</span>
          <span class="wa-conv-badges">${respBadge}${sinRespHtml}<span class="wa-conv-score" style="color:${scoreColor}">${score}</span>${badge}${toggleBtn}</span>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:.15rem">
          ${atendidoHtml}
          ${tagsHtml}
        </div>
      </div>
    </div>`;
  }).join('');
}

function flashConvWA(telefono) {
  const el = document.getElementById('wconv-'+telefono.replace(/['"<>&]/g,''));
  if (el) { el.style.background='rgba(0,212,255,.18)'; setTimeout(()=>{ el.style.background=''; },900); }
}

// =========================================================================
// SELECCIONAR CONTACTO
// =========================================================================
async function seleccionarContacto(telefono) {
  contactoActivo = telefono;
  renderConvList();
  const conv = conversaciones.find(c => c.telefono===telefono);
  const modoHumano = conv ? conv.modo_humano : false;

  document.getElementById('wa-empty').style.display  = 'none';
  const active = document.getElementById('wa-active');
  active.style.display = 'flex';
  active.style.flexDirection = 'column';

  // Mobile: ocultar sidebar, mostrar chat
  document.getElementById('wa-layout').classList.add('chat-abierto');

  // Empujar estado para que el botón atrás del SO vuelva al sidebar
  history.pushState({ view: 'chat', telefono }, '');

  renderChatHeader(telefono, modoHumano);
  if (modoHumano) {
    const input = document.getElementById('wa-input');
    if (input) input.focus();
  }

  // Resetear panel de notas y cargar las del contacto
  _notasData = []; _notasVerTodo = false;
  renderNotasChat();
  cargarNotasChat(telefono);

  await cargarMensajes(telefono);
}

function volverSidebar() {
  document.getElementById('wa-layout').classList.remove('chat-abierto');
  contactoActivo = null;
}

// ── Notas internas del lead en el chat ───────────────────────────────────────

let _notasData    = [];   // cache de notas del contacto activo
let _notasVerTodo = false;
const _NOTAS_VISIBLE = 3;

async function cargarNotasChat(telefono) {
  if (!telefono) return;
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(telefono) + '/notas-internas');
    if (!r.ok) return;
    const d = await r.json();
    _notasData    = d.notas || [];
    _notasVerTodo = false;
    renderNotasChat();
  } catch(e) { console.warn('cargarNotasChat error:', e); }
}

function renderNotasChat() {
  const body  = document.getElementById('wa-notas-body');
  const cnt   = document.getElementById('wa-notas-count');
  const panel = document.getElementById('wa-notas-panel');
  if (!body) return;

  const total = _notasData.length;
  if (cnt) {
    cnt.textContent = total;
    cnt.style.display = total ? 'inline' : 'none';
  }

  if (!total) {
    body.innerHTML = '<div style="font-size:.7rem;color:var(--txt3);padding:.4rem 0;text-align:center">Sin notas aún</div>';
    return;
  }

  const visibles = _notasVerTodo ? _notasData : _notasData.slice(0, _NOTAS_VISIBLE);
  body.innerHTML = visibles.map(n => `
    <div class="wa-nota-item" id="nota-${n.id}">
      <div style="flex:1">
        <div class="wa-nota-texto">${esc(n.contenido)}</div>
        <div class="wa-nota-meta">${n.created_at}</div>
      </div>
      <button class="wa-nota-del" title="Eliminar nota" onclick="eliminarNotaChat(${n.id})">&#10005;</button>
    </div>
  `).join('') + (total > _NOTAS_VISIBLE && !_notasVerTodo
    ? `<div class="wa-nota-ver-todas" onclick="_notasVerTodo=true;renderNotasChat()">Ver todas (${total - _NOTAS_VISIBLE} más)</div>`
    : total > _NOTAS_VISIBLE && _notasVerTodo
    ? `<div class="wa-nota-ver-todas" onclick="_notasVerTodo=false;renderNotasChat()">Ver menos</div>`
    : '');
}

function toggleNotasPanel() {
  const panel = document.getElementById('wa-notas-panel');
  if (panel) panel.classList.toggle('collapsed');
}

async function guardarNotaInterna() {
  if (!contactoActivo) return;
  const input = document.getElementById('wa-nota-input');
  const btn   = document.getElementById('wa-nota-send-btn');
  const texto = (input?.value || '').trim();
  if (!texto) return;
  btn.disabled = true;
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(contactoActivo) + '/notas-internas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ contenido: texto }),
    });
    if (r.ok) {
      const d = await r.json();
      _notasData.unshift({ id: d.id, contenido: texto, created_at: d.created_at });
      input.value = '';
      input.style.height = 'auto';
      renderNotasChat();
      // Expandir panel si está colapsado
      const panel = document.getElementById('wa-notas-panel');
      if (panel?.classList.contains('collapsed')) panel.classList.remove('collapsed');
    }
  } catch(e) { console.warn('guardarNotaInterna error:', e); }
  btn.disabled = false;
}

async function eliminarNotaChat(notaId) {
  try {
    const r = await fetch('/api/leads/notas-internas/' + notaId, { method: 'DELETE' });
    if (r.ok) {
      _notasData = _notasData.filter(n => n.id !== notaId);
      renderNotasChat();
    }
  } catch(e) { console.warn('eliminarNotaChat error:', e); }
}

// ── Tags inline en el chat ────────────────────────────────────────────────────

function renderChatTagChips(tags, safeTel) {
  return tags.map(t => {
    const c = _tagColorModal(t);
    const tEsc = esc(t).replace(/'/g,'&#39;');
    return `<span class="wa-tag-chip" style="background:${c}22;color:${c};border:1px solid ${c}44">
      ${esc(t)}<span class="wa-tag-x" onclick="chatQuitarTag('${safeTel}','${tEsc}')">&#10005;</span>
    </span>`;
  }).join('');
}

let _tagsPopoverAbierto = false;

function toggleTagsPopover(safeTel) {
  const existing = document.getElementById('wa-tags-popover');
  if (existing) { existing.remove(); _tagsPopoverAbierto = false; return; }

  const bar  = document.getElementById('wa-tags-bar');
  if (!bar) return;
  const conv = conversaciones.find(c => c.telefono === safeTel);
  const activos = conv ? (conv.tags || []) : [];

  const pop = document.createElement('div');
  pop.className = 'wa-tags-popover';
  pop.id = 'wa-tags-popover';
  pop.innerHTML = TAGS_PREDEFINIDOS.map(t => {
    const c   = _tagColorModal(t);
    const on  = activos.includes(t);
    return `<button class="wa-tag-pre ${on?'on':'off'}"
      style="background:${c}${on?'33':'11'};color:${c};border:1px solid ${c}${on?'66':'33'}"
      onclick="chatToggleTag('${safeTel}','${esc(t).replace(/'/g,'&#39;')}',this)">${esc(t)}</button>`;
  }).join('');

  bar.appendChild(pop);
  _tagsPopoverAbierto = true;

  // Cerrar al hacer click fuera
  setTimeout(() => {
    document.addEventListener('click', function _close(e) {
      if (!pop.contains(e.target) && e.target.id !== 'wa-tag-add-btn') {
        pop.remove(); _tagsPopoverAbierto = false;
        document.removeEventListener('click', _close);
      }
    });
  }, 50);
}

async function chatToggleTag(telefono, tag, btn) {
  const conv = conversaciones.find(c => c.telefono === telefono);
  if (!conv) return;
  let tags = [...(conv.tags || [])];
  if (tags.includes(tag)) {
    tags = tags.filter(t => t !== tag);
    btn && btn.classList.replace('on','off');
  } else {
    if (tags.length >= 20) return;
    tags.push(tag);
    btn && btn.classList.replace('off','on');
  }
  if (btn) {
    const c = _tagColorModal(tag);
    btn.style.background = tags.includes(tag) ? `${c}33` : `${c}11`;
    btn.style.borderColor = tags.includes(tag) ? `${c}66` : `${c}33`;
  }
  await _chatGuardarTags(telefono, tags);
}

async function chatQuitarTag(telefono, tag) {
  const conv = conversaciones.find(c => c.telefono === telefono);
  if (!conv) return;
  const tags = (conv.tags || []).filter(t => t !== tag);
  await _chatGuardarTags(telefono, tags);
}

async function _chatGuardarTags(telefono, tags) {
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(telefono) + '/tags', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tags }),
    });
    if (!r.ok) return;
    // Actualizar en memoria
    const conv = conversaciones.find(c => c.telefono === telefono);
    if (conv) conv.tags = tags;
    const lead = _leadsData.find(l => l.telefono === telefono);
    if (lead) lead.tags = tags;
    // Re-render chips (sin re-render completo del header)
    const bar = document.getElementById('wa-tags-bar');
    if (bar) {
      const safeTel = telefono.replace(/['"<>&]/g,'');
      // Reemplazar chips (todo excepto el popover y el botón +)
      [...bar.childNodes].forEach(n => {
        if (n.id !== 'wa-tags-popover' && n.id !== 'wa-tag-add-btn') n.remove();
      });
      bar.insertAdjacentHTML('afterbegin', renderChatTagChips(tags, safeTel));
      // Actualizar popover si está abierto
      const pop = document.getElementById('wa-tags-popover');
      if (pop) {
        pop.querySelectorAll('.wa-tag-pre').forEach(btn => {
          const t = btn.textContent.trim();
          const on = tags.includes(t);
          const c  = _tagColorModal(t);
          btn.className = `wa-tag-pre ${on?'on':'off'}`;
          btn.style.background  = on ? `${c}33` : `${c}11`;
          btn.style.borderColor = on ? `${c}66` : `${c}33`;
        });
      }
    }
  } catch(e) { console.warn('chatGuardarTags error:', e); }
}

function renderChatHeader(telefono, modoHumano) {
  const el = document.getElementById('wa-chat-hdr');
  if (!el) return;
  const conv      = conversaciones.find(c => c.telefono===telefono);
  const nombre    = conv ? conv.nombre : telefono;
  const color     = avatarColor(telefono);
  const inicial   = nombre.replace(/[^a-zA-Z0-9]/g,'').charAt(0).toUpperCase()||'#';
  const safeTel   = telefono.replace(/['"<>&]/g,'');
  const score     = conv ? (conv.score || 0) : 0;
  const prioridad = conv ? (conv.prioridad || '\u26AA') : '\u26AA';
  const estado    = conv ? (conv.estado || 'nuevo') : 'nuevo';
  const scoreColor= score>=70?'#ff4d6d':score>=40?'#f4a261':'#888';
  const hint   = document.getElementById('wa-human-hint');
  const banner = document.getElementById('wa-manual-banner');
  const input  = document.getElementById('wa-input');
  const btn    = document.getElementById('wa-send-btn');
  if (hint)   hint.style.display  = modoHumano ? 'block' : 'none';
  if (banner) banner.classList.toggle('visible', modoHumano);
  if (input) {
    input.disabled = false;
    input.classList.toggle('modo-humano', modoHumano);
    input.placeholder = modoHumano
      ? 'Modo manual — escribe y presiona Enter...'
      : 'Mensaje manual (bot sigue activo)...';
  }
  if (btn) { btn.disabled = false; btn.classList.toggle('modo-humano', modoHumano); }
  const tags   = conv ? (conv.tags || []) : [];
  const actions = modoHumano
    ? `<button class="btn-toggle-lead activo" onclick="liberarLeadChat('${safeTel}')">
         <span class="btn-toggle-icon">&#9646;&#9646;</span> Liberar IA &mdash; reactivar Valentina
       </button>`
    : `<button class="btn-toggle-lead" onclick="tomarLeadChat('${safeTel}',this)">
         <span class="btn-toggle-icon">&#128100;</span> Tomar Lead
       </button>`;
  el.innerHTML = `
    <button id="btn-wa-back" onclick="history.back()" title="Volver">&#8592;</button>
    <div class="wa-chat-hdr-avatar" style="background:${color}22;color:${color};border:1.5px solid ${color}55">${inicial}</div>
    <div class="wa-chat-hdr-info">
      <div class="wa-chat-hdr-name">${esc(nombre)}</div>
      <div class="wa-chat-hdr-sub">${prioridad} ${estado} &middot; <span style="color:${scoreColor};font-weight:700">${score} pts</span> &middot; +${safeTel}</div>
      <div class="wa-tags-bar" id="wa-tags-bar" style="position:relative">
        ${renderChatTagChips(tags, safeTel)}
        <button class="wa-tag-add-btn" id="wa-tag-add-btn" onclick="toggleTagsPopover('${safeTel}')">&#43; tag</button>
      </div>
    </div>
    <div class="wa-chat-hdr-actions">${actions}</div>`;
}

// Acciones rápidas desde el sidebar (sin abrir el chat)
async function quickTomar(telefono, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    const r = await fetch('/api/leads/'+encodeURIComponent(telefono)+'/tomar', { method:'POST' });
    if (r.ok) {
      await actualizarConversaciones();
      // Si el chat está abierto para este contacto, actualizar el header también
      if (contactoActivo === telefono) renderChatHeader(telefono, true);
    }
  } catch(_) {}
  btn.disabled = false;
}
async function quickLiberar(telefono) {
  try {
    const r = await fetch('/api/leads/'+encodeURIComponent(telefono)+'/liberar', { method:'POST' });
    if (r.ok) {
      await actualizarConversaciones();
      if (contactoActivo === telefono) renderChatHeader(telefono, false);
    }
  } catch(_) {}
}

async function tomarLeadChat(telefono, btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="btn-toggle-icon">&#8987;</span> Tomando...';
  try {
    const r = await fetch('/api/leads/'+encodeURIComponent(telefono)+'/tomar', { method:'POST' });
    if (r.ok) {
      renderChatHeader(telefono, true);
      // foco al input para que el agente pueda escribir de inmediato
      const inp = document.getElementById('wa-input');
      if (inp) { inp.disabled = false; inp.focus(); }
    } else {
      btn.disabled = false;
      btn.innerHTML = '<span class="btn-toggle-icon">&#128100;</span> Tomar Lead';
    }
  } catch(_) {
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-toggle-icon">&#128100;</span> Tomar Lead';
  }
}
async function liberarLeadChat(telefono) {
  const r = await fetch('/api/leads/'+encodeURIComponent(telefono)+'/liberar', { method:'POST' });
  if (r.ok) renderChatHeader(telefono, false);
}

// =========================================================================
// MENSAJES
// =========================================================================
async function cargarMensajes(telefono) {
  _chatUltimoTS = '';
  const el = document.getElementById('wa-messages');
  el.innerHTML = '<div class="empty" style="margin:auto;padding:2rem;text-align:center">Cargando...</div>';
  try {
    const r = await fetch('/api/chat/'+encodeURIComponent(telefono));
    if (!r.ok) throw new Error('HTTP '+r.status);
    const d = await r.json();
    if (!d.mensajes || !d.mensajes.length) {
      el.innerHTML = '<div class="empty" style="margin:auto;padding:2rem;text-align:center">Sin mensajes a\u00fan</div>'; return;
    }
    let html = ''; let lastLabel = '';
    for (const m of d.mensajes) {
      const lbl = fmtDateLabel(m.timestamp);
      if (lbl && lbl !== lastLabel) { html += `<div class="wa-date-sep">${lbl}</div>`; lastLabel = lbl; }
      html += waBurbuja(m.role, m.content, m.timestamp, m.estado_lead || '');
    }
    el.innerHTML = html;
    _chatUltimoTS = d.mensajes[d.mensajes.length-1].timestamp || '';
    waScrollAbajo();
  } catch(err) {
    el.innerHTML = `<div class="empty" style="margin:auto;padding:2rem;text-align:center">Error al cargar (${err.message})</div>`;
  }
}

function waBurbuja(role, content, ts, estadoLead) {
  // owner = mensaje enviado por el operador humano (yo) desde el dashboard
  const isOwner = role === 'assistant' && estadoLead === 'modo_humano';
  const wrapCls = isOwner ? 'owner' : role;
  const bubbleCls = isOwner ? 'owner' : role;
  const senderLabel = role === 'user'
    ? '<div class="wa-bubble-sender">&#128100; Cliente</div>'
    : isOwner
      ? '<div class="wa-bubble-sender">&#128100; Yo</div>'
      : '<div class="wa-bubble-sender">&#129302; Valentina</div>';
  return `<div class="wa-bubble-wrap ${wrapCls}">${senderLabel}<div class="wa-bubble ${bubbleCls}">${esc(content)}</div><div class="wa-bubble-time">${fmtTime(ts)}</div></div>`;
}

function waAgregarBurbuja(role, content, ts, estadoLead) {
  const el = document.getElementById('wa-messages');
  const empty = el.querySelector('.empty');
  if (empty) empty.remove();
  const wrap = document.createElement('div');
  wrap.innerHTML = waBurbuja(role, content, ts, estadoLead || '');
  el.appendChild(wrap.firstElementChild);
}

function waScrollAbajo() {
  const el = document.getElementById('wa-messages');
  if (el) { el.scrollTop = el.scrollHeight; }
}

// =========================================================================
// ENVIAR MENSAJE
// =========================================================================
async function waSend() {
  if (!contactoActivo) return;
  const input = document.getElementById('wa-input');
  const texto = input.value.trim();
  if (!texto) return;
  const btn = document.getElementById('wa-send-btn');
  btn.disabled = true; input.disabled = true;
  const tsLocal = new Date().toISOString();
  waAgregarBurbuja('assistant', texto, tsLocal, 'modo_humano');
  waScrollAbajo();
  _chatUltimoTS = tsLocal;
  input.value = ''; input.style.height = 'auto';
  try {
    const r = await fetch('/api/chat/'+encodeURIComponent(contactoActivo)+'/send', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ mensaje: texto }),
    });
    const data = await r.json().catch(()=>({}));
    if (!r.ok || data.ok === false) {
      const burbs = document.querySelectorAll('#wa-messages .wa-bubble.assistant');
      if (burbs.length) { const last=burbs[burbs.length-1]; last.classList.add('error'); last.title='Error WA: '+(data.error||'sin detalle'); }
      if (data.error) console.warn('WA error:', data.error);
    }
  } catch(_) { alert('Error de conexi\u00f3n al enviar'); }
  finally { btn.disabled=false; input.disabled=false; input.focus(); }
}

// =========================================================================
// MODAL — Historial completo
// =========================================================================
async function abrirChatCompleto(telefono, nombre) {
  const modal = document.getElementById('modal-chat');
  const msgsEl = document.getElementById('modal-messages');
  document.getElementById('modal-nombre').textContent = nombre || telefono;
  document.getElementById('modal-tel').textContent = '+' + telefono;
  msgsEl.innerHTML = '<div class="empty">Cargando...</div>';
  modal.style.display = 'flex';
  try {
    const r = await fetch('/api/chat/'+encodeURIComponent(telefono));
    if (!r.ok) throw new Error('HTTP '+r.status);
    const d = await r.json();
    if (!d.mensajes||!d.mensajes.length) { msgsEl.innerHTML='<div class="empty">Sin mensajes</div>'; return; }
    msgsEl.innerHTML = d.mensajes.map(m => modalBurbuja(m.role, m.content, m.timestamp, m.estado_lead||'')).join('');
    msgsEl.scrollTop = msgsEl.scrollHeight;
  } catch(err) { msgsEl.innerHTML=`<div class="empty">Error (${err.message})</div>`; }
}
function cerrarModal() { document.getElementById('modal-chat').style.display = 'none'; }
function modalBurbuja(role, content, ts, estadoLead) {
  const isOwner = role === 'assistant' && estadoLead === 'modo_humano';
  const isBot   = role === 'assistant' && !isOwner;
  const align   = role === 'user' ? 'flex-start' : 'flex-end';
  const bg      = isOwner ? 'rgba(34,197,94,0.12)'  : isBot ? 'rgba(0,212,255,0.1)' : 'rgba(255,255,255,0.06)';
  const bdr     = isOwner ? '1px solid rgba(34,197,94,0.25)' : isBot ? '1px solid rgba(0,212,255,0.22)' : '1px solid rgba(255,255,255,0.1)';
  const br      = role === 'user' ? '16px 16px 16px 4px' : '16px 16px 4px 16px';
  const label   = isOwner ? '&#128100; Yo' : isBot ? '&#129302; Valentina' : '&#128100; Cliente';
  const lclr    = isOwner ? 'rgba(34,197,94,.8)' : isBot ? 'var(--neon)' : 'rgba(255,255,255,.4)';
  return `<div style="display:flex;flex-direction:column;align-items:${align};gap:.2rem">
    <span style="font-size:.58rem;color:${lclr};letter-spacing:.06em;text-transform:uppercase;font-weight:600">${label}</span>
    <div style="max-width:78%;background:${bg};border:${bdr};border-radius:${br};padding:.65rem 1rem;font-size:.85rem;line-height:1.5;word-break:break-word;white-space:pre-wrap">${esc(content)}</div>
    <span style="font-size:.58rem;color:rgba(255,255,255,.2);font-family:monospace">${fmtTime(ts)}</span>
  </div>`;
}

// =========================================================================
// KPI MODALS
// =========================================================================
const KPI_CFG = {
  total:     { title: 'TOTAL LEADS',           sub: 'Todos los registros activos',          url: '/api/kpi/leads' },
  calientes: { title: 'LEADS CALIENTES',        sub: 'Estado: caliente + listo para cierre', url: '/api/kpi/leads-calientes' },
  cerrados:  { title: 'CONVERSIONES',           sub: 'Leads cerrados',                       url: '/api/kpi/conversiones' },
  score:     { title: 'SCORE PROMEDIO',         sub: 'Top 10 leads por puntuaci\u00f3n',     url: '/api/kpi/top-score' },
  msgs:      { title: 'MENSAJES HOY',           sub: 'Actividad del d\u00eda en CRM',        url: '/api/kpi/mensajes-hoy' },
  followups: { title: 'FOLLOW-UPS PENDIENTES',  sub: 'Programados y sin enviar',             url: '/api/kpi/followups' },
};
const ESTADO_CLR = {
  nuevo:'#888', contactado:'#00d4ff', interesado:'#7b68ee', tibio:'#ffa500',
  caliente:'#ff2233', direccion_obtenida:'#00ff88', listo_para_cierre:'#ff2233',
  cerrado:'#00ff88', modo_humano:'#ffa500',
};
function estadoBadge(e) {
  const c = ESTADO_CLR[e]||'#888';
  return `<span style="display:inline-block;padding:.12rem .45rem;border-radius:4px;font-size:.55rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;background:${c}22;color:${c};border:1px solid ${c}44">${e||'—'}</span>`;
}
function scoreBadge(s) {
  s = s||0;
  const c = s>=70?'var(--red)':s>=40?'var(--orange)':'var(--txt2)';
  return `<span style="color:${c};font-weight:700;font-size:.9rem">${s}</span><span style="color:var(--txt3);font-size:.58rem">pt</span>`;
}
function kpiRow(tel, nombre, left, right) {
  const st = tel.replace(/['"<>&]/g,'');
  return `<div style="display:flex;align-items:center;gap:.75rem;padding:.75rem .9rem;background:rgba(255,255,255,.025);border:1px solid var(--border);border-radius:10px;transition:background .15s" onmouseover="this.style.background='rgba(0,212,255,.04)'" onmouseout="this.style.background='rgba(255,255,255,.025)'">
  <div style="flex:1;min-width:0">
    <div style="font-size:.75rem;font-weight:600;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(nombre)}</div>
    <div style="font-size:.6rem;color:var(--txt3);font-family:monospace;margin-top:.1rem">+${st}</div>
    <div style="margin-top:.3rem">${left}</div>
  </div>
  <div style="display:flex;flex-direction:column;align-items:flex-end;gap:.35rem;flex-shrink:0">
    ${right}
    <button onclick="irAlChatDesdeModal('${st}')" style="font-size:.6rem;padding:.22rem .55rem;background:rgba(0,212,255,.08);border:1px solid rgba(0,212,255,.3);color:var(--neon);border-radius:6px;cursor:pointer;font-family:'Space Grotesk',sans-serif;transition:background .15s" onmouseover="this.style.background='rgba(0,212,255,.18)'" onmouseout="this.style.background='rgba(0,212,255,.08)'">&#8594; Chat</button>
  </div>
</div>`;
}
function renderKpiItems(tipo, items) {
  if (!items || !items.length) return '<div class="empty">Sin registros a\u00fan</div>';
  if (tipo==='total'||tipo==='calientes'||tipo==='cerrados') {
    return items.map(it => {
      const left = estadoBadge(it.estado);
      const extra = (it.direccion && it.direccion!=='—') ? `<div style="font-size:.58rem;color:var(--txt3);max-width:130px;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(it.direccion)}</div>` : '';
      return kpiRow(it.telefono, it.nombre, left, scoreBadge(it.score)+extra);
    }).join('');
  }
  if (tipo==='score') {
    return items.map((it,i) => {
      const bar = `<div style="margin-top:.3rem"><div style="height:3px;border-radius:2px;background:rgba(255,255,255,.08)"><div style="height:100%;width:${it.pct}%;background:linear-gradient(90deg,var(--neon),var(--red));transition:width .6s .1s ease"></div></div></div>`;
      return kpiRow(it.telefono, it.nombre, estadoBadge(it.estado)+bar, `<span style="font-size:.65rem;color:var(--txt3)">#${i+1}</span>${scoreBadge(it.score)}`);
    }).join('');
  }
  if (tipo==='msgs') {
    return items.map(it => {
      const isBot = it.rol==='assistant';
      const tag = isBot
        ? `<span style="font-size:.52rem;color:var(--neon);font-weight:700;letter-spacing:.06em">BOT</span>`
        : `<span style="font-size:.52rem;color:var(--txt2);font-weight:700;letter-spacing:.06em">USER</span>`;
      const preview = (it.mensaje||'').slice(0,80)+(it.mensaje&&it.mensaje.length>80?'\u2026':'');
      const left = `<div style="font-size:.65rem;color:var(--txt2);margin-top:.2rem;line-height:1.4">${esc(preview)}</div>`;
      return kpiRow(it.telefono, it.nombre, left, tag+`<div style="font-size:.58rem;color:var(--txt3);font-family:monospace">${fmtTime(it.ts)}</div>`);
    }).join('');
  }
  if (tipo==='followups') {
    return items.map(it => {
      const tipoBadge = `<span style="display:inline-block;padding:.1rem .42rem;border-radius:4px;font-size:.55rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase;background:rgba(255,165,0,.1);color:var(--orange);border:1px solid rgba(255,165,0,.25)">${esc(it.tipo)}</span>`;
      const preview = (it.mensaje||'').slice(0,70)+(it.mensaje&&it.mensaje.length>70?'\u2026':'');
      const left = tipoBadge+`<div style="font-size:.63rem;color:var(--txt2);margin-top:.28rem;line-height:1.4">${esc(preview)}</div>`;
      return kpiRow(it.telefono, it.nombre, left, `<div style="font-size:.58rem;color:var(--txt3);font-family:monospace;text-align:right">${fmtTime(it.programado_para)}</div>`);
    }).join('');
  }
  return '<div class="empty">Tipo desconocido</div>';
}
async function abrirKpiModal(tipo) {
  const cfg = KPI_CFG[tipo]; if (!cfg) return;
  document.getElementById('kpi-modal-title').textContent = cfg.title;
  document.getElementById('kpi-modal-sub').textContent   = cfg.sub;
  const body = document.getElementById('kpi-modal-body');
  body.innerHTML = '<div class="empty">Cargando\u2026</div>';
  document.getElementById('modal-kpi').style.display = 'flex';
  try {
    const r = await fetch(cfg.url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    body.innerHTML = renderKpiItems(tipo, d.items);
    // trigger score bar animation
    if (tipo==='score') setTimeout(()=>{}, 50);
  } catch(e) {
    body.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}
function cerrarKpiModal() { document.getElementById('modal-kpi').style.display = 'none'; }
function irAlChatDesdeModal(tel) {
  cerrarKpiModal();
  const pc = document.getElementById('panel-chat');
  if (pc.style.display !== 'flex') {
    _abrirLivePanel();
    history.pushState({ view: 'live' }, '');
  }
  setTimeout(() => seleccionarContacto(tel), 280);
}

// =========================================================================
// VIEWPORT HEIGHT
// --app-h: usa 100dvh nativo si el browser lo soporta (iOS 16+, Chrome 108+).
// Fallback JS para browsers más viejos.
// --header-h: siempre se mide del DOM para ser exacto.
// =========================================================================
const _dvhSupported = CSS.supports('height', '100dvh');

function setAppHeight() {
  if (!_dvhSupported) {
    // Fallback para browsers sin dvh: usar visualViewport para altura real visible
    const h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
    document.documentElement.style.setProperty('--app-h', h + 'px');
  }
  // Con flexbox ya no necesitamos calcular --header-h manualmente
}
setAppHeight();
window.addEventListener('resize', setAppHeight);
window.addEventListener('load', () => requestAnimationFrame(setAppHeight));
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', setAppHeight);
}

// =========================================================================
// HISTORY — botón atrás del SO navega dentro del dashboard
// =========================================================================
// Estado inicial: métricas (replaceState para no añadir entrada extra)
history.replaceState({ view: 'metrics' }, '');

window.addEventListener('popstate', function(e) {
  const state = e.state;
  // Sin estado o estado base = el usuario intentó salir de la app.
  // Empujar un nuevo estado metrics para que nunca pueda salir con el botón atrás.
  if (!state || !state.view || state.view === 'metrics') {
    history.pushState({ view: 'metrics' }, '');
    const pc = document.getElementById('panel-chat');
    if (pc && pc.style.display === 'flex') _cerrarLivePanel();
    return;
  }
  if (state.view === 'chat') {
    // volviendo de chat → mostrar sidebar del Live Chat
    volverSidebar();
  } else if (state.view === 'live') {
    // volviendo de live → si hay chat abierto en móvil, cerrar chat
    if (contactoActivo) volverSidebar();
    else {
      const pc = document.getElementById('panel-chat');
      if (pc && pc.style.display === 'flex') _cerrarLivePanel();
    }
  }
});

// =========================================================================
// INIT
// =========================================================================
(function init() {
  document.addEventListener('keydown', e => { if (e.key==='Escape') { cerrarModal(); cerrarKpiModal(); } });
  const input = document.getElementById('wa-input');
  if (input) {
    input.addEventListener('keydown', e => { if (e.key==='Enter'&&!e.shiftKey) { e.preventDefault(); waSend(); } });
    input.addEventListener('input', () => { input.style.height='auto'; input.style.height=Math.min(input.scrollHeight,120)+'px'; });
  }
  conectarSSE();
  actualizarConversaciones();
  setInterval(actualizarConversaciones, 30_000);
  // Re-renderizar la lista cada 60s para actualizar los indicadores de tiempo sin respuesta
  setInterval(renderConvList, 60_000);
})();

// =========================================================================
// CAMPAÑAS
// =========================================================================
const CPN_ESTADO_COLOR = {
  borrador:   '#7f8c8d',
  enviando:   '#f59e0b',
  completada: '#00FF88',
  cancelada:  '#555',
  error:      '#FF2233',
};
const CPN_ESTADO_LABEL = {
  borrador:   'Borrador',
  enviando:   'Enviando...',
  completada: 'Completada',
  cancelada:  'Cancelada',
  error:      'Error',
};

// =========================================================================
// SIN RESPUESTA
// =========================================================================
let _srData = [];

async function actualizarSinRespuesta() {
  const tbody = document.getElementById('sr-tbody');
  const badge = document.getElementById('sr-total-badge');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="7" class="sr-loading">Cargando...</td></tr>';
  try {
    const r = await fetch('/api/sin-respuesta');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    _srData = d.leads || [];
    if (badge) badge.textContent = `${_srData.length} lead${_srData.length !== 1 ? 's' : ''}`;
    renderSinRespuesta();
  } catch(err) {
    tbody.innerHTML = `<tr><td colspan="7" class="sr-empty">Error al cargar (${err.message})</td></tr>`;
  }
}

function renderSinRespuesta() {
  const tbody = document.getElementById('sr-tbody');
  if (!tbody) return;
  if (!_srData.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="sr-empty">&#10003; Sin leads pendientes — todos han respondido</td></tr>';
    return;
  }
  tbody.innerHTML = _srData.map((lead, idx) => {
    const safeTel  = lead.telefono.replace(/['"<>&]/g, '');
    const nombre   = esc(lead.nombre || lead.telefono);
    const ciudad   = esc(lead.ciudad || '—');
    const dias     = parseFloat(lead.dias_sin_respuesta || 0);
    const diasCls  = dias >= 7 ? 'urgent' : dias >= 3 ? 'warn' : 'ok';
    const diasTxt  = dias < 1 ? 'Hoy' : dias < 2 ? '1 día' : `${Math.floor(dias)} días`;
    const fechaEnv = lead.fecha_envio ? fmtTime(lead.fecha_envio) : '—';
    const promo    = (lead.subproducto || '').trim();
    return `<tr id="sr-row-${idx}">
      <td><span style="font-weight:600">${nombre}</span></td>
      <td><span class="sr-phone">+${safeTel}</span></td>
      <td>${ciudad}</td>
      <td style="color:var(--txt2);font-size:.76rem;font-family:monospace">${fechaEnv}</td>
      <td><span class="sr-days ${diasCls}">&#9200; ${diasTxt}</span></td>
      <td>${promo ? `<span class="sr-promo">${esc(promo)}</span>` : '<span class="sr-promo empty">—</span>'}</td>
      <td>
        <div class="sr-actions">
          <button class="sr-btn reactivar"     onclick="srMostrarModal('${safeTel}',${idx})">&#128172; Reactivar</button>
          <button class="sr-btn incontactable" onclick="srIncontactable('${safeTel}',${idx})">&#10005; Incontactable</button>
          <button class="sr-btn ver-chat"      onclick="srVerChat('${safeTel}')">&#128172; Ver chat</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}


function srMostrarModal(telefono, idx) {
  const lead = _srData[idx];
  const nombre = lead ? (lead.nombre || telefono) : telefono;
  const nombreFmt = nombre.split(' ')[0];
  const msgDefault = `Hola ${nombreFmt}! Te escribo nuevamente desde Conexión Sin Límites. ¿Tienes un momento para que podamos ayudarte? 😊`;
  
  // Crear modal si no existe
  let modal = document.getElementById('modal-reactivar');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'modal-reactivar';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.8);display:flex;align-items:center;justify-content:center;z-index:9999;';
    modal.innerHTML = `
      <div style="background:#161616;border:1px solid #333;border-radius:14px;padding:1.5rem;width:90%;max-width:500px;">
        <h3 style="color:#00D4FF;margin-bottom:1rem;font-family:'Space Grotesk',sans-serif;">💬 Reactivar contacto</h3>
        <textarea id="modal-reactivar-msg" style="width:100%;height:120px;background:#0a0a0a;border:1px solid #333;border-radius:8px;padding:.75rem;color:#f5f5f5;font-size:.85rem;resize:vertical;"></textarea>
        <div style="display:flex;gap:.75rem;margin-top:1rem;justify-content:flex-end;">
          <button onclick="document.getElementById('modal-reactivar').style.display='none'" style="padding:.5rem 1rem;border-radius:8px;border:1px solid #444;background:transparent;color:#888;cursor:pointer;">Cancelar</button>
          <button onclick="srEnviarReactivacion()" style="padding:.5rem 1.25rem;border-radius:8px;border:none;background:#00D4FF;color:#000;font-weight:700;cursor:pointer;">Enviar</button>
          <button onclick="srSoloVerChat()" style="padding:.5rem 1.25rem;border-radius:8px;border:none;background:#333;color:#f5f5f5;cursor:pointer;">Solo ver chat</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
  }
  
  modal._telefono = telefono;
  modal._idx = idx;
  document.getElementById('modal-reactivar-msg').value = msgDefault;
  modal.style.display = 'flex';
}

function srEnviarReactivacion() {
  const modal = document.getElementById('modal-reactivar');
  const telefono = modal._telefono;
  const idx = modal._idx;
  const mensaje = document.getElementById('modal-reactivar-msg').value.trim();
  modal.style.display = 'none';
  srReactivar(telefono, idx, mensaje);
}

function srSoloVerChat() {
  const modal = document.getElementById('modal-reactivar');
  const telefono = modal._telefono;
  modal.style.display = 'none';
  srVerChat(telefono);
}

async function srReactivar(telefono, idx, mensaje = null) {
  const row = document.getElementById('sr-row-' + idx);
  const btn = row ? row.querySelector('.sr-btn.reactivar') : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Enviando...'; }
  try {
    const r = await fetch('/api/sin-respuesta/' + encodeURIComponent(telefono) + '/reactivar', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    const d = await r.json();
    if (d.ok) {
      if (btn) { btn.textContent = '\u2713 Enviado'; btn.style.background = 'rgba(34,197,94,.15)'; btn.style.color = '#4ade80'; }
    } else {
      if (btn) { btn.disabled = false; btn.textContent = 'Reactivar'; alert('Error: ' + (d.error || 'sin detalle')); }
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Reactivar'; }
  }
}

async function srIncontactable(telefono, idx) {
  if (!confirm(`¿Marcar +${telefono} como Incontactable? Se moverá a "seguimiento" y no aparecerá en esta lista.`)) return;
  const row = document.getElementById('sr-row-' + idx);
  const btn = row ? row.querySelector('.sr-btn.incontactable') : null;
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  try {
    const r = await fetch('/api/sin-respuesta/' + encodeURIComponent(telefono) + '/incontactable', { method: 'POST' });
    const d = await r.json();
    if (d.ok && row) {
      row.style.opacity = '0'; row.style.transition = 'opacity .35s';
      setTimeout(() => {
        _srData = _srData.filter(l => l.telefono !== telefono);
        const badge = document.getElementById('sr-total-badge');
        if (badge) badge.textContent = `${_srData.length} lead${_srData.length !== 1 ? 's' : ''}`;
        renderSinRespuesta();
      }, 380);
    } else {
      if (btn) { btn.disabled = false; btn.textContent = '\u2715 Incontactable'; }
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '\u2715 Incontactable'; }
  }
}

function srVerChat(telefono) {
  _abrirLivePanel();
  history.pushState({ view: 'live' }, '');
  // Esperar a que la lista cargue, luego abrir el contacto
  const intentar = (intentos) => {
    const conv = conversaciones.find(c => c.telefono === telefono || c.telefono.replace(/\D/g,'') === telefono.replace(/\D/g,''));
    if (conv) {
      seleccionarContacto(conv.telefono);
    } else if (intentos > 0) {
      setTimeout(() => intentar(intentos - 1), 400);
    }
  };
  setTimeout(() => intentar(5), 350);
}

let _cpnPolling = null;

async function actualizarCampanasList() {
  const r = await fetch('/api/campanas');
  if (!r.ok) return;
  const d = await r.json();
  const lista = d.campanas || [];
  const el = document.getElementById('cpn-list');
  const cnt = document.getElementById('cpn-count');
  if (cnt) cnt.textContent = lista.length + ' campaña' + (lista.length !== 1 ? 's' : '');

  if (!lista.length) {
    el.innerHTML = `<div class="empty" style="padding:2.5rem;text-align:center">
      <div style="font-size:2.5rem;opacity:.2;margin-bottom:.75rem">&#128226;</div>
      <div style="font-size:.82rem;color:var(--txt2)">Aún no hay campañas</div>
      <div style="font-size:.7rem;color:var(--txt3);margin-top:.3rem">Haz clic en "+ Nueva Campaña" para empezar</div>
    </div>`;
    _detenerPolling();
    return;
  }

  const hayEnviando = lista.some(c => c.estado === 'enviando');
  if (hayEnviando) _iniciarPolling(); else _detenerPolling();

  el.innerHTML = lista.map(c => {
    const color = CPN_ESTADO_COLOR[c.estado] || '#888';
    const label = CPN_ESTADO_LABEL[c.estado] || c.estado;
    const total = c.total_destinatarios || 0;
    const env   = c.total_enviados || 0;
    const fail  = c.total_fallidos || 0;
    const pct   = total > 0 ? Math.round(env / total * 100) : 0;
    const fechaStr = c.fecha_envio
      ? fmtDateLabel(c.fecha_envio) + ' ' + fmtTime(c.fecha_envio)
      : fmtDateLabel(c.fecha_creacion) + ' (creada)';
    const filtroBadges = [
      c.filtro_tag    ? `tag: ${esc(c.filtro_tag)}`     : '',
      c.filtro_estado ? `estado: ${esc(c.filtro_estado)}`:'',
      c.filtro_score_min > 0 ? `score ≥ ${c.filtro_score_min}` : '',
      c.filtro_comuna ? `${esc(c.filtro_comuna)}`        : '',
    ].filter(Boolean).join(' · ') || 'Sin filtros';
    const enviarBtn = c.estado === 'borrador'
      ? `<button class="cpn-action-btn enviar" onclick="event.stopPropagation();confirmarEnvio(${c.id},'${esc(c.nombre)}',${total})">&#9658; Enviar</button>`
      : '';
    const pausarBtn = c.estado === 'enviando'
      ? `<button class="cpn-action-btn" style="background:rgba(251,191,36,.15);border-color:rgba(251,191,36,.4);color:#fbbf24" onclick="event.stopPropagation();pausarCampana(${c.id})">&#9646;&#9646; Pausar</button>`
      : '';
    const reanudarBtn = c.estado === 'pausada'
      ? `<button class="cpn-action-btn enviar" onclick="event.stopPropagation();reanudarCampana(${c.id})">&#9654; Reanudar</button>`
      : '';
    const progHtml = c.estado === 'completada' || c.estado === 'enviando'
      ? `<div class="cpn-progress"><div class="cpn-progress-fill" style="width:${pct}%;background:${color}"></div></div>`
      : '';
    return `
    <div class="cpn-row" onclick="abrirDetalleCampana(${c.id})" style="border-left-color:${color}">
      <div class="cpn-row-estado" style="background:${color};box-shadow:0 0 6px ${color}66${c.estado==='enviando'?';animation:pulse 1.2s infinite':''}"></div>
      <div class="cpn-row-info">
        <div class="cpn-row-nombre">${esc(c.nombre)}</div>
        <div class="cpn-row-meta">${fechaStr} &nbsp;·&nbsp; ${filtroBadges}</div>
        ${progHtml}
      </div>
      <div style="text-align:right;min-width:55px">
        <div class="cpn-metrics-num">${total}</div>
        <div class="cpn-metrics-sub">destinatarios</div>
      </div>
      <div style="text-align:right;min-width:60px">
        <div style="font-size:.75rem;font-weight:700;color:${c.estado==='completada'?'var(--green)':color}">${env}<span style="color:var(--txt3);font-weight:400;font-size:.62rem"> env</span></div>
        ${fail > 0 ? `<div style="font-size:.68rem;color:var(--red)">${fail} err</div>` : ''}
      </div>
      <div style="display:flex;flex-direction:column;gap:.3rem;align-items:flex-end">
        <span class="cpn-estado-badge" style="background:${color}22;color:${color};border:1px solid ${color}44">${label}</span>
        ${enviarBtn}${pausarBtn}${reanudarBtn}
        <div id="progreso-${c.id}"></div>
      </div>
    </div>`;
  }).join('');
}

function _iniciarPolling() {
  if (_cpnPolling) return;
  _cpnPolling = setInterval(actualizarCampanasList, 4000);
}
function _detenerPolling() {
  if (_cpnPolling) { clearInterval(_cpnPolling); _cpnPolling = null; }
}

async function confirmarEnvio(id, nombre, total) {
  if (!confirm(`¿Enviar la campaña "${nombre}" a ${total} destinatario${total!==1?'s':''}?\n\nEsta acción no se puede deshacer.`)) return;
  const r = await fetch(`/api/campanas/${id}/enviar`, { method: 'POST' });
  if (r.ok) {
    await actualizarCampanasList();
    iniciarPollingProgreso(id);
  } else {
    const d = await r.json().catch(() => ({}));
    alert('Error al enviar: ' + (d.detail || 'desconocido'));
  }
}

// ── Modal nueva campaña ────────────────────────────────────────────────────
function abrirNuevaCampana() {
  document.getElementById('modal-nueva-campana').style.display = 'flex';
  document.getElementById('ncpn-nombre').value  = '';
  document.getElementById('ncpn-mensaje').value = '';
  document.getElementById('ncpn-tag').value     = '';
  document.getElementById('ncpn-estado').value  = '';
  document.getElementById('ncpn-score').value   = '';
  document.getElementById('ncpn-comuna').value  = '';
  document.getElementById('ncpn-desde').value   = '';
  document.getElementById('ncpn-hasta').value   = '';
  document.getElementById('ncpn-preview-box').innerHTML = '<div class="ncpn-preview-sub">Haz clic en "Ver destinatarios" para previsualizar</div>';
  document.getElementById('ncpn-save-btn').disabled = false;
  document.getElementById('ncpn-save-btn').textContent = 'Crear campaña';
}

function cerrarNuevaCampana() {
  document.getElementById('modal-nueva-campana').style.display = 'none';
}

async function previewDestinatarios() {
  const params = new URLSearchParams({
    tag:       document.getElementById('ncpn-tag').value,
    estado:    document.getElementById('ncpn-estado').value,
    score_min: document.getElementById('ncpn-score').value || 0,
    comuna:    document.getElementById('ncpn-comuna').value,
    desde:     document.getElementById('ncpn-desde').value,
    hasta:     document.getElementById('ncpn-hasta').value,
  });
  const btn = document.getElementById('ncpn-preview-btn');
  btn.disabled = true; btn.textContent = '...';
  try {
    const r = await fetch('/api/campanas/preview?' + params);
    const d = await r.json();
    const box = document.getElementById('ncpn-preview-box');
    const muestraHtml = (d.muestra || []).map(m =>
      `<div class="ncpn-preview-item">
        <span style="color:${CPN_ESTADO_COLOR[m.estado]||'#888'}">${m.estado}</span>
        <span>${esc(m.nombre)}</span>
        <span style="color:var(--txt3);font-family:monospace">${m.telefono}</span>
        <span style="color:${m.score>=70?'var(--red)':m.score>=40?'var(--orange)':'var(--txt3)'}">${m.score}pts</span>
      </div>`
    ).join('');
    box.innerHTML = `
      <div class="ncpn-preview-num">${d.total}</div>
      <div class="ncpn-preview-sub">lead${d.total!==1?'s':''} recibirá${d.total!==1?'n':''} esta campaña</div>
      ${d.muestra.length ? '<div class="ncpn-preview-list">' + muestraHtml + (d.total > 5 ? `<div class="ncpn-preview-item" style="color:var(--txt3)">+ ${d.total - 5} más...</div>` : '') + '</div>' : ''}`;
  } catch(e) {
    document.getElementById('ncpn-preview-box').innerHTML = '<div class="ncpn-preview-sub" style="color:var(--red)">Error al cargar preview</div>';
  }
  btn.disabled = false; btn.textContent = '&#128065; Ver destinatarios';
}

async function crearCampana() {
  const nombre  = document.getElementById('ncpn-nombre').value.trim();
  const mensaje = document.getElementById('ncpn-mensaje').value.trim();
  if (!nombre || !mensaje) { alert('Nombre y mensaje son requeridos'); return; }
  const btn = document.getElementById('ncpn-save-btn');
  btn.disabled = true; btn.textContent = 'Creando...';
  try {
    const r = await fetch('/api/campanas', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        nombre, mensaje,
        tag:       document.getElementById('ncpn-tag').value,
        estado:    document.getElementById('ncpn-estado').value,
        score_min: parseInt(document.getElementById('ncpn-score').value) || 0,
        comuna:    document.getElementById('ncpn-comuna').value,
        desde:     document.getElementById('ncpn-desde').value,
        hasta:     document.getElementById('ncpn-hasta').value,
      }),
    });
    if (r.ok) {
      cerrarNuevaCampana();
      await actualizarCampanasList();
    } else {
      const d = await r.json().catch(() => ({}));
      alert('Error: ' + (d.detail || 'desconocido'));
      btn.disabled = false; btn.textContent = 'Crear campaña';
    }
  } catch(e) {
    alert('Error de red: ' + e.message);
    btn.disabled = false; btn.textContent = 'Crear campaña';
  }
}

// ── Modal detalle campaña ──────────────────────────────────────────────────
async function abrirDetalleCampana(id) {
  const modal = document.getElementById('modal-campana-detail');
  const body  = document.getElementById('cpnd-body');
  body.innerHTML = '<div class="empty">Cargando...</div>';
  modal.style.display = 'flex';
  try {
    const [rc, rd] = await Promise.all([
      fetch(`/api/campanas/${id}`),
      fetch(`/api/campanas/${id}/destinatarios`),
    ]);
    const c = await rc.json();
    const d = await rd.json();
    renderDetalleCampana(c, d.destinatarios || []);
  } catch(e) {
    body.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}

function cerrarDetalleCampana() {
  document.getElementById('modal-campana-detail').style.display = 'none';
}

function renderDetalleCampana(c, dests) {
  const body  = document.getElementById('cpnd-body');
  const color = CPN_ESTADO_COLOR[c.estado] || '#888';
  const titleEl = document.getElementById('cpnd-title');
  const subEl   = document.getElementById('cpnd-sub');
  if (titleEl) titleEl.textContent = (c.nombre || 'CAMPAÑA').toUpperCase();
  if (subEl)   subEl.textContent   = (CPN_ESTADO_LABEL[c.estado] || c.estado) + '  ·  ' +
    (c.fecha_envio ? fmtDateLabel(c.fecha_envio) + ' ' + fmtTime(c.fecha_envio) : 'Sin enviar');
  const total = c.total_destinatarios || 0;
  const env   = c.total_enviados || 0;
  const fail  = c.total_fallidos || 0;
  const pct   = total > 0 ? Math.round(env / total * 100) : 0;
  const tasa_entrega = total > 0 ? (env / total * 100).toFixed(1) : '—';

  // Tasa respuesta: leads que respondieron después de la campaña (simplificado)
  const statsHtml = `
    <div class="cpnd-header-grid">
      <div class="cpnd-stat">
        <div class="cpnd-stat-label">Total</div>
        <div class="cpnd-stat-val" style="color:var(--neon)">${total}</div>
      </div>
      <div class="cpnd-stat">
        <div class="cpnd-stat-label">Enviados</div>
        <div class="cpnd-stat-val" style="color:var(--green)">${env}</div>
      </div>
      <div class="cpnd-stat">
        <div class="cpnd-stat-label">Fallidos</div>
        <div class="cpnd-stat-val" style="color:${fail>0?'var(--red)':'var(--txt3)'}">${fail}</div>
      </div>
      <div class="cpnd-stat">
        <div class="cpnd-stat-label">Entrega</div>
        <div class="cpnd-stat-val" style="color:${pct>=80?'var(--green)':pct>=50?'var(--orange)':'var(--red)'}">${tasa_entrega}%</div>
      </div>
    </div>`;

  // Barra de progreso
  const progHtml = `
    <div style="margin-bottom:1rem">
      <div style="display:flex;justify-content:space-between;font-size:.65rem;color:var(--txt3);margin-bottom:.3rem">
        <span>Progreso de entrega</span><span>${env}/${total}</span>
      </div>
      <div class="cpn-progress" style="height:6px">
        <div class="cpn-progress-fill" style="width:${pct}%;background:${color}"></div>
      </div>
    </div>`;

  // Filtros usados
  const filtros = [
    c.filtro_tag       ? `Tag: ${c.filtro_tag}`        : '',
    c.filtro_estado    ? `Estado: ${c.filtro_estado}`   : '',
    c.filtro_score_min > 0 ? `Score ≥ ${c.filtro_score_min}` : '',
    c.filtro_comuna    ? `Comuna: ${c.filtro_comuna}`   : '',
  ].filter(Boolean).join('  ·  ') || 'Sin filtros de segmentación';

  const msgHtml = `
    <div style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:.75rem 1rem;font-size:.78rem;line-height:1.55;margin-bottom:1rem;white-space:pre-wrap">${esc(c.mensaje)}</div>
    <div style="font-size:.65rem;color:var(--txt3);margin-bottom:1rem">&#128270; Segmentación: ${esc(filtros)}</div>`;

  // Lista de destinatarios
  const envOk  = dests.filter(d => d.estado_envio === 'enviado').length;
  const envErr = dests.filter(d => d.estado_envio === 'fallido').length;
  const envPen = dests.filter(d => d.estado_envio === 'pendiente').length;

  const destHtml = `
    <div style="font-size:.65rem;color:var(--txt3);font-weight:700;letter-spacing:.07em;margin-bottom:.5rem;text-transform:uppercase">
      Destinatarios (${dests.length})
      ${envErr > 0 ? `<span style="color:var(--red);margin-left:.5rem">${envErr} fallidos</span>` : ''}
      ${envPen > 0 ? `<span style="color:var(--orange);margin-left:.5rem">${envPen} pendientes</span>` : ''}
    </div>
    <div class="cpnd-dest-rows">
      ${dests.map(d => {
        const ic = d.estado_envio==='enviado' ? '✓' : d.estado_envio==='fallido' ? '✗' : '…';
        const cl = d.estado_envio==='enviado' ? 'cpnd-envio-ok' : d.estado_envio==='fallido' ? 'cpnd-envio-err' : 'cpnd-envio-pen';
        return `<div class="cpnd-dest-row">
          <span class="${cl}">${ic}</span>
          <div>
            <div style="font-size:.76rem;font-weight:600">${esc(d.nombre)||d.telefono}</div>
            ${d.error ? `<div style="font-size:.6rem;color:var(--red);margin-top:1px">${esc(d.error)}</div>` : ''}
          </div>
          <div style="text-align:right">
            <div style="font-family:monospace;font-size:.62rem;color:var(--txt3)">${d.telefono}</div>
            ${d.enviado_at ? `<div style="font-size:.58rem;color:var(--txt3)">${fmtTime(d.enviado_at)}</div>` : ''}
          </div>
        </div>`;
      }).join('')}
    </div>`;

  body.innerHTML = statsHtml + progHtml + msgHtml + destHtml;

  // Si está enviando, refrescar automáticamente
  if (c.estado === 'enviando') {
    setTimeout(() => {
      if (document.getElementById('modal-campana-detail').style.display === 'flex') {
        abrirDetalleCampana(c.id);
      }
    }, 3000);
  }
}

</script>
</body>
</html>
"""

# ── Login page ─────────────────────────────────────────────────────────────────

_HTML_LOGIN = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Valentina CRM — Acceso</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Space+Grotesk:wght@400;600;700&display=swap" rel="stylesheet" crossorigin>
<style>
  :root {
    --bg: #000;
    --card: #0a0a0a;
    --border: #1a1a1a;
    --neon: #00D4FF;
    --red: #FF2233;
    --txt: #e8e8e8;
    --txt2: #888;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--txt);
    font-family: 'Space Grotesk', sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  /* grid de líneas de fondo */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(0,212,255,.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,212,255,.04) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
    z-index: 0;
  }
  .card {
    position: relative;
    z-index: 1;
    background: var(--card);
    border: 1px solid var(--border);
    border-top: 2px solid var(--neon);
    border-radius: 12px;
    padding: 2.5rem 2rem;
    width: 360px;
    box-shadow: 0 0 40px rgba(0,212,255,.08);
  }
  .logo {
    text-align: center;
    margin-bottom: 2rem;
  }
  .logo h1 {
    font-family: 'Orbitron', sans-serif;
    font-size: 1.1rem;
    font-weight: 900;
    letter-spacing: .12em;
    color: var(--neon);
    text-transform: uppercase;
  }
  .logo p {
    font-size: .7rem;
    color: var(--txt2);
    margin-top: .3rem;
    letter-spacing: .08em;
    text-transform: uppercase;
  }
  label {
    display: block;
    font-size: .72rem;
    font-weight: 600;
    color: var(--txt2);
    text-transform: uppercase;
    letter-spacing: .06em;
    margin-bottom: .4rem;
  }
  input {
    width: 100%;
    background: #111;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--txt);
    font-family: 'Space Grotesk', sans-serif;
    font-size: .88rem;
    padding: .65rem .9rem;
    outline: none;
    transition: border-color .2s;
  }
  input:focus { border-color: var(--neon); }
  .field { margin-bottom: 1.1rem; }
  .btn {
    width: 100%;
    background: var(--neon);
    color: #000;
    border: none;
    border-radius: 6px;
    font-family: 'Orbitron', sans-serif;
    font-size: .78rem;
    font-weight: 700;
    letter-spacing: .1em;
    padding: .75rem;
    cursor: pointer;
    margin-top: .5rem;
    transition: opacity .15s;
    text-transform: uppercase;
  }
  .btn:hover { opacity: .85; }
  .btn:active { opacity: .7; }
  .error {
    background: rgba(255,34,51,.12);
    border: 1px solid rgba(255,34,51,.4);
    border-radius: 6px;
    color: #ff6677;
    font-size: .78rem;
    padding: .6rem .8rem;
    margin-bottom: 1.2rem;
    display: none;
  }
  .error.show { display: block; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <h1>Valentina CRM</h1>
    <p>Conexión Sin Límites</p>
  </div>
  <div class="error" id="err">Usuario o contraseña incorrectos.</div>
  <form method="POST" action="/login">
    <div class="field">
      <label for="user">Usuario</label>
      <input id="user" name="username" type="text" autocomplete="username" required autofocus>
    </div>
    <div class="field">
      <label for="pass">Contraseña</label>
      <input id="pass" name="password" type="password" autocomplete="current-password" required>
    </div>
    <button class="btn" type="submit">Ingresar</button>
  </form>
</div>
<script>
  // Mostrar error si viene ?error=1 en la URL
  if (location.search.includes('error=1')) {
    document.getElementById('err').classList.add('show');
  }

async function pausarCampana(id) {
  if (!confirm('¿Pausar esta campaña?')) return;
  const r = await fetch(`/api/campanas/${id}/pausar`, {method:'POST'});
  const d = await r.json();
  if (d.ok) { actualizarCampanasList(); } else { alert('Error: ' + d.error); }
}

async function reanudarCampana(id) {
  if (!confirm('¿Reanudar esta campaña?')) return;
  const r = await fetch(`/api/campanas/${id}/reanudar`, {method:'POST'});
  const d = await r.json();
  if (d.ok) { actualizarCampanasList(); iniciarPollingProgreso(id); }
  else { alert('Error: ' + d.error); }
}

let _progresoInterval = null;
function iniciarPollingProgreso(id) {
  if (_progresoInterval) clearInterval(_progresoInterval);
  _progresoInterval = setInterval(async () => {
    const r = await fetch(`/api/campanas/${id}/progreso`);
    const d = await r.json();
    if (!d.ok) return;
    const el = document.getElementById(`progreso-${id}`);
    if (el) {
      el.innerHTML = `
        <div style="margin:.5rem 0">
          <div style="background:#222;border-radius:8px;height:8px;overflow:hidden">
            <div style="background:#00D4FF;height:100%;width:${d.porcentaje}%;transition:width .5s"></div>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:.7rem;color:#888;margin-top:.3rem">
            <span>✅ ${d.enviados} enviados · ❌ ${d.fallidos} fallidos</span>
            <span>${d.porcentaje}% · ${d.total} total</span>
          </div>
        </div>`;
    }
    if (d.estado === 'completada' || d.estado === 'cancelada' || d.estado === 'error') {
      clearInterval(_progresoInterval);
      actualizarCampanasList();
    }
  }, 3000);
}

async function subirExcel() {
  const input = document.getElementById('excel-input');
  const archivo = input.files[0];
  if (!archivo) { alert('Selecciona un archivo CSV'); return; }
  const btn = document.getElementById('btn-subir-excel');
  btn.disabled = true; btn.textContent = 'Subiendo...';
  const form = new FormData();
  form.append('archivo', archivo);
  try {
    const r = await fetch('/api/campanas/subir-excel', {method:'POST', body: form});
    const d = await r.json();
    if (d.ok) {
      alert(`✅ Cargados: ${d.insertados} nuevos · ${d.duplicados} duplicados · ${d.errores} errores`);
      input.value = '';
      actualizarCampanasList();
    } else {
      alert('Error: ' + d.error);
    }
  } catch(e) {
    alert('Error al subir: ' + e.message);
  }
  btn.disabled = false; btn.textContent = '📤 Subir CSV';
}

</script>
</body>
</html>"""


# ── Rutas públicas (sin autenticación) ────────────────────────────────────────

@public_router.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(content=_HTML_LOGIN)


@public_router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()

    user_ok = secrets.compare_digest(username, DASHBOARD_USER)
    pass_ok = DASHBOARD_PASSWORD and secrets.compare_digest(password, DASHBOARD_PASSWORD)

    if not (user_ok and pass_ok):
        return RedirectResponse(url="/login?error=1", status_code=303)

    token = _generar_cookie()
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        max_age=_COOKIE_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=False,   # Railway usa HTTPS termination — cookie llega por HTTP internamente
    )
    return response


@public_router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=_COOKIE_NAME)
    return response

