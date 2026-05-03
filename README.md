# Video Downloader

A minimal self-hosted web UI for [yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a URL, pick a format, and the video downloads to the server's `downloads/` folder. No framework — pure Python stdlib + yt-dlp.

## Requirements

- Python 3.8+
- `yt-dlp`
- `ffmpeg` (required for merging video/audio streams — most package managers have it)

## Setup

```bash
pip install yt-dlp
python app.py
```

Open **http://localhost:5000** in your browser.

## Usage

1. Paste any URL supported by yt-dlp (YouTube, Vimeo, Twitter/X, etc.)
2. Choose a format
3. Click **Download** — live progress streams to the page
4. Grab the file from the **Downloaded Files** list when done

## Formats

| Option | Output |
|---|---|
| Best quality | Highest resolution MP4 |
| 1080p / 720p / 480p | Capped resolution MP4 |
| Audio only | MP3 |

## Notes

- Downloads are saved to `downloads/` relative to where you run `app.py`.
- The server uses Python's built-in `ThreadingHTTPServer` — no additional dependencies.
- For remote access, run behind a reverse proxy (nginx/Caddy) and add authentication.
