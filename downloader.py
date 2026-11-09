from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
UPDATE_STATE_FILE = os.path.join(BASE_DIR, "ytdlp_update.json")

log = logging.getLogger("downloader")

# Клиенты YouTube, которые чаще всего ещё отдают DASH/HTTPS-форматы.
# web/mweb часто отдают только SABR/storyboard и ломают селектор формата
# («Requested format is not available») — особенно с серверных IP (Render и др.).
FALLBACK_CLIENTS: tuple[str, ...] = (
    "visionos", "tv_samsung", "android_vr", "android", "mweb", "web_safari",
)
# Последний шанс: клиенты, которые иногда дают хотя бы 360p (itag 18).
LIFELINE_CLIENTS: tuple[str, ...] = ("android", "android_vr", "visionos")

OUTTMPL = "%(title).100B [%(id)s].%(ext)s"

QUALITIES = (360, 480, 720, 1080)

BANNED_SUBSTRINGS = (
    "porn", "xxx", "nsfw", "sex", "adult", "onlyfans", "fansly",
    "stripchat", "chaturbate", "bongacams", "livejasmin", "camsoda",
    "myfreecams", "xhamster", "xvideos", "xnxx", "redtube", "youporn",
    "spankbang", "redgifs", "erome", "hqporner", "beeg", "tube8",
    "vporn", "porntrex", "rule34", "hentai", "nhentai", "hanime",
    "gelbooru", "e621", "fapello", "coomer", "kemono",
)

COOKIE_HINT_HOSTS = {
    "instagram.com", "instagr.am", "twitter.com", "x.com",
    "facebook.com", "fb.watch",
}

def _find_ffmpeg() -> str | None:
    env = (os.environ.get("FFMPEG_LOCATION") or "").strip()
    if env:
        return env
    hit = shutil.which("ffmpeg")
    if hit:
        return hit
    cand = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "ffmpeg.exe"
    )
    return cand if os.path.isfile(cand) else None

def ffmpeg_available() -> bool:
    return _find_ffmpeg() is not None

def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")

def is_allowed(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    if not host:
        return False
    return not any(bad in host for bad in BANNED_SUBSTRINGS)

def host_of(url: str) -> str:
    return _host(url)

def _cookie_hint_needed(host: str) -> bool:
    has_cookies = bool(_resolve_cookies_file()) or bool(os.environ.get("COOKIES_FROM_BROWSER"))
    return host in COOKIE_HINT_HOSTS and not has_cookies

def _convert_json_cookies_to_netscape(data: bytes, out_path: str) -> bool:
    """Расширения часто экспортируют cookies в JSON — yt-dlp умеет только Netscape."""
    import json

    try:
        obj = json.loads(data.decode("utf-8", errors="replace"))
    except Exception:
        return False
    items: list = []
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        for key in ("cookies", "data", "result"):
            val = obj.get(key)
            if isinstance(val, list):
                items = val
                break
        else:
            if isinstance(obj.get("name"), str):
                items = [obj]
    if not isinstance(items, list) or not items:
        return False

    lines = [
        "# Netscape HTTP Cookie File",
        "# Converted from JSON by AuraSaver",
    ]
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        value = "" if it.get("value") is None else str(it.get("value"))
        domain = str(it.get("domain") or it.get("host") or "").strip()
        if not domain:
            continue
        path = str(it.get("path") or "/").strip() or "/"
        exp = it.get("expirationDate") or it.get("expiry") or it.get("expires") or 2147483647
        try:
            exp = int(float(exp))
        except Exception:
            exp = 2147483647
        secure = bool(it.get("secure"))
        host_only = bool(it.get("hostOnly") or it.get("host_only"))
        if host_only:
            if domain.startswith("."):
                domain = domain[1:]
            flag_domain = "FALSE"
        else:
            if not domain.startswith("."):
                domain = "." + domain
            flag_domain = "TRUE"
        flag_secure = "TRUE" if secure else "FALSE"
        lines.append(chr(9).join((domain, flag_domain, path, flag_secure, str(exp), name, value)))

    if len(lines) <= 2:
        return False
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return False
    return True


def _ensure_netscape_cookies(path: str) -> str:
    """Если файл — JSON, конвертируем рядом в Netscape и возвращаем новый путь."""
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return path
    if not head:
        return path
    first = head.split(b"\n", 1)[0].lstrip()
    if first.startswith(b"#") or first.count(b"	") >= 5:
        return path  # уже Netscape
    if not first.startswith(b"[") and not first.startswith(b"{"):
        return path
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return path
    out = path.rstrip(".") + ".netscape.txt"
    if _convert_json_cookies_to_netscape(data, out):
        log.info("Cookies конвертированы из JSON в Netscape: %s", out)
        return out
    log.warning("Cookies похожи на JSON, но конвертировать не удалось: %s", path)
    return path


def _resolve_cookies_file() -> str | None:
    name = (os.environ.get("COOKIES_FILE") or "").strip()
    if not name:
        return None
    path = name if os.path.isabs(name) else os.path.join(BASE_DIR, name)
    if not os.path.isfile(path):
        return None
    return _ensure_netscape_cookies(path)


def _cookies_from_b64() -> str | None:
    import base64
    import tempfile

    b64 = re.sub(r"\s+", "", (os.environ.get("COOKIES_B64") or ""))
    if not b64:
        return None
    try:
        b64 += "=" * (-len(b64) % 4)
        data = base64.b64decode(b64)
        path = os.path.join(tempfile.gettempdir(), "aurasaver_cookies.txt")
        with open(path, "wb") as f:
            f.write(data)
        return _ensure_netscape_cookies(path)
    except Exception:
        return None

def apply_cookies(opts: dict[str, Any]) -> None:
    cf = _resolve_cookies_file() or _cookies_from_b64()
    if cf:
        opts["cookiefile"] = cf
    browser = (os.environ.get("COOKIES_FROM_BROWSER") or "").strip().lower()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    proxy = (os.environ.get("PROXY") or "").strip()
    if proxy:
        opts["proxy"] = proxy

def proxy_pool() -> list[str]:
    pool: list[str] = []
    current = (os.environ.get("PROXY") or "").strip()
    if current:
        pool.append(current)
    for p in (os.environ.get("PROXY_BACKUPS") or "").replace(";", ",").split(","):
        p = p.strip()
        if p and p not in pool:
            pool.append(p)
    return pool

def temp_dir(tag: str) -> str:
    path = os.path.join(DOWNLOADS_DIR, tag)
    os.makedirs(path, exist_ok=True)
    return path

def cleanup_downloads() -> None:
    try:
        if not os.path.isdir(DOWNLOADS_DIR):
            return
        for name in os.listdir(DOWNLOADS_DIR):
            path = os.path.join(DOWNLOADS_DIR, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
    except Exception:
        pass

def maybe_autoupdate_ytdlp() -> None:
    enabled = (os.environ.get("YTDLP_AUTOUPDATE") or "yes").lower() in ("1", "yes", "true", "on")
    if not enabled:
        return

    state: dict = {}
    try:
        with open(UPDATE_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    now = time.time()
    if now - float(state.get("last_check") or 0) < 86400:
        return
    state["last_check"] = now
    try:
        with open(UPDATE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

    before = yt_dlp.version.__version__
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet",
             "--no-warn-script-location", "yt-dlp"],
            capture_output=True, timeout=300,
        )
        shown = subprocess.run(
            [sys.executable, "-m", "pip", "show", "yt-dlp"],
            capture_output=True, text=True, timeout=60,
        )
        after = ""
        for line in (shown.stdout or "").splitlines():
            if line.lower().startswith("version:"):
                after = line.split(":", 1)[1].strip()
                break
        if after and after != before:
            log = logging.getLogger("downloader")
            log.info("yt-dlp обновлён %s -> %s (вступит в силу после перезапуска бота)", before, after)
    except Exception:
        pass

def make_screenshot(video_path: str, out_dir: str) -> str | None:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg or not os.path.isfile(video_path):
        return None
    out = os.path.join(out_dir, "preview.jpg")
    for seek in ("5", "20", "0"):
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-ss", seek, "-i", video_path,
            "-frames:v", "1",
            "-vf", "scale='min(320,iw)':-2",
            "-q:v", "7", out,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
            if os.path.isfile(out) and os.path.getsize(out) > 1024:
                return out
        except Exception:
            return None
    return None

def fetch_best_image(info: dict, proxy: str | None = None) -> str | None:
    import requests

    proxy = proxy or (os.environ.get("PROXY") or "").strip() or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": "Mozilla/5.0"}
    tmp = temp_dir("thumbs")
    tag = os.urandom(4).hex()

    for img in (info.get("images") or [])[:6]:
        u = img.get("url")
        if not u:
            continue
        try:
            r = requests.get(u, proxies=proxies, timeout=25, headers=headers)
            if r.status_code != 200 or len(r.content) < 3_000:
                continue
            data = r.content
        except Exception:
            continue

        if data[:2] == b"\xff\xd8":
            path = os.path.join(tmp, f"i_{tag}.jpg")
            with open(path, "wb") as f:
                f.write(data)
            return path
        if data[:4] == b"\x89PNG":
            path = os.path.join(tmp, f"i_{tag}.png")
            with open(path, "wb") as f:
                f.write(data)
            return path

        ffmpeg = _find_ffmpeg()
        if ffmpeg:
            raw = os.path.join(tmp, f"i_{tag}.raw")
            out = os.path.join(tmp, f"i_{tag}.jpg")
            try:
                with open(raw, "wb") as f:
                    f.write(data)
                subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-i", raw, "-frames:v", "1", "-q:v", "4", out],
                    capture_output=True, timeout=45,
                )
                if os.path.isfile(out) and os.path.getsize(out) > 1_024:
                    return out
            except Exception:
                return None
            finally:
                try:
                    os.remove(raw)
                except OSError:
                    pass
    return None

def youtube_quick(video_id: str, proxy: str | None = None) -> tuple[str | None, str]:
    import requests
    from concurrent.futures import ThreadPoolExecutor

    proxy = proxy or (os.environ.get("PROXY") or "").strip() or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": "Mozilla/5.0"}
    tmp = temp_dir("thumbs")
    tag = os.urandom(4).hex()

    def grab_cover() -> str | None:
        try:
            r = requests.get(
                f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                proxies=proxies, timeout=10, headers=headers,
            )
            if r.status_code == 200 and len(r.content) >= 1024:
                data = r.content
                if data[:2] == b"\xff\xd8":
                    cover = os.path.join(tmp, f"t_{tag}.jpg")
                    with open(cover, "wb") as f:
                        f.write(data)
                    return cover
                ffmpeg = _find_ffmpeg()
                if ffmpeg:
                    raw = os.path.join(tmp, f"t_{tag}.raw")
                    out = os.path.join(tmp, f"t_{tag}.jpg")
                    with open(raw, "wb") as f:
                        f.write(data)
                    try:
                        subprocess.run(
                            [ffmpeg, "-y", "-loglevel", "error", "-i", raw,
                             "-frames:v", "1", "-q:v", "5", out],
                            capture_output=True, timeout=45,
                        )
                        if os.path.isfile(out) and os.path.getsize(out) > 1024:
                            return out
                    finally:
                        try:
                            os.remove(raw)
                        except OSError:
                            pass
        except Exception:
            pass
        return None

    def grab_title() -> str:
        try:
            r = requests.get(
                "https://www.youtube.com/oembed",
                params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
                proxies=proxies, timeout=10, headers=headers,
            )
            if r.status_code == 200:
                return str(r.json().get("title") or "")
        except Exception:
            pass
        return ""

    with ThreadPoolExecutor(max_workers=2) as ex:
        cover = ex.submit(grab_cover).result()
        title = ex.submit(grab_title).result()
    return cover, title

def fetch_thumbnail(info: dict, proxy: str | None = None) -> str | None:
    import requests

    cands: list[str] = []
    best = None
    for t in info.get("thumbnails") or []:
        if t.get("url"):
            if best is None or (t.get("width") or 0) > (best.get("width") or 0):
                best = t
    if best and best.get("url"):
        cands.append(best["url"])
    if info.get("thumbnail"):
        cands.append(info["thumbnail"])

    proxy = proxy or (os.environ.get("PROXY") or "").strip() or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    tmp = temp_dir("thumbs")
    tag = os.urandom(4).hex()

    for u in cands:
        try:
            r = requests.get(u, proxies=proxies, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200 or len(r.content) < 1024:
                continue
            data = r.content
        except Exception:
            continue

        if data[:2] == b"\xff\xd8":
            path = os.path.join(tmp, f"t_{tag}.jpg")
            with open(path, "wb") as f:
                f.write(data)
            return path
        if data[:4] == b"\x89PNG":
            path = os.path.join(tmp, f"t_{tag}.png")
            with open(path, "wb") as f:
                f.write(data)
            return path

        ffmpeg = _find_ffmpeg()
        if ffmpeg:
            raw = os.path.join(tmp, f"t_{tag}.raw")
            out = os.path.join(tmp, f"t_{tag}.jpg")
            try:
                with open(raw, "wb") as f:
                    f.write(data)
                subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-i", raw, "-frames:v", "1", "-q:v", "5", out],
                    capture_output=True,
                    timeout=60,
                )
            except Exception:
                return None
            finally:
                try:
                    os.remove(raw)
                except OSError:
                    pass
            if os.path.isfile(out) and os.path.getsize(out) > 1024:
                return out
    return None

def _codec_pref_video(f: dict) -> int:
    c = (f.get("vcodec") or "").lower()
    if c.startswith(("avc1", "h264")):
        return 4
    if c.startswith(("vp9", "vp09")):
        return 3
    if c.startswith("hevc"):
        return 2
    if c.startswith("av01"):
        return 1
    return 0

def _codec_pref_audio(f: dict) -> int:
    c = (f.get("acodec") or "").lower()
    if c.startswith("mp4a") or f.get("ext") == "m4a":
        return 2
    return 1

def _size_of(f: dict, duration: float | None) -> int | None:
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and duration:
        br = f.get("tbr") or f.get("abr") or f.get("vbr")
        if br:
            size = int(br * 1000 * duration / 8)
    return int(size) if size else None

def _is_video_only(f: dict) -> bool:
    return f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")

def _is_audio_only(f: dict) -> bool:
    return f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")

def _is_progressive(f: dict) -> bool:
    return f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")

def _best_video(fmts: list[dict], max_height: int) -> dict | None:
    vids = [
        f for f in fmts
        if _is_video_only(f)
        and f.get("height") is not None
        and int(f["height"]) <= max_height
        and int(f["height"]) > 0
    ]
    if not vids:
        return None
    return max(
        vids,
        key=lambda f: (_codec_pref_video(f), int(f["height"]), float(f.get("tbr") or 0)),
    )

def _best_audio(fmts: list[dict]) -> dict | None:
    auds = [f for f in fmts if _is_audio_only(f)]
    if not auds:
        return None
    return max(
        auds,
        key=lambda f: (_codec_pref_audio(f), float(f.get("abr") or 0), float(f.get("tbr") or 0)),
    )

def estimate_variant(fmts: list[dict], max_height: int, duration: float | None) -> tuple[int | None, str]:
    prog = [
        f for f in fmts
        if _is_progressive(f)
        and f.get("height") is not None
        and int(f["height"]) <= max_height
        and _size_of(f, duration)
    ]
    if prog:
        best = max(
            prog,
            key=lambda f: (_codec_pref_video(f), int(f["height"] or 0), float(f.get("tbr") or 0)),
        )
        return _size_of(best, duration), f"{best.get('ext', 'mp4')} {int(best['height'])}p (одна дорожка)"

    v = _best_video(fmts, max_height)
    a = _best_audio(fmts)
    if v is None:
        return None, ""
    v_size = _size_of(v, duration)
    if a is None:
        return v_size, f"{v.get('ext', 'mp4')} {int(v['height'])}p"
    a_size = _size_of(a, duration)
    if v_size is None or a_size is None:
        return None, ""
    return v_size + a_size, f"{v.get('ext', 'mp4')} + {a.get('ext', 'm4a')} {int(v['height'])}p"

def build_format_expr(max_height: int) -> str:
    if ffmpeg_available():
        return (
            f"bv*[vcodec^=avc1][height<={max_height}]+ba[acodec^=mp4a]/"
            f"bv*[vcodec^=avc1]+ba/bv*[height<={max_height}]+ba/"
            f"b[height<={max_height}][ext=mp4]/b[height<={max_height}]/b"
        )
    return f"b[ext=mp4][height<={max_height}]/b[height<={max_height}]/b"

def _env_clients() -> list[str] | None:
    raw = (os.environ.get("YT_PLAYER_CLIENTS") or "").strip()
    if not raw:
        return None
    items = [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]
    return items or None


def _js_runtimes() -> dict[str, dict] | None:
    """Включаем найденные JS-рантаймы: без них YouTube часто не отдаёт форматы.

    yt-dlp по умолчанию использует только deno. Если на сервере есть node —
    его нужно включить явно.
    """
    override = (os.environ.get("YTDLP_JS_RUNTIME") or "").strip().lower()
    if override in ("none", "no", "off"):
        return None
    names = [override] if override else ("deno", "node", "bun", "quickjs")
    found: dict[str, dict] = {}
    for name in names:
        if shutil.which(name):
            found[name] = {}
    return found or None


def _base_opts(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "geo_bypass": True,
    }
    js = _js_runtimes()
    if js:
        opts["js_runtimes"] = js
    if extra:
        opts.update(extra)
    pot = _pot_extractor_args()
    if pot:
        _merge_pot(opts)
    return opts


def _apply_clients(opts: dict[str, Any], clients: list[str] | None) -> None:
    if not clients:
        return
    ea = dict(opts.get("extractor_args") or {})
    yt = dict(ea.get("youtube") or {})
    yt["player_client"] = list(clients)
    ea["youtube"] = yt
    opts["extractor_args"] = ea


def _is_storyboard(f: dict) -> bool:
    if f.get("protocol") == "mhtml" or (f.get("ext") or "").lower() == "mhtml":
        return True
    return "storyboard" in (f.get("format_note") or "").lower()


def _is_av_format(f: dict) -> bool:
    if _is_storyboard(f):
        return False
    if (f.get("ext") or "").lower() in ("jpg", "jpeg", "png", "webp", "gif"):
        return False
    has_v = f.get("vcodec") not in (None, "none")
    has_a = f.get("acodec") not in (None, "none")
    return has_v or has_a


def _client_chains() -> list[list[str] | None]:
    env = _env_clients()
    if env:
        extra = [c for c in FALLBACK_CLIENTS if c not in env]
        return [env, env + extra, list(LIFELINE_CLIENTS)]
    return [
        None,
        list(FALLBACK_CLIENTS),
        list(LIFELINE_CLIENTS),
        ["mweb", "web_safari", "web"],
    ]


def friendly_error(exc: BaseException) -> str:
    """Человекочитаемая ошибка вместо сырого лога yt-dlp."""
    raw = (str(exc) or "").strip()
    last = raw.splitlines()[-1] if raw else type(exc).__name__
    blob = (raw + "\n" + last).lower()
    if "requested format is not available" in blob or "use --list-formats" in blob:
        return (
            "YouTube не отдал рабочих форматов (SABR / PO Token на этом IP). "
            "Попробуйте ещё раз, смените IP/прокси или настройте PO Token "
            "провайдер — см. README_RENDER.md."
        )
    if "cookies file must be netscape" in blob or "not json" in blob and "cookies" in blob:
        return (
            "Файл cookies не в формате Netscape. Экспортируйте cookies расширением "
            "«Get cookies.txt LOCALLY» в формате Netscape (не JSON) и обновите COOKIES_B64."
        )
    if "sign in to confirm" in blob or "not a bot" in blob:
        return "YouTube просит подтвердить, что вы не бот — нужен cookies (COOKIES_B64, см. README_RENDER)."
    if "the page needs to be reloaded" in blob:
        return "YouTube попросил перезагрузить страницу. Отправьте ссылку ещё раз."
    if "unavailable" in blob or "video unavailable" in blob:
        return "Видео недоступно: удалено, приватное или заблокировано в регионе."
    return last[:300]


def _retryable_download_error(exc: BaseException) -> bool:
    blob = str(exc).lower()
    return any(
        k in blob
        for k in (
            "requested format is not available",
            "no video formats",
            "sign in to confirm",
            "not a bot",
            "the page needs to be reloaded",
            "http error 403",
            "unplayable",
            "login_required",
            "sabr",
        )
    )


def _ffmpeg_flag(opts: dict[str, Any]) -> None:
    ffmpeg_path = _find_ffmpeg()
    if ffmpeg_path and not os.path.isdir(ffmpeg_path.lstrip('"')):
        opts["ffmpeg_location"] = ffmpeg_path


def _pot_extractor_args() -> dict[str, list[str]]:
    url = (os.environ.get("POT_PROVIDER_URL") or "").strip()
    if not url:
        return {}
    return {"youtubepot-bgutilhttp": {"base_url": [url]}}


def _merge_pot(opts: dict[str, Any]) -> None:
    pot = _pot_extractor_args()
    if not pot:
        return
    ea = dict(opts.get("extractor_args") or {})
    ea.update(pot)
    opts["extractor_args"] = ea


def _unwrap_playlist(info: dict) -> dict:
    if info.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise yt_dlp.utils.DownloadError("По ссылке не найдено ни одного видео.")
        return dict(entries[0])
    return info

def _extract_meta(url: str, proxy: str | None, clients: list[str] | None) -> dict:
    opts = _base_opts()
    _apply_clients(opts, clients)
    apply_cookies(opts)
    if proxy:
        opts["proxy"] = proxy
    with yt_dlp.YoutubeDL(opts) as ydl:
        # process=False не запускает селектор формата: YouTube, отдающий
        # только SABR/storyboard, больше не роняет превью ошибкой
        # «Requested format is not available».
        return _unwrap_playlist(ydl.extract_info(url, download=False, process=False))


def _media_info_from_raw(info: dict, url: str) -> dict:
    duration = info.get("duration")
    fmts = info.get("formats") or []
    variants: dict[int, tuple[int | None, str]] = {}
    has_media = False
    for h in QUALITIES:
        est, desc = estimate_variant(fmts, h, duration)
        variants[h] = (est, desc)
        if est:
            has_media = True
    if not has_media:
        has_media = any(_is_av_format(f) for f in fmts)

    host = host_of(info.get("webpage_url") or url)

    images = sorted(
        (
            {
                "url": f.get("url"),
                "ext": (f.get("ext") or "").lower(),
                "width": int(f.get("width") or 0),
                "height": int(f.get("height") or 0),
            }
            for f in fmts
            if f.get("url")
            and (
                (f.get("vcodec") or "").lower().startswith(("jpeg", "jpg", "png", "webp", "gif", "image"))
                or (f.get("ext") or "").lower() in ("jpg", "jpeg", "png", "webp", "gif")
            )
            and int(f.get("width") or f.get("height") or 0) >= 200
        ),
        key=lambda x: x["width"] * x["height"],
        reverse=True,
    )

    only_storyboards = bool(fmts) and not any(_is_av_format(f) for f in fmts)
    is_yt = "youtube" in host or host == "youtu.be"
    return {
        "title": info.get("title") or "Видео",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": duration,
        "host": host,
        "live": bool(info.get("is_live")) or info.get("live_status") in ("is_live", "is_upcoming"),
        "variants": variants,
        "no_media": not has_media,
        "sabr": only_storyboards and is_yt,
        "images": images[:6],
        "cookie_hint": _cookie_hint_needed(host),
        "ffmpeg_ok": ffmpeg_available(),
        "thumbnail": info.get("thumbnail"),
        "thumbnails": info.get("thumbnails") or [],
    }


def get_media_info(url: str, proxy: str | None = None) -> dict:
    last_exc: Exception | None = None
    empty: dict | None = None
    for clients in _client_chains():
        try:
            info = _extract_meta(url, proxy, clients)
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception as exc:
            last_exc = exc
            log.info(
                "метаданные (%s): %s",
                ",".join(clients) if clients else "default",
                str(exc).splitlines()[-1][:140],
            )
            continue
        if any(_is_av_format(f) for f in (info.get("formats") or [])):
            return _media_info_from_raw(info, url)
        if empty is None:
            empty = info

    # Второй шанс: обычная обработка yt-dlp (нужна части сайтов, не YouTube).
    try:
        opts = _base_opts()
        apply_cookies(opts)
        if proxy:
            opts["proxy"] = proxy
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = _unwrap_playlist(ydl.extract_info(url, download=False))
        if info.get("formats"):
            return _media_info_from_raw(info, url)
    except yt_dlp.utils.DownloadCancelled:
        raise
    except Exception as exc:
        last_exc = last_exc or exc
        log.info("метаданные (process=True): %s", str(exc).splitlines()[-1][:140])

    if empty is not None:
        return _media_info_from_raw(empty, url)
    raise last_exc if last_exc else RuntimeError("Не удалось получить данные по ссылке.")


def _download_hook(reporter: Any):
    def hook(d: dict) -> None:
        if reporter.cancelled():
            raise yt_dlp.utils.DownloadCancelled("Загрузка отменена пользователем")
        reporter.update(d)

    return hook


def _download_common(temp_dir_path: str, hook) -> dict[str, Any]:
    opts = _base_opts({
        "outtmpl": {"default": OUTTMPL},
        "paths": {"home": temp_dir_path},
        "noprogress": True,
        "retries": 3,
        "concurrent_fragment_downloads": 8,
        "windowsfilenames": True,
        "progress_hooks": [hook],
    })
    _ffmpeg_flag(opts)
    return opts


def _finish_download(info: dict) -> tuple[str, dict]:
    rd = info.get("requested_downloads") or []
    filepath = (rd[0].get("filepath") or rd[0].get("filename")) if rd else None
    if not filepath or not os.path.isfile(filepath):
        raise RuntimeError(
            "Готовый файл не найден. Если качаете 720p/1080p — установите ffmpeg "
            "(см. README) и перезапустите бота."
        )
    return filepath, info


def _video_attempts(max_height: int) -> list[dict[str, Any]]:
    env = _env_clients()
    fb = list(env) if env else list(FALLBACK_CLIENTS)
    attempts: list[dict[str, Any]] = []
    first: dict[str, Any] = {"format": build_format_expr(max_height)}
    if ffmpeg_available():
        first["merge_output_format"] = "mp4"
    if env:
        first["extractor_args"] = {"youtube": {"player_client": list(env)}}
    attempts.append(first)

    if ffmpeg_available():
        attempts.append({
            "format": build_format_expr(max_height),
            "merge_output_format": "mp4",
            "extractor_args": {"youtube": {"player_client": fb}},
        })
        attempts.append({
            "format_sort": ["vcodec:h264", "quality", "res", "acodec:aac"],
            "merge_output_format": "mp4",
            "extractor_args": {"youtube": {"player_client": fb}},
        })
        attempts.append({
            "format": f"bv*+ba/b[height<={max_height}]/b",
            "merge_output_format": "mp4",
            "extractor_args": {"youtube": {"player_client": fb}},
        })
    else:
        attempts.append({
            "format": f"b[ext=mp4][height<={max_height}]/b[height<={max_height}]/b",
            "extractor_args": {"youtube": {"player_client": fb}},
        })
    attempts.append({
        "format": "b",
        "extractor_args": {"youtube": {"player_client": list(LIFELINE_CLIENTS)}},
    })
    return attempts


def download(
    url: str,
    max_height: int,
    temp_dir_path: str,
    reporter: Any,
    proxy: str | None = None,
) -> tuple[str, dict]:
    hook = _download_hook(reporter)
    last: Exception | None = None
    for extra in _video_attempts(max_height):
        opts = _download_common(temp_dir_path, hook)
        opts.update(extra)
        _merge_pot(opts)
        apply_cookies(opts)
        if proxy:
            opts["proxy"] = proxy
        label = extra.get("format") or extra.get("format_sort") or "?"
        log.info("скачиваю видео: %s", label)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = _unwrap_playlist(ydl.extract_info(url, download=True))
            return _finish_download(info)
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception as exc:
            last = exc
            log.info("попытка не удалась: %s", str(exc).splitlines()[-1][:160])
            if not _retryable_download_error(exc):
                raise
    if last is not None:
        raise last
    raise RuntimeError("Не удалось скачать видео.")


def _audio_attempts() -> list[dict[str, Any]]:
    pp = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ]
    env = _env_clients()
    fb = list(env) if env else list(FALLBACK_CLIENTS)
    first: dict[str, Any] = {"format": "ba[acodec^=mp4a]/ba/b", "postprocessors": pp}
    if env:
        first["extractor_args"] = {"youtube": {"player_client": list(env)}}
    return [
        first,
        {
            "format": "ba[acodec^=mp4a]/ba/b",
            "postprocessors": pp,
            "extractor_args": {"youtube": {"player_client": fb}},
        },
        {
            "format": "bestaudio/b",
            "postprocessors": pp,
            "extractor_args": {"youtube": {"player_client": fb}},
        },
        {
            "format": "b",
            "postprocessors": pp,
            "extractor_args": {"youtube": {"player_client": list(LIFELINE_CLIENTS)}},
        },
    ]


def download_audio(
    url: str,
    temp_dir_path: str,
    reporter: Any,
    proxy: str | None = None,
) -> tuple[str, dict]:
    hook = _download_hook(reporter)
    last: Exception | None = None
    for extra in _audio_attempts():
        opts = _download_common(temp_dir_path, hook)
        opts.update(extra)
        _merge_pot(opts)
        apply_cookies(opts)
        if proxy:
            opts["proxy"] = proxy
        log.info("скачиваю аудио: %s", extra.get("format") or "?")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = _unwrap_playlist(ydl.extract_info(url, download=True))

            rd = info.get("requested_downloads") or []
            filepath = (rd[0].get("filepath") or rd[0].get("filename")) if rd else None
            if filepath and not filepath.lower().endswith(".mp3"):
                cand = filepath.rsplit(".", 1)[0] + ".mp3"
                if os.path.isfile(cand):
                    filepath = cand
            if not filepath or not os.path.isfile(filepath):
                raise RuntimeError("Аудио-файл не найден (нужен ffmpeg — см. README).")
            return filepath, info
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception as exc:
            last = exc
            log.info("аудио-попытка не удалась: %s", str(exc).splitlines()[-1][:160])
            if not _retryable_download_error(exc):
                raise
    if last is not None:
        raise last
    raise RuntimeError("Не удалось скачать аудио.")
