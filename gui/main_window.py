"""主窗口：左侧导航 + 右页堆栈 + 底部日志。

界面与协议之间只有两个接触点：`set_client` 和 `run`。前者把连接交给各页，
后者把阻塞操作丢到后台线程。除此之外界面不认识 UDS。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from .pages import (ConnectionPage, DidPage, DtcPage, FlashPage, MonitorPage,
                    describe_link)
from .worker import TaskRunner


class MainWindow(QMainWindow):
    # 后台线程要更新界面时，发这个信号而不是直接碰控件：
    # 跨线程操作控件在 Qt 里是未定义行为，表现为偶发崩溃或花屏
    _post = Signal(object)

    def __init__(self, client_factory=None):
        super().__init__()
        self.setWindowTitle("can-studio — CAN/UDS 诊断上位机")
        self.resize(1000, 680)

        self.client = None
        self.runner = TaskRunner(self)
        self._post.connect(lambda fn: fn())

        # 状态栏要在页面之前建好：页面构造时就会往这里写提示
        self.status = QStatusBar()
        self.connected_label = QLabel("未连接")
        self.status.addPermanentWidget(self.connected_label)
        self.setStatusBar(self.status)

        self.pages = [
            ConnectionPage(self),
            DidPage(self),
            DtcPage(self),
            FlashPage(self),
            MonitorPage(self),
        ]

        self.nav = QListWidget()
        self.nav.setFixedWidth(150)
        for page in self.pages:
            self.nav.addItem(page.title)
        self.nav.currentRowChanged.connect(self.switch_page)

        self.stack = QStackedWidget()
        for page in self.pages:
            self.stack.addWidget(page)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setPlaceholderText("操作日志")

        splitter = QSplitter(Qt.Vertical)
        body = QSplitter(Qt.Horizontal)
        body.addWidget(self.nav)
        body.addWidget(self.stack)
        body.setSizes([150, 850])

        splitter.addWidget(body)
        splitter.addWidget(self.log_view)
        splitter.setSizes([520, 160])
        self.setCentralWidget(splitter)

        self._add_menu()
        self.nav.setCurrentRow(0)

    def _add_menu(self):
        file_menu = self.menuBar().addMenu("文件")
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("帮助")
        about = QAction("关于", self)
        about.triggered.connect(lambda: self.log(
            "can-studio — CAN/UDS 诊断上位机；协议层 ISO 14229，传输层 ISO 15765-2"))
        help_menu.addAction(about)

    # ---------------- 页面切换 ----------------

    def switch_page(self, index):
        if 0 <= index < len(self.pages):
            self.stack.setCurrentIndex(index)
            self.pages[index].on_enter()

    # ---------------- 连接管理 ----------------

    def set_client(self, client):
        self.client = client
        for page in self.pages:
            page.set_client(client)
        if client is None:
            self.connected_label.setText("未连接")
        else:
            self.connected_label.setText(f"已连接 {describe_link(client.link)}")

    def disconnect_client(self):
        if self.client is not None:
            try:
                self.client.link.close()
            except Exception:
                pass
        self.set_client(None)
        self.log("已断开")

    def require_client(self):
        if self.client is None:
            self.log("请先在「连接」页建立连接")
            return None
        return self.client

    # ---------------- 后台任务与日志 ----------------

    def run(self, fn, on_ok=None, on_err=None):
        return self.runner.run(fn, on_ok, on_err)

    def post(self, fn):
        """从后台线程安全地回到界面线程执行 fn。"""
        self._post.emit(fn)

    def closeEvent(self, event):
        # 先把后台线程收干净再让窗口走
        self.runner.wait_all()
        super().closeEvent(event)

    def log(self, message):
        self.log_view.appendPlainText(message)
        self.status.showMessage(message, 5000)


def create_app(argv=None):
    app = QApplication.instance() or QApplication(argv or [])
    app.setApplicationName("can-studio")
    # Fusion 在三个平台上观感一致，避免各平台原生样式差异影响截图
    app.setStyle("Fusion")
    return app
