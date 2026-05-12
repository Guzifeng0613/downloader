"""混合下载器 - 智能选择分段或直接下载"""
import asyncio
import os
from typing import Optional, Callable, Dict
from pathlib import Path

import yt_dlp

from yt_dlp_wrapper import YtDlpWrapper, VideoInfo, FormatInfo, VideoQuality
from .segment import SegmentDownloader, DownloadProgress
from .generic import GenericVideoFinder, GenericDownloader
from config import config


class HybridDownloader:
    """混合下载器：yt-dlp 解析 + 自定义分段下载 + 通用下载器"""

    def __init__(
        self,
        yt_dlp_wrapper: YtDlpWrapper = None,
        segment_downloader: SegmentDownloader = None,
        progress_callback: Callable[[Dict], None] = None,
    ):
        self.yt_dlp = yt_dlp_wrapper or YtDlpWrapper()
        self.segment_downloader = segment_downloader
        self.progress_callback = progress_callback
        self.generic_finder = GenericVideoFinder()
        self.generic_downloader = GenericDownloader()

    async def download(
        self,
        url: str,
        output_path: str,
        quality: VideoQuality = VideoQuality.BEST,
        title: str = None,
        segment_count: int = None,
    ) -> bool:
        """下载视频

        Args:
            segment_count: 分段下载的并发数，None 时使用配置默认值
        """
        if segment_count is None:
            segment_count = config.SEGMENT_COUNT

        # Step 1: 使用 yt-dlp 获取信息
        video_info = await asyncio.to_thread(self.yt_dlp.extract_info, url)

        if video_info:
            # yt-dlp 解析成功，使用正常流程下载
            return await self._download_with_info(
                url, output_path, quality, video_info, segment_count
            )
        else:
            # yt-dlp 解析失败，尝试使用通用下载器
            print("yt-dlp 无法解析，尝试通用下载器...")
            return await self._generic_download(url, output_path, title)

    async def _download_with_info(
        self,
        url: str,
        output_path: str,
        quality: VideoQuality,
        video_info,
        segment_count: int,
    ) -> bool:
        """使用 yt-dlp 信息下载视频"""
        # Step 2: 选择格式
        target_format = self._select_format(video_info, quality)
        if not target_format:
            print("未找到可用格式")
            return False

        # Step 3: 创建以视频标题命名的文件夹
        safe_title = self._sanitize_filename(video_info.title)
        video_folder = Path(output_path).parent / safe_title
        video_folder.mkdir(parents=True, exist_ok=True)

        # Step 4: 检查是否支持 Range 下载
        if target_format.download_url and target_format.filesize > 0:
            file_path = str(video_folder / f"{safe_title}.{target_format.ext}")
            return await self._segment_download(
                url=target_format.download_url,
                output_path=file_path,
                total_size=target_format.filesize,
                headers=config.get_platform_headers(video_info.extractor),
                segment_count=segment_count,
            )
        else:
            return await self._yt_dlp_download(
                url, str(Path(output_path).parent), quality, video_info.extractor
            )

    async def _generic_download(
        self,
        url: str,
        output_path: str,
        title: str = None,
    ) -> bool:
        """使用通用下载器从网页源码查找视频 URL 并下载"""
        # 查找视频 URL
        result = await self.generic_finder.find_video_url(url)

        if not result:
            print("无法从网页中找到视频 URL")
            return False

        video_url, format_type = result
        print(f"找到视频 URL: {video_url[:80]}... (类型: {format_type})")

        # 创建文件夹
        safe_title = title or "video"
        safe_title = self._sanitize_filename(safe_title)
        video_folder = Path(output_path).parent / safe_title
        video_folder.mkdir(parents=True, exist_ok=True)

        # 根据格式类型下载
        if format_type == 'm3u8':
            # m3u8 需要用 ffmpeg 下载
            print("检测到 m3u8 流媒体，需要 ffmpeg 下载...")
            # 目前暂不支持 m3u8 的通用下载
            return await self._download_direct(video_url, str(video_folder / f"{safe_title}.mp4"))
        else:
            # 直接下载 mp4/flv/webm
            ext = format_type if format_type != 'unknown' else 'mp4'
            return await self._download_direct(video_url, str(video_folder / f"{safe_title}.{ext}"))

    async def _download_direct(self, url: str, output_path: str) -> bool:
        """直接下载视频文件"""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        try:
            import aiohttp

            headers = {"User-Agent": config.USER_AGENT}
            timeout = aiohttp.ClientTimeout(total=600)
            downloaded = 0

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        print(f"HTTP {resp.status}")
                        return False

                    total_size = int(resp.headers.get('Content-Length', 0))

                    with open(output_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(81920):
                            f.write(chunk)
                            downloaded += len(chunk)

                            if self.progress_callback and total_size > 0:
                                self.progress_callback({
                                    'downloaded_bytes': downloaded,
                                    'total_bytes': total_size,
                                    'speed': len(chunk),
                                    'status': 'downloading',
                                })

                    print(f"下载完成: {output_path}")
                    return True

        except Exception as e:
            print(f"下载失败: {e}")
            return False

    def _sanitize_filename(self, filename: str) -> str:
        """清理文件名中的非法字符"""
        import re
        # Windows 非法字符: \ / : * ? " < > |
        filename = re.sub(r'[\/\\:*?"<>|]', '', filename)
        # 限制长度
        if len(filename) > 200:
            filename = filename[:200]
        return filename.strip()

    def _select_format(
        self,
        video_info: VideoInfo,
        quality: VideoQuality,
    ) -> Optional[FormatInfo]:
        """选择最佳格式"""
        # 获取所有有 height 的格式
        all_formats = [f for f in video_info.formats if f.height]
        if not all_formats:
            return None

        if quality == VideoQuality.BEST:
            # 返回最大的
            return max(all_formats, key=lambda f: f.filesize or 0)

        # 根据高度选择
        quality_map = {
            VideoQuality.BEST_1080P: 1080,
            VideoQuality.BEST_720P: 720,
            VideoQuality.BEST_480P: 480,
            VideoQuality.BEST_360P: 360,
        }

        max_height = quality_map.get(quality)
        if max_height:
            suitable = [f for f in all_formats if f.height <= max_height]
            if suitable:
                return max(suitable, key=lambda f: f.filesize or 0)

        return all_formats[0] if all_formats else None

    async def _segment_download(
        self,
        url: str,
        output_path: str,
        total_size: int,
        headers: Dict,
        segment_count: int = None,
    ) -> bool:
        """使用分段下载"""
        if segment_count is None:
            segment_count = config.SEGMENT_COUNT

        segment_downloader = SegmentDownloader(
            thread_count=segment_count,
            progress_callback=self._on_progress,
        )
        self.segment_downloader = segment_downloader

        try:
            success = await segment_downloader.download(
                url=url,
                output_path=output_path,
                total_size=total_size,
                headers=headers,
            )
            return success
        except Exception as e:
            print(f"分段下载失败: {e}")
            return False

    async def _yt_dlp_download(
        self,
        url: str,
        output_path: str,
        quality: VideoQuality,
        extractor: str = "",
    ) -> bool:
        """使用 yt-dlp 直接下载"""
        def progress_hook(d: Dict):
            if self.progress_callback and d.get("status") == "downloading":
                self.progress_callback({
                    "downloaded_bytes": d.get("downloaded_bytes", 0),
                    "total_bytes": d.get("total_bytes", 0),
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                    "status": d.get("status"),
                })

        # 获取平台特定请求头
        headers = config.get_platform_headers(extractor)

        # 检查 ffmpeg 是否可用
        ffmpeg_path = config.get_ffmpeg_path()
        has_ffmpeg = bool(ffmpeg_path)

        # 让 yt-dlp 自动选择最佳格式
        # ffmpeg 存在时 yt-dlp 会自动合并视频和音频
        format_selector = None

        # 使用模板让视频音频下载到同一文件夹
        # %(title)s/%(title)s.%(ext)s 会创建 "标题/标题.mp4" 的结构
        outtmpl = str(Path(output_path) / "%(title)s" / "%(title)s.%(ext)s")

        ydl_opts = {
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": not has_ffmpeg,  # 没有 ffmpeg 时显示警告
            "progress_hooks": [progress_hook],
            "http_headers": headers,
        }

        # 指定 ffmpeg 位置
        if ffmpeg_path:
            ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)

        # 启用并发分段下载（加快 m3u8 视频下载速度）
        ydl_opts["concurrent_fragment_requests"] = 4

        if format_selector:
            ydl_opts["format"] = format_selector

        def sync_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        try:
            await asyncio.to_thread(sync_download)
            return True
        except Exception as e:
            print(f"yt-dlp 下载失败: {e}")
            return False

    def _on_progress(self, progress: DownloadProgress):
        """进度回调"""
        if self.progress_callback:
            self.progress_callback({
                "downloaded_bytes": progress.downloaded_bytes,
                "total_bytes": progress.total_bytes,
                "speed": progress.speed,
                "eta": progress.eta,
                "status": "downloading",
            })

    def cancel(self):
        """取消下载"""
        if self.segment_downloader:
            self.segment_downloader.cancel()
