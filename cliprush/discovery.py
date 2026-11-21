"""Collect leads from Telegram channels where the bot was explicitly added.

No unauthorized channel scraping, no auto-activation or invented payout rates.
"""
import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.models import ProgramCandidate

TERMS = re.compile(r"(?i)(?:конкурс\s+нарезок|плачу\s+за\s+нарезки|клипперы|content\s+rewards|clips\s+contest)")


def ingest_telegram_post(db: Session, post: dict) -> str | None:
    cfg = get_settings()
    allowed = {item.strip() for item in cfg.discovery_chat_ids.split(",") if item.strip()}
    chat = post.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not allowed or chat_id not in allowed:
        return None
    body = str(post.get("text") or post.get("caption") or "")
    if not TERMS.search(body):
        return None
    message_id = post.get("message_id")
    if not isinstance(message_id, int):
        return None
    fingerprint = hashlib.sha256(f"telegram:{chat_id}:{message_id}".encode()).hexdigest()
    existing = db.scalar(select(ProgramCandidate).where(ProgramCandidate.fingerprint == fingerprint))
    if existing:
        return None
    username = str(chat.get("username") or "")
    url = f"https://t.me/{username}/{message_id}" if username else ""
    db.add(ProgramCandidate(source="telegram", source_url=url,
                            text=body[:1800], fingerprint=fingerprint))
    db.commit()
    return f"Найден новый кандидат программы (проверить вручную): {url or body[:160]}"
