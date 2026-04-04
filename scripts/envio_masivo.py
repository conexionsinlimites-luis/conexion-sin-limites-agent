# scripts/envio_masivo.py — Envío masivo de plantilla WhatsApp desde Excel
# Conexion Sin Limites

"""
Lee un archivo Excel con columnas 'cliente', 'tel_limpio' y 'prioridad',
y envía la plantilla 'bienvenida_conexion' a cada número via Meta Cloud API.

Uso:
    python scripts/envio_masivo.py archivo.xlsx [--prioridad N] [--limite N]

    --prioridad  Filtra solo filas con ese valor en columna 'prioridad' (default: 1)
    --limite     Máximo de contactos a enviar (default: 100)

El Excel debe tener estas columnas (primera fila = encabezados):
    cliente           | tel_limpio    | prioridad
    Juan Pérez        | 56912345678   | 1
    María García      | 56987654321   | 2

IMPORTANTE:
- tel_limpio debe incluir código de país SIN el signo +  (ej: 56978016298)
- La plantilla 'bienvenida_conexion' debe estar aprobada en Meta
- El parámetro {{1}} usa el primer nombre + primer apellido del contacto
  Ejemplos:
    "Juan Carlos Pérez González" → "Juan Pérez"
    "Juan Pérez González"        → "Juan Pérez"
    "María García"               → "María García"
    "Carlos"                     → "Carlos"
"""

import os
import sys
import time
import argparse
import httpx
import openpyxl
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ──────────────────────────────────────────
ACCESS_TOKEN    = "".join((os.getenv("META_ACCESS_TOKEN") or "").split())
PHONE_NUMBER_ID = "".join((os.getenv("META_PHONE_NUMBER_ID") or "").split())
TEMPLATE_NAME   = "bienvenida_conexion"
TEMPLATE_LANG   = "es_CL"
API_VERSION     = "v21.0"
PAUSA_SEGUNDOS  = 1   # pausa entre envíos para respetar rate limits de Meta
# ───────────────────────────────────────────────────────────


def primer_nombre_apellido(nombre_completo: str) -> str:
    """
    Extrae el primer nombre y primer apellido de un nombre completo.

    Lógica según cantidad de palabras:
      1 palabra  → "Juan"               → "Juan"
      2 palabras → "Juan Pérez"         → "Juan Pérez"
      3 palabras → "Juan Pérez González"→ "Juan Pérez"   (1 nombre, 2 apellidos)
      4+ palabras→ "Juan Carlos Pérez González" → "Juan Pérez" (2 nombres, 2 apellidos)
    """
    partes = nombre_completo.strip().split()
    if len(partes) <= 2:
        return " ".join(partes)
    elif len(partes) == 3:
        # Formato: nombre apellido1 apellido2 → tomar nombre + apellido1
        return f"{partes[0]} {partes[1]}"
    else:
        # Formato: nombre1 nombre2 apellido1 apellido2 → tomar nombre1 + apellido1
        return f"{partes[0]} {partes[2]}"


def leer_excel(ruta: str, prioridad: int = 1, limite: int = 100) -> list[dict]:
    """
    Lee el Excel y retorna lista de {nombre, telefono} filtrando por prioridad y limite.

    Columnas requeridas: 'cliente', 'tel_limpio', 'prioridad'
    """
    wb = openpyxl.load_workbook(ruta)
    ws = wb.active

    # Detectar columnas por nombre de encabezado (case-insensitive)
    encabezados = {str(cell.value).strip().lower(): idx
                   for idx, cell in enumerate(next(ws.iter_rows(min_row=1, max_row=1)), start=1)}

    requeridas = ["cliente", "tel_limpio", "prioridad"]
    faltantes = [c for c in requeridas if c not in encabezados]
    if faltantes:
        print(f"ERROR: Faltan columnas en el Excel: {', '.join(faltantes)}")
        print(f"Columnas encontradas: {', '.join(encabezados.keys())}")
        sys.exit(1)

    col_nombre    = encabezados["cliente"]
    col_telefono  = encabezados["tel_limpio"]
    col_prioridad = encabezados["prioridad"]

    contactos = []
    omitidos  = 0
    for fila in ws.iter_rows(min_row=2, values_only=True):
        nombre        = str(fila[col_nombre - 1] or "").strip()
        telefono      = str(fila[col_telefono - 1] or "").strip()
        prio_valor    = fila[col_prioridad - 1]

        # Filtrar por prioridad
        try:
            if int(prio_valor) != prioridad:
                omitidos += 1
                continue
        except (TypeError, ValueError):
            omitidos += 1
            continue

        # Limpiar teléfono: remover +, espacios y guiones
        telefono = telefono.replace("+", "").replace(" ", "").replace("-", "")

        if nombre and telefono:
            contactos.append({"nombre": nombre, "telefono": telefono})

        if len(contactos) >= limite:
            break

    print(f"Filas omitidas (prioridad != {prioridad}): {omitidos}")
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
    ws.append(["nombre_completo", "nombre_plantilla", "telefono", "estado", "detalle", "timestamp"])

    for r in resultados:
        ws.append([r["nombre"], r["nombre_plantilla"], r["telefono"], r["estado"], r["detalle"], r["timestamp"]])

    wb.save(nombre_log)
    return nombre_log


def main():
    parser = argparse.ArgumentParser(description="Envío masivo de plantilla WhatsApp")
    parser.add_argument("archivo", help="Ruta al archivo Excel")
    parser.add_argument("--prioridad", type=int, default=1,
                        help="Valor de prioridad a filtrar (default: 1)")
    parser.add_argument("--limite", type=int, default=100,
                        help="Máximo de contactos a enviar (default: 100)")
    args = parser.parse_args()

    ruta_excel = args.archivo
    if not os.path.exists(ruta_excel):
        print(f"ERROR: No se encontró el archivo '{ruta_excel}'")
        sys.exit(1)

    if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
        print("ERROR: META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no están configurados en .env")
        sys.exit(1)

    # Leer contactos filtrados por prioridad y limitados
    contactos = leer_excel(ruta_excel, prioridad=args.prioridad, limite=args.limite)
    total = len(contactos)
    print(f"\nContactos cargados: {total} (prioridad={args.prioridad}, limite={args.limite})")
    print(f"Plantilla: {TEMPLATE_NAME} | Idioma: {TEMPLATE_LANG}")
    print(f"Phone Number ID: {PHONE_NUMBER_ID} (+56941762315)")
    print("-" * 50)

    confirmacion = input(f"Enviar a {total} contactos? (si/no): ").strip().lower()
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

            nombre_plantilla = primer_nombre_apellido(nombre)
            ok, detalle = enviar_plantilla(cliente, nombre_plantilla, telefono)
            estado = "enviado" if ok else "error"
            ts = datetime.now().strftime("%H:%M:%S")

            resultados.append({
                "nombre": nombre,
                "telefono": telefono,
                "nombre_plantilla": nombre_plantilla,
                "estado": estado,
                "detalle": detalle,
                "timestamp": ts,
            })

            icono = "OK" if ok else "ERROR"
            print(f"[{i}/{total}] {icono} {nombre} -> '{nombre_plantilla}' ({telefono}) - {detalle}")

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
    print(f"  Envio completado")
    print(f"  Exitosos : {exitosos}")
    print(f"  Fallidos : {fallidos}")
    print(f"  Log      : {archivo_log}")
    print("=" * 50)


if __name__ == "__main__":
    main()
