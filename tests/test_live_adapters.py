"""Official Kick and YouTube Live adapter tests with mocked external providers."""
import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cliprush.kick import handle_kick_event, verify_kick_signature
from cliprush.models import Program, Streamer
from cliprush.db import Base
from cliprush.youtube_live import YouTubeLive
from cliprush.config import get_settings


class KeyProvider:
    def __init__(self, key):
        self.key = key

    def __call__(self):
        return self.key

    def cache_clear(self):
        pass


def test_signed_kick_webhook_valid_and_tampered(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr("cliprush.kick.public_key", KeyProvider(private_key.public_key()))
    body = b'{"content":"KEKW"}'
    timestamp = datetime.now(timezone.utc).isoformat()
    message = b"message-id." + timestamp.encode() + b"." + body
    signature = base64.b64encode(private_key.sign(
        message, padding.PKCS1v15(), hashes.SHA256()
    )).decode()
    headers = {
        "kick-event-message-id": "message-id",
        "kick-event-message-timestamp": timestamp,
        "kick-event-signature": signature,
    }
    verify_kick_signature(body, headers)
    with pytest.raises(ValueError):
        verify_kick_signature(body+b"changed", headers)
    with pytest.raises(ValueError):
        verify_kick_signature(body, dict(headers, **{
            "kick-event-message-timestamp": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        }))


class FakeRedis:
    def __init__(self):
        self.seen = set()
        self.live = False
        self.messages = []

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.seen:
            return None
        self.seen.add(key)
        self.live = self.live or key.startswith("kick:live:")
        return True

    def delete(self, key):
        if key.startswith("kick:live:"):
            self.live = False

    def xadd(self, stream, message, **kwargs):
        self.messages.append((stream, message))

    def expire(self, stream, ttl):
        pass


def test_kick_only_accepts_approved_channels_and_deduplicates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        program = Program(name="P", enabled=True, rights_confirmed=True)
        db.add(program)
        db.flush()
        db.add(Streamer(program_id=program.id, channel="creator", platform="kick", enabled=True))
        db.commit()
        redis = FakeRedis()
        headers = {"kick-event-message-id": "event-1", "kick-event-type": "chat.message.sent"}
        event = {"broadcaster": {"channel_slug": "creator"}, "content": "KEKW"}
        assert handle_kick_event(db, event, headers, redis)["accepted"]
        assert handle_kick_event(db, event, headers, redis)["duplicate"]
        assert len(redis.messages) == 1
        assert "kick:chat:creator" == redis.messages[0][0]
        event["broadcaster"]["channel_slug"] = "not-approved"
        assert handle_kick_event(db, event, dict(headers, **{
            "kick-event-message-id": "event-2",
        }), redis)["ignored"]
    engine.dispose()


def test_youtube_adapter_honors_page_tokens(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "youtube_api_key", "fake-key")
    requested = []

    def handler(req):
        requested.append((req.url.path, dict(req.url.params)))
        if req.url.path.endswith("/videos"):
            return httpx.Response(200, json={"items": [{
                "liveStreamingDetails": {"activeLiveChatId": "live-chat-A"}
            }]})
        return httpx.Response(200, json={
            "nextPageToken": "next-page", "pollingIntervalMillis": 7000,
            "items": [{"snippet": {"displayMessage": "KEKW"}}],
        })
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        yt = YouTubeLive(client)
        assert yt.active_chat_id("abcdefghijk") == "live-chat-A"
        result = yt.messages("live-chat-A", "page-1")
    assert result["pollingIntervalMillis"] == 7000
    assert requested[-1][1]["pageToken"] == "page-1"
