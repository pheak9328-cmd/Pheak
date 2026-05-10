import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL

load_dotenv()

BOT_NAME = "I'M TRA"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads")).resolve()
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "49"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(BOT_NAME)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "open.spotify.com",
    "spotify.link",
)


def is_supported_url(text: str) -> bool:
    match = URL_RE.search(text or "")
    if not match:
        return False
    host = urlparse(match.group(0)).netloc.lower().replace("www.", "")
    return any(domain in host for domain in SUPPORTED_HOSTS)


def extract_first_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).strip() if match else None


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def clean_filename(name: str) -> str:
    return re.sub(r"[^\w\-.() \[\]ក-អា-ោះ-៎]+", "_", name).strip()[:120] or "audio"


def spotify_to_search_query(url: str) -> str:
    """Resolve a Spotify URL into a YouTube search query using Spotify oEmbed when available."""
    try:
        response = requests.get(
            "https://open.spotify.com/oembed",
            params={"url": url},
            timeout=12,
            headers={"User-Agent": f"{BOT_NAME} Telegram Bot"},
        )
        if response.ok:
            data = response.json()
            title = data.get("title") or ""
            if title:
                return f"{title} official audio"
    except Exception as exc:
        logger.warning("Could not resolve Spotify metadata: %s", exc)
    return url


def build_ydl_options(output_dir: Path) -> dict:
    ffmpeg_available = has_ffmpeg()
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(title).120s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "continuedl": False,
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
    }
    if ffmpeg_available:
        options.update(
            {
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
                "postprocessor_args": ["-ar", "44100"],
            }
        )
    return options


def download_audio(query_or_url: str) -> tuple[Path, dict]:
    with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as temp_dir:
        temp_path = Path(temp_dir)
        target = query_or_url.strip()
        if "open.spotify.com" in target or "spotify.link" in target:
            target = spotify_to_search_query(target)
        elif not URL_RE.search(target):
            target = f"ytsearch1:{target} audio"

        ydl_options = build_ydl_options(temp_path)
        with YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(target, download=True)
            if "entries" in info:
                info = next((entry for entry in info["entries"] if entry), None)
            if not info:
                raise RuntimeError("រកមិនឃើញបទចម្រៀងនេះទេ។")

            candidates = list(temp_path.glob(f"*[{info.get('id')}].*")) + list(temp_path.glob("*"))
            candidates = [p for p in candidates if p.is_file()]
            if not candidates:
                raise RuntimeError("ទាញយកមិនបានទេ។")

            audio_file = max(candidates, key=lambda p: p.stat().st_size)
            final_name = clean_filename(audio_file.name)
            final_path = DOWNLOAD_DIR / final_name
            counter = 1
            while final_path.exists():
                final_path = DOWNLOAD_DIR / f"{final_path.stem}_{counter}{final_path.suffix}"
                counter += 1
            shutil.move(str(audio_file), final_path)

            size_mb = final_path.stat().st_size / (1024 * 1024)
            if size_mb > MAX_FILE_MB:
                final_path.unlink(missing_ok=True)
                raise RuntimeError(f"File ធំពេក ({size_mb:.1f} MB)។ សូមសាកល្បងបទខ្លីជាងនេះ។")
            return final_path, info


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        f"សួស្តី! ខ្ញុំជា {BOT_NAME} Music Bot.\n\n"
        "ប្រើ៖\n"
        "• /song name ដើម្បីស្វែងរកបទ\n"
        "• ផ្ញើ link YouTube ឬ Spotify ដោយផ្ទាល់\n\n"
        "ឧទាហរណ៍៖ /song faded alan walker"
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "របៀបប្រើ៖ /song <ឈ្មោះបទ> ឬផ្ញើ YouTube/Spotify link។ "
        "Spotify link នឹងត្រូវយកឈ្មោះបទទៅស្វែងរកលើ YouTube ដើម្បីផ្ញើជា audio។"
    )


async def song(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("សូមវាយ /song name ឧទាហរណ៍៖ /song shape of you")
        return
    await process_request(update, query)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    url = extract_first_url(text)
    if not url or not is_supported_url(url):
        await update.message.reply_text("សូមផ្ញើ YouTube ឬ Spotify link ឬប្រើ /song name។")
        return
    await process_request(update, url)


async def process_request(update: Update, query_or_url: str) -> None:
    message = update.message
    await message.chat.send_action(ChatAction.TYPING)
    notice = await message.reply_text("កំពុងស្វែងរក និងទាញយក audio... សូមរង់ចាំបន្តិច។")

    try:
        await message.chat.send_action(ChatAction.UPLOAD_AUDIO)
        audio_path, info = await asyncio.to_thread(download_audio, query_or_url)
        title = info.get("title") or audio_path.stem
        artist = info.get("uploader") or info.get("artist") or BOT_NAME
        duration = info.get("duration")

        caption = f"{BOT_NAME} | {title}"
        with audio_path.open("rb") as audio:
            await message.reply_audio(
                audio=audio,
                title=title[:64],
                performer=str(artist)[:64],
                duration=duration if isinstance(duration, int) else None,
                caption=caption[:1024],
                read_timeout=120,
                write_timeout=120,
                connect_timeout=60,
            )
        await notice.delete()
        audio_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.exception("Download/send failed")
        await notice.edit_text(f"សុំទោស មានបញ្ហា៖ {exc}")


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN មិនទាន់បានកំណត់។ សូមបង្កើត file .env ឬ export BOT_TOKEN។")

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("song", song))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    if WEBHOOK_URL:
        logger.info("Starting bot with webhook mode at %s", WEBHOOK_URL)
        async with app:
            await app.bot.set_webhook(url=WEBHOOK_URL)
            await app.start()
            await app.updater.start_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=BOT_TOKEN,
                webhook_url=WEBHOOK_URL,
            )
            logger.info("%s is running in webhook mode on port %d", BOT_NAME, PORT)
            await asyncio.Event().wait()
    else:
        logger.info("Starting bot with polling mode")
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    asyncio.run(main())
