"""错误定义和分类"""
from enum import Enum
from typing import Dict, Optional, Tuple
from dataclasses import dataclass


class ErrorCategory(Enum):
    """错误分类"""
    NETWORK_ERROR = "network"
    PLATFORM_ERROR = "platform"
    FILE_ERROR = "file"
    AUTH_ERROR = "auth"
    FORMAT_ERROR = "format"
    UNKNOWN_ERROR = "unknown"


@dataclass
class RetryConfig:
    """重试配置"""
    max_retries: int = 3
    initial_delay: float = 1.0  # 秒
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True


@dataclass
class DownloadError(Exception):
    """下载异常"""
    category: ErrorCategory
    code: str
    message: str
    recoverable: bool = True
    original_error: Optional[Exception] = None

    def __str__(self):
        return f"[{self.code}] {self.message}"


class ErrorHandler:
    """错误处理器"""

    # 平台特定错误映射
    PLATFORM_ERROR_MAP: Dict[str, Dict[str, Tuple[ErrorCategory, str, bool]]] = {
        "youtube": {
            "Video unavailable": (ErrorCategory.PLATFORM_ERROR, "VIDEO_UNAVAILABLE", False),
            "Sign in to confirm": (ErrorCategory.AUTH_ERROR, "AUTH_REQUIRED", True),
            "This video may be age-restricted": (ErrorCategory.PLATFORM_ERROR, "AGE_RESTRICTED", False),
            "has been removed": (ErrorCategory.PLATFORM_ERROR, "VIDEO_REMOVED", False),
            "private video": (ErrorCategory.PLATFORM_ERROR, "VIDEO_PRIVATE", False),
        },
        "bilibili": {
            "下载失败": (ErrorCategory.PLATFORM_ERROR, "BILIBILI_DOWNLOAD_FAILED", True),
            "cookies": (ErrorCategory.AUTH_ERROR, "NEED_COOKIE", True),
            "访问过于频繁": (ErrorCategory.PLATFORM_ERROR, "RATE_LIMITED", True),
            "稿件不可见": (ErrorCategory.PLATFORM_ERROR, "VIDEO_HIDDEN", False),
        },
        "douyin": {
            "copy write": (ErrorCategory.PLATFORM_ERROR, "COPYRIGHT", False),
            "作品不可见": (ErrorCategory.PLATFORM_ERROR, "VIDEO_HIDDEN", False),
        },
        "weibo": {
            "此微博已被删除": (ErrorCategory.PLATFORM_ERROR, "WEIBO_DELETED", False),
            "登录后才可观看": (ErrorCategory.AUTH_ERROR, "AUTH_REQUIRED", True),
        },
    }

    def __init__(self, retry_config: RetryConfig = None):
        self.retry_config = retry_config or RetryConfig()

    def categorize_error(
        self,
        error: Exception,
        platform: str = "",
    ) -> DownloadError:
        """分类错误"""
        error_msg = str(error)

        # 检查平台特定错误
        if platform in self.PLATFORM_ERROR_MAP:
            for pattern, (category, code, recoverable) in self.PLATFORM_ERROR_MAP[platform].items():
                if pattern in error_msg:
                    return DownloadError(
                        category=category,
                        code=code,
                        message=error_msg,
                        recoverable=recoverable,
                        original_error=error,
                    )

        # 检查网络错误
        if self._is_network_error(error):
            return DownloadError(
                category=ErrorCategory.NETWORK_ERROR,
                code="NETWORK_ERROR",
                message=error_msg,
                recoverable=True,
                original_error=error,
            )

        # 未知错误
        return DownloadError(
            category=ErrorCategory.UNKNOWN_ERROR,
            code="UNKNOWN",
            message=error_msg,
            recoverable=False,
            original_error=error,
        )

    def _is_network_error(self, error: Exception) -> bool:
        """判断是否为网络错误"""
        network_keywords = [
            "Connection",
            "Timeout",
            "DNS",
            "network",
            "HTTP",
            "Socket",
            "Proxy",
            "SSL",
            "TLS",
            "BadStatusLine",
            "IncompleteRead",
        ]
        error_msg = str(error).lower()
        return any(kw.lower() in error_msg for kw in network_keywords)

    def should_retry(self, error: DownloadError) -> bool:
        """判断是否应该重试"""
        return error.recoverable and error.category in (
            ErrorCategory.NETWORK_ERROR,
            ErrorCategory.PLATFORM_ERROR,
        )
