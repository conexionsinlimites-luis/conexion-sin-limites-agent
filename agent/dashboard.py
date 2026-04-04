# agent/dashboard.py — Dashboard web del CRM Valentina
# Conexion Sin Limites

"""
Expone tres rutas:
  GET /dashboard         → página HTML del dashboard
  GET /api/stats         → KPIs y distribución por estado
  GET /api/leads         → últimos 20 leads
  GET /api/messages      → últimos 30 mensajes del historial CRM
"""

import aiosqlite
from datetime import datetime, date
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from agent.crm import DB_PATH

router = APIRouter()

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

    return JSONResponse({
        "total_leads":          total_leads,
        "leads_calientes":      leads_calientes,
        "leads_cerrados":       leads_cerrados,
        "score_promedio":       score_promedio,
        "por_estado":           por_estado,
        "followups_pendientes": followups_pendientes,
        "mensajes_hoy":         mensajes_hoy,
        "actualizado":          datetime.now().strftime("%H:%M:%S"),
    })


# ── API: leads recientes ───────────────────────────────────────────────────────

@router.get("/api/leads")
async def api_leads():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT nombre, telefono, estado, score, subproducto,
                   ultima_interaccion, objeciones
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
                }
                for r in rows
            ]
    return JSONResponse({"leads": leads})


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
            mensajes = [
                {
                    "telefono":   r["telefono"],
                    "nombre":     r["nombre"] or r["telefono"],
                    "rol":        r["rol"],
                    "mensaje":    r["mensaje"][:120] + ("…" if len(r["mensaje"]) > 120 else ""),
                    "timestamp":  r["timestamp"],
                    "estado":     r["estado_lead"] or "—",
                    "intencion":  r["intencion_detectada"] or "—",
                }
                for r in rows
            ]
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
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --gold:    #c9a227;
    --gold-lt: #e8c547;
    --bg:      #0a0a0a;
    --card:    #141414;
    --card2:   #1c1c1c;
    --border:  #2a2a2a;
    --txt:     #f0f0f0;
    --txt2:    #888;
    --red:     #e74c3c;
    --green:   #2ecc71;
    --blue:    #3498db;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--txt); font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; }

  /* ── header ── */
  header {
    background: linear-gradient(135deg, #111 0%, #1a1500 100%);
    border-bottom: 1px solid var(--gold);
    padding: 0 2rem;
    height: 64px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .logo { display: flex; align-items: center; gap: .75rem; }
  .logo-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--gold); box-shadow: 0 0 8px var(--gold); }
  .logo-name { font-size: 1.1rem; font-weight: 700; color: var(--gold); letter-spacing: .05em; }
  .logo-sub  { font-size: .75rem; color: var(--txt2); margin-top: 1px; }
  .header-right { display: flex; align-items: center; gap: 1.5rem; }
  .live-badge {
    display: flex; align-items: center; gap: .4rem;
    background: #0d1a0d; border: 1px solid #2ecc71;
    border-radius: 20px; padding: .3rem .8rem;
    font-size: .72rem; color: #2ecc71; font-weight: 600;
  }
  .live-dot { width: 7px; height: 7px; border-radius: 50%; background: #2ecc71; animation: pulse 1.5s infinite; }
  #last-update { font-size: .75rem; color: var(--txt2); }

  /* ── layout ── */
  main { padding: 2rem; max-width: 1400px; margin: 0 auto; }
  .section-title {
    font-size: .7rem; font-weight: 700; letter-spacing: .12em;
    color: var(--gold); text-transform: uppercase; margin-bottom: 1rem;
    display: flex; align-items: center; gap: .5rem;
  }
  .section-title::after { content: ''; flex: 1; height: 1px; background: var(--border); }

  /* ── KPI cards ── */
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
  .kpi-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 1.25rem 1.5rem;
    transition: border-color .2s;
  }
  .kpi-card:hover { border-color: var(--gold); }
  .kpi-card.gold  { border-color: var(--gold); background: #1a1500; }
  .kpi-label { font-size: .7rem; color: var(--txt2); text-transform: uppercase; letter-spacing: .08em; margin-bottom: .5rem; }
  .kpi-value { font-size: 2.4rem; font-weight: 800; line-height: 1; }
  .kpi-value.gold  { color: var(--gold); }
  .kpi-value.red   { color: var(--red);  }
  .kpi-value.green { color: var(--green);}
  .kpi-value.blue  { color: var(--blue); }
  .kpi-sub { font-size: .72rem; color: var(--txt2); margin-top: .4rem; }

  /* ── main grid ── */
  .main-grid { display: grid; grid-template-columns: 1fr 1.6fr; gap: 1.5rem; margin-bottom: 1.5rem; }
  @media(max-width: 900px) { .main-grid { grid-template-columns: 1fr; } }

  /* ── cards genéricas ── */
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 1.5rem; overflow: hidden;
  }
  .card-title {
    font-size: .75rem; font-weight: 700; color: var(--gold);
    text-transform: uppercase; letter-spacing: .1em; margin-bottom: 1.25rem;
  }

  /* ── chart ── */
  .chart-wrap { position: relative; height: 260px; }

  /* ── tabla leads ── */
  .leads-list { display: flex; flex-direction: column; gap: .5rem; max-height: 320px; overflow-y: auto; }
  .lead-row {
    display: grid; grid-template-columns: 1fr auto auto;
    align-items: center; gap: .75rem;
    background: var(--card2); border-radius: 8px; padding: .6rem 1rem;
    border-left: 3px solid var(--border);
    transition: border-color .2s;
  }
  .lead-row:hover { border-left-color: var(--gold); }
  .lead-name  { font-size: .85rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .lead-phone { font-size: .72rem; color: var(--txt2); }
  .lead-score { font-size: .8rem; font-weight: 700; color: var(--gold-lt); }
  .estado-badge {
    font-size: .65rem; font-weight: 700; padding: .2rem .6rem;
    border-radius: 20px; white-space: nowrap; text-transform: uppercase; letter-spacing: .05em;
  }

  /* ── tabla mensajes ── */
  .msg-table { width: 100%; border-collapse: collapse; font-size: .8rem; }
  .msg-table th {
    text-align: left; padding: .6rem .75rem;
    font-size: .65rem; font-weight: 700; color: var(--txt2);
    text-transform: uppercase; letter-spacing: .08em;
    border-bottom: 1px solid var(--border);
  }
  .msg-table td { padding: .65rem .75rem; border-bottom: 1px solid #1e1e1e; vertical-align: top; }
  .msg-table tr:hover td { background: var(--card2); }
  .msg-table tr:last-child td { border-bottom: none; }
  .tag-user      { color: #3498db; font-weight: 600; }
  .tag-assistant { color: var(--gold); font-weight: 600; }
  .tag-alta   { color: #e74c3c; }
  .tag-media  { color: #e67e22; }
  .tag-baja   { color: #888;    }
  .msg-text { color: var(--txt); line-height: 1.4; }
  .msg-time { font-size: .65rem; color: var(--txt2); white-space: nowrap; }
  .msg-wrap { max-height: 400px; overflow-y: auto; }

  /* ── scrollbar personalizado ── */
  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--gold); }

  /* ── animaciones ── */
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  .fade-in { animation: fadeIn .4s ease; }
  @keyframes fadeIn { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }

  /* ── empty state ── */
  .empty { text-align: center; padding: 2.5rem; color: var(--txt2); font-size: .85rem; }
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-dot"></div>
    <div>
      <div class="logo-name">CONEXION SIN LIMITES</div>
      <div class="logo-sub">Valentina CRM — Panel de Control</div>
    </div>
  </div>
  <div class="header-right">
    <div class="live-badge"><div class="live-dot"></div> EN VIVO</div>
    <div id="last-update">Actualizando...</div>
  </div>
</header>

<main>

  <!-- KPIs -->
  <div class="section-title">Resumen general</div>
  <div class="kpi-grid">
    <div class="kpi-card gold">
      <div class="kpi-label">Total Leads</div>
      <div class="kpi-value gold" id="k-total">—</div>
      <div class="kpi-sub">contactos registrados</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Leads Calientes</div>
      <div class="kpi-value red" id="k-hot">—</div>
      <div class="kpi-sub">caliente + listo cierre</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Leads Cerrados</div>
      <div class="kpi-value green" id="k-closed">—</div>
      <div class="kpi-sub">conversiones</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Score Promedio</div>
      <div class="kpi-value blue" id="k-score">—</div>
      <div class="kpi-sub">sobre 100 puntos</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Mensajes Hoy</div>
      <div class="kpi-value" id="k-msgs">—</div>
      <div class="kpi-sub">en historial CRM</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Follow-ups Pendientes</div>
      <div class="kpi-value" id="k-followups">—</div>
      <div class="kpi-sub">programados sin enviar</div>
    </div>
  </div>

  <!-- Chart + Leads recientes -->
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
        <div class="empty">Cargando leads...</div>
      </div>
    </div>

  </div>

  <!-- Mensajes recientes -->
  <div class="card">
    <div class="card-title">Historial de mensajes recientes</div>
    <div class="msg-wrap">
      <table class="msg-table">
        <thead>
          <tr>
            <th>Contacto</th>
            <th>Rol</th>
            <th>Mensaje</th>
            <th>Estado</th>
            <th>Intención</th>
            <th>Hora</th>
          </tr>
        </thead>
        <tbody id="msgs-body">
          <tr><td colspan="6" class="empty">Cargando mensajes...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</main>

<script>
// ── Chart.js instance ──────────────────────────────────────────────────────────
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
        backgroundColor: colors.map(c => c + 'cc'),
        borderColor:     colors,
        borderWidth: 1,
        borderRadius: 6,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1c1c1c',
          borderColor: '#c9a227',
          borderWidth: 1,
          titleColor: '#c9a227',
          bodyColor: '#f0f0f0',
        }
      },
      scales: {
        x: {
          ticks: { color: '#888', font: { size: 11 } },
          grid:  { color: '#1e1e1e' }
        },
        y: {
          ticks: { color: '#888', font: { size: 11 }, stepSize: 1 },
          grid:  { color: '#1e1e1e' },
          beginAtZero: true
        }
      }
    }
  });
}

// ── Estado badge HTML ──────────────────────────────────────────────────────────
function estadoBadge(estado, color) {
  return `<span class="estado-badge" style="background:${color}22;color:${color};border:1px solid ${color}44">${estado}</span>`;
}

// ── Intención badge ────────────────────────────────────────────────────────────
function intencionTag(int) {
  const cls = int === 'alta' ? 'tag-alta' : int === 'media' ? 'tag-media' : 'tag-baja';
  return `<span class="${cls}">${int}</span>`;
}

// ── Formatear hora ─────────────────────────────────────────────────────────────
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts.replace(' ', 'T'));
  if (isNaN(d)) return ts.slice(10, 16) || ts;
  return d.toLocaleTimeString('es-CL', { hour: '2-digit', minute: '2-digit' });
}

// ── Actualizar KPIs ────────────────────────────────────────────────────────────
async function actualizarStats() {
  const r = await fetch('/api/stats');
  const d = await r.json();

  document.getElementById('k-total').textContent     = d.total_leads;
  document.getElementById('k-hot').textContent       = d.leads_calientes;
  document.getElementById('k-closed').textContent    = d.leads_cerrados;
  document.getElementById('k-score').textContent     = d.score_promedio;
  document.getElementById('k-msgs').textContent      = d.mensajes_hoy;
  document.getElementById('k-followups').textContent = d.followups_pendientes;
  document.getElementById('last-update').textContent = 'Actualizado ' + d.actualizado;

  const labels = d.por_estado.map(e => e.estado);
  const data   = d.por_estado.map(e => e.total);
  const colors = d.por_estado.map(e => e.color);
  initChart(labels, data, colors);
}

// ── Actualizar leads ───────────────────────────────────────────────────────────
async function actualizarLeads() {
  const r = await fetch('/api/leads');
  const d = await r.json();
  const el = document.getElementById('leads-list');

  if (!d.leads.length) {
    el.innerHTML = '<div class="empty">No hay leads registrados aún</div>';
    return;
  }

  el.innerHTML = d.leads.map(l => `
    <div class="lead-row fade-in" style="border-left-color:${l.color}">
      <div>
        <div class="lead-name">${l.nombre}</div>
        <div class="lead-phone">${l.telefono} &nbsp;·&nbsp; ${l.subproducto}</div>
      </div>
      ${estadoBadge(l.estado, l.color)}
      <div class="lead-score">${l.score}pts</div>
    </div>
  `).join('');
}

// ── Actualizar mensajes ────────────────────────────────────────────────────────
async function actualizarMensajes() {
  const r = await fetch('/api/messages');
  const d = await r.json();
  const el = document.getElementById('msgs-body');

  if (!d.mensajes.length) {
    el.innerHTML = '<tr><td colspan="6" class="empty">No hay mensajes registrados aún</td></tr>';
    return;
  }

  el.innerHTML = d.mensajes.map(m => `
    <tr class="fade-in">
      <td><span style="font-size:.8rem;font-weight:600">${m.nombre}</span></td>
      <td><span class="${m.rol === 'user' ? 'tag-user' : 'tag-assistant'}">${m.rol}</span></td>
      <td class="msg-text">${m.mensaje}</td>
      <td>${m.estado !== '—' ? estadoBadge(m.estado, '#888') : '<span style="color:#444">—</span>'}</td>
      <td>${m.intencion !== '—' ? intencionTag(m.intencion) : '<span style="color:#444">—</span>'}</td>
      <td class="msg-time">${fmtTime(m.timestamp)}</td>
    </tr>
  `).join('');
}

// ── Loop principal ─────────────────────────────────────────────────────────────
async function refresh() {
  try {
    await Promise.all([
      actualizarStats(),
      actualizarLeads(),
      actualizarMensajes(),
    ]);
  } catch (e) {
    document.getElementById('last-update').textContent = 'Error de conexion';
  }
}

refresh();
setInterval(refresh, 10_000); // refresca cada 10 segundos
</script>
</body>
</html>
"""
