"""UDS 客户端：ISO 14229 应用层。

这一层只做两件事：把请求按标准编码，把响应按标准解回来。时间、线程、
界面一概不管 —— 保持无状态，才能被单元测试直接驱动。

服务覆盖 CAN 诊断里实际会用到的那些：会话、复位、故障码、DID 读写、
安全访问、例程、传输（刷写）、会话保持。
"""
import struct

from .errors import BadResponse, NegativeResponse, TransportTimeout

# 服务标识
SID_SESSION_CONTROL = 0x10
SID_ECU_RESET = 0x11
SID_CLEAR_DTC = 0x14
SID_READ_DTC = 0x19
SID_READ_DID = 0x22
SID_SECURITY_ACCESS = 0x27
SID_WRITE_DID = 0x2E
SID_ROUTINE_CONTROL = 0x31
SID_REQUEST_DOWNLOAD = 0x34
SID_TRANSFER_DATA = 0x36
SID_TRANSFER_EXIT = 0x37
SID_TESTER_PRESENT = 0x3E

NEGATIVE_RESPONSE = 0x7F
POSITIVE_OFFSET = 0x40
NRC_RESPONSE_PENDING = 0x78

# 会话类型
SESSION_DEFAULT = 0x01
SESSION_PROGRAMMING = 0x02
SESSION_EXTENDED = 0x03

# 复位类型
RESET_HARD = 0x01
RESET_SOFT = 0x03

# 例程控制
ROUTINE_START = 0x01
ROUTINE_STOP = 0x02
ROUTINE_RESULTS = 0x03


def default_key_algo(level, seed, params=None):
    """与 can-node 的 ecu-server 对齐的密钥算法。

    真实项目里每个 ECU 的算法由主机厂定义，所以这里做成可注入的回调：
    换一台 ECU 只换函数，不动客户端代码。
    """
    return ((seed ^ 0x5A5A5A5A) + 0x12345678) & 0xFFFFFFFF


class UdsClient:
    """一个 tester 会话。

    参数：
        link        实现了 send / recv 的链路（见 link.py）
        p2_ms       收到请求后的等待窗口，ISO 14229 默认 50ms
        p2_star_ms  收到 0x78 之后的等待窗口，默认 5000ms
        key_algo    (level, seed, params) -> key，用于安全访问
    """

    def __init__(self, link, p2_ms=50, p2_star_ms=5000, key_algo=None, params=None):
        self.link = link
        self.p2_ms = p2_ms
        self.p2_star_ms = p2_star_ms
        self.key_algo = key_algo or default_key_algo
        self.params = params or {}
        self.tx_id = getattr(link, "txid", None)
        self.rx_id = getattr(link, "rxid", None)
        self.unlocked_level = None

    # ---------------- 底层收发 ----------------

    def _exchange(self, request: bytes) -> bytes:
        """发一帧请求，等一帧正响应；0x78 继续等，7F 抛异常。"""
        self.link.send(request)
        sid = request[0]
        waited = 0.0
        window = self.p2_ms

        while True:
            resp = self.link.recv(window / 1000.0)
            if resp is None:
                raise TransportTimeout(
                    f"service 0x{sid:02X} got no response", int(waited + window)
                )
            waited += window

            if not resp:
                raise BadResponse("empty response")

            if resp[0] == NEGATIVE_RESPONSE:
                if len(resp) < 3:
                    raise BadResponse(f"short negative response: {resp.hex()}")
                nrc = resp[2]
                if nrc == NRC_RESPONSE_PENDING:
                    # ECU 明确表示「在处理」，窗口按 P2* 重新计时
                    window = self.p2_star_ms
                    continue
                raise NegativeResponse(resp[1], nrc, resp)

            if resp[0] != (sid + POSITIVE_OFFSET):
                raise BadResponse(
                    f"expected SID 0x{sid + POSITIVE_OFFSET:02X}, got 0x{resp[0]:02X}"
                )
            return bytes(resp)

    # ---------------- 会话与状态 ----------------

    def change_session(self, session_type):
        resp = self._exchange(bytes([SID_SESSION_CONTROL, session_type]))
        if len(resp) < 2 or resp[1] != session_type:
            raise BadResponse(f"session echo mismatch: {resp.hex()}")
        self.unlocked_level = None  # 换会话后安全访问状态失效
        return resp[1]

    def ecu_reset(self, reset_type=RESET_HARD):
        resp = self._exchange(bytes([SID_ECU_RESET, reset_type]))
        return resp[1] if len(resp) > 1 else reset_type

    def tester_present(self, suppress=True, timeout_ms=None):
        sub = 0x80 if suppress else 0x00
        req = bytes([SID_TESTER_PRESENT, sub])
        if suppress:
            # 抑制正响应时 ECU 不回；这里只发不等
            self.link.send(req)
            return None
        return self._exchange(req)

    # ---------------- 安全访问 ----------------

    def security_seed(self, level):
        resp = self._exchange(bytes([SID_SECURITY_ACCESS, level]))
        if len(resp) < 6:
            raise BadResponse(f"seed shorter than 4 bytes: {resp.hex()}")
        return int.from_bytes(resp[2:6], "big")

    def security_key(self, level, key):
        resp = self._exchange(
            bytes([SID_SECURITY_ACCESS, level + 1]) + key.to_bytes(4, "big")
        )
        if self.unlocked_level is None:
            self.unlocked_level = level
        return resp

    def unlock(self, level=1):
        seed = self.security_seed(level)
        key = self.key_algo(level, seed, self.params) & 0xFFFFFFFF
        self.security_key(level, key)
        self.unlocked_level = level
        return seed, key

    # ---------------- 故障码 ----------------

    def read_dtc(self, subfunction, **kwargs):
        """0x19 的通用入口。

        各个子功能的响应结构差别很大，解析放在 dtc.py，这里只负责发和收。
        """
        req = bytearray([SID_READ_DTC, subfunction])
        if "mask" in kwargs:
            req.append(kwargs["mask"])
        if "dtc" in kwargs:
            req += int(kwargs["dtc"]).to_bytes(3, "big")
        if "record" in kwargs:
            req.append(kwargs["record"])
        resp = self._exchange(bytes(req))
        return resp[2:] if len(resp) > 2 else b""

    def clear_dtc(self, group=0xFFFFFF):
        resp = self._exchange(bytes([SID_CLEAR_DTC]) + group.to_bytes(3, "big"))
        return resp

    # ---------------- DID ----------------

    def read_did(self, did):
        resp = self._exchange(bytes([SID_READ_DID]) + did.to_bytes(2, "big"))
        if len(resp) < 3:
            raise BadResponse(f"read DID response too short: {resp.hex()}")
        echo = int.from_bytes(resp[1:3], "big")
        if echo != did:
            raise BadResponse(f"DID echo mismatch: sent 0x{did:04X}, got 0x{echo:04X}")
        return resp[3:]

    def write_did(self, did, data):
        resp = self._exchange(
            bytes([SID_WRITE_DID]) + did.to_bytes(2, "big") + bytes(data)
        )
        echo = int.from_bytes(resp[1:3], "big") if len(resp) >= 3 else None
        if echo is not None and echo != did:
            raise BadResponse(f"DID echo mismatch: sent 0x{did:04X}, got 0x{echo:04X}")
        return resp

    # ---------------- 例程 ----------------

    def routine_control(self, routine_id, control=ROUTINE_START, data=b""):
        req = (
            bytes([SID_ROUTINE_CONTROL, control])
            + routine_id.to_bytes(2, "big")
            + bytes(data)
        )
        resp = self._exchange(req)
        echo = int.from_bytes(resp[2:4], "big") if len(resp) >= 4 else None
        if echo is not None and echo != routine_id:
            raise BadResponse(
                f"routine id echo mismatch: sent 0x{routine_id:04X}, got 0x{echo:04X}"
            )
        return resp[4:]

    # ---------------- 传输（刷写） ----------------

    def request_download(self, address, size, data_format=0x00):
        """0x34。请求里 ALFID = 0x44：高半字节是地址字节数，低半字节是长度字节数。

        返回值是 ECU 通告的 maxNumberOfBlockLength —— 含 SID 与块序号两字节，
        真正能装的数据要减 2。这个细节漏掉会让每块超长，ECU 报 0x13。
        """
        req = bytes([SID_REQUEST_DOWNLOAD, data_format, 0x44])
        req += address.to_bytes(4, "big") + size.to_bytes(4, "big")
        resp = self._exchange(req)
        if len(resp) < 3:
            raise BadResponse(f"download response too short: {resp.hex()}")
        return parse_download_length(resp[1], resp[2:])

    def transfer_data(self, sequence, data):
        seq = sequence & 0xFF
        resp = self._exchange(bytes([SID_TRANSFER_DATA, seq]) + bytes(data))
        if len(resp) < 2:
            raise BadResponse(f"transfer response too short: {resp.hex()}")
        if resp[1] != seq:
            raise BadResponse(f"block sequence echo {resp[1]:02X} != sent {seq:02X}")
        return resp[1]

    def request_transfer_exit(self):
        return self._exchange(bytes([SID_TRANSFER_EXIT]))


def parse_download_length(alfid, raw):
    """解析 0x34 响应里的长度字段。

    ALFID 的**高**半字节才是 maxNumberOfBlockLength 的字节数。
    （请求方向相反：0x44 里高半字节是地址长度、低半字节是长度字段长度。
    两个方向含义不同，混用会让块大小算成 0 或超长。）
    """
    n = (alfid >> 4) & 0x0F
    if n == 0 or len(raw) < n:
        raise BadResponse(f"bad length format identifier 0x{alfid:02X}")
    return int.from_bytes(raw[:n], "big")


def encode_address_length(address, size):
    """0x34 的地址与长度字段，统一 4 字节（ALFID = 0x44）。"""
    return struct.pack(">II", address & 0xFFFFFFFF, size & 0xFFFFFFFF)
