import os
import re
import time
import uuid
import hmac
import shutil
import subprocess
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse

import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse

APP_NAME = "FAZ Audio Worker"
APP_VERSION = "2.1.0"
ROOT = Path(os.getenv("FAZ_WORKER_TMP", "/tmp/faz-dj-worker"))
ROOT.mkdir(parents=True, exist_ok=True)

MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
TARGET_FINAL_SECONDS = 418.0
HARD_FINAL_SECONDS = 419.0
ALLOWED_SPEEDS = {0.50, 0.75, 0.90, 1.00, 1.10, 1.25, 1.50, 2.00}
ALLOWED_BITRATES = {64, 96, 128, 160, 192}
TTL_SECONDS = max(900, int(os.getenv("JOB_TTL_SECONDS", "3600")))
MAX_CONCURRENT = max(1, min(4, int(os.getenv("MAX_CONCURRENT_JOBS", "2"))))
WORKER_KEY = (os.getenv("FAZ_WORKER_KEY") or os.getenv("WORKER_API_KEY") or "").strip()
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}

app = FastAPI(title=APP_NAME, version=APP_VERSION)
jobs = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)


def now_ts() -> int:
    return int(time.time())


def set_job(job_id: str, **values):
    with jobs_lock:
        job = jobs.setdefault(job_id, {})
        job.update(values)
        job["updated_at"] = now_ts()


def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        return dict(job) if job else None


def public_job(job: dict) -> dict:
    if not job:
        return {}
    keys = ["id", "status", "progress", "message", "source_type", "source_title", "source_duration_seconds", "duration_seconds", "output_size_bytes", "trim_mode", "trim_applied", "trim_start_seconds", "trim_end_seconds", "speed", "bitrate", "roblox_safe", "safe_duration_seconds", "worker_version", "created_at", "updated_at"]
    return {k: job.get(k) for k in keys if k in job}


def require_key(value: Optional[str]):
    if not WORKER_KEY:
        raise HTTPException(status_code=503, detail="Worker API key is not configured")
    if not value or not hmac.compare_digest(value.strip(), WORKER_KEY):
        raise HTTPException(status_code=401, detail="Invalid worker API key")


def validate_job_options(speed: float, bitrate: int, trim_mode: str, trim_end: Optional[str]):
    speed = round(float(speed), 2)
    if speed not in ALLOWED_SPEEDS:
        raise HTTPException(status_code=400, detail="Unsupported speed")
    if bitrate not in ALLOWED_BITRATES:
        raise HTTPException(status_code=400, detail="Unsupported bitrate")
    if trim_mode not in {"auto", "manual", "none"}:
        trim_mode = "auto"
    parsed_end = None
    if trim_end not in (None, ""):
        try:
            parsed_end = max(0.0, float(trim_end))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid trim_end")
    return speed, bitrate, trim_mode, parsed_end


def validate_youtube_url(url: str) -> str:
    value = (url or "").strip()
    if not value or len(value) > 2048:
        raise HTTPException(status_code=400, detail="YouTube URL is empty or too long")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host not in YOUTUBE_HOSTS:
        raise HTTPException(status_code=400, detail="Only HTTPS YouTube / youtu.be URLs are supported")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    return value


def run_ffmpeg(args, allow_failure=False):
    result = subprocess.run([FFMPEG] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600, check=False)
    if result.returncode != 0 and not allow_failure:
        err = (result.stderr or result.stdout or "FFmpeg command failed").strip()
        raise RuntimeError(err[-2500:])
    return result


def probe(path: Path) -> dict:
    result = run_ffmpeg(["-hide_banner", "-i", str(path)], allow_failure=True)
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        raise RuntimeError("FFmpeg could not determine audio duration")
    duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    if duration <= 0:
        raise RuntimeError("Invalid audio duration")
    if not re.search(r"Stream #.*Audio:", text):
        raise RuntimeError("Source does not contain an audio stream")
    return {"duration": duration, "size": path.stat().st_size}


def safe_filename(name: str) -> str:
    name = Path(name or "source.bin").name
    cleaned = "".join(c for c in name if c.isalnum() or c in "._-")
    return cleaned[:100] or "source.bin"


def cleanup_old_jobs():
    cutoff = now_ts() - TTL_SECONDS
    stale = []
    with jobs_lock:
        for job_id, job in jobs.items():
            if int(job.get("updated_at") or 0) < cutoff:
                stale.append(job_id)
    for job_id in stale:
        with jobs_lock:
            jobs.pop(job_id, None)
        shutil.rmtree(ROOT / job_id, ignore_errors=True)


def atempo_filter(speed: float) -> str:
    return f"atempo={speed:.6f}"


def download_youtube_audio(job_id: str, url: str) -> Path:
    job_dir = ROOT / job_id
    output_template = str(job_dir / "youtube-source.%(ext)s")
    set_job(job_id, status="processing", progress=4, message="Reading YouTube video information")

    def hook(data):
        status = data.get("status")
        if status == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            pct = min(18, max(5, 5 + int((downloaded / total) * 13))) if total > 0 else 8
            set_job(job_id, progress=pct, message="Downloading YouTube audio")
        elif status == "finished":
            set_job(job_id, progress=19, message="YouTube audio downloaded • preparing conversion")

    ydl_opts = {
        "format": "bestaudio[abr<=192]/bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        "max_filesize": MAX_SOURCE_BYTES,
        "progress_hooks": [hook],
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise RuntimeError("YouTube returned no media information")
            if info.get("_type") == "playlist":
                entries = [x for x in (info.get("entries") or []) if x]
                if not entries:
                    raise RuntimeError("Playlist has no playable video")
                info = entries[0]
            if info.get("is_live"):
                raise RuntimeError("YouTube live streams are not supported")
            title = str(info.get("title") or "YouTube Audio").strip()[:150]
            set_job(job_id, source_title=title)
            candidate = None
            requested = info.get("requested_downloads") or []
            if requested and isinstance(requested[0], dict):
                candidate = requested[0].get("filepath")
            if not candidate:
                candidate = ydl.prepare_filename(info)
        source = Path(candidate) if candidate else None
        if not source or not source.is_file():
            candidates = [p for p in job_dir.glob("youtube-source.*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
            source = max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None
        if not source or not source.is_file():
            raise RuntimeError("YouTube audio file was not created")
        size = source.stat().st_size
        if size <= 0:
            raise RuntimeError("YouTube audio file is empty")
        if size > MAX_SOURCE_BYTES:
            raise RuntimeError("YouTube source exceeds 100 MB")
        set_job(job_id, source_path=str(source))
        return source
    except yt_dlp.utils.DownloadError as exc:
        text = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
        raise RuntimeError("YouTube download failed: " + text[-1200:]) from exc


def process_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return
    job_dir = ROOT / job_id
    output = job_dir / "result.mp3"
    safe_output = job_dir / "result-safe.mp3"
    try:
        if job.get("source_type") == "youtube":
            source = download_youtube_audio(job_id, str(job.get("source_url") or ""))
            job = get_job(job_id) or job
        else:
            source = Path(job["source_path"])
        set_job(job_id, status="processing", progress=20, message="Inspecting source duration")
        src = probe(source)
        source_duration = float(src["duration"])
        speed = float(job["speed"])
        bitrate = int(job["bitrate"])
        trim_mode = str(job["trim_mode"])
        trim_start = max(0.0, float(job.get("trim_start") or 0.0))
        requested_end = job.get("trim_end")
        requested_end = float(requested_end) if requested_end not in (None, "") else None
        if trim_start >= source_duration:
            raise RuntimeError("Trim start is after the end of the source")
        source_remaining = source_duration - trim_start
        max_source_for_speed = TARGET_FINAL_SECONDS * speed
        if trim_mode == "manual":
            if requested_end is None or requested_end <= trim_start:
                raise RuntimeError("Manual trim requires an end time after the start time")
            desired_source_len = min(requested_end, source_duration) - trim_start
        elif trim_mode == "none":
            desired_source_len = source_remaining
            predicted = desired_source_len / speed
            if predicted > HARD_FINAL_SECONDS:
                raise RuntimeError(f"No Trim selected but final audio would be about {predicted:.2f}s. Roblox-safe maximum is {HARD_FINAL_SECONDS:.1f}s.")
        else:
            trim_mode = "auto"
            desired_source_len = source_remaining
        clip_source_len = desired_source_len
        safety_clamped = False
        if trim_mode in ("auto", "manual") and clip_source_len > max_source_for_speed:
            clip_source_len = max_source_for_speed
            safety_clamped = True
        if clip_source_len <= 0:
            raise RuntimeError("Selected audio section is empty")
        effective_end = min(source_duration, trim_start + clip_source_len)
        trim_applied = trim_start > 0.001 or effective_end < source_duration - 0.05 or safety_clamped
        set_job(job_id, progress=28, message=f"Converting source {source_duration:.2f}s • final target <= {TARGET_FINAL_SECONDS:.1f}s", source_duration_seconds=round(source_duration, 3), trim_mode=trim_mode, trim_applied=trim_applied, trim_start_seconds=round(trim_start, 3), trim_end_seconds=round(effective_end, 3))
        run_ffmpeg(["-y", "-hide_banner", "-loglevel", "error", "-ss", f"{trim_start:.6f}", "-t", f"{clip_source_len:.6f}", "-i", str(source), "-vn", "-map_metadata", "-1", "-af", atempo_filter(speed), "-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", str(output)])
        set_job(job_id, progress=82, message="Validating FINAL encoded MP3")
        final = probe(output)
        final_duration = float(final["duration"])
        if final_duration > HARD_FINAL_SECONDS:
            set_job(job_id, progress=88, message=f"Final was {final_duration:.2f}s • enforcing safe 6:58 output")
            run_ffmpeg(["-y", "-hide_banner", "-loglevel", "error", "-i", str(output), "-t", f"{TARGET_FINAL_SECONDS:.3f}", "-vn", "-map_metadata", "-1", "-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", str(safe_output)])
            safe_output.replace(output)
            final = probe(output)
            final_duration = float(final["duration"])
            trim_applied = True
        final_size = int(output.stat().st_size)
        if final_duration > HARD_FINAL_SECONDS:
            raise RuntimeError(f"Final validation failed: {final_duration:.3f}s exceeds safe {HARD_FINAL_SECONDS:.1f}s limit")
        if final_size <= 0 or final_size >= MAX_OUTPUT_BYTES:
            raise RuntimeError(f"Final validation failed: output size {final_size} bytes is invalid or >= 20 MB")
        set_job(job_id, status="completed", progress=100, message=f"READY • final {final_duration:.2f}s • {final_size / 1024 / 1024:.2f} MB", duration_seconds=round(final_duration, 3), output_size_bytes=final_size, trim_applied=trim_applied, roblox_safe=True, safe_duration_seconds=HARD_FINAL_SECONDS, output_path=str(output))
    except Exception as exc:
        set_job(job_id, status="failed", progress=100, message=str(exc)[:1500], roblox_safe=False, safe_duration_seconds=HARD_FINAL_SECONDS)


@app.get("/")
def root():
    return {"ok": True, "service": APP_NAME, "version": APP_VERSION, "message": "FAZ DJ Audio Worker online"}


@app.get("/health")
def health():
    cleanup_old_jobs()
    return {"ok": bool(FFMPEG and Path(FFMPEG).exists()), "service": APP_NAME, "version": APP_VERSION, "ffmpeg": bool(FFMPEG and Path(FFMPEG).exists()), "ffprobe": "ffmpeg-metadata-parser", "youtube_import": True, "yt_dlp_version": getattr(yt_dlp.version, "__version__", "unknown"), "speed_aware_trim": True, "final_ffprobe_validation": True, "final_duration_validation": True, "target_final_duration_seconds": TARGET_FINAL_SECONDS, "safe_final_duration_seconds": HARD_FINAL_SECONDS, "max_output_bytes": MAX_OUTPUT_BYTES}


@app.post("/jobs")
async def create_job(file: UploadFile = File(...), speed: float = Form(1.0), bitrate: int = Form(128), trim_mode: str = Form("auto"), trim_start: float = Form(0.0), trim_end: Optional[str] = Form(None), x_faz_worker_key: Optional[str] = Header(None, alias="X-FAZ-WORKER-KEY")):
    require_key(x_faz_worker_key)
    cleanup_old_jobs()
    speed, bitrate, trim_mode, parsed_end = validate_job_options(speed, bitrate, trim_mode, trim_end)
    job_id = uuid.uuid4().hex
    job_dir = ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    source = job_dir / safe_filename(file.filename or "source.bin")
    total = 0
    try:
        with source.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise HTTPException(status_code=413, detail="Source exceeds 100 MB")
                fh.write(chunk)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()
    if total <= 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Empty source file")
    job = {"id": job_id, "status": "queued", "progress": 1, "message": "Queued for FFmpeg", "source_type": "upload", "source_path": str(source), "output_path": None, "speed": speed, "bitrate": bitrate, "trim_mode": trim_mode, "trim_start": max(0.0, float(trim_start)), "trim_end": parsed_end, "trim_applied": False, "roblox_safe": False, "safe_duration_seconds": HARD_FINAL_SECONDS, "worker_version": APP_VERSION, "created_at": now_ts(), "updated_at": now_ts()}
    with jobs_lock:
        jobs[job_id] = job
    executor.submit(process_job, job_id)
    return public_job(job)


@app.post("/jobs/youtube")
def create_youtube_job(url: str = Form(...), speed: float = Form(1.0), bitrate: int = Form(128), trim_mode: str = Form("auto"), trim_start: float = Form(0.0), trim_end: Optional[str] = Form(None), x_faz_worker_key: Optional[str] = Header(None, alias="X-FAZ-WORKER-KEY")):
    require_key(x_faz_worker_key)
    cleanup_old_jobs()
    youtube_url = validate_youtube_url(url)
    speed, bitrate, trim_mode, parsed_end = validate_job_options(speed, bitrate, trim_mode, trim_end)
    job_id = uuid.uuid4().hex
    job_dir = ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job = {"id": job_id, "status": "queued", "progress": 1, "message": "Queued • waiting to fetch YouTube audio", "source_type": "youtube", "source_url": youtube_url, "source_path": None, "output_path": None, "speed": speed, "bitrate": bitrate, "trim_mode": trim_mode, "trim_start": max(0.0, float(trim_start)), "trim_end": parsed_end, "trim_applied": False, "roblox_safe": False, "safe_duration_seconds": HARD_FINAL_SECONDS, "worker_version": APP_VERSION, "created_at": now_ts(), "updated_at": now_ts()}
    with jobs_lock:
        jobs[job_id] = job
    executor.submit(process_job, job_id)
    return public_job(job)


@app.get("/jobs/{job_id}")
def job_status(job_id: str, x_faz_worker_key: Optional[str] = Header(None, alias="X-FAZ-WORKER-KEY")):
    require_key(x_faz_worker_key)
    cleanup_old_jobs()
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return public_job(job)


@app.get("/jobs/{job_id}/download")
def download_job(job_id: str, x_faz_worker_key: Optional[str] = Header(None, alias="X-FAZ-WORKER-KEY")):
    require_key(x_faz_worker_key)
    cleanup_old_jobs()
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Job is not completed")
    path = Path(job.get("output_path") or "")
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Output expired")
    return FileResponse(path, media_type="audio/mpeg", filename="faz-dj-result.mp3")
