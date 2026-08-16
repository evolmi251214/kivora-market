import asyncio
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

import imageio_ffmpeg
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

APP_NAME = "FAZ Audio Worker"
WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")
MAX_SOURCE_MB = int(os.environ.get("MAX_SOURCE_MB", "100"))
MAX_SOURCE_BYTES = MAX_SOURCE_MB * 1024 * 1024
MAX_FINAL_BYTES = 20 * 1024 * 1024
MAX_DURATION = float(os.environ.get("MAX_DURATION_SECONDS", "420"))
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "3600"))
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "1")))

BASE_DIR = Path(os.environ.get("JOB_DIR", "/tmp/fazdj-worker"))
BASE_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
JOBS: Dict[str, dict] = {}
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

ALLOWED_EXTENSIONS = {
    ".mp3", ".ogg", ".wav", ".flac",
    ".mp4", ".webm", ".mov", ".mkv", ".avi",
}
ALLOWED_BITRATES = {64, 96, 128, 160, 192}

app = FastAPI(title=APP_NAME, version="1.0.0")


class JobStatus(BaseModel):
    id: str
    status: str
    progress: int
    message: str
    source_size: Optional[int] = None
    output_size: Optional[int] = None
    duration_seconds: Optional[float] = None
    output_filename: Optional[str] = None
    created_at: float
    updated_at: float


def auth(key: Optional[str]) -> None:
    if not WORKER_API_KEY:
        raise HTTPException(status_code=503, detail="WORKER_API_KEY is not configured")
    if not key or not secrets.compare_digest(key, WORKER_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid worker API key")


def safe_extension(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported media extension")
    return ext


def parse_duration(stderr: str) -> Optional[float]:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


def probe_duration(path: Path) -> Optional[float]:
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=90,
    )
    return parse_duration(proc.stderr or "")


def update_job(job_id: str, **changes) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    job.update(changes)
    job["updated_at"] = time.time()


async def run_conversion(job_id: str, source: Path, output: Path, speed: float, bitrate: int) -> None:
    async with SEMAPHORE:
        try:
            update_job(job_id, status="running", progress=10, message="Checking media")
            source_duration = await asyncio.to_thread(probe_duration, source)
            if source_duration is None:
                raise RuntimeError("Unable to detect media duration")
            if source_duration > MAX_DURATION:
                raise RuntimeError(f"Source exceeds {int(MAX_DURATION)} seconds")

            update_job(job_id, progress=25, message="Converting audio", duration_seconds=round(source_duration, 3))

            command = [
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source), "-map", "0:a:0", "-vn",
                "-filter:a", f"atempo={speed:.2f}",
                "-codec:a", "libmp3lame", "-b:a", f"{bitrate}k",
                "-ar", "44100", "-ac", "2", str(output),
            ]

            proc = await asyncio.to_thread(
                subprocess.run, command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=240,
            )

            if proc.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
                msg = (proc.stderr or "FFmpeg conversion failed").strip()
                raise RuntimeError(msg[-1200:])

            output_size = output.stat().st_size
            if output_size >= MAX_FINAL_BYTES:
                raise RuntimeError("Converted audio is 20 MB or larger")

            update_job(job_id, progress=80, message="Validating output")
            output_duration = await asyncio.to_thread(probe_duration, output)
            if output_duration is None:
                raise RuntimeError("Unable to verify converted duration")
            if output_duration > MAX_DURATION:
                raise RuntimeError(f"Converted audio exceeds {int(MAX_DURATION)} seconds")

            update_job(
                job_id,
                status="completed",
                progress=100,
                message="Ready",
                output_size=output_size,
                duration_seconds=round(output_duration, 3),
                output_filename=output.name,
            )
        except subprocess.TimeoutExpired:
            update_job(job_id, status="failed", progress=100, message="Conversion timed out")
            if output.exists():
                output.unlink(missing_ok=True)
        except Exception as exc:
            update_job(job_id, status="failed", progress=100, message=str(exc)[:1500])
            if output.exists():
                output.unlink(missing_ok=True)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(300)
        cutoff = time.time() - JOB_TTL_SECONDS
        stale = [job_id for job_id, job in list(JOBS.items()) if job.get("updated_at", 0) < cutoff]
        for job_id in stale:
            job = JOBS.pop(job_id, None)
            if job:
                shutil.rmtree(Path(job["job_dir"]), ignore_errors=True)


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(cleanup_loop())


@app.get("/")
async def root():
    return {"service": APP_NAME, "status": "online", "docs": "/docs"}


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": APP_NAME,
        "ffmpeg": True,
        "ffmpeg_path": FFMPEG,
        "max_source_mb": MAX_SOURCE_MB,
        "max_final_mb": 20,
        "max_duration_seconds": MAX_DURATION,
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
    }


@app.post("/jobs", response_model=JobStatus)
async def create_job(
    file: UploadFile = File(...),
    speed: float = Form(1.0),
    bitrate: int = Form(128),
    x_faz_worker_key: Optional[str] = Header(default=None),
):
    auth(x_faz_worker_key)
    if speed < 0.5 or speed > 2.0:
        raise HTTPException(status_code=422, detail="Speed must be between 0.5x and 2.0x")
    if bitrate not in ALLOWED_BITRATES:
        raise HTTPException(status_code=422, detail="Unsupported bitrate")

    ext = safe_extension(file.filename or "")
    job_id = secrets.token_hex(16)
    job_dir = BASE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    source = job_dir / f"source{ext}"
    output = job_dir / "output.mp3"

    total = 0
    try:
        with source.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise HTTPException(status_code=413, detail=f"Source exceeds {MAX_SOURCE_MB} MB")
                handle.write(chunk)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    if total <= 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Empty upload")

    now = time.time()
    JOBS[job_id] = {
        "id": job_id, "status": "queued", "progress": 0, "message": "Queued",
        "source_size": total, "output_size": None, "duration_seconds": None,
        "output_filename": None, "created_at": now, "updated_at": now,
        "job_dir": str(job_dir), "source_path": str(source), "output_path": str(output),
    }
    asyncio.create_task(run_conversion(job_id, source, output, speed, bitrate))
    return JobStatus(**JOBS[job_id])


@app.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str, x_faz_worker_key: Optional[str] = Header(default=None)):
    auth(x_faz_worker_key)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return JobStatus(**job)


@app.get("/jobs/{job_id}/download")
async def download_job(job_id: str, x_faz_worker_key: Optional[str] = Header(default=None)):
    auth(x_faz_worker_key)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail="Job is not completed")
    output = Path(job["output_path"])
    if not output.exists():
        raise HTTPException(status_code=410, detail="Output file expired")
    return FileResponse(output, media_type="audio/mpeg", filename=f"fazdj-{job_id}.mp3")
