"""Whisper + LLM proposal for 15-45 second moments. Never accepts unverifiable output."""
import json
import os
from dataclasses import dataclass

import httpx

from cliprush.config import get_settings
from cliprush.media import ffprobe_seconds

PROMPT = """Ты монтажный редактор стрим-клипов. Найди ОДИН законченный смешной момент
(фейл, реакция, спор, кринж или шутка) с ясным началом и концом, без кликбейта.
Верни строго JSON: {"score": 0-10, "start": seconds, "end": seconds,
"kind": "fail|reaction|argument|cringe|joke", "hook": "короткий хук"}.
Время в секундах ОТ НАЧАЛА данного отрывка. Продолжительность 15-45 с;
если ничего интересного — score=0. Не цитируй личные данные, рекламу казино и музыку."""


@dataclass(frozen=True)
class Candidate:
    start: float
    end: float
    score: float
    kind: str
    hook: str
    transcript: str


def transcribe(path: str) -> tuple[str, list[dict]]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Install requirements-media.txt for Whisper detection") from exc
    device = os.environ.get("WHISPER_DEVICE", "cpu")
    compute_type = os.environ.get("WHISPER_COMPUTE", "int8" if device == "cpu" else "float16")
    model = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device=device, compute_type=compute_type)
    segments, _ = model.transcribe(path, language="ru", word_timestamps=True, vad_filter=True)
    words = []
    for segment in segments:
        for word in segment.words or ():
            words.append({"start": round(word.start, 2), "end": round(word.end, 2), "text": word.word})
    return " ".join(x["text"] for x in words).strip(), words


def llm_json(prompt: str, payload: str, *, vision_parts: list[dict] | None = None, model: str | None = None) -> dict:
    cfg = get_settings()
    if not cfg.openrouter_api_key or not (model or cfg.openrouter_model):
        raise RuntimeError("OPENROUTER_API_KEY and model are required")
    message_content = ([{"type": "text", "text": payload}] + vision_parts) if vision_parts else payload
    response = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {cfg.openrouter_api_key}"},
        json={"model": model or cfg.openrouter_model,
              "messages": [{"role": "system", "content": prompt},
                           {"role": "user", "content": message_content}],
              "temperature": 0, "response_format": {"type": "json_object"}},
        timeout=cfg.request_timeout_seconds,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"]
    return json.loads(raw)


def score_candidate(path: str, prompt: str | None = None) -> Candidate | None:
    duration = ffprobe_seconds(path)
    transcript, words = transcribe(path)
    if not transcript or len(words) < 4 or duration < 15:
        return None
    output = llm_json(prompt or PROMPT, json.dumps({"duration": duration, "words": words}, ensure_ascii=False))
    try:
        score = float(output["score"])
        start = float(output["start"])
        end = float(output["end"])
        kind = str(output["kind"])[:40]
        hook = str(output["hook"])[:160]
    except (KeyError, ValueError, TypeError):
        return None
    if not 6 <= score <= 10 or start < 0 or end > duration + 0.5 or not 15 <= end - start <= 45:
        return None
    if not hook.strip() or kind not in {"fail", "reaction", "argument", "cringe", "joke"}:
        return None
    return Candidate(start, end, score, kind, hook, transcript)
