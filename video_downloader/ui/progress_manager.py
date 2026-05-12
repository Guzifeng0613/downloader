"""进度管理器"""
from PySide6.QtCore import QObject, Signal
from typing import Dict
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TaskProgress:
    """任务进度数据类"""
    task_id: str
    url: str
    title: str = ""
    status: str = "pending"  # pending, downloading, paused, completed, error
    progress: float = 0.0  # 0-100
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0  # bytes/s
    eta: int = 0  # seconds
    error_message: str = ""
    output_path: str = ""
    start_time: datetime = field(default_factory=datetime.now)
    update_time: datetime = field(default_factory=datetime.now)

    @property
    def speed_str(self) -> str:
        """速度字符串"""
        if self.speed > 1024 * 1024:
            return f"{self.speed / (1024 * 1024):.1f} MB/s"
        elif self.speed > 1024:
            return f"{self.speed / 1024:.1f} KB/s"
        return f"{self.speed:.0f} B/s"

    @property
    def size_str(self) -> str:
        """大小字符串"""
        downloaded = self._format_size(self.downloaded_bytes)
        total = self._format_size(self.total_bytes)
        if self.total_bytes > 0:
            return f"{downloaded} / {total}"
        return downloaded

    @staticmethod
    def _format_size(size: int) -> str:
        if size > 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024 * 1024):.2f} GB"
        elif size > 1024 * 1024:
            return f"{size / (1024 * 1024):.2f} MB"
        elif size > 1024:
            return f"{size / 1024:.2f} KB"
        return f"{size} B"


class ProgressManager(QObject):
    """进度管理器 - 单例模式"""

    progress_updated = Signal(str, dict)  # task_id, progress_data
    task_completed = Signal(str, dict)     # task_id, result
    task_error = Signal(str, str)         # task_id, error_message

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tasks = {}
        return cls._instance

    @property
    def tasks(self):
        return self._tasks

    def add_task(self, task_id: str, url: str, title: str = ""):
        """添加新任务"""
        self._tasks[task_id] = TaskProgress(
            task_id=task_id,
            url=url,
            title=title or "等待中...",
        )

    def update_progress(self, task_id: str, data: Dict):
        """更新任务进度"""
        if task_id in self._tasks:
            task = self._tasks[task_id]
            task.downloaded_bytes = data.get("downloaded_bytes", 0)
            task.total_bytes = data.get("total_bytes", 0)
            task.speed = data.get("speed", 0) or 0
            task.eta = data.get("eta", 0) or 0
            task.status = "downloading"

            if task.total_bytes > 0:
                task.progress = (task.downloaded_bytes / task.total_bytes) * 100
            else:
                # 估算进度（根据下载的绝对值，需要外部传入 total）
                task.progress = min(data.get("downloaded_bytes", 0) / (100 * 1024 * 1024) * 100, 99)

            task.update_time = datetime.now()

            self.progress_updated.emit(task_id, self._serialize_task(task))

    def complete_task(self, task_id: str, result: Dict):
        """标记任务完成"""
        if task_id in self._tasks:
            task = self._tasks[task_id]
            task.status = "completed"
            task.progress = 100
            task.output_path = result.get("path", "") or result.get("output_path", "")
            self.task_completed.emit(task_id, self._serialize_task(task))

    def error_task(self, task_id: str, error_message: str):
        """标记任务错误"""
        if task_id in self._tasks:
            task = self._tasks[task_id]
            task.status = "error"
            task.error_message = error_message
            self.task_error.emit(task_id, error_message)

    def set_title(self, task_id: str, title: str):
        """设置任务标题"""
        if task_id in self._tasks:
            self._tasks[task_id].title = title

    def remove_task(self, task_id: str):
        """移除任务"""
        if task_id in self._tasks:
            del self._tasks[task_id]

    def _serialize_task(self, task: TaskProgress) -> Dict:
        """序列化任务数据"""
        return {
            "task_id": task.task_id,
            "title": task.title,
            "status": task.status,
            "progress": task.progress,
            "downloaded_bytes": task.downloaded_bytes,
            "total_bytes": task.total_bytes,
            "speed": task.speed,
            "speed_str": task.speed_str,
            "size_str": task.size_str,
            "eta": task.eta,
            "output_path": task.output_path,
            "error_message": task.error_message,
        }
