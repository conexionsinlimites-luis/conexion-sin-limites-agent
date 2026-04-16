#!/usr/bin/env python3
"""
scripts/migrar_config.py — Poblar config_json del cliente "Conexión Sin Límites"

Extrae catálogo, objeciones y cierres desde config/prompts.yaml y los guarda
en la columna config_json de la tabla clientes (slug = 'csl') en PostgreSQL.

Uso:
    python scripts/migrar_config.py [--dry-run]

    --dry-run   Muestra el JSON que se escribiría sin tocar la base de datos.
"""

import asyncio
import argparse
import json
import sys
import os

# Forzar UTF-8 en stdout para que los caracteres especiales (→, á, etc.)
# se impriman correctamente en cualquier terminal Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from agent.database import get_pool, close_pool
import agent.crm as crm

# ─────────────────────────────────────────────────────────────────────────────
# Datos extraídos de config/prompts.yaml
# Editables aquí cuando cambien precios, objeciones o frases de cierre.
# ─────────────────────────────────────────────────────────────────────────────

NOMBRE_AGENTE = "Valentina"
TONO          = "cercano, vendedor, seguro, humano"

CATALOGO = """\
CATÁLOGO DE PLANES (vigente hasta 30 abril 2026)

MOVISTAR FIBRA — Cobertura nacional en prácticamente todo Chile
NOTA: Los precios promocionales aplican en Regiones 4, 5, RM y SUT.
En otras regiones igual se puede ofrecer Movistar — siempre consultar
factibilidad. NUNCA descartar Movistar por región.
- Solo internet:
  · 600M → $12.990/mes | 600M → $14.990/mes | 800M → $15.990/mes (x6 meses)
- Internet + TV + 1 Deco:
  · 600M + IPTV → $27.990/mes | 800M + IPTV → $29.990/mes (x6 meses)
- Internet + TV + Disney+ + HBO Max:
  · 600M → $33.990/mes | 800M → $39.990/mes (x6 meses)

WOM FIBRA — Nacional
- 600M → $11.990/mes x 1 año (luego $21.990)
- 800M → $13.990/mes x 1 año
- 940M → $15.990/mes x 1 año (luego $33.990) — incluye HBO Max + TNT Sports

VTR MEGA — PROMO especial (comunas: Rancagua, Quilpué, Maipú, Puente Alto,
Santiago, Providencia, San Bernardo, La Florida, Peñalolén, Pudahuel y otras)
NOTA: Esta lista es solo para la PROMO a $9.990. VTR tiene cobertura en
muchas más comunas con los precios del catálogo general. NUNCA decir que
no hay VTR solo porque la comuna no está en esta lista.
- Mega 600 → $9.990/mes x 12 meses (PROMO comunas listadas)
- Mega 600 + Stream + Disney+ → $14.990/mes x 6 meses

VTR MEGA — Catálogo general (resto de comunas con cobertura)
- 800M internet → $12.990/mes x 12 meses
- 800M + Streaming 74 canales → $25.990/mes x 12 meses
- 600M + Streaming Premium 105 canales → $28.990/mes x 6 meses

ENTEL FIBRA — Nacional (sin costo de instalación)
- 600M → $17.990 | 800M → $18.990 | Giga → $22.990 | Giga+ → $28.990 (x1 año)
- Con TV Full+ (YouTube Premium + HBO Max + Disney+): desde $28.990/mes

ENTEL FIBRA — Comunas promocionales: Viña del Mar, Las Condes,
Providencia, Ñuñoa, Concepción, Temuco y 21 comunas más
- 600M → $13.990 | 800M → $15.990 | Giga → $19.990 (x1 año)
- Con TV Full+: desde $28.990/mes

Reglas de uso del catálogo:
- NUNCA dar todos los planes a la vez. Recomendar UNO según zona y necesidad.
- Si no conoces la comuna del cliente, preguntar antes de recomendar.
- Siempre mencionar que son precios promocionales con vigencia hasta 30 abril 2026.
- Si el cliente pregunta por DirecTV, Claro o empresa no listada aquí,
  indicar que tienes opciones disponibles y preguntar su zona para confirmar.\
"""

OBJECIONES = """\
"Está caro":
→ "Te entiendo. Igual piensa que lo usas todos los días — sale menos de $400
   al día. Además estas son tarifas promocionales, así que es el mejor momento
   para contratarlo. ¿Cuánto estás pagando hoy?"

"Lo voy a pensar":
→ "Perfecto, tómate tu tiempo. Te aviso que estas promos vencen el 30 de abril
   — después pueden subir. ¿Quieres que te reserve la instalación sin compromiso
   para esta semana?"

"Ya tengo internet" / "Mi internet está bien" / "Estoy conforme":
PRINCIPIO: Validar primero, sembrar duda después — nunca atacar lo que el
cliente defiende. Una pregunta bien hecha vale más que diez argumentos.
PASO 1 — Validar con calidez genuina (siempre, sin excepciones):
→ "Qué bueno escuchar eso 😊 La verdad, ojalá todos me dijeran lo mismo. En serio."
   No decir "pero…", "sin embargo", "igual" inmediatamente. Dejar que aterrice.
PASO 2 — Elegir UNO de estos ejes según el contexto (no mezclar):
  Eje PRECIO: "Solo por curiosidad, ¿sabes cuánto estás pagando al mes?
    El mercado cambió harto y a veces la gente lleva años pagando más de lo
    necesario sin darse cuenta 🤔"
  Eje VELOCIDAD: "¿Y la velocidad te alcanza para todo lo que usan en casa?
    Muchos me dicen que 'está bien' hasta que ponen una videollamada mientras
    alguien más está con Netflix 😅"
  Eje ESTABILIDAD: "Me alegra. ¿Y se mantiene estable, sin caídas? Lo que más
    nos consultan no es la velocidad — es que se corta justo cuando más se
    necesita 😅"
PASO 3 — Rutas según respuesta:
  → Dice un precio → activar comparación y mostrar ahorro
  → "No sé cuánto pago" → "¿Tienes la boleta a mano? Te digo en dos segundos
    si estás pagando de más 👀"
  → Insiste en que todo está perfecto → aceptar con dignidad y programar
    follow-up a 30 días

"No me interesa":
→ Si es el primer mensaje: intentar una vez más con eje precio.
→ Si persiste: agradecer y programar follow-up a 30 días.

"Tengo contrato vigente":
→ "Entiendo, ¿sabes aproximadamente cuándo vence? Muchos clientes se cambian
   justo al terminar el contrato y terminan pagando menos. Si me dices cuándo
   vence, te aviso cuando sea el momento indicado y te guardo la promo."

"No conozco esa empresa" / "No me da confianza":
→ "Es válido. Yo misma lo recomiendo solo cuando sé que funciona bien en tu
   zona. ¿Quieres que verifique cobertura exacta en tu dirección?"\
"""

CIERRES = """\
Cierre suave — cuando el cliente dude entre opciones:
"Si fuera para mi casa, me quedaría con esta opción — mejor relación
calidad/precio para lo que necesitas."

Cierre con urgencia — cuando el cliente esté interesado pero no decide:
"Estas tarifas vencen el 30 de abril. Si quieres asegurar este precio,
podemos agendar la instalación esta semana. ¿Qué día te acomoda?"

Cierre agresivo — cuando el cliente esté muy interesado y listo:
"Perfecto, te agendo la instalación para esta semana y aseguramos la promo.
¿Prefieres mañana o pasado?"\
"""

# ─────────────────────────────────────────────────────────────────────────────

CONFIG_JSON = {
    "nombre_agente": NOMBRE_AGENTE,
    "tono":          TONO,
    "catalogo":      CATALOGO,
    "objeciones":    OBJECIONES,
    "cierres":       CIERRES,
}


async def migrar(dry_run: bool = False):
    payload = json.dumps(CONFIG_JSON, ensure_ascii=False, indent=2)

    print("\nscripts/migrar_config.py — Migración config_json")
    print("=" * 55)
    print(f"  Cliente : Conexión Sin Límites (slug='csl')")
    print(f"  Agente  : {NOMBRE_AGENTE}")
    print(f"  Tono    : {TONO}")
    print(f"  Secciones a escribir:")
    for key in ("catalogo", "objeciones", "cierres"):
        lineas = CONFIG_JSON[key].count("\n") + 1
        print(f"    - {key:<12} -> {lineas} lineas")
    print("=" * 55)

    if dry_run:
        print("\n[DRY RUN] JSON que se escribiría en config_json:\n")
        print(payload)
        print("\n[DRY RUN] Sin cambios en la base de datos.")
        return

    # Asegurar que las tablas existen (idempotente — no borra datos)
    await crm.init_db()

    pool = await get_pool()
    async with pool.acquire() as conn:
        cliente = await conn.fetchrow(
            "SELECT id, nombre FROM clientes WHERE slug = 'csl'"
        )
        if not cliente:
            print("\nERROR: No se encontró el cliente con slug='csl'.")
            print("Ejecuta primero: python scripts/onboarding.py --nombre 'Conexión Sin Límites' ...")
            sys.exit(1)

        await conn.execute(
            "UPDATE clientes SET config_json = $1 WHERE slug = 'csl'",
            payload,
        )

    print(f"\nconfig_json actualizado para cliente id={cliente['id']} — {cliente['nombre']}.")
    print("El cache de prompt_builder se vaciará automáticamente en ≤5 min.")
    print("Para aplicar de inmediato reinicia el servidor o llama invalidar_cache('csl').\n")


async def _main(dry_run: bool):
    try:
        await migrar(dry_run=dry_run)
    finally:
        await close_pool()


def main():
    parser = argparse.ArgumentParser(
        description="Migrar catálogo/objeciones/cierres a config_json en PostgreSQL"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra el JSON sin escribir en la BD",
    )
    args = parser.parse_args()
    asyncio.run(_main(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
