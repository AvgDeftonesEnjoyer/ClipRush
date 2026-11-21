"""Deduplicated Telegram operations alerts for jobs requiring human attention."""
import logging

from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.models import Account, Clip, Post, Recording, RuntimeControl
from cliprush.telegram import send_message

log = logging.getLogger(__name__)


def operational_alerts(db: Session, cache: Redis | None = None) -> list[str]:
    cfg = get_settings()
    if not (cfg.telegram_owner_id and cfg.telegram_bot_token):
        return []
    flagged = [
        ("recording_unknown", db.scalar(select(func.count(Recording.id)).where(
            Recording.status == "unknown")) or 0),
        ("post_unknown", db.scalar(select(func.count(Post.id)).where(
            Post.status == "unknown")) or 0),
        ("clip_attention", db.scalar(select(func.count(Clip.id)).where(
            Clip.status.in_(("style_failed", "scan_failed")))) or 0),
        ("accounts_paused", db.scalar(select(func.count(Account.id)).where(
            Account.paused.is_(True))) or 0),
    ]
    notices = [f"{name}: {count}" for name, count in flagged if count]
    if not notices:
        return []
    own = cache is None
    cache = cache or Redis.from_url(cfg.redis_url, socket_connect_timeout=2)
    try:
        key = "cliprush:ops-alert:" + ",".join(notices)
        if cache.set(key, "1", ex=3600, nx=True):
            send_message("ClipRush: attention required\n" + "\n".join(notices) +
                         "\nGET /ops/stuck and /ops/stats for details. No uploads retried.")
            return notices
    except Exception:
        log.exception("Operations alert delivery failed")
    finally:
        if own:
            cache.close()
    return []
