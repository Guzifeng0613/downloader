"""Dedicated Edge profile and CDP media request capture."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from config import config


@dataclass
class BrowserCapture:
    url: str
    mime_type: str = ""
    resource_type: str = ""
    method: str = "GET"
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    status: int = 0
    is_drm: bool = False


@dataclass
class BrowserSessionStatus:
    edge_available: bool
    profile_path: str
    profile_exists: bool
    cookie_store_exists: bool
    active: bool
    message: str


class BrowserSessionError(Exception):
    pass


class BrowserSessionManager:
    """Own a persistent application-only Edge profile and capture session."""

    MEDIA_MIME_MARKERS = (
        "video/",
        "audio/",
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "application/dash+xml",
        "application/octet-stream",
    )
    MEDIA_RESOURCE_TYPES = {"Media", "XHR", "Fetch"}
    DRM_MARKERS = ("widevine", "playready", "fairplay", "license", "drm")

    def __init__(self, profile_dir: str | Path | None = None):
        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        self.profile_dir = (
            Path(profile_dir)
            if profile_dir
            else Path(local_app_data) / "VideoDownloader" / "EdgeProfile"
        )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._process: subprocess.Popen | None = None
        self._socket: Any = None
        self._session_ref = ""

    @property
    def session_ref(self) -> str:
        return self._session_ref

    def status(self) -> BrowserSessionStatus:
        edge = config.get_browser_executable("edge")
        cookie_paths = (
            self.profile_dir / "Default" / "Network" / "Cookies",
            self.profile_dir / "Default" / "Cookies",
        )
        cookies = any(path.exists() for path in cookie_paths)
        if not edge:
            message = "未检测到 Microsoft Edge"
        elif cookies:
            message = "专用 Edge 资料档案已登录或已有 Cookie"
        else:
            message = "专用 Edge 资料档案尚未登录"
        return BrowserSessionStatus(
            edge_available=bool(edge),
            profile_path=str(self.profile_dir),
            profile_exists=self.profile_dir.exists(),
            cookie_store_exists=cookies,
            active=bool(self._process and self._process.poll() is None),
            message=message,
        )

    def open_login(self, url: str = "https://www.microsoft.com/") -> None:
        """Open a visible Edge window; the user closes it after login/verification."""
        self.close()
        edge = config.get_browser_executable("edge")
        if not edge:
            raise BrowserSessionError("未检测到 Microsoft Edge。")
        self._process = subprocess.Popen(
            [
                edge,
                f"--user-data-dir={self.profile_dir}",
                "--profile-directory=Default",
                "--no-first-run",
                "--no-default-browser-check",
                url,
            ],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

    def capture(
        self,
        url: str,
        *,
        timeout: int = 45,
        visible: bool = True,
        trigger_playback: bool = True,
    ) -> tuple[list[BrowserCapture], dict[str, str], str]:
        """Capture media-ish requests through CDP using the dedicated profile."""
        try:
            import websocket
        except ModuleNotFoundError as exc:
            raise BrowserSessionError("缺少 websocket-client，无法连接 Edge 调试协议。") from exc

        self.close()
        edge = config.get_browser_executable("edge")
        if not edge:
            raise BrowserSessionError("未检测到 Microsoft Edge。")
        port = self._free_port()
        args = [
            edge,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if not visible:
            args.append("--headless=new")
        args.append(url)
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self._session_ref = f"edge:{int(time.time())}:{port}"
        deadline = time.monotonic() + max(5, timeout)
        endpoint = self._wait_for_page(port, deadline)
        if not endpoint:
            self.close()
            raise BrowserSessionError("Edge 调试端口启动超时。")

        ws = websocket.create_connection(endpoint, timeout=1, origin=f"http://127.0.0.1:{port}")
        self._socket = ws
        commands = 0

        def send(method: str, params: dict | None = None) -> int:
            nonlocal commands
            commands += 1
            ws.send(json.dumps({"id": commands, "method": method, "params": params or {}}))
            return commands

        send("Network.enable", {"maxTotalBufferSize": 100000000, "maxResourceBufferSize": 10000000})
        send("Page.enable")
        send("Runtime.enable")
        send("ServiceWorker.enable")
        send("Network.setCacheDisabled", {"cacheDisabled": True})
        send("Page.navigate", {"url": url})

        requests: dict[str, BrowserCapture] = {}
        extra_headers: dict[str, dict[str, str]] = {}
        all_cookies: list[dict] = []
        playback_triggered = False
        cookie_command = 0

        try:
            while time.monotonic() < deadline and self._process.poll() is None:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if trigger_playback and not playback_triggered:
                        self._trigger_native_playback(send)
                        playback_triggered = True
                    continue
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                method = message.get("method")
                params = message.get("params") or {}
                if method == "Network.requestWillBeSent":
                    request = params.get("request") or {}
                    request_id = str(params.get("requestId") or "")
                    requests[request_id] = BrowserCapture(
                        url=str(request.get("url") or ""),
                        method=str(request.get("method") or "GET"),
                        resource_type=str(params.get("type") or ""),
                        request_headers=self._string_headers(request.get("headers")),
                        is_drm=self._looks_like_drm(str(request.get("url") or "")),
                    )
                elif method == "Network.requestWillBeSentExtraInfo":
                    extra_headers[str(params.get("requestId") or "")] = self._string_headers(params.get("headers"))
                elif method == "Network.responseReceived":
                    request_id = str(params.get("requestId") or "")
                    response = params.get("response") or {}
                    item = requests.setdefault(request_id, BrowserCapture(url=str(response.get("url") or "")))
                    item.url = str(response.get("url") or item.url)
                    item.mime_type = str(response.get("mimeType") or "").lower()
                    item.resource_type = str(params.get("type") or item.resource_type)
                    item.response_headers = self._string_headers(response.get("headers"))
                    item.status = int(response.get("status") or 0)
                    item.is_drm = item.is_drm or self._looks_like_drm(item.url)
                elif method == "Page.loadEventFired" and trigger_playback and not playback_triggered:
                    self._trigger_native_playback(send)
                    playback_triggered = True
                if time.monotonic() + 1 >= deadline and not cookie_command:
                    cookie_command = send("Network.getAllCookies")
                if cookie_command and message.get("id") == cookie_command:
                    all_cookies = ((message.get("result") or {}).get("cookies") or [])
            if not cookie_command:
                cookie_command = send("Network.getAllCookies")
                cookie_deadline = time.monotonic() + 2
                while time.monotonic() < cookie_deadline:
                    try:
                        message = json.loads(ws.recv())
                    except Exception:
                        break
                    if message.get("id") == cookie_command:
                        all_cookies = ((message.get("result") or {}).get("cookies") or [])
                        break
        finally:
            for request_id, headers in extra_headers.items():
                if request_id in requests:
                    requests[request_id].request_headers.update(headers)

        captures = [item for item in requests.values() if self._is_media_capture(item)]
        captures = self._deduplicate(captures)
        cookie_header = self._cookies_for_url(all_cookies, url)
        context_headers = {"User-Agent": config.USER_AGENT, "Referer": url}
        if cookie_header:
            context_headers["Cookie"] = cookie_header
        return captures, context_headers, self._session_ref

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        if self._process and self._process.poll() is None:
            self._terminate_tree(self._process)
        self._process = None

    def _wait_for_page(self, port: int, deadline: float) -> str:
        while time.monotonic() < deadline:
            try:
                request = Request(f"http://127.0.0.1:{port}/json/list")
                with urlopen(request, timeout=1) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                page = next((item for item in targets if item.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    return str(page["webSocketDebuggerUrl"])
            except Exception:
                time.sleep(0.2)
        return ""

    def _trigger_native_playback(self, send) -> None:
        expression = """
        (() => {
          const videos = [...document.querySelectorAll('video')];
          for (const video of videos) {
            video.muted = true;
            video.play().catch(() => {});
          }
          const button = document.querySelector('[aria-label*=play i], [class*=play i], button');
          if (button) button.click();
          return videos.length;
        })()
        """
        send("Runtime.evaluate", {"expression": expression, "userGesture": True})

    def _is_media_capture(self, item: BrowserCapture) -> bool:
        if not item.url or item.url.startswith("blob:"):
            return False
        if item.is_drm:
            return True
        mime = item.mime_type.lower()
        if "application/octet-stream" in mime:
            path = urlsplit(item.url).path.lower()
            return item.resource_type in self.MEDIA_RESOURCE_TYPES and any(
                marker in path for marker in ("media", "stream", "video", "audio", "manifest")
            )
        if any(marker in mime for marker in self.MEDIA_MIME_MARKERS):
            return True
        if item.resource_type in self.MEDIA_RESOURCE_TYPES:
            path = urlsplit(item.url).path.lower()
            return any(marker in path for marker in ("m3u8", "mpd", "manifest", "playurl", "stream", "video"))
        return False

    def _looks_like_drm(self, url: str) -> bool:
        lower = (url or "").lower()
        return any(marker in lower for marker in self.DRM_MARKERS)

    def _deduplicate(self, captures: list[BrowserCapture]) -> list[BrowserCapture]:
        result: list[BrowserCapture] = []
        seen = set()
        for item in captures:
            if item.url in seen:
                continue
            seen.add(item.url)
            result.append(item)
        return result

    def _cookies_for_url(self, cookies: list[dict], url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        values = []
        for cookie in cookies:
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if domain and (host == domain or host.endswith(f".{domain}")):
                values.append(f"{cookie.get('name', '')}={cookie.get('value', '')}")
        return "; ".join(values)

    def _string_headers(self, value: dict | None) -> dict[str, str]:
        return {str(key): str(item) for key, item in (value or {}).items()}

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _terminate_tree(self, process: subprocess.Popen) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
