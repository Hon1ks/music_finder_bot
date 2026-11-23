"""Music Finder Bot: поиск в Яндекс Музыке / YouTube / SoundCloud, выбор кнопкой -> send_audio."""
import asyncio
import logging
import os
import re
import shutil
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("music_finder")

# FadeHost для Telegram-приложений кладёт токен и в TELEGRAM_BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.environ["TELEGRAM_BOT_TOKEN"]
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN", "").strip()

MAX_BYTES = 49 * 1024 * 1024   # лимит Bot API на отправку файла — 50 МБ
MIN_DURATION = 31              # короче — превью SoundCloud Go+
MAX_DURATION = 15 * 60         # длиннее — фильмы и часовые миксы
MAX_CHOICES = 6
# Одновременно качаем не больше N треков — бережём 256 МБ RAM
DOWNLOADS = asyncio.Semaphore(int(os.getenv("MAX_PARALLEL", "1")))

SOURCE_NAMES = {"ym": "Яндекс Музыка", "yt": "YouTube", "sc": "SoundCloud"}


@dataclass
class Candidate:
    source: str   # ym / yt / sc
    id: str
    performer: str
    title: str
    duration: int | None


@dataclass
class Track:
    path: Path
    title: str
    performer: str
    duration: int | None
    source: str


# Найденные варианты по ключу "source:id" — чтобы по нажатию кнопки знать название трека.
# Ограничено по размеру; после перезапуска старые кнопки качают по id без запасного поиска.
_candidates: OrderedDict[str, Candidate] = OrderedDict()


def _remember(c: Candidate) -> str:
    key = f"{c.source}:{c.id}"
    _candidates[key] = c
    _candidates.move_to_end(key)
    while len(_candidates) > 1000:
        _candidates.popitem(last=False)
    return key


# ---------- Яндекс Музыка ----------

_ym_client = None


async def yandex_client():
    global _ym_client
    if _ym_client is None:
        from yandex_music import ClientAsync
        _ym_client = await ClientAsync(YANDEX_TOKEN).init()
    return _ym_client


async def yandex_search(query: str) -> list[Candidate]:
    if not YANDEX_TOKEN:
        return []
    client = await yandex_client()
    result = await client.search(query, type_="track")
    if not result or not result.tracks:
        return []
    return [
        Candidate("ym", str(t.id), ", ".join(t.artists_name()), t.title or "",
                  (t.duration_ms or 0) // 1000 or None)
        for t in result.tracks.results[:MAX_CHOICES] if t.available
    ]


async def yandex_download(c: Candidate, workdir: Path) -> Track | None:
    client = await yandex_client()
    tracks = await client.tracks([c.id])
    if not tracks:
        return None
    path = workdir / "track.mp3"
    await tracks[0].download_async(str(path), codec="mp3", bitrate_in_kbps=192)
    return Track(path, c.title, c.performer, c.duration, SOURCE_NAMES["ym"])


# ---------- YouTube Music: поиск песен напрямую, без ключей ----------

_YTM_URL = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false"
_YTM_BODY = {
    "context": {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20250101.01.00", "hl": "ru"}},
    "params": "EgWKAQIIAWoKEAkQBRAKEAMQBA==",  # фильтр «Песни»
}


def _seconds(text: str) -> int:
    total = 0
    for part in text.split(":"):
        total = total * 60 + int(part)
    return total


async def ytmusic_search(query: str) -> list[Candidate]:
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(_YTM_URL, json={**_YTM_BODY, "query": query},
                                headers={"Origin": "https://music.youtube.com"}) as resp:
            resp.raise_for_status()
            data = await resp.json()
    tabs = data["contents"]["tabbedSearchResultsRenderer"]["tabs"]
    result = []
    for section in tabs[0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"]:
        for item in section.get("musicShelfRenderer", {}).get("contents", []):
            row = item["musicResponsiveListItemRenderer"]
            video_id = (row.get("playlistItemData") or {}).get("videoId")
            title, info = ("".join(r["text"] for r in col["musicResponsiveListItemFlexColumnRenderer"]["text"]
                                   .get("runs", [])) for col in row["flexColumns"][:2])
            # info: «Исполнитель • Альбом • 3:45»
            parts = info.split(" • ")
            duration = _seconds(parts[-1]) if re.fullmatch(r"[\d:]+", parts[-1]) else 0
            if video_id and MIN_DURATION < duration < MAX_DURATION:
                result.append(Candidate("yt", video_id, parts[0], title, duration))
            if len(result) == MAX_CHOICES:
                return result
    return result


# ---------- YouTube / SoundCloud через yt-dlp ----------

_YTDLP_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    # Без JS-рантайма: deno/node вместе с aiogram не влезают в 256 МБ (OOM)
    "js_runtimes": {},
}
_IE_KEYS = {"yt": "Youtube", "sc": "Soundcloud"}
_SEARCH = {"yt": "ytsearch12", "sc": "scsearch12"}  # YouTube ищем через YouTube Music, здесь — запасной путь
_URLS = {"yt": "https://www.youtube.com/watch?v={}", "sc": "https://api.soundcloud.com/tracks/{}"}


def _ytdlp_search(source: str, query: str) -> list[Candidate]:
    import yt_dlp  # тяжёлый модуль — грузим только когда он реально нужен

    with yt_dlp.YoutubeDL({**_YTDLP_BASE, "extract_flat": "in_playlist"}) as ydl:
        found = ydl.extract_info(f"{_SEARCH[source]}:{query}", download=False)
    result = []
    for e in (found or {}).get("entries") or []:
        duration = int(e.get("duration") or 0)
        # в выдаче бывают каналы, плейлисты, фильмы и шортсы — их пропускаем
        if e.get("ie_key") != _IE_KEYS[source] or not MIN_DURATION < duration < MAX_DURATION:
            continue
        performer, title = _split_title(e.get("title") or "", e.get("channel") or e.get("uploader") or "")
        result.append(Candidate(source, str(e["id"]), performer, title, duration))
        if len(result) == MAX_CHOICES:
            break
    return result


_JUNK = re.compile(r"\s*[(\[][^)\]]*(official|video|клип|lyric|audio|visuali[sz]er|премьера|hd|4k)[^)\]]*[)\]]", re.I)


def _split_title(title: str, uploader: str) -> tuple[str, str]:
    """«Макс Корж — Мотылёк (official video)» -> («Макс Корж», «Мотылёк»); иначе исполнитель = канал."""
    title = _JUNK.sub("", title).strip()
    parts = re.split(r"\s[-—–]\s", title, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return uploader, title


def _ytdlp_download(c: Candidate, workdir: Path) -> Track | None:
    import yt_dlp

    opts = {
        **_YTDLP_BASE,
        # превью SoundCloud Go+ (30 сек) не берём
        "format": "bestaudio[ext=m4a][format_id!*=preview]/bestaudio[format_id!*=preview]",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_BYTES,
        # AAC из m4a копируется без перекодирования, opus/webm ffmpeg перегоняет в m4a
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            entry = ydl.extract_info(_URLS[c.source].format(c.id), download=True)
    except yt_dlp.utils.DownloadError as e:  # DRM, бот-проверка YouTube, только превью
        log.info("%s %s not downloadable: %s", c.source, c.id, str(e)[:200])
        return None
    files = [p for p in workdir.iterdir() if p.suffix in (".mp3", ".m4a")]
    if not entry or not files:
        return None
    return Track(
        path=files[0],
        title=c.title or entry.get("title") or "",
        performer=c.performer or entry.get("uploader") or "",
        duration=int(entry["duration"]) if entry.get("duration") else c.duration,
        source=SOURCE_NAMES[c.source],
    )


# ---------- общий поиск и загрузка ----------

async def search_candidates(query: str) -> list[Candidate]:
    try:
        if found := await yandex_search(query):
            return found
    except Exception:
        log.exception("Yandex Music search failed for %r", query)
    try:
        if found := await ytmusic_search(query):
            return found
    except Exception:
        log.exception("YouTube Music search failed for %r", query)
    try:
        return await asyncio.to_thread(_ytdlp_search, "sc", query)
    except Exception:
        log.exception("SoundCloud search failed for %r", query)
        return []


async def download(c: Candidate, workdir: Path) -> Track | None:
    try:
        if c.source == "ym":
            track = await yandex_download(c, workdir)
        else:
            track = await asyncio.to_thread(_ytdlp_download, c, workdir)
        if track:
            return track
    except Exception:
        log.exception("Download failed for %s:%s", c.source, c.id)
    if c.source == "sc" or not c.title:
        return None
    # YouTube блокирует IP хостингов («Sign in to confirm you're not a bot») — ищем то же на SoundCloud
    query = f"{c.performer} {c.title}" if c.performer.lower() not in c.title.lower() else c.title
    alts = await asyncio.to_thread(_ytdlp_search, "sc", query)
    if c.duration:  # сначала загрузки той же длины — скорее всего это тот же трек, а не ремикс
        alts.sort(key=lambda a: abs((a.duration or 0) - c.duration) > 5)
    for alt in alts:
        if track := await asyncio.to_thread(_ytdlp_download, alt, workdir):
            return track
    return None


# ---------- Telegram ----------

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Напиши исполнителя и/или название трека, например:\n<code>Кино Группа крови</code>\n\n"
        "Если вариантов несколько — выбери нужный кнопкой."
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def search(message: Message) -> None:
    query = message.text.strip()[:200]
    status = await message.answer(f"🔎 Ищу: <b>{_escape(query)}</b>…")
    found = await search_candidates(query)
    if not found:
        await status.edit_text("😔 Ничего не нашёл. Попробуй уточнить запрос.")
        return
    if len(found) == 1:
        await status.edit_text(f"⏳ Качаю: <b>{_escape(_label(found[0]))}</b>…")
        await send_track(message, status, found[0])
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_label(c, with_duration=True), callback_data=_remember(c))] for c in found
    ])
    await status.edit_text(f"Что нашлось по запросу <b>{_escape(query)}</b>:", reply_markup=keyboard)


@dp.callback_query(F.data.regexp(r"^(ym|yt|sc):[\w-]+$"))
async def choose(callback: CallbackQuery) -> None:
    source, _, track_id = callback.data.partition(":")
    c = _candidates.get(callback.data) or Candidate(source, track_id, "", "", None)
    await callback.answer("Качаю…")
    status = await callback.message.answer(f"⏳ Качаю: <b>{_escape(_label(c) or 'трек')}</b>…")
    await send_track(callback.message, status, c)


async def send_track(chat: Message, status: Message, c: Candidate) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="mf_"))
    try:
        async with DOWNLOADS:
            track = await download(c, workdir)
        if not track:
            await status.edit_text("😔 Этот трек скачать не получилось. Выбери другой вариант.")
            return
        if track.path.stat().st_size > MAX_BYTES:
            await status.edit_text("Файл больше 50 МБ — Telegram не даст его отправить.")
            return
        await chat.answer_audio(
            FSInputFile(track.path, filename=_filename(track)),
            title=track.title,
            performer=track.performer,
            duration=track.duration,
            caption=f"Источник: {track.source}",
        )
        await status.delete()
    except Exception:
        log.exception("Failed to send %s:%s", c.source, c.id)
        await status.edit_text("⚠️ Ошибка при загрузке, попробуй ещё раз.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # файл удаляется сразу после отправки


def _label(c: Candidate, with_duration: bool = False) -> str:
    text = f"{c.performer} — {c.title}" if c.performer and c.performer.lower() not in c.title.lower() else c.title
    if with_duration and c.duration:
        text = f"{text[:52]} · {c.duration // 60}:{c.duration % 60:02d}"
    return text


def _filename(track: Track) -> str:
    name = f"{track.performer} - {track.title}" if track.performer else track.title
    return "".join(ch for ch in name if ch not in '\\/:*?"<>|')[:100] + track.path.suffix


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def main() -> None:
    from aiogram.client.default import DefaultBotProperties

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    if not YANDEX_TOKEN:
        log.warning("YANDEX_TOKEN не задан — ищу только на YouTube и SoundCloud")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
