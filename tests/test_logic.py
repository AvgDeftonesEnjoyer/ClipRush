import hashlib
import hmac

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from cliprush.db import Base
from cliprush.logic import apply_completed_job, valid_webhook, validate_source_url
from cliprush.models import Clip, Program, Recording


def test_url_validation():
    assert validate_source_url("https://www.youtube.com/watch?v=abc")
    for url in ("http://youtube.com/a", "https://evil.com/a", "https://youtube.com.evil.com/a", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            validate_source_url(url)


def test_webhook_signature():
    body = b'{"event":"job.completed"}'
    secret = "test-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert valid_webhook(body, signature, secret)
    assert not valid_webhook(body + b"!", signature, secret)
    assert not valid_webhook(body, signature, "")


def test_completed_job_is_idempotent():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Program(name="demo", enabled=True, rights_confirmed=True)
        db.add(p)
        db.flush()
        r = Recording(program_id=p.id, external_id="one", source_url="https://youtu.be/abc", openshorts_job_id="job")
        db.add(r)
        db.commit()
        payload = {"status": "completed", "result": {"clips": [{"title": "Moment", "video_url": "/clip.mp4"}]}}
        assert apply_completed_job(db, r, payload) == 1
        assert apply_completed_job(db, r, payload) == 0
        clips = db.scalars(select(Clip)).all()
        assert len(clips) == 1
        assert clips[0].status == "needs_review"
