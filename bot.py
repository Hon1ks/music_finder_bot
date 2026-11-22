"""Music Finder Bot: Яндекс Музыка -> YouTube (yt-dlp) -> send_audio."""
import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("music_finder")

# FadeHost для Telegram-приложений кладёт токен и в TELEGRAM_BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.environ["TELEGRAM_BOT_TOKEN"]
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN", "").strip()

MAX_BYTES = 49 * 1024 * 1024   # лимит Bot API на отправку файла — 50 МБ
MAX_DURATION = 15 * 60         # не качаем часовые миксы с YouTube
# Одновременно качаем не больше N треков — бережём 256 МБ RAM
DOWNLOADS = asyncio.Semaphore(int(os.getenv("MAX_PARALLEL", "1")))


@dataclass
class Track:
    path: Path
    title: str
    performer: str
    duration: int | None
    source: str


_ym_client = None


async def yandex_client():
    global _ym_client
    if _ym_client is None:
        from yandex_music import ClientAsync
        _ym_client = await ClientAsync(YANDEX_TOKEN).init()
    return _ym_client


async def from_yandex(query: str, workdir: Path) -> Track | None:
    if not YANDEX_TOKEN:
        return None
    client = await yandex_client()
    result = await client.search(query, type_="track")
    if not result or not result.tracks or not result.tracks.results:
        return None
    track = result.tracks.results[0]
    if not track.available:
        return None
    path = workdir / "track.mp3"
    await track.download_async(str(path), codec="mp3", bitrate_in_kbps=192)
    return Track(
        path=path,
        title=track.title or query,
        performer=", ".join(track.artists_name()) or "",
        duration=(track.duration_ms or 0) // 1000 or None,
        source="Яндекс Музыка",
    )


def _youtube_download(query: str, workdir: Path) -> Track | None:
    import yt_dlp  # тяжёлый модуль — грузим только когда он реально нужен

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_BYTES,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration < {MAX_DURATION}"),
        # Без JS-рантайма: deno/node вместе с aiogram не влезают в 256 МБ (OOM)
        "js_runtimes": {},
        # AAC из m4a копируется без перекодирования, opus/webm ffmpeg перегоняет в m4a
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=True)
    entries = (info or {}).get("entries") or []
    if not entries or not entries[0]:
        return None
    entry = entries[0]
    files = [p for p in workdir.iterdir() if p.suffix in (".mp3", ".m4a")]
    if not files:
        return None
    return Track(
        path=files[0],
        title=entry.get("track") or entry.get("title") or query,
        performer=entry.get("artist") or entry.get("uploader") or "",
        duration=int(entry["duration"]) if entry.get("duration") else None,
        source="YouTube",
    )


async def from_youtube(query: str, workdir: Path) -> Track | None:
    return await asyncio.to_thread(_youtube_download, query, workdir)


async def find_track(query: str, workdir: Path) -> Track | None:
    try:
        track = await from_yandex(query, workdir)
        if track:
            return track
    except Exception:
        log.exception("Yandex Music failed for %r", query)
    try:
        return await from_youtube(query, workdir)
    except Exception:
        log.exception("YouTube failed for %r", query)
        return None


dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Напиши исполнителя и название трека, например:\n<code>Кино Группа крови</code>")


@dp.message(F.text & ~F.text.startswith("/"))
async def search(message: Message) -> None:
    query = message.text.strip()[:200]
    status = await message.answer(f"🔎 Ищу: <b>{_escape(query)}</b>…")
    workdir = Path(tempfile.mkdtemp(prefix="mf_"))
    try:
        async with DOWNLOADS:
            track = await find_track(query, workdir)
        if not track:
            await status.edit_text("😔 Ничего не нашёл. Попробуй уточнить запрос.")
            return
        if track.path.stat().st_size > MAX_BYTES:
            await status.edit_text("Файл больше 50 МБ — Telegram не даст его отправить.")
            return
        await message.answer_audio(
            FSInputFile(track.path, filename=_filename(track)),
            title=track.title,
            performer=track.performer,
            duration=track.duration,
            caption=f"Источник: {track.source}",
        )
        await status.delete()
    except Exception:
        log.exception("Failed to handle %r", query)
        await status.edit_text("⚠️ Ошибка при загрузке, попробуй ещё раз.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # файл удаляется сразу после отправки


def _filename(track: Track) -> str:
    name = f"{track.performer} - {track.title}" if track.performer else track.title
    return "".join(c for c in name if c not in '\\/:*?"<>|')[:100] + track.path.suffix


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def main() -> None:
    from aiogram.client.default import DefaultBotProperties

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    if not YANDEX_TOKEN:
        log.warning("YANDEX_TOKEN не задан — ищу только на YouTube")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
