import os
import re
import shlex
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

app = Flask(__name__)

DOWNLOAD_DIR = Path("/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}
lock = threading.Lock()

SVT_HOSTS = ("svtplay.se", "svt.se", "urplay.se", "ur.se")


def choose_downloader(url, requested):
    if requested in ("yt-dlp", "svtplay-dl"):
        return requested
    host = re.sub(r"^www\.", "", re.split(r"/", url.split("://", 1)[-1])[0].lower())
    if any(host == h or host.endswith("." + h) for h in SVT_HOSTS):
        return "svtplay-dl"
    return "yt-dlp"


def safe_name(value):
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    return value.strip()[:180] or "download"


def run_job(job_id, url, downloader, subtitles, quality):
    with lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["message"] = f"Startar {downloader}…"

    if downloader == "svtplay-dl":
        cmd = ["svtplay-dl", "--output", str(DOWNLOAD_DIR)]
        if subtitles:
            cmd.append("--all-subtitles")
        if quality != "best":
            # svtplay-dl uses height in pixels for --quality
            cmd += ["--quality", quality]
        cmd.append(url)
    else:
        outtmpl = str(DOWNLOAD_DIR / "%(title)s [%(id)s].%(ext)s")
        cmd = ["yt-dlp", "--newline", "-o", outtmpl]
        if subtitles:
            cmd += ["--write-subs", "--sub-langs", "all"]
        if quality == "best":
            cmd += ["-f", "bv*+ba/b"]
        else:
            cmd += ["-f", f"bv*[height<={quality}]+ba/b[height<={quality}]"]
        cmd += ["--merge-output-format", "mp4", url]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        for line in proc.stdout:
            line = line.strip()
            if line:
                with lock:
                    jobs[job_id]["message"] = line[-500:]
                    # Basic percentage parsing; actual downloader output varies.
                    m = re.search(r"(\d+(?:\.\d+)?)%", line)
                    if m:
                        jobs[job_id]["progress"] = float(m.group(1))
        code = proc.wait()
        with lock:
            if code == 0:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["message"] = "Klar!"
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["message"] = f"Nedladdningen misslyckades (kod {code})."
    except Exception as e:
        with lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["message"] = str(e)[:500]


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    requested = data.get("downloader", "auto")
    subtitles = bool(data.get("subtitles", False))
    quality = str(data.get("quality", "best"))

    if not re.match(r"^https?://", url):
        return jsonify({"error": "Ange en giltig http/https-URL."}), 400

    if requested not in ("auto", "yt-dlp", "svtplay-dl"):
        return jsonify({"error": "Ogiltigt val av downloader."}), 400

    if quality not in ("best", "1080", "720", "480", "360"):
        return jsonify({"error": "Ogiltig kvalitet."}), 400

    downloader = choose_downloader(url, requested)
    job_id = uuid.uuid4().hex[:10]

    with lock:
        jobs[job_id] = {
            "id": job_id, "url": url, "downloader": downloader,
            "status": "queued", "progress": 0, "message": "Väntar…"
        }

    threading.Thread(
        target=run_job,
        args=(job_id, url, downloader, subtitles, quality),
        daemon=True
    ).start()

    return jsonify(jobs[job_id])


@app.get("/api/jobs/<job_id>")
def job(job_id):
    with lock:
        item = jobs.get(job_id)
        if not item:
            return jsonify({"error": "Jobbet finns inte."}), 404
        return jsonify(item)


@app.get("/api/files")
def files():
    result = []
    for p in sorted(DOWNLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file():
            result.append({"name": p.name, "size": p.stat().st_size})
    return jsonify(result[:100])


@app.get("/download/<path:name>")
def get_file(name):
    return send_from_directory(DOWNLOAD_DIR, name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
