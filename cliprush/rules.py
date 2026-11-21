"""Program-specific video rules (owner supplied and verified; never guessed)."""
import re

from cliprush.safety import text_flags

PLATFORMS = {"youtube", "tiktok", "instagram"}


def package_title(title: str, rules: dict | None = None) -> str:
    """Append mandatory tags and disclosure while preserving max 150 characters."""
    rules = rules or {}
    tags = rules.get("required_tags") or []
    if not isinstance(tags, list):
        raise ValueError("required_tags must be a list")
    missing = []
    for tag in tags:
        if not isinstance(tag, str) or not re.fullmatch(r"#[\w]{1,45}", tag, flags=re.UNICODE):
            raise ValueError("Invalid hashtag in program")
        if tag.lower() not in title.lower().split():
            missing.append(tag)
    disclosure = rules.get("required_disclosure") or ""
    if not isinstance(disclosure, str) or len(disclosure) > 100:
        raise ValueError("Invalid disclosure")
    suffix = " ".join([*missing, *([disclosure] if disclosure and disclosure.lower() not in title.lower() else [])])
    if len(suffix) >= 150:
        raise ValueError("Mandatory program tags too long")
    width = 150 - (len(suffix) + 1 if suffix else 0)
    cleaned = title.strip()[:width].rstrip()
    return (cleaned + (" " + suffix if suffix else "")).strip()


def rule_error(clip, account=None) -> str | None:
    """Owner-provided program restrictions evaluated before each remote dispatch."""
    rules = clip.recording.program.rules or {}
    if rules.get("automated_clips_allowed") is False:
        return "Program prohibits automated clipping"
    if rules.get("closed") is True:
        return "Program has closed"
    if rules.get("creator_visible_required") is True:
        # Must be verified by humans; a checkbox is a manual attestation.
        if not clip.safety_checked:
            return "Creator visibility not manually checked"
    if account is not None:
        allowed = rules.get("allowed_platforms")
        if allowed is not None and (not isinstance(allowed, list) or account.platform not in allowed):
            return "Platform not allowed by program"
    required = rules.get("required_tags") or []
    if not isinstance(required, list):
        return "Invalid required_tags program configuration"
    if any(not isinstance(tag, str) or tag.lower() not in clip.title.lower().split() for tag in required):
        return "Missing mandatory program hashtag"
    disclosure = rules.get("required_disclosure") or ""
    if disclosure and (not isinstance(disclosure, str) or disclosure.lower() not in clip.title.lower()):
        return "Missing mandatory program disclosure"
    forbidden = rules.get("forbidden_terms") or []
    if not isinstance(forbidden, list):
        return "Invalid forbidden_terms program configuration"
    if any(isinstance(term, str) and term.strip() and term.casefold() in clip.title.casefold() for term in forbidden):
        return "Program-forbidden text in title"
    if text_flags(clip.title):
        return "Title contains potentially restricted content or personal data"
    minimum = rules.get("min_seconds", 15)
    maximum = rules.get("max_seconds", 45)
    try:
        minimum, maximum = float(minimum), float(maximum)
        duration = float(clip.duration_seconds)
    except (ValueError, TypeError):
        return "Missing video duration or invalid program range"
    if minimum < 1 or maximum > 180 or maximum < minimum:
        return "Invalid program video duration range"
    if duration < minimum - 0.5 or duration > maximum + 0.5:
        return "Clip length violates program rules"
    return None
