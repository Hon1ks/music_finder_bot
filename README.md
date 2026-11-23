# Music Finder Bot

Telegram-бот (aiogram 3, long polling): пишешь исполнителя и/или название — бот показывает
до 6 вариантов кнопками («Исполнитель — Название · 3:45»), по нажатию качает выбранный трек,
отправляет аудиофайлом и сразу удаляет файл с диска. Если вариант один — присылает сразу.

- Поиск: Яндекс Музыка (если задан `YANDEX_TOKEN`) → YouTube Music (раздел «Песни») → SoundCloud.
- Загрузка: Яндекс Музыка напрямую, YouTube и SoundCloud через yt-dlp. Если YouTube не даёт
  скачать (блокирует IP серверов), тот же трек ищется на SoundCloud.

```
bot.py            # весь бот
requirements.txt  # зависимости (FadeHost по нему определяет Python)
.env.example      # список переменных окружения
```

## Переменные окружения

| Переменная     | Обязательна | Что это |
|----------------|-------------|---------|
| `BOT_TOKEN`    | да          | токен бота от [@BotFather](https://t.me/BotFather) |
| `YANDEX_TOKEN` | нет         | OAuth-токен Яндекс Музыки. Без него бот ищет только на YouTube. Полные треки (не 30-секундные превью) — только с токеном аккаунта с Яндекс Плюс |
| `MAX_PARALLEL` | нет         | сколько треков качать одновременно, по умолчанию `1` |

Как получить `YANDEX_TOKEN`: https://yandex-music.readthedocs.io/en/main/token.html

## Деплой на FadeHost (Free, 256 МБ)

FadeHost деплоит из GitHub-репозитория: находит `requirements.txt` → ставит Python-зависимости
→ запускает `python bot.py` (это стартовая команда по умолчанию, указывать не нужно).
ffmpeg уже есть в контейнере.

1. Залить проект в GitHub-репозиторий (приватный тоже можно — тогда в панели FadeHost
   нажать **Connect GitHub**).

2. Войти в CLI (откроется панель, туда вводится код):
   ```bash
   npx fadehost login
   ```

3. Создать приложение:
   ```bash
   npx fadehost apps create \
     --repo https://github.com/<you>/music_finder_bot \
     --name music-finder-bot \
     --kind telegram \
     --runtime python \
     --env BOT_TOKEN=123456:ABC... \
     --env TELEGRAM_BOT_TOKEN=123456:ABC... \
     --env YANDEX_TOKEN=y0_...
   ```
   `TELEGRAM_BOT_TOKEN` — тот же токен: FadeHost проверяет по нему приложения типа
   `telegram` при создании. Бот читает `BOT_TOKEN`, а если его нет — `TELEGRAM_BOT_TOKEN`.
   Без `--tier` создаётся бесплатное приложение (регион выбирается автоматически).
   У бота нет веб-адреса, поэтому на Free он не засыпает.

4. Проверить, что запустился:
   ```bash
   npx fadehost apps status music-finder-bot
   npx fadehost apps logs music-finder-bot --follow
   ```
   В логах должно быть `Start polling`.

### Дальше

```bash
npx fadehost apps deploy music-finder-bot                     # выкатить новый коммит
npx fadehost apps env music-finder-bot set YANDEX_TOKEN=y0_...  # поменять переменную (перезапустит)
npx fadehost apps env music-finder-bot get                    # список переменных
npx fadehost apps restart music-finder-bot
```

Переменные можно задать и в панели: приложение → **Settings → Variables**.

## Почему так устроено (256 МБ)

- aiogram 3 при импорте строит pydantic-модели всех типов Telegram — это ~150 МБ, и
  уменьшить это нельзя. Пиковое потребление бота при загрузке трека — ~210 МБ
  (замерено в cgroup с лимитом 256 МБ).
- `yt-dlp` импортируется только при первом поиске на YouTube.
- JS-рантайм для yt-dlp (deno) **отключён**: вместе с aiogram он не влезает в 256 МБ —
  процесс убивает OOM. YouTube работает через клиенты, которым JS не нужен. Если YouTube
  начнёт отдавать ошибки, это первое, что стоит вернуть (`yt-dlp[default,deno]`,
  убрать `js_runtimes` в `bot.py`), но только на платном тарифе от 1 ГБ.
- Качается один трек за раз (`MAX_PARALLEL=1`), остальные запросы ждут очереди.
- С YouTube берётся m4a и отдаётся без перекодирования; ffmpeg перегоняет только opus/webm.
- Лимит Telegram — 50 МБ на файл, ролики длиннее 15 минут пропускаются.

## Локальный запуск

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=... YANDEX_TOKEN=...
python bot.py
```
Нужен ffmpeg в системе.

## Ограничения

- YouTube блокирует скачивание с IP дата-центров («Sign in to confirm you're not a bot») —
  и на FadeHost, и на обычных VPS. Поиск через YouTube Music при этом работает, а сам трек
  бот берёт с SoundCloud. На SoundCloud у части треков крупных лейблов только 30-секундные
  превью или DRM — такие пропускаются, и иногда вместо оригинала приходит перезалив.
  Самый надёжный источник — Яндекс Музыка с `YANDEX_TOKEN`.
- Скачанная музыка защищена авторским правом — бот для личного использования.
