"""Unit/integration tests requiring no real stream, GPU, secret, or platform account."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cliprush.config import get_settings
from cliprush.db import Base, SessionLocal
from cliprush.detect import score_candidate
from cliprush.live import SpikeDetector
from cliprush.logic import apply_completed_job
from cliprush.models import Account, Clip, Metric, Payout, Post, Program, Recording
from cliprush.openshorts import OpenShorts
from cliprush.publishing import quota_error, reconcile
from cliprush.safety import text_flags
from cliprush.tracking import metric_fields, payout_tier, record_metric


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def seed(db):
    program = Program(name="program1", rules_url="https://example.com/rules",
                      rights_confirmed=True, enabled=True,
                      reward_cents=2500, min_views=100000)
    db.add(program)
    db.flush()
    recording = Recording(program_id=program.id, external_id="1",
                          source_url="https://www.youtube.com/watch?v=abc", status="completed",
                          openshorts_job_id="job-a")
    db.add(recording)
    db.flush()
    clip = Clip(recording_id=recording.id, clip_index=0, title="first clip",
                video_url="/output/clip.mp4", status="approved",
                safety_status="review", safety_checked=True)
    db.add(clip)
    account = Account(platform="youtube", profile="my-profile", enabled=True,
                      created_at=datetime.now(timezone.utc) - timedelta(days=10))
    db.add(account)
    db.commit()
    return program, recording, clip, account


def test_irc_spike_emotes_and_cooldown():
    detector = SpikeDetector()
    flags = [detector.record("KEKW", now_seconds=100 + i) for i in range(6)]
    assert flags[-1] is True
    assert detector.record("KEKW", now_seconds=107) is False


def test_text_guard():
    assert text_flags("Казино с промокодом!") != []
    assert text_flags("contact: help@example.com") != []
    assert text_flags("кот смеётся над игрой") == []


def test_detect_rejects_invalid_scoring(monkeypatch):
    monkeypatch.setattr("cliprush.detect.ffprobe_seconds", lambda path: 90.0)
    monkeypatch.setattr("cliprush.detect.transcribe", lambda path: (
        "a b c d", [{"start": 0, "end": 1, "text": w} for w in ("a", "b", "c", "d")]))
    monkeypatch.setattr("cliprush.detect.llm_json", lambda *a: {
        "score": 8, "start": 10, "end": 36, "kind": "reaction", "hook": "Что случилось?"})
    got = score_candidate("dummy.mp4")
    assert got.score == 8
    assert got.end - got.start == 26
    monkeypatch.setattr("cliprush.detect.llm_json", lambda *a: {
        "score": 5.9, "start": 10, "end": 36, "kind": "reaction", "hook": "no"})
    assert score_candidate("dummy.mp4") is None


def test_upload_account_quota_and_pause(db):
    _, _, clip, account = seed(db)
    assert quota_error(db, account) is None
    post = Post(clip_id=clip.id, account_id=account.id,
                idempotency_key="single", status="published")
    db.add(post)
    db.commit()
    assert "gap" in quota_error(db, account).lower()
    assert quota_error(db, account, urgent=True) is None
    account.paused = True
    db.commit()
    assert "paused" in quota_error(db, account)


def test_provider_reconciliation_and_metrics(db):
    program, _, clip, account = seed(db)
    post = Post(clip_id=clip.id, account_id=account.id,
                idempotency_key="key", status="submitted")
    db.add(post)
    db.commit()
    assert reconcile(db, post, {
        "status": "completed",
        "results": [{"platform": "youtube", "success": True,
                     "url": "https://youtube.com/shorts/abc", "post_id": "abc"}]
    }) == "published"
    assert post.platform_post_id == "abc"
    point = record_metric(db, post, {"platforms": {"youtube": {
        "success": True, "post_metrics": {"views": 130000, "likes": 130}
    }}})
    assert point.views == 130000
    payout = db.scalar(select(Payout).where(Payout.post_id == post.id))
    assert payout.status == "eligible"
    assert payout.amount_cents == 2500
    assert payout_tier(program, 50000) == 0
    assert metric_fields({"platforms": {"youtube": {"success": True, "post_metrics": {
        "views": 123}}}}, "youtube")["views"] == 123


def test_payout_tiers(db):
    p = Program(name="tiers", rules={"tiers": [
        {"views": 100000, "reward_cents": 1500},
        {"views": 500000, "reward_cents": 5000}]})
    assert payout_tier(p, 99999) == 0
    assert payout_tier(p, 600000) == 5000


def test_openshorts_adapter(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "openshorts_webhook_secret", "")
    def handler(request):
        if request.url.path == "/api/process":
            data = json.loads(request.content)
            assert data["acknowledged"] is True
            return httpx.Response(200, json={"job_id": "j1"})
        if request.url.path == "/api/status/j1":
            return httpx.Response(200, json={"status": "completed", "result": {"clips": []}})
        return httpx.Response(404)
    with httpx.Client(base_url="http://upstream", transport=httpx.MockTransport(handler)) as client:
        adapter = OpenShorts(client)
        assert adapter.process("https://www.youtube.com/watch?v=123") == "j1"
        assert adapter.status("j1")["status"] == "completed"


def test_clip_ingest_idempotent(db):
    _, recording, _, _ = seed(db)
    result = {"status": "completed", "result": {"clips": [
        {"title": "a"}, {"title": "b"}]}}
    assert apply_completed_job(db, recording, result) == 1
    assert apply_completed_job(db, recording, result) == 0


def test_api_owner_flow(monkeypatch):
    from cliprush import api
    import cliprush.db as db_module
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    original_engine = db_module.engine
    db_module.engine = engine
    SessionLocal.configure(bind=engine)
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_token", "unit-test-admin-token-strong")
    monkeypatch.setattr(settings, "enable_publish", False)
    monkeypatch.setattr(api.submit_recording, "delay", lambda _: None)
    monkeypatch.setattr(api.scan_clip_task, "delay", lambda _: None)
    monkeypatch.setattr(api.prepare_clips_task, "delay", lambda _: None)
    auth = {"Authorization": "Bearer unit-test-admin-token-strong"}
    try:
        with TestClient(api.app) as client:
            assert client.get("/programs").status_code == 401
            created = client.post("/programs", headers=auth, json={
                "name": "approved-test", "rules_url": "https://example.com/rules",
                "rights_confirmed": True, "enabled": True})
            assert created.status_code == 200, created.text
            program_id = created.json()["id"]
            r = client.post("/recordings", headers=auth, json={
                "program_id": program_id, "external_id": "one",
                "source_url": "https://www.youtube.com/watch?v=abc"})
            assert r.status_code == 200, r.text
            record_id = r.json()["id"]
            repeat = client.post("/recordings", headers=auth, json={
                "program_id": program_id, "external_id": "one",
                "source_url": "https://www.youtube.com/watch?v=abc"})
            assert repeat.json()["duplicate"] is True
            with SessionLocal() as db:
                record = db.get(Recording, record_id)
                record.openshorts_job_id = "mock-job-id"
                db.commit()
            payload = json.dumps({"event": "job.completed", "job_id": "mock-job-id",
                                  "clips": [{"title": "clip", "video_url": "/output/a.mp4"}]}).encode()
            secret = "super-secret-for-tests"
            monkeypatch.setattr(settings, "openshorts_webhook_secret", secret)
            signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            bad = client.post("/hooks/openshorts", content=payload)
            assert bad.status_code == 401
            good = client.post("/hooks/openshorts", content=payload,
                               headers={"X-OpenShorts-Signature": signature})
            assert good.status_code == 200, good.text
            assert good.json()["new_clips"] == 1
            assert client.post("/hooks/openshorts", content=payload,
                headers={"X-OpenShorts-Signature": signature}).json()["new_clips"] == 0
            clip_id = client.get("/clips", headers=auth).json()[0]["id"]
            pending = client.post(f"/clips/{clip_id}/review", headers=auth,
                                  json={"decision": "approve", "safety_checked": True})
            assert pending.status_code == 409  # video has not passed the scanner
            with SessionLocal() as db:
                clip = db.get(Clip, clip_id)
                clip.safety_status = "review"
                clip.safety_reasons = ["Must verify manually"]
                db.commit()
            denied = client.post(f"/clips/{clip_id}/review", headers=auth,
                                 json={"decision": "approve", "safety_checked": False})
            assert denied.status_code == 422
            approved = client.post(f"/clips/{clip_id}/review", headers=auth,
                                   json={"decision": "approve", "safety_checked": True})
            assert approved.status_code == 200
            publish = client.post(f"/clips/{clip_id}/publish", headers=auth,
                                  json={"account_ids": [1], "confirmation": "PUBLISH"})
            assert publish.status_code == 403
    finally:
        SessionLocal.configure(bind=original_engine)
        db_module.engine = original_engine
        engine.dispose()


def test_restyle_adapter_calls_openshorts_subtitles():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"video_url": "/output/styled.mp4"})
    with httpx.Client(base_url="http://upstream", transport=httpx.MockTransport(handler)) as client:
        result = OpenShorts(client).restyle("job-2", 1, "pill")
    assert result["video_url"] == "/output/styled.mp4"
    assert requests == [{"job_id": "job-2", "clip_index": 1, "preset": "pill"}]


def test_authorized_telegram_discovery(monkeypatch, db):
    from cliprush.discovery import ingest_telegram_post
    from cliprush.models import ProgramCandidate
    settings = get_settings()
    monkeypatch.setattr(settings, "discovery_chat_ids", "-100456")
    incoming = {"chat": {"id": -100456, "username": "example"},
                "message_id": 11,
                "text": "Конкурс нарезок: новые условия опубликованы"}
    message = ingest_telegram_post(db, incoming)
    assert "t.me/example/11" in message
    assert ingest_telegram_post(db, incoming) is None
    assert db.scalar(select(ProgramCandidate)).status == "pending"
    incoming["chat"]["id"] = -100123
    incoming["message_id"] = 12
    assert ingest_telegram_post(db, incoming) is None
