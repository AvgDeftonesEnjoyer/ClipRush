"""Read-only weekly performance analysis; never silently edits prompts or budgets."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cliprush.models import Metric, Payout, Post, WeeklyReport, now


def summarize(db: Session) -> dict:
    posts = list(db.scalars(select(Post).where(Post.status == "published")))
    totals = []
    for post in posts:
        latest = db.scalar(select(Metric).where(Metric.post_id == post.id).order_by(Metric.captured_at.desc(), Metric.id.desc()))
        if latest:
            totals.append((post, latest.views))
    totals.sort(key=lambda x: x[1], reverse=True)
    confirmed = sum(int(x.amount_cents) for x in db.scalars(select(Payout).where(Payout.status == "confirmed")))
    eligible = sum(int(x.amount_cents) for x in db.scalars(select(Payout).where(Payout.status == "eligible")))
    hits_100k = sum(1 for _, views in totals if views >= 100000)
    observations = len(totals)
    streamer_stats = defaultdict(list)
    for post, views in totals:
        streamer_stats[post.clip.recording.program.name].append(views)
    top_sources = sorted(
        ({"program": name, "posts": len(values), "avg_views": round(sum(values)/len(values))}
         for name, values in streamer_stats.items()),
        key=lambda x: x["avg_views"], reverse=True,
    )[:5]
    recommendation = "Collect observations (minimum 100 posts for tuning)"
    if observations >= 100:
        if hits_100k >= 3 and confirmed > 0:
            recommendation = "Candidate: scale after explicit owner approval"
        elif hits_100k and confirmed == 0:
            recommendation = "Candidate: review payout terms and alternative niche"
        elif not any(views >= 30000 for _, views in totals):
            recommendation = "Candidate: stop publishing and preserve data"
        else:
            recommendation = "Continue experiment; one A/B test this week"
    return {"published": len(posts), "tracked": observations,
            "views": sum(v for _, v in totals),
            "hits_100k": hits_100k, "eligible_usd_cents": eligible,
            "confirmed_usd_cents": confirmed,
            "top_3": [{"post_id": p.id, "views": v, "url": p.post_url} for p, v in totals[:3]],
            "top_sources": top_sources,
            "can_auto_tune": observations >= 100,
            "recommendation": recommendation}


def render_summary(summary: dict) -> str:
    top = ", ".join(f"#{x['post_id']} {x['views']} views" for x in summary["top_3"]) or "no posts"
    return (
        "ClipRush weekly review\n"
        f"Published: {summary['published']} | Tracked: {summary['tracked']}\n"
        f"Views: {summary['views']} | 100k+: {summary['hits_100k']}\n"
        f"Eligible: {summary['eligible_usd_cents']/100:.2f} (unconfirmed) | "
        f"Confirmed: {summary['confirmed_usd_cents']/100:.2f} (currency conversion NOT applied)\n"
        f"Top 3: {top}\n"
        f"Decision: {summary['recommendation']}\n"
        "No prompts or limits were changed without your approval."
    )


def save_weekly(db: Session) -> WeeklyReport:
    week_key = now().date().isoformat()
    existing = db.scalar(select(WeeklyReport).where(WeeklyReport.week_key == week_key))
    if existing:
        return existing
    summary = summarize(db)
    report = WeeklyReport(week_key=week_key, summary=summary, text=render_summary(summary))
    db.add(report)
    db.commit()
    db.refresh(report)
    return report
