"""Prompt experiments must be opt-in and support reverting previous versions."""
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.db import Base
from cliprush.detect import PROMPT
from cliprush.prompts import active_prompt, activate, list_versions, propose_weekly_experiment, reject
from cliprush.telegram import handle_update


def test_owner_approved_prompts_and_rollback(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert active_prompt(db) == PROMPT
        assert propose_weekly_experiment(db, 99, 1) is None
        id1 = propose_weekly_experiment(db, 100, 1)
        assert id1 is not None
        assert propose_weekly_experiment(db, 100, 2) == id1  # no second pending
        assert active_prompt(db) == PROMPT  # mere proposal changes nothing
        v1 = activate(db, id1)
        assert v1.active and v1.approved
        assert "CLIPRUSH EXPERIMENT" in active_prompt(db)
        id2 = propose_weekly_experiment(db, 100, 2)
        assert id2 != id1
        reject(db, id2)
        assert active_prompt(db) == v1.prompt
        assert len(list_versions(db)) == 2
        # Previous version can be reapplied after a second active one.
        v3_id = propose_weekly_experiment(db, 100, 3)
        activate(db, v3_id)
        assert list_versions(db)[0].active
        activate(db, id1)
        assert active_prompt(db) == v1.prompt
        assert not list_versions(db)[0].active
    engine.dispose()


def test_telegram_owner_can_approve_proposal(monkeypatch):
    import cliprush.telegram as tg
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cfg = get_settings()
    monkeypatch.setattr(cfg, "telegram_owner_id", 987654)
    monkeypatch.setattr(tg, "tg_call", lambda *a, **kwargs: {"ok": True})
    with Session(engine) as db:
        pid = propose_weekly_experiment(db, 100, 12)
        other = handle_update(db, {"callback_query": {
            "from": {"id": 1}, "data": f"pa:{pid}", "id": "x"
        }})
        assert other["ignored"]
        accepted = handle_update(db, {"callback_query": {
            "from": {"id": 987654}, "data": f"pa:{pid}", "id": "y"
        }})
        assert "activated" in accepted["result"].lower()
        assert "CLIPRUSH EXPERIMENT" in active_prompt(db)
    engine.dispose()
