import asyncio, sys, json
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def test():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT config_json FROM clientes WHERE slug='csl'")
        if row and row['config_json']:
            config = json.loads(row['config_json'])
            tiene = all(config.get(s) for s in ['catalogo', 'objeciones', 'cierres'])
            print(f'config_json OK: {tiene}')
            print(f'Claves: {list(config.keys())}')
        else:
            print('ERROR: config_json vacío o no encontrado')
    await close_pool()

asyncio.run(test())
