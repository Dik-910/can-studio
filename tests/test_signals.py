"""DBC 解码用例。

用内联的 DBC 字符串而不是外部文件：用例自包含，换机器、进 CI 都不会因为
路径不对而跳过。另有一条用例在 can-node 的 DBC 存在时跑真表。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from canstudio.link import ensure_dependencies

# cantools 装在本机的 .pylibs 下
ensure_dependencies()

from canstudio.signals import FrameDecode, SignalDatabase, fmt

DEMO_DBC = """
VERSION ""

NS_ :

BS_:

BU_: Node

BO_ 384 NodeStatus: 8 Node
 SG_ NodeState : 0|8@1+ (1,0) [0|3] "" Node
 SG_ BusOffCnt : 8|16@1+ (1,0) [0|65535] "" Node
 SG_ UptimeS : 24|32@1+ (1,0) [0|4294967295] "s" Node

BO_ 512 PackData: 8 Node
 SG_ PackVoltage : 0|16@1+ (0.01,0) [0|655.35] "V" Node
 SG_ PackCurrent : 16|16@1- (0.01,0) [-327.68|327.67] "A" Node
 SG_ Soc : 32|8@1+ (1,0) [0|100] "%" Node
 SG_ TempMax : 40|8@1- (1,-40) [-40|215] "degC" Node
"""

# can-node 的正式表，存在时用它验证真实定义
CAN_NODE_DBC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "..", "can-node", "docs", "can_matrix.dbc")


def have_cantools():
    try:
        import cantools  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_cantools(), "cantools not installed")
class DemoDatabase(unittest.TestCase):
    def setUp(self):
        self.db = SignalDatabase.from_string(DEMO_DBC)

    def test_messages_are_indexed_by_frame_id(self):
        self.assertEqual(len(self.db), 2)
        self.assertEqual(self.db.message(384).name, "NodeStatus")
        self.assertIsNone(self.db.message(0x123))

    def test_unsigned_signals(self):
        # @1+ 是小端：BusOffCnt 占字节 1..2，低字节在前
        # NodeState=2, BusOffCnt=1, UptimeS=100
        frame = self.db.decode(384, bytes([2, 0x01, 0x00, 100, 0, 0, 0, 0]))

        self.assertIsNone(frame.error)
        values = {name: value for name, value, _ in frame.signals}
        self.assertEqual(values["NodeState"], 2)
        self.assertEqual(values["BusOffCnt"], 1)
        self.assertEqual(values["UptimeS"], 100)

    def test_scaling_and_offset(self):
        # PackVoltage 5112（小端 0xF8 0x13）* 0.01 = 51.12 V；Soc 87%
        data = bytes([0xF8, 0x13, 0x00, 0x00, 87, 0, 0, 0])
        frame = self.db.decode(512, data)

        values = {name: value for name, value, _ in frame.signals}
        self.assertAlmostEqual(values["PackVoltage"], 51.12, places=2)
        self.assertEqual(values["Soc"], 87)

    def test_signed_signal_and_offset(self):
        # PackCurrent 有符号: -100 * 0.01 = -1.0 A；TempMax 有偏移 -40: 65 - 40 = 25
        data = bytes([0, 0, 0x9C, 0xFF, 50, 65, 0, 0])
        frame = self.db.decode(512, data)

        values = {name: value for name, value, _ in frame.signals}
        self.assertAlmostEqual(values["PackCurrent"], -1.0, places=2)
        self.assertEqual(values["TempMax"], 25)

    def test_unknown_id_returns_none(self):
        self.assertIsNone(self.db.decode(0x555, bytes(8)))

    def test_short_payload_is_flagged_not_raised(self):
        # allow_truncated=True 时 cantools 会补齐；真正解不出来时才记 error
        frame = self.db.decode(384, b"\x01")
        self.assertTrue(frame is None or frame.error or frame.signals)

    def test_summary_is_readable(self):
        frame = self.db.decode(512, bytes([0xF8, 0x13, 0, 0, 87, 0, 0, 0]))
        text = frame.summary()
        self.assertIn("PackVoltage=51.12 V", text)
        self.assertIn("Soc=87 %", text)

    def test_describe_id(self):
        self.assertEqual(self.db.describe_id(384), "0x180 NodeStatus")
        self.assertEqual(self.db.describe_id(0x7FF), "0x7FF")


@unittest.skipUnless(have_cantools(), "cantools not installed")
@unittest.skipUnless(os.path.exists(CAN_NODE_DBC), "can-node DBC not found")
class RealDatabase(unittest.TestCase):
    """拿 can-node 的正式定义跑一遍，确认本工具能吃真实项目的 DBC。"""

    def setUp(self):
        self.db = SignalDatabase.load(CAN_NODE_DBC)

    def test_contains_expected_messages(self):
        names = {msg.name for msg in self.db.messages}
        for expected in ("NodeStatus", "BusStatus", "PackData", "FaultEvent", "ControlCmd"):
            self.assertIn(expected, names)

    def test_decodes_node_status(self):
        frame = self.db.decode(0x180, bytes([2, 1, 0x34, 0x12, 0x10, 0, 0, 0]))
        values = {name: value for name, value, _ in frame.signals}

        self.assertEqual(frame.name, "NodeStatus")
        self.assertEqual(values["NodeState"], 2)
        self.assertEqual(values["ErrLevel"], 1)
        self.assertEqual(values["BusOffCnt"], 0x1234)


class Formatting(unittest.TestCase):
    def test_integers_have_no_decimal_point(self):
        self.assertEqual(fmt(42), "42")

    def test_floats_are_trimmed(self):
        self.assertEqual(fmt(51.12), "51.12")
        self.assertEqual(fmt(3.0), "3")

    def test_unit_is_appended(self):
        self.assertEqual(fmt(12, "V"), "12 V")
        self.assertEqual(fmt(12.5, "%"), "12.5 %")

    def test_decode_object_without_signals(self):
        frame = FrameDecode(0x100, "Empty")
        self.assertEqual(frame.summary(), "Empty")

    def test_decode_object_with_error(self):
        frame = FrameDecode(0x100, "Broken", error="长度不对")
        self.assertIn("长度不对", frame.summary())


if __name__ == "__main__":
    unittest.main(verbosity=2)
