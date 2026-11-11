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
"""

import glob
import json
import os
import random
import re
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
RUN_LIMIT_MIN = 330                 # itne minute baad naya video nahi uthana, saaf band (workflow step 335 min ka hai)
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
    return {"user": "u:", "tag": "t:", "sound": "s:"}[kind] + name


def label(key):
    kind, name = key.split(":", 1)
    return {"u": f"@{name}", "t": f"#{name}", "s": f"sound {name}"}[kind]


def kind_of(key):
    return {"u": "user", "t": "tag", "s": "sound"}[key[0]]


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

    # persistence
    def save_sources(self):
        with self.lock:
            _write_json(PROFILES_FILE, {
                "paused": self.paused, "trend_on": self.trend_on, "start_delay": self.start_delay,
                "user_limit": self.user_limit, "collect": self.collect,
                "extra_idx": self.extra_idx, "profiles": self.sources})

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
            if time.time() - self.last_nothing_msg > 3600:
                self.last_nothing_msg = time.time()
                self.notify("TikTok: queue khali — sab users ki videos upload ho chuki ya TikTok block kar raha hai.\n"
                            "Naye users add karo (TikTok video ka link bot ko bhejo) ya /tiktok_limit 300 karo.")
        return found

    def next_item(self):
        with self.lock:
            for _ in range(len(self.rr)):
                key = self.rr[0]
                self.rr.rotate(-1)
                q = self.pool.get(key)
                if q:
                    vid, url = q.popleft()
                    return key, vid, url
        return None

    # ek video — returns "nosession" agar Jazz login nahi
    def process_item(self, key, vid, url):
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
                    f"Is run mein {self.session_done} videos upload hui. Total: {len(self.seen)} | Pending: {self.pending_count()}\n"
                    f"Agla scheduled run khud wahin se shuru karega.")
        time.sleep(4)
        os._exit(0)      # workflow ka agla step (session encrypt + save) chal jaye

    def prepare(self):
        """Upload se pehle: trending + videos ki list tayyar (Jazz ki zaroorat nahi)."""
        if self.trend_on and time.time() >= self.next_trend:
            self.refresh_trending()
        if self.sources:
            self.scan_due()

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

                item = self.next_item()
                if not item:
                    if not self.refill():
                        time.sleep(20)
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
                "/tiktok_remove @user | #tag | sound-id")

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
            kinds = {"user": 0, "tag": 0, "sound": 0}
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
