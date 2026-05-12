"""Unified yt-dlp-first download service with diagnostics."""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from config import config


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


@dataclass
class ParseAttempt:
    """A single parse or fallback strategy execution."""

    strategy: str
    success: bool
    category: str = ""
    summary: str = ""
    extractor: str = ""
    candidate_count: int = 0


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

    @property
    def display_name(self) -> str:
        label = self.ext.upper() if self.ext else "MEDIA"
        if self.protocol and self.protocol != self.ext:
            label = f"{label} / {self.protocol.upper()}"
        return f"{self.source}: {label}"


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


@dataclass
class DownloadResult:
    """Download completion result."""

    success: bool
    path: str = ""
    message: str = ""
    attempts: List[ParseAttempt] = field(default_factory=list)
    diagnostic_summary: str = ""


class HtmlMediaScanner:
    """Fallback scanner for ordinary pages with directly exposed media URLs."""

    MEDIA_EXTENSIONS = ("m3u8", "mpd", "mp4", "webm", "flv", "mov")
    MEDIA_PATTERN = re.compile(
        r"""(?P<quote>["'])?(?P<url>(?:https?:)?//[^"'\s<>]+?\.(?:m3u8|mpd|mp4|webm|flv|mov)(?:\?[^"'\s<>]*)?|/[^"'\s<>]+?\.(?:m3u8|mpd|mp4|webm|flv|mov)(?:\?[^"'\s<>]*)?|[^"'\s<>]+?\.(?:m3u8|mpd|mp4|webm|flv|mov)(?:\?[^"'\s<>]*)?)(?P=quote)?""",
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

        priority = {"m3u8": 0, "mpd": 1, "mp4": 2, "webm": 3, "flv": 4, "mov": 5}
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
    """Optional network-based fallback for dynamic pages.

    This is intentionally conservative for now: it is a structured hook point that
    can later integrate with a real browser tracer without changing the service API.
    """

    MEDIA_PATTERN = re.compile(r"\.(m3u8|mpd|mp4|webm|flv|mov)(?:\?|$)", re.IGNORECASE)

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.last_summary = "动态扫描未运行"

    def scan(self, page_url: str) -> List[str]:
        browser = config.get_browser_executable("edge") or config.get_browser_executable("chrome")
        if not browser:
            self.last_summary = "未检测到可用的 Edge/Chrome 浏览器"
            return []

        script = Path(__file__).resolve().parents[1] / "utils" / "capture_media_urls.js"
        if not script.exists():
            self.last_summary = "缺少动态扫描脚本"
            return []

        user_data_dir = Path(tempfile.mkdtemp(prefix="vd-browser-"))
        try:
            process = subprocess.run(
                [
                    "node",
                    str(script),
                    page_url,
                    browser,
                    str(self.timeout),
                    str(user_data_dir),
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout + 10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.returncode != 0:
                stderr = (process.stderr or "").strip()
                self.last_summary = f"动态扫描失败: {stderr or '浏览器执行异常'}"
                return []
            payload = self._parse_output(process.stdout)
            urls = self._filter_urls(payload)
            self.last_summary = "动态网络监听未发现媒体资源" if not urls else f"动态网络监听命中 {len(urls)} 个候选资源"
            return urls
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            self.last_summary = "动态扫描执行失败"
            return []
        finally:
            shutil.rmtree(user_data_dir, ignore_errors=True)

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
            if self.MEDIA_PATTERN.search(value):
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

    def __init__(self, progress_callback: ProgressCallback | None = None):
        self.progress_callback = progress_callback
        self.scanner = HtmlMediaScanner(timeout=config.TIMEOUT)
        self.dynamic_scanner = DynamicMediaScanner()
        self._cancelled = False
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

        strategy_count = 0
        for strategy_name, extractor in (
            ("yt-dlp", None),
            ("yt-dlp-generic", "Generic"),
        ):
            if strategy_count >= options.max_strategy_attempts:
                break
            strategy_count += 1
            try:
                info, log = self._extract_info(clean_url, options, download=False, force_generic=extractor == "Generic")
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
                        success=bool(metadata.formats),
                        extractor=metadata.extractor,
                        candidate_count=len(metadata.formats),
                        summary=f"解析到 {len(metadata.formats)} 个格式",
                    )
                )
                if metadata.formats:
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
                    )
                )
                if last_error.category in {
                    DownloadCategory.AUTH_REQUIRED,
                    DownloadCategory.COOKIE_ERROR,
                    DownloadCategory.RESTRICTED,
                    DownloadCategory.MISSING_DEPENDENCY,
                }:
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

        target_url = (
            options.candidate_url
            or metadata.source_url
            or self._select_candidate_url(metadata)
            or metadata.webpage_url
            or clean_url
        )
        log = ""
        try:
            path, log = self._download_with_ytdlp(target_url, options)
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

        cookie_spec = self._cookie_spec(options.cookie_browser)
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
                return DownloadServiceError(
                    DownloadCategory.COOKIE_ERROR,
                    "登录 Cookie 无效、过期或浏览器 Cookie 无法读取，请确认浏览器已登录后重试。",
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
                f"网络请求失败：{message}",
                error,
            )

        if self._contains_any(lower, self.NO_FORMAT_PATTERNS):
            return DownloadServiceError(
                DownloadCategory.NO_FORMAT,
                "没有找到可下载的视频格式，可能是站点暂不支持或页面没有暴露视频资源。",
                error,
            )

        return DownloadServiceError(DownloadCategory.UNKNOWN, f"下载失败：{message}", error)

    def clean_url(self, url: str) -> str:
        clean = (url or "").strip().strip("'\"")
        if not clean:
            raise DownloadServiceError(DownloadCategory.UNSUPPORTED, "请输入视频链接。")
        return clean

    def get_ffmpeg_path(self) -> str:
        ffmpeg_path = config.get_ffmpeg_path()
        if ffmpeg_path:
            return ffmpeg_path
        return shutil.which("ffmpeg") or ""

    def _record_attempt(self, attempt: ParseAttempt) -> None:
        self._last_attempts.append(attempt)

    def _extract_info(
        self,
        url: str,
        options: DownloadOptions,
        *,
        download: bool,
        force_generic: bool = False,
    ):
        yt_dlp = self._load_yt_dlp()
        logger = YtdlpLogger()
        ydl_opts = self.build_ydl_options(
            options,
            download=download,
            logger=logger,
            url=url,
            force_generic=force_generic,
        )
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=download)
        return info, logger.dump()

    def _download_with_ytdlp(self, url: str, options: DownloadOptions) -> Tuple[str, str]:
        yt_dlp = self._load_yt_dlp()
        progress_state = {"filename": ""}
        logger = YtdlpLogger()
        ydl_opts = self.build_ydl_options(
            options,
            download=True,
            progress_callback=lambda info: self._remember_progress(info, progress_state),
            logger=logger,
            url=url,
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        path = self._find_download_path(info, progress_state.get("filename", ""))
        return path, logger.dump()

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

        if info.get("_type") in {"playlist", "multi_video"} and info.get("entries"):
            first = next((entry for entry in info.get("entries") or [] if entry), None)
            if first:
                info = first

        formats = self._formats_from_info(info)
        requires_ffmpeg = self._requires_ffmpeg(info, formats)
        warning = ""
        ffmpeg_available = bool(self.get_ffmpeg_path())
        if requires_ffmpeg and not ffmpeg_available:
            warning = "当前未检测到 ffmpeg，高清合并或 HLS/DASH 下载可能失败。"

        restriction_status = ""
        title = info.get("title") or "未命名视频"
        if self._contains_any(title.lower(), self.RESTRICTED_PATTERNS):
            restriction_status = "视频标题提示资源可能受版权、付费或加密限制。"

        metadata = VideoMetadata(
            title=title,
            webpage_url=info.get("webpage_url") or original_url,
            extractor=(info.get("extractor") or info.get("ie_key") or config.resolve_platform(url=original_url)).lower(),
            duration=info.get("duration") or 0,
            thumbnail=info.get("thumbnail") or "",
            uploader=info.get("uploader") or info.get("channel") or "未知",
            formats=formats,
            requires_ffmpeg=requires_ffmpeg,
            ffmpeg_available=ffmpeg_available,
            requires_login=False,
            restriction_status=restriction_status,
            warning=warning,
            source_url=original_url,
            candidates=self._candidates_from_formats(formats, original_url, "yt-dlp"),
            attempts=list(attempts),
            cookie_status=cookie_status,
            diagnostic_log=diagnostic_log,
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
                )
            )
        if not candidates and fallback_url:
            candidates.append(
                MediaCandidate(
                    url=fallback_url,
                    source=source,
                    ext=self._extension(fallback_url),
                )
            )
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
        return self._metadata_from_candidate_url(
            page_url=url,
            media_url=urls[0],
            candidate_urls=urls,
            options=options,
            cookie_status=cookie_status.value,
            strategy="dynamic-scan-downloadable",
        )

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

    def _candidates_from_candidate_urls(self, urls: Iterable[str], source: str) -> List[MediaCandidate]:
        candidates: List[MediaCandidate] = []
        seen = set()
        for value in urls:
            url = str(value or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            ext = self._extension(url)
            candidates.append(
                MediaCandidate(
                    url=url,
                    source=source,
                    ext=ext,
                    protocol=ext,
                )
            )
        return candidates

    def _select_candidate_url(self, metadata: VideoMetadata) -> str:
        if metadata.candidates:
            return metadata.candidates[0].url
        return ""

    def _build_diagnostic_summary(self, metadata: VideoMetadata) -> str:
        lines = [
            f"Cookie 状态: {metadata.cookie_status}",
            f"ffmpeg: {'available' if metadata.ffmpeg_available else 'missing'}",
            f"尝试次数: {len(metadata.attempts)}",
            f"最终来源: {metadata.source_url or metadata.webpage_url}",
        ]
        for attempt in metadata.attempts:
            outcome = "success" if attempt.success else attempt.category or "failed"
            lines.append(f"{attempt.strategy}: {outcome} - {attempt.summary}")
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

    def _cookie_spec(self, browser: str) -> Optional[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
        normalized = self._normalize_cookie_browser(browser)
        if normalized == "none":
            return None
        return (normalized, None, None, None)

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
        try:
            import yt_dlp  # type: ignore
        except ModuleNotFoundError as exc:
            raise self.classify_error(exc) from exc
        return yt_dlp

    def _contains_any(self, text: str, patterns: Iterable[str]) -> bool:
        return any(pattern in text for pattern in patterns)
