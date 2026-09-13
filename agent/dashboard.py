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
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from agent.config import TELEFONO_OWNER, DASHBOARD_USER, DASHBOARD_PASSWORD
from agent.database import get_pool
import agent.crm as _crm
import agent.campanas as _campanas
from agent.memory import guardar_mensaje as _guardar_memoria

logger = logging.getLogger("agentkit")

# ── Carga de plantillas HTML desde /templates ─────────────────────────────────
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _cargar_template(nombre: str) -> str:
    """Lee un archivo HTML desde /templates (se cachea una vez al importar el módulo)."""
    return (_TEMPLATES_DIR / nombre).read_text(encoding="utf-8")

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
        "limite":    int(body.get("limite") or 0),
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


HTML_DASHBOARD = _cargar_template("dashboard.html")

# ── Login page ─────────────────────────────────────────────────────────────────

_HTML_LOGIN = _cargar_template("login.html")


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

