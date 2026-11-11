"""
TikTok Pure Auto-Pilot Pipeline (No YouTube, Only TikTok Profiles & Tags).
- Strictly filters out non-TikTok links.
- Prevents infinite loops and ensures clean feed processing.
- Leaves bot.py completely untouched.
"""

import json
import os
import re
import random
import subprocess
import threading
import time

import bot as app

SEEN_FILE = "tiktok_seen.json"
TAGS_FILE = "tiktok_tags.json"
PROFILES_FILE = "tiktok_profiles.json"
CHECK_INTERVAL = 10800  # Har 3 ghante baad naye videos scan honge

_seen_ids = set()
_queued_ids = set()
_seen_lock = threading.Lock()
_current_video = threading.local()

def _load_seen_ids():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, list):
            return {str(video_id) for video_id in data}
    except (OSError, ValueError, TypeError):
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

def _get_videos_from_source(url):
    import yt_dlp
    
    # Sirf TikTok URLs allow karne ke liye check
    if "tiktok.com" not in url:
        return []
    
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": 20,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        try:
            result = ydl.extract_info(url, download=False)
        except Exception as e:
            print(f"Error fetching source ({url}): {e}")
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

        # Dobara tasdeek ke sirf tiktok URL ho
        if "tiktok.com" not in str(video_url):
            continue

        video_id = _video_id_from_url(video_url) or entry_id
        if video_id:
            videos.append((video_id, str(video_url)))
            
    return videos

def start_discovery_worker(instance):
    def worker_loop():
        time.sleep(15)
        while True:
            profiles = _load_json_file(PROFILES_FILE, [])
            tags = _load_json_file(TAGS_FILE, [
                "https://www.tiktok.com/tag/foryou",
                "https://www.tiktok.com/tag/trending",
                "https://www.tiktok.com/tag/viral"
            ])
            
            # Sirf TikTok profiles aur tags ka combination
            sources = profiles + tags
            if not sources:
                sources = ["https://www.tiktok.com/tag/trending"]
                
            random.shuffle(sources)

            total_added = 0
            for source_url in sources:
                try:
                    print(f"Scanning TikTok source: {source_url}")
                    videos = _get_videos_from_source(source_url)
                    random.shuffle(videos)
                    
                    added = 0
                    for video_id, video_url in videos:
                        if "tiktok.com" not in video_url:
                            continue
                            
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
                        print(f"Successfully added {added} TikTok videos from {source_url}")
                        instance.start_worker()
                        
                except Exception as e:
                    print(f"Source error on {source_url}: {e}")
                
                time.sleep(25)
            
            print(f"Cycle completed. Total new TikTok videos queued: {total_added}. Sleeping for {CHECK_INTERVAL/3600} hours...")
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

def _install_feed_tracking():
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
    _install_feed_tracking()
    _install_auto_runner()

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
        time.sleep(2)

    for thread in threads:
        thread.join()

if __name__ == "__main__":
    main()
            
