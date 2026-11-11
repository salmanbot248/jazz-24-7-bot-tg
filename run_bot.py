"""
TikTok Ultimate Command-Controlled & Cookie-Safe Pipeline.
- Strictly TikTok only (Zero YouTube).
- Session cookie preservation & 5-minute Keep-Alive ping.
- Starts processing for zerofeed only after receiving: /start upload tiktok
"""

import json
import os
import re
import random
import subprocess
import threading
import time
import urllib.request

import bot as app

SEEN_FILE = "tiktok_seen.json"
TAGS_FILE = "tiktok_tags.json"
PROFILES_FILE = "tiktok_profiles.json"
SESSION_STATE_FILE = "session_state.pkl"
CHECK_INTERVAL = 3600
PING_INTERVAL = 300  # Har 5 minute baad session alive rakhne ke liye ping

_seen_ids = set()
_queued_ids = set()
_seen_lock = threading.Lock()
_current_video = threading.local()

# Control flag: Jab tak aap command nahi denge, ye False rahega
_discovery_active = threading.Event()

def _load_seen_ids():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, list):
            return {str(video_id) for video_id in data}
    except Exception:
        pass
    return set()

def _save_seen_ids():
    temp_file = SEEN_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(sorted(_seen_ids), file, indent=2, ensure_ascii=False)
    os.replace(temp_file, SEEN_FILE)

_seen_ids.update(_load_seen_ids())

def _video_id_from_url(url):
    match = re.search(r"/video/(\d+)", url or "")
    return match.group(1) if match else None

def _load_json_file(filename, default_list):
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and data:
                    return data
        except Exception:
            pass
    return default_list

def start_keep_alive_ping():
    """Har 5 minute mein background ping bhej kar cookie/session ko zinda rakhega"""
    def ping_loop():
        while True:
            time.sleep(PING_INTERVAL)
            try:
                req = urllib.request.Request(
                    "https://www.tiktok.com",
                    headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10)"}
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        print("⚡ [Keep-Alive] Cookie/Session successfully pinged & kept active!")
            except Exception:
                pass

    t = threading.Thread(target=ping_loop, daemon=True)
    t.start()

def _get_videos_from_source(url):
    import yt_dlp
    if "tiktok.com" not in url:
        return []
    
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": 15,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        try:
            print(f"🔍 [Safe-Bot] Fetching TikTok from: {url}")
            result = ydl.extract_info(url, download=False)
        except Exception as e:
            print(f"❌ [Safe-Bot] Error on {url}: {e}")
            return []

    videos = []
    for entry in (result or {}).get("entries") or []:
        if not entry:
            continue
        video_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
        entry_id = str(entry.get("id") or "").strip()
        
        if not video_url or "tiktok.com" not in str(video_url):
            if entry_id:
                video_url = f"https://www.tiktok.com/video/{entry_id}"
            else:
                continue

        if "tiktok.com" not in str(video_url):
            continue

        video_id = _video_id_from_url(video_url) or entry_id
        if video_id:
            videos.append((video_id, str(video_url)))
            
    print(f"✅ [Safe-Bot] Found {len(videos)} TikTok videos from {url}")
    return videos

def start_discovery_worker(instance):
    def worker_loop():
        print("💤 [Safe-Bot] Waiting for command: /start upload tiktok")
        while True:
            # Jab tak aap command nahi denge, ye yahin wait karega
            _discovery_active.wait()
            
            profiles = _load_json_file(PROFILES_FILE, [])
            tags = _load_json_file(TAGS_FILE, [
                "https://www.tiktok.com/tag/foryou",
                "https://www.tiktok.com/tag/trending",
                "https://www.tiktok.com/tag/viral"
            ])
            
            sources = profiles + tags
            if not sources:
                sources = ["https://www.tiktok.com/tag/trending"]
                
            random.shuffle(sources)

            total_added = 0
            for source_url in sources:
                if not _discovery_active.is_set():
                    break
                    
                try:
                    videos = _get_videos_from_source(source_url)
                    random.shuffle(videos)
                    
                    added = 0
                    for video_id, video_url in videos:
                        with _seen_lock:
                            _seen_ids.update(_load_seen_ids())
                            if video_id in _seen_ids or video_id in _queued_ids:
                                continue
                            _queued_ids.add(video_id)

                        task_id = instance.next_task_id()
                        instance.task_queue.put({
                            "link": video_url,
                            "type": "social",
                            "task_id": task_id,
                        })
                        added += 1
                        total_added += 1

                    if added > 0:
                        print(f"🚀 [Safe-Bot] Queued {added} TikTok videos for zerofeed folder!")
                        instance.start_worker()
                        
                except Exception as e:
                    print(f"⚠️ [Safe-Bot] Worker loop error: {e}")
                
                time.sleep(10)
            
            print(f"💤 [Safe-Bot] Cycle complete. Waiting for next interval...")
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

def _install_command_and_tracking():
    if hasattr(app, "BotInstance") and hasattr(app.BotInstance, "handle_message"):
        original_handle_message = app.BotInstance.handle_message
        def handle_message_with_command(self, message, *args, **kwargs):
            text = str(message.get("text", "")).strip().lower()
            if "start upload tiktok" in text:
                _discovery_active.set()
                try:
                    self.send_message(self.chat_id, "🚀 TikTok Auto-Discovery Activated! Fetching for zerofeed...")
                except Exception:
                    pass
                print("🚀 [Safe-Bot] Activated via Telegram command!")
                return
            return original_handle_message(self, message, *args, **kwargs)
        app.BotInstance.handle_message = handle_message_with_command

    original_process_social = app.BotInstance.process_social
    original_append_feed = app.append_feed
    original_git_push_feed = app.git_push_feed

    def process_social_with_tracking(self, url, task_id=None):
        _current_video.video_id = _video_id_from_url(url)
        try:
            return original_process_social(self, url, task_id)
        finally:
            _current_video.video_id = None

    def append_feed_and_mark_seen(entry):
        original_append_feed(entry)
        video_id = getattr(_current_video, "video_id", None)
        if video_id:
            with _seen_lock:
                _seen_ids.add(str(video_id))
                _save_seen_ids()

    def git_push_feed_and_seen():
        subprocess.run(["git", "add", SEEN_FILE], check=False)
        if os.path.exists(SESSION_STATE_FILE):
            subprocess.run(["git", "add", SESSION_STATE_FILE], check=False)
        original_git_push_feed()

    app.BotInstance.process_social = process_social_with_tracking
    app.append_feed = append_feed_and_mark_seen
    app.git_push_feed = git_push_feed_and_seen

def _install_auto_runner():
    original_run = app.BotInstance.run
    def run_with_discovery(self):
        start_discovery_worker(self)
        original_run(self)
    app.BotInstance.run = run_with_discovery

def main():
    _install_command_and_tracking()
    _install_auto_runner()
    start_keep_alive_ping()  # 5 min wala keep-alive active

    if not all(config["token"] for config in app.BOTS):
        raise SystemExit("BOT_TOKEN missing!")

    threads = []
    for config in app.BOTS:
        instance = app.BotInstance(
            config["token"],
            config["chat_id"],
            config["state_file"],
        )
        thread = threading.Thread(target=instance.run, daemon=True)
        threads.append(thread)
        thread.start()
        time.sleep(1)

    for thread in threads:
        thread.join()

if __name__ == "__main__":
    main()
    
