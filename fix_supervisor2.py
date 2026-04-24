import asyncio, sys, json
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def fix():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT config_json FROM clientes WHERE slug='csl'")
        config = json.loads(row['config_json'])
        config['supervisor_instruccion'] = 'El supervisor es Luis Barrios, numero 56978016298. Si quien escribe es el numero 56978016298, reconocelo como tu jefe y supervisor. Dale un resumen breve de leads pendientes, calientes o listos para cierre si te lo pide. Trátalo con confianza y de forma directa.'
        await conn.execute(
            "UPDATE clientes SET config_json=$1 WHERE slug='csl'",
            json.dumps(config)
        )
        print('OK')
    await close_pool()

asyncio.run(fix())
