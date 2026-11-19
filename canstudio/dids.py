"""DID 表：定义、解码与载入。

工具里如果只把 DID 显示成十六进制，现场就得对着表手工查。这里把 DID 连同
长度、名称、解码方式一起登记，界面直接出「VIN」「电池电压 3.66 V」。

表可以从 JSON 载入。真实项目里 DID 表跟着 ECU 走，换个 ECU 就该换一张表，
不该改源码 —— 所以代码里只留一份默认表，其余全部走配置。

未知 DID 不报错，按原始字节展示：真实项目里一定会遇到表里没有的 DID，
工具不能因为不认识就罢工。
"""
import json
import os

# 解码方式注册表。名字写在配置里，实现在这里，加新类型只动这一处。
_KIND_ASCII = "ascii"
_KIND_HEX = "hex"


class DidSpec:
    """一个数据标识符的元信息。"""

    __slots__ = ("did", "name", "length", "writable", "session", "decode", "unit")

    def __init__(self, did, name, length=None, writable=False, session=0x01,
                 decode=None, unit=""):
        self.did = did
        self.name = name
        self.length = length      # None 表示长度随内容变化
        self.writable = writable
        self.session = session    # 读写所需的最低会话
        self.decode = decode      # bytes -> 展示用的字符串
        self.unit = unit

    def __repr__(self):
        return f"DidSpec(0x{self.did:04X}, {self.name})"


# ---------------- 解码器 ----------------


def _decoder_ascii(raw, spec):
    text = raw.decode("ascii", errors="replace").strip("\x00 ")
    return text if text else "(空)"


def _decoder_hex(raw, spec):
    return raw.hex(" ").upper()


def _decoder_millivolt(raw, spec):
    if len(raw) != 2:
        return raw.hex(" ").upper()
    mv = (raw[0] << 8) | raw[1]
    return f"{mv} mV ({mv / 1000:.2f} V)"


def _decoder_dtc_stats(raw, spec):
    """操作循环号(2) + 故障条数(1)。"""
    if len(raw) < 3:
        return raw.hex(" ").upper()
    cycle = (raw[0] << 8) | raw[1]
    return f"操作循环 {cycle}，故障条目 {raw[2]}"


def _make_integer_decoder(signed, size, scale, offset, unit):
    """整数解码器。

    UDS 的数值一律大端（网络字节序），所以这里固定按大端读 ——
    与 DBC 里常见的 Intel 小端相反，弄反了会得到完全错误的数字。
    """

    def decode(raw, spec):
        if len(raw) < size:
            return raw.hex(" ").upper()
        value = int.from_bytes(raw[:size], "big", signed=signed)
        if scale not in (None, 1):
            value = value * scale

        if isinstance(value, float):
            text = f"{value:.3f}".rstrip("0").rstrip(".")
        else:
            text = str(value)
        return f"{text} {unit}".strip() if unit else text

    return decode


DECODERS = {
    _KIND_ASCII: _decoder_ascii,
    _KIND_HEX: _decoder_hex,
    "millivolt": _decoder_millivolt,
    "dtc_stats": _decoder_dtc_stats,
}

_INTEGER_SIZES = {
    "uint8": (False, 1),
    "uint16": (False, 2),
    "uint32": (False, 4),
    "int8": (True, 1),
    "int16": (True, 2),
    "int32": (True, 4),
}


def build_decoder(kind, scale=None, offset=None, unit=""):
    """按配置里的 kind 造解码函数。未知 kind 回落到 hex，不让整张表失效。"""
    if kind in DECODERS:
        return DECODERS[kind]
    if kind in _INTEGER_SIZES:
        signed, size = _INTEGER_SIZES[kind]
        return _make_integer_decoder(signed, size, scale, offset, unit)
    return _decoder_hex


# ---------------- 表 ----------------


class DidTable:
    """一张 DID 表。"""

    def __init__(self, specs=(), name="", path=None):
        self.specs = {}
        for spec in specs:
            self.specs[spec.did] = spec
        self.name = name
        self.path = path

    def __len__(self):
        return len(self.specs)

    def __iter__(self):
        return iter(sorted(self.specs.values(), key=lambda s: s.did))

    @classmethod
    def load(cls, path):
        """从 JSON 载入。

        格式：
            {"ecu": "名称",
             "dids": [{"id": "0xF190", "name": "VIN", "length": 17,
                       "decode": "ascii", "writable": false, "session": 1}]}

        id 支持 "0xF190" / "F190" / 61712 三种写法；session 可写 1/3 或
        "default"/"extended"。
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            entries = data
            name = ""
        else:
            entries = data.get("dids", [])
            name = data.get("ecu", "")

        specs = []
        for index, entry in enumerate(entries):
            if "id" not in entry:
                raise ValueError(f"{path}: 第 {index + 1} 项缺少 id")
            did = _parse_id(entry["id"])
            decode = build_decoder(
                entry.get("decode", _KIND_HEX),
                entry.get("scale"),
                entry.get("offset"),
                entry.get("unit", ""),
            )
            specs.append(DidSpec(
                did=did,
                name=entry.get("name") or f"0x{did:04X}",
                length=entry.get("length"),
                writable=bool(entry.get("writable", False)),
                session=_parse_session(entry.get("session", 1)),
                decode=decode,
                unit=entry.get("unit", ""),
            ))

        return cls(specs, name=name or os.path.basename(path), path=path)

    def find(self, key):
        """按 DID 数值、十六进制字符串或名称查找。"""
        if isinstance(key, int):
            return self.specs.get(key)

        text = str(key).strip()
        upper = text.upper()

        for spec in self.specs.values():
            if spec.name.upper() == upper:
                return spec

        try:
            return self.specs.get(int(text, 16))
        except ValueError:
            return None

    def describe(self, did, raw):
        """DID + 原始字节 -> (名称, 展示值)"""
        spec = self.specs.get(did)
        if spec is None:
            return f"0x{did:04X}", raw.hex(" ").upper()

        decoder = spec.decode or _decoder_hex
        try:
            value = decoder(raw, spec)
        except Exception as exc:      # 解码失败不能拖垮整个读取流程
            value = f"<decode failed: {exc}>"
        return spec.name, value

    def did_lengths(self):
        """给 dtc.parse_snapshot 用的长度表：快照里的 DID 靠它定界。"""
        return {spec.did: spec.length for spec in self.specs.values() if spec.length}


def _parse_id(value):
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16) if not text.lower().startswith("0x") else int(text, 16)


def _parse_session(value):
    if isinstance(value, str):
        table = {"default": 0x01, "programming": 0x02, "extended": 0x03}
        return table.get(value.strip().lower(), 0x01)
    return int(value)


# ---------------- 默认表 ----------------

# 与 can-node 的 ecu-server 一致。换 ECU 时用 DidTable.load() 载入新的，
# 这张留着当出厂默认，保证工具开箱就能跑。
_BUILTIN = [
    DidSpec(0xF190, "VIN", 17, False, 0x01, _decoder_ascii),
    DidSpec(0x0101, "电池电压", 2, False, 0x01, _decoder_millivolt, "V"),
    DidSpec(0x0201, "过压阈值", 1, True, 0x03, _make_integer_decoder(False, 1, None, None, "")),
    DidSpec(0x0301, "刷写摘要", None, False, 0x01, _decoder_hex),
    DidSpec(0x0302, "故障存储概览", 3, False, 0x01, _decoder_dtc_stats),
    DidSpec(0x0001, "故障上下文", None, False, 0x01, _decoder_hex),
]

DEFAULT_TABLE = DidTable(_BUILTIN, name="can-node ecu-server")
KNOWN_DIDS = DEFAULT_TABLE.specs


# 模块级函数保留：老调用方（CLI、测试）不必都改成对象
def find(variable):
    return DEFAULT_TABLE.find(variable)


def describe(did, raw):
    return DEFAULT_TABLE.describe(did, raw)


def did_lengths():
    return DEFAULT_TABLE.did_lengths()
