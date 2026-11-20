#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram bot: send MP3 + MP4 for an Instagram or YouTube link."""

# ---------- PATCHES for Python 3.13+ missing modules ----------
import sys

try:
    import imghdr  # noqa: F401
except ImportError:
    from types import ModuleType

    imghdr = ModuleType("imghdr")

    def what(*args, **kwargs):
        return None

    imghdr.what = what
    sys.modules["imghdr"] = imghdr

try:
    import pkg_resources  # noqa: F401
except ImportError:
    from types import ModuleType

    pkg_resources = ModuleType("pkg_resources")

    def get_distribution(name):
        class Dist:
            version = "0.0.0"

        return Dist()

    def require(*args, **kwargs):
        return None

    pkg_resources.get_distribution = get_distribution
    pkg_resources.require = require
    pkg_resources.DistributionNotFound = Exception
    sys.modules["pkg_resources"] = pkg_resources
# --------------------------------------------------------------

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    Filters,
    MessageHandler,
    Updater,
)

load_dotenv()

# Telegram bots can upload about 50 MB. Stay a little under the hard limit.
MAX_UPLOAD_BYTES = 49 * 1024 * 1024

IG_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
YT_RES = (
    re.compile(r"https?://(?:www\.|m\.)?youtube\.com/watch\?v=[\w-]{6,}", re.IGNORECASE),
    re.compile(r"https?://youtu\.be/[\w-]{6,}", re.IGNORECASE),
    re.compile(r"https?://(?:www\.|m\.)?youtube\.com/shorts/[\w-]{6,}", re.IGNORECASE),
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = ""
OWNER_USER_ID = 0
APPROVED_USERS_FILE = os.getenv("APPROVED_USERS_FILE", "approved_users.json")

state_lock = threading.Lock()
authorized_users = set()
pending_requests = set()


def configure_from_env():
    """Read required settings. Never fall back to a real token or user id."""
    global BOT_TOKEN, OWNER_USER_ID
    token = os.getenv("BOT_TOKEN", "").strip()
    owner = os.getenv("OWNER_ID", "").strip()
    if not token or token.startswith("your_"):
        raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and set a real token.")
    if not owner.isdigit():
        raise SystemExit("OWNER_ID must be your numeric Telegram user id.")
    BOT_TOKEN = token
    OWNER_USER_ID = int(owner)


def load_approved_users():
    try:
        with open(APPROVED_USERS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            return set()
        return {int(user_id) for user_id in data}
    except (OSError, ValueError, json.JSONDecodeError):
        return set()


def save_approved_users(users_set):
    directory = os.path.dirname(os.path.abspath(APPROVED_USERS_FILE))
    os.makedirs(directory, exist_ok=True)
    payload = json.dumps(sorted(int(user_id) for user_id in users_set))
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, APPROVED_USERS_FILE)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def is_authorized(user_id):
    with state_lock:
        return user_id in authorized_users


def detect_platform_and_url(text):
    """Return (platform, url) for an Instagram or YouTube link, else (None, None)."""
    if not text:
        return (None, None)
    ig_match = IG_RE.search(text)
    if ig_match:
        return ("instagram", ig_match.group(0))
    for pattern in YT_RES:
        yt_match = pattern.search(text)
        if yt_match:
            return ("youtube", yt_match.group(0))
    return (None, None)


def _ydl_options(temp_dir, extra):
    options = {
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "restrictfilenames": True,
    }
    cookies = os.getenv("COOKIES_FILE", "").strip()
    if cookies and os.path.isfile(cookies):
        options["cookiefile"] = cookies
    options.update(extra)
    return options


def _first_file(temp_dir, suffixes):
    best = None
    best_size = 0
    for name in os.listdir(temp_dir):
        if not name.lower().endswith(suffixes):
            continue
        path = os.path.join(temp_dir, name)
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        if size > best_size:
            best = path
            best_size = size
    return best


def _download_with_ydl(url, temp_dir, extra, suffixes):
    import yt_dlp

    with yt_dlp.YoutubeDL(_ydl_options(temp_dir, extra)) as ydl:
        ydl.extract_info(url, download=True)
    path = _first_file(temp_dir, suffixes)
    if not path:
        raise FileNotFoundError("Downloader finished but no output file was created.")
    return path


def _extract_mp3(video_path, temp_dir):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    audio_path = os.path.join(temp_dir, "audio.mp3")
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", video_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", audio_path],
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffmpeg audio extract failed: %s", exc)
        return None
    if result.returncode != 0 or not os.path.isfile(audio_path) or os.path.getsize(audio_path) == 0:
        detail = ""
        if result.stderr:
            detail = result.stderr.decode("utf-8", "replace")[-400:]
        logger.warning("ffmpeg audio extract failed: %s", detail or "no stderr")
        return None
    return audio_path


def download_pair(url):
    """Download video once, then extract MP3. Returns (temp_dir, audio_path, video_path)."""
    temp_dir = tempfile.mkdtemp(prefix="mrdl_")
    try:
        video_path = _download_with_ydl(
            url,
            temp_dir,
            {
                "format": "bv*[ext=mp4]+ba/b[ext=mp4]/bv*+ba/b",
                "merge_output_format": "mp4",
            },
            (".mp4", ".mkv", ".webm"),
        )
        audio_path = _extract_mp3(video_path, temp_dir)
        if audio_path is None:
            audio_dir = os.path.join(temp_dir, "audio_only")
            os.makedirs(audio_dir, exist_ok=True)
            raw_audio = _download_with_ydl(
                url,
                audio_dir,
                {
                    "format": "bestaudio/best",
                    "postprocessors": [
                        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                    ],
                },
                (".mp3",),
            )
            audio_path = os.path.join(temp_dir, "audio.mp3")
            shutil.move(raw_audio, audio_path)
        return temp_dir, audio_path, video_path
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _human_size(num_bytes):
    return "%.1f MB" % (num_bytes / (1024 * 1024))


def _send_file(message, kind, path, caption):
    size = os.path.getsize(path)
    if size > MAX_UPLOAD_BYTES:
        message.reply_text(
            "%s is %s, over Telegram's 50 MB bot limit, so it was not uploaded."
            % ("Audio" if kind == "audio" else "Video", _human_size(size))
        )
        return False
    with open(path, "rb") as handle:
        if kind == "audio":
            message.reply_audio(audio=handle, caption=caption, timeout=180)
        else:
            message.reply_video(video=handle, caption=caption, timeout=300, supports_streaming=True)
    return True


def _request_access(update, context):
    user = update.effective_user
    user_id = user.id
    with state_lock:
        already_pending = user_id in pending_requests
        if not already_pending:
            pending_requests.add(user_id)
    update.message.reply_text(
        "You are not authorized yet. Ask sent to the owner."
        if not already_pending
        else "Your request is already waiting for the owner."
    )
    if already_pending:
        return
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Approve", callback_data="approve_%s" % user_id),
            InlineKeyboardButton("Decline", callback_data="decline_%s" % user_id),
        ]]
    )
    username = "@%s" % user.username if user.username else "no username"
    context.bot.send_message(
        chat_id=OWNER_USER_ID,
        text="Approval request\n\nUser: %s (%s)\nID: %s" % (user.full_name, username, user_id),
        reply_markup=keyboard,
    )


def start(update, context):
    user = update.effective_user
    if update.message is None or user is None:
        return
    if is_authorized(user.id):
        update.message.reply_text(
            "Mr. Downloading Bot\n\n"
            "Send an Instagram or YouTube link. I will reply with an MP3 and an MP4.\n\n"
            "Commands: /help\n"
            "Coded: @riyanshV"
        )
        return
    _request_access(update, context)


def help_command(update, context):
    if update.message is None:
        return
    lines = [
        "Send one Instagram or YouTube link.",
        "I download the video, extract an MP3, and send both when they fit Telegram's 50 MB limit.",
        "",
        "/start - welcome or request access",
        "/help - this message",
    ]
    if update.effective_user and update.effective_user.id == OWNER_USER_ID:
        lines.extend([
            "",
            "Owner:",
            "/users - list approved user ids",
            "/revoke <user_id> - remove access",
        ])
    update.message.reply_text("\n".join(lines))


def users_command(update, context):
    if update.message is None or update.effective_user.id != OWNER_USER_ID:
        if update.message:
            update.message.reply_text("Only the owner can do that.")
        return
    with state_lock:
        others = sorted(user_id for user_id in authorized_users if user_id != OWNER_USER_ID)
    if not others:
        update.message.reply_text("No approved users yet. Owner id: %s" % OWNER_USER_ID)
        return
    update.message.reply_text("Approved users:\n" + "\n".join(str(user_id) for user_id in others))


def revoke_command(update, context):
    if update.message is None or update.effective_user.id != OWNER_USER_ID:
        if update.message:
            update.message.reply_text("Only the owner can do that.")
        return
    if not context.args or not context.args[0].isdigit():
        update.message.reply_text("Usage: /revoke <user_id>")
        return
    user_id = int(context.args[0])
    if user_id == OWNER_USER_ID:
        update.message.reply_text("The owner cannot be revoked.")
        return
    with state_lock:
        removed = user_id in authorized_users
        authorized_users.discard(user_id)
        pending_requests.discard(user_id)
        if removed:
            save_approved_users(authorized_users - {OWNER_USER_ID})
    update.message.reply_text("Access removed." if removed else "That user was not approved.")


def handle_message(update, context):
    if update.message is None or update.effective_user is None:
        return
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        update.message.reply_text("You are not authorized. Send /start to request access.")
        return

    platform, url = detect_platform_and_url(update.message.text or "")
    if not url:
        update.message.reply_text("Send a valid Instagram or YouTube link.")
        return

    status = update.message.reply_text("Found a %s link. Downloading audio and video..." % platform)
    temp_dir = None
    try:
        temp_dir, audio_path, video_path = download_pair(url)
        status.edit_text("Download finished. Uploading...")
        sent_audio = _send_file(update.message, "audio", audio_path, "Audio from %s" % platform)
        sent_video = _send_file(update.message, "video", video_path, "Video from %s" % platform)
        if sent_audio or sent_video:
            status.delete()
        else:
            status.edit_text("Both files were over Telegram's 50 MB bot upload limit.")
    except Exception as exc:
        logger.exception("Download failed for %s", url)
        friendly = "Download failed. The link may be private, age-gated, or unsupported."
        if "ffmpeg" in str(exc).lower():
            friendly = "Download failed because ffmpeg is missing on the server."
        try:
            status.edit_text(friendly)
        except Exception:
            update.message.reply_text(friendly)
        logger.info("Downloader error detail: %s", exc)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def button_callback(update, context):
    query = update.callback_query
    query.answer()
    data = query.data or ""
    if not (data.startswith("approve_") or data.startswith("decline_")):
        return
    if query.from_user.id != OWNER_USER_ID:
        query.edit_message_text("You are not allowed to decide this.")
        return

    try:
        user_id = int(data.split("_", 1)[1])
    except (IndexError, ValueError):
        query.edit_message_text("Invalid request.")
        return

    approved = data.startswith("approve_")
    with state_lock:
        pending_requests.discard(user_id)
        if approved and user_id not in authorized_users:
            authorized_users.add(user_id)
            save_approved_users(authorized_users - {OWNER_USER_ID})
            changed = True
        else:
            changed = False

    if approved:
        query.edit_message_text(
            "User %s approved." % user_id if changed else "User %s was already approved." % user_id
        )
        if changed:
            try:
                context.bot.send_message(
                    chat_id=user_id,
                    text="You are approved. Send an Instagram or YouTube link to get audio and video.",
                )
            except Exception as exc:
                logger.warning("Could not notify user %s: %s", user_id, exc)
        return

    query.edit_message_text("User %s declined." % user_id)
    try:
        context.bot.send_message(chat_id=user_id, text="The owner declined your access request.")
    except Exception as exc:
        logger.warning("Could not notify user %s: %s", user_id, exc)


def error_handler(update, context):
    logger.error("Exception while handling an update", exc_info=context.error)


def main():
    configure_from_env()
    with state_lock:
        authorized_users.clear()
        authorized_users.update(load_approved_users())
        authorized_users.add(OWNER_USER_ID)

    updater = Updater(BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("users", users_command))
    dispatcher.add_handler(CommandHandler("revoke", revoke_command))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(CallbackQueryHandler(button_callback))
    dispatcher.add_error_handler(error_handler)

    logger.info("Bot started polling")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
