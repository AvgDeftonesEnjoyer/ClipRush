"""Conservative bounded retention for files produced by ClipRush.

Never delete active recordings, unapproved videos or uncertain uploads.
This is a local disk policy, not a replacement for encrypted off-site backups.
"""
import re
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cliprush.config import get_settings
from cliprush.models import Clip, Moment, Post, Recording

CACHE_FILE = re.compile(r"clip-([0-9]+)\.mp4$")
TERMINAL_POSTS = {"published", "failed", "blocked"}


def cleanup(db: Session, *, current: float | None = None) -> dict:
    root = Path(get_settings().data_dir).resolve()
    days = max(7, get_settings().media_retention_days)
    now_seconds = time.time() if current is None else current
    cutoff = now_seconds - days * 86400
    stats = {"files_removed": 0, "directories_removed": 0, "bytes_released": 0}

    def aged(path: Path, before: float = cutoff) -> bool:
        try:
            return path.is_file() and path.stat().st_mtime < before and path.resolve().is_relative_to(root)
        except OSError:
            return False

    def remove(path: Path):
        if not aged(path):
            return
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        stats["files_removed"] += 1
        stats["bytes_released"] += size

    cache = root / "cache"
    for path in cache.glob("clip-*.mp4"):
        match = CACHE_FILE.fullmatch(path.name)
        if not match or not aged(path):
            continue
        clip = db.get(Clip, int(match.group(1)))
        if clip is None or clip.status in ("blocked", "rejected"):
            remove(path)
            continue
        posts = list(db.scalars(select(Post.status).where(Post.clip_id == clip.id)))
        if posts and all(status in TERMINAL_POSTS for status in posts):
            remove(path)

    # Orphaned partial downloads can be removed after 24 hours, not 14 days.
    for path in cache.glob("clip-*.part"):
        if aged(path, now_seconds - 86400):
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            stats["files_removed"] += 1
            stats["bytes_released"] += size

    # Candidate raw files are no longer needed after their decision is final.
    candidates = root / "candidates"
    for moment in db.scalars(select(Moment).where(
        Moment.status.in_(("rejected", "selected", "blocked"))
    )):
        path = Path(moment.source_path).resolve()
        if path.parent == candidates and aged(path):
            remove(path)

    # Original selected windows can be removed once upstream processing ended.
    selected = root / "selected"
    for recording in db.scalars(select(Recording).where(
        Recording.status == "completed", Recording.local_path.is_not(None)
    )):
        path = Path(recording.local_path).resolve()
        if path.parent == selected and aged(path):
            remove(path)

    # Failed FFmpeg frame extraction may leave directories behind.
    frames = root / "frames"
    for folder in frames.glob("frames-*"):
        if not folder.is_dir() or folder.is_symlink():
            continue
        if not folder.resolve().is_relative_to(root):
            continue
        try:
            if folder.stat().st_mtime < now_seconds - 86400:
                from shutil import rmtree
                rmtree(folder)
                stats["directories_removed"] += 1
        except OSError:
            continue
    return stats
