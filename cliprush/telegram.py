"""Owner-only Telegram review. No auto-approval, upload, or silent policy bypass."""
import hmac
import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.media import fetch_clip
from cliprush.models import Clip, Post, WeeklyReport
from cliprush.publishing import queue_approved_clip

log = logging.getLogger(__name__)


def tg_call(method: str, *, data: dict | None = None, files: dict | None = None) -> dict:
    cfg = get_settings()
    if not cfg.telegram_bot_token or not cfg.telegram_owner_id:
        raise RuntimeError("Telegram is not configured")
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/{method}"
    resp = httpx.post(url, data=data, files=files, timeout=120)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError("Telegram operation failed")
    return payload


def send_message(text: str) -> None:
    cfg = get_settings()
    if cfg.telegram_bot_token and cfg.telegram_owner_id:
        tg_call("sendMessage", data={"chat_id": str(cfg.telegram_owner_id),
                                   "text": text[:3900]})


def send_report(report: WeeklyReport) -> None:
    """Send owner review with optional explicit experiment-approval buttons."""
    cfg = get_settings()
    if not cfg.telegram_bot_token or not cfg.telegram_owner_id:
        return
    data = {"chat_id": str(cfg.telegram_owner_id), "text": report.text[:3900]}
    proposal_id = (report.summary or {}).get("prompt_proposal_id")
    if proposal_id:
        data["reply_markup"] = json.dumps({"inline_keyboard": [[
            {"text": "✅ Принять промпт", "callback_data": f"pa:{proposal_id}"},
            {"text": "❌ Отклонить промпт", "callback_data": f"pr:{proposal_id}"},
        ]]}, ensure_ascii=False)
    tg_call("sendMessage", data=data)


def send_clip(clip: Clip) -> None:
    cfg = get_settings()
    if not cfg.telegram_bot_token or not cfg.telegram_owner_id:
        return
    if clip.safety_status not in ("review", "clear") or clip.status != "needs_review":
        return
    caption = (f"Clip #{clip.id}: {clip.title[:250]}\n"
               f"Safety: {clip.safety_status}\n"
               f"Warnings: {', '.join(clip.safety_reasons)[:500]}\n"
               "Посмотри видео и проверь права, музыку, казино и личные данные.")
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Одобрить", "callback_data": f"a:{clip.id}"},
        {"text": "❌ Отклонить", "callback_data": f"r:{clip.id}"},
        {"text": "✏️ Заголовок", "callback_data": f"t:{clip.id}"},
    ]]}
    try:
        path = fetch_clip(clip.video_url, clip.id)
        with open(path, "rb") as file:
            tg_call("sendVideo",
                    data={"chat_id": str(cfg.telegram_owner_id),
                          "caption": caption[:1024],
                          "reply_markup": json.dumps(keyboard, ensure_ascii=False)},
                    files={"video": ("clip.mp4", file, "video/mp4")})
    except Exception:
        # Fail closed: without a playable preview, do not expose approval buttons.
        log.warning("Cannot send Telegram clip preview, owner must inspect via API: clip=%d", clip.id)
        send_message(f"Clip #{clip.id}: preview unavailable. Upload/review in admin UI before approval.")


def handle_update(db: Session, payload: dict) -> dict:
    cfg = get_settings()
    from cliprush.discovery import ingest_telegram_post
    if payload.get("channel_post"):
        result = ingest_telegram_post(db, payload["channel_post"])
        if result:
            send_message(result)
        return {"ok": True}
    callback = payload.get("callback_query")
    message = payload.get("message")
    event = callback or message or {}
    user = event.get("from") or {}
    if int(user.get("id", 0)) != cfg.telegram_owner_id or not cfg.telegram_owner_id:
        return {"ignored": True}
    if callback:
        data = str(callback.get("data", ""))
        try:
            action, raw = data.split(":", 1)
            clip_id = int(raw)
        except (ValueError, TypeError):
            return {"ignored": True}
        if action in ("pa", "pr"):
            from cliprush.prompts import activate, reject
            try:
                proposal = activate(db, clip_id) if action == "pa" else reject(db, clip_id)
                response = ("Detect prompt activated" if action == "pa" else "Prompt rejected") + f": v{proposal.version}"
            except ValueError as error:
                response = str(error)
            try:
                tg_call("answerCallbackQuery", data={
                    "callback_query_id": callback["id"], "text": response[:190]
                })
            except Exception:
                log.exception("Telegram prompt callback failed")
            return {"result": response}
        clip = db.get(Clip, clip_id)
        already_dispatched = db.scalar(select(Post.id).where(Post.clip_id == clip_id))
        if not clip or clip.status not in ("needs_review", "approved", "rejected"):
            response = "Clip unavailable"
        elif already_dispatched:
            response = "Post already queued; pause the social account, do not reuse review buttons"
        elif action == "r":
            clip.status = "rejected"
            clip.review_reason = "Telegram owner rejected"
            db.commit()
            response = "Rejected"
        elif action == "a" and clip.safety_status in ("review", "clear"):
            clip.status = "approved"
            clip.safety_checked = True
            clip.review_reason = "Owner watched and approved in Telegram"
            db.commit()
            response = "Approved for manual publishing"
            if cfg.enable_publish and cfg.auto_publish_on_approval:
                try:
                    ids = queue_approved_clip(db, clip)
                    from cliprush.tasks import dispatch_post_task
                    for pid in ids:
                        dispatch_post_task.delay(pid)
                    response = f"Approved; queued {len(ids)} post(s)"
                except ValueError as exc:
                    response = "Approved, but not queued: " + str(exc)[:120]
        elif action == "t":
            response = f"Reply with /title {clip_id} New title (clip will not be posted yet)"
        else:
            response = "Invalid action or safety block"
        try:
            tg_call("answerCallbackQuery", data={"callback_query_id": callback["id"], "text": response[:190]})
        except Exception:
            log.exception("Telegram callback acknowledgment failed")
        return {"result": response}
    if message:
        text = str(message.get("text") or "")
        if text.startswith("/title "):
            bits = text.split(" ", 2)
            if len(bits) == 3 and bits[1].isdigit():
                clip = db.get(Clip, int(bits[1]))
                if clip and clip.status in ("needs_review", "approved", "rejected") and not db.scalar(
                    select(Post.id).where(Post.clip_id == clip.id)
                ):
                    from cliprush.rules import package_title
                    clip.title = package_title(bits[2], clip.recording.program.rules)
                    db.commit()
                    send_message(f"Clip #{clip.id}: title updated. Approve via buttons.")
        elif text.startswith("/report"):
            report = db.scalar(select(WeeklyReport).order_by(WeeklyReport.id.desc()))
            send_message(report.text if report else "No reports available yet.")
    return {"ok": True}
