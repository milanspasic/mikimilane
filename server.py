"""Shared set recognition: official AudioTag free API or experimental ShazamIO."""
from __future__ import annotations
import argparse
import audiotag
import asyncio
from datetime import timezone
from email.utils import parsedate_to_datetime
import hmac
import io
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import wave

BASE = Path(__file__).resolve().parent
JOBS: dict[str, dict] = {}
LOCK = threading.RLock()
ACTIVE = threading.BoundedSemaphore(1)
ACCESS_TOKEN = os.environ.get("SETLENS_ACCESS_TOKEN", "")
CHUNK_SECONDS = 12
REQUEST_PAUSE = 15
NEXT_REQUEST_AT = 0.0
COOLDOWN_UNTIL = 0.0


class RecognitionFailure(RuntimeError):
    def __init__(self, message, code, http_status=None, retry_after=0):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after


def retry_after_seconds(value, now=None):
    now = time.time() if now is None else now
    try:
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError()
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
        except (ValueError, TypeError, OverflowError, AttributeError):
            seconds = 300
    return max(REQUEST_PAUSE, math.ceil(seconds))


def recognition_cooldown():
    with LOCK:
        return max(0, math.ceil(COOLDOWN_UNTIL - time.time()))


async def wait_for_recognition_slot(cancelled):
    global NEXT_REQUEST_AT
    while True:
        if cancelled.is_set():
            raise InterruptedError("Scan stopped. Partial results are available.")
        with LOCK:
            remaining = NEXT_REQUEST_AT - time.monotonic()
            if remaining <= 0:
                NEXT_REQUEST_AT = time.monotonic() + REQUEST_PAUSE
                return
        await asyncio.sleep(min(.2, remaining))


def recognition_failure(error, metadata):
    from shazamio.exceptions import RateLimited
    from shazamio_core import SignatureError
    status = metadata.get("status")
    if isinstance(error, RateLimited) or status == 429:
        wait = retry_after_seconds(metadata.get("retry_after"))
        return RecognitionFailure(f"Shazam is limiting recognition requests (HTTP 429). This scan stopped and partial results are kept. Wait at least {max(1, math.ceil(wait / 60))} minutes before starting another scan.", "SHAZAM_RATE_LIMITED", 429, wait)
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return RecognitionFailure("Shazam did not respond within 45 seconds. This scan stopped; partial results are kept.", "SHAZAM_TIMEOUT")
    if isinstance(error, SignatureError):
        return RecognitionFailure("An audio fragment could not be fingerprinted. This scan stopped; partial results are kept.", "AUDIO_FINGERPRINT_FAILED")
    if status in (401, 403):
        return RecognitionFailure(f"Shazam refused the recognition request (HTTP {status}). This scan stopped; partial results are kept.", "SHAZAM_ACCESS_REFUSED", status)
    if status:
        return RecognitionFailure(f"Shazam returned an invalid recognition response (HTTP {status}). This scan stopped; partial results are kept.", "SHAZAM_RESPONSE_FAILED", status)
    return RecognitionFailure("The connection to Shazam failed. This scan stopped; partial results are kept.", "SHAZAM_CONNECTION_FAILED")


def validate_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Paste a YouTube or SoundCloud set link.")
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        raise ValueError("Use a public HTTPS link without login details.")
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "soundcloud.com", "www.soundcloud.com", "on.soundcloud.com"}
    if host not in allowed:
        raise ValueError("This backend supports public YouTube and SoundCloud links.")
    if not parts or (host.endswith("soundcloud.com") and host != "on.soundcloud.com" and len(parts) < 2):
        raise ValueError("Use a link to a single set, not a profile or playlist.")
    if "sets" in parts or parsed.path == "/playlist":
        raise ValueError("Choose one set rather than a playlist.")
    # Playback start parameters are stripped by the website. Never use cookies or bypass login walls.
    return value.strip()


def update(job_id, **fields):
    with LOCK:
        JOBS[job_id].update(fields, updatedAt=time.time())


def public_job(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return json.loads(json.dumps({k: v for k, v in job.items() if k != "cancel"}))


def merge_match(tracks, track, start, end):
    if not isinstance(track, dict):
        return
    title, artist = str(track.get("title", "")).strip(), str(track.get("subtitle", "")).strip()
    if not title or not artist:
        return
    identity = str(track.get("key") or f"{artist.lower()}::{title.lower()}")
    for previous in reversed(tracks):
        if previous["identity"] == identity and start <= previous["end"] + CHUNK_SECONDS:
            previous["end"] = max(previous["end"], end)
            return
    link = track.get("url")
    if not isinstance(link, str) or not link.startswith("https://"):
        link = None
    tracks.append({"id": f"track-{len(tracks)}", "identity": identity, "artist": artist[:500], "title": title[:500], "start": start, "end": end, "approximate": True, "link": link})


def audio_chunks(path):
    with wave.open(str(path), "rb") as recording:
        rate = recording.getframerate()
        offset = 0
        while True:
            raw = recording.readframes(rate * CHUNK_SECONDS)
            if not raw:
                break
            seconds = len(raw) / (rate * 2)
            if seconds < 2:
                break
            output = io.BytesIO()
            with wave.open(output, "wb") as fragment:
                fragment.setnchannels(1)
                fragment.setsampwidth(2)
                fragment.setframerate(rate)
                fragment.writeframes(raw)
            yield offset, seconds, output.getvalue(), bool(any(raw))
            offset += seconds


async def recognize_file(job_id, path):
    global COOLDOWN_UNTIL
    from aiohttp_retry import ExponentialRetry
    from shazamio import Shazam
    from shazamio.client import HTTPClient
    tracks = []
    cancelled = JOBS[job_id]["cancel"]
    metadata = {}
    async def capture_response(_session, _context, params):
        metadata.update(status=params.response.status, retry_after=params.response.headers.get("Retry-After"))
    # A single client and no automatic retries. Stop on service errors/rate limiting.
    async with HTTPClient(retry_options=ExponentialRetry(attempts=1)) as client:
        client.trace_config.on_request_end.append(capture_response)
        async with Shazam(http_client=client) as recognizer:
            for start, seconds, data, audible in audio_chunks(path):
                if cancelled.is_set():
                    raise InterruptedError("Scan stopped. Partial results are available.")
                if audible:
                    await wait_for_recognition_slot(cancelled)
                    metadata.clear()
                    try:
                        result = await asyncio.wait_for(recognizer.recognize(data, proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")), timeout=45)
                        if metadata.get("status", 200) >= 400:
                            raise RuntimeError("Recognition service returned an HTTP error.")
                    except Exception as exc:
                        failure = recognition_failure(exc, metadata)
                        if failure.retry_after:
                            with LOCK:
                                COOLDOWN_UNTIL = max(COOLDOWN_UNTIL, time.time() + failure.retry_after)
                        print(json.dumps({"event": "recognition_failed", "code": failure.code, "httpStatus": failure.http_status, "errorType": type(exc).__name__, "atSeconds": start}), flush=True)
                        raise failure from exc
                    merge_match(tracks, result.get("track"), start, start + seconds)
                # Clip offsets are set-relative; Shazam's own match offset is song-relative.
                visible = [{k: v for k, v in t.items() if k != "identity"} for t in tracks]
                update(job_id, tracks=visible, processedSeconds=start + seconds)


class QuietLogger:
    def debug(self, message):
        pass
    def warning(self, message):
        pass
    def error(self, message):
        pass


def recognize_audiotag(job_id, path, key):
    import audiotag
    tracks = []
    cancelled = JOBS[job_id]["cancel"]
    for start, seconds, audio in audiotag.audio_chunks(path):
        data = audiotag.recognize_chunk(key, audio, seconds, cancelled)
        for begin, end, track in audiotag.matches(data, start, seconds):
            merge_match(tracks, track, begin, end)
        visible = [{k: v for k, v in t.items() if k != "identity"} for t in tracks]
        update(job_id, tracks=visible, processedSeconds=start + seconds)


def run_scan(job_id, url, minutes, source_file=None, engine="shazam", api_key="", test=False):
    try:
        from yt_dlp import YoutubeDL
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory(prefix="setlens-") as scratch:
            work = Path(scratch)
            if test:
                import urllib.request
                source = work / "test.ogg"
                # Fixed public maintainer sample. Callers cannot choose a download target.
                sample_url = "https://raw.githubusercontent.com/shazamio/ShazamIO/master/examples/data/Gloria.ogg"
                with urllib.request.urlopen(sample_url, timeout=30) as response:
                    data = response.read(10 * 1024 * 1024 + 1)
                if len(data) > 10 * 1024 * 1024:
                    raise ValueError("The connection-test sample was too large.")
                source.write_bytes(data)
                title = "Connection test: Gloria Gaynor — I Will Survive"
            elif source_file:
                source = Path(source_file)
                title = source.name
            else:
                update(job_id, stage="downloading")
                cancelled = JOBS[job_id]["cancel"]
                def hook(progress):
                    if cancelled.is_set():
                        raise InterruptedError("Scan stopped.")
                    if progress.get("downloaded_bytes", 0) > 1024 ** 3:
                        raise ValueError("This recording is too large for this backend (1 GB maximum).")
                node = shutil.which("node")
                deno = shutil.which("deno")
                runtimes = {"node": {"path": node}} if node else {"deno": {"path": deno}} if deno else {}
                if "youtu" in urlparse(url).netloc and not runtimes:
                    raise ValueError("YouTube downloading needs Node.js 22+ or Deno 2.3+. Install a supported runtime on this server, or try SoundCloud.")
                options = {"format": "bestaudio/best", "noplaylist": True, "outtmpl": str(work / "source.%(ext)s"), "quiet": True, "no_warnings": True, "logger": QuietLogger(), "progress_hooks": [hook], "max_filesize": 1024 ** 3, "socket_timeout": 25, "retries": 0, "fragment_retries": 0, "extractor_retries": 0, "js_runtimes": runtimes}
                try:
                    with YoutubeDL(options) as downloader:
                        info = downloader.extract_info(url, download=True)
                        if not info or info.get("_type") in ("playlist", "multi_video") or info.get("is_live"):
                            raise ValueError("Use a finished, single recording rather than a livestream or playlist.")
                        source = Path(info.get("requested_downloads", [{}])[0].get("filepath") or downloader.prepare_filename(info))
                        title = str(info.get("title") or "DJ set")[:300]
                except (ValueError, InterruptedError):
                    raise
                except Exception as exc:
                    raise RuntimeError("The platform would not provide this recording. Use a public set, update yt-dlp, or upload your own audio locally. No login or access restriction is bypassed.") from exc
                if not source.is_file():
                    raise RuntimeError("No audio file was downloaded. The platform may have restricted this link.")
            if JOBS[job_id]["cancel"].is_set():
                raise InterruptedError("Scan stopped.")
            update(job_id, stage="preparing", title=title)
            wav_path = work / "recording.wav"
            command = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-i", str(source), "-t", str(30 if test else minutes * 60), "-vn", "-ac", "1", "-ar", "8000" if engine == "audiotag" else "16000", "-c:a", "pcm_s16le", "-y", str(wav_path)]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 600
            while process.poll() is None:
                if JOBS[job_id]["cancel"].wait(.25) or time.monotonic() > deadline:
                    process.kill(); process.wait()
                    raise InterruptedError("Audio conversion stopped or timed out.")
            if process.returncode:
                raise RuntimeError("The recording could not be decoded as audio.")
            with wave.open(str(wav_path), "rb") as recording:
                duration = recording.getnframes() / recording.getframerate()
            if duration < 2:
                raise ValueError("Provide at least 2 seconds of audio.")
            update(job_id, stage="recognizing", durationSeconds=duration, capReached=duration >= minutes * 60 - .1)
            if engine == "audiotag":
                recognize_audiotag(job_id, wav_path, api_key)
            else:
                asyncio.run(recognize_file(job_id, wav_path))
            if test:
                verified = any("gloria gaynor" in t["artist"].lower() and "i will survive" in t["title"].lower() for t in JOBS[job_id]["tracks"])
                if not verified:
                    raise RuntimeError("The sample was processed, but the expected song was not identified. Recognition is not verified yet.")
                update(job_id, verified=True)
            update(job_id, status="complete", stage="complete")
    except Exception as exc:
        update(job_id, status="stopped" if JOBS[job_id]["cancel"].is_set() else "failed", stage="finished", error=str(exc)[:500], errorCode=getattr(exc, "code", None), retryAfterSeconds=getattr(exc, "retry_after", 0))
    finally:
        ACTIVE.release()


def create_job(url, minutes, source_file=None, engine="shazam", api_key="", test=False):
    if isinstance(minutes, bool) or minutes not in (30, 60, 120, 240):
        raise ValueError("Choose a scan limit of 30, 60, 120 or 240 minutes.")
    if not source_file and not test:
        url = validate_url(url)
    if engine not in ("audiotag", "shazam"):
        raise ValueError("Choose AudioTag or ShazamIO.")
    if engine == "audiotag" and (not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 512 or any(c in api_key for c in "\r\n")):
        raise ValueError("The owner needs to connect AudioTag in Settings.")
    if test and engine != "audiotag":
        raise ValueError("Connection tests use AudioTag.")
    remaining = recognition_cooldown() if engine == "shazam" else 0
    if remaining:
        raise RecognitionFailure("Shazam is temporarily limiting recognition. Please wait before starting another scan.", "SHAZAM_RATE_LIMITED", 429, remaining)
    if not ACTIVE.acquire(blocking=False):
        raise BlockingIOError("The recognition server is busy. Try again after the current set finishes.")
    job_id = secrets.token_urlsafe(24)
    with LOCK:
        for old_id, job in list(JOBS.items()):
            if job["status"] != "processing" and time.time() - job["updatedAt"] > 7200:
                del JOBS[old_id]
        if len(JOBS) >= 100:
            completed = [k for k in JOBS if JOBS[k]["status"] != "processing"]
            if completed:
                del JOBS[completed[0]]
        JOBS[job_id] = {"id": job_id, "status": "processing", "stage": "queued", "title": "DJ set", "tracks": [], "processedSeconds": 0, "durationSeconds": None, "capReached": False, "error": None, "updatedAt": time.time(), "cancel": threading.Event()}
    threading.Thread(target=run_scan, args=(job_id, url, minutes, source_file, engine, api_key, test), daemon=True).start()
    return public_job(job_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "SetlensFree/1.0"
    def log_message(self, *_):
        pass
    def send_json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 429 and body.get("retryAfterSeconds", 0) > 0:
            self.send_header("Retry-After", str(math.ceil(body["retryAfterSeconds"])))
        self.end_headers()
        self.wfile.write(payload)
    def authorized(self):
        if ACCESS_TOKEN:
            expected = "Bearer " + ACCESS_TOKEN
            return hmac.compare_digest(self.headers.get("Authorization", ""), expected)
        host = self.headers.get("Host", "").split(":")[0]
        return host in {"127.0.0.1", "localhost"}
    def do_GET(self):
        if self.path == "/":
            data = (BASE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers(); self.wfile.write(data); return
        if not self.authorized():
            return self.send_json(401, {"error": "This backend requires its private access token."})
        if self.path == "/health":
            return self.send_json(200, {"service": "setlens-free", "version": 3, "engines": ["audiotag", "shazam"], "recognition": "AudioTag API / ShazamIO (unofficial)", "nodeAvailable": bool(shutil.which("node") or shutil.which("deno")), "recognitionIntervalSeconds": REQUEST_PAUSE, "retryAfterSeconds": recognition_cooldown()})
        if self.path.startswith("/v1/scans/"):
            job = public_job(self.path.split("/")[-1])
            return self.send_json(200 if job else 404, job or {"error": "Scan not found. Results expire two hours after completion or when the server restarts."})
        self.send_json(404, {"error": "Not found"})
    def do_POST(self):
        if not self.authorized():
            return self.send_json(401, {"error": "This backend requires its private access token."})
        if self.headers.get("X-Setlens-Client") != "1":
            return self.send_json(400, {"error": "Missing X-Setlens-Client header."})
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.send_json(403, {"error": "Cross-origin browser requests are not allowed."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 8192 or "application/json" not in self.headers.get("Content-Type", ""):
                return self.send_json(400, {"error": "Send a small JSON request."})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Send a JSON object.")
            if self.path == "/v1/connection-check":
                key = body.get("apiKey")
                if not isinstance(key, str) or not key.strip() or len(key) > 512:
                    raise ValueError("Enter an active AudioTag API key.")
                return self.send_json(200, {"valid": True, "freeSeconds": audiotag.free_seconds(key)})
            if self.path in ("/v1/scans", "/v1/test"):
                job = create_job(body.get("url"), body.get("scanMinutes", 120), engine=body.get("engine", "shazam"), api_key=body.get("apiKey", ""), test=self.path == "/v1/test")
                return self.send_json(202, job)
            if self.path.endswith("/cancel"):
                job_id = self.path.split("/")[-2]
                with LOCK:
                    job = JOBS.get(job_id)
                    if not job:
                        return self.send_json(404, {"error": "Scan not found."})
                    job["cancel"].set()
                return self.send_json(200, {"stopping": True})
            return self.send_json(404, {"error": "Not found"})
        except audiotag.AudioTagFailure as exc:
            self.send_json(422, {"error": str(exc), "code": exc.code})
        except RecognitionFailure as exc:
            self.send_json(429 if exc.code == "SHAZAM_RATE_LIMITED" else 500, {"error": str(exc), "code": exc.code, "retryAfterSeconds": exc.retry_after})
        except BlockingIOError as exc:
            self.send_json(429, {"error": str(exc)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception:
            self.send_json(500, {"error": "The recognition backend could not start this scan."})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, help="Scan your own audio file instead of a link.")
    parser.add_argument("--minutes", type=int, default=120, choices=[30, 60, 120, 240])
    args = parser.parse_args()
    host = os.environ.get("SETLENS_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    if host not in {"127.0.0.1", "localhost"} and len(ACCESS_TOKEN) < 32:
        raise SystemExit("An online backend needs SETLENS_ACCESS_TOKEN with at least 32 characters. Keep this token only in your site's owner settings.")
    if args.audio:
        source = args.audio.expanduser().resolve()
        if not source.is_file():
            raise SystemExit("That audio file could not be found.")
        job = create_job("", args.minutes, source)
        print("Scanning your audio. Keep this terminal open.", flush=True)
        while public_job(job["id"])["status"] == "processing":
            time.sleep(1)
        result = public_job(job["id"])
        output = source.with_name(source.stem + "_tracklist.json")
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print("Saved:", output)
        return
    print(f"Setlens Free is ready at http://{host}:{port}", flush=True)
    print("Shared recognition server ready. AudioTag uses an owner API key; ShazamIO is experimental.", flush=True)
    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nSetlens stopped.")


if __name__ == "__main__":
    main()
