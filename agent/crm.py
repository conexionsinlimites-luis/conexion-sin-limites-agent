"""
CRM VALENTINA v1.0
Conexión Sin Límites
Módulo completo de leads, scoring y estados
"""

import re
import sqlite3
import aiosqlite
import asyncio
from datetime import datetime, timedelta
import json

DB_PATH = "valentina_crm.db"

# ═══════════════════════════════════════
# ESQUEMA DE BASE DE DATOS
# ═══════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
);

CREATE TABLE IF NOT EXISTS historial_mensajes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telefono TEXT NOT NULL,
    rol TEXT NOT NULL,
    mensaje TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    estado_lead TEXT,
    intencion_detectada TEXT
);

CREATE TABLE IF NOT EXISTS followup_programado (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telefono TEXT NOT NULL,
    tipo TEXT NOT NULL,
    mensaje TEXT NOT NULL,
    programado_para TIMESTAMP NOT NULL,
    enviado INTEGER DEFAULT 0,
    cancelado INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alertas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telefono TEXT NOT NULL,
    tipo TEXT NOT NULL,
    contenido TEXT NOT NULL,
    enviada INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

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
    "modo_humano",   # IA pausada — humano atiende directamente
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
    "modo_humano": 0,   # no altera score — se preserva el anterior
}

SCORE_POR_INTENCION = {
    "alta": 30,
    "media": 15,
    "baja": 5
}

LIMITE_MENSAJES_POR_ESTADO = {
    "interesado": 3,
    "tibio": 2
}

# ═══════════════════════════════════════
# INICIALIZACIÓN
# ═══════════════════════════════════════

async def init_db():
    """Inicializar la base de datos y aplicar migraciones incrementales."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # Migraciones: agrega columnas nuevas sin romper DBs existentes
        for ddl in [
            "ALTER TABLE leads ADD COLUMN lead_resumen TEXT DEFAULT ''",
        ]:
            try:
                await db.execute(ddl)
                await db.commit()
            except Exception:
                pass  # columna ya existe
    print("CRM Valentina inicializado correctamente")

# ═══════════════════════════════════════
# FUNCIONES DE LEADS
# ═══════════════════════════════════════

async def crear_o_actualizar_lead(telefono: str, nombre: str = None, **kwargs):
    """Crear nuevo lead o actualizar si ya existe"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Verificar si existe
        async with db.execute(
            "SELECT id, estado, score FROM leads WHERE telefono = ?",
            (telefono,)
        ) as cursor:
            lead = await cursor.fetchone()

        if lead:
            # Actualizar lead existente
            updates = ["ultima_interaccion = CURRENT_TIMESTAMP"]
            values = []

            if nombre:
                updates.append("nombre = ?")
                values.append(nombre)

            for key, value in kwargs.items():
                if key in ["estado", "score", "direccion", "comuna",
                          "subproducto", "notas", "proxima_accion",
                          "proximo_followup", "agente"]:
                    updates.append(f"{key} = ?")
                    values.append(value)

            values.append(telefono)
            query = f"UPDATE leads SET {', '.join(updates)} WHERE telefono = ?"
            await db.execute(query, values)
        else:
            # Crear nuevo lead
            identificador = generar_identificador(
                kwargs.get("subproducto", "TELECOM"),
                nombre or "CLIENTE",
                kwargs.get("estado", "nuevo")
            )
            await db.execute("""
                INSERT INTO leads
                (telefono, nombre, identificador, producto_principal,
                 subproducto, estado, score, origen, agente)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                telefono,
                nombre or "Desconocido",
                identificador,
                kwargs.get("producto_principal", "telecom"),
                kwargs.get("subproducto", "general"),
                kwargs.get("estado", "nuevo"),
                SCORE_POR_ESTADO.get(kwargs.get("estado", "nuevo"), 10),
                kwargs.get("origen", "whatsapp"),
                kwargs.get("agente", "valentina")
            ))

        await db.commit()

def generar_identificador(producto: str, nombre: str, estado: str) -> str:
    """Generar identificador único tipo V-DIRECTV-PEDRO PEREZ-CALIENTE"""
    producto_clean = producto.upper().replace(" ", "")
    nombre_clean = nombre.upper().strip()
    estado_clean = estado.upper()
    return f"V-{producto_clean}-{nombre_clean}-{estado_clean}"

async def obtener_lead(telefono: str) -> dict:
    """Obtener datos completos de un lead"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM leads WHERE telefono = ?",
            (telefono,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

async def actualizar_estado(telefono: str, nuevo_estado: str):
    """Actualizar estado y score del lead"""
    score = SCORE_POR_ESTADO.get(nuevo_estado, 10)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE leads
            SET estado = ?,
                score = ?,
                mensajes_en_estado = 0,
                ultima_interaccion = CURRENT_TIMESTAMP,
                identificador = (
                    SELECT 'V-' || UPPER(COALESCE(subproducto,'TELECOM'))
                    || '-' || UPPER(COALESCE(nombre,'CLIENTE'))
                    || '-' || UPPER(?)
                    FROM leads WHERE telefono = ?
                )
            WHERE telefono = ?
        """, (nuevo_estado, score, nuevo_estado, telefono, telefono))
        await db.commit()

async def incrementar_mensajes_estado(telefono: str):
    """Incrementar contador de mensajes en estado actual"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE leads
            SET mensajes_en_estado = mensajes_en_estado + 1
            WHERE telefono = ?
        """, (telefono,))
        await db.commit()

async def guardar_objecion(telefono: str, objecion: str):
    """Guardar objeción detectada para usar en cierre"""
    lead = await obtener_lead(telefono)
    if lead:
        objeciones = json.loads(lead.get("objeciones", "[]"))
        if objecion not in objeciones:
            objeciones.append(objecion)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE leads SET objeciones = ? WHERE telefono = ?",
                (json.dumps(objeciones), telefono)
            )
            await db.commit()

# ═══════════════════════════════════════
# HISTORIAL DE MENSAJES
# ═══════════════════════════════════════

async def guardar_mensaje(telefono: str, rol: str, mensaje: str,
                          estado_lead: str = None, intencion: str = None):
    """Guardar mensaje en historial"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO historial_mensajes
            (telefono, rol, mensaje, estado_lead, intencion_detectada)
            VALUES (?, ?, ?, ?, ?)
        """, (telefono, rol, mensaje, estado_lead, intencion))
        await db.commit()

async def obtener_historial(telefono: str, limite: int = 20) -> list:
    """Obtener historial de conversación"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT rol, mensaje, timestamp
            FROM historial_mensajes
            WHERE telefono = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (telefono, limite)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in reversed(rows)]

# ═══════════════════════════════════════
# LEAD SCORING AUTOMÁTICO
# ═══════════════════════════════════════

async def actualizar_score(telefono: str, intencion: str):
    """Actualizar score según intención detectada"""
    puntos = SCORE_POR_INTENCION.get(intencion, 0)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE leads
            SET score = MIN(100, score + ?)
            WHERE telefono = ?
        """, (puntos, telefono))
        await db.commit()

def clasificar_lead(score: int) -> str:
    """Clasificar lead según score"""
    if score >= 70:
        return "caliente"
    elif score >= 40:
        return "tibio"
    else:
        return "frio"

def extraer_nombre_de_mensaje(mensaje: str) -> str | None:
    """
    Detecta si el cliente menciona su nombre en el mensaje.
    Patrones: 'me llamo X', 'soy X', 'mi nombre es X', 'llámame X'.
    Retorna el nombre en Title Case o None si no encuentra.
    """
    EXCLUIDAS = {
        "bien", "mal", "aqui", "aquí", "solo", "sola", "yo", "tu", "él", "ella",
        "un", "una", "el", "la", "de", "del", "por", "para", "con", "sin",
        "cliente", "persona", "alguien", "nadie", "nuevo", "nueva",
    }
    patrones = [
        r"me llamo\s+([a-záéíóúüñ]+(?:\s+[a-záéíóúüñ]+)?)",
        r"mi nombre es\s+([a-záéíóúüñ]+(?:\s+[a-záéíóúüñ]+)?)",
        r"soy\s+([a-záéíóúüñ]+(?:\s+[a-záéíóúüñ]+)?)",
        r"llámame\s+([a-záéíóúüñ]+)",
        r"puedes llamarme\s+([a-záéíóúüñ]+)",
        r"^([a-záéíóúüñ]{3,}(?:\s+[a-záéíóúüñ]{3,})?)\s*(?:aqui|aquí|presente|👋)?$",
    ]
    texto = mensaje.lower().strip()
    for patron in patrones:
        match = re.search(patron, texto, re.IGNORECASE)
        if match:
            nombre = match.group(1).strip().title()
            if nombre.lower() not in EXCLUIDAS and len(nombre) >= 3:
                return nombre
    return None


async def actualizar_nombre_si_desconocido(telefono: str, nombre: str) -> bool:
    """
    Guarda el nombre del cliente solo si el lead aún no tiene uno válido.
    Nunca sobreescribe un nombre ya registrado. Retorna True si se actualizó.
    """
    INVALIDOS = {"", "desconocido", "none", "null", "cliente", "unknown"}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT nombre FROM leads WHERE telefono = ?", (telefono,)
        ) as c:
            row = await c.fetchone()
        if not row:
            return False
        nombre_actual = (row[0] or "").strip().lower()
        if nombre_actual and nombre_actual not in INVALIDOS:
            return False  # ya tiene nombre válido — no tocar
        await db.execute(
            "UPDATE leads SET nombre = ? WHERE telefono = ?", (nombre, telefono)
        )
        await db.commit()
        return True


def detectar_intencion(mensaje: str) -> str:
    """Detectar nivel de intención en mensaje"""
    mensaje_lower = mensaje.lower()

    palabras_alta = [
        "quiero contratar", "me interesa", "cuánto cuesta",
        "qué plan", "cuando instalan", "quiero el plan",
        "cómo contrato", "quiero contratar", "precio",
        "disponible", "instalar"
    ]
    palabras_media = [
        "planes", "cobertura", "velocidad", "canales",
        "internet", "televisión", "combo", "dúo"
    ]

    for palabra in palabras_alta:
        if palabra in mensaje_lower:
            return "alta"

    for palabra in palabras_media:
        if palabra in mensaje_lower:
            return "media"

    return "baja"

def detectar_objecion(mensaje: str) -> str:
    """Detectar objeción en mensaje"""
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
    """Detectar si el lead está estancado"""
    limite = LIMITE_MENSAJES_POR_ESTADO.get(estado, 999)
    return mensajes_en_estado >= limite

# ═══════════════════════════════════════
# FOLLOW-UP AUTOMÁTICO
# ═══════════════════════════════════════

async def programar_followup(telefono: str, tipo: str):
    """Programar follow-up automático"""
    ahora = datetime.now()

    # Calcular tiempo según tipo
    tiempos = {
        "2h": ahora + timedelta(hours=2),
        "24h": ahora + timedelta(hours=24),
        "3d": ahora + timedelta(days=3),
        "30d": ahora + timedelta(days=30),
        "60d": ahora + timedelta(days=60)
    }

    mensajes = {
        "2h": "Hola {nombre}, solo quería saber si pudiste revisar lo que te comenté 😊",
        "24h": "Hola {nombre}, ¿cómo estás? Quedé pendiente con tu consulta sobre {tema}",
        "3d": "Hola {nombre}, conseguimos una promoción que creo te puede interesar 🔥",
        "30d": "Hola {nombre}, ¿cómo ha estado tu servicio de internet/TV? 😊",
        "60d": "Hola {nombre}, ¿sigues con {empresa}? Han salido planes nuevos que quizás te convengan más 📱"
    }

    programado_para = tiempos.get(tipo)
    if not programado_para:
        return

    # Respetar horario Chile (9am - 9pm)
    if programado_para.hour < 9:
        programado_para = programado_para.replace(hour=9, minute=0)
    elif programado_para.hour >= 21:
        programado_para = (programado_para + timedelta(days=1)).replace(hour=9, minute=0)

    async with aiosqlite.connect(DB_PATH) as db:
        # Cancelar follow-ups anteriores pendientes
        await db.execute("""
            UPDATE followup_programado
            SET cancelado = 1
            WHERE telefono = ? AND enviado = 0 AND cancelado = 0
        """, (telefono,))

        # Programar nuevo
        await db.execute("""
            INSERT INTO followup_programado
            (telefono, tipo, mensaje, programado_para)
            VALUES (?, ?, ?, ?)
        """, (telefono, tipo, mensajes.get(tipo, ""), programado_para))

        await db.commit()

async def cancelar_followups(telefono: str):
    """Cancelar todos los follow-ups cuando cliente responde"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE followup_programado
            SET cancelado = 1
            WHERE telefono = ? AND enviado = 0
        """, (telefono,))
        await db.commit()

async def obtener_followups_pendientes() -> list:
    """Obtener follow-ups listos para enviar"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT f.*, l.nombre, l.subproducto
            FROM followup_programado f
            JOIN leads l ON f.telefono = l.telefono
            WHERE f.enviado = 0
            AND f.cancelado = 0
            AND f.programado_para <= CURRENT_TIMESTAMP
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def marcar_followup_enviado(followup_id: int):
    """Marcar follow-up como enviado"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE followup_programado SET enviado = 1 WHERE id = ?",
            (followup_id,)
        )
        await db.commit()

# ═══════════════════════════════════════
# ALERTAS AL SUPERVISOR
# ═══════════════════════════════════════

async def generar_alerta_supervisor(telefono: str, tipo: str) -> str:
    """Genera y persiste en DB una alerta enriquecida para el supervisor."""
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

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO alertas (telefono, tipo, contenido)
            VALUES (?, ?, ?)
        """, (telefono, tipo, alerta))
        await db.commit()

    return alerta

# ═══════════════════════════════════════
# RESUMEN AUTOMÁTICO DEL LEAD
# ═══════════════════════════════════════

async def generar_resumen_lead(telefono: str) -> str:
    """
    Construye un resumen corto del lead para lectura rápida del supervisor.
    Usa solo datos estructurados ya capturados — sin llamadas externas.
    Formato: qué necesita · estado/score · objeciones · último mensaje del cliente.
    """
    lead = await obtener_lead(telefono)
    if not lead:
        return ""

    partes = []

    # Qué necesita / producto de interés
    subproducto = (lead.get("subproducto") or "").strip()
    if subproducto and subproducto.lower() not in ("general", "telecom", ""):
        partes.append(f"🎯 {subproducto}")
    else:
        partes.append("🎯 Producto sin definir")

    # Estado y score
    estado = lead.get("estado", "nuevo")
    score  = lead.get("score", 0)
    partes.append(f"{estado} · {score}pts")

    # Objeciones detectadas
    try:
        objeciones = json.loads(lead.get("objeciones") or "[]")
    except Exception:
        objeciones = []
    if objeciones:
        partes.append(f"⚠️ {', '.join(objeciones)}")

    # Último mensaje real del cliente (da contexto vivo a la conversación)
    historial = await obtener_historial(telefono, limite=10)
    msgs_cliente = [m["mensaje"] for m in historial if m.get("rol") == "user"]
    if msgs_cliente:
        ultimo = msgs_cliente[-1][:100].strip()
        if ultimo:
            partes.append(f'💬 "{ultimo}"')

    return "  ·  ".join(partes)


async def actualizar_resumen_lead(telefono: str) -> None:
    """Regenera y guarda el resumen del lead en la tabla leads."""
    resumen = await generar_resumen_lead(telefono)
    if not resumen:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE leads SET lead_resumen = ? WHERE telefono = ?",
            (resumen, telefono)
        )
        await db.commit()


# ═══════════════════════════════════════
# ESTADÍSTICAS Y REPORTES
# ═══════════════════════════════════════

async def obtener_estadisticas() -> dict:
    """Obtener estadísticas generales del CRM"""
    async with aiosqlite.connect(DB_PATH) as db:
        stats = {}

        # Total leads
        async with db.execute("SELECT COUNT(*) FROM leads") as c:
            stats["total_leads"] = (await c.fetchone())[0]

        # Leads calientes
        async with db.execute(
            "SELECT COUNT(*) FROM leads WHERE estado IN ('caliente', 'listo_para_cierre')"
        ) as c:
            stats["leads_calientes"] = (await c.fetchone())[0]

        # Leads cerrados
        async with db.execute(
            "SELECT COUNT(*) FROM leads WHERE estado = 'cerrado'"
        ) as c:
            stats["leads_cerrados"] = (await c.fetchone())[0]

        # Por estado
        async with db.execute(
            "SELECT estado, COUNT(*) as total FROM leads GROUP BY estado"
        ) as c:
            rows = await c.fetchall()
            stats["por_estado"] = {row[0]: row[1] for row in rows}

        return stats

# ═══════════════════════════════════════
# INICIALIZAR AL IMPORTAR
# ═══════════════════════════════════════

if __name__ == "__main__":
    asyncio.run(init_db())
    print("CRM Valentina listo")
