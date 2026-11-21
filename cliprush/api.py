import json
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.db import SessionLocal, init_db
from cliprush.logic import ALLOWED_PLATFORMS, apply_completed_job, valid_webhook, validate_source_url
from cliprush.models import Clip, Program, Recording
from cliprush.openshorts import OpenShorts
from cliprush.tasks import submit_recording


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ClipRush", version="0.1.0", lifespan=lifespan)


def db_session():
    with SessionLocal() as db:
        yield db


def admin(authorization: str | None = Header(default=None)) -> None:
    secret = get_settings().admin_token
    if not secret or len(secret) < 16 or authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")


class ProgramIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    rules_url: str = ""
    rights_confirmed: bool = False
    enabled: bool = False


class RecordingIn(BaseModel):
    program_id: int
    external_id: str = Field(min_length=1, max_length=255)
    source_url: str


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    safety_checked: bool = False
    reason: str = ""


class PublishIn(BaseModel):
    platforms: list[str]
    confirmation: Literal["PUBLISH"]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/programs", dependencies=[Depends(admin)])
def create_program(body: ProgramIn, db: Session = Depends(db_session)):
    if body.enabled and not body.rights_confirmed:
        raise HTTPException(400, "Cannot enable program without confirmed rights")
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
    return [{"id": x.id, "name": x.name, "enabled": x.enabled, "rights_confirmed": x.rights_confirmed, "rules_url": x.rules_url}
            for x in db.scalars(select(Program).order_by(Program.id))]


@app.post("/recordings", dependencies=[Depends(admin)])
def ingest(body: RecordingIn, db: Session = Depends(db_session)):
    try:
        validate_source_url(body.source_url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    program = db.get(Program, body.program_id)
    if not program or not program.enabled or not program.rights_confirmed:
        raise HTTPException(403, "Program has not been authorized and enabled")
    existing = db.scalar(select(Recording).where(
        Recording.program_id == body.program_id, Recording.external_id == body.external_id
    ))
    if existing:
        if existing.source_url != body.source_url:
            raise HTTPException(409, "External ID is already used for another source")
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
        raise HTTPException(409, "Not submitted to OpenShorts yet")
    payload = OpenShorts().status(record.openshorts_job_id)
    count = apply_completed_job(db, record, payload)
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
        raise HTTPException(404, "Unknown job")
    event = payload.get("event")
    if event not in ("job.completed", "job.failed"):
        raise HTTPException(400, "Unknown event")
    updated = dict(payload)
    updated["status"] = "completed" if event == "job.completed" else "failed"
    count = apply_completed_job(db, record, updated)
    return {"ok": True, "new_clips": count}


@app.get("/clips", dependencies=[Depends(admin)])
def list_clips(db: Session = Depends(db_session)):
    return [{"id": c.id, "recording_id": c.recording_id, "title": c.title,
             "video_url": c.video_url, "status": c.status, "safety_checked": c.safety_checked}
            for c in db.scalars(select(Clip).order_by(Clip.id.desc()).limit(100))]


@app.post("/clips/{clip_id}/review", dependencies=[Depends(admin)])
def review(clip_id: int, body: ReviewIn, db: Session = Depends(db_session)):
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "Not found")
    if clip.status in ("submitted", "publishing"):
        raise HTTPException(409, "Already submitted for publication")
    if body.decision == "approve" and not body.safety_checked:
        raise HTTPException(422, "A manual safety review is required")
    clip.status = "approved" if body.decision == "approve" else "rejected"
    clip.safety_checked = body.decision == "approve" and body.safety_checked
    clip.review_reason = body.reason
    db.commit()
    return {"id": clip.id, "status": clip.status}


@app.post("/clips/{clip_id}/publish", dependencies=[Depends(admin)])
def publish(clip_id: int, body: PublishIn, db: Session = Depends(db_session)):
    if not get_settings().enable_publish:
        raise HTTPException(403, "Publishing is disabled by default")
    if not body.platforms or len(body.platforms) != len(set(body.platforms)) or not set(body.platforms) <= ALLOWED_PLATFORMS:
        raise HTTPException(422, "Invalid publishing platforms")
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "Not found")
    if clip.status != "approved" or not clip.safety_checked:
        raise HTTPException(409, "Clip is not approved and safety reviewed")
    if not clip.recording.program.enabled or not clip.recording.program.rights_confirmed:
        raise HTTPException(403, "Program is not currently authorized")
    if not clip.recording.openshorts_job_id:
        raise HTTPException(409, "Missing OpenShorts job ID")
    # Reserve status before external call: prevent duplicate submission on concurrent retries.
    clip.status = "publishing"
    db.commit()
    try:
        result = OpenShorts().publish(clip.recording.openshorts_job_id, clip.clip_index, body.platforms, clip.title)
    except Exception:
        clip.status = "publish_unknown"  # do not retry blindly: remote call may have succeeded
        db.commit()
        raise HTTPException(502, "Publish outcome unknown; inspect upstream before retrying")
    clip.status = "submitted"
    clip.publish_response = result
    db.commit()
    return {"id": clip.id, "status": "submitted", "upstream": result}
