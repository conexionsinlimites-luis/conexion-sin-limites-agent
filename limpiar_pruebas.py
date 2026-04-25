import asyncio, sys
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def fix():
    pool = await get_pool()
    async with pool.acquire() as conn:
        for tel in ['56978016298', '56974394322', '56941762315']:
            await conn.execute("DELETE FROM historial_mensajes WHERE telefono=$1", tel)
            await conn.execute("DELETE FROM leads WHERE telefono=$1", tel)
            await conn.execute("DELETE FROM followup_programado WHERE telefono=$1", tel)
            print(f'Limpiado: {tel}')
    await close_pool()

asyncio.run(fix())
