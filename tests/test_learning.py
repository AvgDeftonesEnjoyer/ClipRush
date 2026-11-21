"""Week-seven decision rules are proposals, never automatic account actions."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cliprush.db import Base
from cliprush.learning import CHECKPOINT, summarize


def test_checkpoint_zero_posts_recommends_stop():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        before = summarize(db, at=CHECKPOINT - timedelta(days=2))
        assert before["checkpoint_due"] is False
        after = summarize(db, at=CHECKPOINT)
        assert after["checkpoint_due"] is True
        assert "stop publishing" in after["recommendation"]
        assert after["can_auto_tune"] is False
    engine.dispose()
