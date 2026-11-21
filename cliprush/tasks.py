"""Celery orchestration of all pipeline stages; external publication disabled by default."""
import logging
from pathlib import Path

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import or_, select

from cliprush.config import get_settings
from cliprush.db import SessionLocal, init_db
from cliprush.models import Clip, Moment, Post, Program, PromptVersion, Recording, Streamer
from cliprush.openshorts import OpenShorts

log = logging.getLogger(__name__)
celery_app = Celery("cliprush", broker=get_settings().redis_url, backend=get_settings().redis_url)
celery_app.conf.beat_schedule = {
    "openshorts-sync": {"task": "cliprush.sync_jobs", "schedule": 180.0},
    "crash-recovery": {"task": "cliprush.recover_stale", "schedule": 300.0},
    "post-dispatch": {"task": "cliprush.dispatch_pending", "schedule": 180.0},
    "post-reconcile": {"task": "cliprush.reconcile_posts", "schedule": 600.0},
    "post-metrics": {"task": "cliprush.collect_metrics", "schedule": 21600.0},
    "payout-alerts": {"task": "cliprush.payout_alerts", "schedule": 21600.0},
    "weekly-review": {"task": "cliprush.weekly_report", "schedule": crontab(hour=18, minute=0, day_of_week="sunday")},
}


@celery_app.task(name="cliprush.submit_recording")
def submit_recording(recording_id: int):
    init_db()
    with SessionLocal() as db:
        record = db.scalar(select(Recording).where(Recording.id == recording_id).with_for_update())
        if not record or record.openshorts_job_id or record.status != "queued":
            return
        from cliprush.budget import within_budget
        if not within_budget(db):
            record.status = "budget_hold"
            record.error = "Monthly recorded spending reached the owner limit"
            db.commit()
            return
        if not record.program.enabled or not record.program.rights_confirmed:
            record.status = "blocked"
            db.commit()
            return
        record.status = "submitting"
        db.commit()  # durable claim BEFORE any external network activity
        try:
            if record.local_path:
                target = Path(record.local_path).resolve()
                if not target.is_relative_to(Path(get_settings().data_dir).resolve()) or not target.is_file():
                    raise ValueError("Unsafe local source path")
                job_id = OpenShorts().process_file(str(target))
            else:
                job_id = OpenShorts().process(record.source_url)
            record.openshorts_job_id = job_id
            record.status = "processing"
            record.error = None
        except Exception as exc:
            # Do not retry automatically: OpenShorts may have created the job.
            record.status = "unknown"
            record.error = str(exc)[:1000]
        db.commit()


@celery_app.task(name="cliprush.detect_moment")
def detect_moment_task(moment_id: int):
    from cliprush.detect import score_candidate
    from cliprush.media import trim_video
    init_db()
    with SessionLocal() as db:
        moment = db.scalar(select(Moment).where(Moment.id == moment_id).with_for_update())
        if not moment or moment.status != "queued":
            return
        moment.status = "detecting"
        db.commit()
        try:
            active_prompt = db.scalar(select(PromptVersion).where(
                PromptVersion.module == "detect", PromptVersion.active.is_(True),
                PromptVersion.approved.is_(True)
            ).order_by(PromptVersion.version.desc()))
            decision = score_candidate(moment.source_path, active_prompt.prompt if active_prompt else None)
            if not decision:
                moment.status = "rejected"
                db.commit()
                return
            moment.score = decision.score
            moment.kind = decision.kind
            moment.hook = decision.hook
            moment.transcript = decision.transcript
            moment.start_seconds = decision.start
            moment.end_seconds = decision.end
            out = str(Path(get_settings().data_dir) / "selected" / f"moment-{moment.id}.mp4")
            trim_video(moment.source_path, out, decision.start, decision.end - decision.start)
            streamer = db.get(Streamer, moment.streamer_id)
            program = db.get(Program, streamer.program_id)
            if not program.enabled or not program.rights_confirmed:
                moment.status = "blocked"
                db.commit()
                return
            source_url = {
                "twitch": f"https://www.twitch.tv/{streamer.channel}",
                "kick": f"https://kick.com/{streamer.channel}",
                "youtube": f"https://www.youtube.com/watch?v={streamer.channel}",
            }.get(streamer.platform)
            if not source_url:
                raise ValueError("Unrecognized streamer platform")
            rec = Recording(program_id=streamer.program_id, external_id=moment.event_key,
                            source_url=source_url, local_path=out, status="queued")
            db.add(rec)
            db.commit()
            db.refresh(rec)
            moment.status = "selected"
            db.commit()
            submit_recording.delay(rec.id)
        except Exception as exc:
            moment.status = "failed"
            moment.error = str(exc)[:1500]
            db.commit()


@celery_app.task(name="cliprush.prepare_clips")
def prepare_clips_task(recording_id: int):
    """Apply one of six REAL caption presets, then screen clips serially per job."""
    init_db()
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if not recording or not recording.openshorts_job_id:
            return
        clip_ids = list(db.scalars(select(Clip.id).where(
            Clip.recording_id == recording.id,
            Clip.status == "needs_review", Clip.safety_status == "unknown"
        ).order_by(Clip.id)))
    for clip_id in clip_ids:
        with SessionLocal() as db:
            clip = db.scalar(select(Clip).where(Clip.id == clip_id).with_for_update())
            if clip is None or clip.status != "needs_review" or clip.safety_status != "unknown":
                continue
            clip.status = "styling"
            db.commit()
            try:
                result = OpenShorts().restyle(
                    recording.openshorts_job_id, clip.clip_index, clip.style
                )
                url = result.get("video_url") or result.get("download_url")
                # OpenShorts may return only metadata for current render.
                if url:
                    clip.video_url = str(url)
                from cliprush.media import invalidate_cached_clip
                invalidate_cached_clip(clip.id)
                clip.status = "needs_review"
                db.commit()
            except Exception as exc:
                clip.status = "blocked"
                clip.safety_status = "blocked"
                clip.safety_reasons = ["Caption style was not applied: " + str(exc)[:150]]
                db.commit()
                continue
        # Invoke stage synchronously to keep per-job updates serialized.
        scan_clip_task(clip_id)


@celery_app.task(name="cliprush.scan_clip")
def scan_clip_task(clip_id: int):
    from cliprush.media import fetch_clip, visual_fingerprint, ffprobe_seconds
    from cliprush.safety import inspect
    from cliprush.telegram import send_clip
    init_db()
    with SessionLocal() as db:
        clip = db.scalar(select(Clip).where(Clip.id == clip_id).with_for_update())
        if not clip or clip.status not in ("needs_review", "scan_failed"):
            return
        clip.status = "scanning"
        clip.safety_status = "unknown"
        db.commit()  # prevent duplicate scanners/Telegram previews
        try:
            clip_path = fetch_clip(clip.video_url, clip.id)
            clip.duration_seconds = ffprobe_seconds(clip_path)
            fingerprint = visual_fingerprint(clip_path)
            clip.fingerprint = fingerprint
            from cliprush.logic import duplicate_status
            duplicate, dispatched_duplicate = duplicate_status(db, clip.id, fingerprint)
            if dispatched_duplicate:
                clip.safety_status = "blocked"
                clip.status = "blocked"
                clip.safety_reasons = [f"Duplicate already dispatched clip #{dispatched_duplicate.id}"]
                db.commit()
                return
            outcome = inspect(clip_path)
            if duplicate:
                outcome = type(outcome)(outcome.status,
                    list(outcome.reasons) + [f"Possible duplicate of clip #{duplicate.id}: owner check required"])
            clip.safety_status = outcome.status
            clip.safety_reasons = outcome.reasons
            if outcome.status == "blocked":
                clip.status = "blocked"
            else:
                clip.status = "needs_review"
            db.commit()
            if clip.status != "blocked":
                send_clip(clip)
        except Exception as exc:
            clip.status = "scan_failed"
            clip.safety_status = "unknown"
            clip.safety_reasons = ["Safety check unavailable: " + str(exc)[:150]]
            db.commit()
            log.exception("Clip preflight failed, no automatic approval")


@celery_app.task(name="cliprush.sync_jobs")
def sync_jobs_task():
    from cliprush.logic import apply_completed_job
    init_db()
    with SessionLocal() as db:
        for rec in db.scalars(select(Recording).where(Recording.status == "processing").limit(50)):
            try:
                before = {c.id for c in db.scalars(select(Clip).where(Clip.recording_id == rec.id))}
                result = OpenShorts().status(rec.openshorts_job_id)
                apply_completed_job(db, rec, result)
                new = [c.id for c in db.scalars(select(Clip).where(Clip.recording_id == rec.id))
                       if c.id not in before]
                if new:
                    prepare_clips_task.delay(rec.id)
            except Exception:
                log.exception("OpenShorts status sync failed for recording %s", rec.id)


@celery_app.task(name="cliprush.dispatch_post")
def dispatch_post_task(post_id: int):
    from cliprush.publishing import dispatch
    init_db()
    with SessionLocal() as db:
        return dispatch(db, post_id)


@celery_app.task(name="cliprush.dispatch_pending")
def dispatch_pending_task():
    if not get_settings().enable_publish:
        return
    with SessionLocal() as db:
        ids = list(db.scalars(select(Post.id).where(Post.status.in_(["queued", "deferred"])).limit(30)))
    for post_id in ids:
        dispatch_post_task.delay(post_id)


@celery_app.task(name="cliprush.reconcile_posts")
def reconcile_posts_task():
    from cliprush.publishing import poll_status, reconcile
    with SessionLocal() as db:
        posts = list(db.scalars(select(Post).where(
            Post.status.in_(["sending", "submitted", "unknown"])
        ).limit(50)))
        for post in posts:
            if not post.request_id:
                continue
            try:
                reconcile(db, post, poll_status(post))
            except Exception:
                log.exception("Provider status reconciliation failed for post %s", post.id)


@celery_app.task(name="cliprush.collect_metrics")
def collect_metrics_task():
    from cliprush.tracking import fetch_post_metrics, record_metric
    with SessionLocal() as db:
        posts = list(db.scalars(select(Post).where(
            Post.status == "published", or_(Post.request_id.is_not(None), Post.platform_post_id.is_not(None))
        ).limit(200)))
        for post in posts:
            try:
                record_metric(db, post, fetch_post_metrics(post))
            except Exception:
                log.exception("Per-post metrics unavailable for post %s", post.id)


@celery_app.task(name="cliprush.weekly_report")
def weekly_report_task():
    from cliprush.learning import save_weekly
    from cliprush.telegram import send_report
    with SessionLocal() as db:
        report = save_weekly(db)
        try:
            send_report(report)
        except Exception:
            log.exception("Could not send weekly Telegram report")


@celery_app.task(name="cliprush.payout_alerts")
def payout_alerts_task():
    from cliprush.models import Payout, now
    from cliprush.telegram import send_message
    cfg = get_settings()
    if not cfg.telegram_bot_token or not cfg.telegram_owner_id:
        return
    with SessionLocal() as db:
        for payout in db.scalars(select(Payout).where(
            Payout.status == "eligible", Payout.notified_at.is_(None)
        ).limit(30)):
            post = db.get(Post, payout.post_id)
            if not post:
                continue
            program = post.clip.recording.program
            try:
                send_message(
                    f"ClipRush: potential payout {payout.amount_cents / 100:.2f} "
                    f"{payout.currency} for post #{post.id}.\n"
                    f"Program: {program.name}\nRules: {program.rules_url}\n"
                    f"Post: {post.post_url}\n"
                    "Submit evidence in the program manually. Mark confirmed ONLY after receipt."
                )
                payout.notified_at = now()
                db.commit()
            except Exception:
                log.exception("Payout notice failed; will retry later")


@celery_app.task(name="cliprush.recover_stale")
def recover_stale_task():
    """Never re-post after a timeout. Resume only read-only/pre-send tasks."""
    from cliprush.recovery import recover
    init_db()
    with SessionLocal() as db:
        report = recover(db)
    for record_id in report["recordings"]:
        submit_recording.delay(record_id)
    for moment_id in report["moments"]:
        detect_moment_task.delay(moment_id)
    for recording_id in report["clips_to_prepare"]:
        prepare_clips_task.delay(recording_id)
    if report["flagged_unknown"]:
        log.warning("Operator attention required: %s", report["flagged_unknown"][:30])
    return report
