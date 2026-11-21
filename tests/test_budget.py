from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cliprush.budget import budget_remaining_cents, month_spent_cents, within_budget
from cliprush.config import get_settings
from cliprush.db import Base
from cliprush.models import Expense


def test_manual_budget_blocks_recorded_spend(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = get_settings()
    monkeypatch.setattr(settings, "budget_limit_usd", 80)
    with Session(engine) as db:
        assert within_budget(db)
        db.add(Expense(vendor="Compute", amount_cents=5000))
        db.commit()
        assert month_spent_cents(db) == 5000
        assert budget_remaining_cents(db) == 3000
        db.add(Expense(vendor="LLM", amount_cents=3200))
        db.commit()
        assert budget_remaining_cents(db) == -200
        assert not within_budget(db)
    engine.dispose()
