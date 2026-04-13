# agent/database.py — Pool de conexiones PostgreSQL compartido
# Conexion Sin Limites

"""
Pool asyncpg singleton. Todos los módulos (crm, memory, dashboard)
importan get_pool() de aquí para evitar abrir múltiples pools.
"""

import ssl as ssl_module
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
        # Separar sslmode de la URL para pasarlo como parámetro explícito
        url = DATABASE_URL
        ssl_param = None
        if "sslmode=require" in url:
            url = url.replace("?sslmode=require", "").replace("&sslmode=require", "")
            ssl_ctx = ssl_module.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl_module.CERT_NONE
            ssl_param = ssl_ctx
        _pool = await asyncpg.create_pool(
            url,
            min_size=2,
            max_size=10,
            command_timeout=30,
            ssl=ssl_param,
        )
    return _pool


async def close_pool():
    """Cierra el pool al apagar el servidor."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
