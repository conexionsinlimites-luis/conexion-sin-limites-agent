"""
CRM VALENTINA v2.0 — PostgreSQL
Conexión Sin Límites
Módulo completo de leads, scoring y estados
"""

import re
import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from agent.database import get_pool

_ZONA_CHILE = ZoneInfo("America/Santiago")

# ═══════════════════════════════════════
# ESTADOS Y SCORES
# ═══════════════════════════════════════

ESTADOS = [
    "nuevo",
    "contactado",
    "interesado",
    "tibio",
    "caliente",
    "direccion_obtenida",
    "listo_para_cierre",
    "cerrado",
    "seguimiento",
    "modo_humano",
]

SCORE_POR_ESTADO = {
    "nuevo": 10,
    "contactado": 20,
    "interesado": 40,
    "tibio": 35,
    "caliente": 70,
    "direccion_obtenida": 85,
    "listo_para_cierre": 95,
    "cerrado": 100,
    "seguimiento": 25,
    "modo_humano": 0,
}

SCORE_POR_INTENCION = {
    "alta": 30,
    "media": 15,
    "baja": 5
}

# Señales de comportamiento: puntos a sumar (negativo = restar)
SCORE_COMPORTAMIENTO: dict[str, int] = {
    "precio_especifico":    20,   # menciona un precio o pregunta el costo exacto
    "producto_especifico":  15,   # nombra un plan concreto (Movistar 200MB, VTR TV, etc.)
    "pregunta_instalacion": 25,   # pregunta cuándo o cómo instalan
    "direccion_mencionada": 30,   # da su dirección o pregunta cobertura por calle
    "urgencia":             20,   # "urgente", "necesito ya", "esta semana"
    "comparacion":          10,   # compara con su proveedor actual
    "familia_menciona":     10,   # "somos X personas", "mi familia", "mi casa"
    "multi_pregunta":       10,   # hace 2 o más preguntas en el mismo mensaje
    "rechazo_fuerte":      -30,   # "no me interesa", "no quiero", "no necesito"
    "ya_tiene_servicio":   -10,   # "ya tengo internet/tv" sin señal de cambio
    "muy_caro":            -15,   # "muy caro", "no puedo pagar"
}

LIMITE_MENSAJES_POR_ESTADO = {
    "interesado": 3,
    "tibio": 2
}

# ═══════════════════════════════════════
# INICIALIZACIÓN
# ═══════════════════════════════════════

async def init_db():
    """Crea las tablas del CRM si no existen y aplica migraciones."""
    pool = await get_pool()
    async with pool.acquire() as conn:

        # ── Multi-tenant: tabla clientes ──────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id                     SERIAL PRIMARY KEY,
                nombre                 TEXT NOT NULL,
                slug                   TEXT UNIQUE NOT NULL,
                whatsapp_phone_id      TEXT,
                whatsapp_token         TEXT,
                dashboard_user         TEXT,
                dashboard_password_hash TEXT,
                config_json            TEXT DEFAULT '{}',
                activo                 BOOLEAN DEFAULT TRUE,
                creado_en              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Primer cliente: Conexión Sin Límites (slug "csl")
        await conn.execute("""
            INSERT INTO clientes (nombre, slug, activo)
            VALUES ('Conexión Sin Límites', 'csl', TRUE)
            ON CONFLICT (slug) DO NOTHING
        """)

        # ── Tablas principales ────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                identificador TEXT UNIQUE,
                nombre TEXT,
                telefono TEXT UNIQUE NOT NULL,
                producto_principal TEXT DEFAULT 'telecom',
                subproducto TEXT,
                estado TEXT DEFAULT 'nuevo',
                score INTEGER DEFAULT 0,
                direccion TEXT,
                comuna TEXT,
                ultima_interaccion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                proximo_followup TIMESTAMP,
                proxima_accion TEXT,
                notas TEXT,
                origen TEXT DEFAULT 'whatsapp',
                agente TEXT DEFAULT 'valentina',
                objeciones TEXT DEFAULT '[]',
                mensajes_en_estado INTEGER DEFAULT 0,
                lead_resumen TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS historial_mensajes (
                id SERIAL PRIMARY KEY,
                telefono TEXT NOT NULL,
                rol TEXT NOT NULL,
                mensaje TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                estado_lead TEXT,
                intencion_detectada TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS followup_programado (
                id SERIAL PRIMARY KEY,
                telefono TEXT NOT NULL,
                tipo TEXT NOT NULL,
                mensaje TEXT NOT NULL,
                programado_para TIMESTAMP NOT NULL,
                enviado INTEGER DEFAULT 0,
                cancelado INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS alertas (
                id SERIAL PRIMARY KEY,
                telefono TEXT NOT NULL,
                tipo TEXT NOT NULL,
                contenido TEXT NOT NULL,
                enviada INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Migraciones incrementales de columnas ─────────────────
        await conn.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS lead_resumen TEXT DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS tags TEXT DEFAULT '[]'"
        )
        await conn.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS email TEXT DEFAULT ''"
        )

        # Multi-tenant: cliente_id en todas las tablas operativas
        await conn.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS "
            "cliente_id INTEGER REFERENCES clientes(id)"
        )
        await conn.execute(
            "ALTER TABLE historial_mensajes ADD COLUMN IF NOT EXISTS "
            "cliente_id INTEGER REFERENCES clientes(id)"
        )
        await conn.execute(
            "ALTER TABLE followup_programado ADD COLUMN IF NOT EXISTS "
            "cliente_id INTEGER REFERENCES clientes(id)"
        )
        await conn.execute(
            "ALTER TABLE alertas ADD COLUMN IF NOT EXISTS "
            "cliente_id INTEGER REFERENCES clientes(id)"
        )

        # Asignar registros existentes al cliente "csl" (id=1)
        await conn.execute("""
            UPDATE leads
            SET cliente_id = (SELECT id FROM clientes WHERE slug = 'csl')
            WHERE cliente_id IS NULL
        """)
        await conn.execute("""
            UPDATE historial_mensajes
            SET cliente_id = (SELECT id FROM clientes WHERE slug = 'csl')
            WHERE cliente_id IS NULL
        """)
        await conn.execute("""
            UPDATE followup_programado
            SET cliente_id = (SELECT id FROM clientes WHERE slug = 'csl')
            WHERE cliente_id IS NULL
        """)
        await conn.execute("""
            UPDATE alertas
            SET cliente_id = (SELECT id FROM clientes WHERE slug = 'csl')
            WHERE cliente_id IS NULL
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS lead_notas (
                id SERIAL PRIMARY KEY,
                telefono TEXT NOT NULL,
                contenido TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lead_notas_telefono ON lead_notas(telefono)"
        )
    print("CRM Valentina inicializado correctamente (PostgreSQL)")


# ═══════════════════════════════════════
# FUNCIONES DE LEADS
# ═══════════════════════════════════════

async def crear_o_actualizar_lead(telefono: str, nombre: str = None, **kwargs):
    """Crear nuevo lead o actualizar si ya existe."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, estado, score FROM leads WHERE telefono = $1",
                telefono
            )

            if row:
                # Actualizar lead existente
                parts = ["ultima_interaccion = CURRENT_TIMESTAMP"]
                values = []
                idx = 1

                if nombre:
                    parts.append(f"nombre = ${idx}")
                    values.append(nombre)
                    idx += 1

                allowed = {
                    "estado", "score", "direccion", "comuna",
                    "subproducto", "notas", "proxima_accion",
                    "proximo_followup", "agente"
                }
                for key, value in kwargs.items():
                    if key in allowed:
                        parts.append(f"{key} = ${idx}")
                        values.append(value)
                        idx += 1

                values.append(telefono)
                query = f"UPDATE leads SET {', '.join(parts)} WHERE telefono = ${idx}"
                await conn.execute(query, *values)
            else:
                # Crear nuevo lead
                identificador = generar_identificador(
                    kwargs.get("subproducto", "TELECOM"),
                    nombre or "CLIENTE",
                    kwargs.get("estado", "nuevo"),
                    telefono
                )
                await conn.execute("""
                    INSERT INTO leads
                    (telefono, nombre, identificador, producto_principal,
                     subproducto, estado, score, origen, agente)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                    telefono,
                    nombre or "Desconocido",
                    identificador,
                    kwargs.get("producto_principal", "telecom"),
                    kwargs.get("subproducto", "general"),
                    kwargs.get("estado", "nuevo"),
                    SCORE_POR_ESTADO.get(kwargs.get("estado", "nuevo"), 10),
                    kwargs.get("origen", "whatsapp"),
                    kwargs.get("agente", "valentina")
                )


def generar_identificador(producto: str, nombre: str, estado: str, telefono: str = "") -> str:
    """Generar identificador único tipo V-DIRECTV-PEDRO PEREZ-CALIENTE"""
    tel_suffix = telefono[-4:] if telefono else ""
    return f"V-{producto.upper().replace(' ', '')}-{nombre.upper().strip()}-{estado.upper()}-{tel_suffix}"


async def obtener_lead(telefono: str) -> dict | None:
    """Obtener datos completos de un lead."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM leads WHERE telefono = $1", telefono
        )
        return dict(row) if row else None


async def actualizar_estado(telefono: str, nuevo_estado: str):
    """Actualizar estado y score del lead."""
    score = SCORE_POR_ESTADO.get(nuevo_estado, 10)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE leads
            SET estado = $1,
                score = $2,
                mensajes_en_estado = 0,
                ultima_interaccion = CURRENT_TIMESTAMP,
                identificador = 'V-' || UPPER(COALESCE(subproducto, 'TELECOM'))
                                || '-' || UPPER(COALESCE(nombre, 'CLIENTE'))
                                || '-' || UPPER($3)
            WHERE telefono = $4
        """, nuevo_estado, score, nuevo_estado, telefono)


async def incrementar_mensajes_estado(telefono: str):
    """Incrementar contador de mensajes en estado actual."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE leads
            SET mensajes_en_estado = mensajes_en_estado + 1
            WHERE telefono = $1
        """, telefono)


async def guardar_objecion(telefono: str, objecion: str):
    """Guardar objeción detectada."""
    lead = await obtener_lead(telefono)
    if not lead:
        return
    objeciones = json.loads(lead.get("objeciones") or "[]")
    if objecion not in objeciones:
        objeciones.append(objecion)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE leads SET objeciones = $1 WHERE telefono = $2",
            json.dumps(objeciones), telefono
        )


# ═══════════════════════════════════════
# HISTORIAL DE MENSAJES
# ═══════════════════════════════════════

async def guardar_mensaje(telefono: str, rol: str, mensaje: str,
                          estado_lead: str = None, intencion: str = None):
    """Guardar mensaje en historial."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO historial_mensajes
            (telefono, rol, mensaje, estado_lead, intencion_detectada)
            VALUES ($1, $2, $3, $4, $5)
        """, telefono, rol, mensaje, estado_lead, intencion)


async def obtener_historial(telefono: str, limite: int = 20) -> list:
    """Obtener historial de conversación."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT rol, mensaje, timestamp
            FROM historial_mensajes
            WHERE telefono = $1
            ORDER BY timestamp DESC
            LIMIT $2
        """, telefono, limite)
        return [dict(r) for r in reversed(rows)]


# ═══════════════════════════════════════
# LEAD SCORING AUTOMÁTICO
# ═══════════════════════════════════════

def puntuar_mensaje(mensaje: str) -> int:
    """
    Analiza el texto del cliente y retorna puntos de comportamiento.
    Señales positivas suman; señales negativas restan. Rango: [-55, +90].
    """
    m = mensaje.lower()
    puntos = 0

    # Señales positivas
    if re.search(r'\$\s*\d|cuánto cuesta|cuanto cuesta|precio|tarifa|costo|vale|cobran', m):
        puntos += SCORE_COMPORTAMIENTO["precio_especifico"]
    if re.search(r'plan\s+\w|movistar|vtr|claro|entel|gtd|mundo\s+pacifico|fibra|coaxial|mbps|megas', m):
        puntos += SCORE_COMPORTAMIENTO["producto_especifico"]
    if re.search(r'cuándo instalan|cuando instalan|horario.*instal|visita.*técnico|técnico|instalación|instalar', m):
        puntos += SCORE_COMPORTAMIENTO["pregunta_instalacion"]
    if re.search(r'calle|avenida|pasaje|villa|sector|barrio|dirección|cobertura en|llega a|tienen en', m):
        puntos += SCORE_COMPORTAMIENTO["direccion_mencionada"]
    if re.search(r'urgente|lo necesito ya|esta semana|cuanto antes|pronto|inmediato|hoy mismo', m):
        puntos += SCORE_COMPORTAMIENTO["urgencia"]
    if re.search(r'tengo con|estoy con|actualmente tengo|mi proveedor|me cobran|me están cobrando', m):
        puntos += SCORE_COMPORTAMIENTO["comparacion"]
    if re.search(r'somos \d|mi familia|mi esposa|mi pareja|mi marido|mi casa|toda la familia|los niños', m):
        puntos += SCORE_COMPORTAMIENTO["familia_menciona"]
    # multi-pregunta: 2+ signos de interrogación o palabras interrogativas seguidas
    if len(re.findall(r'\?', m)) >= 2 or len(re.findall(r'\b(cuánto|cómo|cuándo|qué|cuál|dónde)\b', m)) >= 2:
        puntos += SCORE_COMPORTAMIENTO["multi_pregunta"]

    # Señales negativas
    if re.search(r'no me interesa|no quiero|no necesito|no gracias|dejame|déjame|no por ahora', m):
        puntos += SCORE_COMPORTAMIENTO["rechazo_fuerte"]
    if re.search(r'ya tengo|ya cuento con|ya contrato|estoy conforme|no me cambio|quedo con', m):
        puntos += SCORE_COMPORTAMIENTO["ya_tiene_servicio"]
    if re.search(r'muy caro|demasiado caro|no puedo pagar|no alcanzo|fuera de mi presupuesto|no me alcanza', m):
        puntos += SCORE_COMPORTAMIENTO["muy_caro"]

    return puntos


async def actualizar_score(telefono: str, intencion: str, mensaje: str = ""):
    """Actualizar score según intención detectada y señales de comportamiento en el mensaje."""
    puntos_intencion     = SCORE_POR_INTENCION.get(intencion, 0)
    puntos_comportamiento = puntuar_mensaje(mensaje) if mensaje else 0
    puntos_total          = puntos_intencion + puntos_comportamiento
    if puntos_total == 0:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE leads
            SET score = GREATEST(0, LEAST(100, score + $1))
            WHERE telefono = $2
        """, puntos_total, telefono)


def clasificar_lead(score: int) -> str:
    if score >= 70:
        return "caliente"
    elif score >= 40:
        return "tibio"
    return "frio"


def extraer_nombre_de_mensaje(mensaje: str) -> str | None:
    """
    Intenta extraer el nombre del cliente del texto de su mensaje.
    Cubre: "me llamo X", "soy X", "te habla X", "acá X", respuesta
    directa de solo-nombre, y nombre+emoji al final.
    """
    EXCLUIDAS = {
        "bien", "mal", "aqui", "aquí", "solo", "sola", "yo", "tu", "él", "ella",
        "un", "una", "el", "la", "de", "del", "por", "para", "con", "sin",
        "cliente", "persona", "alguien", "nadie", "nuevo", "nueva",
        "buenas", "buenos", "hola", "holas", "chao", "gracias",
        "hola!", "hello", "hey", "buenas!", "buenos!", "listo", "claro",
        "dale", "ok", "oka", "oki", "okey", "perfecto", "entendido",
    }
    patrones = [
        r"me llamo\s+([a-záéíóúüñ]+(?:\s+[a-záéíóúüñ]+)?)",
        r"mi nombre es\s+([a-záéíóúüñ]+(?:\s+[a-záéíóúüñ]+)?)",
        r"(?:^|\s)soy\s+([a-záéíóúüñ]+(?:\s+[a-záéíóúüñ]+)?)(?:\s|$|,|\.)",
        r"llámame\s+([a-záéíóúüñ]+)",
        r"puedes llamarme\s+([a-záéíóúüñ]+)",
        r"te habla\s+([a-záéíóúüñ]+(?:\s+[a-záéíóúüñ]+)?)",
        r"habla\s+([a-záéíóúüñ]+(?:\s+[a-záéíóúüñ]+)?)(?:\s|$)",
        r"^(?:acá|aquí|aqui)\s+([a-záéíóúüñ]+(?:\s+[a-záéíóúüñ]+)?)",
        r"^([a-záéíóúüñ]{3,}(?:\s+[a-záéíóúüñ]{3,})?)\s*(?:aqui|aquí|presente|👋)?$",
    ]
    texto = mensaje.lower().strip()
    # Versión limpia: quita emojis y puntuación final para capturar "Pedro 😊" o "María!"
    texto_limpio = re.sub(r'[^a-záéíóúüñ\s]+$', '', texto, flags=re.IGNORECASE).strip()

    for patron in patrones:
        for t in ([texto, texto_limpio] if texto_limpio != texto else [texto]):
            match = re.search(patron, t, re.IGNORECASE)
            if match:
                nombre = match.group(1).strip().title()
                if nombre.lower() not in EXCLUIDAS and len(nombre) >= 3:
                    return nombre
    return None


async def actualizar_nombre_si_desconocido(telefono: str, nombre: str) -> bool:
    """Guarda el nombre solo si el lead aún no tiene uno válido."""
    INVALIDOS = {"", "desconocido", "none", "null", "cliente", "unknown"}
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT nombre FROM leads WHERE telefono = $1", telefono
        )
        if not row:
            return False
        nombre_actual = (row["nombre"] or "").strip().lower()
        if nombre_actual and nombre_actual not in INVALIDOS:
            return False
        await conn.execute(
            "UPDATE leads SET nombre = $1 WHERE telefono = $2", nombre, telefono
        )
        return True


def detectar_intencion(mensaje: str) -> str:
    mensaje_lower = mensaje.lower()
    palabras_alta = [
        "quiero contratar", "me interesa", "cuánto cuesta",
        "qué plan", "cuando instalan", "quiero el plan",
        "cómo contrato", "precio", "disponible", "instalar"
    ]
    palabras_media = [
        "planes", "cobertura", "velocidad", "canales",
        "internet", "televisión", "combo", "dúo"
    ]
    for p in palabras_alta:
        if p in mensaje_lower:
            return "alta"
    for p in palabras_media:
        if p in mensaje_lower:
            return "media"
    return "baja"


def detectar_objecion(mensaje: str) -> str | None:
    mensaje_lower = mensaje.lower()
    if any(p in mensaje_lower for p in ["caro", "mucho", "precio alto"]):
        return "precio"
    if any(p in mensaje_lower for p in ["pensar", "después", "luego", "no sé"]):
        return "indecision"
    if any(p in mensaje_lower for p in ["ya tengo", "tengo internet", "tengo tv"]):
        return "ya_tiene_servicio"
    if any(p in mensaje_lower for p in ["no me interesa", "no gracias", "no quiero"]):
        return "rechazo"
    return None


def detectar_estancamiento(mensajes_en_estado: int, estado: str) -> bool:
    limite = LIMITE_MENSAJES_POR_ESTADO.get(estado, 999)
    return mensajes_en_estado >= limite


# ═══════════════════════════════════════
# FOLLOW-UP AUTOMÁTICO
# ═══════════════════════════════════════

MENSAJES_FOLLOWUP = {
    "2h":  "Hola {nombre}, solo quería saber si pudiste revisar lo que te comenté 😊",
    "24h": "Hola {nombre}, ¿cómo estás? Quedé pendiente con tu consulta sobre {tema}",
    "3d":  "Hola {nombre}, conseguimos una promoción que creo te puede interesar 🔥",
    "30d": "Hola {nombre}, ¿cómo ha estado tu servicio de internet/TV? 😊",
    "60d": "Hola {nombre}, ¿sigues con {empresa}? Han salido planes nuevos que quizás te convengan más 📱",
}

_DELTAS_FOLLOWUP = {
    "2h":  timedelta(hours=2),
    "24h": timedelta(hours=24),
    "3d":  timedelta(days=3),
    "30d": timedelta(days=30),
    "60d": timedelta(days=60),
}


async def programar_followup(telefono: str, tipo: str):
    """
    Programa un follow-up automático.
    Almacena en UTC naive; el ajuste de ventana horaria se calcula en hora Chile.
    """
    delta = _DELTAS_FOLLOWUP.get(tipo)
    if not delta:
        return

    # Hora objetivo en UTC
    objetivo_utc = datetime.now(timezone.utc) + delta

    # Convertir a Chile para verificar ventana 9am-9pm
    objetivo_chile = objetivo_utc.astimezone(_ZONA_CHILE)
    if objetivo_chile.hour < 9:
        objetivo_chile = objetivo_chile.replace(hour=9, minute=0, second=0, microsecond=0)
    elif objetivo_chile.hour >= 21:
        siguiente_dia = objetivo_chile + timedelta(days=1)
        objetivo_chile = siguiente_dia.replace(hour=9, minute=0, second=0, microsecond=0)

    # Guardar como UTC naive (TIMESTAMP WITHOUT TIME ZONE en PG)
    programado_para = objetivo_chile.astimezone(timezone.utc).replace(tzinfo=None)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                UPDATE followup_programado
                SET cancelado = 1
                WHERE telefono = $1 AND enviado = 0 AND cancelado = 0
            """, telefono)
            await conn.execute("""
                INSERT INTO followup_programado (telefono, tipo, mensaje, programado_para)
                VALUES ($1, $2, $3, $4)
            """, telefono, tipo, MENSAJES_FOLLOWUP.get(tipo, ""), programado_para)


async def cancelar_followups(telefono: str):
    """Cancelar todos los follow-ups cuando cliente responde."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE followup_programado
            SET cancelado = 1
            WHERE telefono = $1 AND enviado = 0
        """, telefono)


async def obtener_followups_pendientes() -> list:
    """Obtener follow-ups listos para enviar."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT f.*, l.nombre, l.subproducto
            FROM followup_programado f
            JOIN leads l ON f.telefono = l.telefono
            WHERE f.enviado = 0
              AND f.cancelado = 0
              AND f.programado_para <= CURRENT_TIMESTAMP
        """)
        return [dict(r) for r in rows]


async def marcar_followup_enviado(followup_id: int):
    """Marcar follow-up como enviado."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE followup_programado SET enviado = 1 WHERE id = $1",
            followup_id
        )


# ═══════════════════════════════════════
# ALERTAS AL SUPERVISOR
# ═══════════════════════════════════════

async def generar_alerta_supervisor(telefono: str, tipo: str) -> str | None:
    """Genera y persiste una alerta enriquecida para el supervisor."""
    lead = await obtener_lead(telefono)
    if not lead:
        return None

    tel_limpio = telefono.replace("+", "").replace(" ", "").replace("-", "")
    nombre   = lead.get("nombre") or "Desconocido"
    estado   = (lead.get("estado") or "nuevo").upper()
    score    = lead.get("score", 0)
    producto = lead.get("subproducto") or "Telecom general"
    dir_     = lead.get("direccion") or "No registrada"
    resumen  = lead.get("lead_resumen") or "—"
    wa_link  = f"https://wa.me/{tel_limpio}"

    alerta = (
        f"🔥 LEAD {tipo.upper()} — CONEXIÓN SIN LÍMITES\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {nombre}\n"
        f"📱 +{tel_limpio}\n"
        f"📍 {dir_}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 {producto}\n"
        f"⭐ {estado}  •  {score}/100 pts\n"
        f"📋 {resumen}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 {wa_link}"
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO alertas (telefono, tipo, contenido) VALUES ($1, $2, $3)
        """, telefono, tipo, alerta)

    return alerta


# ═══════════════════════════════════════
# RESUMEN AUTOMÁTICO DEL LEAD (Claude Haiku)
# ═══════════════════════════════════════

async def generar_resumen_lead(telefono: str) -> str:
    """
    Genera un resumen estructurado del lead usando Claude Haiku.
    Analiza el historial real de la conversación y los datos del CRM.
    Devuelve un texto corto multi-línea con: producto, objeciones,
    urgencia y próximo paso.
    """
    from anthropic import AsyncAnthropic
    from agent.config import ANTHROPIC_API_KEY

    lead = await obtener_lead(telefono)
    if not lead:
        return ""

    historial = await obtener_historial(telefono, limite=20)
    if not historial:
        return ""

    # Construir transcripción resumida para el prompt
    transcripcion = []
    for m in historial:
        rol  = "Cliente" if m.get("rol") == "user" else "Valentina"
        text = (m.get("mensaje") or "").strip()[:300]
        if text:
            transcripcion.append(f"{rol}: {text}")

    if not transcripcion:
        return ""

    try:
        objeciones_raw = json.loads(lead.get("objeciones") or "[]")
    except Exception:
        objeciones_raw = []

    contexto_crm = (
        f"Estado CRM: {lead.get('estado','nuevo')} | "
        f"Score: {lead.get('score',0)}/100 | "
        f"Producto detectado: {lead.get('subproducto') or 'sin definir'} | "
        f"Objeciones registradas: {', '.join(objeciones_raw) if objeciones_raw else 'ninguna'}"
    )

    prompt = f"""Eres un analista de ventas. Analiza esta conversación de WhatsApp y el contexto CRM.

CONTEXTO CRM:
{contexto_crm}

CONVERSACIÓN:
{chr(10).join(transcripcion[-16:])}

Genera un resumen ejecutivo CONCISO del lead. Responde ÚNICAMENTE con este formato exacto (sin introducción, sin markdown extra):

🎯 Producto: [producto o servicio de interés específico, o "Por definir"]
⚠️ Objeciones: [lista breve separada por comas, o "Ninguna"]
⏱ Urgencia: [Alta / Media / Baja] — [razón en 5 palabras máximo]
➡️ Próximo paso: [acción concreta y específica en una línea]
💡 Contexto: [1 oración clave sobre el perfil del cliente]"""

    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        # Fallback a resumen simple si la API falla
        partes = []
        subprod = (lead.get("subproducto") or "").strip()
        partes.append(f"🎯 Producto: {subprod or 'Por definir'}")
        partes.append(f"⚠️ Objeciones: {', '.join(objeciones_raw) if objeciones_raw else 'Ninguna'}")
        partes.append(f"⏱ Urgencia: Media")
        partes.append(f"➡️ Próximo paso: Continuar seguimiento")
        return "\n".join(partes)


async def actualizar_resumen_lead(telefono: str) -> None:
    """Regenera y guarda el resumen IA del lead en background."""
    try:
        resumen = await generar_resumen_lead(telefono)
        if not resumen:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE leads SET lead_resumen = $1 WHERE telefono = $2",
                resumen, telefono
            )
    except Exception:
        pass  # nunca propagar — es una tarea de background


# ═══════════════════════════════════════
# ESTADÍSTICAS Y REPORTES
# ═══════════════════════════════════════

async def obtener_estadisticas() -> dict:
    """Obtener estadísticas generales del CRM."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_leads     = await conn.fetchval("SELECT COUNT(*) FROM leads")
        leads_calientes = await conn.fetchval(
            "SELECT COUNT(*) FROM leads WHERE estado IN ('caliente', 'listo_para_cierre')"
        )
        leads_cerrados  = await conn.fetchval(
            "SELECT COUNT(*) FROM leads WHERE estado = 'cerrado'"
        )
        rows = await conn.fetch(
            "SELECT estado, COUNT(*) AS total FROM leads GROUP BY estado"
        )
        return {
            "total_leads":      total_leads,
            "leads_calientes":  leads_calientes,
            "leads_cerrados":   leads_cerrados,
            "por_estado":       {r["estado"]: r["total"] for r in rows},
        }


# ═══════════════════════════════════════
# INICIALIZAR AL EJECUTAR DIRECTAMENTE
# ═══════════════════════════════════════

if __name__ == "__main__":
    asyncio.run(init_db())
    print("CRM Valentina listo")
