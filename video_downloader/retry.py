"""异步重试装饰器"""
import asyncio
import random
from functools import wraps
from typing import Callable, TypeVar

from errors import DownloadError, ErrorHandler, RetryConfig


T = TypeVar("T")


def async_retry(
    config: RetryConfig = None,
    error_handler: ErrorHandler = None,
    platform: str = "",
):
    """异步重试装饰器"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            cfg = config or RetryConfig()
            handler = error_handler or ErrorHandler()
            last_error = None
            delay = cfg.initial_delay

            for attempt in range(cfg.max_retries + 1):
                try:
                    return await func(*args, **kwargs)

                except Exception as e:
                    last_error = e
                    download_error = handler.categorize_error(e, platform)

                    if not handler.should_retry(download_error):
                        raise download_error

                    if attempt < cfg.max_retries:
                        # 计算延迟
                        if cfg.jitter:
                            delay = delay * cfg.exponential_base + random.uniform(0, 1)
                        else:
                            delay = delay * cfg.exponential_base

                        delay = min(delay, cfg.max_delay)

                        print(f"重试 {attempt + 1}/{cfg.max_retries}, "
                              f"等待 {delay:.1f}秒, 错误: {download_error.message}")

                        await asyncio.sleep(delay)
                    else:
                        raise download_error

            raise last_error

        return wrapper

    return decorator
