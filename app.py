import os
import re
import shutil
import tempfile
import threading
import time

import requests
import yt_dlp
from flask import Flask, Response, jsonify, request, send_file, send_from_directory, after_this_request
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# ---------- সেটিংস (Render Environment-এ বদলানো যায়) ----------
MAX_DURATION = int(os.environ.get("MAX_DURATION", 1800))          # সর্বোচ্চ ভিডিও দৈর্ঘ্য (সেকেন্ড)
PIPE_SLOTS = threading.BoundedSemaphore(int(os.environ.get("MAX_PIPE", 4)))   # একসাথে কয়টা পাইপ ডাউনলোড
DISK_SLOTS = threading.BoundedSemaphore(int(os.environ.get("MAX_DISK", 2)))   # একসাথে কয়টা ডিস্ক ডাউনলোড
# যেসব সাইটে "সরাসরি লিংক" অপশন দেখানো হবে (ছোট হাতের নাম, কমা দিয়ে)
DIRECT_EXTRACTORS = {
    x.strip().lower()
    for x in os.environ.get("DIRECT_EXTRACTORS", "twitter,facebook,vimeo").split(",")
    if x.strip()
}
# YouTube-এর জন্য বিকল্প ক্লায়েন্ট
YT_CLIENTS = [c.strip() for c in os.environ.get("YT_CLIENTS", "tv,web_safari,android_vr").split(",") if c.strip()]

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
    "socket_timeout": 30,
    "retries": 3,
    "extractor_args": {"youtube": {"player_client": YT_CLIENTS}},
}


# ---------- কুকি (ঐচ্ছিক) ----------
def _has_cookies(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#HttpOnly_") or (not line.startswith("#") and "\t" in line):
                    return True
    except OSError:
        pass
    return False


COOKIES_LOADED = False
for _src in ("/etc/secrets/cookies.txt", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")):
    if os.path.exists(_src) and _has_cookies(_src):
        _dst = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        shutil.copyfile(_src, _dst)
        BASE_OPTS["cookiefile"] = _dst
        COOKIES_LOADED = True
        break


# ---------- সাহায্যকারী ----------
def valid_url(url):
    return isinstance(url, str) and url.startswith(("http://", "https://")) and len(url) < 2000


def friendly(e):
    t = str(e).replace("ERROR: ", "").strip()
    if "Sign in to confirm" in t or "not a bot" in t:
        return "YouTube এই সার্ভারকে ব্লক করেছে (বট চেক)। অন্য সাইটের লিংক দিন বা পরে চেষ্টা করুন।"
    if "Unsupported URL" in t:
        return "এই লিংক সাপোর্টেড নয়"
    if "Private video" in t:
        return "ভিডিওটা প্রাইভেট"
    return t[:300]


def error_page(text, code):
    html = (
        '<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>ত্রুটি</title></head>"
        '<body style="font-family:system-ui,sans-serif;padding:24px;max-width:480px;margin:auto">'
        f"<h3>ডাউনলোড হয়নি</h3><p>{text}</p>"
        '<p><a href="/">← ফিরে যান</a></p></body></html>'
    )
    return html, code, {"Content-Type": "text/html; charset=utf-8"}


def make_filename(info):
    title = re.sub(r'[\\/:*?"<>|\r\n]+', " ", info.get("title") or "video").strip()[:80] or "video"
    return f"{title}.{info.get('ext') or 'mp4'}"


# ভিডিও তথ্য ৫ মিনিট মনে রাখে, যাতে একই ভিডিওতে বারবার খুঁজতে না হয়
INFO_CACHE = {}
INFO_LOCK = threading.Lock()


def get_info(url, quality):
    key = (url, quality)
    now = time.time()
    with INFO_LOCK:
        hit = INFO_CACHE.get(key)
        if hit and now - hit[0] < 300:
            return hit[1]
    opts = {**BASE_OPTS, "skip_download": True, "format": FORMATS[quality]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False))
    if info.get("entries"):
        info = info["entries"][0]
    with INFO_LOCK:
        if len(INFO_CACHE) >= 50:
            INFO_CACHE.pop(min(INFO_CACHE, key=lambda k: INFO_CACHE[k][0]), None)
        INFO_CACHE[key] = (now, info)
    return info


def single_http_url(info):
    """একটাই ফাইল (ভিডিও+অডিও একসাথে) আর সরাসরি http(s) লিংক হলে সেই লিংক, নইলে None"""
    if info.get("requested_formats"):
        return None
    u = info.get("url")
    if not u or info.get("protocol") not in ("https", "http"):
        return None
    return u


# ---------- রুট ----------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/health")
def health():
    return jsonify(
        ok=True, yt_dlp=yt_dlp.version.__version__, cookies_loaded=COOKIES_LOADED,
        yt_clients=YT_CLIENTS, direct=sorted(DIRECT_EXTRACTORS),
    )


@app.route("/api/info", methods=["POST"])
def info_route():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="সঠিক লিংক দিন"), 400
    try:
        data = get_info(url, "best")
        return jsonify(
            title=data.get("title", "ভিডিও"),
            thumbnail=data.get("thumbnail"),
            duration=data.get("duration"),
            uploader=data.get("uploader"),
        )
    except Exception as e:
        app.logger.error("info error: %s", e)
        return jsonify(error="ভিডিও পাওয়া যায়নি", detail=friendly(e)), 400


@app.route("/api/resolve", methods=["POST"])
def resolve():
    """সরাসরি লিংক দেওয়া যাবে কিনা বলে দেয়"""
    body = request.get_json(silent=True) or {}
    url = str(body.get("url", "")).strip()
    quality = body.get("q", "best")
    if not valid_url(url) or quality not in FORMATS:
        return jsonify(error="ভুল অনুরোধ"), 400
    try:
        info = get_info(url, quality)
    except Exception as e:
        app.logger.error("resolve error: %s", e)
        return jsonify(error="ভিডিও পাওয়া যায়নি", detail=friendly(e)), 400
    if (info.get("duration") or 0) > MAX_DURATION:
        return jsonify(error="ভিডিও অনেক বড়", detail="সর্বোচ্চ %d মিনিট" % (MAX_DURATION // 60)), 400
    direct = single_http_url(info)
    extractor = (info.get("extractor_key") or "").lower()
    if direct and extractor in DIRECT_EXTRACTORS:
        return jsonify(mode="direct", url=direct, filename=make_filename(info))
    return jsonify(mode="server")


def make_releaser(sem):
    """স্লট একবারই ছাড়বে, যতবারই ডাকা হোক (ডাবল রিলিজ বা লিক ঠেকাতে)"""
    state = {"done": False}
    lock = threading.Lock()

    def release():
        with lock:
            if state["done"]:
                return
            state["done"] = True
        sem.release()

    return release


def try_pipe(info, direct_url):
    """ডিস্কে না রেখে সরাসরি ফোনে পাঠায়। ব্যর্থ হলে None"""
    if not PIPE_SLOTS.acquire(blocking=False):
        return error_page("সার্ভার এখন ব্যস্ত, কিছুক্ষণ পরে আবার চেষ্টা করুন।", 429)
    release = make_releaser(PIPE_SLOTS)
    r = None
    try:
        headers = dict(info.get("http_headers") or {})
        headers.pop("Accept-Encoding", None)
        r = requests.get(direct_url, headers=headers, stream=True, timeout=(10, 30))
        if r.status_code >= 400:
            r.close()
            release()
            return None

        def gen():
            try:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                r.close()
                release()

        resp = Response(gen(), content_type=r.headers.get("Content-Type", "application/octet-stream"))
        if r.headers.get("Content-Length") and not r.headers.get("Content-Encoding"):
            resp.headers["Content-Length"] = r.headers["Content-Length"]
        resp.headers.set("Content-Disposition", "attachment", filename=make_filename(info))
        # রেসপন্স যেভাবেই বন্ধ হোক (এমনকি জেনারেটর শুরুই না হলেও) স্লট ছাড়বে
        resp.call_on_close(lambda: (r.close(), release()))
        return resp
    except Exception as e:
        app.logger.error("pipe error: %s", e)
        if r is not None:
            r.close()
        release()
        return None


def disk_download(url, quality):
    """পাইপ না চললে (যেমন ভিডিও-অডিও আলাদা, স্ট্রিম ফরম্যাট) অস্থায়ী ফাইলে নামিয়ে পাঠায়"""
    if not DISK_SLOTS.acquire(blocking=False):
        return error_page("সার্ভার এখন ব্যস্ত, কিছুক্ষণ পরে আবার চেষ্টা করুন।", 429)
    tmp = tempfile.mkdtemp()
    try:
        opts = {
            **BASE_OPTS,
            "format": FORMATS[quality],
            "outtmpl": os.path.join(tmp, "%(title).80s.%(ext)s"),
            "match_filter": lambda info, **kw: (
                "ভিডিও অনেক বড়" if (info.get("duration") or 0) > MAX_DURATION else None
            ),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [f for f in os.listdir(tmp) if not f.endswith((".part", ".ytdl", ".temp"))]
        if not files:
            raise RuntimeError("ফাইল তৈরি হয়নি (ভিডিও খুব বড় হতে পারে)")
        files.sort(key=lambda f: os.path.getsize(os.path.join(tmp, f)), reverse=True)
        path = os.path.join(tmp, files[0])
    except Exception as e:
        app.logger.error("download error: %s", e)
        shutil.rmtree(tmp, ignore_errors=True)
        return error_page(friendly(e), 500)
    finally:
        DISK_SLOTS.release()

    @after_this_request
    def cleanup(response):
        shutil.rmtree(tmp, ignore_errors=True)
        return response

    return send_file(path, as_attachment=True, download_name=files[0])


@app.route("/api/download")
def download():
    # ব্রাউজার/Telegram অনেক সময় আগে HEAD রিকোয়েস্ট পাঠায়, তাতে স্লট বা ডাউনলোড চালানো দরকার নেই
    if request.method == "HEAD":
        return Response(status=200, content_type="application/octet-stream")
    url = request.args.get("url", "").strip()
    quality = request.args.get("q", "best")
    if not valid_url(url) or quality not in FORMATS:
        return error_page("ভুল অনুরোধ", 400)
    try:
        info = get_info(url, quality)
    except Exception as e:
        app.logger.error("download info error: %s", e)
        return error_page(friendly(e), 400)
    if (info.get("duration") or 0) > MAX_DURATION:
        return error_page("ভিডিও অনেক বড় (সর্বোচ্চ %d মিনিট)" % (MAX_DURATION // 60), 400)

    direct = single_http_url(info)
    if direct:
        resp = try_pipe(info, direct)
        if resp is not None:
            return resp
    return disk_download(url, quality)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
