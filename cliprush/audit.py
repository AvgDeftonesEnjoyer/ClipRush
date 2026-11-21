"""Append-only actor/action ledger. Callers supply non-sensitive metadata only."""
from sqlalchemy.orm import Session
from cliprush.models import AuditEvent


def audit(db: Session, actor: str, action: str, target_type: str,
          target_id: int | None = None, details: dict | None = None) -> None:
    fields = details or {}
    # Defense in depth: forbid obvious secret-bearing keys entirely.
    redacted = {}
    for key, value in fields.items():
        if any(word in str(key).lower() for word in ("token", "secret", "password", "key", "auth")):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            redacted[str(key)[:40]] = str(value)[:300] if isinstance(value, str) else value
    db.add(AuditEvent(actor=actor[:32], action=action[:80], target_type=target_type[:40],
                      target_id=target_id, details=redacted))
