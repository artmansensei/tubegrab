#!/usr/bin/env python3
"""
TubeGrab - a small, self-hosted YouTube downloader.

Backend: FastAPI + yt-dlp (+ bundled ffmpeg via imageio-ffmpeg).
Run:     python app.py            -> http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp.utils import DownloadCancelled

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _pick_download_dir() -> Path:
    """Prefer ./downloads, but fall back to the system temp dir if it is read-only."""
    preferred = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads")).expanduser().resolve()
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "tubegrab-downloads"
        fallback.mkdir(parents=True, exist_ok=True)
        print(f"[warn] cannot write to {preferred}, using {fallback} instead")
        return fallback


DOWNLOAD_DIR = _pick_download_dir()

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


def _detect_js_runtime() -> tuple[str | None, str | None]:
    """yt-dlp needs Node or Deno to solve YouTube signatures."""
    for name in ("node", "deno"):
        path = shutil.which(name) or shutil.which(f"{name}.exe")
        if path:
            return name, path
    return None, None


JS_RUNTIME, JS_RUNTIME_PATH = _detect_js_runtime()

MAX_CONCURRENT = 3
JOB_TTL_SECONDS = 60 * 60  # keep finished files around for one hour
MAX_HEIGHT = 4320
AUDIO_BITRATES = (320, 256, 192)  # kbps, highest first

ALLOWED_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def clean_url(raw: str) -> str:
    """Validate that the URL points at YouTube and return a normalised form."""
    url = (raw or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Please paste a YouTube link.")
    if not re.match(r"^https?://", url, re.I):
        # Bare "youtube.com/watch?v=..." or "youtu.be/..." input
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        raise HTTPException(status_code=400, detail="That does not look like a valid URL.")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise HTTPException(
            status_code=400,
            detail="Only YouTube links are supported (youtube.com, youtu.be, Shorts).",
        )
    if host == "youtu.be" or host == "www.youtu.be":
        if not parsed.path.strip("/"):
            raise HTTPException(status_code=400, detail="That youtu.be link is missing a video id.")
    else:
        query = parse_qs(parsed.query)
        # A pure playlist link has no single video to download and makes yt-dlp
        # enumerate the whole list (slow). Ask for a video link instead.
        if "list" in query and "v" not in query and not YOUTUBE_ID_RE.search(url):
            raise HTTPException(
                status_code=400,
                detail="Playlist links are not supported. Open a video from the playlist and paste its link.",
            )
        if not YOUTUBE_ID_RE.search(url):
            raise HTTPException(status_code=400, detail="Could not find a video id in that link.")
    return url


def fmt_bytes(n: float | None) -> str | None:
    if not n:
        return None
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB"):
        if n < step or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return None


def friendly_error(exc: Exception) -> str:
    msg = ANSI_RE.sub("", str(exc)).replace("ERROR: ", "").strip()
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube asked this server to verify it is not a bot. Try again in a moment, or from a different network."
    if "private video" in low:
        return "This video is private, so it cannot be downloaded."
    if "age" in low and ("restrict" in low or "confirm your age" in low):
        return "This video is age-restricted and requires a signed-in account."
    if "video unavailable" in low or "removed by the uploader" in low:
        return "This video is unavailable (removed, region-blocked or private)."
    if "members-only" in low or "join this channel" in low:
        return "This video is members-only."
    if "unsupported url" in low:
        return "That link is not a supported YouTube video."
    if "is live" in low or "live event" in low:
        return "Live streams are not supported. Try again once the stream has ended."
    if "timed out" in low or "timeout" in low:
        return "The download timed out. Check your connection and try again."
    first = msg.splitlines()[0] if msg else "Unknown error."
    return first[:300]


def base_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 2,
        "ffmpeg_location": FFMPEG_PATH,
        "cachedir": False,
    }
    # yt-dlp needs a JS runtime to solve YouTube signatures for all formats.
    opts["js_runtimes"] = {JS_RUNTIME: {}} if JS_RUNTIME else {}
    # Try multiple player clients — YouTube changes frequently and some clients
    # stop working; falling through the list keeps extraction robust.
    opts["extractor_args"] = {
        "youtube": {
            "player_client": ["default", "web", "web_safari", "android", "tv", "mweb"],
        }
    }
    # Download the EJS challenge solver from GitHub (helps when YouTube changes).
    opts["remote_components"] = ["ejs:github"]
    return opts


def pick_video_format(formats: list[dict]) -> dict:
    """Prefer H.264 (widest device support), then best fps/bitrate."""
    avc = [f for f in formats if str(f.get("vcodec") or "").startswith("avc1")]
    pool = avc or formats
    return max(
        pool,
        key=lambda f: (f.get("fps") or 0, f.get("tbr") or 0, f.get("filesize") or 0),
    )


def build_info(info: dict) -> dict:
    """Turn a raw yt-dlp info dict into a small payload for the UI."""
    duration = info.get("duration") or 0
    formats = info.get("formats") or []

    audio_formats = [
        f
        for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    best_audio = max(
        audio_formats,
        key=lambda f: (f.get("abr") or 0, f.get("filesize") or 0),
        default=None,
    )
    audio_size = None
    audio_estimated = False
    if best_audio:
        audio_size = best_audio.get("filesize") or best_audio.get("filesize_approx")
        if not audio_size and duration and best_audio.get("abr"):
            audio_size = duration * float(best_audio["abr"]) * 1000 / 8
            audio_estimated = True

    by_height: dict[int, list[dict]] = {}
    for f in formats:
        height = f.get("height")
        if f.get("vcodec") in (None, "none") or not height:
            continue
        height = int(height)
        if height < 144 or height > MAX_HEIGHT:
            continue
        by_height.setdefault(height, []).append(f)

    qualities = []
    for height in sorted(by_height, reverse=True):
        pool = by_height[height]
        chosen = pick_video_format(pool)
        fps = int(round(chosen.get("fps") or 0)) or None

        size = chosen.get("filesize") or chosen.get("filesize_approx")
        approximate = chosen.get("filesize") is None
        if not size:
            known = [
                f.get("filesize") or f.get("filesize_approx")
                for f in pool
                if f.get("filesize") or f.get("filesize_approx")
            ]
            size = max(known) if known else None
            approximate = True

        # Audio is downloaded separately and merged for DASH streams.
        audio_bytes = audio_size
        if not audio_bytes and duration:
            audio_bytes = duration * 160_000 / 8  # assume ~160 kbps
            approximate = True
        approximate = approximate or audio_estimated

        total = int(size + audio_bytes) if size and audio_bytes else None
        qualities.append(
            {
                "height": height,
                "fps": fps,
                "label": f"{height}p{fps if fps and fps > 30 else ''}",
                "ext": "mp4",
                "filesize": total,
                "filesize_text": fmt_bytes(total),
                "approximate": approximate,
                "needs_merge": (chosen.get("acodec") in (None, "none")),
                "note": chosen.get("format_note") or "",
            }
        )

    return {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled video",
        "thumbnail": info.get("thumbnail")
        or (f"https://i.ytimg.com/vi/{info.get('id')}/hqdefault.jpg" if info.get("id") else None),
        "duration": duration,
        "duration_text": format_duration(duration),
        "uploader": info.get("uploader") or info.get("channel") or "Unknown channel",
        "view_count": info.get("view_count"),
        "view_count_text": f"{info['view_count']:,}" if info.get("view_count") else None,
        "upload_date": info.get("upload_date"),
        "is_live": bool(info.get("is_live")),
        "webpage_url": info.get("webpage_url"),
        "qualities": qualities,
        "audio": [
            {
                "bitrate": bitrate,
                "label": f"{bitrate} kbps",
                "filesize": int(duration * bitrate * 1000 / 8) if duration else None,
                "filesize_text": fmt_bytes(duration * bitrate * 1000 / 8) if duration else None,
            }
            for bitrate in AUDIO_BITRATES
        ],
    }


def format_duration(seconds: float | int | None) -> str:
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
# Job management
# --------------------------------------------------------------------------- #


class Job:
    def __init__(
        self,
        url: str,
        height: int | None,
        audio_only: bool,
        info: dict,
        audio_bitrate: int | None = None,
    ):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.height = height
        self.audio_only = audio_only
        self.audio_bitrate = audio_bitrate
        self.title = info.get("title") or "Video"
        self.thumbnail = info.get("thumbnail")
        self.duration_text = info.get("duration_text") or ""
        self.status = "queued"  # queued | downloading | processing | finished | error
        self.progress = 0.0
        self.downloaded: int | None = None
        self.total: int | None = None
        self.speed: float | None = None
        self.eta: int | None = None
        self.message = "Waiting to start..."
        self.error: str | None = None
        self.filename: str | None = None
        self.path: Path | None = None
        self.size: int | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.cancelled = False
        self.dir = DOWNLOAD_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stream_index = 0
        self.expected_streams = 1
        self._lock = threading.Lock()

    # -- yt-dlp hooks ----------------------------------------------------- #

    def progress_hook(self, d: dict) -> None:
        if self.cancelled:
            raise DownloadCancelled("Cancelled by user")
        try:
            with self._lock:
                info = d.get("info_dict") or {}
                if info.get("vcodec") not in (None, "none") and info.get("acodec") in (None, "none"):
                    self.expected_streams = max(self.expected_streams, 2)

                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    done = d.get("downloaded_bytes") or 0
                    self.status = "downloading"
                    self.downloaded = int(done)
                    self.total = int(total) if total else None
                    self.speed = d.get("speed")
                    self.eta = d.get("eta")
                    self.message = "Downloading..."
                    if total:
                        part = min(done / total, 1.0)
                        phases = max(self.expected_streams, 1)
                        overall = (min(self.stream_index, phases - 1) + part) / phases
                        self.progress = min(overall * 100.0, 98.5)
                elif d.get("status") == "finished":
                    self.stream_index += 1
                    if self.stream_index >= self.expected_streams:
                        self.status = "processing"
                        self.message = "Merging audio & video..."
                        self.progress = 99.0
        except Exception:
            pass  # never let a hook break the download

    def postprocessor_hook(self, d: dict) -> None:
        if self.cancelled:
            raise DownloadCancelled("Cancelled by user")
        try:
            with self._lock:
                if d.get("status") in ("started", "processing"):
                    self.status = "processing"
                    self.message = "Processing..."
                    self.progress = max(self.progress, 99.0)
        except Exception:
            pass

    # -- serialisation ---------------------------------------------------- #

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "title": self.title,
                "thumbnail": self.thumbnail,
                "duration_text": self.duration_text,
                "height": self.height,
                "audio_only": self.audio_only,
                "audio_bitrate": self.audio_bitrate,
                "status": self.status,
                "progress": round(self.progress, 1),
                "downloaded": self.downloaded,
                "total": self.total,
                "speed": self.speed,
                "eta": self.eta,
                "message": self.message,
                "error": self.error,
                "filename": self.filename,
                "size": self.size,
                "created_at": self.created_at,
            }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="dl")


def _download_worker(job: Job) -> None:
    try:
        for attempt in range(3):
            if job.cancelled:
                shutil.rmtree(job.dir, ignore_errors=True)
                return
            try:
                opts = base_opts()
                opts.update(
                    {
                        "outtmpl": str(job.dir / "%(title).150B [%(id)s].%(ext)s"),
                        "windowsfilenames": True,
                        "progress_hooks": [job.progress_hook],
                        "postprocessor_hooks": [job.postprocessor_hook],
                        "concurrent_fragment_downloads": 4,
                        "fragment_retries": 5,
                    }
                )
                if job.audio_only:
                    opts["format"] = "bestaudio/best"
                    opts["postprocessors"] = [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": str(job.audio_bitrate or 192),
                        }
                    ]
                else:
                    if job.height:
                        h = job.height
                        opts["format"] = (
                            f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[ext=m4a]/"
                            f"bestvideo[height<={h}]+bestaudio/"
                            f"best[height<={h}]/best"
                        )
                    else:
                        opts["format"] = "bestvideo+bestaudio/best"
                    opts["merge_output_format"] = "mp4"

                if attempt == 1:
                    opts["extractor_args"] = {
                        "youtube": {
                            "player_client": ["android_vr", "android", "tv"],
                        }
                    }
                elif attempt == 2:
                    opts["extractor_args"] = {
                        "youtube": {
                            "player_client": ["tv", "web", "mweb"],
                        }
                    }

                with job._lock:
                    job.status = "downloading"
                    job.message = "Starting download..."
                    job.error = None
                    job.progress = 0.0
                    job.stream_index = 0

                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(job.url, download=True)
                    if info and info.get("title"):
                        with job._lock:
                            job.title = info["title"]

                _finalise(job)
                return
            except DownloadCancelled:
                shutil.rmtree(job.dir, ignore_errors=True)
                return
            except Exception as exc:  # noqa: BLE001
                if attempt >= 2:
                    raise
                # Clean partial files before retrying with another client.
                for item in job.dir.iterdir():
                    if item.is_file():
                        try:
                            item.unlink()
                        except OSError:
                            pass
                with job._lock:
                    job.message = "Retrying with a different YouTube client..."
                time.sleep(1.5)
    except Exception as exc:  # noqa: BLE001
        with job._lock:
            job.status = "error"
            job.error = friendly_error(exc)
            job.message = "Failed"
            job.progress = 0.0
            job.finished_at = time.time()


def _finalise(job: Job) -> None:
    files = [
        p
        for p in job.dir.iterdir()
        if p.is_file() and p.suffix.lower() not in (".part", ".ytdl", ".temp", ".json")
    ]
    if not files:
        raise RuntimeError("Download finished but no output file was produced.")
    out = max(files, key=lambda p: p.stat().st_size)
    with job._lock:
        job.path = out
        job.filename = out.name
        job.size = out.stat().st_size
        job.status = "finished"
        job.progress = 100.0
        job.message = "Ready"
        job.eta = 0
        job.speed = None
        job.finished_at = time.time()


def _sweeper() -> None:
    """Delete finished jobs (and their files) after the TTL."""
    while True:
        time.sleep(300)
        cutoff = time.time() - JOB_TTL_SECONDS
        with JOBS_LOCK:
            stale = [
                jid
                for jid, job in JOBS.items()
                if job.finished_at and job.finished_at < cutoff
            ]
            for jid in stale:
                job = JOBS.pop(jid)
                shutil.rmtree(job.dir, ignore_errors=True)


threading.Thread(target=_sweeper, daemon=True).start()

# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

app = FastAPI(title="TubeGrab", docs_url=None, redoc_url=None)


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    height: int | None = None
    audio_only: bool = False
    audio_bitrate: int | None = None


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "yt_dlp": yt_dlp.version.__version__,
        "ffmpeg": Path(FFMPEG_PATH).name,
        "js_runtime": f"{JS_RUNTIME} ({JS_RUNTIME_PATH})" if JS_RUNTIME else None,
    }


@app.post("/api/info")
def api_info(payload: InfoRequest) -> dict:
    url = clean_url(payload.url)
    opts = base_opts()
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=friendly_error(exc)) from exc

    if not info:
        raise HTTPException(status_code=422, detail="Could not read that video.")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise HTTPException(status_code=422, detail="That playlist is empty.")
        info = entries[0]
    if info.get("is_live"):
        raise HTTPException(
            status_code=422, detail="Live streams are not supported yet. Try a finished video."
        )

    data = build_info(info)
    if not data["qualities"]:
        raise HTTPException(status_code=422, detail="No downloadable video formats were found.")
    return data


@app.post("/api/jobs")
def create_job(payload: DownloadRequest) -> dict:
    url = clean_url(payload.url)
    height = payload.height
    if height is not None and (height < 144 or height > MAX_HEIGHT):
        raise HTTPException(status_code=400, detail="Unsupported quality requested.")

    audio_bitrate = None
    if payload.audio_only:
        audio_bitrate = payload.audio_bitrate or 192
        if audio_bitrate not in AUDIO_BITRATES:
            allowed = ", ".join(str(b) for b in AUDIO_BITRATES)
            raise HTTPException(
                status_code=400, detail=f"Unsupported audio bitrate. Choose {allowed} kbps."
            )

    # Re-extract cheaply to get the title/thumbnail for the job card.
    opts = base_opts()
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=friendly_error(exc)) from exc

    if not info:
        raise HTTPException(status_code=422, detail="Could not read that video.")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise HTTPException(status_code=422, detail="That playlist is empty.")
        info = entries[0]

    summary = build_info(info)
    job = Job(url, height, payload.audio_only, summary, audio_bitrate)
    with JOBS_LOCK:
        JOBS[job.id] = job
    EXECUTOR.submit(_download_worker, job)
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs() -> dict:
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
    return {"jobs": [j.to_dict() for j in jobs]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/file")
def get_file(job_id: str) -> FileResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    with job._lock:
        path = job.path
        name = job.filename
    if not path or not path.exists():
        raise HTTPException(status_code=409, detail="File is not ready yet.")
    return FileResponse(path, media_type="application/octet-stream", filename=name or path.name)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    job.cancelled = True
    shutil.rmtree(job.dir, ignore_errors=True)
    return {"ok": True}


# Static frontend (registered last so /api/* keeps priority).
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="TubeGrab - YouTube downloader")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = parser.parse_args()

    print(f"\n  TubeGrab is running at http://{args.host}:{args.port}\n")
    if not JS_RUNTIME:
        print("  [warn] Node.js / Deno was not found on PATH.")
        print("         YouTube format extraction may be limited.")
        print("         Install Node.js from https://nodejs.org for best results.\n")
    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)
