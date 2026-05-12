"""yt-dlp 封装模块"""
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Dict, Any
from enum import Enum

import yt_dlp

from config import config


class VideoQuality(Enum):
    """视频画质选项"""
    BEST = "best"
    WORST = "worst"
    BEST_1080P = "best[height<=1080]"
    BEST_720P = "best[height<=720]"
    BEST_480P = "best[height<=480]"
    BEST_360P = "best[height<=360]"


@dataclass
class FormatInfo:
    """视频格式信息"""
    format_id: str
    ext: str
    resolution: str
    filesize: int
    tbr: float = 0.0  # total bitrate
    vcodec: str = "none"
    acodec: str = "none"
    fps: float = 0.0
    download_url: str = ""

    @property
    def height(self) -> Optional[int]:
        """获取视频高度"""
        if self.resolution and 'x' in self.resolution:
            try:
                return int(self.resolution.split('x')[1])
            except (ValueError, IndexError):
                pass
        if self.resolution and self.resolution.endswith('p'):
            try:
                return int(self.resolution.rstrip('p'))
            except ValueError:
                pass
        return None

    @property
    def display_name(self) -> str:
        """显示名称"""
        if self.height:
            return f"{self.height}p ({self.ext})"
        return f"{self.resolution} ({self.ext})"


@dataclass
class VideoInfo:
    """视频信息"""
    title: str
    duration: float
    thumbnail: str
    description: str
    uploader: str
    upload_date: str
    view_count: int
    like_count: int
    formats: List[FormatInfo] = field(default_factory=list)
    webpage_url: str = ""
    extractor: str = ""

    @property
    def duration_str(self) -> str:
        """时长字符串 (HH:MM:SS)"""
        if not self.duration or self.duration <= 0:
            return "未知"
        total_seconds = int(self.duration)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def get_formats_with_url(self) -> List[FormatInfo]:
        """获取有直接下载地址的格式"""
        return [f for f in self.formats if f.download_url]

    def get_best_format(self) -> Optional[FormatInfo]:
        """获取最佳格式（有 URL 的）"""
        formats_with_url = self.get_formats_with_url()
        if not formats_with_url:
            return None
        # 按文件大小排序，返回最大的
        return max(formats_with_url, key=lambda f: f.filesize or 0)


class YtDlpWrapper:
    """yt-dlp 封装类"""

    def __init__(self, progress_callback: Optional[Callable] = None):
        self.progress_callback = progress_callback

    def _get_yt_dlp_options(self, full_parse: bool = True, **kwargs) -> Dict:
        """生成 yt-dlp 选项

        Args:
            full_parse: 是否完整解析格式信息。False 时只获取基本信息，速度更快
        """
        headers = {
            "User-Agent": config.USER_AGENT,
        }

        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,  # 只获取信息
            "http_headers": headers,
            # 性能优化选项
            "nocheckcertificate": True,
            **kwargs
        }

        # 是否提取格式信息
        if not full_parse:
            options["extract_flat"] = True  # 只获取基本信息，不获取格式
            options["lazy_load_formats"] = True  # 延迟加载格式

        # 添加进度钩子
        if self.progress_callback:
            options["progress_hooks"] = [self._progress_hook]

        return options

    def _progress_hook(self, d: Dict):
        """进度回调钩子"""
        if self.progress_callback and d.get("status") == "downloading":
            self.progress_callback({
                "status": d.get("status"),
                "downloaded_bytes": d.get("downloaded_bytes", 0),
                "total_bytes": d.get("total_bytes", 0),
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "filename": d.get("filename"),
            })

    def extract_info(self, url: str, full_parse: bool = True) -> Optional[VideoInfo]:
        """提取视频信息

        Args:
            url: 视频 URL
            full_parse: 是否完整解析格式信息。False 时只获取基本信息，速度更快
        """
        ydl_opts = self._get_yt_dlp_options(full_parse=full_parse)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info is None:
                    return None

                return self._parse_video_info(info, full_parse=full_parse)
        except Exception as e:
            print(f"提取视频信息失败: {e}")
            return None

    def _parse_video_info(self, info: Dict, full_parse: bool = True) -> VideoInfo:
        """解析视频信息为统一格式"""
        extractor = info.get("extractor", "").lower()

        # 平台特定请求头
        platform_headers = config.get_platform_headers(extractor)

        # 解析格式列表
        formats = []
        if full_parse:
            # 完整解析：获取所有格式信息
            for fmt in info.get("formats", []) or []:
                download_url = (
                    fmt.get("url") or
                    (fmt.get("fragments") or [{}])[0].get("url", "") if fmt.get("fragments") else ""
                )

                format_info = FormatInfo(
                    format_id=fmt.get("format_id", ""),
                    ext=fmt.get("ext", ""),
                    resolution=fmt.get("resolution", "unknown"),
                    filesize=fmt.get("filesize") or fmt.get("filesize_approx") or 0,
                    tbr=fmt.get("tbr") or 0,
                    vcodec=fmt.get("vcodec", "none"),
                    acodec=fmt.get("acodec", "none"),
                    fps=fmt.get("fps") or 0,
                    download_url=download_url,
                )
                formats.append(format_info)
        else:
            # 快速解析：只获取基本可用信息，添加一个默认格式
            formats.append(FormatInfo(
                format_id="default",
                ext="mp4",
                resolution="unknown",
                filesize=0,
            ))

        return VideoInfo(
            title=info.get("title", "未知标题"),
            duration=info.get("duration", 0) or 0,
            thumbnail=info.get("thumbnail", ""),
            description=info.get("description", ""),
            uploader=info.get("uploader", "未知作者"),
            upload_date=info.get("upload_date", ""),
            view_count=info.get("view_count", 0) or 0,
            like_count=info.get("like_count", 0) or 0,
            formats=formats,
            webpage_url=info.get("webpage_url", ""),
            extractor=extractor,
        )

    def get_available_qualities(self, video_info: VideoInfo) -> List[str]:
        """获取可用画质列表"""
        qualities = set()
        for fmt in video_info.formats:
            if fmt.height:
                qualities.add(f"{fmt.height}p")
            else:
                qualities.add(fmt.resolution)
        return sorted(qualities, key=lambda x: int(x.rstrip('p')) if x.rstrip('p').isdigit() else 0, reverse=True)
