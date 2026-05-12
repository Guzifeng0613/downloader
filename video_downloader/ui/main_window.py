"""Main window for the video downloader."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import config
from downloader.service import DownloadService
from ui.progress_manager import ProgressManager
from ui.threads import DownloadTask, ParseTask


class MainWindow(QMainWindow):
    """Desktop UI."""

    COLOR_TEXT = "#d7e0e8"
    COLOR_MUTED = "#8d9aa7"
    COLOR_SUCCESS = "#45d483"
    COLOR_WARNING = "#f0b45b"
    COLOR_ERROR = "#ff6b5f"

    COOKIE_LABELS = {
        "不使用": "none",
        "Chrome": "chrome",
        "Edge": "edge",
        "Firefox": "firefox",
    }

    COOKIE_STATUS_LABELS = {
        "disabled": "未启用 Cookie",
        "available": "Cookie 可读取",
        "browser_not_found": "未识别浏览器",
        "cookie_store_not_found": "未找到 Cookie 存储",
        "read_failed": "Cookie 读取失败",
        "maybe_expired": "Cookie 可能已过期",
    }

    def __init__(self):
        super().__init__()
        self.download_tasks = {}
        self.current_video_info = None
        self.progress_manager = ProgressManager()
        self.service = DownloadService()
        self.output_dir = config.get_download_path()

        self._setup_ui()
        self._connect_signals()
        self._refresh_environment_status()

    def _setup_ui(self):
        self.setWindowTitle("视频下载器")
        self.resize(1180, 780)
        self.setMinimumSize(980, 680)
        self._apply_theme()

        central_widget = QWidget()
        central_widget.setObjectName("CentralRoot")
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 18, 20, 16)
        main_layout.setSpacing(14)

        main_layout.addWidget(self._build_header())
        main_layout.addWidget(self._build_url_panel())

        workspace_layout = QHBoxLayout()
        workspace_layout.setSpacing(14)
        workspace_layout.addWidget(self._build_settings_panel(), 0)
        workspace_layout.addWidget(self._build_content_panel(), 1)
        main_layout.addLayout(workspace_layout, 1)

        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label, 1)
        self._set_label_tone(self.status_label, "muted")

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("HeaderPanel")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 14, 18, 14)
        header_layout.setSpacing(12)

        title_block = QVBoxLayout()
        title_block.setSpacing(3)
        title_label = QLabel("视频下载器")
        title_label.setObjectName("AppTitle")
        subtitle_label = QLabel("解析、诊断并下载在线视频资源")
        subtitle_label.setObjectName("AppSubtitle")
        title_block.addWidget(title_label)
        title_block.addWidget(subtitle_label)
        header_layout.addLayout(title_block, 1)

        self.header_state_label = QLabel("READY")
        self.header_state_label.setObjectName("HeaderState")
        self.header_state_label.setAlignment(Qt.AlignCenter)
        self.header_state_label.setMinimumWidth(92)
        header_layout.addWidget(self.header_state_label)

        return header

    def _build_url_panel(self) -> QWidget:
        url_group = QGroupBox("视频地址")
        url_layout = QHBoxLayout(url_group)
        url_layout.setContentsMargins(16, 22, 16, 16)
        url_layout.setSpacing(10)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("粘贴视频或网页链接")
        self.url_input.setMinimumHeight(38)
        url_layout.addWidget(self.url_input, 1)

        self.parse_btn = QPushButton("解析")
        self.parse_btn.setObjectName("PrimaryButton")
        self.parse_btn.setMinimumSize(96, 38)
        url_layout.addWidget(self.parse_btn)

        self.retry_btn = QPushButton("重试")
        self.retry_btn.setMinimumSize(88, 38)
        self.retry_btn.setEnabled(False)
        url_layout.addWidget(self.retry_btn)

        return url_group

    def _build_settings_panel(self) -> QWidget:
        settings_group = QGroupBox("下载设置")
        settings_group.setObjectName("SettingsPanel")
        settings_group.setMinimumWidth(330)
        settings_group.setMaximumWidth(390)

        settings_layout = QVBoxLayout(settings_group)
        settings_layout.setContentsMargins(16, 24, 16, 16)
        settings_layout.setSpacing(12)

        form_layout = QGridLayout()
        form_layout.setHorizontalSpacing(10)
        form_layout.setVerticalSpacing(10)
        form_layout.setColumnStretch(1, 1)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["最佳", "1080P", "720P", "480P", "360P"])
        form_layout.addWidget(self._field_label("画质"), 0, 0)
        form_layout.addWidget(self.quality_combo, 0, 1)

        self.cookie_combo = QComboBox()
        self.cookie_combo.addItems(list(self.COOKIE_LABELS.keys()))
        self.cookie_combo.setCurrentText("Chrome")
        form_layout.addWidget(self._field_label("Cookie"), 1, 0)
        form_layout.addWidget(self.cookie_combo, 1, 1)

        self.fragments_spin = QSpinBox()
        self.fragments_spin.setRange(1, 16)
        self.fragments_spin.setValue(config.CONCURRENT_REQUESTS)
        form_layout.addWidget(self._field_label("并发分片"), 2, 0)
        form_layout.addWidget(self.fragments_spin, 2, 1)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 5)
        self.delay_spin.setValue(0)
        self.delay_spin.setSuffix(" 秒")
        form_layout.addWidget(self._field_label("请求延迟"), 3, 0)
        form_layout.addWidget(self.delay_spin, 3, 1)

        self.candidate_combo = QComboBox()
        self.candidate_combo.setMinimumWidth(220)
        self.candidate_combo.setEnabled(False)
        form_layout.addWidget(self._field_label("候选资源"), 4, 0)
        form_layout.addWidget(self.candidate_combo, 4, 1)
        settings_layout.addLayout(form_layout)

        path_box = QFrame()
        path_box.setObjectName("PathBox")
        path_layout = QVBoxLayout(path_box)
        path_layout.setContentsMargins(12, 10, 12, 12)
        path_layout.setSpacing(8)
        path_layout.addWidget(self._field_label("保存到"))

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.output_label = QLabel(str(self.output_dir))
        self.output_label.setObjectName("OutputPath")
        self.output_label.setWordWrap(True)
        self.output_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_row.addWidget(self.output_label, 1)

        self.choose_dir_btn = QPushButton("选择")
        self.choose_dir_btn.setMinimumHeight(32)
        path_row.addWidget(self.choose_dir_btn)
        path_layout.addLayout(path_row)
        settings_layout.addWidget(path_box)

        checks_layout = QVBoxLayout()
        checks_layout.setSpacing(8)
        self.playlist_check = QCheckBox("下载播放列表")
        self.overwrite_check = QCheckBox("覆盖已有文件")
        self.dynamic_scan_check = QCheckBox("启用动态网页兜底")
        self.dynamic_scan_check.setChecked(True)
        self.diagnostic_check = QCheckBox("启用诊断模式")
        self.diagnostic_check.setChecked(True)
        checks_layout.addWidget(self.playlist_check)
        checks_layout.addWidget(self.overwrite_check)
        checks_layout.addWidget(self.dynamic_scan_check)
        checks_layout.addWidget(self.diagnostic_check)
        settings_layout.addLayout(checks_layout)

        self.env_label = QLabel("")
        self.env_label.setObjectName("EnvStatus")
        self.env_label.setWordWrap(True)
        settings_layout.addWidget(self.env_label)
        settings_layout.addStretch(1)

        self.download_btn = QPushButton("开始下载")
        self.download_btn.setObjectName("PrimaryButton")
        self.download_btn.setMinimumHeight(42)
        self.download_btn.setEnabled(False)
        settings_layout.addWidget(self.download_btn)

        return settings_group

    def _build_content_panel(self) -> QWidget:
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)

        info_group = QGroupBox("视频信息")
        info_layout = QVBoxLayout(info_group)
        info_layout.setContentsMargins(16, 24, 16, 16)
        self.info_label = QLabel("请输入链接并点击解析")
        self.info_label.setObjectName("InfoLabel")
        self.info_label.setWordWrap(True)
        self.info_label.setTextFormat(Qt.RichText)
        self.info_label.setMinimumHeight(74)
        self._set_label_tone(self.info_label, "muted")
        info_layout.addWidget(self.info_label)
        content_layout.addWidget(info_group)

        diag_group = QGroupBox("诊断信息")
        diag_layout = QVBoxLayout(diag_group)
        diag_layout.setContentsMargins(16, 24, 16, 16)
        diag_layout.setSpacing(8)

        self.cookie_status_label = QLabel("Cookie 状态: -")
        self.cookie_status_label.setWordWrap(True)
        self._set_label_tone(self.cookie_status_label, "muted")
        diag_layout.addWidget(self.cookie_status_label)

        self.attempts_label = QLabel("策略链: -")
        self.attempts_label.setWordWrap(True)
        self._set_label_tone(self.attempts_label, "muted")
        diag_layout.addWidget(self.attempts_label)

        self.diagnostic_text = QPlainTextEdit()
        self.diagnostic_text.setReadOnly(True)
        self.diagnostic_text.setPlaceholderText("解析或下载后的诊断摘要会显示在这里")
        self.diagnostic_text.setMinimumHeight(145)
        self.diagnostic_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        diag_layout.addWidget(self.diagnostic_text, 1)
        content_layout.addWidget(diag_group, 1)

        progress_group = QGroupBox("下载进度")
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setContentsMargins(16, 24, 16, 16)
        self.progress_list = QListWidget()
        self.progress_list.setObjectName("ProgressList")
        self.progress_list.setAlternatingRowColors(True)
        self.progress_list.setMinimumHeight(150)
        progress_layout.addWidget(self.progress_list)
        content_layout.addWidget(progress_group, 1)

        return content

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return label

    def _apply_theme(self):
        self.setStyleSheet(
            """
            QMainWindow {
                background: #0f1418;
            }
            QWidget#CentralRoot {
                background: #0f1418;
                color: #d7e0e8;
                font-size: 13px;
            }
            QFrame#HeaderPanel {
                background: #121a20;
                border: 1px solid #1e2b33;
                border-radius: 10px;
            }
            QLabel#AppTitle {
                color: #f3f7f8;
                font-size: 24px;
                font-weight: 700;
            }
            QLabel#AppSubtitle {
                color: #8d9aa7;
                font-size: 12px;
            }
            QLabel#HeaderState {
                background: #10251f;
                border: 1px solid #1d6b54;
                border-radius: 12px;
                color: #45d483;
                font-size: 11px;
                font-weight: 700;
                padding: 5px 10px;
            }
            QGroupBox {
                background: #141b21;
                border: 1px solid #24313a;
                border-radius: 8px;
                color: #d7e0e8;
                font-weight: 700;
                margin-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 6px;
                color: #b9c6cf;
                background: #141b21;
            }
            QGroupBox#SettingsPanel {
                background: #12191f;
            }
            QLabel {
                color: #d7e0e8;
            }
            QLabel#FieldLabel {
                color: #8d9aa7;
                font-size: 12px;
                font-weight: 700;
            }
            QLabel#OutputPath,
            QLabel#EnvStatus,
            QLabel#InfoLabel {
                line-height: 145%;
            }
            QLabel#OutputPath {
                color: #b9c6cf;
                font-size: 12px;
            }
            QFrame#PathBox {
                background: #0f171d;
                border: 1px solid #202e37;
                border-radius: 7px;
            }
            QLineEdit,
            QComboBox,
            QSpinBox,
            QPlainTextEdit,
            QListWidget {
                background: #0d1318;
                border: 1px solid #273640;
                border-radius: 7px;
                color: #d7e0e8;
                selection-background-color: #1f8f72;
                selection-color: #f6fffc;
            }
            QLineEdit,
            QComboBox,
            QSpinBox {
                min-height: 32px;
                padding: 4px 10px;
            }
            QLineEdit:focus,
            QComboBox:focus,
            QSpinBox:focus,
            QPlainTextEdit:focus,
            QListWidget:focus {
                border: 1px solid #24b891;
            }
            QLineEdit::placeholder {
                color: #64717c;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #8d9aa7;
                margin-right: 8px;
            }
            QComboBox QAbstractItemView {
                background: #111920;
                border: 1px solid #273640;
                border-radius: 6px;
                color: #d7e0e8;
                selection-background-color: #1f8f72;
                outline: 0;
            }
            QSpinBox::up-button,
            QSpinBox::down-button {
                background: transparent;
                border: none;
                width: 18px;
            }
            QPlainTextEdit {
                padding: 10px;
                font-family: "Cascadia Mono", "Consolas", monospace;
                font-size: 12px;
                line-height: 140%;
            }
            QListWidget {
                padding: 6px;
                outline: none;
                alternate-background-color: #101820;
            }
            QListWidget::item {
                border-radius: 6px;
                padding: 8px 10px;
                margin: 2px 0;
            }
            QListWidget::item:selected {
                background: #153e34;
                color: #f6fffc;
            }
            QPushButton {
                background: #202b33;
                border: 1px solid #2d3b45;
                border-radius: 7px;
                color: #d7e0e8;
                font-weight: 700;
                padding: 7px 14px;
            }
            QPushButton:hover {
                background: #263640;
                border-color: #3d515c;
            }
            QPushButton:pressed {
                background: #19232a;
                padding-top: 8px;
                padding-bottom: 6px;
            }
            QPushButton:disabled {
                background: #151c22;
                border-color: #202a32;
                color: #5f6b74;
            }
            QPushButton#PrimaryButton {
                background: #1f8f72;
                border-color: #24b891;
                color: #f7fffc;
            }
            QPushButton#PrimaryButton:hover {
                background: #24a985;
                border-color: #36d5aa;
            }
            QPushButton#PrimaryButton:pressed {
                background: #18775f;
            }
            QPushButton#PrimaryButton:disabled {
                background: #183329;
                border-color: #1e4639;
                color: #6f9488;
            }
            QCheckBox {
                color: #c9d4dc;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid #40515d;
                background: #0d1318;
            }
            QCheckBox::indicator:hover {
                border-color: #24b891;
            }
            QCheckBox::indicator:checked {
                background: #1f8f72;
                border-color: #24b891;
            }
            QStatusBar {
                background: #0f1418;
                border-top: 1px solid #1f2a32;
                color: #8d9aa7;
            }
            QStatusBar::item {
                border: none;
            }
            QScrollBar:vertical {
                background: #0d1318;
                width: 10px;
                margin: 2px;
            }
            QScrollBar::handle:vertical {
                background: #2a3a44;
                border-radius: 5px;
                min-height: 24px;
            }
            QScrollBar::handle:vertical:hover {
                background: #3c505c;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0;
            }
            """
        )

    def _tone_color(self, tone: str) -> str:
        return {
            "text": self.COLOR_TEXT,
            "muted": self.COLOR_MUTED,
            "success": self.COLOR_SUCCESS,
            "warning": self.COLOR_WARNING,
            "error": self.COLOR_ERROR,
        }.get(tone, self.COLOR_TEXT)

    def _set_label_tone(self, label: QLabel, tone: str = "text"):
        label.setStyleSheet(f"color: {self._tone_color(tone)};")

    def _set_activity_state(self, text: str, tone: str = "muted"):
        self.status_label.setText(text)
        self._set_label_tone(self.status_label, tone)
        self.header_state_label.setText(text.upper()[:18])
        background = {
            "success": "#10251f",
            "warning": "#2b2112",
            "error": "#2a1717",
        }.get(tone, "#172029")
        self.header_state_label.setStyleSheet(
            f"""
            background: {background};
            border: 1px solid {self._tone_color(tone)};
            border-radius: 12px;
            color: {self._tone_color(tone)};
            font-size: 11px;
            font-weight: 700;
            padding: 5px 10px;
            """
        )

    def _connect_signals(self):
        self.parse_btn.clicked.connect(self._on_parse_clicked)
        self.retry_btn.clicked.connect(self._on_retry_clicked)
        self.download_btn.clicked.connect(self._on_download_clicked)
        self.choose_dir_btn.clicked.connect(self._on_choose_dir_clicked)
        self.url_input.returnPressed.connect(self._on_parse_clicked)

        self.progress_manager.progress_updated.connect(self._on_progress_updated)
        self.progress_manager.task_completed.connect(self._on_task_completed)
        self.progress_manager.task_error.connect(self._on_task_error)

    def _refresh_environment_status(self):
        ffmpeg_path = self.service.get_ffmpeg_path()
        if ffmpeg_path:
            self.env_label.setText(f"ffmpeg: 已检测到 ({ffmpeg_path})")
            self._set_label_tone(self.env_label, "success")
        else:
            self.env_label.setText("ffmpeg: 未检测到。高清合并、HLS/DASH 下载可能失败。")
            self._set_label_tone(self.env_label, "warning")

    def _on_choose_dir_clicked(self):
        selected = QFileDialog.getExistingDirectory(self, "选择保存目录", str(self.output_dir))
        if selected:
            self.output_dir = Path(selected)
            self.output_label.setText(str(self.output_dir))

    def _on_retry_clicked(self):
        if self.url_input.text().strip():
            self._on_parse_clicked()

    def _on_parse_clicked(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入视频链接")
            return

        self.current_video_info = None
        self.download_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self.parse_btn.setEnabled(False)
        self.parse_btn.setText("解析中...")
        self.info_label.setText("正在解析视频信息...")
        self._set_label_tone(self.info_label, "muted")
        self._set_activity_state("解析中", "warning")
        self.cookie_status_label.setText("Cookie 状态: 检查中...")
        self._set_label_tone(self.cookie_status_label, "warning")
        self.attempts_label.setText("策略链: 解析中...")
        self._set_label_tone(self.attempts_label, "warning")
        self.diagnostic_text.clear()
        self._populate_candidate_combo([])

        self.parse_task = ParseTask(
            url=url,
            cookie_browser=self._cookie_browser(),
            concurrent_fragments=self.fragments_spin.value(),
            download_playlist=self.playlist_check.isChecked(),
            diagnostic_mode=self.diagnostic_check.isChecked(),
            dynamic_scan_enabled=self.dynamic_scan_check.isChecked(),
            request_delay_seconds=float(self.delay_spin.value()),
        )
        self.parse_task.signals.finished.connect(self._on_parse_finished)
        self.parse_task.signals.error.connect(self._on_parse_error)
        self.parse_task.start()

    def _on_parse_finished(self, data: dict):
        self.parse_btn.setEnabled(True)
        self.parse_btn.setText("解析")
        self.retry_btn.setEnabled(True)
        self.current_video_info = data

        title = data.get("title", "未知标题")
        duration = data.get("duration_str", "未知")
        uploader = data.get("uploader", "未知")
        extractor = data.get("extractor", "generic")
        formats = data.get("formats", [])
        candidates = data.get("candidates", [])
        warning = data.get("warning", "")

        info_text = (
            f"<span style='font-size:18px; font-weight:700; color:{self.COLOR_TEXT};'>"
            f"{self._escape(title)}</span><br>"
        )
        info_text += (
            f"<span style='color:{self.COLOR_MUTED};'>"
            f"作者: {self._escape(uploader)} &nbsp;|&nbsp; 时长: {duration}<br>"
            f"平台: {self._escape(extractor)} &nbsp;|&nbsp; 可用格式: {len(formats)}"
            f"</span>"
        )
        if data.get("requires_ffmpeg") and not data.get("ffmpeg_available"):
            info_text += f"<br><span style='color:{self.COLOR_WARNING};'>需要 ffmpeg，当前未检测到。</span>"
        if warning:
            info_text += f"<br><span style='color:{self.COLOR_WARNING};'>{self._escape(warning)}</span>"
        if data.get("restriction_status"):
            info_text += f"<br><span style='color:{self.COLOR_ERROR};'>{self._escape(data['restriction_status'])}</span>"
        self.info_label.setText(info_text)
        self._set_label_tone(self.info_label, "text")

        self._populate_quality_combo(formats)
        self._populate_candidate_combo(candidates)
        self.download_btn.setEnabled(bool(formats) and not data.get("restriction_status"))
        self._set_activity_state(f"解析完成 {len(formats)} 格式", "success")

        cookie_label = self.COOKIE_STATUS_LABELS.get(data.get("cookie_status", ""), data.get("cookie_status", "未知"))
        self.cookie_status_label.setText(f"Cookie 状态: {cookie_label}")
        self._set_label_tone(self.cookie_status_label, "success" if data.get("cookie_status") == "available" else "muted")
        self.attempts_label.setText(f"策略链: {self._attempts_text(data.get('attempts', []))}")
        self._set_label_tone(self.attempts_label, "muted")
        diagnostic = data.get("diagnostic_summary", "")
        if data.get("diagnostic_log"):
            diagnostic = f"{diagnostic}\n\n{data['diagnostic_log']}".strip()
        if not diagnostic:
            diagnostic = "没有额外诊断日志。"
        self.diagnostic_text.setPlainText(diagnostic)

    def _on_parse_error(self, error: str):
        self.parse_btn.setEnabled(True)
        self.parse_btn.setText("解析")
        self.retry_btn.setEnabled(True)
        self.info_label.setText(f"<span style='color:{self.COLOR_ERROR};'>解析失败: {self._escape(error)}</span>")
        self._set_label_tone(self.info_label, "error")
        self._set_activity_state("解析失败", "error")
        self.download_btn.setEnabled(False)
        self.attempts_label.setText("策略链: 解析失败")
        self._set_label_tone(self.attempts_label, "error")
        self.cookie_status_label.setText("Cookie 状态: -")
        self._set_label_tone(self.cookie_status_label, "muted")
        self.diagnostic_text.setPlainText(error or "解析失败")
        self._populate_candidate_combo([])

    def _on_download_clicked(self):
        if not self.current_video_info:
            return

        url = self.current_video_info.get("source_url") or self.url_input.text().strip().strip("'\"")
        title = self.current_video_info.get("title", "video")
        quality = self.quality_combo.currentText()
        task_id = f"task_{len(self.download_tasks)}"

        self.download_btn.setEnabled(False)
        self.download_btn.setText("下载中...")
        self.progress_manager.add_task(task_id, url, title)
        self.progress_manager.set_title(task_id, title)

        item = QListWidgetItem(f"{title} - 等待中...")
        item.setData(Qt.UserRole, task_id)
        self.progress_list.addItem(item)

        task = DownloadTask(
            task_id=task_id,
            url=url,
            output_dir=str(self.output_dir),
            quality=quality,
            cookie_browser=self._cookie_browser(),
            concurrent_fragments=self.fragments_spin.value(),
            download_playlist=self.playlist_check.isChecked(),
            overwrite=self.overwrite_check.isChecked(),
            diagnostic_mode=self.diagnostic_check.isChecked(),
            dynamic_scan_enabled=self.dynamic_scan_check.isChecked(),
            request_delay_seconds=float(self.delay_spin.value()),
            candidate_url=self._selected_candidate_url(),
        )
        task.signals.started.connect(self._on_download_started)
        task.signals.progress.connect(self._on_download_progress)
        task.signals.finished.connect(self._on_download_finished)
        task.signals.error.connect(self._on_download_error)

        self.download_tasks[task_id] = task
        task.start()

        self._set_activity_state("下载中", "warning")

    def _on_download_started(self, data: dict):
        task_id = data.get("task_id")
        self._set_activity_state(f"任务 {task_id} 已开始", "warning")

    def _on_download_progress(self, data: dict):
        task_id = data.get("task_id")
        self.progress_manager.update_progress(task_id, data)

    def _on_download_finished(self, data: dict):
        task_id = data.get("task_id")
        self.progress_manager.complete_task(task_id, data)
        self.download_tasks.pop(task_id, None)
        self._reset_download_button()
        attempts = data.get("attempts", [])
        diagnostic = data.get("diagnostic_summary", "")
        self.attempts_label.setText(f"策略链: {self._attempts_text(attempts)}")
        self._set_label_tone(self.attempts_label, "success")
        self.diagnostic_text.setPlainText(diagnostic or "下载完成")

    def _on_download_error(self, data: dict):
        task_id = data.get("task_id")
        error = data.get("error", "未知错误")
        self.progress_manager.error_task(task_id, error)
        self.download_tasks.pop(task_id, None)
        self._reset_download_button()
        self._set_activity_state("下载失败", "error")
        self.diagnostic_text.setPlainText(error)

    def _reset_download_button(self):
        self.download_btn.setText("开始下载")
        self.download_btn.setEnabled(bool(self.current_video_info) and not self.current_video_info.get("restriction_status"))

    def _on_progress_updated(self, task_id: str, data: dict):
        for i in range(self.progress_list.count()):
            item = self.progress_list.item(i)
            if item.data(Qt.UserRole) == task_id:
                title = data.get("title", "")
                progress = data.get("progress", 0)
                speed_str = data.get("speed_str", "")
                size_str = data.get("size_str", "")
                suffix = f"{progress:.1f}%"
                if size_str:
                    suffix += f" | {size_str}"
                if speed_str:
                    suffix += f" | {speed_str}"
                item.setText(f"{title} - {suffix}")
                break

    def _on_task_completed(self, task_id: str, data: dict):
        for i in range(self.progress_list.count()):
            item = self.progress_list.item(i)
            if item.data(Qt.UserRole) == task_id:
                title = data.get("title", "")
                output_path = data.get("output_path", "")
                text = f"[完成] {title}"
                if output_path:
                    text += f" - {output_path}"
                item.setText(text)
                item.setForeground(QColor(self.COLOR_SUCCESS))
                break
        self._set_activity_state("下载完成", "success")

    def _on_task_error(self, task_id: str, error_message: str):
        for i in range(self.progress_list.count()):
            item = self.progress_list.item(i)
            if item.data(Qt.UserRole) == task_id:
                task = self.progress_manager.tasks.get(task_id)
                if task:
                    item.setText(f"[失败] {task.title} - {error_message}")
                else:
                    item.setText(f"[失败] {error_message}")
                item.setForeground(QColor(self.COLOR_ERROR))
                break
        self._set_activity_state("下载失败", "error")

    def _populate_quality_combo(self, formats):
        self.quality_combo.clear()
        heights = sorted({fmt.get("height") for fmt in formats if fmt.get("height")}, reverse=True)
        items = ["最佳"]
        for height in heights:
            label = f"{height}P"
            if label not in items:
                items.append(label)

        if len(items) == 1:
            for fallback in ("1080P", "720P", "480P", "360P"):
                items.append(fallback)
        self.quality_combo.addItems(items)

    def _populate_candidate_combo(self, candidates):
        self.candidate_combo.clear()
        if not candidates:
            self.candidate_combo.setEnabled(False)
            self.candidate_combo.addItem("自动选择")
            return

        self.candidate_combo.addItem("自动选择", "")
        for candidate in candidates:
            self.candidate_combo.addItem(candidate.get("display_name", candidate.get("url", "")), candidate.get("url", ""))
        self.candidate_combo.setEnabled(True)

    def _selected_candidate_url(self) -> str:
        return str(self.candidate_combo.currentData() or "")

    def _attempts_text(self, attempts):
        if not attempts:
            return "-"
        parts = []
        for attempt in attempts:
            marker = "ok" if attempt.get("success") else attempt.get("category", "failed")
            parts.append(f"{attempt.get('strategy', 'unknown')}:{marker}")
        return " -> ".join(parts)

    def _cookie_browser(self) -> str:
        return self.COOKIE_LABELS.get(self.cookie_combo.currentText(), "none")

    def _escape(self, value) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
