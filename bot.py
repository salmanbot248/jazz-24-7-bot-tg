import os, re, time, threading, queue, subprocess, requests, zipfile, telebot
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

BROWSER_ARGS = ["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--single-process"]
WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
VIDEO_EXTS = [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"]
ZIP_EXTS   = [".zip", ".rar", ".7z", ".tar", ".gz"]
MAX_SIZE_MB = 1990

BOTS = [
    {"token": "8350099407:AAEAX6NzIykESMj50CnduDAwngfHW1ER-oM", "chat_id": 7144917062, "state_file": "state1.json"},
]

ALLOWED_USERS_FILE = "allowed_users.json"

def load_allowed(admin_id):
    import json
    if not os.path.exists(ALLOWED_USERS_FILE):
        with open(ALLOWED_USERS_FILE, "w") as f: json.dump([admin_id], f)
    with open(ALLOWED_USERS_FILE) as f: return set(json.load(f))

def save_allowed(s):
    import json
    with open(ALLOWED_USERS_FILE, "w") as f: json.dump(list(s), f)

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

    def msg(self, text, uid=None):
        target = uid or self.chat_id
        try:
            self.bot.send_message(target, text)
        except:
            try: self.bot.send_message(target, re.sub(r'[*_`\[\]]', '', text))
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
        import uuid
        return str(uuid.uuid4())[:8]

    def is_cancelled(self, task_id):
        return task_id and (task_id in self.cancelled or f"all_{self.chat_id}" in self.cancelled)

    # LOGIN
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
            except Exception as e:
                self.msg(f"Error: {str(e)[:150]}")
            finally:
                browser.close()

    # DOWNLOAD
    def download_file(self, url, out_path, task_id=None):
        last_error = "Unknown"
        clean(out_path)
        referers = get_referers(url)

        if is_m3u8(url):
            if not out_path.endswith('.mp4'):
                out_path = out_path.rsplit('.', 1)[0] + '.mp4'
            for referer in referers[:2]:
                if self.is_cancelled(task_id): return None, "Cancelled"
                clean(out_path)
                try:
                    cmd = ["ffmpeg", "-y"]
                    if referer: cmd += ["-headers", f"Referer: {referer}\r\nUser-Agent: {WEB_UA}\r\n"]
                    else: cmd += ["-user_agent", WEB_UA]
                    cmd += ["-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", out_path]
                    subprocess.run(cmd, capture_output=True, timeout=600)
                    if file_ok(out_path): return out_path, "Success"
                except Exception as e: last_error = str(e)
            return None, f"M3U8 fail: {last_error}"

        try:
            import yt_dlp
            tmp_template = out_path.rsplit('.', 1)[0] + '.%(ext)s'
            ydl_opts = {
                "outtmpl": tmp_template, "quiet": True, "no_warnings": True,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "http_headers": {"User-Agent": WEB_UA, "Referer": referers[0], "Origin": referers[0].rstrip("/")},
                "socket_timeout": 30,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
            base = out_path.rsplit('.', 1)[0]
            for ext in VIDEO_EXTS:
                candidate = base + ext
                if file_ok(candidate, min_mb=0.1): return candidate, "Success"
            if file_ok(out_path, min_mb=0.1): return out_path, "Success"
        except Exception as e: last_error = f"yt-dlp: {str(e)[:100]}"

        for referer in referers:
            if self.is_cancelled(task_id): return None, "Cancelled"
            clean(out_path)
            try:
                cmd = ["aria2c", "-x", "16", "-s", "16", "-k", "1M",
                       "--max-tries=3", "--retry-wait=5", "--allow-overwrite=true",
                       f"--user-agent={WEB_UA}",
                       "-d", os.path.dirname(out_path) or "/tmp",
                       "-o", os.path.basename(out_path)]
                if referer: cmd += [f"--referer={referer}", f"--header=Origin: {referer.rstrip('/')}"]
                cmd.append(url)
                result = subprocess.run(cmd, capture_output=True, timeout=600)
                if file_ok(out_path, min_mb=0.1): return out_path, "Success"
                last_error = "aria2c: " + result.stderr.decode()[:100]
            except Exception as e: last_error = f"aria2c: {str(e)[:100]}"

        for referer in referers:
            if self.is_cancelled(task_id): return None, "Cancelled"
            clean(out_path)
            try:
                cmd = ["curl", "-L", "-k", "--retry", "3", "--retry-delay", "3",
                       "--connect-timeout", "30", "-H", f"User-Agent: {WEB_UA}", "-o", out_path]
                if referer: cmd += ["-H", f"Referer: {referer}", "-H", f"Origin: {referer.rstrip('/')}"]
                cmd.append(url)
                subprocess.run(cmd, timeout=600)
                if file_ok(out_path, min_mb=0.1): return out_path, "Success"
            except Exception as e: last_error = f"curl: {str(e)[:100]}"

        for referer in referers[:2]:
            if self.is_cancelled(task_id): return None, "Cancelled"
            clean(out_path)
            try:
                cmd = ["wget", "-q", "--tries=3", "--timeout=120",
                       f"--user-agent={WEB_UA}", "-O", out_path]
                if referer: cmd += [f"--referer={referer}"]
                cmd.append(url)
                subprocess.run(cmd, timeout=600)
                if file_ok(out_path, min_mb=0.1): return out_path, "Success"
            except Exception as e: last_error = f"wget: {str(e)[:100]}"

        for referer in referers:
            if self.is_cancelled(task_id): return None, "Cancelled"
            clean(out_path)
            try:
                hdrs = {"User-Agent": WEB_UA}
                if referer: hdrs["Referer"] = referer; hdrs["Origin"] = referer.rstrip("/")
                with requests.get(url, headers=hdrs, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(out_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk: f.write(chunk)
                if file_ok(out_path, min_mb=0.1): return out_path, "Success"
            except Exception as e: last_error = f"requests: {str(e)[:100]}"

        return None, last_error

    # SPLIT
    def split_video(self, filepath):
        size_mb = os.path.getsize(filepath) / (1024*1024)
        if size_mb <= MAX_SIZE_MB: return [filepath]
        self.msg(f"File {size_mb:.0f}MB splitting...")
        base = filepath.rsplit(".", 1)[0]; ext = filepath.rsplit(".", 1)[-1]
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True)
        try: total_duration = float(result.stdout.strip())
        except: return [filepath]
        num_parts = int(size_mb / MAX_SIZE_MB) + 1
        part_duration = total_duration / num_parts
        parts = []
        for i in range(num_parts):
            part_path = f"{base}_part{i+1}.{ext}"
            subprocess.run(["ffmpeg", "-y", "-i", filepath,
                "-ss", str(i * part_duration), "-t", str(part_duration),
                "-c", "copy", part_path], capture_output=True, timeout=3600)
            if os.path.exists(part_path) and os.path.getsize(part_path) > 1024:
                parts.append(part_path)
        if parts: clean(filepath)
        return parts if parts else [filepath]

    # SHARE LINK
    def get_share_link(self, page, filename):
        share_link = None
        try:
            self.msg("Share link nikal raha hoon...")
            page.reload(wait_until="networkidle")
            time.sleep(5)
            short_name = os.path.basename(filename)[:25]
            file_element = page.get_by_text(short_name).first
            if file_element.is_visible():
                file_element.click(button="right")
                time.sleep(2)
                share_btn = None
                for selector in ["text=Share", '[data-testid="ShareIcon"]',
                                  "button:has-text('Share')", "li:has-text('Share')"]:
                    try:
                        btn = page.locator(selector).first
                        if btn.is_visible(timeout=2000): share_btn = btn; break
                    except: pass
                if share_btn:
                    share_btn.click()
                    time.sleep(3)
                    for input_sel in ['input[name="get-link-url"]', 'input[readonly]', 'input[type="text"]']:
                        try:
                            inp = page.locator(input_sel).first
                            if inp.is_visible(timeout=2000):
                                val = inp.get_attribute("value")
                                if val and val.startswith("http"): share_link = val; break
                        except: pass
                    page.keyboard.press("Escape")
                    time.sleep(1)
        except Exception as e: self.msg(f"Share link error: {str(e)[:100]}")
        return share_link

    # UPLOAD
    def jazz_drive_upload(self, filename, folder_name=""):
        share_link = None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 720},
                storage_state=self.state_file if os.path.exists(self.state_file) else None
            )
            page = ctx.new_page()
            try:
                page.goto("https://cloud.jazzdrive.com.pk/#folders", wait_until="networkidle", timeout=90000)
                time.sleep(5)
                if page.locator("#msisdn").is_visible():
                    self.msg("Session expire! Login karo...")
                    ok = self.do_login(page, ctx)
                    if not ok: self.msg("Login fail."); return None
                    page.goto("https://cloud.jazzdrive.com.pk/#folders", wait_until="networkidle", timeout=90000)
                    time.sleep(5)

                # FOLDER SELECTION
                if folder_name and folder_name.strip().upper() not in ("ROOT", ""):
                    try:
                        page.get_by_text(folder_name.strip(), exact=False).first.click(timeout=5000)
                        time.sleep(3)
                        self.msg(f"Folder: {folder_name}")
                    except:
                        self.msg(f"Folder '{folder_name}' nahi mila — root mein upload")

                ctx.storage_state(path=self.state_file)
                abs_path = os.path.abspath(filename)
                for sel in ["xpath=/html/body/div/div/div[1]/div/header/div/div/button", "button:has-text('Upload')"]:
                    try: page.click(sel, timeout=5000); break
                    except: pass
                page.wait_for_selector("input[type='file']", state="attached")
                with page.expect_file_chooser() as fc_info:
                    page.click("xpath=/html/body/div[2]/div[3]/div/div/form/div/div/div/div[1]")
                fc_info.value.set_files(abs_path)
                time.sleep(3)
                try:
                    yes_btn = page.get_by_text("Yes", exact=True)
                    if yes_btn.is_visible(): yes_btn.click()
                except: pass
                sz = os.path.getsize(filename) / (1024*1024)
                wait_sec = max(60, int(sz * 4))
                self.msg(f"Uploading {os.path.basename(filename)[:50]}... (~{wait_sec}s)")
                elapsed = 0; upload_done = False
                while elapsed < wait_sec:
                    time.sleep(30); elapsed += 30
                    try:
                        if page.locator("text=Uploads completed").is_visible():
                            self.msg(f"Upload complete! ({elapsed}s)")
                            upload_done = True; break
                    except: pass
                    if elapsed % 60 == 0: self.take_screenshot(page, f"Progress {elapsed}s/{wait_sec}s")
                if not upload_done: self.take_screenshot(page, f"Final check {elapsed}s")

                # SHARE LINK
                share_link = self.get_share_link(page, filename)
                ctx.storage_state(path=self.state_file)
            except Exception as e: self.msg(f"Upload error: {str(e)[:200]}")
            finally: browser.close()
        return share_link

    def upload_with_split(self, filepath, folder_name="", task_id=None):
        if self.is_cancelled(task_id): return []
        parts = self.split_video(filepath)
        links = []
        for i, part in enumerate(parts, 1):
            if self.is_cancelled(task_id): clean(part); break
            if len(parts) > 1: self.msg(f"Part {i}/{len(parts)} upload...")
            link = self.jazz_drive_upload(part, folder_name)
            if link: links.append(link)
            clean(part)
        return links

    # PROCESSORS
    def process_direct(self, url, filename, folder_name="", task_id=None):
        # YouTube detect — Cobalt se direct link lo
        if any(x in url for x in ["youtube.com/watch", "youtu.be/", "youtube.com/shorts"]):
            self.msg("YouTube link detect hua!\nCobalt se direct link nikal raha hoon...")
            direct, quality = self.youtube_to_direct(url)
            if direct:
                self.msg(f"Direct link mila! ({quality}p)\nDownloading...")
                url = direct
                filename = filename or "video.mp4"
            else:
                self.msg("Cobalt fail — yt-dlp try karega...")
        fname = filename or get_filename_from_url(url)
        out_path = f"/tmp/{safe_filename(fname)}"
        clean(out_path)
        self.msg(f"Downloading...\n{fname[:60]}")
        result, error_msg = self.download_file(url, out_path, task_id)
        if not result: self.msg(f"Download fail!\n{error_msg[:200]}"); return
        sz = os.path.getsize(result) / (1024*1024)
        self.msg(f"Downloaded! {sz:.1f} MB\nUploading...")
        links = self.upload_with_split(result, folder_name, task_id)
        if links: self.msg(f"Upload Done!\n\nShare Link:\n{links[0]}")
        else: self.msg("Upload Done!\n(Share link nahi mila)")

    def process_zip(self, url, folder_name="", task_id=None):
        import shutil
        zip_path = f"/tmp/series_{self.chat_id}.zip"
        extract_dir = f"/tmp/series_{self.chat_id}_extracted"
        clean(zip_path)
        if os.path.exists(extract_dir): shutil.rmtree(extract_dir)
        os.makedirs(extract_dir, exist_ok=True)
        self.msg("ZIP/Season download ho raha hai...")
        result, error_msg = self.download_file(url, zip_path, task_id)
        if not result or not file_ok(zip_path): self.msg(f"ZIP fail!\n{error_msg[:200]}"); return
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
        self.msg(f"Total {len(video_files)} Episodes mili!\nUpload shuru...")
        all_links = []
        for i, video_path in enumerate(video_files, 1):
            if self.is_cancelled(task_id): self.msg("Season upload cancelled!"); break
            fname = os.path.basename(video_path)
            fsize = os.path.getsize(video_path) / (1024*1024)
            self.msg(f"Episode {i}/{len(video_files)}\n{fname}\n{fsize:.1f} MB")
            links = self.upload_with_split(video_path, folder_name, task_id)
            if links: all_links.append(f"Ep {i}: {links[0]}"); self.msg(f"Ep {i} Done!\n{links[0]}")
            else: all_links.append(f"Ep {i}: Uploaded (No Link)"); self.msg(f"Ep {i} Done!")
        shutil.rmtree(extract_dir, ignore_errors=True)
        self.msg(f"SEASON COMPLETE!\nTotal {len(all_links)} episodes.\n\n" + "\n".join(all_links))

    # WORKER
    def worker_loop(self):
        try:
            while not self.task_queue.empty():
                while self.queue_paused: time.sleep(5)
                item = self.task_queue.get()
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

    # HANDLERS
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
                bot.reply_to(m, "Format (ek line mein ek link):\nurl - Name - .ext"); return
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
            self.msg(
                f"BOT STATUS\n\n"
                f"State: {'Working' if self.is_working else 'Idle'}\n"
                f"Queue: {self.task_queue.qsize()}\n"
                f"Paused: {'YES' if self.queue_paused else 'No'}\n"
                f"Session: {'Active' if os.path.exists(self.state_file) else 'None'}"
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
                uid = int(parts[1])
                self.allowed.add(uid); save_allowed(self.allowed)
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
                        "link": self.ctx["pending_link"],
                        "type": self.ctx["pending_type"],
                        "filename": self.ctx.get("pending_name", ""),
                        "folder": folder, "task_id": tid
                    })
                    bot.reply_to(m, f"Task added!\nFolder: {folder or 'Root'}\nTask ID: {tid}\nQueue: {self.task_queue.qsize()}")
                    self.ctx.update({"pending_link": None, "pending_type": None, "pending_name": None, "state": "IDLE"})
                self.start_worker(); return

            if text.startswith("http"):
                if is_zip_url(text): ltype = "zip"; hint = "ZIP/Season link mila!"
                elif is_m3u8(text): ltype = "direct"; hint = "M3U8/HLS link mila!"
                else: ltype = "direct"; hint = "Direct link mila!"
                self.ctx["pending_link"] = text
                self.ctx["pending_type"] = ltype
                self.ctx["pending_name"] = get_filename_from_url(text)
                self.ctx["state"]        = "WAITING_FOR_FOLDER"
                bot.reply_to(m, f"{hint}\n\nFolder name bhejein\n(ya 'root')")
            else:
                bot.reply_to(m, "Link bhejein ya /start dekho")

    # SESSION KEEP-ALIVE — har 5 minute mein ping
    def session_ping_loop(self):
        while True:
            time.sleep(5 * 60)  # 5 minute wait
            if not os.path.exists(self.state_file):
                continue
            try:
                import json
                with open(self.state_file) as f:
                    state = json.load(f)
                cookies = {c["name"]: c["value"] for c in state.get("cookies", [])}
                key = cookies.get("validationKey") or cookies.get("validationkey")
                if not key:
                    continue
                # JazzDrive API ko silent ping
                r = requests.get(
                    f"https://cloud.jazzdrive.com.pk/sapi/media/folder?action=get&validationkey={key}",
                    cookies=cookies,
                    headers={"User-Agent": WEB_UA},
                    timeout=15
                )
                if r.status_code == 200:
                    pass  # session alive
                else:
                    self.msg("Session ping fail — /checklogin karo")
            except Exception as e:
                pass  # silently fail, next ping pe try hoga

    # COBALT YouTube direct link
    def youtube_to_direct(self, url):
        for quality in ["1080", "720", "480"]:
            try:
                r = requests.post(
                    "https://api.cobalt.tools/",
                    json={"url": url, "videoQuality": quality},
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    },
                    timeout=30
                )
                data = r.json()
                if data.get("status") in ("redirect", "stream", "tunnel", "picker"):
                    link = data.get("url")
                    if link: return link, quality
            except Exception as e:
                continue
        return None, None

    def run(self):
        self.register_handlers()
        # Session keep-alive thread start karo
        threading.Thread(target=self.session_ping_loop, daemon=True).start()
        self.msg("BOT ONLINE!\n\nDirect / M3U8 / ZIP / YouTube link bhejein\n/start dekho commands ke liye")
        self.bot.infinity_polling()


if __name__ == "__main__":
    instances = []; threads = []
    for cfg in BOTS:
        instance = BotInstance(cfg["token"], cfg["chat_id"], cfg["state_file"])
        instances.append(instance)
        t = threading.Thread(target=instance.run, daemon=True)
        threads.append(t); t.start(); time.sleep(2)
    for t in threads: t.join()
