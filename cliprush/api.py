"""ClipRush owner API and signed provider webhooks."""
import asyncio
import logging
import time
import uuid
import hmac
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.db import SessionLocal, init_db
from cliprush.logic import ALLOWED_PLATFORMS, apply_completed_job, valid_webhook, validate_source_url
from cliprush.models import (
    Account, AuditEvent, Clip, Expense, Metric, Moment, Payout, Post, Program, ProgramCandidate, PromptVersion, Recording, Streamer, WeeklyReport, now,
)
from cliprush.tasks import (
    dispatch_post_task, prepare_clips_task, scan_clip_task, submit_recording,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from cliprush.config import verify_boot_configuration
    verify_boot_configuration()
    init_db()
    yield


app = FastAPI(title="ClipRush", version="0.3.0", lifespan=lifespan)
http_log = logging.getLogger("cliprush.http")


@app.middleware("http")
async def request_log(request: Request, call_next):
    """Request tracing without logging Authorization headers or webhook bodies."""
    supplied = request.headers.get("X-Request-ID", "")
    rid = supplied if re.fullmatch(r"[A-Za-z0-9-]{8,64}", supplied) else uuid.uuid4().hex
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        http_log.exception("request_failed rid=%s method=%s path=%s elapsed_ms=%d",
                           rid, request.method, request.url.path,
                           int((time.perf_counter() - start) * 1000))
        raise
    response.headers["X-Request-ID"] = rid
    http_log.info("request rid=%s method=%s path=%s status=%d elapsed_ms=%d",
                  rid, request.method, request.url.path, response.status_code,
                  int((time.perf_counter() - start) * 1000))
    return response


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


class AttachUpstreamIn(BaseModel):
    job_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")
    confirmation: Literal["VERIFIED_UPSTREAM_JOB"]


@app.post("/jobs/{recording_id}/attach-upstream", dependencies=[Depends(admin)])
def attach_upstream(recording_id: int, body: AttachUpstreamIn, db: Session = Depends(db_session)):
    """Link an upstream job after uncertain network outcome; never auto-resubmit."""
    record = db.get(Recording, recording_id)
    if not record:
        raise HTTPException(404, "Unknown recording")
    if record.status != "unknown" or record.openshorts_job_id:
        raise HTTPException(409, "Only unmatched unknown recordings can be reconciled")
    if db.scalar(select(Recording.id).where(Recording.openshorts_job_id == body.job_id)):
        raise HTTPException(409, "Upstream job ID already belongs to another recording")
    from cliprush.audit import audit
    record.openshorts_job_id = body.job_id
    record.status = "processing"
    record.error = None
    audit(db, "owner_api", "recording.upstream_attached", "recording", record.id)
    db.commit()
    return {"id": record.id, "status": record.status, "upstream_job_id": body.job_id}


@app.post("/clips/{clip_id}/retry-safety", dependencies=[Depends(admin)])
def retry_safety(clip_id: int, db: Session = Depends(db_session)):
    """Explicit retry of a read-only failed safety scan. Never overrides a block."""
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "Unknown clip")
    if (clip.status != "scan_failed" or clip.safety_status != "unknown"
            or db.scalar(select(Post.id).where(Post.clip_id == clip_id))):
        raise HTTPException(409, "Only failed, unpublished safety scans can be retried")
    clip.status = "needs_review"
    db.commit()
    scan_clip_task.delay(clip_id)
    return {"id": clip_id, "status": "needs_review", "queued_for": "safety_rescan"}


@app.post("/clips/{clip_id}/retry-style", dependencies=[Depends(admin)])
def retry_style(clip_id: int, db: Session = Depends(db_session)):
    """Owner verified OpenShorts style outcome and requested a new render."""
    clip = db.get(Clip, clip_id)
    if not clip or clip.status != "style_failed" or db.scalar(select(Post.id).where(Post.clip_id == clip_id)):
        raise HTTPException(409, "Only failed pre-publication styles can be retried")
    clip.status = "needs_review"
    clip.safety_status = "unknown"
    clip.safety_reasons = []
    db.commit()
    prepare_clips_task.delay(clip.recording_id)
    return {"id": clip.id, "status": "needs_review", "queued_for": "restyle"}


@app.get("/ops/stats", dependencies=[Depends(admin)])
def operational_stats(db: Session = Depends(db_session)):
    def histogram(model, field):
        return {str(status): count for status, count in
                db.execute(select(field, func.count(model.id)).group_by(field)).all()}
    return {
        "recordings": histogram(Recording, Recording.status),
        "moments": histogram(Moment, Moment.status),
        "clips": histogram(Clip, Clip.status),
        "posts": histogram(Post, Post.status),
        "publishing_env_enabled": get_settings().enable_publish,
    }


@app.get("/ops/audit", dependencies=[Depends(admin)])
def audit_history(db: Session = Depends(db_session)):
    events = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200))
    return [{"id": e.id, "at": e.created_at.isoformat(),
             "actor": e.actor, "action": e.action, "target_type": e.target_type,
             "target_id": e.target_id, "details": e.details} for e in events]


@app.get("/ops/stuck", dependencies=[Depends(admin)])
def stuck_jobs(db: Session = Depends(db_session)):
    from cliprush.recovery import _stale_time
    cutoff = _stale_time()
    return {
        "recordings": [{"id": r.id, "status": r.status, "error": r.error}
          for r in db.scalars(select(Recording).where(
              Recording.status.in_(("unknown", "failed", "submitting"))).limit(100))],
        "clips": [{"id": c.id, "status": c.status, "reasons": c.safety_reasons}
          for c in db.scalars(select(Clip).where(
              Clip.status.in_(("style_failed", "scan_failed", "styling", "scanning"))).limit(100))],
        "posts": [{"id": p.id, "status": p.status, "request_id": p.request_id}
          for p in db.scalars(select(Post).where(
              Post.status.in_(("unknown", "preparing", "sending"))).limit(100))],
    }


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


@app.get("/health/ready")
def health_ready(db: Session = Depends(db_session)):
    """Readiness requires database and Redis, but discloses no credentials."""
    from redis import Redis
    try:
        db.execute(text("SELECT 1")).scalar_one()
        with Redis.from_url(get_settings().redis_url, socket_connect_timeout=2,
                            socket_timeout=2) as redis_client:
            redis_client.ping()
    except Exception:
        raise HTTPException(503, "A required dependency is unavailable")
    return {"status": "ready"}


class ExpenseIn(BaseModel):
    vendor: str = Field(min_length=1, max_length=80)
    amount_cents: int = Field(ge=0)
    note: str = Field(default="", max_length=500)


@app.post("/expenses", dependencies=[Depends(admin)])
def record_expense(body: ExpenseIn, db: Session = Depends(db_session)):
    from cliprush.audit import audit
    entry = Expense(**body.model_dump())
    db.add(entry)
    db.flush()
    audit(db, "owner_api", "expense.recorded", "expense", entry.id,
          {"vendor": entry.vendor, "amount_cents": entry.amount_cents})
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "amount_cents": entry.amount_cents}


@app.get("/budget", dependencies=[Depends(admin)])
def budget_snapshot(db: Session = Depends(db_session)):
    from cliprush.budget import month_spent_cents, budget_remaining_cents
    return {"currency": "USD", "manual_accounting": True,
            "month_spent_cents": month_spent_cents(db),
            "remaining_cents": budget_remaining_cents(db),
            "limit_usd": get_settings().budget_limit_usd}


class PromptProposal(BaseModel):
    prompt: str = Field(min_length=120, max_length=10000)


class PromptApproval(BaseModel):
    confirmation: Literal["ACTIVATE"]


@app.get("/prompts", dependencies=[Depends(admin)])
def prompt_versions(db: Session = Depends(db_session)):
    from cliprush.prompts import list_versions
    return [{"id": p.id, "version": p.version, "active": p.active,
             "approved": p.approved, "rejected": p.rejected,
             "prompt": p.prompt} for p in list_versions(db)]


@app.post("/prompts", dependencies=[Depends(admin)])
def propose_prompt(body: PromptProposal, db: Session = Depends(db_session)):
    from cliprush.prompts import propose
    try:
        version = propose(db, body.prompt)
    except ValueError as error:
        raise HTTPException(422, str(error))
    return {"id": version.id, "version": version.version, "active": False}


@app.post("/prompts/{version_id}/activate", dependencies=[Depends(admin)])
def approve_prompt(version_id: int, body: PromptApproval, db: Session = Depends(db_session)):
    from cliprush.prompts import activate
    try:
        version = activate(db, version_id)
    except ValueError as error:
        raise HTTPException(409, str(error))
    return {"id": version.id, "active": version.active, "approved": version.approved}


@app.post("/prompts/{version_id}/reject", dependencies=[Depends(admin)])
def dismiss_prompt(version_id: int, db: Session = Depends(db_session)):
    from cliprush.prompts import reject
    try:
        version = reject(db, version_id)
    except ValueError as error:
        raise HTTPException(409, str(error))
    return {"id": version.id, "rejected": version.rejected}


@app.get("/candidates", dependencies=[Depends(admin)])
def list_candidates(db: Session = Depends(db_session)):
    return [{"id": c.id, "source": c.source, "source_url": c.source_url,
             "text": c.text, "status": c.status} for c in db.scalars(
            select(ProgramCandidate).order_by(ProgramCandidate.id.desc()).limit(100))]


class CandidateDecision(BaseModel):
    status: Literal["dismissed", "reviewed"]


@app.post("/candidates/{candidate_id}/decision", dependencies=[Depends(admin)])
def candidate_decision(candidate_id: int, body: CandidateDecision, db: Session = Depends(db_session)):
    item = db.get(ProgramCandidate, candidate_id)
    if item is None:
        raise HTTPException(404, "Candidate not found")
    item.status = body.status
    db.commit()
    return {"id": item.id, "status": item.status}


class PublishPauseIn(BaseModel):
    paused: bool
    reason: str = Field(default="", max_length=500)


@app.get("/controls/publishing", dependencies=[Depends(admin)])
def get_publishing_control(db: Session = Depends(db_session)):
    from cliprush.control import publishing_paused
    return {"enabled_in_config": get_settings().enable_publish,
            "paused": publishing_paused(db)}


@app.post("/controls/publishing", dependencies=[Depends(admin)])
def update_publishing_control(body: PublishPauseIn, db: Session = Depends(db_session)):
    from cliprush.control import set_publishing_pause
    record = set_publishing_pause(db, body.paused, body.reason)
    return {"paused": record.paused, "reason": record.reason,
            "enabled_in_config": get_settings().enable_publish}


@app.get("/readiness", dependencies=[Depends(admin)])
def readiness():
    cfg = get_settings()
    return {"open_shorts_url": cfg.openshorts_api_url,
            "publishing_enabled": cfg.enable_publish,
            "missing_or_optional": {
                "admin_token": len(cfg.admin_token) < 16,
                "openshorts_webhook": not (cfg.public_base_url and cfg.openshorts_webhook_secret),
                "telegram": not (cfg.telegram_bot_token and cfg.telegram_owner_id and cfg.telegram_webhook_secret),
                "kick": not cfg.kick_webhooks_enabled,
                "youtube": not cfg.youtube_api_key,
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
    from cliprush.audit import audit
    program.enabled = body.enabled
    program.rights_confirmed = body.rights_confirmed
    audit(db, "owner_api", "program.activation", "program", program.id,
          {"enabled": program.enabled, "rights_confirmed": program.rights_confirmed})
    db.commit()
    return {"id": program.id, "enabled": program.enabled}


@app.post("/streamers", dependencies=[Depends(admin)])
def create_streamer(body: StreamerIn, db: Session = Depends(db_session)):
    program = db.get(Program, body.program_id)
    if not program or not program.enabled or not program.rights_confirmed:
        raise HTTPException(403, "Activate an authorized program first")
    if body.platform == "twitch" and not re.fullmatch(r"[A-Za-z0-9_]{3,25}", body.channel):
        raise HTTPException(422, "Invalid Twitch channel name")
    if body.platform == "kick" and not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", body.channel):
        raise HTTPException(422, "Invalid Kick slug")
    if body.platform == "youtube" and not re.fullmatch(r"[A-Za-z0-9_-]{11}", body.channel):
        raise HTTPException(422, "YouTube stream source must be an 11-character video ID")
    data = body.model_dump()
    if body.platform == "kick":
        data["channel"] = body.channel.lower()
    streamer = Streamer(**data)
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
    from cliprush.audit import audit
    account.paused = body.paused
    account.warning = body.warning[:500]
    audit(db, "owner_api", "account.pause", "account", account.id,
          {"paused": account.paused})
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
    if count:
        prepare_clips_task.delay(record.id)
    return {"status": record.status, "new_clips": count}


@app.post("/hooks/openshorts")
async def openshorts_hook(request: Request, db: Session = Depends(db_session),
                          x_openshorts_signature: str | None = Header(default=None)):
    from cliprush.security import read_limited_body
    raw = await read_limited_body(request, 1_000_000)
    if not valid_webhook(raw, x_openshorts_signature, get_settings().openshorts_webhook_secret):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected an event object")
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
    if count:
        prepare_clips_task.delay(record.id)
    return {"ok": True, "new_clips": count}


@app.post("/hooks/kick")
async def kick_hook(request: Request, db: Session = Depends(db_session)):
    from cliprush.kick import verify_kick_signature, handle_kick_event
    if not get_settings().kick_webhooks_enabled:
        raise HTTPException(503, "Kick integration not configured")
    from cliprush.security import read_limited_body
    raw = await read_limited_body(request, 100_000)
    try:
        await asyncio.to_thread(verify_kick_signature, raw, request.headers)
    except ValueError:
        raise HTTPException(401, "Invalid Kick signature or replay timestamp")
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(event, dict):
        raise HTTPException(400, "Invalid event payload")
    try:
        return handle_kick_event(db, event, request.headers)
    except ValueError as error:
        raise HTTPException(422, str(error))


@app.post("/hooks/telegram")
async def telegram_hook(request: Request, db: Session = Depends(db_session),
                        x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    from cliprush.telegram import handle_update
    secret = get_settings().telegram_webhook_secret
    if not secret or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, secret
    ):
        raise HTTPException(401, "Invalid Telegram secret")
    from cliprush.security import read_limited_body
    raw = await read_limited_body(request, 200_000)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected an update object")
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
    from cliprush.rules import package_title
    try:
        clip.title = package_title(body.title, clip.recording.program.rules)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
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
    if (body.decision == "approve" and
            (clip.safety_status not in ("review", "clear")
             or clip.status not in ("needs_review", "approved", "rejected"))):
        raise HTTPException(409, "Clip must finish safety review before approval")
    if body.decision == "approve" and not body.safety_checked:
        raise HTTPException(422, "Owner must manually watch and verify clip")
    clip.status = "approved" if body.decision == "approve" else "rejected"
    clip.safety_checked = body.decision == "approve" and body.safety_checked
    from cliprush.audit import audit
    clip.review_reason = body.reason[:500]
    audit(db, "owner_api", "clip.reviewed", "clip", clip.id,
          {"decision": body.decision, "safety_checked": clip.safety_checked})
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
    from cliprush.audit import audit
    payout.confirmed_at = now()
    audit(db, "owner_api", "payout.confirmed", "payout", payout.id,
          {"amount_cents": payout.amount_cents, "currency": payout.currency})
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
