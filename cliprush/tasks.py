from celery import Celery
from sqlalchemy import select

from cliprush.config import get_settings
from cliprush.db import SessionLocal
from cliprush.models import Recording
from cliprush.openshorts import OpenShorts

celery_app = Celery("cliprush", broker=get_settings().redis_url, backend=get_settings().redis_url)


@celery_app.task(name="cliprush.submit_recording", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def submit_recording(recording_id: int) -> None:
    with SessionLocal() as db:
        record = db.get(Recording, recording_id)
        if record is None or record.openshorts_job_id or record.status not in ("queued", "error"):
            return
        if not record.program.enabled or not record.program.rights_confirmed:
            record.status = "blocked"
            db.commit()
            return
        job_id = OpenShorts().process(record.source_url)
        record.openshorts_job_id = job_id
        record.status = "processing"
        record.error = None
        db.commit()
