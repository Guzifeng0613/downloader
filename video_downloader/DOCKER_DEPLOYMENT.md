# Docker 打包与异机部署完整流程

本文档适用于当前 `video_downloader` 项目。项目是基于 `PySide6` 的桌面 GUI 程序，并在下载流程中依赖 `yt-dlp`、`ffmpeg`，部分动态扫描能力还会依赖浏览器会话。

> 重要说明：Docker 更适合部署服务端或命令行程序。当前项目是 GUI 桌面程序，容器运行时需要额外配置显示服务。若只是给普通 Windows 用户使用，优先考虑打包为 `.exe`；若必须用 Docker，请按本文流程执行。

## 1. 本机准备

### 1.1 安装 Docker

在打包电脑安装 Docker Desktop：

- Windows：安装 Docker Desktop，并启用 WSL2 后端。
- Linux：安装 Docker Engine。
- macOS：安装 Docker Desktop。

安装后验证：

```powershell
docker --version
docker compose version
docker run --rm hello-world
```

### 1.2 确认项目目录

进入项目目录：

```powershell
cd C:\Users\39187\Desktop\下载\video_downloader
```

确认入口文件存在：

```powershell
dir main.py
dir requirements.txt
```

## 2. 添加 Dockerfile

在项目根目录创建 `Dockerfile`：

```dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV QT_QPA_PLATFORM=xcb
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    wget \
    ca-certificates \
    libegl1 \
    libgl1 \
    libglib2.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcb-cursor0 \
    libxcb-glx0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-shm0 \
    libxcb-sync1 \
    libxcb-util1 \
    libxcb-xfixes0 \
    libxcb-xinerama0 \
    libxcb-xkb1 \
    libxext6 \
    libxkbcommon0 \
    libxkbcommon-x11-0 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

RUN wget -O /usr/local/bin/yt-dlp https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /downloads

VOLUME ["/downloads"]

CMD ["python", "main.py"]
```

说明：

- `ffmpeg` 用于合并音视频、处理 `m3u8` 等流媒体。
- `yt-dlp` 使用 Linux 可执行文件，不使用项目里 Windows 专用的 `yt-dlp.exe`。
- `PySide6` GUI 需要 X11/显示服务支持。
- `/downloads` 用于挂载下载输出目录。

## 3. 添加 .dockerignore

在项目根目录创建 `.dockerignore`：

```dockerignore
__pycache__/
*.pyc
*.pyo
*.pyd
.Python
.venv/
venv/
env/
.git/
.pytest_cache/
tests/
downloads/
output/
*.log
```

## 4. 本机打包镜像

在项目根目录执行：

```powershell
docker build -t video-downloader:1.0.0 .
```

查看镜像：

```powershell
docker images video-downloader
```

## 5. 本机验证运行

### 5.1 Windows 运行 GUI 容器

Windows 上需要安装一个 X Server，例如 VcXsrv。

1. 安装并启动 VcXsrv。
2. 启动时选择：
   - Multiple windows
   - Start no client
   - 勾选 Disable access control

然后在 PowerShell 中执行：

```powershell
$env:DISPLAY="host.docker.internal:0.0"

docker run --rm `
  -e DISPLAY=$env:DISPLAY `
  -v "${PWD}\downloads:/downloads" `
  video-downloader:1.0.0
```

如果窗口正常弹出，说明镜像可运行。

### 5.2 Linux 运行 GUI 容器

在 Linux 桌面环境中执行：

```bash
xhost +local:docker

docker run --rm \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$PWD/downloads:/downloads" \
  video-downloader:1.0.0
```

### 5.3 macOS 运行 GUI 容器

macOS 需要安装并启动 XQuartz。

```bash
open -a XQuartz
xhost + 127.0.0.1

docker run --rm \
  -e DISPLAY=host.docker.internal:0 \
  -v "$PWD/downloads:/downloads" \
  video-downloader:1.0.0
```

## 6. 导出镜像给其他电脑

在打包电脑执行：

```powershell
docker save video-downloader:1.0.0 -o video-downloader-1.0.0.tar
```

压缩可选：

```powershell
Compress-Archive -Path video-downloader-1.0.0.tar -DestinationPath video-downloader-1.0.0.zip
```

将以下文件拷贝到目标电脑：

- `video-downloader-1.0.0.tar` 或 `video-downloader-1.0.0.zip`
- 本文档
- 可选：用于保存下载文件的 `downloads` 目录

## 7. 其他电脑部署流程

### 7.1 安装 Docker

目标电脑安装 Docker Desktop 或 Docker Engine。

验证：

```powershell
docker --version
docker run --rm hello-world
```

### 7.2 导入镜像

如果拿到的是 `.zip`：

```powershell
Expand-Archive -Path video-downloader-1.0.0.zip -DestinationPath .
```

导入 Docker 镜像：

```powershell
docker load -i video-downloader-1.0.0.tar
```

确认镜像存在：

```powershell
docker images video-downloader
```

### 7.3 创建下载目录

```powershell
mkdir C:\video-downloader-downloads
```

### 7.4 在 Windows 目标电脑运行

先安装并启动 VcXsrv，然后执行：

```powershell
$env:DISPLAY="host.docker.internal:0.0"

docker run --rm `
  --name video-downloader `
  -e DISPLAY=$env:DISPLAY `
  -v "C:\video-downloader-downloads:/downloads" `
  video-downloader:1.0.0
```

### 7.5 在 Linux 目标电脑运行

```bash
mkdir -p ~/video-downloader-downloads
xhost +local:docker

docker run --rm \
  --name video-downloader \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$HOME/video-downloader-downloads:/downloads" \
  video-downloader:1.0.0
```

### 7.6 在 macOS 目标电脑运行

先安装并启动 XQuartz：

```bash
open -a XQuartz
xhost + 127.0.0.1
mkdir -p ~/video-downloader-downloads

docker run --rm \
  --name video-downloader \
  -e DISPLAY=host.docker.internal:0 \
  -v "$HOME/video-downloader-downloads:/downloads" \
  video-downloader:1.0.0
```

## 8. 使用 docker compose 部署

也可以创建 `docker-compose.yml`：

```yaml
services:
  video-downloader:
    image: video-downloader:1.0.0
    container_name: video-downloader
    environment:
      DISPLAY: host.docker.internal:0.0
    volumes:
      - ./downloads:/downloads
```

Windows 运行：

```powershell
mkdir downloads
docker compose up
```

停止：

```powershell
docker compose down
```

## 9. 更新版本流程

### 9.1 重新构建新镜像

修改代码后，在打包电脑执行：

```powershell
docker build -t video-downloader:1.0.1 .
```

### 9.2 导出新镜像

```powershell
docker save video-downloader:1.0.1 -o video-downloader-1.0.1.tar
```

### 9.3 目标电脑导入新镜像

```powershell
docker load -i video-downloader-1.0.1.tar
```

### 9.4 使用新版本运行

```powershell
$env:DISPLAY="host.docker.internal:0.0"

docker run --rm `
  --name video-downloader `
  -e DISPLAY=$env:DISPLAY `
  -v "C:\video-downloader-downloads:/downloads" `
  video-downloader:1.0.1
```

## 10. 常见问题

### 10.1 Qt 报错：Could not load the Qt platform plugin xcb

通常是容器缺少 X11/Qt 依赖。确认 Dockerfile 中已安装以下包：

```text
libxcb-cursor0
libxkbcommon-x11-0
libxcb-xinerama0
libgl1
libegl1
```

然后重新构建镜像：

```powershell
docker build --no-cache -t video-downloader:1.0.0 .
```

### 10.2 Windows 没有弹出窗口

检查：

- VcXsrv 是否已启动。
- VcXsrv 是否勾选 `Disable access control`。
- `DISPLAY` 是否为 `host.docker.internal:0.0`。
- Windows 防火墙是否阻止 VcXsrv。

### 10.3 下载文件没有出现在宿主机

确认运行命令里有挂载目录：

```powershell
-v "C:\video-downloader-downloads:/downloads"
```

应用内下载目录建议选择 `/downloads`。

### 10.4 浏览器 Cookie 或动态扫描不可用

容器内默认无法直接读取宿主机 Chrome、Edge、Firefox 的 Cookie，也无法直接控制宿主机浏览器。可选方案：

- 在宿主机直接运行 Python 程序。
- 将 Cookie 文件导出后挂载进容器，并在代码中支持读取该路径。
- 在容器中安装浏览器并配置远程调试，但这会显著增加镜像复杂度。

### 10.5 yt-dlp 版本过旧

重新构建镜像即可拉取最新 `yt-dlp`：

```powershell
docker build --no-cache -t video-downloader:1.0.1 .
```

## 11. 推荐交付清单

交付给其他电脑时，建议包含：

```text
video-downloader-1.0.0.tar
DOCKER_DEPLOYMENT.md
```

如果需要 compose 方式部署，再附加：

```text
docker-compose.yml
```

## 12. 一键命令汇总

### 打包电脑

```powershell
cd C:\Users\39187\Desktop\下载\video_downloader
docker build -t video-downloader:1.0.0 .
docker save video-downloader:1.0.0 -o video-downloader-1.0.0.tar
```

### 目标 Windows 电脑

```powershell
docker load -i video-downloader-1.0.0.tar
mkdir C:\video-downloader-downloads
$env:DISPLAY="host.docker.internal:0.0"

docker run --rm `
  --name video-downloader `
  -e DISPLAY=$env:DISPLAY `
  -v "C:\video-downloader-downloads:/downloads" `
  video-downloader:1.0.0
```
