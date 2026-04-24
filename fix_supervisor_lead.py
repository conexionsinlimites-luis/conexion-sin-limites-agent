import asyncio, sys
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def fix():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM leads WHERE telefono='56978016298'")
        await conn.execute("DELETE FROM historial_mensajes WHERE telefono='56978016298'")
        await conn.execute("DELETE FROM followup_programado WHERE telefono='56978016298'")
        print('OK - numero supervisor limpiado de la DB')
    await close_pool()

asyncio.run(fix())
