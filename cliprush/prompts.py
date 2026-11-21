"""Owner-approved Detect prompt versions and reversible experiments."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cliprush.detect import PROMPT
from cliprush.models import PromptVersion


def list_versions(db: Session) -> list[PromptVersion]:
    return list(db.scalars(select(PromptVersion).where(
        PromptVersion.module == "detect"
    ).order_by(PromptVersion.version.desc())))


def propose(db: Session, prompt: str) -> PromptVersion:
    if not 120 <= len(prompt) <= 10000:
        raise ValueError("Detect prompt must be 120 to 10000 characters")
    if not all(term in prompt for term in ("score", "start", "end", "kind", "hook")):
        raise ValueError("Detect prompt must preserve structured fields: score/start/end/kind/hook")
    latest = db.scalar(select(func.max(PromptVersion.version)).where(PromptVersion.module == "detect")) or 0
    version = PromptVersion(module="detect", version=latest + 1, prompt=prompt,
                            approved=False, active=False, rejected=False)
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def active_prompt(db: Session) -> str:
    active = db.scalar(select(PromptVersion).where(
        PromptVersion.module == "detect", PromptVersion.approved.is_(True),
        PromptVersion.active.is_(True)
    ).order_by(PromptVersion.version.desc()))
    return active.prompt if active else PROMPT


def activate(db: Session, version_id: int) -> PromptVersion:
    candidate = db.get(PromptVersion, version_id)
    if not candidate or candidate.module != "detect" or candidate.rejected:
        raise ValueError("Unknown or rejected prompt proposal")
    for item in list_versions(db):
        item.active = item.id == version_id
    from cliprush.audit import audit
    candidate.approved = True
    candidate.active = True
    audit(db, "owner", "prompt.activated", "prompt_version", candidate.id,
          {"version": candidate.version})
    db.commit()
    db.refresh(candidate)
    return candidate


def reject(db: Session, version_id: int) -> PromptVersion:
    candidate = db.get(PromptVersion, version_id)
    if not candidate or candidate.module != "detect" or candidate.active:
        raise ValueError("Unknown or active prompt proposal")
    from cliprush.audit import audit
    candidate.rejected = True
    candidate.approved = False
    audit(db, "owner", "prompt.rejected", "prompt_version", candidate.id,
          {"version": candidate.version})
    db.commit()
    return candidate


def propose_weekly_experiment(db: Session, eligible_count: int, week_number: int) -> int | None:
    """Suggest at most one unapproved change; never alter the running prompt."""
    if eligible_count < 100:
        return None
    pending = db.scalar(select(PromptVersion).where(
        PromptVersion.module == "detect", PromptVersion.approved.is_(False),
        PromptVersion.active.is_(False), PromptVersion.rejected.is_(False)
    ).order_by(PromptVersion.version.desc()))
    if pending:
        return pending.id
    base = active_prompt(db).split("\n\n# CLIPRUSH EXPERIMENT")[0]
    variant = ("Вопрос: сформулируй 1-й хук как интригующий вопрос."
               if week_number % 2 else
               "Утверждение: сформулируй 1-й хук как конкретное сильное утверждение.")
    proposal = (
        base + "\n\n# CLIPRUSH EXPERIMENT - требует подтверждения владельца\n"
        "Без изменения критериев безопасности и формата JSON. "
        "Тестируй ровно одну гипотезу по формулировке первых 2 секунд. " + variant
    )
    return propose(db, proposal).id
