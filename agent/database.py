# agent/database.py — Pool de conexiones PostgreSQL compartido
# Conexion Sin Limites

"""
Pool asyncpg singleton. Todos los módulos (crm, memory, dashboard)
importan get_pool() de aquí para evitar abrir múltiples pools.
"""

import asyncpg
from agent.config import DATABASE_URL

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Retorna el pool, creándolo si aún no existe."""
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL no configurada. "
                "Agrega la variable de entorno en Railway."
            )
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=30,
            ssl='disable',
        )
    return _pool


async def close_pool():
    """Cierra el pool al apagar el servidor."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
