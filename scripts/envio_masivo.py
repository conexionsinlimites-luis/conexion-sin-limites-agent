# scripts/envio_masivo.py — Envío masivo de plantilla WhatsApp desde Excel
# Conexion Sin Limites

"""
Lee un archivo Excel con columnas 'nombre' y 'telefono',
y envía la plantilla 'bienvenida_conexion' a cada número via Meta Cloud API.

Uso:
    python scripts/envio_masivo.py contactos.xlsx

El Excel debe tener estas columnas (primera fila = encabezados):
    nombre    | telefono
    Juan Pérez| 56912345678
    María G.  | 56987654321

IMPORTANTE:
- El teléfono debe incluir código de país SIN el signo +  (ej: 56978016298)
- La plantilla 'bienvenida_conexion' debe estar aprobada en Meta
- Se asume que el parámetro {{1}} de la plantilla es el nombre del contacto
"""

import os
import sys
import time
import httpx
import openpyxl
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ──────────────────────────────────────────
ACCESS_TOKEN    = "".join((os.getenv("META_ACCESS_TOKEN") or "").split())
PHONE_NUMBER_ID = "".join((os.getenv("META_PHONE_NUMBER_ID") or "").split())
TEMPLATE_NAME   = "bienvenida_conexion"
TEMPLATE_LANG   = "es"
API_VERSION     = "v21.0"
PAUSA_SEGUNDOS  = 1   # pausa entre envíos para respetar rate limits de Meta
# ───────────────────────────────────────────────────────────


def leer_excel(ruta: str) -> list[dict]:
    """Lee el Excel y retorna lista de {nombre, telefono}."""
    wb = openpyxl.load_workbook(ruta)
    ws = wb.active

    # Detectar columnas por nombre de encabezado (case-insensitive)
    encabezados = {str(cell.value).strip().lower(): idx
                   for idx, cell in enumerate(next(ws.iter_rows(min_row=1, max_row=1)), start=1)}

    if "nombre" not in encabezados or "telefono" not in encabezados:
        print("ERROR: El Excel debe tener columnas 'nombre' y 'telefono'")
        sys.exit(1)

    col_nombre   = encabezados["nombre"]
    col_telefono = encabezados["telefono"]

    contactos = []
    for fila in ws.iter_rows(min_row=2, values_only=True):
        nombre   = str(fila[col_nombre - 1] or "").strip()
        telefono = str(fila[col_telefono - 1] or "").strip()

        # Limpiar teléfono: remover +, espacios y guiones
        telefono = telefono.replace("+", "").replace(" ", "").replace("-", "")

        if nombre and telefono:
            contactos.append({"nombre": nombre, "telefono": telefono})

    return contactos


def enviar_plantilla(cliente: httpx.Client, nombre: str, telefono: str) -> tuple[bool, str]:
    """
    Envía la plantilla 'bienvenida_conexion' a un número.
    Retorna (éxito, mensaje_error_o_id).
    """
    url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": TEMPLATE_LANG},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": nombre}
                    ]
                }
            ]
        }
    }

    try:
        r = cliente.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code == 200:
            msg_id = r.json().get("messages", [{}])[0].get("id", "ok")
            return True, msg_id
        else:
            error = r.json().get("error", {}).get("message", r.text)
            return False, error
    except Exception as e:
        return False, str(e)


def guardar_log(resultados: list[dict], ruta_excel: str):
    """Guarda un log Excel con los resultados del envío."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_log = f"envio_log_{timestamp}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Resultados"
    ws.append(["nombre", "telefono", "estado", "detalle", "timestamp"])

    for r in resultados:
        ws.append([r["nombre"], r["telefono"], r["estado"], r["detalle"], r["timestamp"]])

    wb.save(nombre_log)
    return nombre_log


def main():
    if len(sys.argv) < 2:
        print("Uso: python scripts/envio_masivo.py archivo.xlsx")
        sys.exit(1)

    ruta_excel = sys.argv[1]
    if not os.path.exists(ruta_excel):
        print(f"ERROR: No se encontró el archivo '{ruta_excel}'")
        sys.exit(1)

    if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
        print("ERROR: META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no están configurados en .env")
        sys.exit(1)

    # Leer contactos
    contactos = leer_excel(ruta_excel)
    total = len(contactos)
    print(f"\nContactos cargados: {total}")
    print(f"Plantilla: {TEMPLATE_NAME} | Idioma: {TEMPLATE_LANG}")
    print(f"Phone Number ID: {PHONE_NUMBER_ID}")
    print("-" * 50)

    confirmacion = input(f"¿Enviar a {total} contactos? (si/no): ").strip().lower()
    if confirmacion != "si":
        print("Cancelado.")
        sys.exit(0)

    print()
    resultados = []
    exitosos = 0
    fallidos = 0

    with httpx.Client() as cliente:
        for i, contacto in enumerate(contactos, start=1):
            nombre   = contacto["nombre"]
            telefono = contacto["telefono"]

            ok, detalle = enviar_plantilla(cliente, nombre, telefono)
            estado = "enviado" if ok else "error"
            ts = datetime.now().strftime("%H:%M:%S")

            resultados.append({
                "nombre": nombre,
                "telefono": telefono,
                "estado": estado,
                "detalle": detalle,
                "timestamp": ts,
            })

            icono = "✅" if ok else "❌"
            print(f"[{i}/{total}] {icono} {nombre} ({telefono}) — {detalle}")

            if ok:
                exitosos += 1
            else:
                fallidos += 1

            # Pausa entre envíos para respetar rate limits
            if i < total:
                time.sleep(PAUSA_SEGUNDOS)

    # Guardar log
    archivo_log = guardar_log(resultados, ruta_excel)

    print()
    print("=" * 50)
    print(f"  Envío completado")
    print(f"  ✅ Exitosos : {exitosos}")
    print(f"  ❌ Fallidos : {fallidos}")
    print(f"  📄 Log      : {archivo_log}")
    print("=" * 50)


if __name__ == "__main__":
    main()
