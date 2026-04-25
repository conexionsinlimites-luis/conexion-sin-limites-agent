import asyncio, sys, csv
sys.path.insert(0, '.')
from agent.providers import obtener_proveedor

async def enviar():
    proveedor = obtener_proveedor()
    enviados = 0
    fallidos = 0
    with open('campana_manana_9am_100.csv', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            tel = row['telefono'].strip()
            nombre = row['nombre'].strip().split()[0]
            try:
                ok = await proveedor.enviar_template(tel, 'bienvenida_conexion', [nombre])
                if ok:
                    enviados += 1
                    print(f'OK {tel} - {nombre}')
                else:
                    fallidos += 1
                    print(f'FALLO {tel}')
            except Exception as e:
                fallidos += 1
                print(f'ERROR {tel}: {e}')
            await asyncio.sleep(0.5)
    print(f'Total: {enviados} enviados, {fallidos} fallidos')

asyncio.run(enviar())
