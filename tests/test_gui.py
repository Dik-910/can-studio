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

try:
    import cantools  # noqa: F401
    HAVE_CANTOOLS = True
except ImportError:
    HAVE_CANTOOLS = False


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


DBC_TEXT = """
VERSION ""

NS_ :

BS_:

BU_: Node

BO_ 384 NodeStatus: 8 Node
 SG_ NodeState : 0|8@1+ (1,0) [0|3] "" Node
 SG_ BusOffCnt : 8|16@1+ (1,0) [0|65535] "" Node

BO_ 512 PackData: 8 Node
 SG_ PackVoltage : 0|16@1+ (0.01,0) [0|655.35] "V" Node
 SG_ Soc : 32|8@1+ (1,0) [0|100] "%" Node
"""


class FakeMonitor:
    """按脚本吐帧的监视器，read() 空了就稍等一下，避免空转。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    def read(self, timeout_s):
        if self.frames:
            return self.frames.pop(0)
        time.sleep(0.02)
        return None

    def close(self):
        self.closed = True


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
@unittest.skipUnless(HAVE_CANTOOLS, "cantools not installed")
class MonitorPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile

        from gui.main_window import MainWindow

        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".dbc", delete=False)
        self.tmp.write(DBC_TEXT)
        self.tmp.close()

        self.window = MainWindow()
        self.window.set_client(UdsClient(ScriptedEcu(ecu_handler)))
        self.page = {p.title: p for p in self.window.pages}["报文监视"]

    def tearDown(self):
        self.page._stop = True
        self.window.runner.wait_all(2000)
        self.window.set_client(None)
        self.window.close()
        os.unlink(self.tmp.name)

    def start_with(self, frames):
        monitor = FakeMonitor(frames)
        self.page._monitor_factory = lambda channel: monitor
        self.page.toggle_capture()
        return monitor

    def test_dbc_loaded_from_path(self):
        self.page.dbc_path.setText(self.tmp.name)
        self.page.load_dbc()
        self.assertIsNotNone(self.page.db)
        self.assertEqual(len(self.page.db), 2)

    def test_frames_are_decoded_in_table(self):
        self.page.dbc_path.setText(self.tmp.name)
        self.page.load_dbc()

        self.start_with([
            (1000.0, 0x180, bytes([2, 0x01, 0x00, 0, 0, 0, 0, 0]), False),
            (1001.0, 0x200, bytes([0xF8, 0x13, 0, 0, 87, 0, 0, 0]), False),
        ])

        self.assertTrue(wait_for(self.app, lambda: self.page.table.rowCount() == 2))
        table = self.page.table

        self.assertEqual(table.item(0, 1).text(), "0x180")
        self.assertEqual(table.item(0, 2).text(), "NodeStatus")
        self.assertIn("NodeState=2", table.item(0, 4).text())
        self.assertIn("BusOffCnt=1", table.item(0, 4).text())

        self.assertEqual(table.item(1, 2).text(), "PackData")
        self.assertIn("PackVoltage=51.12 V", table.item(1, 4).text())
        self.assertIn("Soc=87 %", table.item(1, 4).text())

    def test_unknown_id_is_marked_but_shown(self):
        self.page.dbc_path.setText(self.tmp.name)
        self.page.load_dbc()

        self.start_with([(1000.0, 0x555, bytes(8), False)])

        self.assertTrue(wait_for(self.app, lambda: self.page.table.rowCount() == 1))
        self.assertEqual(self.page.table.item(0, 2).text(), "（DBC 无此报文）")
        self.assertEqual(self.page.table.item(0, 4).text(), "")

    def test_without_dbc_only_raw_bytes(self):
        self.page.dbc_path.setText("")
        self.page.db = None

        self.start_with([(1000.0, 0x180, bytes([1, 2, 3]), False)])

        self.assertTrue(wait_for(self.app, lambda: self.page.table.rowCount() == 1))
        self.assertEqual(self.page.table.item(0, 3).text(), "01 02 03")
        self.assertEqual(self.page.table.item(0, 4).text(), "")

    def test_stop_closes_monitor(self):
        monitor = self.start_with([])
        self.page.toggle_capture()          # 第二次点击 = 停止
        self.assertEqual(self.page.toggle.text(), "开始")
        self.assertTrue(wait_for(self.app, lambda: monitor.closed))


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class DidTablePageTest(unittest.TestCase):
    """DID 表可换、可周期读取 —— 这两条是「换个 ECU 不用改源码」的前提。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile

        from gui.main_window import MainWindow

        payload = {
            "ecu": "测试 ECU",
            "dids": [
                {"id": "0x0101", "name": "电池电压", "length": 2, "decode": "millivolt"},
                {"id": "0x0201", "name": "过压阈值", "length": 1, "decode": "uint8",
                 "writable": True, "session": "extended"},
            ],
        }
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        import json
        json.dump(payload, handle, ensure_ascii=False)
        handle.close()
        self.config = handle.name

        self.window = MainWindow()
        self.window.set_client(UdsClient(ScriptedEcu(ecu_handler)))
        self.page = {p.title: p for p in self.window.pages}["数据标识"]

    def tearDown(self):
        self.page.stop_polling()
        self.window.runner.wait_all(2000)
        self.window.set_client(None)
        self.window.close()
        os.unlink(self.config)

    def test_default_table_is_builtin(self):
        self.assertEqual(self.page.table.rowCount(), len(self.window.did_table))
        self.assertIn("内置表", self.page.table_path.text())

    def test_switch_table_rebuilds_rows(self):
        self.assertTrue(self.page.apply_table(self.config))

        self.assertEqual(self.page.table.rowCount(), 2)
        self.assertEqual(self.page.table.item(0, 0).text(), "电池电压")
        self.assertEqual(self.page.table.item(1, 3).text(), "可写")
        self.assertIn(self.config, self.page.table_path.text())

    def test_broken_table_keeps_previous(self):
        before = self.page.table.rowCount()

        self.assertFalse(self.page.apply_table("/nonexistent/table.json"))
        self.assertEqual(self.page.table.rowCount(), before)
        self.assertIn("DID 表载入失败", self.window.log_view.toPlainText())

    def test_polling_fills_values_and_stops(self):
        self.page.apply_table(self.config)
        self.page.interval.setValue(0.2)
        self.page.poll.setChecked(True)

        self.assertTrue(self.page.timer.isActive())
        self.assertTrue(wait_for(
            self.app, lambda: self.page.table.item(0, 2).text() != "—"))
        self.assertIn("3660 mV", self.page.table.item(0, 2).text())

        self.page.poll.setChecked(False)
        self.assertFalse(self.page.timer.isActive())

    def test_polling_requires_connection(self):
        self.window.set_client(None)
        self.page.poll.setChecked(True)

        self.assertFalse(self.page.poll.isChecked(), "无连接时不该真的开起来")
        self.assertFalse(self.page.timer.isActive())
        self.assertIn("先连接再开周期读取", self.window.log_view.toPlainText())

    def test_disconnect_stops_polling(self):
        self.page.apply_table(self.config)
        self.page.poll.setChecked(True)
        self.assertTrue(self.page.timer.isActive())

        self.window.set_client(None)
        self.assertFalse(self.page.timer.isActive(), "断开后必须停掉定时器")


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
@unittest.skipUnless(HAVE_CANTOOLS, "cantools not installed")
class MonitorFilterTest(unittest.TestCase):
    """过滤与暂停：0x181 每 20ms 一帧，不筛会把 0x180 淹掉。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from gui.main_window import MainWindow

        self.window = MainWindow()
        self.window.set_client(UdsClient(ScriptedEcu(ecu_handler)))
        self.page = {p.title: p for p in self.window.pages}["报文监视"]

    def tearDown(self):
        self.page._stop = True
        self.window.runner.wait_all(2000)
        self.window.set_client(None)
        self.window.close()

    def push(self, frame_id, payload=b"\x01"):
        self.page.append_row((1000.0, frame_id, payload, False))

    def test_filter_keeps_only_listed_ids(self):
        self.page.id_filter.setText("0x180")
        self.push(0x180)
        self.push(0x181)
        self.push(0x200)
        self.push(0x180)

        self.assertEqual(self.page.table.rowCount(), 2)
        self.assertEqual(self.page.table.item(0, 1).text(), "0x180")
        self.assertEqual(self.page.filtered_out, 2)

    def test_multiple_ids(self):
        self.page.id_filter.setText("0x180, 0x181")
        for frame_id in (0x180, 0x181, 0x200):
            self.push(frame_id)

        self.assertEqual(self.page.table.rowCount(), 2)

    def test_clearing_filter_shows_everything_again(self):
        self.page.id_filter.setText("0x180")
        self.push(0x181)
        self.assertEqual(self.page.table.rowCount(), 0)

        self.page.id_filter.setText("")
        self.push(0x181)
        self.assertEqual(self.page.table.rowCount(), 1)

    def test_pause_holds_rows_but_counts_frames(self):
        self.page.pause.setChecked(True)
        self.push(0x180)
        self.push(0x180)

        self.assertEqual(self.page.table.rowCount(), 0, "暂停期间不该插行")
        self.assertEqual(self.page.held, 2)
        self.assertIn("2 帧", self.page.pause_hint.text())

        self.page.pause.setChecked(False)
        self.push(0x180)
        self.assertEqual(self.page.table.rowCount(), 1, "恢复后应当继续插行")
        self.assertIn("2 帧", self.window.log_view.toPlainText())

    def test_pause_takes_precedence_over_filter(self):
        self.page.id_filter.setText("0x180")
        self.page.pause.setChecked(True)
        self.push(0x181)      # 被过滤掉，不该计入暂停计数
        self.push(0x180)      # 命中过滤但被暂停

        self.assertEqual(self.page.filtered_out, 1)
        self.assertEqual(self.page.held, 1)

    def test_clear_resets_counters(self):
        self.page.pause.setChecked(True)
        self.push(0x180)
        self.page.clear_rows()
        self.assertEqual(self.page.held, 0)
