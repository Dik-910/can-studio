"""故障码：编码转换与 0x19 各子功能的响应解析。

服务器侧怎么编码，这里就怎么解。0x19 各子功能的响应结构差别很大
（有的带可用性掩码，有的带状态，有的带快照），所以按子功能分开写，
不用一个万能函数把它们糊在一起。
"""
import struct

from .errors import BadResponse

# 状态位（ISO 14229-1，位序不可改）
STATUS_BITS = [
    (0x01, "testFailed", "本次测试失败"),
    (0x02, "testFailedThisCycle", "本操作循环内失败"),
    (0x04, "pending", "待确认"),
    (0x08, "confirmed", "已确认"),
    (0x10, "notCompletedSinceClear", "清码后未测过"),
    (0x20, "failedSinceClear", "清码后失败过"),
    (0x40, "notCompletedThisCycle", "本循环未测过"),
    (0x80, "warningIndicator", "请求点亮故障灯"),
]

DTC_FORMAT_ISO14229 = 0x01


def format_dtc(code):
    """3 字节 DTC -> 人类可读形式，例如 0xC10087 -> U010087。

    高两位决定系统类别：00 动力总成、01 底盘、10 车身、11 网络。
    剩下 22 位固定写 6 个十六进制位 —— 不省略前导零。省了就会出现
    「U10087」这种 5 位写法，与 2 字节 DTC 的 4 位写法撞在一起，
    解析时无法判断该按几位还原。
    """
    code &= 0xFFFFFF
    letters = "PCBU"
    first = (code >> 16) & 0xFF
    letter = letters[(first >> 6) & 0x03]
    rest = ((first & 0x3F) << 16) | (code & 0xFFFF)
    return f"{letter}{rest:06X}"


def parse_dtc(text):
    """format_dtc 的逆运算。

    接受 3~6 位十六进制：现场手抄的码常有漏写前导零的情况（U10087），
    只要数值能落进 22 位就还原得回来。
    """
    text = text.strip().upper()
    letters = {"P": 0, "C": 1, "B": 2, "U": 3}
    if not text or text[0] not in letters:
        raise ValueError(f"not a DTC: {text}")

    body = text[1:]
    if not 3 <= len(body) <= 6 or any(c not in "0123456789ABCDEF" for c in body):
        raise ValueError(f"not a DTC: {text}")

    value = int(body, 16)
    if value > 0x3FFFFF:
        raise ValueError(f"DTC value out of range for 3 bytes: {text}")

    first = ((letters[text[0]] & 0x03) << 6) | ((value >> 16) & 0x3F)
    return ((first & 0xFF) << 16) | (value & 0xFFFF)


def decode_status(status):
    """状态字节 -> [(名字, 中文说明)]，只返回置位的位。"""
    return [(name, cn) for bit, name, cn in STATUS_BITS if status & bit]


def describe_status(status):
    """一行摘要，界面表格里用。"""
    names = [name for bit, name, _ in STATUS_BITS if status & bit]
    return "|".join(names) if names else "-"


class DtcEntry:
    """一条故障记录。字段与 0x19 各子功能能提供的信息一一对应。"""

    __slots__ = ("code", "status", "snapshot", "extended", "record")

    def __init__(self, code, status=0, snapshot=None, extended=None, record=None):
        self.code = code
        self.status = status
        self.snapshot = snapshot or []
        self.extended = extended
        self.record = record

    @property
    def text(self):
        return format_dtc(self.code)

    def __repr__(self):
        return f"DtcEntry({self.text}, status=0x{self.status:02X})"

    def __eq__(self, other):
        return isinstance(other, DtcEntry) and self.code == other.code and \
            self.status == other.status

    def __hash__(self):
        return hash((self.code, self.status))


def parse_snapshot(blob, did_lengths=None):
    """解析快照内容：[identifier 个数] + [(DID(2), 数据)] × n

    每个 DID 的数据长度按 did_lengths 查；查不到时：
      - 若是最后一个 identifier，把剩下的字节都给它（能读出来总比报错好）
      - 否则无法定界，抛 BadResponse 说明配置缺了长度
    """
    blob = bytes(blob)
    if not blob:
        return []

    count = blob[0]
    pos = 1
    out = []

    for index in range(count):
        if pos + 2 > len(blob):
            raise BadResponse(f"snapshot truncated at identifier {index}: {blob.hex()}")
        did = int.from_bytes(blob[pos : pos + 2], "big")
        pos += 2

        length = (did_lengths or {}).get(did)
        if length is None:
            if index == count - 1:
                length = len(blob) - pos
            else:
                raise BadResponse(
                    f"unknown length for DID 0x{did:04X} at identifier {index}; "
                    "pass did_lengths to decode"
                )

        if pos + length > len(blob):
            raise BadResponse(
                f"DID 0x{did:04X} expects {length} bytes, only {len(blob) - pos} left"
            )
        out.append((did, blob[pos : pos + length]))
        pos += length

    return out


def _dtc_status_pairs(payload):
    entries = []
    i = 0
    while i + 4 <= len(payload):
        code = int.from_bytes(payload[i : i + 3], "big")
        entries.append(DtcEntry(code, payload[i + 3]))
        i += 4
    return entries


def parse_report_number(payload):
    """0x19 01 -> dict(availability, format, count)"""
    if len(payload) < 4:
        raise BadResponse(f"0x19 01 response too short: {payload.hex()}")
    return {
        "availability": payload[0],
        "format": payload[1],
        "count": struct.unpack(">H", payload[2:4])[0],
    }


def parse_report_by_mask(payload):
    """0x19 02 / 0x0A -> 故障列表（0A 的第一字节也是可用性掩码）"""
    if not payload:
        raise BadResponse("empty DTC list response")
    return _dtc_status_pairs(payload[1:])


def parse_snapshot_identification(payload):
    """0x19 03 -> [(code, record)]"""
    out = []
    i = 0
    while i + 4 <= len(payload):
        code = int.from_bytes(payload[i : i + 3], "big")
        out.append((code, payload[i + 3]))
        i += 4
    return out


def parse_snapshot_by_dtc(payload, did_lengths=None):
    """0x19 04 -> DtcEntry（含快照）"""
    if len(payload) < 6:
        raise BadResponse(f"0x19 04 response too short: {payload.hex()}")
    code = int.from_bytes(payload[0:3], "big")
    status = payload[3]
    record = payload[4]
    entry = DtcEntry(code, status, parse_snapshot(payload[5:], did_lengths), record=record)
    return entry


def parse_stored_data(payload, did_lengths=None):
    """0x19 05 -> (record, [DtcEntry])。快照长度不定，逐条往回推。"""
    if not payload:
        raise BadResponse("empty 0x19 05 response")
    record = payload[0]
    body = payload[1:]
    entries = []

    # 每条记录至少 4 字节 DTC+状态，快照首字节是 identifier 个数。
    # 长度不定意味着必须靠 DID 长度表定界；没有表就只能按「最后一条吃剩余」处理。
    pos = 0
    while pos + 5 <= len(body):
        code = int.from_bytes(body[pos : pos + 3], "big")
        status = body[pos + 3]
        rest = body[pos + 4 :]
        snaps = parse_snapshot(rest, did_lengths)
        consumed = 4 + _snapshot_consumed(rest, did_lengths)
        entries.append(DtcEntry(code, status, snaps, record=record))
        if consumed <= 0:
            break
        pos += consumed

    return record, entries


def _snapshot_consumed(blob, did_lengths):
    """算一个快照占了多少字节，用于在 0x19 05 的多条记录间移动游标。"""
    if not blob:
        return 0
    count = blob[0]
    pos = 1
    for index in range(count):
        if pos + 2 > len(blob):
            return 0
        did = int.from_bytes(blob[pos : pos + 2], "big")
        pos += 2
        length = (did_lengths or {}).get(did)
        if length is None:
            if index == count - 1:
                return len(blob)
            return 0
        pos += length
    return pos


def parse_extended_data(payload):
    """0x19 06 -> DtcEntry（扩展数据放在 extended 里）"""
    if len(payload) < 6:
        raise BadResponse(f"0x19 06 response too short: {payload.hex()}")
    code = int.from_bytes(payload[0:3], "big")
    status = payload[3]
    record = payload[4]
    entry = DtcEntry(code, status, extended=bytes(payload[5:]), record=record)
    return entry
