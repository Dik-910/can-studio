#!/usr/bin/python3
"""命令行入口。界面还没接上，先用它验证工具真在跟 ECU 说话。

    ./cli.py did read VIN 0x0101 0x0302
    ./cli.py did write 0x0201 3C
    ./cli.py dtc list
    ./cli.py dtc clear
    ./cli.py raw 22 F1 90
    ./cli.py monitor

所有子命令共用 --if / --tx / --rx，默认对准 can-node 的 ecu-server
（vcan0、0x7E0 / 0x7E8）。
"""
import argparse
import atexit
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canstudio import dtc as dtc_mod
from canstudio import dids
from canstudio.dids import DEFAULT_TABLE, DidTable
from canstudio.flash import FlashJob, load_firmware
from canstudio.dids import describe, find
from canstudio.errors import CanStudioError, NegativeResponse, TransportTimeout
from canstudio.link import FrameMonitor, RecordingLink, SocketCanLink
from canstudio.signals import SignalDatabase, format_timestamp, parse_id_filter
from canstudio.uplink import UplinkSource, parse_broker
from canstudio.uds import SESSION_EXTENDED, UdsClient


def open_client(args, recording=False):
    link = SocketCanLink(channel=args.iface, txid=args.tx, rxid=args.rx)
    # 不管是正常结束还是抛异常，都要关总线，否则 python-can 退出时报警告
    atexit.register(link.close)
    if recording:
        link = RecordingLink(link)
    return UdsClient(link, p2_ms=args.p2, key_algo=_key_algo(args)), link


def load_did_table(args):
    """按 --did-table 载入 DID 表，没给就用内置默认表。"""
    path = getattr(args, "did_table", None)
    if not path:
        return DEFAULT_TABLE

    try:
        table = DidTable.load(path)
    except Exception as exc:
        print(f"DID 表载入失败：{exc}", file=sys.stderr)
        raise SystemExit(2)

    print(f"DID 表 {path}：{len(table)} 项（{table.name}）", file=sys.stderr)
    return table


def _key_algo(args):
    """默认按 can-node 的算法；--key-file 可以换成别的 ECU 的算法。"""
    if not args.key_file:
        return None

    namespace = {}
    with open(args.key_file, encoding="utf-8") as f:
        exec(compile(f.read(), args.key_file, "exec"), namespace)

    for name in ("key_algo", "calculate_key", "key"):
        if callable(namespace.get(name)):
            return namespace[name]
    raise SystemExit(f"{args.key_file} 里没有 key_algo 函数")


def require_session(client, session_type, args):
    if args.no_session:
        return
    client.change_session(session_type)
    if session_type == SESSION_EXTENDED and not args.no_unlock:
        try:
            client.unlock(1)
        except NegativeResponse as exc:
            print(f"[warn] 安全访问失败：{exc}", file=sys.stderr)


def cmd_session(args):
    client, _ = open_client(args)
    print(client.change_session(args.type))
    return 0


def cmd_reset(args):
    client, _ = open_client(args)
    print(client.ecu_reset(args.type))
    return 0


def cmd_did_read(args):
    client, _ = open_client(args)
    table = load_did_table(args)
    if args.unlock:
        require_session(client, SESSION_EXTENDED, args)

    failed = 0
    for name in args.dids:
        spec = table.find(name)
        if spec is None:
            print(f"{name}: 无法识别的 DID", file=sys.stderr)
            failed += 1
            continue
        try:
            raw = client.read_did(spec.did)
        except (NegativeResponse, TransportTimeout) as exc:
            print(f"0x{spec.did:04X} {spec.name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        label, value = table.describe(spec.did, raw)
        print(f"0x{spec.did:04X}  {label:<12} {value}")
    return 0 if failed == 0 else 1


def cmd_did_write(args):
    client, _ = open_client(args)
    table = load_did_table(args)
    spec = table.find(args.did)
    if spec is None:
        print(f"无法识别的 DID: {args.did}", file=sys.stderr)
        return 1

    data = bytes.fromhex(args.value.replace(" ", ""))
    if spec.length is not None and len(data) != spec.length:
        print(f"警告：{spec.name} 期望 {spec.length} 字节，给了 {len(data)} 字节",
              file=sys.stderr)

    client.change_session(SESSION_EXTENDED)
    if not args.no_unlock:
        client.unlock(1)
    client.write_did(spec.did, data)
    print(f"0x{spec.did:04X} {spec.name} 写入成功（{len(data)} 字节）")
    return 0


def cmd_did_watch(args):
    """周期读取 DID，值变化时标出来。

    现场最常见的用法：改一个参数，盯着它和相邻的几个量看变化。
    """
    client, _ = open_client(args)
    table = load_did_table(args)

    specs = [table.find(name) for name in args.dids] if args.dids else list(table)
    specs = [s for s in specs if s is not None]
    if not specs:
        print("没有可监视的 DID", file=sys.stderr)
        return 1

    if args.unlock:
        require_session(client, SESSION_EXTENDED, args)

    previous = {}
    rounds = 0
    try:
        while True:
            moment = time.strftime("%H:%M:%S")
            parts = []
            changed = []

            for spec in specs:
                try:
                    raw = client.read_did(spec.did)
                except (NegativeResponse, TransportTimeout) as exc:
                    parts.append(f"{spec.name}=<{exc}>")
                    continue

                label, value = table.describe(spec.did, raw)
                parts.append(f"{label}={value}")
                if spec.did in previous and previous[spec.did] != value:
                    changed.append(label)
                previous[spec.did] = value

            mark = f"  *变化 {','.join(changed)}" if changed else ""
            print(f"[{moment}] " + "  ".join(parts) + mark, flush=True)

            rounds += 1
            if args.count and rounds >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()

    return 0


def cmd_dtc_list(args):
    client, _ = open_client(args)
    payload = client.read_dtc(0x02, mask=args.mask)
    entries = dtc_mod.parse_report_by_mask(payload)

    if args.json:
        import json
        print(json.dumps([
            {"code": e.text, "raw": f"0x{e.code:06X}", "status": f"0x{e.status:02X}",
             "bits": [n for n, _ in dtc_mod.decode_status(e.status)]}
            for e in entries], ensure_ascii=False, indent=2))
        return 0

    if not entries:
        print("无故障")
        return 0

    print(f"{'DTC':<8} {'状态':<6} 明细")
    for e in entries:
        print(f"{e.text:<8} 0x{e.status:02X}   {dtc_mod.describe_status(e.status)}")
    return 0


def cmd_dtc_supported(args):
    client, _ = open_client(args)
    payload = client.read_dtc(0x0A)
    entries = dtc_mod.parse_report_by_mask(payload)
    for e in entries:
        print(f"{e.text:<8} 0x{e.code:06X}")
    return 0


def cmd_dtc_clear(args):
    client, _ = open_client(args)
    client.change_session(SESSION_EXTENDED)
    client.clear_dtc(args.group)
    print(f"已清除 group 0x{args.group:06X}")
    return 0


def cmd_dtc_snapshot(args):
    client, _ = open_client(args)
    record = args.record

    payload = client.read_dtc(0x03)
    pairs = dtc_mod.parse_snapshot_identification(payload)
    if not pairs:
        print("没有任何 DTC 存了快照")
        return 0

    print("快照标识：", ", ".join(f"{dtc_mod.format_dtc(c)}@记录{r}" for c, r in pairs))

    payload = client.read_dtc(0x05, record=record)
    rec, entries = dtc_mod.parse_stored_data(payload, dids.did_lengths())
    for e in entries:
        print(f"\n{dtc_mod.format_dtc(e.code)} 状态 0x{e.status:02X} 记录 {rec}")
        for did, data in e.snapshot:
            label, value = describe(did, data)
            print(f"  0x{did:04X} {label:<12} {value}")
    return 0


def cmd_flash(args):
    image = load_firmware(args.file, base_address=args.base)
    print(f"固件 {args.file}: 0x{image.start:08X}..0x{image.end:08X}  "
          f"{image.size} 字节  crc32 0x{image.crc32:08X}")

    if not args.yes:
        answer = input("确认刷写？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消")
            return 1

    client, _ = open_client(args)
    job = FlashJob(client, block_retries=args.retries)

    last = [0]

    def progress(sent, total):
        # 同一行刷新，避免刷出几百行
        percent = sent * 100 // total
        bar = "#" * (percent // 4)
        print(f"\r  [{bar:<25}] {percent:3d}%  {sent}/{total}", end="", flush=True)
        last[0] = sent

    result = job.run(image, progress=progress)
    print()
    print(f"完成：{result.bytes_written} 字节 / {result.blocks} 块，"
          f"{result.elapsed_s:.2f} s")
    if result.crc_ecu is not None:
        print(f"CRC 校验通过：0x{result.crc_ecu:08X}")
    else:
        print("CRC 校验：ECU 只回了状态，未提供 CRC 值")

    if args.reset:
        client.ecu_reset(1)
        print("已发送复位")
    return 0


def cmd_raw(args):
    client, link = open_client(args, recording=True)
    request = bytes.fromhex(" ".join(args.bytes).replace(" ", ""))
    client.link.send(request)
    resp = client.link.recv(args.p2 / 1000.0)
    if resp is None:
        print("无响应", file=sys.stderr)
        return 1
    print(" ".join(f"{b:02X}" for b in resp))
    return 0


def cmd_monitor_mqtt(args):
    """读网关的 MQTT 上行。

    站在网关后面时总线上的周期帧看不到（网关不复制整条总线），
    只能靠这条通道看节点与总线状态。
    """
    host, port = parse_broker(args.broker)
    topic = args.topic or "gw/can"

    db = None
    if args.dbc:
        try:
            db = SignalDatabase.load(args.dbc)
        except Exception as exc:
            print(f"DBC 载入失败：{exc}", file=sys.stderr)
            return 1
        print(f"DBC {args.dbc}：{len(db)} 条报文")

    only = parse_id_filter(args.ids)
    seen = 0

    def on_frame(frame):
        nonlocal seen
        if only and frame.frame_id not in only:
            return
        seen += 1
        moment = time.strftime("%H:%M:%S")
        id_text = f"0x{frame.frame_id:03X}"
        if db is None:
            print(f"{moment}  {id_text}  {frame.data.hex(' ').upper()}  ×{frame.count}")
            return

        decoded = db.decode(frame.frame_id, frame.data)
        if decoded is None:
            print(f"{moment}  {id_text}  {frame.data.hex(' ').upper()}  ×{frame.count}")
        else:
            print(f"{moment}  {id_text}  {decoded.name:<12} ×{frame.count:<3} "
                  f"{decoded.summary()}")

    source = UplinkSource(host, port, topic, on_frame=on_frame,
                          on_state=lambda text: print(text))
    try:
        source.start()
    except Exception as exc:
        print(f"上行连接失败：{exc}", file=sys.stderr)
        return 1

    print(f"订阅 {host}:{port} {topic}（Ctrl-C 退出）")
    try:
        while not args.count or seen < args.count:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()

    print(f"共 {source.received} 条上行消息，丢弃 {source.dropped} 条")
    return 0


def cmd_monitor(args):
    """原始帧监视。

    单独开一条链路：UDS 那条的帧会被 ISO-TP 协议栈消费掉，
    在这里只能看到重组后的报文，看不到总线上真正流动的周期帧。
    """
    if args.mqtt:
        return cmd_monitor_mqtt(args)

    client, _ = open_client(args, recording=True)

    db = None
    if args.dbc:
        try:
            db = SignalDatabase.load(args.dbc)
        except Exception as exc:
            print(f"DBC 载入失败：{exc}", file=sys.stderr)
            return 1
        print(f"DBC {args.dbc}：{len(db)} 条报文")

    only = parse_id_filter(args.ids)
    monitor = FrameMonitor(args.iface)
    if only:
        print(f"只看 {', '.join(f'0x{v:03X}' for v in sorted(only))}")
    print(f"监视 {args.iface}（Ctrl-C 退出）")
    last_ping = time.monotonic()
    seen = 0

    try:
        while True:
            now = time.monotonic()
            if now - last_ping > 2.0:
                client.tester_present()
                last_ping = now

            item = monitor.read(0.1)
            if item is None:
                continue

            timestamp, frame_id, data, extended = item
            if only and frame_id not in only:
                continue
            seen += 1
            moment = format_timestamp(timestamp)
            id_text = f"0x{frame_id:08X}" if extended else f"0x{frame_id:03X}"
            raw = " ".join(f"{b:02X}" for b in data)

            if db is None:
                print(f"{moment}  {id_text}  {raw}")
            else:
                decoded = db.decode(frame_id, data)
                if decoded is None:
                    print(f"{moment}  {id_text}  {raw}  (DBC 无此报文)")
                else:
                    print(f"{moment}  {id_text}  {decoded.name:<12} {decoded.summary()}")

            if args.count and seen >= args.count:
                break
    except KeyboardInterrupt:
        print()
    finally:
        monitor.close()
    return 0


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-i", "--iface", default="vcan0", help="CAN 接口（默认 vcan0）")
    common.add_argument("--tx", type=lambda s: int(s, 0), default=0x7E0, help="请求 ID")
    common.add_argument("--rx", type=lambda s: int(s, 0), default=0x7E8, help="响应 ID")
    common.add_argument("--p2", type=int, default=50, help="P2 窗口（毫秒）")
    common.add_argument("--key-file", help="提供 key_algo 的 python 文件，换 ECU 用")
    common.add_argument("--did-table", help="DID 表 JSON；不给则用内置默认表")
    common.add_argument("--no-session", action="store_true", help="不自动切会话")

    parser = argparse.ArgumentParser(prog="can-studio", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("session", parents=[common], help="切换会话")
    p.add_argument("type", type=lambda s: int(s, 0), nargs="?", default=3)
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("reset", parents=[common], help="ECU 复位")
    p.add_argument("type", type=lambda s: int(s, 0), nargs="?", default=1)
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("did", parents=[common], help="DID 读写")
    did_sub = p.add_subparsers(dest="did_command", required=True)

    q = did_sub.add_parser("read", parents=[common], help="读一个或多个 DID")
    q.add_argument("dids", nargs="+", help="DID 数值或名字，如 0xF190 / VIN")
    q.add_argument("--unlock", action="store_true", help="读之前先切扩展会话并解锁")
    q.set_defaults(func=cmd_did_read)

    q = did_sub.add_parser("watch", parents=[common], help="周期读取，变化处标出")
    q.add_argument("dids", nargs="*", help="要监视的 DID；省略则监视整张表")
    q.add_argument("--interval", type=float, default=1.0, help="周期（秒）")
    q.add_argument("--count", type=int, default=0, help="采这么多轮后退出，0 表示一直跑")
    q.add_argument("--unlock", action="store_true", help="先切扩展会话并解锁")
    q.set_defaults(func=cmd_did_watch)

    q = did_sub.add_parser("write", parents=[common], help="写 DID")
    q.add_argument("did")
    q.add_argument("value", help="十六进制字节，如 3C")
    q.add_argument("--no-unlock", action="store_true")
    q.set_defaults(func=cmd_did_write)

    p = sub.add_parser("dtc", parents=[common], help="故障码")
    dtc_sub = p.add_subparsers(dest="dtc_command", required=True)

    q = dtc_sub.add_parser("list", parents=[common], help="按状态掩码读码")
    q.add_argument("--mask", type=lambda s: int(s, 0), default=0xFF)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_dtc_list)

    q = dtc_sub.add_parser("supported", parents=[common], help="列出支持的码")
    q.set_defaults(func=cmd_dtc_supported)

    q = dtc_sub.add_parser("clear", parents=[common], help="清码（需扩展会话）")
    q.add_argument("--group", type=lambda s: int(s, 0), default=0xFFFFFF)
    q.set_defaults(func=cmd_dtc_clear)

    q = dtc_sub.add_parser("snapshot", parents=[common], help="读快照")
    q.add_argument("--record", type=lambda s: int(s, 0), default=1)
    q.set_defaults(func=cmd_dtc_snapshot)

    p = sub.add_parser("flash", parents=[common], help="刷写固件（hex / s19 / bin）")
    p.add_argument("file")
    p.add_argument("--base", type=lambda s: int(s, 0), default=0x08000000,
                   help="裸 bin 的基地址")
    p.add_argument("--retries", type=int, default=2, help="单块重试次数")
    p.add_argument("--reset", action="store_true", help="刷完复位")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p.set_defaults(func=cmd_flash)

    p = sub.add_parser("raw", parents=[common], help="发一帧原始 UDS 请求")
    p.add_argument("bytes", nargs="+")
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("monitor", parents=[common], help="原始帧监视，可按 DBC 解析")
    p.add_argument("--dbc", help="DBC 文件，给了就按信号显示")
    p.add_argument("--count", type=int, default=0, help="收到这么多帧后退出，0 表示一直跑")
    p.add_argument("--id", dest="ids", default="", help="只看这些 ID，如 0x180,0x181")
    p.add_argument("--mqtt", action="store_true", help="改读网关的 MQTT 上行")
    p.add_argument("--broker", default="127.0.0.1:1883", help="broker 地址 host:port")
    p.add_argument("--topic", default="gw/can", help="上行主题")
    p.set_defaults(func=cmd_monitor)

    return parser


def main(argv=None):
    # 依赖目录要尽早挂上：--mqtt 分支不会走 open_client，
    # 那里面的 ensure_dependencies 就轮不到执行，cantools 会 import 失败
    from canstudio.link import ensure_dependencies

    ensure_dependencies()

    # 输出重定向到文件/管道时 Python 默认整块缓冲，进程被 Ctrl-C 或 timeout
    # 杀掉，缓冲区里的内容就全丢了 —— 长时间跑监视最容易踩到
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    parser = build_parser()
    args = parser.parse_args(argv)
    args.no_unlock = getattr(args, "no_unlock", False)

    try:
        return args.func(args)
    except NegativeResponse as exc:
        print(f"否定响应：{exc}", file=sys.stderr)
        return 2
    except TransportTimeout as exc:
        print(f"超时：{exc}", file=sys.stderr)
        return 3
    except CanStudioError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
