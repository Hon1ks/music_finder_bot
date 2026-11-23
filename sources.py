"""Поиск и скачивание: Яндекс Музыка, YouTube Music (поиск), YouTube и SoundCloud (yt-dlp)."""
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("music_finder.sources")

YANDEX_TOKEN = os.getenv("YANDEX_TOKEN", "").strip()
# Прокси только для скачивания с YouTube (например, socks5://127.0.0.1:40000)
YT_PROXY = os.getenv("YT_PROXY", "").strip()

MAX_BYTES = 49 * 1024 * 1024   # лимит Bot API на отправку файла — 50 МБ
MIN_DURATION = 31              # короче — превью SoundCloud Go+
MAX_DURATION = 15 * 60         # длиннее — фильмы и часовые миксы
MAX_RESULTS = 60
YT_COOLDOWN = 30 * 60          # после бот-проверки YouTube столько секунд качаем сразу с SoundCloud

SOURCE_NAMES = {"ym": "Яндекс Музыка", "yt": "YouTube", "sc": "SoundCloud"}


@dataclass
class Candidate:
    source: str   # ym / yt / sc
    id: str
    performer: str
    title: str
    duration: int | None
    thumb: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"


@dataclass
class Track:
    path: Path
    title: str
    performer: str
    duration: int | None
    source: str
    thumb: str | None = None


class YouTubeBlocked(Exception):
    """YouTube просит «Sign in to confirm you're not a bot» — IP сервера временно заблокирован."""


_yt_blocked_until = 0.0


def youtube_blocked() -> bool:
    return time.time() < _yt_blocked_until


def label(c: Candidate, with_duration: bool = False) -> str:
    text = f"{c.performer} — {c.title}" if c.performer and c.performer.lower() not in c.title.lower() else c.title
    if with_duration and c.duration:
        text = f"{text[:52]} · {c.duration // 60}:{c.duration % 60:02d}"
    return text


# ---------- Яндекс Музыка ----------

_ym_client = None


async def _yandex_client():
    global _ym_client
    if _ym_client is None:
        from yandex_music import ClientAsync
        _ym_client = await ClientAsync(YANDEX_TOKEN).init()
    return _ym_client


async def yandex_search(query: str) -> list[Candidate]:
    if not YANDEX_TOKEN:
        return []
    client = await _yandex_client()
    result = await client.search(query, type_="track")
    if not result or not result.tracks:
        return []
    return [
        Candidate("ym", str(t.id), ", ".join(t.artists_name()), t.title or "",
                  (t.duration_ms or 0) // 1000 or None,
                  f"https://{t.cover_uri.replace('%%', '300x300')}" if t.cover_uri else None)
        for t in result.tracks.results if t.available
    ]


async def yandex_download(c: Candidate, workdir: Path) -> Track | None:
    client = await _yandex_client()
    tracks = await client.tracks([c.id])
    if not tracks:
        return None
    path = workdir / "track.mp3"
    await tracks[0].download_async(str(path), codec="mp3", bitrate_in_kbps=192)
    return Track(path, c.title, c.performer, c.duration, SOURCE_NAMES["ym"], c.thumb)


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


async def _ytmusic_page(query: str, more: str | None = None) -> tuple[list[Candidate], str | None]:
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
        thumbs = next(_find(row.get("thumbnail", {}), "thumbnails"), [])
        thumb = re.sub(r"=w\d+-h\d+", "=w320-h320", thumbs[-1]["url"]) if thumbs else None
        if video_id and MIN_DURATION < duration < MAX_DURATION:
            result.append(Candidate("yt", video_id, parts[0], title, duration, thumb))
    token = next(_find(data, "nextContinuationData"), {}).get("continuation")
    return result, token


async def ytmusic_search(query: str, limit: int = MAX_RESULTS) -> list[Candidate]:
    found, more = await _ytmusic_page(query)
    while more and len(found) < limit:
        page, more = await _ytmusic_page(query, more)
        if not page:
            break
        found += page
    return found[:limit]


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


def split_title(title: str, uploader: str) -> tuple[str, str]:
    """«Макс Корж — Мотылёк (official video)» -> («Макс Корж», «Мотылёк»); иначе исполнитель = канал."""
    title = _JUNK.sub("", title).strip()
    parts = re.split(r"\s[-—–]\s", title, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return uploader.removesuffix(" - Topic"), title


def _thumb(entry: dict, source: str) -> str | None:
    if source == "yt":
        return f"https://i.ytimg.com/vi/{entry['id']}/mqdefault.jpg"  # 320x180 JPEG
    by_id = {t.get("id"): t.get("url") for t in entry.get("thumbnails") or []}
    return by_id.get("t300x300") or entry.get("thumbnail")


def _entry_to_candidate(e: dict, source: str) -> Candidate | None:
    duration = int(e.get("duration") or 0)
    if duration and not MIN_DURATION < duration < MAX_DURATION:
        return None
    performer, title = split_title(e.get("title") or "", e.get("channel") or e.get("uploader") or "")
    return Candidate(source, str(e["id"]), performer, title, duration or None, _thumb(e, source))


def soundcloud_search(query: str, limit: int) -> list[Candidate]:
    import yt_dlp  # тяжёлый модуль — грузим только когда он реально нужен

    with yt_dlp.YoutubeDL({**_YTDLP_BASE, "extract_flat": "in_playlist"}) as ydl:
        found = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
    return [c for e in (found or {}).get("entries") or []
            if e.get("ie_key") == "Soundcloud" and e.get("duration") and (c := _entry_to_candidate(e, "sc"))]


# ---------- ссылки ----------

_YT_VIDEO = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/)|youtu\.be/)([\w-]{11})")
_YT_LIST = re.compile(r"youtube\.com/(?:playlist\?(?:.*&)?list=[\w-]+|browse/MPRE[\w-]+)")
_SC_URL = re.compile(r"(?:on\.)?soundcloud\.com/[\w-]+(?:/[\w-]+)+")
LINK = re.compile(r"https?://\S+")


def resolve_link(url: str, limit: int) -> tuple[str, list[Candidate]]:
    """Ссылка на трек, альбом или плейлист YouTube / YouTube Music / SoundCloud -> (название, треки)."""
    import yt_dlp

    if _YT_LIST.search(url):
        source = "yt"
    elif m := _YT_VIDEO.search(url):
        # одиночное видео не разбираем через yt-dlp — это тот же запрос, что блокирует YouTube
        return "", [Candidate("yt", m.group(1), "", "", None, f"https://i.ytimg.com/vi/{m.group(1)}/mqdefault.jpg")]
    elif _SC_URL.search(url):
        source = "sc"
    else:
        return "", []
    with yt_dlp.YoutubeDL({**_YTDLP_BASE, "extract_flat": "in_playlist", "playlistend": limit}) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries")
    if entries is None:  # одиночный трек SoundCloud
        c = _entry_to_candidate(info, source)
        return "", [c] if c else []
    return info.get("title") or "", [c for e in list(entries)[:limit] if e and (c := _entry_to_candidate(e, source))]


async def fill_from_oembed(c: Candidate) -> None:
    """Название и канал видео по ссылке — через oEmbed, его бот-проверка YouTube не трогает."""
    import aiohttp

    url = f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={c.id}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                data = await resp.json(content_type=None) if resp.status == 200 else {}
    except Exception:
        log.exception("oEmbed failed for %s", c.id)
        return
    c.performer, c.title = split_title(data.get("title") or "", data.get("author_name") or "")


# ---------- скачивание ----------

def ytdlp_download(c: Candidate, workdir: Path, progress: dict) -> Track | None:
    import yt_dlp

    global _yt_blocked_until

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
        log.info("%s not downloadable: %s", c.key, str(e)[:200])
        if c.source == "yt" and "confirm you" in str(e):
            _yt_blocked_until = time.time() + YT_COOLDOWN
            raise YouTubeBlocked from e
        return None
    files = [p for p in workdir.iterdir() if p.suffix in (".mp3", ".m4a")]
    if not entry or not files:
        return None
    if entry.get("duration") and not entry.get("duration") < MAX_DURATION:
        return None
    performer, title = split_title(entry.get("title") or "", entry.get("channel") or entry.get("uploader") or "")
    return Track(
        path=files[0],
        title=c.title or title,
        performer=c.performer or performer,
        duration=int(entry["duration"]) if entry.get("duration") else c.duration,
        source=SOURCE_NAMES[c.source],
        thumb=c.thumb or _thumb(entry, c.source),
    )


def same_track(original: Candidate, alt: Candidate) -> int | None:
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
