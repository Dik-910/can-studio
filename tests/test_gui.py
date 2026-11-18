"""界面冒烟测试：离屏构建窗口，注入假 ECU，点按钮验证表格被填写。

不需要显示器，也不需要 CAN 接口 —— QT_QPA_PLATFORM=offscreen 让 Qt 在没有
X11 的机器上也能跑完整的事件循环，这样界面代码就不是「写完没跑过」。
"""
import os
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
for candidate in (os.environ.get("CANSTUDIO_PYLIB"), "/home/dikl/work/.pylibs"):
    if candidate and os.path.isdir(candidate):
        sys.path.insert(0, candidate)
        break

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from fake_ecu import ScriptedEcu, negative

from canstudio.uds import UdsClient


def ecu_handler(request):
    sid = request[0]
    if sid == 0x22:
        did = int.from_bytes(request[1:3], "big")
        if did == 0xF190:
            vin = b"LSVAA1234567890AB"
            return bytes([0x62, 0xF1, 0x90]) + vin
        if did == 0x0101:
            return b"\x62\x01\x01\x0E\x4C"
        if did == 0x0302:
            return b"\x62\x03\x02\x00\x01\x00"
        return negative(request, 0x31)
    if sid == 0x19:
        if request[1] == 0x02:
            return bytes([0x59, 0x02, 0xFF, 0xC1, 0x00, 0x87, 0x26])
        if request[1] == 0x03:
            return bytes([0x59, 0x03, 0xC1, 0x00, 0x87, 0x01])
        if request[1] == 0x05:
            return bytes([0x59, 0x05, 0x01, 0xC1, 0x00, 0x87, 0x26,
                          1, 0x00, 0x01, 0xAA, 0x08, 0x00, 0x00, 0xF8])
        return negative(request, 0x12)
    if sid == 0x10:
        return b"\x50" + request[1:2] + b"\x00\x32\x13\x88"
    if sid == 0x14:
        return b"\x54"
    return negative(request, 0x11)


def wait_for(app, condition, timeout=5.0):
    """转事件循环直到条件成立。QThread 的结果要靠事件循环送回来。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class MainWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from gui.main_window import MainWindow

        self.window = MainWindow()
        self.window.set_client(UdsClient(ScriptedEcu(ecu_handler)))
        self.pages = {page.title: page for page in self.window.pages}

    def tearDown(self):
        self.window.set_client(None)
        self.window.close()

    def test_all_pages_present(self):
        self.assertEqual([p.title for p in self.window.pages],
                         ["连接", "数据标识", "故障码", "刷写", "报文监视"])
        self.assertEqual(self.window.nav.count(), 5)

    def test_requires_connection_first(self):
        self.window.set_client(None)
        self.pages["故障码"].read()
        self.assertIn("请先在「连接」页建立连接",
                      self.window.log_view.toPlainText())

    def test_did_page_fills_table(self):
        page = self.pages["数据标识"]
        page.read_all.click()

        did_row = next(i for i, spec in enumerate(page.specs) if spec.did == 0x0101)
        ok = wait_for(self.app, lambda: page.table.item(did_row, 2).text() != "—")
        self.assertTrue(ok, "DID value never arrived")
        self.assertIn("3660 mV", page.table.item(did_row, 2).text())

    def test_did_page_decodes_vin(self):
        page = self.pages["数据标识"]
        page.read_all.click()

        row = next(i for i, spec in enumerate(page.specs) if spec.did == 0xF190)
        self.assertTrue(wait_for(self.app, lambda: page.table.item(row, 2).text() != "—"))
        self.assertEqual(page.table.item(row, 2).text(), "LSVAA1234567890AB")

    def test_dtc_page_lists_faults(self):
        page = self.pages["故障码"]
        page.read_button.click()

        self.assertTrue(wait_for(self.app, lambda: page.table.rowCount() > 0))
        self.assertEqual(page.table.item(0, 0).text(), "U010087")
        self.assertEqual(page.table.item(0, 1).text(), "0x26")
        self.assertIn("pending", page.table.item(0, 2).text())

    def test_dtc_snapshot_is_logged(self):
        page = self.pages["故障码"]
        page.snapshot_button.click()

        self.assertTrue(wait_for(
            self.app,
            lambda: "U010087" in self.window.log_view.toPlainText()))
        log = self.window.log_view.toPlainText()
        self.assertIn("0x0001", log, "快照里的 DID 应当被解码后打印")

    def test_flash_page_rejects_missing_file(self):
        page = self.pages["刷写"]
        page.path.setText("/nonexistent/firmware.hex")
        page.run_flash()

        self.assertIn("读取固件失败", self.window.log_view.toPlainText())
        self.assertTrue(page.start.isEnabled(), "start button must stay usable")

    def test_monitor_requires_connection(self):
        self.window.set_client(None)
        self.pages["报文监视"].toggle_capture()
        self.assertIn("先连接", self.window.log_view.toPlainText())


if __name__ == "__main__":
    unittest.main(verbosity=2)
