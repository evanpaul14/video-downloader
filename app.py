import json
import os
import queue
import re
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

app = Flask(__name__)

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Active download jobs: job_id -> {"queue": Queue, "status": str}
jobs: dict[str, dict] = {}


def run_download(job_id: str, url: str, fmt: str, output_dir: Path):
    q = jobs[job_id]["queue"]

    def emit(event: str, data: dict):
        q.put(f"event: {event}\ndata: {json.dumps(data)}\n\n")

    format_args = {
        "best": ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"],
        "1080": ["-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]", "--merge-output-format", "mp4"],
        "720": ["-f", "bestvideo[height<=720]+bestaudio/best[height<=720]", "--merge-output-format", "mp4"],
        "480": ["-f", "bestvideo[height<=480]+bestaudio/best[height<=480]", "--merge-output-format", "mp4"],
        "audio": ["-f", "bestaudio", "-x", "--audio-format", "mp3"],
    }.get(fmt, ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"])

    output_template = str(output_dir / "%(title)s.%(ext)s")

    cmd = ["yt-dlp", "--newline", "--progress", *format_args, "-o", output_template, url]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        filename = None
        for line in proc.stdout:
            line = line.strip()

            # Parse progress lines like: [download]  45.3% of  123.45MiB at  2.00MiB/s ETA 00:30
            m = re.search(r"(\d+\.?\d*)%\s+of\s+([\d\.]+\S+)\s+at\s+([\S]+)\s+ETA\s+(\S+)", line)
            if m:
                emit("progress", {
                    "percent": float(m.group(1)),
                    "size": m.group(2),
                    "speed": m.group(3),
                    "eta": m.group(4),
                })
                continue

            # Capture destination filename
            dest_match = re.search(r"\[(?:download|ffmpeg|Merger)\]\s+(?:Destination:|Merging formats into) \"?(.+?)\"?$", line)
            if dest_match:
                filename = dest_match.group(1)

            emit("log", {"text": line})

        proc.wait()

        if proc.returncode == 0:
            jobs[job_id]["status"] = "done"
            emit("done", {"filename": os.path.basename(filename) if filename else None})
        else:
            jobs[job_id]["status"] = "error"
            emit("error", {"text": "yt-dlp exited with an error."})

    except FileNotFoundError:
        jobs[job_id]["status"] = "error"
        emit("error", {"text": "yt-dlp is not installed. Run: pip install yt-dlp"})
    except Exception as e:
        jobs[job_id]["status"] = "error"
        emit("error", {"text": str(e)})
    finally:
        q.put(None)  # sentinel


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def start_download():
    body = request.get_json(force=True)
    url = (body.get("url") or "").strip()
    fmt = body.get("format", "best")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"queue": queue.Queue(), "status": "running"}

    t = threading.Thread(target=run_download, args=(job_id, url, fmt, DOWNLOADS_DIR), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def progress_stream(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        q = jobs[job_id]["queue"]
        while True:
            item = q.get()
            if item is None:
                break
            yield item

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/files")
def list_files():
    files = []
    for f in sorted(DOWNLOADS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            files.append({"name": f.name, "size": f.stat().st_size})
    return jsonify(files)


@app.route("/downloads/<path:filename>")
def serve_file(filename):
    return send_from_directory(DOWNLOADS_DIR.resolve(), filename, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)
