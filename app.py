import os
import shutil
import tempfile

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory, after_this_request
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)  # GitHub Pages থেকে API কল করার জন্য

# সার্ভার বাঁচাতে সর্বোচ্চ ভিডিও দৈর্ঘ্য (সেকেন্ড), Render-এ env দিয়ে বদলানো যায়
MAX_DURATION = int(os.environ.get("MAX_DURATION", 1800))

# ffmpeg ছাড়াই চলে এমন ফরম্যাট
FORMATS = {
    "best": "best[ext=mp4]/best",
    "720": "best[height<=720][ext=mp4]/best[height<=720]/best",
    "480": "best[height<=480][ext=mp4]/best[height<=480]/best",
    "360": "best[height<=360][ext=mp4]/best[height<=360]/best",
    "audio": "bestaudio[ext=m4a]/bestaudio",
}

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
}


def valid_url(url):
    return isinstance(url, str) and url.startswith(("http://", "https://")) and len(url) < 2000


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/info", methods=["POST"])
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="সঠিক লিংক দিন"), 400
    try:
        with yt_dlp.YoutubeDL({**BASE_OPTS, "skip_download": True}) as ydl:
            data = ydl.extract_info(url, download=False)
        return jsonify(
            title=data.get("title", "ভিডিও"),
            thumbnail=data.get("thumbnail"),
            duration=data.get("duration"),
            uploader=data.get("uploader"),
        )
    except Exception:
        return jsonify(error="ভিডিও খুঁজে পাওয়া যায়নি বা এই লিংক সাপোর্টেড নয়"), 400


@app.route("/api/download")
def download():
    url = request.args.get("url", "").strip()
    quality = request.args.get("q", "best")
    if not valid_url(url) or quality not in FORMATS:
        return jsonify(error="ভুল অনুরোধ"), 400

    tmp = tempfile.mkdtemp()

    @after_this_request
    def cleanup(response):
        shutil.rmtree(tmp, ignore_errors=True)
        return response

    opts = {
        **BASE_OPTS,
        "format": FORMATS[quality],
        "outtmpl": os.path.join(tmp, "%(title).80s.%(ext)s"),
        "match_filter": lambda info, **kw: (
            "ভিডিও অনেক বড়" if (info.get("duration") or 0) > MAX_DURATION else None
        ),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = os.listdir(tmp)
        if not files:
            raise RuntimeError("no file")
        path = os.path.join(tmp, files[0])
        return send_file(path, as_attachment=True, download_name=files[0])
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error="ডাউনলোড ব্যর্থ হয়েছে"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
