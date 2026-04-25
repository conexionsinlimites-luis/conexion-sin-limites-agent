import asyncio, sys, csv, os
import httpx
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("META_ACCESS_TOKEN") or os.getenv("TOKEN_DE_ACCESO_META")
PHONE_ID = os.getenv("META_PHONE_NUMBER_ID") or os.getenv("ID_DE_NUMERO_DE_TELEFONO_META")

async def enviar():
    if not TOKEN or not PHONE_ID:
        print(f"ERROR: TOKEN={TOKEN[:20] if TOKEN else 'NONE'} PHONE_ID={PHONE_ID}")
        return
    enviados = fallidos = 0
    async with httpx.AsyncClient() as client:
        with open('test_envio.csv', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                tel = row['telefono'].strip()
                nombre = row['nombre'].strip().split()[0]
                payload = {
                    "messaging_product": "whatsapp",
                    "to": tel,
                    "type": "template",
                    "template": {
                        "name": "bienvenida_conexion",
                        "language": {"code": "es_CL"},
                        "components": [
                            {
                                "type": "body",
                                "parameters": [{"type": "text", "text": nombre}]
                            }
                        ]
                    }
                }
                r = await client.post(
                    f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages",
                    json=payload,
                    headers={"Authorization": f"Bearer {TOKEN}"}
                )
                if r.status_code == 200:
                    enviados += 1
                    print(f"OK {tel} - {nombre}")
                else:
                    fallidos += 1
                    print(f"FALLO {tel}: {r.text[:100]}")
                await asyncio.sleep(0.5)
    print(f"\nTotal: {enviados} enviados, {fallidos} fallidos")

asyncio.run(enviar())
