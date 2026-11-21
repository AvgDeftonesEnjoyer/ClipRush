"""Authorized Twitch, Kick and YouTube live-scout integration."""
import asyncio
import logging
import os
import re
import subprocess
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets
from sqlalchemy import select

from cliprush.config import get_settings
from cliprush.db import SessionLocal, init_db
from cliprush.models import Moment, Program, Streamer
from cliprush.tasks import detect_moment_task

log = logging.getLogger(__name__)
EMOTES = re.compile(r"(?i)\b(?:KEKW|LUL|OMEGALUL|ахах[аха]*|лол|WTF|вау)\b")
IRC_MESSAGE = re.compile(r"^:[^ ]+ PRIVMSG #[^ ]+ :(.*)$")


class SpikeDetector:
    def __init__(self, threshold: float = 3.0, cooldown: float = 90):
        self.events: deque[tuple[float, bool]] = deque()
        self.threshold = threshold
        self.cooldown = cooldown
        self.last_trigger = float("-inf")

    def record(self, message: str, now_seconds: float | None = None) -> bool:
        now = time.monotonic() if now_seconds is None else now_seconds
        while self.events and now - self.events[0][0] > 130:
            self.events.popleft()
        recent = sum(1 for t, _ in self.events if now - t <= 10)
        baseline = sum(1 for t, _ in self.events if 10 < now - t <= 130) / 12
        emote_recent = sum(1 for t, e in self.events if now - t <= 10 and e)
        emote = bool(EMOTES.search(message))
        self.events.append((now, emote))
        if now - self.last_trigger < self.cooldown:
            return False
        spike = recent + 1 >= max(8, baseline * self.threshold) and baseline >= 2
        reaction = emote_recent + (1 if emote else 0) >= 6
        if spike or reaction:
            self.last_trigger = now
            return True
        return False


class Twitch:
    def __init__(self):
        self.settings = get_settings()
        self.token = ""
        self.expires_at = 0.0

    def access_token(self) -> str:
        if time.time() >= self.expires_at:
            cfg = self.settings
            response = httpx.post("https://id.twitch.tv/oauth2/token",
                                  data={"client_id": cfg.twitch_client_id,
                                        "client_secret": cfg.twitch_client_secret,
                                        "grant_type": "client_credentials"}, timeout=20)
            response.raise_for_status()
            data = response.json()
            self.token = data["access_token"]
            self.expires_at = time.time() + int(data.get("expires_in", 3600)) - 60
        return self.token

    def is_live(self, channel: str) -> bool:
        response = httpx.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_login": channel},
            headers={"Client-Id": self.settings.twitch_client_id,
                     "Authorization": "Bearer " + self.access_token()},
            timeout=20,
        )
        response.raise_for_status()
        return bool(response.json().get("data"))


class RollingCapture:
    def __init__(self, channel: str, streamer_id: int, source_url: str | None = None):
        self.channel = channel
        self.source_url = source_url or f"https://twitch.tv/{channel}"
        self.folder = Path(get_settings().data_dir) / "live" / str(streamer_id)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.streamlink: subprocess.Popen | None = None
        self.ffmpeg: subprocess.Popen | None = None

    def start(self):
        self.streamlink = subprocess.Popen(
            ["streamlink", "--stdout", self.source_url, "best"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.ffmpeg = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
             "-i", "pipe:0", "-c", "copy", "-f", "segment", "-segment_time", "5",
             "-segment_wrap", "160", "-reset_timestamps", "1",
             str(self.folder / "buffer-%03d.ts")],
            stdin=self.streamlink.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.streamlink.stdout.close()

    def stop(self):
        for proc in (self.ffmpeg, self.streamlink):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def extract(self, timestamp: float) -> str | None:
        """Find completed chunks by modification time; late segments are excluded."""
        files = sorted(self.folder.glob("buffer-*.ts"), key=lambda p: p.stat().st_mtime)
        selected = [p for p in files if timestamp - 66 <= p.stat().st_mtime <= timestamp + 32]
        if len(selected) < 4:
            return None
        dest_dir = Path(get_settings().data_dir) / "candidates"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = str(dest_dir / f"live-{self.channel}-{int(timestamp)}.mp4")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", dir=dest_dir, delete=False) as listing:
            list_path = listing.name
            for path in selected:
                listing.write("file '" + str(path.resolve()).replace("'", "'\\''") + "'\n")
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                            "-f", "concat", "-safe", "0", "-i", list_path,
                            "-c:v", "libx264", "-preset", "veryfast",
                            "-c:a", "aac", "-movflags", "+faststart", dest],
                           check=True, timeout=240)
            return dest
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("Failed to extract Twitch buffer: %s", error)
            return None
        finally:
            Path(list_path).unlink(missing_ok=True)


async def emit_live_moment(streamer: Streamer, capture: RollingCapture, when: float):
    """Persist one candidate after the post-spike buffer has been recorded."""
    await asyncio.sleep(32)
    path = await asyncio.to_thread(capture.extract, when)
    if path is None:
        return
    key = f"{streamer.platform}-{streamer.id}-{int(when)}"
    with SessionLocal() as db:
        if db.scalar(select(Moment.id).where(Moment.streamer_id == streamer.id, Moment.event_key == key)):
            return
        moment = Moment(streamer_id=streamer.id, event_key=key,
                        source_path=path, occurred_at=datetime.fromtimestamp(when, timezone.utc))
        db.add(moment)
        db.commit()
        db.refresh(moment)
        detect_moment_task.delay(moment.id)


async def watch_kick_streamer(streamer: Streamer):
    """Consume authenticated Kick webhook chat events from Redis."""
    from redis.asyncio import Redis as AsyncRedis
    capture = RollingCapture(streamer.channel, streamer.id,
                             source_url=f"https://kick.com/{streamer.channel}")
    detector = SpikeDetector()
    pending: set[asyncio.Task] = set()
    redis_client = AsyncRedis.from_url(get_settings().redis_url, decode_responses=True)
    capture.start()
    try:
        last_id = "$"  # ignore event backlog from before video capture started
        while True:
            if capture.ffmpeg and capture.ffmpeg.poll() is not None:
                return
            if not await redis_client.exists("kick:live:" + streamer.channel):
                return
            entries = await redis_client.xread({"kick:chat:" + streamer.channel: last_id},
                                               block=1000, count=100)
            for _, messages in entries:
                for stream_id, fields in messages:
                    last_id = stream_id
                    if detector.record(str(fields.get("content") or "")):
                        task = asyncio.create_task(emit_live_moment(streamer, capture, time.time()))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
    finally:
        for task in pending:
            task.cancel()
        await redis_client.aclose()
        capture.stop()


async def watch_streamer(streamer: Streamer):
    settings = get_settings()
    capture = RollingCapture(streamer.channel, streamer.id)
    detector = SpikeDetector()
    capture.start()
    pending: set[asyncio.Task] = set()

    async def capture_spike(when: float):
        await emit_live_moment(streamer, capture, when)

    try:
        while True:
            async with websockets.connect("wss://irc-ws.chat.twitch.tv:443", ping_interval=25) as ws:
                await ws.send("PASS " + settings.twitch_bot_oauth)
                await ws.send("NICK " + settings.twitch_bot_nick)
                await ws.send("JOIN #" + streamer.channel.lower())
                async for data in ws:
                    if data.startswith("PING"):
                        await ws.send(data.replace("PING", "PONG", 1))
                        continue
                    for line in data.split("\r\n"):
                        match = IRC_MESSAGE.match(line)
                        if match and detector.record(match.group(1)):
                            t = asyncio.create_task(capture_spike(time.time()))
                            pending.add(t)
                            t.add_done_callback(pending.discard)
    finally:
        for task in pending:
            task.cancel()
        capture.stop()


async def main():
    from redis.asyncio import Redis as AsyncRedis
    from cliprush.youtube_live import YouTubeLive, watch_youtube_streamer

    init_db()
    cfg = get_settings()
    twitch_ready = all([cfg.twitch_client_id, cfg.twitch_client_secret,
                        cfg.twitch_bot_oauth, cfg.twitch_bot_nick])
    twitch = Twitch() if twitch_ready else None
    youtube = YouTubeLive() if cfg.youtube_api_key else None
    redis_client = AsyncRedis.from_url(cfg.redis_url, decode_responses=True)
    watches: dict[int, asyncio.Task] = {}
    while True:
        try:
            with SessionLocal() as db:
                streamers = db.scalars(select(Streamer).where(Streamer.enabled.is_(True))).all()
                active = [(x.id, x.program_id, x.channel, x.platform) for x in streamers]
                enabled_programs = {x.id for x in db.scalars(select(Program).where(
                    Program.enabled.is_(True), Program.rights_confirmed.is_(True)))}
            active_ids = set()
            for sid, program_id, channel, platform in active:
                if program_id not in enabled_programs:
                    continue
                chat_id = None
                if platform == "twitch" and twitch:
                    is_live = await asyncio.to_thread(twitch.is_live, channel)
                elif platform == "youtube" and youtube:
                    chat_id = await asyncio.to_thread(youtube.active_chat_id, channel)
                    is_live = bool(chat_id)
                elif platform == "kick" and cfg.kick_webhooks_enabled:
                    is_live = bool(await redis_client.exists("kick:live:" + channel))
                else:
                    continue
                if is_live:
                    active_ids.add(sid)
                    if sid not in watches or watches[sid].done():
                        streamer = Streamer(id=sid, program_id=program_id,
                                            channel=channel, platform=platform)
                        if platform == "twitch":
                            watches[sid] = asyncio.create_task(watch_streamer(streamer))
                        elif platform == "kick":
                            watches[sid] = asyncio.create_task(watch_kick_streamer(streamer))
                        else:
                            watches[sid] = asyncio.create_task(watch_youtube_streamer(streamer, chat_id))
            for sid, task in list(watches.items()):
                if sid not in active_ids:
                    task.cancel()
                    watches.pop(sid)
        except Exception:
            log.exception("Scout polling failed; will retry")
        await asyncio.sleep(max(30, cfg.scout_interval_seconds))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
