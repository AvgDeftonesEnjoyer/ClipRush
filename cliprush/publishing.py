"""Upload-Post dispatch: single account, at-most-once local reservation and reconciliation.

The upload provider handles each connected platform account. A timeout leaves the
job in 'unknown'; never create a new upload until the upstream result is checked.
"""
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.media import fetch_clip
from cliprush.models import Account, Clip, Post, now


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def quota_error(db: Session, account: Account, urgent: bool = False) -> str | None:
    cfg = get_settings()
    if not account.enabled or account.paused:
        return "Account disabled or paused"
    current = now()
    recent = list(db.scalars(select(Post).where(
        Post.account_id == account.id,
        Post.created_at >= current - timedelta(hours=24),
        Post.status.in_(["sending", "submitted", "published", "unknown"]),
    ).order_by(Post.created_at.desc())))
    new = (current - utc(account.created_at)) < timedelta(days=7)
    limit = cfg.max_post_day_new if new else cfg.max_post_day_established
    if len(recent) >= limit:
        return f"24h account quota reached ({limit})"
    if recent and not urgent and (current - utc(recent[0].created_at)).total_seconds() < cfg.min_post_gap_seconds:
        return "Minimum account gap not elapsed"
    return None


def queue_approved_clip(db: Session, clip: Clip, account_ids: list[int] | None = None) -> list[int]:
    if clip.status != "approved" or not clip.safety_checked or clip.safety_status == "blocked":
        raise ValueError("Explicit manual review and a non-blocked safety result are required")
    if not clip.recording.program.enabled or not clip.recording.program.rights_confirmed:
        raise ValueError("Program is inactive or lacks confirmed rights")
    accounts = list(db.scalars(select(Account).where(
        Account.enabled.is_(True), Account.paused.is_(False)
    )))
    if account_ids is not None:
        accounts = [a for a in accounts if a.id in account_ids]
        if len(accounts) != len(set(account_ids)):
            raise ValueError("At least one requested account is not active")
    if not accounts:
        raise ValueError("No active social account available")
    ids = []
    for account in accounts:
        existing = db.scalar(select(Post).where(Post.clip_id == clip.id, Post.account_id == account.id))
        if existing:
            ids.append(existing.id)
            continue
        key = hashlib.sha256(f"cliprush:{clip.id}:{account.id}".encode()).hexdigest()
        post = Post(clip_id=clip.id, account_id=account.id, idempotency_key=key, status="queued")
        db.add(post)
        db.flush()
        ids.append(post.id)
    db.commit()
    return ids


def upload(post: Post, local_path: str) -> dict:
    cfg = get_settings()
    if not cfg.upload_post_api_key:
        raise RuntimeError("Upload-Post API key is missing")
    request_id = post.request_id or str(uuid.uuid5(uuid.NAMESPACE_URL, post.idempotency_key))
    with open(local_path, "rb") as file:
        response = httpx.post(
            "https://api.upload-post.com/api/upload",
            headers={"Authorization": "Apikey " + cfg.upload_post_api_key,
                     "Idempotency-Key": post.idempotency_key},
            data={"user": post.account.profile, "platform[]": post.account.platform,
                  "title": post.clip.title[:150],
                  "description": post.clip.title[:150],
                  "request_id": request_id, "async_upload": "true"},
            files={"video": ("clip.mp4", file, "video/mp4")}, timeout=300,
        )
    response.raise_for_status()
    return response.json()


def dispatch(db: Session, post_id: int) -> str:
    post = db.scalar(select(Post).where(Post.id == post_id).with_for_update())
    if post is None:
        return "not_found"
    if post.status not in ("queued", "deferred"):
        return post.status
    if not get_settings().enable_publish:
        return "disabled"
    from cliprush.budget import within_budget
    if not within_budget(db):
        post.status = "deferred"
        post.error = "Recorded monthly expense cap reached"
        db.commit()
        return post.status
    if not post.clip.safety_checked or post.clip.status != "approved" or post.clip.safety_status == "blocked":
        post.status = "blocked"
        db.commit()
        return post.status
    if not post.clip.recording.program.enabled or not post.clip.recording.program.rights_confirmed:
        post.status = "blocked"
        db.commit()
        return post.status
    urgent = bool(post.clip.recording.local_path)
    reason = quota_error(db, post.account, urgent=urgent)
    if reason:
        post.status = "deferred"
        post.error = reason
        db.commit()
        return post.status
    post.status = "preparing"
    db.commit()
    try:
        local = fetch_clip(post.clip.video_url, post.clip.id)
    except Exception as error:
        post.status = "blocked"
        post.error = "Media unavailable: " + str(error)[:300]
        db.commit()
        return post.status
    # Durable reservation BEFORE the external network operation.
    post.status = "sending"
    post.request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, post.idempotency_key))
    post.error = ""
    db.commit()
    try:
        result = upload(post, local)
        post.status = "submitted"
        post.request_id = result.get("request_id") or post.request_id
        post.error = ""
    except Exception as error:
        post.status = "unknown"
        post.error = "Check provider request_id before any retry: " + str(error)[:300]
    db.commit()
    return post.status


def poll_status(post: Post) -> dict:
    cfg = get_settings()
    if not post.request_id or not cfg.upload_post_api_key:
        raise RuntimeError("Missing request ID or Upload-Post credentials")
    response = httpx.get(
        "https://api.upload-post.com/api/uploadposts/status",
        headers={"Authorization": "Apikey " + cfg.upload_post_api_key},
        params={"request_id": post.request_id}, timeout=30,
    )
    response.raise_for_status()
    return response.json()


def reconcile(db: Session, post: Post, result: dict) -> str:
    state = str(result.get("status") or "")
    if state in ("pending", "queued", "processing", "in_progress"):
        post.status = "submitted"
    elif state == "completed":
        matched = next((x for x in result.get("results", []) if x.get("platform") == post.account.platform), None)
        if matched is None:
            post.status = "unknown"
            post.error = "Missing platform result"
        elif not matched.get("success"):
            post.status = "failed"
            post.error = str(matched.get("error") or matched.get("message") or "Platform error")[:500]
        else:
            post.status = "published"
            post.post_url = str(matched.get("url") or matched.get("post_url") or "")
            post.platform_post_id = str(matched.get("post_id") or matched.get("platform_post_id") or "") or None
            post.published_at = post.published_at or now()
    elif state == "failed":
        post.status = "failed"
        post.error = str(result.get("message") or "Upload failed")[:500]
    else:
        post.status = "unknown"
        post.error = "Provider status unknown: " + state
    if post.status == "failed" and any(word in post.error.lower() for word in
            ("copyright", "suspend", "ban", "strike", "policy violation")):
        post.account.paused = True
        post.account.warning = post.error
    db.commit()
    return post.status
