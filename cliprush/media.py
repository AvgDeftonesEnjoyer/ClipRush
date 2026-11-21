"""Trusted media operations: no shell interpolation and no arbitrary URL downloads."""
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from cliprush.config import get_settings


def ffprobe_seconds(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return float(result.stdout.strip())


def trim_video(source: str, dest: str, start: float, length: float) -> str:
    if start < 0 or length <= 0 or length > 180 or not Path(source).is_file():
        raise ValueError("Invalid media clipping parameters")
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
         "-ss", str(start), "-i", source, "-t", str(length),
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
         "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
         "-movflags", "+faststart", dest],
        check=True, timeout=300,
    )
    return dest


def frame_samples(source: str, every_seconds: int = 1, max_frames: int = 45) -> list[str]:
    root = Path(get_settings().data_dir) / "frames"
    root.mkdir(parents=True, exist_ok=True)
    folder = tempfile.mkdtemp(prefix="frames-", dir=root)
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-i", source, "-vf", f"fps=1/{every_seconds}",
            "-frames:v", str(max_frames), "-q:v", "7", str(Path(folder) / "%04d.jpg")
        ], check=True, timeout=180)
        return [str(x) for x in sorted(Path(folder).glob("*.jpg"))]
    except Exception:
        from shutil import rmtree
        rmtree(folder, ignore_errors=True)
        raise


def upstream_url(video_url: str) -> str:
    """Only access media from configured OpenShorts origin, no redirects."""
    base = get_settings().openshorts_api_url.rstrip("/") + "/"
    origin = urlparse(base)
    video = urlparse(video_url)
    if not video_url or video.username or video.password or video.fragment:
        raise ValueError("Unsafe media URL")
    if not video.scheme:
        if not video_url.startswith("/") or video_url.startswith("//"):
            raise ValueError("Expected absolute path under OpenShorts")
        target = urljoin(base, video_url)
    else:
        target = video_url
    parsed = urlparse(target)
    if parsed.scheme != origin.scheme or parsed.netloc != origin.netloc:
        raise ValueError("Media URL is not on configured OpenShorts origin")
    return target


def fetch_clip(video_url: str, clip_id: int) -> str:
    settings = get_settings()
    endpoint = upstream_url(video_url)
    dest = Path(settings.data_dir) / "cache" / f"clip-{clip_id}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = ({"Authorization": f"Bearer {settings.openshorts_api_key}"}
               if settings.openshorts_api_key else {})
    if dest.is_file() and 0 < dest.stat().st_size <= settings.max_download_bytes:
        return str(dest)
    fd, tmp_name = tempfile.mkstemp(prefix=f"clip-{clip_id}-", suffix=".part", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with httpx.Client(timeout=120, follow_redirects=False) as client:
            with client.stream("GET", endpoint, headers=headers) as response:
                response.raise_for_status()
                if response.status_code in (301, 302, 303, 307, 308):
                    raise ValueError("Media redirects are not allowed")
                total = 0
                with tmp.open("wb") as out:
                    for chunk in response.iter_bytes(chunk_size=1 << 20):
                        total += len(chunk)
                        if total > settings.max_download_bytes:
                            raise ValueError("Video exceeds maximum download size")
                        out.write(chunk)
        if not tmp.stat().st_size:
            raise ValueError("Empty video file")
        os.replace(tmp, dest)
        return str(dest)
    finally:
        tmp.unlink(missing_ok=True)


def visual_fingerprint(path: str) -> str:
    """A coarse multi-frame fingerprint for duplicate review (not copyright proof)."""
    import hashlib
    try:
        raw = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
             "-i", path, "-vf", "fps=1/5,scale=16:16,format=gray",
             "-frames:v", "3", "-f", "rawvideo", "-"],
            capture_output=True, check=True, timeout=80,
        ).stdout
        if len(raw) < 256:
            raise ValueError("No video frames")
        return hashlib.sha256(raw).hexdigest()
    except (OSError, subprocess.SubprocessError, ValueError):
        with open(path, "rb") as source:
            return hashlib.sha256(source.read(5 * 1024 * 1024)).hexdigest()


def invalidate_cached_clip(clip_id: int) -> None:
    """Invalidate media when OpenShorts re-renders a clip at the same URL."""
    root = (Path(get_settings().data_dir) / "cache").resolve()
    target = (root / f"clip-{int(clip_id)}.mp4").resolve()
    if target.parent != root:
        raise ValueError("Unsafe cache target")
    target.unlink(missing_ok=True)
