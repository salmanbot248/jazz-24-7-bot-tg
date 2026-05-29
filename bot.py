import os, json, time, asyncio, re, mimetypes, uuid, zipfile, shutil
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urljoin
import random

import aiohttp, aiofiles, requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# ── CREDENTIALS ───────────────────────────────────────────────
BOT_TOKEN = "8552273158:AAERHvFbWO3eFrd2f1xBWGz3a5ILKE0DUUg"
ADMIN_ID  = 7144917062

ALLOWED_USERS_FILE = "allowed_users.json"
SETTINGS_FILE      = "bot_settings.json"

# ── STATE ─────────────────────────────────────────────────────
user_states       = {}
user_pending_jobs = {}
cancelled_tasks   = set()
login_events      = {}

# ── USERS ─────────────────────────────────────────────────────
def load_allowed_users():
    if not os.path.exists(ALLOWED_USERS_FILE):
        with open(ALLOWED_USERS_FILE, "w") as f: json.dump([ADMIN_ID], f)
    with open(ALLOWED_USERS_FILE) as f: return set(json.load(f))

def save_allowed_users(s):
    with open(ALLOWED_USERS_FILE, "w") as f: json.dump(list(s), f)

def load_settings():
    default = {"ping_interval": 5}
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w") as f: json.dump(default, f)
        return default
    with open(SETTINGS_FILE) as f: return {**default, **json.load(f)}

allowed_users = load_allowed_users()
bot_settings  = load_settings()

# ── JAZZDRIVE HELPERS ─────────────────────────────────────────
def cookie_file(uid): return f"jazz_cookies_{uid}.json"

def load_cookies(uid):
    p = cookie_file(uid)
    if not os.path.exists(p): return None, None
    try:
        with open(p) as f: data = json.load(f)
        raw = data.get("cookies", [])
        cookies = {c["name"]: c["value"] for c in raw}
        key = next((c["value"] for c in raw if c["name"] == "validationKey"), None)
        return cookies, key
    except: return None, None

def get_cloud_folders(cookies, key):
    try:
        url = f"https://cloud.jazzdrive.com.pk/sapi/media/folder?action=get&validationkey={key}"
        r = requests.get(url, cookies=cookies, headers={"User-Agent":"Mozilla/5.0"}, timeout=20)
        folders = r.json().get("data", {}).get("folders", [])
        root = next((f["id"] for f in folders if f.get("name") == "/"), None)
        if not root: return [], None
        subs = [(f["name"], f["id"]) for f in folders if f.get("parentid") == root and f.get("name") != "/"]
        return subs, root
    except: return [], None

def create_folder(name, parent_id, cookies, key):
    try:
        url = f"https://cloud.jazzdrive.com.pk/sapi/media/folder?action=save&validationkey={key}"
        r = requests.post(url, cookies=cookies,
            json={"data": {"magic": False, "offline": False, "name": name, "parentid": int(parent_id)}},
            headers={"User-Agent":"Mozilla/5.0"}, timeout=20)
        d = r.json()
        return d.get("id") or d.get("data", {}).get("id") or parent_id
    except: return parent_id

def generate_share_link(item_id, is_folder, cookies, key):
    try:
        if is_folder:
            url = f"https://cloud.jazzdrive.com.pk/sapi/link/folder?action=save&validationkey={key}"
            payload = {"data": {"folderid": int(item_id)}}
        else:
            url = f"https://cloud.jazzdrive.com.pk/sapi/media/set?action=save&validationkey={key}"
            payload = {"data": {"set": {"items": [int(item_id)]}}}
        s = requests.Session()
        s.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5)))
        r = s.post(url, cookies=cookies, json=payload, headers={"User-Agent":"Mozilla/5.0"}, timeout=25)
        d = r.json()
        link = d.get("url") or d.get("data", {}).get("url")
        if not link:
            h = d.get("hash") or d.get("data", {}).get("hash")
            if h: link = f"https://cloud.jazzdrive.com.pk/share/{'f/' if is_folder else ''}{h}"
        return link
    except: return None

def index_wait(size_bytes):
    mb = size_bytes / (1024*1024)
    if mb <= 20: return 3
    if mb <= 100: return 12
    if mb <= 500: return 20
    if mb <= 1200: return 30
    return 40

def fmt_bytes(n):
    if n <= 0: return "0 B"
    n = float(n)
    for unit in ["B","KB","MB","GB"]:
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

# ── DOWNLOAD ──────────────────────────────────────────────────
async def download_url(url, path, task_id, on_progress=None):
    timeout = aiohttp.ClientTimeout(total=3600)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            done = 0
            async with aiofiles.open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(4*1024*1024):
                    if task_id in cancelled_tasks: raise asyncio.CancelledError()
                    await f.write(chunk)
                    done += len(chunk)
                    if on_progress: await on_progress(done, total)

# ── UPLOAD ────────────────────────────────────────────────────
def upload_file(local_path, filename, folder_id, cookies, key, task_id):
    fsize = os.path.getsize(local_path)
    mime  = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    meta  = {
        "name": filename, "size": str(fsize), "folderid": str(folder_id),
        "contenttype": mime,
        "modificationdate": datetime.now().strftime("%Y%m%dT%H%M%SZ")
    }
    sess = requests.Session()
    sess.mount("https://", HTTPAdapter(max_retries=Retry(total=5, backoff_factor=0.5)))

    def cb(monitor):
        if task_id in cancelled_tasks: raise Exception("Cancelled by user")

    with open(local_path, "rb") as f:
        m   = MultipartEncoder(fields={
            "data": (None, json.dumps({"data": meta}), "application/json"),
            "file": (filename, f, mime)
        })
        mon = MultipartEncoderMonitor(m, cb)
        r   = sess.post(
            f"https://cloud.jazzdrive.com.pk/sapi/upload?action=save&acceptasynchronous=true&validationkey={key}",
            data=mon,
            headers={"User-Agent":"Mozilla/5.0", "Content-Type": mon.content_type},
            cookies=cookies, timeout=600
        )
    if r.status_code == 200:
        d   = r.json()
        fid = d.get("data", {}).get("id") or d.get("id")
        if not fid and isinstance(d.get("data"), list) and d["data"]:
            fid = d["data"][0].get("id")
        return True, fid
    return False, None

# ── LOGIN ─────────────────────────────────────────────────────
async def do_login(bot, uid, phone, reply_msg):
    msg  = await reply_msg.reply_text("⚙️ Connecting to JazzDrive...")
    sess = requests.Session()
    sess.headers.update({"User-Agent":"Mozilla/5.0"})
    loop = asyncio.get_event_loop()

    verify_url = ""; res2 = None
    for attempt in range(3):
        try:
            if attempt: await asyncio.sleep(2)
            await loop.run_in_executor(None, lambda: sess.get("https://jazzdrive.com.pk/", timeout=15))
            state    = random.randint(10000, 99999)
            auth_url = (f"https://jazzdrive.com.pk/oauth2/authorization.php"
                        f"?response_type=code&client_id=web&state={state}"
                        f"&redirect_uri=https://cloud.jazzdrive.com.pk/ui/html/oauth.html")
            res1     = await loop.run_in_executor(None, lambda: sess.get(auth_url, timeout=15, allow_redirects=True))
            signup_url = res1.url
            if "signup.php" not in signup_url: continue

            post_url = signup_url
            fa = re.search(r'<form[^>]+action\s*=\s*["\']([^"\']+)["\']', res1.text, re.I)
            if fa: post_url = urljoin(signup_url, fa.group(1).replace("&amp;","&"))

            payload = {}
            for m in re.finditer(r'<input([^>]+)>', res1.text, re.I):
                a = m.group(1)
                n = re.search(r'name\s*=\s*["\']([^"\']+)["\']', a, re.I)
                v = re.search(r'value\s*=\s*["\']([^"\']*)["\']', a, re.I)
                if n: payload[n.group(1)] = v.group(1) if v else ""
            payload["msisdn"] = phone
            payload.setdefault("submit", "submit")

            sess.headers.update({
                "Origin": "https://jazzdrive.com.pk", "Referer": signup_url,
                "Content-Type": "application/x-www-form-urlencoded"
            })
            res2       = await loop.run_in_executor(None, lambda: sess.post(post_url, data=payload, timeout=15, allow_redirects=True))
            verify_url = res2.url
            if "verify.php" in verify_url: break
        except Exception as e:
            if attempt == 2: return await msg.edit_text(f"❌ Network error: {e}")

    if "verify.php" not in verify_url:
        return await msg.edit_text("❌ Server rejected the number. Try again.")

    await msg.edit_text("🔑 OTP sent! Enter the 4-digit code:")
    user_states[uid]  = "WAITING_FOR_OTP"
    login_events[uid] = loop.create_future()

    try:
        otp = await asyncio.wait_for(login_events[uid], timeout=300)
    except asyncio.TimeoutError:
        user_states.pop(uid, None)
        return await msg.edit_text("⏳ Timed out. Use /login again.")

    await msg.edit_text("⚙️ Verifying OTP...")
    try:
        post_url_otp = verify_url
        fa = re.search(r'<form[^>]+action\s*=\s*["\']([^"\']+)["\']', res2.text, re.I)
        if fa: post_url_otp = urljoin(verify_url, fa.group(1).replace("&amp;","&"))

        otp_payload = {}
        for m in re.finditer(r'<input([^>]+)>', res2.text, re.I):
            a = m.group(1)
            n = re.search(r'name\s*=\s*["\']([^"\']+)["\']', a, re.I)
            v = re.search(r'value\s*=\s*["\']([^"\']*)["\']', a, re.I)
            if n: otp_payload[n.group(1)] = v.group(1) if v else ""
        otp_payload["otp"] = otp
        otp_payload.setdefault("submit", "submit")

        sess.headers["Referer"] = verify_url
        res3   = await loop.run_in_executor(None, lambda: sess.post(post_url_otp, data=otp_payload, timeout=15, allow_redirects=True))
        params = parse_qs(urlparse(res3.url).query)
        if "code" not in params: return await msg.edit_text("❌ Wrong OTP. Try /login again.")

        auth_code = params["code"][0]
        oauth_url = (f"https://cloud.jazzdrive.com.pk/sapi/login/oauth"
                     f"?action=login&platform=web&keytype=authorizationcode&key={auth_code}")
        res4    = await loop.run_in_executor(None, lambda: sess.get(oauth_url, timeout=15))
        val_key = None
        try: val_key = res4.json().get("data", {}).get("validationkey")
        except: pass
        if not val_key: val_key = sess.cookies.get("validationKey")

        if val_key:
            formatted = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in sess.cookies]
            if not any(c["name"] == "validationKey" for c in formatted):
                formatted.append({"name":"validationKey","value":val_key,"domain":"cloud.jazzdrive.com.pk","path":"/"})
            with open(cookie_file(uid), "w") as f: json.dump({"cookies": formatted}, f)
            await msg.edit_text("✅ Login successful! Session saved.")
        else:
            await msg.edit_text("❌ Could not extract session key. Try again.")
    except Exception as e:
        await msg.edit_text(f"❌ OTP error: {e}")

# ── COMMANDS ──────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in allowed_users: return
    await update.message.reply_text(
        "🤖 *JazzDrive Bot*\n\n"
        "*/login* — Login to JazzDrive (OTP)\n"
        "*/logout* — Logout / delete session\n\n"
        "*/link* `url - FileName - .ext` — Download & upload single file\n"
        "*/mlink* — Multiple links batch upload\n"
        "*/ziplink* `url - FolderName` — Download ZIP, extract all & upload\n\n"
        "*/cancel* `task_id` — Cancel a task\n"
        "*/cancelall* — Cancel all active tasks\n\n"
        "*/allow* `user_id` — *(Admin)* Authorize a user\n"
        "*/disallow* `user_id` — *(Admin)* Remove a user",
        parse_mode="Markdown"
    )

async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in allowed_users: return
    user_states[uid] = "WAITING_FOR_NUMBER"
    await update.message.reply_text("📱 Enter your Jazz number (03xxxxxxxxx):")

async def cmd_logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in allowed_users: return
    p = cookie_file(uid)
    if os.path.exists(p):
        os.remove(p)
        await update.message.reply_text("✅ Logged out. Session deleted.")
    else:
        await update.message.reply_text("⚠️ You are not logged in.")

async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in allowed_users: return
    text = update.message.text.replace("/link", "", 1).strip()
    if not text or " - " not in text:
        return await update.message.reply_text(
            "⚠️ *Format:* `/link https://url - FileName - .mkv`", parse_mode="Markdown"
        )
    try:
        parts    = text.split(" - ")
        filename = f"{parts[1].strip()}{parts[2].strip()}"
        job      = {"is_batch": False, "batch_name": filename,
                    "links": [{"url": parts[0].strip(), "filename": filename}]}
        try: await update.message.delete()
        except: pass
        await show_folder_picker(ctx.bot, uid, job, update)
    except:
        await update.message.reply_text("❌ Invalid format. Example:\n`/link https://... - Episode 1 - .mkv`",
                                        parse_mode="Markdown")

async def cmd_ziplink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in allowed_users: return
    text = update.message.text.replace("/ziplink", "", 1).strip()
    if not text or " - " not in text:
        return await update.message.reply_text(
            "⚠️ *Format:* `/ziplink https://url - FolderName`\n\nThis downloads a ZIP, extracts all files and uploads them.",
            parse_mode="Markdown"
        )
    try:
        parts       = text.split(" - ", 1)
        folder_name = parts[1].strip()
        job = {
            "is_batch": True, "is_zip": True,
            "batch_name": folder_name,
            "links": [{"url": parts[0].strip(), "filename": f"{folder_name}.zip"}]
        }
        try: await update.message.delete()
        except: pass
        await show_folder_picker(ctx.bot, uid, job, update)
    except:
        await update.message.reply_text("❌ Invalid format.")

async def cmd_mlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in allowed_users: return
    text  = update.message.text.replace("/mlink", "", 1).strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    links = []
    for line in lines:
        if " - " in line:
            try:
                p = line.split(" - ")
                links.append({"url": p[0].strip(), "filename": f"{p[1].strip()}{p[2].strip()}"})
            except: pass
    if not links:
        return await update.message.reply_text(
            "❌ No valid links.\n\n*Format (one per line):*\n`url - Name - .ext`", parse_mode="Markdown"
        )
    job = {"is_batch": True, "links": links}
    user_states[uid] = {"action": "WAITING_FOR_BATCH_NAME", "data": job}
    try: await update.message.delete()
    except: pass
    await update.message.reply_text(f"✅ {len(links)} links found. Send folder name:")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in allowed_users: return
    if not ctx.args:
        return await update.message.reply_text("⚠️ Format: `/cancel task_id`", parse_mode="Markdown")
    cancelled_tasks.add(ctx.args[0])
    await update.message.reply_text(f"🛑 Cancel signal sent for `{ctx.args[0]}`", parse_mode="Markdown")

async def cmd_cancelall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in allowed_users: return
    cancelled_tasks.add(f"all_{uid}")
    await update.message.reply_text("🛑 All tasks will be cancelled.")

async def cmd_allow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        new_uid = int(ctx.args[0])
        allowed_users.add(new_uid)
        save_allowed_users(allowed_users)
        await update.message.reply_text(f"✅ User `{new_uid}` authorized.", parse_mode="Markdown")
    except:
        await update.message.reply_text("⚠️ Format: `/allow user_id`", parse_mode="Markdown")

async def cmd_disallow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        rem_uid = int(ctx.args[0])
        if rem_uid == ADMIN_ID:
            return await update.message.reply_text("❌ Cannot remove yourself.")
        allowed_users.discard(rem_uid)
        save_allowed_users(allowed_users)
        await update.message.reply_text(f"🚫 User `{rem_uid}` removed.", parse_mode="Markdown")
    except:
        await update.message.reply_text("⚠️ Format: `/disallow user_id`", parse_mode="Markdown")

# ── TEXT HANDLER ──────────────────────────────────────────────
async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    if uid not in allowed_users: return
    state = user_states.get(uid)
    text  = update.message.text.strip()

    if state == "WAITING_FOR_NUMBER":
        user_states[uid] = None
        asyncio.create_task(do_login(ctx.bot, uid, text, update.message))

    elif state == "WAITING_FOR_OTP":
        user_states[uid] = None
        if uid in login_events and not login_events[uid].done():
            login_events[uid].set_result(text)

    elif isinstance(state, dict) and state.get("action") == "WAITING_FOR_BATCH_NAME":
        job = state["data"]
        job["batch_name"] = text
        user_states[uid]  = None
        try: await update.message.delete()
        except: pass
        await show_folder_picker(ctx.bot, uid, job, update)

# ── FOLDER PICKER ─────────────────────────────────────────────
async def show_folder_picker(bot, uid, job, update=None):
    cookies, key = load_cookies(uid)
    if not key:
        target = update.message if update else None
        if target: await target.reply_text("❌ Please /login first.")
        else: await bot.send_message(uid, "❌ Please /login first.")
        return

    msg  = await bot.send_message(uid, "🔎 Loading folders...")
    loop = asyncio.get_event_loop()
    folders, root_id = await loop.run_in_executor(None, get_cloud_folders, cookies, key)
    if not root_id:
        return await msg.edit_text("❌ Session expired. Please /login again.")

    job_id = str(uuid.uuid4())[:8]
    user_pending_jobs.setdefault(uid, {})[job_id] = job

    btns = [[InlineKeyboardButton("🏠 ROOT", callback_data=f"up_{root_id}_{job_id}")]]
    row  = []
    for name, fid in folders:
        row.append(InlineKeyboardButton(f"📁 {name}", callback_data=f"up_{fid}_{job_id}"))
        if len(row) == 2: btns.append(row); row = []
    if row: btns.append(row)

    await msg.edit_text(
        f"📂 Select destination for *{job.get('batch_name','Upload')}*:",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown"
    )

async def folder_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    if uid not in allowed_users:
        return await query.answer("Unauthorized", show_alert=True)

    parts     = query.data.split("_")
    folder_id = int(parts[1])
    job_id    = parts[2]

    job = user_pending_jobs.get(uid, {}).pop(job_id, None)
    if not job: return await query.answer("Session expired.", show_alert=True)

    try: await query.message.delete()
    except: pass
    await query.answer()

    asyncio.create_task(run_upload_job(ctx.bot, uid, job, folder_id))

# ── MAIN UPLOAD JOB ───────────────────────────────────────────
async def run_upload_job(bot, uid, job, parent_folder_id):
    cookies, key = load_cookies(uid)
    if not key:
        return await bot.send_message(uid, "❌ Session expired. Please /login again.")

    loop       = asyncio.get_event_loop()
    is_batch   = job.get("is_batch", False)
    is_zip     = job.get("is_zip", False)
    batch_name = job.get("batch_name", "Upload")
    links      = job.get("links", [])

    status_msg = await bot.send_message(uid, "🔄 Starting...")

    # ── ZIP EXTRACTION MODE ───────────────────────────────────
    if is_zip and links:
        zip_url  = links[0]["url"]
        zip_path = f"temp_zip_{uuid.uuid4().hex[:8]}.zip"
        task_id  = f"zip_{uid}_{uuid.uuid4().hex[:4]}"
        last_edit = [time.time()]

        async def zip_dl_progress(done, total):
            now = time.time()
            if now - last_edit[0] > 5:
                last_edit[0] = now
                pct = (done/total*100) if total else 0
                try:
                    await status_msg.edit_text(
                        f"⬇️ Downloading ZIP...\n{fmt_bytes(done)} / {fmt_bytes(total)} ({pct:.1f}%)"
                    )
                except: pass

        try:
            await status_msg.edit_text("⬇️ Downloading ZIP file...")
            await download_url(zip_url, zip_path, task_id, zip_dl_progress)
        except asyncio.CancelledError:
            return await status_msg.edit_text("🛑 Download cancelled.")
        except Exception as e:
            return await status_msg.edit_text(f"❌ Download failed: {e}")

        await status_msg.edit_text("📦 Extracting ZIP...")
        extract_dir = f"temp_extract_{uuid.uuid4().hex[:8]}"
        os.makedirs(extract_dir, exist_ok=True)

        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(extract_dir)
            os.remove(zip_path)
        except Exception as e:
            shutil.rmtree(extract_dir, ignore_errors=True)
            return await status_msg.edit_text(f"❌ Extraction failed: {e}")

        extracted = []
        for root, _, files in os.walk(extract_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                extracted.append((fname, fpath))
        extracted.sort(key=lambda x: x[0])

        if not extracted:
            shutil.rmtree(extract_dir, ignore_errors=True)
            return await status_msg.edit_text("❌ ZIP is empty or unsupported.")

        await status_msg.edit_text(f"📁 Creating folder '{batch_name}'...")
        folder_id = await loop.run_in_executor(None, create_folder, batch_name, parent_folder_id, cookies, key)

        results = []
        for i, (fname, fpath) in enumerate(extracted):
            safe_name = re.sub(r'[\\/*?:"<>|]', "", fname)
            tid       = f"zf{uid}{i}"

            if f"all_{uid}" in cancelled_tasks:
                results.append((safe_name, "cancelled"))
                continue

            try:
                await status_msg.edit_text(
                    f"⬆️ Uploading {i+1}/{len(extracted)}\n📄 {safe_name}"
                )
                success, file_id = await loop.run_in_executor(
                    None, upload_file, fpath, safe_name, folder_id, cookies, key, tid
                )
                if success and file_id:
                    fsize = os.path.getsize(fpath)
                    wait  = index_wait(fsize)
                    await status_msg.edit_text(f"⏳ Indexing {safe_name} ({wait}s)...")
                    await asyncio.sleep(wait)
                    link = await loop.run_in_executor(None, generate_share_link, file_id, False, cookies, key)
                    results.append((safe_name, link or "❌ link failed"))
                else:
                    results.append((safe_name, "❌ upload failed"))
            except Exception as e:
                results.append((safe_name, f"❌ {e}"))
            finally:
                try: os.remove(fpath)
                except: pass

        shutil.rmtree(extract_dir, ignore_errors=True)

        folder_link = await loop.run_in_executor(None, generate_share_link, folder_id, True, cookies, key)

        summary = f"✅ *ZIP Upload Done!*\n📁 *{batch_name}*\n━━━━━━━━━━━━━━━\n\n"
        for fname, link in results:
            summary += f"📄 {fname}\n🔗 {link}\n\n"
        summary += f"━━━━━━━━━━━━━━━\n🔗 *Folder:* {folder_link or 'N/A'}"

        try: await status_msg.delete()
        except: pass
        for chunk in [summary[i:i+4000] for i in range(0, len(summary), 4000)]:
            await bot.send_message(uid, chunk, parse_mode="Markdown", disable_web_page_preview=True)
        return

    # ── NORMAL LINK MODE ──────────────────────────────────────
    target_folder_id = parent_folder_id
    if is_batch:
        await status_msg.edit_text(f"📁 Creating folder '{batch_name}'...")
        target_folder_id = await loop.run_in_executor(
            None, create_folder, batch_name, parent_folder_id, cookies, key
        )

    results = []
    for i, item in enumerate(links):
        url       = item["url"]
        filename  = re.sub(r'[\\/*?:"<>|]', "", item["filename"])
        tid       = f"t{uid}{i}_{uuid.uuid4().hex[:4]}"
        local_path = f"temp_{tid}_{filename}"

        if f"all_{uid}" in cancelled_tasks:
            results.append((filename, "cancelled"))
            continue

        last_edit = [time.time()]

        async def dl_prog(done, total, fn=filename, idx=i):
            now = time.time()
            if now - last_edit[0] > 5:
                last_edit[0] = now
                pct = (done/total*100) if total else 0
                try:
                    await status_msg.edit_text(
                        f"⬇️ {idx+1}/{len(links)} — {fn}\n"
                        f"{fmt_bytes(done)} / {fmt_bytes(total)} ({pct:.1f}%)"
                    )
                except: pass

        try:
            await status_msg.edit_text(f"⬇️ Downloading {i+1}/{len(links)}\n📄 {filename}")
            await download_url(url, local_path, tid, dl_prog)
        except asyncio.CancelledError:
            results.append((filename, "cancelled"))
            continue
        except Exception as e:
            results.append((filename, f"❌ download failed: {e}"))
            continue

        fsize = os.path.getsize(local_path)
        try:
            await status_msg.edit_text(f"⬆️ Uploading {i+1}/{len(links)}\n📄 {filename}")
            success, file_id = await loop.run_in_executor(
                None, upload_file, local_path, filename, target_folder_id, cookies, key, tid
            )
            if success and file_id:
                wait = index_wait(fsize)
                await status_msg.edit_text(f"⏳ Indexing {filename} ({wait}s)...")
                await asyncio.sleep(wait)
                link = await loop.run_in_executor(None, generate_share_link, file_id, False, cookies, key)
                results.append((filename, link or "❌ link failed"))
            else:
                results.append((filename, "❌ upload failed"))
        except Exception as e:
            results.append((filename, f"❌ {e}"))
        finally:
            try: os.remove(local_path)
            except: pass

    summary = f"✅ *Upload Complete!*\n━━━━━━━━━━━━━━━\n\n"
    for fname, link in results:
        summary += f"📄 {fname}\n🔗 {link}\n\n"

    if is_batch:
        folder_link = await loop.run_in_executor(
            None, generate_share_link, target_folder_id, True, cookies, key
        )
        summary += f"━━━━━━━━━━━━━━━\n🔗 *Folder:* {folder_link or 'N/A'}"

    try: await status_msg.delete()
    except: pass
    for chunk in [summary[i:i+4000] for i in range(0, len(summary), 4000)]:
        await bot.send_message(uid, chunk, parse_mode="Markdown", disable_web_page_preview=True)

# ── SESSION PING ──────────────────────────────────────────────
async def ping_sessions():
    while True:
        interval = bot_settings.get("ping_interval", 5)
        await asyncio.sleep(interval * 60)
        loop = asyncio.get_event_loop()
        for uid in list(allowed_users):
            c, k = load_cookies(uid)
            if c and k:
                try: await loop.run_in_executor(None, get_cloud_folders, c, k)
                except: pass

# ── MAIN ──────────────────────────────────────────────────────
async def post_init(application):
    asyncio.create_task(ping_sessions())

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("login",     cmd_login))
    app.add_handler(CommandHandler("logout",    cmd_logout))
    app.add_handler(CommandHandler("link",      cmd_link))
    app.add_handler(CommandHandler("ziplink",   cmd_ziplink))
    app.add_handler(CommandHandler("mlink",     cmd_mlink))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("cancelall", cmd_cancelall))
    app.add_handler(CommandHandler("allow",     cmd_allow))
    app.add_handler(CommandHandler("disallow",  cmd_disallow))
    app.add_handler(CallbackQueryHandler(folder_callback, pattern=r"^up_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("✅ JazzDrive Bot v6 started! (No API_ID/HASH needed)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
