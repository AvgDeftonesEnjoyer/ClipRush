"""ClipRush owner API and signed provider webhooks."""
import hmac
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.db import SessionLocal, init_db
from cliprush.logic import ALLOWED_PLATFORMS, apply_completed_job, valid_webhook, validate_source_url
from cliprush.models import (
    Account, Clip, Metric, Payout, Post, Program, Recording, Streamer, WeeklyReport, now,
)
from cliprush.tasks import (
    dispatch_post_task, scan_clip_task, submit_recording,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ClipRush", version="0.2.0", lifespan=lifespan)


def db_session():
    with SessionLocal() as db:
        yield db


def admin(authorization: str | None = Header(default=None)) -> None:
    secret = get_settings().admin_token
    if not secret or len(secret) < 16 or not authorization or not hmac.compare_digest(
        authorization, "Bearer " + secret
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


class ProgramIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    rules_url: str = ""
    rights_confirmed: bool = False
    enabled: bool = False
    currency: Literal["USD", "USDT", "EUR"] = "USD"
    payout_method: str = Field(default="", max_length=40)
    rules: dict = Field(default_factory=dict)
    min_views: int = Field(default=100000, ge=1)
    reward_cents: int = Field(default=0, ge=0)


class ProgramToggle(BaseModel):
    enabled: bool
    rights_confirmed: bool


class StreamerIn(BaseModel):
    program_id: int
    platform: Literal["twitch", "kick", "youtube"] = "twitch"
    channel: str = Field(min_length=2, max_length=160)
    enabled: bool = False


class AccountIn(BaseModel):
    platform: Literal["youtube", "tiktok", "instagram"]
    profile: str = Field(min_length=1, max_length=160)
    enabled: bool = False


class AccountPause(BaseModel):
    paused: bool
    warning: str = ""


class RecordingIn(BaseModel):
    program_id: int
    external_id: str = Field(min_length=1, max_length=255)
    source_url: str


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    safety_checked: bool = False
    reason: str = ""


class TitleIn(BaseModel):
    title: str = Field(min_length=5, max_length=150)


class PublishIn(BaseModel):
    account_ids: list[int] = Field(min_length=1)
    confirmation: Literal["PUBLISH"]


class MetricIn(BaseModel):
    views: int = Field(ge=0)
    likes: int = Field(default=0, ge=0)
    completion_rate: float | None = Field(default=None, ge=0, le=1)
    avg_view_seconds: float | None = Field(default=None, ge=0)


class ConfirmPayoutIn(BaseModel):
    evidence_url: str = Field(min_length=1)
    amount_cents: int = Field(ge=0)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/readiness", dependencies=[Depends(admin)])
def readiness():
    cfg = get_settings()
    return {"open_shorts_url": cfg.openshorts_api_url,
            "publishing_enabled": cfg.enable_publish,
            "missing_or_optional": {
                "admin_token": len(cfg.admin_token) < 16,
                "openshorts_webhook": not (cfg.public_base_url and cfg.openshorts_webhook_secret),
                "telegram": not (cfg.telegram_bot_token and cfg.telegram_owner_id and cfg.telegram_webhook_secret),
                "twitch": not all([cfg.twitch_client_id, cfg.twitch_client_secret,
                                   cfg.twitch_bot_oauth, cfg.twitch_bot_nick]),
                "detection_llm": not (cfg.openrouter_api_key and cfg.openrouter_model),
                "vision_llm": not (cfg.openrouter_api_key and cfg.openrouter_vision_model),
                "music_reference": not cfg.acoustid_api_key,
                "upload_post": not cfg.upload_post_api_key,
            }}


@app.post("/programs", dependencies=[Depends(admin)])
def create_program(body: ProgramIn, db: Session = Depends(db_session)):
    if body.enabled and (not body.rights_confirmed or not body.rules_url):
        raise HTTPException(400, "Published program rules and confirmed rights required")
    program = Program(**body.model_dump())
    db.add(program)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Program name already exists")
    db.refresh(program)
    return {"id": program.id, "name": program.name, "enabled": program.enabled}


@app.get("/programs", dependencies=[Depends(admin)])
def list_programs(db: Session = Depends(db_session)):
    return [{"id": x.id, "name": x.name, "enabled": x.enabled, "rights_confirmed": x.rights_confirmed,
             "rules_url": x.rules_url, "currency": x.currency, "rules": x.rules,
             "min_views": x.min_views, "reward_cents": x.reward_cents}
            for x in db.scalars(select(Program).order_by(Program.id))]


@app.post("/programs/{program_id}/activation", dependencies=[Depends(admin)])
def activate_program(program_id: int, body: ProgramToggle, db: Session = Depends(db_session)):
    program = db.get(Program, program_id)
    if not program:
        raise HTTPException(404, "Unknown program")
    if body.enabled and (not body.rights_confirmed or not program.rules_url):
        raise HTTPException(400, "Enable only after verifying permissions and rules")
    program.enabled = body.enabled
    program.rights_confirmed = body.rights_confirmed
    db.commit()
    return {"id": program.id, "enabled": program.enabled}


@app.post("/streamers", dependencies=[Depends(admin)])
def create_streamer(body: StreamerIn, db: Session = Depends(db_session)):
    program = db.get(Program, body.program_id)
    if not program or not program.enabled or not program.rights_confirmed:
        raise HTTPException(403, "Activate an authorized program first")
    if body.platform == "twitch" and not re.fullmatch(r"[A-Za-z0-9_]{3,25}", body.channel):
        raise HTTPException(422, "Invalid Twitch channel name")
    streamer = Streamer(**body.model_dump())
    db.add(streamer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Streamer already present")
    db.refresh(streamer)
    return {"id": streamer.id, "channel": streamer.channel, "enabled": streamer.enabled}


@app.get("/streamers", dependencies=[Depends(admin)])
def list_streamers(db: Session = Depends(db_session)):
    return [{"id": x.id, "program_id": x.program_id, "platform": x.platform,
             "channel": x.channel, "enabled": x.enabled}
            for x in db.scalars(select(Streamer))]


@app.post("/accounts", dependencies=[Depends(admin)])
def create_account(body: AccountIn, db: Session = Depends(db_session)):
    account = Account(**body.model_dump())
    db.add(account)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Duplicate connected account")
    db.refresh(account)
    return {"id": account.id, "platform": account.platform, "profile": account.profile,
            "enabled": account.enabled}


@app.get("/accounts", dependencies=[Depends(admin)])
def list_accounts(db: Session = Depends(db_session)):
    return [{"id": x.id, "platform": x.platform, "profile": x.profile,
             "enabled": x.enabled, "paused": x.paused, "warning": x.warning}
            for x in db.scalars(select(Account))]


@app.post("/accounts/{account_id}/pause", dependencies=[Depends(admin)])
def pause_account(account_id: int, body: AccountPause, db: Session = Depends(db_session)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    account.paused = body.paused
    account.warning = body.warning[:500]
    db.commit()
    return {"id": account.id, "paused": account.paused}


@app.post("/recordings", dependencies=[Depends(admin)])
def ingest(body: RecordingIn, db: Session = Depends(db_session)):
    try:
        validate_source_url(body.source_url)
    except ValueError as error:
        raise HTTPException(422, str(error))
    program = db.get(Program, body.program_id)
    if not program or not program.enabled or not program.rights_confirmed:
        raise HTTPException(403, "Program has not been authorized and enabled")
    existing = db.scalar(select(Recording).where(
        Recording.program_id == body.program_id, Recording.external_id == body.external_id
    ))
    if existing:
        if existing.source_url != body.source_url:
            raise HTTPException(409, "External ID already used for another URL")
        return {"id": existing.id, "status": existing.status, "duplicate": True}
    record = Recording(**body.model_dump())
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Duplicate recording")
    db.refresh(record)
    submit_recording.delay(record.id)
    return {"id": record.id, "status": record.status, "duplicate": False}


@app.get("/jobs", dependencies=[Depends(admin)])
def list_jobs(db: Session = Depends(db_session)):
    return [{"id": r.id, "status": r.status, "openshorts_job_id": r.openshorts_job_id, "error": r.error}
            for r in db.scalars(select(Recording).order_by(Recording.id.desc()).limit(100))]


@app.post("/jobs/{recording_id}/sync", dependencies=[Depends(admin)])
def sync_job(recording_id: int, db: Session = Depends(db_session)):
    record = db.get(Recording, recording_id)
    if not record:
        raise HTTPException(404, "Not found")
    if not record.openshorts_job_id:
        raise HTTPException(409, "No upstream OpenShorts job ID")
    try:
        payload = OpenShorts().status(record.openshorts_job_id)
    except Exception:
        raise HTTPException(502, "Upstream OpenShorts unavailable")
    before = {c.id for c in db.scalars(select(Clip).where(Clip.recording_id == record.id))}
    count = apply_completed_job(db, record, payload)
    for clip in db.scalars(select(Clip).where(Clip.recording_id == record.id)):
        if clip.id not in before:
            scan_clip_task.delay(clip.id)
    return {"status": record.status, "new_clips": count}


@app.post("/hooks/openshorts")
async def openshorts_hook(request: Request, db: Session = Depends(db_session),
                          x_openshorts_signature: str | None = Header(default=None)):
    raw = await request.body()
    if not valid_webhook(raw, x_openshorts_signature, get_settings().openshorts_webhook_secret):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON")
    job_id = payload.get("job_id")
    if not isinstance(job_id, str):
        raise HTTPException(400, "Missing job_id")
    record = db.scalar(select(Recording).where(Recording.openshorts_job_id == job_id))
    if not record:
        raise HTTPException(404, "Unknown job (automatic status sync remains available)")
    event = payload.get("event")
    if event not in ("job.completed", "job.failed"):
        raise HTTPException(400, "Unknown event")
    before = {c.id for c in db.scalars(select(Clip).where(Clip.recording_id == record.id))}
    updated = dict(payload)
    updated["status"] = "completed" if event == "job.completed" else "failed"
    count = apply_completed_job(db, record, updated)
    for clip in db.scalars(select(Clip).where(Clip.recording_id == record.id)):
        if clip.id not in before:
            scan_clip_task.delay(clip.id)
    return {"ok": True, "new_clips": count}


@app.post("/hooks/telegram")
async def telegram_hook(request: Request, db: Session = Depends(db_session),
                        x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    from cliprush.telegram import handle_update
    secret = get_settings().telegram_webhook_secret
    if not secret or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, secret
    ):
        raise HTTPException(401, "Invalid Telegram secret")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    return handle_update(db, payload)


@app.get("/clips", dependencies=[Depends(admin)])
def list_clips(db: Session = Depends(db_session)):
    return [{"id": c.id, "recording_id": c.recording_id, "title": c.title,
             "video_url": c.video_url, "status": c.status, "style": c.style,
             "safety_status": c.safety_status, "safety_reasons": c.safety_reasons,
             "safety_checked": c.safety_checked}
            for c in db.scalars(select(Clip).order_by(Clip.id.desc()).limit(100))]


@app.post("/clips/{clip_id}/title", dependencies=[Depends(admin)])
def update_title(clip_id: int, body: TitleIn, db: Session = Depends(db_session)):
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    if db.scalar(select(Post.id).where(Post.clip_id == clip_id)):
        raise HTTPException(409, "Title locked: a publication job already exists")
    clip.title = body.title
    db.commit()
    return {"id": clip.id, "title": clip.title}


@app.post("/clips/{clip_id}/review", dependencies=[Depends(admin)])
def review(clip_id: int, body: ReviewIn, db: Session = Depends(db_session)):
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "Not found")
    if db.scalar(select(Post.id).where(Post.clip_id == clip_id)):
        raise HTTPException(409, "Cannot change review after dispatch request")
    if clip.status == "blocked" or clip.safety_status == "blocked":
        raise HTTPException(409, "Automatic safety block requires investigation, not approval")
    if body.decision == "approve" and not body.safety_checked:
        raise HTTPException(422, "Owner must manually watch and verify clip")
    clip.status = "approved" if body.decision == "approve" else "rejected"
    clip.safety_checked = body.decision == "approve" and body.safety_checked
    clip.review_reason = body.reason[:500]
    db.commit()
    return {"id": clip.id, "status": clip.status}


@app.post("/clips/{clip_id}/publish", dependencies=[Depends(admin)])
def publish(clip_id: int, body: PublishIn, db: Session = Depends(db_session)):
    from cliprush.publishing import queue_approved_clip
    if not get_settings().enable_publish:
        raise HTTPException(403, "Publishing is disabled by default")
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "Not found")
    try:
        ids = queue_approved_clip(db, clip, body.account_ids)
    except ValueError as error:
        raise HTTPException(409, str(error))
    for post_id in ids:
        dispatch_post_task.delay(post_id)
    return {"clip_id": clip.id, "post_ids": ids, "status": "queued"}


@app.get("/posts", dependencies=[Depends(admin)])
def list_posts(db: Session = Depends(db_session)):
    return [{"id": x.id, "clip_id": x.clip_id, "account_id": x.account_id,
             "status": x.status, "post_url": x.post_url, "error": x.error}
            for x in db.scalars(select(Post).order_by(Post.id.desc()).limit(100))]


@app.post("/posts/{post_id}/metrics", dependencies=[Depends(admin)])
def manual_metric(post_id: int, body: MetricIn, db: Session = Depends(db_session)):
    from cliprush.tracking import record_metric
    post = db.get(Post, post_id)
    if not post or post.status != "published":
        raise HTTPException(409, "Unknown or unpublished post")
    obj = record_metric(db, post, {"platforms": {post.account.platform: {
        "success": True, "post_metrics": {
            "views": body.views, "likes": body.likes,
            "full_video_watched_rate": body.completion_rate,
            "average_time_watched": body.avg_view_seconds}}}})
    return {"id": obj.id, "views": obj.views, "source": "manually_entered"}


@app.get("/payouts", dependencies=[Depends(admin)])
def list_payouts(db: Session = Depends(db_session)):
    return [{"id": p.id, "post_id": p.post_id, "amount_cents": p.amount_cents,
             "currency": p.currency, "status": p.status, "evidence_url": p.evidence_url}
            for p in db.scalars(select(Payout).order_by(Payout.id.desc()))]


@app.post("/payouts/{payout_id}/confirm", dependencies=[Depends(admin)])
def confirm_payout(payout_id: int, body: ConfirmPayoutIn, db: Session = Depends(db_session)):
    payout = db.get(Payout, payout_id)
    if not payout or payout.status != "eligible":
        raise HTTPException(409, "Only eligible, verified payouts can be confirmed")
    payout.status = "confirmed"
    payout.amount_cents = body.amount_cents
    payout.evidence_url = body.evidence_url[:1000]
    payout.confirmed_at = now()
    db.commit()
    return {"id": payout.id, "status": payout.status}


@app.get("/reports/latest", dependencies=[Depends(admin)])
def latest_report(db: Session = Depends(db_session)):
    report = db.scalar(select(WeeklyReport).order_by(WeeklyReport.id.desc()))
    return {"text": report.text, "summary": report.summary} if report else {"text": "No report yet"}


@app.post("/reports/generate", dependencies=[Depends(admin)])
def generate_report(db: Session = Depends(db_session)):
    from cliprush.learning import save_weekly
    report = save_weekly(db)
    return {"text": report.text, "summary": report.summary}
