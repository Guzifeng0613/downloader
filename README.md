# 视频下载器（Video Downloader）

基于 **PySide6** 的 Windows 桌面视频下载工具，以 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 为核心下载引擎，支持多平台视频解析、画质选择、播放列表批量下载，并能通过浏览器 Cookie 处理需要登录的内容。

## 功能特性

- **多平台支持**：YouTube、Bilibili（哔哩哔哩）、抖音、腾讯视频、小红书、快手等，其余 yt-dlp 支持的站点也可直接解析。
- **画质选择**：支持按分辨率（如 1080P / 720P）选择下载格式，音视频自动合并。
- **播放列表**：支持整份播放列表下载，或勾选播放列表中的指定条目。
- **浏览器 Cookie**：自动读取 Chrome / Edge / Firefox 的 Cookie，可下载需要登录或会员可见的内容，并实时显示 Cookie 可用性状态。
- **浏览器媒体捕获**：通过 CDP（Chrome DevTools Protocol）启动浏览器会话，自动嗅探页面播放的媒体资源。
- **运行时自管理**：首次运行自动下载并安装 `yt-dlp`、`ffmpeg`、`Deno` 到用户目录（`%LOCALAPPDATA%`），支持一键检查与更新，无需手动配置环境变量。
  - `Deno` 作为 yt-dlp 的 JavaScript 运行时（`--js-runtimes`），用于需要执行 JS 的提取器。
- **诊断与容错**：网络、鉴权、Cookie、格式、ffmpeg 缺失等错误自动分类并给出可读提示；诊断信息对 URL、请求头等做脱敏处理；内置指数退避自动重试。
- **图形界面**：GUI 实时展示解析结果与下载进度，下载任务在后台线程执行，界面不卡顿。

## 环境要求

- Windows 10 / 11
- Python 3.11 及以上
- 首次运行需要联网（用于自动下载 yt-dlp、ffmpeg、Deno）

## 快速开始

```powershell
# 1. 克隆仓库
git clone https://github.com/guzifeng123/downloader.git
cd downloader/video_downloader

# 2. 创建并激活虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动程序
python main.py
```

启动后在输入框粘贴视频链接，选择画质与 Cookie 来源，点击「开始下载」即可。下载文件默认保存在 `video_downloader/downloads` 目录，也可在界面中自行选择输出目录。

## 目录结构

```text
downloader/
├── README.md
├── .gitignore
└── video_downloader/            # 主程序
    ├── main.py                  # GUI 入口
    ├── config.py                # 全局配置：平台标识、请求头、浏览器/ffmpeg 路径
    ├── errors.py                # 错误定义与分类
    ├── retry.py                 # 指数退避重试
    ├── requirements.txt         # 运行依赖
    ├── requirements-dev.txt     # 开发/测试依赖
    ├── DOCKER_DEPLOYMENT.md     # Docker 打包与部署说明
    ├── downloader/              # 下载核心
    │   ├── service.py           # yt-dlp 优先的统一下载服务（解析、下载、错误分类）
    │   ├── runtime.py           # yt-dlp / ffmpeg / Deno 的下载、校验与版本管理
    │   ├── browser.py           # 浏览器 CDP 会话管理与媒体资源捕获
    │   └── diagnostics.py       # 诊断信息收集与脱敏
    ├── ui/                      # PySide6 图形界面
    │   ├── main_window.py       # 主窗口
    │   ├── threads.py           # 后台下载线程
    │   └── progress_manager.py  # 进度管理
    ├── platforms/               # 平台扩展（预留）
    ├── utils/                   # 通用工具（预留）
    └── tests/                   # pytest 测试
        ├── test_compatibility.py
        └── test_download_service.py
```

## 运行测试

```powershell
cd video_downloader
pip install -r requirements-dev.txt
pytest
```

## Docker 部署

本项目为桌面 GUI 程序，普通 Windows 用户建议直接运行或打包为 `.exe`；如需使用 Docker 打包与异机部署，请参阅 [`video_downloader/DOCKER_DEPLOYMENT.md`](video_downloader/DOCKER_DEPLOYMENT.md)。

## 免责声明

本工具仅供个人学习与合法下载自有/授权内容使用，请遵守各视频平台的服务条款及相关版权法律法规，勿用于任何侵权或商业用途。
