# tests/eval_valentina.py — Suite de evaluacion de Valentina y el modo dueno
# Conexion Sin Limites

"""
Corre casos de prueba fijos, basados en bugs reales encontrados en
produccion (calculo de decos DirecTV, comuna sola, fechas de vencimiento
hardcodeadas, ruteo cruzado DirecTV/VTR/Movistar, router del modo dueno).

Dos categorias:

  DETERMINISTA — no llama a ningun modelo. Gratis, corre en milisegundos.
                 Prueba codigo Python puro (parseo de fechas, extraccion de
                 marcadores, ruteo por keywords) y el texto estatico del
                 catalogo. Los que tocan la BD insertan filas de prueba
                 (prefijo de telefono "999...") y las borran al terminar.

  MODELO       — llama al modelo real (Sonnet para el prompt de ventas,
                 Haiku para el router del modo dueno). Prueba si Valentina
                 "entiende" algo, no solo si el codigo funciona -- por eso
                 no se puede reemplazar por logica determinista. Gasta
                 credito real (acotado: 1 mensaje corto por caso). Solo
                 corre si se pasa --con-modelo.

Uso:
    python tests/eval_valentina.py                # solo deterministas
    python tests/eval_valentina.py --con-modelo    # todos (gasta credito real)

Seguridad: los casos deterministas que tocan BD se niegan a correr si
DATABASE_URL apunta al host de produccion conocido (maglev.proxy.rlwy.net).
Configura una DATABASE_URL distinta (o vacia/local) antes de correr esto.
"""

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from agent.config import DATABASE_URL, ANTHROPIC_API_KEY

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOST_PRODUCCION = "maglev.proxy.rlwy.net"
_TEST_PHONE_PREFIX = "999"
_TEST_MARKER = "__EVAL_TEST__"


@dataclass
class Resultado:
    nombre: str
    categoria: str  # "DETERMINISTA" | "MODELO"
    ok: bool
    detalle: str = ""


RESULTADOS: list[Resultado] = []


async def _caso(nombre: str, categoria: str, fn):
    try:
        await fn()
        RESULTADOS.append(Resultado(nombre, categoria, True))
    except AssertionError as e:
        RESULTADOS.append(Resultado(nombre, categoria, False, str(e)))
    except Exception as e:
        RESULTADOS.append(Resultado(nombre, categoria, False, f"ERROR INESPERADO: {e!r}"))


def _verificar_bd_no_produccion():
    if _HOST_PRODUCCION in (DATABASE_URL or ""):
        raise RuntimeError(
            f"DATABASE_URL apunta al host de producción conocido ({_HOST_PRODUCCION}). "
            "Este caso inserta y borra filas de prueba — configura una "
            "DATABASE_URL distinta antes de correr esta suite."
        )


async def _insertar_lead_fixture(
    conn, telefono, created_at, estado="nuevo", ultima_interaccion=None, subproducto="DirecTV"
):
    await conn.execute(
        """
        INSERT INTO leads (telefono, nombre, identificador, estado, score, subproducto,
                            origen, created_at, ultima_interaccion)
        VALUES ($1, $2, $3, $4, $5, $6, 'eval_test', $7, $8)
        ON CONFLICT (telefono) DO UPDATE SET
            estado = EXCLUDED.estado,
            created_at = EXCLUDED.created_at,
            ultima_interaccion = EXCLUDED.ultima_interaccion,
            subproducto = EXCLUDED.subproducto
        """,
        telefono, _TEST_MARKER, f"EVAL-{telefono}", estado, 10, subproducto,
        created_at, ultima_interaccion or created_at,
    )


async def _limpiar_fixtures():
    from agent.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM historial_mensajes WHERE telefono LIKE $1", f"{_TEST_PHONE_PREFIX}%")
        await conn.execute(
            "DELETE FROM leads WHERE nombre = $1 OR telefono LIKE $2", _TEST_MARKER, f"{_TEST_PHONE_PREFIX}%"
        )


# ════════════════════════════════════════════════════════════════
# CATEGORÍA A — DETERMINISTAS
# ════════════════════════════════════════════════════════════════

async def caso_rango_fecha_chile():
    from agent import crm

    desde, hasta = crm.rango_fecha_chile("2026-09-07", "2026-09-10")
    dias = (hasta - desde).total_seconds() / 86400
    assert desde < hasta, f"desde ({desde}) debería ser antes que hasta ({hasta})"
    assert 3.9 <= dias <= 4.0, f"el rango 07→10 sept debería cubrir ~4 días, cubrió {dias:.2f}"

    # Bug real de esta noche: fecha_hasta ausente colapsaba el rango a un
    # solo día en vez de cubrir el día completo (00:00 a 23:59:59 Chile).
    desde2, hasta2 = crm.rango_fecha_chile("2026-09-07")
    assert (hasta2 - desde2).total_seconds() > 86000, (
        f"un rango de un solo día debería cubrir ~24h, cubrió "
        f"{(hasta2 - desde2).total_seconds() / 3600:.1f}h"
    )


async def caso_leads_nuevos_fixtures():
    _verificar_bd_no_produccion()
    from agent import crm
    from agent.database import get_pool

    await _limpiar_fixtures()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await _insertar_lead_fixture(conn, "9990000001", datetime(2026, 9, 8, 15, 0, 0))
            await _insertar_lead_fixture(conn, "9990000002", datetime(2026, 9, 10, 10, 0, 0))
            await _insertar_lead_fixture(conn, "9990000003", datetime(2026, 9, 1, 10, 0, 0))  # fuera de rango

        resultado = await crm.leads_nuevos("2026-09-07", "2026-09-10")
        assert resultado["total"] == 2, f"esperaba 2 leads nuevos en el rango, obtuvo {resultado['total']}"
        telefonos = {d["telefono"] for d in resultado["detalle"]}
        assert telefonos == {"9990000001", "9990000002"}, f"detalle inesperado: {telefonos}"
    finally:
        await _limpiar_fixtures()


async def caso_contactos_activos_fixtures():
    _verificar_bd_no_produccion()
    from agent import crm
    from agent.database import get_pool

    await _limpiar_fixtures()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Lead viejo (created_at fuera de rango) que escribió DENTRO del período
            # -- exactamente el caso real: "quién me escribió hoy y ayer".
            await _insertar_lead_fixture(conn, "9990000004", datetime(2026, 1, 1, 10, 0, 0))
            await conn.execute(
                "INSERT INTO historial_mensajes (telefono, rol, mensaje, timestamp) "
                "VALUES ($1, 'user', 'hola', $2)",
                "9990000004", datetime(2026, 9, 9, 23, 30, 0),
            )
            # Mensaje fuera del período -- no debe contar
            await conn.execute(
                "INSERT INTO historial_mensajes (telefono, rol, mensaje, timestamp) "
                "VALUES ($1, 'user', 'hola vieja', $2)",
                "9990000005", datetime(2026, 8, 1, 10, 0, 0),
            )

        resultado = await crm.contactos_activos("2026-09-09", "2026-09-10")
        assert resultado["total"] == 1, f"esperaba 1 contacto activo, obtuvo {resultado['total']}"
        assert resultado["detalle"][0]["telefono"] == "9990000004", resultado["detalle"]
    finally:
        await _limpiar_fixtures()


async def caso_ventas_cerradas_detalle():
    _verificar_bd_no_produccion()
    from agent import crm
    from agent.database import get_pool

    await _limpiar_fixtures()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await _insertar_lead_fixture(
                conn, "9990000006", datetime(2026, 9, 1, 10, 0, 0),
                estado="cerrado", ultima_interaccion=datetime(2026, 9, 8, 12, 0, 0),
                subproducto="VTR",
            )

        resultado = await crm.ventas_cerradas("2026-09-07", "2026-09-10")
        assert resultado["total"] == 1, f"esperaba 1 venta cerrada, obtuvo {resultado['total']}"
        assert resultado["detalle"][0]["telefono"] == "9990000006", resultado["detalle"]
        assert resultado["detalle"][0]["subproducto"] == "VTR", resultado["detalle"]
    finally:
        await _limpiar_fixtures()


async def caso_ruteo_cruzado_directv_vtr():
    from agent.main import _clasificar_producto_lead

    lead_dtv = {"lead_resumen": "Cliente interesado en DirecTV full", "subproducto": ""}
    lead_vtr = {"lead_resumen": "Preguntó por VTR y Movistar", "subproducto": ""}
    lead_otro = {"lead_resumen": "Preguntó por Entel", "subproducto": ""}
    lead_falso_positivo = {"lead_resumen": "quiere buen servicio y atención", "subproducto": ""}

    assert _clasificar_producto_lead(lead_dtv, []) == "directv"
    assert _clasificar_producto_lead(lead_vtr, []) == "vtr_movistar"
    assert _clasificar_producto_lead(lead_otro, []) == "otro"
    assert _clasificar_producto_lead(lead_falso_positivo, []) == "otro", (
        "falso positivo: 'servicio' no debería matchear 'vtr' por substring"
    )


async def caso_cambio_modo_producto():
    from agent.main import _detectar_cambio_modo_producto

    assert _detectar_cambio_modo_producto("quiero que respondas solo dtv") == "directv"
    assert _detectar_cambio_modo_producto("solo vtr y movistar por ahora") == "vtr_movistar"
    assert _detectar_cambio_modo_producto("vuelve a todos los productos") == "todos"
    assert _detectar_cambio_modo_producto("hola como estas") is None


async def caso_extraer_alerta_supervisor():
    from agent.main import _extraer_alerta

    texto = (
        "Perfecto, ya tengo todo!\n"
        "[ALERTA_SUPERVISOR|nombre=Juan Perez|tel=56912345678|dir=Calle Falsa 123, Providencia]"
    )
    limpio, datos = _extraer_alerta(texto)
    assert datos is not None, "debería haber detectado la alerta"
    assert datos["nombre"] == "Juan Perez", datos
    assert datos["tel"] == "56912345678", datos
    assert "ALERTA_SUPERVISOR" not in limpio, "el marcador no debe llegar al cliente"


async def caso_extraer_alerta_duda():
    from agent.main import _extraer_alerta_duda

    texto = "Buena pregunta, déjame confirmar eso con el equipo [ALERTA_DUDA|pregunta=cual es el precio de WOM]"
    limpio, pregunta = _extraer_alerta_duda(texto)
    assert pregunta == "cual es el precio de WOM", pregunta
    assert "ALERTA_DUDA" not in limpio, "el marcador no debe llegar al cliente"

    limpio2, pregunta2 = _extraer_alerta_duda("mensaje normal sin marcador")
    assert pregunta2 is None, "no debería detectar nada sin el marcador"


async def caso_validar_campos_obligatorios():
    from agent.main import _validar_campos_obligatorios

    incompleto = {"tipo": "venta", "nombre": "Juan", "campos_faltantes": []}
    faltantes = _validar_campos_obligatorios(incompleto)
    assert "telefono" in faltantes, faltantes
    assert "producto" in faltantes, faltantes  # es venta -> producto es obligatorio

    completo = {
        "tipo": "venta", "nombre": "Juan", "telefono": "56912345678",
        "producto": "DirecTV", "campos_faltantes": [],
    }
    assert _validar_campos_obligatorios(completo) == []


async def caso_router_dueno_multi_llamada():
    import agent.main as main

    bloque_hoy = SimpleNamespace(
        type="tool_use", name="contactos_activos",
        input={"fecha_desde": "2026-09-10", "fecha_hasta": "2026-09-10"},
    )
    bloque_ayer = SimpleNamespace(
        type="tool_use", name="contactos_activos",
        input={"fecha_desde": "2026-09-09", "fecha_hasta": "2026-09-09"},
    )
    respuesta_falsa = SimpleNamespace(content=[bloque_hoy, bloque_ayer])

    async def _fake_create(*args, **kwargs):
        return respuesta_falsa

    original_create = main.claude_client.messages.create
    main.claude_client.messages.create = _fake_create
    try:
        llamadas = await main._rutear_consulta_dueño("quien me escribio hoy y ayer")
    finally:
        main.claude_client.messages.create = original_create

    assert len(llamadas) == 2, f"esperaba 2 llamadas (hoy y ayer), obtuvo {len(llamadas)}"
    nombres = [n for n, _ in llamadas]
    assert nombres == ["contactos_activos", "contactos_activos"], nombres


_PATRON_FECHA_DURA = re.compile(
    r"\b\d{1,2}\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|octubre|noviembre|diciembre)\b",
    re.IGNORECASE,
)


async def caso_sin_fechas_vencidas_hardcodeadas():
    archivos = ["config/prompts.yaml", "config/prompt_base.txt", "config/comportamiento.md"]
    encontrados = []
    for ruta in archivos:
        ruta_completa = os.path.join(_RAIZ, ruta)
        if not os.path.exists(ruta_completa):
            continue
        with open(ruta_completa, "r", encoding="utf-8") as f:
            texto = f.read()
        for m in _PATRON_FECHA_DURA.finditer(texto):
            inicio, fin = max(0, m.start() - 40), min(len(texto), m.end() + 40)
            encontrados.append(f"{ruta}: ...{texto[inicio:fin]}...")
    assert not encontrados, "fecha de vencimiento hardcodeada encontrada:\n" + "\n".join(encontrados)


CASOS_DETERMINISTAS = [
    ("rango_fecha_chile límites correctos", caso_rango_fecha_chile),
    ("leads_nuevos con fixtures", caso_leads_nuevos_fixtures),
    ("contactos_activos con fixtures", caso_contactos_activos_fixtures),
    ("ventas_cerradas trae detalle", caso_ventas_cerradas_detalle),
    ("ruteo cruzado DirecTV/VTR/Movistar", caso_ruteo_cruzado_directv_vtr),
    ("cambio de modo de producto", caso_cambio_modo_producto),
    ("extracción [ALERTA_SUPERVISOR]", caso_extraer_alerta_supervisor),
    ("extracción [ALERTA_DUDA]", caso_extraer_alerta_duda),
    ("validación de campos obligatorios (carga)", caso_validar_campos_obligatorios),
    ("router dueño ejecuta TODAS las llamadas (mock)", caso_router_dueno_multi_llamada),
    ("sin fechas de vencimiento hardcodeadas", caso_sin_fechas_vencidas_hardcodeadas),
]


# ════════════════════════════════════════════════════════════════
# CATEGORÍA B — REQUIEREN MODELO REAL (gastan crédito, solo con --con-modelo)
# ════════════════════════════════════════════════════════════════

def _cargar_system_prompt_real() -> str:
    with open(os.path.join(_RAIZ, "config", "prompts.yaml"), "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["system_prompt"]


async def _preguntar_sonnet(mensaje: str, max_tokens: int = 500) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    respuesta = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=_cargar_system_prompt_real(),
        messages=[{"role": "user", "content": mensaje}],
    )
    return "".join(b.text for b in respuesta.content if b.type == "text")


async def caso_precio_2_decos_adicionales():
    texto = await _preguntar_sonnet(
        "Hola, quiero cotizar el Duo DTH Esencial 800 Megas pagando en efectivo, "
        "y necesito agregar 2 decos adicionales. Cuanto seria el total mensual?"
    )
    assert "38.910" in texto, f"no calculó el total correcto. Respuesta:\n{texto}"
    assert "35.970" not in texto, f"repitió el error de aplicar descuento a ambos decos. Respuesta:\n{texto}"
    assert "3.980" not in texto, f"repitió el error de aplicar descuento a ambos decos. Respuesta:\n{texto}"


async def caso_comuna_sola_pide_calle_numero():
    texto = await _preguntar_sonnet("Hola, vivo en Providencia, cuanto me saldria el internet?")
    texto_low = texto.lower()
    assert "calle" in texto_low or "dirección" in texto_low or "direccion" in texto_low, (
        f"no pidió la dirección completa. Respuesta:\n{texto}"
    )
    assert "[alerta_supervisor" not in texto_low, (
        f"disparó la alerta de dirección completa con solo la comuna. Respuesta:\n{texto}"
    )


async def caso_wom_sin_precio_inventado():
    texto = await _preguntar_sonnet("Hola, cuanto cuesta el plan de WOM de 600 megas?")
    assert not re.search(r"\$\s?\d", texto), f"inventó un precio de WOM. Respuesta:\n{texto}"


async def caso_router_dueno_preguntas_reales():
    import agent.main as main

    casos = [
        ("cuantos leads entraron esta semana", "leads_nuevos"),
        ("cuantas ventas llevamos este mes", "ventas_cerradas"),
        ("cuanto pesa la luna", "dato_no_registrado"),
        ("busca el telefono de Juan Perez", "buscar_lead_por_nombre"),
    ]
    fallos = []
    for pregunta, esperado in casos:
        llamadas = await main._rutear_consulta_dueño(pregunta)
        nombres = [n for n, _ in llamadas]
        if esperado not in nombres:
            fallos.append(f"'{pregunta}' -> esperaba incluir {esperado}, obtuvo {nombres}")
    assert not fallos, "fallos de ruteo:\n" + "\n".join(fallos)

    llamadas = await main._rutear_consulta_dueño("quien me escribio hoy y ayer")
    assert len(llamadas) >= 2, f"esperaba al menos 2 llamadas para 'hoy y ayer', obtuvo {len(llamadas)}"


async def caso_catalogo_extensor_gratis_940():
    import agent.main as main

    pregunta = "cuanto por internet de 940 megas directv y un extensor wifi"
    llamadas = await main._rutear_consulta_dueño(pregunta)
    nombres = [n for n, _ in llamadas]
    assert "consultar_catalogo" in nombres, f"esperaba consultar_catalogo, obtuvo {nombres}"

    resultados = []
    for nombre_funcion, parametros in llamadas:
        datos = await main._ejecutar_consulta_dueño(nombre_funcion, parametros)
        resultados.append((nombre_funcion, datos))
    respuesta = await main._formatear_respuesta_dueño(pregunta, resultados)

    assert "16.990" in respuesta, (
        f"no calculó el total correcto (debería ser $16.990 — el extensor es "
        f"gratis en plan 940). Respuesta:\n{respuesta}"
    )
    texto_low = respuesta.lower()
    assert "gratis" in texto_low or "$0" in respuesta or "sin costo" in texto_low, (
        f"no mencionó que el extensor es gratis en plan 940. Respuesta:\n{respuesta}"
    )


CASOS_MODELO = [
    ("2 decos adicionales DirecTV", caso_precio_2_decos_adicionales),
    ("comuna sola pide calle+número", caso_comuna_sola_pide_calle_numero),
    ("WOM sin precio inventado", caso_wom_sin_precio_inventado),
    ("router dueño con preguntas reales", caso_router_dueno_preguntas_reales),
    ("catálogo: extensor gratis en 940 DirecTV", caso_catalogo_extensor_gratis_940),
]


async def main():
    con_modelo = "--con-modelo" in sys.argv

    print("=== EVAL VALENTINA ===\n")

    for nombre, fn in CASOS_DETERMINISTAS:
        await _caso(nombre, "DETERMINISTA", fn)

    if con_modelo:
        print("(--con-modelo activo: los siguientes casos llaman a la API real y gastan crédito)\n")
        for nombre, fn in CASOS_MODELO:
            await _caso(nombre, "MODELO", fn)
    else:
        print("(saltando casos de MODELO — correr con --con-modelo para incluirlos)\n")

    for r in RESULTADOS:
        estado = "PASS" if r.ok else "FAIL"
        etiqueta = f"[{r.categoria}]"
        print(f"{etiqueta:15} {r.nombre:.<55} {estado}")
        if not r.ok:
            for linea in r.detalle.splitlines():
                print(f"    -> {linea}")

    total = len(RESULTADOS)
    pasaron = sum(1 for r in RESULTADOS if r.ok)
    print("-" * 70)
    if pasaron == total:
        print(f"{pasaron}/{total} pasaron")
    else:
        print(f"{pasaron}/{total} pasaron ({total - pasaron} fallaron — detalle arriba)")

    from agent.database import close_pool
    await close_pool()

    if pasaron < total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
