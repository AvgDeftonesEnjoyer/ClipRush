"""YouTube Live watcher for an explicitly registered broadcast video ID.

Uses the official API, honors pollingIntervalMillis, never polls random channels
or performs quota-expensive search.list discovery.
"""
import asyncio
import logging
import time

import httpx

from cliprush.config import get_settings
from cliprush.live import RollingCapture, SpikeDetector, emit_live_moment
from cliprush.models import Streamer

log = logging.getLogger(__name__)


class YouTubeLive:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=25)
        self.key = get_settings().youtube_api_key

    def active_chat_id(self, video_id: str) -> str | None:
        if not self.key:
            return None
        response = self.client.get("https://www.googleapis.com/youtube/v3/videos", params={
            "part": "liveStreamingDetails", "id": video_id, "key": self.key,
            "fields": "items(liveStreamingDetails(activeLiveChatId))",
        })
        response.raise_for_status()
        items = response.json().get("items") or []
        return (items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId") if items else None

    def messages(self, chat_id: str, page_token: str | None) -> dict:
        params = {"liveChatId": chat_id, "part": "snippet", "maxResults": 200,
                  "key": self.key}
        if page_token:
            params["pageToken"] = page_token
        response = self.client.get("https://www.googleapis.com/youtube/v3/liveChat/messages", params=params)
        response.raise_for_status()
        return response.json()


async def watch_youtube_streamer(streamer: Streamer, chat_id: str):
    from cliprush.live import RollingCapture, SpikeDetector, emit_live_moment
    source_url = f"https://www.youtube.com/watch?v={streamer.channel}"
    capture = RollingCapture(streamer.channel, streamer.id, source_url=source_url)
    capture.start()
    detector = SpikeDetector()
    youtube = YouTubeLive()
    page_token = None
    pending: set[asyncio.Task] = set()
    try:
        while True:
            payload = await asyncio.to_thread(youtube.messages, chat_id, page_token)
            if payload.get("offlineAt"):
                return
            next_token = payload.get("nextPageToken")
            if page_token is not None:  # first request contains historical chat
                for item in payload.get("items") or []:
                    snippet = item.get("snippet") or {}
                    text = snippet.get("displayMessage") or (
                        snippet.get("textMessageDetails") or {}).get("messageText") or ""
                    if text and detector.record(str(text)):
                        task = asyncio.create_task(emit_live_moment(streamer, capture, time.time()))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
            page_token = next_token or page_token
            if capture.ffmpeg and capture.ffmpeg.poll() is not None:
                return
            delay = max(1.0, float(payload.get("pollingIntervalMillis") or 5000) / 1000)
            await asyncio.sleep(delay)
    finally:
        for task in pending:
            task.cancel()
        capture.stop()
