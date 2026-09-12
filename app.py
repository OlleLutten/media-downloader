import os
import json
import re
import shlex
import subprocess
import threading
import uuid
from pathlib import Path
from html import unescape
from urllib.request import Request, urlopen

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



def sanitize_folder_name(value):
    value = unescape(str(value or "")).strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:120]


def _svt_url_program_slug(url):
    """Get the programme slug from a normal SVT Play /video/... URL."""
    try:
        parts = [p for p in url.split("?", 1)[0].split("#", 1)[0].split("/") if p]
        for i, part in enumerate(parts):
            if part.lower() == "video" and i + 2 < len(parts):
                # /video/<video-id>/<programme>/<episode-slug>
                candidate = parts[i + 2]
                if candidate and candidate.lower() not in {"video", "play"}:
                    return candidate
    except Exception:
        pass
    return ""


def _svt_humanize_slug(value):
    """Turn common SVT URL slugs into a pleasant Swedish folder/file name."""
    value = unescape(str(value or "")).strip()
    value = value.replace("_", " ").replace("-", " ").replace(".", " ")
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return ""

    # Common Swedish words whose URL slugs lose diacritics.
    replacements = {
        "allsang": "allsång",
        "sang": "sång",
        "pa": "på",
        "for": "för",
        "fran": "från",
    }
    words = []
    for word in value.split():
        lower = word.lower()
        word = replacements.get(lower, word)
        words.append(word)

    # Programme names are normally written in title case. Keep short Swedish
    # connecting words lowercase after the first word.
    words = [w[:1].upper() + w[1:] if w else w for w in words]
    for i in range(1, len(words)):
        if words[i].lower() in {"på", "i", "och", "av", "för", "med", "från", "till", "om"}:
            words[i] = words[i].lower()
    return sanitize_folder_name(" ".join(words))


def get_svt_program_name(url):
    """Get a human-readable programme name, preferring the URL's programme slug.

    SVT's current single-episode pages do not always expose series metadata in
    the initial HTML. In those cases svtplay-dl can correctly download the
    episode but its --subfolder logic may classify it as a non-series and put it
    in 'movies'. The URL itself reliably contains the programme slug for normal
    /video/<id>/<programme>/<episode> links, so use that first.
    """
    slug = _svt_url_program_slug(url)
    name = _svt_humanize_slug(slug)
    if name:
        return name

    # Fallback for unusual pages: try metadata from the HTML.
    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; MediaDownloader/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", "ignore")
        for pattern in (
            r'<meta[^>]+(?:property|name)=["\'](?:og:series|twitter:label1)["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:series|twitter:label1)["\']',
        ):
            match = re.search(pattern, html, flags=re.I)
            if match:
                name = sanitize_folder_name(match.group(1))
                if name:
                    return name
    except Exception:
        pass
    return ""


def _humanize_download_name(value):
    """Make svtplay-dl's dot/slug-heavy names readable without changing IDs."""
    value = unescape(str(value or ""))
    suffix = ""
    match = re.match(r"^(.*?)(-[0-9a-z]+-svtplay)(\.[^.]+)$", value, re.I)
    if match:
        value, suffix = match.group(1), match.group(2) + match.group(3)
    else:
        ext = Path(value).suffix
        if ext:
            suffix = ext
            value = value[:-len(ext)]

    value = value.replace("_", " ").replace("-", " ").replace(".", " ")
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        return ""
    words = []
    replacements = {"allsang": "allsång", "sang": "sång", "pa": "på", "for": "för", "fran": "från"}
    for word in value.split():
        words.append(replacements.get(word.lower(), word))
    pretty = " ".join(words)
    return sanitize_folder_name(pretty) + suffix


def _append_job_log(job_id, line):
    with lock:
        log_output = jobs[job_id].setdefault("log", [])
        log_output.append(line)
        jobs[job_id]["log"] = log_output[-2000:]


def _run_command(job_id, cmd, recent_output, progress_base=0.0, progress_span=100.0):
    """Run a downloader command while continuously updating the job log/progress."""
    display_command = shlex.join(cmd)
    _append_job_log(job_id, f"Kommando: {display_command}")
    with lock:
        jobs[job_id]["command"] = display_command

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line:
            continue
        recent_output.append(line)
        del recent_output[:-20]
        _append_job_log(job_id, line)

        progress = None
        m = re.search(r"(\d+(?:\.\d+)?)%", line)
        if m:
            progress = float(m.group(1))
        else:
            m = re.search(r"\[(\d+)\s*/\s*(\d+)\]", line)
            if m:
                current, total = int(m.group(1)), int(m.group(2))
                if total:
                    progress = current * 100 / total
        with lock:
            jobs[job_id]["message"] = line[-500:]
            jobs[job_id]["output"] = list(recent_output)
            if progress is not None:
                jobs[job_id]["progress"] = progress_base + (progress_span * progress / 100.0)

    return_code = proc.wait()
    return return_code


def _extract_episode_urls(lines):
    urls = []
    seen = set()
    for line in lines:
        for match in re.findall(r"https?://[^\s<>\"']+", line):
            clean = match.rstrip(".,;)")
            if clean not in seen:
                seen.add(clean)
                urls.append(clean)
    return urls


def _enumerate_collection_urls(url, downloader, settings, job_id=None):
    """Return episode/video URLs in newest-first order."""
    include_clips = bool(settings.get("include_clips", False))
    tv4_token = read_tv4_token() if downloader == "svtplay-dl" else ""

    if downloader == "svtplay-dl":
        cmd = ["svtplay-dl", "--all-episodes", "--get-only-episode-url", "--reverse"]
        if tv4_token:
            cmd += ["--token", tv4_token]
        if include_clips:
            cmd += ["--include-clips"]
    else:
        cmd = ["yt-dlp", "--flat-playlist", "--playlist-reverse", "--print", "webpage_url", "--no-warnings", url]
        # The URL is already the final argument for yt-dlp, so append it below
        # only for the SVT branch.
        if job_id:
            _append_job_log(job_id, f"Urvalskommando: {shlex.join(cmd)}")
            _append_job_log(job_id, "Hämtar listan med avsnitt/video-URL:er…")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        lines = []
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if line:
                lines.append(line)
                if job_id:
                    _append_job_log(job_id, line)
        code = proc.wait()
        urls = _extract_episode_urls(lines)
        if code != 0 or not urls:
            raise RuntimeError(f"Kunde inte hämta listan (kod {code}). Hittade {len(urls)} URL:er.")
        return urls

    cmd.append(url)
    display = shlex.join(cmd).replace(tv4_token, "***REDACTED***") if tv4_token else shlex.join(cmd)
    if job_id:
        _append_job_log(job_id, f"Urvalskommando: {display}")
        _append_job_log(job_id, "Hämtar listan med avsnitt…")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line:
            continue
        lines.append(line)
        if job_id:
            _append_job_log(job_id, line)
    code = proc.wait()
    urls = _extract_episode_urls(lines)
    if code != 0 or not urls:
        raise RuntimeError(f"Kunde inte hämta episodlistan (kod {code}). Hittade {len(urls)} episod-URL:er.")
    return urls


def run_job(job_id, url, downloader, folder, quality, settings=None):
    settings = settings or {}
    target_dir = DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    with lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["message"] = f"Startar {downloader}…"

    include_clips = bool(settings.get("include_clips", False))
    chapters = True
    raw_subtitles = bool(settings.get("raw_subtitles", False))
    thumbnail = True
    embed_thumbnail = bool(settings.get("embed_thumbnail", False))
    recent_output = []
    tv4_token = read_tv4_token() if downloader == "svtplay-dl" else ""

    try:
        if downloader == "svtplay-dl":
            program_name = get_svt_program_name(url)
            svt_output_dir = target_dir / program_name if program_name else target_dir
            svt_output_dir.mkdir(parents=True, exist_ok=True)

            cmd = ["svtplay-dl", "--output", str(svt_output_dir), "--all-subtitles"]
            if tv4_token:
                cmd += ["--token", tv4_token]
            if quality != "best":
                cmd += ["--quality", quality]
            if chapters:
                cmd += ["--chapters"]
            if raw_subtitles:
                cmd += ["--raw-subtitles"]
            if thumbnail:
                cmd += ["--thumbnail"]
            if include_clips:
                cmd += ["--include-clips"]
            cmd.append(url)
        else:
            outtmpl = str(target_dir / "%(playlist_title|movies)s" / "%(title)s [%(id)s].%(ext)s")
            cmd = [
                "yt-dlp", "--newline", "-o", outtmpl,
                "--write-subs", "--write-auto-subs", "--sub-langs", "all",
            ]
            if raw_subtitles:
                cmd += ["--sub-format", "best"]
            else:
                cmd += ["--sub-format", "srt/vtt/ass/best"]
            if chapters:
                cmd += ["--embed-chapters"]
            if thumbnail:
                cmd += ["--write-thumbnail"]
            if embed_thumbnail:
                cmd += ["--embed-thumbnail"]
            if quality == "best":
                cmd += ["-f", "bv*+ba/b"]
            else:
                cmd += ["-f", f"bv*[height<={quality}]+ba/b[height<={quality}]"]
            cmd += ["--merge-output-format", "mp4", url]

        display = shlex.join(cmd).replace(tv4_token, "***REDACTED***") if tv4_token else shlex.join(cmd)
        _append_job_log(job_id, f"Kommando: {display}")
        code = _run_command(job_id, cmd, recent_output)
        if code != 0:
            raise RuntimeError(f"Nedladdningen misslyckades (kod {code}).")

        if downloader == "svtplay-dl":
            program_name = get_svt_program_name(url)
            svt_output_dir = target_dir / program_name if program_name else target_dir
            if program_name and svt_output_dir.exists():
                for path in list(svt_output_dir.iterdir()):
                    if not path.is_file():
                        continue
                    pretty = _humanize_download_name(path.name)
                    if pretty and pretty != path.name:
                        destination = path.with_name(pretty)
                        if not destination.exists():
                            try:
                                path.rename(destination)
                            except OSError:
                                pass

        with lock:
            jobs[job_id]["output"] = recent_output
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["message"] = "Klar!"
    except Exception as e:
        with lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["message"] = str(e)[:500]
            jobs[job_id]["output"] = recent_output


@app.get("/")
def index():
    return render_template("index.html")


def parse_download_settings(data):
    quality = str(data.get("quality", "best"))
    if quality not in ("best", "1080", "720", "480", "360"):
        raise ValueError("Ogiltig kvalitet.")
    try:
        all_last = int(data.get("all_last", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("Antal senaste avsnitt måste vara ett heltal.")
    if all_last < 0 or all_last > 999:
        raise ValueError("Antal senaste avsnitt måste vara mellan 0 och 999.")
    return {
        "chapters": bool(data.get("chapters", False)),
        "all_episodes": bool(data.get("all_episodes", False)),
        "all_last": all_last,
        "include_clips": bool(data.get("include_clips", False)),
        "raw_subtitles": bool(data.get("raw_subtitles", False)),
        "thumbnail": bool(data.get("thumbnail", False)),
        "embed_thumbnail": bool(data.get("embed_thumbnail", False)),
    }, quality


def create_job(url, downloader, quality, settings, title="Nedladdning"):
    job_id = uuid.uuid4().hex[:10]
    with lock:
        jobs[job_id] = {
            "id": job_id,
            "url": url,
            "downloader": downloader,
            "title": title,
            "status": "queued",
            "progress": 0,
            "message": "Väntar…",
            "output": [],
            "log": [],
            "command": "",
        }
    threading.Thread(
        target=run_job,
        args=(job_id, url, downloader, "", quality, settings),
        daemon=True,
    ).start()
    return jobs[job_id]


def create_collection_jobs(url, downloader, quality, settings, latest_n=0):
    """Enumerate a collection and create one independent job per item."""
    urls = _enumerate_collection_urls(url, downloader, settings)
    if latest_n > 0:
        urls = urls[:latest_n]
        mode_label = f"Senaste {len(urls)} avsnitt"
    else:
        mode_label = "Alla avsnitt"

    if not urls:
        raise RuntimeError("Hittade inga avsnitt/video-URL:er.")

    total = len(urls)
    created = []
    for index, item_url in enumerate(urls, 1):
        title = f"Avsnitt {index}/{total}" if latest_n > 0 else f"Avsnitt {index}/{total} (Alla avsnitt)"
        child_settings = dict(settings)
        child_settings["all_episodes"] = False
        child_settings["all_last"] = 0
        created.append(create_job(item_url, downloader, quality, child_settings, title=title))
    return created, mode_label


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    requested = data.get("downloader", "auto")
    if not re.match(r"^https?://", url):
        return jsonify({"error": "Ange en giltig http/https-URL."}), 400
    if requested not in ("auto", "yt-dlp", "svtplay-dl"):
        return jsonify({"error": "Ogiltigt val av downloader."}), 400
    try:
        settings, quality = parse_download_settings(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    downloader = choose_downloader(url, requested)
    try:
        if settings["all_last"] > 0:
            items, mode = create_collection_jobs(url, downloader, quality, settings, latest_n=settings["all_last"])
            return jsonify({"jobs": items, "count": len(items), "mode": mode})
        item = create_job(url, downloader, quality, settings)
        return jsonify(item)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/download-all")
def download_all():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    requested = data.get("downloader", "auto")
    if not re.match(r"^https?://", url):
        return jsonify({"error": "Ange en giltig http/https-URL."}), 400
    if requested not in ("auto", "yt-dlp", "svtplay-dl"):
        return jsonify({"error": "Ogiltigt val av downloader."}), 400
    try:
        settings, quality = parse_download_settings(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    settings["all_episodes"] = True
    settings["all_last"] = 0
    downloader = choose_downloader(url, requested)
    try:
        items, mode = create_collection_jobs(url, downloader, quality, settings, latest_n=0)
        return jsonify({"jobs": items, "count": len(items), "mode": mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


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
