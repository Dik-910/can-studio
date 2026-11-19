"""DID 表载入与解码用例。

重点是「配置驱动」这条边界：表能换、解码方式能配、坏配置要报错而不是
静默变成空表——空表会让界面看起来一切正常但没有一行数据。
"""
import json
import os
import tempfile
import unittest

import test_signals  # noqa: F401  统一挂 sys.path 与依赖目录

from canstudio.dids import (
    DEFAULT_TABLE,
    DidSpec,
    DidTable,
    build_decoder,
)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_CONFIG = os.path.join(HERE, "config", "dids.json")


def write_config(payload):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
    if isinstance(payload, str):
        handle.write(payload)
    else:
        json.dump(payload, handle, ensure_ascii=False)
    handle.close()
    return handle.name


class ShippedConfig(unittest.TestCase):
    """仓库里那份配置必须和内置默认表对得上，否则两者会悄悄分叉。"""

    def test_config_file_exists(self):
        self.assertTrue(os.path.exists(SHIPPED_CONFIG))

    def test_matches_builtin_table(self):
        table = DidTable.load(SHIPPED_CONFIG)

        self.assertEqual(len(table), len(DEFAULT_TABLE))
        for spec in DEFAULT_TABLE:
            loaded = table.specs.get(spec.did)
            self.assertIsNotNone(loaded, f"配置里缺 0x{spec.did:04X}")
            self.assertEqual(loaded.name, spec.name)
            self.assertEqual(loaded.length, spec.length)
            self.assertEqual(loaded.writable, spec.writable)
            self.assertEqual(loaded.session, spec.session)

    def test_decodes_like_builtin(self):
        table = DidTable.load(SHIPPED_CONFIG)
        for did, raw in ((0x0101, b"\x0E\x4C"), (0x0302, b"\x00\x01\x02"),
                         (0x0301, b"\xDE\xAD")):
            self.assertEqual(table.describe(did, raw), DEFAULT_TABLE.describe(did, raw))


class Loading(unittest.TestCase):
    def test_minimal_entry(self):
        path = write_config({"dids": [{"id": "0x1234"}]})
        try:
            table = DidTable.load(path)
        finally:
            os.unlink(path)

        spec = table.specs[0x1234]
        self.assertEqual(spec.name, "0x1234")   # 没给名字就用 ID
        self.assertFalse(spec.writable)

    def test_id_formats(self):
        path = write_config({"dids": [{"id": "0xF190"}, {"id": "F191"},
                                      {"id": 0xF192}]})
        try:
            table = DidTable.load(path)
        finally:
            os.unlink(path)

        self.assertEqual(sorted(table.specs), [0xF190, 0xF191, 0xF192])

    def test_session_names(self):
        path = write_config({"dids": [
            {"id": "0x1001", "session": "default"},
            {"id": "0x1002", "session": "extended"},
            {"id": "0x1003", "session": 3},
        ]})
        try:
            table = DidTable.load(path)
        finally:
            os.unlink(path)

        self.assertEqual(table.specs[0x1001].session, 0x01)
        self.assertEqual(table.specs[0x1002].session, 0x03)
        self.assertEqual(table.specs[0x1003].session, 0x03)

    def test_plain_list_is_accepted(self):
        path = write_config([{"id": "0x2001", "name": "A"}])
        try:
            table = DidTable.load(path)
        finally:
            os.unlink(path)

        self.assertEqual(table.specs[0x2001].name, "A")

    def test_missing_id_is_an_error(self):
        path = write_config({"dids": [{"name": "没有 id"}]})
        try:
            with self.assertRaises(ValueError):
                DidTable.load(path)
        finally:
            os.unlink(path)

    def test_broken_json_is_an_error(self):
        path = write_config("{ not json")
        try:
            with self.assertRaises(json.JSONDecodeError):
                DidTable.load(path)
        finally:
            os.unlink(path)


class Decoders(unittest.TestCase):
    def test_ascii_trims_padding(self):
        decode = build_decoder("ascii")
        self.assertEqual(decode(b"ABC\x00  ", None), "ABC")

    def test_hex_is_space_separated(self):
        decode = build_decoder("hex")
        self.assertEqual(decode(b"\xDE\xAD\xBE\xEF", None), "DE AD BE EF")

    def test_uint16_is_big_endian(self):
        # UDS 数值一律大端：0x0E4C = 3660
        decode = build_decoder("uint16", scale=0.001, unit="V")
        self.assertEqual(decode(b"\x0E\x4C", None), "3.66 V")

    def test_int16_is_signed(self):
        decode = build_decoder("int16")
        self.assertEqual(decode(b"\xFF\x9C", None), "-100")

    def test_uint32(self):
        decode = build_decoder("uint32")
        self.assertEqual(decode(b"\x00\x00\x01\x00", None), "256")

    def test_unknown_kind_falls_back_to_hex(self):
        # 配置写错不该让整张表失效，退回十六进制显示
        decode = build_decoder("no_such_kind")
        self.assertEqual(decode(b"\x01\x02", None), "01 02")

    def test_short_payload_shows_raw(self):
        decode = build_decoder("uint32")
        self.assertEqual(decode(b"\x01", None), "01")

    def test_builtin_millivolt_keeps_detail(self):
        name, value = DEFAULT_TABLE.describe(0x0101, b"\x0E\x4C")
        self.assertEqual(name, "电池电压")
        self.assertIn("3660 mV", value)
        self.assertIn("3.66 V", value)


class Lookup(unittest.TestCase):
    def test_find_by_id_and_name(self):
        self.assertEqual(DEFAULT_TABLE.find(0xF190).did, 0xF190)
        self.assertEqual(DEFAULT_TABLE.find("0xF190").did, 0xF190)
        self.assertEqual(DEFAULT_TABLE.find("VIN").did, 0xF190)

    def test_unknown_did_describes_as_hex(self):
        name, value = DEFAULT_TABLE.describe(0x9999, b"\xDE\xAD")
        self.assertEqual(name, "0x9999")
        self.assertEqual(value, "DE AD")

    def test_lengths_table_skips_variable_length(self):
        lengths = DEFAULT_TABLE.did_lengths()
        self.assertEqual(lengths[0x0101], 2)
        self.assertNotIn(0x0301, lengths)     # 长度不定，不给快照定界用

    def test_empty_table(self):
        table = DidTable([])
        self.assertEqual(len(table), 0)
        self.assertIsNone(table.find(0x1234))
        self.assertEqual(table.describe(0x1234, b"\x01"), ("0x1234", "01"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
