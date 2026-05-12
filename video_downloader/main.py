"""视频下载器 - 主入口"""
import sys
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow


def main():
    """主函数"""
    app = QApplication(sys.argv)

    # 设置应用信息
    app.setApplicationName("视频下载器")
    app.setOrganizationName("VideoDownloader")
    app.setFont(QFont("Microsoft YaHei UI", 10))

    # 创建并显示主窗口
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
