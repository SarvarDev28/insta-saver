import os
import json
import asyncio
import logging
import re
import threading
import time
import redis
import yt_dlp
from pathlib import Path
from datetime import date
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.enums import ChatType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
REDIS_URL = os.getenv("REDIS_URL")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise ValueError("BOT_TOKEN, API_ID va API_HASH environment variable lar o'rnatilishi shart!")

# Redis ulanishi (ixtiyoriy — REDIS_URL bo'lmasa statistika/cache ishlamaydi)
r = None
if REDIS_URL:
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        logger.info("✅ Redis ga ulandi")
    except Exception as e:
        logger.warning(f"⚠️ Redis ga ulanib bo'lmadi: {e}. Statistika/cache o'chirilgan.")
        r = None

app = Client("instabot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# URL xotira — callback_data 64 bayt limiti uchun
# URL ni ID bilan saqlaymiz
pending_urls = {}  # {msg_id: url}

# Flask keep-alive
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Bot ishlayapti ✅"


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


# ═══════════════════════════════════════════════════════════
# 📊 REDIS YORDAMCHI FUNKSIYALAR
# ═══════════════════════════════════════════════════════════

def save_user(user_id: int):
    if not r:
        return
    today = str(date.today())
    uid = str(user_id)
    r.sadd("users:all", uid)
    r.sadd(f"users:date:{today}", uid)


def get_stats() -> dict:
    if not r:
        return {"total": 0, "today": 0}
    today = str(date.today())
    total = r.scard("users:all") or 0
    today_count = r.scard(f"users:date:{today}") or 0
    return {"total": total, "today": today_count}


# ═══════════════════════════════════════════════════════════
# 💾 CACHE TIZIMI — bir xil link qayta yuborilsa tezkor javob
# ═══════════════════════════════════════════════════════════

CACHE_TTL = 86400 * 7  # 7 kun


def get_cache(url: str, mode: str):
    """Cache dan file_id olish"""
    if not r:
        return None
    key = f"cache:{mode}:{url}"
    data = r.get(key)
    if data:
        return json.loads(data)
    return None


def set_cache(url: str, mode: str, file_data: dict):
    """Cache ga file_id saqlash"""
    if not r:
        return
    key = f"cache:{mode}:{url}"
    r.setex(key, CACHE_TTL, json.dumps(file_data))


# ═══════════════════════════════════════════════════════════
# ⭐ SEVIMLILAR RO'YXATI
# ═══════════════════════════════════════════════════════════

MAX_FAVORITES = 10


def add_favorite(user_id: int, url: str):
    """Sevimlilar ro'yxatiga qo'shish"""
    if not r:
        return
    key = f"favorites:{user_id}"
    # Oxirgi 10 tani saqlash (LIFO)
    r.lpush(key, url)
    r.ltrim(key, 0, MAX_FAVORITES - 1)


def get_favorites(user_id: int) -> list:
    """Sevimlilar ro'yxatini olish"""
    if not r:
        return []
    key = f"favorites:{user_id}"
    return r.lrange(key, 0, MAX_FAVORITES - 1)


# ═══════════════════════════════════════════════════════════
# 🔗 HAVOLA ANIQLAGICH — xabar ichidan Instagram linkni topish
# ═══════════════════════════════════════════════════════════

def extract_instagram_url(text: str) -> str | None:
    """Matn ichidan Instagram URL ni topib qaytaradi"""
    pattern = r'https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|reels|tv|stories)/[^\s\'"<>]+'
    match = re.search(pattern, text)
    return match.group(0) if match else None


def is_instagram_url(url: str) -> bool:
    pattern = r'(https?://)?(www\.)?(instagram\.com|instagr\.am)/(p|reel|reels|tv|stories)/[^\s]+'
    return bool(re.match(pattern, url))


# ═══════════════════════════════════════════════════════════
# 📊 ZAMONAVIY PROGRESS BAR
# ═══════════════════════════════════════════════════════════

class ProgressBar:
    """Zamonaviy animatsion progress bar"""

    STAGES = [
        ("🔍", "Link tekshirilmoqda...", 10),
        ("📡", "Instagram ga ulanilmoqda...", 25),
        ("⬇️", "Yuklab olinmoqda...", 50),
        ("🔄", "Format o'zgartirilmoqda...", 75),
        ("📤", "Telegram ga yuborilmoqda...", 90),
        ("✅", "Tayyor!", 100),
    ]

    @staticmethod
    def render(percent: int) -> str:
        """Progress bar chizish"""
        filled = int(percent / 10)
        empty = 10 - filled
        bar = "▓" * filled + "░" * empty
        return f"[{bar}] {percent}%"

    @staticmethod
    def get_stage_text(stage_index: int) -> str:
        """Stage bo'yicha to'liq xabar"""
        if stage_index >= len(ProgressBar.STAGES):
            stage_index = len(ProgressBar.STAGES) - 1
        emoji, text, percent = ProgressBar.STAGES[stage_index]
        bar = ProgressBar.render(percent)
        return f"{emoji} {text}\n\n{bar}"


async def update_progress(status_msg: Message, stage: int):
    """Progress xabarini yangilash"""
    try:
        text = ProgressBar.get_stage_text(stage)
        await status_msg.edit_text(text)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# ⬇️ YUKLAB OLISH FUNKSIYALARI
# ═══════════════════════════════════════════════════════════

async def download_video(url: str, chat_id: int):
    """Video yuklash (H.264 + AAC)"""
    output_template = str(DOWNLOAD_DIR / f"{chat_id}_%(title).40s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]/best[ext=mp4][vcodec^=avc]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
        "postprocessor_args": {
            "FFmpegVideoConvertor": ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"]
        },
    }

    return await _execute_download(url, chat_id, ydl_opts)


async def download_audio(url: str, chat_id: int):
    """Faqat audio (MP3) yuklash"""
    output_template = str(DOWNLOAD_DIR / f"{chat_id}_%(title).40s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
    }

    return await _execute_download(url, chat_id, ydl_opts)


async def _execute_download(url: str, chat_id: int, ydl_opts: dict):
    """Umumiy yuklash mexanizmi"""
    try:
        loop = asyncio.get_running_loop()

        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)

        await loop.run_in_executor(None, _download)

        files = [f for f in DOWNLOAD_DIR.iterdir() if f.name.startswith(str(chat_id))]
        if files:
            return files, None
        return None, "❌ Fayl topilmadi."

    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "private" in err:
            return None, "❌ Bu akkaunt yoki post xususiy (private)."
        elif "login" in err or "sign in" in err or "cookie" in err:
            return None, "❌ Instagram login talab qilmoqda. Admin bilan bog'laning."
        elif "not available" in err or "removed" in err:
            return None, "❌ Bu post mavjud emas yoki o'chirilgan."
        else:
            return None, f"❌ Yuklab bo'lmadi.\n`{str(e)[:120]}`"
    except Exception as e:
        return None, f"❌ Xatolik yuz berdi: `{str(e)[:120]}`"


def cleanup(chat_id: int):
    for f in DOWNLOAD_DIR.iterdir():
        if f.name.startswith(str(chat_id)):
            try:
                f.unlink()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════
# 🤖 BOT KOMANDALAR
# ═══════════════════════════════════════════════════════════

@app.on_message(filters.command("start"))
async def cmd_start(client, message: Message):
    user = message.from_user
    save_user(user.id)

    await message.reply(
        f"👋 Salom, **{user.first_name}**!\n\n"
        "📥 Men **Instagram Saver** botman.\n\n"
        "📌 Nima yuklay olaman:\n"
        "• 🎬 Reels (video yoki audio)\n"
        "• 🖼 Postlar (rasm/video)\n"
        "• 📖 Stories\n\n"
        "🎯 **Buyruqlar:**\n"
        "• /favorites — Sevimli yuklanishlar\n"
        "• /help — Qo'llanma\n\n"
        "➡️ Instagram havolasini yuboring!"
    )


@app.on_message(filters.command("help"))
async def cmd_help(client, message: Message):
    await message.reply(
        "📖 **Qo'llanma:**\n\n"
        "1️⃣ Instagram da post/reel/story ni oching\n"
        "2️⃣ Havolasini nusxalang\n"
        "3️⃣ Menga yuboring\n"
        "4️⃣ 🎬 Video yoki 🎵 Audio tanlang\n"
        "5️⃣ Yuklanib keladi ✅\n\n"
        "💡 **Qo'shimcha:**\n"
        "• Xabar ichida link bo'lsa ham topaman\n"
        "• Bir xil link tez yuklanadi (cache)\n"
        "• /favorites — oxirgi yuklanishlar\n\n"
        "⚠️ **Ishlamaydi:**\n"
        "• Private akkaunt postlari\n"
        "• O'chirilgan postlar\n\n"
        "📩 **Talab va takliflar:** @theSarvar_04"
    )


@app.on_message(filters.command("favorites"))
async def cmd_favorites(client, message: Message):
    favs = get_favorites(message.from_user.id)

    if not favs:
        await message.reply(
            "⭐ **Sevimlilar ro'yxati bo'sh.**\n\n"
            "Instagram havolasi yuboring — avtomatik saqlanadi!"
        )
        return

    text = "⭐ **Oxirgi yuklanishlar:**\n\n"
    for i, url in enumerate(favs, 1):
        # URL ni qisqartirish
        short = url.split("?")[0]  # query params olib tashlash
        text += f"{i}. [{short[:50]}...]({url})\n"

    text += "\n💡 Havolani bosib qayta yuklashingiz mumkin."

    await message.reply(text, disable_web_page_preview=True)


@app.on_message(filters.command("stats"))
async def cmd_stats(client, message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Bu buyruq faqat admin uchun.")
        return

    stats = get_stats()
    await message.reply(
        f"📊 **Bot statistikasi:**\n\n"
        f"👥 Jami foydalanuvchilar: **{stats['total']}** ta\n"
        f"📅 Bugun qo'shildi: **{stats['today']}** ta"
    )


# ═══════════════════════════════════════════════════════════
# 📨 XABAR HANDLER — Havola aniqlagich bilan
# ═══════════════════════════════════════════════════════════

@app.on_message(filters.text & ~filters.command(["start", "help", "stats", "favorites"]))
async def handle_message(client, message: Message):
    text = message.text.strip()

    is_group = message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    # 🔗 Havola aniqlagich — matn ichidan Instagram linkni topish
    url = extract_instagram_url(text)

    if is_group:
        if not url:
            return  # Guruhda faqat Instagram linkga javob berish
    else:
        if not url:
            # Oddiy matn — link yo'q
            if not text.startswith("http"):
                await message.reply(
                    "📎 Instagram havolasini yuboring.\n\n"
                    "Masalan:\n`https://www.instagram.com/reel/ABC123/`\n\n"
                    "💡 Xabar ichida link bo'lsa ham topaman!"
                )
            else:
                await message.reply(
                    "⚠️ Faqat **Instagram** havolalarini qabul qilaman.\n\n"
                    "Masalan:\n`https://www.instagram.com/reel/...`"
                )
            return

    # 📊 Inline tugmalar — Video yoki Audio tanlash
    sent = await message.reply(
        f"🔗 **Link topildi!**\n\n"
        f"Qanday formatda yuklayman?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎬 Video", callback_data=f"video|{message.id}"),
                InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"audio|{message.id}"),
            ]
        ])
    )
    # URL ni xotirada saqlash
    pending_urls[message.id] = url


# ═══════════════════════════════════════════════════════════
# 🔘 CALLBACK HANDLER — Inline tugma bosilganda
# ═══════════════════════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^(video|audio)\|"))
async def callback_download(client, callback: CallbackQuery):
    data = callback.data
    mode, msg_id_str = data.split("|", 1)
    msg_id = int(msg_id_str)
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    # URL ni xotiradan olish
    url = pending_urls.pop(msg_id, None)
    if not url:
        await callback.answer("⚠️ Link eskirgan. Qayta yuboring.", show_alert=True)
        await callback.message.delete()
        return

    # Tugmani o'chirish
    await callback.message.edit_reply_markup(None)

    # 💾 Cache tekshirish
    cached = get_cache(url, mode)
    if cached:
        try:
            if cached["type"] == "video":
                await callback.message.reply_video(
                    cached["file_id"],
                    caption="📥 Instagram dan yuklandi ⚡ (cache)\n@InstaDownloader_uzBot",
                    supports_streaming=True
                )
            elif cached["type"] == "audio":
                await callback.message.reply_audio(
                    cached["file_id"],
                    caption="🎵 Instagram dan yuklandi ⚡ (cache)\n@InstaDownloader_uzBot"
                )
            elif cached["type"] == "photo":
                await callback.message.reply_photo(
                    cached["file_id"],
                    caption="📥 Instagram dan yuklandi ⚡ (cache)\n@InstaDownloader_uzBot"
                )
            elif cached["type"] == "document":
                await callback.message.reply_document(
                    cached["file_id"],
                    caption="📥 Instagram dan yuklandi ⚡ (cache)\n@InstaDownloader_uzBot"
                )

            await callback.message.delete()
            add_favorite(user_id, url)
            return
        except Exception:
            pass  # Cache eskirgan bo'lsa, qaytadan yuklaymiz

    # 📊 Progress bar boshlash
    status = await callback.message.edit_text(ProgressBar.get_stage_text(0))

    await asyncio.sleep(0.5)
    await update_progress(status, 1)

    await asyncio.sleep(0.3)
    await update_progress(status, 2)

    # ⬇️ Yuklab olish
    if mode == "video":
        files, error = await download_video(url, chat_id)
    else:
        files, error = await download_audio(url, chat_id)

    if error:
        await status.edit_text(error)
        return

    await update_progress(status, 3)
    await asyncio.sleep(0.3)
    await update_progress(status, 4)

    try:
        for filepath in files:
            ext = filepath.suffix.lower()

            if mode == "audio" or ext in [".mp3", ".m4a", ".ogg", ".wav"]:
                sent = await callback.message.reply_audio(
                    str(filepath),
                    caption="🎵 Instagram dan yuklandi\n@InstaDownloader_uzBot"
                )
                set_cache(url, mode, {"type": "audio", "file_id": sent.audio.file_id})

            elif ext in [".mp4", ".mov", ".mkv", ".webm"]:
                try:
                    sent = await callback.message.reply_video(
                        str(filepath),
                        caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot",
                        supports_streaming=True
                    )
                    set_cache(url, mode, {"type": "video", "file_id": sent.video.file_id})
                except Exception:
                    sent = await callback.message.reply_document(
                        str(filepath),
                        caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot"
                    )
                    set_cache(url, mode, {"type": "document", "file_id": sent.document.file_id})

            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                try:
                    sent = await callback.message.reply_photo(
                        str(filepath),
                        caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot"
                    )
                    set_cache(url, mode, {"type": "photo", "file_id": sent.photo.file_id})
                except Exception:
                    sent = await callback.message.reply_document(
                        str(filepath),
                        caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot"
                    )
                    set_cache(url, mode, {"type": "document", "file_id": sent.document.file_id})
            else:
                sent = await callback.message.reply_document(
                    str(filepath),
                    caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot"
                )
                set_cache(url, mode, {"type": "document", "file_id": sent.document.file_id})

        # ⭐ Sevimlilar ga qo'shish
        add_favorite(user_id, url)

        # ✅ Progress yakunlash
        await update_progress(status, 5)
        await asyncio.sleep(1)
        await status.delete()

    except Exception as e:
        await status.edit_text(f"❌ Yuborishda xatolik: `{str(e)[:100]}`")
    finally:
        cleanup(chat_id)


# ═══════════════════════════════════════════════════════════
# 🚀 BOTNI ISHGA TUSHIRISH
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    logger.info("✅ InstaBot ishga tushdi")
    app.run()
