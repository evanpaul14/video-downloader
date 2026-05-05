# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
pip install yt-dlp        # only dependency (ffmpeg must also be installed system-wide)
python app.py             # serves on http://localhost:5000
```

There are no tests, no build step, and no linter configured.

## Architecture

The entire backend is a single file (`app.py`) built on Python's stdlib `ThreadingHTTPServer` — no web framework.

**Request flow:**
1. `POST /api/download` — validates the URL, creates a job entry (`jobs` dict keyed by UUID), spawns a daemon thread running `run_download()`, returns the `job_id`.
2. `GET /api/progress/<job_id>` — opens a Server-Sent Events stream. The download thread pushes SSE-formatted strings (`event: progress/log/done/error`) into a `queue.Queue`; this handler drains it and writes directly to the socket.
3. `POST /api/cancel/<job_id>` — sets `jobs[job_id]["cancelled"] = True` and calls `proc.terminate()`. The download thread checks the flag each line and cleans up partial files on exit.

**Format selection (`_build_format_args`):** Maps a quality string (`best`, `4k`, `1080`, `720`, `480`, `360`, `audio`) and a container string (`mp4`, `mkv`, `webm`, or audio formats) into yt-dlp `-f` format selectors. Audio-only quality uses `-x`/`--audio-format` instead.

**Frontend (`templates/index.html`):** Single self-contained HTML file with vanilla JS. Uses `EventSource` to consume the SSE stream. The quality `<select>` dynamically swaps container options between video formats and audio formats via `updateFormatOptions()`. Path traversal is prevented server-side by resolving paths relative to `DOWNLOADS_DIR` and rejecting anything that escapes it.

**JS runtime detection (`_js_runtime_args`):** Probes for `node`/`nodejs`/`deno` on `PATH` and passes `--js-runtimes` to yt-dlp when found (needed for some sites that require JS).

Downloads are saved to `downloads/` relative to the working directory where `app.py` is started.
