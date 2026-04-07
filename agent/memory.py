# agent/memory.py — Memoria de conversaciones con PostgreSQL
# Conexion Sin Limites

"""
Sistema de memoria del agente. Guarda el historial de conversaciones
por número de teléfono usando asyncpg + PostgreSQL.
"""

from datetime import datetime
from agent.database import get_pool


async def inicializar_db():
    """Crea la tabla mensajes si no existe."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mensajes (
                id        SERIAL PRIMARY KEY,
                telefono  TEXT NOT NULL,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mensajes_telefono ON mensajes (telefono)"
        )


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de conversación."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO mensajes (telefono, role, content, timestamp) VALUES ($1, $2, $3, $4)",
            telefono, role, content, datetime.utcnow().isoformat()
        )


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Recupera los últimos N mensajes de una conversación en orden cronológico.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT role, content FROM (
                SELECT role, content, timestamp
                FROM mensajes
                WHERE telefono = $1
                ORDER BY timestamp DESC
                LIMIT $2
            ) sub ORDER BY timestamp ASC
        """, telefono, limite)
        return [{"role": r["role"], "content": r["content"]} for r in rows]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversación."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM mensajes WHERE telefono = $1", telefono)
