"""Music Finder Bot: поиск музыки, выбор кнопкой, очередь загрузок, кэш, инлайн-режим, доступ по приглашению."""
import asyncio
import itertools
import logging
import os
import shutil
import tempfile
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeChat, BufferedInputFile, CallbackQuery, ChosenInlineResult,
                           FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery,
                           InlineQueryResultArticle, InlineQueryResultCachedAudio, InlineQueryResultsButton,
                           InputMediaAudio, InputTextMessageContent, Message, User)

import sources
import storage
from sources import Candidate, Track, YouTubeBlocked, label

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("music_finder")

# FadeHost для Telegram-приложений кладёт токен и в TELEGRAM_BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.environ["TELEGRAM_BOT_TOKEN"]
# Telegram id владельца: ему приходят запросы доступа, оповещения, /stats. Без него бот открыт для всех
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0) or None
# PUBLIC=1 — бот для всех, без запроса доступа
PUBLIC = os.getenv("PUBLIC", "") == "1"
# Сколько треков качается одновременно (каждая загрузка ~50 МБ RAM сверх базовых ~170 МБ)
WORKERS = int(os.getenv("MAX_PARALLEL", "1"))
MAX_PER_USER = int(os.getenv("MAX_PER_USER", "30"))   # треков в очереди на одного пользователя
LINK_LIMIT = 30                                      # треков из одного альбома/плейлиста
PAGE_SIZE = 8


@dataclass
class SearchSession:
    query: str
    items: list[Candidate]


@dataclass
class Job:
    bot: Bot
    user_id: int
    candidate: Candidate
    key: str                  # "user:source:id" — от повторных нажатий
    chat: Message | None = None        # куда прислать трек (обычный режим)
    status: Message | None = None      # сообщение со статусом (обычный режим)
    inline_id: str | None = None       # сообщение, отправленное через @бот (инлайн-режим)
    text: str = ""                     # последний показанный текст статуса
    progress: dict = field(default_factory=dict)   # пишется из потока yt-dlp


# ---------- память поисков (по ним работают кнопки) ----------

_sessions: OrderedDict[str, SearchSession] = OrderedDict()
_session_ids = itertools.count(1)
_inline_found: OrderedDict[str, Candidate] = OrderedDict()   # результаты инлайн-поиска по ключу трека


def _remember(store: OrderedDict, key: str, value, limit: int) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > limit:
        store.popitem(last=False)


def _new_session(s: SearchSession) -> str:
    sid = format(next(_session_ids), "x")
    _remember(_sessions, sid, s, 300)
    return sid


# ---------- доступ ----------

def has_access(user: User) -> bool:
    if PUBLIC or ADMIN_ID is None or user.id == ADMIN_ID:
        return True
    return storage.user_status(user.id) == "allowed"


def _user_name(user: User) -> str:
    return user.full_name + (f" (@{user.username})" if user.username else "")


async def request_access(message: Message) -> None:
    user = message.from_user
    status = storage.user_status(user.id)
    if status == "blocked":
        await message.answer("🔒 Доступ закрыт.")
        return
    if status == "pending":
        await message.answer("⏳ Запрос уже отправлен владельцу — дождись ответа.")
        return
    storage.user_set(user.id, "pending", _user_name(user))
    await message.bot.send_message(
        ADMIN_ID, f"🔔 Запрос доступа: <b>{_escape(_user_name(user))}</b>, id <code>{user.id}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Разрешить", callback_data=f"acc:allowed:{user.id}"),
            InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"acc:blocked:{user.id}"),
        ]]))
    await message.answer("🔒 Бот пока только для друзей. Я отправил запрос владельцу — "
                         "как только он разрешит, придёт сообщение.")


# ---------- оповещения владельцу ----------

_last_alert: dict[str, float] = {}
_failures_in_row = 0


async def alert(bot: Bot, kind: str, text: str) -> None:
    """Не чаще раза в час на каждый вид проблемы."""
    if not ADMIN_ID or time.time() - _last_alert.get(kind, 0) < 3600:
        return
    _last_alert[kind] = time.time()
    try:
        await bot.send_message(ADMIN_ID, f"⚠️ {text}")
    except Exception:
        log.exception("Alert failed")


# ---------- поиск ----------

async def search(bot: Bot, user_id: int, query: str) -> SearchSession:
    items, source = [], ""
    try:
        items, source = await sources.yandex_search(query), "ym"
    except Exception:
        log.exception("Yandex Music search failed for %r", query)
    if not items:
        try:
            items, source = await sources.ytmusic_search(query), "ytm"
        except Exception as e:
            log.exception("YouTube Music search failed for %r", query)
            await alert(bot, "ytm", f"Поиск YouTube Music сломался: <code>{_escape(repr(e)[:200])}</code>\n"
                                    "Ищу через SoundCloud. Возможно, YouTube поменял формат ответа.")
    if not items:
        try:
            items, source = await asyncio.to_thread(sources.soundcloud_search, query, 40), "sc"
        except Exception:
            log.exception("SoundCloud search failed for %r", query)
    storage.event(user_id, "search", source, ok=bool(items), detail=query)
    return SearchSession(query, items)


# ---------- загрузка ----------

async def download(job: Job, workdir: Path) -> Track | None:
    c = job.candidate
    if c.source == "yt" and sources.youtube_blocked():
        job.progress.update(stage="🔁 YouTube временно не отдаёт файлы, ищу этот трек на SoundCloud…", pct=None)
    else:
        try:
            if c.source == "ym":
                track = await sources.yandex_download(c, workdir)
            else:
                track = await asyncio.to_thread(sources.ytdlp_download, c, workdir, job.progress)
            if track:
                return track
        except YouTubeBlocked:
            storage.event(job.user_id, "yt_block", "yt", ok=False, detail=c.id)
            await alert(job.bot, "yt_block", "YouTube заблокировал скачивание с сервера («Sign in to confirm "
                                             "you're not a bot»). 30 минут качаю с SoundCloud, потом попробую снова.")
        except Exception:
            log.exception("Download failed for %s", c.key)
        job.progress.update(stage="🔁 YouTube не отдал файл, ищу этот трек на SoundCloud…", pct=None)
    if c.source == "sc" or not c.title:
        return None
    alts = await asyncio.to_thread(sources.soundcloud_search, f"{c.performer} {c.title}", 12)
    scored = sorted((score, i, alt) for i, alt in enumerate(alts) if (score := sources.same_track(c, alt)) is not None)
    for _, _, alt in scored[:4]:
        job.progress.update(stage="⬇️ Скачиваю с SoundCloud", pct=0.0)
        if track := await asyncio.to_thread(sources.ytdlp_download, alt, workdir, job.progress):
            track.thumb = c.thumb or track.thumb   # обложка оригинала лучше, чем у перезалива
            return track
    return None


async def fetch_thumb(url: str | None) -> BufferedInputFile | None:
    """Обложка для send_audio: JPEG до 200 КБ."""
    if not url:
        return None
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                data = await resp.read() if resp.status == 200 else b""
    except Exception:
        log.info("Cover download failed: %s", url)
        return None
    return BufferedInputFile(data, "cover.jpg") if data[:2] == b"\xff\xd8" and len(data) < 200_000 else None


# ---------- очередь загрузок ----------

_queue: asyncio.Queue[Job] = asyncio.Queue()
_waiting: deque[Job] = deque()     # стоят в очереди, ещё не начались
_active: set[str] = set()          # ключи "user:трек" в очереди и в работе


def _bar(pct: float) -> str:
    filled = round(pct * 10)
    return "▰" * filled + "▱" * (10 - filled) + f" {pct:.0%}"


async def _set_status(job: Job, text: str) -> None:
    if text == job.text:
        return
    job.text = text
    try:
        if job.inline_id:
            await job.bot.edit_message_text(text, inline_message_id=job.inline_id)
        else:
            await job.status.edit_text(text)
    except TelegramBadRequest:
        pass  # «message is not modified» или сообщение удалили


async def _refresh_queue_positions() -> None:
    for position, job in enumerate(list(_waiting), start=1):
        await _set_status(job, f"🕐 В очереди: {position}-й\n<b>{_escape(label(job.candidate))}</b>")


async def _show_progress(job: Job) -> None:
    """Раз в 1.5 сек переносит прогресс из потока загрузки в сообщение."""
    name = f"<b>{_escape(label(job.candidate))}</b>"
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
    global _failures_in_row
    c = job.candidate
    workdir = Path(tempfile.mkdtemp(prefix="mf_"))
    job.progress.update(stage="⬇️ Скачиваю", pct=0.0 if c.source != "ym" else None)
    ticker = asyncio.create_task(_show_progress(job))
    try:
        started = time.monotonic()
        track = await download(job, workdir)
        if not track or track.path.stat().st_size > sources.MAX_BYTES:
            ticker.cancel()
            storage.event(job.user_id, "download", c.source, ok=False, detail=label(c))
            _failures_in_row += 1
            if _failures_in_row >= 3:
                await alert(job.bot, "failures", f"{_failures_in_row} загрузки подряд не удались. "
                                                 "Посмотри логи: <code>journalctl -u music-finder-bot</code>")
            await _set_status(job, f"😔 Не удалось скачать оригинал:\n<b>{_escape(label(c))}</b>\n"
                                   "Попробуй другой вариант." if track is None
                              else "Файл больше 50 МБ — Telegram не даст его отправить.")
            return
        job.progress.update(stage="📤 Отправляю в Telegram", pct=None)
        thumb = await fetch_thumb(track.thumb)
        audio = FSInputFile(track.path, filename=_filename(track))
        meta = dict(title=track.title, performer=track.performer, duration=track.duration,
                    caption=f"Источник: {track.source}")
        if job.inline_id:
            # в инлайн-сообщение можно вставить только уже загруженный в Telegram файл:
            # грузим его владельцу без звука, берём file_id и сразу удаляем сообщение
            stored = await job.bot.send_audio(ADMIN_ID, audio, thumbnail=thumb, disable_notification=True, **meta)
            file_id = stored.audio.file_id
            ticker.cancel()
            await stored.delete()
            await job.bot.edit_message_media(InputMediaAudio(media=file_id, **meta), inline_message_id=job.inline_id)
        else:
            sent = await job.chat.answer_audio(audio, thumbnail=thumb, **meta)
            file_id = sent.audio.file_id
            ticker.cancel()
            await job.status.delete()
        storage.cache_put(c.key, file_id, track.performer, track.title, track.duration, track.source)
        storage.event(job.user_id, "download", {v: k for k, v in sources.SOURCE_NAMES.items()}[track.source])
        _failures_in_row = 0
        log.info("Sent %s in %.0fs from %s", c.key, time.monotonic() - started, track.source)
    except Exception:
        ticker.cancel()
        log.exception("Failed to send %s", job.key)
        await _set_status(job, "⚠️ Ошибка при загрузке, попробуй ещё раз.")
    finally:
        ticker.cancel()
        shutil.rmtree(workdir, ignore_errors=True)  # файл удаляется сразу после отправки


async def enqueue(bot: Bot, user_id: int, c: Candidate, chat: Message | None = None,
                  inline_id: str | None = None) -> str:
    """Ставит трек в очередь (или сразу шлёт из кэша). Возвращает ответ для всплывающей подсказки."""
    if cached := storage.cache_get(c.key):
        storage.event(user_id, "cache", cached["source"])
        meta = dict(title=cached["title"], performer=cached["performer"], duration=cached["duration"],
                    caption=f"Источник: {cached['source']}")
        if inline_id:
            await bot.edit_message_media(InputMediaAudio(media=cached["file_id"], **meta), inline_message_id=inline_id)
        else:
            await chat.answer_audio(cached["file_id"], **meta)
        return "Готово"
    key = f"{user_id}:{c.key}"
    if key in _active:
        return "Этот трек уже в очереди"
    if sum(k.startswith(f"{user_id}:") for k in _active) >= MAX_PER_USER:
        return f"В очереди уже {MAX_PER_USER} твоих треков — дождись, пока скачаются"
    _active.add(key)
    job = Job(bot, user_id, c, key, chat=chat, inline_id=inline_id)
    if chat:
        job.status = await chat.answer("🕐 В очереди…")
    _waiting.append(job)
    await _refresh_queue_positions()
    _queue.put_nowait(job)
    return "Добавил в очередь"


# ---------- Telegram: команды ----------

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    if not has_access(message.from_user):
        await request_access(message)
        return
    me = await message.bot.me()
    await message.answer(
        "Напиши исполнителя и/или название трека — например, <code>Кино Группа крови</code> — "
        "и выбери нужный вариант кнопкой. Можно несколько, они встанут в очередь.\n\n"
        "🔗 Можно прислать ссылку на трек, альбом или плейлист YouTube / YouTube Music / SoundCloud.\n"
        f"💬 В любом чате: <code>@{me.username} название</code> — и трек отправится прямо туда."
    )


@dp.message(Command("id"))
async def my_id(message: Message) -> None:
    log.info("/id from %s: %s", _user_name(message.from_user), message.from_user.id)
    await message.answer(f"Твой Telegram id: <code>{message.from_user.id}</code>")


@dp.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def stats(message: Message) -> None:
    def block(title: str, since: int) -> str:
        s = storage.stats(since)
        searches, found = s["searches"][0], s["searches"][1] or 0
        lines = [f"<b>{title}</b>", f"👤 Пользователей: {s['users']}",
                 f"🔎 Поисков: {searches} (без результата: {searches - found})",
                 f"⚡️ Из кэша: {s['cache_hits']}"]
        for source, total, ok in s["downloads"]:
            lines.append(f"⬇️ {sources.SOURCE_NAMES.get(source, source)}: {ok or 0} из {total}")
        if s["yt_blocks"]:
            lines.append(f"🚧 Блокировок YouTube: {s['yt_blocks']}")
        return "\n".join(lines)

    now = int(time.time())
    total = storage.stats(0)
    await message.answer(
        f"{block('📊 За 24 часа', now - 86400)}\n\n{block('📊 За 7 дней', now - 7 * 86400)}\n\n"
        f"💾 Треков в кэше: {total['cached_total']}\n"
        f"🕐 Сейчас в очереди: {len(_waiting)}, в работе: {len(_active) - len(_waiting)}\n"
        f"YouTube: {'🚧 на паузе' if sources.youtube_blocked() else '✅ работает'}"
    )


@dp.message(Command("users"), F.from_user.id == ADMIN_ID)
async def users(message: Message) -> None:
    icons = {"allowed": "✅", "pending": "⏳", "blocked": "🚫"}
    rows = [f"{icons.get(s, s)} {_escape(n)} — <code>{i}</code>" for i, n, s in storage.users_list()]
    await message.answer(("\n".join(rows) or "Пока никого.") +
                         "\n\n/allow &lt;id&gt; — дать доступ, /deny &lt;id&gt; — закрыть")


@dp.message(Command("allow", "deny"), F.from_user.id == ADMIN_ID)
async def allow_deny(message: Message) -> None:
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /allow 123456789 или /deny 123456789")
        return
    status = "allowed" if parts[0].startswith("/allow") else "blocked"
    await _set_access(message.bot, int(parts[1]), status)
    await message.answer(f"Готово: <code>{parts[1]}</code> — {'✅ доступ открыт' if status == 'allowed' else '🚫 закрыт'}")


@dp.callback_query(F.data.startswith("acc:"), F.from_user.id == ADMIN_ID)
async def on_access_button(callback: CallbackQuery) -> None:
    _, status, user_id = callback.data.split(":")
    await _set_access(callback.bot, int(user_id), status)
    await callback.answer("Готово")
    await callback.message.edit_text(callback.message.html_text + ("\n\n✅ Разрешён" if status == "allowed"
                                                                   else "\n\n🚫 Отклонён"))


async def _set_access(bot: Bot, user_id: int, status: str) -> None:
    storage.user_set(user_id, status)
    if status == "allowed":
        try:
            await bot.send_message(user_id, "✅ Доступ открыт! Напиши исполнителя или название трека.")
        except Exception:
            log.info("Can't notify %s", user_id)


# ---------- Telegram: поиск и выбор ----------

@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    if not has_access(message.from_user):
        await request_access(message)
        return
    if link := sources.LINK.search(message.text):
        await on_link(message, link.group(0))
        return
    query = message.text.strip()[:200]
    status = await message.answer(f"🔎 Ищу: <b>{_escape(query)}</b>…")
    session = await search(message.bot, message.from_user.id, query)
    if not session.items:
        await status.edit_text("😔 Ничего не нашёл. Попробуй уточнить запрос.")
        return
    if len(session.items) == 1:
        await status.edit_text(f"Нашёл: <b>{_escape(label(session.items[0]))}</b>")
        await enqueue(message.bot, message.from_user.id, session.items[0], chat=message)
        return
    sid = _new_session(session)
    await status.edit_text(_page_text(session), reply_markup=_page_keyboard(sid, session, 0))


async def on_link(message: Message, url: str) -> None:
    status = await message.answer("🔗 Разбираю ссылку…")
    try:
        title, found = await asyncio.to_thread(sources.resolve_link, url, LINK_LIMIT)
    except Exception as e:
        log.info("Link %s failed: %s", url, str(e)[:200])
        await status.edit_text("😔 Не получилось открыть ссылку. Она точно публичная?")
        return
    if not found:
        await status.edit_text(f"В «{_escape(title)}» нет отдельных треков — только записи длиннее 15 минут."
                               if title else "Не понял ссылку. Поддерживаются YouTube, YouTube Music и SoundCloud: "
                                             "трек, альбом или плейлист.")
        return
    if len(found) == 1:
        c = found[0]
        if c.source == "yt" and not c.title:
            await sources.fill_from_oembed(c)
        await status.edit_text(f"Нашёл: <b>{_escape(label(c) or 'трек')}</b>")
        await enqueue(message.bot, message.from_user.id, c, chat=message)
        return
    await status.edit_text(f"📀 <b>{_escape(title or 'Плейлист')}</b>: {len(found)} треков — ставлю в очередь")
    skipped = 0
    for c in found:
        if (await enqueue(message.bot, message.from_user.id, c, chat=message)).startswith("В очереди уже"):
            skipped += 1
    if skipped:
        await message.answer(f"Очередь заполнена — {skipped} треков не поместились. Пришли ссылку ещё раз позже.")


@dp.callback_query(F.data.startswith("p:"))
async def on_page(callback: CallbackQuery) -> None:
    _, sid, page = callback.data.split(":")
    session = _sessions.get(sid)
    if not session:
        await callback.answer("Поиск устарел — отправь запрос заново", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_text(_page_text(session), reply_markup=_page_keyboard(sid, session, int(page)))
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("d:"))
async def on_choose(callback: CallbackQuery) -> None:
    if not has_access(callback.from_user):
        await callback.answer("🔒 Нет доступа", show_alert=True)
        return
    _, sid, index = callback.data.split(":")
    session = _sessions.get(sid)
    if not session:
        await callback.answer("Поиск устарел — отправь запрос заново", show_alert=True)
        return
    answer = await enqueue(callback.bot, callback.from_user.id, session.items[int(index)], chat=callback.message)
    await callback.answer(answer, show_alert=answer.startswith("В очереди уже"))


@dp.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ---------- Telegram: инлайн-режим (@бот запрос в любом чате) ----------

@dp.inline_query()
async def on_inline(inline: InlineQuery) -> None:
    if not has_access(inline.from_user):
        await inline.answer([], cache_time=5, is_personal=True,
                            button=InlineQueryResultsButton(text="🔒 Запросить доступ", start_parameter="access"))
        return
    query = inline.query.strip()[:200]
    if len(query) < 2:
        await inline.answer([], cache_time=5, is_personal=True)
        return
    try:
        found = await sources.ytmusic_search(query, limit=20)
    except Exception:
        log.exception("Inline search failed for %r", query)
        found = []
    results, seen = [], set()
    for c in found:
        if c.key in seen:
            continue
        seen.add(c.key)
        _remember(_inline_found, c.key, c, 2000)
        if cached := storage.cache_get(c.key):
            results.append(InlineQueryResultCachedAudio(id=c.key, audio_file_id=cached["file_id"]))
            continue
        if not ADMIN_ID:
            continue  # загрузить новый трек в инлайн-сообщение можно только через чат владельца
        results.append(InlineQueryResultArticle(
            id=c.key,
            title=c.title,
            description=" · ".join(filter(None, [c.performer, c.duration and f"{c.duration // 60}:{c.duration % 60:02d}"])),
            thumbnail_url=c.thumb,
            input_message_content=InputTextMessageContent(message_text=f"⏳ Загружаю: <b>{_escape(label(c))}</b>"),
            # без кнопки Telegram не даёт inline_message_id, и сообщение потом не заменить на трек
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⏳ Загрузка…", callback_data="noop")]]),
        ))
    storage.event(inline.from_user.id, "search", "inline", ok=bool(results), detail=query)
    await inline.answer(results, cache_time=30, is_personal=True)


@dp.chosen_inline_result()
async def on_inline_chosen(chosen: ChosenInlineResult) -> None:
    if not chosen.inline_message_id or storage.cache_get(chosen.result_id):
        return  # трек из кэша Telegram уже отправил сам
    source, _, track_id = chosen.result_id.partition(":")
    c = _inline_found.get(chosen.result_id) or Candidate(source, track_id, "", "", None)
    await enqueue(chosen.bot, chosen.from_user.id, c, inline_id=chosen.inline_message_id)


# ---------- вспомогательное ----------

def _page_text(session: SearchSession) -> str:
    return f"Что нашлось по запросу <b>{_escape(session.query)}</b> ({len(session.items)} шт.) — выбери трек:"


def _page_keyboard(sid: str, session: SearchSession, page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    rows = [
        [InlineKeyboardButton(text=label(c, with_duration=True), callback_data=f"d:{sid}:{i}")]
        for i, c in enumerate(session.items[start:start + PAGE_SIZE], start=start)
    ]
    pages = -(-len(session.items) // PAGE_SIZE)
    nav = [
        InlineKeyboardButton(text="◀️", callback_data=f"p:{sid}:{page - 1}") if page else None,
        InlineKeyboardButton(text=f"стр. {page + 1} / {pages}", callback_data="noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"p:{sid}:{page + 1}") if page + 1 < pages else None,
    ]
    if pages > 1:
        rows.append([b for b in nav if b])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _filename(track: Track) -> str:
    name = f"{track.performer} - {track.title}" if track.performer else track.title
    return "".join(ch for ch in name if ch not in '\\/:*?"<>|')[:100] + track.path.suffix


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def main() -> None:
    from aiogram.client.default import DefaultBotProperties

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    if not sources.YANDEX_TOKEN:
        log.warning("YANDEX_TOKEN не задан — ищу на YouTube Music и SoundCloud")
    if ADMIN_ID is None:
        log.warning("ADMIN_ID не задан — бот открыт для всех, оповещений и /stats нет")
    workers = [asyncio.create_task(worker()) for _ in range(WORKERS)]
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Как пользоваться"),
                                   BotCommand(command="id", description="Мой Telegram id")])
        if ADMIN_ID:
            try:
                await bot.set_my_commands([
                    BotCommand(command="start", description="Как пользоваться"),
                    BotCommand(command="stats", description="Статистика"),
                    BotCommand(command="users", description="Кто имеет доступ"),
                ], scope=BotCommandScopeChat(chat_id=ADMIN_ID))
            except TelegramBadRequest:
                log.warning("Не удалось задать меню владельцу — он ещё не писал боту?")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for w in workers:
            w.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
