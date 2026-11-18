"""协议层用例：请求编码是否标准、响应解码是否正确、异常是否分得清。"""
import unittest

from fake_ecu import FakeLink, ScriptedEcu, negative, positive

from canstudio.errors import BadResponse, NegativeResponse, TransportTimeout
from canstudio.uds import (
    ROUTINE_START,
    SESSION_EXTENDED,
    UdsClient,
    default_key_algo,
)


class SessionAndReset(unittest.TestCase):
    def test_change_session_encodes_10_03(self):
        link = FakeLink([b"\x50\x03\x00\x32\x13\x88"])
        client = UdsClient(link)

        self.assertEqual(client.change_session(SESSION_EXTENDED), SESSION_EXTENDED)
        self.assertEqual(link.sent, [b"\x10\x03"])

    def test_session_echo_mismatch_is_rejected(self):
        link = FakeLink([b"\x50\x01\x00\x32\x13\x88"])
        with self.assertRaises(BadResponse):
            UdsClient(link).change_session(SESSION_EXTENDED)

    def test_negative_response_carries_nrc(self):
        link = FakeLink([negative(b"\x10\x03", 0x22)])
        with self.assertRaises(NegativeResponse) as ctx:
            UdsClient(link).change_session(SESSION_EXTENDED)

        self.assertEqual(ctx.exception.nrc, 0x22)
        self.assertEqual(ctx.exception.service, 0x10)

    def test_response_pending_then_positive(self):
        # 0x78 表示「在处理」，必须按 P2* 继续等，不能当成失败
        link = FakeLink([b"\x7F\x10\x78", b"\x50\x03\x00\x32\x13\x88"])
        client = UdsClient(link, p2_star_ms=100)

        self.assertEqual(client.change_session(SESSION_EXTENDED), SESSION_EXTENDED)
        self.assertEqual(len(link.sent), 1, "0x78 must not trigger a resend")

    def test_timeout_is_reported_separately(self):
        link = FakeLink([])
        with self.assertRaises(TransportTimeout) as ctx:
            UdsClient(link, p2_ms=5).change_session(SESSION_EXTENDED)
        self.assertIn("0x10", str(ctx.exception))

    def test_unexpected_sid_is_bad_response(self):
        link = FakeLink([b"\x62\xF1\x90"])
        with self.assertRaises(BadResponse):
            UdsClient(link).change_session(SESSION_EXTENDED)

    def test_ecu_reset(self):
        link = FakeLink([b"\x51\x01"])
        self.assertEqual(UdsClient(link).ecu_reset(0x01), 0x01)
        self.assertEqual(link.sent, [b"\x11\x01"])

    def test_tester_present_suppressed_sends_without_waiting(self):
        link = FakeLink([])
        client = UdsClient(link)
        self.assertIsNone(client.tester_present())
        self.assertEqual(link.sent, [b"\x3E\x80"])


class SecurityAccess(unittest.TestCase):
    def test_seed_key_round_trip(self):
        seed = 0x11223344
        link = FakeLink([bytes([0x67, 0x01]) + seed.to_bytes(4, "big"),
                         b"\x67\x02"])
        client = UdsClient(link)

        got_seed, key = client.unlock(1)

        self.assertEqual(got_seed, seed)
        self.assertEqual(key, default_key_algo(1, seed))
        self.assertEqual(link.sent[0], b"\x27\x01")
        self.assertEqual(link.sent[1], b"\x27\x02" + key.to_bytes(4, "big"))

    def test_invalid_key_raises_nrc_35(self):
        seed = 0x0A0B0C0D
        link = FakeLink([bytes([0x67, 0x01]) + seed.to_bytes(4, "big"),
                         b"\x7F\x27\x35"])
        with self.assertRaises(NegativeResponse) as ctx:
            UdsClient(link).unlock(1)
        self.assertEqual(ctx.exception.nrc, 0x35)

    def test_locked_out_raises_nrc_36(self):
        seed = 1
        link = FakeLink([bytes([0x67, 0x01]) + seed.to_bytes(4, "big"),
                         b"\x7F\x27\x36"])
        with self.assertRaises(NegativeResponse) as ctx:
            UdsClient(link).unlock(1)
        self.assertEqual(ctx.exception.nrc, 0x36)

    def test_custom_key_algorithm(self):
        seed = 0x00000001
        link = FakeLink([bytes([0x67, 0x01]) + seed.to_bytes(4, "big"), b"\x67\x02"])
        client = UdsClient(link, key_algo=lambda level, s, p: 0xDEADBEEF)

        client.unlock(1)
        self.assertEqual(link.sent[1], b"\x27\x02\xDE\xAD\xBE\xEF")

    def test_session_change_invalidates_unlock(self):
        link = FakeLink([bytes([0x67, 0x01]) + (7).to_bytes(4, "big"), b"\x67\x02",
                         b"\x50\x01\x00\x32\x13\x88"])
        client = UdsClient(link)
        client.unlock(1)
        self.assertEqual(client.unlocked_level, 1)

        client.change_session(0x01)
        self.assertIsNone(client.unlocked_level)


class DataByIdentifier(unittest.TestCase):
    def test_read_did_returns_payload_only(self):
        link = FakeLink([b"\x62\xF1\x90LSVAA1234567890123"])
        data = UdsClient(link).read_did(0xF190)
        self.assertEqual(data, b"LSVAA1234567890123")
        self.assertEqual(link.sent, [b"\x22\xF1\x90"])

    def test_read_did_echo_mismatch(self):
        link = FakeLink([b"\x62\x01\x01\x0E\x4C"])
        with self.assertRaises(BadResponse):
            UdsClient(link).read_did(0xF190)

    def test_write_did(self):
        link = FakeLink([b"\x6E\x02\x01"])
        UdsClient(link).write_did(0x0201, b"\x3C")
        self.assertEqual(link.sent, [b"\x2E\x02\x01\x3C"])

    def test_write_did_echo_mismatch(self):
        link = FakeLink([b"\x6E\x09\x09"])
        with self.assertRaises(BadResponse):
            UdsClient(link).write_did(0x0201, b"\x3C")


class RoutineControl(unittest.TestCase):
    def test_start_routine_encodes_id_and_data(self):
        link = FakeLink([b"\x71\x01\x02\x02\x01"])
        payload = UdsClient(link).routine_control(0x0202, ROUTINE_START, b"\x01\x02")

        self.assertEqual(link.sent, [b"\x31\x01\x02\x02\x01\x02"])
        self.assertEqual(payload, b"\x01")

    def test_routine_echo_mismatch(self):
        link = FakeLink([b"\x71\x01\xFF\xFF"])
        with self.assertRaises(BadResponse):
            UdsClient(link).routine_control(0x0202)


class Transfer(unittest.TestCase):
    def test_download_parses_block_length(self):
        # ALFID = 0x20：长度字段 2 字节，值 0x0200 = 512
        link = FakeLink([b"\x74\x20\x02\x00"])
        max_block = UdsClient(link).request_download(0x08000000, 4096)

        self.assertEqual(max_block, 512)
        self.assertEqual(link.sent, [
            b"\x34\x00\x44\x08\x00\x00\x00\x00\x00\x10\x00"
        ])

    def test_download_rejects_zero_length_field(self):
        link = FakeLink([b"\x74\x00"])
        with self.assertRaises(BadResponse):
            UdsClient(link).request_download(0x08000000, 16)

    def test_transfer_data_echoes_sequence(self):
        link = FakeLink([b"\x76\x01"])
        self.assertEqual(UdsClient(link).transfer_data(1, b"\xAA\xBB"), 1)
        self.assertEqual(link.sent, [b"\x36\x01\xAA\xBB"])

    def test_transfer_data_sequence_wraps_at_ff(self):
        link = FakeLink([b"\x76\x00"])
        UdsClient(link).transfer_data(0x100, b"\x01")
        self.assertEqual(link.sent, [b"\x36\x00\x01"])

    def test_transfer_data_wrong_echo(self):
        link = FakeLink([b"\x76\x05"])
        with self.assertRaises(BadResponse):
            UdsClient(link).transfer_data(1, b"\x01")

    def test_transfer_exit(self):
        link = FakeLink([b"\x77"])
        UdsClient(link).request_transfer_exit()
        self.assertEqual(link.sent, [b"\x37"])


class AgainstScriptedEcu(unittest.TestCase):
    """用「会应答的假 ECU」跑一遍完整交互，验证请求与响应能对上。"""

    def test_seed_key_against_pattern_matching_ecu(self):
        def handler(request):
            if request[0] == 0x10:
                return b"\x50" + bytes([request[1]]) + b"\x00\x32\x13\x88"
            if request[0] == 0x27 and request[1] == 0x01:
                return b"\x67\x01\x00\x00\x00\x2A"
            if request[0] == 0x27 and request[1] == 0x02:
                key = int.from_bytes(request[2:6], "big")
                if key == default_key_algo(1, 0x2A):
                    return b"\x67\x02"
                return negative(request, 0x35)
            return negative(request, 0x11)

        client = UdsClient(ScriptedEcu(handler))
        seed, key = client.unlock(1)

        self.assertEqual(seed, 0x2A)
        self.assertEqual(key, default_key_algo(1, 0x2A))
        self.assertEqual(client.unlocked_level, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
