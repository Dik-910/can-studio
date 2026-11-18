"""测试用的假链路与假 ECU。

这套东西的意义：协议层可以完全脱离硬件被测。真实总线上会出的情况
（否定响应、0x78 挂起、超时、回显不对）都能在这里精确构造出来，
比在 vcan 上撞运气可靠得多。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canstudio.link import Link


class FakeLink(Link):
    """按脚本回应的链路：sent 记下每一帧请求，replies 依次作为响应。"""

    txid = 0x7E0
    rxid = 0x7E8

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(bytes(data))

    def recv(self, timeout_s):
        if self.replies:
            item = self.replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None

    def close(self):
        self.closed = True


class ScriptedEcu(Link):
    """能应答的假 ECU：把请求交给 handler，按返回值回应。

    handler 收到请求字节，返回响应字节；返回 None 表示不回应（模拟超时）。
    """

    txid = 0x7E0
    rxid = 0x7E8

    def __init__(self, handler):
        self.handler = handler
        self.sent = []
        self.pending = []

    def send(self, data):
        request = bytes(data)
        self.sent.append(request)
        reply = self.handler(request)
        self.pending = list(reply) if isinstance(reply, (list, tuple)) else (
            [reply] if reply is not None else []
        )

    def recv(self, timeout_s):
        if self.pending:
            return self.pending.pop(0)
        return None


def positive(request, body=b""):
    """构造正响应：SID + 0x40，子功能原样回显。"""
    out = bytes([request[0] + 0x40])
    if len(request) > 1:
        out += bytes([request[1]])
    return out + bytes(body)


def negative(request, nrc):
    return bytes([0x7F, request[0], nrc])
