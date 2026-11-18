"""链路层：把「发一帧 UDS 报文、等一帧回来」这件事独立出来。

分成两层是有原因的：
  - 上层（`uds.py`）只关心 PDU 字节，不关心是 SocketCAN 还是别的东西
  - 测试里换成假链路，就能在没有 CAN 接口的机器上跑完整协议用例

SocketCAN 实现复用 python-can + can-isotp：ISO-TP 是标准化的搬运层，
自己再写一份只会引入新的 bug；本项目的价值在上面的 UDS 语义。
"""
import os
import sys
import time

ISO_TP_PAD = 0xAA

# 依赖目录。本机的 python-can / can-isotp 装在 .pylibs 下而不是全局，
# 与同项目其他仓库保持一致；也接受 CANSTUDIO_PYLIB 指向别处。
_PYLIB_CANDIDATES = (
    os.environ.get("CANSTUDIO_PYLIB"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", ".pylibs"),
    "/home/dikl/work/.pylibs",
)


def ensure_dependencies():
    """把依赖目录挂进 sys.path。找不到就原样返回，让 import 自然报错。"""
    for candidate in _PYLIB_CANDIDATES:
        if candidate and os.path.isdir(candidate):
            resolved = os.path.abspath(candidate)
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            return resolved
    return None


class Link:
    """链路接口。实现只需要 send / recv 两个方法。"""

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def recv(self, timeout_s: float):
        raise NotImplementedError

    def close(self) -> None:
        pass


class SocketCanLink(Link):
    """基于 SocketCAN + ISO-TP 的链路。

    txid / rxid 是诊断请求与响应的 CAN ID。默认 0x7E0 / 0x7E8 是 ISO 14229
    规定的诊断仪侧地址，与 can-node 的 ecu-server 一致。

    填充字节用 0xAA 而不是 0x00：pytest 里用 00 填充会把短响应伪装成
    「后面还有一串零」，某些 ECU 也会把全零帧当成填充丢掉。
    """

    def __init__(self, channel="vcan0", txid=0x7E0, rxid=0x7E8,
                 block_size=8, stmin=5, pad=ISO_TP_PAD):
        ensure_dependencies()
        import can
        import isotp

        self.channel = channel
        self.txid = txid
        self.rxid = rxid

        self._bus = can.interface.Bus(channel=channel, interface="socketcan")
        try:
            addr = isotp.Address(isotp.AddressingMode.Normal_11bits,
                                 txid=txid, rxid=rxid)
            self._stack = isotp.CanStack(
                bus=self._bus,
                address=addr,
                # 参数名是 tx_padding，不是 padding —— 名字写错会抛 AttributeError
                params={"blocksize": block_size, "stmin": stmin, "tx_padding": pad},
            )
        except Exception:
            # 构造失败也要关总线，否则进程退出时 python-can 会报未正常关闭
            self._bus.shutdown()
            raise

    def send(self, data: bytes) -> None:
        self._stack.send(bytes(data))

    def recv(self, timeout_s: float):
        # can-isotp 不是回调式的，得自己转这个循环。
        # 1ms 的粒度足够：P2 是 50ms 量级，多等 1ms 不影响判定。
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._stack.process()
            if self._stack.available():
                return bytes(self._stack.recv())
            time.sleep(0.001)
        return None

    def close(self) -> None:
        try:
            self._bus.shutdown()
        except Exception:
            pass


class RecordingLink(Link):
    """把另一个链路包一层，记录收发内容。调试和演示都用得上。"""

    def __init__(self, inner, max_entries=500):
        self._inner = inner
        self.max_entries = max_entries
        self.log = []          # [(方向, 字节, 时刻)]
        self.on_frame = None   # 可选回调，界面用它实时刷新

    def _record(self, direction, data):
        entry = (direction, bytes(data), time.monotonic())
        self.log.append(entry)
        if len(self.log) > self.max_entries:
            del self.log[: len(self.log) - self.max_entries]
        if self.on_frame is not None:
            self.on_frame(*entry)

    def send(self, data: bytes) -> None:
        self._record("tx", data)
        self._inner.send(data)

    def recv(self, timeout_s: float):
        data = self._inner.recv(timeout_s)
        if data is not None:
            self._record("rx", data)
        return data

    def close(self) -> None:
        self._inner.close()
