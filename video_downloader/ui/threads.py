"""Qt thread wrappers for parse and download tasks."""
from __future__ import annotations

from typing import Dict

from PySide6.QtCore import QObject, QThread, Signal

from downloader.service import (
    DownloadOptions,
    DownloadService,
    DownloadServiceError,
    VideoMetadata,
)


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
        concurrent_fragments: int = 4,
        download_playlist: bool = False,
        overwrite: bool = False,
        format_id: str = "",
        diagnostic_mode: bool = True,
        dynamic_scan_enabled: bool = True,
        request_delay_seconds: float = 0.0,
        candidate_url: str = "",
    ):
        super().__init__()
        self.task_id = task_id
        self.url = url
        self.options = DownloadOptions(
            quality=quality,
            cookie_browser=cookie_browser,
            concurrent_fragments=concurrent_fragments,
            output_dir=output_dir,
            download_playlist=download_playlist,
            overwrite=overwrite,
            format_id=format_id,
            diagnostic_mode=diagnostic_mode,
            dynamic_scan_enabled=dynamic_scan_enabled,
            request_delay_seconds=request_delay_seconds,
            candidate_url=candidate_url,
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
        concurrent_fragments: int = 4,
        download_playlist: bool = False,
        diagnostic_mode: bool = True,
        dynamic_scan_enabled: bool = True,
        request_delay_seconds: float = 0.0,
    ):
        super().__init__()
        self.url = url
        self.options = DownloadOptions(
            cookie_browser=cookie_browser,
            concurrent_fragments=concurrent_fragments,
            download_playlist=download_playlist,
            diagnostic_mode=diagnostic_mode,
            dynamic_scan_enabled=dynamic_scan_enabled,
            request_delay_seconds=request_delay_seconds,
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
            "thumbnail": metadata.thumbnail,
            "uploader": metadata.uploader,
            "extractor": metadata.extractor,
            "webpage_url": metadata.webpage_url,
            "source_url": metadata.source_url or metadata.webpage_url,
            "requires_ffmpeg": metadata.requires_ffmpeg,
            "ffmpeg_available": metadata.ffmpeg_available,
            "requires_login": metadata.requires_login,
            "restriction_status": metadata.restriction_status,
            "warning": metadata.warning,
            "cookie_status": metadata.cookie_status,
            "diagnostic_summary": metadata.diagnostic_summary,
            "diagnostic_log": metadata.diagnostic_log,
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
                    "url": item.url,
                    "source": item.source,
                    "ext": item.ext,
                    "protocol": item.protocol,
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
                    "download_url": item.download_url,
                }
                for item in metadata.formats
            ],
        }
