"""Music Finder Bot: поиск в Яндекс Музыке / YouTube Music / SoundCloud, выбор кнопкой, очередь загрузок."""
import asyncio
import itertools
import logging
import os
import re
import shutil
import tempfile
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("music_finder")

# FadeHost для Telegram-приложений кладёт токен и в TELEGRAM_BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.environ["TELEGRAM_BOT_TOKEN"]
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN", "").strip()
# Прокси для YouTube (например, socks5://127.0.0.1:40000) — YouTube блокирует IP серверов
YT_PROXY = os.getenv("YT_PROXY", "").strip()

MAX_BYTES = 49 * 1024 * 1024   # лимит Bot API на отправку файла — 50 МБ
MIN_DURATION = 31              # короче — превью SoundCloud Go+
MAX_DURATION = 15 * 60         # длиннее — фильмы и часовые миксы
PAGE_SIZE = 8
MAX_RESULTS = 60
# Сколько треков качается одновременно (каждая загрузка ~50 МБ RAM сверх базовых ~170 МБ)
WORKERS = int(os.getenv("MAX_PARALLEL", "1"))
MAX_PER_USER = 5               # треков в очереди на одного пользователя

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


@dataclass
class SearchSession:
    query: str
    items: list[Candidate]
    more: str | None = None   # токен следующей страницы YouTube Music


@dataclass
class Job:
    chat: Message
    status: Message
    candidate: Candidate
    user_id: int
    key: str
    text: str = ""            # последний показанный текст статуса
    progress: dict = field(default_factory=dict)   # пишется из потока yt-dlp


# ---------- память поисков (по ним работают кнопки) ----------

_sessions: OrderedDict[str, SearchSession] = OrderedDict()
_session_ids = itertools.count(1)


def _new_session(s: SearchSession) -> str:
    sid = format(next(_session_ids), "x")
    _sessions[sid] = s
    while len(_sessions) > 300:
        _sessions.popitem(last=False)
    return sid


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
        for t in result.tracks.results if t.available
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
_YTM_CONTEXT = {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20250101.01.00", "hl": "ru"}}
_YTM_SONGS = "EgWKAQIIAWoKEAkQBRAKEAMQBA=="  # фильтр «Песни»


def _find(obj, key: str):
    """Все значения ключа `key` на любой глубине JSON."""
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for value in obj.values():
            yield from _find(value, key)
    elif isinstance(obj, list):
        for value in obj:
            yield from _find(value, key)


def _seconds(text: str) -> int:
    total = 0
    for part in text.split(":"):
        total = total * 60 + int(part)
    return total


async def ytmusic_search(query: str, more: str | None = None) -> tuple[list[Candidate], str | None]:
    """Одна страница (~20 песен) и токен следующей."""
    import aiohttp

    url, body = _YTM_URL, {"context": _YTM_CONTEXT, "query": query, "params": _YTM_SONGS}
    if more:
        url, body = f"{_YTM_URL}&ctoken={more}&continuation={more}&type=next", {"context": _YTM_CONTEXT}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(url, json=body, headers={"Origin": "https://music.youtube.com"}) as resp:
            resp.raise_for_status()
            data = await resp.json()
    result = []
    for row in _find(data, "musicResponsiveListItemRenderer"):
        video_id = (row.get("playlistItemData") or {}).get("videoId")
        title, info = ("".join(r["text"] for r in col["musicResponsiveListItemFlexColumnRenderer"]["text"]
                               .get("runs", [])) for col in row["flexColumns"][:2])
        # info: «Исполнитель • Альбом • 3:45»
        parts = info.split(" • ")
        duration = _seconds(parts[-1]) if re.fullmatch(r"[\d:]+", parts[-1]) else 0
        if video_id and MIN_DURATION < duration < MAX_DURATION:
            result.append(Candidate("yt", video_id, parts[0], title, duration))
    token = next(_find(data, "nextContinuationData"), {}).get("continuation")
    return result, token


# ---------- YouTube / SoundCloud через yt-dlp ----------

_YTDLP_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    # Без JS-рантайма: deno/node вместе с aiogram не влезают в 256 МБ (OOM)
    "js_runtimes": {},
}
_URLS = {"yt": "https://www.youtube.com/watch?v={}", "sc": "https://api.soundcloud.com/tracks/{}"}
_JUNK = re.compile(r"\s*[(\[][^)\]]*(official|video|клип|lyric|audio|visuali[sz]er|премьера|hd|4k)[^)\]]*[)\]]", re.I)
# Признаки «не оригинала» — такие загрузки SoundCloud не берём, если их нет в названии самого трека
_NOT_ORIGINAL = re.compile(
    r"remix|rmx|cover|slowed|sped|speed\s?up|nightcore|karaoke|instrumental|acapella|minus|"
    r"bass\s?boost|8d|reverb|mashup|bootleg|flip|live|ремикс|кавер|минус|караоке|концерт|акустик", re.I)


def _split_title(title: str, uploader: str) -> tuple[str, str]:
    """«Макс Корж — Мотылёк (official video)» -> («Макс Корж», «Мотылёк»); иначе исполнитель = канал."""
    title = _JUNK.sub("", title).strip()
    parts = re.split(r"\s[-—–]\s", title, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return uploader, title


def _soundcloud_search(query: str, limit: int) -> list[Candidate]:
    import yt_dlp  # тяжёлый модуль — грузим только когда он реально нужен

    with yt_dlp.YoutubeDL({**_YTDLP_BASE, "extract_flat": "in_playlist"}) as ydl:
        found = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
    result = []
    for e in (found or {}).get("entries") or []:
        duration = int(e.get("duration") or 0)
        if e.get("ie_key") != "Soundcloud" or not MIN_DURATION < duration < MAX_DURATION:
            continue
        performer, title = _split_title(e.get("title") or "", e.get("uploader") or "")
        result.append(Candidate("sc", str(e["id"]), performer, title, duration))
    return result


def _ytdlp_download(c: Candidate, workdir: Path, progress: dict) -> Track | None:
    import yt_dlp

    def hook(d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress["pct"] = d.get("downloaded_bytes", 0) / total
            elif d.get("fragment_count"):  # HLS у SoundCloud
                progress["pct"] = (d.get("fragment_index") or 0) / d["fragment_count"]

    opts = {
        **_YTDLP_BASE,
        # превью SoundCloud Go+ (30 сек) не берём
        "format": "bestaudio[ext=m4a][format_id!*=preview]/bestaudio[format_id!*=preview]",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_BYTES,
        "progress_hooks": [hook],
        # AAC из m4a копируется без перекодирования, opus/webm ffmpeg перегоняет в m4a
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}],
    }
    if c.source == "yt" and YT_PROXY:
        opts["proxy"] = YT_PROXY
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


def _same_track(original: Candidate, alt: Candidate) -> int | None:
    """Насколько загрузка SoundCloud похожа на оригинал: меньше — лучше, None — точно не он."""
    if _NOT_ORIGINAL.search(f"{alt.performer} {alt.title}") and not _NOT_ORIGINAL.search(original.title):
        return None
    text = f"{alt.performer} {alt.title}".lower()
    for part in (original.title, original.performer):  # и название, и исполнитель должны совпасть
        words = {w for w in re.findall(r"\w+", part.lower()) if len(w) > 2} or {part.lower()}
        if part and not any(w in text for w in words):
            return None
    diff = abs((alt.duration or 0) - (original.duration or 0)) if original.duration else 0
    return diff if diff <= 15 else None


# ---------- общий поиск и загрузка ----------

async def search(query: str) -> SearchSession:
    try:
        if found := await yandex_search(query):
            return SearchSession(query, found)
    except Exception:
        log.exception("Yandex Music search failed for %r", query)
    try:
        found, more = await ytmusic_search(query)
        if found:
            return SearchSession(query, found, more)
    except Exception:
        log.exception("YouTube Music search failed for %r", query)
    try:
        return SearchSession(query, await asyncio.to_thread(_soundcloud_search, query, 40))
    except Exception:
        log.exception("SoundCloud search failed for %r", query)
        return SearchSession(query, [])


async def download(job: Job, workdir: Path) -> Track | None:
    c = job.candidate
    try:
        if c.source == "ym":
            track = await yandex_download(c, workdir)
        else:
            track = await asyncio.to_thread(_ytdlp_download, c, workdir, job.progress)
        if track:
            return track
    except Exception:
        log.exception("Download failed for %s:%s", c.source, c.id)
    if c.source == "sc" or not c.title:
        return None
    # YouTube блокирует IP серверов («Sign in to confirm you're not a bot») — ищем тот же трек на SoundCloud
    job.progress.update(stage="🔁 YouTube не отдал файл, ищу этот трек на SoundCloud…", pct=None)
    alts = await asyncio.to_thread(_soundcloud_search, f"{c.performer} {c.title}", 12)
    scored = sorted((score, i, alt) for i, alt in enumerate(alts) if (score := _same_track(c, alt)) is not None)
    for _, _, alt in scored[:4]:
        job.progress.update(stage="⬇️ Скачиваю с SoundCloud", pct=0.0)
        if track := await asyncio.to_thread(_ytdlp_download, alt, workdir, job.progress):
            return track
    return None


# ---------- очередь загрузок ----------

_queue: asyncio.Queue[Job] = asyncio.Queue()
_waiting: deque[Job] = deque()     # стоят в очереди, ещё не начались
_active: set[str] = set()          # ключи "user:трек" в очереди и в работе — от повторных нажатий


def _bar(pct: float) -> str:
    filled = round(pct * 10)
    return "▰" * filled + "▱" * (10 - filled) + f" {pct:.0%}"


async def _set_status(job: Job, text: str) -> None:
    if text == job.text:
        return
    job.text = text
    try:
        await job.status.edit_text(text)
    except TelegramBadRequest:
        pass  # «message is not modified» или сообщение удалили


async def _refresh_queue_positions() -> None:
    for position, job in enumerate(list(_waiting), start=1):
        await _set_status(job, f"🕐 В очереди: {position}-й\n<b>{_escape(_label(job.candidate))}</b>")


async def _show_progress(job: Job) -> None:
    """Раз в 1.5 сек переносит прогресс из потока загрузки в сообщение."""
    name = f"<b>{_escape(_label(job.candidate))}</b>"
    while True:
        stage = job.progress.get("stage", "⬇️ Скачиваю")
        pct = job.progress.get("pct")
        await _set_status(job, f"{stage}\n{name}" + (f"\n{_bar(pct)}" if pct is not None else ""))
        await asyncio.sleep(1.5)


async def worker() -> None:
    while True:
        job = await _queue.get()
        _waiting.remove(job)
        asyncio.create_task(_refresh_queue_positions())
        try:
            await process(job)
        except Exception:
            log.exception("Job failed: %s", job.key)
        finally:
            _active.discard(job.key)


async def process(job: Job) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="mf_"))
    job.progress.update(stage="⬇️ Скачиваю", pct=0.0 if job.candidate.source != "ym" else None)
    ticker = asyncio.create_task(_show_progress(job))
    try:
        started = time.monotonic()
        track = await download(job, workdir)
        if not track:
            ticker.cancel()
            await _set_status(job, f"😔 Не удалось скачать оригинал:\n<b>{_escape(_label(job.candidate))}</b>\n"
                                   "Попробуй другой вариант из списка.")
            return
        if track.path.stat().st_size > MAX_BYTES:
            ticker.cancel()
            await _set_status(job, "Файл больше 50 МБ — Telegram не даст его отправить.")
            return
        job.progress.update(stage="📤 Отправляю в Telegram", pct=None)
        await job.chat.answer_audio(
            FSInputFile(track.path, filename=_filename(track)),
            title=track.title,
            performer=track.performer,
            duration=track.duration,
            caption=f"Источник: {track.source}",
        )
        ticker.cancel()
        await job.status.delete()
        log.info("Sent %s in %.0fs from %s", job.key, time.monotonic() - started, track.source)
    except Exception:
        ticker.cancel()
        log.exception("Failed to send %s", job.key)
        await _set_status(job, "⚠️ Ошибка при загрузке, попробуй ещё раз.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # файл удаляется сразу после отправки


# ---------- Telegram ----------

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Напиши исполнителя и/или название трека, например:\n<code>Кино Группа крови</code>\n\n"
        "Выбери нужный вариант кнопкой — можно несколько, они встанут в очередь."
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def on_query(message: Message) -> None:
    query = message.text.strip()[:200]
    status = await message.answer(f"🔎 Ищу: <b>{_escape(query)}</b>…")
    session = await search(query)
    if not session.items:
        await status.edit_text("😔 Ничего не нашёл. Попробуй уточнить запрос.")
        return
    sid = _new_session(session)
    if len(session.items) == 1:
        await status.edit_text(f"Нашёл: <b>{_escape(_label(session.items[0]))}</b>")
        await enqueue(message, message.from_user.id, sid, 0)
        return
    await status.edit_text(_page_text(session), reply_markup=_page_keyboard(sid, session, 0))


@dp.callback_query(F.data.startswith("p:"))
async def on_page(callback: CallbackQuery) -> None:
    _, sid, page = callback.data.split(":")
    session, page = _sessions.get(sid), int(page)
    if not session:
        await callback.answer("Поиск устарел — отправь запрос заново", show_alert=True)
        return
    # листаем дальше загруженного — догружаем следующую страницу YouTube Music
    if (page + 1) * PAGE_SIZE > len(session.items) and session.more and len(session.items) < MAX_RESULTS:
        try:
            found, session.more = await ytmusic_search(session.query, session.more)
            session.items.extend(found)
        except Exception:
            log.exception("YouTube Music next page failed for %r", session.query)
            session.more = None
    await callback.answer()
    try:
        await callback.message.edit_text(_page_text(session), reply_markup=_page_keyboard(sid, session, page))
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("d:"))
async def on_choose(callback: CallbackQuery) -> None:
    _, sid, index = callback.data.split(":")
    if sid not in _sessions:
        await callback.answer("Поиск устарел — отправь запрос заново", show_alert=True)
        return
    await enqueue(callback.message, callback.from_user.id, sid, int(index), callback)


@dp.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


async def enqueue(chat: Message, user_id: int, sid: str, index: int, callback: CallbackQuery | None = None) -> None:
    c = _sessions[sid].items[index]
    key = f"{user_id}:{c.source}:{c.id}"

    async def reply(text: str, alert: bool = False) -> None:
        if callback:
            await callback.answer(text, show_alert=alert)

    if key in _active:
        await reply("Этот трек уже в очереди")
        return
    if sum(k.startswith(f"{user_id}:") for k in _active) >= MAX_PER_USER:
        await reply(f"В очереди уже {MAX_PER_USER} твоих трека — дождись, пока скачаются", alert=True)
        return
    await reply("Добавил в очередь")
    _active.add(key)
    job = Job(chat, await chat.answer("🕐 В очереди…"), c, user_id, key)
    _waiting.append(job)
    await _refresh_queue_positions()
    _queue.put_nowait(job)


def _page_text(session: SearchSession) -> str:
    return f"Что нашлось по запросу <b>{_escape(session.query)}</b> — выбери трек:"


def _page_keyboard(sid: str, session: SearchSession, page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    rows = [
        [InlineKeyboardButton(text=_label(c, with_duration=True), callback_data=f"d:{sid}:{i}")]
        for i, c in enumerate(session.items[start:start + PAGE_SIZE], start=start)
    ]
    has_next = start + PAGE_SIZE < len(session.items) or (session.more and len(session.items) < MAX_RESULTS)
    nav = [
        InlineKeyboardButton(text="◀️", callback_data=f"p:{sid}:{page - 1}") if page else None,
        InlineKeyboardButton(text=f"стр. {page + 1}", callback_data="noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"p:{sid}:{page + 1}") if has_next else None,
    ]
    if page or has_next:
        rows.append([b for b in nav if b])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        log.warning("YANDEX_TOKEN не задан — ищу только на YouTube Music и SoundCloud")
    workers = [asyncio.create_task(worker()) for _ in range(WORKERS)]
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        for w in workers:
            w.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
