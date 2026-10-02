"""Materialize event-bound media only when Application requests it after admission."""

from urllib.parse import urlencode

from chatcopilot.contracts.resources import FetchedResource
from chatcopilot.contracts.gateway import ResourceTicket
from .crypto import decrypt
from chatcopilot.contracts.weixin import WeixinMediaClientPort
from chatcopilot.core.image_content import validate_image_bytes
from chatcopilot.contracts.weixin import CDN_BASE, WeixinError, media_key


class WeixinResourceFetcher:
    def __init__(self, client: WeixinMediaClientPort, *, account_id: str):
        self.client, self.account_id = client, account_id

    async def fetch(self, ticket: ResourceTicket, *, max_bytes: int) -> FetchedResource:
        if (
            ticket.account.channel != "weixin"
            or ticket.account.account_id != self.account_id
            or ticket.kind not in ("image", "file")
        ):
            raise WeixinError("weixin_resource_binding_invalid")
        media = ticket.provider_ref.get("media")
        if not isinstance(media, dict):
            raise WeixinError("weixin_media_reference_invalid")
        key = (
            media_key(ticket.provider_ref["aeskey"], hexadecimal=True)
            if ticket.provider_ref.get("aeskey")
            else media_key(media.get("aes_key", ""))
        )
        url = media.get("full_url")
        if not url:
            parameter = media.get("encrypt_query_param")
            if not isinstance(parameter, str) or not parameter:
                raise WeixinError("weixin_media_reference_invalid")
            url = CDN_BASE + "/download?" + urlencode({"encrypted_query_param": parameter})
        encrypted = await self.client.download(url, max_bytes=max_bytes + 16)
        data = decrypt(encrypted, key)
        if len(data) > max_bytes:
            raise WeixinError("weixin_resource_too_large")
        if ticket.kind == "image":
            image = validate_image_bytes(data, max_bytes=max_bytes)
            return FetchedResource(
                data=data, name=ticket.ticket_id + image.extension, media_type=image.media_type
            )
        return FetchedResource(data=data, name=ticket.name, media_type=ticket.media_type)
