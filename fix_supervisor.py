import asyncio, sys, json
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def fix():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT config_json FROM clientes WHERE slug='csl'")
        config = json.loads(row['config_json'])
        config['supervisor_nombre'] = 'Luis Barrios'
        config['supervisor_telefono'] = '56978016298'
        config['supervisor_instruccion'] = 'El supervisor es Luis Barrios. Si el cliente pregunta si hay novedades o algo pendiente, dale un resumen breve y directo de su estado actual.'
        await conn.execute(
            "UPDATE clientes SET config_json=$1 WHERE slug='csl'",
            json.dumps(config)
        )
        print('OK - supervisor agregado al config_json')
    await close_pool()

asyncio.run(fix())
