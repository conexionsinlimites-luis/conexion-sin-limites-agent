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

        # ── Tabla ventas — separada de leads a propósito, para no perder
        # historial cuando un mismo telefono compra más de una vez ────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ventas (
                id                          SERIAL PRIMARY KEY,

                -- Identificación
                telefono                    TEXT NOT NULL,
                lead_id                     INTEGER REFERENCES leads(id),
                nombre                      TEXT,
                rut                         TEXT,
                calle                       TEXT,
                numero                      TEXT,
                comuna                      TEXT,

                -- Detalle de venta
                compania                    TEXT NOT NULL,
                plan_vendido                TEXT,
                incluye_internet            BOOLEAN DEFAULT FALSE,
                incluye_tv                  BOOLEAN DEFAULT FALSE,
                incluye_telefonia           BOOLEAN DEFAULT FALSE,
                forma_pago                  TEXT,
                decos_adicionales           INTEGER DEFAULT 0,
                extras                      TEXT DEFAULT '',
                monto_venta                 INTEGER,
                fecha_venta                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha_instalacion_estimada  DATE,

                -- Seguimiento manual (checklist operativo, se marca después de cargar)
                estado_pago                 TEXT DEFAULT 'pendiente',
                biometrica_enviada          BOOLEAN DEFAULT FALSE,
                contrato_firmado            BOOLEAN DEFAULT FALSE,
                carnet_foto_recibida        BOOLEAN DEFAULT FALSE,
                ingresado_sistema_compania  BOOLEAN DEFAULT FALSE,
                ingresado_drive             BOOLEAN DEFAULT FALSE,
                plantilla_whatsapp_enviada  BOOLEAN DEFAULT FALSE,
                ingresado_cds               BOOLEAN DEFAULT FALSE,

                -- Comisión calculada (la llena el cálculo del mes, no la carga)
                rgu_o_puntos                NUMERIC(6,1),
                tramo_alcanzado             TEXT,
                comision_calculada          INTEGER,
                fecha_pago_estimada         DATE,

                -- Metadata
                origen                      TEXT DEFAULT 'carga_manual',
                notas                       TEXT DEFAULT '',
                cliente_id                  INTEGER REFERENCES clientes(id),
                created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                actualizado_en              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migraciones incrementales — cubren tablas `ventas` ya creadas antes
        # de que existieran estas columnas (dev/producción con el schema viejo).
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS rut TEXT")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS nombre TEXT")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS calle TEXT")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS numero TEXT")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS comuna TEXT")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS decos_adicionales INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS extras TEXT DEFAULT ''")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS fecha_instalacion_estimada DATE")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS biometrica_enviada BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS contrato_firmado BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS carnet_foto_recibida BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS ingresado_sistema_compania BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS ingresado_drive BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS plantilla_whatsapp_enviada BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS ingresado_cds BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS rgu_o_puntos NUMERIC(6,1)")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS tramo_alcanzado TEXT")
        await conn.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS comision_calculada INTEGER")
        # Columnas del diseño original de ventas, superadas por rgu_o_puntos/
        # tramo_alcanzado/comision_calculada y fecha_instalacion_estimada —
        # nunca tuvieron datos (nada las llegó a usar), se eliminan sin riesgo.
        await conn.execute("ALTER TABLE ventas DROP COLUMN IF EXISTS tier_rgu")
        await conn.execute("ALTER TABLE ventas DROP COLUMN IF EXISTS puntaje_comision")
        await conn.execute("ALTER TABLE ventas DROP COLUMN IF EXISTS fecha_instalacion")

        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ventas_telefono ON ventas(telefono)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ventas_fecha_venta ON ventas(fecha_venta)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ventas_compania ON ventas(compania)")
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
                    "proximo_followup", "agente", "origen"
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
# MODO DUEÑO — MOTOR DE CONSULTA ABIERTA
# ═══════════════════════════════════════
#
# Funciones de solo lectura que el router (Haiku, en main.py) elige según
# la pregunta en lenguaje natural del dueño. Cada una recibe parámetros ya
# resueltos por el router (fechas concretas, teléfono, nombre) y devuelve
# datos crudos — nunca texto — para que el paso de formateo no pueda
# heredar un número mal calculado por la IA.

def rango_fecha_chile(fecha_desde: str, fecha_hasta: str | None = None) -> tuple[datetime, datetime]:
    """
    Convierte un rango de fechas en horario de Chile (YYYY-MM-DD) al rango
    UTC naive equivalente, para comparar contra columnas TIMESTAMP que
    Postgres guarda en UTC (CURRENT_TIMESTAMP corre en UTC en Railway).
    Sin esta conversión, "hoy" calculado en UTC puede no coincidir con el
    día calendario real del dueño en Chile.
    """
    fecha_hasta = fecha_hasta or fecha_desde
    inicio_local = datetime.strptime(fecha_desde, "%Y-%m-%d").replace(
        tzinfo=_ZONA_CHILE, hour=0, minute=0, second=0, microsecond=0
    )
    fin_local = datetime.strptime(fecha_hasta, "%Y-%m-%d").replace(
        tzinfo=_ZONA_CHILE, hour=23, minute=59, second=59, microsecond=999999
    )
    return (
        inicio_local.astimezone(timezone.utc).replace(tzinfo=None),
        fin_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


_LIMITE_DETALLE_CONSULTA = 10


async def leads_nuevos(fecha_desde: str, fecha_hasta: str | None = None) -> dict:
    """
    Cuenta leads cuyo primer contacto (created_at) cae dentro del período, y
    devuelve además el detalle (nombre, teléfono, producto, estado) de hasta
    los últimos _LIMITE_DETALLE_CONSULTA — para preguntas tipo "dame un
    ejemplo" o "pásame el teléfono de algún lead reciente", no solo conteos.
    """
    desde, hasta = rango_fecha_chile(fecha_desde, fecha_hasta)
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM leads WHERE created_at BETWEEN $1 AND $2",
            desde, hasta
        )
        rows = await conn.fetch("""
            SELECT nombre, telefono, subproducto, estado, created_at
            FROM leads WHERE created_at BETWEEN $1 AND $2
            ORDER BY created_at DESC LIMIT $3
        """, desde, hasta, _LIMITE_DETALLE_CONSULTA)
    return {
        "total": total,
        "detalle": [dict(r) for r in rows],
        "detalle_limitado_a": _LIMITE_DETALLE_CONSULTA,
        "desde": fecha_desde,
        "hasta": fecha_hasta or fecha_desde,
    }


async def contactos_activos(fecha_desde: str, fecha_hasta: str | None = None) -> dict:
    """
    Cuenta contactos distintos que escribieron al menos un mensaje dentro
    del período, sin importar cuándo se creó el lead — a diferencia de
    leads_nuevos, que solo cuenta el primer contacto. Devuelve además el
    detalle (teléfono, nombre, hora del último mensaje) de hasta los
    últimos _LIMITE_DETALLE_CONSULTA contactos, para preguntas tipo "quién
    me escribió" o "dame un ejemplo de alguien que escribió".
    """
    desde, hasta = rango_fecha_chile(fecha_desde, fecha_hasta)
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("""
            SELECT COUNT(DISTINCT telefono) FROM historial_mensajes
            WHERE rol = 'user' AND timestamp BETWEEN $1 AND $2
        """, desde, hasta)
        rows = await conn.fetch("""
            SELECT h.telefono, l.nombre, l.subproducto, l.estado,
                   MAX(h.timestamp) AS ultimo_mensaje
            FROM historial_mensajes h
            LEFT JOIN leads l ON l.telefono = h.telefono
            WHERE h.rol = 'user' AND h.timestamp BETWEEN $1 AND $2
            GROUP BY h.telefono, l.nombre, l.subproducto, l.estado
            ORDER BY ultimo_mensaje DESC
            LIMIT $3
        """, desde, hasta, _LIMITE_DETALLE_CONSULTA)
    return {
        "total": total,
        "detalle": [dict(r) for r in rows],
        "detalle_limitado_a": _LIMITE_DETALLE_CONSULTA,
        "desde": fecha_desde,
        "hasta": fecha_hasta or fecha_desde,
    }


async def ventas_cerradas(fecha_desde: str, fecha_hasta: str | None = None) -> dict:
    """
    Cuenta leads con estado='cerrado' cuya ultima_interaccion cae en el
    período. Se usa ultima_interaccion como proxy de la fecha de cierre
    porque no existe una columna fecha_cierre — asume que el estado no se
    vuelve a tocar después de cerrarse. Todo registro 'cerrado' proviene de
    la carga manual del dueño (Parte 2 del modo dueño): el bot nunca marca
    un lead como cerrado por su cuenta, por eso el llamador (main.py) debe
    aclarar siempre que estas ventas son solo las que el dueño cargó a mano.
    """
    desde, hasta = rango_fecha_chile(fecha_desde, fecha_hasta)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT nombre, telefono, subproducto FROM leads
            WHERE estado = 'cerrado' AND ultima_interaccion BETWEEN $1 AND $2
            ORDER BY ultima_interaccion
        """, desde, hasta)
    return {
        "total": len(rows),
        "detalle": [dict(r) for r in rows],
        "desde": fecha_desde,
        "hasta": fecha_hasta or fecha_desde,
    }


async def buscar_lead_por_telefono(telefono: str) -> dict | None:
    """Busca un lead exacto por teléfono. Reusa obtener_lead()."""
    telefono_limpio = re.sub(r"\D", "", telefono or "")
    return await obtener_lead(telefono_limpio)


async def buscar_lead_por_nombre(nombre: str) -> list[dict]:
    """Busca leads por coincidencia parcial de nombre (case-insensitive)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM leads WHERE nombre ILIKE $1 "
            "ORDER BY ultima_interaccion DESC LIMIT 10",
            f"%{nombre}%"
        )
    return [dict(r) for r in rows]


async def contar_respondieron_envio_masivo() -> dict:
    """
    Cuenta cuántos leads de origen='envio_masivo' tienen al menos un
    mensaje de usuario en historial_mensajes (respondieron) vs los que no.
    Usado por el modo dueño. Se limita a envío masivo porque los leads con
    origen='whatsapp' incluyen registros importados sin conversación real
    — mezclarlos daría un número que no refleja lo que se está preguntando.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        fila = await conn.fetchrow("""
            SELECT
              COUNT(*) FILTER (WHERE h.n_user > 0)                AS respondieron,
              COUNT(*) FILTER (WHERE h.n_user IS NULL OR h.n_user = 0) AS no_respondieron,
              COUNT(*)                                             AS total
            FROM leads l
            LEFT JOIN (
              SELECT telefono, COUNT(*) FILTER (WHERE rol = 'user') AS n_user
              FROM historial_mensajes GROUP BY telefono
            ) h ON h.telefono = l.telefono
            WHERE l.origen = 'envio_masivo'
        """)
        return dict(fila)


# ═══════════════════════════════════════
# REGISTRO DE VENTAS (tabla ventas)
# ═══════════════════════════════════════

async def registrar_venta(
    telefono: str,
    compania: str,
    incluye_internet: bool,
    incluye_tv: bool,
    rut: str | None = None,
    forma_pago: str | None = None,
    plan_vendido: str | None = None,
    nombre: str | None = None,
    calle: str | None = None,
    numero: str | None = None,
    comuna: str | None = None,
    decos_adicionales: int = 0,
    extras: str | None = None,
    monto_venta: int | None = None,
    fecha_instalacion_estimada: str | None = None,
    carnet_foto_recibida: bool = False,
) -> None:
    """
    Inserta una fila en `ventas` para el cálculo de comisión. Se llama
    desde el modo dueño (_ejecutar_carga_dueño) al confirmar una carga de
    tipo='venta' — nunca para lead_nuevo, que no genera comisión.

    Los campos de "seguimiento manual" del schema (contrato_firmado,
    ingresado_sistema_compania, ingresado_drive, plantilla_whatsapp_enviada,
    ingresado_cds, biometrica_enviada) NO se piden en la carga — quedan en
    FALSE por defecto y se actualizan después, por otro flujo (pendiente,
    no implementado todavía). carnet_foto_recibida es la única excepción
    porque sí se confirma en el momento de la carga (VTR/Movistar).
    """
    fecha_instalacion_date = None
    if fecha_instalacion_estimada:
        fecha_instalacion_date = datetime.strptime(fecha_instalacion_estimada, "%Y-%m-%d").date()

    pool = await get_pool()
    async with pool.acquire() as conn:
        lead = await conn.fetchrow("SELECT id FROM leads WHERE telefono = $1", telefono)
        await conn.execute("""
            INSERT INTO ventas (
                telefono, lead_id, nombre, rut, calle, numero, comuna,
                compania, plan_vendido, incluye_internet, incluye_tv,
                forma_pago, decos_adicionales, extras, monto_venta,
                fecha_instalacion_estimada, carnet_foto_recibida, origen
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, 'carga_manual')
        """,
            telefono, lead["id"] if lead else None, nombre, rut, calle, numero, comuna,
            compania, plan_vendido, incluye_internet, incluye_tv,
            forma_pago, decos_adicionales, extras or "", monto_venta,
            fecha_instalacion_date, carnet_foto_recibida,
        )


async def buscar_venta_por_telefono(telefono: str) -> dict | None:
    """
    Busca la venta MÁS RECIENTE de un teléfono (puede haber más de una en
    el tiempo). Devuelve la fila completa — el paso de formateo extrae y
    muestra solo el campo puntual que el dueño haya pedido (ej. "el RUT").
    """
    telefono_limpio = re.sub(r"\D", "", telefono or "")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ventas WHERE telefono = $1 ORDER BY fecha_venta DESC LIMIT 1",
            telefono_limpio,
        )
        return dict(row) if row else None


async def buscar_venta_por_nombre(nombre: str) -> list[dict]:
    """Busca ventas por coincidencia parcial de nombre (case-insensitive)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM ventas WHERE nombre ILIKE $1 "
            "ORDER BY fecha_venta DESC LIMIT 10",
            f"%{nombre}%",
        )
        return [dict(r) for r in rows]


async def exportar_ventas(fecha_desde: str, fecha_hasta: str | None = None) -> list[dict]:
    """Todas las filas de `ventas` en el período, para exportar (CSV o resumen en chat)."""
    desde, hasta = rango_fecha_chile(fecha_desde, fecha_hasta)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM ventas WHERE fecha_venta BETWEEN $1 AND $2 ORDER BY fecha_venta",
            desde, hasta,
        )
        return [dict(r) for r in rows]


async def exportar_leads(fecha_desde: str, fecha_hasta: str | None = None) -> list[dict]:
    """Todas las filas de `leads` creadas en el período, para exportar (CSV o resumen en chat)."""
    desde, hasta = rango_fecha_chile(fecha_desde, fecha_hasta)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM leads WHERE created_at BETWEEN $1 AND $2 ORDER BY created_at",
            desde, hasta,
        )
        return [dict(r) for r in rows]


# ═══════════════════════════════════════
# COMISIONES — DIRECTV
# ═══════════════════════════════════════
#
# Reglas confirmadas por el dueño el 2026-09-10. Cubren SOLO DirecTV — VTR
# y Movistar tienen su propio sistema de tier/RGU, pendiente de definir
# aparte. No inventar ni extender esta lógica a otras compañías.

_PUNTOS_DUO_DIRECTV = 2.0      # TV + Internet juntos en la misma venta
_PUNTOS_SOLO_DIRECTV = 1.5     # Solo TV o solo Internet, por separado
_BONO_PAT_PAC = 0.5            # Adicional si esa venta se pagó con PAT/PAC
_UMBRAL_SUELDO_BASE = 14       # Puntos totales del mes a partir de los cuales aplica el sueldo base
_SUELDO_BASE = 350_000         # CLP, con >= 14 puntos en el mes
_VALOR_POR_PUNTO = 20_000      # CLP por punto, tanto bajo el umbral como por sobre él

# Bono Rally (confirmado 2026-09-11): adicional al sueldo por puntos, no lo
# reemplaza. Requiere AMBAS condiciones en el mes: >= 6 ventas Dúo, Y que
# al menos el 30% del TOTAL de ventas DirecTV del mes (no solo las Dúo) se
# hayan pagado con PAT/PAC.
_UMBRAL_RALLY_VENTAS_DUO = 6
_UMBRAL_RALLY_PORCENTAJE_PAT_PAC = 0.30
_BONO_RALLY = 100_000


def puntos_venta_directv(venta: dict) -> float:
    """
    Calcula los puntos de comisión DirecTV de UNA venta.

    Dúo (TV + Internet juntos) = 2 puntos. Solo TV o solo Internet (por
    separado) = 1.5 puntos. +0.5 puntos si esa venta se pagó con PAT/PAC.

    `venta` espera las claves incluye_internet, incluye_tv (bool) y
    forma_pago (str) — el mismo shape que una fila de la tabla `ventas`.
    Lanza ValueError si la venta no tiene ni internet ni TV, porque la
    regla confirmada no cubre ese caso — mejor fallar fuerte que asumir.
    """
    tiene_internet = bool(venta.get("incluye_internet"))
    tiene_tv = bool(venta.get("incluye_tv"))

    if tiene_internet and tiene_tv:
        puntos = _PUNTOS_DUO_DIRECTV
    elif tiene_internet or tiene_tv:
        puntos = _PUNTOS_SOLO_DIRECTV
    else:
        raise ValueError(
            "Venta DirecTV sin internet ni TV — no se puede calcular puntos "
            "(la regla confirmada solo cubre Dúo, Solo TV o Solo Internet)."
        )

    forma_pago = (venta.get("forma_pago") or "").strip().upper()
    if forma_pago == "PAT/PAC":
        puntos += _BONO_PAT_PAC

    return puntos


def calcular_sueldo_directv(puntos_totales: float) -> int:
    """
    Sueldo mensual DirecTV según puntos totales acumulados en el mes.

    < 14 puntos: proporcional ($20.000 por punto).
    >= 14 puntos: $350.000 base + $20.000 por cada punto sobre 14.
    """
    if puntos_totales < _UMBRAL_SUELDO_BASE:
        sueldo = puntos_totales * _VALOR_POR_PUNTO
    else:
        sueldo = _SUELDO_BASE + (puntos_totales - _UMBRAL_SUELDO_BASE) * _VALOR_POR_PUNTO
    return round(sueldo)


def calcular_bono_rally_directv(ventas: list[dict]) -> dict:
    """
    Bono Rally DirecTV: +$100.000 en el mes si se cumplen AMBAS condiciones
    — al menos 6 ventas Dúo (TV+Internet), Y al menos 30% del TOTAL de
    ventas DirecTV del mes (todas, no solo las Dúo) pagadas con PAT/PAC.
    Es adicional al sueldo por puntos, no lo reemplaza.
    """
    total_ventas = len(ventas)
    ventas_duo = sum(
        1 for v in ventas if bool(v.get("incluye_internet")) and bool(v.get("incluye_tv"))
    )
    ventas_pat_pac = sum(
        1 for v in ventas if (v.get("forma_pago") or "").strip().upper() == "PAT/PAC"
    )
    porcentaje_pat_pac = (ventas_pat_pac / total_ventas) if total_ventas else 0.0
    califica = (
        ventas_duo >= _UMBRAL_RALLY_VENTAS_DUO
        and porcentaje_pat_pac >= _UMBRAL_RALLY_PORCENTAJE_PAT_PAC
    )
    return {
        "ventas_duo": ventas_duo,
        "total_ventas": total_ventas,
        "ventas_pat_pac": ventas_pat_pac,
        "porcentaje_pat_pac": round(porcentaje_pat_pac * 100, 1),
        "califica": califica,
        "bono": _BONO_RALLY if califica else 0,
    }


def calcular_comision_directv(ventas: list[dict]) -> dict:
    """
    Suma los puntos de una lista de ventas DirecTV del mes, calcula el
    sueldo por puntos, y le suma el bono Rally si corresponde. `ventas` es
    una lista de dicts con el mismo shape que espera puntos_venta_directv.
    """
    puntos_totales = sum(puntos_venta_directv(v) for v in ventas)
    sueldo_por_puntos = calcular_sueldo_directv(puntos_totales)
    rally = calcular_bono_rally_directv(ventas)
    return {
        "puntos_totales": puntos_totales,
        "sueldo_por_puntos": sueldo_por_puntos,
        "rally": rally,
        "sueldo_total": sueldo_por_puntos + rally["bono"],
        "cantidad_ventas": len(ventas),
    }


# ═══════════════════════════════════════
# COMISIONES — VTR / CLARO / MOVISTAR (POR RGU)
# ═══════════════════════════════════════
#
# Reglas confirmadas por el dueño el 2026-09-10. VTR y Claro comparten UN
# solo contador combinado de RGU (una venta de cualquiera de las dos suma
# al mismo total); Movistar tiene su propio contador, independiente y sin
# bonos/variables adicionales. DirecTV usa el sistema de puntos de arriba
# — no mezclar la lógica de ambos sistemas.
#
# Mecanismo de pago (aplica a ambos grupos): durante el mes cada venta se
# paga de inmediato a la tarifa del tramo 1 (base); el día 5 del mes
# siguiente se paga la diferencia, aplicada RETROACTIVAMENTE a TODAS las
# ventas del mes según el tramo FINAL alcanzado por el RGU total acumulado
# — no solo a las ventas que cayeron dentro del tramo nuevo. Por eso el
# cálculo de "total_final" usa la tarifa del tramo final para cada venta,
# sin importar en qué momento del mes se hizo.

# (rgu_minimo, tarifa_solo_internet, tarifa_duo) — de mayor a menor umbral,
# _tarifa_por_tramo recorre la lista y toma el primer tramo que calza.
_TRAMOS_VTR_CLARO = [
    (51, 50_000, 80_000),
    (31, 45_000, 75_000),
    (1,  40_000, 70_000),
]

_TRAMOS_MOVISTAR = [
    (51, 45_000, 65_000),
    (1,  40_000, 60_000),
]

_COMPANIAS_VTR_CLARO = {"vtr", "claro"}
_COMPANIAS_MOVISTAR = {"movistar"}


def rgu_venta(venta: dict) -> int:
    """
    Cuenta los RGU de UNA venta VTR/Claro/Movistar. Dúo (Internet+TV) = 2
    RGU. Solo Internet = 1 RGU. La regla confirmada NO cubre "solo TV" para
    estas compañías (a diferencia de DirecTV) — lanza ValueError en ese
    caso, en vez de asumir un número.
    """
    tiene_internet = bool(venta.get("incluye_internet"))
    tiene_tv = bool(venta.get("incluye_tv"))

    if tiene_internet and tiene_tv:
        return 2
    if tiene_internet and not tiene_tv:
        return 1
    raise ValueError(
        "Venta VTR/Claro/Movistar sin el patrón Dúo o Solo Internet — no "
        "se puede contar RGU (la regla confirmada no cubre 'solo TV' para "
        "estas compañías)."
    )


def _tarifa_por_tramo(rgu_acumulado: int, tramos: list[tuple[int, int, int]]) -> tuple[int, int]:
    """Retorna (tarifa_solo_internet, tarifa_duo) del tramo que corresponde a rgu_acumulado."""
    for rgu_minimo, tarifa_solo, tarifa_duo in tramos:
        if rgu_acumulado >= rgu_minimo:
            return tarifa_solo, tarifa_duo
    raise ValueError(f"No hay tramo definido para {rgu_acumulado} RGU")


def _validar_companias(ventas: list[dict], companias_validas: set[str], nombre_grupo: str) -> None:
    """
    Red de seguridad: si una venta trae `compania` y no pertenece al grupo
    esperado, falla fuerte en vez de sumarla en silencio al contador
    equivocado (VTR/Claro y Movistar NUNCA se mezclan).
    """
    for v in ventas:
        compania = (v.get("compania") or "").strip().lower()
        if compania and compania not in companias_validas:
            raise ValueError(
                f"Venta de compañía '{v.get('compania')}' no pertenece al "
                f"grupo {nombre_grupo} — revisa que no se mezclaron ventas "
                f"de otra compañía en la lista."
            )


def _calcular_comision_por_rgu(ventas: list[dict], tramos: list[tuple[int, int, int]]) -> dict:
    """
    Núcleo compartido del cálculo de comisión por RGU — VTR+Claro y
    Movistar usan la misma mecánica (conteo de RGU, tramos, pago
    retroactivo al tramo final), solo cambian el contador y la tabla de
    tarifas.
    """
    rgus = [rgu_venta(v) for v in ventas]
    rgu_total = sum(rgus)
    tarifa_solo, tarifa_duo = _tarifa_por_tramo(rgu_total, tramos)
    tarifa_base_solo, tarifa_base_duo = _tarifa_por_tramo(1, tramos)  # tramo 1, siempre

    total_final = sum(tarifa_duo if r == 2 else tarifa_solo for r in rgus)
    pago_inicial = sum(tarifa_base_duo if r == 2 else tarifa_base_solo for r in rgus)

    return {
        "rgu_total": rgu_total,
        "cantidad_ventas": len(ventas),
        "tarifa_solo_internet": tarifa_solo,
        "tarifa_duo": tarifa_duo,
        "pago_inicial": pago_inicial,
        "total_final": total_final,
        "diferencia_dia_5": total_final - pago_inicial,
    }


def calcular_comision_vtr_claro(ventas: list[dict]) -> dict:
    """
    VTR + Claro comparten UN solo contador combinado de RGU — una venta de
    cualquiera de las dos compañías cuenta para el mismo total. `ventas`
    debe incluir las de ambas juntas.
    """
    _validar_companias(ventas, _COMPANIAS_VTR_CLARO, "VTR/Claro")
    return _calcular_comision_por_rgu(ventas, _TRAMOS_VTR_CLARO)


def calcular_comision_movistar(ventas: list[dict]) -> dict:
    """Movistar tiene su propio contador de RGU, independiente de VTR/Claro."""
    _validar_companias(ventas, _COMPANIAS_MOVISTAR, "Movistar")
    return _calcular_comision_por_rgu(ventas, _TRAMOS_MOVISTAR)


async def comision_mes(fecha_desde: str, fecha_hasta: str | None = None) -> dict:
    """
    Calcula la comisión combinada del mes (DirecTV + VTR/Claro + Movistar)
    a partir de las filas reales de la tabla `ventas` en el período. Si un
    grupo tiene ventas con datos incompletos (ej. sin incluye_internet ni
    incluye_tv), NO se cae todo el cálculo — se reporta el error de ESE
    grupo específicamente en `<grupo>_error`, y los demás grupos igual se
    calculan.
    """
    desde, hasta = rango_fecha_chile(fecha_desde, fecha_hasta)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT compania, plan_vendido, incluye_internet, incluye_tv, forma_pago
            FROM ventas WHERE fecha_venta BETWEEN $1 AND $2
        """, desde, hasta)
    ventas = [dict(r) for r in rows]

    def _de_compania(*nombres):
        return [v for v in ventas if (v.get("compania") or "").strip().lower() in nombres]

    resultado = {"desde": fecha_desde, "hasta": fecha_hasta or fecha_desde, "total_ventas": len(ventas)}

    for grupo, ventas_grupo, funcion in (
        ("directv",   _de_compania("directv"),     calcular_comision_directv),
        ("vtr_claro", _de_compania("vtr", "claro"), calcular_comision_vtr_claro),
        ("movistar",  _de_compania("movistar"),     calcular_comision_movistar),
    ):
        if not ventas_grupo:
            continue
        try:
            resultado[grupo] = funcion(ventas_grupo)
        except ValueError as e:
            resultado[f"{grupo}_error"] = str(e)

    resultado["sueldo_total_estimado"] = (
        resultado.get("directv", {}).get("sueldo_total", 0)
        + resultado.get("vtr_claro", {}).get("total_final", 0)
        + resultado.get("movistar", {}).get("total_final", 0)
    )
    return resultado


# ═══════════════════════════════════════
# INICIALIZAR AL EJECUTAR DIRECTAMENTE
# ═══════════════════════════════════════

if __name__ == "__main__":
    asyncio.run(init_db())
    print("CRM Valentina listo")
