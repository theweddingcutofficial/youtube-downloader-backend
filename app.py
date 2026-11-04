"""YouTube Downloader backend — works together with the YouTube Downloader website.

Run it on any computer or VPS, then put its address into the website's
"Backend URL" setting. The website then downloads videos for real.

Requires: Python 3.9+, ffmpeg on PATH.
Install:  pip install -r requirements.txt
Run:      python app.py            (serves on http://0.0.0.0:5000)

How extraction works (in order, first success wins):
1. yt-dlp directly (default player client, then android, then ios).
   Datacenter IPs (e.g. Render) are often bot-walled by YouTube, so
   this step can fail there even though it works from a home PC.
2. Piped public API fallback (wireway). The Piped server does the
   YouTube extraction on its own IP, which is not blocked, and this
   backend downloads the streams it returns and merges them.
3. Invidious public API fallback (chocolatemoo53), same idea — used
   when the Piped instance itself is unreachable (some hosts block it).
"""

import json
import os
import re
import subprocess
import tempfile
import urllib.request
import uuid

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)  # the website calls this API from the browser

DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "yt-downloader")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PIPED_API = "https://pipedapi.wireway.ch"
INVIDIOUS_API = "https://yt.chocolatemoo53.com"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def _ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if extra:
        opts.update(extra)
    return opts


def _video_id(url):
    """Extract the 11-char YouTube video id from a watch/shorts/embed URL."""
    m = re.search(r"[?&]v=([\w-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([\w-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"/(shorts|embed|live)/([\w-]{11})", url)
    if m:
        return m.group(2)
    return None


def _piped_streams(video_id):
    """Fetch stream data from the Piped API. Returns dict or None."""
    req = urllib.request.Request(
        f"{PIPED_API}/streams/{video_id}", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
    except Exception:
        return None
    if not data.get("videoStreams"):
        return None
    return data


def _info_ytdlp(url):
    """Try yt-dlp with several player clients. Returns data or raises."""
    last_error = None
    for client in (None, "android", "ios"):
        extra = {}
        if client:
            extra["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(extra)) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - try the next client
            last_error = exc
    raise last_error


def _info_from_piped(url):
    """Build the /api/info response from Piped data. None if unusable."""
    video_id = _video_id(url)
    if not video_id:
        return None
    data = _piped_streams(video_id)
    if not data:
        return None

    audio = [s for s in data.get("audioStreams", [])
             if s.get("url") and "audio/mp4" in s.get("mimeType", "")]
    if not audio:
        audio = [s for s in data.get("audioStreams", []) if s.get("url")]
    best_audio = None
    if audio:
        best_audio = max(audio, key=lambda s: s.get("bitrate") or 0)

    by_height = {}
    for s in data.get("videoStreams", []):
        if not s.get("url") or not s.get("height"):
            continue
        height = s["height"]
        combined = not s.get("videoOnly", True)
        rank = (
            1 if combined else 0,
            1 if "video/mp4" in s.get("mimeType", "") else 0,
            s.get("bitrate") or 0,
        )
        if height not in by_height or rank > by_height[height][0]:
            by_height[height] = (rank, s, combined)

    formats = []
    for height, (_rank, s, combined) in by_height.items():
        ext = "mp4" if "video/mp4" in s.get("mimeType", "") else "webm"
        if combined or best_audio is None:
            format_id = f"piped:{video_id}:{s['itag']}:"
        else:
            format_id = f"piped:{video_id}:{s['itag']}:{best_audio['itag']}"
        formats.append({
            "format_id": format_id,
            "ext": ext,
            "height": height,
            "label": f"{height}p",
            "filesize": None,
        })
    formats.sort(key=lambda x: x["height"], reverse=True)

    return {
        "title": data.get("title"),
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "uploader": data.get("uploader"),
        "formats": formats,
    }


def _download_url(url, dest):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)


def _safe_name(title, ext):
    name = re.sub(r'[\\/*?:"<>|]', "", title or "video").strip()[:80]
    return f"{name or 'video'}.{ext}"


def _download_piped(format_id, job):
    """Download a piped:<video_id>:<video_itag>:<audio_itag> format."""
    _tag, video_id, v_itag, a_itag = format_id.split(":")
    data = _piped_streams(video_id)
    if not data:
        raise RuntimeError("Piped lookup failed")
    video = next((s for s in data.get("videoStreams", [])
                  if str(s.get("itag")) == v_itag), None)
    if not video:
        raise RuntimeError("Video stream not found")
    ext = "mp4" if "video/mp4" in video.get("mimeType", "") else "webm"
    v_path = os.path.join(job, f"video.{ext}")
    _download_url(video["url"], v_path)
    if not a_itag:
        return v_path, _safe_name(data.get("title"), ext)
    audio = next((s for s in data.get("audioStreams", [])
                  if str(s.get("itag")) == a_itag), None)
    if not audio:
        return v_path, _safe_name(data.get("title"), ext)
    a_ext = "m4a" if "audio/mp4" in audio.get("mimeType", "") else "webm"
    a_path = os.path.join(job, f"audio.{a_ext}")
    _download_url(audio["url"], a_path)
    out_path = os.path.join(job, _safe_name(data.get("title"), ext))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", v_path, "-i", a_path,
         "-c", "copy", out_path],
        check=True, timeout=600)
    return out_path, os.path.basename(out_path)


def _invidious_data(video_id):
    """Fetch video data from the Invidious API. Returns dict or None."""
    req = urllib.request.Request(
        f"{INVIDIOUS_API}/api/v1/videos/{video_id}", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
    except Exception:
        return None
    if not data.get("adaptiveFormats"):
        return None
    return data


def _invidious_height(s):
    if s.get("height"):
        return s["height"]
    m = re.search(r"(\d{3,4})p", s.get("qualityLabel") or s.get("quality") or "")
    return int(m.group(1)) if m else None


def _info_from_invidious(url):
    """Build the /api/info response from Invidious data. None if unusable."""
    video_id = _video_id(url)
    if not video_id:
        return None
    data = _invidious_data(video_id)
    if not data:
        return None

    audio = [s for s in data.get("adaptiveFormats", [])
             if s.get("type", "").startswith("audio/")
             and "mp4" in s.get("type", "")]
    if not audio:
        audio = [s for s in data.get("adaptiveFormats", [])
                 if s.get("type", "").startswith("audio/")]
    best_audio = None
    if audio:
        best_audio = max(audio, key=lambda s: int(s.get("bitrate") or 0))

    by_height = {}
    for s in data.get("adaptiveFormats", []):
        if not s.get("type", "").startswith("video/"):
            continue
        height = _invidious_height(s)
        if not height or not s.get("url"):
            continue
        rank = (
            1 if "video/mp4" in s.get("type", "") else 0,
            int(s.get("bitrate") or 0),
        )
        if height not in by_height or rank > by_height[height][0]:
            by_height[height] = (rank, s)

    formats = []
    for height, (_rank, s) in by_height.items():
        ext = "mp4" if "video/mp4" in s.get("type", "") else "webm"
        if best_audio is None:
            format_id = f"invid:{video_id}:{s['itag']}:"
        else:
            format_id = f"invid:{video_id}:{s['itag']}:{best_audio['itag']}"
        formats.append({
            "format_id": format_id,
            "ext": ext,
            "height": height,
            "label": f"{height}p",
            "filesize": None,
        })
    formats.sort(key=lambda x: x["height"], reverse=True)

    return {
        "title": data.get("title"),
        "thumbnail": None,
        "duration": data.get("lengthSeconds"),
        "uploader": data.get("author"),
        "formats": formats,
    }


def _download_invidious(format_id, job):
    """Download an invid:<video_id>:<video_itag>:<audio_itag> format."""
    _tag, video_id, v_itag, a_itag = format_id.split(":")
    data = _invidious_data(video_id)
    if not data:
        raise RuntimeError("Invidious lookup failed")
    video = next((s for s in data.get("adaptiveFormats", [])
                  if str(s.get("itag")) == v_itag), None)
    if not video:
        raise RuntimeError("Video stream not found")
    ext = "mp4" if "video/mp4" in video.get("type", "") else "webm"
    v_path = os.path.join(job, f"video.{ext}")
    _download_url(video["url"], v_path)
    if not a_itag:
        return v_path, _safe_name(data.get("title"), ext)
    audio = next((s for s in data.get("adaptiveFormats", [])
                  if str(s.get("itag")) == a_itag), None)
    if not audio:
        return v_path, _safe_name(data.get("title"), ext)
    a_ext = "m4a" if "mp4" in audio.get("type", "") else "webm"
    a_path = os.path.join(job, f"audio.{a_ext}")
    _download_url(audio["url"], a_path)
    out_path = os.path.join(job, _safe_name(data.get("title"), ext))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", v_path, "-i", a_path,
         "-c", "copy", out_path],
        check=True, timeout=600)
    return out_path, os.path.basename(out_path)


@app.route("/api/info")
def info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400
    try:
        data = _info_ytdlp(url)
    except Exception:
        # Datacenter IPs get bot-walled by YouTube; fall back to the
        # public Piped API, then to Invidious.
        piped = _info_from_piped(url)
        if piped is not None:
            return jsonify(piped)
        invid = _info_from_invidious(url)
        if invid is not None:
            return jsonify(invid)
        return jsonify({"error": "Could not extract video info"}), 400

    all_formats = data.get("formats", [])

    # Best audio track to pair with video-only streams (prefer m4a/140).
    audio_only = [
        f for f in all_formats
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    best_audio = None
    for f in audio_only:
        if f.get("format_id") == "140":
            best_audio = f
            break
    if best_audio is None and audio_only:
        best_audio = max(
            audio_only,
            key=lambda f: f.get("abr") or f.get("tbr") or 0,
        )

    # YouTube now serves almost everything as separate video-only and
    # audio-only streams; combined streams are rare. Build one option
    # per height: a combined stream when one exists, otherwise the
    # best video-only stream at that height paired with the best
    # audio ("137+140") — /api/download merges them with ffmpeg.
    by_height = {}
    for f in all_formats:
        if f.get("vcodec") in (None, "none"):
            continue
        height = f.get("height")
        if not height:
            continue
        combined = f.get("acodec") not in (None, "none")
        current = by_height.get(height)
        # Prefer combined, then mp4, then the largest file (best bitrate).
        rank = (
            1 if combined else 0,
            1 if f.get("ext") == "mp4" else 0,
            f.get("filesize") or f.get("filesize_approx") or f.get("tbr") or 0,
        )
        if current is None or rank > current[0]:
            by_height[height] = (rank, f, combined)

    formats = []
    for height, (_rank, f, combined) in by_height.items():
        format_id = f["format_id"]
        filesize = f.get("filesize") or f.get("filesize_approx")
        if not combined and best_audio is not None:
            format_id = f["format_id"] + "+" + best_audio["format_id"]
            audio_size = best_audio.get("filesize") or best_audio.get("filesize_approx")
            if filesize and audio_size:
                filesize = filesize + audio_size
        formats.append({
            "format_id": format_id,
            "ext": "mp4",
            "height": height,
            "label": f"{height}p",
            "filesize": filesize,
        })
    formats.sort(key=lambda x: x["height"], reverse=True)

    return jsonify({
        "title": data.get("title"),
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "uploader": data.get("uploader"),
        "formats": formats,
    })


@app.route("/api/download")
def download():
    url = request.args.get("url", "").strip()
    format_id = request.args.get("format_id", "").strip()
    if not url or not format_id:
        return jsonify({"error": "Missing url or format_id parameter"}), 400

    job = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(job, exist_ok=True)

    if format_id.startswith("piped:"):
        try:
            path, name = _download_piped(format_id, job)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            return jsonify({"error": str(exc)}), 400
        return send_file(path, as_attachment=True, download_name=name)

    if format_id.startswith("invid:"):
        try:
            path, name = _download_invidious(format_id, job)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            return jsonify({"error": str(exc)}), 400
        return send_file(path, as_attachment=True, download_name=name)

    outtmpl = os.path.join(job, "%(title)s.%(ext)s")
    last_error = None
    for client in (None, "android", "ios"):
        extra = {
            "format": format_id,
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
        }
        if client:
            extra["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(extra)) as ydl:
                ydl.download([url])
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 - try the next client
            last_error = exc
    if last_error is not None:
        return jsonify({"error": str(last_error)}), 400

    files = [f for f in os.listdir(job) if os.path.isfile(os.path.join(job, f))]
    if not files:
        return jsonify({"error": "Download produced no file"}), 500
    path = os.path.join(job, files[0])
    return send_file(path, as_attachment=True, download_name=files[0])


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    # Landing page so opening http://localhost:5000 shows something useful
    # instead of a 404 — the real endpoints are /api/info and /api/download.
    return (
        "<h1>YouTube Downloader backend is running</h1>"
        "<p>Server is up. Check <a href='/api/health'>/api/health</a> "
        "for status.</p>"
        "<p>Now open the YouTube Downloader website and set its Backend URL "
        "to <b>http://localhost:5000</b>.</p>"
    )


if __name__ == "__main__":
    # Hosting platforms (e.g. Render) assign the port via $PORT.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
