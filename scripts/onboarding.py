#!/usr/bin/env python3
"""
scripts/onboarding.py — Alta de nuevo cliente en la plataforma multi-tenant
Conexión Sin Límites / AgentKit

Uso:
    python scripts/onboarding.py \
        --nombre "Mi Empresa" \
        --slug "mi-empresa" \
        --phone-id "1234567890" \
        --token "EAABwzLixnjYBO..." \
        --dashboard-user "admin_miempresa" \
        --dashboard-password "contraseña_segura"

El script:
  1. Conecta a la base de datos PostgreSQL (usa DATABASE_URL del .env)
  2. Hashea la contraseña con SHA-256
  3. Inserta el registro en la tabla `clientes`
  4. Imprime el id asignado y confirma que el cliente está listo
"""

import asyncio
import argparse
import hashlib
import sys
import os

# Permite ejecutar desde la raíz del proyecto sin instalar el paquete
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from agent.database import get_pool, close_pool


def hashear_password(password: str) -> str:
    """SHA-256 del password. Compatible con el mecanismo de auth del dashboard."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


async def crear_cliente(
    nombre: str,
    slug: str,
    phone_id: str,
    token: str,
    dashboard_user: str,
    dashboard_password: str,
) -> int:
    """
    Inserta un nuevo cliente en la tabla `clientes`.
    Retorna el id asignado.
    Lanza ValueError si el slug ya existe.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verificar slug único
        existente = await conn.fetchval(
            "SELECT id FROM clientes WHERE slug = $1", slug
        )
        if existente:
            raise ValueError(
                f"El slug '{slug}' ya está en uso por el cliente con id={existente}."
            )

        password_hash = hashear_password(dashboard_password)

        nuevo_id = await conn.fetchval("""
            INSERT INTO clientes
                (nombre, slug, whatsapp_phone_id, whatsapp_token,
                 dashboard_user, dashboard_password_hash, activo)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
            RETURNING id
        """,
            nombre,
            slug,
            phone_id or None,
            token or None,
            dashboard_user or None,
            password_hash,
        )

    return nuevo_id


def parsear_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dar de alta un nuevo cliente en AgentKit (multi-tenant)"
    )
    parser.add_argument("--nombre",             required=True,  help="Nombre del cliente")
    parser.add_argument("--slug",               required=True,  help="Slug único (ej: mi-empresa)")
    parser.add_argument("--phone-id",           required=False, default="", help="Meta Phone Number ID")
    parser.add_argument("--token",              required=False, default="", help="Token de WhatsApp (Meta/Whapi)")
    parser.add_argument("--dashboard-user",     required=False, default="admin", help="Usuario del dashboard")
    parser.add_argument("--dashboard-password", required=True,  help="Contraseña del dashboard")
    return parser.parse_args()


async def main():
    args = parsear_args()

    print(f"\nAgentKit — Alta de nuevo cliente")
    print("=" * 45)
    print(f"  Nombre:         {args.nombre}")
    print(f"  Slug:           {args.slug}")
    print(f"  Phone ID:       {args.phone_id or '(no configurado)'}")
    print(f"  Dashboard user: {args.dashboard_user}")
    print("=" * 45)

    try:
        cliente_id = await crear_cliente(
            nombre=args.nombre,
            slug=args.slug,
            phone_id=args.phone_id,
            token=args.token,
            dashboard_user=args.dashboard_user,
            dashboard_password=args.dashboard_password,
        )
        print(f"\nCliente creado exitosamente.")
        print(f"  ID asignado: {cliente_id}")
        print(f"\nEl cliente '{args.nombre}' (slug: {args.slug}) está listo para usar.")
        print(f"Todos los leads nuevos de este cliente se crearán con cliente_id={cliente_id}.\n")
    except ValueError as e:
        print(f"\nError: {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\nError de base de datos: {e}\n")
        sys.exit(1)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
