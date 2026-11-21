"""Authorized Kick webhooks with RSA verification and Redis chat delivery.

Owner registers a Kick developer app, configures this webhook URL and explicitly
subscribes only to approved channel chat/status events. No unofficial chat API.
"""
import base64
from datetime import datetime, timezone
from functools import lru_cache

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.models import Program, Streamer


@lru_cache(maxsize=1)
def public_key():
    """Official key fetched dynamically; deployment can refresh on rotation."""
    response = httpx.get("https://api.kick.com/public/v1/public-key", timeout=15)
    response.raise_for_status()
    data = response.json()
    pem = data.get("data") or data.get("public_key")
    if isinstance(pem, dict):
        pem = pem.get("public_key") or pem.get("key")
    if not isinstance(pem, str) or "BEGIN PUBLIC KEY" not in pem:
        raise ValueError("Unrecognized Kick public key response")
    return serialization.load_pem_public_key(pem.encode())


def verify_kick_signature(body: bytes, headers) -> None:
    message_id = headers.get("kick-event-message-id", "")
    timestamp = headers.get("kick-event-message-timestamp", "")
    signature = headers.get("kick-event-signature", "")
    if not message_id or not timestamp or not signature or len(message_id) > 80:
        raise ValueError("Missing webhook signature headers")
    try:
        event_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if event_time.tzinfo is None or abs(
            (datetime.now(timezone.utc) - event_time).total_seconds()
        ) > 600:
            raise ValueError("Stale Kick webhook")
        signature_bytes = base64.b64decode(signature, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid timestamp or signature encoding") from error
    message = message_id.encode() + b"." + timestamp.encode() + b"." + body
    for attempt in (1, 2):
        try:
            public_key().verify(signature_bytes, message, padding.PKCS1v15(), hashes.SHA256())
            return
        except InvalidSignature as error:
            if attempt == 2:
                raise ValueError("Kick webhook signature mismatch") from error
            public_key.cache_clear()


def handle_kick_event(db: Session, event: dict, headers, redis_client: Redis | None = None) -> dict:
    """Verify event references a creator we approved, then relay transient signals."""
    name = str(headers.get("kick-event-type", ""))
    msg_id = str(headers.get("kick-event-message-id", ""))
    if name not in ("chat.message.sent", "livestream.status.updated"):
        return {"ignored": True}
    broadcaster = event.get("broadcaster") or {}
    slug = str(broadcaster.get("channel_slug") or "").lower()
    if not slug or len(slug) > 80:
        return {"ignored": True}
    streamer = db.scalar(select(Streamer).join(Program, Streamer.program_id == Program.id).where(
        Streamer.platform == "kick", Streamer.channel == slug,
        Streamer.enabled.is_(True), Program.enabled.is_(True),
        Program.rights_confirmed.is_(True),
    ))
    if streamer is None:
        return {"ignored": True}
    redis_client = redis_client or Redis.from_url(get_settings().redis_url, decode_responses=True)
    # Kick retries signed deliveries. Do not double count a chat message.
    replay_key = "kick:event:" + msg_id
    if not redis_client.set(replay_key, "1", ex=24 * 3600, nx=True):
        return {"duplicate": True}
    key = "kick:live:" + slug
    if name == "livestream.status.updated":
        if event.get("is_live") is True:
            redis_client.set(key, "1", ex=24 * 3600)
        elif event.get("is_live") is False:
            redis_client.delete(key)
        return {"accepted": True}
    text = str(event.get("content") or "")
    if text:
        topic = "kick:chat:" + slug
        redis_client.xadd(topic, {"content": text[:600]}, maxlen=1000, approximate=True)
        redis_client.expire(topic, 3600)
    return {"accepted": True}
