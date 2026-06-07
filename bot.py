import os
import json
import asyncio
import logging
import re
import threading
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

# Redis ulanishi
r = None
if REDIS_URL:
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        logger.info("✅ Redis ga ulandi")
    except Exception as e:
        logger.warning(f"⚠️ Redis ga ulanib bo'lmadi: {e}")
        r = None

app = Client("instabot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# URL xotira — callback_data 64 bayt limiti uchun
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
# 💾 CACHE TIZIMI
# ═══════════════════════════════════════════════════════════

CACHE_TTL = 86400 * 7  # 7 kun


def get_cache(url: str, mode: str):
    if not r:
        return None
    key = f"cache:{mode}:{url}"
    data = r.get(key)
    if data:
        return json.loads(data)
    return None


def set_cache(url: str, mode: str, file_data: dict):
    if not r:
        return
    key = f"cache:{mode}:{url}"
    r.setex(key, CACHE_TTL, json.dumps(file_data))


# ═══════════════════════════════════════════════════════════
# ⭐ SEVIMLILAR RO'YXATI
# ═══════════════════════════════════════════════════════════

MAX_FAVORITES = 10


def add_favorite(user_id: int, url: str):
    if not r:
        return
    key = f"favorites:{user_id}"
    # Dublikat qo'shmaslik
    existing = r.lrange(key, 0, -1)
    if url in existing:
        return
    r.lpush(key, url)
    r.ltrim(key, 0, MAX_FAVORITES - 1)


def get_favorites(user_id: int) -> list:
    if not r:
        return []
    key = f"favorites:{user_id}"
    return r.lrange(key, 0, MAX_FAVORITES - 1)


# ═══════════════════════════════════════════════════════════
# 🔗 HAVOLA ANIQLAGICH
# ═══════════════════════════════════════════════════════════

def extract_instagram_url(text: str) -> str | None:
    pattern = r'https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|reels|tv|stories)/[^\s\'"<>]+'
    match = re.search(pattern, text)
    return match.group(0) if match else None


# ═══════════════════════════════════════════════════════════
# 📊 ZAMONAVIY PROGRESS BAR
# ═══════════════════════════════════════════════════════════

class ProgressBar:
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
        filled = int(percent / 10)
        empty = 10 - filled
        bar = "▓" * filled + "░" * empty
        return f"[{bar}] {percent}%"

    @staticmethod
    def get_stage_text(stage_index: int) -> str:
        if stage_index >= len(ProgressBar.STAGES):
            stage_index = len(ProgressBar.STAGES) - 1
        emoji, text, percent = ProgressBar.STAGES[stage_index]
        bar = ProgressBar.render(percent)
        return f"{emoji} {text}\n\n{bar}"


async def update_progress(status_msg: Message, stage: int):
    try:
        text = ProgressBar.get_stage_text(stage)
        await status_msg.edit_text(text)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# ⬇️ YUKLAB OLISH FUNKSIYALARI
# ═══════════════════════════════════════════════════════════

async def detect_media_type(url: str) -> str:
    """Link rasm mi yoki video: 'photo', 'video', 'unknown'"""
    try:
        loop = asyncio.get_running_loop()

        def _extract():
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 15,
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await loop.run_in_executor(None, _extract)

        if not info:
            return "unknown"

        # Carousel tekshirish
        entries = info.get("entries")
        if entries:
            for entry in entries:
                if entry and entry.get("ext") in ["mp4", "webm", "mov", "mkv"]:
                    return "video"
                if entry and entry.get("vcodec") and entry.get("vcodec") != "none":
                    return "video"
            return "photo"

        # Yagona media
        ext = info.get("ext", "")
        vcodec = info.get("vcodec", "none")

        if ext in ["jpg", "jpeg", "png", "webp"]:
            return "photo"
        elif ext in ["mp4", "webm", "mov", "mkv"] or (vcodec and vcodec != "none"):
            return "video"
        else:
            return "unknown"

    except yt_dlp.utils.DownloadError as e:
        # "no video in this post" — bu rasm
        if "no video" in str(e).lower():
            return "photo"
        return "unknown"
    except Exception:
        return "unknown"


async def download_video(url: str, chat_id: int):
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


async def download_photo(url: str, chat_id: int):
    """Rasmni yuklab olish — instaloader orqali"""
    try:
        loop = asyncio.get_running_loop()

        def _download_with_instaloader():
            import instaloader

            L = instaloader.Instaloader(
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True,
                dirname_pattern=str(DOWNLOAD_DIR),
                filename_pattern=f"{chat_id}_{{shortcode}}"
            )

            # URL dan shortcode olish
            shortcode = None
            import re as _re
            match = _re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
            if match:
                shortcode = match.group(1)

            if not shortcode:
                return False

            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
                L.download_post(post, target="")
            except Exception:
                # Target ni DOWNLOAD_DIR ga moslaymiz
                try:
                    post = instaloader.Post.from_shortcode(L.context, shortcode)
                    # Manually download
                    if post.typename == 'GraphSidecar':
                        # Carousel — ko'p rasm
                        for i, node in enumerate(post.get_sidecar_nodes()):
                            if not node.is_video:
                                img_url = node.display_url
                                filepath = DOWNLOAD_DIR / f"{chat_id}_photo_{i}.jpg"
                                import urllib.request
                                urllib.request.urlretrieve(img_url, str(filepath))
                    else:
                        if not post.is_video:
                            img_url = post.url
                            filepath = DOWNLOAD_DIR / f"{chat_id}_photo_0.jpg"
                            import urllib.request
                            urllib.request.urlretrieve(img_url, str(filepath))
                except Exception:
                    return False

            return True

        result = await loop.run_in_executor(None, _download_with_instaloader)

        # Fayllarni tekshirish
        files = [f for f in DOWNLOAD_DIR.iterdir() if f.name.startswith(str(chat_id))]
        if files:
            return files, None

        if not result:
            return await _download_photo_fallback(url, chat_id)

        return None, "❌ Rasm topilmadi."

    except Exception as e:
        # Fallback urinish
        return await _download_photo_fallback(url, chat_id)


async def _download_photo_fallback(url: str, chat_id: int):
    """Fallback: instaloader Post.from_shortcode dan display_url orqali yuklab olish"""
    try:
        import instaloader
        import urllib.request

        loop = asyncio.get_running_loop()

        def _get_photo():
            L = instaloader.Instaloader(quiet=True)

            # URL dan shortcode olish
            import re as _re
            match = _re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
            if not match:
                return None

            shortcode = match.group(1)
            post = instaloader.Post.from_shortcode(L.context, shortcode)

            downloaded = []

            if post.typename == 'GraphSidecar':
                # Carousel post
                for i, node in enumerate(post.get_sidecar_nodes()):
                    if not node.is_video:
                        img_url = node.display_url
                        filepath = DOWNLOAD_DIR / f"{chat_id}_photo_{i}.jpg"
                        urllib.request.urlretrieve(img_url, str(filepath))
                        downloaded.append(filepath)
            else:
                if not post.is_video:
                    img_url = post.url
                    filepath = DOWNLOAD_DIR / f"{chat_id}_photo_0.jpg"
                    urllib.request.urlretrieve(img_url, str(filepath))
                    downloaded.append(filepath)

            return downloaded

        files = await loop.run_in_executor(None, _get_photo)

        if files:
            return files, None
        return None, "❌ Rasm yuklab bo'lmadi."

    except Exception as e:
        err = str(e).lower()
        if "private" in err or "login" in err:
            return None, "❌ Bu post xususiy (private) yoki login talab qiladi."
        return None, f"❌ Rasm yuklab bo'lmadi: `{str(e)[:120]}`"


async def download_audio(url: str, chat_id: int):
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
        "• /favorites — Sevimlilar\n"
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
        "4️⃣ Yuklanib keladi ✅\n\n"
        "💡 **Qo'shimcha:**\n"
        "• Rasm bo'lsa — avtomatik yuklab beradi\n"
        "• Video bo'lsa — Video/Audio tanlash mumkin\n"
        "• Xabar ichida link bo'lsa ham topaman\n"
        "• /favorites — sevimlilar ro'yxati\n\n"
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
            "Instagram havolasi yuboring va \"⭐ Sevimlilarga qo'shish\" tugmasini bosing!"
        )
        return

    text = "⭐ **Sevimlilar:**\n\n"
    for i, url in enumerate(favs, 1):
        short = url.split("?")[0]
        text += f"{i}. {short}\n"

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
# 📨 ASOSIY XABAR HANDLER
# ═══════════════════════════════════════════════════════════

@app.on_message(filters.text & ~filters.command(["start", "help", "stats", "favorites"]))
async def handle_message(client, message: Message):
    text = message.text.strip()
    is_group = message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    # 🔗 Havola aniqlagich
    url = extract_instagram_url(text)

    if is_group:
        if not url:
            return
    else:
        if not url:
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

    # ❤️ Like reaction bosish (xavfsiz — versiyaga bog'liq)
    try:
        await app.send_reaction(message.chat.id, message.id, "❤")
    except AttributeError:
        # Pyrogram versiyasida send_reaction mavjud emas
        pass
    except Exception:
        pass

    # 📊 Progress boshlash — media turini aniqlash
    status = await message.reply(ProgressBar.get_stage_text(0))

    await asyncio.sleep(0.3)
    await update_progress(status, 1)

    # 🔍 Media turini aniqlash
    media_type = await detect_media_type(url)

    if media_type == "photo":
        # 🖼 RASM — to'g'ridan-to'g'ri yuklab yuborish
        await update_progress(status, 2)

        files, error = await download_photo(url, message.chat.id)

        if error:
            await status.edit_text(error)
            return

        await update_progress(status, 4)

        try:
            for filepath in files:
                ext = filepath.suffix.lower()
                if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    try:
                        sent = await message.reply_photo(
                            str(filepath),
                            caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("⭐ Sevimlilarga qo'shish", callback_data=f"fav|{message.id}")]
                            ])
                        )
                        set_cache(url, "photo", {"type": "photo", "file_id": sent.photo.file_id})
                    except Exception:
                        sent = await message.reply_document(
                            str(filepath),
                            caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot"
                        )
                else:
                    sent = await message.reply_document(
                        str(filepath),
                        caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot"
                    )

            # URL ni saqlash (fav tugma uchun)
            pending_urls[message.id] = url

            await update_progress(status, 5)
            await asyncio.sleep(0.8)
            await status.delete()

        except Exception as e:
            await status.edit_text(f"❌ Yuborishda xatolik: `{str(e)[:100]}`")
        finally:
            cleanup(message.chat.id)

    else:
        # 🎬 VIDEO yoki UNKNOWN — avval video, xato bo'lsa rasm sifatida yuklab ko'rish
        await update_progress(status, 2)

        files, error = await download_video(url, message.chat.id)

        # Agar video yuklab bo'lmasa — rasm sifatida urinib ko'ramiz
        if error and ("no video" in str(error).lower() or "not a video" in str(error).lower() or "there is no video" in str(error).lower()):
            cleanup(message.chat.id)
            files, error = await download_photo(url, message.chat.id)

            if error:
                await status.edit_text(error)
                return

            await update_progress(status, 4)

            try:
                for filepath in files:
                    ext = filepath.suffix.lower()
                    if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        try:
                            sent = await message.reply_photo(
                                str(filepath),
                                caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot",
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("⭐ Sevimlilarga qo'shish", callback_data=f"fav|{message.id}")]
                                ])
                            )
                            set_cache(url, "photo", {"type": "photo", "file_id": sent.photo.file_id})
                        except Exception:
                            sent = await message.reply_document(
                                str(filepath),
                                caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot"
                            )
                    else:
                        sent = await message.reply_document(
                            str(filepath),
                            caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot"
                        )

                pending_urls[message.id] = url
                await update_progress(status, 5)
                await asyncio.sleep(0.8)
                await status.delete()

            except Exception as e:
                await status.edit_text(f"❌ Yuborishda xatolik: `{str(e)[:100]}`")
            finally:
                cleanup(message.chat.id)
            return

        if error:
            await status.edit_text(error)
            return

        await update_progress(status, 3)
        await asyncio.sleep(0.3)
        await update_progress(status, 4)

        try:
            for filepath in files:
                ext = filepath.suffix.lower()

                if ext in [".mp4", ".mov", ".mkv", ".webm"]:
                    try:
                        sent = await message.reply_video(
                            str(filepath),
                            caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot",
                            supports_streaming=True,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("⭐ Sevimlilarga qo'shish", callback_data=f"fav|{message.id}")]
                            ])
                        )
                        set_cache(url, "video", {"type": "video", "file_id": sent.video.file_id})
                    except Exception:
                        sent = await message.reply_document(
                            str(filepath),
                            caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("⭐ Sevimlilarga qo'shish", callback_data=f"fav|{message.id}")]
                            ])
                        )
                        set_cache(url, "video", {"type": "document", "file_id": sent.document.file_id})
                else:
                    sent = await message.reply_document(
                        str(filepath),
                        caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("⭐ Sevimlilarga qo'shish", callback_data=f"fav|{message.id}")]
                        ])
                    )

            # URL ni saqlash (fav tugma uchun)
            pending_urls[message.id] = url

            await update_progress(status, 5)
            await asyncio.sleep(0.8)
            await status.delete()

        except Exception as e:
            await status.edit_text(f"❌ Yuborishda xatolik: `{str(e)[:100]}`")
        finally:
            cleanup(message.chat.id)


# ═══════════════════════════════════════════════════════════
# 🔘 CALLBACK HANDLER — Tugma bosilganda
# ═══════════════════════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^fav\|"))
async def callback_favorite(client, callback: CallbackQuery):
    """⭐ Sevimlilarga qo'shish tugmasi bosilganda"""
    msg_id = int(callback.data.split("|")[1])
    user_id = callback.from_user.id
    url = pending_urls.get(msg_id)

    if url:
        add_favorite(user_id, url)
        await callback.answer("⭐ Sevimlilarga qo'shildi!", show_alert=False)
    else:
        await callback.answer("⚠️ Link topilmadi.", show_alert=False)


# ═══════════════════════════════════════════════════════════
# 🚀 BOTNI ISHGA TUSHIRISH
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Flask ni avval ishga tushiramiz — Render port bind ni kutadi
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    logger.info("✅ Flask server ishga tushdi (port bind qilindi)")

    # Biroz kutamiz — port to'liq ochilsin
    import time
    time.sleep(1)

    logger.info("✅ InstaBot ishga tushmoqda...")
    app.run()
