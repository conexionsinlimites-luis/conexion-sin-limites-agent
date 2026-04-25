import asyncio, sys
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def check():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, nombre, total_destinatarios FROM campanas ORDER BY id DESC LIMIT 1")
        print(dict(row))
        count = await conn.fetchval("SELECT COUNT(*) FROM campana_destinatarios WHERE campana_id=$1", row['id'])
        print(f'Destinatarios reales en DB: {count}')
    await close_pool()

asyncio.run(check())
