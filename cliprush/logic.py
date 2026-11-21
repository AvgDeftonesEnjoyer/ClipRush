import hashlib
import hmac
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from cliprush.models import Clip, Recording

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "www.twitch.tv", "twitch.tv", "kick.com", "www.kick.com"}
ALLOWED_PLATFORMS = {"youtube", "tiktok", "instagram"}


def validate_source_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise ValueError("Use an HTTPS URL from an allowed video host")
    return value


def valid_webhook(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def apply_completed_job(session: Session, recording: Recording, payload: dict) -> int:
    if payload.get("status") == "failed":
        recording.status = "failed"
        recording.error = str(payload.get("error") or "OpenShorts failed")[:2000]
        session.commit()
        return 0
    if payload.get("status") != "completed":
        recording.status = "processing"
        session.commit()
        return 0
    clips = (payload.get("result") or {}).get("clips") or payload.get("clips") or []
    created = 0
    for index, data in enumerate(clips):
        current = session.scalar(select(Clip).where(Clip.recording_id == recording.id, Clip.clip_index == index))
        if current:
            continue
        session.add(Clip(
            recording_id=recording.id,
            clip_index=index,
            title=str(data.get("title") or data.get("video_title_for_youtube_short") or ""),
            video_url=str(data.get("download_url") or data.get("video_url") or ""),
        ))
        created += 1
    recording.status = "completed"
    session.commit()
    return created
