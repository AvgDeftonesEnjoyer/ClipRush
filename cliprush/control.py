"""Emergency kill switch persisted in PostgreSQL.

ENABLE_PUBLISH=false is absolute. A hold can additionally disable a live
deployment until the owner deliberately unpauses it.
"""
from sqlalchemy.orm import Session
from cliprush.models import RuntimeControl

PAUSE_KEY = "publishing"


def publishing_paused(db: Session) -> bool:
    setting = db.get(RuntimeControl, PAUSE_KEY)
    return bool(setting and setting.paused)


def set_publishing_pause(db: Session, paused: bool, reason: str = "") -> RuntimeControl:
    setting = db.get(RuntimeControl, PAUSE_KEY)
    if setting is None:
        setting = RuntimeControl(key=PAUSE_KEY)
        db.add(setting)
    setting.paused = paused
    setting.reason = (reason or "")[:500]
    db.commit()
    db.refresh(setting)
    return setting
