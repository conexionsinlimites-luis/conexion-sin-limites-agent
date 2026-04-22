import asyncio, sys
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def fix():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            ALTER TABLE followup_programado 
            ADD COLUMN IF NOT EXISTS tipo_followup VARCHAR(20) DEFAULT 'suave'
        """)
        print('OK - columna tipo_followup agregada')
    await close_pool()

asyncio.run(fix())
