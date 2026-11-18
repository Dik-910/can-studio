"""刷写流程：把 UDS 的几个传输服务编排成一次完整的升级。

协议层（uds.py）只负责单帧请求，谁来管块大小、序号回绕、重试、进度、
校验？就是这里。真实的上位机刷写工具，差别也主要在这一层：

  1. 解析固件文件（Intel HEX / S-record / 裸 bin），拿到地址与数据
  2. 进编程会话、解锁
  3. 0x34 通告下载范围，拿到 ECU 容许的块长
  4. 0x36 逐块发送，块序号 1..255 回绕，遇 NRC 0x73（序号错）重发本块
  5. 0x37 结束传输
  6. 0x31 让 ECU 自己算 CRC，与本地算出的值比对
  7. 可选 0x11 复位

CRC 交叉校验这一步不能省：刷写过程中任何一块传错，ECU 算出来的 CRC 都对不上，
这比读回全部数据比对更省带宽，也是产线工具的标准做法。
"""
import zlib

from .errors import CanStudioError, NegativeResponse
from .uds import ROUTINE_START, SESSION_EXTENDED

# can-node 的 bootloader：0x0202 = 按地址与长度校验 CRC32
ROUTINE_CHECK_MEMORY = 0x0202

NRC_WRONG_BLOCK_SEQUENCE = 0x73
NRC_RESPONSE_PENDING = 0x78


class FirmwareImage:
    """一段待写入的固件：连续地址 + 数据。

    多段固件（HEX 里常见）取最小地址到最大地址的并集，空洞用 0xFF 填 ——
    这与 Flash 擦除后的状态一致，不会把空洞当成有内容。
    """

    def __init__(self, segments):
        segments = [(int(addr), bytes(data)) for addr, data in segments if data]
        if not segments:
            raise CanStudioError("firmware image is empty")
        self.segments = sorted(segments)

    @property
    def start(self):
        return self.segments[0][0]

    @property
    def end(self):
        return self.segments[-1][0] + len(self.segments[-1][1])

    @property
    def size(self):
        return self.end - self.start

    @property
    def crc32(self):
        return zlib.crc32(self.as_bytes()) & 0xFFFFFFFF

    def as_bytes(self):
        """摊平成连续缓冲，空洞补 0xFF。"""
        buffer = bytearray(b"\xFF" * self.size)
        for addr, data in self.segments:
            offset = addr - self.start
            buffer[offset : offset + len(data)] = data
        return bytes(buffer)

    def __repr__(self):
        return (f"FirmwareImage(0x{self.start:08X}..0x{self.end:08X}, "
                f"{self.size} bytes, crc32=0x{self.crc32:08X})")


def load_firmware(path, base_address=0x08000000):
    """读固件文件。

    .hex / .s19 / .srec / .mot 用 bincopy 解析（Intel HEX 与 S-record 的
    格式细节不必自己维护）；.bin 需要显式给基地址。
    """
    import os
    import sys

    for candidate in (os.environ.get("CANSTUDIO_PYLIB"), "/home/dikl/work/.pylibs"):
        if candidate and os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)

    import bincopy

    text_like = path.lower().endswith((".hex", ".ihex", ".s19", ".srec", ".mot", ".asc"))
    fw = bincopy.BinFile()

    if text_like:
        # add_file 要的是路径，不是已打开的文件对象
        fw.add_file(path)
    else:
        with open(path, "rb") as f:
            fw.add_binary(f.read(), address=base_address)

    segments = [(seg.address, bytes(seg.data)) for seg in fw.segments]
    return FirmwareImage(segments)


class FlashResult:
    __slots__ = ("blocks", "bytes_written", "crc_local", "crc_ecu", "elapsed_s")

    def __init__(self, blocks, bytes_written, crc_local, crc_ecu, elapsed_s):
        self.blocks = blocks
        self.bytes_written = bytes_written
        self.crc_local = crc_local
        self.crc_ecu = crc_ecu
        self.elapsed_s = elapsed_s

    def __repr__(self):
        return (f"FlashResult({self.bytes_written} bytes, {self.blocks} blocks, "
                f"crc 0x{self.crc_local:08X} == 0x{self.crc_ecu:08X})")


class FlashJob:
    """一次刷写任务。

    参数：
        client          UdsClient
        block_retries   单块失败（超时 / 0x73）时的重发次数
        verify_routine  校验用的例程号，用不到可传 None 跳过校验
    """

    def __init__(self, client, block_retries=2, verify_routine=ROUTINE_CHECK_MEMORY):
        self.client = client
        self.block_retries = block_retries
        self.verify_routine = verify_routine
        self.last_result = None

    def _enter_flash_mode(self):
        self.client.change_session(SESSION_EXTENDED)
        self.client.unlock(1)

    def _send_block(self, sequence, chunk):
        attempts = self.block_retries + 1
        last_error = None

        for _ in range(attempts):
            try:
                self.client.transfer_data(sequence, chunk)
                return
            except NegativeResponse as exc:
                if exc.nrc != NRC_WRONG_BLOCK_SEQUENCE:
                    raise
                # 序号错是唯一值得原地重发的情况：ECU 只是没收到上一块
                last_error = exc
            except CanStudioError as exc:
                # 超时之类可以重试；重试完仍失败就抛，交给上层
                last_error = exc

        raise CanStudioError(
            f"block {sequence:02X} failed after {attempts} attempts: {last_error}"
        )

    def run(self, image, progress=None, cancel=None):
        """执行刷写。

        progress(sent_bytes, total_bytes) 每块回调一次；
        cancel() 返回 True 时在块边界停下，抛 Cancelled。
        回调里抛异常同样会中止 —— 界面线程用它处理用户取消。
        """
        import time

        started = time.monotonic()
        payload = image.as_bytes()
        total = len(payload)
        crc_local = image.crc32

        self._enter_flash_mode()

        max_block = self.client.request_download(image.start, total)
        # 这个长度包含 SID 与块序号两字节，能装的数据要减 2
        chunk_size = max(1, max_block - 2)

        sent = 0
        sequence = 1
        blocks = 0

        while sent < total:
            if cancel is not None and cancel():
                raise Cancelled(f"cancelled after {sent} bytes")

            chunk = payload[sent : sent + chunk_size]
            self._send_block(sequence, chunk)

            sent += len(chunk)
            blocks += 1
            sequence = (sequence + 1) & 0xFF

            if progress is not None:
                progress(sent, total)

        self.client.request_transfer_exit()

        crc_ecu = None
        if self.verify_routine is not None:
            crc_ecu = self._verify(image)

        self.last_result = FlashResult(blocks, sent, crc_local, crc_ecu,
                                       time.monotonic() - started)
        return self.last_result

    def _verify(self, image):
        option = image.start.to_bytes(4, "big") + image.size.to_bytes(4, "big")

        self.client.routine_control(self.verify_routine, ROUTINE_START, option)
        result = self.client.routine_control(self.verify_routine, 0x03)

        # 结果格式由厂商定义，can-node 回 4 字节 CRC32（大端）
        if len(result) >= 4:
            crc_ecu = int.from_bytes(result[:4], "big")
        elif len(result) == 1:
            # 只有一字节时是例程状态：0x00 通过、其他失败，拿不到具体 CRC
            if result[0] != 0x00:
                raise CanStudioError(f"ECU reported verification failure: 0x{result[0]:02X}")
            return None
        else:
            raise CanStudioError(f"unexpected verification result: {result.hex()}")

        if crc_ecu != image.crc32:
            raise CanStudioError(
                f"CRC mismatch: ECU 0x{crc_ecu:08X}, local 0x{image.crc32:08X}"
            )
        return crc_ecu


class Cancelled(CanStudioError):
    """用户取消。与协议错误分开，界面要区分「中止」和「失败」。"""
