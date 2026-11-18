"""五个功能页：连接、DID、故障码、刷写、报文监视。

界面只调用 canstudio 里已经有测试覆盖的接口，不在这里做任何协议编码。
"""
import os
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canstudio import dids
from canstudio import dtc as dtc_mod
from canstudio.flash import Cancelled, FlashJob, load_firmware


def describe_link(link):
    """链路的可读描述。

    不直接取 link.channel：链路接口只保证 send/recv，测试用的假链路没有
    通道名。按有无属性拼装，界面才不会因为换链路实现而崩。
    """
    parts = []
    channel = getattr(link, "channel", None)
    if channel:
        parts.append(str(channel))
    txid = getattr(link, "txid", None)
    rxid = getattr(link, "rxid", None)
    if txid is not None and rxid is not None:
        parts.append(f"0x{txid:03X} → 0x{rxid:03X}")
    return " ".join(parts) if parts else "已连接"


def make_table(headers, stretch_last=True):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    for i in range(len(headers) - 1):
        header.setSectionResizeMode(i, QHeaderView.ResizeToContents)
    if stretch_last:
        header.setSectionResizeMode(len(headers) - 1, QHeaderView.Stretch)
    return table


class Page(QWidget):
    """所有页的基类：拿到主窗口就能用它的后台任务与当前连接。"""

    title = "页面"

    def __init__(self, app):
        super().__init__()
        self.app = app

    def set_client(self, client):
        """连接建立或断开时调用。子类按需重写。"""

    def on_enter(self):
        """切换到本页时调用。"""


class ConnectionPage(Page):
    title = "连接"

    def __init__(self, app):
        super().__init__(app)

        self.iface = QLineEdit("vcan0")
        self.tx = QLineEdit("0x7E0")
        self.rx = QLineEdit("0x7E8")
        self.p2 = QSpinBox()
        self.p2.setRange(20, 5000)
        self.p2.setValue(50)
        self.p2.setSuffix(" ms")

        self.button = QPushButton("连接")
        self.button.clicked.connect(self.toggle)

        self.state = QLabel("未连接")
        self.state.setWordWrap(True)

        form = QGridLayout()
        form.addWidget(QLabel("接口"), 0, 0)
        form.addWidget(self.iface, 0, 1)
        form.addWidget(QLabel("请求 ID"), 1, 0)
        form.addWidget(self.tx, 1, 1)
        form.addWidget(QLabel("响应 ID"), 2, 0)
        form.addWidget(self.rx, 2, 1)
        form.addWidget(QLabel("P2"), 3, 0)
        form.addWidget(self.p2, 3, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.button)
        layout.addWidget(self.state)
        layout.addStretch(1)

    def toggle(self):
        if self.app.client is not None:
            self.app.disconnect_client()
            return

        try:
            iface = self.iface.text().strip()
            tx = int(self.tx.text(), 0)
            rx = int(self.rx.text(), 0)
        except ValueError as exc:
            self.state.setText(f"参数不合法：{exc}")
            return

        self.button.setEnabled(False)
        self.state.setText("连接中…")

        def work():
            from canstudio.link import SocketCanLink
            from canstudio.uds import UdsClient

            link = SocketCanLink(channel=iface, txid=tx, rxid=rx)
            return UdsClient(link, p2_ms=self.p2.value())

        self.app.run(work, self.on_connected, self.on_failed)

    def on_connected(self, client):
        self.app.set_client(client)
        self.button.setText("断开")
        self.button.setEnabled(True)
        self.state.setText(f"已连接 {describe_link(client.link)}")
        self.app.log(f"已连接 {describe_link(client.link)}")

    def on_failed(self, message):
        self.button.setEnabled(True)
        self.state.setText(f"连接失败：{message}")
        self.app.log(f"连接失败：{message}")

    def set_client(self, client):
        if client is None:
            self.button.setText("连接")
            self.state.setText("未连接")


class DidPage(Page):
    title = "数据标识"

    def __init__(self, app):
        super().__init__(app)

        self.table = make_table(["名称", "DID", "值"])
        self.table.setRowCount(len(dids.KNOWN_DIDS))
        self.specs = sorted(dids.KNOWN_DIDS.values(), key=lambda s: s.did)

        for row, spec in enumerate(self.specs):
            self.table.setItem(row, 0, QTableWidgetItem(spec.name))
            self.table.setItem(row, 1, QTableWidgetItem(f"0x{spec.did:04X}"))
            self.table.setItem(row, 2, QTableWidgetItem("—"))

        self.read_all = QPushButton("全部读取")
        self.read_all.clicked.connect(lambda: self.read(self.specs))
        self.read_one = QPushButton("读取选中")
        self.read_one.clicked.connect(self.read_selected)
        self.write_one = QPushButton("写入选中")
        self.write_one.clicked.connect(self.write_selected)

        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("十六进制字节，如 3C")

        buttons = QHBoxLayout()
        buttons.addWidget(self.read_all)
        buttons.addWidget(self.read_one)
        buttons.addWidget(self.value_edit)
        buttons.addWidget(self.write_one)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.table)

    def read_selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        self.read([self.specs[r] for r in sorted(rows)])

    def read(self, specs):
        if not specs:
            return

        client = self.app.require_client()
        if client is None:
            return

        for spec in specs:
            self.app.run(lambda s=spec: (s.did, client.read_did(s.did)),
                         self.on_value, self.on_error)

    def on_value(self, item):
        did, raw = item
        label, value = dids.describe(did, raw)
        for row, spec in enumerate(self.specs):
            if spec.did == did:
                self.table.setItem(row, 2, QTableWidgetItem(value))
                break
        self.app.log(f"0x{did:04X} {label} = {value}")

    def on_error(self, message):
        self.app.log(f"读取失败：{message}")

    def write_selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            self.app.log("写入前请选中一行")
            return

        spec = self.specs[sorted(rows)[0]]
        if not spec.writable:
            self.app.log(f"{spec.name} 不可写")
            return

        try:
            data = bytes.fromhex(self.value_edit.text().replace(" ", ""))
        except ValueError:
            self.app.log("写入值必须是十六进制字节")
            return

        if spec.length is not None and len(data) != spec.length:
            self.app.log(f"{spec.name} 需要 {spec.length} 字节，给了 {len(data)} 字节")
            return

        client = self.app.require_client()
        if client is None:
            return

        def work():
            client.change_session(0x03)
            client.unlock(1)
            client.write_did(spec.did, data)
            return client.read_did(spec.did)

        self.app.run(work, self.on_value_did(spec.did), self.on_error)

    def on_value_did(self, did):
        def handler(raw):
            self.on_value((did, raw))
        return handler


class DtcPage(Page):
    title = "故障码"

    def __init__(self, app):
        super().__init__(app)

        self.table = make_table(["DTC", "状态", "明细"])
        self.mask = QLineEdit("0xFF")
        self.mask.setMaximumWidth(80)

        self.read_button = QPushButton("读取")
        self.read_button.clicked.connect(self.read)
        self.clear_button = QPushButton("清除")
        self.clear_button.clicked.connect(self.clear)
        self.snapshot_button = QPushButton("读快照")
        self.snapshot_button.clicked.connect(self.snapshot)

        buttons = QHBoxLayout()
        buttons.addWidget(QLabel("状态掩码"))
        buttons.addWidget(self.mask)
        buttons.addWidget(self.read_button)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.snapshot_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.table)

    def read(self):
        client = self.app.require_client()
        if client is None:
            return

        mask = int(self.mask.text(), 0)

        def work():
            payload = client.read_dtc(0x02, mask=mask)
            return dtc_mod.parse_report_by_mask(payload)

        self.app.run(work, self.on_entries, self.on_error)

    def on_entries(self, entries):
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(entry.text))
            self.table.setItem(row, 1, QTableWidgetItem(f"0x{entry.status:02X}"))
            self.table.setItem(row, 2,
                               QTableWidgetItem(dtc_mod.describe_status(entry.status)))
        self.app.log(f"读到 {len(entries)} 条故障")

    def clear(self):
        client = self.app.require_client()
        if client is None:
            return

        def work():
            client.change_session(0x03)
            client.clear_dtc(0xFFFFFF)

        self.app.run(work, lambda _: self.read(), self.on_error)

    def snapshot(self):
        client = self.app.require_client()
        if client is None:
            return

        def work():
            ids = dtc_mod.parse_snapshot_identification(client.read_dtc(0x03))
            if not ids:
                return []
            payload = client.read_dtc(0x05, record=1)
            return dtc_mod.parse_stored_data(payload, dids.did_lengths())[1]

        self.app.run(work, self.on_snapshots, self.on_error)

    def on_snapshots(self, entries):
        if not entries:
            self.app.log("没有任何 DTC 存了快照")
            return
        for entry in entries:
            self.app.log(f"{entry.text} 记录 {entry.record} 状态 0x{entry.status:02X}")
            for did, raw in entry.snapshot:
                label, value = dids.describe(did, raw)
                self.app.log(f"    0x{did:04X} {label} = {value}")

    def on_error(self, message):
        self.app.log(f"故障码操作失败：{message}")


class FlashPage(Page):
    title = "刷写"

    def __init__(self, app):
        super().__init__(app)

        self.path = QLineEdit()
        self.path.setPlaceholderText("选择 .hex / .s19 / .bin")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self.browse)

        self.base = QLineEdit("0x08000000")
        self.base.setMaximumWidth(120)
        self.base_label = QLabel("基地址")

        self.verify = QCheckBox("刷完校验 CRC")
        self.verify.setChecked(True)

        self.start = QPushButton("开始刷写")
        self.start.clicked.connect(self.run_flash)
        self.cancel = QPushButton("取消")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_flash)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%  (%v / %m 字节)")

        row = QHBoxLayout()
        row.addWidget(self.path)
        row.addWidget(browse)
        row.addWidget(self.base_label)
        row.addWidget(self.base)

        buttons = QHBoxLayout()
        buttons.addWidget(self.verify)
        buttons.addStretch(1)
        buttons.addWidget(self.start)
        buttons.addWidget(self.cancel)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addLayout(buttons)
        layout.addWidget(self.progress)
        layout.addStretch(1)

        self._cancel_flag = False

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择固件", "", "固件 (*.hex *.s19 *.srec *.mot *.bin);;所有文件 (*)")
        if path:
            self.path.setText(path)

    def run_flash(self):
        client = self.app.require_client()
        if client is None:
            return

        try:
            image = load_firmware(self.path.text(), base_address=int(self.base.text(), 0))
        except Exception as exc:
            self.app.log(f"读取固件失败：{exc}")
            return

        self.app.log(f"固件 {image.size} 字节，起始 0x{image.start:08X}，"
                     f"crc32 0x{image.crc32:08X}")

        self._cancel_flag = False
        self.start.setEnabled(False)
        self.cancel.setEnabled(True)
        self.progress.setRange(0, image.size)
        self.progress.setValue(0)

        job = FlashJob(client, verify_routine=0x0202 if self.verify.isChecked() else None)

        def work():
            return job.run(image,
                           progress=self.on_progress,
                           cancel=lambda: self._cancel_flag)

        self.app.run(work, self.on_done, self.on_failed)

    def on_progress(self, sent, total):
        # 这个回调在后台线程里，只能通过信号回界面；这里直接改控件在
        # 单核抢占下偶发花屏，所以走 app.post。见 MainWindow.post。
        self.app.post(lambda: self.progress.setValue(sent))

    def on_done(self, result):
        self.start.setEnabled(True)
        self.cancel.setEnabled(False)
        self.progress.setValue(self.progress.maximum())
        text = f"完成：{result.bytes_written} 字节 / {result.blocks} 块，{result.elapsed_s:.2f} s"
        if result.crc_ecu is not None:
            text += f"，CRC 0x{result.crc_ecu:08X} 校验通过"
        self.app.log(text)

    def on_failed(self, message):
        self.start.setEnabled(True)
        self.cancel.setEnabled(False)
        self.progress.setValue(0)
        self.app.log(f"刷写失败：{message}")

    def cancel_flash(self):
        self._cancel_flag = True
        self.app.log("已请求取消，将在当前块结束后停止")


class MonitorPage(Page):
    title = "报文监视"

    frame_seen = Signal(str, str)

    def __init__(self, app):
        super().__init__(app)
        self.frame_seen.connect(self.append_row)

        self.table = make_table(["时刻", "方向", "字节"])
        self.table.setRowLimit = None

        self.toggle = QPushButton("开始")
        self.toggle.clicked.connect(self.toggle_capture)
        self.clear = QPushButton("清空")
        self.clear.clicked.connect(lambda: self.table.setRowCount(0))
        self.keepalive = QCheckBox("保持会话（0x3E）")
        self.keepalive.setChecked(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.toggle)
        buttons.addWidget(self.clear)
        buttons.addWidget(self.keepalive)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.table)

        self._stop = False

    def toggle_capture(self):
        if self.app.client is None:
            self.app.log("先连接")
            return

        if self.toggle.text() == "停止":
            self._stop = True
            self.toggle.setText("开始")
            return

        self._stop = False
        self.toggle.setText("停止")
        client = self.app.client
        link = client.link

        def work():
            last_ping = 0.0
            while not self._stop:
                now = time.monotonic()
                if self.keepalive.isChecked() and now - last_ping > 2.0:
                    client.tester_present()
                    last_ping = now
                data = link.recv(0.1)
                if data:
                    self.frame_seen.emit(time.strftime("%H:%M:%S"),
                                         " ".join(f"{b:02X}" for b in data))
            return None

        self.app.run(work, lambda _: None, lambda m: self.app.log(f"监视中止：{m}"))

    def append_row(self, moment, payload):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(moment))
        self.table.setItem(row, 1, QTableWidgetItem("rx"))
        self.table.setItem(row, 2, QTableWidgetItem(payload))
        if row > 200:
            self.table.removeRow(0)
        self.table.scrollToBottom()
