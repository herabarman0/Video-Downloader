import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
from urllib.parse import parse_qsl, urlparse

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
    "best": "best[height<=720][ext=mp4]/best[height<=720]/best",
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


# যেসব সাইট এই সার্ভারের IP ব্লক করে, সেগুলো আগেই থামিয়ে সুন্দর বার্তা দেখানো হয়।
# ভবিষ্যতে প্রক্সি পেলে Render Environment-এ BLOCKED_SITES খালি করে দিলে আবার চালু হবে।
BLOCKED_SITES = {
    x.strip().lower()
    for x in os.environ.get("BLOCKED_SITES", "youtube.com,youtu.be,tiktok.com").split(",")
    if x.strip()
}
SITE_NAMES = {"youtube.com": "YouTube", "youtu.be": "YouTube", "tiktok.com": "TikTok"}


def blocked_site(url):
    host = (urlparse(url).hostname or "").lower()
    for d in BLOCKED_SITES:
        if host == d or host.endswith("." + d):
            return SITE_NAMES.get(d, d)
    return None


UNSUPPORTED_MSG = "এই সাইটের ভিডিও এখন সাপোর্টেড নয়, তাই ডাউনলোড করা যাবে না। পরবর্তী আপডেটের জন্য অপেক্ষা করুন।"


def blocked_message(name):
    return UNSUPPORTED_MSG


def friendly(e):
    # আসল (লম্বা ইংরেজি) এরর Render-এর Logs-এ থাকে, ইউজারকে শুধু ছোট বার্তা দেখানো হয়
    if "Private video" in str(e):
        return "ভিডিওটা প্রাইভেট, ডাউনলোড করা যাবে না।"
    return UNSUPPORTED_MSG


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
    """নামে # ইমোজি ইত্যাদি থাকলে Android ফাইলের ধরন চিনতে পারে না, তাই শুধু অক্ষর/সংখ্যা রাখি"""
    raw = info.get("title") or ""
    # "32K views · 1.1K reactions |" জাতীয় শুরুর অংশ বাদ, বাকি অংশ " - " দিয়ে জোড়া
    raw = re.sub(r"^\s*[\d.,]+\s*[KMBkmb]?\s*(views?|plays?|reactions?|likes?)\b[^|]*\|\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*\|\s*", " - ", raw)
    out = []
    for ch in raw:
        cat = unicodedata.category(ch)
        if 0xFE00 <= ord(ch) <= 0xFE0F:
            continue
        out.append(ch if (cat[0] in "LNM" or ch in " ._-") else " ")
    title = re.sub(r"\s+", " ", "".join(out)).strip(" .-")[:60].strip(" .-") or "video"
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


# ---------- প্রতি ইউজার (IP) সীমা ----------
# খোঁজা: মিনিটে INFO_PER_MIN বার, ডাউনলোড: ১০ মিনিটে DL_PER_10MIN বার (Render Environment-এ বদলানো যায়)
LIMITS = {
    "info": (int(os.environ.get("INFO_PER_MIN", 10)), 60),
    "dl": (int(os.environ.get("DL_PER_10MIN", 6)), 600),
}
RATE = {}
RATE_LOCK = threading.Lock()


def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else request.remote_addr) or "?"


def rate_ok(kind):
    limit, window = LIMITS[kind]
    key = (client_ip(), kind)
    now = time.time()
    with RATE_LOCK:
        stamps = [t for t in RATE.get(key, []) if now - t < window]
        if len(stamps) >= limit:
            RATE[key] = stamps
            return False
        stamps.append(now)
        RATE[key] = stamps
        if len(RATE) > 5000:  # মেমরি বাঁচাতে পুরনো এন্ট্রি ঝেড়ে ফেলা
            for k in [k for k, v in RATE.items() if not v or now - v[-1] > 600]:
                RATE.pop(k, None)
    return True


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
    blocked = blocked_site(url)
    if blocked:
        if WORKER_KEY:   # ফোন-ওয়ার্কার দিয়ে নামবে, এখানে প্রিভিউ আনা যায় না
            return jsonify(title="", thumbnail=None, no_preview=True)
        return jsonify(error=blocked_message(blocked)), 400
    if not rate_ok("info"):
        return jsonify(error="অনেকবার চেষ্টা হয়েছে", detail="কিছুক্ষণ পরে আবার চেষ্টা করুন"), 429
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
        return jsonify(error=friendly(e)), 400


@app.route("/api/resolve", methods=["POST"])
def resolve():
    """সরাসরি লিংক দেওয়া যাবে কিনা বলে দেয়"""
    body = request.get_json(silent=True) or {}
    url = str(body.get("url", "")).strip()
    quality = body.get("q", "best")
    if not valid_url(url) or quality not in FORMATS:
        return jsonify(error="ভুল অনুরোধ"), 400
    blocked = blocked_site(url)
    if blocked:
        if WORKER_KEY:
            return jsonify(mode="bot", url=None)
        return jsonify(error=blocked_message(blocked)), 400
    try:
        info = get_info(url, quality)
    except Exception as e:
        app.logger.error("resolve error: %s", e)
        return jsonify(error=friendly(e)), 400
    if (info.get("duration") or 0) > MAX_DURATION:
        return jsonify(error="ভিডিও অনেক বড়", detail="সর্বোচ্চ %d মিনিট" % (MAX_DURATION // 60)), 400
    direct = single_http_url(info)
    extractor = (info.get("extractor_key") or "").lower()
    # ব্রাউজারে যেসব সাইট ঠিকমতো নামে না (যেমন Facebook, Instagram) সেগুলো Telegram বট দিয়ে নামবে
    if BOT_TOKEN and any(extractor.startswith(s) for s in BOT_ONLY_SITES):
        return jsonify(mode="bot", url=direct)
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
            "outtmpl": os.path.join(tmp, "video.%(ext)s"),
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

    try:
        base_info = get_info(url, quality)
    except Exception:
        base_info = {}
    ext = os.path.splitext(files[0])[1].lstrip(".") or "mp4"
    return send_file(path, as_attachment=True, download_name=make_filename({**base_info, "ext": ext}))


@app.route("/api/download")
def download():
    # ব্রাউজার/Telegram অনেক সময় আগে HEAD রিকোয়েস্ট পাঠায়, তাতে স্লট বা ডাউনলোড চালানো দরকার নেই
    if request.method == "HEAD":
        return Response(status=200, content_type="application/octet-stream")
    url = request.args.get("url", "").strip()
    quality = request.args.get("q", "best")
    if not valid_url(url) or quality not in FORMATS:
        return error_page("ভুল অনুরোধ", 400)
    blocked = blocked_site(url)
    if blocked:
        return error_page(blocked_message(blocked), 400)
    if not rate_ok("dl"):
        return error_page("অনেকবার চেষ্টা হয়েছে, কিছুক্ষণ পরে আবার চেষ্টা করুন।", 429)
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


# ====================== Telegram বট ======================
# লিংক পাঠালে বট লেখা ছাড়া শুধু ভিডিও পাঠায়। Render Environment-এ BOT_TOKEN ও BOT_WEBHOOK_SECRET দিলে চালু হয়।
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BOT_WEBHOOK_SECRET = os.environ.get("BOT_WEBHOOK_SECRET", "").strip()
SITE_URL = os.environ.get("SITE_URL", "").strip().rstrip("/")
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOT_MAX_BYTES = 49 * 1024 * 1024          # Bot API-তে ফাইল আপলোডের সীমা ~৫০ MB
URL_RE = re.compile(r"https?://\S+")
BOT_RATE = {}
BOT_RATE_LOCK = threading.Lock()


def tg_call(method, timeout=30, **kwargs):
    try:
        r = requests.post(f"{BOT_API}/{method}", timeout=timeout, **kwargs)
        return r.json()
    except Exception as e:
        app.logger.error("telegram %s error: %s", method, e)
        return {"ok": False}


def tg_text(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call("sendMessage", json=payload)


def bot_cleanup(chat_id, msg_id):
    """ভিডিও পাঠানো হয়ে গেলে ইউজারের পাঠানো লিংকের মেসেজটা মুছে দেয়, চ্যাটে শুধু ভিডিও থাকে"""
    if msg_id:
        tg_call("deleteMessage", json={"chat_id": chat_id, "message_id": msg_id})


def bot_rate_ok(chat_id, limit=6, window=600):
    now = time.time()
    with BOT_RATE_LOCK:
        stamps = [t for t in BOT_RATE.get(chat_id, []) if now - t < window]
        if len(stamps) >= limit:
            BOT_RATE[chat_id] = stamps
            return False
        stamps.append(now)
        BOT_RATE[chat_id] = stamps
    return True


def bot_download(url, tmp):
    """৫০ MB-এর মধ্যে ফাইল পেতে আগে সেরা কোয়ালিটি, না পেলে 360p চেষ্টা করে। না পেলে None"""
    for q in ("best", "360"):
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        opts = {
            **BASE_OPTS,
            "format": FORMATS[q],
            "outtmpl": os.path.join(tmp, "video.%(ext)s"),
            "max_filesize": BOT_MAX_BYTES,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [f for f in os.listdir(tmp) if not f.endswith((".part", ".ytdl", ".temp"))]
        if not files:
            continue
        path = os.path.join(tmp, max(files, key=lambda f: os.path.getsize(os.path.join(tmp, f))))
        if os.path.getsize(path) <= BOT_MAX_BYTES:
            return path
    return None


def bot_send_file(chat_id, url, info):
    """সার্ভারে নামিয়ে Telegram-এ আপলোড (সরাসরি লিংক কাজ না করলে এই পথ)"""
    if not DISK_SLOTS.acquire(blocking=False):
        tg_text(chat_id, "সার্ভার এখন ব্যস্ত, কিছুক্ষণ পরে আবার চেষ্টা করুন।")
        return
    tmp = tempfile.mkdtemp()
    try:
        tg_call("sendChatAction", json={"chat_id": chat_id, "action": "upload_video"})
        path = bot_download(url, tmp)
        if not path:
            tg_text(chat_id, "ভিডিওটা পাঠানো গেল না (ফাইল ৫০ MB-এর বেশি হতে পারে)।")
            return
        with open(path, "rb") as fh:
            res = tg_call(
                "sendVideo", timeout=300,
                data={"chat_id": chat_id, "supports_streaming": "true"},
                files={"video": (make_filename({**info, "ext": "mp4"}), fh)},
            )
        if not res.get("ok"):
            app.logger.error("bot upload failed: %s", res)
            tg_text(chat_id, "ভিডিও পাঠানো গেল না, পরে আবার চেষ্টা করুন।")
            return False
        return True
    except Exception as e:
        app.logger.error("bot download error: %s", e)
        tg_text(chat_id, friendly(e))
        return False
    finally:
        DISK_SLOTS.release()
        shutil.rmtree(tmp, ignore_errors=True)


def bot_handle(update):
    try:
        msg = update.get("message")
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        msg_id = msg.get("message_id")
        text = (msg.get("text") or "").strip()

        # ওয়েবসাইট থেকে আসা "Telegram-এ ভিডিও নিন" লিংক: /start <টোকেন>
        if text.startswith("/start") and len(text.split(None, 1)) == 2:
            link = bot_link_pop(text.split(None, 1)[1].strip())
            if link:
                bot_handle({"message": {"chat": {"id": chat_id}, "text": link}})
                return

        if text.startswith(("/start", "/help")):
            markup = None
            if SITE_URL:
                markup = {"inline_keyboard": [[{"text": "ওয়েবসাইট খুলুন", "web_app": {"url": SITE_URL}}]]}
            tg_text(chat_id, "ভিডিওর লিংক পাঠান, আমি শুধু ভিডিওটা পাঠিয়ে দেব।", markup)
            return

        m = URL_RE.search(text)
        if not m:
            tg_text(chat_id, "ভিডিওর লিংক পাঠান।")
            return
        url = m.group(0).rstrip(").,]>\"'")
        if not valid_url(url):
            tg_text(chat_id, "সঠিক লিংক দিন।")
            return
        blocked = blocked_site(url)
        if blocked:
            if WORKER_KEY:
                if not bot_rate_ok(chat_id):
                    tg_text(chat_id, "অনেকবার চেষ্টা হয়েছে, কিছুক্ষণ পরে আবার চেষ্টা করুন।")
                elif not worker_online():
                    tg_text(chat_id, blocked_message(blocked))   # ফোন-ওয়ার্কার বন্ধ
                else:
                    worker_enqueue(chat_id, url)
            else:
                tg_text(chat_id, blocked_message(blocked))
            return
        if not bot_rate_ok(chat_id):
            tg_text(chat_id, "অনেকবার চেষ্টা হয়েছে, কিছুক্ষণ পরে আবার চেষ্টা করুন।")
            return

        tg_call("sendChatAction", json={"chat_id": chat_id, "action": "upload_video"})
        try:
            info = get_info(url, "best")
        except Exception as e:
            app.logger.error("bot info error: %s", e)
            tg_text(chat_id, friendly(e))
            return
        if (info.get("duration") or 0) > MAX_DURATION:
            tg_text(chat_id, "ভিডিও অনেক বড় (সর্বোচ্চ %d মিনিট)।" % (MAX_DURATION // 60))
            return

        # ১) Telegram নিজেই লিংক থেকে ভিডিও টেনে নেয়, সার্ভারের ব্যান্ডউইথ লাগে না
        direct = single_http_url(info)
        if direct and (info.get("extractor_key") or "").lower() in DIRECT_EXTRACTORS:
            res = tg_call("sendVideo", json={"chat_id": chat_id, "video": direct, "supports_streaming": True})
            if res.get("ok"):
                bot_cleanup(chat_id, msg_id)
                return
            app.logger.info("bot url-send failed: %s", res)

        # ২) না হলে সার্ভারে নামিয়ে আপলোড
        if bot_send_file(chat_id, url, info):
            bot_cleanup(chat_id, msg_id)
    except Exception as e:
        app.logger.error("bot handle error: %s", e)


# ---------- ফোন-ওয়ার্কার (YouTube/TikTok-এর জন্য) ----------
# এই সার্ভারের IP-কে YouTube/TikTok আটকায়। তাই ওই লিংকের কাজ আপনার নিজের ফোনের Termux-এ চলা worker.py-কে দেওয়া হয়।
# ফোন প্রতি কয়েক সেকেন্ডে এখানে জিজ্ঞেস করে (/worker/next), কাজ পেলে ভিডিও নামিয়ে সরাসরি Telegram-এ পাঠায়।
WORKER_KEY = os.environ.get("WORKER_KEY", "").strip()
JOBS_Q = []
JOBS_LOCK = threading.Lock()
WORKER_SEEN = {"t": 0.0}


def worker_online():
    return time.time() - WORKER_SEEN["t"] < 30


def worker_enqueue(chat_id, url):
    now = time.time()
    with JOBS_LOCK:
        JOBS_Q[:] = [j for j in JOBS_Q if now - j["t"] < 600]
        JOBS_Q.append({"id": uuid.uuid4().hex[:10], "chat_id": chat_id, "url": url, "t": now})
    tg_call("sendChatAction", json={"chat_id": chat_id, "action": "upload_video"})


@app.route("/worker/next")
def worker_next():
    got = request.headers.get("X-Worker-Key", "")
    if not WORKER_KEY or not hmac.compare_digest(got, WORKER_KEY):
        return "forbidden", 403
    WORKER_SEEN["t"] = time.time()
    now = time.time()
    with JOBS_LOCK:
        JOBS_Q[:] = [j for j in JOBS_Q if now - j["t"] < 600]
        job = JOBS_Q.pop(0) if JOBS_Q else None
    return jsonify(job=job)


# ---------- ওয়েবসাইট থেকে বটে পাঠানো (যেসব সাইট ব্রাউজারে নামে না) ----------
BOT_ONLY_SITES = {
    x.strip().lower()
    for x in os.environ.get("BOT_ONLY_SITES", "").split(",")
    if x.strip()
}
BOT_LINKS = {}            # টোকেন -> (সময়, লিংক)
BOT_LINKS_LOCK = threading.Lock()
_BOT_NAME = {"v": None}


def bot_username():
    if not _BOT_NAME["v"]:
        res = tg_call("getMe")
        _BOT_NAME["v"] = (res.get("result") or {}).get("username")
    return _BOT_NAME["v"]


def bot_link_new(url):
    token = uuid.uuid4().hex[:16]
    now = time.time()
    with BOT_LINKS_LOCK:
        for k in [k for k, (t, _) in BOT_LINKS.items() if now - t > 600]:
            BOT_LINKS.pop(k, None)
        BOT_LINKS[token] = (now, url)
    return token


def bot_link_pop(token):
    with BOT_LINKS_LOCK:
        item = BOT_LINKS.pop(token, None)
    if item and time.time() - item[0] <= 600:
        return item[1]
    return None


def tg_user_from_init_data(init_data):
    """Mini App-এর initData যাচাই করে ইউজারের Telegram আইডি দেয়। ভুল হলে None"""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        return json.loads(pairs["user"])["id"]
    except Exception:
        return None


@app.route("/api/bot-send", methods=["POST"])
def api_bot_send():
    """Mini App-এর ভেতর থেকে: ভিডিও সরাসরি ইউজারের বট-চ্যাটে পাঠানো হয়"""
    if not BOT_TOKEN:
        return jsonify(error="বট চালু নেই"), 400
    body = request.get_json(silent=True) or {}
    url = str(body.get("url", "")).strip()
    if not valid_url(url):
        return jsonify(error="সঠিক লিংক দিন"), 400
    chat_id = tg_user_from_init_data(str(body.get("init_data", "")))
    if chat_id is None:
        return jsonify(error="Telegram যাচাই হয়নি"), 403
    blocked = blocked_site(url)
    if blocked and not WORKER_KEY:
        return jsonify(error=blocked_message(blocked)), 400
    if not rate_ok("dl"):
        return jsonify(error="অনেকবার চেষ্টা হয়েছে", detail="কিছুক্ষণ পরে আবার চেষ্টা করুন"), 429
    threading.Thread(
        target=bot_handle,
        args=({"message": {"chat": {"id": chat_id}, "text": url}},),
        daemon=True,
    ).start()
    return jsonify(ok=True)


@app.route("/api/bot-link", methods=["POST"])
def api_bot_link():
    """সাধারণ ব্রাউজারে: বটের একটা লিংক দেয়, Start চাপলেই ভিডিও আসে"""
    if not BOT_TOKEN:
        return jsonify(error="বট চালু নেই"), 400
    url = str((request.get_json(silent=True) or {}).get("url", "")).strip()
    if not valid_url(url):
        return jsonify(error="সঠিক লিংক দিন"), 400
    blocked = blocked_site(url)
    if blocked and not WORKER_KEY:
        return jsonify(error=blocked_message(blocked)), 400
    if not rate_ok("dl"):
        return jsonify(error="অনেকবার চেষ্টা হয়েছে", detail="কিছুক্ষণ পরে আবার চেষ্টা করুন"), 429
    name = bot_username()
    if not name:
        return jsonify(error="বট খুঁজে পাওয়া যায়নি"), 500
    return jsonify(link=f"https://t.me/{name}?start={bot_link_new(url)}")


@app.route("/tg/webhook", methods=["POST"])
def tg_webhook():
    if not BOT_TOKEN:
        return "bot off", 404
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not BOT_WEBHOOK_SECRET or not hmac.compare_digest(got, BOT_WEBHOOK_SECRET):
        return "forbidden", 403
    update = request.get_json(silent=True) or {}
    threading.Thread(target=bot_handle, args=(update,), daemon=True).start()
    return "ok"


@app.route("/tg/set-webhook")
def tg_set_webhook():
    """একবার খুললেই Telegram-কে এই সার্ভারের ঠিকানা জানিয়ে দেয়"""
    if not BOT_TOKEN or not BOT_WEBHOOK_SECRET:
        return "BOT_TOKEN ও BOT_WEBHOOK_SECRET সেট করুন", 400
    if not hmac.compare_digest(request.args.get("key", ""), BOT_WEBHOOK_SECRET):
        return "forbidden", 403
    res = tg_call("setWebhook", json={
        "url": f"https://{request.host}/tg/webhook",
        "secret_token": BOT_WEBHOOK_SECRET,
        "allowed_updates": ["message"],
    })
    return jsonify(res)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
