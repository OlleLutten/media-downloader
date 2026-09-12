import os
import json
import re
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

app = Flask(__name__)

# /downloads is the host directory:
# /mnt/Hem-NAS/media/UWTD-Nedladdningar
DOWNLOAD_DIR = Path("/downloads")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/uploads"))
TOKEN_FILE = Path(os.getenv("TV4_TOKEN_FILE", "/config/tv4play-token"))
DEFAULT_FOLDER = "Nedladdningar Osorterade"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
(DOWNLOAD_DIR / DEFAULT_FOLDER).mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}
lock = threading.Lock()

SVT_HOSTS = ("svtplay.se", "svt.se", "urplay.se", "ur.se")
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def choose_downloader(url, requested):
    host = re.sub(r"^www\.", "", re.split(r"/", url.split("://", 1)[-1])[0].lower())
    if any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS):
        return "yt-dlp"
    return "svtplay-dl"


def read_tv4_token():
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""


def save_tv4_token(token):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if token:
        temporary_file = TOKEN_FILE.with_suffix(".tmp")
        temporary_file.write_text(token + "\n", encoding="utf-8")
        temporary_file.replace(TOKEN_FILE)
    else:
        try:
            TOKEN_FILE.unlink()
        except FileNotFoundError:
            pass


def safe_folder_path(value):
    """Turn a user-entered relative folder into a safe path below DOWNLOAD_DIR."""
    value = (value or "").strip().replace("\\", "/")
    parts = []
    for part in value.split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        part = re.sub(r'[<>:"|?*\x00-\x1f]+', "_", part)
        part = part.strip(" .")
        if part:
            parts.append(part[:120])
    return Path(*parts) if parts else Path(DEFAULT_FOLDER)


def relative_folder(folder):
    path = safe_folder_path(folder)
    full = DOWNLOAD_DIR / path
    full.mkdir(parents=True, exist_ok=True)
    return path.as_posix()


def safe_upload_path(value):
    value = (value or "").strip().replace("\\", "/")
    parts = []
    for part in value.split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        part = re.sub(r'[<>:"|?*\x00-\x1f]+', "_", part).strip(" .")
        if part:
            parts.append(part[:120])
    return Path(*parts)


def safe_upload_file_path(value):
    path = safe_upload_path(value)
    if not path.parts:
        return None
    return path


def run_job(job_id, url, downloader, folder, quality):
    target_dir = DOWNLOAD_DIR / safe_folder_path(folder)
    target_dir.mkdir(parents=True, exist_ok=True)

    with lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["message"] = f"Startar {downloader}…"

    if downloader == "svtplay-dl":
        # svtplay-dl downloads subtitles by default; --all-subtitles asks it
        # to download all available subtitle tracks.
        cmd = ["svtplay-dl", "--output", str(target_dir), "--all-subtitles"]
        tv4_token = read_tv4_token()
        if tv4_token:
            cmd += ["--token", tv4_token]
        if quality != "best":
            # Keep compatibility with the existing UI's quality selector.
            cmd += ["--quality", quality]
        cmd.append(url)
    else:
        outtmpl = str(target_dir / "%(title)s [%(id)s].%(ext)s")
        cmd = [
            "yt-dlp", "--newline",
            "-o", outtmpl,
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", "all",
            "--sub-format", "best",
        ]
        if quality == "best":
            cmd += ["-f", "bv*+ba/b"]
        else:
            cmd += ["-f", f"bv*[height<={quality}]+ba/b[height<={quality}]"]
        cmd += ["--merge-output-format", "mp4", url]

    recent_output = []

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue

            recent_output.append(line)
            recent_output = recent_output[-20:]

            progress = None
            m = re.search(r"(\d+(?:\.\d+)?)%", line)
            if m:
                progress = float(m.group(1))
            else:
                # svtplay-dl often reports progress as [12/100].
                m = re.search(r"\[(\d+)\s*/\s*(\d+)\]", line)
                if m:
                    current, total = int(m.group(1)), int(m.group(2))
                    if total:
                        progress = current * 100 / total

            with lock:
                jobs[job_id]["message"] = line[-500:]
                jobs[job_id]["output"] = recent_output
                if progress is not None:
                    jobs[job_id]["progress"] = progress

        code = proc.wait()

        # Some downloader failures can be accompanied by a zero exit code.
        # Explicit error output should therefore override the exit code.
        error_patterns = (
            r"\\berror\\b",
            r"\\bfailed\\b",
            r"\\bfailure\\b",
            r"\\bunable to\\b",
            r"\\bexception\\b",
            r"http error",
            r"traceback",
        )
        output_text = "\\n".join(recent_output)
        explicit_error = any(
            re.search(pattern, output_text, re.IGNORECASE)
            for pattern in error_patterns
        )

        success = code == 0 and not explicit_error

        with lock:
            jobs[job_id]["output"] = recent_output
            if success:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["message"] = "Klar!"
            else:
                jobs[job_id]["status"] = "error"
                if code != 0:
                    jobs[job_id]["message"] = (
                        f"Nedladdningen misslyckades (kod {code})."
                    )
                else:
                    jobs[job_id]["message"] = (
                        "Nedladdningen misslyckades trots att processen avslutades utan felkod."
                    )
    except Exception as e:
        with lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["message"] = str(e)[:500]
            jobs[job_id]["output"] = recent_output


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    requested = data.get("downloader", "auto")
    quality = str(data.get("quality", "best"))
    folder = relative_folder(data.get("folder", ""))

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
            "id": job_id,
            "url": url,
            "downloader": downloader,
            "folder": folder,
            "status": "queued",
            "progress": 0,
            "message": "Väntar…",
            "output": [],
        }

    threading.Thread(
        target=run_job,
        args=(job_id, url, downloader, folder, quality),
        daemon=True,
    ).start()

    return jsonify(jobs[job_id])


@app.get("/api/jobs/<job_id>")
def job(job_id):
    with lock:
        item = jobs.get(job_id)
        if not item:
            return jsonify({"error": "Jobbet finns inte."}), 404
        return jsonify(item)


@app.get("/api/settings")
def settings():
    return jsonify({"tv4_token_configured": bool(read_tv4_token())})


@app.post("/api/settings")
def update_settings():
    data = request.get_json(silent=True) or {}
    token = str(data.get("tv4_token") or "").strip()
    try:
        save_tv4_token(token)
    except OSError:
        return jsonify({"error": "Kunde inte spara TV4 Play-token på servern."}), 500
    return jsonify({"tv4_token_configured": bool(token)})


@app.get("/api/upload-folders")
def upload_folders():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    folders = [""]
    for path in UPLOAD_DIR.rglob("*"):
        if path.is_dir():
            folders.append(path.relative_to(UPLOAD_DIR).as_posix())
    return jsonify(sorted(set(folders), key=str.casefold))


@app.post("/api/upload")
def upload():
    folder = safe_upload_path(request.form.get("folder", ""))
    target_dir = UPLOAD_DIR / folder
    target_dir.mkdir(parents=True, exist_ok=True)

    raw_paths = request.form.get("paths", "[]")
    try:
        paths = json.loads(raw_paths)
    except (TypeError, ValueError):
        return jsonify({"error": "Ogiltig filinformation."}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Välj minst en fil eller mapp."}), 400

    uploaded = 0
    try:
        for index, file in enumerate(files):
            relative_name = paths[index] if index < len(paths) else file.filename
            safe_name = safe_upload_file_path(relative_name)
            if safe_name is None:
                continue
            destination = UPLOAD_DIR / folder / safe_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            file.save(destination)
            uploaded += 1
    except OSError:
        return jsonify({"error": "Kunde inte spara filerna på servern."}), 500

    return jsonify({"uploaded": uploaded, "folder": (folder / Path(".")).as_posix() if folder.parts else ""})


@app.get("/api/folders")
def folders():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    folders = [Path(DEFAULT_FOLDER)]

    for p in DOWNLOAD_DIR.iterdir():
        if p.is_dir():
            rel = p.relative_to(DOWNLOAD_DIR)
            if len(rel.parts) == 1 and rel not in folders:
                folders.append(rel)

    folders = sorted({p.as_posix() for p in folders}, key=str.casefold)
    return jsonify(folders)


@app.get("/api/files")
def files():
    result = []
    if not DOWNLOAD_DIR.exists():
        return jsonify(result)

    for p in DOWNLOAD_DIR.rglob("*"):
        if p.is_file():
            rel = p.relative_to(DOWNLOAD_DIR).as_posix()
            try:
                size = p.stat().st_size
                mtime = p.stat().st_mtime
            except OSError:
                continue
            result.append({"name": rel, "size": size, "mtime": mtime})

    result.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(result[:200])


@app.get("/download/<path:name>")
def get_file(name):
    return send_from_directory(DOWNLOAD_DIR, name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
