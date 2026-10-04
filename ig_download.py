#!/usr/bin/env python3
"""
ig_download.py — Download Instagram posts from a URL list using yt-dlp + instaloader.

Usage:
    python ig_download.py urls.txt art
    python ig_download.py urls.txt art --cookies cookies.txt
    python ig_download.py urls.txt art --dry-run
    python ig_download.py urls.txt art --retry-failed

Rate-limit protection: consecutive rate-limit responses trigger exponential backoff
(30s -> 60s -> 120s -> 300s) and the same URL is retried automatically.

Log deduplication: each URL has at most one entry — retries overwrite the previous
result rather than creating a second entry.

Caption: actual post caption is now stored alongside the synthetic title.

Cookie policy: by default cookies are NOT used for normal downloads. They are
sent only as a fallback when a post fails because it is age-restricted or
private. Override with --cookies-mode always|never.
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is not installed.\nRun: pip install yt-dlp")

try:
    import instaloader
    _INSTALOADER_AVAILABLE = True
except ImportError:
    _INSTALOADER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RATE_LIMIT_DELAYS = [30, 60, 120, 300]
_MAX_CONSECUTIVE_RATE_LIMITS = len(_RATE_LIMIT_DELAYS) + 2

# Instagram sometimes answers a JSON endpoint with an HTML login/consent/challenge
# page (HTTP 200) when it is throttling. yt-dlp's _parse_json() then raises
# "Failed to parse JSON (caused by JSONDecodeError(...))". That is a transient
# throttle/login-wall rather than a broken post, so it is backed off and retried
# exactly like an explicit rate limit instead of being logged as a hard failure.
_TRANSIENT_ERROR_CATEGORIES = frozenset({"rate_limited", "parse_error"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHABCDFJsu]')

def strip_ansi(s):
    return _ANSI_RE.sub('', s or '').strip()


def load_urls(path):
    with open(path, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    seen = set()
    unique = []
    for url in lines:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


class LogCorruptedError(Exception):
    """Raised when an existing log file cannot be parsed.

    We never silently treat a corrupt log as empty, because the next save would
    overwrite it and destroy the download history.
    """

    def __init__(self, path, reason):
        super().__init__(f"Log file is not valid JSON: {path} ({reason})")
        self.path = path
        self.reason = reason


def load_log(path):
    """Load and dedupe the log (latest entry wins per URL).

    Raises LogCorruptedError if the file exists but is not a valid log - the
    caller must decide what to do rather than losing the data.
    """
    if not os.path.exists(path):
        return {"items": [], "summary": {}}
    if os.path.getsize(path) == 0:
        return {"items": [], "summary": {}}
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LogCorruptedError(path, e) from e

    if not (isinstance(data, dict) and isinstance(data.get("items"), list)):
        raise LogCorruptedError(path, "unexpected structure (missing 'items' list)")

    seen = {}
    for item in data["items"]:
        key = item.get("url") or item.get("shortcode") or f"__{len(seen)}"
        seen[key] = item
    data["items"] = list(seen.values())
    return data


def load_log_or_backup(path):
    """Load the log, but if it is corrupt move it aside to <path>.corrupt-<ts>
    and start a fresh one instead of overwriting history."""
    try:
        return load_log(path)
    except LogCorruptedError as e:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.corrupt-{stamp}"
        try:
            os.replace(path, backup)
            print(f"WARNING: {path} is corrupt ({e}); moved it to {backup} "
                  f"and starting a new log.", file=sys.stderr)
        except OSError:
            print(f"WARNING: {path} is corrupt and could not be backed up; "
                  f"starting a new log (the old file was left untouched).", file=sys.stderr)
        return {"items": [], "summary": {}}


def save_log(log, path):
    """Write the log atomically: a crash can never leave a half-written file.

    The JSON is written to a temp file in the same directory, flushed to disk,
    then os.replace()d over the target. os.replace is atomic on one filesystem,
    so readers always see either the old file or the complete new one.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".iglog-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def upsert_log_entry(log, result):
    """Insert or update a log entry keyed by URL. Prevents duplicates on retry runs."""
    url = result.get("url")
    for i, item in enumerate(log["items"]):
        if item.get("url") == url:
            log["items"][i] = result
            return
    log["items"].append(result)


_SHORTCODE_RE = re.compile(
    r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv|share)/"
    r"(?P<code>[A-Za-z0-9_-]+)(?=[/?#]|$)",
    re.IGNORECASE,
)


def shortcode_from_url(url):
    """Extract the post shortcode from common Instagram URL shapes.

    Handles /p/, /reel/, /reels/, /tv/, /share/ and the /<user>/p/... form,
    and ignores query strings and fragments. Returns "unknown" if nothing
    matches.
    """
    m = _SHORTCODE_RE.search(url or "")
    return m.group("code") if m else "unknown"


def already_done(log, url):
    return any(item["url"] == url and item.get("status") == "ok" for item in log["items"])


def already_attempted(log, url):
    return any(item["url"] == url for item in log["items"])


# ---------------------------------------------------------------------------
# Input validation (paths)
# ---------------------------------------------------------------------------

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_collection(name):
    """A collection becomes a single directory name, so reject anything that
    could escape the output folder or is invalid as a folder name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Collection name must not be empty.")
    if name in (".", "..") or os.path.isabs(name) or os.path.basename(name) != name:
        raise ValueError(f"Collection name must not contain path separators or be absolute: {name!r}")
    if _INVALID_NAME_CHARS.search(name):
        raise ValueError(f"Collection name contains characters invalid in a folder name: {name!r}")
    if name.upper() in _WINDOWS_RESERVED:
        raise ValueError(f"Collection name is a reserved system name: {name!r}")
    return name


def validate_filename_template(template):
    """The template is turned into a file path, so it must not escape the
    output folder."""
    template = (template or "").strip() or "{shortcode}"
    if os.path.isabs(template) or "/" in template or "\\" in template:
        raise ValueError(f"Filename template must not contain path separators: {template!r}")
    if ".." in template:
        raise ValueError(f"Filename template must not contain '..': {template!r}")
    if any(ord(c) < 32 for c in template):
        raise ValueError("Filename template must not contain control characters.")
    return template


def resolve_output_dir(output_base, collection):
    """Join base + collection and refuse to leave the base folder."""
    output_base = (output_base or "downloads").strip() or "downloads"
    base_abs = os.path.abspath(output_base)
    output_dir = os.path.join(base_abs, collection)
    if os.path.commonpath([base_abs, os.path.abspath(output_dir)]) != base_abs:
        raise ValueError(f"Resolved output folder escapes the base folder: {output_dir!r}")
    return output_dir


def ffmpeg_available():
    """yt-dlp needs ffmpeg on PATH to merge best video + audio into one mp4."""
    return shutil.which("ffmpeg") is not None


_RE_RATE = re.compile(r"rate[- ]limit reached|too many requests|\b429\b")
_RE_RESTRICTED = re.compile(
    r"private|login|log in|not available|isn't available|\bage\b|"
    r"certain audiences|available to everyone|restricted|sign in|authentication|"
    r"registered users|follow this account")
_RE_DELETED = re.compile(r"does not exist|sorry|removed|deleted")
_RE_UNSUPPORTED = re.compile(r"unsupported url")
_RE_IMAGE = re.compile(r"no video|\bimage\b|\bphoto\b|no formats")
_RE_NETWORK = re.compile(r"network|connect|timed out|http error")
_RE_PARSE = re.compile(r"failed to parse json|jsondecodeerror|unable to extract.*json")


def classify_error(msg):
    m = (msg or "").lower()
    if _RE_RATE.search(m):
        return "rate_limited"
    if _RE_RESTRICTED.search(m):
        return "private_or_restricted"
    if _RE_DELETED.search(m):
        return "deleted_or_not_found"
    if _RE_UNSUPPORTED.search(m):
        return "unsupported_url"
    if _RE_IMAGE.search(m):
        return "image_only"
    if _RE_NETWORK.search(m):
        return "network_error"
    if _RE_PARSE.search(m):
        # Instagram served HTML where JSON was expected (throttle / consent /
        # login wall). Treated as transient by _TRANSIENT_ERROR_CATEGORIES.
        return "parse_error"
    return "other_error"


# ---------------------------------------------------------------------------
# Cookie policy
# ---------------------------------------------------------------------------

# How the cookies file is used:
#   "fallback" (default) - never sent on the first attempt; retried with cookies
#                          ONLY when the post is age-restricted or private.
#   "always"             - sent on the first attempt.
#   "never"              - the cookies file is ignored entirely.
COOKIES_MODE_FALLBACK = "fallback"
COOKIES_MODE_ALWAYS = "always"
COOKIES_MODE_NEVER = "never"
COOKIES_MODES = (COOKIES_MODE_FALLBACK, COOKIES_MODE_ALWAYS, COOKIES_MODE_NEVER)

# Categories/details that mean "this failed because of an age gate or a
# private/login requirement" - the only time cookies are allowed to be used.
_RESTRICTED_ERROR_CATEGORIES = {"private_or_restricted"}
_RESTRICTED_ERROR_MARKERS = (
    "private",
    "login",
    "log in",
    "require_login",
    "registered users",
    "follow this account",
    "authentication",
    "sign in",
    "age-restricted",
    "age restricted",
    "confirm your age",
    "isn't available to everyone",
    "not available to everyone",
    "available to everyone",
    "certain audiences",
    "only available to logged",
    "logged-in users",
    "18+",
    "nsfw",
    "restricted",
)


def is_restricted_error(result):
    """True when a failed download was blocked by an age gate or a
    private/login requirement.

    Network, rate-limit, deleted-post and JSON/parse errors deliberately do NOT
    qualify - cookies are not sent for those.
    """
    if not result or result.get("status") != "failed":
        return False
    if result.get("error_category") in _RESTRICTED_ERROR_CATEGORIES:
        return True
    detail = (result.get("error_detail") or "").lower()
    return any(marker in detail for marker in _RESTRICTED_ERROR_MARKERS)


# ---------------------------------------------------------------------------
# Filename template system
# ---------------------------------------------------------------------------

_TEMPLATE_TO_YTDLP = {
    "{shortcode}":   "%(id)s",
    "{author}":      "%(uploader_id)s",
    "{title}":       "%(title)s",
    "{upload_date}": "%(upload_date)s",
}

FILENAME_PRESETS = {
    "{shortcode}":                              "Shortcode only (default)",
    "{author}_{shortcode}":                     "Author + shortcode",
    "{date}_{shortcode}":                       "Download date + shortcode",
    "{index:04d}_{shortcode}":                  "4-digit index + shortcode",
    "{upload_date}_{shortcode}":                "Upload date + shortcode",
    "{author}_{upload_date}_{shortcode}":       "Author + upload date + shortcode",
    "{date}_{index:04d}_{author}_{shortcode}": "Date + index + author + shortcode",
}

FILENAME_VARIABLE_DOCS = """
Available template variables
────────────────────────────
{shortcode}      Instagram post shortcode, e.g. DYFgyOEuIRN
{author}         Uploader username (e.g. natgeo)
{title}          Post title as reported by yt-dlp
{upload_date}    Original upload date in YYYYMMDD format (e.g. 20240318)
{date}           Today's date in YYYY-MM-DD format (e.g. 2026-05-18)
{index}          Sequential number within the current run (0-based)
{index:04d}      Zero-padded index, width 4  →  0001, 0002, …
{index:03d}      Zero-padded index, width 3  →  001, 002, …

The file extension (.mp4, .jpg, …) is always appended automatically.
For carousel posts the per-slide index (_01, _02, …) is appended before the ext.
"""


def build_outtmpl(template: str, output_dir: str, file_index: int = 0) -> str:
    """Convert our {var} template to a yt-dlp output path string."""
    today = datetime.now().strftime("%Y-%m-%d")
    t = template
    # Pre-substitute non-yt-dlp variables
    t = re.sub(r"\{index:(\d+)d\}", lambda m: f"{file_index:0{int(m.group(1))}d}", t)
    t = t.replace("{index}", str(file_index))
    t = t.replace("{date}", today)
    # Map our var names to yt-dlp %(...)s placeholders
    for our_var, ytdlp_var in _TEMPLATE_TO_YTDLP.items():
        t = t.replace(our_var, ytdlp_var)
    return os.path.join(output_dir, t + ".%(ext)s")


def extract_author_from_title(title, fallback):
    if title and " by " in title:
        return title.split(" by ")[-1].strip()
    return fallback or "unknown"


def _wait_rate_limit(consecutive, stop_event=None, progress_cb=None):
    """Sleep with countdown. Returns True if completed, False if stop_event fired."""
    delay = _RATE_LIMIT_DELAYS[min(consecutive - 1, len(_RATE_LIMIT_DELAYS) - 1)]
    print(f"\n  WARNING Throttled ({consecutive}x in a row) - waiting {delay}s before retrying...", flush=True)
    if progress_cb:
        progress_cb({"type": "rate_limited", "wait_seconds": delay, "consecutive": consecutive})
    deadline = time.monotonic() + delay
    while True:
        if stop_event and stop_event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        print(f"\r  Waiting: {int(remaining):3d}s remaining...  ", end="", flush=True)
        if progress_cb:
            progress_cb({"type": "rate_limit_tick", "remaining": int(remaining)})
        time.sleep(min(1.0, remaining))
    print(f"\r  Resuming...                   ", flush=True)
    return True


def update_manifest(log_path):
    log_dir = os.path.dirname(os.path.abspath(log_path)) or "."
    manifest_path = os.path.join(log_dir, "ig_logs_manifest.json")
    try:
        files = sorted(
            f for f in os.listdir(log_dir)
            if f.endswith(".json") and f != "ig_logs_manifest.json"
        )
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump({"files": files, "updated": datetime.now(timezone.utc).isoformat()}, fh, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Quiet logger
# ---------------------------------------------------------------------------

class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# ---------------------------------------------------------------------------
# yt-dlp wrappers
# ---------------------------------------------------------------------------

def _ydl_opts(outtmpl=None, cookies_file=None, skip_download=False, noplaylist=True):
    opts = {
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": noplaylist,
    }
    if skip_download:
        opts["skip_download"] = True
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        opts["embedmetadata"] = True
    if outtmpl:
        opts["outtmpl"] = outtmpl
    if cookies_file:
        opts["cookiefile"] = cookies_file
    return opts


def yt_extract_info(url, cookies_file):
    # noplaylist=False so carousels return their full entries list
    with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True, cookies_file=cookies_file, noplaylist=False)) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise RuntimeError("yt-dlp returned no info")
    return info


def yt_download(url, outtmpl, cookies_file):
    with yt_dlp.YoutubeDL(_ydl_opts(outtmpl=outtmpl, cookies_file=cookies_file)) as ydl:
        retcode = ydl.download([url])
    if retcode != 0:
        raise RuntimeError(f"yt-dlp download failed with return code {retcode}")


def yt_download_carousel(url, output_dir, shortcode, cookies_file,
                         filename_template="{shortcode}", file_index=0):
    base = build_outtmpl(filename_template, output_dir, file_index)
    # For carousels, %(id)s is the per-item media ID, not the post shortcode.
    # Replace it with the actual shortcode so all slides share a consistent prefix.
    base = base.replace("%(id)s", shortcode)
    # Insert per-slide index before the extension
    outtmpl = base.replace(".%(ext)s", "_%(playlist_index)02d.%(ext)s")
    opts = {
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "embedmetadata": True,
        "ignoreerrors": True,
        "outtmpl": outtmpl,
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    try:
        return sorted(
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if shortcode in f
        )
    except OSError:
        return []


# ---------------------------------------------------------------------------
# instaloader wrapper
# ---------------------------------------------------------------------------

def _apply_instaloader_cookies(L, cookies_file):
    """Load a Netscape cookies.txt into an instaloader session (best effort).

    Returns True when cookies were loaded. instaloader refuses to follow an
    authenticated redirect unless ``context.is_logged_in`` is true, so when a
    ``sessionid`` cookie is present we also mark the context as authenticated.
    Only ever called when the caller has decided cookies are required.
    """
    if not cookies_file or not os.path.exists(cookies_file):
        return False
    try:
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(cookies_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        L.context._session.cookies.update(jar)
        names = {c.name for c in L.context._session.cookies}
        if "sessionid" in names and not L.context.username:
            ds = next((c.value for c in L.context._session.cookies
                       if c.name == "ds_user_id"), None)
            L.context.username = ds or "cookies"
        return True
    except Exception:
        return False


def instaloader_download(url, output_dir, cookies_file=None, download_videos=False):
    """Download a post using instaloader.

    Works without authentication for public posts. When ``cookies_file`` is
    supplied the session is authenticated with it - used as a fallback for
    age-restricted or private content only. Set ``download_videos=True`` when
    yt-dlp could not extract the post at all, so mixed carousels keep their
    videos instead of silently dropping them.
    """
    if not _INSTALOADER_AVAILABLE:
        raise RuntimeError("instaloader is not installed. Run: pip install instaloader")
    shortcode = shortcode_from_url(url)
    if shortcode == "unknown":
        raise RuntimeError(f"Cannot extract shortcode from URL: {url}")
    os.makedirs(output_dir, exist_ok=True)
    L = instaloader.Instaloader(
        dirname_pattern=output_dir,
        filename_pattern="{shortcode}",
        download_videos=download_videos,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
    )
    _apply_instaloader_cookies(L, cookies_file)
    try:
        before = set(os.listdir(output_dir))
    except OSError:
        before = set()
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        L.download_post(post, target=pathlib.Path(output_dir))
    except instaloader.exceptions.InstaloaderException as e:
        raise RuntimeError(f"instaloader: {e}")
    try:
        after = set(os.listdir(output_dir))
        return sorted(
            os.path.join(output_dir, f)
            for f in (after - before)
            if os.path.splitext(f)[1].lower() in {'.jpg', '.jpeg', '.png', '.mp4', '.webp'}
        )
    except OSError:
        return []


def find_saved_files(output_dir, shortcode):
    try:
        return [
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if shortcode in f
        ]
    except OSError:
        return []


def file_exists_on_disk(output_dir, shortcode):
    try:
        return any(shortcode in f for f in os.listdir(output_dir))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Core per-URL handler
# ---------------------------------------------------------------------------

def _download_url_once(url, output_dir, cookies_file, dry_run,
                       filename_template="{shortcode}", file_index=0):
    shortcode = shortcode_from_url(url)
    timestamp = datetime.now(timezone.utc).isoformat()

    username = None
    title = None
    author = None
    caption = ""
    media_type = "unknown"

    try:
        info = yt_extract_info(url, cookies_file)
        username = (info.get("uploader_id") or info.get("uploader") or "unknown").lstrip("@")
        title = info.get("title") or shortcode
        author = extract_author_from_title(title, username)
        caption = (info.get("description") or "").strip()

        entries = info.get("entries")
        formats = info.get("formats") or []

        if entries is not None:
            media_type = "carousel"
        elif not formats:
            media_type = "image_only"
        else:
            media_type = "video"

    except yt_dlp.utils.DownloadError as e:
        err_str = strip_ansi(str(e))
        cat = classify_error(err_str)
        if cat == "image_only":
            if dry_run:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "dry_run", "timestamp": timestamp}
            try:
                saved = instaloader_download(url, output_dir, cookies_file,
                                             download_videos=True)
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "ok",
                        "saved_path": saved[0] if saved else None, "timestamp": timestamp}
            except Exception as insta_err:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "failed",
                        "error_category": "instaloader_error",
                        "error_detail": str(insta_err), "timestamp": timestamp}
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": media_type, "status": "failed",
                "error_category": cat, "error_detail": err_str, "timestamp": timestamp}
    except Exception as e:
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": media_type, "status": "failed",
                "error_category": "extract_error",
                "error_detail": strip_ansi(str(e)), "timestamp": timestamp}

    if dry_run:
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": media_type, "status": "dry_run", "timestamp": timestamp}

    if media_type == "image_only":
        try:
            saved_files = instaloader_download(url, output_dir, cookies_file)
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "image", "status": "ok",
                    "saved_path": saved_files[0] if saved_files else None, "timestamp": timestamp}
        except Exception as e:
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "image", "status": "failed",
                    "error_category": "instaloader_error",
                    "error_detail": str(e), "timestamp": timestamp}

    if media_type == "carousel":
        try:
            saved = yt_download_carousel(url, output_dir, shortcode, cookies_file,
                                         filename_template, file_index)
            if saved:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "carousel", "status": "ok",
                        "carousel_count": len(saved),
                        "saved_path": saved[0], "timestamp": timestamp}
            # yt-dlp got nothing — likely an image-only carousel; try instaloader
            saved_insta = instaloader_download(url, output_dir, cookies_file,
                                                download_videos=True)
            if saved_insta:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "carousel", "status": "ok",
                        "carousel_count": len(saved_insta),
                        "saved_path": saved_insta[0], "timestamp": timestamp}
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "carousel", "status": "failed",
                    "error_category": "no_files_saved",
                    "error_detail": "Neither yt-dlp nor instaloader saved any files for this carousel",
                    "timestamp": timestamp}
        except Exception as e:
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "carousel", "status": "failed",
                    "error_category": "other_error",
                    "error_detail": strip_ansi(str(e)), "timestamp": timestamp}

    outtmpl = build_outtmpl(filename_template, output_dir, file_index)
    try:
        yt_download(url, outtmpl, cookies_file)
        saved_files = find_saved_files(output_dir, shortcode)
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": "video", "status": "ok",
                "saved_path": saved_files[0] if saved_files else None, "timestamp": timestamp}
    except yt_dlp.utils.DownloadError as e:
        err_str = strip_ansi(str(e))
        cat = classify_error(err_str)
        if cat == "image_only":
            try:
                saved_files = instaloader_download(url, output_dir, cookies_file,
                                                   download_videos=True)
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "ok",
                        "saved_path": saved_files[0] if saved_files else None, "timestamp": timestamp}
            except Exception as insta_err:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "failed",
                        "error_category": "instaloader_error",
                        "error_detail": str(insta_err), "timestamp": timestamp}
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": "video", "status": "failed",
                "error_category": cat, "error_detail": err_str, "timestamp": timestamp}
    except Exception as e:
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": "video", "status": "failed",
                "error_category": "other_error",
                "error_detail": strip_ansi(str(e)), "timestamp": timestamp}


def download_url(url, output_dir, cookies_file, dry_run,
                 filename_template="{shortcode}", file_index=0,
                 cookies_mode=COOKIES_MODE_FALLBACK):
    """Download a single URL, applying the cookie policy.

    Default policy (``"fallback"``): the first attempt always runs WITHOUT
    cookies. Cookies are sent on a second attempt only when the first one
    failed because the content is age-restricted or private/login-gated.
    ``"always"`` uses cookies on the first attempt; ``"never"`` ignores them.

    Every returned result carries ``used_cookies`` and ``cookie_fallback`` so
    callers (and the log) can tell exactly what happened.
    """
    if cookies_mode == COOKIES_MODE_NEVER or not cookies_file:
        result = _download_url_once(url, output_dir, None, dry_run,
                                    filename_template, file_index)
        result["used_cookies"] = False
        result["cookie_fallback"] = False
        return result

    if cookies_mode == COOKIES_MODE_ALWAYS:
        result = _download_url_once(url, output_dir, cookies_file, dry_run,
                                    filename_template, file_index)
        result["used_cookies"] = True
        result["cookie_fallback"] = False
        return result

    # Default: try unauthenticated first, fall back to cookies only if the
    # failure is due to an age gate or private/login-gated content.
    result = _download_url_once(url, output_dir, None, dry_run,
                                filename_template, file_index)
    result["used_cookies"] = False
    result["cookie_fallback"] = False

    if is_restricted_error(result):
        retry = _download_url_once(url, output_dir, cookies_file, dry_run,
                                   filename_template, file_index)
        retry["used_cookies"] = True
        retry["cookie_fallback"] = True
        retry["first_error_category"] = result.get("error_category")
        retry["first_error_detail"] = result.get("error_detail")
        return retry
    return result


# ---------------------------------------------------------------------------
# Programmatic entry point
# ---------------------------------------------------------------------------

def run_download(
    url_file,
    collection,
    log_file=None,
    cookies=None,
    cookies_mode=COOKIES_MODE_FALLBACK,
    output_base="downloads",
    retry_failed=False,
    dry_run=False,
    filename_template="{shortcode}",
    progress_cb=None,
    stop_event=None,
):
    """
    Download URLs from url_file into <output_base>/<collection>/.

    cookies_mode: when to use the cookies file - see COOKIES_MODE_*. The
                  default "fallback" sends cookies only for posts that fail
                  because they are age-restricted or private.
    output_base:  base folder for downloads (default "downloads"); each
                  collection is created as a subfolder inside it.
    filename_template: how to name output files — see FILENAME_VARIABLE_DOCS.
    progress_cb: optional callable(event_dict) for real-time progress reporting.
    stop_event:  optional threading.Event; set to cancel cleanly mid-run.
    Returns the final summary dict.
    """
    # Validate anything that becomes a filesystem path before doing real work.
    collection = validate_collection(collection)
    filename_template = validate_filename_template(filename_template)
    output_base = (output_base or "downloads").strip() or "downloads"

    if not _INSTALOADER_AVAILABLE:
        print("Warning: instaloader not installed — image-only downloads will fail.\n"
              "Install with: pip install instaloader", file=sys.stderr)

    if not dry_run and not ffmpeg_available():
        msg = ("ffmpeg was not found on PATH - yt-dlp cannot merge best video+audio, "
               "so video/reel downloads may be single-stream or fail.\n"
               "Install ffmpeg and add it to PATH: https://ffmpeg.org/download.html")
        print(f"Warning: {msg}\n", file=sys.stderr)
        if progress_cb:
            progress_cb({"type": "warning",
                         "message": "ffmpeg not found - video/audio merging may fail"})

    # Derive log file name from collection if not specified
    if log_file is None:
        log_file = f"{collection}.json"

    urls = load_urls(url_file)
    if not urls:
        raise ValueError(f"No URLs found in {url_file}")

    log = load_log_or_backup(log_file)
    if "items" not in log:
        log["items"] = []

    total = len(urls)

    before = len(urls)
    if retry_failed:
        urls = [u for u in urls if not already_done(log, u)]
    else:
        urls = [u for u in urls if not already_attempted(log, u)]
    skipped_auto = before - len(urls)

    if skipped_auto:
        print(f"Skipping {skipped_auto} already-processed URL(s) - {len(urls)} remaining.")
        if progress_cb:
            progress_cb({"type": "skip_info", "skipped": skipped_auto, "remaining": len(urls)})

    output_dir = resolve_output_dir(output_base, collection)
    os.makedirs(output_dir, exist_ok=True)

    counts = {}
    skipped_disk = 0
    consecutive_throttle = 0
    cookie_fallbacks = 0
    i = 0

    if progress_cb:
        progress_cb({"type": "start", "total": total, "to_process": len(urls),
                     "log_file": log_file, "collection": collection})

    while i < len(urls):
        if stop_event and stop_event.is_set():
            print("\n  Download stopped by user.")
            if progress_cb:
                progress_cb({"type": "stopped"})
            break

        url = urls[i]
        shortcode = shortcode_from_url(url)

        if not dry_run and file_exists_on_disk(output_dir, shortcode):
            skipped_disk += 1
            print(f"[{i + 1 + skipped_auto}/{total}] {url}")
            print(f"  -> {shortcode} already on disk, skipping")
            if progress_cb:
                progress_cb({"type": "skipped_disk", "url": url, "shortcode": shortcode,
                             "index": i + skipped_auto, "total": total})
            i += 1
            continue

        print(f"[{i + 1 + skipped_auto}/{total}] {url}", flush=True)
        if progress_cb:
            progress_cb({"type": "downloading", "url": url, "shortcode": shortcode,
                         "index": i + skipped_auto, "total": total})

        result = download_url(url, output_dir, cookies, dry_run, filename_template,
                              i + skipped_auto, cookies_mode=cookies_mode)

        if result.get("cookie_fallback"):
            cookie_fallbacks += 1
            print("  RETRY with cookies (post is age/private restricted)")
            if progress_cb:
                progress_cb({"type": "cookie_fallback", "url": url, "shortcode": shortcode,
                             "first_error_category": result.get("first_error_category")})

        if result.get("error_category") in _TRANSIENT_ERROR_CATEGORIES:
            consecutive_throttle += 1
            if consecutive_throttle >= 5:
                # 5+ consecutive hits — treat as a real throttle, wait and retry
                if consecutive_throttle > _MAX_CONSECUTIVE_RATE_LIMITS:
                    print(f"\n  {consecutive_throttle} consecutive throttled responses - saving progress and aborting.")
                    if progress_cb:
                        progress_cb({"type": "rate_limit_abort", "consecutive": consecutive_throttle})
                    upsert_log_entry(log, result)
                    save_log(log, log_file)
                    update_manifest(log_file)
                    break
                ok = _wait_rate_limit(consecutive_throttle, stop_event=stop_event, progress_cb=progress_cb)
                if not ok:
                    break
                continue  # retry same URL
            # Fewer than 5 consecutive hits — may be unavailable/deleted posts, log and continue
            print(f"  FAIL [{result.get('error_category')}] ({consecutive_throttle}x, not waiting yet) {result.get('error_detail', '')[:120]}")
        else:
            consecutive_throttle = 0
        upsert_log_entry(log, result)

        status = result["status"]
        counts[status] = counts.get(status, 0) + 1

        if status == "ok":
            display = result.get("author") or result.get("username") or "?"
            print(f"  OK {display} - {result.get('saved_path', '')}")
        elif status == "failed":
            print(f"  FAIL [{result.get('error_category', 'error')}] {result.get('error_detail', '')[:120]}")
        elif status == "dry_run":
            display = result.get("author") or result.get("username") or "?"
            print(f"  DRY {display} / {result.get('media_type', '?')}")

        if progress_cb:
            progress_cb({"type": "progress", "index": i + skipped_auto, "total": total,
                         "url": url, "shortcode": shortcode, "status": status,
                         "author": result.get("author") or result.get("username") or "?",
                         "error_category": result.get("error_category"),
                         "error_detail": (result.get("error_detail") or "")[:300],
                         "used_cookies": bool(result.get("used_cookies")),
                         "cookie_fallback": bool(result.get("cookie_fallback"))})

        summary = {
            "total_in_file": total,
            "processed": skipped_auto + i + 1,
            "ok": counts.get("ok", 0),
            "failed": counts.get("failed", 0),
            "dry_run": counts.get("dry_run", 0),
            "skipped_auto": skipped_auto,
            "cookie_fallbacks": cookie_fallbacks,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        log["summary"] = summary
        save_log(log, log_file)
        update_manifest(log_file)
        i += 1

    final_summary = log.get("summary") or {
        "total_in_file": total, "processed": skipped_auto + i,
        "ok": counts.get("ok", 0), "failed": counts.get("failed", 0),
        "dry_run": counts.get("dry_run", 0), "skipped_auto": skipped_auto,
        "cookie_fallbacks": cookie_fallbacks,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    print("\n--- Done ---")
    print(f"  OK:      {counts.get('ok', 0)}")
    print(f"  Failed:  {counts.get('failed', 0)}")
    print(f"  Skipped: {skipped_auto} (already in log)")
    if skipped_disk:
        print(f"  Skipped: {skipped_disk} (file already on disk)")
    if cookie_fallbacks:
        print(f"  Cookies: {cookie_fallbacks} restricted post(s) retried with cookies")
    if dry_run:
        print(f"  Dry-run: {counts.get('dry_run', 0)}")
    print(f"  Output:  {output_dir}")
    print(f"  Log:     {log_file}")

    if progress_cb:
        progress_cb({"type": "done", "summary": final_summary})

    return final_summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Instagram posts from a URL list using yt-dlp + instaloader."
    )
    parser.add_argument("url_file")
    parser.add_argument("collection")
    parser.add_argument("-l", "--log", default=None,
                        help="Log file path (default: <collection>.json)")
    parser.add_argument("--cookies", default=None, metavar="FILE",
                        help=("Path to a cookies.txt file. Only used when a post is "
                              "age-restricted or private (see --cookies-mode)."))
    parser.add_argument("--cookies-mode", choices=COOKIES_MODES,
                        default=COOKIES_MODE_FALLBACK,
                        help=("When to use the cookies file. 'fallback' (default): "
                              "only for age/private restricted posts. 'always': on "
                              "every request. 'never': ignore the cookies file."))
    parser.add_argument("--output-dir", default="downloads", metavar="DIR",
                        help=("Base folder for downloads (default: downloads). "
                              "Each collection is created as a subfolder."))
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--filename-template", default="{shortcode}",
                        metavar="TEMPLATE",
                        help=(
                            "Output filename template. Default: {shortcode}. "
                            "Variables: {shortcode} {author} {title} {upload_date} "
                            "{date} {index} {index:04d}. "
                            "Example: --filename-template '{author}_{shortcode}'"
                        ))
    args = parser.parse_args()

    run_download(
        url_file=args.url_file,
        collection=args.collection,
        log_file=args.log,
        cookies=args.cookies,
        cookies_mode=args.cookies_mode,
        output_base=args.output_dir,
        retry_failed=args.retry_failed,
        dry_run=args.dry_run,
        filename_template=args.filename_template,
    )


if __name__ == "__main__":
    main()
