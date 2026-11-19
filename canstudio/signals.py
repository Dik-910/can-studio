"""按 DBC 解码 CAN 帧。

监视页如果只显示十六进制字节，看的人得自己对表。有了 DBC 就能显示
「NodeState=2  BusOffCnt=0  UptimeS=412」，这才是诊断工具该有的样子。

DBC 的解析交给 cantools —— 格式里有多路复用、值表、浮点编码这些细节，
自己实现只会引入新的 bug。这里做的是把它包成「解不出来也不炸」的接口：
总线上永远会有 DBC 里没有的 ID，工具不能因为不认识就中断监视。
"""


class FrameDecode:
    """一帧的解码结果。"""

    __slots__ = ("frame_id", "name", "signals", "error")

    def __init__(self, frame_id, name=None, signals=(), error=None):
        self.frame_id = frame_id
        self.name = name
        self.signals = list(signals)     # [(信号名, 值, 单位)]
        self.error = error

    def summary(self):
        """一行文字，直接放进表格。"""
        if self.error:
            return f"<{self.error}>"
        if not self.signals:
            return self.name or ""
        return "  ".join(f"{name}={fmt(value, unit)}"
                         for name, value, unit in self.signals)

    def __repr__(self):
        return f"FrameDecode(0x{self.frame_id:03X}, {self.name}, {len(self.signals)} signals)"


def parse_id_filter(text):
    """把 "0x180, 181 0x200" 这类输入解析成 ID 集合。

    空串表示不过滤，返回空集合。无法识别的项直接跳过 —— 用户边打字边过滤，
    中途的 "0x1" 这种半截输入不该弹错误框。
    """
    if not text:
        return set()

    ids = set()
    for token in str(text).replace(",", " ").replace(";", " ").split():
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token, 16)
        except ValueError:
            continue
        if 0 <= value <= 0x1FFFFFFF:
            ids.add(value)
    return ids


def fmt(value, unit=""):
    """数值格式化：整数不带小数点，小数留两位。"""
    if isinstance(value, float):
        text = f"{value:.2f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return f"{text} {unit}".strip() if unit else text


class SignalDatabase:
    """一张 DBC 数据库。"""

    def __init__(self, database, path=None):
        self._db = database
        self.path = path
        self._cache = {}

    @classmethod
    def load(cls, path):
        try:
            import cantools
        except ImportError as exc:
            raise RuntimeError(
                "cantools 未安装，无法解析 DBC：pip install cantools"
            ) from exc

        database = cantools.database.load_file(path)
        return cls(database, path)

    @classmethod
    def from_string(cls, text):
        import cantools

        return cls(cantools.database.load_string(text))

    def __len__(self):
        return len(self._db.messages)

    @property
    def messages(self):
        return list(self._db.messages)

    def message(self, frame_id):
        if frame_id in self._cache:
            return self._cache[frame_id]

        try:
            msg = self._db.get_message_by_frame_id(frame_id)
        except KeyError:
            msg = None
        self._cache[frame_id] = msg
        return msg

    def decode(self, frame_id, data):
        """解一帧。DBC 里没有的 ID 返回 None，长度不对返回带 error 的结果。"""
        msg = self.message(frame_id)
        if msg is None:
            return None

        try:
            values = msg.decode(bytes(data), decode_choices=False,
                                allow_truncated=True)
        except Exception as exc:      # cantools 的异常类型不统一，统一兜住
            return FrameDecode(frame_id, msg.name, error=f"解码失败: {exc}")

        signals = []
        for signal in msg.signals:
            if signal.name not in values:
                continue
            signals.append((signal.name, values[signal.name], signal.unit or ""))
        return FrameDecode(frame_id, msg.name, signals)

    def describe_id(self, frame_id):
        msg = self.message(frame_id)
        return f"0x{frame_id:03X} {msg.name}" if msg else f"0x{frame_id:03X}"
