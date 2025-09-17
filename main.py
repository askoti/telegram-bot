#!/usr/bin/env python3
import os
import re
import time
import logging
import yt_dlp
import asyncio
from dotenv import load_dotenv
load_dotenv()
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --- Config ---
TOKEN = os.environ.get("TELEGRAM_TOKEN") or "YourTokenHere"
cookie_content = os.getenv("YOUTUBE_COOKIES", None)
cookie_file_path = "/tmp/cookies.txt" if cookie_content else None

if cookie_content:
    # Write cookies to temp file for yt-dlp
    with open(cookie_file_path, "w", encoding="utf-8") as f:
        f.write(cookie_content)

# Tunables
page_size = 10
max_results = 30
SEARCH_TTL = 300
THREAD_WORKERS = 2
TEMP_DIR = "/tmp"
EMBED_THUMBNAIL = True

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# --- Global Stores ---
search_results = {}
search_messages = {}
user_query_messages = {}
_search_cache = OrderedDict()
SEARCH_CACHE_MAX = 200
_executor = ThreadPoolExecutor(max_workers=THREAD_WORKERS)

# --- Helpers ---
def youtube_search(query, max_results=max_results):
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "format": "bestaudio/best",
    }
    if cookie_file_path:
        ydl_opts["cookiefile"] = cookie_file_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
    return info.get("entries", [])

def clean_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"$.*?$|$$.*?$$", "", t)
    t = " ".join(t.split())
    return t.strip()

def remove_duplicates(results):
    seen = set()
    filtered = []
    for video in results:
        norm_title = clean_title(video.get("title", ""))
        if norm_title not in seen:
            seen.add(norm_title)
            filtered.append(video)
    return filtered

def parse_artist_title(raw_title: str):
    t = re.sub(r"$.*?$|$$.*?$$", "", raw_title)
    parts = t.split("-")
    if len(parts) >= 2:
        artist = parts[0].strip()
        title = "-".join(parts[1:]).strip()
    else:
        artist = "Unknown"
        title = t.strip()
    return artist, title

def get_page(results, page):
    start = page * page_size
    end = start + page_size
    return results[start:end]

def build_keyboard(results, page, query_id, include_close=True):
    keyboard = []
    for i, video in enumerate(results):
        title = (video.get("title") or "")[:40]
        keyboard.append([InlineKeyboardButton(title, callback_data=f"play|{query_id}|{page}|{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"page|{query_id}|{page-1}"))
    if (page + 1) * page_size < len(search_results.get(query_id, [])):
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"page|{query_id}|{page+1}"))
    if include_close:
        nav.append(InlineKeyboardButton("❌ Close", callback_data=f"close|{query_id}"))
    if nav:
        keyboard.append(nav)
    return InlineKeyboardMarkup(keyboard)

def download_mp3_with_thumb(url, filename=None, embed_thumbnail=EMBED_THUMBNAIL, use_proxy=False):
    if filename is None:
        base = os.path.join(TEMP_DIR, f"yt_{int(time.time()*1000)}")
    else:
        base = os.path.join(TEMP_DIR, filename.replace(".mp3", ""))
    outtmpl = base + ".%(ext)s"

    postprocessors = [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]
    if embed_thumbnail:
        postprocessors.append({"key": "EmbedThumbnail"})

    ydl_opts = {
        "quiet": True,
        "skip_download": False,
        "extract_flat": "in_playlist",
        "format": "bestaudio/best",
        "cookiefile": os.environ.get("YOUTUBE_COOKIES")  # path to cookies
    }


    if cookie_file_path:
        ydl_opts["cookiefile"] = cookie_file_path

    if use_proxy:
        proxy_url = os.getenv("YTDLP_PROXY")
        if proxy_url:
            ydl_opts["proxy"] = proxy_url

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        err_text = str(e).lower()
        if "sign in to confirm" in err_text or "region-restricted" in err_text:
            if not use_proxy and os.getenv("YTDLP_PROXY"):
                return download_mp3_with_thumb(url, filename, embed_thumbnail, use_proxy=True)
            logging.warning(f"Skipping video (login/region restriction): {url}")
            return None, None, None, None
        else:
            raise

    mp3_path = None
    base_prefix = os.path.basename(base)
    for fname in os.listdir(TEMP_DIR):
        if fname.startswith(base_prefix) and fname.lower().endswith(".mp3"):
            mp3_path = os.path.join(TEMP_DIR, fname)
            break

    title = info.get("title")
    thumbnail = info.get("thumbnail")
    uploader = info.get("uploader")
    return mp3_path, title, thumbnail, uploader

# --- Async wrappers ---
async def youtube_search_async(query, max_results=max_results):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: youtube_search(query, max_results))

async def download_mp3_with_thumb_async(url, filename=None, embed_thumbnail=EMBED_THUMBNAIL):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: download_mp3_with_thumb(url, filename, embed_thumbnail))

async def remove_file_async(path):
    if not path:
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_executor, lambda: os.remove(path) if os.path.exists(path) else None)
    except Exception:
        pass

# --- Simple LRU cache ---
def _cache_set(key, results):
    ts = time.time()
    if key in _search_cache:
        del _search_cache[key]
    _search_cache[key] = (ts, results)
    while len(_search_cache) > SEARCH_CACHE_MAX:
        _search_cache.popitem(last=False)

def _cache_get(key):
    item = _search_cache.get(key)
    if not item:
        return None
    ts, results = item
    if time.time() - ts > SEARCH_TTL:
        try:
            del _search_cache[key]
        except KeyError:
            pass
        return None
    return results

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me an artist or song name 🎵\n(Example: Coldplay Viva La Vida)"
    )

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    chat_id = str(update.message.chat_id)
    cache_key = (chat_id, query)

    results = _cache_get(cache_key)
    if results is None:
        try:
            results = await youtube_search_async(query, max_results=max_results)
        except Exception as e:
            logging.exception("Search failure")
            await update.message.reply_text(f"❌ Search error: {e}")
            return
        results = remove_duplicates(results)
        _cache_set(cache_key, results)

    if not results:
        await update.message.reply_text("❌ No results found.")
        return

    search_results[chat_id] = results
    user_query_messages[chat_id] = update.message.message_id

    page = 0
    page_results = get_page(results, page)
    reply_markup = build_keyboard(page_results, page, chat_id)
    text_response = f"🎵 Results for *{query}* (found {len(results)})"
    msg = await update.message.reply_text(
        text_response, parse_mode="Markdown", reply_markup=reply_markup
    )
    search_messages[chat_id] = msg.message_id

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    chat_id = data[1]

    if data[0] == "page":
        page = int(data[2])
        results = search_results.get(chat_id, [])
        page_results = get_page(results, page)
        msg_id = search_messages.get(chat_id)
        reply_markup = build_keyboard(page_results, page, chat_id)
        if msg_id:
            try:
                await context.bot.edit_message_reply_markup(chat_id=int(chat_id), message_id=msg_id, reply_markup=reply_markup)
            except Exception:
                pass

    elif data[0] == "close":
        msg_id = search_messages.get(chat_id)
        if msg_id:
            try:
                await context.bot.delete_message(chat_id=int(chat_id), message_id=msg_id)
            except Exception:
                pass
        user_msg_id = user_query_messages.get(chat_id)
        if user_msg_id:
            try:
                await context.bot.delete_message(chat_id=int(chat_id), message_id=user_msg_id)
            except Exception:
                pass
        search_results.pop(chat_id, None)
        search_messages.pop(chat_id, None)
        user_query_messages.pop(chat_id, None)

    elif data[0] == "play":
        page, index = int(data[2]), int(data[3])
        results = search_results.get(chat_id, [])
        try:
            video = get_page(results, page)[index]
        except Exception:
            await query.message.reply_text("❌ Item not found (maybe expired). Try searching again.")
            return
        url = f"https://www.youtube.com/watch?v={video.get('id')}"

        temp_msg = await query.message.reply_text(f"⏳ Downloading: {video.get('title')}")
        mp3_file = None
        try:
            mp3_file, raw_title, thumbnail_url, uploader = await download_mp3_with_thumb_async(url, embed_thumbnail=EMBED_THUMBNAIL)
            if not mp3_file:
                await query.message.reply_text("❌ Cannot download this video (login/region restriction).")
                return

            artist, title = parse_artist_title(raw_title or video.get("title", "Unknown"))

            with open(mp3_file, "rb") as fh:
                await query.message.reply_audio(audio=fh, title=title, performer=artist)

            await remove_file_async(mp3_file)

        except Exception as e:
            logging.exception("Download/play error")
            await query.message.reply_text(f"❌ Error: {e}")
            if mp3_file:
                await remove_file_async(mp3_file)

        try:
            await temp_msg.delete()
        except Exception:
            pass

# --- Main ---
def main():
    if not TOKEN or TOKEN == "YourTokenHere":
        raise RuntimeError("Set TELEGRAM_TOKEN environment variable.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("Bot is starting... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
