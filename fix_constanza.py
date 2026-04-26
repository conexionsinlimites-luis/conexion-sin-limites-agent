import asyncio, sys
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def fix():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE clientes SET 
                whatsapp_phone_id = '1105775159282550',
                whatsapp_token = 'USAR_TOKEN_DE_RAILWAY'
            WHERE id = 5
        """)
        print('Constanza actualizada OK')
    await close_pool()

asyncio.run(fix())
