#!/usr/bin/python3
"""离屏渲染界面截图。

QT_QPA_PLATFORM=offscreen 下 widget.grab() 不依赖 X11，所以没有显示器的
机器也能出图，方便把界面状态留在仓库里。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".pylibs"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from canstudio.uds import UdsClient
from fake_ecu import ScriptedEcu, negative


def handler(request):
    sid = request[0]
    if sid == 0x22:
        did = int.from_bytes(request[1:3], "big")
        table = {
            0xF190: b"LSVAA1234567890AB",
            0x0101: b"\x0E\x4C",
            0x0201: b"\x3C",
            0x0301: bytes.fromhex("8F2C1A44"),
            0x0302: b"\x00\x01\x02",
            0x0001: bytes.fromhex("01AA080000F8"),
        }
        if did in table:
            return bytes([0x62]) + request[1:3] + table[did]
        return negative(request, 0x31)
    if sid == 0x19 and request[1] == 0x02:
        return bytes([0x59, 0x02, 0xFF,
                      0xC1, 0x00, 0x87, 0x2A,
                      0xC1, 0x00, 0x8F, 0x24,
                      0xC1, 0x00, 0x91, 0x04])
    return negative(request, 0x11)


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


class SampleMonitor:
    """给截图用的假监视器：吐几帧真实格式的报文，然后停住。"""

    frames = [
        (0.0, 0x180, bytes([2, 0, 0x00, 0x00, 0x9C, 0x01, 0x00, 0x00]), False),
        (0.0, 0x200, bytes([0xF8, 0x13, 0x9C, 0xFF, 87, 65, 0, 0]), False),
        (0.0, 0x181, bytes([0, 0, 0x99, 0x00, 0, 0, 0, 0]), False),
        (0.0, 0x180, bytes([2, 0, 0x00, 0x00, 0x9D, 0x01, 0x00, 0x00]), False),
        (0.0, 0x200, bytes([0xF9, 0x13, 0x9C, 0xFF, 87, 65, 0, 0]), False),
    ]

    def read(self, timeout_s):
        if self.frames:
            ts, fid, data, ext = self.frames.pop(0)
            return (time.time(), fid, data, ext)
        time.sleep(0.02)
        return None

    def close(self):
        pass


def render(app, window, out):
    pixmap = window.grab()
    ok = pixmap.save(out)
    print(f"{'已生成' if ok else '保存失败'}: {out}  ({pixmap.width()}x{pixmap.height()})")
    return ok


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "docs", "screenshot.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    from gui.main_window import MainWindow, create_app

    app = create_app([])
    window = MainWindow()
    window.resize(1000, 680)
    window.set_client(UdsClient(ScriptedEcu(handler)))
    window.show()

    pages = {page.title: page for page in window.pages}

    did_page = pages["数据标识"]
    did_page.read_all.click()
    pump(app, 1.2)

    window.nav.setCurrentRow(2)
    pages["故障码"].read_button.click()
    pump(app, 0.8)

    # 监视页：载入 can-node 的 DBC 并灌几帧
    monitor = pages["报文监视"]
    dbc = os.path.normpath(os.path.join(HERE, "..", "can-node", "docs", "can_matrix.dbc"))
    if os.path.exists(dbc):
        monitor.dbc_path.setText(dbc)
        monitor.load_dbc()
        monitor._monitor_factory = lambda channel: SampleMonitor()
        monitor.toggle_capture()
        pump(app, 0.5)
        monitor.toggle_capture()
        # 展示过滤控件：只看两条报文
        monitor.id_filter.setText("0x180 0x181 0x200")

    window.log("已连接 vcan0 0x7E0 → 0x7E8")
    window.log("0xF190 VIN = LSVAA1234567890AB")
    window.log("读到 3 条故障")
    window.log("DBC 已载入：5 条报文（NodeStatus, BusStatus, PackData, FaultEvent…）")
    window.nav.setCurrentRow(4)
    pump(app, 0.3)

    ok = render(app, window, out)

    # 第二张：DID 页，带周期读取
    did_page = pages["数据标识"]
    did_page.interval.setValue(0.5)
    did_page.poll.setChecked(True)
    pump(app, 0.6)
    did_page.poll.setChecked(False)
    window.nav.setCurrentRow(1)
    pump(app, 0.3)
    ok = render(app, window, out.replace(".png", "-dids.png")) and ok

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
