"""Unified yt-dlp-first download service with diagnostics."""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.request import Request, urlopen

from config import config
from downloader.browser import BrowserCapture, BrowserSessionManager
from downloader.diagnostics import redact_text, redact_url
from downloader.runtime import RuntimeManager


ProgressCallback = Callable[[Dict], None]


class DownloadCategory(Enum):
    """User-facing error categories."""

    NETWORK = "network"
    AUTH_REQUIRED = "auth_required"
    COOKIE_ERROR = "cookie_error"
    NO_FORMAT = "no_format"
    RESTRICTED = "restricted"
    FFMPEG_MISSING = "ffmpeg_missing"
    MISSING_DEPENDENCY = "missing_dependency"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class CookieStatus(Enum):
    """Readable browser-cookie states for UI display."""

    DISABLED = "disabled"
    AVAILABLE = "available"
    BROWSER_NOT_FOUND = "browser_not_found"
    COOKIE_STORE_NOT_FOUND = "cookie_store_not_found"
    READ_FAILED = "read_failed"
    MAYBE_EXPIRED = "maybe_expired"


class DownloadServiceError(Exception):
    """Exception raised after converting low-level failures to UI messages."""

    def __init__(self, category: DownloadCategory, message: str, original: Exception | None = None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.original = original


class DownloadCancelled(Exception):
    """Raised from progress hooks when a task is cancelled."""


class ValidationStatus(Enum):
    UNKNOWN = "unknown"
    VALID = "valid"
    EXPIRED = "expired"
    FORBIDDEN = "forbidden"
    INVALID = "invalid"


class ResourceKind(Enum):
    VIDEO = "video"
    PLAYLIST = "playlist"
    LIVE = "live"


@dataclass
class ParseAttempt:
    """A single parse or fallback strategy execution."""

    strategy: str
    success: bool
    category: str = ""
    summary: str = ""
    extractor: str = ""
    candidate_count: int = 0
    duration_ms: int = 0
    http_status: int = 0


@dataclass
class RequestContext:
    """Private request state. Instances are never serialized to the UI."""

    headers: Dict[str, str] = field(default_factory=dict)
    referer: str = ""
    origin: str = ""
    cookie_source: str = ""
    browser_session_ref: str = ""


class RequestContextStore:
    def __init__(self):
        self._items: Dict[str, RequestContext] = {}
        self._lock = threading.Lock()

    def put(self, context: RequestContext) -> str:
        context_id = uuid.uuid4().hex
        with self._lock:
            self._items[context_id] = context
            while len(self._items) > 1000:
                self._items.pop(next(iter(self._items)))
        return context_id

    def get(self, context_id: str) -> RequestContext | None:
        with self._lock:
            return self._items.get(context_id)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_REQUEST_CONTEXTS = RequestContextStore()
_CANDIDATE_REFS: Dict[str, Tuple[str, str]] = {}
_CANDIDATE_REFS_LOCK = threading.Lock()


@dataclass
class VideoFormat:
    """Normalized format information shown in the UI."""

    format_id: str
    ext: str = ""
    resolution: str = "unknown"
    filesize: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = "none"
    acodec: str = "none"
    protocol: str = ""
    format_note: str = ""
    download_url: str = ""

    @property
    def display_name(self) -> str:
        parts: List[str] = []
        if self.height:
            parts.append(f"{self.height}P")
        elif self.resolution and self.resolution != "unknown":
            parts.append(self.resolution)
        else:
            parts.append("未知清晰度")

        if self.ext:
            parts.append(self.ext.upper())
        if self.format_note:
            parts.append(self.format_note)
        if self.vcodec == "none":
            parts.append("仅音频")
        elif self.acodec == "none":
            parts.append("仅视频")
        return " / ".join(parts)


@dataclass
class MediaCandidate:
    """Direct or semi-direct media candidate discovered during fallback."""

    url: str
    source: str
    ext: str = ""
    protocol: str = ""
    mime_type: str = ""
    media_role: str = "combined"
    validation_status: str = ValidationStatus.UNKNOWN.value
    expires_at: str = ""
    score: int = 0
    request_context_id: str = ""
    http_status: int = 0
    candidate_ref: str = ""

    @property
    def display_name(self) -> str:
        label = self.ext.upper() if self.ext else "MEDIA"
        if self.protocol and self.protocol != self.ext:
            label = f"{label} / {self.protocol.upper()}"
        role = {"video": "仅视频", "audio": "仅音频", "combined": "音视频"}.get(
            self.media_role, self.media_role
        )
        status = {"valid": "已验证", "unknown": "未验证"}.get(
            self.validation_status, self.validation_status
        )
        return f"{self.source}: {label} / {role} / {status}"


@dataclass
class PlaylistEntry:
    entry_id: str
    title: str
    webpage_url: str = ""
    duration: float = 0.0
    is_live: bool = False


@dataclass
class VideoMetadata:
    """Normalized parse result."""

    title: str
    webpage_url: str
    extractor: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    uploader: str = ""
    formats: List[VideoFormat] = field(default_factory=list)
    requires_ffmpeg: bool = False
    ffmpeg_available: bool = False
    requires_login: bool = False
    restriction_status: str = ""
    warning: str = ""
    source_url: str = ""
    candidates: List[MediaCandidate] = field(default_factory=list)
    attempts: List[ParseAttempt] = field(default_factory=list)
    cookie_status: str = CookieStatus.DISABLED.value
    diagnostic_summary: str = ""
    diagnostic_log: str = ""
    resource_kind: str = ResourceKind.VIDEO.value
    is_live: bool = False
    live_status: str = ""
    playlist_entries: List[PlaylistEntry] = field(default_factory=list)
    playlist_count: int = 0

    @property
    def duration_str(self) -> str:
        if not self.duration or self.duration <= 0:
            return "未知"
        total_seconds = int(self.duration)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


@dataclass
class DownloadOptions:
    """Options supplied by UI or CLI callers."""

    quality: str = "最佳"
    cookie_browser: str = "none"
    cookie_profile: str = ""
    concurrent_fragments: int = 4
    output_dir: str | Path | None = None
    download_playlist: bool = False
    overwrite: bool = False
    format_id: str = ""
    diagnostic_mode: bool = True
    dynamic_scan_enabled: bool = True
    request_delay_seconds: float = 0.0
    max_strategy_attempts: int = 4
    candidate_url: str = ""
    candidate_ref: str = ""
    candidate_context_id: str = ""
    browser_assist_mode: str = "fallback"
    live_start: str = "current"
    wait_for_live: bool = False
    playlist_items: List[str] = field(default_factory=list)


@dataclass
class DownloadResult:
    """Download completion result."""

    success: bool
    path: str = ""
    message: str = ""
    attempts: List[ParseAttempt] = field(default_factory=list)
    diagnostic_summary: str = ""


class _MediaTagParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() not in {"video", "audio", "source"}:
            return
        values = dict(attrs)
        for name in ("src", "data-src"):
            if values.get(name):
                self.urls.append(str(values[name]))


class HtmlMediaScanner:
    """Fallback scanner for ordinary pages with directly exposed media URLs."""

    MEDIA_EXTENSIONS = ("m3u8", "mpd", "mp4", "webm", "flv", "mov")
    MEDIA_PATTERN = re.compile(
        r"""(?P<quote>["'])?(?P<url>(?:https?:)?//[^"'\s<>]+?\.(?:m3u8|mpd|mp4|webm|flv|mov)(?:\?[^"'\s<>]*)?|/[^"'\s<>]+?\.(?:m3u8|mpd|mp4|webm|flv|mov)(?:\?[^"'\s<>]*)?|[^"'\s<>]+?\.(?:m3u8|mpd|mp4|webm|flv|mov)(?:\?[^"'\s<>]*)?)(?P=quote)?""",
        re.IGNORECASE,
    )
    JSON_MEDIA_PATTERN = re.compile(
        r'''["'](?:url|src|file|manifest|playurl|play_url|stream_url)["']\s*:\s*["'](?P<url>(?:https?:)?//[^"']+|/[^"']+)["']''',
        re.IGNORECASE,
    )

    SKIP_KEYWORDS = (
        "poster",
        "thumbnail",
        "subtitle",
        "caption",
        "sprite",
        "logo",
        "icon",
        "font",
        "css",
    )

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def scan(self, page_url: str) -> List[str]:
        request = Request(
            page_url,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
        return self._extract_urls(text, page_url)

    def _extract_urls(self, text: str, page_url: str) -> List[str]:
        candidates: List[str] = []
        parser = _MediaTagParser()
        try:
            parser.feed(text)
        except Exception:
            pass
        for value in parser.urls:
            if not any(keyword in value.lower() for keyword in self.SKIP_KEYWORDS):
                candidates.append(urljoin(page_url, html.unescape(value).strip()))

        for match in self.JSON_MEDIA_PATTERN.finditer(text):
            value = html.unescape(match.group("url")).replace(r"\/", "/").strip()
            if not any(keyword in value.lower() for keyword in self.SKIP_KEYWORDS):
                candidates.append(urljoin(page_url, value))

        for match in self.MEDIA_PATTERN.finditer(text):
            raw_url = html.unescape(match.group("url")).strip()
            if self._looks_like_media(raw_url):
                candidates.append(urljoin(page_url, raw_url))

        unique: List[str] = []
        seen = set()
        for item in candidates:
            if item not in seen:
                unique.append(item)
                seen.add(item)

        priority = {"m3u8": 0, "mpd": 1, "mp4": 2, "webm": 3, "flv": 4, "mov": 5, "": 6}
        unique.sort(key=lambda value: priority.get(self._extension(value), 99))
        return unique

    def _looks_like_media(self, url: str) -> bool:
        lower = url.lower()
        if any(keyword in lower for keyword in self.SKIP_KEYWORDS):
            return False
        return any(f".{ext}" in lower for ext in self.MEDIA_EXTENSIONS)

    def _extension(self, url: str) -> str:
        lower = url.lower().split("?", 1)[0]
        for ext in self.MEDIA_EXTENSIONS:
            if lower.endswith(f".{ext}"):
                return ext
        return ""


class DynamicMediaScanner:
    """Compatibility facade over the dedicated Edge CDP capture session."""

    MEDIA_PATTERN = re.compile(r"\.(m3u8|mpd|mp4|webm|flv|mov)(?:\?|$)", re.IGNORECASE)

    def __init__(
        self,
        timeout: int = 45,
        browser_manager: BrowserSessionManager | None = None,
    ):
        self.timeout = timeout
        self.browser_manager = browser_manager or BrowserSessionManager()
        self.last_summary = "动态扫描未运行"
        self.last_captures: List[BrowserCapture] = []
        self.last_headers: Dict[str, str] = {}
        self.last_session_ref = ""

    def scan(self, page_url: str) -> List[str]:
        try:
            captures, headers, session_ref = self.browser_manager.capture(
                page_url,
                timeout=self.timeout,
                visible=True,
                trigger_playback=True,
            )
            self.last_captures = captures
            self.last_headers = headers
            self.last_session_ref = session_ref
            urls = self._filter_urls([item.url for item in captures])
            self.last_summary = (
                "动态网络监听未发现媒体资源"
                if not urls
                else f"专用 Edge 会话命中 {len(urls)} 个候选资源"
            )
            return urls
        except Exception as exc:
            self.last_summary = f"动态扫描执行失败: {redact_text(str(exc))}"
            return []
        finally:
            self.browser_manager.close()

    def _parse_output(self, text: str) -> List[str]:
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        return [line.strip() for line in cleaned.splitlines() if line.strip()]

    def _filter_urls(self, urls: Iterable[str]) -> List[str]:
        seen = set()
        result: List[str] = []
        for url in urls:
            value = str(url or "").strip()
            if not value or value in seen:
                continue
            capture = next((item for item in self.last_captures if item.url == value), None)
            if capture and capture.is_drm:
                continue
            mime_match = bool(
                capture
                and (
                    capture.mime_type.startswith(("video/", "audio/"))
                    or "mpegurl" in capture.mime_type
                    or "dash+xml" in capture.mime_type
                    or capture.resource_type in {"Media", "XHR", "Fetch"}
                )
            )
            if self.MEDIA_PATTERN.search(value) or mime_match:
                seen.add(value)
                result.append(value)
        return result


class YtdlpLogger:
    """Capture yt-dlp diagnostic output for the UI."""

    def __init__(self):
        self.lines: List[str] = []

    def debug(self, message):
        self._record(message)

    def warning(self, message):
        self._record(f"WARNING: {message}")

    def error(self, message):
        self._record(f"ERROR: {message}")

    def _record(self, message):
        text = str(message).strip()
        if text:
            self.lines.append(text)

    def dump(self) -> str:
        return "\n".join(self.lines[-80:])


class DownloadService:
    """High-level service used by the UI for parsing and downloading."""

    AUTH_PATTERNS = (
        "sign in",
        "login",
        "log in",
        "cookie",
        "cookies",
        "auth",
        "unauthorized",
        "forbidden",
        "private",
        "age-restricted",
        "401",
        "403",
        "登录",
        "登陆",
        "权限",
        "私密",
    )
    RESTRICTED_PATTERNS = (
        "drm",
        "widevine",
        "fairplay",
        "playready",
        "encrypted",
        "protected",
        "premium",
        "subscription",
        "subscriber",
        "paywall",
        "purchase",
        "rent",
        "copyright",
        "not available in your country",
        "geo restricted",
        "会员",
        "付费",
        "版权",
        "地区",
        "受保护",
        "加密",
    )
    NETWORK_PATTERNS = (
        "timeout",
        "timed out",
        "connection",
        "network",
        "dns",
        "ssl",
        "tls",
        "proxy",
        "http error 5",
        "远程",
        "连接",
        "超时",
    )
    NO_FORMAT_PATTERNS = (
        "no video formats",
        "requested format is not available",
        "no formats",
        "unsupported url",
        "unable to extract",
        "not a valid url",
        "没有可用",
        "无法提取",
    )

    COOKIE_BROWSERS = {"none", "chrome", "edge", "firefox"}

    def __init__(
        self,
        progress_callback: ProgressCallback | None = None,
        runtime_manager: RuntimeManager | None = None,
        browser_manager: BrowserSessionManager | None = None,
    ):
        self.progress_callback = progress_callback
        self.runtime_manager = runtime_manager or RuntimeManager()
        self.browser_manager = browser_manager or BrowserSessionManager()
        self.scanner = HtmlMediaScanner(timeout=config.TIMEOUT)
        self.dynamic_scanner = DynamicMediaScanner(browser_manager=self.browser_manager)
        self._cancelled = False
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen] = set()
        self._candidate_contexts: Dict[str, str] = {}
        self._last_attempts: List[ParseAttempt] = []
        self._last_diagnostic_log = ""
        self._last_cookie_status = CookieStatus.DISABLED.value

    def parse(self, url: str, options: DownloadOptions | None = None) -> VideoMetadata:
        """Parse a page/video URL into normalized metadata."""
        options = options or DownloadOptions()
        clean_url = self.clean_url(url)
        self._cancelled = False
        self._last_attempts = []
        self._last_diagnostic_log = ""

        cookie_status = self._detect_cookie_status(options.cookie_browser)
        self._last_cookie_status = cookie_status.value
        last_error: DownloadServiceError | None = None

        started = time.monotonic()
        try:
            resolved_url = self._resolve_redirects(clean_url)
            self._record_attempt(
                ParseAttempt(
                    strategy="url-resolve",
                    success=True,
                    summary="URL 已清洗并解析重定向" if resolved_url != clean_url else "URL 已清洗",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
            clean_url = resolved_url
        except Exception as exc:
            self._record_attempt(
                ParseAttempt(
                    strategy="url-resolve",
                    success=False,
                    category=DownloadCategory.NETWORK.value,
                    summary=redact_text(str(exc)),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )

        strategy_count = 0
        for strategy_name, force_generic, impersonate in (
            ("yt-dlp", False, False),
            ("yt-dlp-generic", True, False),
            ("yt-dlp-impersonate", True, True),
        ):
            if strategy_count >= options.max_strategy_attempts:
                break
            strategy_count += 1
            started = time.monotonic()
            try:
                info, log = self._extract_info(
                    clean_url,
                    options,
                    download=False,
                    force_generic=force_generic,
                    impersonate=impersonate,
                )
                metadata = self._metadata_from_info(
                    info,
                    clean_url,
                    attempts=self._last_attempts,
                    cookie_status=cookie_status.value,
                    diagnostic_log=log,
                )
                self._record_attempt(
                    ParseAttempt(
                        strategy=strategy_name,
                        success=bool(metadata.formats or metadata.playlist_count),
                        extractor=metadata.extractor,
                        candidate_count=len(metadata.formats) or metadata.playlist_count,
                        summary=(
                            f"解析到播放列表 {metadata.playlist_count} 个条目"
                            if metadata.playlist_count
                            else f"解析到 {len(metadata.formats)} 个格式"
                        ),
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
                if metadata.formats or metadata.playlist_count:
                    metadata.attempts = list(self._last_attempts)
                    metadata.diagnostic_summary = self._build_diagnostic_summary(metadata)
                    return metadata
            except Exception as exc:
                last_error = self.classify_error(exc, options)
                self._record_attempt(
                    ParseAttempt(
                        strategy=strategy_name,
                        success=False,
                        category=last_error.category.value,
                        summary=last_error.message,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
                if last_error.category in {DownloadCategory.RESTRICTED, DownloadCategory.MISSING_DEPENDENCY}:
                    raise last_error from exc
                if last_error.category in {DownloadCategory.AUTH_REQUIRED, DownloadCategory.COOKIE_ERROR} and (
                    not options.dynamic_scan_enabled or options.browser_assist_mode == "disabled"
                ):
                    raise last_error from exc

        static_result = self._parse_with_static_scan(clean_url, options, cookie_status)
        if static_result:
            static_result.attempts = list(self._last_attempts)
            static_result.diagnostic_summary = self._build_diagnostic_summary(static_result)
            return static_result

        if options.dynamic_scan_enabled:
            dynamic_result = self._parse_with_dynamic_scan(clean_url, options, cookie_status)
            if dynamic_result:
                dynamic_result.attempts = list(self._last_attempts)
                dynamic_result.diagnostic_summary = self._build_diagnostic_summary(dynamic_result)
                return dynamic_result

        if last_error:
            raise last_error
        raise DownloadServiceError(DownloadCategory.NO_FORMAT, "没有找到可下载的视频资源。")

    def download(
        self,
        url: str,
        options: DownloadOptions | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadResult:
        """Download a URL with yt-dlp and return the final path when known."""
        options = options or DownloadOptions()
        clean_url = self.clean_url(url)
        self._cancelled = False
        if progress_callback:
            self.progress_callback = progress_callback

        metadata = self.parse(clean_url, options)
        if metadata.restriction_status:
            raise DownloadServiceError(
                DownloadCategory.RESTRICTED,
                metadata.restriction_status,
            )

        selected_ref = self._resolve_candidate_ref(options.candidate_ref)
        if selected_ref:
            options.candidate_context_id = selected_ref[1]
        target_url = (
            (selected_ref[0] if selected_ref else "")
            or options.candidate_url
            or metadata.source_url
            or self._select_candidate_url(metadata)
            or metadata.webpage_url
            or clean_url
        )
        log = ""
        try:
            try:
                path, log = self._download_with_ytdlp(target_url, options)
            except Exception as first_error:
                refreshed = ""
                if (
                    options.browser_assist_mode != "disabled"
                    and target_url != metadata.webpage_url
                    and self._is_expired_download_error(first_error)
                ):
                    refreshed = self._recapture_candidate(metadata.webpage_url, target_url, options)
                if not refreshed:
                    raise
                target_url = refreshed
                options.candidate_context_id = self._candidate_contexts.get(refreshed, "")
                path, log = self._download_with_ytdlp(refreshed, options)
                self._record_attempt(
                    ParseAttempt(
                        strategy="edge-recapture",
                        success=True,
                        candidate_count=1,
                        summary="签名资源过期后已重新捕获并继续下载",
                    )
                )
            self._record_attempt(
                ParseAttempt(
                    strategy="download",
                    success=True,
                    extractor=metadata.extractor,
                    summary="下载完成",
                )
            )
            metadata.attempts = list(self._last_attempts)
            metadata.diagnostic_summary = self._build_diagnostic_summary(metadata)
            return DownloadResult(
                success=True,
                path=path,
                message="下载完成",
                attempts=list(self._last_attempts),
                diagnostic_summary=metadata.diagnostic_summary,
            )
        except DownloadCancelled:
            raise DownloadServiceError(DownloadCategory.UNKNOWN, "下载已取消。")
        except Exception as exc:
            service_error = self.classify_error(exc, options)
            self._record_attempt(
                ParseAttempt(
                    strategy="download",
                    success=False,
                    category=service_error.category.value,
                    summary=service_error.message,
                )
            )
            if log:
                self._last_diagnostic_log = log
            raise service_error from exc

    def cancel(self) -> None:
        self._cancelled = True
        self.browser_manager.close()
        with self._process_lock:
            processes = list(self._active_processes)
        for process in processes:
            self._terminate_process_tree(process)

    def build_ydl_options(
        self,
        options: DownloadOptions,
        *,
        download: bool,
        progress_callback: ProgressCallback | None = None,
        logger: YtdlpLogger | None = None,
        url: str = "",
        force_generic: bool = False,
    ) -> Dict:
        """Build YoutubeDL options in one testable place."""
        platform_profile = config.get_platform_profile(url=url)
        request_delay = max(options.request_delay_seconds, platform_profile.get("sleep_interval_requests", 0.0))
        headers = dict(platform_profile.get("http_headers", {"User-Agent": config.USER_AGENT}))

        ydl_opts: Dict = {
            "quiet": not options.diagnostic_mode,
            "no_warnings": not options.diagnostic_mode,
            "nocheckcertificate": True,
            "retries": config.RETRY_COUNT,
            "fragment_retries": config.RETRY_COUNT,
            "extractor_retries": config.RETRY_COUNT,
            "socket_timeout": config.TIMEOUT,
            "http_headers": headers,
            "noplaylist": not options.download_playlist,
            "concurrent_fragment_requests": max(1, int(options.concurrent_fragments or 1)),
            "sleep_interval_requests": request_delay,
        }

        if logger:
            ydl_opts["logger"] = logger
            ydl_opts["verbose"] = options.diagnostic_mode
            ydl_opts["progress_with_newline"] = options.diagnostic_mode

        cookie_spec = self._cookie_spec(options.cookie_browser, options.cookie_profile)
        if cookie_spec:
            ydl_opts["cookiesfrombrowser"] = cookie_spec

        ffmpeg_path = self.get_ffmpeg_path()
        if ffmpeg_path:
            ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)

        if force_generic:
            ydl_opts["force_generic_extractor"] = True

        if download:
            output_dir = Path(options.output_dir) if options.output_dir else config.get_download_path()
            output_dir.mkdir(parents=True, exist_ok=True)
            ydl_opts.update(
                {
                    "skip_download": False,
                    "outtmpl": str(output_dir / "%(title).200B" / "%(title).200B.%(ext)s"),
                    "format": self._format_selector(options, bool(ffmpeg_path)),
                    "merge_output_format": "mp4",
                    "continuedl": True,
                    "overwrites": bool(options.overwrite),
                    "windowsfilenames": True,
                    "progress_hooks": [self._make_progress_hook(progress_callback)],
                }
            )
        else:
            ydl_opts["skip_download"] = True

        return ydl_opts

    def classify_error(
        self,
        error: Exception,
        options: DownloadOptions | None = None,
    ) -> DownloadServiceError:
        """Convert low-level errors to actionable Chinese messages."""
        message = str(error)
        lower = message.lower()
        options = options or DownloadOptions()

        if isinstance(error, DownloadServiceError):
            return error

        if isinstance(error, ModuleNotFoundError) and error.name == "yt_dlp":
            return DownloadServiceError(
                DownloadCategory.MISSING_DEPENDENCY,
                "缺少 yt-dlp 依赖，请先在项目虚拟环境中安装 requirements.txt。",
                error,
            )

        if "ffmpeg" in lower and any(word in lower for word in ("not found", "missing", "unable", "找不到")):
            return DownloadServiceError(
                DownloadCategory.FFMPEG_MISSING,
                "缺少 ffmpeg，无法合并音视频或处理部分 HLS/DASH 流。",
                error,
            )

        if self._contains_any(lower, self.RESTRICTED_PATTERNS):
            return DownloadServiceError(
                DownloadCategory.RESTRICTED,
                "该资源可能受 DRM、付费墙、版权或平台权限限制，工具不会绕过这些限制。",
                error,
            )

        if self._contains_any(lower, self.AUTH_PATTERNS):
            if self._normalize_cookie_browser(options.cookie_browser) != "none":
                profile_tip = (
                    f"当前资料档案: {options.cookie_profile or '自动选择'}。"
                    "如果浏览器已登录，请在 Cookie 资料档案中切换 Default/Profile 1/Profile 2 后重试；"
                    "仍失败时请完全退出浏览器再试一次。"
                )
                return DownloadServiceError(
                    DownloadCategory.COOKIE_ERROR,
                    f"登录 Cookie 无效、过期或浏览器 Cookie 无法读取。{profile_tip}",
                    error,
                )
            return DownloadServiceError(
                DownloadCategory.AUTH_REQUIRED,
                "该视频可能需要登录或 Cookie 才能访问，请选择已登录的浏览器 Cookie 后重试。",
                error,
            )

        if self._contains_any(lower, self.NETWORK_PATTERNS):
            return DownloadServiceError(
                DownloadCategory.NETWORK,
                f"网络请求失败：{redact_text(message)}",
                error,
            )

        if self._contains_any(lower, self.NO_FORMAT_PATTERNS):
            return DownloadServiceError(
                DownloadCategory.NO_FORMAT,
                "没有找到可下载的视频格式，可能是站点暂不支持或页面没有暴露视频资源。",
                error,
            )

        return DownloadServiceError(
            DownloadCategory.UNKNOWN,
            f"下载失败：{redact_text(message)}",
            error,
        )

    def clean_url(self, url: str) -> str:
        clean = (url or "").strip().strip("'\"")
        if not clean:
            raise DownloadServiceError(DownloadCategory.UNSUPPORTED, "请输入视频链接。")
        return clean

    def _resolve_redirects(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise DownloadServiceError(DownloadCategory.UNSUPPORTED, "请输入有效的 HTTP/HTTPS 视频链接。")
        request = Request(
            url,
            headers={"User-Agent": config.USER_AGENT, "Range": "bytes=0-0"},
        )
        try:
            with urlopen(request, timeout=min(config.TIMEOUT, 15)) as response:
                return response.geturl() or url
        except HTTPError as exc:
            if exc.code in {401, 403, 405, 416}:
                return exc.geturl() or url
            raise

    def get_ffmpeg_path(self) -> str:
        return self.runtime_manager.ffmpeg_path or config.get_ffmpeg_path()

    def _record_attempt(self, attempt: ParseAttempt) -> None:
        self._last_attempts.append(attempt)

    def _extract_info(
        self,
        url: str,
        options: DownloadOptions,
        *,
        download: bool,
        force_generic: bool = False,
        impersonate: bool = False,
    ):
        args = self._build_ytdlp_command(
            url,
            options,
            download=False,
            force_generic=force_generic,
            request_context_id=self._candidate_contexts.get(url, ""),
            impersonate=impersonate,
        )
        return self._run_ytdlp_json(args)

    def _download_with_ytdlp(self, url: str, options: DownloadOptions) -> Tuple[str, str]:
        context_id = options.candidate_context_id or self._candidate_contexts.get(url, "")
        args = self._build_ytdlp_command(
            url,
            options,
            download=True,
            request_context_id=context_id,
        )
        process = self._start_process(args)
        log_lines: List[str] = []
        output_path = ""
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if line.startswith("VDPROGRESS:"):
                    self._emit_subprocess_progress(line[len("VDPROGRESS:") :])
                elif line.startswith("VDPATH:"):
                    output_path = line[len("VDPATH:") :].strip()
                elif line:
                    log_lines.append(line)
                if self._cancelled:
                    self._terminate_process_tree(process)
                    raise DownloadCancelled()
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError("\n".join(log_lines[-30:]) or f"yt-dlp 退出码 {return_code}")
        finally:
            self._unregister_process(process)
        return output_path, redact_text("\n".join(log_lines[-80:]))

    def _build_ytdlp_command(
        self,
        url: str,
        options: DownloadOptions,
        *,
        download: bool,
        force_generic: bool = False,
        request_context_id: str = "",
        impersonate: bool = False,
    ) -> List[str]:
        executable = self.runtime_manager.yt_dlp_path
        if not executable:
            raise DownloadServiceError(
                DownloadCategory.MISSING_DEPENDENCY,
                "未检测到独立 yt-dlp.exe，请先在运行环境面板安装或修复 yt-dlp。",
            )
        profile = config.get_platform_profile(url=url)
        delay = max(options.request_delay_seconds, profile.get("sleep_interval_requests", 0.0))
        args = [
            executable,
            "--encoding",
            "utf-8",
            "--no-check-certificates",
            "--retries",
            str(config.RETRY_COUNT),
            "--fragment-retries",
            str(config.RETRY_COUNT),
            "--extractor-retries",
            str(config.RETRY_COUNT),
            "--socket-timeout",
            str(config.TIMEOUT),
            "--concurrent-fragments",
            str(max(1, int(options.concurrent_fragments or 1))),
        ]
        if delay:
            args.extend(["--sleep-requests", str(delay)])
        args.append("--yes-playlist" if options.download_playlist else "--no-playlist")
        if options.playlist_items:
            args.extend(["--playlist-items", ",".join(options.playlist_items)])
        if options.wait_for_live:
            args.extend(["--wait-for-video", "30"])
        if options.live_start == "beginning":
            args.append("--live-from-start")
        if force_generic:
            args.append("--force-generic-extractor")
        if impersonate:
            args.extend(["--impersonate", "chrome"])
        cookie_spec = self._cookie_spec(options.cookie_browser, options.cookie_profile)
        if cookie_spec:
            cookie_value = cookie_spec[0]
            if cookie_spec[1]:
                cookie_value += f":{cookie_spec[1]}"
            args.extend(["--cookies-from-browser", cookie_value])
        ffmpeg_path = self.get_ffmpeg_path()
        if ffmpeg_path:
            args.extend(["--ffmpeg-location", str(Path(ffmpeg_path).parent)])
        deno_path = self.runtime_manager.deno_path
        if deno_path:
            args.extend(["--js-runtimes", f"deno:{deno_path}", "--remote-components", "ejs:github"])
        if self.runtime_manager.ejs_plugin_dir:
            args.extend(["--plugin-dirs", self.runtime_manager.ejs_plugin_dir])

        headers = dict(profile.get("http_headers") or {"User-Agent": config.USER_AGENT})
        context = _REQUEST_CONTEXTS.get(request_context_id)
        if context:
            headers.update(context.headers)
            if context.referer:
                headers["Referer"] = context.referer
            if context.origin:
                headers["Origin"] = context.origin
        blocked_headers = {"host", "content-length", "connection", "transfer-encoding"}
        for name, value in headers.items():
            clean_name = str(name).strip()
            clean_value = str(value).replace("\r", " ").replace("\n", " ").strip()
            if (
                clean_value
                and clean_name
                and not clean_name.startswith(":")
                and clean_name.lower() not in blocked_headers
            ):
                args.extend(["--add-header", f"{clean_name}:{clean_value}"])

        if download:
            output_dir = Path(options.output_dir) if options.output_dir else config.get_download_path()
            output_dir.mkdir(parents=True, exist_ok=True)
            args.extend(
                [
                    "--newline",
                    "--progress-template",
                    "download:VDPROGRESS:%(progress.downloaded_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress.filename)s",
                    "--print",
                    "after_move:VDPATH:%(filepath)s",
                    "--output",
                    str(output_dir / "%(title).200B" / "%(title).200B.%(ext)s"),
                    "--format",
                    self._format_selector(options, bool(ffmpeg_path)),
                    "--merge-output-format",
                    "mp4",
                    "--windows-filenames",
                    "--continue",
                ]
            )
            if options.overwrite:
                args.append("--force-overwrites")
            else:
                args.append("--no-overwrites")
        else:
            args.extend(["--dump-single-json", "--skip-download"])
        args.append(url)
        return args

    def _run_ytdlp_json(self, args: List[str]) -> Tuple[Dict, str]:
        process = self._start_process(args)
        try:
            stdout, _ = process.communicate()
            if self._cancelled:
                raise DownloadCancelled()
            lines = [line for line in (stdout or "").splitlines() if line.strip()]
            if process.returncode != 0:
                raise RuntimeError("\n".join(lines[-30:]) or f"yt-dlp 退出码 {process.returncode}")
            json_line = next((line for line in reversed(lines) if line.lstrip().startswith("{")), "")
            if not json_line:
                raise RuntimeError("yt-dlp 未返回结构化解析结果")
            info = json.loads(json_line)
            log = "\n".join(line for line in lines if line != json_line)
            return info, redact_text(log[-20000:])
        finally:
            self._unregister_process(process)

    def _start_process(self, args: List[str]) -> subprocess.Popen:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._process_lock:
            self._active_processes.add(process)
        return process

    def _unregister_process(self, process: subprocess.Popen) -> None:
        with self._process_lock:
            self._active_processes.discard(process)

    def _terminate_process_tree(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()

    def _emit_subprocess_progress(self, payload: str) -> None:
        values = (payload.split("|", 4) + ["", "", "", "", ""])[:5]

        def number(value: str) -> float:
            try:
                return float(value) if value not in {"", "NA", "N/A", "None"} else 0.0
            except ValueError:
                return 0.0

        data = {
            "status": "downloading",
            "downloaded_bytes": number(values[0]),
            "total_bytes": number(values[1]),
            "speed": number(values[2]),
            "eta": number(values[3]),
            "filename": values[4],
        }
        if self.progress_callback:
            self.progress_callback(data)

    def _is_expired_download_error(self, error: Exception) -> bool:
        text = str(error).lower()
        return any(marker in text for marker in ("http error 403", "http error 410", "forbidden", "gone"))

    def _recapture_candidate(
        self,
        page_url: str,
        previous_url: str,
        options: DownloadOptions,
    ) -> str:
        previous_ext = self._extension(previous_url)
        urls = self.dynamic_scanner.scan(page_url)
        if not urls:
            return ""
        self._register_browser_contexts(page_url, urls, options)
        candidates = self._candidates_from_browser_captures(
            self.dynamic_scanner.last_captures,
            "edge-recapture",
        )
        matching = [item for item in candidates if not previous_ext or item.ext == previous_ext]
        selected = (matching or candidates)[0] if (matching or candidates) else None
        return selected.url if selected else ""

    def _remember_progress(self, info: Dict, progress_state: Dict[str, str]) -> None:
        filename = info.get("filename") or info.get("tmpfilename")
        if filename:
            progress_state["filename"] = filename
        if self.progress_callback:
            self.progress_callback(info)

    def _metadata_from_info(
        self,
        info: Dict,
        original_url: str,
        *,
        attempts: List[ParseAttempt],
        cookie_status: str,
        diagnostic_log: str,
    ) -> VideoMetadata:
        if not info:
            raise DownloadServiceError(DownloadCategory.NO_FORMAT, "没有解析到视频信息。")

        root_info = info
        raw_entries = [entry for entry in (root_info.get("entries") or []) if entry]
        is_playlist = root_info.get("_type") in {"playlist", "multi_video"} or bool(raw_entries)
        representative = raw_entries[0] if raw_entries else root_info
        formats = self._formats_from_info(representative)
        requires_ffmpeg = self._requires_ffmpeg(representative, formats)
        warning = ""
        ffmpeg_available = bool(self.get_ffmpeg_path())
        if requires_ffmpeg and not ffmpeg_available:
            warning = "当前未检测到 ffmpeg，高清合并或 HLS/DASH 下载可能失败。"

        restriction_status = ""
        title = root_info.get("title") or representative.get("title") or "未命名视频"
        if self._contains_any(title.lower(), self.RESTRICTED_PATTERNS):
            restriction_status = "视频标题提示资源可能受版权、付费或加密限制。"

        is_live = bool(
            root_info.get("is_live")
            or representative.get("is_live")
            or root_info.get("live_status") in {"is_live", "is_upcoming"}
            or representative.get("live_status") in {"is_live", "is_upcoming"}
        )
        playlist_entries = [
            PlaylistEntry(
                entry_id=str(entry.get("id") or index + 1),
                title=str(entry.get("title") or f"条目 {index + 1}"),
                webpage_url=str(entry.get("webpage_url") or entry.get("url") or ""),
                duration=float(entry.get("duration") or 0),
                is_live=bool(entry.get("is_live")),
            )
            for index, entry in enumerate(raw_entries)
        ]
        context_id = self._store_info_context(representative, original_url)
        metadata = VideoMetadata(
            title=title,
            webpage_url=root_info.get("webpage_url") or original_url,
            extractor=(root_info.get("extractor") or representative.get("extractor") or representative.get("ie_key") or config.resolve_platform(url=original_url)).lower(),
            duration=root_info.get("duration") or representative.get("duration") or 0,
            thumbnail=root_info.get("thumbnail") or representative.get("thumbnail") or "",
            uploader=root_info.get("uploader") or representative.get("uploader") or representative.get("channel") or "未知",
            formats=formats,
            requires_ffmpeg=requires_ffmpeg,
            ffmpeg_available=ffmpeg_available,
            requires_login=False,
            restriction_status=restriction_status,
            warning=warning,
            source_url=original_url,
            candidates=self._candidates_from_formats(
                formats,
                original_url,
                "yt-dlp",
                request_context_id=context_id,
            ),
            attempts=list(attempts),
            cookie_status=cookie_status,
            diagnostic_log=diagnostic_log,
            resource_kind=(
                ResourceKind.PLAYLIST.value
                if is_playlist
                else ResourceKind.LIVE.value if is_live else ResourceKind.VIDEO.value
            ),
            is_live=is_live,
            live_status=str(root_info.get("live_status") or representative.get("live_status") or ""),
            playlist_entries=playlist_entries,
            playlist_count=len(playlist_entries),
        )
        metadata.diagnostic_summary = self._build_diagnostic_summary(metadata)
        self._last_diagnostic_log = diagnostic_log
        return metadata

    def _formats_from_info(self, info: Dict) -> List[VideoFormat]:
        raw_formats = info.get("formats") or []
        formats: List[VideoFormat] = []
        for item in raw_formats:
            if not item:
                continue
            height = item.get("height") or 0
            filesize = item.get("filesize") or item.get("filesize_approx") or 0
            resolution = item.get("resolution") or self._resolution_from_format(item)
            formats.append(
                VideoFormat(
                    format_id=str(item.get("format_id") or ""),
                    ext=item.get("ext") or "",
                    resolution=resolution,
                    filesize=int(filesize or 0),
                    height=int(height or 0),
                    fps=item.get("fps") or 0,
                    vcodec=item.get("vcodec") or "none",
                    acodec=item.get("acodec") or "none",
                    protocol=item.get("protocol") or "",
                    format_note=item.get("format_note") or "",
                    download_url=item.get("url") or "",
                )
            )

        if not formats and info.get("url"):
            formats.append(
                VideoFormat(
                    format_id=str(info.get("format_id") or "direct"),
                    ext=info.get("ext") or self._extension(info.get("url") or "") or "mp4",
                    resolution=info.get("resolution") or "unknown",
                    filesize=int(info.get("filesize") or info.get("filesize_approx") or 0),
                    height=int(info.get("height") or 0),
                    vcodec=info.get("vcodec") or "unknown",
                    acodec=info.get("acodec") or "unknown",
                    protocol=info.get("protocol") or "",
                    download_url=info.get("url") or "",
                )
            )

        formats.sort(key=lambda value: (value.height, value.filesize), reverse=True)
        return formats

    def _candidates_from_formats(
        self,
        formats: Iterable[VideoFormat],
        fallback_url: str,
        source: str,
        request_context_id: str = "",
    ) -> List[MediaCandidate]:
        candidates: List[MediaCandidate] = []
        seen = set()
        for item in formats:
            url = item.download_url or fallback_url
            if not url or url in seen:
                continue
            seen.add(url)
            candidates.append(
                MediaCandidate(
                    url=url,
                    source=source,
                    ext=item.ext or self._extension(url),
                    protocol=item.protocol,
                    mime_type=self._mime_from_ext(item.ext),
                    media_role=self._media_role(item.vcodec, item.acodec),
                    validation_status=ValidationStatus.VALID.value,
                    expires_at=self._expires_at(url),
                    score=self._candidate_score(url, item.ext, self._mime_from_ext(item.ext), item.protocol),
                    request_context_id=request_context_id,
                )
            )
        if not candidates and fallback_url:
            candidates.append(
                MediaCandidate(
                    url=fallback_url,
                    source=source,
                    ext=self._extension(fallback_url),
                    request_context_id=request_context_id,
                )
            )
        for candidate in candidates:
            if request_context_id:
                self._candidate_contexts[candidate.url] = request_context_id
            self._register_candidate_ref(candidate)
        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates

    def _resolution_from_format(self, item: Dict) -> str:
        width = item.get("width")
        height = item.get("height")
        if width and height:
            return f"{width}x{height}"
        if height:
            return f"{height}p"
        return "unknown"

    def _requires_ffmpeg(self, info: Dict, formats: Iterable[VideoFormat]) -> bool:
        protocols = {fmt.protocol.lower() for fmt in formats if fmt.protocol}
        if any("m3u8" in protocol or "dash" in protocol or "mpd" in protocol for protocol in protocols):
            return True
        has_video_only = any(fmt.vcodec != "none" and fmt.acodec == "none" for fmt in formats)
        has_audio_only = any(fmt.vcodec == "none" and fmt.acodec != "none" for fmt in formats)
        if has_video_only and has_audio_only:
            return True
        requested_formats = info.get("requested_formats") or []
        return len(requested_formats) > 1

    def _store_info_context(self, info: Dict, original_url: str) -> str:
        headers = {
            str(name): str(value)
            for name, value in (info.get("http_headers") or {}).items()
            if value is not None
        }
        referer = str(headers.pop("Referer", "") or info.get("webpage_url") or original_url)
        parts = urlsplit(referer)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
        return _REQUEST_CONTEXTS.put(
            RequestContext(
                headers=headers,
                referer=referer,
                origin=origin,
                cookie_source="yt-dlp",
            )
        )

    def _mime_from_ext(self, ext: str) -> str:
        return {
            "m3u8": "application/vnd.apple.mpegurl",
            "mpd": "application/dash+xml",
            "mp4": "video/mp4",
            "webm": "video/webm",
            "flv": "video/x-flv",
            "mov": "video/quicktime",
            "m4a": "audio/mp4",
        }.get((ext or "").lower(), "")

    def _media_role(self, vcodec: str, acodec: str) -> str:
        if vcodec == "none" and acodec != "none":
            return "audio"
        if vcodec != "none" and acodec == "none":
            return "video"
        return "combined"

    def _expires_at(self, url: str) -> str:
        for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
            if name.lower() not in {"expire", "expires", "exp", "x-amz-expires"}:
                continue
            try:
                timestamp = int(value)
                if timestamp > 1_000_000_000:
                    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            except (ValueError, OSError, OverflowError):
                continue
        return ""

    def _candidate_score(self, url: str, ext: str, mime: str, protocol: str = "") -> int:
        lower = url.lower()
        score = 0
        if ext == "m3u8" or "mpegurl" in mime:
            score += 100
            if "master" in lower or "index" in lower:
                score += 25
        elif ext == "mpd" or "dash+xml" in mime:
            score += 115
        elif mime.startswith("video/") or ext in {"mp4", "webm", "mov", "flv"}:
            score += 80
        elif mime.startswith("audio/"):
            score += 45
        if protocol in {"https", "http"}:
            score += 5
        if any(marker in lower for marker in ("segment", "chunk", "frag", "init.mp4", ".ts")):
            score -= 100
        if any(marker in lower for marker in ("adserver", "/ads/", "doubleclick")):
            score -= 200
        return score

    def _validate_candidate(self, candidate: MediaCandidate) -> MediaCandidate:
        context = _REQUEST_CONTEXTS.get(candidate.request_context_id)
        headers = dict(context.headers if context else {})
        if context and context.referer:
            headers.setdefault("Referer", context.referer)
        if context and context.origin:
            headers.setdefault("Origin", context.origin)
        headers.setdefault("User-Agent", config.USER_AGENT)
        headers.setdefault("Range", "bytes=0-4095")
        request = Request(candidate.url, headers=headers)
        try:
            with urlopen(request, timeout=min(config.TIMEOUT, 15)) as response:
                candidate.http_status = int(getattr(response, "status", 200) or 200)
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                body = response.read(4096)
        except HTTPError as exc:
            candidate.http_status = exc.code
            candidate.validation_status = (
                ValidationStatus.EXPIRED.value
                if exc.code in {403, 410} and candidate.expires_at
                else ValidationStatus.FORBIDDEN.value if exc.code in {401, 403} else ValidationStatus.INVALID.value
            )
            return candidate
        except Exception:
            candidate.validation_status = ValidationStatus.UNKNOWN.value
            return candidate
        text = body.decode("utf-8", errors="ignore").lstrip()
        manifest = text.startswith("#EXTM3U") or "<MPD" in text[:1000]
        if content_type:
            candidate.mime_type = content_type
        if not candidate.ext:
            if "mpegurl" in content_type or text.startswith("#EXTM3U"):
                candidate.ext = "m3u8"
                candidate.protocol = "m3u8"
            elif "dash+xml" in content_type or "<MPD" in text[:1000]:
                candidate.ext = "mpd"
                candidate.protocol = "mpd"
            elif content_type.startswith("video/"):
                candidate.ext = content_type.split("/", 1)[1].replace("quicktime", "mov")
                candidate.protocol = "https"
        valid_mime = content_type.startswith(("video/", "audio/")) or any(
            marker in content_type for marker in ("mpegurl", "dash+xml", "octet-stream")
        )
        candidate.validation_status = (
            ValidationStatus.VALID.value if candidate.http_status in {200, 206} and (valid_mime or manifest) else ValidationStatus.INVALID.value
        )
        candidate.score = self._candidate_score(
            candidate.url, candidate.ext, candidate.mime_type, candidate.protocol
        )
        return candidate

    def _parse_with_static_scan(
        self,
        url: str,
        options: DownloadOptions,
        cookie_status: CookieStatus,
    ) -> Optional[VideoMetadata]:
        try:
            urls = self.scanner.scan(url)
        except Exception as exc:
            error = self.classify_error(exc, options)
            self._record_attempt(
                ParseAttempt(
                    strategy="html-scan",
                    success=False,
                    category=error.category.value,
                    summary=error.message,
                )
            )
            return None

        self._record_attempt(
            ParseAttempt(
                strategy="html-scan",
                success=bool(urls),
                candidate_count=len(urls),
                summary=f"扫描到 {len(urls)} 个候选资源",
            )
        )
        if not urls:
            return None
        context_id = _REQUEST_CONTEXTS.put(
            RequestContext(
                headers={"User-Agent": config.USER_AGENT},
                referer=url,
                origin=self._origin(url),
                cookie_source=options.cookie_browser,
            )
        )
        for candidate_url in urls:
            self._candidate_contexts[candidate_url] = context_id
        ranked = self._candidates_from_candidate_urls(urls, "html-scan", validate=True)
        ranked = [
            item
            for item in ranked
            if item.validation_status in {ValidationStatus.VALID.value, ValidationStatus.UNKNOWN.value}
            and item.score >= 0
        ]
        if not ranked:
            return None
        urls = [item.url for item in ranked]
        return self._metadata_from_candidate_url(
            page_url=url,
            media_url=urls[0],
            candidate_urls=urls,
            options=options,
            cookie_status=cookie_status.value,
            strategy="html-scan-downloadable",
        )

    def _parse_with_dynamic_scan(
        self,
        url: str,
        options: DownloadOptions,
        cookie_status: CookieStatus,
    ) -> Optional[VideoMetadata]:
        urls = self.dynamic_scanner.scan(url)
        if any(item.is_drm for item in self.dynamic_scanner.last_captures):
            raise DownloadServiceError(
                DownloadCategory.RESTRICTED,
                "浏览器会话检测到 DRM 授权请求，工具不会绕过该保护。",
            )
        self._record_attempt(
            ParseAttempt(
                strategy="dynamic-scan",
                success=bool(urls),
                candidate_count=len(urls),
                summary=self.dynamic_scanner.last_summary,
            )
        )
        if not urls:
            return None
        self._register_browser_contexts(url, urls, options)
        ranked = self._candidates_from_browser_captures(
            self.dynamic_scanner.last_captures,
            "edge-cdp",
        )
        if not ranked:
            return None
        urls = [item.url for item in ranked]
        metadata = self._metadata_from_candidate_url(
            page_url=url,
            media_url=urls[0],
            candidate_urls=urls,
            options=options,
            cookie_status=cookie_status.value,
            strategy="dynamic-scan-downloadable",
        )
        metadata.candidates = ranked
        metadata.source_url = metadata.candidates[0].url if metadata.candidates else metadata.source_url
        return metadata

    def _register_browser_contexts(
        self,
        page_url: str,
        urls: Iterable[str],
        options: DownloadOptions,
    ) -> None:
        captures = {item.url: item for item in self.dynamic_scanner.last_captures}
        for candidate_url in urls:
            capture = captures.get(candidate_url)
            headers = dict(self.dynamic_scanner.last_headers)
            if capture:
                headers.update(capture.request_headers)
            referer = str(headers.pop("Referer", "") or page_url)
            origin = str(headers.pop("Origin", "") or self._origin(page_url))
            context_id = _REQUEST_CONTEXTS.put(
                RequestContext(
                    headers=headers,
                    referer=referer,
                    origin=origin,
                    cookie_source=options.cookie_browser or "dedicated-edge",
                    browser_session_ref=self.dynamic_scanner.last_session_ref,
                )
            )
            self._candidate_contexts[candidate_url] = context_id

    def _metadata_from_candidate_url(
        self,
        *,
        page_url: str,
        media_url: str,
        candidate_urls: List[str],
        options: DownloadOptions,
        cookie_status: str,
        strategy: str,
    ) -> VideoMetadata:
        try:
            info, log = self._extract_info(media_url, options, download=False)
            metadata = self._metadata_from_info(
                info,
                media_url,
                attempts=self._last_attempts,
                cookie_status=cookie_status,
                diagnostic_log=log,
            )
            metadata.warning = metadata.warning or "通过网页资源扫描找到视频地址。"
            metadata.webpage_url = page_url
            metadata.source_url = media_url
            metadata.candidates = self._candidates_from_candidate_urls(candidate_urls, strategy)
            self._record_attempt(
                ParseAttempt(
                    strategy=strategy,
                    success=True,
                    extractor=metadata.extractor,
                    candidate_count=len(metadata.formats),
                    summary="候选资源可由 yt-dlp 继续解析",
                )
            )
            return metadata
        except Exception:
            ext = self._extension(media_url) or "mp4"
            requires_ffmpeg = ext in {"m3u8", "mpd"}
            self._record_attempt(
                ParseAttempt(
                    strategy=strategy,
                    success=True,
                    candidate_count=1,
                    summary="候选资源将以直链方式交由 yt-dlp 下载",
                )
            )
            return VideoMetadata(
                title=f"通用视频 ({ext})",
                webpage_url=page_url,
                extractor="generic-scan",
                formats=[
                    VideoFormat(
                        format_id="direct",
                        ext=ext,
                        resolution="unknown",
                        protocol=ext,
                        download_url=media_url,
                    )
                ],
                requires_ffmpeg=requires_ffmpeg,
                ffmpeg_available=bool(self.get_ffmpeg_path()),
                warning="通过网页资源扫描找到视频地址。",
                source_url=media_url,
                candidates=self._candidates_from_candidate_urls(candidate_urls, strategy),
                attempts=list(self._last_attempts),
                cookie_status=cookie_status,
                diagnostic_summary="资源来自网页扫描候选直链。",
            )

    def _candidates_from_candidate_urls(
        self,
        urls: Iterable[str],
        source: str,
        *,
        validate: bool = False,
    ) -> List[MediaCandidate]:
        candidates: List[MediaCandidate] = []
        seen = set()
        for value in urls:
            url = str(value or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            ext = self._extension(url)
            candidate = MediaCandidate(
                url=url,
                source=source,
                ext=ext,
                protocol=ext,
                mime_type=self._mime_from_ext(ext),
                validation_status=ValidationStatus.UNKNOWN.value,
                expires_at=self._expires_at(url),
                score=self._candidate_score(url, ext, self._mime_from_ext(ext), ext),
                request_context_id=self._candidate_contexts.get(url, ""),
            )
            candidates.append(self._validate_candidate(candidate) if validate else candidate)
            self._register_candidate_ref(candidates[-1])
        if validate:
            candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates

    def _candidates_from_browser_captures(
        self,
        captures: Iterable[BrowserCapture],
        source: str,
    ) -> List[MediaCandidate]:
        candidates: List[MediaCandidate] = []
        for capture in captures:
            ext = self._extension(capture.url)
            mime = capture.mime_type.lower()
            candidate = MediaCandidate(
                url=capture.url,
                source=source,
                ext=ext,
                protocol=ext or urlsplit(capture.url).scheme,
                mime_type=mime,
                validation_status=(
                    ValidationStatus.VALID.value
                    if capture.status in {200, 206}
                    else ValidationStatus.UNKNOWN.value
                ),
                expires_at=self._expires_at(capture.url),
                score=self._candidate_score(capture.url, ext, mime, ext),
                request_context_id=self._candidate_contexts.get(capture.url, ""),
                http_status=capture.status,
            )
            if not ext or mime == "application/octet-stream":
                candidate = self._validate_candidate(candidate)
            if candidate.score >= 0:
                self._register_candidate_ref(candidate)
                candidates.append(candidate)
        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates

    def _select_candidate_url(self, metadata: VideoMetadata) -> str:
        if metadata.candidates:
            return metadata.candidates[0].url
        return ""

    def _register_candidate_ref(self, candidate: MediaCandidate) -> None:
        if not candidate.candidate_ref:
            candidate.candidate_ref = uuid.uuid4().hex
        with _CANDIDATE_REFS_LOCK:
            _CANDIDATE_REFS[candidate.candidate_ref] = (
                candidate.url,
                candidate.request_context_id,
            )
            while len(_CANDIDATE_REFS) > 1000:
                _CANDIDATE_REFS.pop(next(iter(_CANDIDATE_REFS)))

    def _resolve_candidate_ref(self, candidate_ref: str) -> Tuple[str, str] | None:
        if not candidate_ref:
            return None
        with _CANDIDATE_REFS_LOCK:
            return _CANDIDATE_REFS.get(candidate_ref)

    def _origin(self, url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""

    def _build_diagnostic_summary(self, metadata: VideoMetadata) -> str:
        lines = [
            f"Cookie 状态: {metadata.cookie_status}",
            f"ffmpeg: {'available' if metadata.ffmpeg_available else 'missing'}",
            f"资源类型: {metadata.resource_kind}",
            f"播放列表条目: {metadata.playlist_count}",
            f"直播状态: {metadata.live_status or ('live' if metadata.is_live else 'not-live')}",
            f"尝试次数: {len(metadata.attempts)}",
            f"最终来源: {redact_url(metadata.source_url or metadata.webpage_url)}",
        ]
        for attempt in metadata.attempts:
            outcome = "success" if attempt.success else attempt.category or "failed"
            details = f"{attempt.duration_ms}ms"
            if attempt.http_status:
                details += f", HTTP {attempt.http_status}"
            lines.append(f"{attempt.strategy}: {outcome} - {redact_text(attempt.summary)} [{details}]")
        return "\n".join(lines)

    def _detect_cookie_status(self, browser: str) -> CookieStatus:
        normalized = self._normalize_cookie_browser(browser)
        if normalized == "none":
            return CookieStatus.DISABLED
        cookie_paths = config.get_browser_cookie_paths(normalized)
        if normalized in {"chrome", "edge"} and not config.get_browser_executable(normalized):
            return CookieStatus.BROWSER_NOT_FOUND

        if not cookie_paths:
            return CookieStatus.BROWSER_NOT_FOUND

        existing = [path for path in cookie_paths if Path(path).exists()]
        if not existing:
            return CookieStatus.COOKIE_STORE_NOT_FOUND

        for path in existing:
            try:
                if Path(path).is_dir():
                    if any(child.suffix == ".sqlite" for child in Path(path).rglob("cookies.sqlite")):
                        return CookieStatus.AVAILABLE
                elif Path(path).stat().st_size >= 0:
                    return CookieStatus.AVAILABLE
            except OSError:
                return CookieStatus.READ_FAILED
        return CookieStatus.READ_FAILED

    def _make_progress_hook(self, progress_callback: ProgressCallback | None) -> ProgressCallback:
        def hook(data: Dict) -> None:
            if self._cancelled:
                raise DownloadCancelled()
            if data.get("status") == "downloading":
                normalized = {
                    "status": data.get("status"),
                    "downloaded_bytes": data.get("downloaded_bytes", 0) or 0,
                    "total_bytes": data.get("total_bytes") or data.get("total_bytes_estimate") or 0,
                    "speed": data.get("speed") or 0,
                    "eta": data.get("eta") or 0,
                    "filename": data.get("filename") or data.get("tmpfilename") or "",
                }
                if progress_callback:
                    progress_callback(normalized)
            elif data.get("status") == "finished":
                normalized = {
                    "status": data.get("status"),
                    "downloaded_bytes": data.get("total_bytes") or data.get("downloaded_bytes") or 0,
                    "total_bytes": data.get("total_bytes") or data.get("downloaded_bytes") or 0,
                    "speed": 0,
                    "eta": 0,
                    "filename": data.get("filename") or "",
                }
                if progress_callback:
                    progress_callback(normalized)

        return hook

    def _format_selector(self, options: DownloadOptions, has_ffmpeg: bool) -> str:
        if options.format_id:
            return options.format_id

        height = self._quality_height(options.quality)
        if has_ffmpeg:
            if height:
                return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
            return "bestvideo*+bestaudio/best"

        if height:
            return f"best[height<={height}][vcodec!=none][acodec!=none]/best[height<={height}]/best"
        return "best[vcodec!=none][acodec!=none]/best"

    def _quality_height(self, quality: str) -> int:
        match = re.search(r"(\d{3,4})", quality or "")
        if not match:
            return 0
        return int(match.group(1))

    def _cookie_spec(
        self,
        browser: str,
        profile: str = "",
    ) -> Optional[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
        normalized = self._normalize_cookie_browser(browser)
        if normalized == "none":
            return None
        normalized_profile = (profile or "").strip() or None
        return (normalized, normalized_profile, None, None)

    def _normalize_cookie_browser(self, browser: str) -> str:
        value = (browser or "none").strip().lower()
        aliases = {
            "不使用": "none",
            "none": "none",
            "无": "none",
            "chrome": "chrome",
            "google chrome": "chrome",
            "edge": "edge",
            "microsoft edge": "edge",
            "firefox": "firefox",
        }
        normalized = aliases.get(value, value)
        return normalized if normalized in self.COOKIE_BROWSERS else "none"

    def _find_download_path(self, info, fallback: str) -> str:
        if isinstance(info, dict):
            for download in info.get("requested_downloads") or []:
                for key in ("filepath", "filename", "_filename"):
                    if download.get(key):
                        return str(download[key])
            for key in ("filepath", "filename", "_filename"):
                if info.get(key):
                    return str(info[key])
            for entry in info.get("entries") or []:
                path = self._find_download_path(entry, "")
                if path:
                    return path
        return str(fallback or "")

    def _extension(self, url: str) -> str:
        lower = (url or "").lower().split("?", 1)[0]
        return Path(lower).suffix.lstrip(".")

    def _load_yt_dlp(self):
        raise RuntimeError("进程内 yt-dlp API 已停用；请使用 RuntimeManager 管理的 yt-dlp.exe")

    def _contains_any(self, text: str, patterns: Iterable[str]) -> bool:
        return any(pattern in text for pattern in patterns)
