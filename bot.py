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
                if d.get("status") in ("redirect", "stream", "tunnel", "picker"):
                    link = d.get("url")
                    if link: return link, quality
            except: continue
        return None, None

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
        # YouTube
        if any(x in url for x in ["youtube.com/watch", "youtu.be/", "youtube.com/shorts"]):
            self.msg("YouTube link!\nCobalt se direct link nikal raha hoon...")
            direct, quality = self.youtube_to_direct(url)
            if direct:
                self.msg(f"Direct link mila! ({quality}p)\nDownloading...")
                url = direct; filename = filename or "video.mp4"
            else:
                self.msg("Cobalt fail — yt-dlp try karega...")

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

        @bot.message_handler(commands=["status"])
        def cmd_status(m):
            if m.chat.id not in self.allowed: return
            cookies, key = load_cookies(self.state_file)
            session_ok = "Active" if cookies and key else "None"
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
        threading.Thread(target=self.session_ping_loop, daemon=True).start()
        self.msg("BOT ONLINE!\n\nDirect / M3U8 / ZIP / YouTube link bhejein\n/start dekho commands ke liye")
        self.bot.infinity_polling()


# ─── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    instances = []; threads = []
    for cfg in BOTS:
        instance = BotInstance(cfg["token"], cfg["chat_id"], cfg["state_file"])
        instances.append(instance)
        t = threading.Thread(target=instance.run, daemon=True)
        threads.append(t); t.start(); time.sleep(2)
    for t in threads: t.join()
