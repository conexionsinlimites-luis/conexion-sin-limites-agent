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
import json
import aiosqlite
from datetime import datetime, date
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from agent.config import DB_PATH as MEMORY_DB_PATH, TELEFONO_OWNER
from agent.crm import DB_PATH
import agent.crm as _crm
from agent.memory import guardar_mensaje as _guardar_memoria

router = APIRouter()

# ── SSE broadcast system ───────────────────────────────────────────────────────
_sse_queues: set[asyncio.Queue] = set()


async def broadcast_event(data: dict):
    """Emite un evento SSE a todos los clientes conectados al Live Chat."""
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT COUNT(*) FROM leads") as c:
            total_leads = (await c.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM leads WHERE estado IN ('caliente','listo_para_cierre')"
        ) as c:
            leads_calientes = (await c.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM leads WHERE estado = 'cerrado'"
        ) as c:
            leads_cerrados = (await c.fetchone())[0]

        async with db.execute(
            "SELECT ROUND(AVG(score),1) FROM leads"
        ) as c:
            score_promedio = (await c.fetchone())[0] or 0

        async with db.execute(
            "SELECT estado, COUNT(*) as total FROM leads GROUP BY estado ORDER BY total DESC"
        ) as c:
            rows = await c.fetchall()
            por_estado = [
                {"estado": r["estado"], "total": r["total"], "color": COLOR_ESTADO.get(r["estado"], "#888")}
                for r in rows
            ]

        async with db.execute(
            "SELECT COUNT(*) FROM followup_programado WHERE enviado=0 AND cancelado=0"
        ) as c:
            followups_pendientes = (await c.fetchone())[0]

        hoy = date.today().isoformat()
        async with db.execute(
            "SELECT COUNT(*) FROM historial_mensajes WHERE timestamp >= ?", (hoy,)
        ) as c:
            mensajes_hoy = (await c.fetchone())[0]

        # ── Tasas de conversión por etapa del embudo ───────────────────────────
        # Leads en modo_humano se excluyen: no aportan señal de conversión propia.
        async with db.execute("""
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

              COUNT(CASE WHEN estado IN (
                'listo_para_cierre','cerrado'
              ) THEN 1 END) AS n_cierre,

              COUNT(CASE WHEN estado = 'cerrado' THEN 1 END) AS n_cerrado
            FROM leads
            WHERE estado != 'modo_humano'
        """) as c:
            r = await c.fetchone()
            n_contactado = r[0] or 0
            n_interesado = r[1] or 0
            n_caliente   = r[2] or 0
            n_cierre     = r[3] or 0
            n_cerrado    = r[4] or 0

        def tasa(num, den):
            return round(num / den * 100, 1) if den else 0

        conversion = {
            "contactado_interesado": {
                "label": "Contactado → Interesado",
                "pct":   tasa(n_interesado, n_contactado),
                "num":   n_interesado,
                "den":   n_contactado,
            },
            "interesado_caliente": {
                "label": "Interesado → Caliente",
                "pct":   tasa(n_caliente, n_interesado),
                "num":   n_caliente,
                "den":   n_interesado,
            },
            "caliente_cierre": {
                "label": "Caliente → Cierre",
                "pct":   tasa(n_cierre, n_caliente),
                "num":   n_cierre,
                "den":   n_caliente,
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


# ── API: leads recientes ───────────────────────────────────────────────────────

@router.get("/api/leads")
async def api_leads():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT nombre, telefono, estado, score, subproducto,
                   ultima_interaccion, objeciones, lead_resumen
            FROM leads
            ORDER BY ultima_interaccion DESC
            LIMIT 20
        """) as c:
            rows = await c.fetchall()
            leads = [
                {
                    "nombre":             r["nombre"] or "Desconocido",
                    "telefono":           r["telefono"],
                    "estado":             r["estado"],
                    "score":              r["score"],
                    "subproducto":        r["subproducto"] or "—",
                    "ultima_interaccion": r["ultima_interaccion"],
                    "color":              COLOR_ESTADO.get(r["estado"], "#888"),
                    "prioridad":          calcular_prioridad(r["estado"], r["score"] or 0),
                    "resumen":            r["lead_resumen"] or "",
                }
                for r in rows
            ]
    return JSONResponse({"leads": leads})


# ── API: tomar / liberar lead (modo humano) ────────────────────────────────────

@router.post("/api/leads/{telefono}/tomar")
async def tomar_lead(telefono: str):
    """Activa modo_humano: pausa las respuestas de Valentina y cancela follow-ups."""
    nombre = "Cliente"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT nombre FROM leads WHERE telefono = ?", (telefono,)
        ) as c:
            row = await c.fetchone()
            if row and row["nombre"]:
                nombre = row["nombre"]
        await db.execute(
            "UPDATE leads SET estado = 'modo_humano', ultima_interaccion = CURRENT_TIMESTAMP WHERE telefono = ?",
            (telefono,)
        )
        await db.execute(
            "UPDATE followup_programado SET cancelado = 1 WHERE telefono = ? AND enviado = 0 AND cancelado = 0",
            (telefono,)
        )
        await db.commit()
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE leads SET estado = 'seguimiento', ultima_interaccion = CURRENT_TIMESTAMP WHERE telefono = ?",
            (telefono,)
        )
        await db.commit()
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
    # 1) leer contactos únicos desde la DB de memoria (agentkit.db)
    async with aiosqlite.connect(MEMORY_DB_PATH) as mem_db:
        mem_db.row_factory = aiosqlite.Row
        async with mem_db.execute("""
            SELECT
                telefono,
                MAX(timestamp)  AS ultima_actividad,
                COUNT(*)        AS total_mensajes,
                (SELECT content FROM mensajes m2
                 WHERE m2.telefono = mensajes.telefono
                 ORDER BY m2.timestamp DESC LIMIT 1) AS ultimo_mensaje,
                (SELECT role FROM mensajes m2
                 WHERE m2.telefono = mensajes.telefono
                 ORDER BY m2.timestamp DESC LIMIT 1) AS ultimo_rol
            FROM mensajes
            GROUP BY telefono
            ORDER BY ultima_actividad DESC
            LIMIT 50
        """) as c:
            filas_mem = await c.fetchall()

    if not filas_mem:
        return JSONResponse({"conversaciones": []})

    telefonos = [f["telefono"] for f in filas_mem]

    # 2) enriquecer con datos del CRM (valentina_crm.db)
    info_lead: dict[str, dict] = {}
    async with aiosqlite.connect(DB_PATH) as crm_db:
        crm_db.row_factory = aiosqlite.Row
        ph = ",".join("?" * len(telefonos))
        async with crm_db.execute(
            f"SELECT telefono, nombre, estado, score FROM leads WHERE telefono IN ({ph})",
            telefonos,
        ) as c:
            for row in await c.fetchall():
                info_lead[row["telefono"]] = dict(row)

    conversaciones = []
    for f in filas_mem:
        tel = f["telefono"]
        lead = info_lead.get(tel, {})
        estado = lead.get("estado") or "nuevo"
        score = lead.get("score") or 0
        _n = (lead.get("nombre") or "").strip()
        _nombre = _n if (_n and _n.lower() not in ("desconocido", "cliente", "unknown", "")) else tel
        conversaciones.append({
            "telefono":         tel,
            "nombre":           _nombre,
            "estado":           estado,
            "score":            score,
            "ultima_actividad": f["ultima_actividad"],
            "ultimo_mensaje":   f["ultimo_mensaje"] or "",
            "ultimo_rol":       f["ultimo_rol"] or "user",
            "total_mensajes":   f["total_mensajes"],
            "modo_humano":      estado == "modo_humano",
            "color":            COLOR_ESTADO.get(estado, "#888"),
            "prioridad":        calcular_prioridad(estado, score),
        })

    return JSONResponse({"conversaciones": conversaciones})


# ── API: historial de un contacto — /api/chat/{tel} ───────────────────────────

@router.get("/api/chat/{telefono}")
async def api_chat_historial(telefono: str):
    """Retorna el historial completo de mensajes de un contacto."""
    tel = telefono.lstrip("+")          # normalizar: quitar + si viene en la URL
    async with aiosqlite.connect(MEMORY_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content, timestamp FROM mensajes WHERE telefono = ? ORDER BY timestamp ASC",
            (tel,),
        ) as c:
            filas = await c.fetchall()

    mensajes = [
        {"role": f["role"], "content": f["content"], "timestamp": f["timestamp"]}
        for f in filas
    ]
    return JSONResponse({"mensajes": mensajes, "telefono": tel})


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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT h.telefono, h.rol, h.mensaje, h.timestamp,
                   h.estado_lead, h.intencion_detectada, l.nombre
            FROM historial_mensajes h
            LEFT JOIN leads l ON h.telefono = l.telefono
            ORDER BY h.timestamp DESC
            LIMIT 30
        """) as c:
            rows = await c.fetchall()
            mensajes = []
            for r in rows:
                _n = (r["nombre"] or "").strip()
                _nombre = _n if (_n and _n.lower() not in ("desconocido", "cliente", "unknown", "")) else r["telefono"]
                mensajes.append({
                    "telefono":   r["telefono"],
                    "nombre":     _nombre,
                    "rol":        r["rol"],
                    "mensaje":    r["mensaje"][:100] + ("…" if len(r["mensaje"]) > 100 else ""),
                    "timestamp":  r["timestamp"],
                    "estado":     r["estado_lead"] or "—",
                    "intencion":  r["intencion_detectada"] or "—",
                })
    return JSONResponse({"mensajes": mensajes})


# ── HTML del dashboard ─────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=HTML_DASHBOARD)


HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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
    min-height: 100vh;
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
    cursor: default;
    position: relative; overflow: hidden;
  }
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
    display: grid; grid-template-columns: 1.4rem 1fr auto auto auto;
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
      padding: 0 1rem;
      height: auto;
      min-height: 54px;
      flex-wrap: wrap;
      gap: .3rem;
      padding-top: .5rem;
      padding-bottom: .5rem;
    }
    .logo-icon { width: 30px; height: 30px; font-size: .9rem; border-radius: 6px; }
    .logo-name { font-size: .72rem; letter-spacing: .08em; }
    .logo-sub  { font-size: .55rem; letter-spacing: .12em; }
    .header-right { gap: .6rem; }
    .live-badge { padding: .25rem .65rem; font-size: .6rem; }
    #last-update { display: none; }

    main { padding: 1rem; }

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
      grid-template-columns: 1.2rem 1fr auto;
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

  /* ── input de modo humano — indicador visual ── */
  .chat-input-row .human-mode-hint {
    font-size: .62rem; color: #c084fc;
    padding: .2rem .7rem .0rem;
    letter-spacing: .04em;
    display: none;
  }
  .chat-input-row.modo-humano-activo .human-mode-hint { display: block; }
  .chat-input-row.modo-humano-activo .chat-input {
    border-color: rgba(168,85,247,0.5);
  }
  .chat-input-row.modo-humano-activo .chat-input:focus {
    border-color: rgba(168,85,247,0.8);
    box-shadow: 0 0 8px rgba(168,85,247,0.3);
  }
  .chat-input-row.modo-humano-activo .chat-send-btn {
    background: rgba(168,85,247,0.15);
    border-color: rgba(168,85,247,0.5);
    color: #c084fc;
  }
  .chat-input-row.modo-humano-activo .chat-send-btn:hover:not(:disabled) {
    background: rgba(168,85,247,0.28);
    box-shadow: 0 0 12px rgba(168,85,247,0.35);
  }
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
  <div class="header-right">
    <div class="live-badge"><div class="live-dot"></div>EN VIVO</div>
    <div id="last-update">Iniciando...</div>
  </div>
  <div class="scan-line"></div>
</header>

<main>

  <!-- KPIs -->
  <div class="section-label" style="margin-top:1.5rem">Resumen del sistema</div>
  <div class="kpi-grid">
    <div class="kpi-card primary">
      <div class="kpi-label">Total Leads</div>
      <div class="kpi-value neon" id="k-total">0</div>
      <div class="kpi-sub">registros activos</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Leads Calientes</div>
      <div class="kpi-value red" id="k-hot">0</div>
      <div class="kpi-sub">caliente + listo cierre</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Conversiones</div>
      <div class="kpi-value green" id="k-closed">0</div>
      <div class="kpi-sub">leads cerrados</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Score Promedio</div>
      <div class="kpi-value white" id="k-score">0</div>
      <div class="kpi-sub">sobre 100 pts</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Mensajes Hoy</div>
      <div class="kpi-value white" id="k-msgs">0</div>
      <div class="kpi-sub">en historial CRM</div>
    </div>
    <div class="kpi-card">
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
      <div class="card-title">Leads recientes</div>
      <div class="leads-list" id="leads-list">
        <div class="empty">Sin datos</div>
      </div>
    </div>

  </div>

  <!-- Live Chat -->
  <div class="section-label" style="margin-top:2rem">Live Chat &mdash; Tiempo Real</div>
  <div class="chat-container">

    <!-- Sidebar: lista de conversaciones -->
    <div class="chat-sidebar">
      <div class="chat-sidebar-header">
        <span>Conversaciones</span>
        <span id="conv-count" style="font-family:'Space Grotesk',sans-serif;color:var(--txt3);font-size:.65rem;font-weight:500;letter-spacing:.04em">—</span>
      </div>
      <div class="conv-list" id="conv-list">
        <div class="empty" style="padding:2rem 1rem">Cargando...</div>
      </div>
    </div>

    <!-- Panel derecho: vista de chat -->
    <div class="chat-main">
      <div class="chat-main-header">
        <div class="chat-contact-info">
          <div class="chat-contact-name" id="chat-contact-name">Selecciona una conversaci&oacute;n</div>
          <div class="chat-contact-phone" id="chat-contact-phone"></div>
        </div>
        <div class="chat-header-actions" id="chat-header-actions"></div>
      </div>
      <div class="chat-messages" id="chat-messages">
        <div class="empty" style="margin-top:5rem">Selecciona un contacto para ver la conversaci&oacute;n</div>
      </div>
      <div class="chat-input-row" id="chat-input-row">
        <span class="human-mode-hint">🟣 Modo humano — enviando directo a WhatsApp</span>
        <textarea class="chat-input" id="chat-input" placeholder="Selecciona una conversación..." rows="1" disabled></textarea>
        <button class="chat-send-btn" id="chat-send-btn" onclick="enviarMensaje()" disabled>Enviar</button>
      </div>
    </div>

  </div>

  <!-- Mensajes recientes -->
  <div class="section-label">Actividad reciente</div>
  <div class="card" style="margin-bottom:2.5rem">
    <div class="card-title">Últimos mensajes por contacto</div>
    <div class="msg-card-list" id="msgs-list">
      <div class="empty">Sin datos</div>
    </div>
  </div>

</main>

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

<script>
// ── Chart instance ─────────────────────────────────────────────────────────────
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
        borderWidth: 1,
        borderRadius: 6,
        hoverBackgroundColor: colors.map(c => c + '66'),
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(0,0,0,0.9)',
          borderColor: '#00D4FF',
          borderWidth: 1,
          titleColor: '#00D4FF',
          bodyColor: '#ffffff',
          titleFont: { family: 'Orbitron', size: 11 },
          bodyFont:  { family: 'Space Grotesk', size: 12 },
          padding: 12,
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

// ── Helpers ────────────────────────────────────────────────────────────────────
function prioridadLabel(emoji) {
  return { '🔴': 'Caliente', '🟡': 'Tibio', '⚪': 'Frío', '🟣': 'En atención humana' }[emoji] || '';
}

function botonAccion(lead) {
  if (lead.estado === 'modo_humano') {
    return `<button class="btn-liberar" onclick="liberarLead('${lead.telefono}')">Liberar IA</button>`;
  }
  return `<button class="btn-tomar" onclick="tomarLead('${lead.telefono}', this)">Tomar lead</button>`;
}

async function tomarLead(telefono, btn) {
  btn.disabled = true;
  btn.textContent = '...';
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(telefono) + '/tomar', { method: 'POST' });
    if (r.ok) await actualizarLeads();
    else { btn.disabled = false; btn.textContent = 'Tomar lead'; }
  } catch(e) { btn.disabled = false; btn.textContent = 'Tomar lead'; }
}

async function liberarLead(telefono) {
  await fetch('/api/leads/' + encodeURIComponent(telefono) + '/liberar', { method: 'POST' });
  await actualizarLeads();
}

function estadoBadge(estado, color) {
  return `<span class="estado-badge" style="background:${color}1a;color:${color};border:1px solid ${color}55;box-shadow:0 0 6px ${color}33">${estado}</span>`;
}

function intencionTag(v) {
  const cls = v==='alta' ? 'tag-alta' : v==='media' ? 'tag-media' : 'tag-baja';
  return `<span class="${cls}">${v}</span>`;
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts.replace(' ','T'));
  if (isNaN(d)) return ts.slice(10,16) || ts;
  return d.toLocaleTimeString('es-CL', { hour:'2-digit', minute:'2-digit' });
}

// ── Actualizar stats ───────────────────────────────────────────────────────────
async function actualizarStats() {
  const r = await fetch('/api/stats');
  const d = await r.json();
  document.getElementById('k-total').textContent     = d.total_leads;
  document.getElementById('k-hot').textContent       = d.leads_calientes;
  document.getElementById('k-closed').textContent    = d.leads_cerrados;
  document.getElementById('k-score').textContent     = d.score_promedio;
  document.getElementById('k-msgs').textContent      = d.mensajes_hoy;
  document.getElementById('k-followups').textContent = d.followups_pendientes;
  document.getElementById('last-update').textContent = d.actualizado;
  initChart(
    d.por_estado.map(e => e.estado),
    d.por_estado.map(e => e.total),
    d.por_estado.map(e => e.color)
  );
  renderEmbudo(d.conversion);
}

// ── Embudo de conversión ───────────────────────────────────────────────────────
function renderEmbudo(conv) {
  if (!conv) return;
  const etapas = [
    conv.contactado_interesado,
    conv.interesado_caliente,
    conv.caliente_cierre,
  ];
  etapas.forEach((e, i) => {
    const pct = e.pct;
    document.getElementById(`f-pct-${i}`).textContent = e.den > 0 ? pct + '%' : '—';
    // Barra animada: pequeño delay para que la transición CSS sea visible
    setTimeout(() => {
      document.getElementById(`f-bar-${i}`).style.width = e.den > 0 ? pct + '%' : '0%';
    }, 80 + i * 60);
    document.getElementById(`f-cnt-${i}`).innerHTML =
      e.den > 0
        ? `<strong>${e.num}</strong> de ${e.den} leads`
        : 'sin datos suficientes';
  });
}

// ── Actualizar leads ───────────────────────────────────────────────────────────
async function actualizarLeads() {
  const r = await fetch('/api/leads');
  const d = await r.json();
  const el = document.getElementById('leads-list');
  if (!d.leads.length) { el.innerHTML = '<div class="empty">Sin leads registrados</div>'; return; }
  el.innerHTML = d.leads.map(l => `
    <div class="lead-row fade-in" style="border-left-color:${l.color};box-shadow:inset 2px 0 8px ${l.color}22">
      <div class="lead-priority" title="${prioridadLabel(l.prioridad)}">${l.prioridad}</div>
      <div style="min-width:0">
        <div class="lead-name">${l.nombre}</div>
        <div class="lead-phone">${l.telefono} &middot; ${l.subproducto}</div>
        ${l.resumen ? `<div class="lead-resumen" title="${l.resumen}">${l.resumen}</div>` : ''}
      </div>
      ${estadoBadge(l.estado, l.color)}
      <div class="lead-score">${l.score}<span style="font-size:.55rem;opacity:.6">pts</span></div>
      ${botonAccion(l)}
    </div>
  `).join('');
}

// ── Color avatar por teléfono ─────────────────────────────────────────────────
function avatarColor(tel) {
  const palette = ['#00D4FF','#FF2233','#00FF88','#FF8C00','#c084fc','#F59E0B','#10B981','#3B82F6','#EC4899'];
  let h = 0;
  for (let i = 0; i < tel.length; i++) h = (Math.imul(31, h) + tel.charCodeAt(i)) | 0;
  return palette[Math.abs(h) % palette.length];
}

// ── Actualizar mensajes ────────────────────────────────────────────────────────
async function actualizarMensajes() {
  const r = await fetch('/api/messages');
  const d = await r.json();
  const el = document.getElementById('msgs-list');
  if (!d.mensajes.length) { el.innerHTML = '<div class="empty">Sin mensajes</div>'; return; }

  // Un card por contacto — mostrar su último mensaje (API ya viene DESC)
  const seen = new Set();
  const porContacto = [];
  for (const m of d.mensajes) {
    if (!seen.has(m.telefono)) { seen.add(m.telefono); porContacto.push(m); }
  }

  el.innerHTML = porContacto.map(m => {
    const nombre = m.nombre || m.telefono;
    const inicial = nombre.replace(/[^a-zA-Z0-9]/g, '').charAt(0).toUpperCase() || '#';
    const color   = avatarColor(m.telefono);
    const esBot   = m.rol === 'assistant';
    const safeTel = m.telefono.replace(/['"<>&]/g, '');
    const previewClass = esBot ? 'msg-card-preview bot-msg' : 'msg-card-preview';
    const prefix = esBot ? '↩ ' : '';
    const estadoHtml = m.estado !== '—'
      ? estadoBadge(m.estado, '#00D4FF')
      : '';
    const intHtml = m.intencion !== '—' ? intencionTag(m.intencion) : '';
    return `
    <div class="msg-card fade-in" onclick="seleccionarContacto('${safeTel}')">
      <div class="msg-avatar" style="background:${color}22;color:${color};border:1.5px solid ${color}55">${inicial}</div>
      <div class="msg-card-body">
        <div class="msg-card-name">
          <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(nombre)}</span>
          ${estadoHtml}
        </div>
        <div class="${previewClass}">${prefix}${esc(m.mensaje)}</div>
      </div>
      <div class="msg-card-right">
        <div class="msg-card-time">${fmtTime(m.timestamp)}</div>
        ${intHtml}
      </div>
    </div>`;
  }).join('');
}

// ── Loop ───────────────────────────────────────────────────────────────────────
async function refresh() {
  try {
    await Promise.all([actualizarStats(), actualizarLeads(), actualizarMensajes()]);
  } catch(e) {
    document.getElementById('last-update').textContent = 'ERROR';
  }
}

refresh();
setInterval(refresh, 10_000);

// ── Live Chat ──────────────────────────────────────────────────────────────────
let contactoActivo = null;
let conversaciones  = [];
let _chatUltimoTS   = '';   // ISO timestamp del último msg cargado — para deduplicar SSE

// ── SSE connection ─────────────────────────────────────────────────────────────
let _sse = null;
function conectarSSE() {
  if (_sse) { try { _sse.close(); } catch(_){} }
  _sse = new EventSource('/api/events');
  _sse.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.type === 'new_message') {
        actualizarConversaciones();
        if (contactoActivo === d.telefono) {
          // Solo agregar si el mensaje es posterior al historial ya cargado
          if (!_chatUltimoTS || d.ts > _chatUltimoTS) {
            agregarBurbuja(d.role, d.content, d.ts);
            scrollAbajo();
            _chatUltimoTS = d.ts;
          }
        } else {
          flashConv(d.telefono);
        }
      } else if (d.type === 'mode_change') {
        actualizarConversaciones();
        if (contactoActivo === d.telefono) {
          renderHeaderActions(d.telefono, d.modo_humano);
        }
      }
    } catch(_) {}
  };
  _sse.onerror = () => { setTimeout(conectarSSE, 4000); };
}

// ── Conversaciones ─────────────────────────────────────────────────────────────
async function actualizarConversaciones() {
  try {
    const r = await fetch('/api/conversations');
    const d = await r.json();
    conversaciones = d.conversaciones || [];
    document.getElementById('conv-count').textContent = conversaciones.length;
    renderConvList();
  } catch(_) {}
}

function renderConvList() {
  const el = document.getElementById('conv-list');
  if (!conversaciones.length) {
    el.innerHTML = '<div class="empty" style="padding:2rem 1rem">Sin conversaciones</div>';
    return;
  }
  el.innerHTML = conversaciones.map(c => {
    const activo  = c.telefono === contactoActivo ? ' active' : '';
    const badge   = c.modo_humano
      ? '<span class="modo-badge humano">Humano</span>'
      : '<span class="modo-badge bot">Bot</span>';
    const preview = c.ultimo_rol === 'assistant' ? '↩ ' : '';
    const safeTel = c.telefono.replace(/['"<>&]/g, '');
    const nombre  = c.nombre || c.telefono;
    const inicial = nombre.replace(/[^a-zA-Z0-9]/g, '').charAt(0).toUpperCase() || '#';
    const color   = avatarColor(c.telefono);
    return `
    <div class="conv-item${activo}" id="conv-${safeTel}" onclick="seleccionarContacto('${safeTel}')">
      <div style="display:flex;gap:.65rem;align-items:center;min-width:0">
        <div style="width:36px;height:36px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:700;font-family:'Space Grotesk',sans-serif;background:${color}22;color:${color};border:1.5px solid ${color}44">${inicial}</div>
        <div style="min-width:0;flex:1">
          <div class="conv-item-top">
            <span class="conv-item-name" title="${esc(nombre)}">${esc(nombre)}</span>
            <span class="conv-item-time">${fmtTime(c.ultima_actividad)}</span>
          </div>
          <div class="conv-item-bottom">
            <span class="conv-item-preview">${preview}${esc((c.ultimo_mensaje||'').slice(0,45))}</span>
            <span style="display:flex;gap:.3rem;align-items:center;flex-shrink:0">
              ${badge}
              <button class="btn-ver-chat" title="Ver historial completo" onclick="event.stopPropagation();abrirChatCompleto('${safeTel}','${esc(nombre)}')">&#128065;</button>
            </span>
          </div>
        </div>
      </div>
    </div>`;
  }).join('');
}

function flashConv(telefono) {
  const el = document.getElementById('conv-' + telefono.replace(/['"<>&]/g, ''));
  if (el) { el.classList.add('conv-new-msg'); setTimeout(() => el.classList.remove('conv-new-msg'), 900); }
}

// ── Seleccionar contacto ───────────────────────────────────────────────────────
async function seleccionarContacto(telefono) {
  contactoActivo = telefono;
  renderConvList();

  const conv = conversaciones.find(c => c.telefono === telefono);
  const nombre = conv ? conv.nombre : telefono;

  document.getElementById('chat-contact-name').textContent = nombre;
  document.getElementById('chat-contact-phone').textContent = '+' + telefono;

  renderHeaderActions(telefono, conv ? conv.modo_humano : false);

  // Habilitar input
  document.getElementById('chat-input').disabled = false;
  document.getElementById('chat-send-btn').disabled = false;
  document.getElementById('chat-input').focus();

  await cargarMensajes(telefono);
}

function renderHeaderActions(telefono, modoHumano) {
  const el = document.getElementById('chat-header-actions');
  const safeTel = telefono.replace(/['"<>&]/g, '');
  const conv = conversaciones.find(c => c.telefono === telefono);
  const nombre = conv ? esc(conv.nombre) : safeTel;
  const btnVerChat = `<button class="btn-tomar" style="border-color:rgba(0,212,255,.5);background:rgba(0,212,255,.1)" onclick="abrirChatCompleto('${safeTel}','${nombre}')">Ver chat completo</button>`;
  if (modoHumano) {
    el.innerHTML = `
      <span class="modo-badge humano" style="font-size:.65rem;padding:.25rem .8rem">Modo Humano</span>
      ${btnVerChat}
      <button class="btn-liberar" onclick="liberarLead('${safeTel}')">Liberar IA</button>`;
  } else {
    el.innerHTML = `
      <span class="modo-badge bot" style="font-size:.65rem;padding:.25rem .8rem">Bot activo</span>
      ${btnVerChat}
      <button class="btn-tomar" onclick="tomarDesdeChat('${safeTel}', this)">Tomar lead</button>`;
  }
  // Actualizar el estilo y placeholder del área de input según el modo
  const inputRow = document.getElementById('chat-input-row');
  const input    = document.getElementById('chat-input');
  if (modoHumano) {
    inputRow.classList.add('modo-humano-activo');
    input.placeholder = 'Escribe tu respuesta y presiona Enter...';
  } else {
    inputRow.classList.remove('modo-humano-activo');
    input.placeholder = 'Toma el lead primero para responder manualmente...';
  }
}

async function tomarDesdeChat(telefono, btn) {
  btn.disabled = true; btn.textContent = '...';
  try {
    const r = await fetch('/api/leads/' + encodeURIComponent(telefono) + '/tomar', { method: 'POST' });
    if (!r.ok) { btn.disabled = false; btn.textContent = 'Tomar lead'; }
  } catch(_) { btn.disabled = false; btn.textContent = 'Tomar lead'; }
}

// ── Mensajes ───────────────────────────────────────────────────────────────────
async function cargarMensajes(telefono) {
  _chatUltimoTS = '';   // reset para este contacto
  const el = document.getElementById('chat-messages');
  el.innerHTML = '<div class="empty" style="margin-top:3rem">Cargando...</div>';
  try {
    const r = await fetch('/api/chat/' + encodeURIComponent(telefono));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (!d.mensajes || !d.mensajes.length) {
      el.innerHTML = '<div class="empty" style="margin-top:3rem">Sin mensajes</div>';
      return;
    }
    el.innerHTML = d.mensajes.map(m => burbuja(m.role, m.content, m.timestamp)).join('');
    // Registrar timestamp del último mensaje para deduplicar SSE
    _chatUltimoTS = d.mensajes[d.mensajes.length - 1].timestamp || '';
    scrollAbajo();
  } catch(err) {
    el.innerHTML = '<div class="empty" style="margin-top:3rem">Error al cargar (' + err.message + ')</div>';
  }
}

function burbuja(role, content, ts) {
  return `
  <div class="msg-bubble-wrap ${role}">
    <div class="msg-bubble ${role}">${esc(content)}</div>
    <div class="msg-bubble-time">${fmtTime(ts)}</div>
  </div>`;
}

function agregarBurbuja(role, content, ts) {
  const el = document.getElementById('chat-messages');
  const empty = el.querySelector('.empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.innerHTML = burbuja(role, content, ts);
  el.appendChild(div.firstElementChild);
}

function scrollAbajo() {
  const el = document.getElementById('chat-messages');
  el.scrollTop = el.scrollHeight;
}

// ── Enviar mensaje ─────────────────────────────────────────────────────────────
async function enviarMensaje() {
  if (!contactoActivo) return;
  const input = document.getElementById('chat-input');
  const texto = input.value.trim();
  if (!texto) return;

  const btn = document.getElementById('chat-send-btn');
  btn.disabled = true; input.disabled = true;

  // Agregar burbuja localmente de inmediato — no esperar SSE
  const tsLocal = new Date().toISOString();
  agregarBurbuja('assistant', texto, tsLocal);
  scrollAbajo();
  _chatUltimoTS = tsLocal;   // evitar que SSE duplique esta burbuja
  input.value = '';
  input.style.height = 'auto';

  try {
    const r = await fetch('/api/chat/' + encodeURIComponent(contactoActivo) + '/send', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ mensaje: texto }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.ok === false) {
      // Marcar la última burbuja como error
      const burbujas = document.querySelectorAll('#chat-messages .msg-bubble.assistant');
      if (burbujas.length) {
        const last = burbujas[burbujas.length - 1];
        last.style.opacity = '.55';
        last.title = 'Error al enviar a WhatsApp: ' + (data.error || 'sin detalle');
      }
      if (data.error) console.warn('WA send error:', data.error);
    }
  } catch(_) {
    alert('Error de conexi\u00f3n al enviar');
  } finally {
    btn.disabled = false; input.disabled = false; input.focus();
  }
}

// ── Modal: Ver chat completo ───────────────────────────────────────────────────
async function abrirChatCompleto(telefono, nombre) {
  const modal = document.getElementById('modal-chat');
  const msgsEl = document.getElementById('modal-messages');
  document.getElementById('modal-nombre').textContent = nombre || telefono;
  document.getElementById('modal-tel').textContent = '+' + telefono;
  msgsEl.innerHTML = '<div class="empty">Cargando...</div>';
  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';

  try {
    const r = await fetch('/api/chat/' + encodeURIComponent(telefono));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (!d.mensajes || !d.mensajes.length) {
      msgsEl.innerHTML = '<div class="empty">Sin mensajes en el historial</div>';
      return;
    }
    msgsEl.innerHTML = d.mensajes.map(m => modalBurbuja(m.role, m.content, m.timestamp)).join('');
    msgsEl.scrollTop = msgsEl.scrollHeight;
  } catch(err) {
    msgsEl.innerHTML = '<div class="empty">Error al cargar (' + err.message + ')</div>';
  }
}

function cerrarModal() {
  document.getElementById('modal-chat').style.display = 'none';
  document.body.style.overflow = '';
}

function modalBurbuja(role, content, ts) {
  const isBot = role === 'assistant';
  const align = isBot ? 'flex-end' : 'flex-start';
  const bg    = isBot ? 'rgba(0,212,255,0.1)' : 'rgba(255,255,255,0.06)';
  const bdr   = isBot ? '1px solid rgba(0,212,255,0.22)' : '1px solid rgba(255,255,255,0.1)';
  const br    = isBot ? '16px 16px 4px 16px' : '16px 16px 16px 4px';
  const label = isBot ? 'Valentina' : 'Cliente';
  const lclr  = isBot ? 'var(--neon)' : 'rgba(255,255,255,.4)';
  return `
  <div style="display:flex;flex-direction:column;align-items:${align};gap:.2rem">
    <span style="font-size:.58rem;color:${lclr};letter-spacing:.06em;text-transform:uppercase;font-weight:600">${label}</span>
    <div style="max-width:78%;background:${bg};border:${bdr};border-radius:${br};padding:.65rem 1rem;font-size:.85rem;line-height:1.5;word-break:break-word;white-space:pre-wrap">${esc(content)}</div>
    <span style="font-size:.58rem;color:rgba(255,255,255,.2);font-family:monospace">${fmtTime(ts)}</span>
  </div>`;
}

// ── Utilidades ─────────────────────────────────────────────────────────────────
function esc(str) {
  return String(str||'')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/\\n/g,'<br>');
}

// ── Init ───────────────────────────────────────────────────────────────────────
(function initChat() {
  // Escape cierra el modal
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') cerrarModal();
  });

  // Enter envía, Shift+Enter hace salto de línea
  const input = document.getElementById('chat-input');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        enviarMensaje();
      }
    });
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 110) + 'px';
    });
  }
  conectarSSE();
  actualizarConversaciones();
  // Refrescar lista cada 30 s como fallback (SSE cubre el tiempo real)
  setInterval(actualizarConversaciones, 30_000);
})();
</script>
</body>
</html>
"""
