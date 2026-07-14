"""Qt thread wrappers for parse and download tasks."""
from __future__ import annotations

from typing import Dict

from PySide6.QtCore import QObject, QThread, Signal

from downloader.diagnostics import redact_url
from downloader.service import (
    DownloadOptions,
    DownloadService,
    DownloadServiceError,
    VideoMetadata,
)
from downloader.runtime import RuntimeManager


class DownloadSignals(QObject):
    """Download task signals."""

    started = Signal(dict)
    progress = Signal(dict)
    finished = Signal(dict)
    error = Signal(dict)


class DownloadTask(QThread):
    """Run a blocking yt-dlp download outside the UI thread."""

    def __init__(
        self,
        task_id: str,
        url: str,
        output_dir: str,
        quality: str,
        cookie_browser: str = "none",
        cookie_profile: str = "",
        concurrent_fragments: int = 4,
        download_playlist: bool = False,
        overwrite: bool = False,
        format_id: str = "",
        diagnostic_mode: bool = True,
        dynamic_scan_enabled: bool = True,
        request_delay_seconds: float = 0.0,
        candidate_url: str = "",
        candidate_ref: str = "",
        browser_assist_mode: str = "fallback",
        live_start: str = "current",
        wait_for_live: bool = False,
        playlist_items: list[str] | None = None,
    ):
        super().__init__()
        self.task_id = task_id
        self.url = url
        self.options = DownloadOptions(
            quality=quality,
            cookie_browser=cookie_browser,
            cookie_profile=cookie_profile,
            concurrent_fragments=concurrent_fragments,
            output_dir=output_dir,
            download_playlist=download_playlist,
            overwrite=overwrite,
            format_id=format_id,
            diagnostic_mode=diagnostic_mode,
            dynamic_scan_enabled=dynamic_scan_enabled,
            request_delay_seconds=request_delay_seconds,
            candidate_url=candidate_url,
            candidate_ref=candidate_ref,
            browser_assist_mode=browser_assist_mode,
            live_start=live_start,
            wait_for_live=wait_for_live,
            playlist_items=list(playlist_items or []),
        )
        self.signals = DownloadSignals()
        self.service = DownloadService()

    def run(self):
        self.signals.started.emit({"task_id": self.task_id, "url": self.url})

        try:
            result = self.service.download(
                self.url,
                self.options,
                progress_callback=self._emit_progress,
            )
            self.signals.finished.emit(
                {
                    "task_id": self.task_id,
                    "path": result.path,
                    "message": result.message,
                    "attempts": [
                        {
                            "strategy": item.strategy,
                            "success": item.success,
                            "category": item.category,
                            "summary": item.summary,
                            "extractor": item.extractor,
                            "candidate_count": item.candidate_count,
                        }
                        for item in result.attempts
                    ],
                    "diagnostic_summary": result.diagnostic_summary,
                }
            )
        except DownloadServiceError as exc:
            self.signals.error.emit(
                {
                    "task_id": self.task_id,
                    "error": exc.message,
                    "category": exc.category.value,
                }
            )
        except Exception as exc:
            self.signals.error.emit(
                {
                    "task_id": self.task_id,
                    "error": str(exc),
                    "category": "unknown",
                }
            )

    def cancel(self):
        self.service.cancel()

    def _emit_progress(self, info: Dict):
        self.signals.progress.emit({"task_id": self.task_id, **info})


class ParseTask(QThread):
    """Run URL parsing outside the UI thread."""

    class Signals(QObject):
        finished = Signal(dict)
        error = Signal(str)

    def __init__(
        self,
        url: str,
        cookie_browser: str = "none",
        cookie_profile: str = "",
        concurrent_fragments: int = 4,
        download_playlist: bool = False,
        diagnostic_mode: bool = True,
        dynamic_scan_enabled: bool = True,
        request_delay_seconds: float = 0.0,
        browser_assist_mode: str = "fallback",
    ):
        super().__init__()
        self.url = url
        self.options = DownloadOptions(
            cookie_browser=cookie_browser,
            cookie_profile=cookie_profile,
            concurrent_fragments=concurrent_fragments,
            download_playlist=download_playlist,
            diagnostic_mode=diagnostic_mode,
            dynamic_scan_enabled=dynamic_scan_enabled,
            request_delay_seconds=request_delay_seconds,
            browser_assist_mode=browser_assist_mode,
        )
        self.signals = self.Signals()
        self.service = DownloadService()

    def run(self):
        try:
            metadata = self.service.parse(self.url, self.options)
            self.signals.finished.emit(self._serialize_metadata(metadata))
        except DownloadServiceError as exc:
            self.signals.error.emit(exc.message)
        except Exception as exc:
            self.signals.error.emit(str(exc))

    def _serialize_metadata(self, metadata: VideoMetadata) -> Dict:
        return {
            "title": metadata.title,
            "duration": metadata.duration,
            "duration_str": metadata.duration_str,
            "thumbnail": redact_url(metadata.thumbnail),
            "uploader": metadata.uploader,
            "extractor": metadata.extractor,
            "webpage_url": redact_url(metadata.webpage_url),
            "source_url": redact_url(metadata.source_url or metadata.webpage_url),
            "requires_ffmpeg": metadata.requires_ffmpeg,
            "ffmpeg_available": metadata.ffmpeg_available,
            "requires_login": metadata.requires_login,
            "restriction_status": metadata.restriction_status,
            "warning": metadata.warning,
            "cookie_status": metadata.cookie_status,
            "diagnostic_summary": metadata.diagnostic_summary,
            "diagnostic_log": metadata.diagnostic_log,
            "resource_kind": metadata.resource_kind,
            "is_live": metadata.is_live,
            "live_status": metadata.live_status,
            "playlist_count": metadata.playlist_count,
            "playlist_entries": [
                {
                    "index": index + 1,
                    "entry_id": item.entry_id,
                    "title": item.title,
                    "webpage_url": redact_url(item.webpage_url),
                    "duration": item.duration,
                    "is_live": item.is_live,
                }
                for index, item in enumerate(metadata.playlist_entries)
            ],
            "attempts": [
                {
                    "strategy": item.strategy,
                    "success": item.success,
                    "category": item.category,
                    "summary": item.summary,
                    "extractor": item.extractor,
                    "candidate_count": item.candidate_count,
                }
                for item in metadata.attempts
            ],
            "candidates": [
                {
                    "url": redact_url(item.url),
                    "candidate_ref": item.candidate_ref,
                    "source": item.source,
                    "ext": item.ext,
                    "protocol": item.protocol,
                    "mime_type": item.mime_type,
                    "media_role": item.media_role,
                    "validation_status": item.validation_status,
                    "expires_at": item.expires_at,
                    "score": item.score,
                    "http_status": item.http_status,
                    "display_name": item.display_name,
                }
                for item in metadata.candidates
            ],
            "formats": [
                {
                    "format_id": item.format_id,
                    "display_name": item.display_name,
                    "filesize": item.filesize,
                    "height": item.height,
                    "ext": item.ext,
                    "resolution": item.resolution,
                    "protocol": item.protocol,
                    "vcodec": item.vcodec,
                    "acodec": item.acodec,
                    "download_url": redact_url(item.download_url),
                }
                for item in metadata.formats
            ],
        }


class RuntimeTask(QThread):
    class Signals(QObject):
        finished = Signal(dict)
        progress = Signal(dict)
        error = Signal(str)

    def __init__(self, action: str):
        super().__init__()
        self.action = action
        self.signals = self.Signals()
        self.manager = RuntimeManager()

    def run(self):
        try:
            if self.action == "check":
                self.signals.progress.emit(
                    {
                        "phase": "check",
                        "message": "正在检查运行时更新...",
                        "indeterminate": True,
                    }
                )
                statuses = self.manager.check_updates()
            elif self.action == "install":
                statuses = self._install_updates()
            else:
                statuses = self.manager.statuses()
            self.signals.finished.emit(
                {
                    name: {
                        "name": item.name,
                        "version": item.version,
                        "path": item.path,
                        "installed": item.installed,
                        "healthy": item.healthy,
                        "managed": item.managed,
                        "message": item.message,
                        "update_available": item.update_available,
                    }
                    for name, item in statuses.items()
                }
            )
        except Exception as exc:
            self.signals.error.emit(str(exc))

    def _install_updates(self):
        statuses = self.manager.check_updates()
        pending = [
            name
            for name in self.manager.COMPONENTS
            if statuses[name].update_available
            or not statuses[name].managed
            or not statuses[name].healthy
        ]
        if not pending:
            self.signals.progress.emit(
                {
                    "phase": "complete",
                    "message": "运行时已是最新版本",
                    "percent": 100,
                    "indeterminate": False,
                }
            )
            return statuses

        component_total = len(pending)
        for component_index, component in enumerate(pending):
            self.signals.progress.emit(
                {
                    "phase": "prepare",
                    "component": component,
                    "message": f"正在准备 {component}...",
                    "percent": int(component_index * 100 / component_total),
                    "indeterminate": True,
                }
            )
            release = self.manager.latest_release(component)

            def report(name, completed, total, index=component_index):
                component_fraction = (completed / total) if total else 0.0
                overall = ((index + component_fraction) / component_total) * 100
                self.signals.progress.emit(
                    {
                        "phase": "download",
                        "component": name,
                        "message": f"正在下载 {name}",
                        "downloaded": completed,
                        "total": total,
                        "percent": int(max(0, min(100, overall))),
                        "indeterminate": not bool(total),
                    }
                )

            self.manager.install(component, release=release, progress=report)
            self.signals.progress.emit(
                {
                    "phase": "activate",
                    "component": component,
                    "message": f"{component} 已校验并激活",
                    "percent": int((component_index + 1) * 100 / component_total),
                    "indeterminate": False,
                }
            )
        return self.manager.statuses()
