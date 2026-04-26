import asyncio, sys, json
sys.path.insert(0, '.')
from agent.database import get_pool, close_pool

async def ver():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM clientes WHERE id=5")
        if row:
            data = dict(row)
            if data.get('config_json'):
                data['config_json'] = json.loads(data['config_json'])
            print(json.dumps(data, indent=2, default=str))
        else:
            print('No existe cliente id=5')
    await close_pool()

asyncio.run(ver())
