#!/usr/bin/python3
"""图形界面入口。

    ./studio.py

依赖 PySide6（界面）与 python-can / can-isotp（链路）。找不到时会提示
安装命令，而不是抛一堆 ImportError 栈。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".pylibs"))

from canstudio.link import ensure_dependencies

ensure_dependencies()


def main():
    try:
        from PySide6.QtWidgets import QMessageBox
    except ImportError:
        print("缺少 PySide6。安装：pip install PySide6-Essentials", file=sys.stderr)
        return 1

    from gui.main_window import MainWindow, create_app

    app = create_app(sys.argv)

    if os.environ.get("QT_QPA_PLATFORM") not in ("offscreen", "minimal"):
        try:
            import can  # noqa: F401
        except ImportError:
            QMessageBox.warning(
                None, "缺少依赖",
                "未找到 python-can / can-isotp。\n\n"
                "pip install python-can can-isotp\n\n"
                "界面仍可打开，但无法连接总线。")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
