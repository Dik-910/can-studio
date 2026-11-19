"""上行解析用例。

网关的 JSON 是手写 snprintf 拼的，字段顺序与大小写都不保证；解析要对
「格式不对」宽容（返回 None），对「内容不对」严格（d 不是合法十六进制就丢）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from canstudio.uplink import (
    DEFAULT_BROKER,
    UplinkFrame,
    parse_broker,
    parse_message,
)

SAMPLE = ('{"gw":"can-gateway","ch":"vcan1","id":"0x181","n":25,"dlc":8,'
          '"d":"0000000000000000","ts":123456789}')


class Parsing(unittest.TestCase):
    def test_full_message(self):
        frame = parse_message(SAMPLE)

        self.assertIsInstance(frame, UplinkFrame)
        self.assertEqual(frame.channel, "vcan1")
        self.assertEqual(frame.frame_id, 0x181)
        self.assertEqual(frame.data, bytes(8))
        self.assertEqual(frame.count, 25)
        self.assertEqual(frame.ts_ms, 123456789)

    def test_lowercase_hex(self):
        frame = parse_message('{"id":"0x200","d":"f8139cff"}')
        self.assertEqual(frame.data, bytes.fromhex("f8139cff"))

    def test_integer_id(self):
        frame = parse_message('{"id":384,"d":"01"}')
        self.assertEqual(frame.frame_id, 0x180)

    def test_count_defaults_to_one(self):
        frame = parse_message('{"id":"0x180","d":"01"}')
        self.assertEqual(frame.count, 1)
        self.assertIsNone(frame.ts_ms)

    def test_negative_count_is_ignored(self):
        frame = parse_message('{"id":"0x180","d":"01","n":-3}')
        self.assertEqual(frame.count, 1)

    def test_dlc_pads_short_data(self):
        frame = parse_message('{"id":"0x180","d":"01","dlc":4}')
        self.assertEqual(frame.data, b"\x01\x00\x00\x00")

    def test_dlc_truncates_long_data(self):
        frame = parse_message('{"id":"0x180","d":"0102030405","dlc":2}')
        self.assertEqual(frame.data, b"\x01\x02")

    def test_rejects_odd_hex(self):
        self.assertIsNone(parse_message('{"id":"0x180","d":"012"}'))

    def test_rejects_non_hex(self):
        self.assertIsNone(parse_message('{"id":"0x180","d":"zz"}'))

    def test_rejects_missing_fields(self):
        self.assertIsNone(parse_message('{"id":"0x180"}'))
        self.assertIsNone(parse_message('{"d":"00"}'))

    def test_rejects_broken_json(self):
        self.assertIsNone(parse_message("{not json"))
        self.assertIsNone(parse_message(""))
        self.assertIsNone(parse_message(b"\x00\x01"))

    def test_rejects_json_that_is_not_object(self):
        self.assertIsNone(parse_message("[1,2,3]"))

    def test_rejects_bad_id(self):
        self.assertIsNone(parse_message('{"id":"0xZZ","d":"00"}'))

    def test_extra_fields_are_ignored(self):
        frame = parse_message('{"id":"0x180","d":"01","gw":"x","extra":[1,2]}')
        self.assertEqual(frame.frame_id, 0x180)


class Broker(unittest.TestCase):
    def test_host_and_port(self):
        self.assertEqual(parse_broker("192.168.1.5:1884"), ("192.168.1.5", 1884))

    def test_with_scheme(self):
        self.assertEqual(parse_broker("tcp://10.0.0.2:1883"), ("10.0.0.2", 1883))
        self.assertEqual(parse_broker("mqtt://broker:2000"), ("broker", 2000))

    def test_host_only_uses_default_port(self):
        self.assertEqual(parse_broker("localhost"), ("localhost", 1883))

    def test_empty_uses_default(self):
        self.assertEqual(parse_broker(""), DEFAULT_BROKER)
        self.assertEqual(parse_broker(None), DEFAULT_BROKER)

    def test_bad_port_falls_back(self):
        self.assertEqual(parse_broker("host:abc"), DEFAULT_BROKER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
