#!/usr/bin/env python3
"""
Multi-Downloader — Local Web Server
Run:  python server.py
Open: http://localhost:5000
"""

import os, sys, json, queue, threading, subprocess, shutil, tempfile, mimetypes
from pathlib import Path

def pip(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pkg])

try:
    from flask import Flask, request, Response, jsonify, send_from_directory, send_file
except ImportError:
    print("📦 Installing Flask...")
    pip("flask")
    from flask import Flask, request, Response, jsonify, send_from_directory, send_file

try:
    import yt_dlp
except ImportError:
    print("📦 Installing yt-dlp...")
    pip("yt-dlp")
    import yt_dlp

app = Flask(__name__, static_folder="static")

# job_id -> { "queue": Queue, "file": Path|None, "filename": str|None, "error": str|None }
jobs: dict[str, dict] = {}


# ── SSE helper ───────────────────────────────────────────────────────────────
def sse(q: queue.Queue, event: str, data: dict):
    q.put(f"event: {event}\ndata: {json.dumps(data)}\n\n")


def parse_time(value: str) -> float:
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


def make_progress_hook(q: queue.Queue):
    def hook(d):
        status = d.get("status")
        if status == "downloading":
            sse(q, "progress", {
                "pct":   d.get("_percent_str", "").strip(),
                "speed": d.get("_speed_str",   "").strip(),
                "eta":   d.get("_eta_str",     "").strip(),
            })
        elif status == "finished":
            sse(q, "progress", {"pct": "100%", "speed": "", "eta": "finishing…"})
    return hook


# ── Download worker ──────────────────────────────────────────────────────────
def run_download(job_id: str, payload: dict):
    job = jobs[job_id]
    q   = job["queue"]
    url       = payload["url"]
    fmt       = payload.get("fmt", "best")
    fname     = payload.get("fname", "").strip()
    start     = payload.get("start", "").strip()
    end       = payload.get("end",   "").strip()
    is_spot   = "spotify.com" in url
    has_ffmpeg = shutil.which("ffmpeg") is not None

    tmpdir = Path(tempfile.mkdtemp())

    try:
        if is_spot:
            # ── spotdl ──────────────────────────────────────────────────────
            if not shutil.which("spotdl"):
                sse(q, "log", {"msg": "📦 Installing spotdl…"})
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "--quiet", "spotdl"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            sse(q, "log", {"msg": "🎵 Starting Spotify download…"})
            proc = subprocess.Popen(
                ["spotdl", url, "--output", str(tmpdir)],
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
        outtmpl = str(tmpdir / (f"{fname}.%(ext)s" if fname else "%(title)s.%(ext)s"))

        is_audio_only = fmt == "bestaudio/best"

        opts = {
            "quiet":          False,
            "no_warnings":    False,
            "no_cache_dir":   True,
            "outtmpl":        outtmpl,
            "format":         fmt,
            "progress_hooks": [make_progress_hook(q)],
        }

        if is_audio_only:
            if has_ffmpeg:
                opts["postprocessors"] = [{
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "mp3",
                    "preferredquality": "192",
                }]
            else:
                sse(q, "log", {"msg": "⚠️  ffmpeg not found — can't convert to MP3. Install ffmpeg and retry."})

        if "+" in fmt:
            if has_ffmpeg:
                opts["merge_output_format"] = "mp4"
            else:
                sse(q, "log", {"msg": "⚠️  ffmpeg not found — falling back to pre-merged stream."})
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
            sse(q, "log", {"msg": "⚠️  Time trimming skipped — ffmpeg required."})

        sse(q, "log", {"msg": "⏳ Contacting servers…"})
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.cache.remove()
            info  = ydl.extract_info(url, download=False)
            title = info.get("title", "download")
            sse(q, "log", {"msg": f"📄 {title}"})
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
        sse(q, "error", {"msg": f"❌ {exc}"})
        shutil.rmtree(tmpdir, ignore_errors=True)

    finally:
        q.put(None)   # sentinel — closes SSE stream


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/start", methods=["POST"])
def start():
    import uuid
    payload = request.get_json(force=True)
    job_id  = str(uuid.uuid4())
    jobs[job_id] = {"queue": queue.Queue(), "file": None, "filename": None, "error": None}
    threading.Thread(target=run_download, args=(job_id, payload), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/stream/<job_id>")
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
                while chunk := f.read(65536):
                    yield chunk
        finally:
            # Clean up temp file + job after download
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
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(os.path.getsize(filepath)),
        }
    )


if __name__ == "__main__":
    print("=" * 50)
    print("  Multi-Downloader  —  Local Server")
    print("=" * 50)
    print("  Open: http://localhost:5000")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
