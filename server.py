#!/usr/bin/env python3
"""
Multi-Downloader — Web Server
Env vars: PASSWORD, SECRET_KEY
"""

import os, sys, json, queue, threading, subprocess, shutil, tempfile, mimetypes, time
from pathlib import Path
from functools import wraps
from urllib.parse import quote
from flask import (Flask, request, Response, jsonify,
                   send_from_directory, session, redirect, url_for)
from werkzeug.security import generate_password_hash, check_password_hash
import yt_dlp

# ── Config ────────────────────────────────────────────────────────────────────
PASSWORD      = os.environ.get("PASSWORD", "changeme")
SECRET_KEY    = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
COOKIES_PATH  = Path("/app/cookies.txt")
PASSWORD_HASH = generate_password_hash(PASSWORD)

app = Flask(__name__, static_folder="static")
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)

# ── Brute-force protection ────────────────────────────────────────────────────
failed_attempts: dict = {}
MAX_ATTEMPTS  = 5
LOCKOUT_SECS  = 60 * 15

def is_locked_out(ip):
    now = time.time()
    attempts = [t for t in failed_attempts.get(ip, []) if now - t < LOCKOUT_SECS]
    failed_attempts[ip] = attempts
    return len(attempts) >= MAX_ATTEMPTS

def record_failure(ip):
    failed_attempts.setdefault(ip, []).append(time.time())

def clear_failures(ip):
    failed_attempts.pop(ip, None)

# ── Auth ──────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

# ── Jobs ──────────────────────────────────────────────────────────────────────
jobs: dict = {}

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

def make_progress_hook(q):
    def hook(d):
        status = d.get("status")
        if status == "downloading":
            sse(q, "progress", {
                "pct":   d.get("_percent_str", "").strip(),
                "speed": d.get("_speed_str",   "").strip(),
                "eta":   d.get("_eta_str",     "").strip(),
            })
        elif status == "finished":
            sse(q, "progress", {"pct": "100%", "speed": "", "eta": "finishing..."})
    return hook

def safe_disposition(filename):
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip()
    if not ascii_name:
        ascii_name = "download"
    encoded = quote(filename, safe="")
    return "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(ascii_name, encoded)

# ── Download worker ───────────────────────────────────────────────────────────
def run_download(job_id, payload):
    job = jobs[job_id]
    q   = job["queue"]
    url        = payload["url"]
    fmt        = payload.get("fmt", "best")
    fname      = payload.get("fname", "").strip()
    start      = payload.get("start", "").strip()
    end        = payload.get("end",   "").strip()
    is_spot    = "spotify.com" in url
    has_ffmpeg = shutil.which("ffmpeg") is not None

    tmpdir = Path(tempfile.mkdtemp())

    try:
        if is_spot:
            sse(q, "log", {"msg": "Spotify download starting..."})
            proc = subprocess.Popen(
                [sys.executable, "-m", "spotdl", url, "--output", str(tmpdir)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    sse(q, "log", {"msg": line})
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError("spotdl exited with an error.")
            files = list(tmpdir.iterdir())
            if not files:
                raise RuntimeError("spotdl produced no files.")
            out_file = files[0]
            job["file"]     = out_file
            job["filename"] = out_file.name
            sse(q, "done", {"job_id": job_id, "filename": out_file.name})
            return

        # ── yt-dlp ──────────────────────────────────────────────────────────
        outtmpl       = str(tmpdir / ("{}.%(ext)s".format(fname) if fname else "%(title)s.%(ext)s"))
        is_audio_only = fmt == "bestaudio/best"

        opts = {
            "quiet":          False,
            "no_warnings":    False,
            "no_cache_dir":   True,
            "outtmpl":        outtmpl,
            "format":         fmt,
            "progress_hooks": [make_progress_hook(q)],
        }

        if COOKIES_PATH.exists():
            opts["cookiefile"] = str(COOKIES_PATH)
            sse(q, "log", {"msg": "Using cookies"})
        else:
            sse(q, "log", {"msg": "No cookies — may hit bot detection"})

        if is_audio_only:
            if has_ffmpeg:
                opts["postprocessors"] = [{
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "mp3",
                    "preferredquality": "192",
                }]
            else:
                sse(q, "log", {"msg": "ffmpeg not found — audio will be in original format"})
                opts["format"] = "bestaudio"

        if "+" in fmt:
            if has_ffmpeg:
                opts["merge_output_format"] = "mp4"
            else:
                sse(q, "log", {"msg": "ffmpeg not found — falling back to pre-merged stream"})
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
            sse(q, "log", {"msg": "Time trimming skipped — ffmpeg required"})

        sse(q, "log", {"msg": "Contacting servers..."})
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.cache.remove()
            info  = ydl.extract_info(url, download=False)
            title = info.get("title", "download")
            sse(q, "log", {"msg": title})
            ydl.download([url])

        files = list(tmpdir.iterdir())
        if not files:
            raise RuntimeError("yt-dlp produced no output file.")
        out_file = files[0]
        job["file"]     = out_file
        job["filename"] = out_file.name
        sse(q, "done", {"job_id": job_id, "filename": out_file.name})

    except Exception as exc:
        job["error"] = str(exc)
        sse(q, "error", {"msg": "ERROR: {}".format(exc)})
        shutil.rmtree(tmpdir, ignore_errors=True)

    finally:
        q.put(None)

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
        remaining = MAX_ATTEMPTS - len(failed_attempts.get(ip, []))
        return jsonify({"error": "Wrong password. {} attempts left.".format(remaining)}), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ── Cookie upload ─────────────────────────────────────────────────────────────
@app.route("/upload-cookies", methods=["POST"])
@login_required
def upload_cookies():
    f = request.files.get("cookies")
    if not f:
        return jsonify({"error": "No file provided"}), 400
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(COOKIES_PATH))
    return jsonify({"ok": True, "msg": "Cookies uploaded successfully!"})

@app.route("/cookies-status")
@login_required
def cookies_status():
    return jsonify({"exists": COOKIES_PATH.exists()})

# ── App routes ────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return send_from_directory("static", "index.html")

@app.route("/start", methods=["POST"])
@login_required
def start():
    import uuid
    payload = request.get_json(force=True)
    job_id  = str(uuid.uuid4())
    jobs[job_id] = {"queue": queue.Queue(), "file": None, "filename": None, "error": None}
    threading.Thread(target=run_download, args=(job_id, payload), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/stream/<job_id>")
@login_required
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
@login_required
def download(job_id):
    job = jobs.get(job_id)
    if not job or not job["file"]:
        return Response("File not ready", status=404)
    filepath = job["file"]
    filename = job["filename"]
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
            try:
                filepath.unlink(missing_ok=True)
                filepath.parent.rmdir()
            except Exception:
                pass
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
    print("  Password: {}".format("SET" if os.environ.get("PASSWORD") else "NOT SET"))
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
