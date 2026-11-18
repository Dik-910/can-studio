"""故障码与 DID 的解析用例。数据全部取自 can-node 的真实响应分布。"""
import unittest

from fake_ecu import FakeLink

from canstudio import dtc, dids
from canstudio.errors import BadResponse

# can-node 的 ecu-server 上报的两个码
DTC_BUS_OFF = 0xC10087
DTC_ERROR_PASSIVE = 0xC1008F


class DtcFormat(unittest.TestCase):
    def test_format_and_parse_round_trip(self):
        for code in (DTC_BUS_OFF, DTC_ERROR_PASSIVE, 0x000001, 0xFFFFFF):
            self.assertEqual(dtc.parse_dtc(dtc.format_dtc(code)), code)

    def test_letter_selects_the_system(self):
        self.assertTrue(dtc.format_dtc(0x000001).startswith("P"))
        self.assertTrue(dtc.format_dtc(0x400001).startswith("C"))
        self.assertTrue(dtc.format_dtc(0x800001).startswith("B"))
        self.assertTrue(dtc.format_dtc(0xC00001).startswith("U"))

    def test_known_text(self):
        self.assertEqual(dtc.format_dtc(DTC_BUS_OFF), "U010087")
        self.assertEqual(dtc.parse_dtc("U10087"), DTC_BUS_OFF)
        self.assertEqual(dtc.parse_dtc("U010087"), DTC_BUS_OFF)

    def test_rejects_garbage(self):
        for text in ("", "X1234", "P12", "PZZZZ"):
            with self.assertRaises(ValueError):
                dtc.parse_dtc(text)


class StatusBits(unittest.TestCase):
    def test_decode_returns_only_set_bits(self):
        # 0x26 = testFailedThisCycle | pending | failedSinceClear
        names = [n for n, _ in dtc.decode_status(0x26)]
        self.assertEqual(names, ["testFailedThisCycle", "pending", "failedSinceClear"])

    def test_zero_has_no_bits(self):
        self.assertEqual(dtc.decode_status(0x00), [])
        self.assertEqual(dtc.describe_status(0x00), "-")

    def test_all_bits_described(self):
        names = [n for n, _ in dtc.decode_status(0xFF)]
        self.assertEqual(len(names), 8)


class ReportByMask(unittest.TestCase):
    def test_parses_availability_and_entries(self):
        payload = bytes([0xFF]) + bytes([0xC1, 0x00, 0x87, 0x26]) + \
            bytes([0xC1, 0x00, 0x8F, 0x04])

        entries = dtc.parse_report_by_mask(payload)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].code, DTC_BUS_OFF)
        self.assertEqual(entries[0].status, 0x26)
        self.assertEqual(entries[0].text, "U010087")
        self.assertEqual(entries[1].code, DTC_ERROR_PASSIVE)

    def test_empty_list(self):
        self.assertEqual(dtc.parse_report_by_mask(b"\xFF"), [])

    def test_short_payload_rejected(self):
        with self.assertRaises(BadResponse):
            dtc.parse_report_by_mask(b"")


class ReportNumber(unittest.TestCase):
    def test_parses_count(self):
        info = dtc.parse_report_number(bytes([0xFF, 0x01, 0x00, 0x02]))
        self.assertEqual(info["availability"], 0xFF)
        self.assertEqual(info["format"], 0x01)
        self.assertEqual(info["count"], 2)

    def test_short_payload_rejected(self):
        with self.assertRaises(BadResponse):
            dtc.parse_report_number(b"\xFF\x01")


class SnapshotIdentification(unittest.TestCase):
    def test_parses_pairs(self):
        payload = bytes([0xC1, 0x00, 0x87, 0x01, 0xC1, 0x00, 0x8F, 0x02])
        pairs = dtc.parse_snapshot_identification(payload)

        self.assertEqual(pairs, [(DTC_BUS_OFF, 1), (DTC_ERROR_PASSIVE, 2)])

    def test_empty(self):
        self.assertEqual(dtc.parse_snapshot_identification(b""), [])


class SnapshotContent(unittest.TestCase):
    def test_single_identifier_with_known_length(self):
        # 一个 DID 0x0001，数据 5 字节
        blob = bytes([1, 0x00, 0x01, 0xAA, 0x08, 0x00, 0x00, 0xF8])
        parsed = dtc.parse_snapshot(blob, {0x0001: 5})

        self.assertEqual(parsed, [(0x0001, bytes([0xAA, 0x08, 0x00, 0x00, 0xF8]))])

    def test_last_identifier_takes_remaining_bytes(self):
        blob = bytes([1, 0x00, 0x01, 1, 2, 3])
        self.assertEqual(dtc.parse_snapshot(blob), [(0x0001, bytes([1, 2, 3]))])

    def test_unknown_length_in_the_middle_is_an_error(self):
        blob = bytes([2, 0x00, 0x01, 1, 2, 0x00, 0x02, 3])
        with self.assertRaises(BadResponse):
            dtc.parse_snapshot(blob, {})

    def test_two_identifiers_with_table(self):
        blob = bytes([2, 0x00, 0x01, 0xAA, 0x02, 0x01, 0x00, 0x3C])
        parsed = dtc.parse_snapshot(blob, {0x0001: 1, 0x0201: 1})

        self.assertEqual(parsed, [(0x0001, b"\xAA"), (0x0201, b"\x00")])

    def test_truncated_blob_rejected(self):
        with self.assertRaises(BadResponse):
            dtc.parse_snapshot(bytes([1, 0x00]))


class SnapshotByDtc(unittest.TestCase):
    def test_parses_full_record(self):
        payload = bytes([0xC1, 0x00, 0x87, 0x26, 0x01,
                         1, 0x00, 0x01, 0xAA, 0x08, 0x00, 0x00, 0xF8])
        entry = dtc.parse_snapshot_by_dtc(payload, {0x0001: 5})

        self.assertEqual(entry.code, DTC_BUS_OFF)
        self.assertEqual(entry.status, 0x26)
        self.assertEqual(entry.record, 1)
        self.assertEqual(entry.snapshot[0][0], 0x0001)
        self.assertEqual(len(entry.snapshot[0][1]), 5)


class StoredData(unittest.TestCase):
    def test_single_record(self):
        payload = bytes([0x01, 0xC1, 0x00, 0x87, 0x26,
                         1, 0x00, 0x01, 0xAA, 0x08, 0x00, 0x00, 0xF8])
        record, entries = dtc.parse_stored_data(payload, {0x0001: 5})

        self.assertEqual(record, 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].code, DTC_BUS_OFF)
        self.assertEqual(entries[0].status, 0x26)
        self.assertEqual(entries[0].snapshot[0][0], 0x0001)

    def test_two_records_are_split_correctly(self):
        one = bytes([0xC1, 0x00, 0x87, 0x26, 1, 0x00, 0x01, 0xAA, 0x08])
        two = bytes([0xC1, 0x00, 0x8F, 0x04, 1, 0x00, 0x01, 0xBB, 0x09])
        payload = bytes([0x01]) + one + two

        record, entries = dtc.parse_stored_data(payload, {0x0001: 2})

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].snapshot[0][1], b"\xAA\x08")
        self.assertEqual(entries[1].snapshot[0][1], b"\xBB\x09")

    def test_empty_payload_rejected(self):
        with self.assertRaises(BadResponse):
            dtc.parse_stored_data(b"")


class DidLookup(unittest.TestCase):
    def test_lookup_by_number_and_name(self):
        self.assertEqual(dids.find(0xF190).did, 0xF190)
        self.assertEqual(dids.find("0xF190").did, 0xF190)
        self.assertEqual(dids.find("F190").did, 0xF190)
        self.assertEqual(dids.find("VIN").did, 0xF190)

    def test_unknown_returns_none(self):
        self.assertIsNone(dids.find(0x1234))
        self.assertIsNone(dids.find("not a did"))

    def test_describe_known_did_decodes(self):
        name, value = dids.describe(0x0101, b"\x0E\x4C")
        self.assertEqual(name, "电池电压")
        self.assertIn("3660 mV", value)

    def test_describe_unknown_did_falls_back_to_hex(self):
        name, value = dids.describe(0x9999, b"\xDE\xAD")
        self.assertEqual(name, "0x9999")
        self.assertEqual(value, "DE AD")

    def test_describe_survives_bad_payload(self):
        name, value = dids.describe(0x0101, b"\x01")   # 电压需要 2 字节
        self.assertEqual(name, "电池电压")
        self.assertNotIn("decode failed", value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
