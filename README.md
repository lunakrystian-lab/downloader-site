# Multi-Downloader — Web App

Download YouTube, Spotify, and anything yt-dlp supports — right from your browser.

## Setup (one time)

1. Make sure you have **Python 3.9+** installed.
2. Install everything in one shot (don't install `yt-dlp` and `spotdl` in
   separate `pip install` calls — doing that has caused real version
   conflicts between the two for other users):
   ```
   pip install -r requirements.txt
   ```
3. Install **ffmpeg** (needed for "Best Video" merging and time trimming):
   - **Mac:** `brew install ffmpeg`
   - **Windows:** https://ffmpeg.org/download.html
   - **Linux:** `sudo apt install ffmpeg`
   - **Termux (Android):** `pkg install ffmpeg`
4. Install **Deno** (needed for YouTube downloads to work reliably).
   As of late 2025, YouTube's anti-download JS challenges got complex enough
   that yt-dlp's built-in interpreter isn't sufficient on its own anymore —
   it now shells out to a real JS runtime, and Deno is the one it looks for
   by default. Without it, some videos will fail or only offer limited
   quality.
   - **Mac:** `brew install deno`
   - **Windows:** `winget install --id=DenoLand.Deno`
   - **Linux:** `curl -fsSL https://deno.land/install.sh | sh`
   - **Termux (Android):** `pkg install deno` (or use a standalone binary if
     that's unavailable on your device's architecture)
   - For the **Spotify** path specifically, spotdl can fetch its own local
     copy instead of a system install: `spotdl --download-deno`

## Run it

```
python server.py
```

Then open **http://localhost:5000** in your browser. You'll be asked for a
password before you can use it — see below.

## Password protection

Every route is gated behind a password (the login page, brute-force lockout,
etc. are there because this app is also set up to be deployed somewhere
reachable by other people — see `railway.toml` / `Procfile`). Set it via
environment variables before running:

```
PASSWORD=something-only-you-know
SECRET_KEY=a-long-random-string
python server.py
```

**If you don't set `PASSWORD`, it defaults to `changeme`.** That's fine for
poking around on your own machine, but if you deploy this anywhere
publicly reachable, set a real password — the default is well known.

If you deploy behind HTTPS (Railway, Render, etc.), also set:

```
SECURE_COOKIES=1
```

so the session cookie is marked `Secure`. Leave it unset for local
`http://localhost` use — with it forced on, login can silently fail to
"stick" in some browsers over plain HTTP.

## How it works

- Paste a URL → pick a format → hit Download.
- A live progress bar + log shows what's happening in real time over
  server-sent events.
- Spotify links automatically switch to spotdl. Albums and playlists are
  bundled into a single `.zip` for download.
- The finished file streams straight to your browser's normal
  download — nothing is kept on the server afterward.
- Upload a `cookies.txt` (exported from a logged-in YouTube session) from
  the in-app card if you're hitting bot-detection walls.

## Deploying elsewhere

`Procfile` and `railway.toml` are set up for Railway specifically (the
`nixPkgs` list already includes `ffmpeg` and `deno` for you). If you deploy
to a different platform, make sure both of those, plus a `PASSWORD`,
`SECRET_KEY`, and `SECURE_COOKIES=1`, are present in that environment.
