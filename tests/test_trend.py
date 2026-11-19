"""曲线控件用例：数据模型与离屏绘制。

绘制部分用 grab() 真跑一次 paintEvent —— 只测数据模型的话，画崩了也看不出来。
"""
import os
import sys
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


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TrendData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 必须自己持有引用：Widget 一旦被回收，它的 QTimer 也会被销毁，
        # 之后再访问就是 "Internal C++ object already deleted"
        self.charts = []

    def tearDown(self):
        for chart in self.charts:
            chart._timer.stop()

    def make(self, **kwargs):
        from gui.trend import TrendChart

        chart = TrendChart(**kwargs)
        self.charts.append(chart)
        return chart

    def test_add_and_remove_series(self):
        chart = self.make()

        self.assertTrue(chart.add_series("180.UptimeS", "NodeStatus.UptimeS"))
        self.assertFalse(chart.add_series("180.UptimeS"), "重复加入应当被拒")
        self.assertEqual(chart.series_keys(), ["180.UptimeS"])

        self.assertTrue(chart.remove_series("180.UptimeS"))
        self.assertEqual(chart.series_keys(), [])
        self.assertFalse(chart.remove_series("180.UptimeS"))

    def test_feed_unknown_or_non_numeric_is_ignored(self):
        chart = self.make()
        chart.add_series("a")

        self.assertFalse(chart.feed("nope", 1.0, 5))
        self.assertFalse(chart.feed("a", 1.0, "0x26"), "非数值不该进曲线")
        self.assertFalse(chart.feed("a", 1.0, None))
        self.assertTrue(chart.feed("a", 1.0, 5))

    def test_latest_and_range(self):
        chart = self.make()
        chart.add_series("a")
        for stamp, value in ((1.0, 10), (2.0, 30), (3.0, 20)):
            chart.feed("a", stamp, value)

        self.assertEqual(chart.series("a").latest(), 20)
        self.assertEqual(chart.range(), (10, 30))

    def test_range_spans_all_series(self):
        chart = self.make()
        chart.add_series("a")
        chart.add_series("b")
        chart.feed("a", 1.0, -5)
        chart.feed("b", 1.0, 100)

        self.assertEqual(chart.range(), (-5, 100))

    def test_points_outside_window_are_trimmed(self):
        chart = self.make(window_s=2.0)
        chart.add_series("a")
        for stamp in (0.0, 1.0, 2.0, 5.0):
            chart.feed("a", stamp, stamp)

        # 最新点是 5.0，窗口 2 秒 -> 只留 >= 3.0 的；2.0 那个点被裁掉
        stamps = [p[0] for p in chart.series("a").points]
        self.assertEqual(stamps, [5.0], "窗口外的点必须裁掉")

    def test_trim_based_on_latest_not_wall_clock(self):
        # 暂停采集后曲线不该自己往左跑空
        chart = self.make(window_s=2.0)
        chart.add_series("a")
        chart.feed("a", 100.0, 1)
        chart.feed("a", 101.0, 2)

        self.assertEqual(len(chart.series("a").points), 2)

    def test_clear_keeps_series_but_drops_points(self):
        chart = self.make()
        chart.add_series("a")
        chart.feed("a", 1.0, 1)

        chart.clear()
        self.assertEqual(chart.series_keys(), ["a"])
        self.assertIsNone(chart.range())

    def test_clear_series_removes_everything(self):
        chart = self.make()
        chart.add_series("a")
        chart.add_series("b")
        chart.clear_series()
        self.assertEqual(chart.series_keys(), [])

    def test_time_span(self):
        chart = self.make()
        chart.add_series("a")
        chart.add_series("b")
        chart.feed("a", 10.0, 1)
        chart.feed("b", 12.0, 1)

        self.assertEqual(chart.time_span(), (10.0, 12.0))

    def test_empty_chart_reports_no_range(self):
        chart = self.make()
        self.assertIsNone(chart.range())
        self.assertIsNone(chart.time_span())


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TrendPaint(unittest.TestCase):
    """真跑一遍 paintEvent。空图、单点、多线三种情况都不该抛异常。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.charts = []

    def tearDown(self):
        for chart in self.charts:
            chart._timer.stop()

    def make(self):
        from gui.trend import TrendChart

        chart = TrendChart(window_s=5.0)
        chart.resize(480, 200)
        self.charts.append(chart)
        return chart

    def test_paint_empty(self):
        chart = self.make()
        image = chart.grab().toImage()
        self.assertEqual((image.width(), image.height()), (480, 200))

    def test_paint_single_point(self):
        chart = self.make()
        chart.add_series("a", "单点")
        chart.feed("a", 1000.0, 42)
        self.assertFalse(chart.grab().toImage().isNull())

    def test_paint_flat_line_does_not_divide_by_zero(self):
        chart = self.make()
        chart.add_series("a", "常量")
        for i in range(10):
            chart.feed("a", 1000.0 + i * 0.1, 7)
        self.assertFalse(chart.grab().toImage().isNull())

    def test_paint_two_series(self):
        chart = self.make()
        chart.add_series("a", "甲")
        chart.add_series("b", "乙")
        for i in range(20):
            chart.feed("a", 1000.0 + i * 0.1, i)
            chart.feed("b", 1000.0 + i * 0.1, 100 - i)
        self.assertFalse(chart.grab().toImage().isNull())


if __name__ == "__main__":
    unittest.main(verbosity=2)
