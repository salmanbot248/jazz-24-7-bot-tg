"""
run_bot.py — TikTok AUTO UPLOADER (unlimited, kabhi khali nahi) — Pakistan trending + profiles + hashtags + sounds

bot.py ko bina chhede uske upar chalta hai. Workflow mein `python -u run_bot.py` chalao.

KAISE CHALTA HAI (sab khud, koi command ki zaroorat nahi):
  1. Bot start hote hi Pakistan ke trending hashtags/sounds (TikTok Creative Center, PK) nikalta hai
     aur unki videos ki list tayyar rakhta hai.
  2. Jaise hi aap bot mein JazzDrive LOGIN kar dete hain (ya purana session mil jata hai),
     10 minute (START_DELAY_MIN) baad AUTO-UPLOAD shuru. Is dauran videos ki list pehle se ready hoti hai.
  3. Phir lagataar: download -> JazzDrive (ZeroFeed) upload -> feed.json. Ek ke baad ek, bina ruke.
  4. Queue khali ho jaye to bot khud naye videos dhoondta hai (trending refresh, hashtags, extra
     Pakistani hashtags) — uploading khali nahi chhodta.
  5. Workflow ke 6 ghante khatam hone se pehle (RUN_LIMIT_MIN) bot saaf tareeqe se band hota hai,
     progress save hoti hai, aur agla scheduled run wahin se aage chalta hai.

Har video ki ID save hoti hai (tiktok_seen.json) — duplicate nahi banta. Koi upload limit nahi.
Koi video fail ho to chhod kar agli par jata hai (3 koshish ke baad skip).
JazzDrive session expire ho jaye to bot rukta nahi: /login karte hi 1 minute mein khud chal padta hai.

Commands (optional):
  /tiktok_add @user      profile (saari videos + nayi)       /tiktok_tag pakistan   hashtag
  /tiktok_sound <link>   sound                                /tiktok_trending       Pakistan trending abhi lao
  /tiktok_trend on|off   auto-trending                        /tiktok_delay 10       login ke kitne minute baad shuru
  /tiktok_remove @user | #tag | sound-id
  /tiktok_list  /tiktok_status  /tiktok_scan  /tiktok_pause  /tiktok_resume  /tiktok_retry

INSTAGRAM (naya): sources "i:<user>" — TikTok users ke saath MIX hokar upload hote hain.
  /ig_add @u1 @u2 (ya instagram.com/user links)   /ig_remove @user
  /ig_status  /ig_scan  /ig_resume (cooldown hatao)  /ig_cap 100 (is run mein max IG videos)
  Listing: gallery-dl (cookies ke saath, sleeps), download: yt-dlp (fallback gallery-dl).
  Cookies: GitHub Secret INSTAGRAM_COOKIES (JSON ya Netscape) -> /tmp par likhi jati hain (repo mein kabhi nahi).
  429 / login-required par SIRF Instagram 30-60 min ruk-ta hai, TikTok chalta rehta hai.
"""

import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

import bot as app


# ─── Files ────────────────────────────────────────────────────
PROFILES_FILE = "tiktok_profiles.json"   # sources + settings
SEEN_FILE = "tiktok_seen.json"           # upload ho chuki video IDs
FAILED_FILE = "tiktok_failed.json"       # fail hui videos ka count

# ─── Settings ─────────────────────────────────────────────────
START_DELAY_MIN = 10                # login ke itne minute baad upload shuru (/tiktok_delay se badal sakte ho)
RUN_LIMIT_MIN = 335                 # itne minute baad saaf band (exit 0) -> workflow naya run shuru karta hai (workflow timeout 340 min)
SCAN_INTERVAL_USER = 6 * 60 * 60    # profiles dobara scan (200 users ko baar baar scan nahi karna)
SCAN_INTERVAL_TAG = 2 * 60 * 60     # hashtag / sound pages dobara scan
TREND_REFRESH = 6 * 60 * 60         # trending list refresh
USERS_ONLY = True                   # sirf aapke add kiye hue users (hashtag/trending band — wo GitHub par block hote hain)
PER_USER_LIMIT = 100                # har user ki kitni (nayi se purani) videos — /tiktok_limit 300 se badlo
SCAN_WORKERS = 3                    # ek saath kitne users scan hon (zyada karoge to TikTok block karega)
MAX_NEW_PER_SCAN = 100              # ek hashtag/sound ke ek scan mein nayi videos (profile par koi limit nahi)
STARVE_SCAN_CAP = 300               # queue khali ho to cap barh jata hai
REFILL_GAP_MIN = 3 * 60             # queue khali ho to refill ki koshish kitne second baad
REFILL_GAP_MAX = 45 * 60            # kuch na mile to refill ka gap yahan tak barhta hai
MAX_AUTO_TAGS = 10                  # auto-trending: kitne hashtags
MAX_AUTO_SOUNDS = 5                 # auto-trending: kitni sounds
EXTRA_TAGS_PER_REFILL = 4           # queue khali ho to ek baar mein itne extra hashtags
SCROLL_ROUNDS = 25                  # hashtag/sound page par scroll
MAX_RETRY = 3                       # ek video itni baar try hogi
MAX_CONSEC_FAIL = 6                 # lagataar itne fail -> 10 minute ruk kar dobara (band nahi hota)
FAIL_COOLDOWN = 10 * 60
GAP_SECONDS = 2                     # videos ke beech gap
PROGRESS_EVERY = 25                 # itni uploads ke baad ek progress message

# ─── Instagram settings ───────────────────────────────────────
IG_COOKIE_ENV = "INSTAGRAM_COOKIES"          # GitHub Secret (JSON ya Netscape)
IG_REPO_COOKIES = "instagram_cookies.txt"    # purana tareeqa (/setigcookies) — sirf fallback
IG_COOKIE_PATH = "/tmp/ig_cookies.txt"       # runtime par yahan likhi jati hain (repo ke bahar — commit nahi hoti)
IG_BATCH = 40                       # ek listing mein kitni reels (gallery-dl --range)
IG_LOW_WATER = 8                    # kisi IG account ki queue itni se kam ho to agli batch list karo
IG_SRC_GAP = 10 * 60                # ek hi account ki do listings ke beech kam az kam gap (sec)
IG_SCAN_SPACING = (120, 240)        # do alag IG listings ke beech random gap (sec)
IG_SCAN_REFRESH = 12 * 60 * 60      # saari reels ho chuki to nayi reels ke liye dobara check
IG_RUN_CAP = 100                    # is run mein max itni IG videos (/ig_cap se badlo)
IG_GAP = (15, 40)                   # IG downloads ke beech random gap (sec)
IG_COOLDOWN_RATE = (30 * 60, 60 * 60)   # 429 / rate limit par IG kitni der ruke
IG_COOLDOWN_LOGIN = 60 * 60         # cookies expire / login required par
IG_CONSEC_FAIL = 5                  # IG par lagataar itne (non-rate) fail -> IG cooldown
IG_BLOCK_STREAK_MAX = 5             # bina kisi success ke itne cooldown -> is run mein IG band
IG_LIST_TIMEOUT = 20 * 60
IG_SLEEP_REQUEST = "6.0-12.0"

# Trending na mile to ye pehle lagte hain
DEFAULT_PK_TAGS = ["pakistan", "pakistanitiktok", "foryoupakistan", "lahore", "karachi", "urdu"]

# Queue khali ho to inhe ek ek karke try kiya jata hai (uploading ruke nahi)
EXTRA_PK_TAGS = [
    "pakistani", "islamabad", "punjab", "peshawar", "desi", "pakistanicomedy", "pakistanifood",
    "pakistanifashion", "pakistanicricket", "psl", "cricket", "naat", "pakistaniwedding",
    "pakistanimemes", "viralpakistan", "trendingpakistan", "pakistanivlogger", "dailyvlog",
    "urdupoetry", "shayari", "funnyvideos", "foryou", "viral", "trending", "islamic", "quran",
    "pakistanarmy", "kashmir", "multan", "faisalabad", "hyderabad", "quetta", "sialkot",
    "pakistanimusic", "pakistanidrama", "bollywood", "lollywood", "gaming", "mobilegaming", "cars",
]

CREATIVE_CENTER_TAGS = ("https://ads.tiktok.com/business/creativecenter/inspiration/popular/"
                        "hashtag/pc/en?countryCode=PK&period=7")
CREATIVE_CENTER_MUSIC = ("https://ads.tiktok.com/business/creativecenter/inspiration/popular/"
                         "music/pc/en?countryCode=PK&period=7")

_ctx = threading.local()     # auto worker: quiet mode + current video


# ─── File helpers ─────────────────────────────────────────────

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ─── Source parsing ───────────────────────────────────────────

def parse_profile(token):
    token = (token or "").strip().rstrip("/")
    if not token:
        return None
    if token.lower().startswith(("http://", "https://")):
        p = urlparse(token)
        if (p.hostname or "").lower() not in ("tiktok.com", "www.tiktok.com", "m.tiktok.com"):
            return None
        parts = [x for x in p.path.split("/") if x]
        if not parts or not parts[0].startswith("@") or "video" in parts:
            return None
        token = parts[0]
    m = re.fullmatch(r"@?([A-Za-z0-9._]{2,24})", token)
    return m.group(1).lower() if m else None


def parse_tag(token):
    token = (token or "").strip()
    m = re.search(r"tiktok\.com/tag/([^/?#\s]+)", token)
    if m:
        token = m.group(1)
    token = token.lstrip("#").strip().lower()
    return token if re.fullmatch(r"[\w]{2,60}", token, re.UNICODE) else None


def parse_sound(token):
    token = (token or "").strip()
    m = re.search(r"/music/[^?#\s]*?(\d{10,})", token)
    if m:
        return m.group(1)
    return token if re.fullmatch(r"\d{10,}", token) else None


_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_USER_IN_URL = re.compile(r"tiktok\.com/@([A-Za-z0-9._]{2,24})")
_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Mobile Safari/537.36")


def resolve_username(url):
    """TikTok link (lamba ya chhota vm.tiktok.com) se username nikalo."""
    m = _USER_IN_URL.search(url)
    if m:
        return m.group(1).lower()
    final = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            final = r.geturl()
    except urllib.error.HTTPError as e:
        final = getattr(e, "url", None)
    except Exception:
        final = None
    if final:
        m = _USER_IN_URL.search(final)
        if m:
            return m.group(1).lower()
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 30}
        if os.path.exists("tiktok_cookies.txt"):
            opts["cookiefile"] = "tiktok_cookies.txt"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        m = _USER_IN_URL.search(str(info.get("webpage_url") or ""))
        if m:
            return m.group(1).lower()
        up = str(info.get("uploader") or "")
        if re.fullmatch(r"[A-Za-z0-9._]{2,24}", up):
            return up.lower()
    except Exception:
        pass
    return None


def extract_users(text, bare=False):
    """Text mein se saare TikTok usernames: links, @user, (bare=True ho to seedhe naam bhi). -> (names, bad_links)"""
    names, seen, bad = [], set(), 0

    def add(n):
        if n and n not in seen:
            seen.add(n)
            names.append(n)

    text = text or ""
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,)")
        if "tiktok.com" not in u.lower():
            continue
        n = resolve_username(u)
        if n:
            add(n)
        else:
            bad += 1
    rest = _URL_RE.sub(" ", text)
    for tok in re.findall(r"@([A-Za-z0-9._]{2,24})", rest):
        add(tok.lower())
    if bare:
        for tok in rest.split():
            if not tok.startswith("@"):
                add(parse_profile(tok))
    return names, bad


def make_key(kind, name):
    return {"user": "u:", "tag": "t:", "sound": "s:", "ig": "i:"}[kind] + name


def label(key):
    kind, name = key.split(":", 1)
    return {"u": f"@{name}", "t": f"#{name}", "s": f"sound {name}", "i": f"IG @{name}"}[kind]


def kind_of(key):
    return {"u": "user", "t": "tag", "s": "sound", "i": "ig"}[key[0]]


# ─── Listing ──────────────────────────────────────────────────

_VIDEO_RE = re.compile(r"tiktok\.com/@([\w.]+)/video/(\d+)")


def _ytdlp_list(url, cookies=True, limit=None):
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "socket_timeout": 60,
        "retries": 5,
    }
    if limit:
        opts["playlistend"] = int(limit)
    if cookies and os.path.exists("tiktok_cookies.txt"):
        opts["cookiefile"] = "tiktok_cookies.txt"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out, seen_ids = [], set()
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        vid = str(e.get("id") or "").strip()
        vurl = e.get("webpage_url") or e.get("original_url") or e.get("url") or ""
        m = re.search(r"/video/(\d+)", str(vurl))
        if m:
            vid = m.group(1)
        if not vid or not str(vurl).startswith(("http://", "https://")):
            continue
        if vid not in seen_ids:
            seen_ids.add(vid)
            out.append((vid, str(vurl)))
    return out


def list_profile_videos(username, limit=None):
    last_err = None
    for attempt in range(3):
        try:
            return _ytdlp_list(f"https://www.tiktok.com/@{username}", limit=limit)
        except Exception as e:
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(str(last_err)[:200])


def _new_browser_page(p):
    args = list(getattr(app, "BROWSER_ARGS", ["--no-sandbox", "--disable-dev-shm-usage"]))
    args.append("--disable-blink-features=AutomationControlled")
    browser = p.chromium.launch(headless=True, args=args)
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=getattr(app, "WEB_UA", None) or
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        locale="en-US",
    )
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return browser, ctx.new_page()


def scrape_video_links(page_url, max_items=150):
    from playwright.sync_api import sync_playwright

    found = {}
    with sync_playwright() as p:
        browser, page = _new_browser_page(p)
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(6)
            stale = 0
            for _ in range(SCROLL_ROUNDS):
                try:
                    hrefs = page.eval_on_selector_all("a[href*='/video/']", "els => els.map(e => e.href)")
                except Exception:
                    hrefs = []
                before = len(found)
                for h in hrefs:
                    m = _VIDEO_RE.search(h or "")
                    if m and m.group(2) not in found:
                        found[m.group(2)] = f"https://www.tiktok.com/@{m.group(1)}/video/{m.group(2)}"
                if len(found) >= max_items:
                    break
                stale = stale + 1 if len(found) == before else 0
                if stale >= 3:
                    break
                page.mouse.wheel(0, 5000)
                time.sleep(2)
        finally:
            browser.close()
    return list(found.items())


def list_source_videos(key, info, max_items=MAX_NEW_PER_SCAN + 50, user_limit=None):
    kind, name = kind_of(key), info["name"]
    if kind == "user":
        return list_profile_videos(name, user_limit)
    url = f"https://www.tiktok.com/tag/{name}" if kind == "tag" else f"https://www.tiktok.com/music/original-sound-{name}"
    try:
        vids = _ytdlp_list(url)
        if vids:
            return vids
    except Exception as e:
        print(f"[tiktok] yt-dlp {label(key)} fail, browser se try: {str(e)[:100]}", flush=True)
    return scrape_video_links(url, max_items=max_items)


# ─── Instagram helpers ────────────────────────────────────────

_IG_HOSTS = ("instagram.com", "www.instagram.com", "m.instagram.com")
_IG_RESERVED = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "about",
                "web", "s", "share", "developers", "legal", "privacy", "directory"}
_IG_STATE = {"ok": False, "note": "cookies abhi load nahi hui"}
_GDL_READY = [False]


class IGError(Exception):
    pass


def parse_ig_user(token):
    token = (token or "").strip().rstrip("/")
    if not token:
        return None
    if token.lower().startswith(("http://", "https://")):
        p = urlparse(token)
        if (p.hostname or "").lower() not in _IG_HOSTS:
            return None
        parts = [x for x in p.path.split("/") if x]
        if not parts or parts[0].lower() in _IG_RESERVED:
            return None
        token = parts[0]
    m = re.fullmatch(r"@?([A-Za-z0-9._]{1,30})", token)
    if not m or m.group(1).lower() in _IG_RESERVED:
        return None
    return m.group(1).lower()


def extract_ig_users(text):
    """Text mein se Instagram usernames: @user, seedha naam, ya instagram.com/user links. -> (names, bad_count)"""
    names, seen, bad = [], set(), 0
    text = text or ""
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,)")
        if "instagram.com" not in u.lower():
            continue
        n = parse_ig_user(u)
        if n:
            if n not in seen:
                seen.add(n)
                names.append(n)
        else:
            bad += 1
    rest = _URL_RE.sub(" ", text)
    for tok in rest.split():
        n = parse_ig_user(tok)
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    return names, bad


_SECRET_RE = re.compile(r"(sessionid|csrftoken|ds_user_id|mid|ig_did|rur)([=:\s'\"]+)[^\s'\";,]+", re.I)


def scrub(text):
    """Error text se cookie-jaisi values hata do (logs / Telegram mein kabhi leak na ho)."""
    t = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", str(text or ""))
    return _SECRET_RE.sub(r"\1\2***", t)


def ig_cookies_to_netscape(raw):
    """Cookie-Editor JSON ya Netscape text -> Netscape text."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw[0] in "[{":
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("cookies") if isinstance(data.get("cookies"), list) else [data]
        far = int(time.time()) + 365 * 86400       # session cookie ko door ki expiry (0 dene se kuch tools use expired maan lete hain)
        lines = ["# Netscape HTTP Cookie File"]
        for c in data:
            if not isinstance(c, dict):
                continue
            name, value = c.get("name"), c.get("value")
            if not name or value is None:
                continue
            domain = str(c.get("domain") or ".instagram.com")
            if domain.lower().endswith("instagram.com"):
                domain = ".instagram.com"
            exp = c.get("expirationDate", c.get("expires"))
            try:
                exp = int(float(exp)) if exp and float(exp) > 0 else far
            except (TypeError, ValueError):
                exp = far
            lines.append("\t".join([
                domain, "TRUE" if domain.startswith(".") else "FALSE", str(c.get("path") or "/"),
                "TRUE" if c.get("secure") else "FALSE", str(exp), str(name),
                str(value).replace("\n", "").replace("\t", " ")]))
        return "\n".join(lines) + "\n"
    first = raw.splitlines()[0]
    if "Netscape" not in first and "HTTP Cookie File" not in first:
        raw = "# Netscape HTTP Cookie File\n" + raw
    return raw + "\n"


def prepare_ig_cookies():
    """Secret (ya purani file) se cookies -> /tmp/ig_cookies.txt (0600). Returns (ok, note). Values kabhi print nahi hoti."""
    raw = os.environ.get(IG_COOKIE_ENV, "").strip()
    if not raw and os.path.exists(IG_REPO_COOKIES):
        try:
            with open(IG_REPO_COOKIES, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read().strip()
        except OSError:
            raw = ""
    if not raw:
        _IG_STATE.update(ok=False, note="INSTAGRAM_COOKIES secret nahi mila")
        return False, _IG_STATE["note"]
    try:
        text = ig_cookies_to_netscape(raw)
    except Exception:
        _IG_STATE.update(ok=False, note="cookies ka format samajh nahi aaya (JSON ya Netscape chahiye)")
        return False, _IG_STATE["note"]
    names = set()
    for line in (text or "").splitlines():
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        cols = line.split("\t")
        if len(cols) >= 7:
            names.add(cols[5])
    if "sessionid" not in names:
        _IG_STATE.update(ok=False, note="sessionid cookie nahi hai (logged-in browser se export karo)")
        return False, _IG_STATE["note"]
    fd = os.open(IG_COOKIE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    _IG_STATE.update(ok=True, note="ok")
    return True, "ok"


def ig_cookies_ok():
    if not os.path.exists(IG_COOKIE_PATH):
        prepare_ig_cookies()
    return bool(_IG_STATE["ok"]) and os.path.exists(IG_COOKIE_PATH)


def ig_classify(text):
    """'rate' (429 / thodi der ruko), 'login' (cookies expire), 'gone' (video hat gayi), 'other'."""
    t = (text or "").lower()
    if any(x in t for x in ("429", "too many requests", "wait a few minutes", "rate-limit", "rate limit",
                            "ratelimit", "not granting access", "try again later", "temporarily blocked")):
        return "rate"
    if any(x in t for x in ("login required", "login_required", "checkpoint", "challenge_required",
                            "sessionid", "cookies nahi", "not logged in", "consent_required", "401")):
        return "login"
    if any(x in t for x in ("unavailable", "has been removed", "deleted", "not found", "404", "no video")):
        return "gone"
    return "other"


def ensure_gallery_dl():
    if _GDL_READY[0]:
        return True

    def ok():
        try:
            r = subprocess.run([sys.executable, "-m", "gallery_dl", "--version"],
                               capture_output=True, timeout=60)
            return r.returncode == 0
        except Exception:
            return False

    if not ok():
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "gallery-dl"],
                           capture_output=True, timeout=300)
        except Exception:
            pass
    _GDL_READY[0] = ok()
    return _GDL_READY[0]


def _ig_codes_from_output(out):
    """gallery-dl -j output se [(ig_<shortcode>, url)] — naye se purane."""
    res, seen = [], set()

    def add(code, url=None):
        code = str(code or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,}", code) or code in seen:
            return
        seen.add(code)
        if not url or "instagram.com" not in str(url):
            url = f"https://www.instagram.com/reel/{code}/"
        res.append((f"ig_{code}", str(url)))

    try:
        data = json.loads(out)
        if isinstance(data, list):
            for e in data:
                if isinstance(e, list) and len(e) >= 3 and e[0] == 3 and isinstance(e[-1], dict):
                    md = e[-1]
                    if str(md.get("extension", "")).lower() in ("jpg", "jpeg", "png", "webp", "heic"):
                        continue
                    add(md.get("post_shortcode") or md.get("shortcode"), md.get("post_url"))
    except ValueError:
        pass
    if not res:
        for m in re.finditer(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]{5,})", out or ""):
            add(m.group(1))
    return res


def list_ig_videos(username, start, end):
    """gallery-dl se reels ki list (download nahi). Returns (videos, complete). IGError agar kuch nahi mila + error."""
    if not ensure_gallery_dl():
        raise IGError("gallery-dl install nahi ho saka")
    if not ig_cookies_ok():
        raise IGError("login required: Instagram cookies nahi mili (" + _IG_STATE["note"] + ")")
    cmd = [sys.executable, "-m", "gallery_dl", "--cookies", IG_COOKIE_PATH, "-j",
           "--range", f"{start}-{end}", "--sleep-request", IG_SLEEP_REQUEST, "-R", "2",
           f"https://www.instagram.com/{username}/reels/"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=IG_LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise IGError("gallery-dl timeout")
    out, err = r.stdout or "", scrub((r.stderr or "")[-700:])
    vids = _ig_codes_from_output(out)
    if not vids:
        if r.returncode != 0 or "[error]" in err.lower():
            raise IGError(err.strip()[-300:] or f"gallery-dl exit {r.returncode}")
        return [], True
    return vids, r.returncode == 0


def ig_download(url, cookies_file=None):
    """Instagram video download: yt-dlp (cookies ke saath), phir gallery-dl. Error _ctx.err mein. Returns (path, title)."""
    _ctx.err = ""
    m = re.search(r"instagram\.com/(?:[\w.]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url or "")
    code = m.group(1) if m else "x%d" % int(time.time())
    if not ig_cookies_ok():
        _ctx.err = "login required: Instagram cookies nahi mili"
        return None, None
    try:
        import yt_dlp
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"], capture_output=True)
        import yt_dlp

    tmpc = f"/tmp/ig_dl_cookies_{os.getpid()}_{threading.get_ident()}.txt"
    try:
        shutil.copyfile(IG_COOKIE_PATH, tmpc)
        os.chmod(tmpc, 0o600)
    except OSError:
        tmpc = IG_COOKIE_PATH
    for old in glob.glob(f"/tmp/ig_{code}.*"):
        try:
            os.remove(old)
        except OSError:
            pass
    err = ""
    try:
        opts = {"format": "best[ext=mp4]/best", "outtmpl": f"/tmp/ig_{code}.%(ext)s",
                "merge_output_format": "mp4", "quiet": True, "no_warnings": True, "noprogress": True,
                "retries": 3, "fragment_retries": 3, "socket_timeout": 60, "cookiefile": tmpc}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    fp = ydl.prepare_filename(info)
                    base = os.path.splitext(fp)[0]
                    for cand in (fp, base + ".mp4", base + ".mkv", base + ".webm"):
                        if os.path.exists(cand):
                            return cand, (info.get("title") or "Instagram video")
                    err = "yt-dlp: file nahi bani"
        except Exception as e:
            err = str(e)

        # gallery-dl fallback — sirf tab jab rate/login/gone wali wajah na ho (warna requests aur barhengi)
        if ig_classify(err) == "other":
            d = f"/tmp/igdl_{code}"
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
            try:
                if ensure_gallery_dl():
                    r = subprocess.run([sys.executable, "-m", "gallery_dl", "--cookies", tmpc, "-D", d, "-R", "2",
                                        "--sleep-request", "3.0-6.0", url],
                                       capture_output=True, text=True, timeout=600)
                    vids = [f for f in glob.glob(d + "/*") if f.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))]
                    if vids:
                        best = max(vids, key=os.path.getsize)
                        dst = f"/tmp/ig_{code}.mp4"
                        shutil.move(best, dst)
                        return dst, "Instagram reel " + code
                    err = (err + " | gallery-dl: " + (r.stderr or "")[-200:]).strip()
            except Exception as e:
                err = (err + " | gallery-dl: " + str(e)[:150]).strip()
            finally:
                shutil.rmtree(d, ignore_errors=True)
        _ctx.err = scrub(err)[:300]
        return None, None
    finally:
        if tmpc != IG_COOKIE_PATH:
            try:
                os.remove(tmpc)
            except OSError:
                pass


# ─── Pakistan trending ────────────────────────────────────────

def _walk(obj, fn):
    if isinstance(obj, dict):
        for k, v in obj.items():
            fn(k, v)
            _walk(v, fn)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, fn)


def fetch_pk_trending():
    """TikTok Creative Center (PK) se trending hashtags aur sounds. Best-effort: block ho to khali."""
    from playwright.sync_api import sync_playwright

    tags, sounds = [], []

    def add_tag(v):
        t = parse_tag(str(v))
        if t and t not in tags:
            tags.append(t)

    def add_sound(v):
        s = str(v).strip()
        if re.fullmatch(r"\d{10,}", s) and s not in sounds:
            sounds.append(s)

    def grab(key, value):
        k = str(key).lower()
        if k in ("hashtag_name", "hashtagname") and isinstance(value, str):
            add_tag(value)
        elif k in ("music_id", "musicid", "clip_id", "clipid", "song_id", "songid") and isinstance(value, (str, int)):
            add_sound(value)

    with sync_playwright() as p:
        browser, page = _new_browser_page(p)
        try:
            def on_response(resp):
                try:
                    if "json" in (resp.headers.get("content-type") or "") and (
                            "popular_trend" in resp.url or "creative_radar" in resp.url):
                        _walk(resp.json(), grab)
                except Exception:
                    pass

            page.on("response", on_response)

            for url, is_tags in ((CREATIVE_CENTER_TAGS, True), (CREATIVE_CENTER_MUSIC, False)):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(10)
                    page.mouse.wheel(0, 1500)
                    time.sleep(3)
                    if is_tags and not tags:
                        text = page.inner_text("body", timeout=5000)
                        for m in re.finditer(r"#\s?([\w]{2,40})", text, re.UNICODE):
                            add_tag(m.group(1))
                    if not is_tags and not sounds:
                        hrefs = page.eval_on_selector_all("a[href*='/music/']", "els => els.map(e => e.href)")
                        for h in hrefs:
                            sid = parse_sound(h)
                            if sid:
                                add_sound(sid)
                except Exception as e:
                    print(f"[tiktok] trending page fail: {str(e)[:120]}", flush=True)
        finally:
            browser.close()

    return tags[:MAX_AUTO_TAGS], sounds[:MAX_AUTO_SOUNDS]


# ─── Auto engine ──────────────────────────────────────────────

class TikAuto:
    def __init__(self, instance):
        self.inst = instance
        self.lock = threading.RLock()

        data = _read_json(PROFILES_FILE, {})
        self.paused = bool(data.get("paused", False))
        self.trend_on = False if USERS_ONLY else bool(data.get("trend_on", True))
        self.user_limit = int(data.get("user_limit", PER_USER_LIMIT))
        self.collect = bool(data.get("collect", True))
        self.scan_lock = threading.Lock()
        self.start_delay = int(data.get("start_delay", START_DELAY_MIN))
        self.extra_idx = int(data.get("extra_idx", 0))
        self.sources = {}
        for k, v in dict(data.get("profiles", {})).items():
            if ":" not in k:
                k, v = make_key("user", k), {**v, "type": "user", "name": k}
            self.sources[k] = v
        self.seen = {str(x) for x in _read_json(SEEN_FILE, [])}
        self.fails = {str(k): int(v) for k, v in _read_json(FAILED_FILE, {}).items()}

        self.pool = {}
        self.inflight = set()
        self.rr = deque()
        self.last_scan = {}
        self.force_scan = True
        self.scan_cap = MAX_NEW_PER_SCAN
        self.next_trend = 0
        self.run_started = time.time()
        self.login_at = None
        self.started_uploading = False
        self.done_total = 0
        self.session_done = 0
        self.consec_fail = 0
        self.cooldown_until = 0
        self.current = None
        self.warned = set()
        self.last_refill = 0
        self.refill_gap = REFILL_GAP_MIN
        self.last_nothing_msg = 0
        self.no_session_notified = False

        # Instagram state
        self.ig_cap = int(data.get("ig_cap", IG_RUN_CAP))
        self.ig_cooldown_until = 0
        self.ig_next_ok = 0
        self.ig_done_run = 0
        self.ig_consec_fail = 0
        self.ig_block_streak = 0
        self.ig_next_scan = 0
        self.ig_scan_lock = threading.Lock()
        self.ig_last_msg = {}
        self.ig_announced = set()
        for key, info in self.sources.items():          # pichle run ki bachi hui IG queue wapas
            if not key.startswith("i:"):
                continue
            dq = deque()
            for item in (info.pop("queue", None) or []):
                try:
                    vid, url = item
                except (TypeError, ValueError):
                    continue
                if vid in self.seen or vid in self.inflight or self.fails.get(vid, 0) >= MAX_RETRY:
                    continue
                dq.append((vid, url))
                self.inflight.add(vid)
            if dq:
                self.pool[key] = dq
                self.rr.append(key)

    # persistence
    def save_sources(self):
        with self.lock:
            profiles = {}
            for k, v in self.sources.items():
                if k.startswith("i:"):                  # IG queue bhi save: agla run wahin se aage chale
                    v = dict(v)
                    v["queue"] = [[a, b] for a, b in self.pool.get(k, ())]
                profiles[k] = v
            _write_json(PROFILES_FILE, {
                "paused": self.paused, "trend_on": self.trend_on, "start_delay": self.start_delay,
                "user_limit": self.user_limit, "collect": self.collect, "ig_cap": self.ig_cap,
                "extra_idx": self.extra_idx, "profiles": profiles})

    def save_seen(self):
        with self.lock:
            _write_json(SEEN_FILE, sorted(self.seen))

    def save_fails(self):
        with self.lock:
            _write_json(FAILED_FILE, self.fails)

    def dead_count(self):
        return sum(1 for v in self.fails.values() if v >= MAX_RETRY)

    def pending_count(self):
        with self.lock:
            return sum(len(q) for q in self.pool.values())

    def notify(self, text):
        try:
            self.inst.msg(text)
        except Exception as e:
            print(f"[tiktok] notify fail: {e}", flush=True)

    def elapsed_min(self):
        return (time.time() - self.run_started) / 60

    # sources
    def add_source(self, kind, name, auto=False):
        key = make_key(kind, name)
        with self.lock:
            if key in self.sources:
                return False
            self.sources[key] = {"type": kind, "name": name, "added": datetime.now().isoformat(),
                                 "listed": False, "auto": auto, "empty_scans": 0}
        return True

    def remove_source(self, key):
        with self.lock:
            if key not in self.sources:
                return False
            self.sources.pop(key)
            for vid, _ in self.pool.pop(key, deque()):
                self.inflight.discard(vid)
            if key in self.rr:
                self.rr.remove(key)
            self.last_scan.pop(key, None)
        return True

    # trending
    def refresh_trending(self, announce=False):
        tags, sounds, err = [], [], None
        try:
            tags, sounds = fetch_pk_trending()
        except Exception as e:
            err = str(e)[:150]

        used_default = False
        with self.lock:
            has_auto = any(v.get("auto") for v in self.sources.values())
        if not tags and not sounds and not has_auto:
            tags, used_default = list(DEFAULT_PK_TAGS), True

        new_keys = []
        for t in tags:
            if self.add_source("tag", t, auto=True):
                new_keys.append(make_key("tag", t))
        for s in sounds:
            if self.add_source("sound", s, auto=True):
                new_keys.append(make_key("sound", s))

        if tags or sounds:
            current = {make_key("tag", t) for t in tags} | {make_key("sound", s) for s in sounds}
            with self.lock:
                stale = [k for k, v in self.sources.items()
                         if v.get("auto") and k not in current and not self.pool.get(k)]
            for k in stale:
                self.remove_source(k)

        self.save_sources()
        self.next_trend = time.time() + TREND_REFRESH
        self.force_scan = True
        if announce or (new_keys and not self.started_uploading):
            if used_default:
                note = "Trending list nahi mili (TikTok block / page badla) — default Pakistani hashtags laga diye."
            elif not tags and not sounds:
                note = "Trending list nahi mili." + (f"\n({err})" if err else "")
            else:
                note = "Pakistan trending se sources:"
            lines = ", ".join(label(k) for k in new_keys) if new_keys else "(koi naya nahi)"
            self.notify(f"{note}\n{lines}")
        return bool(new_keys)

    # scanning
    def scan_source(self, key):
        info = self.sources[key]
        kind = kind_of(key)
        videos = list_source_videos(key, info, max_items=self.scan_cap + 50, user_limit=self.user_limit)
        added = 0
        with self.lock:
            q = self.pool.setdefault(key, deque())
            for vid, url in videos:
                if vid in self.seen or vid in self.inflight:
                    continue
                if self.fails.get(vid, 0) >= MAX_RETRY:
                    continue
                if kind != "user" and added >= self.scan_cap:
                    break
                q.append((vid, url))
                self.inflight.add(vid)
                added += 1
            if key not in self.rr:
                self.rr.append(key)
            if key in self.sources:
                self.sources[key]["listed"] = True
                self.sources[key]["empty_scans"] = 0 if added else self.sources[key].get("empty_scans", 0) + 1
        self.last_scan[key] = time.time()
        return len(videos), added

    def due_keys(self):
        out = []
        if self.paused:
            return out
        for key in list(self.sources.keys()):
            kind = kind_of(key)
            if kind == "ig":            # Instagram ki listing ig_scan_step() karta hai (alag gap + cooldown)
                continue
            if USERS_ONLY and kind != "user":
                continue
            interval = SCAN_INTERVAL_USER if kind == "user" else SCAN_INTERVAL_TAG
            due = self.force_scan or (time.time() - self.last_scan.get(key, 0) >= interval)
            if not due:
                continue
            if kind != "user" and self.pool.get(key) and not self.force_scan:
                continue
            out.append(key)
        return out

    def scan_async(self):
        """Upload rukay bina background mein naye/due users scan karo."""
        if self.scan_lock.locked() or not self.due_keys():
            return
        threading.Thread(target=self._scan_safe, daemon=True).start()

    def _scan_safe(self):
        try:
            self.scan_due(quiet=True)
        except Exception as e:
            print(f"[tiktok] scan error: {str(e)[:150]}", flush=True)

    def scan_due(self, quiet=False):
        with self.scan_lock:
            total_new, problems = 0, []
            keys = self.due_keys()
            self.force_scan = False

            def one(key):
                time.sleep(random.uniform(0.5, 2.5))
                try:
                    found, added = self.scan_source(key)
                    return key, found, added, None
                except Exception as e:
                    self.last_scan[key] = time.time()
                    return key, 0, 0, str(e)[:100]

            if keys:
                with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                    for key, found, added, err in ex.map(one, keys):
                        if err is None:
                            total_new += added
                            if found > 0:
                                self.warned.discard(key)
                            elif key not in self.warned:
                                self.warned.add(key)
                                problems.append(f"{label(key)}: koi video nahi mili (private / TikTok block?)")
                        elif key not in self.warned:
                            self.warned.add(key)
                            problems.append(f"{label(key)}: list nahi ban saki ({err})")
                with self.lock:
                    order = list(self.rr)
                    random.shuffle(order)       # users ka mix: category ke blocks nahi banenge
                    self.rr = deque(order)
            if keys and not quiet and (total_new and not self.started_uploading or problems):
                text = f"TikTok scan: {len(keys)} users scan hue, {total_new} nayi videos queue mein.\nPending total: {self.pending_count()}"
                if problems:
                    text += "\n\nMasle:\n" + "\n".join(problems[:6])
                    if len(problems) > 6:
                        text += f"\n(+{len(problems) - 6} aur)"
                self.notify(text)
            self.save_sources()
            return total_new

    def refill(self):
        """Queue khali: naye videos dhoondo. Trending refresh -> purane sources dobara -> extra Pakistani hashtags."""
        if time.time() - self.last_refill < self.refill_gap:
            return False
        self.last_refill = time.time()
        self.scan_cap = STARVE_SCAN_CAP
        found = False
        try:
            self.force_scan = True
            self.scan_due(quiet=True)
            found = self.pending_count() > 0

            if not found and self.trend_on and not USERS_ONLY:
                self.refresh_trending()
                self.scan_due(quiet=True)
                found = self.pending_count() > 0

            if not found and not USERS_ONLY:
                # thaki hui (lagataar khali) auto sources hata kar extra Pakistani hashtags
                with self.lock:
                    dead_auto = [k for k, v in self.sources.items()
                                 if v.get("auto") and v.get("empty_scans", 0) >= 2 and not self.pool.get(k)]
                for k in dead_auto:
                    self.remove_source(k)
                added = 0
                tries = 0
                while added < EXTRA_TAGS_PER_REFILL and tries < len(EXTRA_PK_TAGS):
                    tag = EXTRA_PK_TAGS[self.extra_idx % len(EXTRA_PK_TAGS)]
                    self.extra_idx += 1
                    tries += 1
                    if self.add_source("tag", tag, auto=True):
                        added += 1
                self.save_sources()
                if added:
                    self.force_scan = True
                    self.scan_due(quiet=True)
                    found = self.pending_count() > 0
        except Exception as e:
            print(f"[tiktok] refill error: {str(e)[:150]}", flush=True)
        finally:
            self.scan_cap = MAX_NEW_PER_SCAN

        if found:
            self.refill_gap = REFILL_GAP_MIN
        else:
            self.refill_gap = min(self.refill_gap * 2, REFILL_GAP_MAX)
            if time.time() - self.last_nothing_msg > 3600 and not self.ig_has_pending():
                self.last_nothing_msg = time.time()
                self.notify("TikTok: queue khali — sab users ki videos upload ho chuki ya TikTok block kar raha hai.\n"
                            "Naye users add karo (TikTok video ka link bot ko bhejo) ya /tiktok_limit 300 karo.")
        return found

    def next_item(self):
        with self.lock:
            ig_ok = self.ig_allowed_now()
            for _ in range(len(self.rr)):
                key = self.rr[0]
                self.rr.rotate(-1)
                if key.startswith("i:") and not ig_ok:      # IG gap / cooldown / cap: TikTok chalta rahe
                    continue
                q = self.pool.get(key)
                if q:
                    vid, url = q.popleft()
                    return key, vid, url
        return None

    # ─── Instagram ────────────────────────────────────────────

    def ig_keys(self):
        with self.lock:
            return [k for k in self.sources if k.startswith("i:")]

    def ig_has_pending(self):
        with self.lock:
            return any(self.pool.get(k) for k in self.sources if k.startswith("i:"))

    def ig_allowed_now(self):
        now = time.time()
        return (now >= self.ig_cooldown_until and now >= self.ig_next_ok
                and self.ig_done_run < self.ig_cap and ig_cookies_ok())

    def ig_set_cooldown(self, kind, detail=""):
        """Sirf Instagram ko roko. TikTok / consec_fail par koi asar nahi."""
        if kind == "login":
            secs = IG_COOLDOWN_LOGIN
        elif kind == "rate":
            secs = random.uniform(*IG_COOLDOWN_RATE)
        else:
            secs = IG_COOLDOWN_RATE[0]
        if kind in ("rate", "login"):
            self.ig_block_streak += 1
        disabled = self.ig_block_streak >= IG_BLOCK_STREAK_MAX
        if disabled:
            secs = max(secs, 24 * 3600)
        self.ig_cooldown_until = time.time() + secs
        mins = int(secs / 60)
        if time.time() - self.ig_last_msg.get(kind, 0) < 20 * 60 and not disabled:
            return
        self.ig_last_msg[kind] = time.time()
        d = scrub(detail)[:120]
        if disabled:
            text = ("Instagram lagataar block ho raha hai — is run mein Instagram BAND kar diya. TikTok chal raha hai.\n"
                    "Agla run dobara try karega. Naye cookies chahiye to INSTAGRAM_COOKIES secret update karo.")
        elif kind == "login":
            text = (f"Instagram: login required / cookies expire lag rahi hain.\n"
                    f"INSTAGRAM_COOKIES secret mein naye cookies daalo. {mins} min baad dobara koshish.\n"
                    f"TikTok chal raha hai.")
        elif kind == "rate":
            text = (f"Instagram ne rate limit (429 / thodi der ruko) lagayi.\n"
                    f"Sirf Instagram {mins} minute ruka hai — TikTok chal raha hai.")
            if self.ig_block_streak >= 3:
                text += "\nBar bar ho raha hai — cookies expire ya GitHub IP block bhi ho sakti hai."
        else:
            text = (f"Instagram par lagataar {IG_CONSEC_FAIL} videos fail hui.\n"
                    f"Instagram {mins} minute ruka hai — TikTok chal raha hai.")
        if d:
            text += f"\n({d})"
        self.notify(text)

    def ig_summary(self, detail=False):
        keys = self.ig_keys()
        left = int((self.ig_cooldown_until - time.time()) / 60)
        s = (f"Instagram: {len(keys)} accounts | cookies {'OK' if ig_cookies_ok() else 'NAHI (' + _IG_STATE['note'] + ')'}"
             f" | is run mein {self.ig_done_run}/{self.ig_cap}")
        if left > 0:
            s += f" | cooldown {left} min"
        if detail:
            lines = []
            with self.lock:
                for k in keys:
                    info = self.sources.get(k, {})
                    pos = int(info.get("ig_pos", 0))
                    end = " (sab list ho gayi)" if info.get("ig_end") else ""
                    lines.append(f"{label(k)}: pending {len(self.pool.get(k, ()))} | listed {pos}/{self.user_limit}{end}")
            if lines:
                s += "\n" + "\n".join(lines)
        return s

    def ig_startup(self):
        ok, note = prepare_ig_cookies()
        n = len(self.ig_keys())
        if not n:
            return
        if ok:
            self.notify(f"Instagram: cookies load ho gayi, {n} accounts. Videos TikTok ke saath mix hokar upload hongi "
                        f"(is run mein max {self.ig_cap}).")
        else:
            self.notify(f"Instagram: cookies nahi mili ({note}).\nTab tak sirf TikTok upload hoga. "
                        f"GitHub Secret INSTAGRAM_COOKIES daalo.")

    def ig_scan_step(self):
        """Ek waqt mein sirf ek IG account ki listing, lambe gaps ke saath (background thread)."""
        if self.paused or self.ig_scan_lock.locked():
            return
        keys = self.ig_keys()
        if not keys:
            return
        now = time.time()
        if now < self.ig_cooldown_until or now < self.ig_next_scan or self.ig_done_run >= self.ig_cap:
            return
        if not ig_cookies_ok():
            return
        best, best_t = None, None
        with self.lock:
            for key in keys:
                info = self.sources.get(key) or {}
                last = self.last_scan.get(key, 0)
                plen = len(self.pool.get(key, ()))
                if not info.get("listed"):
                    due = True
                elif info.get("ig_end") or int(info.get("ig_pos", 0)) >= self.user_limit:
                    due = plen == 0 and now - last >= IG_SCAN_REFRESH
                else:
                    due = plen < IG_LOW_WATER and now - last >= IG_SRC_GAP
                if due and (best is None or last < best_t):
                    best, best_t = key, last
        if best:
            self.ig_next_scan = now + 60          # double-start se bachao
            threading.Thread(target=self._ig_scan_safe, args=(best,), daemon=True).start()

    def _ig_scan_safe(self, key):
        with self.ig_scan_lock:
            try:
                self._ig_scan(key)
            except Exception as e:
                print(f"[tiktok] ig scan error: {scrub(e)[:150]}", flush=True)
            finally:
                self.ig_next_scan = time.time() + random.uniform(*IG_SCAN_SPACING)

    def _ig_scan(self, key):
        with self.lock:
            info = self.sources.get(key)
            if not info:
                return
            name = info["name"]
            pos = int(info.get("ig_pos", 0))
            refresh = bool(info.get("ig_end")) or pos >= self.user_limit
            limit = self.user_limit
        if refresh:
            start, end = 1, IG_BATCH
        else:
            start, end = pos + 1, min(pos + IG_BATCH, limit)
        want = end - start + 1
        try:
            vids, complete = list_ig_videos(name, start, end)
        except IGError as e:
            self.last_scan[key] = time.time()
            kind = ig_classify(str(e))
            if kind in ("rate", "login"):
                self.ig_set_cooldown(kind, str(e))
            elif key not in self.warned:
                self.warned.add(key)
                self.notify(f"Instagram {label(key)}: list nahi ban saki ({scrub(e)[:120]})")
            return
        added = 0
        with self.lock:
            if key not in self.sources:
                return
            q = self.pool.setdefault(key, deque())
            for vid, url in vids:
                if vid in self.seen or vid in self.inflight or self.fails.get(vid, 0) >= MAX_RETRY:
                    continue
                q.append((vid, url))
                self.inflight.add(vid)
                added += 1
            if key not in self.rr:
                self.rr.append(key)
            info["listed"] = True
            if not refresh:
                info["ig_pos"] = pos + len(vids)
                if complete and len(vids) < want:
                    info["ig_end"] = True
        self.last_scan[key] = time.time()
        if vids:
            self.warned.discard(key)
        elif pos == 0 and not refresh and key not in self.warned:
            self.warned.add(key)
            self.notify(f"Instagram {label(key)}: koi reel nahi mili (private account / reels nahi / cookies?)")
        self.save_sources()
        if added and key not in self.ig_announced:
            self.ig_announced.add(key)
            self.notify(f"Instagram {label(key)}: {added} reels queue mein.\nPending total: {self.pending_count()}")

    # ek video: Instagram ya TikTok
    def process_item(self, key, vid, url):
        if key.startswith("i:"):
            return self.process_item_ig(key, vid, url)
        return self.process_item_tiktok(key, vid, url)

    def process_item_ig(self, key, vid, url):
        inst = self.inst
        if not app.session_valid(inst.state_file):
            with self.lock:
                self.pool.setdefault(key, deque()).appendleft((vid, url))
            return "nosession"

        code = vid[3:]
        self.current = f"{label(key)} / {code}"
        _ctx.quiet = True
        _ctx.vid = vid
        _ctx.ok = False
        _ctx.err = ""
        try:
            app.BotInstance.process_social(inst, url, None)
        except Exception as e:
            _ctx.err = _ctx.err or scrub(e)
            print(f"[tiktok] ig process error {code}: {scrub(e)[:150]}", flush=True)
        finally:
            _ctx.quiet = False
            ok = bool(getattr(_ctx, "ok", False))
            err = getattr(_ctx, "err", "") or ""
            _ctx.vid = None
            self.current = None

        for leftover in glob.glob(f"/tmp/*{code}*"):
            try:
                if os.path.isdir(leftover):
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    os.remove(leftover)
            except OSError:
                pass

        kind = "ok" if ok else ig_classify(err)
        with self.lock:
            if ok:
                self.seen.add(vid)
                self.inflight.discard(vid)
                self.fails.pop(vid, None)
                self.done_total += 1
                self.session_done += 1
                self.ig_done_run += 1
                self.ig_consec_fail = 0
                self.ig_block_streak = 0
                self.ig_next_ok = time.time() + random.uniform(*IG_GAP)
            elif kind in ("rate", "login"):
                self.pool.setdefault(key, deque()).appendleft((vid, url))     # video ka koi qasoor nahi — fails count nahi
            else:
                n = self.fails.get(vid, 0) + 1
                self.fails[vid] = n
                if n < MAX_RETRY:
                    self.pool.setdefault(key, deque()).append((vid, url))
                else:
                    self.inflight.discard(vid)
                if kind != "gone":
                    self.ig_consec_fail += 1
                self.ig_next_ok = time.time() + random.uniform(*IG_GAP)
            trip = (not ok) and kind not in ("rate", "login") and self.ig_consec_fail >= IG_CONSEC_FAIL
        if ok:
            self.save_seen()
        self.save_fails()
        self.save_sources()
        if not ok and kind in ("rate", "login"):
            self.ig_set_cooldown(kind, err)
        elif trip:
            self.ig_consec_fail = 0
            self.ig_set_cooldown("other", err)
        elif ok and self.ig_done_run % 10 == 0:
            self.notify(f"Instagram: is run mein {self.ig_done_run}/{self.ig_cap} videos upload.\n"
                        f"Total: {len(self.seen)} | Pending: {self.pending_count()}")
        return None

    # TikTok video — returns "nosession" agar Jazz login nahi
    def process_item_tiktok(self, key, vid, url):
        inst = self.inst

        if not app.session_valid(inst.state_file):
            with self.lock:
                self.pool.setdefault(key, deque()).appendleft((vid, url))
            return "nosession"

        self.current = f"{label(key)} / {vid}"
        _ctx.quiet = True
        _ctx.vid = vid
        _ctx.ok = False
        try:
            app.BotInstance.process_social(inst, url, None)
        except Exception as e:
            print(f"[tiktok] process error {vid}: {str(e)[:150]}", flush=True)
        finally:
            _ctx.quiet = False
            ok = bool(getattr(_ctx, "ok", False))
            _ctx.vid = None
            self.current = None

        for leftover in glob.glob(f"/tmp/*{vid}*"):
            try:
                os.remove(leftover)
            except OSError:
                pass

        with self.lock:
            if ok:
                self.seen.add(vid)
                self.inflight.discard(vid)
                self.fails.pop(vid, None)
                self.done_total += 1
                self.session_done += 1
                self.consec_fail = 0
            else:
                self.consec_fail += 1
                n = self.fails.get(vid, 0) + 1
                self.fails[vid] = n
                if n < MAX_RETRY:
                    self.pool.setdefault(key, deque()).append((vid, url))
                else:
                    self.inflight.discard(vid)
            cooldown = self.consec_fail >= MAX_CONSEC_FAIL
        if ok:
            self.save_seen()
        self.save_fails()
        if cooldown:
            self.consec_fail = 0
            self.cooldown_until = time.time() + FAIL_COOLDOWN
            self.notify(f"TikTok: lagataar {MAX_CONSEC_FAIL} videos fail hui (storage full / TikTok block / session?).\n"
                        f"10 minute ruk kar bot khud dobara koshish karega.")
            return None
        if ok and self.session_done % PROGRESS_EVERY == 0:
            self.notify(f"TikTok auto: {self.session_done} videos is run mein upload.\n"
                        f"Total: {len(self.seen)} | Pending: {self.pending_count()} | Failed: {self.dead_count()}\n"
                        f"Run ka waqt: {int(self.elapsed_min())} min")
        return None

    # ─── Loop ─────────────────────────────────────────────────

    def finish_run(self):
        self.save_seen()
        self.save_fails()
        self.save_sources()
        self.notify(f"TikTok auto: ye run khatam hone wala hai.\n"
                    f"Is run mein {self.session_done} videos upload hui (Instagram: {self.ig_done_run}). Total: {len(self.seen)} | Pending: {self.pending_count()}\n"
                    f"Agla scheduled run khud wahin se shuru karega.")
        time.sleep(4)
        os._exit(0)      # workflow ka agla step (session encrypt + save) chal jaye

    def prepare(self):
        """Upload se pehle: trending + videos ki list tayyar (Jazz ki zaroorat nahi)."""
        if self.trend_on and time.time() >= self.next_trend:
            self.refresh_trending()
        if self.sources:
            self.scan_due()
        self.ig_scan_step()

    def wait_for_start(self):
        """Jazz login ka intezar, phir start_delay minute (is dauran list tayyar)."""
        told_login = False
        while True:
            if self.elapsed_min() > RUN_LIMIT_MIN:
                self.finish_run()
            if self.paused:
                time.sleep(5)
                continue
            if self.login_at is None:
                if app.session_valid(self.inst.state_file):
                    self.login_at = time.time()
                    self.notify(f"JazzDrive login mil gaya!\nAuto-upload {self.start_delay} minute baad shuru hoga "
                                f"(tab tak videos ki list tayyar kar raha hoon).")
                elif not told_login:
                    told_login = True
                    self.notify(f"TikTok auto-upload tayyar hai. JazzDrive login karo (/login) — "
                                f"login ke {self.start_delay} minute baad upload khud shuru ho jayega.")
            else:
                if time.time() - self.login_at >= self.start_delay * 60:
                    break
            try:
                self.prepare()
            except Exception as e:
                print(f"[tiktok] prepare error: {str(e)[:150]}", flush=True)
            time.sleep(15)
        self.started_uploading = True
        self.notify(f"AUTO-UPLOAD SHURU!\nPending: {self.pending_count()} videos. Ab lagataar chalega, "
                    f"run khatam hone tak.")

    def wait_for_session(self):
        """Run ke beech session expire: rukta nahi, login hote hi 1 minute mein chal padta hai."""
        if not self.no_session_notified:
            self.no_session_notified = True
            self.notify("JazzDrive session expire!\n/login karo — login hote hi 1 minute mein upload khud chal padega.")
        while not app.session_valid(self.inst.state_file):
            if self.elapsed_min() > RUN_LIMIT_MIN:
                self.finish_run()
            time.sleep(30)
        self.no_session_notified = False
        self.notify("JazzDrive login mil gaya — 1 minute mein upload dobara shuru.")
        time.sleep(60)

    def run(self):
        time.sleep(15)       # bot.py startup / webhook clear
        self.run_started = time.time()
        try:
            self.ig_startup()
        except Exception as e:
            print(f"[tiktok] ig startup error: {scrub(e)[:150]}", flush=True)
        while True:
            try:
                self.wait_for_start()
                break
            except Exception as e:
                print(f"[tiktok] start wait error: {str(e)[:150]}", flush=True)
                time.sleep(10)

        while True:
            try:
                if self.elapsed_min() > RUN_LIMIT_MIN:
                    self.finish_run()

                if self.paused or getattr(self.inst, "is_working", False) or time.time() < self.cooldown_until:
                    time.sleep(5)
                    continue

                # routine scans / trending refresh
                if self.trend_on and time.time() >= self.next_trend:
                    self.refresh_trending()
                if self.sources:
                    self.scan_async()
                self.ig_scan_step()

                item = self.next_item()
                if not item:
                    if not self.refill():
                        time.sleep(5 if self.ig_has_pending() else 20)
                    continue

                if self.process_item(*item) == "nosession":
                    self.wait_for_session()
                    continue
                time.sleep(GAP_SECONDS)
            except Exception as e:
                print(f"[tiktok] loop error: {str(e)[:200]}", flush=True)
                time.sleep(10)


# ─── Bot ke sath jodna ────────────────────────────────────────

def _install_hooks():
    orig_msg = app.BotInstance.msg
    orig_append_feed = app.append_feed

    def quiet_msg(self, text):
        if getattr(_ctx, "quiet", False):
            return
        return orig_msg(self, text)

    def append_feed_mark(entry):
        orig_append_feed(entry)
        if getattr(_ctx, "vid", None):
            _ctx.ok = True

    app.BotInstance.msg = quiet_msg
    app.append_feed = append_feed_mark

    orig_download_social = app.download_social

    def download_social_ig(url, cookies_file=None):
        if "instagram.com" in (url or "").lower():
            return ig_download(url, cookies_file)       # cookies Secret se, error classify ke saath
        return orig_download_social(url, cookies_file)

    app.download_social = download_social_ig

    orig_register = app.BotInstance.register_handlers

    def register_with_tiktok(self):
        auto = self._tik
        bot = self.bot

        def allowed(m):
            return m.chat.id in self.allowed

        def args(m):
            return (m.text or "").split()[1:]

        HELP = ("TikTok auto-upload (unlimited, khud chalta hai):\n"
                "TikTok video ka link seedha bhejo — username khud add (/tiktok_collect on|off)\n"
                "/tiktok_add @user1 @user2 ya links — profiles\n"
                "/tiktok_limit 100 — har user ki kitni videos\n"
                "/tiktok_tag pakistan lahore — hashtags\n"
                "/tiktok_sound <link ya id> — sound\n"
                "/tiktok_trending — Pakistan trending abhi lao\n"
                "/tiktok_trend on|off — auto-trending\n"
                "/tiktok_delay 10 — login ke kitne minute baad shuru\n"
                "/tiktok_list /tiktok_status /tiktok_scan\n"
                "/tiktok_pause /tiktok_resume /tiktok_retry\n"
                "/tiktok_remove @user | #tag | sound-id\n"
                "INSTAGRAM:\n"
                "/ig_add @user1 @user2 ya instagram.com/user links\n"
                "/ig_remove @user\n"
                "/ig_status  /ig_scan  /ig_resume  /ig_cap 100")

        def reply_added(m, names, bad):
            added, existing = [], []
            for n in names:
                (added if auto.add_source("user", n) else existing).append("@" + n)
            if added:
                auto.save_sources()          # naya user khud scan ho jata hai (force_scan nahi)
            parts = []
            if added:
                parts.append("Add hue: " + ", ".join(added))
            if existing:
                parts.append("Pehle se the: " + ", ".join(existing))
            if bad:
                parts.append(f"{bad} link se username nahi nikla (link dobara copy karo)")
            with auto.lock:
                n_users = sum(1 for k in auto.sources if kind_of(k) == "user")
            parts.append(f"Total users: {n_users}")
            if auto.paused:
                parts.append("Note: auto-upload paused hai — /tiktok_resume bhejo.")
            bot.reply_to(m, "\n".join(parts))

        def is_link_msg(m):
            t = m.text or ""
            return bool(allowed(m) and auto.collect and t and not t.startswith("/") and
                        ("tiktok.com" in t.lower() or re.fullmatch(r"\s*(@[A-Za-z0-9._]{2,24}\s*)+", t)))

        @bot.message_handler(func=is_link_msg)
        def on_links(m):
            names, bad = extract_users(m.text)
            reply_added(m, names, bad)

        @bot.message_handler(commands=["tiktok_collect"])
        def cmd_collect(m):
            if not allowed(m):
                return
            a = (args(m) or [""])[0].lower()
            if a not in ("on", "off"):
                bot.reply_to(m, f"Link-collect abhi: {'ON' if auto.collect else 'OFF'}\n"
                                "ON mein TikTok link bhejo to uska username list mein add hota hai.\n"
                                "/tiktok_collect on  ya  /tiktok_collect off")
                return
            auto.collect = (a == "on")
            auto.save_sources()
            bot.reply_to(m, f"Link-collect {'ON' if auto.collect else 'OFF'}")

        @bot.message_handler(commands=["tiktok_limit"])
        def cmd_limit(m):
            if not allowed(m):
                return
            a = (args(m) or [""])[0]
            if not a.isdigit() or not (1 <= int(a) <= 2000):
                bot.reply_to(m, f"Har user ki {auto.user_limit} videos li jati hain.\nBadalne ke liye: /tiktok_limit 100 (1 se 2000)")
                return
            auto.user_limit = int(a)
            auto.force_scan = True
            auto.save_sources()
            bot.reply_to(m, f"Ab har user ki {auto.user_limit} videos. Sab users dobara scan honge.")

        def add_many(m, kind, parser, shown):
            names = [parser(t) for t in args(m) if not t.isdigit() or kind == "sound"]
            names = [n for n in names if n]
            if not names:
                bot.reply_to(m, HELP)
                return
            added, existing = [], []
            for n in names:
                (added if auto.add_source(kind, n) else existing).append(shown(n))
            auto.save_sources()
            auto.force_scan = True
            text = ""
            if added:
                text += "Add hue: " + ", ".join(added) + "\n"
            if existing:
                text += "Pehle se the: " + ", ".join(existing) + "\n"
            text += "\nVideos dhoondh kar upload apne aap hoga."
            if auto.paused:
                text += "\n\nNote: auto-upload paused hai — /tiktok_resume bhejo."
            bot.reply_to(m, text)

        @bot.message_handler(commands=["tiktok", "tiktok_add"])
        def cmd_add(m):
            if not allowed(m):
                return
            body = (m.text or "").split(None, 1)
            names, bad = extract_users(body[1] if len(body) > 1 else "", bare=True)
            if not names and not bad:
                bot.reply_to(m, HELP)
                return
            reply_added(m, names, bad)

        @bot.message_handler(commands=["tiktok_tag"])
        def cmd_tag(m):
            if allowed(m):
                add_many(m, "tag", parse_tag, lambda n: "#" + n)

        @bot.message_handler(commands=["tiktok_sound"])
        def cmd_sound(m):
            if allowed(m):
                add_many(m, "sound", parse_sound, lambda n: "sound " + n)

        @bot.message_handler(commands=["tiktok_trending"])
        def cmd_trending(m):
            if not allowed(m):
                return
            bot.reply_to(m, "Pakistan trending dhoondh raha hoon (1 se 2 minute)...")
            threading.Thread(target=lambda: auto.refresh_trending(announce=True), daemon=True).start()

        @bot.message_handler(commands=["tiktok_trend"])
        def cmd_trend(m):
            if not allowed(m):
                return
            a = (args(m) or [""])[0].lower()
            if a not in ("on", "off"):
                bot.reply_to(m, f"Auto-trending abhi: {'ON' if auto.trend_on else 'OFF'}\n/tiktok_trend on  ya  /tiktok_trend off")
                return
            auto.trend_on = (a == "on")
            if auto.trend_on:
                auto.next_trend = 0
            auto.save_sources()
            bot.reply_to(m, f"Auto-trending {'ON' if auto.trend_on else 'OFF'}")

        @bot.message_handler(commands=["tiktok_delay"])
        def cmd_delay(m):
            if not allowed(m):
                return
            a = (args(m) or [""])[0]
            if not a.isdigit() or not (0 <= int(a) <= 120):
                bot.reply_to(m, f"Login ke baad upload {auto.start_delay} minute mein shuru hota hai.\nBadalne ke liye: /tiktok_delay 10 (0 se 120)")
                return
            auto.start_delay = int(a)
            auto.save_sources()
            bot.reply_to(m, f"Ab login ke {auto.start_delay} minute baad upload shuru hoga.")

        @bot.message_handler(commands=["tiktok_remove"])
        def cmd_remove(m):
            if not allowed(m):
                return
            t = (args(m) or [""])[0]
            key = None
            if t.startswith("#"):
                n = parse_tag(t)
                key = make_key("tag", n) if n else None
            elif t.startswith("@") or parse_profile(t):
                n = parse_profile(t)
                key = make_key("user", n) if n else None
            else:
                n = parse_sound(t)
                key = make_key("sound", n) if n else None
            if key and auto.remove_source(key):
                auto.save_sources()
                bot.reply_to(m, f"{label(key)} hata diya. (Pehle upload hui videos feed mein rahengi.)")
            else:
                bot.reply_to(m, "Ye source list mein nahi hai. /tiktok_list dekho.")

        @bot.message_handler(commands=["tiktok_list"])
        def cmd_list(m):
            if not allowed(m):
                return
            if not auto.sources:
                bot.reply_to(m, "Abhi koi source nahi — auto-trending thodi der mein laga dega.")
                return
            lines = []
            with auto.lock:
                for key, info in auto.sources.items():
                    pend = len(auto.pool.get(key, ()))
                    tag = " [auto]" if info.get("auto") else ""
                    lines.append(f"{label(key)}{tag} — pending {pend}")
            bot.reply_to(m, "TikTok sources:\n" + "\n".join(lines)[:3500])

        @bot.message_handler(commands=["tiktok_status"])
        def cmd_status(m):
            if not allowed(m):
                return
            kinds = {"user": 0, "tag": 0, "sound": 0, "ig": 0}
            for k in auto.sources:
                kinds[kind_of(k)] += 1
            if auto.paused:
                state = "PAUSED"
            elif not auto.started_uploading:
                if auto.login_at is None:
                    state = "JazzDrive login ka intezar"
                else:
                    left = max(0, int(auto.start_delay - (time.time() - auto.login_at) / 60))
                    state = f"{left} minute mein upload shuru"
            else:
                state = "upload chal raha hai"
            bot.reply_to(m,
                f"TikTok auto: {state}\n"
                f"Auto-trending: {'ON' if auto.trend_on else 'OFF'}\n"
                f"Users: {kinds['user']} | har user ki {auto.user_limit} videos | link-collect {'ON' if auto.collect else 'OFF'}\n"
                f"Total uploaded: {len(auto.seen)} (is run mein {auto.session_done})\n"
                f"{auto.ig_summary()}\n"
                f"Pending: {auto.pending_count()}\n"
                f"Failed (3 koshish ke baad): {auto.dead_count()}\n"
                f"Run ka waqt: {int(auto.elapsed_min())}/{RUN_LIMIT_MIN} min\n"
                f"Abhi: {auto.current or '-'}")

        @bot.message_handler(commands=["tiktok_scan"])
        def cmd_scan(m):
            if allowed(m):
                auto.force_scan = True
                bot.reply_to(m, "Scan abhi shuru hoga...")

        @bot.message_handler(commands=["tiktok_pause"])
        def cmd_pause(m):
            if allowed(m):
                auto.paused = True
                auto.save_sources()
                bot.reply_to(m, "TikTok auto-upload pause. /tiktok_resume se chalao.")

        @bot.message_handler(commands=["tiktok_resume"])
        def cmd_resume(m):
            if allowed(m):
                auto.paused = False
                auto.consec_fail = 0
                auto.cooldown_until = 0
                auto.save_sources()
                bot.reply_to(m, "TikTok auto-upload chal pada.")

        @bot.message_handler(commands=["tiktok_retry"])
        def cmd_retry(m):
            if not allowed(m):
                return
            with auto.lock:
                n = auto.dead_count()
                auto.fails = {}
            auto.save_fails()
            auto.force_scan = True
            bot.reply_to(m, f"{n} failed videos dobara try hongi.")

        # ─── Instagram commands ───────────────────────────────
        @bot.message_handler(commands=["ig_add"])
        def cmd_ig_add(m):
            if not allowed(m):
                return
            body = (m.text or "").split(None, 1)
            names, bad = extract_ig_users(body[1] if len(body) > 1 else "")
            if not names and not bad:
                bot.reply_to(m, "Instagram accounts add karne ke liye:\n/ig_add @user1 @user2\n"
                                "ya profile links (instagram.com/user)")
                return
            added, existing = [], []
            for n in names:
                (added if auto.add_source("ig", n) else existing).append("@" + n)
            if added:
                auto.ig_next_scan = 0
                auto.save_sources()
            parts = []
            if added:
                parts.append("IG add hue: " + ", ".join(added))
            if existing:
                parts.append("Pehle se the: " + ", ".join(existing))
            if bad:
                parts.append(f"{bad} link se username nahi nikla (reel/post link nahi, profile link bhejo)")
            parts.append(f"Har account ki {auto.user_limit} reels tak (badalne ke liye /tiktok_limit 400)")
            if not ig_cookies_ok():
                parts.append(f"Warning: Instagram cookies nahi mili ({_IG_STATE['note']}). "
                             f"GitHub Secret INSTAGRAM_COOKIES daalo.")
            if auto.paused:
                parts.append("Note: auto-upload paused hai — /tiktok_resume bhejo.")
            bot.reply_to(m, "\n".join(parts))

        @bot.message_handler(commands=["ig_remove"])
        def cmd_ig_remove(m):
            if not allowed(m):
                return
            n = parse_ig_user((args(m) or [""])[0])
            key = make_key("ig", n) if n else None
            if key and auto.remove_source(key):
                auto.save_sources()
                bot.reply_to(m, f"{label(key)} hata diya. (Pehle upload hui videos feed mein rahengi.)")
            else:
                bot.reply_to(m, "Ye Instagram account list mein nahi hai. /tiktok_list dekho.")

        @bot.message_handler(commands=["ig_status"])
        def cmd_ig_status(m):
            if allowed(m):
                bot.reply_to(m, auto.ig_summary(detail=True))

        @bot.message_handler(commands=["ig_scan"])
        def cmd_ig_scan(m):
            if not allowed(m):
                return
            with auto.lock:
                for k, info in auto.sources.items():
                    if k.startswith("i:"):
                        info["listed"] = False
            auto.ig_next_scan = 0
            bot.reply_to(m, "Instagram list jaldi hi (gaps ke saath, ek ek account) dobara banegi.")

        @bot.message_handler(commands=["ig_resume"])
        def cmd_ig_resume(m):
            if not allowed(m):
                return
            auto.ig_cooldown_until = 0
            auto.ig_block_streak = 0
            auto.ig_consec_fail = 0
            prepare_ig_cookies()
            bot.reply_to(m, "Instagram cooldown hata diya.\n" + auto.ig_summary())

        @bot.message_handler(commands=["ig_cap"])
        def cmd_ig_cap(m):
            if not allowed(m):
                return
            a = (args(m) or [""])[0]
            if not a.isdigit() or not (1 <= int(a) <= 1000):
                bot.reply_to(m, f"Is run mein max {auto.ig_cap} Instagram videos.\nBadalne ke liye: /ig_cap 100 (1 se 1000)")
                return
            auto.ig_cap = int(a)
            auto.save_sources()
            bot.reply_to(m, f"Ab is run mein max {auto.ig_cap} Instagram videos.")

        orig_register(self)

    app.BotInstance.register_handlers = register_with_tiktok


def main():
    _install_hooks()

    if not all(c["token"] for c in app.BOTS):
        raise SystemExit("BOT_TOKEN missing! Repo Settings > Secrets and variables > Actions mein BOT_TOKEN add karo.")

    threads = []
    for cfg in app.BOTS:
        inst = app.BotInstance(cfg["token"], cfg["chat_id"], cfg["state_file"])
        inst._tik = TikAuto(inst)
        threading.Thread(target=inst._tik.run, daemon=True).start()
        t = threading.Thread(target=inst.run, daemon=True)
        threads.append(t)
        t.start()
        time.sleep(2)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
