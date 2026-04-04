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
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  /* ═══════════════════════════════════════════════════
     CONEXION SIN LIMITES — Dark Futurista Premium
     #000000 | #00D4FF neon | #FF2233 rojo estrella
     ═══════════════════════════════════════════════════ */
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
    content: '✦';
    position: absolute;
    top: -6px; right: -5px;
    font-size: .45em;
    color: var(--red);
    text-shadow: 0 0 8px var(--red);
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
  .section-label::before { content: '//'; opacity: .5; }
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

  .kpi-icon {
    font-size: 1.2rem; margin-bottom: .6rem; opacity: .6;
    display: block;
  }
  .kpi-label {
    font-size: .6rem; font-weight: 600;
    color: var(--txt2); text-transform: uppercase;
    letter-spacing: .12em; margin-bottom: .5rem;
  }
  .kpi-value {
    font-family: 'Orbitron', sans-serif;
    font-size: 2.6rem; font-weight: 900; line-height: 1;
    transition: text-shadow .3s;
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
  .card-title::before { content: '›'; font-size: 1em; }

  /* ── chart ── */
  .chart-wrap { position: relative; height: 260px; }

  /* ── leads list ── */
  .leads-list { display: flex; flex-direction: column; gap: .5rem; max-height: 320px; overflow-y: auto; }

  .lead-row {
    display: grid; grid-template-columns: 1fr auto auto;
    align-items: center; gap: .75rem;
    background: rgba(255,255,255,0.02);
    border-radius: 10px; padding: .65rem 1rem;
    border-left: 2px solid transparent;
    transition: background .2s, border-color .2s, box-shadow .2s;
  }
  .lead-row:hover {
    background: var(--glass-h);
    box-shadow: inset 0 0 20px rgba(0,212,255,0.04);
  }
  .lead-name  { font-size: .84rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .lead-phone { font-size: .68rem; color: var(--txt2); margin-top: 2px; font-family: monospace; letter-spacing: .03em; }
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

  /* ── tabla mensajes ── */
  .msg-wrap { max-height: 420px; overflow-y: auto; }

  .msg-table { width: 100%; border-collapse: collapse; font-size: .78rem; }
  .msg-table thead th {
    text-align: left; padding: .65rem .9rem;
    font-size: .58rem; font-weight: 700;
    color: var(--txt2); text-transform: uppercase;
    letter-spacing: .12em;
    border-bottom: 1px solid var(--border);
    font-family: 'Orbitron', sans-serif;
    background: transparent;
  }
  .msg-table td {
    padding: .7rem .9rem;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    vertical-align: middle;
  }
  .msg-table tr:hover td { background: var(--glass-h); }
  .msg-table tr:last-child td { border-bottom: none; }

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
  .empty::before { content: '— '; }
  .empty::after  { content: ' —'; }

  /* ── scan line decorativa en header ── */
  .scan-line {
    position: absolute; bottom: -1px; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent 0%, var(--neon) 50%, transparent 100%);
    opacity: .6;
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
      <span class="kpi-icon">◈</span>
      <div class="kpi-label">Total Leads</div>
      <div class="kpi-value neon" id="k-total">—</div>
      <div class="kpi-sub">registros activos</div>
    </div>
    <div class="kpi-card">
      <span class="kpi-icon">◉</span>
      <div class="kpi-label">Leads Calientes</div>
      <div class="kpi-value red" id="k-hot">—</div>
      <div class="kpi-sub">caliente + listo cierre</div>
    </div>
    <div class="kpi-card">
      <span class="kpi-icon">✓</span>
      <div class="kpi-label">Conversiones</div>
      <div class="kpi-value green" id="k-closed">—</div>
      <div class="kpi-sub">leads cerrados</div>
    </div>
    <div class="kpi-card">
      <span class="kpi-icon">◎</span>
      <div class="kpi-label">Score Promedio</div>
      <div class="kpi-value white" id="k-score">—</div>
      <div class="kpi-sub">sobre 100 pts</div>
    </div>
    <div class="kpi-card">
      <span class="kpi-icon">▲</span>
      <div class="kpi-label">Mensajes Hoy</div>
      <div class="kpi-value white" id="k-msgs">—</div>
      <div class="kpi-sub">en historial CRM</div>
    </div>
    <div class="kpi-card">
      <span class="kpi-icon">◷</span>
      <div class="kpi-label">Follow-ups</div>
      <div class="kpi-value neon" id="k-followups">—</div>
      <div class="kpi-sub">pendientes de envío</div>
    </div>
  </div>

  <!-- Chart + Leads recientes -->
  <div class="section-label">Análisis de pipeline</div>
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

  <!-- Mensajes recientes -->
  <div class="section-label">Historial de conversaciones</div>
  <div class="card" style="margin-bottom:2.5rem">
    <div class="card-title">Mensajes recientes</div>
    <div class="msg-wrap">
      <table class="msg-table">
        <thead>
          <tr>
            <th>Contacto</th>
            <th>Rol</th>
            <th>Mensaje</th>
            <th>Estado</th>
            <th>Intenci&oacute;n</th>
            <th>Hora</th>
          </tr>
        </thead>
        <tbody id="msgs-body">
          <tr><td colspan="6" class="empty">Sin datos</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</main>

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
}

// ── Actualizar leads ───────────────────────────────────────────────────────────
async function actualizarLeads() {
  const r = await fetch('/api/leads');
  const d = await r.json();
  const el = document.getElementById('leads-list');
  if (!d.leads.length) { el.innerHTML = '<div class="empty">Sin leads registrados</div>'; return; }
  el.innerHTML = d.leads.map(l => `
    <div class="lead-row fade-in" style="border-left-color:${l.color};box-shadow:inset 2px 0 8px ${l.color}22">
      <div style="min-width:0">
        <div class="lead-name">${l.nombre}</div>
        <div class="lead-phone">${l.telefono} &middot; ${l.subproducto}</div>
      </div>
      ${estadoBadge(l.estado, l.color)}
      <div class="lead-score">${l.score}<span style="font-size:.55rem;opacity:.6">pts</span></div>
    </div>
  `).join('');
}

// ── Actualizar mensajes ────────────────────────────────────────────────────────
async function actualizarMensajes() {
  const r = await fetch('/api/messages');
  const d = await r.json();
  const el = document.getElementById('msgs-body');
  if (!d.mensajes.length) { el.innerHTML = '<tr><td colspan="6" class="empty">Sin mensajes</td></tr>'; return; }
  el.innerHTML = d.mensajes.map(m => `
    <tr class="fade-in">
      <td style="font-weight:600;font-size:.8rem">${m.nombre}</td>
      <td><span class="${m.rol==='user'?'tag-user':'tag-assistant'}">${m.rol}</span></td>
      <td class="msg-text">${m.mensaje}</td>
      <td>${m.estado!=='—' ? estadoBadge(m.estado,'#00D4FF') : '<span style="color:rgba(255,255,255,.15)">—</span>'}</td>
      <td>${m.intencion!=='—' ? intencionTag(m.intencion) : '<span style="color:rgba(255,255,255,.15)">—</span>'}</td>
      <td class="msg-time">${fmtTime(m.timestamp)}</td>
    </tr>
  `).join('');
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
</script>
</body>
</html>
"""
