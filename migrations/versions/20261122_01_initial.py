"""Initialize ClipRush schema in an EMPTY database.

This is a frozen baseline, independent from later changes to ORM models.
Existing databases created by the old create_all() must be migrated or
explicitly reconciled by an operator after a backup; never stamp blindly.

Revision ID: 20261122_01
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "20261122_01"
down_revision = None
branch_labels = None
depends_on = None

def _id():
    return sa.Column("id", sa.Integer(), primary_key=True)

def _date(name="created_at", nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)

def _txt(name, nullable=False):
    return sa.Column(name, sa.Text(), nullable=nullable)

def _s(name, length, nullable=False):
    return sa.Column(name, sa.String(length), nullable=nullable)

def _bool(name):
    return sa.Column(name, sa.Boolean(), nullable=False)

def _int(name, nullable=False):
    return sa.Column(name, sa.Integer(), nullable=nullable)

def upgrade() -> None:
    op.create_table("programs",
        _id(), _s("name", 160), _txt("rules_url"), _bool("rights_confirmed"), _bool("enabled"),
        _s("currency", 8), _s("payout_method", 40), sa.Column("rules", sa.JSON(), nullable=False),
        _int("min_views"), _int("reward_cents"), _date(),
        sa.UniqueConstraint("name"))
    op.create_table("accounts",
        _id(), _s("platform", 16), _s("profile", 160), _bool("enabled"),
        _bool("paused"), _txt("warning"), _date(),
        sa.UniqueConstraint("platform", "profile"))
    op.create_table("streamers",
        _id(), _int("program_id"), _s("platform", 16), _s("channel", 160),
        _bool("enabled"), _date(),
        sa.ForeignKeyConstraint(["program_id"], ["programs.id"]),
        sa.UniqueConstraint("platform", "channel"))
    op.create_table("recordings",
        _id(), _int("program_id"), _s("external_id", 255), _txt("source_url"),
        _txt("local_path", nullable=True), _s("status", 32),
        _s("openshorts_job_id", 128, nullable=True), _txt("error", nullable=True), _date(),
        sa.ForeignKeyConstraint(["program_id"], ["programs.id"]),
        sa.UniqueConstraint("program_id", "external_id"),
        sa.UniqueConstraint("openshorts_job_id"))
    op.create_table("moments",
        _id(), _int("streamer_id"), _s("event_key", 128),
        _txt("source_path"), _date("occurred_at"), _s("status", 40),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("start_seconds", sa.Float(), nullable=True),
        sa.Column("end_seconds", sa.Float(), nullable=True),
        _s("kind", 40), _txt("hook"), _txt("transcript"), _txt("error"),
        sa.ForeignKeyConstraint(["streamer_id"], ["streamers.id"]),
        sa.UniqueConstraint("streamer_id", "event_key"))
    op.create_table("clips",
        _id(), _int("recording_id"), _int("clip_index"), _txt("title"),
        _txt("video_url"), _s("status", 32), _s("safety_status", 32),
        sa.Column("safety_reasons", sa.JSON(), nullable=False),
        _bool("safety_checked"), _txt("review_reason"), _s("style", 40),
        _s("fingerprint", 64, nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("publish_response", sa.JSON(), nullable=True), _date(),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"]),
        sa.UniqueConstraint("recording_id", "clip_index"))
    op.create_index("ix_clips_fingerprint", "clips", ["fingerprint"])
    op.create_table("posts",
        _id(), _int("clip_id"), _int("account_id"), _s("idempotency_key", 100),
        _s("request_id", 100, nullable=True),
        _s("platform_post_id", 160, nullable=True), _txt("post_url"),
        _s("status", 32), _txt("error"), _date(),
        _date("published_at", nullable=True),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.UniqueConstraint("clip_id", "account_id"),
        sa.UniqueConstraint("idempotency_key"))
    op.create_table("metrics",
        _id(), _int("post_id"), _int("views"), _int("likes"),
        sa.Column("completion_rate", sa.Float(), nullable=True),
        sa.Column("avg_view_seconds", sa.Float(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False), _date("captured_at"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"]))
    op.create_table("payouts",
        _id(), _int("post_id"), _int("amount_cents"), _s("currency", 8),
        _s("status", 32), _txt("evidence_url"),
        _date("confirmed_at", nullable=True),
        _date("notified_at", nullable=True), _date(),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"]))
    op.create_table("prompt_versions",
        _id(), _s("module", 40), _int("version"), _txt("prompt"),
        _bool("active"), _bool("approved"), _bool("rejected"), _date(),
        sa.UniqueConstraint("module", "version"))
    op.create_table("weekly_reports",
        _id(), _s("week_key", 20), sa.Column("summary", sa.JSON(), nullable=False),
        _txt("text"), _date(), sa.UniqueConstraint("week_key"))
    op.create_table("program_candidates",
        _id(), _s("source", 40), _txt("source_url"), _txt("text"),
        _s("fingerprint", 64), _s("status", 32), _date(),
        sa.UniqueConstraint("fingerprint"))
    op.create_table("expenses",
        _id(), _s("vendor", 80), _int("amount_cents"), _txt("note"), _date())

def downgrade() -> None:
    for table in ("expenses", "program_candidates", "weekly_reports", "prompt_versions",
                  "payouts", "metrics", "posts", "clips", "moments", "recordings",
                  "streamers", "accounts", "programs"):
        op.drop_table(table)
