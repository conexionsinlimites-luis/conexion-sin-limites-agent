# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor (Whapi, Meta, Twilio) gracias a la capa de providers.
"""

import re
import csv
import io
import json
import yaml
import asyncio
import logging
import traceback
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from agent.brain import generar_respuesta, client as claude_client
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
import agent.prompt_builder as prompt_builder
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

            # Detectar marcador de duda (Valentina prometió revisar algo con el ejecutivo)
            respuesta_sin_duda, duda_pregunta = _extraer_alerta_duda(respuesta)

            # Detectar marcador de alerta al supervisor y procesarlo antes de enviar al cliente
            respuesta_limpia, alerta = _extraer_alerta(respuesta_sin_duda)

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

            # Valentina prometió revisar algo con el ejecutivo — avisarle de verdad
            if duda_pregunta:
                await _enviar_alerta_duda(msg.telefono, duda_pregunta)

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


def _extraer_alerta_duda(respuesta: str) -> tuple[str, str | None]:
    """
    Detecta el marcador [ALERTA_DUDA|pregunta=...] que Valentina agrega
    cuando le dice al cliente que va a revisar algo con el ejecutivo (ver
    "Cuándo derivar al ejecutivo humano" en config/comportamiento.md).
    Se extrae ANTES de pasar por _extraer_alerta, cuyo patron_marcador
    genérico también lo limpiaría del texto pero sin capturar la pregunta.
    """
    patron = r'\[ALERTA_DUDA\|pregunta=([^\]]*)\]'
    m = re.search(patron, respuesta)
    if not m:
        return respuesta, None
    pregunta = m.group(1).strip()
    limpia = re.sub(patron, "", respuesta).strip()
    return limpia, (pregunta or None)


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


async def _enviar_alerta_duda(telefono_cliente: str, pregunta: str):
    """
    Avisa al supervisor por WhatsApp cuando Valentina le dijo al cliente
    que iba a revisar algo con el ejecutivo (marcador [ALERTA_DUDA]).
    Sin esta notificación real, esa promesa quedaría vacía.
    """
    lead    = await crm.obtener_lead(telefono_cliente)
    nombre  = (lead.get("nombre") if lead else "") or "Cliente"
    tel     = telefono_cliente.replace("+", "").replace(" ", "").split("@")[0]
    wa_link = f"https://wa.me/{tel}"

    mensaje = (
        f"❓ *DUDA SIN RESOLVER — VALENTINA*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *{nombre}*\n"
        f"📱 +{tel}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"❓ {pregunta}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 {wa_link}"
    )
    try:
        enviado = await proveedor.enviar_mensaje(TELEFONO_SUPERVISOR, mensaje)
        if enviado:
            _log("INFO", f"Alerta de duda enviada al supervisor — {nombre} ({tel}): {pregunta[:60]}")
        else:
            _log("ERROR", f"Alerta de duda falló para {tel}")
    except Exception as e:
        _log("ERROR", f"Error enviando alerta de duda: {e}")


_ZONA_CHILE = ZoneInfo("America/Santiago")

_HERRAMIENTAS_CONSULTA_DUEÑO = [
    {
        "name": "leads_nuevos",
        "description": (
            "Cuenta cuántos leads NUEVOS (primer contacto con Valentina) se "
            "registraron en un período, y devuelve además el detalle "
            "(nombre, teléfono, producto, estado) de hasta los últimos 10. "
            "Úsala tanto para '¿cuántos leads/clientes nuevos entraron "
            "hoy/esta semana/en [mes]?' como para 'dame un ejemplo de un "
            "lead reciente', 'pásame el teléfono de algún lead nuevo' o "
            "'cuáles fueron los últimos leads que entraron'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string", "description": "Fecha de inicio del período, formato YYYY-MM-DD, horario de Chile."},
                "fecha_hasta": {
                    "type": "string",
                    "description": (
                        "Fecha de fin del período, formato YYYY-MM-DD, SIEMPRE obligatoria. "
                        "Igual a fecha_desde si la pregunta es de un solo día. Para preguntas "
                        "abiertas como 'reciente' o 'últimamente' (sin límite claro), usa como "
                        "fecha_hasta el día de HOY — nunca la dejes igual a fecha_desde si el "
                        "período pretende cubrir varios días."
                    ),
                },
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "contactos_activos",
        "description": (
            "Cuenta cuántos contactos distintos escribieron al menos un "
            "mensaje en un período, sin importar si son leads nuevos o "
            "antiguos, y devuelve además el detalle (nombre, teléfono, hora "
            "del último mensaje) de hasta los últimos 10. Úsala tanto para "
            "'¿cuánta gente escribió esta semana?' — a diferencia de "
            "leads_nuevos, que solo cuenta el primer contacto — como para "
            "'¿quién me escribió hoy?' o 'dame un ejemplo de alguien que "
            "escribió'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string", "description": "Fecha de inicio, YYYY-MM-DD, horario Chile."},
                "fecha_hasta": {
                    "type": "string",
                    "description": (
                        "Fecha de fin, YYYY-MM-DD, SIEMPRE obligatoria. Igual a fecha_desde si "
                        "es un solo día. Para preguntas abiertas como 'reciente' o "
                        "'últimamente', usa como fecha_hasta el día de HOY — nunca la dejes "
                        "igual a fecha_desde si el período pretende cubrir varios días. Si la "
                        "pregunta menciona días separados (ej. 'hoy y ayer'), haz una llamada "
                        "por cada día en vez de una sola con un rango amplio."
                    ),
                },
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "ventas_cerradas",
        "description": (
            "Cuenta ventas marcadas como cerradas en un período. IMPORTANTE: "
            "estas ventas son solo las que el dueño cargó manualmente por "
            "WhatsApp (modo dueño, comando 'carga') — no incluyen cierres "
            "por llamada telefónica que él no haya cargado a mano."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string", "description": "Fecha de inicio, YYYY-MM-DD, horario Chile."},
                "fecha_hasta": {
                    "type": "string",
                    "description": (
                        "Fecha de fin, YYYY-MM-DD, SIEMPRE obligatoria. Igual a fecha_desde si "
                        "es un solo día. Para preguntas abiertas como 'reciente' o "
                        "'últimamente', usa como fecha_hasta el día de HOY — nunca la dejes "
                        "igual a fecha_desde si el período pretende cubrir varios días."
                    ),
                },
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "buscar_lead_por_telefono",
        "description": "Busca los datos de un lead específico por su número de teléfono.",
        "input_schema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string", "description": "Número de teléfono mencionado, solo dígitos, con código de país si se conoce."},
            },
            "required": ["telefono"],
        },
    },
    {
        "name": "buscar_lead_por_nombre",
        "description": "Busca lead(s) por coincidencia de nombre. Puede devolver varios resultados.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre o parte del nombre mencionado."},
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "buscar_venta_por_telefono",
        "description": (
            "Busca el registro COMPLETO de una venta (RUT, plan, dirección, "
            "forma de pago, decos, extras, checklist operativo, etc.) por "
            "teléfono. Úsala para preguntas de un campo puntual de una venta "
            "ya cargada, ej. 'dame el RUT de ese cliente' o 'a qué dirección "
            "va la instalación de Juan' — trae el registro completo y deja "
            "que tú extraigas solo lo que se preguntó."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string", "description": "Número de teléfono mencionado."},
            },
            "required": ["telefono"],
        },
    },
    {
        "name": "buscar_venta_por_nombre",
        "description": "Igual que buscar_venta_por_telefono, pero busca por coincidencia de nombre. Puede devolver varias ventas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre o parte del nombre mencionado."},
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "exportar_datos",
        "description": (
            "Úsala cuando el dueño pida 'exportar', 'dame todo', 'mándame la "
            "lista completa', o similar, de ventas o leads en un período. "
            "IMPORTANTE: esta herramienta NUNCA entrega los datos "
            "directamente — solo registra qué quiere exportar. El sistema le "
            "pregunta al dueño el formato (archivo o chat) antes de actuar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "que_exportar": {
                    "type": "string",
                    "enum": ["ventas", "leads"],
                    "description": "Qué tabla exportar.",
                },
                "fecha_desde": {"type": "string", "description": "Fecha de inicio, YYYY-MM-DD, horario Chile."},
                "fecha_hasta": {"type": "string", "description": "Fecha de fin, YYYY-MM-DD, SIEMPRE obligatoria."},
            },
            "required": ["que_exportar", "fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "consultar_catalogo",
        "description": (
            "Responde preguntas sobre el catálogo de planes y precios "
            "(DirecTV, VTR, Movistar) y calcula escenarios combinados (ej. "
            "un plan + decos/extensores adicionales). Úsala para '¿qué "
            "incluye el plan X?', '¿cuánto cuesta...?', o cualquier "
            "combinación de plan + extras. NO la confundas con las "
            "funciones de CRM (leads_nuevos, ventas_cerradas, etc.) — esas "
            "son datos de CLIENTES reales, esta es el catálogo de "
            "PRODUCTOS."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pregunta": {
                    "type": "string",
                    "description": "La pregunta de catálogo/precio tal cual la escribió el dueño, sin modificar ni resumir.",
                },
            },
            "required": ["pregunta"],
        },
    },
    {
        "name": "respondieron_envio_masivo",
        "description": (
            "Cuenta cuántos contactos del último envío masivo respondieron "
            "vs. no respondieron. Solo para preguntas sobre 'respondieron' "
            "en el contexto de un envío masivo."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "comision_mes",
        "description": (
            "Calcula la comisión/sueldo del mes combinando las 3 reglas "
            "confirmadas (DirecTV por puntos, VTR+Claro y Movistar por "
            "RGU), usando las ventas reales cargadas en el sistema en ese "
            "período. Úsala para '¿cuánto es mi comisión este mes?', "
            "'¿cuánto llevo de sueldo?', '¿cómo voy este mes?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string", "description": "Fecha de inicio, YYYY-MM-DD, horario Chile."},
                "fecha_hasta": {
                    "type": "string",
                    "description": (
                        "Fecha de fin, YYYY-MM-DD, SIEMPRE obligatoria. Para 'este mes', usa "
                        "el día 1 del mes actual como fecha_desde y hoy como fecha_hasta."
                    ),
                },
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "dato_no_registrado",
        "description": (
            "Úsala cuando la pregunta del dueño NO se puede responder con "
            "ningún dato real disponible en el sistema (ej. pendientes de "
            "llamar, desglose de ventas por producto, proyecciones de "
            "ingresos). Nunca inventes ni estimes un número — usa esta "
            "función en su lugar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "razon": {"type": "string", "description": "Explicación breve de por qué no existe ese dato en el sistema."},
            },
            "required": ["razon"],
        },
    },
]


# Historial reciente de la conversación del modo dueño, en memoria — mismo
# patrón y misma limitación conocida que _CARGA_PENDIENTE/_EXPORT_PENDIENTE
# (se pierde si Railway redespliega a mitad de una conversación; se
# reconstruye solo en los próximos mensajes). Sin esto, cada pregunta se
# procesaba aislada y el router no podía resolver referencias como "esos
# dos" o "el mismo período" a preguntas anteriores.
_HISTORIAL_DUEÑO: dict[str, list[dict]] = {}
_LIMITE_TURNOS_HISTORIAL = 6                    # últimos N intercambios pregunta+respuesta
_LIMITE_CARACTERES_RESPUESTA_HISTORIAL = 500    # evita que un resumen largo (ej. export en chat) infle el contexto en turnos futuros


def _guardar_turno_historial_dueño(telefono: str, pregunta: str, respuesta: str) -> None:
    """Guarda un intercambio pregunta/respuesta y descarta los más viejos si se pasa del límite."""
    turnos = _HISTORIAL_DUEÑO.setdefault(telefono, [])
    turnos.append({"pregunta": pregunta, "respuesta": respuesta[:_LIMITE_CARACTERES_RESPUESTA_HISTORIAL]})
    del turnos[:-_LIMITE_TURNOS_HISTORIAL]


async def _rutear_consulta_dueño(pregunta: str, telefono: str) -> list[tuple[str, dict]]:
    """
    Llamada 1 de 2 (Haiku): elige qué función(es) de consulta responden la
    pregunta del dueño y con qué parámetros. tool_choice="any" obliga a
    Claude a SIEMPRE elegir al menos una herramienta — nunca responde en
    texto libre. Puede elegir más de una (ej. "hoy y ayer" -> dos llamadas
    de contactos_activos, una por día) — se ejecutan y formatean todas.

    Recibe el historial reciente de _HISTORIAL_DUEÑO como turnos previos de
    la conversación, para poder resolver referencias como "esos dos leads"
    o "el mismo período" sin que el dueño tenga que repetir el contexto.
    """
    ahora_chile = datetime.now(_ZONA_CHILE)
    inicio_semana = ahora_chile - timedelta(days=ahora_chile.weekday())
    contexto_fecha = (
        f"Hoy es {ahora_chile.strftime('%Y-%m-%d')} (horario de Chile). "
        f"Esta semana empezó el {inicio_semana.strftime('%Y-%m-%d')}."
    )

    mensajes = []
    for turno in _HISTORIAL_DUEÑO.get(telefono, []):
        mensajes.append({"role": "user", "content": turno["pregunta"]})
        mensajes.append({"role": "assistant", "content": turno["respuesta"]})
    mensajes.append({"role": "user", "content": pregunta})

    respuesta = await claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=(
            "Eres el router de consultas del modo dueño de Conexión Sin "
            "Límites. El dueño pregunta en lenguaje natural sobre su CRM de "
            "WhatsApp. Tu único trabajo es elegir la herramienta correcta y "
            "sus parámetros — nunca respondes la pregunta directamente. "
            f"{contexto_fecha} Si la pregunta no encaja claramente en "
            "ninguna herramienta de datos, usa dato_no_registrado. "
            "Si la pregunta actual NO menciona un período/fecha explícito "
            "pero la conversación anterior sí estableció uno (ej. 'del 8 a "
            "la fecha'), usa ESE MISMO período — no asumas 'hoy' por "
            "defecto cuando el contexto ya estableció otro rango. Lo mismo "
            "aplica a referencias como 'esos dos', 'ese cliente', 'el "
            "mismo período': resuélvelas con la conversación anterior, no "
            "las trates como una pregunta nueva sin contexto."
        ),
        tools=_HERRAMIENTAS_CONSULTA_DUEÑO,
        tool_choice={"type": "any"},
        messages=mensajes,
    )

    llamadas = [b for b in respuesta.content if b.type == "tool_use"]
    _log(
        "INFO",
        f"Modo dueño: router eligió {[(b.name, b.input) for b in llamadas]} "
        f"para la pregunta: {pregunta!r}"
    )
    if not llamadas:
        raise RuntimeError("Haiku no eligió ninguna herramienta de consulta")
    return [(b.name, (b.input or {})) for b in llamadas]


async def _consultar_catalogo_interno(pregunta: str) -> str:
    """
    Responde una pregunta de catálogo/precios usando el mismo cerebro que
    Valentina (Sonnet + catálogo completo de config/prompts.yaml), pero en
    modo verificación interna del dueño — nunca en tono de venta. Se carga
    el system_prompt crudo del YAML (no via prompt_builder.construir_prompt,
    que agrega captura de nombre/estado de lead/fases de venta que no
    aplican acá) y se le agrega el bloque de modo verificación al final.
    """
    with open("config/prompts.yaml", "r", encoding="utf-8") as f:
        catalogo_completo = yaml.safe_load(f)["system_prompt"]

    system_prompt = catalogo_completo + (
        "\n\n─────────────────────────────────────────────\n"
        "MODO VERIFICACIÓN INTERNA DEL DUEÑO — ESTO NO ES UN CLIENTE\n"
        "Quien pregunta es Luis Barrios, el dueño del negocio, verificando "
        "internamente los precios y cálculos del catálogo de arriba ANTES "
        "de que lleguen a un cliente real.\n"
        "→ NO uses tono de venta, ni frases de cierre, ni emojis, ni las "
        "7 fases del flujo de venta — esto NO es una conversación comercial.\n"
        "→ Responde directo, tipo ficha técnica: el o los precios, el "
        "cálculo paso a paso si hay más de un ítem, y el total.\n"
        "→ Si la pregunta involucra un dato que NO está en el catálogo de "
        "arriba, dilo explícito ('eso no está confirmado en el catálogo') "
        "— NUNCA inventes ni asumas un precio o característica no listada.\n"
        "→ NO pidas dirección, NO ofrezcas agendar, NO derives a un "
        "supervisor, NO agregues marcadores [ALERTA_...] — esto es una "
        "consulta interna, no un lead real.\n"
        "─────────────────────────────────────────────"
    )

    respuesta = await claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system=system_prompt,
        messages=[{"role": "user", "content": pregunta}],
    )
    return "".join(b.text for b in respuesta.content if b.type == "text")


async def _ejecutar_consulta_dueño(nombre_funcion: str, parametros: dict) -> dict:
    """Ejecuta contra el CRM la función elegida por el router y devuelve datos crudos."""
    hoy_chile = datetime.now(_ZONA_CHILE).strftime("%Y-%m-%d")

    if nombre_funcion in ("leads_nuevos", "contactos_activos", "ventas_cerradas", "comision_mes"):
        # Red de seguridad: fecha_hasta ya es obligatoria en el schema, pero
        # si igual llega vacía, un rango amplio (hasta hoy) es un fallo más
        # seguro que uno angosto (= fecha_desde, que puede excluir días
        # reales del período que el dueño quería consultar).
        fecha_desde = parametros.get("fecha_desde") or hoy_chile
        fecha_hasta = parametros.get("fecha_hasta") or hoy_chile
        funcion_crm = getattr(crm, nombre_funcion)
        return await funcion_crm(fecha_desde, fecha_hasta)

    if nombre_funcion == "buscar_lead_por_telefono":
        lead = await crm.buscar_lead_por_telefono(parametros.get("telefono", ""))
        return {"encontrado": lead is not None, "lead": lead}

    if nombre_funcion == "buscar_lead_por_nombre":
        leads = await crm.buscar_lead_por_nombre(parametros.get("nombre", ""))
        return {"total": len(leads), "leads": leads}

    if nombre_funcion == "buscar_venta_por_telefono":
        venta = await crm.buscar_venta_por_telefono(parametros.get("telefono", ""))
        return {"encontrada": venta is not None, "venta": venta}

    if nombre_funcion == "buscar_venta_por_nombre":
        ventas = await crm.buscar_venta_por_nombre(parametros.get("nombre", ""))
        return {"total": len(ventas), "ventas": ventas}

    if nombre_funcion == "respondieron_envio_masivo":
        return await crm.contar_respondieron_envio_masivo()

    if nombre_funcion == "consultar_catalogo":
        respuesta_directa = await _consultar_catalogo_interno(parametros.get("pregunta", ""))
        return {"respuesta_directa": respuesta_directa}

    if nombre_funcion == "dato_no_registrado":
        return {"razon": parametros.get("razon", "")}

    raise ValueError(f"Función de consulta desconocida: {nombre_funcion}")


async def _formatear_respuesta_dueño(pregunta: str, resultados: list[tuple[str, dict]]) -> str:
    """
    Llamada 2 de 2 (Haiku): redacta la respuesta final en español a partir
    ÚNICAMENTE de los datos ya calculados en Python — nunca toca la BD, así
    que no puede inventar una cifra que no esté en `resultados`. Puede
    recibir más de un resultado (ej. "hoy y ayer" -> dos llamadas de
    contactos_activos) y debe combinarlos en una sola respuesta coherente.
    """
    # consultar_catalogo ya devuelve una respuesta completa en lenguaje
    # natural (Sonnet, no datos crudos) — pasarla por Haiku de nuevo solo
    # arriesgaría recortarla al límite de "máximo 4 líneas" del formateador
    # genérico, además de gastar una llamada de más sin necesidad.
    if len(resultados) == 1 and resultados[0][0] == "consultar_catalogo":
        return resultados[0][1]["respuesta_directa"]

    # Si TODOS los resultados son "no sé", no hace falta gastar una llamada
    # a Haiku — se responde directo con las razones (sin duplicar si se repiten).
    if all(nombre == "dato_no_registrado" for nombre, _ in resultados):
        razones = [d.get("razon", "").strip() for _, d in resultados if d.get("razon", "").strip()]
        razon_texto = " ".join(dict.fromkeys(razones))
        return f"No tengo ese dato todavía en el sistema.{(' ' + razon_texto) if razon_texto else ''}"

    aviso_extra = ""
    if any(nombre == "ventas_cerradas" for nombre, _ in resultados):
        aviso_extra = (
            " Aclara siempre, en la misma respuesta, que estas ventas son "
            "las que el dueño (Luis) cargó manualmente por WhatsApp — no "
            "incluyen cierres por llamada telefónica que él no haya "
            "cargado a mano."
        )

    bloques_datos = "\n\n".join(
        f"Resultado {i + 1} (función {nombre}):\n{json.dumps(datos, ensure_ascii=False, default=str)}"
        for i, (nombre, datos) in enumerate(resultados)
    )

    respuesta = await claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=(
            "Redactas la respuesta de WhatsApp para el dueño de Conexión "
            "Sin Límites, basada EXCLUSIVAMENTE en los datos entregados. "
            "Puede haber más de un resultado (ej. una pregunta que mezcla "
            "'hoy y ayer') — combínalos en una sola respuesta coherente que "
            "cubra cada parte de la pregunta. Nunca inventes ni asumas un "
            "número que no esté en los datos. Si alguno de los resultados es "
            "dato_no_registrado mientras otros sí tienen datos reales, "
            "acláralo brevemente para esa parte y responde igual el resto. "
            "Tono directo y breve — máximo 4 líneas, sin relleno ni "
            "markdown, en español." + aviso_extra
        ),
        messages=[{
            "role": "user",
            "content": f"Pregunta original del dueño: {pregunta}\n\n{bloques_datos}",
        }],
    )
    for bloque in respuesta.content:
        if bloque.type == "text":
            return bloque.text.strip()
    return "Tuve un problema generando la respuesta."


async def _responder_consulta_dueño(pregunta: str, telefono: str) -> str:
    """
    Orquesta las 2 llamadas del motor de consulta abierta (Parte 4).
    exportar_datos es un caso especial: no se ejecuta de inmediato — se
    guarda la intención en _EXPORT_PENDIENTE y se pregunta el formato
    primero (ver Parte 5, _procesar_mensaje_dueño la resuelve en el
    siguiente turno).
    """
    llamadas = await _rutear_consulta_dueño(pregunta, telefono)

    llamada_export = next((l for l in llamadas if l[0] == "exportar_datos"), None)
    if llamada_export:
        _EXPORT_PENDIENTE[telefono] = llamada_export[1]
        respuesta = "¿Lo quieres como archivo (CSV/Excel) o te lo muestro aquí en el chat?"
    else:
        resultados = []
        for nombre_funcion, parametros in llamadas:
            datos = await _ejecutar_consulta_dueño(nombre_funcion, parametros)
            resultados.append((nombre_funcion, datos))
        respuesta = await _formatear_respuesta_dueño(pregunta, resultados)

    _guardar_turno_historial_dueño(telefono, pregunta, respuesta)
    return respuesta


# ── Parte 2 — cargar ventas/leads con confirmación explícita ──────────────

# Propuesta pendiente por número de dueño, en memoria (se pierde si Railway
# redespliega entre la propuesta y la confirmación — limitación conocida).
_CARGA_PENDIENTE: dict[str, dict] = {}

_PALABRAS_CONFIRMACION = {"si", "sí", "confirmar", "confirmo", "dale", "ok", "okay", "correcto"}
_PALABRAS_CANCELACION = {"no", "cancelar", "cancela", "cancelalo", "cancélalo"}

# Exportación pendiente por número de dueño — mismo patrón y misma
# limitación que _CARGA_PENDIENTE (en memoria, se pierde si Railway
# redespliega entre la pregunta de formato y la respuesta).
_EXPORT_PENDIENTE: dict[str, dict] = {}
_PALABRAS_FORMATO_ARCHIVO = {"archivo", "csv", "excel", "documento", "adjunto"}
_PALABRAS_FORMATO_CHAT = {"chat", "aqui", "aquí", "mostrar", "muestramelo", "muéstramelo", "texto", "aca", "acá"}

_ETIQUETAS_CAMPOS = {
    "telefono": "el teléfono",
    "nombre": "el nombre",
    "producto": "el producto/compañía",
    "comuna": "la comuna",
    "direccion": "la dirección",
    "rut": "el RUT (verificación de identidad)",
    "internet_o_tv": "si incluye internet, TV, o ambos (dúo)",
    "forma_pago": "la forma de pago (PAT/PAC, efectivo, tarjeta o cuenta)",
    "carnet_foto_recibida": "si ya tienes la foto del carnet del cliente",
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
            "rut": {
                "type": "string",
                "description": "RUT del cliente, para verificación de identidad. Obligatorio si tipo=venta.",
            },
            "producto": {
                "type": "string",
                "enum": ["DirecTV", "VTR", "Movistar", "Claro", "Entel", "WOM", "otro"],
                "description": "Compañía/producto mencionado, si se menciona.",
            },
            "incluye_internet": {
                "type": "boolean",
                "description": "true si la venta incluye Internet. Solo relevante si tipo=venta.",
            },
            "incluye_tv": {
                "type": "boolean",
                "description": "true si la venta incluye TV. Solo relevante si tipo=venta.",
            },
            "forma_pago": {
                "type": "string",
                "enum": ["PAT/PAC", "efectivo", "tarjeta", "cuenta"],
                "description": "Forma de pago de la venta. Obligatorio siempre que tipo=venta (afecta el bono Rally y es un dato general útil, sin importar la compañía).",
            },
            "carnet_foto_recibida": {
                "type": "boolean",
                "description": "true si el dueño confirma que ya tiene la foto del carnet del cliente. SOLO relevante si producto=VTR o producto=Movistar — para DirecTV no se pide, el RUT solo ya alcanza.",
            },
            "comuna": {"type": "string"},
            "direccion": {"type": "string"},
            "calle": {"type": "string", "description": "Calle de la dirección de instalación, si se menciona por separado."},
            "numero": {"type": "string", "description": "Número de la dirección de instalación, si se menciona por separado."},
            "decos_adicionales": {"type": "integer", "description": "Cantidad de decodificadores adicionales vendidos, si se menciona."},
            "extras": {"type": "string", "description": "Extras vendidos aparte del plan base, en texto libre (ej. 'extensor wifi'), si se menciona."},
            "monto_venta": {"type": "integer", "description": "Monto mensual de la venta en pesos chilenos, si se menciona."},
            "fecha_instalacion_estimada": {"type": "string", "description": "Fecha estimada de instalación, formato YYYY-MM-DD, si se menciona."},
            "campos_faltantes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Nombres de campos importantes que el dueño todavía no mencionó (ej. "
                    "['telefono']). telefono y nombre siempre son importantes. Si tipo=venta, "
                    "también son importantes: producto, rut, si incluye internet y/o TV "
                    "(usa 'internet_o_tv' si falta), forma_pago (siempre), y "
                    "carnet_foto_recibida SOLO si producto=VTR o producto=Movistar."
                ),
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

    if datos.get("tipo") == "venta":
        if not datos.get("producto") and "producto" not in faltantes:
            faltantes.append("producto")
        if not datos.get("rut") and "rut" not in faltantes:
            faltantes.append("rut")
        if (
            datos.get("incluye_internet") is None
            and datos.get("incluye_tv") is None
            and "internet_o_tv" not in faltantes
        ):
            faltantes.append("internet_o_tv")
        # forma_pago ahora es obligatorio siempre (afecta el bono Rally de
        # DirecTV, y es un dato general útil sin importar la compañía).
        if not datos.get("forma_pago") and "forma_pago" not in faltantes:
            faltantes.append("forma_pago")

        # carnet_foto_recibida SOLO se pide para VTR/Movistar -- para
        # DirecTV el RUT solo ya alcanza para verificación de identidad.
        producto_lower = (datos.get("producto") or "").strip().lower()
        if producto_lower in ("vtr", "movistar"):
            if datos.get("carnet_foto_recibida") is None and "carnet_foto_recibida" not in faltantes:
                faltantes.append("carnet_foto_recibida")

    return faltantes


def _texto_confirmacion_carga(datos: dict) -> str:
    tipo_label = "Venta cerrada" if datos.get("tipo") == "venta" else "Lead nuevo"
    lineas = ["Voy a cargar esto — ¿confirmas?", "", f"Tipo: {tipo_label}"]
    if datos.get("nombre"):
        lineas.append(f"Nombre: {datos['nombre']}")
    if datos.get("telefono"):
        lineas.append(f"Teléfono: {datos['telefono']}")
    if datos.get("rut"):
        lineas.append(f"RUT: {datos['rut']}")
    if datos.get("producto"):
        lineas.append(f"Producto: {datos['producto']}")
    if datos.get("tipo") == "venta":
        incluye = []
        if datos.get("incluye_internet"):
            incluye.append("Internet")
        if datos.get("incluye_tv"):
            incluye.append("TV")
        if incluye:
            etiqueta = "Dúo" if len(incluye) == 2 else f"Solo {incluye[0]}"
            lineas.append(f"Incluye: {' + '.join(incluye)} ({etiqueta})")
        if datos.get("forma_pago"):
            lineas.append(f"Forma de pago: {datos['forma_pago']}")
        if datos.get("decos_adicionales"):
            lineas.append(f"Decos adicionales: {datos['decos_adicionales']}")
        if datos.get("extras"):
            lineas.append(f"Extras: {datos['extras']}")
        if datos.get("monto_venta"):
            lineas.append(f"Monto: ${datos['monto_venta']:,}".replace(",", "."))
        if datos.get("fecha_instalacion_estimada"):
            lineas.append(f"Instalación estimada: {datos['fecha_instalacion_estimada']}")
        producto_lower = (datos.get("producto") or "").strip().lower()
        if producto_lower in ("vtr", "movistar"):
            estado_carnet = "sí" if datos.get("carnet_foto_recibida") else "no"
            lineas.append(f"Foto del carnet recibida: {estado_carnet}")
    if datos.get("calle") or datos.get("numero") or datos.get("comuna"):
        partes_dir = " ".join(p for p in [datos.get("calle"), datos.get("numero")] if p)
        if partes_dir and datos.get("comuna"):
            lineas.append(f"Dirección: {partes_dir}, {datos['comuna']}")
        elif partes_dir:
            lineas.append(f"Dirección: {partes_dir}")
        elif datos.get("comuna"):
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

    if datos.get("tipo") == "venta":
        await crm.registrar_venta(
            telefono=telefono,
            compania=producto,
            incluye_internet=bool(datos.get("incluye_internet")),
            incluye_tv=bool(datos.get("incluye_tv")),
            rut=datos.get("rut"),
            forma_pago=datos.get("forma_pago"),
            nombre=nombre,
            calle=datos.get("calle"),
            numero=datos.get("numero"),
            comuna=datos.get("comuna"),
            decos_adicionales=datos.get("decos_adicionales") or 0,
            extras=datos.get("extras"),
            monto_venta=datos.get("monto_venta"),
            fecha_instalacion_estimada=datos.get("fecha_instalacion_estimada"),
            carnet_foto_recibida=bool(datos.get("carnet_foto_recibida")),
        )

    accion = "actualicé el lead existente" if ya_existia else "creé un lead nuevo"
    return f"Listo, {accion} para {nombre or telefono} ✅"


# ── Parte 3 — modo de producto activo (global, persiste en BD) ────────────

_KEYWORDS_MODO_TODOS = [
    "responde todo", "responde todos", "modo normal", "sin restriccion",
    "sin restricción", "todas las companias", "todas las compañías",
    "todos los productos", "vuelve a todos", "quita la restriccion",
    "quita la restricción", "levanta la restriccion", "levanta la restricción",
]
_KEYWORDS_SOLO = ["solo", "sólo", "unicamente", "únicamente"]
_KEYWORDS_DTV = ["dtv", "directv"]
_KEYWORDS_VTR_MOVISTAR = ["vtr", "movistar"]


def _detectar_cambio_modo_producto(texto_lower: str) -> str | None:
    """
    Detecta si el dueño está pidiendo cambiar el modo de producto activo.
    Retorna "todos", "directv", "vtr_movistar", o None si no aplica.
    """
    if _keyword_match(texto_lower, _KEYWORDS_MODO_TODOS):
        return "todos"

    tiene_solo = _keyword_match(texto_lower, _KEYWORDS_SOLO)
    if not tiene_solo:
        return None

    if _keyword_match(texto_lower, _KEYWORDS_DTV):
        return "directv"
    if _keyword_match(texto_lower, _KEYWORDS_VTR_MOVISTAR):
        return "vtr_movistar"
    return None


# ── Parte 5 — exportar ventas/leads, preguntando el formato primero ───────

def _generar_csv(filas: list[dict]) -> bytes:
    """CSV con BOM (para que Excel abra bien los acentos) a partir de una lista de dicts."""
    if not filas:
        return b""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(filas[0].keys()))
    writer.writeheader()
    for fila in filas:
        writer.writerow({k: ("" if v is None else v) for k, v in fila.items()})
    return output.getvalue().encode("utf-8-sig")


def _texto_resumen_export(filas: list[dict], limite: int = 15) -> str:
    """Resumen legible en texto plano para el formato 'chat' — sin pasar por Haiku, para no arriesgar que reformatee mal un número."""
    lineas = [f"{len(filas)} resultado(s):", ""]
    for fila in filas[:limite]:
        resumen_fila = ", ".join(f"{k}: {v}" for k, v in fila.items() if v not in (None, "", False))
        lineas.append(f"• {resumen_fila}")
    if len(filas) > limite:
        lineas.append(f"\n...y {len(filas) - limite} más (pide 'archivo' para verlos todos).")
    return "\n".join(lineas)


async def _ejecutar_exportacion(parametros: dict, formato: str, telefono_destino: str) -> str:
    """Genera y entrega la exportación pendiente, en el formato que eligió el dueño."""
    que = parametros.get("que_exportar")
    fecha_desde = parametros.get("fecha_desde")
    fecha_hasta = parametros.get("fecha_hasta")

    if que == "ventas":
        filas = await crm.exportar_ventas(fecha_desde, fecha_hasta)
    elif que == "leads":
        filas = await crm.exportar_leads(fecha_desde, fecha_hasta)
    else:
        return "No me quedó claro qué exportar — ¿ventas o leads?"

    if not filas:
        return f"No hay {que} en ese período — nada que exportar."

    if formato == "chat":
        return _texto_resumen_export(filas)

    # formato == "archivo"
    csv_bytes = _generar_csv(filas)
    nombre_archivo = f"{que}_{fecha_desde}_a_{fecha_hasta or fecha_desde}.csv"
    enviado = await proveedor.enviar_documento(
        telefono_destino, csv_bytes, nombre_archivo, "text/csv",
        caption=f"{len(filas)} {que} del período {fecha_desde} al {fecha_hasta or fecha_desde}",
    )
    if enviado:
        return "Listo, te mandé el archivo 📎"
    return "Tuve un problema mandando el archivo. Te lo muestro aquí en el chat en su lugar:\n\n" + _texto_resumen_export(filas)


async def _procesar_mensaje_dueño(telefono: str, texto: str):
    """
    Modo dueño — mensajes desde cualquiera de los dos números del dueño
    (56974394322, 56978016298) se enrutan aquí, sin distinción de producto.
    Parte 1: reportes de solo lectura. Parte 2: cargar ventas/leads con
    confirmación explícita antes de escribir en la BD. Parte 3: cambiar el
    modo de producto activo (qué compañías puede ofrecer Valentina a TODOS
    los clientes, globalmente, hasta que el dueño lo cambie de nuevo).
    """
    texto_original = (texto or "").strip()
    texto_lower = texto_original.lower()

    try:
        pendiente = _CARGA_PENDIENTE.get(telefono)
        pendiente_export = _EXPORT_PENDIENTE.get(telefono)

        # Modo producto tiene prioridad, salvo que haya una carga o una
        # exportación en curso (para no confundir texto pendiente con un
        # cambio de modo).
        modo_nuevo = None if (pendiente or pendiente_export) else _detectar_cambio_modo_producto(texto_lower)

        if modo_nuevo:
            await prompt_builder.actualizar_modo_producto(modo_nuevo, cliente_slug=CLIENTE_SLUG)
            respuesta = f"Listo, modo de producto activo: {prompt_builder.NOMBRE_MODO[modo_nuevo]} ✅"

        elif pendiente_export and _keyword_match(texto_lower, _PALABRAS_FORMATO_ARCHIVO):
            respuesta = await _ejecutar_exportacion(pendiente_export, "archivo", telefono)
            del _EXPORT_PENDIENTE[telefono]

        elif pendiente_export and _keyword_match(texto_lower, _PALABRAS_FORMATO_CHAT):
            respuesta = await _ejecutar_exportacion(pendiente_export, "chat", telefono)
            del _EXPORT_PENDIENTE[telefono]

        elif pendiente_export and texto_lower in _PALABRAS_CANCELACION:
            del _EXPORT_PENDIENTE[telefono]
            respuesta = "Cancelado, no exporté nada."

        elif pendiente_export:
            respuesta = "¿Lo quieres como archivo (CSV/Excel) o te lo muestro aquí en el chat?"

        elif pendiente and texto_lower in _PALABRAS_CONFIRMACION:
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
            respuesta = await _responder_consulta_dueño(texto_original, telefono)

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
