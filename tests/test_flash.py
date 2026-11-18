"""刷写流程用例：块切分、序号回绕、重试、取消、CRC 交叉校验。"""
import os
import tempfile
import unittest
import zlib

from fake_ecu import ScriptedEcu, negative

from canstudio.errors import CanStudioError, NegativeResponse
from canstudio.flash import (
    Cancelled,
    FirmwareImage,
    FlashJob,
    load_firmware,
)
from canstudio.uds import UdsClient

BASE = 0x08000000

# 本机的 bincopy 装在 .pylibs 下，import 之前先把目录挂进 sys.path
from canstudio.link import ensure_dependencies

ensure_dependencies()

try:
    import bincopy  # noqa: F401
    HAVE_BINCOPY = True
except ImportError:
    HAVE_BINCOPY = False


class FlashEcu:
    """能跑完整刷写状态机的假 ECU。"""

    def __init__(self, max_block=64, crc_override=None, fail_sequence_at=None,
                 no_crc_result=False):
        self.max_block = max_block
        self.crc_override = crc_override
        self.fail_sequence_at = fail_sequence_at
        self.fail_used = False
        self.no_crc_result = no_crc_result

        self.download = None
        self.received = bytearray()
        self.blocks = 0
        self.sequences = []
        self.exited = False
        self.verify_requested = None

    def __call__(self, req):
        sid = req[0]

        if sid == 0x10:
            return b"\x50" + req[1:2] + b"\x00\x32\x13\x88"
        if sid == 0x27:
            if req[1] == 0x01:
                return b"\x67\x01\x11\x22\x33\x44"
            return b"\x67\x02"
        if sid == 0x34:
            addr = int.from_bytes(req[3:7], "big")
            size = int.from_bytes(req[7:11], "big")
            self.download = (addr, size)
            self.received = bytearray()
            return bytes([0x74, 0x20,
                          (self.max_block >> 8) & 0xFF, self.max_block & 0xFF])
        if sid == 0x36:
            seq = req[1]
            if self.fail_sequence_at == seq and not self.fail_used:
                self.fail_used = True
                return negative(req, 0x73)
            self.sequences.append(seq)
            self.received += req[2:]
            self.blocks += 1
            return bytes([0x76, seq])
        if sid == 0x37:
            self.exited = True
            return b"\x77"
        if sid == 0x31:
            if req[1] == 0x01:
                self.verify_requested = req[4:12]
                return bytes([0x71, 0x01]) + req[2:4] + b"\x00"
            if self.no_crc_result:
                return bytes([0x71, 0x03]) + req[2:4] + b"\x00"
            crc = zlib.crc32(bytes(self.received)) & 0xFFFFFFFF
            if self.crc_override is not None:
                crc = self.crc_override
            return bytes([0x71, 0x03]) + req[2:4] + crc.to_bytes(4, "big")

        return negative(req, 0x11)


def make_client(ecu):
    return UdsClient(ScriptedEcu(ecu))


class ImageTests(unittest.TestCase):
    def test_single_segment(self):
        image = FirmwareImage([(BASE, b"\x01\x02\x03\x04")])
        self.assertEqual(image.size, 4)
        self.assertEqual(image.start, BASE)
        self.assertEqual(image.crc32, zlib.crc32(b"\x01\x02\x03\x04") & 0xFFFFFFFF)

    def test_gap_is_filled_with_erased_value(self):
        image = FirmwareImage([(BASE, b"\xAA"), (BASE + 3, b"\xBB")])
        self.assertEqual(image.as_bytes(), b"\xAA\xFF\xFF\xBB")
        self.assertEqual(image.size, 4)

    def test_empty_image_rejected(self):
        with self.assertRaises(CanStudioError):
            FirmwareImage([])

    @unittest.skipUnless(HAVE_BINCOPY, "bincopy not installed")
    def test_load_intel_hex(self):
        # 两段：0x08000000 起 4 字节，0x08000010 起 2 字节
        with tempfile.NamedTemporaryFile("w", suffix=".hex", delete=False) as f:
            f.write(":020000040800F2\n")          # 扩展线性地址 0x0800
            f.write(":0400000001020304F2\n")      # 4 字节数据
            f.write(":02001000AABB89\n")          # 偏移 0x10 的 2 字节
            f.write(":00000001FF\n")
            path = f.name

        try:
            image = load_firmware(path)
        finally:
            os.unlink(path)

        self.assertEqual(image.start, BASE)
        self.assertEqual(image.segments[0][1], b"\x01\x02\x03\x04")
        self.assertEqual(image.segments[1][0], BASE + 0x10)


class FlashFlow(unittest.TestCase):
    def test_full_flash_writes_every_byte(self):
        payload = bytes(range(256))
        ecu = FlashEcu(max_block=64)
        job = FlashJob(make_client(ecu))

        result = job.run(FirmwareImage([(BASE, payload)]))

        self.assertEqual(bytes(ecu.received), payload)
        self.assertEqual(result.bytes_written, 256)
        self.assertEqual(result.crc_local, result.crc_ecu)
        self.assertTrue(ecu.exited)
        self.assertEqual(ecu.download, (BASE, 256))

    def test_block_size_honours_two_byte_overhead(self):
        payload = bytes(200)
        ecu = FlashEcu(max_block=32)     # 通告 32，实际每块只能装 30
        job = FlashJob(make_client(ecu))

        result = job.run(FirmwareImage([(BASE, payload)]))

        self.assertEqual(result.blocks, 7)          # ceil(200 / 30)
        self.assertEqual(bytes(ecu.received), payload)

    def test_sequence_wraps_after_ff(self):
        payload = bytes(1024)
        ecu = FlashEcu(max_block=6)     # 每块 4 字节 -> 256 块，正好回绕
        job = FlashJob(make_client(ecu))

        result = job.run(FirmwareImage([(BASE, payload)]))

        self.assertEqual(result.blocks, 256)
        self.assertEqual(ecu.sequences[0], 1)
        self.assertEqual(ecu.sequences[253], 254)
        self.assertEqual(ecu.sequences[254], 255)
        self.assertEqual(ecu.sequences[255], 0)     # 0xFF 之后回绕到 0

    def test_progress_is_monotonic_and_complete(self):
        seen = []
        ecu = FlashEcu(max_block=64)
        job = FlashJob(make_client(ecu))

        job.run(FirmwareImage([(BASE, bytes(100))]), progress=lambda s, t: seen.append((s, t)))

        self.assertTrue(seen)
        self.assertEqual([s for s, _ in seen], sorted(s for s, _ in seen))
        self.assertEqual(seen[-1], (100, 100))

    def test_crc_mismatch_is_fatal(self):
        ecu = FlashEcu(max_block=64, crc_override=0xDEADBEEF)
        job = FlashJob(make_client(ecu))

        with self.assertRaises(CanStudioError) as ctx:
            job.run(FirmwareImage([(BASE, bytes(64))]))
        self.assertIn("CRC mismatch", str(ctx.exception))

    def test_missing_crc_is_not_treated_as_success(self):
        # 只回一字节状态 0x00 = 例程成功，但没给 CRC；这种情况放行但结果里为 None
        ecu = FlashEcu(max_block=64, no_crc_result=True)
        job = FlashJob(make_client(ecu))

        result = job.run(FirmwareImage([(BASE, bytes(64))]))
        self.assertIsNone(result.crc_ecu)

    def test_wrong_block_sequence_is_retried(self):
        ecu = FlashEcu(max_block=64, fail_sequence_at=2)
        job = FlashJob(make_client(ecu), block_retries=2)

        result = job.run(FirmwareImage([(BASE, bytes(100))]))

        self.assertEqual(result.bytes_written, 100)
        self.assertEqual(bytes(ecu.received), bytes(100))
        # 100 字节按 62 字节切块 -> 2 块；第 2 块被拒一次后重发，序号序列仍是 1,2
        self.assertEqual(ecu.sequences, [1, 2])

    def test_non_retryable_nrc_is_raised(self):
        class RejectingEcu(FlashEcu):
            def __call__(self, req):
                if req[0] == 0x36:
                    return negative(req, 0x31)
                return super().__call__(req)

        job = FlashJob(make_client(RejectingEcu()), block_retries=3)
        with self.assertRaises(NegativeResponse) as ctx:
            job.run(FirmwareImage([(BASE, bytes(64))]))
        self.assertEqual(ctx.exception.nrc, 0x31)

    def test_cancel_stops_at_block_boundary(self):
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 2

        ecu = FlashEcu(max_block=16)
        job = FlashJob(make_client(ecu))

        with self.assertRaises(Cancelled):
            job.run(FirmwareImage([(BASE, bytes(100))]), cancel=cancel)

        # 取消了就不该发 0x37 结束传输
        self.assertFalse(ecu.exited)

    def test_verify_can_be_skipped(self):
        ecu = FlashEcu(max_block=64)
        job = FlashJob(make_client(ecu), verify_routine=None)

        result = job.run(FirmwareImage([(BASE, bytes(32))]))

        self.assertIsNone(result.crc_ecu)
        self.assertIsNone(ecu.verify_requested)

    def test_verify_uses_address_and_size(self):
        ecu = FlashEcu(max_block=64)
        job = FlashJob(make_client(ecu))

        job.run(FirmwareImage([(BASE, bytes(48))]))

        self.assertEqual(ecu.verify_requested,
                         BASE.to_bytes(4, "big") + (48).to_bytes(4, "big"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
