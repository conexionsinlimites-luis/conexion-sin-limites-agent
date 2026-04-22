import asyncio, sys
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def fix():
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE leads SET nombre='desconocido' WHERE nombre='Hola'")
        print('OK:', result)
    await close_pool()

asyncio.run(fix())
