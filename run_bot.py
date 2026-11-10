"""
TikTok profile se videos queue karne ka add-on.

Command:
  /tiktok https://www.tiktok.com/@username
  /tiktok https://www.tiktok.com/@username 50

Default limit 20 hai; ek command mein maximum 100 videos.
"""

import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlparse

import bot as app


SEEN_FILE = "tiktok_seen.json"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

_seen_ids = set()
_queued_ids = set()
_seen_lock = threading.Lock()
_current_video = threading.local()


def _load_seen_ids():
    """Pehle se feed mein add kiye TikTok video IDs load kare."""
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, list):
            return {str(video_id) for video_id in data}
    except (OSError, ValueError, TypeError):
        pass
    return set()


def _save_seen_ids():
    """Seen IDs ko file mein safely save kare."""
    temp_file = SEEN_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(sorted(_seen_ids), file, indent=2, ensure_ascii=False)
    os.replace(temp_file, SEEN_FILE)


_seen_ids.update(_load_seen_ids())


def _video_id_from_url(url):
    match = re.search(r"/video/(\d+)", url or "")
    return match.group(1) if match else None


def _get_profile_videos(profile_url, limit):
    """TikTok profile se available video links aur IDs nikaale."""
    import yt_dlp

    parsed = urlparse(profile_url)
    host = (parsed.hostname or "").lower()

    if host not in ("tiktok.com", "www.tiktok.com", "m.tiktok.com"):
        raise ValueError("TikTok profile ka poora URL dein.")

    path_parts = [part for part in parsed.path.split("/") if part]

    if (
        not path_parts
        or not path_parts[0].startswith("@")
        or "/video/" in parsed.path
    ):
        raise ValueError(
            "Profile link /@username wala hona chahiye, video link nahi."
        )

    username = path_parts[0]

    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": limit,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        profile = ydl.extract_info(profile_url, download=False)

    videos = []

    for entry in (profile or {}).get("entries") or []:
        if not entry:
            continue

        video_url = (
            entry.get("webpage_url")
            or entry.get("original_url")
            or entry.get("url")
        )

        entry_id = str(entry.get("id") or "").strip()

        if not video_url or not str(video_url).startswith(("http://", "https://")):
            if not entry_id:
                continue
            video_url = (
                f"https://www.tiktok.com/{username}/video/{entry_id}"
            )

        video_url = str(video_url)
        video_id = _video_id_from_url(video_url) or entry_id

        if video_id:
            videos.append((video_id, video_url))

    return videos


def _install_feed_tracking():
    """
    Existing social-video pipeline ko use karte hue successful TikTok
    upload ke baad video ID yaad rakhe.
    """
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
        # Seen file ko feed.json ke saath commit/push hone ke liye stage kare.
        subprocess.run(["git", "add", SEEN_FILE], check=False)
        original_git_push_feed()

    app.BotInstance.process_social = process_social_with_tracking
    app.append_feed = append_feed_and_mark_seen
    app.git_push_feed = git_push_feed_and_seen


def _install_tiktok_command():
    """Bot ke existing handlers ke saath /tiktok command register kare."""
    original_register_handlers = app.BotInstance.register_handlers

    def register_handlers_with_tiktok(self):
        @self.bot.message_handler(commands=["tiktok"])
        def handle_tiktok_command(message):
            if message.chat.id not in self.allowed:
                return

            parts = (message.text or "").split()

            if len(parts) < 2:
                self.bot.reply_to(
                    message,
                    "Format:\n"
                    "/tiktok https://www.tiktok.com/@username [limit]\n\n"
                    f"Default limit {DEFAULT_LIMIT}; maximum {MAX_LIMIT} videos.",
                )
                return

            profile_url = parts[1]

            try:
                limit = int(parts[2]) if len(parts) > 2 else DEFAULT_LIMIT
                limit = max(1, min(limit, MAX_LIMIT))
            except ValueError:
                self.bot.reply_to(
                    message,
                    "Limit number mein dein. Misaal:\n"
                    "/tiktok https://www.tiktok.com/@username 20",
                )
                return

            self.bot.reply_to(message, "TikTok profile check kar raha hoon...")

            def fetch_and_queue():
                try:
                    videos = _get_profile_videos(profile_url, limit)
                except Exception as error:
                    self.bot.send_message(
                        message.chat.id,
                        f"Profile read nahi ho saka: {str(error)[:250]}",
                    )
                    return

                added = 0
                skipped = 0

                for video_id, video_url in videos:
                    with _seen_lock:
                        # Disk par saved IDs ke saath memory wali IDs bhi check hon.
                        _seen_ids.update(_load_seen_ids())

                        if video_id in _seen_ids or video_id in _queued_ids:
                            skipped += 1
                            continue

                        _queued_ids.add(video_id)

                    task_id = self.next_task_id()
                    self.task_queue.put({
                        "link": video_url,
                        "type": "social",
                        "task_id": task_id,
                    })
                    added += 1

                self.bot.send_message(
                    message.chat.id,
                    f"Profile se {added} naye videos queue mein add hue.\n"
                    f"Pehle se add/queue mein: {skipped}\n"
                    f"Queue mein total: {self.task_queue.qsize()}\n\n"
                    "Videos process hokar ZeroFeed aur feed.json mein jayengi.",
                )

                if added:
                    self.start_worker()

            threading.Thread(target=fetch_and_queue, daemon=True).start()

        # Iske baad original handlers, including existing catch-all, register hon.
        original_register_handlers(self)

    app.BotInstance.register_handlers = register_handlers_with_tiktok


def main():
    _install_feed_tracking()
    _install_tiktok_command()

    if not all(config["token"] for config in app.BOTS):
        raise SystemExit(
            "BOT_TOKEN missing! Repo Settings > Secrets and variables > "
            "Actions mein BOT_TOKEN add karein."
        )

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
