from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import downloader
import yt_dlp

try:
    from aiohttp import web as aio_web
except ImportError:
    aio_web = None

try:
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
except ImportError:
    AiohttpSession = None
    TelegramAPIServer = None

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
LOCAL_SERVER = (os.getenv("LOCAL_SERVER") or "no").lower() in ("1", "yes", "true", "on")
API_BASE = (os.getenv("API_BASE") or "http://localhost:8081").strip().rstrip("/")
BOT_PROXY = (os.getenv("BOT_PROXY") or "").strip()
MAX_FILESIZE_MB = int(os.getenv("MAX_FILESIZE_MB") or (2000 if LOCAL_SERVER else 49))
if not LOCAL_SERVER:
    MAX_FILESIZE_MB = min(MAX_FILESIZE_MB, 49)
MAX_FILESIZE = MAX_FILESIZE_MB * 1024 * 1024
KEEP_FILES = (os.getenv("KEEP_FILES") or "no").lower() in ("1", "yes", "true", "on")
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT") or 3))
YTDLP_AUTOUPDATE = (os.getenv("YTDLP_AUTOUPDATE") or "yes").lower() in ("1", "yes", "true", "on")
BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").strip() or "AuraMoggSaverBot"
HEALTH_PORT = int(os.getenv("HEALTH_PORT") or os.getenv("PORT") or 0)

PROXY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxystate.json")

SEND_TIMEOUT = 36_000
METADATA_BATCH = 3
METADATA_RACE_TIMEOUT = 40
# Без прокси (Render/VPS) метаданные могут думать дольше: 0.1 CPU + JS-челленджи YouTube.
METADATA_TIMEOUT = int(os.getenv("METADATA_TIMEOUT") or 120)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

dp = Dispatcher()

bot: Bot | None = None

PENDING: dict[str, dict[str, Any]] = {}
ACTIVE: dict[int, dict[str, Any]] = {}
TASKS: set[asyncio.Task] = set()
DL_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

HEXT_RE_SIMPLE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
HEXT_RE = re.compile(
    r"(?:^|\s)((?:www\.|m\.)?(?:[\w-]+\.)+[a-z]{2,}/[^\s<>\"']+)", re.IGNORECASE
)

def human_size(n: float | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit != "ГБ" else f"{value:.2f} {unit}"
        value /= 1024
    return "?"

def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"

def extract_urls(text: str) -> list[str]:
    found = [m.group(0) for m in HEXT_RE_SIMPLE.finditer(text)]
    if found:
        return found
    for m in HEXT_RE.finditer(text):
        found.append("https://" + m.group(1).strip())
    return found

QUALITY_LABELS: dict[int, str] = {360: "360p", 480: "480p", 720: "720p", 1080: "1080p"}

YT_ID_RE = re.compile(
    r"(?:watch\?v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})", re.IGNORECASE
)

async def _ack(cb: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        await cb.answer(text, show_alert=alert)
    except Exception:
        pass

def _spawn(coro: Any) -> None:
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)

async def _edit_preview(msg: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        if msg.photo or msg.video:
            await msg.edit_caption(caption=text, reply_markup=kb)
        else:
            await msg.edit_text(text=text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    except Exception:
        log.debug("edit_preview failed", exc_info=True)

def _load_proxy_state() -> dict:
    try:
        with open(PROXY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_proxy_state(state: dict) -> None:
    try:
        with open(PROXY_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

PROXY_STATE: dict = _load_proxy_state()

def _mark_proxy_fail(p: str | None, exc: Exception) -> None:
    if not p:
        return
    fails = PROXY_STATE.setdefault("fails", {})
    fails[p] = time.time()
    cutoff = time.time() - 86400
    for k in [k for k, v in fails.items() if v < cutoff]:
        fails.pop(k, None)
    _save_proxy_state(PROXY_STATE)

def current_proxy() -> str | None:
    pool = downloader.proxy_pool()
    if not pool:
        return None
    cur = PROXY_STATE.get("current")
    return cur if cur in pool else pool[0]

def _proxy_candidates() -> list[str | None]:
    pool = downloader.proxy_pool()
    if not pool:
        return [None]
    now = time.time()
    fails = PROXY_STATE.get("fails") or {}

    def key(p: str) -> tuple:
        first = 0 if p == PROXY_STATE.get("current") else 1
        last_fail = float(fails.get(p) or 0)
        recent_dead = 1 if now - last_fail < 600 else 0
        return (first, recent_dead, last_fail)

    return sorted(pool, key=key)

def _set_proxy(p: str) -> None:
    PROXY_STATE["current"] = p
    _save_proxy_state(PROXY_STATE)
    log.info("Рабочий прокси: %s", p[:40])

async def _get_media_info_direct(url: str) -> dict:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(downloader.get_media_info, url, None),
            timeout=METADATA_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"YouTube не ответил за {METADATA_TIMEOUT} с. Отправьте ссылку ещё раз."
        )


async def _get_media_info_rotating(url: str) -> dict:
    # Без прокси «гонка прокси» не нужна: на бесплатном Render 40 секунд
    # часто не хватает, и пользователь видел «прокси не ответили за 40 с»
    # даже без единого прокси в конфиге.
    if not downloader.proxy_pool():
        return await _get_media_info_direct(url)

    candidates = _proxy_candidates()
    last_exc: Exception | None = None
    for i in range(0, len(candidates), METADATA_BATCH):
        batch = candidates[i : i + METADATA_BATCH]
        tasks = {
            asyncio.create_task(asyncio.to_thread(downloader.get_media_info, url, p)): p
            for p in batch
        }
        try:
            while tasks:
                done, _ = await asyncio.wait(
                    set(tasks), timeout=METADATA_RACE_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    last_exc = TimeoutError(f"прокси не ответили за {METADATA_RACE_TIMEOUT} с")
                    log.info("Батч прокси молчит %d с - пробую следующий", METADATA_RACE_TIMEOUT)
                    break
                winner = None
                for t in done:
                    p = tasks.pop(t)
                    exc = t.exception()
                    if exc is None:
                        winner = (t, p)
                        continue
                    _mark_proxy_fail(p, exc)
                    last_exc = exc
                    log.info(
                        "Прокси не ответчик (%s): %s",
                        (p or "?")[:30], str(exc).splitlines()[-1][:90],
                    )
                if winner:
                    t, p = winner
                    if p and p != PROXY_STATE.get("current"):
                        PROXY_STATE["current"] = p
                        log.info("Рабочий прокси: %s", p[:40])
                    if p:
                        (PROXY_STATE.get("fails") or {}).pop(p, None)
                        _save_proxy_state(PROXY_STATE)
                    return t.result()
        finally:
            for t in tasks:
                t.cancel()
    raise last_exc if last_exc else RuntimeError("Нет доступных прокси")

class Reporter:

    def __init__(
        self,
        bot_ref: Bot,
        chat_id: int,
        message_id: int,
        host: str,
        loop: asyncio.AbstractEventLoop,
    ):
        self.bot = bot_ref
        self.chat_id = chat_id
        self.message_id = message_id
        self.host = host
        self._loop = loop
        self._cancel = asyncio.Event()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def update(self, d: dict) -> None:
        pass

    def request_cancel(self) -> None:
        self._cancel.set()

def build_bot() -> Bot:
    defaults = DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        link_preview_is_disabled=True,
    )
    if LOCAL_SERVER and AiohttpSession is not None and TelegramAPIServer is not None:
        session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE))
        return Bot(token=BOT_TOKEN, session=session, default=defaults)
    if BOT_PROXY and AiohttpSession is not None:
        proxy = re.sub(r"^socks5h://", "socks5://", BOT_PROXY)
        session = AiohttpSession(proxy=proxy)
        return Bot(token=BOT_TOKEN, session=session, default=defaults)
    return Bot(token=BOT_TOKEN, default=defaults)

def build_keyboard(msg_id: int, info: dict, user_id: int) -> InlineKeyboardMarkup:
    variants = info.get("variants") or {}
    heights = [h for h in (360, 480, 720, 1080) if variants.get(h, (None, ""))[0]]
    if not heights:
        heights = [360, 480, 720, 1080]

    def label(h: int) -> str:
        est = variants.get(h, (None, ""))[0]
        size = f" · {human_size(est)}" if est else ""
        return f"🎬 {QUALITY_LABELS[h]}{size}"

    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(heights), 2):
        rows.append(
            [
                InlineKeyboardButton(text=label(h), callback_data=f"dl:v:{h}:{msg_id}")
                for h in heights[i : i + 2]
            ]
        )
    rows.append([InlineKeyboardButton(text="🎧 Audio", callback_data=f"dl:a:{msg_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

WELCOME_TEXT = (
    "🔥 <b>Привет! Я AuraSaver</b> — скачиваю видео и аудио с популярных сайтов.\n\n"
    "📲 <b>Как пользоваться:</b>\n"
    "1️⃣ Скопируй ссылку на видео\n"
    "2️⃣ Пришли её мне\n"
    "3️⃣ Выбери качество (или 🎧 только звук) — и файл у тебя 🎉\n\n"
    "⚡️ <b>Умею:</b> YouTube · TikTok · Instagram · RuTube · VK · X (Twitter)\n"
    "…и ещё 1800+ сайтов\n\n"
    "💡 Просто пришли ссылку — остальное я сделаю сам."
)

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME_TEXT)

@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME_TEXT)

@dp.message(Command("diag"))
async def cmd_diag(message: Message) -> None:
    cpath = downloader._resolve_cookies_file() or downloader._cookies_from_b64()
    pot = (os.getenv("POT_PROVIDER_URL") or "").strip()
    js = ",".join(downloader._js_runtimes() or ["нет"]) or "нет"

    def _plugin_loaded() -> str:
        import importlib.util
        for name in (
            "bgutil_ytdlp_pot_provider",
            "yt_dlp_plugins.extractor.bgutil_ytdlp_pot_provider",
        ):
            try:
                if importlib.util.find_spec(name):
                    return "есть"
            except Exception:
                pass
        try:
            import yt_dlp_plugins  # type: ignore
            import os as _os
            root = _os.path.dirname(yt_dlp_plugins.__file__)
            for dirpath, _, files in _os.walk(root):
                if any("bgutil" in f.lower() or "pot" in f.lower() for f in files):
                    return "есть (yt_dlp_plugins)"
        except Exception:
            pass
        return "НЕТ (плагин не установлен)"

    pot_http = "—"
    if pot:
        try:
            import requests as _rq
            r = _rq.get(pot, timeout=8, headers={"User-Agent": "aurasaver-diag"})
            pot_http = f"HTTP {r.status_code}"
        except Exception as exc:
            pot_http = "НЕ ОТВЕЧАЕТ: " + str(exc)[:80]

    plugin = _plugin_loaded()
    lines = [
        "<b>🔍 Диагностика</b>",
        f"yt-dlp: <code>{yt_dlp.version.__version__}</code>",
        f"ffmpeg: <code>{'есть' if downloader.ffmpeg_available() else 'НЕТ'}</code>",
        f"JS-рантаймы: <code>{html.escape(js)}</code>",
        f"cookies: <code>{'есть' if cpath else 'НЕТ'}</code>",
        f"YOUTUBE_COOKIES: <code>{html.escape(os.getenv('YOUTUBE_COOKIES') or 'auto')}</code>",
        f"POT_PROVIDER_URL: <code>{html.escape(pot[:70]) if pot else 'НЕТ'}</code>",
        f"PO провайдер HTTP: <code>{html.escape(pot_http)}</code>",
        f"плагин bgutil: <code>{plugin}</code>",
    ]
    text = message.text or ""
    m = re.search(r"https?://\S+", text)
    url = m.group(0) if m else "https://www.youtube.com/watch?v=wUrUd5xRkqg"
    lines.append(f"тест: <code>{html.escape(url[:80])}</code>")
    status = await message.answer("\n".join(lines) + "\n\n⏳ Тестирую клиенты YouTube…")

    results: list[str] = []
    variants = [
        ("lifeline (android/vr/visionos)", list(downloader.LIFELINE_CLIENTS)),
        ("fallback", list(downloader.FALLBACK_CLIENTS)),
        ("default", None),
    ]
    for label, clients in variants:
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(downloader._extract_meta, url, None, clients),
                timeout=50,
            )
            fmts = info.get("formats") or []
            real = [f for f in fmts if downloader._is_av_format(f)]
            heights = sorted({int(f["height"]) for f in real if f.get("height")})
            if real:
                results.append(f"✅ {label}: {len(real)} форматов, высоты {heights[:10]}")
            else:
                results.append(f"🚫 {label}: только storyboard (SABR), всего {len(fmts)}")
        except Exception as exc:
            results.append(f"❌ {label}: {html.escape(str(exc).splitlines()[-1][:140])}")

    verdict = "\n".join(results)
    if all(("🚫" in r or "❌" in r) for r in results):
        if "НЕТ" in plugin:
            verdict += (
                "\n\n💡 Плагин bgutil не установлен — POT_PROVIDER_URL сам по себе ничего не даёт. "
                "Залей requirements.txt и сделай Manual Deploy → Clear build cache & deploy."
            )
        elif "НЕ ОТВЕЧАЕТ" in pot_http or pot_http == "—":
            verdict += (
                "\n\n💡 Провайдер не отвечает: сервис спит, не задан порт 4416, "
                "или URL неправильный. Открой адрес провайдера в браузере."
            )
        else:
            verdict += (
                "\n\n💡 Плагин и провайдер на месте, но форматов нет. "
                "На датацентровом IP YouTube часто требует И токен, И свежие cookies "
                "(экспорт из инкогнито). Если cookies старые — обнови COOKIES_B64. "
                "Либо переезжай на VPS."
            )
    try:
        await status.edit_text("\n".join(lines) + "\n\n" + verdict)
    except Exception:
        await message.answer(verdict)



@dp.message(Command("q"))
async def cmd_quality(message: Message) -> None:
    await message.answer(
        "🎬 Качество выбирается кнопками после отправки ссылки:\n"
        "360p / 480p / 720p / 1080p — как есть на сайте\n"
        "🎧 Audio — только звук в MP3\n\n"
        "Кнопки появляются под превью видео."
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    st = ACTIVE.get(message.chat.id)
    if st and st.get("reporter"):
        st["reporter"].request_cancel()
        await message.answer("🛑 Отменяю текущую загрузку…")
        return
    await message.answer("Сейчас ничего не качается 🙂")

def _preview_caption(info: dict, url: str, with_hint: bool = True) -> str:
    title = html.escape(_clip(info.get("title") or "Видео", 180))
    link = html.escape(_clip(url, 300))
    lines = [f"🍿 <b>{title}</b>", "", f"🔗 {link}"]
    if with_hint:
        lines.append("")
        lines.append("Выбери качество (или 🎧 Audio):")
    return "\n".join(lines)

def _remember_pending(msg_id: int, pend: dict[str, Any]) -> None:
    if len(PENDING) > 60:
        for k in list(PENDING)[:30]:
            PENDING.pop(k, None)
    PENDING[str(msg_id)] = pend

async def _show_preview(message: Message, url: str, info: dict, user_id: int) -> None:
    kb = build_keyboard(0, info, user_id)
    caption = _preview_caption(info, url)
    fallback_caption = _preview_caption(info, url, with_hint=False) + "\n\n<i>обложка не получилась — но видео скачается в полном порядке</i>"

    photo_file = await asyncio.to_thread(downloader.fetch_thumbnail, info, current_proxy())
    preview: Message | None = None
    try:
        if photo_file:
            try:
                preview = await message.answer_photo(
                    photo=FSInputFile(photo_file),
                    caption=caption,
                    reply_markup=kb,
                )
            except TelegramBadRequest:
                preview = None
    finally:
        if photo_file:
            try:
                os.remove(photo_file)
            except OSError:
                pass
    if preview is None:
        preview = await message.answer(
            fallback_caption,
            reply_markup=kb,
        )

    _remember_pending(preview.message_id, {"url": url, "user_id": user_id, "info": info})

@dp.message(F.text | F.caption)
async def on_message(message: Message) -> None:
    try:
        await _handle_link_message(message)
    except Exception:
        log.exception("ошибка обработки сообщения")

async def _handle_link_message(message: Message) -> None:
    text = (message.text or message.caption or "").strip()
    if not text:
        return
    urls = extract_urls(text)
    if not urls:
        return
    allowed = [u for u in urls if downloader.is_allowed(u)]
    if not allowed:
        return await message.answer(
            "🚫 18+ сайты не поддерживаются. Пришли ссылку с YouTube, TikTok, RuTube,\n"
            "Pinterest, Instagram, X/Twitter, VK и т.п."
        )

    user_id = message.from_user.id if message.from_user else 0
    url = allowed[0]
    if message.chat.id in ACTIVE:
        return await message.answer("⏳ Уже качаю предыдущий файл — /cancel чтобы прервать.")

    m = YT_ID_RE.search(url)
    if not m:
        await _slow_preview_flow(message, url, user_id)
        return

    cover, quick_title = await asyncio.to_thread(
        downloader.youtube_quick, m.group(1), current_proxy()
    )
    lines: list[str] = []
    if quick_title:
        lines.append(f"🍿 <b>{html.escape(_clip(quick_title, 180))}</b>")
    lines.append(f"🔗 {html.escape(_clip(url, 300))}")
    lines.append("")
    lines.append("⏳ Ищу качество…")
    preview: Message | None = None
    try:
        if cover:
            try:
                preview = await message.answer_photo(
                    photo=FSInputFile(cover),
                    caption="\n".join(lines),
                )
            except TelegramBadRequest:
                preview = None
    finally:
        if cover:
            try:
                os.remove(cover)
            except OSError:
                pass
    if preview is None:
        await _slow_preview_flow(message, url, user_id)
        return

    pend: dict[str, Any] = {
        "url": url, "user_id": user_id, "info": None,
        "done": asyncio.Event(), "locked": False, "failed": False,
    }
    _remember_pending(preview.message_id, pend)
    _spawn(_fill_yt_info(preview, pend))

async def _fill_yt_info(preview: Message, pend: dict[str, Any]) -> None:
    async def pulse() -> None:
        waited = 0
        while not pend["done"].is_set() and not pend.get("locked"):
            await asyncio.sleep(15)
            waited += 15
            if pend["done"].is_set() or pend.get("locked"):
                return
            await _edit_preview(preview, f"⏳ Ищу качество… {waited} с")

    pt = asyncio.create_task(pulse())
    try:
        info: dict = await _get_media_info_rotating(pend["url"])
    except Exception as exc:
        pend["failed"] = True
        pend["done"].set()
        if not pend["locked"]:
            err = html.escape(downloader.friendly_error(exc))
            await _edit_preview(preview, f"❌ Не удалось:\n{err}")
        return
    finally:
        pt.cancel()
    if info["live"] or info["no_media"]:
        pend["failed"] = True
        pend["done"].set()
        if not pend["locked"]:
            if info["live"]:
                txt = "📺 Это прямая трансляция — эфиры записывать не умею."
            elif info.get("sabr"):
                txt = (
                    "🔧 YouTube пока не отдаёт файлы этого видео (SABR). "
                    "Попробуйте ещё раз или позже."
                )
            else:
                txt = "🤷 Видео по ссылке не найдено."
            await _edit_preview(preview, txt)
        return
    pend["info"] = info
    pend["done"].set()
    if pend["locked"]:
        return
    try:
        await preview.edit_caption(
            caption=_preview_caption(info, pend["url"]),
            reply_markup=build_keyboard(0, info, pend["user_id"]),
        )
    except Exception:
        pass


def _needs_cookie_hint(url: str, err: Exception) -> bool:
    if downloader.host_of(url) in downloader.COOKIE_HINT_HOSTS:
        return True
    low = str(err).lower()
    return "not a bot" in low or "sign in to confirm" in low or "войдите" in low

async def _slow_preview_flow(message: Message, url: str, user_id: int) -> None:
    status = await message.answer("🔍 Смотрю видео…")
    try:
        info: dict = await _get_media_info_rotating(url)
    except yt_dlp.utils.DownloadError as exc:
        msg = f"❌ Не удалось:\n{html.escape(downloader.friendly_error(exc))}"
        if _needs_cookie_hint(url, exc):
            msg += "\n\n💡 YouTube требует вход — положите cookies.txt рядом с ботом (см. README)."
        return await status.edit_text(msg)
    except Exception as exc:
        log.info("метаданные не получены: %s", exc)
        return await status.edit_text(
            "❌ Не удалось получить данные по ссылке. Попробуйте ещё раз —\n"
            "иногда помогает повторная отправка."
        )

    if info["live"]:
        return await status.edit_text("📺 Это прямая трансляция — эфиры записывать не умею.")

    if info["no_media"] and info.get("images"):
        img_file = await asyncio.to_thread(downloader.fetch_best_image, info, current_proxy())
        if img_file:
            try:
                await message.answer_photo(
                    photo=FSInputFile(img_file),
                    caption=result_caption(info.get("title") or "Фото", url, "фото", False),
                )
            finally:
                try:
                    os.remove(img_file)
                except OSError:
                    pass
            try:
                await status.delete()
            except Exception:
                pass
            return

    if info["no_media"]:
        if info.get("sabr"):
            return await status.edit_text(
                "🔧 YouTube пока не отдаёт файлы этого видео (SABR). "
                "Попробуйте позже или добавьте cookies/PO Token (см. README_RENDER)."
            )
        return await status.edit_text("🤷 Видео по ссылке не найдено.")

    await _show_preview(message, url, info, user_id)
    try:
        await status.delete()
    except Exception:
        pass

def result_caption(
    title: str, link: str, quality: str, downgraded: bool, audio: bool = False
) -> str:
    q = quality
    if downgraded:
        q += " <i>(авто: влезло в лимит)</i>"
    icon_q = "🎧" if audio else ("🖼" if quality == "фото" else "📺")
    return (
        f"🍿 <b>{html.escape(_clip(title, 160))}</b>\n\n"
        f"🔗 {html.escape(_clip(link, 300))}\n\n"
        f"{icon_q} {q}\n\n"
        f"❤️ <a href=\"https://t.me/{BOT_USERNAME}\">@{BOT_USERNAME}</a>"
    )

def _run_download(
    audio_mode: bool, url: str, height: int, temp_path: str, reporter: Reporter, proxy: str | None
) -> tuple[str, dict]:
    if audio_mode:
        return downloader.download_audio(url, temp_path, reporter, proxy)
    return downloader.download(url, height, temp_path, reporter, proxy)

def _dl_error_is_rotatable(err: Exception) -> bool:
    low = str(err).lower()
    return any(k in low for k in (
        "not a bot", "proxy", "timed out", "timeout", "connection", "socket", "unavailable", "eof",
        "requested format is not available", "sabr", "sign in to confirm",
        "the page needs to be reloaded", "http error 403",
    ))

class _FileTooBig(Exception):
    pass

async def _send_media(send: Any, **kwargs: Any) -> None:
    try:
        await send(request_timeout=SEND_TIMEOUT, **kwargs)
    except TelegramRetryAfter as exc:
        wait_s = int(getattr(exc, "retry_after", 0) or 0) + 1
        if wait_s > 300:
            raise
        log.info("Флуд-контрол: ждём %d с перед отправкой", wait_s)
        await asyncio.sleep(wait_s)
        await send(request_timeout=SEND_TIMEOUT, **kwargs)

@dp.callback_query(F.data.startswith("dl:"))
async def cb_download(cb: CallbackQuery) -> None:
    log.info("callback %s", cb.data)
    if not bot:
        return
    if not cb.message:
        return await _ack(cb, "Сообщение устарело — пришли ссылку заново 🙂", alert=True)
    parts = (cb.data or "").split(":")
    kind = parts[1] if len(parts) > 1 else ""
    mid = parts[-1]
    if mid == "0":
        mid = str(cb.message.message_id)
    st = PENDING.get(mid)
    if not st:
        return await _ack(cb, "Ссылка устарела — пришли её заново 🙂", alert=True)
    if st.get("user_id") and cb.from_user.id != st["user_id"]:
        return await _ack(cb, "Это чужой запрос — пришли себе такую же ссылку 🙂", alert=True)

    if st.get("info") is None:
        await _ack(cb, "⏳ Ещё получаю данные…")
        await _edit_preview(cb.message, "⏳ Добираю данные о качестве, это может занять минуту…")
        try:
            await asyncio.wait_for(st["done"].wait(), timeout=METADATA_TIMEOUT)
        except asyncio.TimeoutError:
            PENDING.pop(mid, None)
            await _edit_preview(cb.message, "⌛ Не успел получить данные. Пришли ссылку ещё раз.")
            return
        # данные пришли — рисуем кнопки и продолжаем выбранное качество
        info0 = st.get("info")
        if info0 and not st.get("failed"):
            try:
                await cb.message.edit_caption(
                    caption=_preview_caption(info0, st.get("url") or ""),
                    reply_markup=build_keyboard(0, info0, st.get("user_id") or cb.from_user.id),
                )
            except Exception:
                pass
    if st.get("failed"):
        PENDING.pop(mid, None)
        return await _ack(cb, "Не удалось обработать это видео 🙂", alert=True)

    chat_id = cb.message.chat.id
    if chat_id in ACTIVE:
        return await _ack(cb, "⏳ Уже качаю предыдущий файл — /cancel чтобы прервать.", alert=True)

    pending = PENDING.pop(mid, None)
    if not pending:
        return await _ack(cb, "Уже обрабатываю этот запрос 🙂", alert=True)
    pending["locked"] = True
    ACTIVE[chat_id] = {"reporter": None, "kind": "pending"}
    await _ack(cb)
    url: str = pending["url"]
    info: dict = pending["info"] or {}

    height: int | None = None
    downgraded = False
    audio_mode = kind == "a"
    if not audio_mode:
        try:
            height = int(parts[2])
        except (IndexError, ValueError):
            height = 720
        variants = info.get("variants") or {}
        est = variants.get(height, (None, ""))[0]
        if est and est > MAX_FILESIZE:
            for h in sorted((h for h in QUALITY_LABELS if h < height), reverse=True):
                e2 = variants.get(h, (None, ""))[0]
                if e2 and e2 <= MAX_FILESIZE:
                    height, downgraded = h, True
                    break
            else:
                ACTIVE.pop(chat_id, None)
                return await _edit_preview(
                    cb.message,
                    f"🧱 Даже самое низкое качество (~{human_size(est)}) больше лимита {MAX_FILESIZE_MB} МБ.",
                )

    height_label = QUALITY_LABELS.get(height or 0, f"{height}p" if height else "видео")

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    reporter = Reporter(
        bot, chat_id, cb.message.message_id,
        info.get("host") or downloader.host_of(url),
        asyncio.get_running_loop(),
    )
    ACTIVE[chat_id] = {"reporter": reporter, "kind": "audio" if audio_mode else "video"}
    temp_path = downloader.temp_dir(f"c{chat_id}_m{cb.message.message_id}")

    filepath: str | None = None
    try:
        async with DL_SEMAPHORE:
            try:
                filepath, dinfo = await asyncio.to_thread(
                    _run_download, audio_mode, url, height, temp_path, reporter, current_proxy()
                )
            except Exception as first_err:
                pool = downloader.proxy_pool()
                nxt = [p for p in pool if p != current_proxy()]
                if nxt and _dl_error_is_rotatable(first_err):
                    _set_proxy(nxt[0])
                    log.info(
                        "Ретрай скачивания через %s (%s)",
                        nxt[0][:40], str(first_err).splitlines()[-1][:80],
                    )
                    filepath, dinfo = await asyncio.to_thread(
                        _run_download, audio_mode, url, height, temp_path, reporter, nxt[0]
                    )
                else:
                    raise

            size = os.path.getsize(filepath)
            if size > MAX_FILESIZE:
                raise _FileTooBig()

            if audio_mode:
                await _send_media(
                    cb.message.answer_audio,
                    audio=FSInputFile(filepath),
                    caption=result_caption(
                        dinfo.get("title") or info.get("title") or "видео",
                        url, "MP3 · 192 kbps", False, audio=True,
                    ),
                    title=(dinfo.get("title") or "audio")[:60],
                )
            else:
                thumb = await asyncio.to_thread(downloader.make_screenshot, filepath, temp_path)
                await _send_media(
                    cb.message.answer_video,
                    video=FSInputFile(filepath),
                    caption=result_caption(
                        dinfo.get("title") or info.get("title") or "видео",
                        url, height_label, downgraded,
                    ),
                    supports_streaming=True,
                    thumbnail=FSInputFile(thumb) if thumb else None,
                )
        try:
            await cb.message.delete()
        except Exception:
            pass
    except yt_dlp.utils.DownloadCancelled:
        await _edit_preview(cb.message, "❌ Отменено.")
    except _FileTooBig:
        size2 = os.path.getsize(filepath) if filepath and os.path.isfile(filepath) else 0
        await _edit_preview(
            cb.message,
            f"🧱 Файл {human_size(size2 or None)} больше лимита {MAX_FILESIZE_MB} МБ."
            " Возьми качество пониже или 🎧 Audio.",
        )
    except TelegramBadRequest as exc:
        low = str(exc).lower()
        if "too big" in low or "too large" in low or "entity" in low:
            await _edit_preview(
                cb.message,
                "🧱 Файл больше лимита. Возьми качество пониже (360p) или 🎧 Audio.",
            )
        else:
            await _edit_preview(cb.message, f"❌ Telegram: {html.escape(str(exc))[:250]}")
    except Exception as exc:
        log.exception("download failed: %s", exc)
        text = f"❌ Ошибка:\n{html.escape(downloader.friendly_error(exc))}"
        if _needs_cookie_hint(url, exc):
            text += "\n\n💡 YouTube требует вход — попробуйте cookies.txt (см. README)."
        await _edit_preview(cb.message, text)
    finally:
        ACTIVE.pop(chat_id, None)
        if not KEEP_FILES and os.path.isdir(temp_path):
            shutil.rmtree(temp_path, ignore_errors=True)

@dp.errors()
async def on_unexpected_error(event: ErrorEvent) -> bool:
    log.error("Необработанная ошибка апдейта: %s", event.exception, exc_info=event.exception)
    try:
        update = event.update
        msg = update.message or (
            update.callback_query.message if update.callback_query else None
        )
        if msg is not None and bot:
            await _edit_preview(msg, "😢 Внутренняя ошибка — пришли ссылку ещё раз.")
    except Exception:
        pass
    return True

async def _health_server() -> None:
    if aio_web is None or not HEALTH_PORT:
        return
    started = time.time()

    async def handler(_: "aio_web.Request") -> "aio_web.Response":
        return aio_web.json_response({
            "ok": True,
            "bot": BOT_USERNAME,
            "uptime_h": round((time.time() - started) / 3600, 1),
        })

    app = aio_web.Application()
    app.router.add_get("/", handler)
    app.router.add_get("/ping", handler)
    runner = aio_web.AppRunner(app)
    await runner.setup()
    await aio_web.TCPSite(runner, "0.0.0.0", HEALTH_PORT).start()
    log.info("Health-сервер слушает порт %d", HEALTH_PORT)
    await asyncio.Event().wait()

async def main() -> None:
    global bot, BOT_USERNAME
    if not KEEP_FILES:
        downloader.cleanup_downloads()
    if YTDLP_AUTOUPDATE:
        threading.Thread(
            target=downloader.maybe_autoupdate_ytdlp, daemon=True, name="ytdlp-update"
        ).start()
    bot = build_bot()
    try:
        me = await bot.get_me()
    except Exception as exc:
        log.error("Не могу связаться с Telegram: %s", str(exc).splitlines()[-1][:200])
        log.error(
            "Проверьте интернет и BOT_PROXY в .env (публичные прокси умирают — "
            "возьмите свежий через .venv\\Scripts\\python.exe test_proxies.py), "
            "затем запустите заново."
        )
        try:
            await bot.session.close()
        except Exception:
            pass
        sys.exit(1)
    if os.getenv("BOT_USERNAME", "").strip():
        BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip()
    elif me.username:
        BOT_USERNAME = me.username
    mode = "локальный Bot API (до 2 ГБ)" if LOCAL_SERVER else "стандартный Bot API (до 49 МБ)"
    log.info("Бот @%s запущен. Отправка: %s. Лимит: %d МБ", me.username, mode, MAX_FILESIZE_MB)
    log.info(
        "Загрузок одновременно: %d · yt-dlp %s · прокси в пуле: %d",
        MAX_CONCURRENT, yt_dlp.version.__version__, len(downloader.proxy_pool()),
    )
    cookies_src = "нет"
    cpath = downloader._resolve_cookies_file() or downloader._cookies_from_b64()
    if cpath:
        try:
            n = sum(
                1
                for line in open(cpath, encoding="utf-8", errors="replace")
                if line.strip() and not line.lstrip().startswith("#")
            )
            cookies_src = f"есть ({n} строк, {os.path.basename(cpath)})"
        except OSError:
            cookies_src = "есть (не прочитаны)"
    log.info(
        "ffmpeg: %s · JS-рантаймы: %s · PO Token: %s · cookies: %s · YOUTUBE_COOKIES=%s",
        "есть" if downloader.ffmpeg_available() else "НЕТ (720p/1080p не соберутся)",
        ",".join(downloader._js_runtimes() or ["нет"]) or "нет",
        "да" if (os.getenv("POT_PROVIDER_URL") or "").strip() else "нет",
        cookies_src,
        (os.getenv("YOUTUBE_COOKIES") or "auto"),
    )
    if BOT_PROXY:
        log.info("Telegram API через прокси %s", BOT_PROXY)
    if LOCAL_SERVER:
        log.info("API_BASE: %s", API_BASE)

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="🚀 Запустить бота"),
            BotCommand(command="q", description="🎬 Как выбрать качество"),
            BotCommand(command="cancel", description="🛑 Отменить загрузку"),
            BotCommand(command="help", description="ℹ️ Помощь"),
        ])
        await bot.set_my_name("AuraSaver — скачать видео")
        await bot.set_my_description(
            "Скачиваю видео и аудио с YouTube, TikTok, Instagram, VK, RuTube, X (Twitter) "
            "и ещё 1800+ сайтов. Просто пришли ссылку — выбери качество и получи файл."
        )
        await bot.set_my_short_description(
            "Скачивает видео и аудио с YouTube, TikTok, Instagram и других сайтов по ссылке"
        )
        log.info("Меню команд и описание бота обновлены")
    except Exception as exc:
        log.info("Не удалось обновить профиль бота: %s", str(exc).splitlines()[-1][:120])

    if HEALTH_PORT and aio_web is not None:
        _spawn(_health_server())
    await dp.start_polling(bot)

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")
        sys.exit(1)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")
