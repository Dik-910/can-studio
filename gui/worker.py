"""后台任务：CAN 收发是阻塞的，不能放在界面线程里。

界面卡住的典型原因就是把同步 I/O 直接调在槽函数里 —— 一次 5 秒超时
会让窗口假死。这里统一走 QThread，结果通过信号回到界面线程。
"""
import traceback

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    """跑一个可调用对象，成功发 finished，失败发 failed(说明+栈)。"""

    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.failed.emit(detail)
            return
        self.finished_ok.emit(result)


class TaskRunner:
    """持有正在运行的 Worker，避免它被回收导致线程被杀。

    Qt 里只要 Worker 对象被垃圾回收，线程就没了，界面表现为「点了没反应」。
    """

    def __init__(self, owner):
        self._owner = owner
        self._active = []

    def run(self, fn, on_ok=None, on_err=None, *args, **kwargs):
        worker = Worker(fn, *args, **kwargs)
        self._active.append(worker)

        def cleanup():
            if worker in self._active:
                self._active.remove(worker)

        worker.finished_ok.connect(lambda result: (on_ok and on_ok(result), cleanup()))
        worker.failed.connect(lambda message: (on_err and on_err(message), cleanup()))
        worker.start()
        return worker

    def busy(self):
        return bool(self._active)
