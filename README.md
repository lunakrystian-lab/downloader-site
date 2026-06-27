# Multi-Downloader — Local Web App

Download YouTube, Spotify, and anything yt-dlp supports — right from your browser.

## Setup (one time)

1. Make sure you have **Python 3.8+** installed
2. Install dependencies:
   ```
   pip install flask yt-dlp
   ```
3. For Spotify links, also install:
   ```
   pip install spotdl
   ```
4. For Best Video quality or time trimming, install **ffmpeg**:
   - **Mac:** `brew install ffmpeg`
   - **Windows:** https://ffmpeg.org/download.html
   - **Linux:** `sudo apt install ffmpeg`

## Run it

```
python server.py
```

Then open **http://localhost:5000** in your browser.

## How it works

- Paste a URL → pick format → hit Download
- Files are saved to your **~/Downloads** folder
- Live progress bar + log shows what's happening in real time
- Spotify links automatically switch to spotdl
- The server only runs locally — nothing is sent anywhere
- - Made by Claude ai
  - 
