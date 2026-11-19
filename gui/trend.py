"""实时曲线控件。

监视表格能看出「有没有在动」，看不出「趋势」。电压慢慢往下掉、总线负载
在爬升，这种变化要放在时间轴上才明显。

实现上不引 pyqtgraph —— 它会把 numpy 一起拖进来，而这个控件只需要
「一串时间点画一条折线」。QPainter 自绘约两百行，还能离屏截图验证。

设计取舍：
  - 每个序列只保留窗口内的点（deque + 左侧裁剪），长时间跑不会吃内存
  - 重绘走定时器而不是每收到一帧就 update()：0x181 是 20ms 一帧，
    50Hz 的重绘会让界面线程白忙
"""
import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

# 序列配色，深色浅色都能看清
PALETTE = ["#1D9E75", "#378ADD", "#D85A30", "#534AB7", "#BA7517", "#993556"]

GRID_COLOR = "#D3D1C7"
AXIS_TEXT = "#5F5E5A"
EMPTY_TEXT = "#888780"
BACKGROUND = "#FFFFFF"
BORDER = "#B4B2A9"


class Series:
    __slots__ = ("key", "label", "color", "points")

    def __init__(self, key, label, color, max_points):
        self.key = key
        self.label = label
        self.color = QColor(color)
        self.points = deque(maxlen=max_points)

    def latest(self):
        return self.points[-1][1] if self.points else None

    def value_range(self):
        values = [v for _, v in self.points]
        return (min(values), max(values)) if values else None


class TrendChart(QWidget):
    """多条信号共用一个时间轴。

    window_s 是窗口长度：只显示最近这么多秒，更早的点在 feed 时被裁掉。
    """

    def __init__(self, parent=None, window_s=10.0, max_points=1200, refresh_ms=50):
        super().__init__(parent)
        self.setMinimumHeight(140)
        self.window_s = window_s
        self.max_points = max_points

        self._series = {}       # key -> Series
        self._order = []        # 保持加入顺序，图例与配色稳定
        self._started = None    # 第一个点的时间，用于窗口裁切

        self._timer = QTimer(self)
        self._timer.setInterval(refresh_ms)
        self._timer.timeout.connect(self.update)

    # ---------------- 数据 ----------------

    def add_series(self, key, label=None):
        """加入一条序列。已存在返回 False，避免重复图例。"""
        if key in self._series:
            return False

        color = PALETTE[len(self._order) % len(PALETTE)]
        self._series[key] = Series(key, label or key, color, self.max_points)
        self._order.append(key)

        if not self._timer.isActive():
            self._timer.start()
        return True

    def remove_series(self, key):
        if key in self._series:
            del self._series[key]
            self._order.remove(key)
            self.update()
            return True
        return False

    def clear(self):
        for series in self._series.values():
            series.points.clear()
        self._started = None
        self.update()

    def clear_series(self):
        for key in list(self._series):
            self.remove_series(key)

    def series_keys(self):
        return list(self._order)

    def series(self, key):
        return self._series.get(key)

    def feed(self, key, timestamp, value):
        """喂一个点。未注册的 key 与非数值直接忽略 —— 界面不该因为
        DBC 里某个信号解出字符串就崩。"""
        series = self._series.get(key)
        if series is None:
            return False
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False

        series.points.append((float(timestamp), float(value)))
        if self._started is None:
            self._started = float(timestamp)

        self._trim()
        return True

    def _trim(self):
        """裁掉窗口外的点。用最新的点做基准，而不是当前时刻 ——
        暂停采集之后曲线不该自己往左跑空。"""
        if not self._order:
            return
        latest = max((s.points[-1][0] for s in self._series.values() if s.points),
                     default=None)
        if latest is None:
            return
        cutoff = latest - self.window_s
        for series in self._series.values():
            while series.points and series.points[0][0] < cutoff:
                series.points.popleft()

    def range(self):
        """所有序列在窗口内的 (最小, 最大)。没有数据返回 None。"""
        ranges = [s.value_range() for s in self._series.values() if s.points]
        ranges = [r for r in ranges if r is not None]
        if not ranges:
            return None
        return min(r[0] for r in ranges), max(r[1] for r in ranges)

    def time_span(self):
        stamps = [p[0] for s in self._series.values() for p in s.points]
        if not stamps:
            return None
        return min(stamps), max(stamps)

    # ---------------- 绘制 ----------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(BACKGROUND))

        plot = self.rect().adjusted(52, 26, -10, -22)
        painter.setPen(QPen(QColor(BORDER), 0.5))
        painter.drawRect(plot)

        if not any(s.points for s in self._series.values()):
            painter.setPen(QPen(QColor(EMPTY_TEXT)))
            painter.drawText(plot, Qt.AlignCenter, "加入信号后开始绘制")
            painter.end()
            return

        self._draw_grid(painter, plot)
        self._draw_series(painter, plot)
        self._draw_legend(painter, plot)
        painter.end()

    def _scale(self):
        value_range = self.range()
        low, high = value_range if value_range else (0.0, 1.0)
        if high - low < 1e-9:
            # 一条直线时上下各留 1，否则线会贴在边上
            pad = max(abs(high), 1.0) * 0.1
            return low - pad, high + pad
        pad = (high - low) * 0.15
        return low - pad, high + pad

    def _draw_grid(self, painter, plot):
        low, high = self._scale()
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        rows = 4
        for i in range(rows + 1):
            y = plot.top() + plot.height() * i / rows
            painter.setPen(QPen(QColor(GRID_COLOR), 0.5))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

            value = high - (high - low) * i / rows
            painter.setPen(QPen(QColor(AXIS_TEXT)))
            painter.drawText(QRectF(0, y - 8, plot.left() - 6, 16),
                             Qt.AlignRight | Qt.AlignVCenter, _fmt(value))

        span = self.time_span()
        if span:
            painter.setPen(QPen(QColor(AXIS_TEXT)))
            painter.drawText(QRectF(plot.left(), plot.bottom() + 4, 80, 16),
                             Qt.AlignLeft | Qt.AlignTop,
                             f"-{self.window_s:.0f}s")
            painter.drawText(QRectF(plot.right() - 80, plot.bottom() + 4, 80, 16),
                             Qt.AlignRight | Qt.AlignTop, "现在")

    def _draw_series(self, painter, plot):
        span = self.time_span()
        if span is None:
            return

        latest = span[1]
        x0 = latest - self.window_s
        low, high = self._scale()

        for key in self._order:
            series = self._series[key]
            if not series.points:
                continue

            path = QPainterPath()
            first = True
            for stamp, value in series.points:
                x = plot.left() + (stamp - x0) / self.window_s * plot.width()
                y = plot.bottom() - (value - low) / (high - low) * plot.height()
                if first:
                    path.moveTo(x, y)
                    first = False
                else:
                    path.lineTo(x, y)

            painter.setPen(QPen(series.color, 1.6))
            painter.drawPath(path)

    def _draw_legend(self, painter, plot):
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        x = plot.left() + 8
        for key in self._order:
            series = self._series[key]
            latest = series.latest()
            text = f"{series.label}" + ("" if latest is None else f" {_fmt(latest)}")
            width = painter.fontMetrics().horizontalAdvance(text) + 22

            painter.setPen(Qt.NoPen)
            painter.setBrush(series.color)
            painter.drawRect(QRectF(x, plot.top() + 8, 8, 8))
            painter.setBrush(Qt.NoBrush)

            painter.setPen(QPen(QColor(AXIS_TEXT)))
            painter.drawText(QRectF(x + 12, plot.top() + 2, width, 18),
                             Qt.AlignLeft | Qt.AlignVCenter, text)
            x += width + 10


def _fmt(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def now():
    """统一取时刻，测试里方便替换。"""
    return time.monotonic()
