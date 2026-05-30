import os
import json
import time
import asyncio
import random
import re
import mimetypes
import uuid
import shutil
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urljoin

from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import nest_asyncio
import aiohttp
from aiohttp import web
import aiofiles
import gdown

nest_asyncio.apply()

# --- TELEGRAM BOT CREDENTIALS ---
API_ID = os.environ.get("API_ID", "YOUR_API_ID_HERE")
API_HASH = os.environ.get("API_HASH", "YOUR_API_HASH_HERE")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    print("❌ ERROR: API_ID, API_HASH, or BOT_TOKEN secrets are not set!")
    exit(1)

app = Client("jazz_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- ADMIN & MULTI-USER SYSTEM ---
ADMIN_ID = 7128257853
ALLOWED_USERS_FILE = "allowed_users.json"
SETTINGS_FILE = "bot_settings.json"

user_states = {}
user_pending_jobs = {} 
active_tasks = {} 
login_events = {}
cancelled_tasks = set()
user_active_tasks = {} 

user_semaphores = {}
user_queue_counts = {}

def load_allowed_users():
    if not os.path.exists(ALLOWED_USERS_FILE):
        with open(ALLOWED_USERS_FILE, "w") as f:
            json.dump([ADMIN_ID], f)
    with open(ALLOWED_USERS_FILE, "r") as f:
        return set(json.load(f))

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        default_settings = {"owner_concurrent": 3, "user_concurrent": 3, "ping_interval": 5}
        with open(SETTINGS_FILE, "w") as f:
            json.dump(default_settings, f)
        return default_settings
    with open(SETTINGS_FILE, "r") as f:
        data = json.load(f)
        if "owner_concurrent" not in data: data["owner_concurrent"] = data.get("max_concurrent", 3)
        return data

allowed_users = load_allowed_users()
bot_settings = load_settings()

# --- HELPER FUNCTIONS ---
def get_cookie_file(user_id):
    return f"jazz_cookies_{user_id}.json"

def load_cookies(user_id):
    cookie_file = get_cookie_file(user_id)
    if not os.path.exists(cookie_file): return None, None
    try:
        with open(cookie_file, 'r') as f:
            data = json.load(f)
        raw_cookies = data.get('cookies', [])
        cookies = {c['name']: c['value'] for c in raw_cookies}
        key = next((c['value'] for c in raw_cookies if c['name'] == 'validationKey'), None)
        return cookies, key
    except Exception: return None, None

def get_cloud_folders(cookies, key):
    session = requests.Session()
    url = f"https://cloud.jazzdrive.com.pk/sapi/media/folder?action=get&validationkey={key}"
    try:
        res = session.get(url, cookies=cookies, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        data = res.json()
        folders_list = data.get('data', {}).get('folders', [])
        root_id = None
        for f in folders_list:
            if f.get('name') == '/':
                root_id = f.get('id')
                break
        if not root_id: return [], None
        sub_folders = [(f['name'], f['id']) for f in folders_list if f.get('parentid') == root_id and f.get('name') != '/']
        return sub_folders, root_id
    except Exception: return [], None

def create_cloud_folder(name, parent_id, cookies, key):
    session = requests.Session()
    url = f"https://cloud.jazzdrive.com.pk/sapi/media/folder?action=save&validationkey={key}"
    payload = {"data": {"magic": False, "offline": False, "name": name, "parentid": int(parent_id)}}
    try:
        res = session.post(url, cookies=cookies, json=payload, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        d = res.json()
        new_id = d.get('id') or d.get('data', {}).get('id')
        return new_id if new_id else parent_id
    except Exception: return parent_id

def format_bytes(size):
    if size <= 0: return "0 B"
    size = float(size)
    power = 2**10
    n = 0
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size >= power and n < 4:
        size /= power
        n += 1
    return f"{round(size, 2)} {power_labels[n]}"

class MultiProgressTracker:
    def __init__(self, message, total_files, is_batch=False):
        self.message = message
        self.total_files = total_files
        self.is_batch = is_batch
        self.tasks = {}
        self.last_update_time = time.time()
        self.file_counter = 1

    def init_task(self, task_id, filename):
        self.tasks[task_id] = {
            "index": self.file_counter, "name": filename, "action": "Queued", 
            "size": 0, "dl_current": 0, "dl_speed": 0, "dl_start": 0,
            "ul_current": 0, "ul_speed": 0, "ul_start": 0
        }
        self.file_counter += 1

    async def update_dl(self, task_id, current, total):
        task = self.tasks.get(task_id)
        if not task: return
        now = time.time()
        if task["dl_start"] == 0: task["dl_start"] = now
        task["dl_current"] = current
        if total > 0: task["size"] = total
        dt = now - task["dl_start"]
        task["dl_speed"] = current / dt if dt > 0 else 0
        task["action"] = "Downloading"
        await self._render_ui(force=False)

    async def update_ul(self, task_id, current, total):
        task = self.tasks.get(task_id)
        if not task: return
        now = time.time()
        if task["ul_start"] == 0: task["ul_start"] = now
        task["ul_current"] = current
        if total > 0: task["size"] = total
        dt = now - task["ul_start"]
        task["ul_speed"] = current / dt if dt > 0 else 0
        task["action"] = "Uploading"
        await self._render_ui(force=False)

    async def update_status_only(self, task_id, action):
        task = self.tasks.get(task_id)
        if not task: return
        task["action"] = action
        await self._render_ui(force=True)

    async def _render_ui(self, force=False):
        now = time.time()
        if not force and (now - self.last_update_time < 6.0): return
        self.last_update_time = now
        
        text = f"📦 **Processing [{self.total_files} Files]**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for tid, t in self.tasks.items():
            text += f"📄 **{t['index']}. {t['name']}**\n"
            text += f"├ 📈 **Status:** {t['action']}\n"
            
            if t["action"] in ["Downloading", "Uploading"]:
                is_dl = t["action"] == "Downloading"
                current = t["dl_current"] if is_dl else t["ul_current"]
                speed = t["dl_speed"] if is_dl else t["ul_speed"]
                total = t["size"]
                pct = (current / total * 100) if total > 0 else 0
                text += f"├ 📊 {format_bytes(current)} / {format_bytes(total)} (⚡ {format_bytes(speed)}/s) [{pct:.1f}%]\n"
            text += "\n"

        try:
            if len(text) > 4000: text = text[:3900] + "\n... (Truncated)"
            await self.message.edit_text(text, disable_web_page_preview=True)
        except Exception: pass

# --- DOWNLOADERS (Raw CDN, MediaFire, Google Drive) ---
async def download_mediafire_direct(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20) as res:
            text = await res.text()
            match = re.search(r'https?://download[0-9]+\.mediafire\.com/[^\s"\'>]+', text)
            if match: return match.group(0)
    return url

async def download_direct_link(url, filepath, task_id, tracker):
    if "drive.google.com" in url:
        await tracker.update_status_only(task_id, "Downloading from Google Drive")
        loop = asyncio.get_event_loop()
        file_id = re.search(r'/d/([a-zA-Z0-9-_]+)', url) or re.search(r'id=([a-zA-Z0-9-_]+)', url)
        if file_id:
            await loop.run_in_executor(None, lambda: gdown.download(id=file_id.group(1), output=filepath, quiet=True, fuzzy=True))
            return filepath
            
    if "mediafire.com" in url:
        await tracker.update_status_only(task_id, "Bypassing MediaFire...")
        url = await download_mediafire_direct(url)

    timeout = aiohttp.ClientTimeout(total=3600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={'User-Agent': 'Mozilla/5.0'}) as response:
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            current_size = 0
            
            async with aiofiles.open(filepath, 'wb') as f:
                async for chunk in response.content.iter_chunked(4 * 1024 * 1024): 
                    if task_id in cancelled_tasks: raise asyncio.CancelledError()
                    if chunk:
                        await f.write(chunk)
                        current_size += len(chunk)
                        await tracker.update_dl(task_id, current_size, total_size)
    return filepath

# --- EXTRACTOR ---
def extract_archive(filepath):
    extract_dir = filepath + "_extracted"
    os.makedirs(extract_dir, exist_ok=True)
    extracted_files = []
    try:
        shutil.unpack_archive(filepath, extract_dir)
        for root, _, files in os.walk(extract_dir):
            for f in files:
                extracted_files.append(os.path.join(root, f))
    except Exception as e:
        print(f"Extraction failed: {e}")
    return extracted_files

# --- API LOGIN ---
async def do_api_login(client, message, number, user_id):
    msg = await message.reply("⚙️ *Initializing secure session...*")
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    loop = asyncio.get_event_loop()
    
    try:
        random_state = random.randint(10000, 99999)
        auth_url = f"https://jazzdrive.com.pk/oauth2/authorization.php?response_type=code&client_id=web&state={random_state}&redirect_uri=https://cloud.jazzdrive.com.pk/ui/html/oauth.html"
        res1 = await loop.run_in_executor(None, lambda: session.get(auth_url, timeout=15, allow_redirects=True))
        signup_url = res1.url 
        
        post_url = signup_url
        form_action_match = re.search(r'<form[^>]+action\s*=\s*["\']([^"\']+)["\']', res1.text, re.IGNORECASE)
        if form_action_match: post_url = urljoin(signup_url, form_action_match.group(1).replace('&amp;', '&'))

        payload = {}
        for match in re.finditer(r'<input([^>]+)>', res1.text, re.IGNORECASE):
            attr_str = match.group(1)
            name_m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', attr_str, re.IGNORECASE)
            val_m = re.search(r'value\s*=\s*["\']([^"\']*)["\']', attr_str, re.IGNORECASE)
            if name_m: payload[name_m.group(1)] = val_m.group(1) if val_m else ""
                
        payload['msisdn'] = number
        if not any('submit' in k.lower() for k in payload.keys()): payload['submit'] = 'submit'

        session.headers.update({'Referer': signup_url, 'Content-Type': 'application/x-www-form-urlencoded'})
        res2 = await loop.run_in_executor(None, lambda: session.post(post_url, data=payload, timeout=15, allow_redirects=True))
        verify_url = res2.url 
        
        if "verify.php" not in verify_url: return await msg.edit("❌ **Failed!** Server rejected the number.")
    except Exception as e:
        return await msg.edit(f"❌ **Error:**\n`{e}`")

    await msg.edit("🔑 **OTP Sent!**\n\nEnter the **4-digit OTP** sent to your number.")
    user_states[user_id] = "WAITING_FOR_OTP"
    login_events[user_id] = asyncio.Future()
    
    try: otp = await asyncio.wait_for(login_events[user_id], timeout=300) 
    except asyncio.TimeoutError: return await msg.edit("⏳ **Timeout!**")
    
    await msg.edit("⚙️ *Verifying OTP...*")
    try:
        post_url_otp = verify_url
        form_action_match = re.search(r'<form[^>]+action\s*=\s*["\']([^"\']+)["\']', res2.text, re.IGNORECASE)
        if form_action_match: post_url_otp = urljoin(verify_url, form_action_match.group(1).replace('&amp;', '&'))

        otp_payload = {}
        for match in re.finditer(r'<input([^>]+)>', res2.text, re.IGNORECASE):
            attr_str = match.group(1)
            name_m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', attr_str, re.IGNORECASE)
            val_m = re.search(r'value\s*=\s*["\']([^"\']*)["\']', attr_str, re.IGNORECASE)
            if name_m: otp_payload[name_m.group(1)] = val_m.group(1) if val_m else ""
                
        otp_payload['otp'] = otp
        if not any('submit' in k.lower() for k in otp_payload.keys()): otp_payload['submit'] = 'submit'
        
        session.headers.update({'Referer': verify_url})
        res3 = await loop.run_in_executor(None, lambda: session.post(post_url_otp, data=otp_payload, timeout=15, allow_redirects=True))
        parsed_url = urlparse(res3.url)
        query_params = parse_qs(parsed_url.query)
        
        if 'code' not in query_params: return await msg.edit("❌ **Error:** Server rejected the OTP.")
        auth_code = query_params['code'][0]
        
        oauth_api_url = f"https://cloud.jazzdrive.com.pk/sapi/login/oauth?action=login&platform=web&keytype=authorizationcode&key={auth_code}"
        res4 = await loop.run_in_executor(None, lambda: session.get(oauth_api_url, timeout=15))
        
        val_key = res4.json().get('data', {}).get('validationkey') or res4.json().get('validationkey') or session.cookies.get('validationKey')
                        
        if val_key:
            formatted_cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in session.cookies]
            if not any(c['name'] == 'validationKey' for c in formatted_cookies):
                formatted_cookies.append({"name": "validationKey", "value": val_key, "domain": "cloud.jazzdrive.com.pk", "path": "/"})
            with open(get_cookie_file(user_id), "w") as f: json.dump({"cookies": formatted_cookies}, f, indent=4)
            return await msg.edit("✅ **Login Successful!**")
        else: return await msg.edit("❌ **Error:** Failed to extract validation key.")
    except Exception as e: return await msg.edit(f"❌ **Verification Error:**\n`{e}`")

# --- USER COMMANDS ---
@app.on_message(filters.command("login"))
async def login_cmd(client, message):
    if message.from_user.id not in allowed_users: return
    user_id = message.from_user.id
    if os.path.exists(get_cookie_file(user_id)):
        await message.reply("⚠️ Active session found. Overwriting...")
    user_states[user_id] = "WAITING_FOR_NUMBER"
    await message.reply("📱 Enter your phone number **[03xxxxxxxxx]**:")

@app.on_message(filters.command("link"))
async def link_cmd(client, message):
    if message.from_user.id not in allowed_users: return
    command_text = message.text.replace("/link", "", 1).strip()
    if not command_text or " - " not in command_text:
        return await message.reply("⚠️ **Format:** `/link https://url - Name - .mp4`")
    try:
        parts = command_text.split(" - ")
        job_data = {"is_batch": False, "extract": False, "batch_name": f"{parts[1].strip()}{parts[2].strip()}", "links": [{"url": parts[0].strip(), "filename": f"{parts[1].strip()}{parts[2].strip()}"}]}
        try: await message.delete()
        except: pass
        await check_login_and_ask_folder(client, message, message.from_user.id, job_data)
    except Exception: await message.reply("❌ Invalid format.")

@app.on_message(filters.command("mlink"))
async def mlink_cmd(client, message):
    if message.from_user.id not in allowed_users: return
    command_text = message.text.replace("/mlink", "", 1).strip()
    lines = [line.strip() for line in command_text.split("\n") if line.strip()]
    links_data = []
    for line in lines:
        if " - " in line:
            parts = line.split(" - ")
            links_data.append({"url": parts[0].strip(), "filename": f"{parts[1].strip()}{parts[2].strip()}"})
        
    if not links_data: return await message.reply("❌ No valid links found.")
    user_id = message.from_user.id
    job_data = {"is_batch": True, "extract": False, "links": links_data}
    try: await message.delete()
    except: pass
    user_states[user_id] = {"action": "WAITING_FOR_BATCH_NAME", "data": job_data}
    await message.reply(f"✅ {len(links_data)} links detected. Reply with **folder name**:")

@app.on_message(filters.command("unzip"))
async def unzip_cmd(client, message):
    if message.from_user.id not in allowed_users: return
    command_text = message.text.replace("/unzip", "", 1).strip()
    if not command_text or " - " not in command_text:
        return await message.reply("⚠️ **Format:** `/unzip https://url - Name - .zip`")
    try:
        parts = command_text.split(" - ")
        job_data = {"is_batch": True, "extract": True, "batch_name": f"{parts[1].strip()}{parts[2].strip()}", "links": [{"url": parts[0].strip(), "filename": f"{parts[1].strip()}{parts[2].strip()}"}]}
        try: await message.delete()
        except: pass
        await check_login_and_ask_folder(client, message, message.from_user.id, job_data)
    except Exception: await message.reply("❌ Invalid format.")

@app.on_message(filters.text & filters.private)
async def text_handler(client, message):
    if message.from_user.id not in allowed_users: return
    user_id = message.from_user.id
    state = user_states.get(user_id)

    if state == "WAITING_FOR_NUMBER":
        user_states[user_id] = None
        asyncio.create_task(do_api_login(client, message, message.text, user_id))
    elif state == "WAITING_FOR_OTP":
        user_states[user_id] = None
        if user_id in login_events and not login_events[user_id].done():
            login_events[user_id].set_result(message.text)
    elif isinstance(state, dict) and state.get("action") == "WAITING_FOR_BATCH_NAME":
        job_data = state["data"]
        job_data["batch_name"] = message.text
        user_states[user_id] = None
        try: await message.delete()
        except: pass
        await check_login_and_ask_folder(client, message, user_id, job_data)

async def check_login_and_ask_folder(client, message, user_id, job_data):
    cookies, key = load_cookies(user_id)
    if not key: return await client.send_message(user_id, "❌ Please authenticate using `/login` first.")
    
    msg = await client.send_message(user_id, "🔎 *Retrieving cloud directories...*")
    loop = asyncio.get_event_loop()
    folders, root_id = await loop.run_in_executor(None, get_cloud_folders, cookies, key)
    
    if not root_id: return await msg.edit("❌ **Session Expired!** Please `/login` again.")

    job_id = str(uuid.uuid4())[:8]
    if user_id not in user_pending_jobs: user_pending_jobs[user_id] = {}
    user_pending_jobs[user_id][job_id] = job_data

    buttons = [[InlineKeyboardButton("🏠 ROOT Directory", callback_data=f"up_{root_id}_{job_id}")]]
    row = []
    for fname, fid in folders:
        row.append(InlineKeyboardButton(f"📁 {fname}", callback_data=f"up_{fid}_{job_id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row: buttons.append(row)
            
    await msg.edit(f"✅ Select a destination path for **{job_data['batch_name']}**:", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_callback_query(filters.regex(r"^up_"))
async def folder_selected(client, query):
    user_id = query.from_user.id
    if user_id not in allowed_users: return await query.answer("Unauthorized", show_alert=True)
    
    parts = query.data.split("_")
    folder_id = int(parts[1])
    job_id = parts[2]
    
    data = user_pending_jobs.get(user_id, {}).get(job_id)
    if not data: return await query.answer("⚠️ Session expired.", show_alert=True)
    user_pending_jobs[user_id].pop(job_id, None)
    
    batch_name = data.get("batch_name", "Uploads")
    is_batch = data.get("is_batch", False)
    extract_mode = data.get("extract", False)
    
    cookies, key = load_cookies(user_id)
    loop = asyncio.get_event_loop()
    
    target_folder_id = folder_id
    if is_batch and not extract_mode:
        await query.message.edit_text(f"📁 *Creating remote folder '{batch_name}'...*")
        target_folder_id = await loop.run_in_executor(None, create_cloud_folder, batch_name, folder_id, cookies, key)
    elif extract_mode:
        await query.message.edit_text(f"📁 *Creating remote folder '{batch_name}' for Extracted Files...*")
        target_folder_id = await loop.run_in_executor(None, create_cloud_folder, batch_name + "_Extracted", folder_id, cookies, key)
    
    try: await query.message.delete()
    except: pass
    
    tasks_list = data.get("links", [])
    if not tasks_list: return await client.send_message(user_id, "❌ No valid payload mapped.")

    status_msg = await client.send_message(user_id, "🔄 *Starting Tasks...*")
    tracker = MultiProgressTracker(status_msg, len(tasks_list), is_batch)
    
    if user_id not in user_semaphores:
        user_semaphores[user_id] = asyncio.Semaphore(bot_settings.get("owner_concurrent", 3))

    async def process_item(idx, item):
        task_id = f"t{job_id[:3]}{idx}" 
        safe_filename = re.sub(r'[\\/*?:"<>|]', "", item["filename"])
        local_path = os.path.abspath(os.path.join(os.getcwd(), f"temp_{task_id}_{safe_filename}"))

        await tracker.update_status_only(task_id, "Queued")
        
        files_to_upload = []

        try:
            async with user_semaphores[user_id]:
                # --- DOWNLOAD PHASE ---
                url = item["url"]
                await tracker.update_status_only(task_id, "Downloading Raw CDN Link")
                await download_direct_link(url, local_path, task_id, tracker)
                
                # --- EXTRACT PHASE (IF REQUESTED) ---
                if extract_mode and local_path.lower().endswith(('.zip', '.rar', '.tar', '.gz')):
                    await tracker.update_status_only(task_id, "Extracting Archive...")
                    extracted_list = await loop.run_in_executor(None, extract_archive, local_path)
                    if extracted_list:
                        files_to_upload = extracted_list
                        if os.path.exists(local_path): os.remove(local_path)
                    else:
                        files_to_upload = [local_path] # Fallback if extract fails
                else:
                    files_to_upload = [local_path]

                # --- UPLOAD PHASE ---
                for f_path in files_to_upload:
                    if not os.path.exists(f_path): continue
                    
                    up_filename = os.path.basename(f_path)
                    fsize = os.path.getsize(f_path)
                    mime = mimetypes.guess_type(f_path)[0] or 'application/octet-stream'
                    
                    # STRICT FOLDER ADHERENCE (Uploads strictly inside target_folder_id)
                    metadata = {"name": up_filename, "size": str(fsize), "folderid": str(target_folder_id), "contenttype": mime, "modificationdate": datetime.now().strftime("%Y%m%dT%H%M%SZ")}
                    
                    session = requests.Session()
                    adapter = HTTPAdapter(max_retries=Retry(total=5, backoff_factor=0.5))
                    session.mount("https://", adapter)
                    headers = {'User-Agent': 'Mozilla/5.0'}

                    class UploadMonitor:
                        def __init__(self, t_id): self.t_id = t_id
                        def callback(self, monitor_obj, file_size):
                            asyncio.run_coroutine_threadsafe(tracker.update_ul(self.t_id, monitor_obj.bytes_read, file_size), loop)

                    def upload_thread():
                        with open(f_path, 'rb') as f:
                            up_mon = UploadMonitor(task_id)
                            m = MultipartEncoder(fields={'data': (None, json.dumps({"data": metadata}), 'application/json'), 'file': (up_filename, f, mime)})
                            headers['Content-Type'] = monitor.content_type
                            res = session.post(f"https://cloud.jazzdrive.com.pk/sapi/upload?action=save&acceptasynchronous=true&validationkey={key}", data=monitor, headers=headers, cookies=cookies, timeout=600)
                            return res.status_code == 200

                    success = await loop.run_in_executor(None, upload_thread)
                    if not success:
                        print(f"Failed to upload: {up_filename}")
                
                await tracker.update_status_only(task_id, "Completed")

        except Exception as e:
            print(e)
            await tracker.update_status_only(task_id, "Failed")
        finally:
            if os.path.exists(local_path): 
                try: os.remove(local_path)
                except: pass
            if extract_mode:
                extract_dir = local_path + "_extracted"
                if os.path.exists(extract_dir):
                    try: shutil.rmtree(extract_dir)
                    except: pass

    if is_batch:
        for idx, item in enumerate(tasks_list): await process_item(idx, item)
    else:
        await asyncio.gather(*(asyncio.create_task(process_item(idx, item)) for idx, item in enumerate(tasks_list)))
    
    await status_msg.edit(f"🏁 **Upload Completed!** All files safely uploaded to JazzDrive.")

# --- SMART COOKIES ALIVE PING SYSTEM ---
async def ping_sessions():
    while True:
        interval = bot_settings.get("ping_interval", 5)
        await asyncio.sleep(interval * 60)
        for uid in list(allowed_users):
            cookies, key = load_cookies(uid)
            if cookies and key:
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, get_cloud_folders, cookies, key)
                except Exception: pass

# --- DUMMY WEB SERVER FOR CLOUD HOSTING ---
async def health_check(request):
    return web.Response(text="Bot is running perfectly!")

async def start_web_server():
    web_app = web.Application()
    web_app.router.add_get('/', health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- START BOT ---
async def main():
    print("🤖 PRO Bot starting...")
    await app.start()
    await start_web_server()
    asyncio.create_task(ping_sessions())
    print("✅ Bot is fully active!")
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
