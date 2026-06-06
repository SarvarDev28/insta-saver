import os
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
from pyrogram.types import Message
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

# Redis ulanishi (ixtiyoriy — REDIS_URL bo'lmasa statistika ishlamaydi)
r = None
if REDIS_URL:
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        logger.info("✅ Redis ga ulandi")
    except Exception as e:
        logger.warning(f"⚠️ Redis ga ulanib bo'lmadi: {e}. Statistika o'chirilgan.")
        r = None

app = Client("instabot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Flask keep-alive
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot ishlayapti ✅"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


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


def is_instagram_url(url: str) -> bool:
    pattern = r'(https?://)?(www\.)?(instagram\.com|instagr\.am)/(p|reel|reels|tv|stories)/[^\s]+'
    return bool(re.match(pattern, url))


async def download_media(url: str, chat_id: int):
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


@app.on_message(filters.command("start"))
async def cmd_start(client, message: Message):
    user = message.from_user
    save_user(user.id)

    await message.reply(
        f"👋 Salom, **{user.first_name}**!\n\n"
        "📥 Men **Instagram Saver** botman.\n\n"
        "📌 Nima yuklay olaman:\n"
        "• 🎬 Reels\n"
        "• 🖼 Postlar (rasm/video)\n"
        "• 📖 Stories\n\n"
        "➡️ Faqat Instagram havolasini yuboring!"
    )


@app.on_message(filters.command("help"))
async def cmd_help(client, message: Message):
    await message.reply(
        "📖 **Qo'llanma:**\n\n"
        "1️⃣ Instagram da post/reel/story ni oching\n"
        "2️⃣ Havolasini nusxalang\n"
        "3️⃣ Menga yuboring\n"
        "4️⃣ Yuklanib keladi ✅\n\n"
        "⚠️ **Ishlamaydi:**\n"
        "• Private akkaunt postlari\n"
        "• O'chirilgan postlar\n\n"
        "📩 **Talab va takliflar:** @theSarvar_04"
    )


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


@app.on_message(filters.text & ~filters.command(["start", "help", "stats"]))
async def handle_message(client, message: Message):
    url = message.text.strip()

    is_group = message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    if is_group:
        if not is_instagram_url(url):
            return
    else:
        if not url.startswith("http"):
            await message.reply("📎 Instagram havolasini yuboring.\n\nMasalan:\n`https://www.instagram.com/reel/ABC123/`")
            return
        if not is_instagram_url(url):
            await message.reply("⚠️ Faqat **Instagram** havolalarini qabul qilaman.\n\nMasalan:\n`https://www.instagram.com/reel/...`")
            return

    status = await message.reply("⏳ Yuklanmoqda...")

    files, error = await download_media(url, message.chat.id)

    if error:
        await status.edit(error)
        return

    try:
        await status.edit("📤 Yuborilmoqda...")

        for filepath in files:
            ext = filepath.suffix.lower()
            if ext in [".mp4", ".mov", ".mkv", ".webm"]:
                try:
                    await message.reply_video(
                        str(filepath),
                        caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot",
                        supports_streaming=True
                    )
                except Exception:
                    await message.reply_document(str(filepath), caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot")
            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                try:
                    await message.reply_photo(str(filepath), caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot")
                except Exception:
                    await message.reply_document(str(filepath), caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot")
            else:
                await message.reply_document(str(filepath), caption="📥 Instagram dan yuklandi\n@InstaDownloader_uzBot")

        await status.delete()

    except Exception as e:
        await status.edit(f"❌ Yuborishda xatolik: `{str(e)[:100]}`")
    finally:
        cleanup(message.chat.id)


if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    logger.info("✅ InstaBot ishga tushdi")
    app.run()
