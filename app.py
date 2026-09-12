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


def _filter_svt_subtitles(folder):
    """Keep only Swedish/English subtitle files after svtplay-dl download.

    svtplay-dl currently exposes --all-subtitles but no language-selection
    option, so we filter the resulting subtitle files by their language tag.
    This also keeps variants such as sv-caption and en-US.
    """
    subtitle_exts = {".srt", ".vtt", ".ttml", ".dfxp", ".xml", ".ass", ".sub", ".smi"}
    lang_re = re.compile(r"(?:^|[._-])(sv|swe|en|eng)(?:[._-]|$)", re.IGNORECASE)
    removed = []
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in subtitle_exts:
            continue
        if not lang_re.search(path.stem):
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed


def run_job(job_id, url, downloader, folder, quality, settings=None):
    settings = settings or {}
    target_dir = DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    with lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["message"] = f"Startar {downloader}…"

    all_episodes = bool(settings.get("all_episodes", False))
    all_last = int(settings.get("all_last", 0) or 0)
    effective_all_episodes = all_episodes or all_last > 0
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

            common = ["svtplay-dl", "--output", str(svt_output_dir), "--all-subtitles"]
            if tv4_token:
                common += ["--token", tv4_token]
            if quality != "best":
                common += ["--quality", quality]
            if chapters:
                common += ["--chapters"]
            if raw_subtitles:
                common += ["--raw-subtitles"]
            if thumbnail:
                common += ["--thumbnail"]

            if all_last > 0:
                # Do not rely on svtplay-dl's --all-last implementation here.
                # Enumerate episode URLs explicitly, newest first, then download
                # exactly the requested number of URLs. This avoids cases where
                # --all-last is ignored by a service-specific series-page parser.
                enum_cmd = ["svtplay-dl", "--all-episodes", "--get-only-episode-url", "--reverse"]
                if tv4_token:
                    enum_cmd += ["--token", tv4_token]
                if include_clips:
                    enum_cmd += ["--include-clips"]
                enum_cmd.append(url)
                display_enum = shlex.join(enum_cmd).replace(tv4_token, "***REDACTED***") if tv4_token else shlex.join(enum_cmd)
                _append_job_log(job_id, f"Urvalskommando: {display_enum}")
                _append_job_log(job_id, f"Hämtar episodlistan för att välja exakt senaste {all_last} avsnitt…")

                enum_proc = subprocess.Popen(
                    enum_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
                enum_lines = []
                for raw_line in enum_proc.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    enum_lines.append(line)
                    recent_output.append(line)
                    del recent_output[:-20]
                    _append_job_log(job_id, line)
                    with lock:
                        jobs[job_id]["message"] = line[-500:]
                        jobs[job_id]["output"] = list(recent_output)
                enum_code = enum_proc.wait()
                episode_urls = _extract_episode_urls(enum_lines)
                if enum_code != 0 or not episode_urls:
                    raise RuntimeError(
                        f"Kunde inte hämta episodlistan (kod {enum_code}). Hittade {len(episode_urls)} episod-URL:er."
                    )

                selected = episode_urls[:all_last]
                _append_job_log(job_id, f"Hittade {len(episode_urls)} avsnitt. Väljer exakt {len(selected)} senaste:")
                for i, episode_url in enumerate(selected, 1):
                    _append_job_log(job_id, f"  {i}. {episode_url}")

                total = len(selected)
                for index, episode_url in enumerate(selected):
                    cmd = list(common) + [episode_url]
                    display = shlex.join(cmd).replace(tv4_token, "***REDACTED***") if tv4_token else shlex.join(cmd)
                    _append_job_log(job_id, f"Startar avsnitt {index + 1}/{total}")
                    _append_job_log(job_id, f"Kommando: {display}")
                    # _run_command logs the command too, so temporarily run the
                    # actual command directly through the helper with redaction
                    # handled by the log append below.
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
                        p = None
                        m = re.search(r"(\d+(?:\.\d+)?)%", line)
                        if m:
                            p = float(m.group(1))
                        else:
                            m = re.search(r"\[(\d+)\s*/\s*(\d+)\]", line)
                            if m:
                                cur, tot = int(m.group(1)), int(m.group(2))
                                if tot:
                                    p = cur * 100 / tot
                        with lock:
                            jobs[job_id]["message"] = line[-500:]
                            jobs[job_id]["output"] = list(recent_output)
                            if p is not None:
                                jobs[job_id]["progress"] = (index + p / 100.0) * 100.0 / total
                    code = proc.wait()
                    if code != 0:
                        raise RuntimeError(f"Avsnitt {index + 1}/{total} misslyckades (kod {code}).")

            elif effective_all_episodes:
                cmd = list(common) + ["--all-episodes"]
                if include_clips:
                    cmd += ["--include-clips"]
                display = shlex.join(cmd).replace(tv4_token, "***REDACTED***") if tv4_token else shlex.join(cmd)
                _append_job_log(job_id, f"Kommando: {display}")
                code = _run_command(job_id, cmd, recent_output)
                if code != 0:
                    raise RuntimeError(f"Nedladdningen misslyckades (kod {code}).")
            else:
                cmd = list(common) + [url]
                display = shlex.join(cmd).replace(tv4_token, "***REDACTED***") if tv4_token else shlex.join(cmd)
                _append_job_log(job_id, f"Kommando: {display}")
                code = _run_command(job_id, cmd, recent_output)
                if code != 0:
                    raise RuntimeError(f"Nedladdningen misslyckades (kod {code}).")

            if svt_output_dir.exists():
                removed_subtitles = _filter_svt_subtitles(svt_output_dir)
                if removed_subtitles:
                    _append_job_log(job_id, f"Tog bort {len(removed_subtitles)} undertexter som inte är svenska eller engelska.")

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

        else:
            outtmpl = str(target_dir / "%(playlist_title|movies)s" / "%(title)s [%(id)s].%(ext)s")
            cmd = [
                "yt-dlp", "--newline", "-o", outtmpl,
                "--write-subs", "--write-auto-subs", "--sub-langs", "sv.*,en.*",
                # YouTube can rate-limit subtitle requests (HTTP 429). Slow
                # subtitle/request traffic down and retry HTTP failures with
                # exponential backoff instead of failing immediately.
                "--sleep-subtitles", "3",
                "--sleep-requests", "1",
                "--retries", "5",
                "--extractor-retries", "5",
                "--retry-sleep", "http:exp=2:30",
                "--ignore-errors",
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
            if effective_all_episodes and all_last > 0:
                cmd += ["--playlist-reverse", "--playlist-end", str(all_last)]
            if quality == "best":
                cmd += ["-f", "bv*+ba/b"]
            else:
                cmd += ["-f", f"bv*[height<={quality}]+ba/b[height<={quality}]"]
            cmd += ["--merge-output-format", "mp4", url]
            display = shlex.join(cmd)
            _append_job_log(job_id, f"Kommando: {display}")
            code = _run_command(job_id, cmd, recent_output)
            if code != 0:
                raise RuntimeError(f"Nedladdningen misslyckades (kod {code}).")

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


def create_job(url, downloader, quality, settings):
    job_id = uuid.uuid4().hex[:10]
    with lock:
        jobs[job_id] = {
            "id": job_id, "url": url, "downloader": downloader,
            "status": "queued", "progress": 0, "message": "Väntar…", "output": [], "log": [], "command": "",
        }
    threading.Thread(
        target=run_job,
        args=(job_id, url, downloader, "", quality, settings),
        daemon=True,
    ).start()
    return jobs[job_id]


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
    return jsonify(create_job(url, downloader, quality, settings))


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
    # This button explicitly means *all* episodes, even if the "Senaste NN"
    # setting contains a number.
    settings["all_last"] = 0
    downloader = choose_downloader(url, requested)
    item = create_job(url, downloader, quality, settings)
    return jsonify({"jobs": [item], "count": 1})


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
