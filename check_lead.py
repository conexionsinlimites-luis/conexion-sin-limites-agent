import asyncio, sys
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def check():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT telefono, nombre, estado, score FROM leads WHERE telefono='56974394322'")
        print(dict(row))
    await close_pool()

asyncio.run(check())
