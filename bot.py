import subprocess
subprocess.run(["pip", "install", "--upgrade", "yt-dlp", "pyrogram", "tgcrypto"], capture_output=True)

import os
import asyncio
import logging
import re
import yt_dlp
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

app = Client("instabot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def is_instagram_url(url: str) -> bool:
    pattern = r'(https?://)?(www\.)?instagram\.com/(p|reel|tv|stories)/.+'
    return bool(re.match(pattern, url))


async def download_media(url: str, chat_id: int):
    output_template = str(DOWNLOAD_DIR / f"{chat_id}_%(title).40s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[filesize<50M]/best",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }

    try:
        loop = asyncio.get_event_loop()

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
    await message.reply(
        f"👋 Salom, **{message.from_user.first_name}**!\n\n"
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


@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_message(client, message: Message):
    url = message.text.strip()

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
                        caption="📥 Instagram dan yuklandi | @InstaDownloader_uzBot",
                        supports_streaming=True
                    )
                except Exception:
                    await message.reply_document(str(filepath), caption="📥 Instagram dan yuklandi | @InstaDownloader_uzBot")
            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                try:
                    await message.reply_photo(str(filepath), caption="📥 Instagram dan yuklandi | @InstaDownloader_uzBot")
                except Exception:
                    await message.reply_document(str(filepath), caption="📥 Instagram dan yuklandi | @InstaDownloader_uzBot")
            else:
                await message.reply_document(str(filepath), caption="📥 Instagram dan yuklandi | @InstaDownloader_uzBot")

        await status.delete()

    except Exception as e:
        await status.edit(f"❌ Yuborishda xatolik: `{str(e)[:100]}`")
    finally:
        cleanup(message.chat.id)


if __name__ == "__main__":
    logger.info("✅ InstaBot ishga tushdi")
    app.run()
