"""分段下载器模块"""
import asyncio
import aiohttp
import os
from dataclasses import dataclass
from typing import Optional, Callable, Dict, List
from pathlib import Path

from config import config


@dataclass
class Segment:
    """分段数据类"""
    index: int
    start: int
    end: int
    url: str


@dataclass
class DownloadProgress:
    """下载进度"""
    downloaded_bytes: int
    total_bytes: int
    speed: float  # bytes/s
    eta: int  # seconds
    progress: float  # 0-100

    @property
    def speed_str(self) -> str:
        """速度字符串"""
        if self.speed > 1024 * 1024:
            return f"{self.speed / (1024 * 1024):.1f} MB/s"
        elif self.speed > 1024:
            return f"{self.speed / 1024:.1f} KB/s"
        return f"{self.speed:.0f} B/s"

    @property
    def eta_str(self) -> str:
        """剩余时间字符串"""
        if self.eta <= 0:
            return "--:--"
        total_seconds = int(self.eta)
        minutes, seconds = divmod(total_seconds, 60)
        if total_seconds >= 3600:
            hours, remainder = divmod(total_seconds, 3600)
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


class SegmentDownloader:
    """分段下载器"""

    def __init__(
        self,
        thread_count: int = None,
        segment_size: int = 10 * 1024 * 1024,  # 10MB per segment
        retry_count: int = None,
        retry_delay: float = None,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ):
        self.thread_count = thread_count or config.SEGMENT_COUNT
        self.segment_size = segment_size
        self.retry_count = retry_count or config.RETRY_COUNT
        self.retry_delay = retry_delay or config.RETRY_DELAY
        self.progress_callback = progress_callback

        self._is_cancelled = False
        self._is_paused = False
        self._downloaded_bytes = 0
        self._speed_history: List[float] = []
        self._last_update_time = 0
        self._last_downloaded = 0

    async def download(
        self,
        url: str,
        output_path: str,
        total_size: int,
        headers: Optional[Dict] = None,
    ) -> bool:
        """执行分段下载"""
        self._is_cancelled = False
        self._is_paused = False
        self._downloaded_bytes = 0
        self._speed_history.clear()

        # 创建分段
        segments = self._create_segments(url, total_size)
        if not segments:
            return False

        # 创建临时目录
        temp_dir = f"{output_path}.segments"
        os.makedirs(temp_dir, exist_ok=True)

        # 并发下载所有分段
        tasks = [
            self._download_segment(seg, temp_dir, headers)
            for seg in segments
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 检查是否所有分段都下载成功
        if all(r is True for r in results):
            # 合并分段
            success = await self._merge_segments(output_path, temp_dir, len(segments))
            return success
        elif not self._is_cancelled:
            print("部分分段下载失败")
            return False

        return not self._is_cancelled

    def _create_segments(self, url: str, total_size: int) -> List[Segment]:
        """创建分段列表"""
        if total_size <= 0:
            return []

        segments = []
        num_segments = (total_size + self.segment_size - 1) // self.segment_size

        for i in range(num_segments):
            start = i * self.segment_size
            end = min((i + 1) * self.segment_size - 1, total_size - 1)
            segments.append(Segment(index=i, start=start, end=end, url=url))

        return segments

    async def _download_segment(
        self,
        segment: Segment,
        temp_dir: str,
        headers: Optional[Dict],
    ) -> bool:
        """下载单个分段，带重试"""
        headers = dict(headers) if headers else {}
        headers["Range"] = f"bytes={segment.start}-{segment.end}"

        segment_file = os.path.join(temp_dir, f"segment_{segment.index}")

        for attempt in range(self.retry_count):
            if self._is_cancelled:
                return False

            try:
                async with aiohttp.ClientSession() as session:
                    timeout = aiohttp.ClientTimeout(total=config.TIMEOUT)
                    async with session.get(
                        segment.url,
                        headers=headers,
                        timeout=timeout,
                        allow_redirects=True,
                    ) as resp:
                        if resp.status not in (200, 206):
                            raise aiohttp.ClientError(f"HTTP {resp.status}")

                        chunk_size = config.CHUNK_SIZE
                        downloaded = 0
                        segment_start = segment.start

                        with open(segment_file, "wb") as f:
                            async for chunk in resp.content.iter_chunked(chunk_size):
                                if self._is_cancelled:
                                    f.close()
                                    os.remove(segment_file)
                                    return False

                                f.write(chunk)
                                downloaded += len(chunk)
                                segment_start += len(chunk)
                                self._update_progress(downloaded, segment.end - segment.start + 1)

                        return True

            except Exception as e:
                if attempt < self.retry_count - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                else:
                    print(f"分段 {segment.index} 下载失败: {e}")
                    return False

        return False

    def _update_progress(self, segment_downloaded: int, segment_total: int):
        """更新进度"""
        self._downloaded_bytes += segment_downloaded

        import time
        now = time.time()
        if self._last_update_time > 0:
            elapsed = now - self._last_update_time
            if elapsed > 0:
                speed = segment_downloaded / elapsed
                self._speed_history.append(speed)
                if len(self._speed_history) > 5:
                    self._speed_history.pop(0)

        self._last_update_time = now
        self._last_downloaded = self._downloaded_bytes

        if self.progress_callback:
            avg_speed = sum(self._speed_history) / len(self._speed_history) if self._speed_history else 0
            progress = DownloadProgress(
                downloaded_bytes=self._downloaded_bytes,
                total_bytes=0,  # 总大小从外层传入
                speed=avg_speed,
                eta=0,  # ETA 计算需要总大小
                progress=0,
            )
            self.progress_callback(progress)

    async def _merge_segments(
        self,
        output_path: str,
        temp_dir: str,
        num_segments: int,
    ) -> bool:
        """合并分段"""
        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, "wb") as out_f:
                for i in range(num_segments):
                    segment_file = os.path.join(temp_dir, f"segment_{i}")
                    with open(segment_file, "rb") as in_f:
                        out_f.write(in_f.read())
                    os.remove(segment_file)  # 删除临时分段

            os.rmdir(temp_dir)
            return True
        except Exception as e:
            print(f"合并分段失败: {e}")
            return False

    def cancel(self):
        """取消下载"""
        self._is_cancelled = True

    def pause(self):
        """暂停下载"""
        self._is_paused = True

    def resume(self):
        """恢复下载"""
        self._is_paused = False
