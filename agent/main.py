# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor (Whapi, Meta, Twilio) gracias a la capa de providers.
"""

import re
import asyncio
import logging
import traceback
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
from agent.transcriber import transcribir
from agent.config import PORT, ENVIRONMENT, TELEFONO_OWNER
import agent.crm as crm
from agent.scheduler import iniciar_scheduler
from agent.dashboard import router as dashboard_router, broadcast_event
from agent.make_integration import enviar_a_make
from agent.database import get_pool, close_pool

# Número del supervisor comercial que recibe alertas
TELEFONO_SUPERVISOR = "56978016298"

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
    await inicializar_db()
    await crm.init_db()
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
        if msg.es_propio or (not msg.texto and not msg.audio_id):
            _log("INFO", f"Mensaje ignorado — es_propio={msg.es_propio} texto='{msg.texto}'")
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
            nombre_extraido = crm.extraer_nombre_de_mensaje(msg.texto)
            if nombre_extraido:
                actualizado = await crm.actualizar_nombre_si_desconocido(msg.telefono, nombre_extraido)
                if actualizado:
                    _log("INFO", f"Nombre capturado para {msg.telefono}: {nombre_extraido}")

            await crm.cancelar_followups(msg.telefono)

            # --- CRM: Detectar intención y objeción en el mensaje entrante ---
            intencion = crm.detectar_intencion(msg.texto)
            objecion  = crm.detectar_objecion(msg.texto)

            await crm.actualizar_score(msg.telefono, intencion)
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

            historial = await obtener_historial(msg.telefono)
            _log("INFO", f"Historial recuperado: {len(historial)} mensajes previos")

            nombre_cliente = (lead_ref.get("nombre") or "") if lead_ref else ""
            respuesta = await generar_respuesta(msg.texto, historial, nombre_cliente)
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

            # Actualizar resumen del lead con la información más reciente
            await crm.actualizar_resumen_lead(msg.telefono)

            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta_limpia)
            if enviado:
                _log("INFO", f"Respuesta enviada OK a {msg.telefono}")
            else:
                _log("ERROR", f"enviar_mensaje falló para {msg.telefono} — revisar token/credenciales en Railway")

            # Enviar alerta al supervisor y marcar lead como listo para cierre
            if alerta:
                await _enviar_alerta_supervisor(alerta, msg.telefono)
                await crm.actualizar_estado(msg.telefono, "listo_para_cierre")
                dir_ = alerta.get("dir", "")
                if dir_ and dir_ != "pendiente":
                    await crm.crear_o_actualizar_lead(msg.telefono, direccion=dir_)
                _log("INFO", f"Lead {msg.telefono} marcado como listo_para_cierre en CRM")

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
    """
    Detecta y extrae el marcador [ALERTA_SUPERVISOR|...] de la respuesta de Valentina.
    Retorna (respuesta_sin_marcador, datos_alerta_o_None).
    """
    patron = r'\[ALERTA_SUPERVISOR\|nombre=([^|]*)\|tel=([^|]*)\|dir=([^\]]*)\]'
    match = re.search(patron, respuesta)
    if not match:
        return respuesta, None

    datos = {
        "nombre": match.group(1).strip(),
        "tel":    match.group(2).strip(),
        "dir":    match.group(3).strip(),
    }
    respuesta_limpia = re.sub(patron, "", respuesta).strip()
    return respuesta_limpia, datos


async def _enviar_notificacion_caliente(telefono_cliente: str):
    """Avisa al dueño por WhatsApp cuando un lead se vuelve caliente."""
    lead = await crm.obtener_lead(telefono_cliente)
    if not lead:
        return
    nombre   = lead.get("nombre") or "Cliente"
    score    = lead.get("score", 0)
    producto = lead.get("subproducto") or "Telecom"
    resumen  = lead.get("lead_resumen") or "—"
    tel      = telefono_cliente.replace("+", "").replace(" ", "")
    wa_link  = f"https://wa.me/{tel}"

    mensaje = (
        f"🔥 *LEAD CALIENTE — VALENTINA*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *{nombre}*\n"
        f"📱 {wa_link}\n"
        f"⭐ Score: {score}/100\n"
        f"📦 {producto}\n"
        f"📋 _{resumen}_\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Entra al dashboard para tomar el lead."
    )
    try:
        enviado = await proveedor.enviar_mensaje(TELEFONO_OWNER, mensaje)
        if enviado:
            _log("INFO", f"Notif. lead caliente enviada — {nombre} ({tel})")
        else:
            _log("ERROR", f"Notif. lead caliente falló para {tel}")
    except Exception as e:
        _log("ERROR", f"Error notificando lead caliente: {e}")


async def _enviar_alerta_supervisor(datos: dict, telefono_cliente: str):
    """
    Envía alerta enriquecida al supervisor cuando Valentina captura una
    dirección o detecta intención de contratar.
    Combina los datos del marcador con la info actualizada del CRM.
    """
    tel  = (datos.get("tel") or telefono_cliente).replace("+", "").replace(" ", "").replace("-", "")
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
        enviado = await proveedor.enviar_mensaje(TELEFONO_SUPERVISOR, mensaje)
        if enviado:
            _log("INFO", f"Alerta supervisor enviada — {nombre} ({tel})")
        else:
            _log("ERROR", f"Alerta supervisor falló para {tel} — revisar credenciales")
    except Exception as e:
        _log("ERROR", f"Error enviando alerta supervisor: {e}")
