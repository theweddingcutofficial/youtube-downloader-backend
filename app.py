"""YouTube Downloader backend — works together with the YouTube Downloader website.

Run it on any computer or VPS, then put its address into the website's
"Backend URL" setting. The website then downloads videos for real.

Requires: Python 3.9+, ffmpeg on PATH.
Install:  pip install -r requirements.txt
Run:      python app.py            (serves on http://0.0.0.0:5000)
"""

import os
import tempfile
import uuid

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)  # the website calls this API from the browser

DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "yt-downloader")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if extra:
        opts.update(extra)
    return opts


@app.route("/api/info")
def info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return jsonify({"error": str(exc)}), 400

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
    outtmpl = os.path.join(job, "%(title)s.%(ext)s")
    try:
        with yt_dlp.YoutubeDL(_ydl_opts({
            "format": format_id,
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
        })) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return jsonify({"error": str(exc)}), 400

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
