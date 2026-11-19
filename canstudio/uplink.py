"""网关 MQTT 上行：把网关聚合后上报的报文还原成帧。

为什么需要这条通道：站在网关后面时，tester 侧只能看到诊断流量，看不到
节点发的周期帧（网关不把整条总线复制过来）。想看「节点状态 / 总线状态」，
只能读网关的上行。

上行格式由 can-gateway 的 route_engine.c 里 on_agg_export() 决定：

    {"gw":"can-gateway","ch":"vcan1","id":"0x181",
     "n":25,"dlc":8,"d":"0000000000000000","ts":123456789}

`n` 是这 500 ms 窗口里聚合到的帧数 —— 0x181 每 20 ms 一帧，所以 n 一般在 25
上下。这个数字反过来能校验周期有没有跑偏：期望 25，实际 12，说明丢帧了。

`ts` 是网关的 CLOCK_MONOTONIC 毫秒，**不是墙上时间**，不能直接当时刻显示。
所以这里只把它留给排序与排查用，表格显示的是收到消息时的本地时间。
"""
import json
import os
import sys
import time

DEFAULT_TOPIC = "gw/can"
DEFAULT_BROKER = ("127.0.0.1", 1883)


class UplinkFrame:
    """一条上行报文。"""

    __slots__ = ("channel", "frame_id", "data", "count", "ts_ms")

    def __init__(self, channel, frame_id, data, count=1, ts_ms=None):
        self.channel = channel
        self.frame_id = frame_id
        self.data = data
        self.count = count
        self.ts_ms = ts_ms

    def __repr__(self):
        return (f"UplinkFrame(0x{self.frame_id:03X}, {len(self.data)} bytes, "
                f"n={self.count})")


def parse_message(payload):
    """解析一条上行 JSON。格式不对返回 None —— 总线上/代理里可能有别的东西，
    一条坏消息不该让整个订阅停掉。"""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    frame_id = _parse_id(data.get("id"))
    raw = data.get("d")
    if frame_id is None or not isinstance(raw, str):
        return None

    try:
        blob = bytes.fromhex(raw)
    except ValueError:
        return None

    dlc = data.get("dlc")
    if isinstance(dlc, int) and 0 <= dlc <= 64:
        if len(blob) > dlc:
            blob = blob[:dlc]
        elif len(blob) < dlc:
            # dlc 与数据长度不一致时以 dlc 为准补齐，避免下游按错长度解码
            blob = blob + bytes(dlc - len(blob))

    count = data.get("n")
    if not isinstance(count, int) or count <= 0:
        count = 1

    ts_ms = data.get("ts")
    if not isinstance(ts_ms, int):
        ts_ms = None

    return UplinkFrame(data.get("ch", ""), frame_id, blob, count, ts_ms)


def _parse_id(value):
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def parse_broker(text):
    """"host:port" 或 "tcp://host:port" -> (host, port)。"""
    if not text:
        return DEFAULT_BROKER

    cleaned = text.strip()
    for prefix in ("tcp://", "mqtt://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]

    if ":" in cleaned:
        host, _, port = cleaned.rpartition(":")
        try:
            return host or DEFAULT_BROKER[0], int(port)
        except ValueError:
            return DEFAULT_BROKER
    return cleaned or DEFAULT_BROKER[0], DEFAULT_BROKER[1]


class UplinkSource:
    """订阅网关上行。

    on_frame(UplinkFrame) 会被 paho 的网络线程调用 —— 界面侧要经信号转回
    主线程，不能在这个回调里直接碰控件。
    """

    def __init__(self, host=DEFAULT_BROKER[0], port=DEFAULT_BROKER[1],
                 topic=DEFAULT_TOPIC, on_frame=None, on_state=None,
                 client_id=None):
        self.host = host
        self.port = port
        self.topic = topic
        self.on_frame = on_frame
        self.on_state = on_state
        self.client_id = client_id or f"can-studio-{os.getpid()}"
        self._client = None
        self._loop_started = False
        self.received = 0
        self.dropped = 0

    # ---------------- 生命周期 ----------------

    def start(self, timeout_s=3.0):
        ensure_dependencies()
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=self.client_id)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        self._client = client
        client.connect(self.host, self.port, keepalive=20)
        client.loop_start()
        self._loop_started = True
        return self

    def stop(self):
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            try:
                self._client.loop_stop()
            except Exception:
                pass
        self._client = None
        self._loop_started = False

    @property
    def running(self):
        return self._loop_started

    # ---------------- 回调 ----------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # 重连后要重新订阅：broker 端不会记住上一次的订阅
        client.subscribe(self.topic, qos=0)
        self._notify(f"已连接 {self.host}:{self.port}，订阅 {self.topic}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._notify(f"上行断开（{reason_code}）")

    def _on_message(self, client, userdata, message):
        frame = parse_message(message.payload)
        if frame is None:
            self.dropped += 1
            return

        self.received += 1
        if self.on_frame is not None:
            self.on_frame(frame)

    def _notify(self, text):
        if self.on_state is not None:
            self.on_state(text)


def ensure_dependencies():
    """paho 装在本机的 .pylibs 下（与项目其他依赖一致）。"""
    for candidate in (os.environ.get("CANSTUDIO_PYLIB"), "/home/dikl/work/.pylibs"):
        if candidate and os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)


def now():
    return time.time()
