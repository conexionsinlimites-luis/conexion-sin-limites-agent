# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor (Whapi, Meta, Twilio) gracias a la capa de providers.
"""

import re
import json
import asyncio
import logging
import traceback
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from agent.brain import generar_respuesta, client as claude_client
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
from agent.transcriber import transcribir
from agent.config import PORT, ENVIRONMENT, TELEFONO_OWNER, MAKE_WEBHOOK_TOKEN
import agent.crm as crm
from agent.scheduler import iniciar_scheduler
from agent.dashboard import router as dashboard_router, public_router as dashboard_public_router, broadcast_event
from agent.make_integration import enviar_a_make
from agent.database import get_pool, close_pool
from agent.campanas import inicializar_campanas

# Número del supervisor comercial que recibe alertas (mismo que TELEFONO_OWNER)
TELEFONO_SUPERVISOR = TELEFONO_OWNER

# Ruteo de alertas "Lead Listo" por producto detectado (ver _clasificar_producto_lead)
TELEFONO_ALERTA_DIRECTV = "56974394322"
TELEFONO_ALERTA_VTR_MOVISTAR = "56978016298"
_notificaciones_enviadas: dict = {}

# Slug del cliente activo — usado por prompt_builder para cargar config_json
CLIENTE_SLUG = "csl"


def _keyword_match(texto_lower: str, keywords: list[str]) -> bool:
    """
    Coincidencia de palabra completa para keywords de una sola palabra (evita
    falsos positivos por substring, ej. "curso" dentro de "concurso"/"recurso"/
    "transcurso", o "conserva" dentro de "conservar"). Las frases de varias
    palabras usan substring simple, ya que la coincidencia accidental de una
    frase completa es mucho menos probable.
    """
    for kw in keywords:
        if " " in kw:
            if kw in texto_lower:
                return True
        elif re.search(rf"\b{re.escape(kw)}\b", texto_lower):
            return True
    return False


def _clasificar_producto_lead(lead: dict | None, historial: list[dict]) -> str:
    """
    Clasifica el producto de un lead para rutear la alerta "Lead Listo",
    buscando menciones de compañía en el resumen del lead y en el historial
    de conversación (coincidencia de palabra completa). No modifica el campo
    `subproducto` en la BD — solo decide a qué número mandar la alerta.

    Returns: "directv", "vtr_movistar", u "otro".
    """
    texto = " ".join([
        (lead.get("lead_resumen") or "") if lead else "",
        (lead.get("subproducto") or "") if lead else "",
        *(m.get("mensaje", "") for m in historial),
    ]).lower()

    if re.search(r"\bdirectv\b", texto):
        return "directv"
    if re.search(r"\bvtr\b", texto) or re.search(r"\bmovistar\b", texto):
        return "vtr_movistar"
    return "otro"

# Configuración de logging según entorno
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

# Proveedor de WhatsApp (se configura en .env con WHATSAPP_PROVIDER)
proveedor = obtener_proveedor()

# Buffer en memoria con los últimos 20 eventos (para /debug)
_eventos = deque(maxlen=20)


def _log(nivel: str, mensaje: str):
    """Loggea y guarda en buffer de debug."""
    ts = datetime.utcnow().strftime("%H:%M:%S")
    entrada = {"ts": ts, "nivel": nivel, "msg": mensaje}
    _eventos.append(entrada)
    if nivel == "ERROR":
        logger.error(mensaje)
    else:
        logger.info(mensaje)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa el pool PostgreSQL, tablas y el scheduler de follow-ups."""
    await get_pool()
    _log("INFO", "Pool PostgreSQL inicializado")
    await crm.init_db()       # crea clientes primero (mensajes la referencia)
    await inicializar_db()
    await inicializar_campanas()
    _log("INFO", "Tablas PostgreSQL verificadas")
    _log("INFO", f"Servidor AgentKit en puerto {PORT}")
    _log("INFO", f"Proveedor: {proveedor.__class__.__name__}")

    # Arrancar scheduler de follow-ups en background
    tarea_scheduler = asyncio.create_task(iniciar_scheduler(proveedor))
    _log("INFO", "Scheduler de follow-ups iniciado")

    yield

    # Apagar: cancelar scheduler y cerrar pool
    tarea_scheduler.cancel()
    try:
        await tarea_scheduler
    except asyncio.CancelledError:
        pass
    await close_pool()
    _log("INFO", "Pool PostgreSQL cerrado")


app = FastAPI(
    title="AgentKit — Valentina | Conexion Sin Limites",
    version="1.0.0",
    lifespan=lifespan
)

app.include_router(dashboard_router)
app.include_router(dashboard_public_router)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway/monitoreo."""
    return {"status": "ok", "agente": "Valentina", "empresa": "Conexion Sin Limites"}


@app.get("/debug")
async def debug():
    """Muestra los últimos eventos del webhook para diagnóstico."""
    return {"eventos": list(_eventos)}


@app.get("/webhook")
@app.head("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET/HEAD del webhook — responde 200 a cualquier proveedor."""
    try:
        resultado = await proveedor.validar_webhook(request)
        if resultado is not None:
            return PlainTextResponse(str(resultado))
    except Exception:
        pass
    return PlainTextResponse("ok")


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.
    Siempre retorna 200 para evitar reintentos de Meta/Whapi.
    """
    # Capturar el body crudo para debug antes de parsearlo
    try:
        body_raw = await request.body()
        _log("INFO", f"Webhook recibido: {body_raw[:300].decode('utf-8', errors='replace')}")
    except Exception:
        pass

    # Parsear webhook
    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:
        _log("ERROR", f"Error parseando webhook: {e}")
        return {"status": "ok"}

    if not mensajes:
        _log("INFO", "Webhook sin mensajes de texto (probablemente status update)")
        return {"status": "ok"}

    for msg in mensajes:
        # Normalizar teléfono: eliminar espacios y sufijo @s.whatsapp.net / @c.us de Whapi
        raw = msg.telefono.replace(" ", "").strip()
        msg.telefono = raw.split("@")[0]

        if msg.es_propio or (not msg.texto and not msg.audio_id):
            _log("INFO", f"Mensaje ignorado — es_propio={msg.es_propio} texto='{msg.texto}'")
            continue

        if msg.telefono == "56941762315":
            _log("INFO", f"Mensaje del supervisor ignorado — {msg.telefono}")
            continue

        if msg.telefono in ("56974394322", "56978016298"):
            await _procesar_mensaje_dueño(msg.telefono, msg.texto)
            continue

        # Si es audio, transcribirlo antes de procesar
        if msg.audio_id:
            try:
                _log("INFO", f"Audio recibido de {msg.telefono} — transcribiendo con Whisper...")
                audio_bytes, mime_type = await proveedor.descargar_audio(msg.audio_id)
                msg.texto = await transcribir(audio_bytes, mime_type)
                if not msg.texto:
                    _log("ERROR", f"Transcripción vacía para audio de {msg.telefono}")
                    continue
                _log("INFO", f"Transcripción: '{msg.texto[:100]}'")
            except Exception as e:
                _log("ERROR", f"Error transcribiendo audio de {msg.telefono}: {e}")
                continue

        _log("INFO", f"Procesando mensaje de {msg.telefono}: '{msg.texto}'")

        try:
            # --- CRM: Registrar lead si es nuevo ---
            await crm.crear_o_actualizar_lead(msg.telefono)
            # Notificar al dashboard para que refresque el sidebar en tiempo real
            await broadcast_event({"type": "conversations_update"})

            # --- MODO HUMANO: si un agente tomó el lead, Valentina no responde ---
            lead_mode = await crm.obtener_lead(msg.telefono)
            if lead_mode and lead_mode.get("estado") == "modo_humano":
                await crm.guardar_mensaje(msg.telefono, "user", msg.texto, "modo_humano", None)
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await broadcast_event({
                    "type": "new_message", "telefono": msg.telefono,
                    "role": "user", "content": msg.texto,
                    "ts": datetime.utcnow().isoformat(),
                })
                _log("INFO", f"Lead {msg.telefono} en modo_humano — IA silenciada, mensaje guardado")
                continue

            # --- NOMBRE: extraer del mensaje y guardar si aún no hay nombre válido ---
            nombre_recien_capturado = False
            nombre_extraido = crm.extraer_nombre_de_mensaje(msg.texto)
            if nombre_extraido:
                nombre_recien_capturado = await crm.actualizar_nombre_si_desconocido(msg.telefono, nombre_extraido)
                if nombre_recien_capturado:
                    _log("INFO", f"Nombre capturado para {msg.telefono}: {nombre_extraido}")

            await crm.cancelar_followups(msg.telefono)

            # --- CRM: Detectar intención y objeción en el mensaje entrante ---
            intencion = crm.detectar_intencion(msg.texto)
            objecion  = crm.detectar_objecion(msg.texto)

            await crm.actualizar_score(msg.telefono, intencion, msg.texto)
            if objecion:
                await crm.guardar_objecion(msg.telefono, objecion)
                _log("INFO", f"Objecion detectada en {msg.telefono}: {objecion}")

            # --- CRM: Avanzar estado según intención ---
            lead = await crm.obtener_lead(msg.telefono)
            estado_actual = lead["estado"] if lead else "nuevo"
            nuevo_estado  = _calcular_nuevo_estado(estado_actual, intencion)

            if nuevo_estado != estado_actual:
                await crm.actualizar_estado(msg.telefono, nuevo_estado)
                estado_actual = nuevo_estado
                _log("INFO", f"Lead {msg.telefono} avanzó a estado: {estado_actual}")
                if nuevo_estado == "caliente":
                    await _enviar_notificacion_caliente(msg.telefono)
            else:
                await crm.incrementar_mensajes_estado(msg.telefono)

            # --- CRM: Detectar estancamiento ---
            lead_ref = await crm.obtener_lead(msg.telefono)
            if lead_ref and crm.detectar_estancamiento(lead_ref["mensajes_en_estado"], estado_actual):
                _log("INFO", f"Estancamiento detectado — {msg.telefono} lleva {lead_ref['mensajes_en_estado']} mensajes en '{estado_actual}'")

            # --- Make.com: notificar lead actualizado ---
            await enviar_a_make(
                telefono=msg.telefono,
                nombre=lead_ref.get("nombre", "") if lead_ref else "",
                estado=estado_actual,
                score=lead_ref.get("score", 0) if lead_ref else 0,
                producto=lead_ref.get("subproducto", "") if lead_ref else "",
                ultimo_mensaje=msg.texto,
                intencion=intencion,
            )

            historial_raw = await crm.obtener_historial(msg.telefono, limite=20)
            historial = [{"role": m.get("rol", m.get("role", "user")), "content": m.get("mensaje", m.get("content", ""))} for m in historial_raw]
            _log("INFO", f"Historial recuperado: {len(historial)} mensajes previos")

            # Extraer campos clave del lead para el prompt_builder
            nombre_cliente = (lead_ref.get("nombre") or "")       if lead_ref else ""
            cliente_id     = (lead_ref.get("cliente_id"))          if lead_ref else None
            estado_lead    = (lead_ref.get("estado") or "nuevo")   if lead_ref else "nuevo"
            resumen_lead   = (lead_ref.get("lead_resumen") or "")  if lead_ref else ""

            _log("INFO",
                f"prompt_builder: cliente_id={cliente_id} "
                f"estado={estado_lead} resumen={'sí' if resumen_lead else 'no'}"
            )

            # Detectar si el mensaje es sobre productos Hotmart → usar Constanza
            # (coincidencia de palabra completa — ver _keyword_match arriba)
            KEYWORDS_CONSTANZA = [
                "canva", "conservas", "envasar", "curso", "pdf", "hotmart",
                "emprender", "negocio desde casa", "salsas", "recetas",
                "diseño grafico", "arte de envasar", "mrr"
            ]
            texto_lower = msg.texto.lower()
            slug_activo = "constanza" if _keyword_match(texto_lower, KEYWORDS_CONSTANZA) else CLIENTE_SLUG
            cliente_id_activo = cliente_id
            if slug_activo == "constanza":
                cliente_id_activo = 5
                _log("INFO", f"Orquestador: derivando a Constanza — keyword detectada")

            respuesta = await generar_respuesta(
                msg.texto,
                historial,
                nombre_cliente,
                nombre_recien_capturado,
                telefono=msg.telefono,
                cliente_slug=slug_activo,
                cliente_id=cliente_id_activo,
                lead=lead_ref,
            )
            _log("INFO", f"Respuesta generada: '{respuesta[:100]}'")

            # Detectar marcador de alerta al supervisor y procesarlo antes de enviar al cliente
            respuesta_limpia, alerta = _extraer_alerta(respuesta)

            # Guardar en memoria conversacional y en historial CRM
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta_limpia)
            await crm.guardar_mensaje(msg.telefono, "user", msg.texto, estado_actual, intencion)
            await crm.guardar_mensaje(msg.telefono, "assistant", respuesta_limpia, estado_actual, None)

            # Notificar al Live Chat en tiempo real vía SSE
            ts_ahora = datetime.utcnow().isoformat()
            await broadcast_event({
                "type": "new_message", "telefono": msg.telefono,
                "role": "user", "content": msg.texto, "ts": ts_ahora,
            })
            await broadcast_event({
                "type": "new_message", "telefono": msg.telefono,
                "role": "assistant", "content": respuesta_limpia, "ts": ts_ahora,
            })

            # Actualizar resumen del lead en background — no bloquea el envío de respuesta
            asyncio.create_task(crm.actualizar_resumen_lead(msg.telefono))

            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta_limpia)
            if enviado:
                _log("INFO", f"Respuesta enviada OK a {msg.telefono}")
            else:
                _log("ERROR", f"enviar_mensaje falló para {msg.telefono} — revisar token/credenciales en Railway")

            # Programar follow-up automático: si el cliente no responde en 2h, Valentina lo recordará
            # La cadena completa es 2h → 24h → 3d → 30d → 60d (cada uno se encadena en scheduler.py)
            if estado_actual not in ("cerrado", "modo_humano"):
                try:
                    await crm.programar_followup(msg.telefono, "9h")
                    _log("INFO", f"Follow-up 9h programado para {msg.telefono}")
                except Exception as _fe:
                    _log("ERROR", f"Error programando follow-up: {_fe}")

            # Enviar alerta al supervisor y marcar lead como listo para cierre
            alerta_ya_enviada = False
            if alerta:
                await _enviar_alerta_supervisor(alerta, msg.telefono)
                alerta_ya_enviada = True
                await crm.actualizar_estado(msg.telefono, "listo_para_cierre")
                dir_ = alerta.get("dir", "")
                if dir_ and dir_ != "pendiente":
                    await crm.crear_o_actualizar_lead(msg.telefono, direccion=dir_)
                _log("INFO", f"Lead {msg.telefono} marcado como listo_para_cierre en CRM")

            # Extraer dirección del 📍 en la respuesta de Valentina y notificar supervisor
            # ([ \t]* en vez de \s*: no debe cruzar saltos de línea, o "captura" el párrafo siguiente)
            m_dir = re.search(r'📍[ \t]*([^\n]+)', respuesta_limpia)
            if m_dir and not alerta_ya_enviada:
                dir_extraida = m_dir.group(1).strip()
                if len(dir_extraida) > 5 and dir_extraida.lower() not in ("pendiente", "por confirmar"):
                    await crm.crear_o_actualizar_lead(msg.telefono, direccion=dir_extraida)
                    lead_final = await crm.obtener_lead(msg.telefono)
                    tiene_nombre = (lead_final.get("nombre") or "").strip().lower() not in ("", "desconocido", "cliente") if lead_final else False
                    ultimo_notif = _notificaciones_enviadas.get(msg.telefono, 0)
                    ahora = datetime.utcnow().timestamp()
                    if tiene_nombre and (ahora - ultimo_notif) > 3600:
                        _notificaciones_enviadas[msg.telefono] = ahora
                        await _enviar_alerta_supervisor(
                            {"dir": dir_extraida, "nombre": lead_final.get("nombre", ""), "tel": msg.telefono},
                            msg.telefono
                        )
                        _log("INFO", f"Alerta supervisor por dirección detectada — {msg.telefono}")

        except Exception as e:
            _log("ERROR", f"Error procesando mensaje de {msg.telefono}: {e}\n{traceback.format_exc()}")

    return {"status": "ok"}


def _calcular_nuevo_estado(estado_actual: str, intencion: str) -> str:
    """
    Avanza el estado del lead en la máquina de estados según la intención detectada.
    Nunca retrocede — solo avanza o se mantiene.
    """
    if estado_actual == "nuevo":
        return "contactado"
    if intencion == "alta" and estado_actual in ("contactado", "interesado", "tibio"):
        return "caliente"
    if intencion == "media" and estado_actual in ("contactado",):
        return "interesado"
    return estado_actual


def _extraer_alerta(respuesta: str) -> tuple[str, dict | None]:
    patron_alerta   = r'\[ALERTA_SUPERVISOR[^\]]*\]'
    patron_caliente = r'(?im)^[^\n]*\b(LEAD[\s_]CALIENTE|ALERTA[\s_]SUPERVISOR)\b[^\n]*$'
    patron_marcador = r'\[[A-Z_]+\|[^\]]*\]'
    patron_ficha    = r'(?s)-{3,}[\s\n]+[─━─]{5,}.*?[─━─]{5,}[\s\n]*'
    patron_ficha2   = r'(?s)[─━]{5,}.*?[─━]{5,}'

    tiene_alerta = bool(re.search(patron_alerta, respuesta))
    datos = None

    if tiene_alerta:
        def _campo(key):
            m = re.search(rf'{key}=([^|\]]*)', respuesta)
            return m.group(1).strip() if m else ""
        datos = {"nombre": _campo("nombre"), "tel": _campo("tel"), "dir": _campo("dir")}

    # Detectar ficha formateada con separadores ─── o ━━━
    if not datos:
        bloque = re.search(patron_ficha2, respuesta, re.DOTALL)
        if bloque:
            bloque_texto = bloque.group(0)
            def _extraer_campo(emoji, texto):
                m = re.search(rf'{emoji}\s*([^\n]+)', texto)
                return m.group(1).strip() if m else ""
            nombre = _extraer_campo('👤', bloque_texto).replace('Nombre:', '').replace('*','').strip()
            tel    = _extraer_campo('📱', bloque_texto).replace('Teléfono:', '').replace('+','').strip()
            dir_   = _extraer_campo('📍', bloque_texto).replace('Dirección:', '').strip()
            if dir_ and dir_ not in ('pendiente', 'Por confirmar', ''):
                datos = {"nombre": nombre, "tel": tel, "dir": dir_}

    # Limpiar todo lo que no debe llegar al cliente
    limpia = re.sub(patron_alerta,   "", respuesta)
    limpia = re.sub(patron_caliente, "", limpia, flags=re.IGNORECASE | re.MULTILINE)
    limpia = re.sub(patron_marcador, "", limpia)
    limpia = re.sub(patron_ficha2,   "", limpia, flags=re.DOTALL)
    limpia = re.sub(r'-{3,}',        "", limpia)
    limpia = re.sub(r'\n{3,}', '\n\n', limpia).strip()

    return limpia, datos


async def _enviar_notificacion_caliente(telefono_cliente: str):
    """Avisa al supervisor por WhatsApp cuando un lead se vuelve caliente."""
    lead = await crm.obtener_lead(telefono_cliente)
    if not lead:
        return
    nombre    = lead.get("nombre") or "Cliente"
    score     = lead.get("score", 0)
    producto  = lead.get("subproducto") or "Telecom"
    estado    = (lead.get("estado") or "caliente").upper()
    direccion = lead.get("direccion") or "pendiente"
    resumen   = lead.get("lead_resumen") or "—"
    tel       = telefono_cliente.replace("+", "").replace(" ", "").split("@")[0]
    wa_link   = f"https://wa.me/{tel}"

    mensaje = (
        f"🔥 *LEAD CALIENTE — VALENTINA*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *{nombre}*\n"
        f"📱 +{tel}\n"
        f"📍 {direccion}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 {producto}  •  ⭐ {score}/100\n"
        f"🔖 {estado}\n"
        f"📋 _{resumen}_\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 {wa_link}"
    )
    try:
        enviado = await proveedor.enviar_mensaje(TELEFONO_SUPERVISOR, mensaje)
        if enviado:
            _log("INFO", f"Notif. lead caliente enviada al supervisor — {nombre} ({tel})")
        else:
            _log("ERROR", f"Notif. lead caliente falló para {tel}")
    except Exception as e:
        _log("ERROR", f"Error notificando lead caliente: {e}")


_MENU_DUEÑO = (
    "No reconocí esa consulta. Por ahora puedo responder:\n"
    "• leads de hoy / de esta semana\n"
    "• respondieron vs no respondieron (envío masivo)\n\n"
    "Ventas cerradas, pendientes de llamar y desglose por producto: "
    "no tengo ese dato todavía en el sistema."
)


async def _generar_reporte_dueño(texto_lower: str) -> str:
    """
    Clasifica la pregunta del dueño por palabra clave (Parte 1: reportes de
    solo lectura) y devuelve la respuesta calculada con datos reales.

    Regla estricta: si la pregunta cae en un caso donde no hay un dato
    confiable hoy en la BD, responde exactamente "no tengo ese dato
    todavía" — nunca estima ni aproxima.
    """
    # Ventas cerradas -> el estado 'cerrado' nunca se usa para leads de
    # telecom (el bot solo llega hasta listo_para_cierre); el cierre real
    # lo hace un humano por teléfono y no queda registrado en el sistema.
    if _keyword_match(texto_lower, ["venta", "ventas"]) and \
       _keyword_match(texto_lower, ["cerrada", "cerradas", "cerrado", "cerrados"]):
        return "no tengo ese dato todavía"

    # Pendientes de llamar -> no existe ningún campo de "llamar en tal fecha"
    if _keyword_match(texto_lower, ["llamar"]):
        return "no tengo ese dato todavía"

    # Desglose por producto -> subproducto nunca tiene estos valores, y
    # lead_resumen (única fuente alternativa) cubre una muestra demasiado
    # chica para presentarla como un desglose real.
    if _keyword_match(texto_lower, ["directv", "vtr", "movistar", "claro"]) or \
       (_keyword_match(texto_lower, ["desglose"]) and _keyword_match(texto_lower, ["producto", "productos"])):
        return "no tengo ese dato todavía"

    # Respondieron vs no respondieron -> acotado a envío masivo (ver docstring
    # de contar_respondieron_envio_masivo en crm.py)
    if _keyword_match(texto_lower, ["respondieron"]):
        datos = await crm.contar_respondieron_envio_masivo()
        return (
            f"De los {datos['total']} contactos de envío masivo: "
            f"{datos['respondieron']} respondieron, "
            f"{datos['no_respondieron']} no respondieron."
        )

    # Leads/clientes ingresados hoy o esta semana
    contiene_conteo = _keyword_match(texto_lower, ["lead", "leads", "cliente", "clientes"])
    contiene_semana = _keyword_match(texto_lower, ["semana"])
    contiene_hoy    = _keyword_match(texto_lower, ["hoy"])
    if contiene_conteo and (contiene_semana or contiene_hoy):
        ahora = datetime.utcnow()
        if contiene_semana:
            desde = (ahora - timedelta(days=ahora.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            n = await crm.contar_leads_creados_desde(desde)
            return f"{n} leads ingresaron esta semana."
        else:
            desde = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
            n = await crm.contar_leads_creados_desde(desde)
            return f"{n} leads ingresaron hoy."

    return _MENU_DUEÑO


# ── Parte 2 — cargar ventas/leads con confirmación explícita ──────────────

# Propuesta pendiente por número de dueño, en memoria (se pierde si Railway
# redespliega entre la propuesta y la confirmación — limitación conocida).
_CARGA_PENDIENTE: dict[str, dict] = {}

_PALABRAS_CONFIRMACION = {"si", "sí", "confirmar", "confirmo", "dale", "ok", "okay", "correcto"}
_PALABRAS_CANCELACION = {"no", "cancelar", "cancela", "cancelalo", "cancélalo"}

_ETIQUETAS_CAMPOS = {
    "telefono": "el teléfono",
    "nombre": "el nombre",
    "producto": "el producto/compañía",
    "comuna": "la comuna",
    "direccion": "la dirección",
}

_HERRAMIENTA_CARGA_DUEÑO = {
    "name": "registrar_carga",
    "description": (
        "Extrae los datos de una venta cerrada o un lead nuevo que el dueño "
        "quiere cargar manualmente al CRM de Conexión Sin Límites."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tipo": {
                "type": "string",
                "enum": ["venta", "lead_nuevo"],
                "description": "'venta' si el dueño dice que ya se cerró/vendió; 'lead_nuevo' si es un contacto nuevo sin cerrar todavía.",
            },
            "nombre": {"type": "string", "description": "Nombre del cliente, si se menciona."},
            "telefono": {"type": "string", "description": "Teléfono del cliente, solo dígitos, con código de país (ej. 56912345678)."},
            "producto": {
                "type": "string",
                "enum": ["DirecTV", "VTR", "Movistar", "Claro", "Entel", "WOM", "otro"],
                "description": "Compañía/producto mencionado, si se menciona.",
            },
            "comuna": {"type": "string"},
            "direccion": {"type": "string"},
            "campos_faltantes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Nombres de campos importantes que el dueño todavía no mencionó (ej. ['telefono']). telefono y nombre siempre son importantes; producto es importante solo si tipo=venta.",
            },
            "listo_para_confirmar": {
                "type": "boolean",
                "description": "true solo si ya no faltan campos importantes.",
            },
        },
        "required": ["tipo", "campos_faltantes", "listo_para_confirmar"],
    },
}


async def _extraer_carga_dueño(mensaje_nuevo: str, datos_previos: dict | None) -> dict:
    """
    Llama a Claude para extraer/actualizar los datos de una carga de venta o
    lead nuevo pedida por el dueño. Nunca inventa datos que el dueño no
    mencionó — los deja fuera y los reporta en campos_faltantes.
    """
    contexto = ""
    if datos_previos:
        contexto = (
            "Datos ya recopilados hasta ahora en esta misma carga (el dueño "
            "puede estar completando lo que falta o corrigiendo algo):\n"
            f"{json.dumps(datos_previos, ensure_ascii=False)}\n\n"
        )

    respuesta = await claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=(
            "Extraes datos estructurados de mensajes del dueño de Conexión Sin "
            "Límites, que está cargando manualmente una venta cerrada o un lead "
            "nuevo al CRM por WhatsApp. Usa SIEMPRE la herramienta registrar_carga. "
            "NUNCA inventes ni asumas datos que el dueño no mencionó explícitamente."
        ),
        tools=[_HERRAMIENTA_CARGA_DUEÑO],
        tool_choice={"type": "tool", "name": "registrar_carga"},
        messages=[{"role": "user", "content": contexto + mensaje_nuevo}],
    )

    for bloque in respuesta.content:
        if bloque.type == "tool_use":
            return bloque.input
    raise RuntimeError("Claude no devolvió la herramienta registrar_carga")


def _validar_campos_obligatorios(datos: dict) -> list[str]:
    """Red de seguridad en Python — no confía solo en el criterio de la IA."""
    faltantes = list(datos.get("campos_faltantes") or [])
    if not datos.get("telefono") and "telefono" not in faltantes:
        faltantes.append("telefono")
    if not datos.get("nombre") and "nombre" not in faltantes:
        faltantes.append("nombre")
    if datos.get("tipo") == "venta" and not datos.get("producto") and "producto" not in faltantes:
        faltantes.append("producto")
    return faltantes


def _texto_confirmacion_carga(datos: dict) -> str:
    tipo_label = "Venta cerrada" if datos.get("tipo") == "venta" else "Lead nuevo"
    lineas = ["Voy a cargar esto — ¿confirmas?", "", f"Tipo: {tipo_label}"]
    if datos.get("nombre"):
        lineas.append(f"Nombre: {datos['nombre']}")
    if datos.get("telefono"):
        lineas.append(f"Teléfono: {datos['telefono']}")
    if datos.get("producto"):
        lineas.append(f"Producto: {datos['producto']}")
    if datos.get("comuna"):
        lineas.append(f"Comuna: {datos['comuna']}")
    if datos.get("direccion"):
        lineas.append(f"Dirección: {datos['direccion']}")
    lineas.append("")
    lineas.append('Responde "sí" para guardar, o "no" para cancelar.')
    return "\n".join(lineas)


def _texto_pregunta_faltantes(datos: dict, faltantes: list[str]) -> str:
    pedir = ", ".join(_ETIQUETAS_CAMPOS.get(f, f) for f in faltantes)
    conocido = [
        f"{k}: {v}" for k, v in datos.items()
        if k not in ("campos_faltantes", "listo_para_confirmar", "tipo") and v
    ]
    resumen = ("Hasta ahora tengo: " + ", ".join(conocido) + ".\n\n") if conocido else ""
    return f"{resumen}Me falta {pedir}. ¿Me lo pasas?"


async def _ejecutar_carga_dueño(datos: dict) -> str:
    """Escribe la carga confirmada en la BD. Solo se llama tras un 'sí' explícito."""
    telefono = (datos.get("telefono") or "").replace("+", "").replace(" ", "").replace("-", "")
    nombre = datos.get("nombre")
    producto = datos.get("producto")

    kwargs: dict = {"origen": "carga_manual"}
    if datos.get("comuna"):
        kwargs["comuna"] = datos["comuna"]
    if datos.get("direccion"):
        kwargs["direccion"] = datos["direccion"]
    if producto:
        kwargs["subproducto"] = producto
    if datos.get("tipo") == "venta":
        kwargs["estado"] = "cerrado"
        kwargs["score"] = 100
    # Si es lead_nuevo, no se toca 'estado': un lead ya existente conserva su
    # progreso real (no lo bajamos a 'nuevo' solo porque se recargó a mano).

    ya_existia = (await crm.obtener_lead(telefono)) is not None
    await crm.crear_o_actualizar_lead(telefono, nombre=nombre, **kwargs)

    accion = "actualicé el lead existente" if ya_existia else "creé un lead nuevo"
    return f"Listo, {accion} para {nombre or telefono} ✅"


async def _procesar_mensaje_dueño(telefono: str, texto: str):
    """
    Modo dueño — mensajes desde cualquiera de los dos números del dueño
    (56974394322, 56978016298) se enrutan aquí, sin distinción de producto.
    Parte 1: reportes de solo lectura. Parte 2: cargar ventas/leads con
    confirmación explícita antes de escribir en la BD.
    """
    texto_original = (texto or "").strip()
    texto_lower = texto_original.lower()

    try:
        pendiente = _CARGA_PENDIENTE.get(telefono)

        if pendiente and texto_lower in _PALABRAS_CONFIRMACION:
            respuesta = await _ejecutar_carga_dueño(pendiente["datos"])
            del _CARGA_PENDIENTE[telefono]

        elif pendiente and texto_lower in _PALABRAS_CANCELACION:
            del _CARGA_PENDIENTE[telefono]
            respuesta = "Cancelado, no se guardó nada."

        elif pendiente or _keyword_match(texto_lower, ["carga", "cargar"]):
            datos = await _extraer_carga_dueño(
                texto_original, pendiente["datos"] if pendiente else None
            )
            faltantes = _validar_campos_obligatorios(datos)
            _CARGA_PENDIENTE[telefono] = {"datos": datos}
            if faltantes:
                respuesta = _texto_pregunta_faltantes(datos, faltantes)
            else:
                respuesta = _texto_confirmacion_carga(datos)

        else:
            respuesta = await _generar_reporte_dueño(texto_lower)

    except Exception as e:
        _log("ERROR", f"Modo dueño: error procesando mensaje de {telefono}: {e}")
        respuesta = "Tuve un problema procesando eso. Intenta de nuevo en un momento."

    try:
        enviado = await proveedor.enviar_mensaje(telefono, respuesta)
        if enviado:
            _log("INFO", f"Modo dueño: respuesta enviada a {telefono}")
        else:
            _log("ERROR", f"Modo dueño: falló el envío a {telefono}")
    except Exception as e:
        _log("ERROR", f"Modo dueño: error enviando respuesta a {telefono}: {e}")


async def _enviar_alerta_supervisor(datos: dict, telefono_cliente: str):
    """
    Envía alerta enriquecida al supervisor cuando Valentina captura una
    dirección o detecta intención de contratar.
    Combina los datos del marcador con la info actualizada del CRM.
    """
    # Siempre usar el teléfono real del cliente; ignorar el marcador si contiene valores inútiles
    tel_marcador = (datos.get("tel") or "").replace("+","").replace(" ","").replace("-","").split("@")[0].strip()
    tel_base     = telefono_cliente.replace("+","").replace(" ","").replace("-","").split("@")[0]
    tel = tel_base if (not tel_marcador or tel_marcador in ("pendiente","desconocido","")) else tel_marcador
    dir_ = datos.get("dir", "pendiente")

    # Enriquecer con datos del CRM (nombre real, estado, score, resumen)
    lead = await crm.obtener_lead(telefono_cliente)
    if lead:
        nombre   = lead.get("nombre") or datos.get("nombre") or "Cliente"
        estado   = (lead.get("estado") or "—").upper()
        score    = lead.get("score", 0)
        producto = lead.get("subproducto") or "Telecom"
        resumen  = lead.get("lead_resumen") or "—"
    else:
        nombre   = datos.get("nombre") or "Cliente"
        estado   = "—"
        score    = 0
        producto = "Telecom"
        resumen  = "—"

    # Clasificar producto para decidir a qué número enviar la alerta
    historial = await crm.obtener_historial(telefono_cliente, limite=30)
    clasificacion = _clasificar_producto_lead(lead, historial)
    if clasificacion == "directv":
        destino = TELEFONO_ALERTA_DIRECTV
    elif clasificacion == "vtr_movistar":
        destino = TELEFONO_ALERTA_VTR_MOVISTAR
    else:
        destino = TELEFONO_SUPERVISOR

    # Dirección desde el marcador; si el CRM ya la tiene, usar la más completa
    if not dir_ or dir_ == "pendiente":
        dir_ = lead.get("direccion") or "pendiente" if lead else "pendiente"

    wa_link = f"https://wa.me/{tel}"

    mensaje = (
        f"🔥 *LEAD LISTO — CONEXIÓN SIN LÍMITES*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *{nombre}*\n"
        f"📱 +{tel}\n"
        f"📍 {dir_}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 {producto}\n"
        f"⭐ {estado}  •  {score}/100 pts\n"
        f"📋 _{resumen}_\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 Abrir chat: {wa_link}"
    )

    try:
        enviado = await proveedor.enviar_mensaje(destino, mensaje)
        if enviado:
            _log("INFO", f"Alerta supervisor enviada — {nombre} ({tel}) -> {destino} [{clasificacion}]")
        else:
            _log("ERROR", f"Alerta supervisor falló para {tel} — revisar credenciales")
    except Exception as e:
        _log("ERROR", f"Error enviando alerta supervisor: {e}")


# ═══════════════════════════════════════════════════════════════
# MAKE.COM — Webhook receptor de eventos externos
# ═══════════════════════════════════════════════════════════════

def _verificar_make_token(request: Request) -> bool:
    """Valida el header X-Make-Token contra MAKE_WEBHOOK_TOKEN."""
    if not MAKE_WEBHOOK_TOKEN:
        return False
    token_recibido = request.headers.get("X-Make-Token", "")
    import secrets as _secrets
    return _secrets.compare_digest(
        token_recibido.encode("utf-8"),
        MAKE_WEBHOOK_TOKEN.encode("utf-8"),
    )


@app.post("/webhook/make")
async def webhook_make(request: Request):
    """
    Receptor de eventos desde Make.com.

    Seguridad: requiere header X-Make-Token = MAKE_WEBHOOK_TOKEN.

    Eventos soportados:
      hotmart_compra         — nueva compra en Hotmart
      lead_frio_retargeting  — lista de leads fríos para reactivar
      score_alto_remarketing — lead caliente sin cierre
      comuna_registro        — actualiza la comuna del lead
    """
    from fastapi.responses import JSONResponse as _JR

    if not _verificar_make_token(request):
        _log("ERROR", "Make.com: token inválido o no configurado")
        return _JR({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return _JR({"error": "JSON inválido"}, status_code=400)

    tipo  = (body.get("tipo") or "").strip()
    datos = body.get("datos") or {}

    _log("INFO", f"Make.com evento recibido: tipo='{tipo}'")

    # ── 1. hotmart_compra ──────────────────────────────────────────
    if tipo == "hotmart_compra":
        nombre   = (datos.get("nombre") or "").strip()
        email    = (datos.get("email") or "").strip()
        telefono = (datos.get("telefono") or "").strip().replace("+", "").replace(" ", "")
        producto = (datos.get("producto") or "").strip()
        precio   = datos.get("precio") or ""

        if not telefono:
            return _JR({"error": "telefono requerido"}, status_code=400)

        # Crear o actualizar lead con los datos de compra
        await crm.crear_o_actualizar_lead(telefono, nombre=nombre)
        await crm.actualizar_estado(telefono, "cerrado")
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE leads SET subproducto=$1, email=$2 WHERE telefono=$3",
                producto, email, telefono,
            )

        # Mensaje de bienvenida personalizado
        msg_bienvenida = (
            f"¡Hola {nombre or 'bienvenido'}! 🎉\n\n"
            f"Confirmamos tu compra de *{producto}*"
            + (f" por ${precio}" if precio else "") + ".\n\n"
            f"Soy Valentina de *Conexión Sin Límites* y estaré aquí para ayudarte "
            f"con todo lo que necesites.\n\n"
            f"¿Tienes alguna pregunta sobre tu nuevo servicio? 😊"
        )
        await proveedor.enviar_mensaje(telefono, msg_bienvenida)
        await crm.guardar_mensaje(telefono, "assistant", msg_bienvenida, "cerrado", None)
        await guardar_mensaje(telefono, "assistant", msg_bienvenida)

        _log("INFO", f"Make.com hotmart_compra: lead {telefono} ({nombre}) — {producto}")
        return _JR({"ok": True, "telefono": telefono, "estado": "cerrado"})

    # ── 2. lead_frio_retargeting ────────────────────────────────────
    elif tipo == "lead_frio_retargeting":
        telefonos = datos.get("telefonos") or []
        if isinstance(telefonos, str):
            telefonos = [t.strip() for t in telefonos.split(",") if t.strip()]
        if not telefonos:
            return _JR({"error": "lista de telefonos vacía"}, status_code=400)

        msg_reactivacion = (
            datos.get("mensaje") or
            "¡Hola! 👋 Te escribo desde *Conexión Sin Límites*.\n\n"
            "Hace un tiempo estuviste consultando sobre nuestros servicios de internet y TV. "
            "¿Sigues interesado? Tenemos nuevas promociones disponibles en tu zona. 🚀\n\n"
            "¿Cuándo podríamos conversar?"
        )

        enviados = fallidos = 0
        for tel in telefonos:
            tel = tel.replace("+", "").replace(" ", "")
            try:
                ok = await proveedor.enviar_mensaje(tel, msg_reactivacion)
                if ok:
                    enviados += 1
                    await crm.guardar_mensaje(tel, "assistant", msg_reactivacion, None, None)
                    await guardar_mensaje(tel, "assistant", msg_reactivacion)
                else:
                    fallidos += 1
            except Exception as e:
                fallidos += 1
                _log("ERROR", f"Make.com retargeting {tel}: {e}")
            await asyncio.sleep(0.1)  # rate limiting 10 msg/s

        _log("INFO", f"Make.com lead_frio_retargeting: {enviados} enviados, {fallidos} fallidos")
        return _JR({"ok": True, "enviados": enviados, "fallidos": fallidos})

    # ── 3. score_alto_remarketing ────────────────────────────────────
    elif tipo == "score_alto_remarketing":
        telefono = (datos.get("telefono") or "").strip().replace("+", "").replace(" ", "")
        if not telefono:
            return _JR({"error": "telefono requerido"}, status_code=400)

        lead = await crm.obtener_lead(telefono)
        nombre   = (lead.get("nombre") or "").strip() if lead else ""
        producto = (lead.get("subproducto") or "").strip() if lead else ""
        score    = lead.get("score", 0) if lead else 0

        # Mensaje de remarketing personalizado
        nombre_str  = f"{nombre}, " if nombre else ""
        producto_str = f" de *{producto}*" if producto else ""

        msg_remarketing = (
            datos.get("mensaje") or
            f"¡Hola {nombre_str}espero que estés bien! 😊\n\n"
            f"Vi que estuviste muy interesado en nuestro servicio{producto_str}. "
            f"Quería saber si todavía tienes la consulta activa y si puedo ayudarte a cerrar los detalles.\n\n"
            f"¿Tienes unos minutos para conversar hoy? 📞"
        )

        ok = await proveedor.enviar_mensaje(telefono, msg_remarketing)
        if ok:
            await crm.guardar_mensaje(telefono, "assistant", msg_remarketing, None, None)
            await guardar_mensaje(telefono, "assistant", msg_remarketing)

        _log("INFO", f"Make.com score_alto_remarketing: {telefono} (score={score}) enviado={ok}")
        return _JR({"ok": ok, "telefono": telefono, "score": score})

    # ── 4. comuna_registro ───────────────────────────────────────────
    elif tipo == "comuna_registro":
        telefono = (datos.get("telefono") or "").strip().replace("+", "").replace(" ", "")
        comuna   = (datos.get("comuna") or "").strip()
        if not telefono or not comuna:
            return _JR({"error": "telefono y comuna requeridos"}, status_code=400)

        pool = await get_pool()
        async with pool.acquire() as conn:
            updated = await conn.fetchval(
                "UPDATE leads SET comuna=$1 WHERE telefono=$2 RETURNING telefono",
                comuna, telefono,
            )
        if not updated:
            # Lead no existe aún — crearlo
            await crm.crear_o_actualizar_lead(telefono)
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE leads SET comuna=$1 WHERE telefono=$2", comuna, telefono
                )

        _log("INFO", f"Make.com comuna_registro: {telefono} → {comuna}")
        return _JR({"ok": True, "telefono": telefono, "comuna": comuna})

    else:
        _log("ERROR", f"Make.com: tipo desconocido '{tipo}'")
        return _JR({"error": f"tipo desconocido: {tipo}"}, status_code=400)
