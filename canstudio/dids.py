"""DID 定义与解码。

工具里如果只把 DID 显示成十六进制，现场就得对着表手工查。这里把已知 DID
连同长度、名称、解码方式一起登记，界面直接出「VIN」「电池电压 3.66 V」。

未知 DID 不报错，按原始字节展示 —— 真实项目里一定会遇到表里没有的 DID，
工具不能因为不认识就罢工。
"""


class DidSpec:
    """一个数据标识符的元信息。"""

    __slots__ = ("did", "name", "length", "writable", "session", "decode")

    def __init__(self, did, name, length=None, writable=False, session=0x01, decode=None):
        self.did = did
        self.name = name
        self.length = length      # None 表示长度随内容变化
        self.writable = writable
        self.session = session    # 读写所需的最低会话
        self.decode = decode      # bytes -> 展示用的字符串

    def __repr__(self):
        return f"DidSpec(0x{self.did:04X}, {self.name})"


def _ascii(raw):
    text = raw.decode("ascii", errors="replace").strip("\x00 ")
    return text if text else "(空)"


def _millivolt(raw):
    if len(raw) != 2:
        return raw.hex()
    mv = (raw[0] << 8) | raw[1]
    return f"{mv} mV ({mv / 1000:.2f} V)"


def _single_byte(raw):
    return str(raw[0]) if len(raw) == 1 else raw.hex()


def _hex(raw):
    return raw.hex(" ").upper()


def _dtc_stats(raw):
    """0x0302：操作循环号(2) + 故障条数(1)。"""
    if len(raw) < 3:
        return raw.hex()
    cycle = (raw[0] << 8) | raw[1]
    return f"操作循环 {cycle}，故障条目 {raw[2]}"


# 与 can-node 的 ecu-server 一致。换 ECU 时改这张表即可，代码不用动。
KNOWN_DIDS = {
    0xF190: DidSpec(0xF190, "VIN", 17, False, 0x01, _ascii),
    0x0101: DidSpec(0x0101, "电池电压", 2, False, 0x01, _millivolt),
    0x0201: DidSpec(0x0201, "过压阈值", 1, True, 0x03, _single_byte),
    0x0301: DidSpec(0x0301, "刷写摘要", None, False, 0x01, _hex),
    0x0302: DidSpec(0x0302, "故障存储概览", 3, False, 0x01, _dtc_stats),
    0x0001: DidSpec(0x0001, "故障上下文", None, False, 0x01, _hex),
}


def find(variable):
    """按 DID 数值或用名字查定义。支持 "0xF190" / "F190" / "VIN"。"""
    if isinstance(variable, int):
        return KNOWN_DIDS.get(variable)

    text = str(variable).strip()
    upper = text.upper()

    for spec in KNOWN_DIDS.values():
        if spec.name.upper() == upper:
            return spec

    try:
        return KNOWN_DIDS.get(int(text, 16))
    except ValueError:
        return None


def describe(did, raw):
    """DID + 原始字节 -> (名称, 展示值)"""
    spec = KNOWN_DIDS.get(did)
    if spec is None:
        return f"0x{did:04X}", raw.hex(" ").upper()

    decoder = spec.decode or _hex
    try:
        value = decoder(raw)
    except Exception as exc:  # 解码失败不能拖垮整个读取流程
        value = f"<decode failed: {exc}>"
    return spec.name, value


def did_lengths():
    """给 dtc.parse_snapshot 用的长度表：快照里的 DID 靠它定界。"""
    return {spec.did: spec.length for spec in KNOWN_DIDS.values() if spec.length}
