# agent/providers/meta.py — Adaptador para Meta WhatsApp Cloud API
# Generado por AgentKit

import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante
from agent.config import META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, META_VERIFY_TOKEN

logger = logging.getLogger("agentkit")


class ProveedorMeta(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando la API oficial de Meta (Cloud API)."""

    def __init__(self):
        # Los tokens ya vienen limpios desde config.py (sin espacios ni saltos de línea)
        self.access_token = META_ACCESS_TOKEN
        self.phone_number_id = META_PHONE_NUMBER_ID
        self.verify_token = META_VERIFY_TOKEN or "agentkit-verify"
        self.api_version = "v21.0"

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """Meta requiere verificación GET con hub.verify_token."""
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if mode == "subscribe" and token == self.verify_token:
            return int(challenge)
        return None

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload anidado de Meta Cloud API. Maneja texto y audio."""
        body = await request.json()
        mensajes = []
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    tipo = msg.get("type")
                    telefono = msg.get("from", "")
                    mensaje_id = msg.get("id", "")

                    if tipo == "text":
                        mensajes.append(MensajeEntrante(
                            telefono=telefono,
                            texto=msg.get("text", {}).get("body", ""),
                            mensaje_id=mensaje_id,
                            es_propio=False,
                        ))
                    elif tipo == "audio":
                        # Guardar el media_id para transcribir en main.py
                        audio_id = msg.get("audio", {}).get("id", "")
                        if audio_id:
                            mensajes.append(MensajeEntrante(
                                telefono=telefono,
                                texto="",
                                mensaje_id=mensaje_id,
                                es_propio=False,
                                audio_id=audio_id,
                            ))
        return mensajes

    async def descargar_audio(self, media_id: str) -> tuple[bytes, str]:
        """
        Descarga un archivo de audio desde Meta API.
        Retorna (bytes_del_audio, mime_type).
        """
        headers = {"Authorization": f"Bearer {self.access_token}"}

        async with httpx.AsyncClient() as client:
            # Paso 1: obtener la URL de descarga
            r = await client.get(
                f"https://graph.facebook.com/{self.api_version}/{media_id}",
                headers=headers,
            )
            if r.status_code != 200:
                raise RuntimeError(f"Error obteniendo URL de media: {r.status_code} {r.text}")
            data = r.json()
            url_descarga = data.get("url")
            mime_type = data.get("mime_type", "audio/ogg")

            # Paso 2: descargar el archivo binario
            r2 = await client.get(url_descarga, headers=headers)
            if r2.status_code != 200:
                raise RuntimeError(f"Error descargando audio: {r2.status_code}")

        return r2.content, mime_type

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía mensaje via Meta WhatsApp Cloud API."""
        if not self.access_token or not self.phone_number_id:
            logger.warning("META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no configurados")
            return False
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "text",
            "text": {"body": mensaje},
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                raise RuntimeError(f"Meta API {r.status_code}: {r.text}")
            return True
