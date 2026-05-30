# ==========================================
# 🤖 KAREEM BOT v4.1 - HIGH QUALITY FIXED
# 📸 YouTube (HD/4K) | Google Drive | MediaFire | Direct Links
# 🧠 Smart Cache | 🔄 Multi-Client Auto-Retry | ⚡ Fast Download
# 💓 Heartbeat Included (Keep Alive)
# ==========================================

import os, asyncio, time, json, re, requests, gdown
import nest_asyncio, yt_dlp
from playwright.async_api import async_playwright

nest_asyncio.apply()

CACHE_FILE    = "selector_cache.json"
COOKIES_FILE  = "jazz_cookies.json"
JAZZDRIVE_URL = "https://cloud.jazzdrive.com.pk/#folders"
LOGIN_URL     = "https://cloud.jazzdrive.com.pk/login"

os.makedirs("downloads",   exist_ok=True)
os.makedirs("screenshots", exist_ok=True)

# ─────────────────────────────────────────
# 💓 HEARTBEAT (Session Keep Alive)
# ─────────────────────────────────────────
async def heartbeat(page):
    """ہر 5 منٹ بعد پیج کو ریفریش کرتا ہے تاکہ کوکیز ایکسپائر نہ ہوں"""
    while True:
        await asyncio.sleep(300) # 5 minutes wait
        try:
            await page.reload()
            print(f"\n💓 [Heartbeat] JazzDrive pinged at: {time.strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"  ⚠️ Heartbeat error: {e}")
            break

# ─────────────────────────────────────────
# 💾  SELECTOR CACHE
# ─────────────────────────────────────────
class SelectorCache:
    def __init__(self):
        self.data = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE) as f:
                    self.data = json.load(f)
                print(f"💾 Cache: {len(self.data)} entries")
            except:
                pass

    def get(self, key):
        return self.data.get(key)

    def save(self, key, sel):
        self.data[key] = sel
        with open(CACHE_FILE, 'w') as f:
            json.dump(self.data, f, indent=2)

    def forget(self, key):
        if key in self.data:
            del self.data[key]
            with open(CACHE_FILE, 'w') as f:
                json.dump(self.data, f, indent=2)
            print(f"  🗑️ Cache forget: {key}")

    def clear_all(self):
        self.data = {}
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)

cache = SelectorCache()

# ─────────────────────────────────────────
# 🔇  SILENT LOGGER
# ─────────────────────────────────────────
class SilentLogger:
    def debug(self, msg):   pass
    def warning(self, msg): pass
    def error(self, msg):   pass

# ─────────────────────────────────────────
# 📸  SCREENSHOT
# ─────────────────────────────────────────
async def take_ss(page, name):
    path = f"screenshots/{name}_{int(time.time())}.png"
    try:
        await page.screenshot(path=path)
        print(f"  📸 {path}")
    except:
        pass

# ─────────────────────────────────────────
# 🌐  JAZZDRIVE OPEN
# ─────────────────────────────────────────
async def open_jazzdrive(page):
    print("🌐 JazzDrive khul raha hai...")
    try:
        await page.goto(JAZZDRIVE_URL, timeout=90000)
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        await asyncio.sleep(5)
    except Exception as e:
        print(f"  ⚠️ Load slow: {e}")

    if "login" in page.url.lower():
        print("❌ Cookies expire! Login karo dobara.")
        if os.path.exists(COOKIES_FILE):
            os.remove(COOKIES_FILE)
        return False

    for chk in ["#uploadActionButton", "button[aria-label='upload']", ".NavigationEntry"]:
        try:
            if await page.is_visible(chk, timeout=3000):
                print("  ✅ JazzDrive ready")
                return True
        except:
            continue

    if "jazzdrive" in page.url and "login" not in page.url:
        print("  ✅ JazzDrive loaded")
        return True

    await take_ss(page, "WARN_state")
    return True

# ─────────────────────────────────────────
# 📂  FOLDER NAVIGATE
# ─────────────────────────────────────────
async def navigate_to_folder(page, folder_name):
    if not folder_name or folder_name.strip().upper() in ["ROOT", ""]:
        print("  📁 ROOT mein upload hoga")
        return True

    fn = folder_name.strip()
    print(f"  📂 Folder dhundh raha hai: '{fn}'")

    for sel in [f"text={fn}", f".mdl-list__item:has-text('{fn}')", f"li:has-text('{fn}')"]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=3000):
                await loc.click(timeout=4000)
                await asyncio.sleep(3)
                print(f"  ✅ Folder mila: {fn}")
                return True
        except:
            continue

    for exact in [True, False]:
        try:
            await page.get_by_text(fn, exact=exact).first.click(timeout=4000)
            await asyncio.sleep(3)
            print(f"  ✅ Folder (text): {fn}")
            return True
        except:
            continue

    try:
        found = await page.evaluate(f"""
            (function() {{
                const t = '{fn.lower()}';
                const els = document.querySelectorAll('[class*="FolderName"],[class*="folder"],li,td');
                for (const el of els) {{
                    if ((el.textContent||'').trim().toLowerCase() === t) {{ el.click(); return true; }}
                }}
                for (const el of els) {{
                    if ((el.textContent||'').toLowerCase().includes(t)) {{ el.click(); return true; }}
                }}
                return false;
            }})()
        """)
        if found:
            await asyncio.sleep(3)
            print(f"  ✅ Folder (JS): {fn}")
            return True
    except:
        pass

    print(f"  ⚠️ Folder '{fn}' nahi mila — ROOT mein upload hoga")
    return False

# ─────────────────────────────────────────
# ⬆️  UPLOAD BUTTON CLICK
# ─────────────────────────────────────────
async def click_upload_button(page):
    cached = cache.get("upload_btn")
    if cached:
        try:
            el = page.locator(cached).first
            await el.wait_for(state="visible", timeout=4000)
            await el.click(timeout=4000)
            print("  💾 Upload btn (cache)")
            return True
        except:
            cache.forget("upload_btn")

    selectors = [
        "#uploadActionButton",
        "button[aria-label='upload']",
        "button[id='uploadActionButton']",
        "button.MuiIconButton-root[aria-label='upload']",
        "button[aria-label*='upload' i]",
        "button[class*='css-1yxmbwk']",
        "#topbarContainer button:last-of-type",
        ".TopBarActions button",
        "header button:last-of-type",
    ]

    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=3000)
            await el.click(timeout=3000)
            cache.save("upload_btn", sel)
            print(f"  ✅ Upload btn: {sel}")
            return True
        except:
            continue

    try:
        ok = await page.evaluate("""
            (function(){
                let b = document.getElementById('uploadActionButton');
                if (b) { b.click(); return true; }
                b = document.querySelector("button[aria-label='upload']");
                if (b) { b.click(); return true; }
                const icons = document.querySelectorAll('[data-testid="CloudUploadIcon"]');
                if (icons.length) {
                    const btn = icons[0].closest('button');
                    if (btn) { btn.click(); return true; }
                }
                return false;
            })()
        """)
        if ok:
            print("  ✅ Upload btn (JS)")
            return True
    except:
        pass

    await take_ss(page, "FAIL_uploadbtn")
    print("  ❌ Upload button nahi mila!")
    return False

# ─────────────────────────────────────────
# 📁  DIALOG → "Upload files" → FILE SET
# ─────────────────────────────────────────
async def set_files_via_dialog(page, file_paths):
    files = file_paths if isinstance(file_paths, list) else [file_paths]
    print(f"  📄 Files: {[os.path.basename(f) for f in files]}")

    await asyncio.sleep(2.5)

    dialog_ok = False
    for d in ["[role='dialog']", ".MuiDialog-paper", ".DialogForm",
              ".OptionsListContainer", ".OptionName"]:
        try:
            if await page.is_visible(d, timeout=4000):
                dialog_ok = True
                break
        except:
            continue

    if not dialog_ok:
        await take_ss(page, "WARN_nodialog")
        print("  ⚠️ Dialog nahi aya — direct input try")
        return await _direct_input(page, files)

    option_selectors = [
        ".OptionName",
        "div.OptionName",
        ".OptionsListContainer .OptionName",
        ".OptionContainer .OptionName",
        ".OptionLabel .OptionName",
        "text=Upload files",
        ":text('Upload files')",
        ".OptionContainer:first-child .OptionName",
        "[class='OptionName']",
    ]

    for sel in option_selectors:
        try:
            async with page.expect_file_chooser(timeout=7000) as fc_info:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=3000)
                await loc.click(timeout=3000)
            fc = await fc_info.value
            await fc.set_files(files)
            print(f"  ✅ File chooser via: {sel}")
            return True
        except:
            continue

    try:
        containers = page.locator(".OptionContainer")
        if await containers.count() > 0:
            async with page.expect_file_chooser(timeout=7000) as fc_info:
                await containers.first.click(timeout=3000)
            fc = await fc_info.value
            await fc.set_files(files)
            print("  ✅ File chooser via .OptionContainer[0]")
            return True
    except:
        pass

    print("  ⚠️ Dialog option click fail — ESC + direct input")
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)
    except:
        pass

    return await _direct_input(page, files)

async def _direct_input(page, files):
    await page.evaluate("""
        document.querySelectorAll('input[type="file"]').forEach(el => {
            el.style.cssText =
                'display:block!important;visibility:visible!important;' +
                'opacity:1!important;width:1px;height:1px;' +
                'position:fixed;top:0;left:0;z-index:99999;';
            el.removeAttribute('hidden');
        });
    """)
    await asyncio.sleep(0.5)
    try:
        inp = page.locator("input[type='file']").first
        await inp.set_input_files(files)
        print("  ✅ Direct file input set")
        return True
    except Exception as e:
        await take_ss(page, "FAIL_fileset")
        print(f"  ❌ File set fail: {e}")
        return False

# ─────────────────────────────────────────
# ✅  WAIT FOR UPLOAD COMPLETE
# ─────────────────────────────────────────
async def wait_upload_done(page, timeout_min=120):
    SUCCESS = [
        "Uploads completed", "Upload complete",
        "uploaded successfully", "All files uploaded",
    ]
    print(f"  ⏳ Upload complete hone ka wait ({timeout_min} min max)...")

    for i in range(timeout_min * 60):
        for txt in SUCCESS:
            try:
                if await page.is_visible(f"text={txt}", timeout=300):
                    print(f"\n  🎉 Upload Done! ({txt})")
                    return True
            except:
                pass
        if i % 120 == 0 and i > 0:
            try:
                body = await page.inner_text("body")
                pcts = [w for w in body.split() if '%' in w and w[:-1].isdigit()]
                print(f"  ⬆️ {i//60} min: {pcts[-1] if pcts else 'upload jari hai...'}")
            except:
                pass
        await asyncio.sleep(1)

    print(f"  ⚠️ Timeout! Manually check karo.")
    return False

# ─────────────────────────────────────────
# 🌐  JAZZDRIVE LOGIN (OTP)
# ─────────────────────────────────────────
async def jazz_login(number):
    async with async_playwright() as p:
        print(f"\n🌐 Login ho raha hai: {number}")
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        ctx  = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await ctx.new_page()
        try:
            await page.goto(LOGIN_URL, timeout=90000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5)

            num = number.strip()
            for sel in ["input[type='tel']", "input[type='number']", "input"]:
                try:
                    await page.fill(sel, num, timeout=3000)
                    print(f"  ✅ Number fill: {sel}")
                    break
                except:
                    continue

            await asyncio.sleep(1)
            for sel in ["#signinbtn", "button[type='submit']", "button"]:
                try:
                    await page.click(sel, timeout=3000)
                    print("  ✅ OTP button clicked")
                    break
                except:
                    continue

            print("📲 OTP aa raha hai — 12 second wait...")
            await asyncio.sleep(12)
            await page.screenshot(path="screenshots/otp_page.png")
            otp = input("\n👉 OTP enter karo: ").strip()

            for sel in ["#otp", "input[name='otp']", "input[type='number']", "input:visible"]:
                try:
                    await page.click(sel, timeout=2000)
                    break
                except:
                    continue

            for d in otp:
                await page.keyboard.press(d)
                await asyncio.sleep(0.25)

            await page.keyboard.press("Enter")
            await asyncio.sleep(10)
            await ctx.storage_state(path=COOKIES_FILE)
            print("✅ Login successful!")
            return True
        except Exception as e:
            print(f"❌ Login fail: {e}")
            return False
        finally:
            await browser.close()

# ─────────────────────────────────────────
# 📋  PLAYLIST / LINK FETCH
# ─────────────────────────────────────────
def get_playlist_entries(link):
    if any(x in link for x in ["drive.google.com", "mediafire.com"]) or \
       not any(x in link for x in ["youtube.com", "youtu.be"]):
        return [{'url': link, 'webpage_url': link, 'title': 'Cloud_or_Direct_File'}]

    ck = next((n for n in ['cookies.txt', 'youtube.com_cookies.txt', 'yt_cookies.txt']
               if os.path.exists(n)), None)
    opts = {
        'quiet': True, 'no_warnings': True,
        'extract_flat': 'in_playlist',
        'ignoreerrors': True, 'logger': SilentLogger(),
    }
    if ck: opts['cookiefile'] = ck
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(link, download=False)
            if not info: return []
            if 'entries' in info:
                out = []
                for e in info['entries']:
                    if not e: continue
                    vid = e.get('webpage_url') or e.get('url', '')
                    if not vid.startswith('http') and e.get('id') and 'youtube' in link:
                        vid = f"https://www.youtube.com/watch?v={e['id']}"
                    if vid:
                        out.append({'url': vid, 'webpage_url': vid,
                                    'title': e.get('title', f'V{len(out)+1}')})
                print(f"  📋 Playlist mein {len(out)} videos mili")
                return out
            url = info.get('webpage_url') or link
            return [{'url': url, 'webpage_url': url, 'title': info.get('title', 'Video')}]
    except Exception as e:
        print(f"  ❌ Playlist fetch fetch fail: {e}")
        return []

# ─────────────────────────────────────────
# 📂  GOOGLE DRIVE DOWNLOAD
# ─────────────────────────────────────────
def download_from_google_drive(url, idx):
    print(f"  🤖 Google Drive link detect hua...")
    match = re.search(r'/d/([a-zA-Z0-9-_]+)', url) or re.search(r'id=([a-zA-Z0-9-_]+)', url)
    if match:
        file_id = match.group(1)
        try:
            print(f"  📥 Google Drive se download ho rahi hai...")
            filepath = gdown.download(id=file_id, output="downloads/", quiet=True, fuzzy=True)
            if filepath and os.path.exists(filepath):
                filename = os.path.basename(filepath)
                new_path = os.path.join("downloads", f"{idx:02d}_{filename}")
                os.rename(filepath, new_path)
                mb = os.path.getsize(new_path) / 1e6
                print(f"  ✅ GD Complete: {os.path.basename(new_path)} | {mb:.0f}MB")
                return new_path
        except Exception as e:
            print(f"  ❌ Google Drive fail: {e}")
    return None

# ─────────────────────────────────────────
# 🔥  MEDIAFIRE DOWNLOAD
# ─────────────────────────────────────────
def download_from_mediafire(url, idx):
    print(f"  🤖 MediaFire link detect hua...")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        res = requests.get(url, headers=headers, timeout=20)
        match = re.search(r'https?://download[0-9]+\.mediafire\.com/[^\s"\'>]+', res.text)
        if match:
            direct_url = match.group(0)
            filename = direct_url.split('/')[-2].split('?')[0] if '/' in direct_url else "mediafire_file"
            out_path = f"downloads/{idx:02d}_{filename}"
            print(f"  📥 MediaFire direct download: {filename}...")
            with requests.get(direct_url, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                if 'content-disposition' in r.headers:
                    fname_match = re.search(r'filename="([^"]+)"', r.headers['content-disposition'])
                    if fname_match:
                        filename = fname_match.group(1)
                        out_path = f"downloads/{idx:02d}_{filename}"
                with open(out_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk: f.write(chunk)
            mb = os.path.getsize(out_path) / 1e6
            print(f"  ✅ MediaFire Complete: {filename} | {mb:.0f}MB")
            return out_path
        else:
            print("  ❌ MediaFire direct link nahi mila!")
    except Exception as e:
        print(f"  ❌ MediaFire fail: {e}")
    return None

# ─────────────────────────────────────────
# 🔗  GENERIC DIRECT DOWNLOAD
# ─────────────────────────────────────────
def download_generic_file(url, idx):
    print(f"  🤖 Direct URL detect hua...")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        filename = url.split('/')[-1].split('?')[0]
        if not filename or '.' not in filename:
            filename = "direct_file.mp4"
        out_path = f"downloads/{idx:02d}_{filename}"
        print(f"  📥 Stream download ho rahi hai: {filename}...")
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            with open(out_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
            mb = downloaded / 1e6
        print(f"  ✅ Direct Complete: {filename} | {mb:.0f}MB")
        return out_path
    except Exception as e:
        print(f"  ❌ Direct download fail: {e}")
        return None

# ─────────────────────────────────────────
# 📥  YOUTUBE DOWNLOAD
# ─────────────────────────────────────────
def download_one(url, idx, total, quality):
    if not url.startswith('http'):
        return None
    print(f"\n  🔗 [{idx}/{total}] {url[:80]}")
    if "drive.google.com" in url: return download_from_google_drive(url, idx)
    if "mediafire.com"    in url: return download_from_mediafire(url, idx)
    if not any(x in url for x in ["youtube.com", "youtu.be"]):
        return download_generic_file(url, idx)

    ck = next((n for n in ['cookies.txt', 'youtube.com_cookies.txt', 'yt_cookies.txt']
               if os.path.exists(n)), None)
    
    fmt = (f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/'
           f'bestvideo[height<={quality}]+bestaudio/'
           f'best[height<={quality}]/best')

    BASE_OPTS = {
        'format': fmt,
        'merge_output_format': 'mp4',
        'outtmpl': f'downloads/{idx:02d}_%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'quiet': False,
        'noprogress': True,
        'retries': 5,
        'fragment_retries': 5,
        'concurrent_fragment_downloads': 4,
        'socket_timeout': 30,
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    }
    if ck: BASE_OPTS['cookiefile'] = ck

    try:
        with yt_dlp.YoutubeDL(BASE_OPTS) as ydl:
            info = ydl.extract_info(url, download=True)
            fpath = ydl.prepare_filename(info).replace('.webm', '.mp4').replace('.mkv', '.mp4')
            if not os.path.exists(fpath): # Fallback
                for f in os.listdir('downloads'):
                    if f.startswith(f'{idx:02d}_'): fpath = os.path.join('downloads', f)
            return fpath
    except Exception as e:
        print(f"  ❌ Youtube download fail: {e}")
        return None

# ─────────────────────────────────────────
# ✂️  FILE SPLIT
# ─────────────────────────────────────────
def split_file(path, max_mb=1900):
    mb = os.path.getsize(path) / 1e6
    if mb <= 1990: return [path]
    print(f"  ✂️ {mb:.0f}MB — 2GB limit ke liye split ho raha hai...")
    base, ext = os.path.splitext(path)
    parts = []
    try:
        import subprocess
        probe = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path], capture_output=True, text=True)
        dur = float(json.loads(probe.stdout)["format"]["duration"])
        pd = dur * (max_mb / mb)
        for i in range(int(dur / pd) + 1):
            s = i * pd
            if s >= dur: break
            pp = f"{base}_part{i+1:02d}{ext}"
            subprocess.run(["ffmpeg", "-y", "-ss", str(s), "-i", path, "-t", str(pd), "-c", "copy", "-avoid_negative_ts", "make_zero", pp], capture_output=True)
            if os.path.exists(pp): parts.append(pp)
        if parts: os.remove(path)
        return parts
    except: return [path]

# ─────────────────────────────────────────
# 🚀  MAIN PIPELINE (Download → Upload)
# ─────────────────────────────────────────
async def pipeline(url_list, quality, folder):
    loop = asyncio.get_event_loop()
    all_entries = []
    for u in url_list:
        ents = get_playlist_entries(u)
        if not ents: ents = [{'url': u, 'webpage_url': u, 'title': 'File'}]
        all_entries.extend(ents)

    total = len(all_entries)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        ctx  = await browser.new_context(storage_state=COOKIES_FILE, viewport={'width': 1280, 'height': 720})
        page = await ctx.new_page()

        # 💓 START HEARTBEAT
        heartbeat_task = asyncio.create_task(heartbeat(page))

        if not await open_jazzdrive(page):
            await browser.close(); return

        await navigate_to_folder(page, folder)

        for idx, entry in enumerate(all_entries, 1):
            fpath = await loop.run_in_executor(None, download_one, entry.get('webpage_url') or entry.get('url', ''), idx, total, quality)
            if not fpath: continue
            
            parts = split_file(fpath)
            for part in parts:
                if not await click_upload_button(page): continue
                if not await set_files_via_dialog(page, part): continue
                await asyncio.sleep(2)
        
        await wait_upload_done(page)
        heartbeat_task.cancel() # STOP HEARTBEAT
        await browser.close()

# ─────────────────────────────────────────
# 📦  BATCH UPLOAD
# ─────────────────────────────────────────
async def batch_upload(file_paths, folder):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        ctx  = await browser.new_context(storage_state=COOKIES_FILE, viewport={'width': 1280, 'height': 720})
        page = await ctx.new_page()
        
        # 💓 START HEARTBEAT
        heartbeat_task = asyncio.create_task(heartbeat(page))

        if not await open_jazzdrive(page): await browser.close(); return
        await navigate_to_folder(page, folder)
        if await click_upload_button(page):
            await set_files_via_dialog(page, file_paths)
            await wait_upload_done(page)
        
        heartbeat_task.cancel() # STOP HEARTBEAT
        await browser.close()

# ─────────────────────────────────────────
# 🚀  MAIN EXECUTION
# ─────────────────────────────────────────
async def main():
    print("\n" + "=" * 55)
    print("  🤖  KAREEM BOT v4.1 — HIGH QUALITY FIXED")
    print("  📺  YouTube (HD/4K) | Drive | MediaFire | Direct Links")
    print("=" * 55)

    if not os.path.exists(COOKIES_FILE):
        num = input("📱 Jazz number (03xxxxxxxxx): ").strip()
        await jazz_login(num)
    
    while True:
        print("\n  1: Link Download/Upload | 2: Batch Upload | 3: Login | c: Clear Cache | x: Exit")
        ch = input("👉 Choice: ").strip()
        if ch.lower() in ['x', 'exit']: break
        elif ch.lower() == 'c': cache.clear_all()
        elif ch == '3':
            if os.path.exists(COOKIES_FILE): os.remove(COOKIES_FILE)
            num = input("📱 Jazz number (03xxxxxxxxx): ").strip()
            await jazz_login(num)
        elif ch == '2':
            existing = [os.path.join("downloads", f) for f in os.listdir("downloads") if f.endswith(('.mp4', '.mkv', '.webm', '.zip', '.rar'))]
            if existing: await batch_upload(existing, input("📁 Folder: ").strip())
        else:
            urls = input("🔗 Link(s): ").split()
            if urls: await pipeline(urls, "1080", input("📁 Folder: ").strip())

if __name__ == "__main__":
    asyncio.run(main())
