"""Unit tests for the yt-dlp-first download service."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader.service import (  # noqa: E402
    CookieStatus,
    DownloadCategory,
    DownloadOptions,
    DownloadService,
    DynamicMediaScanner,
    DownloadServiceError,
    HtmlMediaScanner,
    ParseAttempt,
    VideoFormat,
)


def test_cookie_options_are_generated_for_supported_browser():
    service = DownloadService()
    opts = service.build_ydl_options(
        DownloadOptions(cookie_browser="chrome"),
        download=False,
    )
    assert opts["cookiesfrombrowser"][0] == "chrome"


def test_cookie_options_are_omitted_when_disabled():
    service = DownloadService()
    opts = service.build_ydl_options(
        DownloadOptions(cookie_browser="none"),
        download=False,
    )
    assert "cookiesfrombrowser" not in opts


def test_platform_profile_adds_request_delay():
    service = DownloadService()
    opts = service.build_ydl_options(
        DownloadOptions(request_delay_seconds=0.1),
        download=False,
        url="https://www.bilibili.com/video/BV1xx",
    )
    assert opts["sleep_interval_requests"] == 0.5


def test_format_selector_prefers_merge_when_ffmpeg_exists():
    service = DownloadService()
    selector = service._format_selector(DownloadOptions(quality="720P"), has_ffmpeg=True)
    assert selector == "bestvideo[height<=720]+bestaudio/best[height<=720]/best"


def test_format_selector_prefers_single_file_without_ffmpeg():
    service = DownloadService()
    selector = service._format_selector(DownloadOptions(quality="最佳"), has_ffmpeg=False)
    assert selector == "best[vcodec!=none][acodec!=none]/best"


def test_error_classification_for_auth_without_cookie():
    service = DownloadService()
    error = service.classify_error(Exception("Sign in to confirm your age"))
    assert error.category == DownloadCategory.AUTH_REQUIRED


def test_error_classification_for_auth_with_cookie():
    service = DownloadService()
    error = service.classify_error(
        Exception("HTTP Error 403: Forbidden"),
        DownloadOptions(cookie_browser="edge"),
    )
    assert error.category == DownloadCategory.COOKIE_ERROR


def test_error_classification_for_restricted_content():
    service = DownloadService()
    error = service.classify_error(Exception("This video is DRM protected"))
    assert error.category == DownloadCategory.RESTRICTED


def test_html_scanner_extracts_and_prioritizes_media_urls():
    scanner = HtmlMediaScanner()
    urls = scanner._extract_urls(
        """
        <video poster="/poster.jpg">
          <source src="/videos/file.mp4">
        </video>
        <script>window.play = "https://cdn.example.com/live/master.m3u8?token=1";</script>
        """,
        "https://example.com/watch/1",
    )
    assert urls[0] == "https://cdn.example.com/live/master.m3u8?token=1"
    assert "https://example.com/videos/file.mp4" in urls


def test_requires_ffmpeg_for_separate_audio_and_video():
    service = DownloadService()
    formats = [
        VideoFormat(format_id="v", vcodec="h264", acodec="none"),
        VideoFormat(format_id="a", vcodec="none", acodec="aac"),
    ]
    assert service._requires_ffmpeg({}, formats) is True


def test_clean_url_rejects_empty_input():
    service = DownloadService()
    try:
        service.clean_url("   ")
    except DownloadServiceError as exc:
        assert exc.category == DownloadCategory.UNSUPPORTED
    else:
        raise AssertionError("Expected DownloadServiceError")


def test_cookie_status_detects_disabled():
    service = DownloadService()
    assert service._detect_cookie_status("none") == CookieStatus.DISABLED


def test_diagnostic_summary_contains_attempts():
    service = DownloadService()
    attempts = [
        ParseAttempt(strategy="yt-dlp", success=False, category="network", summary="网络超时"),
        ParseAttempt(strategy="html-scan", success=True, summary="扫描到 1 个候选资源"),
    ]
    metadata = service._metadata_from_candidate_url(
        page_url="https://example.com/post/1",
        media_url="https://cdn.example.com/file.mp4",
        candidate_urls=["https://cdn.example.com/file.mp4"],
        options=DownloadOptions(),
        cookie_status="available",
        strategy="html-scan-downloadable",
    )
    metadata.attempts = attempts
    summary = service._build_diagnostic_summary(metadata)
    assert "Cookie 状态: available" in summary
    assert "yt-dlp: network - 网络超时" in summary


def test_dynamic_scanner_filters_duplicates():
    scanner = DynamicMediaScanner()
    urls = scanner._filter_urls(
        [
            "https://cdn.example.com/a.mp4",
            "https://cdn.example.com/a.mp4",
            "https://cdn.example.com/live/master.m3u8?token=1",
            "https://cdn.example.com/image.jpg",
        ]
    )
    assert urls == [
        "https://cdn.example.com/a.mp4",
        "https://cdn.example.com/live/master.m3u8?token=1",
    ]


def test_dynamic_scanner_parse_output():
    scanner = DynamicMediaScanner()
    urls = scanner._parse_output("https://a.test/v.mp4\n\nhttps://b.test/live.m3u8")
    assert urls == ["https://a.test/v.mp4", "https://b.test/live.m3u8"]


def test_dynamic_scanner_summary_after_filter():
    scanner = DynamicMediaScanner()
    urls = scanner._filter_urls(["https://a.test/v.mp4"])
    assert urls == ["https://a.test/v.mp4"]


def test_candidates_from_candidate_urls_keeps_order():
    service = DownloadService()
    candidates = service._candidates_from_candidate_urls(
        [
            "https://cdn.example.com/live/master.m3u8",
            "https://cdn.example.com/video.mp4",
        ],
        "dynamic-scan",
    )
    assert [item.url for item in candidates] == [
        "https://cdn.example.com/live/master.m3u8",
        "https://cdn.example.com/video.mp4",
    ]


def test_select_candidate_url_prefers_first_candidate():
    service = DownloadService()
    metadata = service._metadata_from_candidate_url(
        page_url="https://example.com/post/1",
        media_url="https://cdn.example.com/live/master.m3u8",
        candidate_urls=[
            "https://cdn.example.com/live/master.m3u8",
            "https://cdn.example.com/video.mp4",
        ],
        options=DownloadOptions(),
        cookie_status="available",
        strategy="dynamic-scan-downloadable",
    )
    assert service._select_candidate_url(metadata) == "https://cdn.example.com/live/master.m3u8"


def test_candidates_from_formats_use_format_urls():
    service = DownloadService()
    formats = [
        VideoFormat(format_id="1", ext="mp4", protocol="https", download_url="https://cdn.example.com/a.mp4"),
        VideoFormat(format_id="2", ext="mp4", protocol="https", download_url="https://cdn.example.com/b.mp4"),
    ]
    candidates = service._candidates_from_formats(formats, "https://fallback.test/file.mp4", "yt-dlp")
    assert [item.url for item in candidates] == [
        "https://cdn.example.com/a.mp4",
        "https://cdn.example.com/b.mp4",
    ]
