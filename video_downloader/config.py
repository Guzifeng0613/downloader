"""配置管理"""
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse


class Config:
    """应用配置"""

    # 下载相关
    DEFAULT_DOWNLOAD_PATH = Path.cwd() / "downloads"
    SEGMENT_COUNT = 8  # 默认分段数（增加并发）
    CHUNK_SIZE = 1024 * 1024  # 1MB chunks
    RETRY_COUNT = 3
    RETRY_DELAY = 2.0  # 秒
    CONCURRENT_REQUESTS = 4  # 并发请求数

    # 网络相关
    TIMEOUT = 60  # 秒
    CONNECTION_pool_size = 10

    # 用户代理
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # 平台特定配置
    PLATFORM_HEADERS = {
        "bilibili": {
            "Referer": "https://www.bilibili.com",
            "User-Agent": USER_AGENT,
        },
        "youtube": {
            "User-Agent": USER_AGENT,
        },
        "douyin": {
            "User-Agent": USER_AGENT,
        },
        "vqq": {
            "Referer": "https://v.qq.com",
            "User-Agent": USER_AGENT,
        },
        "vqq:video": {
            "Referer": "https://v.qq.com",
            "User-Agent": USER_AGENT,
        },
        "xiaohongshu": {
            "Referer": "https://www.xiaohongshu.com",
            "User-Agent": USER_AGENT,
        },
        "kuaishou": {
            "Referer": "https://www.kuaishou.com",
            "User-Agent": USER_AGENT,
        },
    }

    # extractor 别名映射（将 :video 等后缀规整到主名称）
    EXTRACTOR_ALIASES = {
        "vqq:video": "vqq",
        "youtube:playlist": "youtube",
        "bilibili:bangumi": "bilibili",
    }

    PLATFORM_HINTS = {
        "youtube.com": "youtube",
        "youtu.be": "youtube",
        "bilibili.com": "bilibili",
        "douyin.com": "douyin",
        "v.qq.com": "vqq",
        "qq.com": "vqq",
        "xiaohongshu.com": "xiaohongshu",
        "xhslink.com": "xiaohongshu",
        "kuaishou.com": "kuaishou",
    }

    PLATFORM_DOWNLOAD_PROFILES = {
        "youtube": {
            "sleep_interval_requests": 0.25,
            "http_headers": PLATFORM_HEADERS["youtube"],
        },
        "bilibili": {
            "sleep_interval_requests": 0.5,
            "http_headers": PLATFORM_HEADERS["bilibili"],
        },
        "douyin": {
            "sleep_interval_requests": 0.35,
            "http_headers": PLATFORM_HEADERS["douyin"],
        },
        "vqq": {
            "sleep_interval_requests": 0.35,
            "http_headers": PLATFORM_HEADERS["vqq"],
        },
        "xiaohongshu": {
            "sleep_interval_requests": 0.45,
            "http_headers": PLATFORM_HEADERS["xiaohongshu"],
        },
        "kuaishou": {
            "sleep_interval_requests": 0.35,
            "http_headers": PLATFORM_HEADERS["kuaishou"],
        },
    }

    BROWSER_COOKIE_PATHS = {
        "chrome": [
            "~/AppData/Local/Google/Chrome/User Data/Default/Network/Cookies",
            "~/AppData/Local/Google/Chrome/User Data/Profile 1/Network/Cookies",
        ],
        "edge": [
            "~/AppData/Local/Microsoft/Edge/User Data/Default/Network/Cookies",
            "~/AppData/Local/Microsoft/Edge/User Data/Profile 1/Network/Cookies",
        ],
        "firefox": [
            "~/AppData/Roaming/Mozilla/Firefox/Profiles",
        ],
    }

    BROWSER_EXECUTABLE_PATHS = {
        "chrome": [
            "C:/Program Files/Google/Chrome/Application/chrome.exe",
            "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
            "~/AppData/Local/Google/Chrome/Application/chrome.exe",
        ],
        "edge": [
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
            "~/AppData/Local/Microsoft/Edge/Application/msedge.exe",
        ],
    }

    # ffmpeg 可能存在的路径
    FFMPEG_POSSIBLE_PATHS = [
        "~/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1-full_build/bin/ffmpeg.exe",
        "C:/Users/39187/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1-full_build/bin/ffmpeg.exe",
    ]

    @classmethod
    def get_download_path(cls) -> Path:
        """获取下载路径，不存在则创建"""
        path = cls.DEFAULT_DOWNLOAD_PATH
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def get_platform_headers(cls, platform: str) -> dict:
        """获取平台特定请求头"""
        # 先尝试直接匹配
        if platform in cls.PLATFORM_HEADERS:
            return cls.PLATFORM_HEADERS[platform]

        # 尝试别名映射
        alias = cls.EXTRACTOR_ALIASES.get(platform)
        if alias and alias in cls.PLATFORM_HEADERS:
            return cls.PLATFORM_HEADERS[alias]

        # 返回默认
        return {"User-Agent": cls.USER_AGENT}

    @classmethod
    def resolve_platform(cls, url: str = "", extractor: str = "") -> str:
        """根据 extractor 或 URL 推断平台名。"""
        if extractor:
            if extractor in cls.PLATFORM_HEADERS:
                return extractor
            alias = cls.EXTRACTOR_ALIASES.get(extractor)
            if alias:
                return alias

        hostname = (urlparse(url).hostname or "").lower()
        for hint, platform in cls.PLATFORM_HINTS.items():
            if hostname.endswith(hint):
                return platform

        return extractor or ""

    @classmethod
    def get_platform_profile(cls, url: str = "", extractor: str = "") -> dict:
        """获取平台下载配置。"""
        platform = cls.resolve_platform(url=url, extractor=extractor)
        profile = dict(cls.PLATFORM_DOWNLOAD_PROFILES.get(platform, {}))
        headers = {"User-Agent": cls.USER_AGENT}
        headers.update(profile.get("http_headers", {}))
        profile["http_headers"] = headers
        return profile

    @classmethod
    def get_browser_cookie_paths(cls, browser: str) -> list[str]:
        """返回浏览器可能的 Cookie 存储路径。"""
        paths = cls.BROWSER_COOKIE_PATHS.get((browser or "").lower(), [])
        return [os.path.expanduser(path) for path in paths]

    @classmethod
    def get_browser_executable(cls, browser: str) -> str:
        """返回浏览器可执行文件路径。"""
        name = (browser or "").lower()

        if name == "chrome":
            for candidate in ("chrome.exe", "chrome"):
                path = shutil.which(candidate)
                if path:
                    return path
        if name == "edge":
            for candidate in ("msedge.exe", "msedge"):
                path = shutil.which(candidate)
                if path:
                    return path

        for path in cls.BROWSER_EXECUTABLE_PATHS.get(name, []):
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                return expanded

        return ""

    @classmethod
    def get_ffmpeg_path(cls) -> str:
        """获取 ffmpeg 路径"""
        # 先尝试 shutil.which
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            return ffmpeg_path

        # 尝试可能的路径
        for path in cls.FFMPEG_POSSIBLE_PATHS:
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                return expanded

        return ""


# 全局配置实例
config = Config()
