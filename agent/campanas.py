# agent/campanas.py — Campañas de WhatsApp segmentadas
# Conexion Sin Limites

"""
Gestión de campañas masivas con segmentación por tag/estado/score/comuna/fecha.
Envío con rate limiting (máx 10 msg/s) y registro por destinatario.
"""

import asyncio
import logging
from datetime import datetime

from agent.database import get_pool

logger = logging.getLogger("agentkit")


# ── Inicialización de tablas ──────────────────────────────────────────────────

async def inicializar_campanas():
    """Crea las tablas campanas y campana_destinatarios si no existen."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS campanas (
                id                  SERIAL PRIMARY KEY,
                nombre              TEXT NOT NULL,
                mensaje             TEXT NOT NULL,
                estado              TEXT DEFAULT 'borrador',
                filtro_tag          TEXT DEFAULT '',
                filtro_estado       TEXT DEFAULT '',
                filtro_score_min    INT  DEFAULT 0,
                filtro_comuna       TEXT DEFAULT '',
                filtro_desde        DATE,
                filtro_hasta        DATE,
                fecha_creacion      TIMESTAMP DEFAULT NOW(),
                fecha_envio         TIMESTAMP,
                total_destinatarios INT DEFAULT 0,
                total_enviados      INT DEFAULT 0,
                total_fallidos      INT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS campana_destinatarios (
                id           SERIAL PRIMARY KEY,
                campana_id   INT  NOT NULL REFERENCES campanas(id) ON DELETE CASCADE,
                telefono     TEXT NOT NULL,
                nombre       TEXT DEFAULT '',
                estado_envio TEXT DEFAULT 'pendiente',
                error        TEXT DEFAULT '',
                enviado_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_campana_dest_id
            ON campana_destinatarios(campana_id)
        """)
    logger.info("Tablas de campañas listas")


# ── Construcción de filtros ────────────────────────────────────────────────────

def _filtro_sql(filtros: dict) -> tuple[str, list]:
    """
    Construye WHERE clause para leads con los filtros de campaña.
    Retorna (where_sql, params_list).
    """
    conds  = []
    params = []
    n = [1]  # mutable counter

    def p(val):
        params.append(val)
        idx = n[0]; n[0] += 1
        return f"${idx}"

    tag = (filtros.get("tag") or "").strip()
    if tag:
        conds.append(f"tags::text ILIKE {p('%' + tag + '%')}")

    estado = (filtros.get("estado") or "").strip()
    if estado:
        conds.append(f"estado = {p(estado)}")

    score_min = int(filtros.get("score_min") or 0)
    if score_min > 0:
        conds.append(f"COALESCE(score,0) >= {p(score_min)}")

    comuna = (filtros.get("comuna") or "").strip()
    if comuna:
        conds.append(f"LOWER(COALESCE(comuna,'')) LIKE LOWER({p('%' + comuna + '%')})")

    desde = (filtros.get("desde") or "").strip()
    if desde:
        conds.append(f"created_at::date >= {p(desde)}")

    hasta = (filtros.get("hasta") or "").strip()
    if hasta:
        conds.append(f"created_at::date <= {p(hasta)}")

    # Nunca interrumpir conversaciones en modo humano
    conds.append("estado != 'modo_humano'")
    # Solo leads con teléfono válido
    conds.append("telefono IS NOT NULL AND telefono != ''")

    where = "WHERE " + " AND ".join(conds)
    return where, params


# ── Operaciones CRUD ──────────────────────────────────────────────────────────

async def preview_destinatarios(filtros: dict) -> dict:
    """Cuenta leads que recibirán la campaña y devuelve muestra de 5."""
    pool = await get_pool()
    where, params = _filtro_sql(filtros)
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM leads {where}", *params
        ) or 0
        rows = await conn.fetch(
            f"SELECT nombre, telefono, estado, score FROM leads {where} "
            f"ORDER BY ultima_interaccion DESC NULLS LAST LIMIT 5",
            *params
        )
    muestra = [
        {"nombre": r["nombre"] or "Desconocido", "telefono": r["telefono"],
         "estado": r["estado"] or "nuevo", "score": r["score"] or 0}
        for r in rows
    ]
    return {"total": int(total), "muestra": muestra}


async def crear_campana(nombre: str, mensaje: str, filtros: dict) -> int:
    """
    Crea la campaña y pre-carga la lista de destinatarios.
    Retorna el ID de la campaña creada.
    """
    pool = await get_pool()
    where, params = _filtro_sql(filtros)
    async with pool.acquire() as conn:
        leads = await conn.fetch(
            f"SELECT telefono, nombre FROM leads {where} "
            f"ORDER BY ultima_interaccion DESC NULLS LAST",
            *params
        )
        total = len(leads)

        campana_id = await conn.fetchval("""
            INSERT INTO campanas (
                nombre, mensaje, estado,
                filtro_tag, filtro_estado, filtro_score_min,
                filtro_comuna, filtro_desde, filtro_hasta,
                total_destinatarios
            ) VALUES ($1,$2,'borrador',$3,$4,$5,$6,$7,$8,$9)
            RETURNING id
        """,
            nombre, mensaje,
            filtros.get("tag", ""),
            filtros.get("estado", ""),
            int(filtros.get("score_min") or 0),
            filtros.get("comuna", ""),
            filtros.get("desde") or None,
            filtros.get("hasta") or None,
            total,
        )

        limite = int(filtros.get("limite") or 0)
        leads_finales = list(leads)[:limite] if limite > 0 else list(leads)
        if leads_finales:
            await conn.executemany(
                "INSERT INTO campana_destinatarios (campana_id, telefono, nombre) VALUES ($1,$2,$3)",
                [(campana_id, r["telefono"], r["nombre"] or "") for r in leads_finales],
            )

    logger.info(f"Campaña #{campana_id} '{nombre}' creada — {total} destinatarios")
    return campana_id


async def listar_campanas() -> list[dict]:
    """Lista todas las campañas ordenadas por fecha de creación descendente."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, nombre, estado,
                   fecha_creacion, fecha_envio,
                   total_destinatarios, total_enviados, total_fallidos,
                   filtro_tag, filtro_estado, filtro_score_min, filtro_comuna
            FROM campanas
            ORDER BY fecha_creacion DESC
            LIMIT 100
        """)
    result = []
    for r in rows:
        d = dict(r)
        d["fecha_creacion"] = str(d["fecha_creacion"]) if d["fecha_creacion"] else ""
        d["fecha_envio"]    = str(d["fecha_envio"])    if d["fecha_envio"]    else ""
        result.append(d)
    return result


async def obtener_campana(campana_id: int) -> dict | None:
    """Retorna todos los campos de una campaña."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM campanas WHERE id = $1", campana_id)
        if not row:
            return None
        d = dict(row)
        d["fecha_creacion"] = str(d["fecha_creacion"]) if d["fecha_creacion"] else ""
        d["fecha_envio"]    = str(d["fecha_envio"])    if d["fecha_envio"]    else ""
        d["filtro_desde"]   = str(d["filtro_desde"])   if d["filtro_desde"]   else ""
        d["filtro_hasta"]   = str(d["filtro_hasta"])   if d["filtro_hasta"]   else ""
        return d


async def obtener_destinatarios(campana_id: int) -> list[dict]:
    """Destinatarios de una campaña con su estado de envío individual."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT telefono, nombre, estado_envio, error, enviado_at
            FROM campana_destinatarios
            WHERE campana_id = $1
            ORDER BY
                CASE estado_envio
                    WHEN 'fallido'  THEN 0
                    WHEN 'pendiente'THEN 1
                    WHEN 'enviado'  THEN 2
                END, nombre
        """, campana_id)
    result = []
    for r in rows:
        d = dict(r)
        d["enviado_at"] = str(d["enviado_at"]) if d["enviado_at"] else ""
        result.append(d)
    return result


async def cancelar_campana(campana_id: int):
    """Cancela una campaña en estado borrador."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE campanas SET estado='cancelada' WHERE id=$1 AND estado='borrador'",
            campana_id
        )


# ── Envío en background ────────────────────────────────────────────────────────

async def enviar_campana_bg(campana_id: int, proveedor):
    """
    Envía la campaña en segundo plano con rate limiting (máx 10 msg/s).
    Registra el resultado por destinatario y actualiza los totales al terminar.
    Soporta personalización con {{nombre}} en el mensaje.
    """
    logger.info(f"[Campaña #{campana_id}] Iniciando envío en background...")
    pool = await get_pool()

    try:
        # Marcar como enviando + leer datos
        async with pool.acquire() as conn:
            campana = await conn.fetchrow(
                "SELECT * FROM campanas WHERE id = $1", campana_id
            )
            if not campana:
                logger.error(f"[Campaña #{campana_id}] No encontrada")
                return
            if campana["estado"] == "enviando":
                logger.warning(f"[Campaña #{campana_id}] Ya está enviando")
                return

            await conn.execute(
                "UPDATE campanas SET estado='enviando', fecha_envio=NOW() WHERE id=$1",
                campana_id
            )
            destinatarios = await conn.fetch(
                """SELECT id, telefono, nombre FROM campana_destinatarios
                   WHERE campana_id=$1 AND estado_envio='pendiente'""",
                campana_id
            )

        mensaje_base = campana["mensaje"]
        enviados = fallidos = 0
        total = len(destinatarios)
        logger.info(f"[Campaña #{campana_id}] {total} destinatarios pendientes")

        for dest in destinatarios:
            dest_id = dest["id"]
            tel     = dest["telefono"]
            nombre  = (dest["nombre"] or "").strip() or "Cliente"

            # Personalizar mensaje
            mensaje = mensaje_base.replace("{{nombre}}", nombre)

            try:
                ok = await proveedor.enviar_mensaje(tel, mensaje)
                if ok:
                    enviados += 1
                    async with pool.acquire() as conn2:
                        await conn2.execute(
                            "UPDATE campana_destinatarios SET estado_envio='enviado', enviado_at=NOW() WHERE id=$1",
                            dest_id
                        )
                else:
                    fallidos += 1
                    async with pool.acquire() as conn2:
                        await conn2.execute(
                            "UPDATE campana_destinatarios SET estado_envio='fallido', error='El proveedor no confirmó el envío' WHERE id=$1",
                            dest_id
                        )
            except Exception as e:
                fallidos += 1
                logger.warning(f"[Campaña #{campana_id}] Error {tel}: {e}")
                async with pool.acquire() as conn2:
                    await conn2.execute(
                        "UPDATE campana_destinatarios SET estado_envio='fallido', error=$1 WHERE id=$2",
                        str(e)[:200], dest_id
                    )

            # Rate limiting: máx 10 mensajes/segundo
            await asyncio.sleep(0.1)

        # Finalizar campaña
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE campanas
                SET estado='completada', total_enviados=$1, total_fallidos=$2
                WHERE id=$3
            """, enviados, fallidos, campana_id)

        logger.info(
            f"[Campaña #{campana_id}] Completada: {enviados}/{total} enviados, {fallidos} fallidos"
        )

    except Exception as e:
        logger.error(f"[Campaña #{campana_id}] Error fatal en envío: {e}", exc_info=True)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE campanas SET estado='error' WHERE id=$1", campana_id
                )
        except Exception:
            pass


async def pausar_campana(campana_id: int) -> dict:
    """Pausa una campaña en envío."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT estado FROM campanas WHERE id=$1", campana_id)
        if not row:
            return {"ok": False, "error": "Campaña no encontrada"}
        if row["estado"] != "enviando":
            return {"ok": False, "error": f"Solo se puede pausar si está enviando (estado actual: {row['estado']})"}
        await conn.execute("UPDATE campanas SET estado='pausada' WHERE id=$1", campana_id)
    return {"ok": True, "estado": "pausada"}

async def reanudar_campana(campana_id: int) -> dict:
    """Reanuda una campaña pausada."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, nombre, mensaje, filtros FROM campanas WHERE id=$1 AND estado='pausada'",
            campana_id
        )
        if not row:
            return {"ok": False, "error": "Campaña no encontrada o no está pausada"}
        await conn.execute("UPDATE campanas SET estado='enviando' WHERE id=$1", campana_id)
    # Relanzar envío desde donde quedó
    import asyncio
    asyncio.create_task(ejecutar_envio_campana(campana_id))
    return {"ok": True, "estado": "enviando"}

async def progreso_campana(campana_id: int) -> dict:
    """Retorna progreso actual de una campaña."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT c.estado, c.total_enviados, c.total_fallidos,
                   COUNT(d.id) as total
            FROM campanas c
            LEFT JOIN campana_destinatarios d ON d.campana_id = c.id
            WHERE c.id = $1
            GROUP BY c.estado, c.total_enviados, c.total_fallidos
        """, campana_id)
        if not row:
            return {"ok": False}
        enviados = row["total_enviados"] or 0
        fallidos = row["total_fallidos"] or 0
        total = row["total"] or 0
        return {
            "ok": True,
            "estado": row["estado"],
            "enviados": enviados,
            "fallidos": fallidos,
            "total": total,
            "porcentaje": round((enviados + fallidos) / total * 100) if total > 0 else 0
        }
