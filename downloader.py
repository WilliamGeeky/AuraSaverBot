from __future__ import annotations

import json
import logging
import os
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

def _resolve_cookies_file() -> str | None:
    name = (os.environ.get("COOKIES_FILE") or "").strip()
    if not name:
        return None
    path = name if os.path.isabs(name) else os.path.join(BASE_DIR, name)
    return path if os.path.isfile(path) else None

def apply_cookies(opts: dict[str, Any]) -> None:
    cf = _resolve_cookies_file()
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

def _unwrap_playlist(info: dict) -> dict:
    if info.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise yt_dlp.utils.DownloadError("По ссылке не найдено ни одного видео.")
        return dict(entries[0])
    return info

def get_media_info(url: str, proxy: str | None = None) -> dict:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "geo_bypass": True,
    }
    apply_cookies(opts)
    if proxy:
        opts["proxy"] = proxy
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = _unwrap_playlist(ydl.extract_info(url, download=False))

    duration = info.get("duration")
    fmts = info.get("formats") or []
    variants: dict[int, tuple[int | None, str]] = {}
    has_media = False
    for h in QUALITIES:
        est, desc = estimate_variant(fmts, h, duration)
        variants[h] = (est, desc)
        if est:
            has_media = True

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

    return {
        "title": info.get("title") or "Видео",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": duration,
        "host": host,
        "live": bool(info.get("is_live")),
        "variants": variants,
        "no_media": not has_media,
        "images": images[:6],
        "cookie_hint": _cookie_hint_needed(host),
        "ffmpeg_ok": ffmpeg_available(),
        "thumbnail": info.get("thumbnail"),
        "thumbnails": info.get("thumbnails") or [],
    }

def download(url: str, max_height: int, temp_dir_path: str, reporter: Any, proxy: str | None = None) -> tuple[str, dict]:
    def hook(d: dict) -> None:
        if reporter.cancelled():
            raise yt_dlp.utils.DownloadCancelled("Загрузка отменена пользователем")
        reporter.update(d)

    opts: dict[str, Any] = {
        "format": build_format_expr(max_height),
        "merge_output_format": "mp4",
        "outtmpl": {"default": OUTTMPL},
        "paths": {"home": temp_dir_path},
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "concurrent_fragment_downloads": 8,
        "windowsfilenames": True,
        "noplaylist": True,
        "geo_bypass": True,
        "progress_hooks": [hook],
    }
    ffmpeg_path = _find_ffmpeg()
    if ffmpeg_path and not os.path.isdir(ffmpeg_path.lstrip('"')):
        opts["ffmpeg_location"] = ffmpeg_path
    apply_cookies(opts)
    if proxy:
        opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = _unwrap_playlist(ydl.extract_info(url, download=True))

    rd = info.get("requested_downloads") or []
    filepath = (rd[0].get("filepath") or rd[0].get("filename")) if rd else None
    if not filepath or not os.path.isfile(filepath):
        raise RuntimeError(
            "Готовый файл не найден. Если качаете 720p/1080p — установите ffmpeg "
            "(см. README) и перезапустите бота."
        )
    return filepath, info

def download_audio(url: str, temp_dir_path: str, reporter: Any, proxy: str | None = None) -> tuple[str, dict]:
    def hook(d: dict) -> None:
        if reporter.cancelled():
            raise yt_dlp.utils.DownloadCancelled("Загрузка отменена пользователем")
        reporter.update(d)

    opts: dict[str, Any] = {
        "format": "ba[acodec^=mp4a]/ba/b",
        "outtmpl": {"default": OUTTMPL},
        "paths": {"home": temp_dir_path},
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "concurrent_fragment_downloads": 8,
        "windowsfilenames": True,
        "noplaylist": True,
        "geo_bypass": True,
        "progress_hooks": [hook],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    ffmpeg_path = _find_ffmpeg()
    if ffmpeg_path and not os.path.isdir(ffmpeg_path.lstrip('"')):
        opts["ffmpeg_location"] = ffmpeg_path
    apply_cookies(opts)
    if proxy:
        opts["proxy"] = proxy

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
