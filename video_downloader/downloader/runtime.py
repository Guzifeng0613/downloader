"""Versioned, independently updatable downloader runtime management."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.request import Request, urlopen


ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True)
class ReleaseInfo:
    component: str
    version: str
    download_url: str
    checksum_url: str
    archive_member: str = ""


@dataclass
class ComponentStatus:
    name: str
    version: str = ""
    path: str = ""
    installed: bool = False
    healthy: bool = False
    managed: bool = False
    message: str = ""
    update_available: str = ""


class RuntimeError(Exception):
    pass


class RuntimeManager:
    """Manage yt-dlp, ffmpeg/ffprobe and Deno under LOCALAPPDATA.

    Every downloaded archive or executable is SHA-256 verified before it can be
    activated. Activation keeps the previous version for one-click rollback.
    """

    COMPONENTS = ("yt-dlp", "ffmpeg", "deno")
    VERSION_COMMANDS = {
        "yt-dlp": ("--version",),
        "ffmpeg": ("-version",),
        "deno": ("--version",),
    }
    EXECUTABLE_NAMES = {
        "yt-dlp": "yt-dlp.exe",
        "ffmpeg": "ffmpeg.exe",
        "deno": "deno.exe",
    }

    def __init__(self, base_dir: str | Path | None = None, timeout: int = 45):
        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        self.base_dir = Path(base_dir) if base_dir else Path(local_app_data) / "VideoDownloader" / "runtime"
        self.timeout = timeout
        self.registry_path = self.base_dir / "active.json"
        self.plugin_dir = self.base_dir / "plugins"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.plugin_dir.mkdir(parents=True, exist_ok=True)

    def executable(self, component: str) -> str:
        record = self._registry().get(component, {})
        candidate = Path(record.get("path") or "")
        if candidate.is_file():
            return str(candidate)
        command = self.EXECUTABLE_NAMES.get(component, component)
        beside_python = Path(sys.executable).with_name(command)
        if beside_python.is_file():
            return str(beside_python)
        return shutil.which(command) or shutil.which(Path(command).stem) or ""

    @property
    def yt_dlp_path(self) -> str:
        return self.executable("yt-dlp")

    @property
    def ffmpeg_path(self) -> str:
        return self.executable("ffmpeg")

    @property
    def ffprobe_path(self) -> str:
        ffmpeg = self.ffmpeg_path
        if ffmpeg:
            sibling = Path(ffmpeg).with_name("ffprobe.exe")
            if sibling.exists():
                return str(sibling)
        return shutil.which("ffprobe.exe") or shutil.which("ffprobe") or ""

    @property
    def deno_path(self) -> str:
        return self.executable("deno")

    @property
    def ejs_plugin_dir(self) -> str:
        return str(self.plugin_dir)

    def status(self, component: str) -> ComponentStatus:
        path = self.executable(component)
        registry = self._registry().get(component, {})
        managed = bool(path and self._is_within(Path(path), self.base_dir))
        status = ComponentStatus(
            name=component,
            version=str(registry.get("version") or ""),
            path=path,
            installed=bool(path),
            managed=managed,
        )
        if not path:
            status.message = "未安装"
            return status
        healthy, version, message = self.health_check(component, path)
        if not healthy and managed:
            previous = registry.get("previous") or {}
            previous_path = Path(previous.get("path") or "")
            if previous_path.is_file():
                previous_healthy, previous_version, previous_message = self.health_check(
                    component, previous_path
                )
                if previous_healthy:
                    all_records = self._registry()
                    all_records[component] = previous
                    self._write_registry(all_records)
                    status.path = str(previous_path)
                    status.healthy = True
                    status.version = previous_version or str(previous.get("version") or "")
                    status.message = f"当前版本健康检查失败，已自动回滚；{previous_message}"
                    return status
        status.healthy = healthy
        status.version = version or status.version
        status.message = message
        return status

    def statuses(self) -> dict[str, ComponentStatus]:
        return {name: self.status(name) for name in self.COMPONENTS}

    def health_check(self, component: str, path: str | Path | None = None) -> tuple[bool, str, str]:
        executable = str(path or self.executable(component))
        if not executable:
            return False, "", "未安装"
        try:
            process = subprocess.run(
                [executable, *self.VERSION_COMMANDS[component]],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, "", f"健康检查失败: {exc}"
        output = (process.stdout or process.stderr or "").strip().splitlines()
        version = self._version_from_output(component, output[0] if output else "")
        if process.returncode == 0:
            return True, version, "正常"
        return False, version, f"退出码 {process.returncode}"

    def latest_release(self, component: str) -> ReleaseInfo:
        if component == "yt-dlp":
            tag = self._latest_github_tag("yt-dlp/yt-dlp")
            version = tag.lstrip("v")
            base = f"https://github.com/yt-dlp/yt-dlp/releases/download/{tag}"
            return ReleaseInfo(component, version, f"{base}/yt-dlp.exe", f"{base}/SHA2-256SUMS")
        if component == "deno":
            tag = self._latest_github_tag("denoland/deno")
            version = tag.lstrip("v")
            name = "deno-x86_64-pc-windows-msvc.zip"
            base = f"https://github.com/denoland/deno/releases/download/{tag}"
            return ReleaseInfo(
                component,
                version,
                f"{base}/{name}",
                f"{base}/{name}.sha256sum",
                "deno.exe",
            )
        if component == "ffmpeg":
            version_text = self._read_text("https://www.gyan.dev/ffmpeg/builds/release-version").strip()
            version = version_text.splitlines()[0].strip().lstrip("v")
            url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
            return ReleaseInfo(component, version, url, f"{url}.sha256")
        raise ValueError(f"未知运行时组件: {component}")

    def check_updates(self) -> dict[str, ComponentStatus]:
        result = self.statuses()
        for name, status in result.items():
            try:
                release = self.latest_release(name)
                if not status.version or self._version_key(release.version) > self._version_key(status.version):
                    status.update_available = release.version
            except Exception as exc:
                status.message = f"{status.message}；更新检查失败: {exc}".strip("；")
        return result

    def ensure_all(self, progress: ProgressCallback | None = None) -> dict[str, ComponentStatus]:
        for component in self.COMPONENTS:
            status = self.status(component)
            if not status.managed or not status.healthy:
                self.install(component, progress=progress)
        return self.statuses()

    def install(
        self,
        component: str,
        release: ReleaseInfo | None = None,
        progress: ProgressCallback | None = None,
    ) -> ComponentStatus:
        release = release or self.latest_release(component)
        component_root = self.base_dir / component
        target_dir = component_root / release.version
        staging = Path(tempfile.mkdtemp(prefix=f"{component}-", dir=self.base_dir))
        try:
            artifact = staging / Path(release.download_url).name
            self._download(release.download_url, artifact, component, progress)
            expected = self._expected_checksum(release.checksum_url, artifact.name)
            actual = self._sha256(artifact)
            if actual.lower() != expected.lower():
                raise RuntimeError(
                    f"{component} SHA-256 校验失败: expected={expected}, actual={actual}"
                )
            prepared = staging / "prepared"
            prepared.mkdir()
            if artifact.suffix.lower() == ".zip":
                self._safe_extract(artifact, prepared)
            else:
                shutil.copy2(artifact, prepared / self.EXECUTABLE_NAMES[component])
            executable = self._locate_executable(prepared, component)
            healthy, _, message = self.health_check(component, executable)
            if not healthy:
                raise RuntimeError(f"{component} 新版本健康检查失败: {message}")
            if target_dir.exists():
                shutil.rmtree(target_dir)
            shutil.move(str(prepared), str(target_dir))
            installed_executable = self._locate_executable(target_dir, component)
            if component == "ffmpeg":
                ffprobe = next(target_dir.rglob("ffprobe.exe"), None)
                if not ffprobe:
                    raise RuntimeError("ffmpeg 包中缺少 ffprobe.exe")
            self.activate(component, release.version, installed_executable)
            return self.status(component)
        except Exception:
            self.rollback(component, only_if_unhealthy=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def activate(self, component: str, version: str, executable: str | Path) -> None:
        executable = Path(executable).resolve()
        if not self._is_within(executable, self.base_dir):
            raise RuntimeError("拒绝激活运行时目录之外的文件")
        registry = self._registry()
        current = registry.get(component)
        registry[component] = {
            "version": version,
            "path": str(executable),
            "previous": current if current and current.get("path") != str(executable) else current.get("previous", {}) if current else {},
        }
        self._write_registry(registry)

    def rollback(self, component: str, only_if_unhealthy: bool = False) -> ComponentStatus:
        registry = self._registry()
        current = registry.get(component, {})
        if only_if_unhealthy and current.get("path"):
            healthy, _, _ = self.health_check(component, current["path"])
            if healthy:
                return self.status(component)
        previous = current.get("previous") or {}
        if not previous.get("path") or not Path(previous["path"]).exists():
            return self.status(component)
        registry[component] = previous
        self._write_registry(registry)
        return self.status(component)

    def describe(self) -> dict[str, dict]:
        return {name: asdict(status) for name, status in self.statuses().items()}

    def _registry(self) -> dict:
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_registry(self, value: dict) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.registry_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.registry_path)

    def _read_json(self, url: str) -> dict:
        return json.loads(self._read_text(url))

    def _latest_github_tag(self, repository: str) -> str:
        request = Request(
            f"https://github.com/{repository}/releases/latest",
            headers={"User-Agent": "VideoDownloader/2.0"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            final_url = response.geturl()
        tag = final_url.rstrip("/").rsplit("/", 1)[-1]
        if not tag or tag == "latest":
            raise RuntimeError(f"无法解析 {repository} 的最新版本")
        return tag

    def _read_text(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": "VideoDownloader/2.0"})
        with urlopen(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    def _download(self, url: str, target: Path, component: str, progress: ProgressCallback | None) -> None:
        request = Request(url, headers={"User-Agent": "VideoDownloader/2.0"})
        with urlopen(request, timeout=self.timeout) as response, target.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            completed = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                completed += len(chunk)
                if progress:
                    progress(component, completed, total)

    def _expected_checksum(self, url: str, filename: str) -> str:
        text = self._read_text(url)
        discovered: list[str] = []
        for line in text.splitlines():
            matches = re.findall(r"\b([a-fA-F0-9]{64})\b", line)
            discovered.extend(matches)
            if filename in line and matches:
                return matches[0]
        unique = list(dict.fromkeys(discovered))
        if len(unique) == 1:
            return unique[0]
        raise RuntimeError(f"校验文件中没有 {filename} 的 SHA-256")

    def _safe_extract(self, archive: Path, target: Path) -> None:
        target_resolved = target.resolve()
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                destination = (target / member.filename).resolve()
                if not self._is_within(destination, target_resolved):
                    raise RuntimeError("压缩包包含不安全路径")
            bundle.extractall(target)

    def _locate_executable(self, root: Path, component: str) -> Path:
        name = self.EXECUTABLE_NAMES[component]
        candidates = list(root.rglob(name))
        if not candidates:
            raise RuntimeError(f"安装包中缺少 {name}")
        return candidates[0]

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _version_from_output(self, component: str, output: str) -> str:
        if component == "yt-dlp":
            return output.strip()
        match = re.search(r"(?:version\s+|deno\s+)(v?[0-9][^\s]*)", output, re.IGNORECASE)
        return match.group(1).lstrip("v") if match else output.strip()

    def _version_key(self, version: str) -> tuple:
        return tuple(int(item) for item in re.findall(r"\d+", version or "0"))

    def _is_within(self, path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False
