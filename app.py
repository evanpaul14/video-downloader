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

COOKIES_FILE = Path("cookies.txt")
HIDDEN_FILE = Path(".hidden_files.json")

TEMPLATE = (Path(__file__).parent / "templates" / "index.html").read_bytes()

jobs: dict[str, dict] = {}
_hidden_lock = threading.Lock()

_VIDEO_QUALITY = {
    "best": "bestvideo",
    "4k":   "bestvideo[height<=2160]",
    "1080": "bestvideo[height<=1080]",
    "720":  "bestvideo[height<=720]",
    "480":  "bestvideo[height<=480]",
    "360":  "bestvideo[height<=360]",
}

_AUDIO_FORMATS = {"mp3", "aac", "m4a", "flac", "opus"}

_MIME_TYPES = {
    "mp4":  "video/mp4",
    "mkv":  "video/x-matroska",
    "webm": "video/webm",
    "mp3":  "audio/mpeg",
    "aac":  "audio/aac",
    "m4a":  "audio/mp4",
    "flac": "audio/flac",
    "opus": "audio/ogg",
    "wav":  "audio/wav",
    "ogg":  "audio/ogg",
}


def _hidden_list() -> list[str]:
    with _hidden_lock:
        try:
            if HIDDEN_FILE.exists():
                return json.loads(HIDDEN_FILE.read_text())
        except Exception:
            pass
        return []


def _hidden_add(name: str):
    with _hidden_lock:
        try:
            hidden = json.loads(HIDDEN_FILE.read_text()) if HIDDEN_FILE.exists() else []
        except Exception:
            hidden = []
        if name not in hidden:
            hidden.append(name)
        HIDDEN_FILE.write_text(json.dumps(hidden))


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


def run_download(job_id: str, url: str, quality: str, fmt: str,
                 no_playlist: bool = False, embed_subs: bool = False):
    q = jobs[job_id]["queue"]

    def emit(event: str, data: dict):
        q.put(f"event: {event}\ndata: {json.dumps(data)}\n\n")

    playlist_flag = ["--no-playlist"] if no_playlist else []
    sub_flags = ["--write-subs", "--write-auto-subs", "--sub-langs", "en.*", "--embed-subs"] if embed_subs else []
    cookies_flags = ["--cookies", str(COOKIES_FILE.resolve())] if COOKIES_FILE.exists() else []

    cmd = [
        "yt-dlp", "--newline", "--progress",
        *_js_runtime_args(),
        *_build_format_args(quality, fmt),
        *playlist_flag,
        *sub_flags,
        *cookies_flags,
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


def _parse_range(header: str, file_size: int) -> tuple[int, int]:
    """Returns (start, end) byte positions, inclusive."""
    m = re.match(r"bytes=(\d*)-(\d*)", header)
    if not m:
        return 0, file_size - 1
    s, e = m.group(1), m.group(2)
    if s and e:
        return max(0, int(s)), min(file_size - 1, int(e))
    elif s:
        return max(0, int(s)), file_size - 1
    elif e:
        return max(0, file_size - int(e)), file_size - 1
    return 0, file_size - 1


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

    def _resolve_download_path(self, raw: str):
        """Resolve a filename to a safe path inside DOWNLOADS_DIR, or return None."""
        filepath = (DOWNLOADS_DIR / raw).resolve()
        try:
            filepath.relative_to(DOWNLOADS_DIR.resolve())
        except ValueError:
            return None
        return filepath

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

        elif path == "/api/hidden":
            self.send_json({"hidden": _hidden_list()})

        elif path == "/api/cookies":
            self.send_json({"active": COOKIES_FILE.exists()})

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

        elif path.startswith("/stream/"):
            filename = unquote(path[len("/stream/"):])
            filepath = self._resolve_download_path(filename)
            if filepath is None:
                self.send_json({"error": "Forbidden"}, 403)
                return
            if not filepath.is_file():
                self.send_json({"error": "Not found"}, 404)
                return
            ext = filepath.suffix.lstrip(".").lower()
            mime = _MIME_TYPES.get(ext, "application/octet-stream")
            file_size = filepath.stat().st_size
            range_header = self.headers.get("Range")
            if range_header:
                start, end = _parse_range(range_header, file_size)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(filepath, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    try:
                        while remaining > 0:
                            chunk = fh.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            else:
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(file_size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(filepath, "rb") as fh:
                    try:
                        while chunk := fh.read(65536):
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        elif path.startswith("/downloads/"):
            filename = unquote(path[len("/downloads/"):])
            filepath = self._resolve_download_path(filename)
            if filepath is None:
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
        length = int(self.headers.get("Content-Length", 0))

        if path == "/api/download":
            body = json.loads(self.rfile.read(length))
            url = (body.get("url") or "").strip()
            quality = body.get("quality", "best")
            fmt = body.get("format", "mp4")
            no_playlist = bool(body.get("no_playlist", False))
            embed_subs = bool(body.get("embed_subs", False))
            if not url:
                self.send_json({"error": "URL is required"}, 400)
                return
            job_id = str(uuid.uuid4())
            jobs[job_id] = {"queue": queue.Queue(), "cancelled": False}
            threading.Thread(
                target=run_download,
                args=(job_id, url, quality, fmt, no_playlist, embed_subs),
                daemon=True,
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

        elif path == "/api/cookies":
            body = json.loads(self.rfile.read(length))
            content = body.get("content", "")
            if not content.strip():
                self.send_json({"error": "Empty content"}, 400)
                return
            COOKIES_FILE.write_text(content)
            self.send_json({"ok": True})

        elif path == "/api/hidden":
            body = json.loads(self.rfile.read(length))
            name = body.get("name", "")
            if not name:
                self.send_json({"error": "name required"}, 400)
                return
            _hidden_add(name)
            self.send_json({"ok": True})

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path

        if path.startswith("/api/files/"):
            filename = unquote(path[len("/api/files/"):])
            filepath = self._resolve_download_path(filename)
            if filepath is None:
                self.send_json({"error": "Forbidden"}, 403)
                return
            if not filepath.is_file():
                self.send_json({"error": "Not found"}, 404)
                return
            filepath.unlink()
            self.send_json({"ok": True})

        elif path == "/api/cookies":
            if COOKIES_FILE.exists():
                COOKIES_FILE.unlink()
            self.send_json({"ok": True})

        else:
            self.send_json({"error": "Not found"}, 404)


if __name__ == "__main__":
    host, port = "0.0.0.0", 5000
    print(f"Video Downloader running at http://localhost:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
