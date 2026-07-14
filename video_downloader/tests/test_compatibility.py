"""Compatibility tests for runtime, request replay, playlists and diagnostics."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader.diagnostics import redact_headers, redact_text, redact_url  # noqa: E402
from downloader.browser import BrowserCapture, BrowserSessionManager  # noqa: E402
from downloader.service import (  # noqa: E402
    DownloadOptions,
    DownloadService,
    HtmlMediaScanner,
    MediaCandidate,
    RequestContext,
    ResourceKind,
    ValidationStatus,
    _REQUEST_CONTEXTS,
)
from downloader.runtime import RuntimeManager  # noqa: E402
from ui.threads import RuntimeTask  # noqa: E402


class RuntimeStub:
    yt_dlp_path = r"C:\runtime\yt-dlp.exe"
    ffmpeg_path = r"C:\runtime\ffmpeg.exe"
    deno_path = r"C:\runtime\deno.exe"
    ejs_plugin_dir = r"C:\runtime\plugins"


class ProtectedMediaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/public.mp4"):
            payload = b"\x00\x00\x00\x18ftypmp42" + b"0" * 128
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/protected"):
            if self.headers.get("Referer") != "https://page.example/watch/1":
                self.send_error(403)
                return
            if self.headers.get("Cookie") != "session=local-only":
                self.send_error(403)
                return
            self.send_response(206)
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            self.wfile.write(b"\x00\x00\x00\x18ftypmp42")
            return
        self.send_error(404)

    def log_message(self, format, *args):
        return


def test_html_scanner_accepts_extensionless_source_url():
    scanner = HtmlMediaScanner()
    urls = scanner._extract_urls(
        '<video><source src="/api/media?id=123"></video>',
        "https://example.com/watch/1",
    )
    assert urls == ["https://example.com/api/media?id=123"]


def test_candidate_validation_replays_private_request_context():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProtectedMediaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        context_id = _REQUEST_CONTEXTS.put(
            RequestContext(
                headers={"Cookie": "session=local-only"},
                referer="https://page.example/watch/1",
            )
        )
        candidate = MediaCandidate(
            url=f"http://127.0.0.1:{server.server_port}/protected?token=secret",
            source="test",
            request_context_id=context_id,
        )
        result = DownloadService(runtime_manager=RuntimeStub())._validate_candidate(candidate)
        assert result.validation_status == ValidationStatus.VALID.value
        assert result.mime_type == "video/mp4"
        assert result.ext == "mp4"
        assert result.http_status == 206
    finally:
        server.shutdown()
        server.server_close()


def test_ytdlp_command_uses_standalone_runtime_and_live_playlist_options(tmp_path):
    service = DownloadService(runtime_manager=RuntimeStub())
    options = DownloadOptions(
        output_dir=tmp_path,
        download_playlist=True,
        playlist_items=["2", "4-5"],
        live_start="beginning",
        wait_for_live=True,
    )
    command = service._build_ytdlp_command("https://example.com/watch", options, download=True)
    assert command[0] == RuntimeStub.yt_dlp_path
    assert "--live-from-start" in command
    assert command[command.index("--playlist-items") + 1] == "2,4-5"
    assert command[command.index("--wait-for-video") + 1] == "30"
    assert command[command.index("--js-runtimes") + 1] == f"deno:{RuntimeStub.deno_path}"


def test_playlist_metadata_keeps_every_entry():
    info = {
        "_type": "playlist",
        "title": "Series",
        "webpage_url": "https://example.com/list/1",
        "entries": [
            {"id": "a", "title": "Part A", "url": "https://example.com/a", "duration": 10},
            {"id": "b", "title": "Part B", "url": "https://example.com/b", "duration": 20},
        ],
    }
    metadata = DownloadService(runtime_manager=RuntimeStub())._metadata_from_info(
        info,
        info["webpage_url"],
        attempts=[],
        cookie_status="disabled",
        diagnostic_log="",
    )
    assert metadata.resource_kind == ResourceKind.PLAYLIST.value
    assert metadata.playlist_count == 2
    assert [entry.title for entry in metadata.playlist_entries] == ["Part A", "Part B"]


def test_sensitive_diagnostics_are_redacted():
    url = "https://cdn.example/video?token=secret&quality=1080"
    assert "secret" not in redact_url(url)
    assert "1080" in redact_url(url)
    assert redact_headers({"Cookie": "session=secret", "Referer": url})["Cookie"] == "[REDACTED]"
    text = redact_text(f"Authorization: Bearer abc123\nGET {url}")
    assert "abc123" not in text
    assert "secret" not in text


def test_candidate_display_data_can_be_json_serialized_without_credentials():
    candidate = {
        "url": redact_url("https://cdn.example/video?signature=private"),
        "candidate_ref": "opaque-local-reference",
    }
    payload = json.dumps(candidate)
    assert "private" not in payload
    assert "opaque-local-reference" in payload


def test_real_standalone_ytdlp_process_returns_structured_json():
    runtime = RuntimeManager()
    if not runtime.yt_dlp_path:
        pytest.skip("standalone yt-dlp executable is not installed")
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProtectedMediaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/public.mp4"
        info, _ = DownloadService(runtime_manager=runtime)._extract_info(
            url,
            DownloadOptions(dynamic_scan_enabled=False),
            download=False,
        )
        assert info["url"] == url
        assert info["ext"] == "mp4"
    finally:
        server.shutdown()
        server.server_close()


def test_browser_candidate_sorting_prefers_master_and_filters_segments():
    captures = [
        BrowserCapture(
            url="https://cdn.example/chunk-001.ts",
            mime_type="video/mp2t",
            resource_type="Media",
            status=200,
        ),
        BrowserCapture(
            url="https://cdn.example/master.m3u8",
            mime_type="application/vnd.apple.mpegurl",
            resource_type="XHR",
            status=200,
        ),
    ]
    service = DownloadService(runtime_manager=RuntimeStub())
    candidates = service._candidates_from_browser_captures(captures, "edge-cdp")
    assert [item.url for item in candidates] == ["https://cdn.example/master.m3u8"]


def test_browser_session_marks_license_requests_as_drm(tmp_path):
    manager = BrowserSessionManager(profile_dir=tmp_path / "profile")
    capture = BrowserCapture(
        url="https://license.example/widevine",
        resource_type="XHR",
        is_drm=True,
    )
    assert manager._is_media_capture(capture) is True


def test_runtime_status_rolls_back_unhealthy_managed_version(tmp_path):
    manager = RuntimeManager(base_dir=tmp_path)
    current = tmp_path / "yt-dlp" / "new" / "yt-dlp.exe"
    previous = tmp_path / "yt-dlp" / "old" / "yt-dlp.exe"
    current.parent.mkdir(parents=True)
    previous.parent.mkdir(parents=True)
    current.write_bytes(b"new")
    previous.write_bytes(b"old")
    manager._write_registry(
        {
            "yt-dlp": {
                "version": "2",
                "path": str(current),
                "previous": {"version": "1", "path": str(previous), "previous": {}},
            }
        }
    )

    def fake_health(component, path=None):
        return (False, "2", "broken") if Path(path) == current else (True, "1", "正常")

    manager.health_check = fake_health
    status = manager.status("yt-dlp")
    assert status.healthy is True
    assert status.version == "1"
    assert manager._registry()["yt-dlp"]["path"] == str(previous)


def test_runtime_checksum_parser_accepts_powershell_hash_format(tmp_path):
    manager = RuntimeManager(base_dir=tmp_path)
    expected = "A" * 64
    manager._read_text = lambda url: (
        "Algorithm : SHA256\r\n"
        f"Hash      : {expected}\r\n"
        "Path      : C:\\build\\deno.zip\r\n"
    )
    assert manager._expected_checksum("https://example/checksum", "deno.zip") == expected


def test_runtime_task_installs_available_update_and_reports_progress():
    class FakeRuntimeManager:
        COMPONENTS = ("yt-dlp",)

        def __init__(self):
            self.installed = []
            self.updated = False

        def check_updates(self):
            return {
                "yt-dlp": SimpleNamespace(
                    update_available="2026.07.04",
                    managed=True,
                    healthy=True,
                )
            }

        def latest_release(self, component):
            return SimpleNamespace(component=component, version="2026.07.04")

        def install(self, component, release, progress):
            self.installed.append((component, release.version))
            progress(component, 50, 100)
            progress(component, 100, 100)
            self.updated = True

        def statuses(self):
            return {
                "yt-dlp": SimpleNamespace(
                    update_available="",
                    managed=True,
                    healthy=True,
                )
            }

    task = RuntimeTask("install")
    task.manager = FakeRuntimeManager()
    events = []
    task.signals.progress.connect(events.append)
    task._install_updates()

    assert task.manager.installed == [("yt-dlp", "2026.07.04")]
    assert any(item.get("phase") == "download" and item.get("percent") == 50 for item in events)
    assert events[-1]["phase"] == "activate"
    assert events[-1]["percent"] == 100
