"""Upload-Post per-post analytics and manually-confirmed payout accounting."""
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.models import Metric, Payout, Post, now


def fetch_post_metrics(post: Post) -> dict:
    key = get_settings().upload_post_api_key
    if not key or not (post.request_id or post.platform_post_id):
        raise ValueError("Cannot query metrics without provider key and request/post ID")
    if post.request_id:
        # Official endpoint can resolve a specific upload without a native post ID.
        url = ("https://api.upload-post.com/api/uploadposts/post-analytics/"
               + quote(post.request_id, safe=""))
        params = {"platform": post.account.platform}
    else:
        url = "https://api.upload-post.com/api/uploadposts/post-analytics"
        params = {"platform_post_id": post.platform_post_id,
                  "platform": post.account.platform, "user": post.account.profile}
    resp = httpx.get(
        url, headers={"Authorization": "Apikey " + key}, params=params, timeout=45,
    )
    resp.raise_for_status()
    return resp.json()


def metric_fields(data: dict, platform: str) -> dict:
    platform_data = (data.get("platforms") or {}).get(platform) or {}
    raw = platform_data.get("post_metrics") or {}
    if not platform_data.get("success") or not raw:
        raise ValueError("Post-specific metrics unavailable or stale")
    def number(key, fallback=0):
        try:
            return max(0, int(raw.get(key) or fallback))
        except (TypeError, ValueError):
            return 0
    def optional_float(key):
        try:
            return float(raw[key]) if raw.get(key) is not None else None
        except (TypeError, ValueError):
            return None
    return {"views": number("views", raw.get("play_count", 0)),
            "likes": number("likes"),
            "completion_rate": optional_float("full_video_watched_rate"),
            "avg_view_seconds": optional_float("average_time_watched")
                                or optional_float("average_view_duration_seconds"),
            "raw": raw}


def payout_tier(program, views: int) -> int:
    tiers = (program.rules or {}).get("tiers") or []
    amounts = [0]
    for row in tiers:
        if isinstance(row, dict):
            try:
                if views >= int(row.get("views", 0)) > 0:
                    amounts.append(max(0, int(row["reward_cents"])))
            except (ValueError, TypeError, KeyError):
                pass
    if tiers:
        return max(amounts)
    return program.reward_cents if views >= program.min_views else 0


def record_metric(db: Session, post: Post, payload: dict) -> Metric:
    values = metric_fields(payload, post.account.platform)
    provider = (payload.get("platforms") or {}).get(post.account.platform) or {}
    if provider.get("platform_post_id"):
        post.platform_post_id = str(provider["platform_post_id"])[:160]
    if provider.get("post_url"):
        post.post_url = str(provider["post_url"])[:2000]
    point = Metric(post_id=post.id, **values)
    db.add(point)
    db.commit()
    db.refresh(point)
    expected = payout_tier(post.clip.recording.program, point.views)
    existing = db.scalar(select(Payout).where(Payout.post_id == post.id))
    if existing and existing.status != "confirmed":
        existing.amount_cents = expected
        existing.status = "eligible" if expected > 0 else "pending"
        if expected == 0:
            existing.notified_at = None
        db.commit()
    elif expected > 0 and not existing:
        db.add(Payout(post_id=post.id, amount_cents=expected,
                      currency=post.clip.recording.program.currency,
                      status="eligible"))
        db.commit()
    return point
