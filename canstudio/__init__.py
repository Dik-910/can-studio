"""can-studio —— 面向 CAN/UDS 的 PC 侧诊断工具。

分三层，界面只依赖最上面那层：

    link.py    链路：把「发一帧、等一帧」抽象出来（SocketCAN / 假链路）
    uds.py     协议：ISO 14229 请求编码与响应解码
    dtc.py     语义：故障码、状态位、快照的解读
    gui/       Qt 界面（待接入），不碰协议细节

好处是协议层可以完全脱离硬件测试 —— tests/ 里用假链路把整套服务跑一遍。
"""

from .dids import KNOWN_DIDS, DidSpec, describe, find
from .dtc import (
    DtcEntry,
    decode_status,
    format_dtc,
    parse_dtc,
    parse_stored_data,
)
from .errors import BadResponse, CanStudioError, NegativeResponse, TransportTimeout
from .link import Link, RecordingLink, SocketCanLink
from .uds import UdsClient

__all__ = [
    "UdsClient",
    "Link",
    "SocketCanLink",
    "RecordingLink",
    "DtcEntry",
    "DidSpec",
    "KNOWN_DIDS",
    "describe",
    "find",
    "format_dtc",
    "parse_dtc",
    "decode_status",
    "parse_stored_data",
    "CanStudioError",
    "NegativeResponse",
    "TransportTimeout",
    "BadResponse",
]
