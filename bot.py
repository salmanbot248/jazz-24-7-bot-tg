import os, re, time, json, threading, queue, subprocess, requests, zipfile, mimetypes, telebot
from datetime import datetime
from urllib.parse import urlparse
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from playwright.sync_api import sync_playwright

BROWSER_ARGS = ["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--single-process"]
WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
VIDEO_EXTS = [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"]
ZIP_EXTS   = [".zip", ".rar", ".7z", ".tar", ".gz"]
MAX_SIZE_MB = 1990

BOTS = [
    {"token": "8512186971:AAGUia2oicjFlNDgBtM6rC1a7BESGPihisk", "chat_id": 7144917062, "state_file": "state1.json"},
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

# ─── JazzDrive API helpers (from v5) ─────────────────────────

def cookie_file(state_file):
    return state_file  # state_file is already the cookie file

def load_cookies(state_file):
    if not os.path.exists(state_file): return None, None
    try:
        with open(state_file) as f: data = json.load(f)
        raw = data.get("cookies", [])
        cookies = {c["name"]: c["value"] for c in raw}
        key = next((c["value"] for c in raw if c["name"] == "validationKey"), None)
        return cookies, key
    except: return None, None

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

    # ─── Session keep-alive ping ──────────────────────────────

    def session_ping_loop(self):
        while True:
            time.sleep(5 * 60)
            cookies, key = load_cookies(self.state_file)
            if not cookies or not key: continue
            try:
                api_get_folders(cookies, key)  # lightweight API call
            except: pass

    # ─── LOGIN (Playwright — only for login) ─────────────────

    def do_login(self, page, context):
        self.msg("LOGIN REQUIRED\n\nJazz number bhejein\nFormat: 03XXXXXXXXX")
        self.ctx["state"] = "WAITING_FOR_NUMBER"
        for _ in range(500):
            if self.ctx["state"] == "NUMBER_RECEIVED": break
            time.sleep(1)
        else:
            self.msg("Timeout! Task cancel."); return False

        page.locator("#msisdn").fill(self.ctx["number"])
        time.sleep(1)
        page.locator("#signinbtn").first.click()
        time.sleep(3)
        self.take_screenshot(page, "Number submit")
        self.msg("Number accept!\n\nOTP bhejein:")
        self.ctx["state"] = "WAITING_FOR_OTP"
        for _ in range(500):
            if self.ctx["state"] == "OTP_RECEIVED": break
            time.sleep(1)
        else:
            self.msg("Timeout! Task cancel."); return False

        for i, digit in enumerate(self.ctx["otp"].strip()[:6], 1):
            try:
                f = page.locator(f"//input[@aria-label='Digit {i}']")
                if f.is_visible(): f.fill(digit); time.sleep(0.2)
            except: pass
        time.sleep(5)
        self.take_screenshot(page, "OTP submit")
        context.storage_state(path=self.state_file)
        self.msg("LOGIN SUCCESSFUL!\nSession save!\nLink bhejein")
        self.ctx["state"] = "IDLE"
        return True

    def check_login_status(self):
        self.msg("Jazz Drive login check...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 720},
                storage_state=self.state_file if os.path.exists(self.state_file) else None
            )
            page = ctx.new_page()
            try:
                page.goto("https://cloud.jazzdrive.com.pk/", wait_until="networkidle", timeout=90000)
                time.sleep(3)
                if page.locator("#msisdn").is_visible():
                    self.msg("Session expire!\nLogin karte hain...")
                    self.do_login(page, ctx)
                else:
                    self.msg("LOGIN VALID!\nLink bhejein!")
            except Exception as e: self.msg(f"Error: {str(e)[:150]}")
            finally: browser.close()

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

    # ─── Cobalt YouTube ───────────────────────────────────────

    def youtube_to_direct(self, url):
        for quality in ["1080", "720", "480"]:
            try:
                r = requests.post(
                    "https://api.cobalt.tools/",
                    json={"url": url, "videoQuality": quality},
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    timeout=30
                )
                d = r.json()
                if d.get("status") in ("red
