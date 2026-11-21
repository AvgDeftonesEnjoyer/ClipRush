"""Reconcile worker crashes without blindly replaying external side effects.

A reserved 'submitting' OpenShorts job or 'sending' upload may have reached
the provider. Mark as unknown and require lookup by request/job identifier.
Only operations with no external side effects are queued for retry.
"""
from datetime import timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from cliprush.config import get_settings
from cliprush.models import Clip, Moment, Post, Recording, now


def _stale_time():
    return now() - timedelta(minutes=max(15, get_settings().recovery_stale_minutes))


def recover(db: Session) -> dict:
    cutoff = _stale_time()
    report = {"recordings": [], "moments": [], "clips_to_prepare": [],
              "posts_requeued": [], "flagged_unknown": []}
    # FOR UPDATE SKIP LOCKED prevents concurrent sweeps from mutating a row
    # currently being claimed by another worker (Postgres; ignored by SQLite).
    query = lambda model: select(model).where(model.updated_at < cutoff).with_for_update(skip_locked=True)
    for record in db.scalars(query(Recording).where(Recording.status == "queued").limit(50)):
        report["recordings"].append(record.id)
        record.updated_at = now()
    for record in db.scalars(query(Recording).where(Recording.status == "submitting").limit(50)):
        record.status = "unknown"
        record.error = "OpenShorts request may have completed; inspect provider before linking or retrying."
        report["flagged_unknown"].append("recording:" + str(record.id))
    for moment in db.scalars(query(Moment).where(Moment.status == "queued").limit(50)):
        moment.updated_at = now()
        report["moments"].append(moment.id)
    for moment in db.scalars(query(Moment).where(Moment.status == "detecting").limit(50)):
        moment.status = "failed"
        moment.error = "Detection worker interrupted; inspect source and rerun manually"
        report["flagged_unknown"].append("moment:" + str(moment.id))
    for clip in db.scalars(query(Clip).where(
        Clip.status == "needs_review", Clip.safety_status == "unknown"
    ).limit(50)):
        # Original style request should only be reissued if not yet claimed.
        report["clips_to_prepare"].append(clip.recording_id)
        clip.updated_at = now()
    for clip in db.scalars(query(Clip).where(Clip.status == "styling").limit(50)):
        clip.status = "style_failed"
        clip.safety_status = "unknown"
        clip.safety_reasons = ["OpenShorts styling outcome unknown; owner must inspect before retry"]
        report["flagged_unknown"].append("clip:" + str(clip.id))
    for clip in db.scalars(query(Clip).where(Clip.status == "scanning").limit(50)):
        clip.status = "scan_failed"
        clip.safety_status = "unknown"
        clip.safety_reasons = ["Safety worker interrupted; owner must request rescan"]
        report["flagged_unknown"].append("clip:" + str(clip.id))
    for post in db.scalars(query(Post).where(Post.status == "preparing").limit(50)):
        # No remote request occurs before the status changes to sending.
        post.status = "queued"
        post.error = "Media preparation interrupted; safe to prepare again"
        report["posts_requeued"].append(post.id)
    for post in db.scalars(query(Post).where(Post.status == "sending").limit(50)):
        post.status = "unknown"
        post.error = "Upload may have succeeded; reconcile by request_id, do not retry upload"
        report["flagged_unknown"].append("post:" + str(post.id))
    db.commit()
    report["clips_to_prepare"] = sorted(set(report["clips_to_prepare"]))
    return report
