#!/usr/bin/env python3
"""
Multi-Downloader — Web Server
Env vars: PASSWORD, SECRET_KEY, SECURE_COOKIES, COOKIES_PATH
"""

import os, sys, json, queue, threading, subprocess, shutil, tempfile, mimetypes, time, re, zipfile, uuid
from pathlib import Path
from functools import wraps
from urllib.parse import quote
from flask import (Flask, request, Response, jsonify,
                   send_from_directory, session, redirect, url_for)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import yt_dlp

# Try to import spotdl
try:
    from spotdl import Spotdl
    SPOTDL_AVAILABLE = True
except ImportError:
    SPOTDL_AVAILABLE = False

# Default Spotify API credentials bundled with spotdl (same ones the CLI uses)
SPOTDL_CLIENT_ID     = "5f573c9620494bae87890c0f08a60293"
SPOTDL_CLIENT_SECRET = "212476d9b0f3472eaa762d90b19b0ba8"

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).resolve().parent
PASSWORD      = os.environ.get("PASSWORD", "changeme")
SECRET_KEY    = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
# Cookies live next to the script by default so local runs (per README) just
# work. Override with COOKIES_PATH if you deploy somewhere with a different
# writable/persistent location (e.g. a mounted volume).
COOKIES_PATH  = Path(os.environ.get("COOKIES_PATH", str(BASE_DIR / "cookies.txt")))
PASSWORD_HASH = generate_password_hash(PASSWORD)
# Secure cookies require HTTPS. Running locally over http://localhost (the
# documented dev workflow) with this forced on can silently break login in
# some browsers. Default off; set SECURE_COOKIES=1 when deployed behind HTTPS
# (e.g. on Railway).
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "0").lower() in ("1", "true", "yes")
JOB_TTL_SECS   = 2 * 60 * 60  # abandon + clean up jobs nobody ever collected

app = Flask(__name__, static_folder="static")
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_SECURE=SECURE_COOKIES,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)
# Trust exactly one reverse-proxy hop (Railway/Render/Heroku-style PaaS) for
# client IP / scheme. Without this, request.remote_addr is the proxy's IP,
# not the real client's — which breaks the brute-force lockout below (it'd
# either lock out everyone behind the proxy at once, or never trigger).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ── Brute-force protection ────────────────────────────────────────────────────
failed_attempts: dict = {}
failed_attempts_lock = threading.Lock()
MAX_ATTEMPTS  = 5
LOCKOUT_SECS  = 60 * 15

def is_locked_out(ip):
    now = time.time()
    with failed_attempts_lock:
        attempts = [t for t in failed_attempts.get(ip, []) if now - t < LOCKOUT_SECS]
        failed_attempts[ip] = attempts
        return len(attempts) >= MAX_ATTEMPTS

def record_failure(ip):
    with failed_attempts_lock:
        failed_attempts.setdefault(ip, []).append(time.time())

def attempts_remaining(ip):
    with failed_attempts_lock:
        return max(0, MAX_ATTEMPTS - len(failed_attempts.get(ip, [])))

def clear_failures(ip):
    with failed_attempts_lock:
        failed_attempts.pop(ip, None)

# ── Auth ──────────────────────────────────────────────────────────────────────
def login_required(f):
    """For HTML page routes: bounce an unauthenticated browser to /login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def api_login_required(f):
    """For fetch/EventSource routes: a redirect-to-HTML response just shows up
    as a confusing 'could not reach server' to the caller. Return a clean 401
    so the frontend can detect it and send the user to /login itself."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return jsonify({"error": "not authenticated"}), 401
        return f(*args, **kwargs)
    return decorated

# ── Jobs ──────────────────────────────────────────────────────────────────────
jobs: dict = {}
jobs_lock = threading.Lock()

def sweep_stale_jobs():
    """Clean up temp dirs from jobs whose file was never collected via
    /download/<id> (closed tab, crashed client, etc.) so disk usage doesn't
    grow unbounded on a long-running server."""
    now = time.time()
    with jobs_lock:
        stale = [jid for jid, j in jobs.items() if now - j.get("created", now) > JOB_TTL_SECS]
        for jid in stale:
            tmpdir = jobs[jid].get("tmpdir")
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            jobs.pop(jid, None)

# ── Helpers ───────────────────────────────────────────────────────────────────
def sse(q, event, data):
    q.put("event: {}\ndata: {}\n\n".format(event, json.dumps(data)))

def parse_time(value):
    value = value.strip()
    if not value:
        return 0.0
    parts = value.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return 0.0
    if len(parts) == 1: return parts[0]
    if len(parts) == 2: return parts[0] * 60 + parts[1]
    if len(parts) == 3: return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0.0

def sanitize_filename(name: str, max_len: int = 150) -> str:
    """Keep a user-supplied custom filename from escaping the job's temp dir.
    Without this, a name like '../../../whatever' is interpolated straight
    into yt-dlp's output template, which yt-dlp will happily honor — letting
    a logged-in user write files outside the sandboxed temp directory."""
    name = (name or "").strip()
    if not name:
        return ""
    name = name.replace("/", "_").replace("\\", "_")
    name = name.replace("..", "_")
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = name.strip(" .")
    return name[:max_len]

def human_size(n):
    if not n:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"

def human_eta(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def make_progress_hook(q):
    def hook(d):
        status = d.get("status")
        if status == "downloading":
            # NOTE: deliberately computed from the numeric fields rather than
            # yt-dlp's _percent_str / _speed_str / _eta_str. Those "pretty"
            # strings can carry embedded ANSI color codes depending on
            # context, which breaks parseFloat() on the frontend and can
            # leave the progress bar stuck. The numeric fields are stable.
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done  = d.get("downloaded_bytes", 0)
            pct   = (done / total * 100) if total else 0
            sse(q, "progress", {
                "pct":   f"{pct:.1f}%",
                "speed": (human_size(d.get("speed")) + "/s") if d.get("speed") else "",
                "eta":   human_eta(d.get("eta")),
            })
        elif status == "finished":
            sse(q, "progress", {"pct": "100%", "speed": "", "eta": "finishing..."})
    return hook

def make_postprocessor_hook(q):
    def hook(d):
        if d.get("status") == "started":
            pp = d.get("postprocessor", "")
            sse(q, "log", {"msg": f"Post-processing ({pp})…"})
    return hook

def safe_disposition(filename):
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip()
    # A literal quote or newline in the fallback name would break the header.
    ascii_name = ascii_name.replace('"', "'").replace("\r", "").replace("\n", "")
    if not ascii_name:
        ascii_name = "download"
    encoded = quote(filename, safe="")
    return "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(ascii_name, encoded)

def finalize_output(tmpdir, files):
    """Return (path, display_name) for whatever the job produced. If more
    than one file came out (e.g. a Spotify album/playlist downloads many
    tracks), zip them together instead of silently handing back only
    files[0] and discarding the rest."""
    if len(files) == 1:
        return files[0], files[0].name
    zip_path = tmpdir / "downloads.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            zf.write(file, arcname=file.name)
    return zip_path, "downloads.zip"

def is_spotify_url(url: str) -> bool:
    """Check if the URL is a Spotify link."""
    return "spotify.com" in url

# ── Download functions ────────────────────────────────────────────────────────

def run_download_spotdl(job_id, payload):
    """Download from Spotify using spotdl."""
    job = jobs[job_id]
    q   = job["queue"]
    tmpdir = job["tmpdir"]
    url = payload.get("url", "").strip()

    try:
        if not SPOTDL_AVAILABLE:
            raise RuntimeError("spotdl is not installed. Run: pip install spotdl")

        sse(q, "log", {"msg": "Contacting Spotify..."})

        spotdl = Spotdl(
            client_id=SPOTDL_CLIENT_ID,
            client_secret=SPOTDL_CLIENT_SECRET,
            headless=True,
            no_cache=True,
            downloader_settings={
                "output":    str(tmpdir),
                "format":    "mp3",
                "log_level": "ERROR",
                "threads":   4,
                "overwrite": "force",
            },
        )

        sse(q, "log", {"msg": "Fetching track info..."})
        songs = spotdl.search([url])
        if not songs:
            raise RuntimeError("No tracks found for that Spotify URL.")

        sse(q, "log", {"msg": f"📄 Downloading {len(songs)} track(s)…"})
        results = spotdl.download_songs(songs)

        # results is a list of (Song, Path|None) tuples
        files = [path for _, path in results if path is not None]
        if not files:
            raise RuntimeError("spotdl produced no output files.")

        sse(q, "log", {"msg": f"✓ Downloaded {len(files)} track(s)"})
        out_file, out_name = finalize_output(tmpdir, files)
        job["file"]     = out_file
        job["filename"] = out_name
        sse(q, "done", {"job_id": job_id, "filename": out_name})

    except Exception as exc:
        job["error"] = str(exc)
        sse(q, "error", {"msg": "ERROR: {}".format(exc)})
        shutil.rmtree(tmpdir, ignore_errors=True)
        job["tmpdir"] = None

    finally:
        q.put(None)


def run_download_ytdlp(job_id, payload):
    """Download from YouTube and other sources using yt-dlp."""
    job = jobs[job_id]
    q = job["queue"]
    tmpdir = job["tmpdir"]
    url = payload.get("url", "").strip()
    fmt = payload.get("fmt", "best").strip()
    start = payload.get("start", "").strip()
    end = payload.get("end", "").strip()
    custom_name = sanitize_filename(payload.get("fname", "").strip())

    try:
        # Check for ffmpeg and other dependencies
        has_ffmpeg = shutil.which("ffmpeg") is not None
        if not has_ffmpeg:
            sse(q, "log", {"msg": "⚠ ffmpeg not found — some features disabled"})

        # Build the output filename template
        if custom_name:
            outtmpl = str(tmpdir / f"{custom_name}.%(ext)s")
        else:
            outtmpl = str(tmpdir / "%(title)s.%(ext)s")

        # Determine if downloading audio only
        is_audio_only = fmt == "bestaudio/best"

        opts = {
            "quiet":               True,
            "no_warnings":         True,
            "no_cache_dir":        True,
            "outtmpl":             outtmpl,
            "format":              fmt,
            # Prefer widely-available containers so format selection doesn't
            # fail when a specific codec/container combination is absent.
            "format_sort":         ["res", "ext:mp4:m4a:webm:ogg"],
            "progress_hooks":      [make_progress_hook(q)],
            "postprocessor_hooks": [make_postprocessor_hook(q)],
        }

        if COOKIES_PATH.exists():
            opts["cookiefile"] = str(COOKIES_PATH)
            sse(q, "log", {"msg": "Using cookies"})
        else:
            sse(q, "log", {"msg": "⚠ No cookies — may hit bot detection"})

        if is_audio_only:
            if has_ffmpeg:
                opts["postprocessors"] = [{
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "mp3",
                    "preferredquality": "192",
                }]
            else:
                sse(q, "log", {"msg": "⚠ ffmpeg not found — audio will be in original format"})
                opts["format"] = "bestaudio"

        if "+" in fmt:
            if has_ffmpeg:
                opts["merge_output_format"] = "mp4"
            else:
                sse(q, "log", {"msg": "⚠ ffmpeg not found — falling back to pre-merged stream"})
                opts["format"] = "best"

        if (start or end) and has_ffmpeg:
            s_sec = parse_time(start)
            e_sec = parse_time(end) if end else None
            def _ranges(info, ctx):
                entry = {"start_time": s_sec}
                if e_sec is not None:
                    entry["end_time"] = e_sec
                return [entry]
            opts["download_ranges"]         = _ranges
            opts["force_keyframes_at_cuts"] = True
        elif (start or end) and not has_ffmpeg:
            sse(q, "log", {"msg": "⚠ Time trimming skipped — ffmpeg required"})

        sse(q, "log", {"msg": "Contacting servers..."})
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.cache.remove()
            info  = ydl.extract_info(url, download=False)
            title = info.get("title", "download")
            sse(q, "log", {"msg": f"📄 {title}"})
            # Re-use the already-fetched info dict for the actual download
            # instead of calling ydl.download([url]), which makes a second
            # round-trip and can get a different format list — the root cause
            # of "Requested format is not available" errors.
            ydl.process_ie_result(info, download=True)

        files = list(tmpdir.iterdir())
        if not files:
            raise RuntimeError("yt-dlp produced no output file.")
        out_file, out_name = finalize_output(tmpdir, files)
        job["file"]     = out_file
        job["filename"] = out_name
        sse(q, "done", {"job_id": job_id, "filename": out_name})

    except Exception as exc:
        job["error"] = str(exc)
        sse(q, "error", {"msg": "ERROR: {}".format(exc)})
        shutil.rmtree(tmpdir, ignore_errors=True)
        job["tmpdir"] = None

    finally:
        q.put(None)


def run_download(job_id, payload):
    """Route to the appropriate downloader (spotdl or yt-dlp)."""
    url = payload.get("url", "").strip()
    
    if is_spotify_url(url):
        run_download_spotdl(job_id, payload)
    else:
        run_download_ytdlp(job_id, payload)


# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET"])
def login_page():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return send_from_directory("static", "login.html")

@app.route("/login", methods=["POST"])
def login():
    ip = request.remote_addr
    if is_locked_out(ip):
        return jsonify({"error": "Too many failed attempts. Try again in 15 minutes."}), 429
    data     = request.get_json(force=True)
    password = data.get("password", "")
    if check_password_hash(PASSWORD_HASH, password):
        clear_failures(ip)
        session.clear()
        session["logged_in"] = True
        session.permanent    = True
        return jsonify({"ok": True})
    else:
        record_failure(ip)
        return jsonify({"error": "Wrong password. {} attempts left.".format(attempts_remaining(ip))}), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ── Cookie upload ─────────────────────────────────────────────────────────────
@app.route("/upload-cookies", methods=["POST"])
@api_login_required
def upload_cookies():
    f = request.files.get("cookies")
    if not f:
        return jsonify({"error": "No file provided"}), 400
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(COOKIES_PATH))
    return jsonify({"ok": True, "msg": "Cookies uploaded successfully!"})

@app.route("/cookies-status")
@api_login_required
def cookies_status():
    return jsonify({"exists": COOKIES_PATH.exists()})

# ── App routes ────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return send_from_directory("static", "index.html")

@app.route("/start", methods=["POST"])
@api_login_required
def start():
    sweep_stale_jobs()
    payload = request.get_json(force=True)
    job_id  = str(uuid.uuid4())
    tmpdir  = Path(tempfile.mkdtemp(prefix="downloader-"))
    with jobs_lock:
        jobs[job_id] = {
            "queue": queue.Queue(), "file": None, "filename": None,
            "error": None, "tmpdir": tmpdir, "created": time.time(),
        }
    threading.Thread(target=run_download, args=(job_id, payload), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/stream/<job_id>")
@api_login_required
def stream(job_id):
    job = jobs.get(job_id)
    if not job:
        return Response("job not found", status=404)
    q = job["queue"]
    def generate():
        while True:
            item = q.get()
            if item is None:
                break
            yield item
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/download/<job_id>")
@api_login_required
def download(job_id):
    job = jobs.get(job_id)
    if not job or not job["file"]:
        return Response("File not ready", status=404)
    filepath = job["file"]
    filename = job["filename"]
    tmpdir   = job.get("tmpdir")
    mime     = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    def stream_and_cleanup():
        try:
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            # Remove the whole job temp dir (covers both the single-file case
            # and the zipped multi-file case) rather than just the one file.
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            with jobs_lock:
                jobs.pop(job_id, None)
    return Response(
        stream_and_cleanup(),
        mimetype=mime,
        headers={
            "Content-Disposition": safe_disposition(filename),
            "Content-Length": str(os.path.getsize(filepath)),
        }
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print("  Multi-Downloader")
    print("  Open: http://localhost:{}".format(port))
    print("  Password: {}".format("SET" if os.environ.get("PASSWORD") else "NOT SET (default 'changeme' — set PASSWORD!)"))
    print("  Secure cookies: {}".format("on" if SECURE_COOKIES else "off (set SECURE_COOKIES=1 behind HTTPS)"))
    print("  Spotify support: {}".format("✓ enabled" if SPOTDL_AVAILABLE else "✗ spotdl not installed"))
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
