"""通用视频下载器 - 通过分析网页源码查找视频URL"""
import asyncio
import aiohttp
import re
from typing import Optional, Tuple
from pathlib import Path

from config import config


class GenericVideoFinder:
    """通用视频查找器 - 从网页源码中查找视频URL"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback

    async def find_video_url(self, url: str) -> Optional[Tuple[str, str]]:
        """查找视频URL

        Returns:
            Tuple[str, str]: (video_url, format_type) 或 None
        """
        try:
            html = await self._fetch_page(url)
            if not html:
                return None

            video_url, format_type = self._extract_video_url(html)
            if video_url:
                video_url = self._clean_url(video_url, url)
                return (video_url, format_type)

            return None

        except Exception as e:
            print(f"查找视频URL失败: {e}")
            return None

    async def _fetch_page(self, url: str) -> Optional[str]:
        """获取网页源码"""
        headers = {
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    return None
        except Exception as e:
            print(f"获取页面失败: {e}")
            return None

    def _extract_video_url(self, html: str) -> Tuple[Optional[str], str]:
        """从 HTML 中提取视频 URL"""
        # 查找 m3u8
        pattern_m3u8 = r'["\']([^"\']*\.m3u8[^"\']*)["\']'
        matches = re.findall(pattern_m3u8, html)
        for match in matches:
            if 'm3u8' in match:
                return (match, 'm3u8')

        # 查找 mp4
        pattern_mp4 = r'["\']([^"\']*\.mp4[^"\']*)["\']'
        matches = re.findall(pattern_mp4, html)
        for match in matches:
            if self._is_valid_video_url(match):
                return (match, 'mp4')

        # 查找 flv
        pattern_flv = r'["\']([^"\']*\.flv[^"\']*)["\']'
        matches = re.findall(pattern_flv, html)
        for match in matches:
            if self._is_valid_video_url(match):
                return (match, 'flv')

        # 查找 webm
        pattern_webm = r'["\']([^"\']*\.webm[^"\']*)["\']'
        matches = re.findall(pattern_webm, html)
        for match in matches:
            if self._is_valid_video_url(match):
                return (match, 'webm')

        # 查找其他 src 属性
        pattern_src = r'src\s*=\s*["\']([^"\']+)["\']'
        matches = re.findall(pattern_src, html)
        for match in matches:
            if self._is_valid_video_url(match):
                fmt = self._get_format_type(match)
                return (match, fmt)

        # 查找 data-video
        pattern_data = r'data-video\s*=\s*["\']([^"\']+)["\']'
        matches = re.findall(pattern_data, html)
        for match in matches:
            if self._is_valid_video_url(match):
                fmt = self._get_format_type(match)
                return (match, fmt)

        return (None, '')

    def _is_valid_video_url(self, url: str) -> bool:
        """检查是否是有效的视频 URL"""
        if not url:
            return False

        skip_keywords = ['css', 'js', 'image', 'img', 'icon', 'logo', 'background',
                        'stylesheet', 'font', 'track', 'subtitle', 'poster']
        url_lower = url.lower()
        for kw in skip_keywords:
            if kw in url_lower:
                return False

        video_extensions = ['.mp4', '.m3u8', '.flv', '.webm', '.mov']
        return any(ext in url_lower for ext in video_extensions)

    def _get_format_type(self, url: str) -> str:
        """获取视频格式类型"""
        url_lower = url.lower()
        if '.m3u8' in url_lower:
            return 'm3u8'
        elif '.mp4' in url_lower:
            return 'mp4'
        elif '.flv' in url_lower:
            return 'flv'
        elif '.webm' in url_lower:
            return 'webm'
        return 'mp4'

    def _clean_url(self, url: str, base_url: str) -> str:
        """清理 URL，处理相对路径"""
        if not url:
            return url

        if url.startswith('//'):
            return 'https:' + url
        elif url.startswith('/'):
            match = re.match(r'(https?://[^/]+)', base_url)
            if match:
                return match.group(1) + url
        elif not url.startswith('http'):
            return base_url.rsplit('/', 1)[0] + '/' + url

        return url


class GenericDownloader:
    """通用下载器 - 下载直接的视频 URL"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback

    async def download(
        self,
        url: str,
        output_path: str,
        headers: dict = None,
    ) -> bool:
        """下载视频"""
        if headers is None:
            headers = {}

        if 'User-Agent' not in headers:
            headers['User-Agent'] = config.USER_AGENT

        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

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

                    return True

        except Exception as e:
            print(f"下载失败: {e}")
            return False
