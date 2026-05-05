# Video Downloader

A minimal self-hosted web UI for [yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a URL, pick a format, and the video downloads to the server's `downloads/` folder. No framework — pure Python stdlib + yt-dlp.

## Requirements

- Python 3.8+
- `yt-dlp` — `pip install yt-dlp`
- `ffmpeg` (required for merging video/audio streams — most package managers have it)

## Setup

```bash
pip install yt-dlp
python app.py
```

Open **http://localhost:5000** in your browser.

## Features

- **Quality selection** — Best, 4K, 1080p, 720p, 480p, 360p, or audio-only
- **Container formats** — MP4, MKV, WebM (video) · MP3, AAC, M4A, FLAC (audio)
- **Playlist support** — download full playlists or force single-video mode
- **English subtitles** — downloads and embeds English subtitles (manual + auto-generated); shown in the in-browser player with YouTube-style styling
- **In-browser player** — stream any downloaded file directly; video and audio supported
- **Cookies** — paste a `cookies.txt` to access age-restricted or member-only content
- **File management** — download, delete, or hide files from the list; live progress with cancel support

## Usage

1. Paste any URL supported by yt-dlp (YouTube, Vimeo, Twitter/X, etc.)
2. Choose quality and container format
3. Optionally check **Single video only** (skip playlist) or **Subtitles**
4. Click **Download** — live progress and logs stream to the page
5. Use the **Play** button to stream in-browser, or **Save** to download the file

## Notes

- Downloads are saved to `downloads/` relative to where you run `app.py`.
- The server uses Python's built-in `ThreadingHTTPServer` — no additional dependencies beyond yt-dlp.
- For remote access, run behind a reverse proxy (nginx/Caddy) and add authentication.
