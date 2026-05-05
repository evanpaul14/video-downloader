import json
import os
import queue
import re
import shutil
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

TEMPLATE = (Path(__file__).parent / "templates" / "index.html").read_bytes()

jobs: dict[str, dict] = {}

_VIDEO_QUALITY = {
    "best": "bestvideo",
    "4k":   "bestvideo[height<=2160]",
    "1080": "bestvideo[height<=1080]",
    "720":  "bestvideo[height<=720]",
    "480":  "bestvideo[height<=480]",
    "360":  "bestvideo[height<=360]",
}

_AUDIO_FORMATS = {"mp3", "aac", "m4a", "flac", "opus"}


def _build_format_args(quality: str, fmt: str) -> list[str]:
    if quality == "audio":
        audio_fmt = fmt if fmt in _AUDIO_FORMATS else "mp3"
        return ["-f", "bestaudio/best", "-x", "--audio-format", audio_fmt]
    vf = _VIDEO_QUALITY.get(quality, "bestvideo")
    if fmt == "webm":
        return ["-f", f"{vf}[ext=webm]+bestaudio[ext=webm]/best", "--merge-output-format", "webm"]
    elif fmt == "mkv":
        return ["-f", f"{vf}+bestaudio/best", "--merge-output-format", "mkv"]
    else:
        return ["-f", f"{vf}+bestaudio/best", "--merge-output-format", "mp4"]


def _js_runtime_args():
    for rt in ("node", "nodejs", "deno"):
        path = shutil.which(rt)
        if path:
            return ["--js-runtimes", f"{rt}:{path}"]
    return []


def run_download(job_id: str, url: str, quality: str, fmt: str, no_playlist: bool = False):
    q = jobs[job_id]["queue"]

    def emit(event: str, data: dict):
        q.put(f"event: {event}\ndata: {json.dumps(data)}\n\n")

    playlist_flag = ["--no-playlist"] if no_playlist else []
    cmd = [
        "yt-dlp", "--newline", "--progress",
        *_js_runtime_args(),
        *_build_format_args(quality, fmt),
        *playlist_flag,
        "-o", str(DOWNLOADS_DIR / "%(title)s.%(ext)s"),
        url,
    ]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        jobs[job_id]["proc"] = proc
        filename = None
        created_files: list[str] = []
        for line in proc.stdout:
            if jobs[job_id].get("cancelled"):
                break
            line = line.strip()
            m = re.search(r"(\d+\.?\d*)%\s+of\s+~?\s*([\d\.]+\S+)\s+at\s+(\S+)\s+ETA\s+(\S+)", line)
            if m:
                emit("progress", {
                    "percent": float(m.group(1)),
                    "size":    m.group(2),
                    "speed":   m.group(3),
                    "eta":     m.group(4),
                })
                continue
            dm = re.search(
                r"\[(?:download|ffmpeg|Merger)\]\s+(?:Destination:|Merging formats into) \"?(.+?)\"?$",
                line,
            )
            if dm:
                created_files.append(dm.group(1))
                filename = dm.group(1)
            emit("log", {"text": line})
        proc.wait()
        if jobs[job_id].get("cancelled"):
            for f in created_files:
                try:
                    Path(f).unlink(missing_ok=True)
                except OSError:
                    pass
            emit("error", {"text": "Download cancelled."})
        elif proc.returncode == 0:
            emit("done", {"filename": os.path.basename(filename) if filename else None})
        else:
            emit("error", {"text": "yt-dlp exited with an error."})
    except FileNotFoundError:
        emit("error", {"text": "yt-dlp not found — run: pip install yt-dlp"})
    except Exception as e:
        emit("error", {"text": str(e)})
    finally:
        q.put(None)  # sentinel


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default per-request log

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(TEMPLATE)))
            self.end_headers()
            self.wfile.write(TEMPLATE)

        elif path == "/api/files":
            files = [
                {"name": f.name, "size": f.stat().st_size}
                for f in sorted(DOWNLOADS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
                if f.is_file()
            ]
            self.send_json(files)

        elif path.startswith("/api/progress/"):
            job_id = path[len("/api/progress/"):]
            if job_id not in jobs:
                self.send_json({"error": "Job not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = jobs[job_id]["queue"]
            try:
                while True:
                    try:
                        item = q.get(timeout=20)
                    except queue.Empty:
                        # Keepalive comment — prevents proxy/browser from closing idle SSE connections
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if item is None:
                        break
                    self.wfile.write(item.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif path.startswith("/downloads/"):
            filename = unquote(path[len("/downloads/"):])
            filepath = (DOWNLOADS_DIR / filename).resolve()
            try:
                filepath.relative_to(DOWNLOADS_DIR.resolve())
            except ValueError:
                self.send_json({"error": "Forbidden"}, 403)
                return
            if not filepath.is_file():
                self.send_json({"error": "Not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{filepath.name}"')
            self.send_header("Content-Length", str(filepath.stat().st_size))
            self.end_headers()
            with open(filepath, "rb") as fh:
                try:
                    while chunk := fh.read(65536):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/download":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            url = (body.get("url") or "").strip()
            quality = body.get("quality", "best")
            fmt = body.get("format", "mp4")
            no_playlist = bool(body.get("no_playlist", False))
            if not url:
                self.send_json({"error": "URL is required"}, 400)
                return
            job_id = str(uuid.uuid4())
            jobs[job_id] = {"queue": queue.Queue(), "cancelled": False}
            threading.Thread(
                target=run_download, args=(job_id, url, quality, fmt, no_playlist), daemon=True
            ).start()
            self.send_json({"job_id": job_id})

        elif path.startswith("/api/cancel/"):
            job_id = path[len("/api/cancel/"):]
            if job_id not in jobs:
                self.send_json({"error": "Job not found"}, 404)
                return
            jobs[job_id]["cancelled"] = True
            proc = jobs[job_id].get("proc")
            if proc and proc.poll() is None:
                proc.terminate()
            self.send_json({"ok": True})

        else:
            self.send_json({"error": "Not found"}, 404)


    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/files/"):
            filename = unquote(path[len("/api/files/"):])
            filepath = (DOWNLOADS_DIR / filename).resolve()
            try:
                filepath.relative_to(DOWNLOADS_DIR.resolve())
            except ValueError:
                self.send_json({"error": "Forbidden"}, 403)
                return
            if not filepath.is_file():
                self.send_json({"error": "Not found"}, 404)
                return
            filepath.unlink()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "Not found"}, 404)


if __name__ == "__main__":
    host, port = "0.0.0.0", 5000
    print(f"Video Downloader running at http://localhost:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
