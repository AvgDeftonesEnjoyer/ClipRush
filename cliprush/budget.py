"""Budget cutoff based on owner-recorded USD expenses.

Provider invoices must be imported or entered manually; this is not a billing API.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.models import Expense, now


def month_spent_cents(db: Session) -> int:
    current = now()
    start = datetime(current.year, current.month, 1, tzinfo=timezone.utc)
    return sum(x.amount_cents for x in db.scalars(select(Expense).where(
        Expense.created_at >= start
    )))


def budget_remaining_cents(db: Session) -> int:
    limit = round(get_settings().budget_limit_usd * 100)
    return limit - month_spent_cents(db)


def within_budget(db: Session) -> bool:
    return budget_remaining_cents(db) > 0
