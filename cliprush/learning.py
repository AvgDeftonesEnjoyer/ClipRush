"""Honest, read-only analytics and checkpoint recommendations.

Only samples 72h+ old inform pattern evaluation. Requires manual owner approval
for any scale/stop/niche switch or prompt revision.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from cliprush.models import Metric, Payout, Post, WeeklyReport, now

CHECKPOINT = datetime(2027, 1, 10, 0, 0, tzinfo=timezone.utc)


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def summarize(db: Session, *, at: datetime | None = None) -> dict:
    current = at or now()
    posts = list(db.scalars(select(Post).where(Post.status == "published")))
    totals: list[tuple[Post, int]] = []
    matured: list[tuple[Post, int]] = []
    for post in posts:
        readings = list(db.scalars(select(Metric).where(Metric.post_id == post.id).order_by(Metric.captured_at.asc(), Metric.id.asc())))
        if not readings:
            continue
        latest = readings[-1]
        totals.append((post, latest.views))
        published_at = utc(post.published_at)
        if published_at and current >= published_at + timedelta(hours=72):
            target = published_at + timedelta(hours=72)
            # Use the nearest available sample rather than assuming a missing 72h reading is 0.
            nearest = min(readings, key=lambda m: abs((utc(m.captured_at) - target).total_seconds()))
            if abs((utc(nearest.captured_at) - target).total_seconds()) <= 12 * 3600:
                matured.append((post, nearest.views))
    totals.sort(key=lambda entry: entry[1], reverse=True)
    matured.sort(key=lambda entry: entry[1], reverse=True)

    amounts = defaultdict(lambda: {"eligible_cents": 0, "confirmed_cents": 0})
    confirmed_count = 0
    for payout in db.scalars(select(Payout)):
        if payout.status == "confirmed":
            confirmed_count += 1
            amounts[payout.currency]["confirmed_cents"] += payout.amount_cents
        elif payout.status == "eligible":
            amounts[payout.currency]["eligible_cents"] += payout.amount_cents

    source_stats: dict[str, list[int]] = defaultdict(list)
    style_stats: dict[str, list[int]] = defaultdict(list)
    for post, views in matured:
        source_stats[post.clip.recording.program.name].append(views)
        style_stats[post.clip.style].append(views)

    def rank(groups):
        return sorted(
            ({"name": name, "posts": len(values), "avg_views72": round(sum(values) / len(values))}
             for name, values in groups.items()),
            key=lambda entry: entry["avg_views72"], reverse=True,
        )[:5]

    mature_count = len(matured)
    k = max(1, ceil(mature_count * 0.1)) if mature_count else 0
    hits_100k = sum(1 for _, views in totals if views >= 100000)
    highest_views = max((v for _, v in totals), default=0)
    checkpoint_due = current >= CHECKPOINT

    recommendation = "Collect results; less than 100 mature posts"
    if mature_count >= 100:
        recommendation = "Review performance by streamer/style; propose one A/B test (owner approval required)"
    if checkpoint_due:
        if hits_100k >= 3 and confirmed_count >= 1:
            recommendation = "CHECKPOINT: scale only with owner approval"
        elif hits_100k >= 1 and confirmed_count == 0:
            recommendation = "CHECKPOINT: switch niche if confirmed payouts are absent; owner decision"
        elif highest_views < 30000:
            recommendation = "CHECKPOINT: stop publishing and archive data, after owner approval"
        else:
            recommendation = "CHECKPOINT: insufficient evidence for automatic choice; owner must decide"

    return {
        "published": len(posts), "tracked": len(totals), "mature_72h": mature_count,
        "views": sum(v for _, v in totals),
        "hits_100k": hits_100k, "highest_views": highest_views,
        "confirmed_payouts": confirmed_count, "payouts_by_currency": dict(amounts),
        "top_3": [{"post_id": p.id, "views": v, "url": p.post_url} for p, v in totals[:3]],
        "top_sources": rank(source_stats), "top_styles": rank(style_stats),
        "best_decile_post_ids": [p.id for p, _ in matured[:k]],
        "worst_decile_post_ids": [p.id for p, _ in matured[-k:]] if k else [],
        "can_auto_tune": False,  # changes to money/safety always require human approval
        "can_propose_tuning": mature_count >= 100,
        "checkpoint_due": checkpoint_due,
        "recommendation": recommendation,
    }


def render_summary(data: dict) -> str:
    top = ", ".join(f"#{x['post_id']} {x['views']:,}" for x in data["top_3"]) or "нет"
    payout = ", ".join(
        f"{currency}: confirmed {values['confirmed_cents']/100:.2f}, "
        f"eligible {values['eligible_cents']/100:.2f}"
        for currency, values in data["payouts_by_currency"].items()
    ) or "нет"
    top_sources = ", ".join(
        f"{s['name']} {s['avg_views72']:,}" for s in data["top_sources"][:3]
    ) or "недостаточно данных"
    return (
        "ClipRush | недельный отчёт\n"
        f"Опубликовано {data['published']}; 72ч+ выборка: {data['mature_72h']}\n"
        f"Просмотры {data['views']:,}; 100k+ клипов: {data['hits_100k']}\n"
        f"Топ-3: {top}\n"
        f"Источники по средним просмотрам за 72ч: {top_sources}\n"
        f"Выплаты: {payout} (eligible ≠ paid)\n"
        f"Вывод: {data['recommendation']}\n"
        "Изменения промптов, аккаунтов, бюджета и стратегии требуют одобрения."
    )


def save_weekly(db: Session) -> WeeklyReport:
    date = now().date().isoformat()
    existing = db.scalar(select(WeeklyReport).where(WeeklyReport.week_key == date))
    if existing:
        return existing
    summary = summarize(db)
    report = WeeklyReport(week_key=date, summary=summary, text=render_summary(summary))
    db.add(report)
    db.commit()
    db.refresh(report)
    return report
