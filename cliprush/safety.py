"""Fail-closed automatic preflight. It does not certify that a video is lawful.

Always require owner approval and a real review of the source before publication.
Missing provider keys, uncertain models and absent audio references are warnings.
"""
import base64
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from cliprush.config import get_settings
from cliprush.detect import llm_json, transcribe
from cliprush.media import frame_samples

BANNED_WORDS = re.compile(
    r"(?i)\b(?:казино|слоты|промокод|депозит|букмекер|ставки\s+на\s+спорт|casino|slots|free\s+spins|gambling)\b"
)
EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s\-()]{9,}\d)(?!\d)")
VISION_PROMPT = """Ты модератор коротких видео. Не делай догадок при плохом изображении.
Проверь каждый кадр на казино (слоты, бренды, купоны, ссылки), кровь/насилие,
наготу, личные данные (телефоны, документы, адреса) и политическую агитацию.
Верни JSON {"casino":bool,"violence":bool,"nudity":bool,"pii":bool,
"politics":bool,"uncertain":bool}. Не описывай увиденные данные."""
TEXT_PROMPT = """Ты модератор русскоязычных видео. Проверь текст на рекламу азартных игр,
разжигание ненависти по защищённым признакам, угрозы, травлю и политическую агитацию.
Верни JSON {"gambling":bool,"hate":bool,"harassment":bool,"politics":bool,
"uncertain":bool}. Ошибку считай uncertain=true."""


@dataclass(frozen=True)
class SafetyResult:
    status: str
    reasons: list[str]


def text_flags(text: str) -> list[str]:
    reasons = []
    if BANNED_WORDS.search(text):
        reasons.append("Возможные упоминания казино/ставок")
    if EMAIL.search(text) or PHONE.search(text):
        reasons.append("Возможные личные данные")
    return reasons


def vision_flags(frames: list[str]) -> tuple[list[str], bool]:
    cfg = get_settings()
    if not cfg.openrouter_api_key or not cfg.openrouter_vision_model:
        return [], False
    findings = []
    complete = True
    for start in range(0, len(frames), 4):
        parts = []
        for path in frames[start:start + 4]:
            data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data}})
        try:
            result = llm_json(VISION_PROMPT, f"Кадры {start + 1}–{start + len(parts)}", vision_parts=parts,
                              model=cfg.openrouter_vision_model)
            for key in ("casino", "violence", "nudity", "pii", "politics"):
                if result.get(key):
                    findings.append("Видео: " + key)
            if result.get("uncertain", True):
                complete = False
        except Exception:
            complete = False
    return sorted(set(findings)), complete


def ocr_flags(frames: list[str]) -> tuple[list[str], bool]:
    found = []
    complete = True
    for path in frames:
        try:
            proc = subprocess.run(["tesseract", path, "stdout", "-l", "eng"],
                                  capture_output=True, text=True, check=True, timeout=20)
            found.extend(text_flags(proc.stdout))
        except (OSError, subprocess.SubprocessError):
            complete = False
    return sorted(set(found)), complete


def music_flags(path: str) -> tuple[list[str], bool]:
    cfg = get_settings()
    if not cfg.acoustid_api_key:
        return [], False
    try:
        output = subprocess.run(["fpcalc", "-json", "-length", "45", path],
                                capture_output=True, check=True, text=True, timeout=80)
        fingerprint = json.loads(output.stdout)
        resp = httpx.post(
            "https://api.acoustid.org/v2/lookup",
            data={"client": cfg.acoustid_api_key, "meta": "recordings",
                  "duration": str(fingerprint["duration"]), "fingerprint": fingerprint["fingerprint"]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        matched = any(float(x.get("score", 0)) > 0.75 for x in data.get("results", []))
        if matched:
            return ["Совпадение с музыкальной записью (возможные авторские права)"], True
        # No match does NOT mean music is absent or license is clear.
        return [], False
    except (OSError, ValueError, KeyError, httpx.HTTPError, subprocess.SubprocessError):
        return [], False


def inspect(path: str) -> SafetyResult:
    reasons, unchecked = [], []
    cfg = get_settings()
    transcript = ""
    try:
        transcript, _ = transcribe(path)
        reasons.extend(text_flags(transcript))
    except Exception:
        unchecked.append("Не удалось проверить речь")
    if transcript and cfg.openrouter_api_key and cfg.openrouter_model:
        try:
            answer = llm_json(TEXT_PROMPT, transcript[:18000])
            reasons.extend("Речь: " + k for k in ("gambling", "hate", "harassment", "politics") if answer.get(k))
            if answer.get("uncertain", True):
                unchecked.append("Неопределённый результат текстовой модерации")
        except Exception:
            unchecked.append("Нет результата LLM-модерации речи")
    else:
        unchecked.append("Нет LLM-модерации речи")

    frames = []
    try:
        frames = frame_samples(path)
        if not frames:
            unchecked.append("Нет кадров для проверки")
    except Exception:
        unchecked.append("Не удалось извлечь кадры")
    if frames:
        from shutil import rmtree
        try:
            flagged, checked = vision_flags(frames)
            reasons.extend(flagged)
            if not checked:
                unchecked.append("Vision-проверка недоступна или неуверенна")
            flagged, checked = ocr_flags(frames)
            reasons.extend(flagged)
            if not checked:
                unchecked.append("OCR-проверка неполная")
        finally:
            rmtree(str(Path(frames[0]).parent), ignore_errors=True)
    flagged, checked = music_flags(path)
    reasons.extend(flagged)
    if not checked:
        unchecked.append("Нельзя исключить лицензированную музыку")
    if reasons:
        return SafetyResult("blocked", sorted(set(reasons + unchecked)))
    return SafetyResult("review", sorted(set(unchecked)) or ["Проверить разрешение на публикацию и правила программы"])
