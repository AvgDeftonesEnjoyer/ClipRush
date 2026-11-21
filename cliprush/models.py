"""Persistent state. New installations use create_all; migrations required on schema upgrades."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cliprush.db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class Program(Base):
    __tablename__ = "programs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    rules_url: Mapped[str] = mapped_column(Text, default="")
    rights_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    payout_method: Mapped[str] = mapped_column(String(40), default="")
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    min_views: Mapped[int] = mapped_column(Integer, default=100000)
    reward_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Streamer(Base):
    __tablename__ = "streamers"
    id: Mapped[int] = mapped_column(primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    platform: Mapped[str] = mapped_column(String(16), default="twitch")
    channel: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("platform", "channel"),)


class Recording(Base):
    __tablename__ = "recordings"
    __table_args__ = (UniqueConstraint("program_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    external_id: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    openshorts_job_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    program: Mapped[Program] = relationship()


class Moment(Base):
    __tablename__ = "moments"
    __table_args__ = (UniqueConstraint("streamer_id", "event_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    event_key: Mapped[str] = mapped_column(String(128))
    source_path: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    score: Mapped[float | None] = mapped_column(Float)
    start_seconds: Mapped[float | None] = mapped_column(Float)
    end_seconds: Mapped[float | None] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(40), default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")


class Clip(Base):
    __tablename__ = "clips"
    __table_args__ = (UniqueConstraint("recording_id", "clip_index"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("recordings.id"))
    clip_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, default="")
    video_url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="needs_review")
    safety_status: Mapped[str] = mapped_column(String(32), default="unknown")
    safety_reasons: Mapped[list] = mapped_column(JSON, default=list)
    safety_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reason: Mapped[str] = mapped_column(Text, default="")
    style: Mapped[str] = mapped_column(String(40), default="classic")
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    publish_response: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    recording: Mapped[Recording] = relationship()


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform", "profile"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    profile: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    warning: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("clip_id", "account_id"), UniqueConstraint("idempotency_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_id: Mapped[str | None] = mapped_column(String(100))
    platform_post_id: Mapped[str | None] = mapped_column(String(160))
    post_url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    account: Mapped[Account] = relationship()
    clip: Mapped[Clip] = relationship()


class Metric(Base):
    __tablename__ = "metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    completion_rate: Mapped[float | None] = mapped_column(Float)
    avg_view_seconds: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    evidence_url: Mapped[str] = mapped_column(Text, default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    module: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("module", "version"),)


class WeeklyReport(Base):
    __tablename__ = "weekly_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_key: Mapped[str] = mapped_column(String(20), unique=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProgramCandidate(Base):
    __tablename__ = "program_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    source_url: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
