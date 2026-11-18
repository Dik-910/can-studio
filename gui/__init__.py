"""Qt 界面层。只依赖 canstudio 的公开接口，不含协议编码。"""

from .main_window import MainWindow, create_app

__all__ = ["MainWindow", "create_app"]
