# agent/memory.py — Memoria de conversaciones con SQLite
# Generado por AgentKit

"""
Sistema de memoria del agente. Guarda el historial de conversaciones
por número de teléfono usando aiosqlite directamente (compatible con Python 3.14+).
"""

import aiosqlite
from datetime import datetime
from agent.config import DB_PATH


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mensajes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                telefono  TEXT NOT NULL,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_telefono ON mensajes (telefono)")
        await db.commit()


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de conversación."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO mensajes (telefono, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (telefono, role, content, datetime.utcnow().isoformat())
        )
        await db.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Recupera los últimos N mensajes de una conversación en orden cronológico.

    Args:
        telefono: Número de teléfono del cliente
        limite: Máximo de mensajes a recuperar (default: 20)

    Returns:
        Lista de diccionarios con role y content
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, timestamp
                FROM mensajes
                WHERE telefono = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ) ORDER BY timestamp ASC
            """,
            (telefono, limite)
        ) as cursor:
            filas = await cursor.fetchall()
            return [{"role": fila["role"], "content": fila["content"]} for fila in filas]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversación."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM mensajes WHERE telefono = ?", (telefono,))
        await db.commit()
