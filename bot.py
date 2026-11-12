import os, re, time, json, asyncio, threading, queue, subprocess, requests, zipfile, mimetypes, telebot, shutil, hashlib
from datetime import datetime
from urllib.parse import urlparse
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright

BROWSER_ARGS = ["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
VIDEO_EXTS = [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"]
ZIP_EXTS   = [".zip", ".rar", ".7z", ".tar", ".gz"]
MAX_SIZE_MB = 1990
LOGIN_SCREENSHOTS = True   # login ke har step ka screenshot Telegram par (False = band)
TMDB_API_KEY = "e788be7e4b8f7275eb274e76f08832d9"

# pytubefix TV-code login token — repo root mein persist hota hai
YT_TOKEN_FILE = "yt_token.json"

def tmdb_info(query):
    try:
        base = "https://api.themoviedb.org/3"
        r = requests.get(base + "/search/multi", params={
            "api_key": TMDB_API_KEY, "query": query, "include_adult": True
        }, timeout=10)
        results = [x for x in r.json().get("results", []) if x.get("media_type") in ("movie", "tv")]
        if not results:
            return None, None
        res = results[0]
        mtype = res.get("media_type", "movie")
        mid = res["id"]
        if mtype == "tv":
            d = requests.get(base + "/tv/" + str(mid), params={"api_key": TMDB_API_KEY}, timeout=10).json()
            title = d.get("name", "N/A")
            year = d.get("first_air_date", "")[:4]
            rt = d.get("episode_run_time", [])
            runtime = (str(rt[0]) + " min/ep") if rt else "N/A"
            extra = "Seasons: " + str(d.get("number_of_seasons", "N/A")) + " | Episodes: " + str(d.get("number_of_episodes", "N/A"))
        else:
            d = requests.get(base + "/movie/" + str(mid), params={"api_key": TMDB_API_KEY}, timeout=10).json()
            title = d.get("title", "N/A")
            year = d.get("release_date", "")[:4]
            rt = d.get("runtime", 0)
            runtime = (str(rt) + " min") if rt else "N/A"
            extra = ""
        genres = ", ".join([g["name"] for g in d.get("genres", [])])
        countries = ", ".join([c["name"] for c in d.get("production_countries", [])])
        languages = ", ".join([l["english_name"] for l in d.get("spoken_languages", [])])
        overview = d.get("overview", "N/A")
        if len(overview) > 350:
            overview = overview[:350] + "..."
        rating = d.get("vote_average", 0)
        poster = d.get("poster_path", "")
        poster_url = ("https://image.tmdb.org/t/p/w500" + poster) if poster else None
        tag = "Movie" if mtype == "movie" else "Series"
        lines = [
            tag + ": " + title + " (" + year + ")",
            "",
            "Rating: " + str(round(rating, 1)) + "/10",
            "Country: " + (countries or "N/A"),
            "Language: " + (languages or "N/A"),
            "Duration: " + runtime,
            "Genre: " + (genres or "N/A"),
        ]
        if extra:
            lines.append(extra)
        lines.append("")
        lines.append("Story:")
        lines.append(overview)
        return "\n".join(lines), poster_url
    except Exception as e:
        return "Error: " + str(e), None


# ─── YouTube: pytubefix (TV-code login) + yt-dlp (latest) ─────

def ffmpeg_merge_av(vid, aud, out):
    try:
        r = subprocess.run([
            "ffmpeg", "-y", "-i", vid, "-i", aud,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", out
        ], capture_output=True, text=True, timeout=600)
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            for f in [vid, aud]:
                try: os.remove(f)
                except: pass
            return out
        print(f"Merge fail: {r.stderr[-300:]}")
    except Exception as e:
        print(f"FFmpeg merge error: {e}")
    return None


def download_youtube_native(url, quality="1080"):
    """
    pytubefix (TV-code OAuth login, token reused) → yt-dlp (latest) fallback.
    Returns local file path or None.
    """
    safe = safe_filename(url.split("v=")[-1].split("/")[-1].split("?")[0])[:20]

    # ── Method 1: pytubefix TV-code login ──
    try:
        from pytubefix import YouTube as PT
        from pytubefix.cli import on_progress

        print(f"pytubefix: Token {'cached' if os.path.exists(YT_TOKEN_FILE) else 'nahi — TV code aayega'}")

        yt = PT(
            url,
            on_progress_callback=on_progress,
            use_oauth=True,
            allow_oauth_cache=True,
            token_file=YT_TOKEN_FILE,
        )

        q_list = {
            "2160": [2160, 1440, 1080, 720],
            "1440": [1440, 1080, 720],
            "1080": [1080, 720, 480],
            "720":  [720, 480, 360],
            "480":  [480, 360],
        }.get(str(quality), [1080, 720, 480])

        vid_s = None
        for res in q_list:
            vid_s = yt.streams.filter(
                adaptive=True, file_extension="mp4", res=f"{res}p"
            ).filter(lambda s: "avc" in (s.video_codec or "")).first()
            if not vid_s:
                vid_s = yt.streams.filter(
                    adaptive=True, file_extension="mp4", res=f"{res}p"
                ).first()
            if vid_s:
                print(f"pytubefix video: {vid_s.resolution} | {vid_s.video_codec}")
                break

        aud_s = yt.streams.filter(only_audio=True).order_by("abr").desc().first()

        if vid_s and aud_s:
            vid_p = f"/tmp/{safe}_V.mp4"
            aud_p = f"/tmp/{safe}_A.mp4"
            out_p = f"/tmp/{safe}_{vid_s.resolution}.mp4"

            vid_s.download(output_path="/tmp", filename=os.path.basename(vid_p), skip_existing=False)
            aud_s.download(output_path="/tmp", filename=os.path.basename(aud_p), skip_existing=False)

            if os.path.exists(vid_p) and os.path.exists(aud_p):
                merged = ffmpeg_merge_av(vid_p, aud_p, out_p)
                if merged:
                    print(f"pytubefix OK: {yt.title[:40]} | {vid_s.resolution}")
                    return merged

        # Progressive fallback
        prog = None
        for res in [f"{r}p" for r in q_list]:
            prog = yt.streams.filter(progressive=True, file_extension="mp4", res=res).first()
            if prog: break
        if not prog:
            prog = yt.streams.get_highest_resolution()
        if prog:
            fname = prog.download(output_path="/tmp", filename=f"{safe}_{prog.resolution}.mp4", skip_existing=False)
            if fname and os.path.exists(fname):
                print(f"pytubefix progressive OK: {prog.resolution}")
                return fname

    except ImportError:
        subprocess.run(["pip", "install", "-U", "pytubefix", "-q"])
    except Exception as e:
        err = str(e).lower()
        HARD = ["private", "members only", "removed", "not available in your country", "copyright"]
        if any(x in err for x in HARD):
            print(f"YouTube skip (restricted): {str(e)[:150]}")
            return None
        print(f"pytubefix fail: {str(e)[:200]}")

    # ── Method 2: yt-dlp (latest, force-updated below in main) ──
    try:
        import yt_dlp
        fmt = (
            f"bestvideo[height<={quality}][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]/best"
        )
        out_p = f"/tmp/{safe}.%(ext)s"
        opts = {
            "format": fmt,
            "outtmpl": out_p,
            "merge_output_format": "mp4",
            "quiet": False,
            "noprogress": False,
            "retries": 8,
            "fragment_retries": 8,
            "socket_timeout": 60,
            "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
            "postprocessor_args": {"ffmpeg": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                base = os.path.splitext(ydl.prepare_filename(info))[0]
                for ext in [".mp4", ".mkv", ".webm"]:
                    fp = base + ext
                    if os.path.exists(fp):
                        print(f"yt-dlp OK: {info.get('height','?')}p")
                        return fp
    except Exception as e:
        print(f"yt-dlp fail: {str(e)[:200]}")

    return None


# ─── Instagram / TikTok: yt-dlp (individual, no-zip) ───────────

FEED_FILE      = "feed.json"
SOCIAL_FOLDER  = "ZeroFeed"
IG_COOKIES     = "instagram_cookies.txt"

def download_social(url, cookies_file=None):
    """
    Instagram reels / TikTok videos ke liye yt-dlp downloader.
    cookies_file diya jaye to use hoga (Instagram ke liye zaroori,
    TikTok public videos ke liye generally zaroorat nahi).
    Returns (local_path, title) ya (None, None)
    """
    try:
        import yt_dlp
    except ImportError:
        subprocess.run(["pip", "install", "-U", "yt-dlp", "-q"])
        import yt_dlp

    safe = safe_filename(url.split("?")[0].rstrip("/").split("/")[-1])[:20] or "social"
    out_p = f"/tmp/{safe}_%(id)s.%(ext)s"
    opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": out_p,
        "merge_output_format": "mp4",
        "quiet": False,
        "noprogress": False,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 60,
    }
    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                fp = ydl.prepare_filename(info)
                if os.path.exists(fp):
                    return fp, info.get("title") or "Video"
                # kabhi kabhi ext merge ke baad badal jata hai
                base = os.path.splitext(fp)[0]
                for ext in [".mp4", ".mkv", ".webm"]:
                    if os.path.exists(base + ext):
                        return base + ext, info.get("title") or "Video"
    except Exception as e:
        print(f"social dl fail: {str(e)[:200]}")
    return None, None


def load_feed():
    if not os.path.exists(FEED_FILE):
        return []
    try:
        with open(FEED_FILE) as f:
            return json.load(f)
    except:
        return []


def append_feed(entry):
    feed = load_feed()
    feed.insert(0, entry)  # newest video sabse upar (feed mein sabse pehle dikhega)
    with open(FEED_FILE, "w") as f:
        json.dump(feed, f, indent=2, ensure_ascii=False)


def git_push_feed():
    """feed.json ko turant GitHub pe push karo — workflow khatam hone ka wait nahi karna"""
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", FEED_FILE], check=False)
        r = subprocess.run(["git", "commit", "-m", "Update feed.json [skip ci]"], capture_output=True, text=True)
        if "nothing to commit" not in (r.stdout + r.stderr):
            push = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=60)
            if push.returncode != 0:
                print(f"git push fail: {push.stderr[-300:]}")
    except Exception as e:
        print(f"git_push_feed error: {e}")


BOTS = [
    {"token": os.environ.get("BOT_TOKEN", "").strip(),  # GitHub Secret se aata hai — code mein token na likhein
     "chat_id": 7144917062, "state_file": "state1.json"},
]

ALLOWED_USERS_FILE = "allowed_users.json"

# ─── User helpers ─────────────────────────────────────────────

def load_allowed(admin_id):
    if not os.path.exists(ALLOWED_USERS_FILE):
        with open(ALLOWED_USERS_FILE, "w") as f: json.dump([admin_id], f)
    with open(ALLOWED_USERS_FILE) as f: return set(json.load(f))

def save_allowed(s):
    with open(ALLOWED_USERS_FILE, "w") as f: json.dump(list(s), f)

# ─── General helpers ──────────────────────────────────────────

def is_zip_url(link):
    return any(link.lower().endswith(ext) or ext in link.lower() for ext in ZIP_EXTS)

def is_video_file(f):
    return any(f.lower().endswith(ext) for ext in VIDEO_EXTS)

def is_m3u8(url):
    return '.m3u8' in url.lower()

def safe_filename(t):
    return re.sub(r'[\\/*?:"<>|]', '', t).strip().replace(' ', '_')[:80]

def file_ok(f, min_mb=0.5):
    return os.path.exists(f) and os.path.getsize(f) / (1024*1024) >= min_mb

def clean(f):
    if f and os.path.exists(f): os.remove(f)

def fmt_bytes(n):
    for unit in ["B","KB","MB","GB"]:
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def get_referers(url):
    try:
        parsed = urlparse(url)
        dr = f"{parsed.scheme}://{parsed.netloc}/"
    except:
        dr = "https://www.google.com/"
    return [dr, "https://www.google.com/", "https://www.facebook.com/", ""]

def get_filename_from_url(url):
    try:
        path = urlparse(url).path
        name = path.split("/")[-1].split("?")[0]
        name = requests.utils.unquote(name)
        name = safe_filename(name)
        if "." not in name or len(name) < 3: name = "video.mp4"
        return name
    except:
        return "video.mp4"

def get_index_wait(size_bytes):
    mb = size_bytes / (1024*1024)
    if mb <= 20:   return 3
    if mb <= 100:  return 12
    if mb <= 500:  return 20
    if mb <= 1200: return 30
    return 40


# ─── Cookie / session helpers (login ke baad cookie.json + keep-alive) ─────
COOKIE_JSON = "cookie.json"          # login ke baad yahan session save hota hai (aur GitHub par push)
OTP_VALID_SEC = 55                   # Jazz OTP ~60 sec mein expire hota hai
KEEPALIVE_PING_SEC = 4 * 60          # har itne sec mein session ping
KEEPALIVE_BROWSER_SEC = 30 * 60      # har itne sec mein browser se site khol kar session refresh
COOKIE_PUSH_MIN_GAP = 20 * 60        # cookie.json GitHub par baar baar push na ho


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def norm_number(raw):
    """+92 324 3387052 / 923243387052 / 3243387052 -> 03243387052"""
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("92") and len(d) == 12: d = "0" + d[2:]
    elif len(d) == 10 and d.startswith("3"): d = "0" + d
    return d


def cookie_fingerprint(path):
    cookies, key = load_cookies(path)
    if not cookies or not key: return None
    return hashlib.sha1((json.dumps(sorted(cookies.items())) + key).encode()).hexdigest()


def repo_visibility(repo, token=None):
    """'public' / 'private' / None (pata nahi)"""
    if not repo: return None
    h = {"Accept": "application/vnd.github+json", "User-Agent": "jazz-bot"}
    if token: h["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}", headers=h, timeout=15)
        if r.status_code == 200:
            return "private" if r.json().get("private") else "public"
        if r.status_code == 404 and not token:
            return "private"          # bina token ke 404 = private repo
        return None
    except Exception:
        return None


def fetch_cookie_from_repo():
    """COOKIE_REPO + COOKIE_TOKEN set ho to cookie.json wahan se utaro."""
    repo = os.environ.get("COOKIE_REPO", "").strip(); tok = os.environ.get("COOKIE_TOKEN", "").strip()
    if not (repo and tok): return False
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/{COOKIE_JSON}",
                         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github.raw",
                                  "User-Agent": "jazz-bot"}, timeout=20)
        if r.status_code != 200: return False
        json.loads(r.text)
        with open(COOKIE_JSON, "w", encoding="utf-8") as f: f.write(r.text)
        return True
    except Exception:
        return False


def restore_session_from_cookie_json(state_file):
    """state_file kharab/khali ho aur cookie.json valid ho to use restore karo (OTP ki zaroorat nahi)."""
    fetch_cookie_from_repo()
    if not os.path.exists(COOKIE_JSON): return False
    if not session_valid(COOKIE_JSON): return False
    shutil.copyfile(COOKIE_JSON, state_file)
    return True


def _push_cookie_api(repo, token):
    import base64
    url = f"https://api.github.com/repos/{repo}/contents/{COOKIE_JSON}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "jazz-bot"}
    sha = None
    r = requests.get(url, headers=h, timeout=20)
    if r.status_code == 200: sha = r.json().get("sha")
    elif r.status_code != 404: return False, f"api{r.status_code}"
    with open(COOKIE_JSON, "rb") as f: content = base64.b64encode(f.read()).decode()
    body = {"message": "Update cookie.json [skip ci]", "content": content}
    if sha: body["sha"] = sha
    r = requests.put(url, headers=h, json=body, timeout=30)
    return (r.status_code in (200, 201)), f"api{r.status_code}"


def push_cookie_json():
    """cookie.json GitHub par. returns (ok, note). Public repo mein by default push NAHI hota (cookie leak ka khatra)."""
    if not os.path.exists(COOKIE_JSON): return False, "nofile"
    allow_public = os.environ.get("ALLOW_PUBLIC_COOKIE", "1") != "0"   # default: public repo mein bhi push (band karne ke liye ALLOW_PUBLIC_COOKIE=0)
    crepo = os.environ.get("COOKIE_REPO", "").strip(); ctok = os.environ.get("COOKIE_TOKEN", "").strip()
    if crepo and ctok:                      # alag private repo mein (Contents API)
        vis = repo_visibility(crepo, ctok)
        if vis != "private" and not allow_public: return False, "public" if vis == "public" else "unknown"
        return _push_cookie_api(crepo, ctok)
    vis = repo_visibility(os.environ.get("GITHUB_REPOSITORY", "").strip())
    if not allow_public:
        if vis is None: return False, "unknown"
        if vis == "public": return False, "public"
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "-f", COOKIE_JSON], check=False)
        r = subprocess.run(["git", "commit", "-m", "Update cookie.json [skip ci]"], capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        if "nothing to commit" in out or "nothing added" in out: return True, "same"
        push = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=60)
        if push.returncode != 0:
            subprocess.run(["git", "pull", "--rebase", "--autostash"], capture_output=True, text=True, timeout=90)
            push = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=60)
        if push.returncode != 0: return False, "git: " + push.stderr[-150:]
        return True, "pushed"
    except Exception as e:
        return False, "err: " + str(e)[:100]


# ─── JazzDrive API helpers (from v5) ─────────────────────────

def cookie_file(state_file):
    return state_file  # state_file is already the cookie file

def load_cookies(state_file):
    if not os.path.exists(state_file): return None, None
    try:
        with open(state_file) as f: data = json.load(f)
        raw = data.get("cookies", [])
        cookies = {c["name"]: c["value"] for c in raw}
        key = next((c["value"] for c in raw if c["name"].lower() == "validationkey"), None)
        if not key:  # new login: key kabhi localStorage mein hoti hai
            for o in data.get("origins", []):
                for kv in o.get("localStorage", []):
                    if str(kv.get("name", "")).lower() == "validationkey":
                        key = kv.get("value"); break
                if key: break
        return cookies, key
    except: return None, None

def session_valid(state_file):
    """Real API call se check — cookie file maujood hona kaafi nahi"""
    cookies, key = load_cookies(state_file)
    if not cookies or not key: return False
    try:
        _, root_id = api_get_folders(cookies, key)
        return bool(root_id)
    except: return False

def scan_login_data(obj, out=None):
    """Login response JSON mein se 'ticket' aur 'validationKey' dhundo (nested bhi)"""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl == "ticket" and isinstance(v, str) and v: out["ticket"] = v
            elif kl == "validationkey" and isinstance(v, str) and v: out["key"] = v
            elif isinstance(v, (dict, list)): scan_login_data(v, out)
    elif isinstance(obj, list):
        for i in obj: scan_login_data(i, out)
    return out

def ticket_expiry(tok):
    """Ticket JWT-style hai (payload.signature) — exp nikalo"""
    import base64
    for seg in tok.split(".")[:2]:
        try:
            seg += "=" * (-len(seg) % 4)
            d = json.loads(base64.urlsafe_b64decode(seg))
            if isinstance(d, dict) and "exp" in d: return int(d["exp"])
        except: pass
    return None

def api_get_folders(cookies, key):
    try:
        url = f"https://cloud.jazzdrive.com.pk/sapi/media/folder?action=get&validationkey={key}"
        r = requests.get(url, cookies=cookies, headers={"User-Agent": WEB_UA}, timeout=20)
        folders_list = r.json().get("data", {}).get("folders", [])
        root_id = next((f["id"] for f in folders_list if f.get("name") == "/"), None)
        if not root_id: return [], None
        subs = [(f["name"], f["id"]) for f in folders_list
                if f.get("parentid") == root_id and f.get("name") != "/"]
        return subs, root_id
    except: return [], None

def api_create_folder(name, parent_id, cookies, key):
    try:
        url = f"https://cloud.jazzdrive.com.pk/sapi/media/folder?action=save&validationkey={key}"
        payload = {"data": {"magic": False, "offline": False, "name": name, "parentid": int(parent_id)}}
        r = requests.post(url, cookies=cookies, json=payload, headers={"User-Agent": WEB_UA}, timeout=20)
        d = r.json()
        new_id = d.get("id") or d.get("data", {}).get("id")
        return new_id if new_id else parent_id
    except: return parent_id

def api_generate_share_link(item_id, is_folder, cookies, key):
    try:
        sess = requests.Session()
        sess.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5)))
        if is_folder:
            url     = f"https://cloud.jazzdrive.com.pk/sapi/link/folder?action=save&validationkey={key}"
            payload = {"data": {"folderid": int(item_id)}}
        else:
            url     = f"https://cloud.jazzdrive.com.pk/sapi/media/set?action=save&validationkey={key}"
            payload = {"data": {"set": {"items": [int(item_id)]}}}
        r = sess.post(url, cookies=cookies, json=payload, headers={"User-Agent": WEB_UA}, timeout=25)
        d = r.json()
        link = d.get("url") or d.get("data", {}).get("url")
        if not link:
            h = d.get("hash") or d.get("data", {}).get("hash")
            if h: link = f"https://cloud.jazzdrive.com.pk/share/{'f/' if is_folder else ''}{h}"
        return link
    except: return None

def api_upload_file(local_path, filename, folder_id, cookies, key, cancelled_flag=None):
    """Upload file directly via JazzDrive REST API — no Playwright needed"""
    fsize = os.path.getsize(local_path)
    mime  = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    meta  = {
        "name": filename, "size": str(fsize),
        "folderid": str(folder_id), "contenttype": mime,
        "modificationdate": datetime.now().strftime("%Y%m%dT%H%M%SZ")
    }
    sess = requests.Session()
    sess.mount("https://", HTTPAdapter(max_retries=Retry(total=5, backoff_factor=0.5)))

    def progress_cb(monitor):
        if cancelled_flag and cancelled_flag():
            raise Exception("Cancelled by user")

    with open(local_path, "rb") as f:
        m   = MultipartEncoder(fields={
            "data": (None, json.dumps({"data": meta}), "application/json"),
            "file": (filename, f, mime)
        })
        mon = MultipartEncoderMonitor(m, progress_cb)
        r   = sess.post(
            f"https://cloud.jazzdrive.com.pk/sapi/upload?action=save&acceptasynchronous=true&validationkey={key}",
            data=mon,
            headers={"User-Agent": WEB_UA, "Content-Type": mon.content_type},
            cookies=cookies, timeout=600
        )

    if r.status_code == 200:
        d   = r.json()
        fid = d.get("data", {}).get("id") or d.get("id")
        if not fid and isinstance(d.get("data"), list) and d["data"]:
            fid = d["data"][0].get("id")
        return True, fid
    return False, None


# ─── Bot Instance ─────────────────────────────────────────────

class BotInstance:
    def __init__(self, token, chat_id, state_file):
        self.token        = token
        self.chat_id      = chat_id
        self.state_file   = state_file
        self.bot          = telebot.TeleBot(token)
        self.task_queue   = queue.Queue()
        self.is_working   = False
        self.worker_lock  = threading.Lock()
        self.queue_paused = False
        self.cancelled    = set()
        self.allowed      = load_allowed(chat_id)
        self.login_busy   = False
        self._cookie_fp   = None
        self._cookie_push_at = 0
        self.ctx = {
            "state": "IDLE",
            "number": None, "otp": None,
            "pending_link": None, "pending_type": None,
            "pending_links": None, "pending_name": None,
        }

    def msg(self, text):
        try: self.bot.send_message(self.chat_id, text)
        except:
            try: self.bot.send_message(self.chat_id, re.sub(r'[*_`\[\]]', '', text))
            except: pass

    def send_photo(self, path, caption=""):
        try:
            with open(path, "rb") as f: self.bot.send_photo(self.chat_id, f, caption=caption)
        except: pass

    def take_screenshot(self, page, caption=""):
        try:
            page.screenshot(path="s.png")
            self.send_photo("s.png", caption)
            os.remove("s.png")
        except: pass

    def next_task_id(self):
        import uuid; return str(uuid.uuid4())[:8]

    def is_cancelled(self, task_id):
        return task_id and (task_id in self.cancelled or f"all_{self.chat_id}" in self.cancelled)

    # ─── Session keep-alive (cookie expire na ho) ─────────────

    def ping_session(self):
        """Real API call (har 4 min). Server naya cookie de to state file mein update."""
        cookies, key = load_cookies(self.state_file)
        if not cookies or not key: return False
        try:
            sess = requests.Session()
            sess.headers.update({"User-Agent": WEB_UA})
            for k, v in cookies.items(): sess.cookies.set(k, v, domain="cloud.jazzdrive.com.pk")
            r = sess.get(f"https://cloud.jazzdrive.com.pk/sapi/media/folder?action=get&validationkey={key}", timeout=20)
            folders = r.json().get("data", {}).get("folders", [])
            ok = any(f.get("name") == "/" for f in folders)
            if ok: self._merge_cookies(sess)
            return ok
        except Exception:
            return False

    def _merge_cookies(self, sess):
        try:
            with open(self.state_file, encoding="utf-8") as f: data = json.load(f)
            changed = False
            for c in sess.cookies:
                for sc in data.get("cookies", []):
                    if sc.get("name") == c.name and sc.get("value") != c.value:
                        sc["value"] = c.value; changed = True
            if changed: atomic_write_json(self.state_file, data)
        except Exception:
            pass

    def refresh_session_browser(self):
        """Saved session ke saath site browser mein kholo (token/cookie refresh), valid ho to save."""
        if self.login_busy or not os.path.exists(self.state_file): return False
        tmp = self.state_file + ".refresh"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=BROWSER_ARGS)
                try:
                    ctx = browser.new_context(storage_state=self.state_file, user_agent=WEB_UA,
                                              viewport={"width": 1280, "height": 720}, locale="en-US")
                    ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in
                              ("image", "media", "font") else route.continue_())
                    page = ctx.new_page(); page.set_default_timeout(15000)
                    page.goto("https://cloud.jazzdrive.com.pk/", wait_until="domcontentloaded", timeout=45000)
                    time.sleep(8)
                    ctx.storage_state(path=tmp)
                finally:
                    browser.close()
            if session_valid(tmp):
                os.replace(tmp, self.state_file); return True
        except Exception as e:
            print(f"refresh_session_browser: {str(e)[:150]}", flush=True)
        try: os.remove(tmp)
        except OSError: pass
        return False

    def sync_cookie_json(self, force=False):
        """state -> cookie.json; badla ho (ya force) to GitHub par push. returns (ok, note) ya None."""
        fp = cookie_fingerprint(self.state_file)
        if not fp: return None
        if not force:
            if fp == self._cookie_fp: return None
            if time.time() - self._cookie_push_at < COOKIE_PUSH_MIN_GAP: return None
        tmp = COOKIE_JSON + ".tmp"
        shutil.copyfile(self.state_file, tmp); os.replace(tmp, COOKIE_JSON)
        self._cookie_fp = fp; self._cookie_push_at = time.time()
        return push_cookie_json()

    def after_login(self):
        res = self.sync_cookie_json(force=True)
        if not res: return
        ok, note = res
        if ok:
            self.msg("cookie.json GitHub par save ho gayi.\nAgla run bina OTP ke login ho jayega.")
        elif note == "public":
            self.msg("cookie.json GitHub par push NAHI ki: repo PUBLIC hai — cookie se koi bhi aap ka Jazz Drive "
                     "khol sakta hai.\nHal: cookie ke liye alag PRIVATE repo banao aur Secrets mein COOKIE_REPO "
                     "(user/repo) + COOKIE_TOKEN daalo. (Phir bhi public mein chahiye to ALLOW_PUBLIC_COOKIE=1 — apne risk par.)")
        else:
            self.msg(f"cookie.json is run mein save hui, GitHub push nahi hui ({note}).")

    def session_ping_loop(self):
        warned = False; last_browser = time.time(); push_warned = False
        while True:
            time.sleep(KEEPALIVE_PING_SEC)
            if self.login_busy: continue
            try:
                ok = self.ping_session()
                if not ok and self.refresh_session_browser():      # chup chaap session wapas zinda
                    ok = True; last_browser = time.time()
                if ok:
                    if warned: warned = False; self.msg("Jazz session wapas theek ho gayi.")
                    if time.time() - last_browser >= KEEPALIVE_BROWSER_SEC:
                        if self.refresh_session_browser(): last_browser = time.time()
                    res = self.sync_cookie_json()
                    if res and not res[0] and res[1] in ("public", "unknown") and not push_warned:
                        push_warned = True
                        self.msg("cookie.json GitHub par push nahi ho sakti (repo public ya check nahi hua). /cookie dekho.")
                elif not warned:
                    warned = True
                    self.msg("Session expire ho gayi — /login karo (number + OTP).")
            except Exception as e:
                print(f"ping loop: {str(e)[:150]}", flush=True)

    def _startup_session(self):
        try:
            if not session_valid(self.state_file) and restore_session_from_cookie_json(self.state_file):
                self.msg("Purani session cookie.json se mil gayi — OTP ki zaroorat nahi!")
        except Exception as e:
            print(f"startup session: {str(e)[:150]}", flush=True)

    # ─── LOGIN (Playwright async — Colab par test ho chuka, kaam karta hai) ─────

    async def _a_first_visible(self, page, selectors, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible(): return loc
                except Exception: pass
            await asyncio.sleep(0.3)
        return None

    async def _a_page_hint(self, page):
        try: txt = await page.inner_text("body", timeout=3000)
        except Exception: return ""
        keys = ("error", "invalid", "wrong", "limit", "try again", "blocked", "expired", "failed", "resend")
        hits = [l.strip() for l in txt.splitlines() if l.strip() and any(k in l.lower() for k in keys)]
        return " | ".join(hits[:4])[:300]

    async def _a_shot(self, page, caption=""):
        """Login ke har step ka screenshot Telegram par (LOGIN_SCREENSHOTS=False se band)."""
        if not LOGIN_SCREENSHOTS: return
        try:
            await page.screenshot(path="s.png")
            self.send_photo("s.png", caption)
            os.remove("s.png")
        except Exception: pass

    async def _a_click_get_otp(self, page, mode):
        """0 = click, 1 = JS click, 2 = Enter"""
        if mode == 2:
            await page.keyboard.press("Enter"); return True
        btn = await self._a_first_visible(page, ["button:has-text('Get OTP')", "text=Get OTP", "#signinbtn",
                                                 "button[type='submit']", "button:has-text('Continue')",
                                                 "button:has-text('Next')"], 4)
        if not btn: return False
        if mode == 0: await btn.click(timeout=3000)
        else: await btn.evaluate("e => e.click()")
        return True

    async def _a_filled_count(self, page):
        c = 0
        try:
            inputs = page.locator("input:visible")
            for i in range(min(await inputs.count(), 8)):
                try:
                    v = await inputs.nth(i).input_value(timeout=800)
                    if len(v) == 1 and v.isdigit(): c += 1
                except Exception: pass
        except Exception: pass
        return c

    async def _a_enter_otp(self, page, otp):
        for sel in ["//input[@aria-label='Digit 1']", "input[maxlength='1']",
                    "input[autocomplete='one-time-code']", "input[inputmode='numeric']"]:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=1500); break
            except Exception: pass
        try: await page.keyboard.type(otp, delay=40)
        except Exception: pass
        n = await self._a_filled_count(page)
        if n < len(otp):
            for i, d in enumerate(otp):
                try:
                    await page.locator("input[maxlength='1']").nth(i).click(timeout=1200)
                    await page.keyboard.type(d, delay=30)
                except Exception: pass
            n = await self._a_filled_count(page)
        for sel in ["button:has-text('Verify')", "button:has-text('Login')", "button:has-text('Log in')",
                    "button:has-text('Sign in')", "button:has-text('Submit')", "button:has-text('Confirm')"]:
            try:
                vb = page.locator(sel).first
                if await vb.count() and await vb.is_visible() and await vb.is_enabled():
                    await vb.click(timeout=1500); break
            except Exception: pass
        else:
            try: await page.keyboard.press("Enter")
            except Exception: pass
        return n

    async def _a_do_login(self):
        t0 = time.time()
        cap = {"key": None, "log": [], "bad": None, "num": None}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True,
                                              args=BROWSER_ARGS + ["--disable-blink-features=AutomationControlled"])
            try:
                ctx = await browser.new_context(viewport={"width": 1280, "height": 720},
                                                user_agent=WEB_UA, locale="en-US")
                await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

                async def _route(route):          # sirf media/font band (images chalne do — test mein yahi chala)
                    try:
                        if route.request.resource_type in ("media", "font"): await route.abort()
                        else: await route.continue_()
                    except Exception: pass
                await ctx.route("**/*", _route)

                page = await ctx.new_page()
                page.set_default_timeout(4000)

                async def on_response(resp):
                    try:
                        if "jazzdrive" not in resp.url: return
                        if resp.request.resource_type not in ("xhr", "fetch", "document"): return
                        path = urlparse(resp.url).path[:50]
                        tag = ""
                        if "json" in (resp.headers.get("content-type") or ""):
                            try:
                                found = scan_login_data(await resp.json())
                                if found.get("key"): cap["key"] = found["key"]; tag += " [key]"
                                if found.get("ticket"): tag += " [ticket]"
                            except Exception: pass
                        if resp.request.method == "POST" and "login/number" in resp.url: cap["num"] = resp.status
                        if resp.request.method == "POST" and "/auth/" in resp.url and resp.status >= 400:
                            cap["bad"] = f"{resp.status} {path}"
                        cap["log"].append(f"{resp.request.method} {resp.status} {path}{tag}")
                    except Exception: pass

                page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

                await page.goto("https://cloud.jazzdrive.com.pk/", wait_until="domcontentloaded", timeout=60000)

                # number box aane tak ruko (spinner khatam hone tak) — phir screenshot
                box = await self._a_first_visible(page, ["#msisdn", "input[name='msisdn']", "input[type='tel']",
                                                         "input[type='number']"], 60)
                await self._a_shot(page, "Login page" if box else "Number box nahi mila — login page badal gaya?")
                if not box:
                    self.ctx["state"] = "IDLE"; return False

                # 1) number (user ne page load hote waqt bhej diya ho to wo bhi chalega)
                for _ in range(1500):                                  # 300 sec
                    if self.ctx["state"] == "NUMBER_RECEIVED": break
                    await asyncio.sleep(0.2)
                else:
                    self.ctx["state"] = "IDLE"
                    self.msg("Timeout! Number nahi mila. /login dobara."); return False
                self.ctx["state"] = "SENDING_OTP"
                number = norm_number(self.ctx["number"])
                if len(number) != 11:
                    self.ctx["state"] = "IDLE"
                    self.msg("Number galat hai. 03XXXXXXXXX likh kar /login dobara."); return False

                try:
                    await box.click(timeout=3000); await box.fill("", timeout=3000)
                    await box.press_sequentially(number, delay=25)
                except Exception:
                    try: await box.fill(number, timeout=3000)
                    except Exception: pass
                await asyncio.sleep(0.5)

                # 2) Get OTP — asli request (login/number POST) ka intezar
                sent = False
                for mode in (0, 1, 2):
                    cap["num"] = None
                    try:
                        if not await self._a_click_get_otp(page, mode): continue
                    except Exception: pass
                    end = time.time() + 12
                    while time.time() < end and cap["num"] is None: await asyncio.sleep(0.2)
                    if cap["num"] is not None: sent = True; break
                await asyncio.sleep(1)
                await self._a_shot(page, "Get OTP ke baad")
                hint = await self._a_page_hint(page)
                extra = ("\n\nPage: " + hint) if hint else ""
                if sent and cap["num"] != 200:
                    self.ctx["state"] = "IDLE"
                    self.msg(f"Jazz ne OTP request mana kar di (status {cap['num']}). Shayad bohot baar OTP mangwa liya — "
                             "10-15 minute ruk kar /login karo." + extra)
                    return False
                if sent:
                    self.msg(f"OTP SMS bhej diya. {OTP_VALID_SEC} sec ke andar OTP bhejo:" + extra)
                else:
                    dbg = "\n".join(cap["log"][-5:]) or "(koi request nahi gayi)"
                    self.msg("Get OTP ki request nahi gayi, SMS shayad nahi aayega. SMS aa jaye to OTP bhej do, "
                             "warna 10 minute baad /login.\n\nDebug:\n" + dbg + extra)
                self.ctx["state"] = "OTP_RECEIVED" if (self.ctx.get("otp_early") and self.ctx.get("otp")) else "WAITING_FOR_OTP"

                # 3) OTP ka intezar (120 sec)
                for _ in range(600):
                    if self.ctx["state"] == "OTP_RECEIVED": break
                    await asyncio.sleep(0.2)
                else:
                    self.ctx["state"] = "IDLE"
                    self.msg("Timeout! OTP nahi mila. /login dobara."); return False

                # 4) OTP fauran daalo
                otp = re.sub(r"\D", "", self.ctx["otp"] or "")[:6]
                t_in = time.time()
                filled = await self._a_enter_otp(page, otp)
                await self._a_shot(page, "OTP daalne ke baad")

                # 5) validationKey ka intezar (45 sec)
                key = None
                deadline = time.time() + 45
                while time.time() < deadline:
                    await asyncio.sleep(1)
                    key = cap["key"]
                    if not key:
                        try: key = {c["name"].lower(): c["value"] for c in await ctx.cookies()}.get("validationkey")
                        except Exception: key = None
                    if not key:
                        try:
                            h = await page.wait_for_function(
                                "() => { for (const k of Object.keys(localStorage)) "
                                "if (k.toLowerCase() === 'validationkey') return localStorage.getItem(k); return false; }",
                                timeout=1500)
                            key = await h.json_value() or None
                        except Exception: key = None
                    if key or cap["bad"]: break

                ok = False
                if key:
                    try: await ctx.storage_state(path=self.state_file)
                    except Exception as e: print(f"storage_state: {e}", flush=True)
                    if not load_cookies(self.state_file)[1]:
                        try:
                            await ctx.add_cookies([{"name": "validationKey", "value": key,
                                                    "url": "https://cloud.jazzdrive.com.pk"}])
                            await ctx.storage_state(path=self.state_file)
                        except Exception: pass
                    for _ in range(3):
                        if session_valid(self.state_file): ok = True; break
                        await asyncio.sleep(1.5)

                self.ctx["state"] = "IDLE"
                if ok:
                    await self._a_shot(page, "Login ke baad")
                    self.msg(f"LOGIN SUCCESSFUL! ({int(time.time() - t0)} sec, OTP daalne mein {int(time.time() - t_in)} sec)\n"
                             "Session save!\nLink bhejein")
                    threading.Thread(target=self.after_login, daemon=True).start()
                    return True

                await self._a_shot(page, "Login fail")
                dbg = "\n".join(cap["log"][-8:]) or "(koi request capture nahi hui)"
                hint = await self._a_page_hint(page)
                why = f"Server ne error diya: {cap['bad']}\n" if cap["bad"] else ""
                self.msg("LOGIN FAIL!\nOTP galat ya expire (OTP ~60 sec mein expire hota hai) ya login flow badla.\n"
                         f"{why}OTP boxes bhare: {filled}/{len(otp)}\n/login se dobara try karo.\n\nDebug:\n" + dbg
                         + (("\n\nPage: " + hint) if hint else ""))
                return False
            finally:
                try: await browser.close()
                except Exception: pass

    def check_login_status(self, force=False):
        if self.login_busy:
            self.msg("Login pehle se chal raha hai — number/OTP bhejo, ya thori der baad /login."); return
        self.login_busy = True
        try:
            self.msg("Jazz Drive login check...")
            if not force and session_valid(self.state_file):
                self.msg("LOGIN VALID!\nLink bhejein!")
                threading.Thread(target=self.sync_cookie_json, daemon=True).start()
                return
            if not force and restore_session_from_cookie_json(self.state_file):
                self.msg("Session cookie.json se wapas mil gayi — OTP ki zaroorat nahi!\nLink bhejein!"); return
            self.ctx.update({"number": None, "otp": None, "otp_early": False, "state": "WAITING_FOR_NUMBER"})
            self.msg("Session nahi / expire!\nJazz number bhejo (03XXXXXXXXX) — login page load ho raha hai, jaldi bhejo")
            try:
                asyncio.run(self._a_do_login())      # ye thread alag hai, is liye asyncio.run theek hai
            except Exception as e:
                self.msg(f"Login error: {str(e)[:150]}")
        finally:
            if self.ctx.get("state") in ("WAITING_FOR_NUMBER", "NUMBER_RECEIVED", "SENDING_OTP",
                                         "WAITING_FOR_OTP", "OTP_RECEIVED"):
                self.ctx["state"] = "IDLE"
            self.login_busy = False

    # ─── Download ─────────────────────────────────────────────

    def download_file(self, url, out_path, task_id=None):
        last_error = "Unknown"
        clean(out_path)
        referers = get_referers(url)

        if is_m3u8(url):
            if not out_path.endswith('.mp4'):
                out_path = out_path.rsplit('.', 1)[0] + '.mp4'

            # ffmpeg auto-install
            os.system("apt-get install -y ffmpeg > /dev/null 2>&1")

            # surrit.com ya kisi bhi M3U8 ke liye referers
            m3u8_referers = [
                urlparse(url).scheme + "://" + urlparse(url).netloc + "/",
                "https://www.google.com/",
                "",
            ]

            for ref in m3u8_referers:
                if self.is_cancelled(task_id): return None, "Cancelled"
                clean(out_path)
                try:
                    headers_str = f"User-Agent: {WEB_UA}\r\n"
                    if ref:
                        headers_str += f"Referer: {ref}\r\nOrigin: {ref.rstrip('/')}\r\n"
                    cmd = [
                        "ffmpeg", "-y",
                        "-headers", headers_str,
                        "-i", url,
                        "-c", "copy",
                        "-bsf:a", "aac_adtstoasc",
                        "-movflags", "+faststart",
                        out_path
                    ]
                    result = subprocess.run(cmd, capture_output=True, timeout=3600)
                    if file_ok(out_path, min_mb=0.1): return out_path, "Success"
                    last_error = result.stderr.decode()[-200:] if result.stderr else "ffmpeg fail"
                except Exception as e:
                    last_error = str(e)

            # yt-dlp fallback with impersonation (Cloudflare bypass)
            try:
                import yt_dlp
                clean(out_path)
                base_out = out_path.rsplit(".", 1)[0]
                for impersonate in ["chrome", "safari", None]:
                    clean(out_path)
                    try:
                        ydl_opts = {
                            "outtmpl": base_out + ".%(ext)s",
                            "quiet": True, "no_warnings": True,
                            "format": "best",
                            "http_headers": {
                                "User-Agent": WEB_UA,
                                "Referer": urlparse(url).scheme + "://" + urlparse(url).netloc + "/",
                            },
                            "merge_output_format": "mp4",
                        }
                        if impersonate:
                            ydl_opts["impersonate"] = impersonate
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([url])
                        for ext in [".mp4", ".mkv", ".ts", ".webm"]:
                            if file_ok(base_out + ext, min_mb=0.1):
                                return base_out + ext, "Success"
                        if file_ok(out_path, min_mb=0.1):
                            return out_path, "Success"
                    except Exception as e:
                        last_error = f"yt-dlp({impersonate}): {str(e)[:100]}"
            except Exception as e:
                last_error = f"yt-dlp m3u8: {str(e)[:150]}"

            # curl_cffi fallback — Cloudflare bypass
            try:
                os.system("pip install -q curl_cffi > /dev/null 2>&1")
                from curl_cffi import requests as cf_requests
                clean(out_path)
                # M3U8 playlist fetch
                cf_sess = cf_requests.Session(impersonate="chrome110")
                r = cf_sess.get(url, timeout=30)
                r.raise_for_status()
                m3u8_content = r.text
                base_url = url.rsplit("/", 1)[0] + "/"
                # Parse segments
                segments = [line.strip() for line in m3u8_content.splitlines()
                            if line.strip() and not line.startswith("#")]
                if segments:
                    self.msg(f"M3U8 segments: {len(segments)} — downloading...")
                    ts_path = out_path.replace(".mp4", ".ts")
                    with open(ts_path, "wb") as out_f:
                        for seg in segments:
                            seg_url = seg if seg.startswith("http") else base_url + seg
                            try:
                                seg_r = cf_sess.get(seg_url, timeout=60)
                                out_f.write(seg_r.content)
                            except: pass
                    # Convert ts to mp4
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", ts_path, "-c", "copy", out_path],
                        capture_output=True, timeout=600
                    )
                    clean(ts_path)
                    if file_ok(out_path, min_mb=0.1):
                        return out_path, "Success"
            except Exception as e:
                last_error = f"curl_cffi: {str(e)[:150]}"

            return None, f"M3U8 fail: {last_error}"

        try:
            import yt_dlp
            tmp = out_path.rsplit('.', 1)[0] + '.%(ext)s'
            opts = {
                "outtmpl": tmp, "quiet": True, "no_warnings": True,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "http_headers": {"User-Agent": WEB_UA, "Referer": referers[0]},
                "socket_timeout": 30,
            }
            with yt_dlp.YoutubeDL(opts) as ydl: ydl.download([url])
            base = out_path.rsplit('.', 1)[0]
            for ext in VIDEO_EXTS:
                if file_ok(base + ext, min_mb=0.1): return base + ext, "Success"
            if file_ok(out_path, min_mb=0.1): return out_path, "Success"
        except Exception as e: last_error = f"yt-dlp: {str(e)[:100]}"

        for ref in referers:
            if self.is_cancelled(task_id): return None, "Cancelled"
            clean(out_path)
            try:
                cmd = ["aria2c", "-x", "16", "-s", "16", "-k", "1M",
                       "--max-tries=3", "--retry-wait=5", "--allow-overwrite=true",
                       f"--user-agent={WEB_UA}",
                       "-d", os.path.dirname(out_path) or "/tmp",
                       "-o", os.path.basename(out_path)]
                if ref: cmd += [f"--referer={ref}", f"--header=Origin: {ref.rstrip('/')}"]
                cmd.append(url)
                r = subprocess.run(cmd, capture_output=True, timeout=600)
                if file_ok(out_path, min_mb=0.1): return out_path, "Success"
                last_error = "aria2c: " + r.stderr.decode()[:100]
            except Exception as e: last_error = f"aria2c: {str(e)[:100]}"

        for ref in referers:
            if self.is_cancelled(task_id): return None, "Cancelled"
            clean(out_path)
            try:
                cmd = ["curl", "-L", "-k", "--retry", "3", "--retry-delay", "3",
                       "--connect-timeout", "30", "-H", f"User-Agent: {WEB_UA}", "-o", out_path]
                if ref: cmd += ["-H", f"Referer: {ref}", "-H", f"Origin: {ref.rstrip('/')}"]
                cmd.append(url)
                subprocess.run(cmd, timeout=600)
                if file_ok(out_path, min_mb=0.1): return out_path, "Success"
            except Exception as e: last_error = f"curl: {str(e)[:100]}"

        for ref in referers:
            if self.is_cancelled(task_id): return None, "Cancelled"
            clean(out_path)
            try:
                hdrs = {"User-Agent": WEB_UA}
                if ref: hdrs["Referer"] = ref; hdrs["Origin"] = ref.rstrip("/")
                with requests.get(url, headers=hdrs, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(out_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk: f.write(chunk)
                if file_ok(out_path, min_mb=0.1): return out_path, "Success"
            except Exception as e: last_error = f"requests: {str(e)[:100]}"

        return None, last_error

    # ─── Split ────────────────────────────────────────────────

    def split_video(self, filepath):
        import glob
        size_mb = os.path.getsize(filepath) / (1024*1024)
        if size_mb <= MAX_SIZE_MB: return [filepath]
        self.msg(f"File {size_mb:.0f}MB — 7zip parts bana raha hoon...")
        os.system("apt-get install -y p7zip-full > /dev/null 2>&1")
        base = filepath.rsplit(".", 1)[0]
        archive_base = f"{base}.7z"
        for old_part in glob.glob(f"{archive_base}.*"):
            try: os.remove(old_part)
            except: pass
        subprocess.run(
            ["7z", "a", f"-v{MAX_SIZE_MB}m", archive_base, filepath],
            capture_output=True, timeout=7200
        )
        parts = sorted(glob.glob(f"{archive_base}.*"))
        if parts:
            clean(filepath)
            self.msg(f"{len(parts)} parts bane! Upload shuru...")
            return parts
        self.msg("7zip fail — original file upload karega...")
        return [filepath]

    # ─── Upload via API (no Playwright) ──────────────────────

    def upload_file_api(self, local_path, filename, folder_id, task_id=None):
        cookies, key = load_cookies(self.state_file)
        if not cookies or not key:
            self.msg("Session nahi hai — /checklogin karo"); return None

        fname = safe_filename(filename) or os.path.basename(local_path)
        fsize = os.path.getsize(local_path)
        self.msg(f"Uploading {fname[:50]}...\n{fmt_bytes(fsize)}")

        # ── Har 1 minute progress update ──
        upload_done   = threading.Event()
        upload_start  = time.time()

        def jazzdrive_screenshot(minute):
            """JazzDrive website ka live screenshot lo"""
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True, args=BROWSER_ARGS)
                    ctx = browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        storage_state=self.state_file if os.path.exists(self.state_file) else None
                    )
                    page = ctx.new_page()
                    page.goto("https://cloud.jazzdrive.com.pk/#folders",
                              wait_until="networkidle", timeout=30000)
                    import time as t; t.sleep(3)
                    path = f"/tmp/jd_shot_{minute}.png"
                    page.screenshot(path=path, full_page=False)
                    browser.close()
                    return path
            except:
                return None

        def progress_reporter():
            minute = 1
            while not upload_done.wait(timeout=60):
                shot = jazzdrive_screenshot(minute)
                if shot:
                    self.send_photo(shot, f"Upload progress — {minute} min\n{fname[:50]}")
                    try: os.remove(shot)
                    except: pass
                else:
                    self.msg(f"Uploading... {minute} min\n{fname[:50]}")
                minute += 1

        reporter_thread = threading.Thread(target=progress_reporter, daemon=True)
        reporter_thread.start()

        try:
            success, file_id = api_upload_file(
                local_path, fname, folder_id, cookies, key,
                cancelled_flag=lambda: self.is_cancelled(task_id)
            )
        except Exception as e:
            upload_done.set()
            self.msg(f"Upload error: {str(e)[:150]}"); return None
        finally:
            upload_done.set()

        if not success or not file_id:
            self.msg("Upload fail!"); return None

        elapsed_total = int(time.time() - upload_start)
        self.msg(f"Upload complete! ({elapsed_total}s)\n{fmt_bytes(fsize)}")

        # Wait for indexing
        wait = get_index_wait(fsize)
        self.msg(f"Indexing... ({wait}s)")
        time.sleep(wait)

        # Generate share link
        link = api_generate_share_link(file_id, False, cookies, key)
        return link

    def upload_with_split(self, filepath, folder_id, task_id=None):
        if self.is_cancelled(task_id): return []
        parts = self.split_video(filepath)
        links = []
        for i, part in enumerate(parts, 1):
            if self.is_cancelled(task_id): clean(part); break
            if len(parts) > 1: self.msg(f"Part {i}/{len(parts)} upload...")
            fname = os.path.basename(part)
            link = self.upload_file_api(part, fname, folder_id, task_id)
            if link: links.append(link)
            clean(part)
        return links

    def get_folder_id(self, folder_name):
        """folder_name se ID dhundo — nahi mila to root ID lo"""
        cookies, key = load_cookies(self.state_file)
        if not cookies or not key: return None, None
        folders, root_id = api_get_folders(cookies, key)
        if not root_id: return None, None
        if not folder_name or folder_name.upper() == "ROOT":
            return root_id, cookies, key
        # Case-insensitive match
        for name, fid in folders:
            if name.strip().lower() == folder_name.strip().lower():
                return fid, cookies, key
        # Nahi mila — root mein upload
        self.msg(f"Folder '{folder_name}' nahi mila — root mein upload")
        return root_id, cookies, key

    # ─── Processors ───────────────────────────────────────────

    def process_direct(self, url, filename, folder_name="", task_id=None):
        # YouTube — pytubefix (TV-code) + yt-dlp (latest)
        if any(x in url for x in ["youtube.com/watch", "youtu.be/", "youtube.com/shorts"]):
            self.msg("YouTube link!\npytubefix/yt-dlp se download ho raha hai...")
            result = download_youtube_native(url, quality="1080")
            if not result:
                self.msg("YouTube download fail!")
                return

            sz = os.path.getsize(result) / (1024*1024)
            self.msg(f"Downloaded! {sz:.1f} MB")

            result_folder = self.get_folder_id(folder_name)
            folder_id = result_folder[0] if result_folder else None
            if not folder_id:
                self.msg("Session problem — /checklogin karo"); clean(result); return

            links = self.upload_with_split(result, folder_id, task_id)
            if links: self.msg(f"Upload Done!\n\nShare Link:\n{links[0]}")
            else: self.msg("Upload Done!\n(Share link nahi mila)")
            return

        fname    = filename or get_filename_from_url(url)
        out_path = f"/tmp/{safe_filename(fname)}"
        clean(out_path)
        self.msg(f"Downloading...\n{fname[:60]}")

        result, err = self.download_file(url, out_path, task_id)
        if not result: self.msg(f"Download fail!\n{err[:200]}"); return

        sz = os.path.getsize(result) / (1024*1024)
        self.msg(f"Downloaded! {sz:.1f} MB")

        result_folder = self.get_folder_id(folder_name)
        folder_id = result_folder[0] if result_folder else None
        if not folder_id: self.msg("Session problem — /checklogin karo"); clean(out_path); return

        links = self.upload_with_split(result, folder_id, task_id)
        if links: self.msg(f"Upload Done!\n\nShare Link:\n{links[0]}")
        else: self.msg("Upload Done!\n(Share link nahi mila)")

    def process_zip(self, url, folder_name="", task_id=None):
        import shutil
        zip_path    = f"/tmp/series_{self.chat_id}.zip"
        extract_dir = f"/tmp/series_{self.chat_id}_extracted"
        clean(zip_path)
        if os.path.exists(extract_dir): shutil.rmtree(extract_dir)
        os.makedirs(extract_dir, exist_ok=True)

        self.msg("ZIP/Season download ho raha hai...")
        result, err = self.download_file(url, zip_path, task_id)
        if not result or not file_ok(zip_path):
            self.msg(f"ZIP fail!\n{err[:200]}"); return

        sz = os.path.getsize(zip_path) / (1024*1024)
        self.msg(f"Downloaded! {sz:.1f} MB\nExtracting...")

        try:
            if zipfile.is_zipfile(zip_path):
                with zipfile.ZipFile(zip_path, "r") as zf: zf.extractall(extract_dir)
            else: subprocess.run(["unzip", "-o", zip_path, "-d", extract_dir], timeout=120)
        except Exception as e:
            try: subprocess.run(["7z", "x", zip_path, f"-o{extract_dir}", "-y"], timeout=120)
            except: self.msg(f"Extract fail: {str(e)[:100]}"); return
        clean(zip_path)

        video_files = []
        for root, dirs, files in os.walk(extract_dir):
            for f in sorted(files):
                if is_video_file(f): video_files.append(os.path.join(root, f))

        if not video_files: self.msg("ZIP mein koi video nahi mili!"); return
        self.msg(f"Total {len(video_files)} Episodes!\nUpload shuru...")

        # Get/create folder
        cookies, key = load_cookies(self.state_file)
        if not cookies or not key: self.msg("Session problem!"); return
        folders, root_id = api_get_folders(cookies, key)
        if not root_id: self.msg("Folder list fail!"); return

        if folder_name and folder_name.upper() != "ROOT":
            match = next((fid for name, fid in folders if name.lower() == folder_name.lower()), None)
            target_id = match if match else api_create_folder(folder_name, root_id, cookies, key)
            if not match: self.msg(f"Folder '{folder_name}' nahi mila — create kiya")
        else:
            target_id = root_id

        all_links = []
        for i, vp in enumerate(video_files, 1):
            if self.is_cancelled(task_id): self.msg("Season cancelled!"); break
            fname = os.path.basename(vp)
            fsize = os.path.getsize(vp) / (1024*1024)
            self.msg(f"Episode {i}/{len(video_files)}\n{fname}\n{fsize:.1f} MB")
            links = self.upload_with_split(vp, target_id, task_id)
            if links: all_links.append(f"Ep {i}: {links[0]}"); self.msg(f"Ep {i} Done!\n{links[0]}")
            else: all_links.append(f"Ep {i}: Uploaded (No Link)"); self.msg(f"Ep {i} Done!")

        shutil.rmtree(extract_dir, ignore_errors=True)

        # Folder share link
        folder_link = api_generate_share_link(target_id, True, cookies, key)
        report = f"SEASON COMPLETE!\nTotal {len(all_links)} episodes.\n\n"
        report += "\n".join(all_links)
        if folder_link: report += f"\n\nFolder Link:\n{folder_link}"
        self.msg(report)

    def process_social(self, url, task_id=None):
        """Instagram/TikTok — individual upload (no zip), phir feed.json mein add"""
        platform = "Instagram" if "instagram.com" in url else "TikTok"
        self.msg(f"{platform} link mila!\nDownload ho raha hai...")

        cookies_file = IG_COOKIES if platform == "Instagram" else None
        local_path, title = download_social(url, cookies_file)
        if not local_path or not file_ok(local_path, min_mb=0.05):
            if platform == "Instagram" and not os.path.exists(IG_COOKIES):
                self.msg("Instagram download fail!\nCookies file (instagram_cookies.txt) nahi mili — /setigcookies se pehle bhejein.")
            else:
                self.msg(f"{platform} download fail!")
            return

        sz = os.path.getsize(local_path) / (1024*1024)
        self.msg(f"Downloaded! {sz:.1f} MB\nUploading...")

        cookies, key = load_cookies(self.state_file)
        if not cookies or not key:
            self.msg("Session problem — /checklogin karo"); clean(local_path); return
        folders, root_id = api_get_folders(cookies, key)
        if not root_id:
            self.msg("Folder list fail!"); clean(local_path); return
        match = next((fid for name, fid in folders if name.lower() == SOCIAL_FOLDER.lower()), None)
        target_id = match if match else api_create_folder(SOCIAL_FOLDER, root_id, cookies, key)

        links = self.upload_with_split(local_path, target_id, task_id)
        if not links:
            self.msg("Upload fail!"); return

        append_feed({
            "title": (title or "Video")[:100],
            "url": links[0],
            "platform": platform,
            "added": datetime.now().isoformat(),
        })
        git_push_feed()
        self.msg(f"{platform} video feed mein add ho gaya!\n\n{links[0]}")

    # ─── Worker ───────────────────────────────────────────────

    def worker_loop(self):
        try:
            while not self.task_queue.empty():
                while self.queue_paused: time.sleep(5)
                item    = self.task_queue.get()
                task_id = item.get("task_id")
                if self.is_cancelled(task_id): self.task_queue.task_done(); continue
                self.msg(f"PROCESSING...\n{item.get('link','')[:80]}")
                try:
                    folder = item.get("folder", "")
                    fname  = item.get("filename", "")
                    if item["type"] == "zip": self.process_zip(item["link"], folder, task_id)
                    elif item["type"] == "social": self.process_social(item["link"], task_id)
                    else: self.process_direct(item["link"], fname, folder, task_id)
                except Exception as e: self.msg(f"Error: {str(e)[:150]}")
                finally: self.task_queue.task_done()
            self.msg("QUEUE COMPLETE!\n\nAgla link bhejein")
        except Exception as e: self.msg(f"Worker crash: {str(e)[:150]}")
        finally:
            with self.worker_lock: self.is_working = False

    def start_worker(self):
        with self.worker_lock:
            if not self.is_working:
                self.is_working = True
                threading.Thread(target=self.worker_loop, daemon=True).start()

    # ─── Handlers ─────────────────────────────────────────────

    def register_handlers(self):
        bot = self.bot

        @bot.message_handler(commands=["start"])
        def welcome(m):
            if m.chat.id not in self.allowed: return
            self.msg(
                "JAZZ DRIVE BOT\n\n"
                "Commands:\n"
                "/link url - FileName - .ext\n"
                "  Single file download & upload\n\n"
                "/mlink\n"
                "  Batch links (ek line mein ek)\n"
                "  Format: url - Name - .ext\n\n"
                "/zip <url>\n"
                "  Season ZIP extract & upload\n\n"
                "/ziplink url - FolderName\n"
                "  ZIP seedha folder mein\n\n"
                "/cancel <task_id>\n"
                "/cancelall\n"
                "/checklogin\n"
                "/login (force naya login)\n"
                "/cookie (cookie.json GitHub par save)\n"
                "/shot (Jazz Drive ka live screenshot)\n"
                "/status\n"
                "/pause  /resume  /clear\n"
                "/allow <id>  /disallow <id>  (Admin)\n"
                "/cmd <bash>"
            )

        @bot.message_handler(commands=["link"])
        def cmd_link(m):
            if m.chat.id not in self.allowed: return
            text = m.text.replace("/link", "", 1).strip()
            if not text or " - " not in text:
                bot.reply_to(m, "Format:\n/link https://url - FileName - .mkv"); return
            try:
                parts = text.split(" - ")
                filename = f"{parts[1].strip()}{parts[2].strip()}"
                self.ctx["pending_link"] = parts[0].strip()
                self.ctx["pending_type"] = "direct"
                self.ctx["pending_name"] = filename
                self.ctx["state"] = "WAITING_FOR_FOLDER"
                bot.reply_to(m, f"Link mila: {filename}\n\nFolder name bhejein\n(ya 'root')")
            except:
                bot.reply_to(m, "Format:\n/link https://... - Episode 1 - .mkv")

        @bot.message_handler(commands=["mlink"])
        def cmd_mlink(m):
            if m.chat.id not in self.allowed: return
            text = m.text.replace("/mlink", "", 1).strip()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            links = []
            for line in lines:
                if " - " in line:
                    try:
                        p = line.split(" - ")
                        links.append({"url": p[0].strip(), "filename": f"{p[1].strip()}{p[2].strip()}"})
                    except: pass
            if not links:
                bot.reply_to(m, "Format (ek line mein ek):\nurl - Name - .ext"); return
            self.ctx["pending_links"] = links
            self.ctx["pending_type"]  = "mlink"
            self.ctx["state"]         = "WAITING_FOR_FOLDER"
            bot.reply_to(m, f"{len(links)} links mili!\n\nFolder name bhejein\n(ya 'root')")

        @bot.message_handler(commands=["zip"])
        def cmd_zip(m):
            if m.chat.id not in self.allowed: return
            text = m.text.replace("/zip", "", 1).strip()
            if text.startswith("http"):
                self.ctx["pending_link"] = text
                self.ctx["pending_type"] = "zip"
                self.ctx["pending_name"] = ""
                self.ctx["state"]        = "WAITING_FOR_FOLDER"
                bot.reply_to(m, "Season ZIP link mila!\n\nFolder name bhejein\n(ya 'root')")
            else:
                bot.reply_to(m, "Format:\n/zip http://example.com/season.zip")

        @bot.message_handler(commands=["ziplink"])
        def cmd_ziplink(m):
            if m.chat.id not in self.allowed: return
            text = m.text.replace("/ziplink", "", 1).strip()
            if not text or " - " not in text:
                bot.reply_to(m, "Format:\n/ziplink https://url - FolderName"); return
            try:
                parts = text.split(" - ", 1)
                folder = parts[1].strip()
                tid = self.next_task_id()
                self.task_queue.put({"link": parts[0].strip(), "type": "zip",
                                     "folder": folder, "filename": "", "task_id": tid})
                bot.reply_to(m, f"ZIP task added!\nFolder: {folder}\nTask ID: {tid}\nQueue: {self.task_queue.qsize()}")
                self.start_worker()
            except:
                bot.reply_to(m, "Format:\n/ziplink https://... - FolderName")

        @bot.message_handler(commands=["cancel"])
        def cmd_cancel(m):
            if m.chat.id not in self.allowed: return
            parts = m.text.split()
            if len(parts) < 2: bot.reply_to(m, "Format: /cancel task_id"); return
            self.cancelled.add(parts[1])
            bot.reply_to(m, f"Cancel signal sent: {parts[1]}")

        @bot.message_handler(commands=["cancelall"])
        def cmd_cancelall(m):
            if m.chat.id not in self.allowed: return
            self.cancelled.add(f"all_{self.chat_id}")
            bot.reply_to(m, "Sab tasks cancel ho jayenge!")

        @bot.message_handler(commands=["checklogin"])
        def cmd_check(m):
            if m.chat.id != self.chat_id: return
            threading.Thread(target=self.check_login_status, daemon=True).start()

        @bot.message_handler(commands=["login"])
        def cmd_login(m):
            if m.chat.id != self.chat_id: return
            threading.Thread(target=self.check_login_status, kwargs={"force": True}, daemon=True).start()

        @bot.message_handler(commands=["cookie"])
        def cmd_cookie(m):
            if m.chat.id != self.chat_id: return
            def work():
                if not session_valid(self.state_file):
                    self.msg("Pehle login karo (/login)."); return
                self.after_login()
            threading.Thread(target=work, daemon=True).start()

        @bot.message_handler(commands=["shot"])
        def cmd_shot(m):
            if m.chat.id != self.chat_id: return
            def work():
                if not os.path.exists(self.state_file):
                    self.msg("Session nahi hai — /login karo."); return
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True, args=BROWSER_ARGS)
                        try:
                            ctx = browser.new_context(storage_state=self.state_file, user_agent=WEB_UA,
                                                      viewport={"width": 1280, "height": 720}, locale="en-US")
                            page = ctx.new_page(); page.set_default_timeout(15000)
                            page.goto("https://cloud.jazzdrive.com.pk/", wait_until="domcontentloaded", timeout=45000)
                            time.sleep(8)
                            self.take_screenshot(page, "Jazz Drive — live")
                        finally:
                            browser.close()
                except Exception as e:
                    self.msg(f"Screenshot fail: {str(e)[:150]}")
            threading.Thread(target=work, daemon=True).start()

        @bot.message_handler(commands=["status"])
        def cmd_status(m):
            if m.chat.id not in self.allowed: return
            cookies, key = load_cookies(self.state_file)
            session_ok = "Active" if session_valid(self.state_file) else "Expired/None"
            self.msg(
                f"BOT STATUS\n\n"
                f"State: {'Working' if self.is_working else 'Idle'}\n"
                f"Queue: {self.task_queue.qsize()}\n"
                f"Paused: {'YES' if self.queue_paused else 'No'}\n"
                f"Session: {session_ok}"
            )

        @bot.message_handler(commands=["pause"])
        def cmd_pause(m):
            if m.chat.id not in self.allowed: return
            self.queue_paused = True; self.msg("Queue paused!")

        @bot.message_handler(commands=["resume"])
        def cmd_resume(m):
            if m.chat.id not in self.allowed: return
            self.queue_paused = False; self.msg("Queue resumed!")
            self.start_worker()

        @bot.message_handler(commands=["clear"])
        def cmd_clear(m):
            if m.chat.id not in self.allowed: return
            count = self.task_queue.qsize()
            while not self.task_queue.empty():
                try: self.task_queue.get_nowait()
                except: break
            self.msg(f"Queue cleared! {count} tasks remove.")

        @bot.message_handler(commands=["allow"])
        def cmd_allow(m):
            if m.chat.id != self.chat_id: return
            parts = m.text.split()
            if len(parts) < 2: bot.reply_to(m, "Format: /allow user_id"); return
            try:
                uid = int(parts[1]); self.allowed.add(uid); save_allowed(self.allowed)
                bot.reply_to(m, f"User {uid} authorized.")
            except: bot.reply_to(m, "Invalid user_id")

        @bot.message_handler(commands=["disallow"])
        def cmd_disallow(m):
            if m.chat.id != self.chat_id: return
            parts = m.text.split()
            if len(parts) < 2: bot.reply_to(m, "Format: /disallow user_id"); return
            try:
                uid = int(parts[1])
                if uid == self.chat_id: bot.reply_to(m, "Apne aap ko nahi hata sakte!"); return
                self.allowed.discard(uid); save_allowed(self.allowed)
                bot.reply_to(m, f"User {uid} removed.")
            except: bot.reply_to(m, "Invalid user_id")


        @bot.message_handler(commands=["info"])
        def cmd_info(m):
            if m.chat.id not in self.allowed:
                return
            query = m.text.replace("/info", "", 1).strip()
            if not query:
                bot.reply_to(m, "Format:\n/info Avatar\n/info Breaking Bad")
                return
            bot.reply_to(m, "Searching: " + query + "...")
            text, poster_url = tmdb_info(query)
            if not text:
                bot.reply_to(m, "Koi result nahi mila!")
                return
            try:
                if poster_url:
                    img = requests.get(poster_url, timeout=10).content
                    bot.send_photo(m.chat.id, img, caption=text)
                else:
                    bot.send_message(m.chat.id, text)
            except Exception as e:
                bot.reply_to(m, text)

        @bot.message_handler(commands=["setigcookies"])
        def cmd_setigcookies(m):
            if m.chat.id not in self.allowed: return
            bot.reply_to(m, f"Ab {IG_COOKIES} file (Netscape cookies.txt format) document ke roop mein bhejein.")

        @bot.message_handler(content_types=["document"])
        def handle_doc(m):
            if m.chat.id not in self.allowed: return
            fname = (m.document.file_name or "").lower()
            if "cookie" not in fname:
                bot.reply_to(m, "Ye cookies file jaisi nahi lagi. Filename mein 'cookie' hona chahiye."); return
            try:
                info = bot.get_file(m.document.file_id)
                data = bot.download_file(info.file_path)
                with open(IG_COOKIES, "wb") as f: f.write(data)
                bot.reply_to(m, f"Instagram cookies save ho gaye: {IG_COOKIES}")
            except Exception as e:
                bot.reply_to(m, f"Cookies save fail: {str(e)[:150]}")

        @bot.message_handler(commands=["cmd"])
        def cmd_shell(m):
            if m.chat.id != self.chat_id: return
            try:
                c = m.text.replace("/cmd ", "", 1).strip()
                out = subprocess.check_output(c, shell=True, stderr=subprocess.STDOUT).decode()
                bot.reply_to(m, out[:4000])
            except Exception as e: bot.reply_to(m, f"Error: {e}")

        @bot.message_handler(func=lambda m: True)
        def handle(m):
            if m.chat.id not in self.allowed: return
            text = (m.text or "").strip()

            if self.ctx["state"] == "WAITING_FOR_NUMBER":
                self.ctx["number"] = text; self.ctx["state"] = "NUMBER_RECEIVED"
                bot.reply_to(m, "Number receive hua..."); return

            # OTP jaldi aa gaya (bot abhi Get OTP dabane mein hai) — save kar lo, page ready hote hi daal dunga
            if self.ctx["state"] in ("NUMBER_RECEIVED", "SENDING_OTP") and re.fullmatch(r"\d{4,6}", text):
                self.ctx["otp"] = text; self.ctx["otp_early"] = True
                bot.reply_to(m, "OTP receive hua..."); return

            if self.ctx["state"] == "WAITING_FOR_OTP":
                self.ctx["otp"] = text; self.ctx["state"] = "OTP_RECEIVED"
                bot.reply_to(m, "OTP receive hua..."); return

            if self.ctx["state"] == "WAITING_FOR_FOLDER":
                folder = "" if text.strip().upper() in ("ROOT", "") else text.strip()
                if self.ctx["pending_type"] == "mlink":
                    for item in self.ctx["pending_links"]:
                        tid = self.next_task_id()
                        self.task_queue.put({"link": item["url"], "type": "direct",
                                             "filename": item["filename"], "folder": folder, "task_id": tid})
                    count = len(self.ctx["pending_links"])
                    self.ctx.update({"pending_links": None, "pending_type": None, "state": "IDLE"})
                    bot.reply_to(m, f"{count} tasks added!\nFolder: {folder or 'Root'}\nQueue: {self.task_queue.qsize()}")
                else:
                    tid = self.next_task_id()
                    self.task_queue.put({
                        "link": self.ctx["pending_link"], "type": self.ctx["pending_type"],
                        "filename": self.ctx.get("pending_name", ""), "folder": folder, "task_id": tid
                    })
                    bot.reply_to(m, f"Task added!\nFolder: {folder or 'Root'}\nTask ID: {tid}\nQueue: {self.task_queue.qsize()}")
                    self.ctx.update({"pending_link": None, "pending_type": None, "pending_name": None, "state": "IDLE"})
                self.start_worker(); return

            if text.startswith("http"):
                if "instagram.com" in text or "tiktok.com" in text:
                    tid = self.next_task_id()
                    self.task_queue.put({"link": text, "type": "social", "task_id": tid})
                    bot.reply_to(m, "Social video queue mein add ho gaya!\n(ZeroFeed folder mein upload hoga)")
                    self.start_worker()
                    return
                if is_zip_url(text): ltype = "zip"; hint = "ZIP/Season link mila!"
                elif is_m3u8(text): ltype = "direct"; hint = "M3U8/HLS link mila!"
                else: ltype = "direct"; hint = "Direct link mila!"
                self.ctx.update({"pending_link": text, "pending_type": ltype,
                                 "pending_name": get_filename_from_url(text), "state": "WAITING_FOR_FOLDER"})
                bot.reply_to(m, f"{hint}\n\nFolder name bhejein\n(ya 'root')")
            else:
                bot.reply_to(m, "Link bhejein ya /start dekho")

    def run(self):
        self.register_handlers()
        threading.Thread(target=self._startup_session, daemon=True).start()
        threading.Thread(target=self.session_ping_loop, daemon=True).start()
        self.msg("BOT ONLINE!\n\nDirect / M3U8 / ZIP / YouTube link bhejein\n/start dekho commands ke liye")
        self.bot.infinity_polling()


# ─── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    if not all(c["token"] for c in BOTS):
        raise SystemExit("BOT_TOKEN missing! Repo Settings > Secrets and variables > Actions mein BOT_TOKEN add karo.")
    instances = []; threads = []
    for cfg in BOTS:
        instance = BotInstance(cfg["token"], cfg["chat_id"], cfg["state_file"])
        instances.append(instance)
        t = threading.Thread(target=instance.run, daemon=True)
        threads.append(t); t.start(); time.sleep(2)
    for t in threads: t.join()
