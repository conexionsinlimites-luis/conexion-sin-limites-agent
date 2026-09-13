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
  window.visualViewport.addEventListener('resize', () => {
    setAppHeight();
    // Ajustar chat cuando sube el teclado
    const chatMain = document.querySelector('.chat-main');
    if (chatMain) {
      const vh = window.visualViewport.height;
      const rect = chatMain.getBoundingClientRect();
      const available = vh - rect.top - 10;
      if (available > 150) chatMain.style.height = available + 'px';
    }
  });
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
    // Render cards estilo WhatsApp
    const cards = (d.muestra || []).map(m => {
      const inicial = (m.nombre || m.telefono || '?')[0].toUpperCase();
      const colores = ['#00D4FF','#c084fc','#f59e0b','#22c55e','#ef4444','#3b82f6'];
      const color = colores[inicial.charCodeAt(0) % colores.length];
      const estadoColor = CPN_ESTADO_COLOR[m.estado] || '#888';
      return `<div style="display:flex;align-items:center;gap:.75rem;padding:.5rem .75rem;background:rgba(255,255,255,.03);border-radius:8px;border:1px solid rgba(255,255,255,.06)">
        <div style="width:36px;height:36px;border-radius:50%;background:${color}22;border:1.5px solid ${color}55;display:flex;align-items:center;justify-content:center;font-weight:700;color:${color};font-size:.85rem;flex-shrink:0">${inicial}</div>
        <div style="min-width:0;flex:1">
          <div style="font-size:.82rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(m.nombre || m.telefono)}</div>
          <div style="font-size:.68rem;color:#666;font-family:monospace">+${m.telefono} · ${m.comuna || '—'}</div>
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:.2rem;flex-shrink:0">
          <span style="background:${estadoColor}22;color:${estadoColor};border:1px solid ${estadoColor}44;border-radius:6px;padding:.1rem .4rem;font-size:.62rem;font-weight:700">${m.estado}</span>
          <span style="font-size:.62rem;color:${m.score>=70?'#ef4444':m.score>=40?'#f59e0b':'#555'}">${m.score}pts</span>
        </div>
      </div>`;
    }).join('');

    const extraHtml = d.total > 5
      ? `<div style="text-align:center;font-size:.72rem;color:#555;padding:.5rem">+ ${d.total - 5} contactos más...</div>`
      : '';

    document.getElementById('ncpn-preview-count').textContent = d.total;
    document.getElementById('ncpn-preview-sub').textContent = `lead${d.total!==1?'s':''} recibirá${d.total!==1?'n':''} esta campaña`;
    document.getElementById('ncpn-preview-cards').innerHTML = cards + extraHtml;
    document.getElementById('ncpn-preview-box').style.display = 'block';
  } catch(e) {
    document.getElementById('ncpn-preview-cards').innerHTML = '<div style="color:#ef4444;font-size:.78rem">Error: ' + e.message + '</div>';
    document.getElementById('ncpn-preview-box').style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = '👁 Ver destinatarios';
  }
}

async function crearCampana() {
  const nombre   = document.getElementById('ncpn-nombre').value.trim();
  const template = document.getElementById('ncpn-template') ? document.getElementById('ncpn-template').value : 'bienvenida_conexion';
  const mensaje  = template;
  if (!nombre) { alert('El nombre de la campaña es requerido'); return; }
  const btn = document.getElementById('ncpn-create-btn');
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
        limite:    parseInt(document.getElementById('ncpn-limite').value) || 0,
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


