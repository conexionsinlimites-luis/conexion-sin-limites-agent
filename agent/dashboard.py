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
import io
import json
import logging
import secrets
import traceback
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from agent.config import TELEFONO_OWNER, DASHBOARD_USER, DASHBOARD_PASSWORD
from agent.database import get_pool
import agent.crm as _crm
from agent.memory import guardar_mensaje as _guardar_memoria

logger = logging.getLogger("agentkit")

# ── Autenticación básica del dashboard ────────────────────────────────────────
_http_basic = HTTPBasic()


def _verificar_auth(credentials: HTTPBasicCredentials = Depends(_http_basic)):
    """Valida usuario y contraseña contra las variables de entorno."""
    if not DASHBOARD_PASSWORD:
        # Sin contraseña configurada en Railway, acceso bloqueado por seguridad
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard no disponible: configura DASHBOARD_PASSWORD en Railway.",
        )
    ok_user = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        DASHBOARD_USER.encode("utf-8"),
    )
    ok_pass = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        DASHBOARD_PASSWORD.encode("utf-8"),
    )
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic realm=\"Valentina CRM\""},
        )


router = APIRouter(dependencies=[Depends(_verificar_auth)])

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

    return JSONResponse({
        "total_leads":          total_leads,
        "leads_calientes":      leads_calientes,
        "leads_cerrados":       leads_cerrados,
        "score_promedio":       score_promedio,
        "por_estado":           por_estado,
        "followups_pendientes": followups_pendientes,
        "mensajes_hoy":         mensajes_hoy,
        "conversion":           conversion,
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


# ── API: leads recientes ───────────────────────────────────────────────────────

@router.get("/api/leads")
async def api_leads():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT nombre, telefono, estado, score, subproducto,
                   ultima_interaccion, objeciones, lead_resumen, notas
            FROM leads
            ORDER BY ultima_interaccion DESC
            LIMIT 20
        """)
        leads = [
            {
                "nombre":             r["nombre"] or "Desconocido",
                "telefono":           r["telefono"],
                "estado":             r["estado"],
                "score":              r["score"],
                "subproducto":        r["subproducto"] or "—",
                "ultima_interaccion": str(r["ultima_interaccion"]),
                "color":              COLOR_ESTADO.get(r["estado"], "#888"),
                "prioridad":          calcular_prioridad(r["estado"], r["score"] or 0),
                "resumen":            r["lead_resumen"] or "",
                "notas":              r["notas"] or "",
            }
            for r in rows
        ]
    return JSONResponse({"leads": leads})


# ── API: exportar leads a CSV ──────────────────────────────────────────────────

@router.get("/api/leads/export-csv")
async def export_leads_csv():
    """Descarga todos los leads como archivo CSV (compatible con Excel)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT nombre, telefono, estado, score, subproducto,
                   notas, direccion, comuna,
                   ultima_interaccion, created_at
            FROM leads
            ORDER BY ultima_interaccion DESC NULLS LAST
        """)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Nombre", "Teléfono", "Estado", "Score", "Producto",
        "Notas", "Dirección", "Comuna",
        "Última interacción", "Creado"
    ])
    for r in rows:
        writer.writerow([
            r["nombre"] or "",
            r["telefono"] or "",
            r["estado"] or "",
            r["score"] if r["score"] is not None else 0,
            r["subproducto"] or "",
            r["notas"] or "",
            r["direccion"] or "",
            r["comuna"] or "",
            str(r["ultima_interaccion"]) if r["ultima_interaccion"] else "",
            str(r["created_at"]) if r["created_at"] else "",
        ])

    # utf-8-sig incluye BOM para que Excel lo abra sin problemas de tildes
    csv_bytes = output.getvalue().encode("utf-8-sig")
    filename = f"leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


# ── API: detalle completo de un lead ──────────────────────────────────────────

@router.get("/api/leads/{telefono}/detail")
async def lead_detail(telefono: str):
    """Devuelve todos los campos de un lead para el modal de detalle."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT nombre, telefono, estado, score, subproducto,
                   notas, lead_resumen, direccion, comuna,
                   objeciones, mensajes_en_estado,
                   ultima_interaccion, created_at
            FROM leads WHERE telefono = $1
        """, telefono)
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            objeciones = json.loads(row["objeciones"] or "[]")
        except Exception:
            objeciones = []
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
        await _crm.guardar_mensaje(
            telefono, "user",
            "Valentina, continúa atendiendo a este cliente",
            "seguimiento", None
        )
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
                       nombre, estado, score
                FROM leads
                WHERE SPLIT_PART(REPLACE(telefono, ' ', ''), '@', 1) = ANY($1)
            """, telefonos)
            logger.info(f"[api/conversations] leads CRM={len(crm_rows)}")
            info_lead = {r["telefono"]: dict(r) for r in crm_rows}

        conversaciones = []
        for f in filas_mem:
            tel    = f["telefono"]
            lead   = info_lead.get(tel, {})
            estado = lead.get("estado") or "nuevo"
            score  = int(lead.get("score") or 0)
            _n     = (lead.get("nombre") or "").strip()
            nombre = _n if (_n and _n.lower() not in ("desconocido", "cliente", "unknown", "")) else tel
            conversaciones.append({
                "telefono":         tel,
                "nombre":           nombre,
                "estado":           estado,
                "score":            score,
                "ultima_actividad": str(f["ultima_actividad"]),
                "ultimo_mensaje":   str(f["ultimo_mensaje"] or ""),
                "ultimo_rol":       str(f["ultimo_rol"] or "user"),
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
                "SELECT rol AS role, mensaje AS content, timestamp FROM historial_mensajes WHERE REPLACE(telefono, ' ', '') = $1 ORDER BY timestamp ASC",
                tel
            )
        mensajes = [
            {"role": f["role"], "content": f["content"], "timestamp": str(f["timestamp"])}
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
    background: rgba(0,0,0,0.85);
    backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
    box-shadow: 0 1px 40px rgba(0,212,255,0.08);
    padding: 0 2.5rem;
    height: 70px;
    display: flex; align-items: center; justify-content: space-between;
  }

  .logo { display: flex; align-items: center; gap: 1rem; }

  .logo-icon {
    width: 38px; height: 38px;
    border: 1.5px solid var(--neon);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 0 12px var(--neon-glow), inset 0 0 12px rgba(0,212,255,0.05);
    font-family: 'Orbitron', sans-serif;
    font-size: 1.1rem; font-weight: 900;
    color: var(--neon);
    text-shadow: 0 0 8px var(--neon);
    flex-shrink: 0;
  }

  .logo-text-wrap { display: flex; flex-direction: column; }

  .logo-name {
    font-family: 'Orbitron', sans-serif;
    font-size: .95rem; font-weight: 700;
    letter-spacing: .15em; line-height: 1;
    color: var(--txt);
    text-shadow: 0 0 20px rgba(255,255,255,0.3);
  }

  .logo-x {
    color: var(--red);
    text-shadow: 0 0 10px var(--red), 0 0 20px var(--red-glow);
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

  .logo-sub {
    font-size: .65rem; font-weight: 500;
    color: var(--neon); letter-spacing: .2em;
    text-transform: uppercase; margin-top: 3px;
    opacity: .8;
  }

  .header-right { display: flex; align-items: center; gap: 1.5rem; }

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
    font-size: .7rem; color: var(--txt2);
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
    display: grid; grid-template-columns: 1.4rem 1fr auto auto auto auto auto;
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
      padding: 0 max(.85rem, env(safe-area-inset-left));
      padding-top: max(.45rem, env(safe-area-inset-top));
      padding-bottom: .45rem;
      padding-right: max(.85rem, env(safe-area-inset-right));
      height: auto;
      min-height: 52px;
      flex-wrap: nowrap;   /* fuerza fila única — evita que el header crezca */
      gap: .5rem;
      overflow: hidden;
    }
    .logo-icon { width: 28px; height: 28px; font-size: .82rem; border-radius: 6px; flex-shrink: 0; }
    .logo-name { font-size: .68rem; letter-spacing: .06em; }
    .logo-sub  { font-size: .5rem; letter-spacing: .1em; }
    /* Ocultar live badge y timestamp en móvil — sobran espacio y causan wrap */
    .header-right { display: none; }
    .tab-nav { flex-shrink: 0; }
    .tab-btn { padding: .28rem .75rem; font-size: .68rem; }
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
  .wa-search-wrap { padding: .6rem .9rem; border-bottom: 1px solid rgba(255,255,255,.05); flex-shrink: 0; }
  .wa-search-input {
    width: 100%; background: rgba(255,255,255,.06);
    border: 1px solid rgba(255,255,255,.1); border-radius: 20px;
    padding: .46rem .9rem; color: var(--txt);
    font-size: .8rem; outline: none;
    font-family: 'Space Grotesk', sans-serif; transition: border-color .2s;
  }
  .wa-search-input:focus { border-color: rgba(0,212,255,.4); }
  .wa-search-input::placeholder { color: var(--txt3); }

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
    flex-shrink: 0; display: flex; align-items: center; gap: .85rem;
    padding: .7rem 1.25rem; border-bottom: 1px solid var(--border);
    background: rgba(0,0,0,.2); min-height: 62px;
  }
  .wa-chat-hdr-avatar {
    width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: .88rem; font-weight: 700; font-family: 'Space Grotesk', sans-serif;
  }
  .wa-chat-hdr-info { flex: 1; min-width: 0; }
  .wa-chat-hdr-name { font-size: .92rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .wa-chat-hdr-sub  { font-size: .65rem; color: var(--txt2); margin-top: 2px; font-family: monospace; }
  .wa-chat-hdr-actions { display: flex; gap: .45rem; align-items: center; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }

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
  .wa-bubble-time { font-size: .58rem; color: var(--txt3); margin-top: .15rem; font-family: monospace; }
  .wa-bubble-wrap.user .wa-bubble-time      { text-align: left; }
  .wa-bubble-wrap.assistant .wa-bubble-time { text-align: right; }

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

  /* ── Botón toggle Tomar Lead / Liberar IA ── */
  .btn-toggle-lead {
    display: flex; align-items: center; gap: .35rem;
    padding: .38rem .9rem; border-radius: 20px; border: none; cursor: pointer;
    font-family: 'Space Grotesk', sans-serif; font-size: .72rem; font-weight: 700;
    letter-spacing: .04em; text-transform: uppercase; white-space: nowrap;
    transition: background .2s, box-shadow .2s, transform .1s;
    background: rgba(0,212,255,.12); color: var(--neon);
    border: 1px solid rgba(0,212,255,.4);
    box-shadow: 0 0 8px rgba(0,212,255,.15);
  }
  .btn-toggle-lead:hover:not(:disabled) {
    background: rgba(0,212,255,.22); box-shadow: 0 0 16px rgba(0,212,255,.3);
    transform: translateY(-1px);
  }
  .btn-toggle-lead.activo {
    background: rgba(168,85,247,.15); color: #c084fc;
    border-color: rgba(168,85,247,.5);
    box-shadow: 0 0 8px rgba(168,85,247,.2);
  }
  .btn-toggle-lead.activo:hover:not(:disabled) {
    background: rgba(168,85,247,.25); box-shadow: 0 0 16px rgba(168,85,247,.35);
  }
  .btn-toggle-lead:disabled { opacity: .5; cursor: default; transform: none; }
  .btn-toggle-icon { font-size: .8rem; line-height: 1; }

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
  <div class="logo">
    <div class="logo-icon">V</div>
    <div class="logo-text-wrap">
      <div class="logo-name">
        CONE<span class="logo-x">X</span>I&Oacute;N SIN L&Iacute;MITES
      </div>
      <div class="logo-sub">Valentina &nbsp;·&nbsp; CRM Intelligence</div>
    </div>
  </div>
  <nav class="tab-nav">
    <button class="tab-btn active" id="tab-metricas" onclick="switchTab('metricas')">&#128200; M&eacute;tricas</button>
  </nav>
  <button class="btn-live" id="btn-live" onclick="toggleLive()">&#128172; Live</button>
  <div class="header-right">
    <div class="live-badge"><div class="live-dot"></div>EN VIVO</div>
    <div id="last-update">Iniciando...</div>
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

    <div class="card">
      <div class="card-title" style="display:flex;align-items:center;justify-content:space-between;gap:.75rem">
        <span>Leads recientes</span>
        <button onclick="exportarCSV()" id="btn-export-csv" style="background:rgba(0,212,255,.07);border:1px solid rgba(0,212,255,.3);color:var(--neon);font-family:'Space Grotesk',sans-serif;font-size:.62rem;font-weight:700;letter-spacing:.06em;padding:.3rem .7rem;border-radius:8px;cursor:pointer;transition:background .15s;white-space:nowrap" onmouseover="this.style.background='rgba(0,212,255,.18)'" onmouseout="this.style.background='rgba(0,212,255,.07)'">&#8659; Exportar CSV</button>
      </div>
      <div class="leads-list" id="leads-list">
        <div class="empty">Sin datos</div>
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
  const pm = document.getElementById('panel-metrics');
  const bm = document.getElementById('tab-metricas');
  if (tab === 'metricas') {
    pm.style.display = '';
    bm.classList.add('active');
  }
}

function _abrirLivePanel() {
  const pc  = document.getElementById('panel-chat');
  const pm  = document.getElementById('panel-metrics');
  const btn = document.getElementById('btn-live');
  pm.style.display = 'none';
  pc.style.display = 'flex';
  pc.style.flexDirection = 'column';
  btn.classList.add('active');
  actualizarConversaciones();
}

function _cerrarLivePanel() {
  const pc  = document.getElementById('panel-chat');
  const pm  = document.getElementById('panel-metrics');
  const btn = document.getElementById('btn-live');
  pc.style.display = 'none';
  pc.style.flexDirection = '';
  pm.style.display = '';
  btn.classList.remove('active');
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
    if (d.por_estado && d.por_estado.length) {
      initChart(d.por_estado.map(e=>e.estado), d.por_estado.map(e=>e.total), d.por_estado.map(e=>e.color));
    }
    renderEmbudo(d.conversion);
  } catch(e) {
    console.error('actualizarStats excepción:', e);
    document.getElementById('last-update').textContent = 'ERR JS';
  }
}
function renderEmbudo(conv) {
  if (!conv) return;
  [conv.contactado_interesado, conv.interesado_caliente, conv.caliente_cierre].forEach((e,i) => {
    document.getElementById(`f-pct-${i}`).textContent = e.den>0 ? e.pct+'%' : '\u2014';
    setTimeout(() => { document.getElementById(`f-bar-${i}`).style.width = e.den>0 ? e.pct+'%' : '0%'; }, 80+i*60);
    document.getElementById(`f-cnt-${i}`).innerHTML = e.den>0 ? `<strong>${e.num}</strong> de ${e.den} leads` : 'sin datos suficientes';
  });
}
async function actualizarLeads() {
  const r = await fetch('/api/leads'); const d = await r.json();
  const el = document.getElementById('leads-list');
  if (!d.leads.length) { el.innerHTML = '<div class="empty">Sin leads registrados</div>'; return; }
  el.innerHTML = d.leads.map(l => {
    const safeTel    = l.telefono.replace(/['"<>&]/g, '');
    const safeNombre = esc(l.nombre);
    const notasIcon  = l.notas
      ? `<button class="btn-notas activo" title="Ver/editar notas" onclick="abrirNotasModal('${safeTel}','${safeNombre}',this.dataset.notas)" data-notas="${esc(l.notas)}">&#128221;</button>`
      : `<button class="btn-notas"        title="Agregar nota"     onclick="abrirNotasModal('${safeTel}','${safeNombre}','')"                                              >&#128221;</button>`;
    const detailIcon = `<button class="btn-detail" title="Ver resumen IA" onclick="abrirLeadDetail('${safeTel}')">&#128270;</button>`;
    return `
    <div class="lead-row fade-in" style="border-left-color:${l.color};box-shadow:inset 2px 0 8px ${l.color}22">
      <div class="lead-priority" title="${prioridadLabel(l.prioridad)}">${l.prioridad}</div>
      <div style="min-width:0">
        <div class="lead-name">${esc(l.nombre)}</div>
        <div class="lead-phone">${l.telefono} &middot; ${l.subproducto}</div>
        ${l.resumen ? `<div class="lead-resumen" title="${esc(l.resumen)}">${esc(l.resumen)}</div>` : ''}
      </div>
      ${estadoBadge(l.estado,l.color)}
      <div class="lead-score">${l.score}<span style="font-size:.55rem;opacity:.6">pts</span></div>
      ${detailIcon}
      ${notasIcon}
      ${botonAccion(l)}
    </div>`;
  }).join('');
}
function exportarCSV() {
  const btn = document.getElementById('btn-export-csv');
  if (btn) { btn.textContent = '⏳ Descargando...'; btn.disabled = true; }
  const a = document.createElement('a');
  a.href = '/api/leads/export-csv';
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => { if (btn) { btn.textContent = '↙ Exportar CSV'; btn.disabled = false; } }, 1500);
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

async function refresh() {
  try { await Promise.all([actualizarStats(), actualizarLeads(), actualizarMensajes(), actualizarCampanas()]); }
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
            waAgregarBurbuja(d.role, d.content, d.ts);
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
    const cnt = document.getElementById('wa-conv-count');
    if (cnt) cnt.textContent = conversaciones.length;
    renderConvList();
  } catch(err) { console.error('[LiveChat] actualizarConversaciones error:', err); }
}

function filtrarContactos(q) { _searchQuery = q.toLowerCase(); renderConvList(); }

function renderConvList() {
  const el = document.getElementById('wa-conv-list');
  if (!el) return;
  const lista = _searchQuery
    ? conversaciones.filter(c => { const n=(c.nombre||c.telefono).toLowerCase(); return n.includes(_searchQuery)||c.telefono.includes(_searchQuery); })
    : conversaciones;
  if (!lista.length) {
    el.innerHTML = `<div class="empty" style="padding:2.5rem 1rem;text-align:center">${_searchQuery?'Sin resultados':'Sin conversaciones'}</div>`;
    return;
  }
  el.innerHTML = lista.map(c => {
    const activo      = c.telefono===contactoActivo?' active':'';
    const nombre      = c.nombre||c.telefono;
    const inicial     = nombre.replace(/[^a-zA-Z0-9]/g,'').charAt(0).toUpperCase()||'#';
    const color       = avatarColor(c.telefono);
    const safeTel     = c.telefono.replace(/['"<>&]/g,'');
    const preview     = (c.ultimo_rol==='assistant'?'\u21A9 ':'')+esc((c.ultimo_mensaje||'').slice(0,50));
    const score       = c.score || 0;
    const prioridad   = c.prioridad || '\u26AA';
    const scoreColor  = score>=70?'var(--red)':score>=40?'var(--orange)':'var(--txt3)';
    const badge       = c.modo_humano
      ? '<span class="modo-badge humano">Humano</span>'
      : '<span class="modo-badge bot">Bot</span>';
    const toggleBtn = c.modo_humano
      ? `<button class="wa-quick-liberar" title="Liberar IA" onclick="event.stopPropagation();quickLiberar('${safeTel}')">&#9646;&#9646;</button>`
      : `<button class="wa-quick-tomar"   title="Tomar lead" onclick="event.stopPropagation();quickTomar('${safeTel}',this)">&#128100;</button>`;
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
          <span class="wa-conv-badges"><span class="wa-conv-score" style="color:${scoreColor}">${score}</span>${badge}${toggleBtn}</span>
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

  await cargarMensajes(telefono);
}

function volverSidebar() {
  document.getElementById('wa-layout').classList.remove('chat-abierto');
  contactoActivo = null;
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
  const input  = document.getElementById('wa-input');
  const btn    = document.getElementById('wa-send-btn');
  if (hint)  hint.style.display  = modoHumano ? 'block' : 'none';
  if (input) {
    input.disabled = false;  // siempre habilitado — el dashboard puede enviar en cualquier modo
    input.classList.toggle('modo-humano', modoHumano);
    input.placeholder = modoHumano
      ? 'Modo humano — escribe y presiona Enter...'
      : 'Mensaje manual (bot sigue activo)...';
  }
  if (btn) { btn.disabled = false; btn.classList.toggle('modo-humano', modoHumano); }
  const actions = modoHumano
    ? `<button class="btn-toggle-lead activo" onclick="liberarLeadChat('${safeTel}')">
         <span class="btn-toggle-icon">&#9646;&#9646;</span> Liberar IA
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
      html += waBurbuja(m.role, m.content, m.timestamp);
    }
    el.innerHTML = html;
    _chatUltimoTS = d.mensajes[d.mensajes.length-1].timestamp || '';
    waScrollAbajo();
  } catch(err) {
    el.innerHTML = `<div class="empty" style="margin:auto;padding:2rem;text-align:center">Error al cargar (${err.message})</div>`;
  }
}

function waBurbuja(role, content, ts) {
  return `<div class="wa-bubble-wrap ${role}"><div class="wa-bubble ${role}">${esc(content)}</div><div class="wa-bubble-time">${fmtTime(ts)}</div></div>`;
}

function waAgregarBurbuja(role, content, ts) {
  const el = document.getElementById('wa-messages');
  const empty = el.querySelector('.empty');
  if (empty) empty.remove();
  const wrap = document.createElement('div');
  wrap.innerHTML = waBurbuja(role, content, ts);
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
  waAgregarBurbuja('assistant', texto, tsLocal);
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
    msgsEl.innerHTML = d.mensajes.map(m => modalBurbuja(m.role, m.content, m.timestamp)).join('');
    msgsEl.scrollTop = msgsEl.scrollHeight;
  } catch(err) { msgsEl.innerHTML=`<div class="empty">Error (${err.message})</div>`; }
}
function cerrarModal() { document.getElementById('modal-chat').style.display = 'none'; }
function modalBurbuja(role, content, ts) {
  const isBot=role==='assistant';
  const align=isBot?'flex-end':'flex-start';
  const bg=isBot?'rgba(0,212,255,0.1)':'rgba(255,255,255,0.06)';
  const bdr=isBot?'1px solid rgba(0,212,255,0.22)':'1px solid rgba(255,255,255,0.1)';
  const br=isBot?'16px 16px 4px 16px':'16px 16px 16px 4px';
  const label=isBot?'Valentina':'Cliente';
  const lclr=isBot?'var(--neon)':'rgba(255,255,255,.4)';
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
})();

</script>
</body>
</html>
"""
