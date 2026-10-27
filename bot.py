"""Render-friendly Telegram media downloader bot.

Features: yt-dlp supported public URLs, quality selection, MP3, subtitles,
playlists (bounded), history/stats, progress, recursive file splitting, health route.

Important: no downloader can guarantee every site/video will always work. Private,
DRM-protected, geo-blocked, deleted, login-required, or extractor-broken media can fail.
"""
from __future__ import annotations

import asyncio
import html
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand,
    InputMediaPhoto, InputMediaVideo,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------- Configuration ---------------------------
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment settings.")

# Telegram's standard Bot API currently accepts uploads up to 50 MB. Use a margin.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MIB", "45")) * 1024 * 1024
MAX_SOURCE_BYTES = int(os.environ.get("MAX_SOURCE_MIB", "1024")) * 1024 * 1024
MAX_PLAYLIST = max(1, min(int(os.environ.get("MAX_PLAYLIST", "10")), 20))
MAX_BATCH_LINKS = max(1, min(int(os.environ.get("MAX_BATCH_LINKS", "10")), 20))
MAX_PENDING_QUEUE = max(20, min(int(os.environ.get("MAX_PENDING_QUEUE", "100")), 200))
YOUTUBE_VERIFICATION_COOLDOWN = max(0, min(int(os.environ.get("YOUTUBE_VERIFICATION_COOLDOWN_SECONDS", "600")), 3600))
BOT_VERSION = "v12.6.2-quality-menu-default"
USER_COOLDOWN = max(0, int(os.environ.get("USER_COOLDOWN_SECONDS", "15")))
DOWNLOAD_TIMEOUT = max(90, int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "480")))
FFMPEG_TIMEOUT = max(60, int(os.environ.get("FFMPEG_TIMEOUT_SECONDS", "240")))
# Full software transcoding on Render Free is expensive. Limit it separately and
# prefer stream-copy/remux before ever encoding video frames.
VIDEO_TRANSCODE_TIMEOUT = max(30, min(240, int(os.environ.get("VIDEO_TRANSCODE_TIMEOUT_SECONDS", "120"))))
VIDEO_TRANSCODE_MAX_HEIGHT = max(360, min(1080, int(os.environ.get("VIDEO_TRANSCODE_MAX_HEIGHT", "720"))))
AUTO_DELETE_SECONDS = max(60, int(os.environ.get("AUTO_DELETE_SECONDS", "600")))
MAX_PARTS = 128
MAX_SPLIT_DEPTH = 8
WORK_DIR = os.environ.get("WORK_DIR", "/tmp")
MAX_VIDEO_HEIGHT = 1080
QUALITY_LEVELS = (1080, 720, 480, 360, 240, 144)
QUALITY_PREFERENCES_FILE = os.environ.get(
    "QUALITY_PREFERENCES_FILE", os.path.join(WORK_DIR, "dlbot_quality_preferences.json")
)
_DEFAULT_VIDEO_QUALITY_RAW = os.environ.get("DEFAULT_VIDEO_QUALITY", "best").strip().lower()
if _DEFAULT_VIDEO_QUALITY_RAW in {"best", "auto", "automatic", ""}:
    DEFAULT_VIDEO_QUALITY_VALUE: int | None = None
else:
    try:
        DEFAULT_VIDEO_QUALITY_VALUE = int(_DEFAULT_VIDEO_QUALITY_RAW)
    except ValueError as exc:
        raise RuntimeError("DEFAULT_VIDEO_QUALITY must be 'best' or one of: 1080, 720, 480, 360, 240, 144") from exc
    if DEFAULT_VIDEO_QUALITY_VALUE not in QUALITY_LEVELS:
        raise RuntimeError("DEFAULT_VIDEO_QUALITY must be 'best' or one of: 1080, 720, 480, 360, 240, 144")
# Pending message deletions survive an in-process restart when this state file survives.
# Render Free storage is ephemeral across instance replacement/redeploy.
AUTO_DELETE_STATE_FILE = os.environ.get("AUTO_DELETE_STATE_FILE", os.path.join(WORK_DIR, "dlbot_auto_delete.json"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Render Free web services sleep without inbound HTTP traffic. Webhook mode is the
# Render default; local environments without a Render URL continue to use polling.
TELEGRAM_MODE = os.environ.get(
    "TELEGRAM_MODE", "webhook" if os.environ.get("RENDER_EXTERNAL_URL") else "polling"
).strip().lower()
WEBHOOK_BASE_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = "/" + os.environ.get("TELEGRAM_WEBHOOK_PATH", "telegram").strip("/")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
if not WEBHOOK_SECRET:
    # Keeps first-time Render deployment simple while deriving an unguessable header
    # secret from the already-secret bot token. An explicit independent secret is preferred.
    WEBHOOK_SECRET = hashlib.sha256((TOKEN + ":nabalucky-webhook").encode("utf-8")).hexdigest()
if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
    raise RuntimeError("TELEGRAM_WEBHOOK_SECRET must contain only letters, digits, '_' or '-' (max 256 chars).")
if TELEGRAM_MODE not in {"polling", "webhook"}:
    raise RuntimeError("TELEGRAM_MODE must be either 'polling' or 'webhook'.")
STATS_FILE = os.environ.get("STATS_FILE", "/tmp/dlbot_stats.json")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "/tmp/dlbot_history.json")
ADMIN_IDS = {
    int(value.strip()) for value in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpeg", ".mpg", ".ts"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac", ".weba"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs complete Telegram Bot API request URLs, which include the secret token.
# Never allow those URLs into application/Render logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

class _SecretRedactionFilter(logging.Filter):
    """Redact bot tokens even if a third-party exception contains an API URL."""
    _token_url = re.compile(r"bot[0-9]{5,}:[A-Za-z0-9_-]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        message = self._token_url.sub("bot[REDACTED]", message)
        if TOKEN:
            message = message.replace(TOKEN, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True

_root_logger = logging.getLogger()
for _handler in _root_logger.handlers:
    _handler.addFilter(_SecretRedactionFilter())
log = logging.getLogger("media-bot")

_STATE_LOCK = threading.RLock()
_last_job: dict[int, float] = {}
_user_quality: dict[int, int | None] = {}
_QUALITY_LOCK = threading.RLock()
_QUALITY_PREF_UNSET = object()
_active_users: set[int] = set()
_PENDING: dict[str, dict[str, Any]] = {}
_DELETE_TASKS: dict[tuple[int, int], asyncio.Task] = {}
_DELETE_DUE: dict[tuple[int, int], float] = {}
_DELETE_STATE_LOCK = threading.RLock()
_DELETE_DB_LOCK = threading.RLock()
_DELETE_DB_READY = False
_DELETE_DB_NEXT_RETRY = 0.0
BOT_STARTED = False
BOT_READY = False
LAST_TELEGRAM_OK = 0.0
SERVICE_BOOT_TIME = time.monotonic()

WELCOME = (
    "Hi! Send a public video or reel URL and I'll try to download it.\n\n"
    "A single YouTube video/Short opens a quality menu before downloading. Send multiple links in one message, or send rapid follow-up links, to queue downloads automatically in order.\n\n"
    "Commands:\n"
    "/audio URL — queue audio extraction as MP3\n"
    "/sub URL — queue available English/Hindi subtitles\n"
    "/playlist URL — queue a bounded playlist\n"
    "/choose URL — open the per-video quality menu\n"
    f"Send up to {MAX_BATCH_LINKS} links in one message; up to {MAX_PENDING_QUEUE} jobs can wait in the queue.\n"
    "/q or /quality — set your default YouTube quality\n"
    "/choose URL — choose a quality for one YouTube video\n"
    "/auto — use best available quality for future video downloads\n"
    "/stats — view your download statistics\n"
    "/history — show your last five links\n"
    "/again NUMBER — queue a recent link again\n"
    "/disk — show temporary disk usage\n\n"
    "Please download only media you own or have permission to save."
)

# --------------------------- Persistent-ish JSON state ---------------------------
def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return default


def _write_json_atomic(path: str, payload: Any) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)


def _normalize_quality_preference(value: Any) -> int | None:
    """Return a valid maximum height, or None for best-available/automatic quality."""
    if value is None or (isinstance(value, str) and value.strip().lower() in {"best", "auto", "automatic", ""}):
        return None
    try:
        height = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Unsupported quality preference.") from exc
    if height not in QUALITY_LEVELS:
        raise ValueError("Choose best available (up to 1080p) or one of: 1080p, 720p, 480p, 360p, 240p, 144p.")
    return height


def _load_quality_preferences_file() -> None:
    """Load user quality preferences from local state; invalid values are ignored."""
    raw = _read_json(QUALITY_PREFERENCES_FILE, {})
    if not isinstance(raw, dict):
        return
    loaded: dict[int, int | None] = {}
    for user_id, value in raw.items():
        try:
            uid = int(user_id)
            if uid > 0:
                loaded[uid] = _normalize_quality_preference(value)
        except (TypeError, ValueError):
            continue
    with _QUALITY_LOCK:
        _user_quality.update(loaded)


def _save_quality_preferences_file() -> None:
    with _QUALITY_LOCK:
        payload = {str(uid): value for uid, value in _user_quality.items()}
    _write_json_atomic(QUALITY_PREFERENCES_FILE, payload)


def _get_quality_preference(user_id: int) -> int | None:
    with _QUALITY_LOCK:
        if int(user_id) in _user_quality:
            return _user_quality[int(user_id)]
    return DEFAULT_VIDEO_QUALITY_VALUE


def _set_quality_preference_sync(user_id: int, value: Any) -> int | None:
    """Persist a per-user default locally and to PostgreSQL when configured."""
    normalized = _normalize_quality_preference(value)
    with _QUALITY_LOCK:
        _user_quality[int(user_id)] = normalized
        payload = {str(uid): pref for uid, pref in _user_quality.items()}
    try:
        _write_json_atomic(QUALITY_PREFERENCES_FILE, payload)
    except OSError as exc:
        log.warning("Could not persist quality preferences locally (%s)", type(exc).__name__)

    if DATABASE_URL:
        try:
            if _init_delete_db():
                import psycopg
                with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
                    conn.execute(
                        "INSERT INTO bot_user_quality_preferences (user_id, max_height, updated_at) "
                        "VALUES (%s, %s, CURRENT_TIMESTAMP) "
                        "ON CONFLICT (user_id) DO UPDATE SET max_height = EXCLUDED.max_height, "
                        "updated_at = CURRENT_TIMESTAMP",
                        (int(user_id), normalized),
                    )
        except Exception as exc:
            try:
                _mark_delete_db_unavailable()
            except Exception:
                pass
            log.warning("Could not persist quality preference to PostgreSQL (%s)", type(exc).__name__)
    return normalized


def _load_quality_preferences_from_db() -> int:
    """Overlay durable preferences from PostgreSQL on local fallback state."""
    if not DATABASE_URL or not _init_delete_db():
        return 0
    try:
        import psycopg
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            rows = conn.execute(
                "SELECT user_id, max_height FROM bot_user_quality_preferences"
            ).fetchall()
        count = 0
        with _QUALITY_LOCK:
            for user_id, value in rows:
                try:
                    _user_quality[int(user_id)] = _normalize_quality_preference(value)
                    count += 1
                except (TypeError, ValueError):
                    continue
            payload = {str(uid): pref for uid, pref in _user_quality.items()}
        try:
            _write_json_atomic(QUALITY_PREFERENCES_FILE, payload)
        except OSError as exc:
            log.warning("Could not refresh local quality preferences (%s)", type(exc).__name__)
        return count
    except Exception as exc:
        try:
            _mark_delete_db_unavailable()
        except Exception:
            pass
        log.warning("Could not load quality preferences from PostgreSQL (%s)", type(exc).__name__)
        return 0


def _quality_preference_label(value: int | None) -> str:
    return f"Up to {value}p" if value is not None else "Best available (automatic)"


_load_quality_preferences_file()


def load_stats() -> dict[str, Any]:
    with _STATE_LOCK:
        value = _read_json(STATS_FILE, {})
        return value if isinstance(value, dict) else {}


def bump_stats(uid: int, kind: str, byte_count: int) -> None:
    """Count one source item; fields absent in older JSON files are added lazily."""
    with _STATE_LOCK:
        stats = _read_json(STATS_FILE, {})
        if not isinstance(stats, dict):
            stats = {}
        user = stats.setdefault(str(uid), {})
        defaults = {"videos": 0, "audios": 0, "images": 0, "files": 0,
                    "bundles": 0, "parts": 0, "mb": 0.0}
        for name, value in defaults.items():
            user.setdefault(name, value)
        key = {"audio": "audios", "image": "images", "video": "videos"}.get(kind, "files")
        user[key] = int(user.get(key, 0)) + 1
        if key != "files":
            user["files"] = int(user.get("files", 0)) + 1
        user["mb"] = round(float(user.get("mb", 0.0)) + max(0, byte_count) / 1048576, 1)
        _write_json_atomic(STATS_FILE, stats)


def bump_bundle_stats(uid: int, part_count: int) -> None:
    with _STATE_LOCK:
        stats = _read_json(STATS_FILE, {})
        if not isinstance(stats, dict):
            stats = {}
        user = stats.setdefault(str(uid), {})
        user.setdefault("bundles", 0)
        user.setdefault("parts", 0)
        user["bundles"] = int(user.get("bundles", 0)) + 1
        user["parts"] = int(user.get("parts", 0)) + max(0, int(part_count))
        _write_json_atomic(STATS_FILE, stats)


def add_history(uid: int, url: str) -> None:
    with _STATE_LOCK:
        history = _read_json(HISTORY_FILE, {})
        if not isinstance(history, dict):
            history = {}
        items = history.get(str(uid), [])
        if not isinstance(items, list):
            items = []
        items = [item for item in items if item != url]
        items.insert(0, url)
        history[str(uid)] = items[:5]
        _write_json_atomic(HISTORY_FILE, history)


def get_history(uid: int) -> list[str]:
    with _STATE_LOCK:
        history = _read_json(HISTORY_FILE, {})
        items = history.get(str(uid), []) if isinstance(history, dict) else []
        return items[:5] if isinstance(items, list) else []

# --------------------------- URL and subprocess helpers ---------------------------
def first_url(text: str | None) -> str | None:
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,!;:)]}")


def extract_urls(text: str | None, limit: int | None = None) -> list[str]:
    """Return distinct HTTP(S) URLs in message order, trimming common punctuation."""
    cap = limit or MAX_BATCH_LINKS + 1
    urls: list[str] = []
    for match in URL_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(".,!;:)]}")
        if candidate and candidate not in urls:
            urls.append(candidate)
        if len(urls) >= cap:
            break
    return urls


def _is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def _is_youtube_playlist_url(url: str) -> bool:
    """Identify regular YouTube playlist URLs, including watch URLs carrying list=."""
    if not _is_youtube_url(url):
        return False
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return parsed.path.rstrip("/").lower() == "/playlist" or bool(query.get("list"))


def _youtube_video_id(url: str) -> str | None:
    """Extract a compact YouTube video ID so menu callbacks survive process restarts."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        query = parse_qs(parsed.query)
        candidate = (query.get("v") or [""])[0]
        if not candidate:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0].lower() in {"shorts", "embed", "live", "v"}:
                candidate = parts[1]
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", candidate or "") else None


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Please provide a valid public HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded usernames or passwords are not accepted.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("Local or internal URLs are not supported.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # ordinary hostname, not a literal IP address
    if ip is not None and not ip.is_global:
        raise ValueError("Private or reserved IP addresses are not supported.")
    return url


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DENO_DIR", "/tmp/deno")
    env.setdefault("XDG_CACHE_HOME", "/tmp/xdg")
    return env


def _cookie_args(workdir: str) -> list[str]:
    """Read an optional YouTube cookie secret; Instagram cookies are intentionally unused."""
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return []
    cookie_file = os.path.join(workdir, "cookies.txt")
    with open(cookie_file, "w", encoding="utf-8") as stream:
        stream.write(raw.replace("\\n", "\n"))
    os.chmod(cookie_file, 0o600)
    return ["--cookies", cookie_file]

def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=4)
    except Exception:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            pass


def _run_streaming_command(
    command: list[str], timeout: int, progress: dict[str, Any] | None = None
) -> tuple[int, list[str]]:
    """Run a command with a deadline, draining merged output to avoid pipe deadlocks."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        env=_base_env(),
        start_new_session=(os.name == "posix"),
    )
    tail: deque[str] = deque(maxlen=60)

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            tail.append(line)
            if progress is not None and "PROGRESS|" in line:
                try:
                    payload = line.split("PROGRESS|", 1)[1].split("|")
                    progress["pct"] = payload[0].strip()
                    progress["spd"] = payload[1].strip() if len(payload) > 1 else ""
                    progress["done"] = int(float(payload[2] or 0)) if len(payload) > 2 else 0
                    progress["total"] = int(float(payload[3] or 0)) if len(payload) > 3 else 0
                except (ValueError, IndexError):
                    pass

    thread = threading.Thread(target=reader, daemon=True, name="subprocess-output")
    thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    exceeded_size_limit = False
    while process.poll() is None:
        if progress is not None and int(progress.get("done", 0) or 0) > MAX_SOURCE_BYTES:
            exceeded_size_limit = True
            _stop_process(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _stop_process(process)
            break
        time.sleep(0.2)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _stop_process(process)
    thread.join(timeout=5)
    if timed_out:
        raise TimeoutError(f"Process exceeded its {timeout}-second time limit")
    if exceeded_size_limit:
        raise RuntimeError(f"The download exceeded the configured {MAX_SOURCE_BYTES // 1048576} MiB safety limit.")
    return int(process.returncode or 0), list(tail)


def _run_capture(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=_base_env(),
        check=False,
    )


def _is_youtube_bot_check(output: str | list[str]) -> bool:
    """Recognize YouTube's explicit sign-in-to-confirm request without exposing its raw log."""
    text = "\n".join(output) if isinstance(output, list) else str(output)
    lowered = text.lower()
    return (
        "sign in to confirm you're not a bot" in lowered
        or "sign in to confirm you’re not a bot" in lowered
        or "confirm you're not a bot" in lowered
        or "confirm you’re not a bot" in lowered
    )


_YOUTUBE_VERIFICATION_BLOCK_UNTIL = 0.0


def _is_youtube_verification_error(output: str | list[str]) -> bool:
    """Recognize raw yt-dlp text and the bot's already-normalized error message."""
    text = "\n".join(output) if isinstance(output, list) else str(output)
    lowered = text.lower()
    return (
        _is_youtube_bot_check(text)
        or "youtube refused this server's anonymous request" in lowered
        or "youtube has challenged this bot's server request with an anti-bot verification check" in lowered
    )


def _youtube_verification_cooldown_remaining() -> int:
    return max(0, int(_YOUTUBE_VERIFICATION_BLOCK_UNTIL - time.monotonic() + 0.99))


def _record_youtube_verification_challenge() -> None:
    """Pause repeated YouTube requests after an explicit source-side verification challenge."""
    global _YOUTUBE_VERIFICATION_BLOCK_UNTIL
    if YOUTUBE_VERIFICATION_COOLDOWN > 0:
        _YOUTUBE_VERIFICATION_BLOCK_UNTIL = max(
            _YOUTUBE_VERIFICATION_BLOCK_UNTIL,
            time.monotonic() + YOUTUBE_VERIFICATION_COOLDOWN,
        )
        log.warning(
            "YouTube anonymous verification challenge detected; pausing further YouTube requests for %s seconds",
            YOUTUBE_VERIFICATION_COOLDOWN,
        )


def _friendly_youtube_error(lines: list[str]) -> str:
    """Explain that the source is withholding anonymous media, rather than implying a code crash."""
    if _is_youtube_bot_check(lines):
        return (
            "YouTube has challenged this bot's server request with an anti-bot verification check. "
            "This is a source-access restriction, not a video-quality or Telegram error. The bot cannot "
            "force a download while YouTube withholds the stream, and changing resolution will not fix it. "
            "Please retry later; if it continues, this hosting IP may be temporarily rate-limited."
        )
    return _clean_ytdlp_error(lines)


def _clean_ytdlp_error(lines: list[str]) -> str:
    useful = [line.strip() for line in lines if line.strip()]
    text = useful[-1] if useful else "The source site did not provide a downloadable format."
    text = re.sub(r"https?://\S+", "[URL]", text)
    return text[:240]

# --------------------------- yt-dlp extraction ---------------------------
def _is_instagram_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "instagram.com" or host.endswith(".instagram.com")


def _instagram_url_candidates(url: str) -> list[str]:
    """Try the supplied public Instagram URL, then a canonical tracking-free URL.

    This is a conservative URL normalization retry, not an authentication bypass.
    Private/login-gated content remains inaccessible without legitimate access.
    """
    candidates = [url]
    parsed = urlparse(url)
    match = re.search(r"/(reel|reels|p|tv)/([A-Za-z0-9_-]+)", parsed.path, re.IGNORECASE)
    if match:
        kind, shortcode = match.group(1).lower(), match.group(2)
        # 'reels' is commonly shared as 'reel'; keep canonical public route simple.
        if kind == "reels":
            kind = "reel"
        candidates.append(f"https://www.instagram.com/{kind}/{shortcode}/")
    return list(dict.fromkeys(candidates))


class _InstagramPublicPageParser(HTMLParser):
    """Collect video URLs and titles explicitly exposed in public HTML metadata."""
    _VIDEO_KEYS = {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}
    _TITLE_KEYS = {"og:title", "twitter:title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.video_urls: list[str] = []
        self.titles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(k).lower(): (v or "") for k, v in attrs if k}
        if tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if content and key in self._VIDEO_KEYS:
                self.video_urls.append(content)
            elif content and key in self._TITLE_KEYS:
                self.titles.append(content)
        # Some public pages expose an actual media source element. Only accept it
        # if it points to an allowed CDN below; blob: and relative URLs are ignored.
        elif tag.lower() in {"video", "source"}:
            src = values.get("src", "").strip()
            media_type = values.get("type", "").lower()
            if src and (tag.lower() == "video" or media_type.startswith("video/")):
                self.video_urls.append(src)


def _instagram_allowed_media_host(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        return False
    return (
        host == "cdninstagram.com" or host.endswith(".cdninstagram.com")
        or host == "fbcdn.net" or host.endswith(".fbcdn.net")
    )


def _download_instagram_public_video_fallback(
    url: str, workdir: str, progress: dict[str, Any] | None = None,
) -> tuple[str | None, str]:
    """Try direct video URLs explicitly exposed by a public Instagram HTML page.

    No cookies, private APIs, login-wall workarounds, or third-party mirror sites are
    used. This fallback only succeeds if the public page exposes a direct media URL.
    """
    if not _is_instagram_url(url):
        return None, "Not an Instagram URL"
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None, "curl-cffi is not installed; public-page fallback is unavailable"

    if progress is not None:
        progress.update(stage="Checking public Instagram page", pct="", spd="", done=0, total=0)
    errors: list[str] = []
    candidates = _instagram_url_candidates(url)
    os.makedirs(workdir, exist_ok=True)

    try:
        with curl_requests.Session(impersonate="chrome") as session:
            session.headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            })
            for page_url in candidates[:2]:
                try:
                    page_response = session.get(page_url, timeout=18, allow_redirects=False)
                    try:
                        if int(getattr(page_response, "status_code", 0)) != 200:
                            errors.append(f"public page HTTP {getattr(page_response, 'status_code', 'unknown')}")
                            continue
                        page_html = str(getattr(page_response, "text", "") or "")[:8 * 1024 * 1024]
                    finally:
                        close = getattr(page_response, "close", None)
                        if callable(close):
                            close()
                except Exception as exc:
                    errors.append(f"public page request failed ({type(exc).__name__})")
                    continue

                parser = _InstagramPublicPageParser()
                try:
                    parser.feed(page_html)
                except Exception:
                    pass
                media_urls = list(parser.video_urls)
                # A small fallback for URL values embedded in public JSON/HTML. This
                # does not call internal APIs; it only reads the public page response.
                for match in re.findall(r"[\"'](?:video_url|contentUrl)[\"']\s*:\s*[\"']([^\"']{8,4096})[\"']", page_html, flags=re.IGNORECASE):
                    media_urls.append(match)
                normalized: list[str] = []
                for raw in media_urls:
                    value = html.unescape(str(raw)).replace(r"\/", "/").replace(r"\u0026", "&")
                    value = value.replace(r"\u003D", "=").replace(r"\u003d", "=").strip()
                    if value.startswith("//"):
                        value = "https:" + value
                    if _instagram_allowed_media_host(value) and value not in normalized:
                        normalized.append(value)
                if not normalized:
                    errors.append("public HTML did not expose a direct video URL")
                    continue

                # Only a few metadata URLs are attempted to avoid hammering the CDN.
                for media_url in normalized[:3]:
                    part_path = os.path.join(workdir, f"instagram-public-{uuid.uuid4().hex[:10]}.part")
                    try:
                        media_response = session.get(
                            media_url, timeout=30, stream=True, allow_redirects=False,
                            headers={"Referer": page_url},
                        )
                        try:
                            status = int(getattr(media_response, "status_code", 0))
                            final_url = str(getattr(media_response, "url", media_url) or media_url)
                            if status != 200 or not _instagram_allowed_media_host(final_url):
                                errors.append(f"direct media request rejected (HTTP {status})")
                                continue
                            headers = getattr(media_response, "headers", {}) or {}
                            content_type = str(headers.get("content-type", "")).split(";", 1)[0].lower().strip()
                            length_text = str(headers.get("content-length", "")).strip()
                            if length_text.isdigit() and int(length_text) > MAX_SOURCE_BYTES:
                                errors.append("direct media exceeds configured source size limit")
                                continue
                            if content_type and not (content_type.startswith("video/") or content_type == "application/octet-stream"):
                                errors.append("direct URL did not return video media")
                                continue
                            total = 0
                            with open(part_path, "wb") as stream:
                                iterator = getattr(media_response, "iter_content", None)
                                if not callable(iterator):
                                    raise RuntimeError("streaming is not supported by HTTP client")
                                for chunk in iterator(chunk_size=1024 * 1024):
                                    if not chunk:
                                        continue
                                    total += len(chunk)
                                    if total > MAX_SOURCE_BYTES:
                                        raise RuntimeError("direct media exceeds configured source size limit")
                                    stream.write(chunk)
                            if total < 1024:
                                raise RuntimeError("direct video response was empty or too small")
                            with open(part_path, "rb") as stream:
                                signature = stream.read(16)
                            if b"ftyp" not in signature and not content_type.startswith("video/"):
                                raise RuntimeError("response did not look like a video file")

                            title = next((t for t in parser.titles if t.strip()), "Instagram Reel")
                            title = html.unescape(title).strip()
                            title = re.sub(r"\s*[|•]\s*Instagram(?:\s+photos\s+and\s+videos)?\s*$", "", title, flags=re.IGNORECASE).strip()
                            title = _safe_filename_title(title or "Instagram Reel")
                            shortcode_match = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", urlparse(page_url).path, re.IGNORECASE)
                            shortcode = shortcode_match.group(1) if shortcode_match else uuid.uuid4().hex[:8]
                            final_path = os.path.join(workdir, f"{title[:90]} [{shortcode}]_001.mp4")
                            if os.path.exists(final_path):
                                final_path = os.path.join(workdir, f"{title[:80]} [{shortcode}]_{uuid.uuid4().hex[:6]}.mp4")
                            os.replace(part_path, final_path)
                            log.info("Instagram public HTML fallback downloaded one video using exposed metadata")
                            return final_path, ""
                        finally:
                            close = getattr(media_response, "close", None)
                            if callable(close):
                                close()
                    except Exception as exc:
                        try:
                            os.remove(part_path)
                        except OSError:
                            pass
                        errors.append(f"direct video fetch failed ({type(exc).__name__})")
                        continue
                    finally:
                        try:
                            if os.path.exists(part_path):
                                os.remove(part_path)
                        except OSError:
                            pass
    except Exception as exc:
        errors.append(f"public-page fallback failed ({type(exc).__name__})")

    return None, (errors[-1] if errors else "Instagram did not expose a public direct video URL")


def _media_template(workdir: str) -> str:
    # A bounded title is part of the actual saved filename; ID + sequence prevent collisions.
    return os.path.join(workdir, "%(title).80B [%(id)s]_%(autonumber)03d.%(ext)s")


def _title_from_path(path: str) -> str:
    """Recover the descriptive title from our output template when metadata tags are absent."""
    stem = Path(path).stem
    match = re.match(r"^(.*?)\s+\[[^\]]+\]_\d{3}$", stem)
    if match and match.group(1).strip():
        return match.group(1).strip()[:200]
    legacy = re.sub(r"^media_\d+_", "", stem)
    return (legacy or stem or "Downloaded media")[:200]




def _safe_filename_title(title: str) -> str:
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", title)
    title = re.sub(r"\s+", " ", title).strip(" ._-_")
    return (title or "Downloaded media")[:120]


def _rename_media_file(path: str, filename_stem: str) -> str:
    """Give normalized/remuxed outputs a stable title-based filename."""
    source = Path(path)
    target = source.with_name(f"{filename_stem}{source.suffix.lower()}")
    if source.resolve() == target.resolve():
        return str(source)
    if target.exists():
        target = source.with_name(f"{filename_stem}_{uuid.uuid4().hex[:6]}{source.suffix.lower()}")
    os.replace(source, target)
    return str(target)


def _is_instagram_post_url(url: str) -> bool:
    """True for /p/ links, which can contain image-only or mixed carousels."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (host == "instagram.com" or host.endswith(".instagram.com")) and bool(
        re.search(r"^/p/[A-Za-z0-9_-]+/?$", parsed.path, re.IGNORECASE)
    )


def _collect_media_files(workdir: str, printed_paths: list[str] | None = None) -> list[str]:
    """Collect completed media outputs only; ignore partials and sidecar files."""
    found: set[str] = set()
    for line in printed_paths or []:
        candidate = Path(line.strip().strip('"'))
        try:
            if candidate.is_file() and candidate.stat().st_size > 0 and candidate.suffix.lower() in MEDIA_EXTS and not candidate.name.endswith((".part", ".ytdl")):
                found.add(str(candidate.resolve()))
        except OSError:
            continue
    for path in Path(workdir).rglob("*"):
        try:
            if path.is_file() and path.stat().st_size > 0 and path.suffix.lower() in MEDIA_EXTS and not path.name.endswith((".part", ".ytdl")):
                found.add(str(path.resolve()))
        except OSError:
            continue
    return sorted(found)


def _download_instagram_thumbnail_fallback(
    url: str, workdir: str, progress: dict[str, Any] | None = None,
) -> tuple[list[str], str]:
    """Best-effort public thumbnail/image fallback for image-only /p/ posts.

    yt-dlp is video-focused, but its maintainer recommends --write-thumbnail with
    --ignore-no-formats-error for Instagram posts whose entries are photos. This
    runs only when the main extractor produced no media, so video thumbnails from
    a mixed carousel are never accidentally sent as if they were original photos.
    It uses no cookies and does not bypass an Instagram login wall.
    """
    thumb_dir = os.path.join(workdir, "ytdlp_thumbnail_fallback")
    shutil.rmtree(thumb_dir, ignore_errors=True)
    os.makedirs(thumb_dir, exist_ok=True)
    failures: list[str] = []
    for candidate in _instagram_url_candidates(url):
        command = [
            "yt-dlp", "--ignore-config", "--no-warnings", "--no-progress",
            "--ignore-no-formats-error", "--write-thumbnail", "--skip-download",
            "--yes-playlist", "--playlist-end", str(MAX_PLAYLIST),
            "--socket-timeout", "12", "--retries", "1", "--extractor-retries", "1",
            "--impersonate", os.environ.get("INSTAGRAM_IMPERSONATE", "chrome"),
            "-o", os.path.join(thumb_dir, "%(autonumber)03d_%(id)s.%(ext)s"),
            candidate,
        ]
        if progress is not None:
            progress.update(stage="Trying public Instagram thumbnail fallback", pct="", spd="", done=0, total=0)
        try:
            return_code, tail = _run_streaming_command(command, min(DOWNLOAD_TIMEOUT, 60), None)
        except FileNotFoundError:
            return [], "yt-dlp is not installed"
        except (TimeoutError, OSError, subprocess.SubprocessError) as exc:
            failures.append(type(exc).__name__)
            continue
        images = [
            path for path in _collect_media_files(thumb_dir, tail)
            if Path(path).suffix.lower() in IMAGE_EXTS and os.path.getsize(path) <= MAX_SOURCE_BYTES
        ]
        if images:
            log.info("Instagram public thumbnail fallback retrieved %d image(s)", len(images))
            return images, ""
        failures.append(_clean_ytdlp_error(tail) if tail else f"yt-dlp exited {return_code} without thumbnails")
    shutil.rmtree(thumb_dir, ignore_errors=True)
    return [], (failures[-1] if failures else "No public thumbnails were returned")


def _download_instagram_images(
    url: str,
    workdir: str,
    progress: dict[str, Any] | None = None,
    *,
    use_thumbnail_fallback: bool = False,
) -> tuple[list[str], str]:
    """Best-effort anonymous photo/carousel fallback without cookies.

    gallery-dl is tried first. For image-only posts with no video/media output,
    yt-dlp's write-thumbnail fallback is tried as a second public-access path.
    Both are bounded, and neither bypasses a login wall. If Instagram blocks the
    server or exposes no image URL, downloading without authenticated access cannot
    be guaranteed.
    """
    if not _is_instagram_post_url(url):
        return [], "Not an Instagram /p/ post"
    gallery_dir = os.path.join(workdir, "gallerydl_images")
    shutil.rmtree(gallery_dir, ignore_errors=True)
    os.makedirs(gallery_dir, exist_ok=True)
    command = [
        "gallery-dl", "--no-input", "--no-colors", "--quiet",
        "--directory", gallery_dir,
        "--filename", "{post_shortcode}_{num:>03}.{extension}",
        "--range", f"1-{MAX_PLAYLIST}",
        "--filter", "extension in ('jpg', 'jpeg', 'png', 'webp', 'avif')",
        url,
    ]
    if progress is not None:
        progress.update(stage="Fetching Instagram carousel photos", pct="", spd="", done=0, total=0)
    try:
        return_code, tail = _run_streaming_command(command, min(DOWNLOAD_TIMEOUT, 75), None)
    except FileNotFoundError:
        return_code, tail = 127, ["gallery-dl is not installed"]
    except (TimeoutError, OSError, subprocess.SubprocessError) as exc:
        return_code, tail = 1, [f"gallery-dl failed ({type(exc).__name__})"]

    images = [path for path in _collect_media_files(gallery_dir, tail)
              if Path(path).suffix.lower() in IMAGE_EXTS]
    oversized = [path for path in images if os.path.getsize(path) > MAX_SOURCE_BYTES]
    if oversized:
        return [], f"Instagram image exceeds the configured {MAX_SOURCE_BYTES // 1048576} MiB source limit"
    if images:
        log.info("Instagram anonymous image fallback retrieved %d photo(s)", len(images))
        return images, ""

    gallery_detail = _clean_ytdlp_error(tail) if tail else f"gallery-dl exited {return_code} without images"
    if use_thumbnail_fallback:
        thumb_images, thumb_error = _download_instagram_thumbnail_fallback(url, workdir, progress)
        if thumb_images:
            return thumb_images, ""
        return [], f"gallery-dl: {gallery_detail}; public thumbnail fallback: {thumb_error}"
    return [], gallery_detail


def _download_media(
    url: str,
    workdir: str,
    fmt: str,
    *,
    mp3: bool = False,
    subtitles: bool = False,
    progress: dict[str, Any] | None = None,
) -> list[str]:
    os.makedirs(workdir, exist_ok=True)
    is_instagram = _is_instagram_url(url)
    instagram_post = _is_instagram_post_url(url)
    command_base = [
        "yt-dlp", "--ignore-config", "--no-warnings", "--newline",
        "--retries", "2", "--fragment-retries", "2", "--extractor-retries", "1",
        "--file-access-retries", "2", "--socket-timeout", "15",
        "--concurrent-fragments", "4",
        "--remote-components", "ejs:github",
        "--print", "after_move:filepath",
        "-o", _media_template(workdir),
    ]
    # Instagram posts can mix video slides and photos. yt-dlp can save the video
    # slides while ignoring photo slides that have no video format.
    if is_instagram:
        command_base += ["--impersonate", os.environ.get("INSTAGRAM_IMPERSONATE", "chrome"), "--ignore-errors"]
    elif _is_youtube_url(url):
        # Optional YouTube session data is never sent to unrelated extractor hosts.
        command_base += _cookie_args(workdir)
    if not is_instagram:
        command_base.append("--no-playlist")
    else:
        command_base += ["--yes-playlist", "--playlist-end", str(MAX_PLAYLIST)]
    if progress is not None:
        command_base += [
            "--progress-template",
            "download:PROGRESS|%(progress._percent_str)s|%(progress._speed_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s",
        ]
    if subtitles:
        command_base += ["--write-subs", "--write-auto-subs", "--sub-langs", "en,hi",
                         "--convert-subs", "srt", "--skip-download"]
    else:
        # Embeds title/uploader metadata where the source container supports it.
        command_base += ["--embed-metadata"]
    if mp3:
        command_base += [
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--parse-metadata", "%(uploader|)s:%(meta_artist)s",
        ]
    elif fmt and fmt != "best":
        command_base += ["-f", fmt]
    if not mp3 and not subtitles and not is_instagram:
        command_base += ["--merge-output-format", "mp4"]

    candidates = _instagram_url_candidates(url) if is_instagram else [url]
    return_code = 1
    tail: list[str] = []
    files: list[str] = []
    for candidate_index, candidate in enumerate(candidates):
        if candidate_index:
            # Remove partial downloads from the previous URL variant before retrying.
            for path in Path(workdir).rglob("*"):
                try:
                    if path.is_file() and (path.name.startswith("media_") or path.name.endswith((".part", ".ytdl"))):
                        path.unlink()
                except OSError:
                    pass
        command = command_base + [candidate]
        if progress is not None:
            progress["stage"] = "Downloading"
        timeout = min(DOWNLOAD_TIMEOUT, 150) if is_instagram else (DOWNLOAD_TIMEOUT if not subtitles else 240)
        try:
            return_code, tail = _run_streaming_command(command, timeout, progress)
        except TimeoutError as exc:
            if not is_instagram or candidate_index == len(candidates) - 1:
                raise
            return_code, tail = 124, [str(exc)]
        files = _collect_media_files(workdir, tail) if not subtitles else [
            str(path.resolve()) for path in Path(workdir).rglob("*")
            if path.is_file() and path.suffix.lower() in {".srt", ".vtt", ".ass", ".ssa"}
        ]
        # For Instagram, a playlist may return non-zero for photo-only slides but
        # still have produced valid video files. Keep those files instead of losing
        # the whole post; gallery-dl below handles its image slides separately.
        if files and (return_code == 0 or is_instagram):
            break
        if candidate_index < len(candidates) - 1:
            continue
        if return_code != 0:
            break

    if not subtitles and is_instagram and not files:
        # Extra cookie-free path: some public pages expose an OG/Twitter video URL
        # even when the extractor's media JSON is empty. This is public metadata only;
        # no login walls or authenticated endpoints are bypassed.
        public_file, public_error = _download_instagram_public_video_fallback(url, workdir, progress)
        if public_file:
            files = [public_file]
            return_code = 0
            tail = ["Instagram public-page metadata fallback succeeded"]
        else:
            log.info("Instagram public-page media fallback unavailable (%s)", public_error[:180])

    if not subtitles and is_instagram:
        had_no_video_formats = any("no video formats found" in line.lower() for line in tail)
        # Add photo/carousel slides when yt-dlp reports entries with no video format.
        # For image-only posts, this is also the primary fallback after yt-dlp yields no files.
        if instagram_post and (not files or had_no_video_formats):
            photo_files, photo_error = _download_instagram_images(
                url, workdir, progress, use_thumbnail_fallback=not bool(files)
            )
            if photo_files:
                files = sorted(set(files + photo_files))
            elif not files:
                summary = (
                    "Instagram returned no video formats. This often means the /p/ post contains photos, "
                    "not video streams. The bot tried its no-cookie image fallback, but Instagram did not "
                    "expose downloadable public media. If Instagram requires sign-in or rate-limits the server, "
                    "a no-cookie download cannot be guaranteed."
                )
                log.warning("Instagram photo fallback did not return media (%s)", photo_error[:180])
                raise RuntimeError(summary)
            else:
                # Some carousel video slides were saved but its photo slides were not accessible anonymously.
                if progress is not None:
                    progress["instagram_gallery_warning"] = "Some photo slides were not available anonymously."
                log.warning("Instagram partial carousel: photo fallback unavailable (%s)", photo_error[:180])

    if not files:
        if return_code != 0 and not is_instagram:
            raise RuntimeError(_friendly_youtube_error(tail) if _is_youtube_url(url) else _clean_ytdlp_error(tail))
        tail_message = _clean_ytdlp_error(tail)
        if is_instagram and not subtitles:
            raise RuntimeError(
                "Instagram did not expose a public downloadable video URL to this server. "
                "The bot tried yt-dlp with Chrome impersonation and a public-page metadata fallback; "
                "no Instagram cookies were used. The page may be login-gated, rate-limited, or may not "
                "publish a direct video URL to anonymous requests. If Instagram withholds the media, "
                "a cookie-free download cannot be guaranteed."
            )
        raise RuntimeError(tail_message or "No media files were produced.")
    for path in files:
        if os.path.getsize(path) > MAX_SOURCE_BYTES:
            raise RuntimeError(
                f"The source file exceeds the configured {MAX_SOURCE_BYTES // 1048576} MiB safety limit."
            )
    return files

def _download_youtube_thumbnail(url: str, root_dir: str) -> str | None:
    """Fetch an image from a YouTube thumbnail CDN and normalize it to a small JPEG.

    The host allow-list prevents using extractor-supplied thumbnail URLs as an arbitrary
    server-side fetch target. The caller removes the returned temporary file after sending.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = host in {"i.ytimg.com", "img.youtube.com"} or host.endswith(".ytimg.com")
    if parsed.scheme != "https" or not allowed:
        return None
    os.makedirs(root_dir, exist_ok=True)
    nonce = uuid.uuid4().hex
    raw_path = os.path.join(root_dir, f"yt-thumb-{nonce}.source")
    jpg_path = os.path.join(root_dir, f"yt-thumb-{nonce}.jpg")
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=12) as response:
            content_type = response.headers.get_content_type().lower()
            if not content_type.startswith("image/"):
                return None
            data = response.read(2 * 1024 * 1024 + 1)
        if not data or len(data) > 2 * 1024 * 1024:
            return None
        with open(raw_path, "wb") as stream:
            stream.write(data)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", raw_path,
            "-frames:v", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
            "-q:v", "4", "-update", "1", jpg_path,
        ]
        if _run_ffmpeg(command, timeout=30) and os.path.isfile(jpg_path) and 0 < os.path.getsize(jpg_path) <= 200 * 1024:
            return jpg_path
        return None
    except (OSError, TimeoutError, subprocess.SubprocessError) as exc:
        log.info("Could not fetch YouTube thumbnail (%s)", type(exc).__name__)
        return None
    finally:
        try:
            os.remove(raw_path)
        except OSError:
            pass


def _get_video_meta(url: str) -> dict[str, Any] | None:
    with tempfile.TemporaryDirectory(prefix="dlmeta_", dir=WORK_DIR) as td:
        command = ["yt-dlp", "--ignore-config", "--dump-single-json", "--no-playlist", "--skip-download",
                   "--no-warnings", "--remote-components", "ejs:github"]
        if _is_youtube_url(url):
            command += _cookie_args(td)
        command.append(url)
        try:
            result = _run_capture(command, 90)
            if result.returncode != 0:
                return None
            info = json.loads(result.stdout)
            vids: dict[int, tuple[int, str]] = {}
            for fmt in info.get("formats", []):
                height = fmt.get("height")
                vcodec = fmt.get("vcodec")
                if not height or height < 144 or height > MAX_VIDEO_HEIGHT or not vcodec or vcodec == "none":
                    continue
                size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
                current = vids.get(int(height))
                if current is None or size > current[0]:
                    vids[int(height)] = (size, str(fmt.get("vcodec", "")))
            return {
                "title": str(info.get("title") or "Video")[:200],
                "duration": int(info.get("duration") or 0),
                "thumbnail": info.get("thumbnail"),
                "heights": sorted(vids.keys(), reverse=True),
                "sizes": {str(k): v[0] for k, v in vids.items()},
            }
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as exc:
            log.info("Could not read format metadata: %s", type(exc).__name__)
            return None


def _quality_selector(height: int) -> str:
    """Prefer Telegram-friendly H.264/AAC before a potentially expensive transcode.

    The requested height is a ceiling: if 1080p is only available as VP9/AV1,
    prefer an available lower-resolution AVC stream over forcing a slow transcode.
    """
    h = max(144, min(int(height), MAX_VIDEO_HEIGHT))
    return (
        f"bv*[height={h}][ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a]/"
        f"b[height={h}][ext=mp4][vcodec^=avc1]/"
        f"bv*[height<={h}][ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a]/"
        f"b[height<={h}][ext=mp4][vcodec^=avc1]/"
        f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
        f"b[height<={h}][ext=mp4]/"
        f"bv*[height<={h}]+ba/b[height<={h}]"
    )

def _format_chain(
    user_id: int,
    force_fmt: str | None = None,
    preferred_override: int | None | object = _QUALITY_PREF_UNSET,
) -> list[str]:
    """Build explicit format fallbacks for a one-time choice or saved user default.

    None means best available capped at 1080p. An integer means a maximum
    resolution; lower choices are only attempted if the chosen format cannot be used.
    """
    if force_fmt:
        if force_fmt == "best":
            # Best available is intentionally capped at 1080p for predictable size and bandwidth.
            return [f"bestvideo[height<={MAX_VIDEO_HEIGHT}]+bestaudio/best[height<={MAX_VIDEO_HEIGHT}]", *(_quality_selector(h) for h in QUALITY_LEVELS)]
        match = re.search(r"height[<]?=?([0-9]+)", force_fmt)
        max_height = int(match.group(1)) if match else 1080
        heights = [h for h in QUALITY_LEVELS if h < max_height]
        return list(dict.fromkeys([force_fmt] + [_quality_selector(h) for h in heights]))

    preferred = _get_quality_preference(user_id) if preferred_override is _QUALITY_PREF_UNSET else preferred_override
    if preferred is None:
        # Best available is capped at 1080p; later selectors provide compatible fallbacks.
        return [f"bestvideo[height<={MAX_VIDEO_HEIGHT}]+bestaudio/best[height<={MAX_VIDEO_HEIGHT}]", *(_quality_selector(h) for h in QUALITY_LEVELS)]
    max_height = _normalize_quality_preference(preferred)
    if max_height is None:
        return [f"bestvideo[height<={MAX_VIDEO_HEIGHT}]+bestaudio/best[height<={MAX_VIDEO_HEIGHT}]", *(_quality_selector(h) for h in QUALITY_LEVELS)]
    heights = [h for h in QUALITY_LEVELS if h <= max_height]
    selectors = [_quality_selector(h) for h in heights]
    selectors.append(f"best[height<={max_height}]")
    return list(dict.fromkeys(selectors))

# --------------------------- FFprobe / splitting ---------------------------
def _probe_media(path: str) -> dict[str, Any]:
    try:
        result = _run_capture([
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height:format=duration,format_name:format_tags=title,artist,album,performer,album_artist,uploader",
            "-of", "json", path,
        ], 30)
        if result.returncode != 0:
            return {}
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
        fmt = payload.get("format", {})
        duration = float(fmt.get("duration") or 0)
        raw_tags = fmt.get("tags") or {}
        tags = {str(key).lower(): str(value) for key, value in raw_tags.items() if value is not None}
        artist = tags.get("artist") or tags.get("performer") or tags.get("album_artist") or tags.get("uploader") or ""
        title = tags.get("title") or ""
        return {
            "duration": duration,
            "has_video": bool(video),
            "has_audio": bool(audio),
            "video_codec": str(video.get("codec_name") or ""),
            "audio_codec": str(audio.get("codec_name") or ""),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "format_name": str(fmt.get("format_name") or ""),
            "title": title,
            "artist": artist,
            "tags": tags,
        }
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return {}


def _run_ffmpeg(command: list[str], timeout: int = FFMPEG_TIMEOUT) -> bool:
    try:
        result = _run_capture(command, timeout)
        if result.returncode != 0:
            log.warning("FFmpeg failed: %s", (result.stderr or result.stdout or "unknown error")[-300:])
            return False
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("FFmpeg could not finish: %s", type(exc).__name__)
        return False


def _run_ffmpeg_with_progress(
    command: list[str], *, timeout: int, progress: dict[str, Any] | None, duration: float
) -> bool:
    """Run FFmpeg with machine-readable progress so conversion doesn't look hung."""
    if not command or len(command) < 2:
        return False
    # `output` is the final argument for the generated command; insert progress
    # output options immediately before it, where FFmpeg's output options apply.
    monitored = command[:-1] + ["-progress", "pipe:1", "-nostats", command[-1]]
    try:
        process = subprocess.Popen(
            monitored, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace", bufsize=1, env=_base_env(),
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        log.warning("FFmpeg could not start (%s)", type(exc).__name__)
        return False

    tail: deque[str] = deque(maxlen=40)
    def reader() -> None:
        assert process.stdout is not None
        current: dict[str, str] = {}
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            tail.append(line)
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            current[key] = value
            if key in {"out_time_us", "out_time_ms"} and progress is not None and duration > 0:
                try:
                    seconds = int(value) / 1_000_000
                    pct = max(0.0, min(100.0, seconds * 100.0 / duration))
                    progress["pct"] = f"{pct:.1f}%"
                    # Conversion speed is reported separately below, and no fake MiB
                    # counters are shown for FFmpeg's time-based progress.
                    progress.pop("done", None)
                    progress.pop("total", None)
                except (ValueError, ZeroDivisionError):
                    pass
            elif key == "speed" and progress is not None:
                progress["spd"] = value
            elif key == "progress":
                current.clear()

    thread = threading.Thread(target=reader, daemon=True, name="ffmpeg-progress")
    thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            _stop_process(process)
            break
        time.sleep(0.25)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _stop_process(process)
    thread.join(timeout=5)
    if timed_out:
        raise TimeoutError(f"FFmpeg conversion exceeded its {timeout}-second time limit")
    if process.returncode != 0:
        log.warning("FFmpeg conversion failed: %s", " | ".join(tail)[-300:])
        return False
    if progress is not None:
        progress["pct"] = "100%"
        progress["spd"] = ""
    return True

def _normalize_video_for_telegram(
    path: str, root_dir: str, source_index: int, progress: dict[str, Any] | None = None
) -> str:
    """Prefer fast H.264/AAC MP4 remux; bound transcoding and never discard a valid source.

    Full video transcoding is the last resort. If it cannot complete within the short
    Render-friendly deadline, return a source file that fits Telegram's upload cap:
    MP4 is still sent as a video; other containers are delivered as documents rather
    than making the user wait through repeated 4-minute transcodes with no result.
    """
    info = _probe_media(path)
    ext = Path(path).suffix.lower()
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    has_video = bool(info.get("has_video"))
    if not has_video and ext not in VIDEO_EXTS:
        return path

    os.makedirs(root_dir, exist_ok=True)
    output = os.path.join(root_dir, f"streamable_{source_index:03d}.mp4")
    try:
        os.remove(output)
    except OSError:
        pass

    video_codec = str(info.get("video_codec") or "").lower()
    audio_codec = str(info.get("audio_codec") or "").lower()
    duration = float(info.get("duration") or 0)
    if progress is not None:
        progress.update(stage="Preparing video", pct="0%", spd="", done=0, total=0)

    def original_fallback(reason: str) -> str:
        if size > 0:
            log.warning("Using original media fallback for %s (%s)", Path(path).name, reason)
            if progress is not None:
                progress.update(stage="Using original file", pct="100%", spd="")
            # Oversized sources continue into the normal splitting pipeline below.
            return path
        raise RuntimeError(f"Video conversion failed ({reason}) and the source file is empty or missing.")

    # If the probe fails on an MP4, do not burn CPU attempting a blind full transcode.
    # Send the file as a video and let Telegram/client playback determine compatibility.
    if not info:
        if ext == ".mp4":
            return original_fallback("media metadata could not be read")
        log.warning("FFprobe metadata unavailable for %s; attempting bounded conversion", Path(path).name)

    # AVC/H.264 + AAC can be remuxed in seconds without decoding video frames.
    # Even an MP4 source goes through this stream-copy step so +faststart is set for
    # Telegram streaming; this is not a re-encode and should be much faster than decode/encode.
    if has_video and video_codec == "h264" and audio_codec in {"", "aac"}:
        remux = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-c", "copy",
            "-movflags", "+faststart", "-map_metadata", "0", output,
        ]
        if _run_ffmpeg(remux, timeout=min(60, FFMPEG_TIMEOUT)) and os.path.isfile(output) and os.path.getsize(output) > 0:
            if progress is not None:
                progress.update(stage="Preparing video", pct="100%", spd="")
            return output
        try:
            os.remove(output)
        except OSError:
            pass
        # A valid MP4 source is safer than following a failed stream-copy with a 4-minute transcode.
        if ext == ".mp4":
            return original_fallback("fast remux failed")

    # Preserve H.264 video bit-for-bit when only the audio codec needs conversion.
    if has_video and video_codec == "h264" and audio_codec not in {"", "aac"}:
        audio_fix = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", "-map_metadata", "0", output,
        ]
        if _run_ffmpeg(audio_fix, timeout=min(90, FFMPEG_TIMEOUT)) and os.path.isfile(output) and os.path.getsize(output) > 0:
            final_info = _probe_media(output)
            if final_info.get("has_video") and final_info.get("video_codec") == "h264":
                return output
        try:
            os.remove(output)
        except OSError:
            pass
        if ext == ".mp4":
            return original_fallback("audio conversion failed")

    # True last resort: transcode to a maximum 720p H.264/AAC MP4. This is far faster
    # on a free-tier CPU than trying to preserve 1080p VP9/AV1. Already-compatible
    # 1080p sources bypass this branch entirely.
    max_h = VIDEO_TRANSCODE_MAX_HEIGHT
    max_w = int(round(max_h * 16 / 9))
    transcode = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", f"scale=w='min({max_w},iw)':h='min({max_h},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-pix_fmt", "yuv420p",
        "-threads", "2", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        "-map_metadata", "0", output,
    ]
    try:
        ok = _run_ffmpeg_with_progress(
            transcode, timeout=VIDEO_TRANSCODE_TIMEOUT, progress=progress, duration=duration
        )
    except TimeoutError as exc:
        log.warning("Video transcode deadline reached for %s", Path(path).name)
        try:
            os.remove(output)
        except OSError:
            pass
        return original_fallback(str(exc))

    if not ok or not os.path.isfile(output) or os.path.getsize(output) <= 0:
        try:
            os.remove(output)
        except OSError:
            pass
        return original_fallback("H.264/AAC transcode did not complete")

    final_info = _probe_media(output)
    if not final_info.get("has_video") or final_info.get("video_codec") != "h264":
        try:
            os.remove(output)
        except OSError:
            pass
        return original_fallback("transcode output was not valid H.264 video")
    if final_info.get("has_audio") and final_info.get("audio_codec") != "aac":
        try:
            os.remove(output)
        except OSError:
            pass
        return original_fallback("transcode output audio was not AAC")
    return output

def _create_video_thumbnail(path: str, root_dir: str, source_index: int, part_index: int) -> str | None:
    """Make a small JPEG preview Telegram can display before the uploaded video is opened."""
    output = os.path.join(root_dir, f"preview_{source_index:03d}_{part_index:03d}.jpg")
    info = _probe_media(path)
    duration = float(info.get("duration") or 0)
    seek = min(1.0, max(0.0, duration * 0.1))
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seek:.3f}",
        "-i", path, "-map", "0:v:0", "-frames:v", "1",
        "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
        "-q:v", "5", "-update", "1", output,
    ]
    if _run_ffmpeg(command, timeout=45) and os.path.isfile(output) and 0 < os.path.getsize(output) <= 200 * 1024:
        return output
    try:
        os.remove(output)
    except OSError:
        pass
    log.info("Could not create preview thumbnail for %s", Path(path).name)
    return None


def _copy_split_attempt(path: str, attempt_dir: str, segment_seconds: int, kind: str) -> list[str]:
    os.makedirs(attempt_dir, exist_ok=True)
    ext = Path(path).suffix.lower()
    if kind == "audio":
        audio_containers = {
            ".mp3": (".mp3", "mp3"), ".m4a": (".m4a", "ipod"),
            ".aac": (".aac", "adts"), ".opus": (".opus", "opus"),
            ".ogg": (".ogg", "ogg"), ".wav": (".wav", "wav"),
            ".flac": (".flac", "flac"), ".weba": (".webm", "webm"),
            ".webm": (".webm", "webm"),
        }
        if ext not in audio_containers:
            return []
        out_ext, container = audio_containers[ext]
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                   "-map", "0:a:0", "-vn", "-c:a", "copy", "-f", "segment",
                   "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
                   "-segment_format", container, os.path.join(attempt_dir, f"part_%03d{out_ext}")]
    else:
        # MP4 segments may be sent as Telegram videos; Matroska is a safe fallback for other codecs.
        prefer_mp4 = ext in {".mp4", ".m4v", ".mov"}
        out_ext, container = (".mp4", "mp4") if prefer_mp4 else (".mkv", "matroska")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                   "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-c", "copy",
                   "-f", "segment", "-segment_time", str(segment_seconds),
                   "-reset_timestamps", "1", "-segment_format", container,
                   *(["-segment_format_options", "movflags=+faststart"] if container == "mp4" else []),
                   os.path.join(attempt_dir, f"part_%03d{out_ext}")]
        if prefer_mp4 and not _run_ffmpeg(command):
            # Retrying with Matroska preserves codecs that the MP4 container cannot carry.
            shutil.rmtree(attempt_dir, ignore_errors=True)
            os.makedirs(attempt_dir, exist_ok=True)
            out_ext, container = ".mkv", "matroska"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                       "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-c", "copy",
                       "-f", "segment", "-segment_time", str(segment_seconds),
                       "-reset_timestamps", "1", "-segment_format", container,
                       os.path.join(attempt_dir, f"part_%03d{out_ext}")]
            if not _run_ffmpeg(command):
                return []
        elif not prefer_mp4 and not _run_ffmpeg(command):
            return []
        return sorted(str(Path(attempt_dir) / name) for name in os.listdir(attempt_dir)
                      if name.startswith("part_") and Path(name).suffix.lower() == out_ext)
    if not _run_ffmpeg(command):
        return []
    return sorted(str(Path(attempt_dir) / name) for name in os.listdir(attempt_dir)
                  if name.startswith("part_") and Path(name).suffix.lower() == out_ext)


def _reencode_split_attempt(path: str, attempt_dir: str, segment_seconds: int) -> list[str]:
    """Last-resort video split: re-encode with keyframes to make boundaries more predictable."""
    os.makedirs(attempt_dir, exist_ok=True)
    output_pattern = os.path.join(attempt_dir, "part_%03d.mp4")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "28", "-threads", "1", "-c:a", "aac", "-b:a", "96k",
        "-force_key_frames", f"expr:gte(t,n_forced*{segment_seconds})",
        "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
        "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart", output_pattern,
    ]
    if not _run_ffmpeg(command, timeout=FFMPEG_TIMEOUT):
        return []
    return sorted(str(Path(attempt_dir) / name) for name in os.listdir(attempt_dir)
                  if name.startswith("part_") and Path(name).suffix.lower() == ".mp4")


def _split_oversized(
    path: str,
    root_dir: str,
    kind: str,
    depth: int = 0,
) -> list[str]:
    """Return every part under the upload threshold or fail explicitly; never silently drop parts."""
    size = os.path.getsize(path)
    if size <= MAX_UPLOAD_BYTES:
        return [path]
    if depth >= MAX_SPLIT_DEPTH:
        raise RuntimeError("The file could not be split below Telegram's upload limit after several attempts.")
    if kind == "image":
        raise RuntimeError("This image is larger than the upload limit and cannot be split.")

    info = _probe_media(path)
    duration = float(info.get("duration") or 0)
    if duration <= 1:
        raise RuntimeError("FFprobe could not determine a usable duration for splitting this file.")

    last_error = "FFmpeg did not create smaller segments."
    for scale in (0.70, 0.52, 0.38, 0.27, 0.18):
        estimated = duration * (MAX_UPLOAD_BYTES / max(size, 1)) * scale
        seg_time = max(1, min(int(duration) - 1, int(estimated)))
        attempt_dir = tempfile.mkdtemp(prefix=f"split_{depth}_", dir=root_dir)
        parts = _copy_split_attempt(path, attempt_dir, seg_time, kind)
        # Retry with more conservative segments if stream-copy cut points cannot make progress.
        good_shape = len(parts) >= 2 and all(os.path.getsize(part) < size for part in parts)
        if good_shape:
            try:
                final: list[str] = []
                for part in parts:
                    final.extend(_split_oversized(part, attempt_dir, kind, depth + 1))
                    if len(final) > MAX_PARTS:
                        raise RuntimeError(f"Splitting would create more than {MAX_PARTS} parts.")
                if final and all(os.path.getsize(part) <= MAX_UPLOAD_BYTES for part in final):
                    if len(final) > MAX_PARTS:
                        raise RuntimeError(f"Splitting would create more than {MAX_PARTS} parts.")
                    return final
            except RuntimeError as exc:
                last_error = str(exc)
        else:
            last_error = "FFmpeg did not create at least two smaller segments."
        shutil.rmtree(attempt_dir, ignore_errors=True)

    if kind == "video":
        # One re-encode attempt is CPU-expensive; only use it after stream-copy splitting fails.
        attempt_dir = tempfile.mkdtemp(prefix=f"split_encode_{depth}_", dir=root_dir)
        seg_time = max(1, int(duration * (MAX_UPLOAD_BYTES / max(size, 1)) * 0.35))
        parts = _reencode_split_attempt(path, attempt_dir, seg_time)
        if len(parts) >= 2 and all(os.path.getsize(part) < size for part in parts):
            try:
                final = []
                for part in parts:
                    final.extend(_split_oversized(part, attempt_dir, kind, depth + 1))
                    if len(final) > MAX_PARTS:
                        raise RuntimeError(f"Splitting would create more than {MAX_PARTS} parts.")
                if final and all(os.path.getsize(part) <= MAX_UPLOAD_BYTES for part in final):
                    if len(final) > MAX_PARTS:
                        raise RuntimeError(f"Splitting would create more than {MAX_PARTS} parts.")
                    return final
            except RuntimeError as exc:
                last_error = str(exc)
        shutil.rmtree(attempt_dir, ignore_errors=True)
    raise RuntimeError(f"Could not safely split this file: {last_error}")


def _prepare_files(paths: list[str], root_dir: str, mp3: bool, progress: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    source_count = len(paths)
    for source_index, original_path in enumerate(paths, start=1):
        original_size = os.path.getsize(original_path)
        path = original_path
        ext = Path(path).suffix.lower()
        info = _probe_media(path)
        source_title = str(info.get("title") or _title_from_path(path))[:200]
        if ext in IMAGE_EXTS:
            kind = "image"
        elif mp3 or ext == ".mp3":
            kind = "audio"
        elif info.get("has_video") or (not info and ext in VIDEO_EXTS):
            kind = "video"
        elif info.get("has_audio") or ext in AUDIO_EXTS:
            kind = "audio"
        elif ext in VIDEO_EXTS:
            kind = "video"
        else:
            kind = "document"

        # Convert when fast remux is possible; if transcode is too expensive, the
        # normalizer keeps a deliverable original instead of silently losing the result.
        if kind == "video":
            path = _normalize_video_for_telegram(path, root_dir, source_index, progress=progress)
        output_info = _probe_media(path)
        source_title = str(output_info.get("title") or source_title or _title_from_path(original_path))[:200]
        safe_title = _safe_filename_title(source_title)
        path = _rename_media_file(path, f"{source_index:02d} - {safe_title}")
        output_ext = Path(path).suffix.lower()
        streamable_mp4 = (
            output_ext == ".mp4" and output_info.get("has_video")
            and output_info.get("video_codec") == "h264"
            and output_info.get("audio_codec") in {"", "aac"}
        )
        conversion_fallback = kind == "video" and not streamable_mp4
        if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
            file_parts = [path]
        elif kind in {"video", "audio"}:
            file_parts = _split_oversized(path, root_dir, kind)
            named_parts: list[str] = []
            for split_index, split_path in enumerate(file_parts, start=1):
                named_parts.append(_rename_media_file(
                    split_path, f"{source_index:02d} - {safe_title} - Part {split_index}-of-{len(file_parts)}"
                ))
            file_parts = named_parts
        else:
            raise RuntimeError(f"File {Path(path).name} exceeds Telegram's upload limit and cannot be split.")
        if len(prepared) + len(file_parts) > MAX_PARTS:
            raise RuntimeError(f"This download would create more than {MAX_PARTS} upload parts.")
        for part_index, part in enumerate(file_parts, start=1):
            if os.path.getsize(part) > MAX_UPLOAD_BYTES:
                raise RuntimeError("Internal size check failed: a part is still too large to upload.")
            pinfo = _probe_media(part)
            part_ext = Path(part).suffix.lower()
            is_video = bool(pinfo.get("has_video")) or (not pinfo and part_ext in VIDEO_EXTS)
            is_audio = not is_video and (bool(pinfo.get("has_audio")) or part_ext in AUDIO_EXTS)
            thumbnail = _create_video_thumbnail(part, root_dir, source_index, part_index) if is_video and part_ext == ".mp4" else None
            prepared.append({
                "path": part, "kind": kind, "video": is_video, "audio": is_audio,
                "width": pinfo.get("width"), "height": pinfo.get("height"),
                "duration": int(pinfo.get("duration") or 0),
                "title": str(output_info.get("title") or source_title or _title_from_path(original_path))[:200],
                "artist": str(pinfo.get("artist") or "")[:100],
                "thumbnail": thumbnail,
                "conversion_fallback": conversion_fallback,
                "source_index": source_index, "source_count": source_count,
                "part_index": part_index, "part_count": len(file_parts),
                "source_bytes": original_size,
            })
    return prepared

# --------------------------- Telegram sending ---------------------------
def _init_delete_db() -> bool:
    """Initialize optional durable auto-delete storage (Render Postgres via DATABASE_URL)."""
    global _DELETE_DB_READY, _DELETE_DB_NEXT_RETRY
    if not DATABASE_URL:
        return False
    with _DELETE_DB_LOCK:
        if _DELETE_DB_READY:
            return True
        if time.monotonic() < _DELETE_DB_NEXT_RETRY:
            return False
        try:
            import psycopg
            with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS bot_pending_auto_delete ("
                    "chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, "
                    "due_at DOUBLE PRECISION NOT NULL, "
                    "PRIMARY KEY (chat_id, message_id))"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS bot_user_quality_preferences ("
                    "user_id BIGINT PRIMARY KEY, max_height INTEGER NULL, "
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            _DELETE_DB_READY = True
            _DELETE_DB_NEXT_RETRY = 0.0
            log.info("Auto-delete persistent database enabled")
            return True
        except Exception as exc:
            _DELETE_DB_READY = False
            _DELETE_DB_NEXT_RETRY = time.monotonic() + 60.0
            log.warning("Auto-delete database unavailable (%s); retrying in about 60 seconds", type(exc).__name__)
            return False



def _mark_delete_db_unavailable() -> None:
    global _DELETE_DB_READY, _DELETE_DB_NEXT_RETRY
    with _DELETE_DB_LOCK:
        _DELETE_DB_READY = False
        _DELETE_DB_NEXT_RETRY = time.monotonic() + 60.0

def _upsert_delete_db(key: tuple[int, int], due_at: float) -> None:
    if not _DELETE_DB_READY:
        return
    try:
        import psycopg
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            conn.execute(
                "INSERT INTO bot_pending_auto_delete (chat_id, message_id, due_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (chat_id, message_id) "
                "DO UPDATE SET due_at = EXCLUDED.due_at",
                (key[0], key[1], float(due_at)),
            )
    except Exception as exc:
        _mark_delete_db_unavailable()
        log.warning("Could not save auto-delete to database chat=%s message_id=%s (%s)",
                    key[0], key[1], type(exc).__name__)


def _delete_from_db(key: tuple[int, int]) -> None:
    if not _DELETE_DB_READY:
        return
    try:
        import psycopg
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            conn.execute(
                "DELETE FROM bot_pending_auto_delete WHERE chat_id = %s AND message_id = %s",
                (key[0], key[1]),
            )
    except Exception as exc:
        _mark_delete_db_unavailable()
        log.warning("Could not clear auto-delete from database chat=%s message_id=%s (%s)",
                    key[0], key[1], type(exc).__name__)


def _read_delete_db() -> list[tuple[tuple[int, int], float]]:
    if not _DELETE_DB_READY:
        return []
    try:
        import psycopg
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            rows = conn.execute(
                "SELECT chat_id, message_id, due_at FROM bot_pending_auto_delete"
            ).fetchall()
        return [((int(chat_id), int(message_id)), float(due_at))
                for chat_id, message_id, due_at in rows]
    except Exception as exc:
        _mark_delete_db_unavailable()
        log.warning("Could not read auto-delete database state (%s)", type(exc).__name__)
        return []


def _write_delete_state_locked() -> None:
    """Atomically persist due times so timers can be restored after a process restart."""
    state_path = Path(AUTO_DELETE_STATE_FILE)
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    payload = [
        {"chat_id": chat_id, "message_id": message_id, "due_at": due_at}
        for (chat_id, message_id), due_at in _DELETE_DUE.items()
    ]
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
        os.replace(tmp_path, state_path)
    except OSError as exc:
        log.warning("Could not persist auto-delete state at %s (%s)", state_path, type(exc).__name__)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _remember_delete(key: tuple[int, int], due_at: float) -> None:
    with _DELETE_STATE_LOCK:
        _DELETE_DUE[key] = float(due_at)
        _write_delete_state_locked()
    _upsert_delete_db(key, due_at)


def _forget_delete(key: tuple[int, int]) -> None:
    with _DELETE_STATE_LOCK:
        _DELETE_DUE.pop(key, None)
        _write_delete_state_locked()
    _delete_from_db(key)


async def _delete_message_after_due(bot, chat_id: int, message_id: int, due_at: float,
                                    key: tuple[int, int]) -> None:
    """Delete by IDs, retry transient network errors, and log results at INFO/WARNING."""
    try:
        await asyncio.sleep(max(0.0, due_at - time.time()))
        while True:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
                log.info("Auto-delete succeeded chat=%s message_id=%s", chat_id, message_id)
                _forget_delete(key)
                return
            except BadRequest as exc:
                detail = str(exc).lower()
                # If it is already gone, cleanup succeeded from the user's perspective.
                if "message to delete not found" in detail or "message_id_invalid" in detail:
                    log.info("Auto-delete already complete chat=%s message_id=%s", chat_id, message_id)
                else:
                    log.warning("Telegram rejected auto-delete chat=%s message_id=%s reason=%s",
                                chat_id, message_id, str(exc)[:180])
                _forget_delete(key)
                return
            except RetryAfter as exc:
                wait = max(1.0, float(getattr(exc, "retry_after", 1)) + 1.0)
                log.warning("Auto-delete rate-limited chat=%s message_id=%s; retrying in %.1fs",
                            chat_id, message_id, wait)
                await asyncio.sleep(wait)
            except (NetworkError, TimedOut) as exc:
                log.warning("Auto-delete network error chat=%s message_id=%s (%s); retrying in 30s",
                            chat_id, message_id, type(exc).__name__)
                await asyncio.sleep(30)
            except TelegramError as exc:
                log.warning("Telegram auto-delete failed chat=%s message_id=%s (%s)",
                            chat_id, message_id, type(exc).__name__)
                _forget_delete(key)
                return
            except Exception as exc:
                log.exception("Unexpected auto-delete failure chat=%s message_id=%s (%s)",
                              chat_id, message_id, type(exc).__name__)
                await asyncio.sleep(60)
    finally:
        current = asyncio.current_task()
        # A timer may have been reset while the old task was being cancelled.
        # Never let the old task remove the newer task for this message.
        if _DELETE_TASKS.get(key) is current:
            _DELETE_TASKS.pop(key, None)


def _recover_pending_auto_deletes(app: Application) -> None:
    """Restore pending cleanups from Postgres, falling back to local state when configured."""
    _init_delete_db()
    state_path = Path(AUTO_DELETE_STATE_FILE)
    try:
        with _DELETE_STATE_LOCK:
            if state_path.exists():
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(raw, list):
                    raise ValueError("state root is not a list")
                for item in raw:
                    try:
                        key = (int(item["chat_id"]), int(item["message_id"]))
                        due_at = float(item["due_at"])
                        _DELETE_DUE[key] = min(_DELETE_DUE.get(key, due_at), due_at)
                    except (KeyError, TypeError, ValueError):
                        log.warning("Ignoring malformed auto-delete state entry")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("Could not read auto-delete state at %s (%s)", state_path, type(exc).__name__)

    for key, due_at in _read_delete_db():
        with _DELETE_STATE_LOCK:
            _DELETE_DUE[key] = min(_DELETE_DUE.get(key, due_at), due_at)

    with _DELETE_STATE_LOCK:
        _write_delete_state_locked()
        entries = list(_DELETE_DUE.items())
    # Mirror merged local/DB entries so an old local-only entry also survives the next restart.
    for key, due_at in entries:
        _upsert_delete_db(key, due_at)

    if not entries:
        log.info("Auto-delete recovery: no pending messages found")
        return
    log.info("Auto-delete recovery: restoring %d pending message(s)", len(entries))
    for key, due_at in entries:
        if key in _DELETE_TASKS and not _DELETE_TASKS[key].done():
            continue
        chat_id, message_id = key
        task = asyncio.create_task(
            _delete_message_after_due(app.bot, chat_id, message_id, due_at, key),
            name=f"auto-delete-recovered-{message_id}"
        )
        _DELETE_TASKS[key] = task


def _schedule_auto_delete_for_ids(
    bot,
    chat_id: int,
    message_id: int,
    delay_seconds: int | None = None,
    *,
    reset: bool = False,
) -> None:
    """Schedule one text/message deletion by IDs; media uploads are never passed here."""
    delay = AUTO_DELETE_SECONDS if delay_seconds is None else max(1, int(delay_seconds))
    key = (int(chat_id), int(message_id))
    existing = _DELETE_TASKS.get(key)
    if existing is not None and not existing.done():
        if not reset:
            return
        existing.cancel()
    due_at = time.time() + delay
    _remember_delete(key, due_at)
    task = asyncio.create_task(
        _delete_message_after_due(bot, key[0], key[1], due_at, key),
        name=f"auto-delete-{message_id}"
    )
    _DELETE_TASKS[key] = task
    log.info("Auto-delete scheduled chat=%s message_id=%s in %ss", key[0], message_id, delay)


def schedule_auto_delete(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    delay_seconds: int | None = None,
    *,
    reset: bool = False,
) -> None:
    """Schedule durable deletion of text/menu/source-link messages, never media uploads.

    Timer tasks are deliberately not registered with Application.create_task(): PTB waits
    for such tasks during shutdown, but these timers may sleep for ten minutes. We manage
    them explicitly and cancel them in post_stop while leaving their due times persisted.
    """
    try:
        _schedule_auto_delete_for_ids(
            context.bot, int(message.chat_id), int(message.message_id), delay_seconds, reset=reset
        )
    except Exception as exc:
        log.warning("Could not schedule message cleanup (%s)", type(exc).__name__)


def cancel_auto_delete(message) -> None:
    """Forget a pending deletion when a temporary message was deleted manually."""
    try:
        key = (int(message.chat_id), int(message.message_id))
        task = _DELETE_TASKS.get(key)
        if task is not None and not task.done():
            task.cancel()
        _forget_delete(key)
    except Exception as exc:
        log.debug("Could not cancel auto-delete timer (%s)", type(exc).__name__)


async def reply_text_auto(context: ContextTypes.DEFAULT_TYPE, message, text: str, *args, **kwargs):
    """Send any bot text/menu/status and make it expire after the configured delay."""
    sent = await message.reply_text(text, *args, **kwargs)
    schedule_auto_delete(context, sent)
    return sent


async def safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text)
        # Progress/status messages must not expire while they are still being updated.
        # Every successful edit renews its TTL; terminal paths set it to 10 minutes again.
        key = (int(message.chat_id), int(message.message_id))
        if key in _DELETE_DUE:
            _schedule_auto_delete_for_ids(
                message.get_bot(), key[0], key[1], AUTO_DELETE_SECONDS, reset=True
            )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.debug("Could not edit status message: %s", exc)
    except TelegramError as exc:
        log.debug("Could not edit status message: %s", type(exc).__name__)
    except Exception as exc:
        log.debug("Could not refresh status message expiry (%s)", type(exc).__name__)


async def _upload_retry(send_callable):
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return await send_callable()
        except RetryAfter as exc:
            last_error = exc
            raw_delay = getattr(exc, "retry_after", 2)
            delay = raw_delay.total_seconds() if hasattr(raw_delay, "total_seconds") else float(raw_delay)
            await asyncio.sleep(min(delay + 1, 30))
        except (TimedOut, NetworkError) as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
        except TelegramError:
            raise
    raise RuntimeError(f"Telegram upload failed after retries: {type(last_error).__name__ if last_error else 'unknown error'}")


async def _send_prepared(message, item: dict[str, Any], caption: str) -> None:
    path = item["path"]
    size = os.path.getsize(path)
    if size > MAX_UPLOAD_BYTES:
        raise RuntimeError(f"Refusing to upload an oversized part: {Path(path).name}")
    ext = Path(path).suffix.lower()
    timeouts = {"read_timeout": 300, "write_timeout": 300, "connect_timeout": 30, "pool_timeout": 60}
    bot = message.get_bot()
    chat_id = message.chat_id

    if item["video"] and ext == ".mp4":
        async def send_video():
            with open(path, "rb") as stream:
                thumbnail_path = item.get("thumbnail")
                if thumbnail_path and os.path.isfile(thumbnail_path):
                    with open(thumbnail_path, "rb") as thumb_stream:
                        return await bot.send_video(
                            chat_id=chat_id,
                            video=stream,
                            thumbnail=thumb_stream,
                            caption=caption[:1024] if caption else None,
                            duration=item.get("duration") or None,
                            width=item.get("width") or None,
                            height=item.get("height") or None,
                            supports_streaming=True,
                            **timeouts,
                        )
                return await bot.send_video(
                    chat_id=chat_id,
                    video=stream,
                    caption=caption[:1024] if caption else None,
                    duration=item.get("duration") or None,
                    width=item.get("width") or None,
                    height=item.get("height") or None,
                    supports_streaming=True,
                    **timeouts,
                )
        await _upload_retry(send_video)
        return

    if item["audio"] and ext == ".mp3":
        async def send_audio():
            with open(path, "rb") as stream:
                return await bot.send_audio(
                    chat_id=chat_id,
                    audio=stream,
                    title=(item.get("title") or Path(path).stem)[:64],
                    performer=(item.get("artist") or None),
                    filename=Path(path).name,
                    caption=caption[:1024] if caption else None,
                    **timeouts,
                )
        await _upload_retry(send_audio)
        return

    if item["kind"] == "image" and size <= 9 * 1024 * 1024:
        async def send_photo():
            with open(path, "rb") as stream:
                return await bot.send_photo(chat_id=chat_id, photo=stream, caption=caption[:1024] if caption else None, **timeouts)
        try:
            await _upload_retry(send_photo)
            return
        except BadRequest:
            pass

    async def send_document():
        with open(path, "rb") as stream:
            return await bot.send_document(
                chat_id=chat_id,
                document=stream,
                filename=Path(path).name,
                caption=caption[:1024] if caption else None,
                **timeouts,
            )
    await _upload_retry(send_document)

async def _send_instagram_album(message, items: list[dict[str, Any]], total_sources: int) -> None:
    """Send a run of carousel photos/videos as one Telegram album, reopening streams on retries."""
    if len(items) < 2 or len(items) > 10:
        raise ValueError("Telegram albums must contain between 2 and 10 items.")

    async def send_album():
        streams = []
        media = []
        try:
            for index, item in enumerate(items, start=1):
                path = item["path"]
                stream = open(path, "rb")
                streams.append(stream)
                title = str(item.get("title") or "Instagram carousel")[:160]
                caption = (f"{title}\nInstagram carousel · {int(item.get('source_index') or index)}/{total_sources}"
                           if index == 1 else None)
                if item["kind"] == "image":
                    media.append(InputMediaPhoto(media=stream, caption=caption))
                elif item["video"] and Path(path).suffix.lower() == ".mp4":
                    thumbnail_path = item.get("thumbnail")
                    if thumbnail_path and os.path.isfile(thumbnail_path):
                        thumb_stream = open(thumbnail_path, "rb")
                        streams.append(thumb_stream)
                    else:
                        thumb_stream = None
                    media.append(InputMediaVideo(
                        media=stream, thumbnail=thumb_stream, caption=caption,
                        duration=item.get("duration") or None,
                        width=item.get("width") or None,
                        height=item.get("height") or None,
                        supports_streaming=True,
                    ))
                else:
                    raise ValueError("Unsupported media type for album")
            return await message.get_bot().send_media_group(chat_id=message.chat_id, media=media)
        finally:
            for stream in streams:
                try:
                    stream.close()
                except Exception:
                    pass

    try:
        await _upload_retry(send_album)
    except BadRequest as exc:
        # A source may produce a valid standalone file that Telegram rejects in an album.
        log.info("Instagram album upload was rejected; falling back to separate media (%s)", str(exc)[:120])
        for index, item in enumerate(items, start=1):
            source_index = int(item.get("source_index") or index)
            item_title = str(item.get("title") or "Instagram carousel")[:160]
            await _send_prepared(message, item, f"{item_title} · Instagram carousel {source_index}/{total_sources}")


def _album_eligible(item: dict[str, Any]) -> bool:
    if int(item.get("source_count", 1)) < 2 or int(item.get("part_count", 1)) != 1:
        return False
    path = str(item.get("path", ""))
    ext = Path(path).suffix.lower()
    if item.get("kind") == "image":
        return ext in {".jpg", ".jpeg", ".png"} and os.path.getsize(path) <= 9 * 1024 * 1024
    return bool(item.get("video")) and ext == ".mp4"


# --------------------------- Download flow and rate limiting ---------------------------
def _rate_remaining(uid: int) -> int:
    now = time.monotonic()
    if len(_last_job) > 10000:
        cutoff = now - USER_COOLDOWN - 60
        for old_uid, timestamp in list(_last_job.items()):
            if timestamp < cutoff:
                _last_job.pop(old_uid, None)
    last = _last_job.get(uid, 0.0)
    remaining = USER_COOLDOWN - (now - last)
    if remaining > 0:
        return int(remaining + 0.99)
    _last_job[uid] = now
    return 0


async def _progress_reporter(status, progress: dict[str, Any], stop_event: asyncio.Event) -> None:
    """Edit one status message with a compact live bar, percentage, and transfer speed."""
    last_text = ""
    while not stop_event.is_set():
        await asyncio.sleep(4)
        raw_pct = str(progress.get("pct") or "")
        if not raw_pct:
            continue
        match = re.search(r"-?\d+(?:\.\d+)?", raw_pct.replace(",", ""))
        if not match:
            continue
        percent = max(0.0, min(100.0, float(match.group(0))))
        width = 12
        filled = min(width, max(0, int(percent / 100 * width)))
        bar = "█" * filled + "░" * (width - filled)
        stage = str(progress.get("stage") or "Downloading")
        text = f"{stage}… [{bar}] {percent:.1f}%"
        if progress.get("spd"):
            text += f"\nSpeed: {progress['spd']}"
        done = int(progress.get("done") or 0)
        total = int(progress.get("total") or 0)
        if total > 0:
            text += f" · {done / 1048576:.1f}/{total / 1048576:.1f} MiB"
        if text != last_text:
            await safe_edit(status, text)
            last_text = text


async def _run_pipeline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    mp3: bool = False,
    subtitles: bool = False,
    force_fmt: str | None = None,
    status=None,
    enforce_cooldown: bool = False,
    quality_pref: int | None | object = _QUALITY_PREF_UNSET,
) -> bool:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return False
    uid = user.id
    try:
        url = validate_url(url)
    except ValueError as exc:
        await reply_text_auto(context, message, str(exc))
        return False

    if status is None:
        status = await message.reply_text(
            "Fetching subtitles…" if subtitles else "Preparing your MP3…" if mp3 else "Starting download…"
        )
        schedule_auto_delete(context, status, reset=True)
    if _is_youtube_url(url):
        remaining = _youtube_verification_cooldown_remaining()
        if remaining > 0:
            minutes = max(1, (remaining + 59) // 60)
            await safe_edit(
                status,
                "Skipping this YouTube request because the previous request from this server was challenged "
                f"for verification. To avoid repeated failed requests, YouTube downloads are paused for about {minutes} more minute(s). "
                "Instagram jobs continue normally; this pause expires automatically.",
            )
            schedule_auto_delete(context, status, reset=True)
            return False
    add_history(uid, url)
    # Remove the user's source-link/command text later; uploaded media messages are not scheduled.
    schedule_auto_delete(context, message)
    loop = asyncio.get_running_loop()
    pool = context.bot_data["pool"]

    try:
        with tempfile.TemporaryDirectory(prefix="dlbot_", dir=WORK_DIR) as workdir:
            if subtitles:
                subdir = os.path.join(workdir, "subtitles")
                try:
                    files = await loop.run_in_executor(
                        pool,
                        lambda: _download_media(url, subdir, "best", subtitles=True),
                    )
                    sent = 0
                    oversized_subtitles = [name for name in files if os.path.getsize(name) > MAX_UPLOAD_BYTES]
                    for subfile in files[:6]:
                        if os.path.getsize(subfile) > MAX_UPLOAD_BYTES:
                            continue
                        async def send_sub(file_path=subfile):
                            with open(file_path, "rb") as stream:
                                return await message.get_bot().send_document(
                                    chat_id=message.chat_id, document=stream, filename=Path(file_path).name
                                )
                        await _upload_retry(send_sub)
                        sent += 1
                    if sent:
                        suffix = " Oversized subtitle files were not sent." if oversized_subtitles else ""
                        await safe_edit(status, f"Finished. Sent {sent} subtitle file(s).{suffix}")
                        schedule_auto_delete(context, status, reset=True)
                        return True
                    if oversized_subtitles:
                        await safe_edit(status, "Subtitle files were found, but they exceed the Telegram upload limit.")
                        schedule_auto_delete(context, status, reset=True)
                        return False
                    await safe_edit(status, "No subtitles were available in the requested languages.")
                    schedule_auto_delete(context, status, reset=True)
                    return False
                finally:
                    shutil.rmtree(subdir, ignore_errors=True)

            chain = (
                ["best[ext=mp4]/best"] if _is_instagram_url(url) and not subtitles
                else (["best[ext=mp4]/best"] if mp3 else _format_chain(uid, force_fmt, quality_pref))
            )
            last_error = "No compatible format was found."
            for index, fmt in enumerate(chain, start=1):
                attempt_dir = os.path.join(workdir, f"attempt_{index}")
                progress: dict[str, Any] = {}
                stop_event = asyncio.Event()
                reporter = asyncio.create_task(_progress_reporter(status, progress, stop_event))
                try:
                    progress["stage"] = "Downloading"
                    await safe_edit(status, f"Downloading… (attempt {index}/{len(chain)})")
                    files = await loop.run_in_executor(
                        pool,
                        lambda d=attempt_dir, f=fmt: _download_media(
                            url, d, f, mp3=mp3, progress=progress
                        ),
                    )
                except (RuntimeError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
                    last_error = str(exc)[:500] or type(exc).__name__
                    log.warning("Download attempt %s failed (%s)", index, type(exc).__name__)
                    stop_event.set()
                    reporter.cancel()
                    try:
                        await reporter
                    except asyncio.CancelledError:
                        pass
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                    if _is_youtube_url(url) and _is_youtube_verification_error(last_error):
                        # A resolution change cannot fix a source-side verification gate.
                        # Stop format retries and pause queued YouTube requests temporarily;
                        # other platforms can continue through the FIFO queue.
                        _record_youtube_verification_challenge()
                        break
                    continue

                try:
                    progress.update(stage="Preparing video", pct="0%", spd="", done=0, total=0)
                    await safe_edit(status, "Download complete. Preparing a playable video…")
                    prepared = await loop.run_in_executor(
                        pool,
                        lambda: _prepare_files(files, attempt_dir, mp3, progress),
                    )
                except (RuntimeError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
                    last_error = str(exc)[:240] or type(exc).__name__
                    log.warning("Splitting/preparation failed on attempt %s (%s)", index, type(exc).__name__)
                    stop_event.set()
                    reporter.cancel()
                    try:
                        await reporter
                    except asyncio.CancelledError:
                        pass
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                    continue
                else:
                    stop_event.set()
                    reporter.cancel()
                    try:
                        await reporter
                    except asyncio.CancelledError:
                        pass

                sent = 0
                total_count = len(prepared)
                upload_error: str | None = None
                is_instagram = _is_instagram_url(url)
                part_index = 0
                while part_index < total_count:
                    item = prepared[part_index]
                    await safe_edit(status, f"Uploading file {part_index + 1}/{total_count} to Telegram…")
                    # Keep gallery order and send eligible Instagram carousel media as albums.
                    if is_instagram and _album_eligible(item):
                        batch = []
                        for candidate in prepared[part_index:part_index + 10]:
                            if not _album_eligible(candidate):
                                break
                            batch.append(candidate)
                        if len(batch) >= 2:
                            try:
                                await context.bot.send_chat_action(
                                    chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO
                                )
                                await _send_instagram_album(message, batch, int(item.get("source_count") or len(batch)))
                                sent += len(batch)
                                part_index += len(batch)
                                if part_index < total_count:
                                    await asyncio.sleep(1.05)
                                continue
                            except Exception as exc:
                                upload_error = type(exc).__name__
                                log.exception("Instagram carousel album upload failed")
                                break
                    media_title = str(item.get("title") or _title_from_path(item["path"]) or "Downloaded media")[:180]
                    if item["part_count"] > 1:
                        caption = f"{media_title} — Part {item['part_index']}/{item['part_count']}"
                    elif item["source_count"] > 1:
                        caption = f"{media_title} — Media {item['source_index']}/{item['source_count']}"
                    elif item.get("conversion_fallback"):
                        caption = f"{media_title}\nOriginal format (playback support depends on your app)"
                    elif item.get("video") and item.get("height"):
                        caption = f"{media_title}\nDownloaded · {int(item['height'])}p"
                    else:
                        caption = media_title
                    try:
                        await context.bot.send_chat_action(
                            chat_id=update.effective_chat.id,
                            action=ChatAction.UPLOAD_VIDEO if item["video"] else ChatAction.UPLOAD_DOCUMENT,
                        )
                        await _send_prepared(message, item, caption)
                        sent += 1
                        await safe_edit(status, f"Uploaded {sent}/{total_count} file(s)…")
                        # Telegram limits repeated messages in one chat; pace multi-part uploads.
                        part_index += 1
                        if part_index < total_count:
                            await asyncio.sleep(1.05)
                    except Exception as exc:
                        upload_error = type(exc).__name__
                        log.exception("Upload failed for prepared part %s/%s", part_index + 1, total_count)
                        break

                if upload_error:
                    await safe_edit(status, f"Downloaded {sent}/{total_count} file(s), but an upload failed ({upload_error}). Please retry.")
                    schedule_auto_delete(context, status, reset=True)
                    return False

                counted_sources: set[int] = set()
                for item in prepared:
                    source_index = int(item["source_index"])
                    if source_index in counted_sources:
                        continue
                    counted_sources.add(source_index)
                    source_parts = [part for part in prepared if int(part["source_index"]) == source_index]
                    source_bytes = sum(os.path.getsize(part["path"]) for part in source_parts)
                    source_kind = item.get("kind", "video")
                    if mp3 or item.get("audio"):
                        source_kind = "audio"
                    bump_stats(uid, source_kind, source_bytes)
                bump_bundle_stats(uid, sent)
                final_note = f"Done! Sent {sent} file(s) successfully."
                if progress.get("instagram_gallery_warning"):
                    final_note += " Some photo slides could not be fetched anonymously."
                await safe_edit(status, final_note)
                schedule_auto_delete(context, status, reset=True)
                return True

            if _is_instagram_url(url):
                lowered = last_error.lower()
                if (not lowered.startswith("instagram did not expose a public downloadable video url")
                        and any(token in lowered for token in ("empty media response", "login", "cookies", "private", "not available", "requested content is not available"))):
                    last_error = (
                        "Instagram did not expose downloadable video data to this server. The bot used the current yt-dlp extractor with browser impersonation and also checked the public page metadata; no Instagram cookies were used. "
                        "Instagram may require sign-in, rate-limit this hosting IP, or omit a direct video URL. Try again later or use media that opens publicly while signed out. Downloads cannot be guaranteed when Instagram withholds anonymous access."
                    )
            await safe_edit(status, f"Download failed: {last_error}")
            schedule_auto_delete(context, status, reset=True)
            return False
    except Exception as exc:
        log.exception("Unexpected download pipeline failure")
        await safe_edit(status, f"Something went wrong ({type(exc).__name__}). Please try again later.")
        if status is not None:
            schedule_auto_delete(context, status, reset=True)
        return False


async def download_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    mp3: bool = False,
    subtitles: bool = False,
    force_fmt: str | None = None,
    playlist: bool = False,
) -> None:
    """Queue one request; downloads run in a single FIFO background worker."""
    await _enqueue_download_requests(
        update, context, [url], mp3=mp3, subtitles=subtitles,
        force_fmt=force_fmt, playlist=playlist,
    )


def _expand_playlist_links(url: str) -> list[str]:
    """Resolve a bounded YouTube playlist to individual URLs using yt-dlp flat metadata."""
    with tempfile.TemporaryDirectory(prefix="dlqueue_playlist_", dir=WORK_DIR) as td:
        command = ["yt-dlp", "--ignore-config", "--dump-single-json", "--flat-playlist",
                   "--playlist-end", str(MAX_PLAYLIST), "--no-warnings", "--remote-components", "ejs:github"]
        if _is_youtube_url(url):
            command += _cookie_args(td)
        command.append(url)
        result = _run_capture(command, 120)
        if result.returncode != 0:
            lines = (result.stdout + "\n" + result.stderr).splitlines()
            detail = _friendly_youtube_error(lines) if _is_youtube_url(url) else _clean_ytdlp_error(lines)
            raise RuntimeError(detail or "Could not read this playlist.")
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("The playlist response was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("The playlist response had an unexpected format.")
        entries = payload.get("entries") or []
        links: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            target = entry.get("webpage_url") or entry.get("original_url")
            if not target:
                raw = entry.get("url")
                if isinstance(raw, str) and raw.startswith(("http://", "https://")):
                    target = raw
            if not target and entry.get("id"):
                extractor = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
                if "youtube" in extractor or _is_youtube_url(url):
                    target = f"https://www.youtube.com/watch?v={entry['id']}"
            if target:
                try:
                    target = validate_url(str(target))
                except ValueError:
                    log.warning("Skipping invalid URL in playlist metadata")
                    continue
                if target not in links:
                    links.append(target)
            if len(links) >= MAX_PLAYLIST:
                break
        if not links:
            raise RuntimeError("No public items were found in this playlist. It may be private or unsupported.")
        return links


def _queue_item_key(url: str, *, mp3: bool, subtitles: bool, force_fmt: str | None, playlist: bool) -> str:
    fields = [url, "mp3" if mp3 else "video", "subs" if subtitles else "no-subs", force_fmt or "auto", "playlist" if playlist else "single"]
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


async def _enqueue_download_requests(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    urls: list[str],
    *,
    mp3: bool = False,
    subtitles: bool = False,
    force_fmt: str | None = None,
    playlist: bool = False,
) -> None:
    """Add links to the shared FIFO queue and return immediately while jobs run in background."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    schedule_auto_delete(context, message)
    queue: asyncio.Queue | None = context.bot_data.get("download_queue")
    lock: asyncio.Lock | None = context.bot_data.get("queue_enqueue_lock")
    queued_keys: set[str] = context.bot_data.get("queued_job_keys", set())
    if queue is None or lock is None:
        await reply_text_auto(context, message, "The download queue is starting. Please resend this link shortly.")
        return

    accepted = duplicates = invalid = full = 0
    candidates: list[tuple[str, dict[str, Any]]] = []
    for raw_url in urls[:MAX_BATCH_LINKS]:
        try:
            url = validate_url(raw_url)
        except ValueError:
            invalid += 1
            continue
        key = _queue_item_key(url, mp3=mp3, subtitles=subtitles, force_fmt=force_fmt, playlist=playlist)
        candidates.append((key, {
            "key": key, "url": url, "update": update, "context": context,
            "user_id": user.id, "chat_id": message.chat_id,
            "mp3": mp3, "subtitles": subtitles, "force_fmt": force_fmt,
            "playlist": playlist, "quality_pref": _get_quality_preference(user.id),
        }))

    # The lock plus queue.put_nowait keeps enqueue operations atomic and in handler order.
    async with lock:
        for key, item in candidates:
            if key in queued_keys:
                duplicates += 1
                continue
            if queue.full():
                full += 1
                continue
            queued_keys.add(key)
            queue.put_nowait(item)
            accepted += 1

    context.bot_data["queued_job_keys"] = queued_keys
    pending = queue.qsize() + (1 if context.bot_data.get("queue_active") else 0)
    notes: list[str] = []
    if accepted:
        notes.append(f"Queued {accepted} link(s). Downloads will start automatically, one at a time.")
        notes.append(f"Queue status: about {pending} job(s) pending or running.")
    if duplicates:
        notes.append(f"Skipped {duplicates} duplicate link(s) already waiting or downloading.")
    if invalid:
        notes.append(f"Skipped {invalid} invalid URL(s).")
    if full:
        notes.append(f"Queue is full; {full} link(s) were not added. Try again when some jobs finish.")
    if not notes:
        notes.append("No new links were added to the queue.")
    await reply_text_auto(context, message, "\n".join(notes))


async def download_batch_job(update: Update, context: ContextTypes.DEFAULT_TYPE, urls: list[str]) -> None:
    """Queue several links from one message while retaining their message order."""
    await _enqueue_download_requests(update, context, urls[:MAX_BATCH_LINKS])


async def _execute_queued_item(app: Application, item: dict[str, Any]) -> None:
    update: Update = item["update"]
    context: ContextTypes.DEFAULT_TYPE = item["context"]
    message = update.effective_message
    chat = update.effective_chat
    chat_id = item.get("chat_id") or (chat.id if chat else None)
    if chat_id is None or message is None:
        return

    status = await context.bot.send_message(
        chat_id=chat_id, text="Starting queued download…",
        connect_timeout=20, read_timeout=30, write_timeout=30,
    )
    schedule_auto_delete(context, status, reset=True)
    url = item["url"]
    should_expand_playlist = bool(item.get("playlist")) or _is_youtube_playlist_url(url)

    if should_expand_playlist:
        await safe_edit(status, "Reading playlist entries…")
        try:
            links = await asyncio.get_running_loop().run_in_executor(
                context.bot_data["pool"], lambda target=url: _expand_playlist_links(target)
            )
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, TimeoutError) as exc:
            log.warning("Queued playlist expansion failed (%s)", type(exc).__name__)
            detail = _friendly_youtube_error([str(exc)]) if _is_youtube_url(url) else str(exc)
            await safe_edit(status, f"Playlist failed: {detail[:350] or type(exc).__name__}")
            schedule_auto_delete(context, status, reset=True)
            return
        if not links:
            await safe_edit(status, "No public playlist items were available.")
            schedule_auto_delete(context, status, reset=True)
            return
        successes = 0
        for index, link in enumerate(links, start=1):
            await safe_edit(status, f"Playlist item {index}/{len(links)}: starting…")
            ok = await _run_pipeline(
                update, context, link,
                mp3=bool(item.get("mp3")), subtitles=bool(item.get("subtitles")),
                force_fmt=item.get("force_fmt"), status=status,
                quality_pref=item.get("quality_pref", _QUALITY_PREF_UNSET),
            )
            successes += int(ok)
            if index < len(links):
                await asyncio.sleep(1.05)
        await safe_edit(status, f"Playlist finished: {successes}/{len(links)} item(s) downloaded successfully.")
        schedule_auto_delete(context, status, reset=True)
        return

    await _run_pipeline(
        update, context, url, mp3=bool(item.get("mp3")), subtitles=bool(item.get("subtitles")),
        force_fmt=item.get("force_fmt"), status=status,
        quality_pref=item.get("quality_pref", _QUALITY_PREF_UNSET),
    )


async def _download_queue_worker(app: Application) -> None:
    """Single consumer: messages can arrive continuously while downloads stay sequential."""
    queue: asyncio.Queue = app.bot_data["download_queue"]
    while True:
        item = await queue.get()
        key = item.get("key", "")
        uid = item.get("user_id")
        app.bot_data["queue_active"] = True
        try:
            async with app.bot_data["download_semaphore"]:
                if isinstance(uid, int):
                    _active_users.add(uid)
                await _execute_queued_item(app, item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Queued download job failed (%s)", type(exc).__name__)
            try:
                await app.bot.send_message(
                    chat_id=item.get("chat_id"),
                    text=f"Queued job failed unexpectedly ({type(exc).__name__}). The next job will continue.",
                )
            except Exception:
                log.warning("Could not send queued-job failure notice")
        finally:
            if isinstance(uid, int):
                _active_users.discard(uid)
            app.bot_data.get("queued_job_keys", set()).discard(key)
            queue.task_done()
            app.bot_data["queue_active"] = not queue.empty()


# --------------------------- Commands ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 MP3 help", callback_data="help_mp3"),
        InlineKeyboardButton("⚙️ Quality", callback_data="quality_menu"),
        InlineKeyboardButton("📊 Stats", callback_data="help_stats"),
    ]])
    await reply_text_auto(context, update.effective_message, WELCOME, reply_markup=keyboard)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    url = first_url(update.effective_message.text)
    if not url:
        await reply_text_auto(context, update.effective_message, "Usage: /audio https://example.com/video")
        return
    await download_job(update, context, url, mp3=True)


async def handle_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    url = first_url(update.effective_message.text)
    if not url:
        await reply_text_auto(context, update.effective_message, "Usage: /sub https://example.com/video")
        return
    await download_job(update, context, url, subtitles=True)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = (message.text or "") if message else ""
    if text.startswith("/"):
        return
    urls = extract_urls(text, limit=MAX_BATCH_LINKS + 1)
    if not urls:
        if message:
            schedule_auto_delete(context, message)
            await reply_text_auto(context, message, "I couldn't find a valid URL. Please send a full HTTP/HTTPS link.")
        return
    was_truncated = len(urls) > MAX_BATCH_LINKS
    if was_truncated:
        urls = urls[:MAX_BATCH_LINKS]

    # A single YouTube video/Short must show the quality menu instead of silently
    # downloading with the automatic default. Keep playlist and multi-link messages
    # on the FIFO path so bulk jobs continue to process without extra taps.
    user = update.effective_user
    if (len(urls) == 1 and user is not None
            and _is_youtube_url(urls[0])
            and _youtube_video_id(urls[0])
            and not _is_youtube_playlist_url(urls[0])):
        now = time.monotonic()
        recent_menu_open = any(
            entry.get("uid") == user.id
            and 0 <= now - float(entry.get("created", 0)) <= 45
            for entry in _PENDING.values()
        )
        if not recent_menu_open:
            log.info("Opening YouTube quality menu for one video/Short; metadata lookup may use generic options")
            await youtube_menu(update, context, urls[0])
            return
        # Rapid follow-up YouTube links are queued behind the most recent menu/job,
        # using the user's saved default so bulk sending remains hands-free.
        # A user can always use /choose URL for an explicit one-off menu.

    # Enqueue before any warning await so later Telegram messages cannot jump ahead.
    await download_batch_job(update, context, urls)
    if was_truncated:
        await reply_text_auto(context, message, f"This message is limited to {MAX_BATCH_LINKS} links; the first {MAX_BATCH_LINKS} were queued.")


async def handle_choose_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    url = first_url(message.text)
    if not url or not _is_youtube_url(url):
        await reply_text_auto(context, message, "Usage: /choose https://www.youtube.com/watch?v=VIDEO_ID")
        return
    await youtube_menu(update, context, url)

async def youtube_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    try:
        url = validate_url(url)
    except ValueError as exc:
        await reply_text_auto(context, update.effective_message, str(exc))
        return
    # Source URL messages are temporary too; only bot text is cleaned by the helper below.
    schedule_auto_delete(context, update.effective_message)
    status = await update.effective_message.reply_text("Reading available video qualities…")
    schedule_auto_delete(context, status)
    try:
        meta = await asyncio.get_running_loop().run_in_executor(
            context.bot_data["pool"], lambda: _get_video_meta(url)
        )
    except Exception as exc:
        log.warning("YouTube quality metadata lookup failed (%s); showing generic quality menu",
                    type(exc).__name__)
        meta = None

    metadata_available = bool(meta)
    formats_available = bool(meta and meta.get("heights"))
    if not meta:
        # Metadata probing can fail even when a later cookie-authenticated download
        # works. Never silently start an automatic download just because probing failed.
        video_id_hint = _youtube_video_id(url)
        meta = {
            "title": f"YouTube video ({video_id_hint})" if video_id_hint else "YouTube video",
            "duration": 0,
            "thumbnail": None,
            "heights": list(QUALITY_LEVELS),
            "sizes": {},
        }
        log.info("Showing generic YouTube quality menu because format metadata was unavailable")

    options: list[tuple[str, str]] = []
    # If yt-dlp returns metadata but no enumerated video formats, still show the
    # supported resolution choices and let the selected downloader selector decide.
    raw_heights = meta.get("heights") or list(QUALITY_LEVELS)
    menu_heights = sorted(
        {int(h) for h in raw_heights if str(h).isdigit() and int(h) in QUALITY_LEVELS},
        reverse=True,
    )
    if not menu_heights:
        menu_heights = list(QUALITY_LEVELS)
    for height in menu_heights:
        size = int(meta["sizes"].get(str(height), 0))
        label_size = f" — {size / 1048576:.1f} MiB" if size else ""
        selector = _quality_selector(height)
        options.append((f"🎬 {height}p{label_size}", selector))
    options += [
        ("⚡ Best available", "__AUTO__"),
        ("⚙️ Use my default", "__DEFAULT__"),
        ("🎵 Download MP3", "__MP3__"),
    ]
    if not options:
        try:
            await status.delete()
            cancel_auto_delete(status)
        except TelegramError:
            schedule_auto_delete(context, status, reset=True)
        await download_job(update, context, url)
        return

    token = uuid.uuid4().hex[:8]
    video_id = _youtube_video_id(url)
    _PENDING[token] = {"url": url, "uid": update.effective_user.id, "options": options,
                      "created": time.monotonic(), "message_id": None}
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, (label, fmt) in enumerate(options):
        if video_id:
            owner = update.effective_user.id
            if fmt == "__MP3__":
                callback_data = f"ym:{video_id}:{owner}"
            elif fmt == "__AUTO__":
                callback_data = f"ya:{video_id}:{owner}"
            elif fmt == "__DEFAULT__":
                callback_data = f"yd:{video_id}:{owner}"
            else:
                height_match = re.search(r"(\d{3,4})p", label)
                height = int(height_match.group(1)) if height_match else 1080
                callback_data = f"yq:{video_id}:{height}:{owner}"
        else:
            # Fallback for unusual YouTube URL forms. These legacy tokens are process-local.
            callback_data = f"pick_{token}_{index}"
        row.append(InlineKeyboardButton(label, callback_data=callback_data))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    title = html.escape(meta["title"])
    duration = int(meta.get("duration") or 0)
    caption = f"<b>{title}</b>"
    if duration:
        caption += f"\nDuration: {duration // 60}:{duration % 60:02d}"
    caption += "\nChoose a one-time resolution, Best available, or Use my default. /q changes your default for future links."
    if not metadata_available or not formats_available:
        caption += "\nFormat details could not be loaded, so file sizes are unavailable. The selected option will be tried when you tap a button."
    try:
        await status.delete()
        cancel_auto_delete(status)
    except TelegramError:
        schedule_auto_delete(context, status, reset=True)
    keyboard = InlineKeyboardMarkup(rows)
    sent = None
    thumbnail_url = str(meta.get("thumbnail") or "")
    thumbnail_path = None
    if thumbnail_url:
        thumbnail_path = await asyncio.get_running_loop().run_in_executor(
            context.bot_data["pool"], lambda: _download_youtube_thumbnail(thumbnail_url, WORK_DIR)
        )
    if thumbnail_path:
        try:
            with open(thumbnail_path, "rb") as photo_stream:
                sent = await update.effective_message.reply_photo(
                    photo=photo_stream,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    connect_timeout=20,
                    read_timeout=30,
                    write_timeout=30,
                )
            schedule_auto_delete(context, sent)
        except Exception as exc:
            log.info("Could not send YouTube thumbnail preview (%s); using text menu", type(exc).__name__)
        finally:
            try:
                os.remove(thumbnail_path)
            except OSError:
                pass
    if sent is None:
        sent = await reply_text_auto(
            context, update.effective_message, caption,
            parse_mode="HTML", reply_markup=keyboard
        )
    _PENDING[token]["message_id"] = sent.message_id
    # Bound memory if the process has many old menus.
    cutoff = time.monotonic() - AUTO_DELETE_SECONDS
    for old_token in list(_PENDING):
        if _PENDING[old_token].get("created", 0) < cutoff:
            _PENDING.pop(old_token, None)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    # Avoid sending a fresh chat message for each repeated tap while a download is active.
    if (data.startswith(("yq:", "ya:", "yd:", "ym:", "pick_"))
            and query.from_user.id in _active_users):
        await query.answer("A download is already running. Please wait for it to finish.", show_alert=True)
        return
    await query.answer()
    # Self-contained YouTube callbacks remain valid if Render restarts and loses RAM state.
    if data.startswith(("yq:", "ya:", "yd:", "ym:")):
        try:
            fields = data.split(":")
            action = fields[0]
            video_id = fields[1]
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
                raise ValueError("invalid video id")
            if action == "yq:" or action == "yq":
                if len(fields) != 4:
                    raise ValueError("invalid quality callback")
                height, owner = int(fields[2]), int(fields[3])
                if height < 144 or height > MAX_VIDEO_HEIGHT or height % 2:
                    raise ValueError("unsupported quality")
            else:
                if len(fields) != 3:
                    raise ValueError("invalid callback")
                owner = int(fields[2])
                height = 0
            if owner != query.from_user.id:
                await reply_text_auto(context, query.message, "This download menu belongs to another user.")
                return
            source_url = f"https://www.youtube.com/watch?v={video_id}"
            if action == "ym":
                await download_job(update, context, source_url, mp3=True)
            elif action == "ya":
                # One-time best choice; do not silently change the user's saved default.
                await download_job(update, context, source_url, force_fmt="best")
            elif action == "yd":
                preferred = _get_quality_preference(query.from_user.id)
                selector = _quality_selector(preferred) if preferred is not None else "best"
                await download_job(update, context, source_url, force_fmt=selector)
            elif action == "yq":
                await download_job(update, context, source_url, force_fmt=_quality_selector(height))
            return
        except (ValueError, IndexError):
            await reply_text_auto(context, query.message, "This menu button is invalid. Send the video link again.")
            return
    if data == "quality_menu":
        await send_quality_menu(query.message, context, query.from_user.id)
        return
    if data == "help_mp3":
        await reply_text_auto(context, query.message, "To download audio, send: /audio https://example.com/video")
        return
    if data == "help_stats":
        await reply_text_auto(context, query.message, "Use /stats to view your download statistics.")
        return
    if data.startswith("setq_"):
        value = data.split("_", 1)[1].strip().lower()
        try:
            normalized = _normalize_quality_preference(value)
        except ValueError:
            await reply_text_auto(context, query.message, "That quality option is invalid. Open /q and choose an available option.")
            return
        await asyncio.to_thread(_set_quality_preference_sync, query.from_user.id, normalized)
        await reply_text_auto(
            context, query.message,
            f"Default YouTube quality saved: {_quality_preference_label(normalized)}. Maximum supported resolution is 1080p."
        )
        return
    if not data.startswith("pick_"):
        return
    try:
        _, token, index_text = data.split("_", 2)
        index = int(index_text)
    except (ValueError, TypeError):
        await reply_text_auto(context, query.message, "This menu is invalid. Send the link again.")
        return
    entry = _PENDING.get(token)
    if (not entry or entry.get("uid") != query.from_user.id or
            entry.get("message_id") != getattr(query.message, "message_id", None) or
            time.monotonic() - entry.get("created", 0) > AUTO_DELETE_SECONDS):
        await reply_text_auto(context, query.message, "This menu has expired. Please send the link again.")
        return
    try:
        label, fmt = entry["options"][index]
    except (IndexError, TypeError):
        await reply_text_auto(context, query.message, "That option is no longer available. Send the link again.")
        return
    # Keep this menu reusable until its normal 10-minute expiry. Users may
    # select another resolution, automatic quality, or MP3 from the same menu.
    if fmt == "__MP3__":
        await download_job(update, context, entry["url"], mp3=True)
    elif fmt == "__AUTO__":
        await download_job(update, context, entry["url"], force_fmt="best")
    elif fmt == "__DEFAULT__":
        preferred = _get_quality_preference(query.from_user.id)
        await download_job(update, context, entry["url"],
                           force_fmt=_quality_selector(preferred) if preferred is not None else "best")
    else:
        await download_job(update, context, entry["url"], force_fmt=fmt)


async def send_quality_menu(message, context: ContextTypes.DEFAULT_TYPE, user_id: int | None = None) -> None:
    uid = int(user_id) if user_id is not None else None
    current = _quality_preference_label(_get_quality_preference(uid)) if uid is not None else _quality_preference_label(DEFAULT_VIDEO_QUALITY_VALUE)
    rows = [
        [InlineKeyboardButton("1080p", callback_data="setq_1080"), InlineKeyboardButton("720p", callback_data="setq_720")],
        [InlineKeyboardButton("480p", callback_data="setq_480"), InlineKeyboardButton("360p", callback_data="setq_360")],
        [InlineKeyboardButton("240p", callback_data="setq_240"), InlineKeyboardButton("144p", callback_data="setq_144")],
        [InlineKeyboardButton("⚡ Best available", callback_data="setq_best")],
    ]
    keyboard = InlineKeyboardMarkup(rows)
    text = (
        f"Your default YouTube quality: {current}\n\n"
        "Choose a maximum resolution for future video and Shorts links (1080p maximum). This is your saved default; "
        "videos may download at a lower resolution when the source has no matching format.\n\n"
        "⚡ Best available selects the highest compatible format up to 1080p. 1440p and 2160p are disabled.\n"
        "For a one-time choice, use /choose followed by a YouTube URL."
    )
    await reply_text_auto(context, message, text, reply_markup=keyboard)


async def handle_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    await send_quality_menu(update.effective_message, context, update.effective_user.id if update.effective_user else None)


async def handle_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    uid = update.effective_user.id if update.effective_user else 0
    if not uid:
        return
    await asyncio.to_thread(_set_quality_preference_sync, uid, None)
    await reply_text_auto(context, update.effective_message, "Default quality set to Best available (maximum 1080p). Future videos use the highest compatible format up to 1080p.")


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    stats = load_stats().get(str(update.effective_user.id), {"videos": 0, "audios": 0, "mb": 0.0})
    await reply_text_auto(context, update.effective_message,
        f"Your download statistics:\nBundles: {stats.get('bundles', 0)}\n"
        f"Videos: {stats.get('videos', 0)}\nAudio files: {stats.get('audios', 0)}\n"
        f"Images: {stats.get('images', 0)}\nFiles: {stats.get('files', 0)}\n"
        f"Telegram parts sent: {stats.get('parts', 0)}\nTotal processed: {stats.get('mb', 0.0)} MiB"
    )


async def handle_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    history = get_history(update.effective_user.id)
    if not history:
        await reply_text_auto(context, update.effective_message, "You don't have any recent links yet.")
        return
    lines = [f"{index}. {url[:150]}" for index, url in enumerate(history, start=1)]
    await reply_text_auto(context, update.effective_message, 
        "Your last five links:\n" + "\n".join(lines) + "\n\nUse /again NUMBER to download one again."
    )


async def handle_again(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    parts = (update.effective_message.text or "").split()
    history = get_history(update.effective_user.id)
    if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= len(history):
        await reply_text_auto(context, update.effective_message, f"Usage: /again 1 (available range: 1–{max(len(history), 1)})")
        return
    await download_job(update, context, history[int(parts[1]) - 1])


async def handle_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    url = first_url(message.text if message else "")
    if not url:
        await reply_text_auto(context, message, "Usage: /playlist https://www.youtube.com/playlist?list=...")
        return
    await download_job(update, context, url, playlist=True)


async def handle_disk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    try:
        stat = os.statvfs(WORK_DIR)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        await reply_text_auto(context, update.effective_message, 
            f"Temporary disk usage:\nUsed: {used / 1048576:.0f} MiB\n"
            f"Free: {free / 1048576:.0f} MiB\nTotal: {total / 1048576:.0f} MiB\n"
            "This storage is temporary on Render Free."
        )
    except OSError as exc:
        await reply_text_auto(context, update.effective_message, f"Unable to read disk information ({type(exc).__name__}).")


async def handle_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    if not ADMIN_IDS or update.effective_user.id not in ADMIN_IDS:
        await reply_text_auto(context, update.effective_message, "This diagnostic command is disabled for this account.")
        return
    url = first_url(update.effective_message.text)
    if not url:
        await reply_text_auto(context, update.effective_message, "Usage: /debug https://youtube.com/watch?v=...")
        return
    try:
        url = validate_url(url)
    except ValueError as exc:
        await reply_text_auto(context, update.effective_message, str(exc))
        return
    await reply_text_auto(context, update.effective_message, "Checking downloader and JavaScript runtime…")
    with tempfile.TemporaryDirectory(prefix="dldebug_", dir=WORK_DIR) as td:
        command = ["yt-dlp", "--ignore-config", "-F", "--no-playlist", "--verbose", "--remote-components", "ejs:github"]
        # YouTube session cookies must never be passed to non-YouTube diagnostics.
        if _is_youtube_url(url):
            command += _cookie_args(td)
        command.append(url)
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                context.bot_data["pool"], lambda: _run_capture(command, 90)
            )
            version = _run_capture(["yt-dlp", "--version"], 10).stdout.strip()
            out = (result.stdout + "\n" + result.stderr)[-2800:]
            await reply_text_auto(context, update.effective_message, f"yt-dlp: {version}\nExit code: {result.returncode}\n\n{out}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            await reply_text_auto(context, update.effective_message, f"Diagnostic failed ({type(exc).__name__}).")

# --------------------------- Health endpoint and startup recovery ---------------------------
def start_health_server(
    *,
    app: Application | None = None,
    event_loop: asyncio.AbstractEventLoop | None = None,
    webhook_secret: str | None = None,
    webhook_path: str | None = None,
) -> ThreadingHTTPServer:
    """Serve health checks and, in webhook mode, authenticated Telegram updates."""
    port = int(os.environ.get("PORT", "10000"))
    path_expected = webhook_path or WEBHOOK_PATH
    secret_expected = webhook_secret or WEBHOOK_SECRET

    class HealthHandler(BaseHTTPRequestHandler):
        def _respond(self, status_code: int, body: bytes) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path not in {"/", "/health", "/healthz"}:
                self._respond(404, b"not-found\n")
                return
            now = time.monotonic()
            healthy = bool(BOT_READY and LAST_TELEGRAM_OK and now - LAST_TELEGRAM_OK < 600)
            still_starting = (not BOT_READY and now - SERVICE_BOOT_TIME < 180)
            status = 200 if healthy or still_starting else 503
            self._respond(status, b"ok\n" if status == 200 else b"telegram-unhealthy\n")

        def do_POST(self):  # noqa: N802
            if app is None or event_loop is None or self.path != path_expected:
                self._respond(404, b"not-found\n")
                return
            received_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(received_secret, secret_expected):
                self._respond(403, b"forbidden\n")
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._respond(400, b"bad-content-length\n")
                return
            if content_length <= 0 or content_length > 1_048_576:
                self._respond(413, b"payload-too-large\n")
                return
            try:
                payload = json.loads(self.rfile.read(content_length))
                if not isinstance(payload, dict):
                    raise ValueError("update must be a JSON object")
                update = Update.de_json(payload, app.bot)
                if update is None:
                    raise ValueError("update is empty")
                future = asyncio.run_coroutine_threadsafe(app.update_queue.put(update), event_loop)
                future.result(timeout=10)
            except Exception as exc:
                log.warning("Rejected Telegram webhook update (%s)", type(exc).__name__)
                self._respond(400, b"bad-update\n")
                return
            self._respond(200, b"ok\n")

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.daemon_threads = True
    threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.5),
        daemon=True,
        name="telegram-health-webhook-server",
    ).start()
    log.info("HTTP health server listening on port %s", port)
    return server


async def _telegram_heartbeat(app: Application) -> None:
    global LAST_TELEGRAM_OK
    while True:
        try:
            await app.bot.get_me(connect_timeout=10, read_timeout=10, pool_timeout=10)
            LAST_TELEGRAM_OK = time.monotonic()
        except Exception as exc:
            log.warning("Telegram heartbeat failed (%s)", type(exc).__name__)
        await asyncio.sleep(120)


async def post_init(app: Application) -> None:
    global BOT_READY, LAST_TELEGRAM_OK
    _recover_pending_auto_deletes(app)
    if DATABASE_URL:
        loaded_preferences = await asyncio.to_thread(_load_quality_preferences_from_db)
        if loaded_preferences:
            log.info("Loaded %s saved quality preference(s) from PostgreSQL", loaded_preferences)
    if not DATABASE_URL:
        log.warning(
            "DATABASE_URL is not configured: auto-delete can recover only while the local state file survives; "
            "Render Free restarts/spin-down can discard it."
        )
    await app.bot.set_my_commands([
        BotCommand("start", "Show help and commands"),
        BotCommand("q", "Set default YouTube quality"),
        BotCommand("quality", "Set default YouTube quality"),
        BotCommand("choose", "Choose quality for one video"),
        BotCommand("auto", "Set Best available as default"),
        BotCommand("audio", "Download audio as MP3"),
        BotCommand("sub", "Download available subtitles"),
        BotCommand("playlist", "Download a playlist (limited items)"),
        BotCommand("stats", "Show your download statistics"),
        BotCommand("history", "Show recent links"),
        BotCommand("again", "Download a recent link again"),
        BotCommand("disk", "Show temporary disk usage"),
    ])
    BOT_READY = True
    LAST_TELEGRAM_OK = time.monotonic()
    # post_init runs before Application.start(); use a regular asyncio task and
    # cancel/await it in post_stop to avoid PTB's not-yet-running task warning.
    app.bot_data["heartbeat_task"] = asyncio.create_task(
        _telegram_heartbeat(app), name="telegram-heartbeat"
    )
    # A single FIFO consumer guarantees all separately sent links are accepted while
    # another download runs and then processed one by one in arrival order.
    app.bot_data["download_queue"] = asyncio.Queue(maxsize=MAX_PENDING_QUEUE)
    app.bot_data["queue_enqueue_lock"] = asyncio.Lock()
    app.bot_data["queued_job_keys"] = set()
    app.bot_data["queue_active"] = False
    app.bot_data["download_queue_task"] = asyncio.create_task(
        _download_queue_worker(app), name="download-queue-worker"
    )
    log.info("Download FIFO queue started (waiting capacity=%s; sequential update intake)", MAX_PENDING_QUEUE)
    if DATABASE_URL:
        app.bot_data["delete_db_retry_task"] = asyncio.create_task(
            _retry_auto_delete_database(app), name="auto-delete-db-retry"
        )
    log.info("Telegram commands registered")


async def _retry_auto_delete_database(app: Application) -> None:
    """Retry transient Postgres outages and reload pending rows after reconnection."""
    if not DATABASE_URL:
        return
    previously_ready = _DELETE_DB_READY
    while True:
        await asyncio.sleep(60)
        try:
            ready = await asyncio.to_thread(_init_delete_db)
        except Exception as exc:
            log.warning("Auto-delete database retry failed (%s)", type(exc).__name__)
            continue
        if ready and not previously_ready:
            # Reload rows that may have been missed while the database was unavailable.
            _recover_pending_auto_deletes(app)
            loaded_preferences = await asyncio.to_thread(_load_quality_preferences_from_db)
            if loaded_preferences:
                log.info("Loaded %s saved quality preference(s) after PostgreSQL reconnect", loaded_preferences)
            log.info("Auto-delete/quality preference recovery completed after database reconnect")
        previously_ready = ready


async def post_stop(app: Application) -> None:
    # Cancel ordinary asyncio timer tasks explicitly. Pending due times remain in
    # persistent state/database and are restored the next time the bot starts.
    pending_queue = app.bot_data.get("download_queue")
    if pending_queue is not None and not pending_queue.empty():
        log.warning("Shutdown with %s queued job(s); the in-memory queue is not persistent", pending_queue.qsize())
    for name in ("heartbeat_task", "delete_db_retry_task", "download_queue_task"):
        task = app.bot_data.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    delete_tasks = list(_DELETE_TASKS.values())
    for task in delete_tasks:
        if not task.done():
            task.cancel()
    if delete_tasks:
        await asyncio.gather(*delete_tasks, return_exceptions=True)


def build_application() -> Application:
    # Keep update handlers sequential so rapidly sent messages enter the FIFO queue
    # in Telegram update order. Downloads themselves run in the separate queue worker,
    # so a long download does not block new links from being accepted.
    app = (Application.builder().token(TOKEN)
           .post_init(post_init).post_stop(post_stop).build())
    app.bot_data["pool"] = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-worker")
    app.bot_data["download_semaphore"] = asyncio.Semaphore(1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("audio", handle_audio))
    app.add_handler(CommandHandler("sub", handle_sub))
    app.add_handler(CommandHandler("playlist", handle_playlist))
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(CommandHandler("auto", handle_auto))
    app.add_handler(CommandHandler("q", handle_quality))
    app.add_handler(CommandHandler("quality", handle_quality))
    app.add_handler(CommandHandler("choose", handle_choose_quality))
    app.add_handler(CommandHandler("history", handle_history))
    app.add_handler(CommandHandler("again", handle_again))
    app.add_handler(CommandHandler("disk", handle_disk))
    app.add_handler(CommandHandler("debug", handle_debug))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        error_type = type(error).__name__ if error else "unknown"
        if error_type == "Conflict":
            log.critical(
                "Telegram polling conflict: another process/service is using this bot token. "
                "Keep exactly one active polling instance and rotate the token if it was exposed."
            )
        else:
            log.error("Telegram handler error: %s", error_type)
    app.add_error_handler(on_error)
    return app


def check_runtime() -> None:
    for executable in ("yt-dlp", "ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"Required executable '{executable}' was not found in PATH.")
    result = _run_capture(["yt-dlp", "--version"], 20)
    log.info("Bot version: %s", BOT_VERSION)
    log.info("yt-dlp version: %s", result.stdout.strip() or "unknown")
    deno = shutil.which("deno")
    log.info("Deno runtime: %s", deno or "not detected (some YouTube downloads may fail)")
    log.info("Pending queue capacity: %s", MAX_PENDING_QUEUE)
    log.info("Default YouTube quality for new users: %s", _quality_preference_label(DEFAULT_VIDEO_QUALITY_VALUE))
    log.info("User quality preferences file: %s", QUALITY_PREFERENCES_FILE)
    log.info("YouTube verification failures stop format retries early")




async def _run_webhook_service(app: Application) -> None:
    """Run PTB using Telegram webhooks on the same port as Render health checks."""
    global BOT_READY, LAST_TELEGRAM_OK, SERVICE_BOOT_TIME
    if not WEBHOOK_BASE_URL.startswith("https://"):
        raise RuntimeError(
            "Webhook mode needs WEBHOOK_URL=https://your-service.onrender.com "
            "or Render's RENDER_EXTERNAL_URL."
        )
    loop = asyncio.get_running_loop()
    server: ThreadingHTTPServer | None = None
    initialized = False
    started = False
    try:
        await app.initialize()
        initialized = True
        await post_init(app)
        await app.start()
        started = True
        server = start_health_server(
            app=app,
            event_loop=loop,
            webhook_secret=WEBHOOK_SECRET,
            webhook_path=WEBHOOK_PATH,
        )
        webhook_url = WEBHOOK_BASE_URL + WEBHOOK_PATH
        await app.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
        log.info("Telegram webhook registered at %s (secret value omitted)", webhook_url)
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        await stop_event.wait()
    finally:
        if server is not None:
            try:
                await asyncio.to_thread(server.shutdown)
                await asyncio.to_thread(server.server_close)
            except Exception as exc:
                log.warning("Webhook HTTP server shutdown issue (%s)", type(exc).__name__)
        if started:
            try:
                await app.stop()
            except Exception as exc:
                log.warning("Telegram application shutdown issue (%s)", type(exc).__name__)
        if initialized:
            try:
                await post_stop(app)
            finally:
                await app.shutdown()
        BOT_READY = False
        LAST_TELEGRAM_OK = 0.0


def main() -> None:
    os.makedirs(WORK_DIR, exist_ok=True)
    check_runtime()
    if TELEGRAM_MODE == "webhook" and not WEBHOOK_BASE_URL:
        raise RuntimeError(
            "TELEGRAM_MODE=webhook requires WEBHOOK_URL or Render's RENDER_EXTERNAL_URL."
        )
    # Polling needs a health-only HTTP server. Webhook mode starts its combined health/
    # webhook server only after the Telegram application is initialized.
    if TELEGRAM_MODE == "polling":
        start_health_server()
    log.info("Telegram transport mode: %s", TELEGRAM_MODE)
    delay = 2
    global BOT_READY, LAST_TELEGRAM_OK, SERVICE_BOOT_TIME
    while True:
        app = None
        pool = None
        BOT_READY = False
        LAST_TELEGRAM_OK = 0.0
        SERVICE_BOOT_TIME = time.monotonic()
        try:
            app = build_application()
            pool = app.bot_data["pool"]
            if TELEGRAM_MODE == "webhook":
                log.info("Starting Telegram webhook service")
                asyncio.run(_run_webhook_service(app))
                log.warning("Webhook service stopped cleanly; exiting restart loop")
            else:
                log.info("Starting Telegram bot polling")
                app.run_polling(bootstrap_retries=-1, drop_pending_updates=False)
                log.warning("Polling stopped cleanly; exiting restart loop")
            break
        except KeyboardInterrupt:
            log.info("Shutdown requested")
            break
        except Exception:
            log.exception("Bot process loop failed; retrying in %s seconds", delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
        finally:
            BOT_READY = False
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        delay = 2


if __name__ == "__main__":
    main()
