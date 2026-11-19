"""落库与查询用例。用临时文件跑真实的 SQLite，不做 mock。"""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from canstudio.history import HistoryStore, Recorder
from canstudio.uplink import UplinkFrame


def frame(can_id=0x180, data=bytes(8), count=1, channel="vcan1"):
    return UplinkFrame(channel, can_id, data, count)


class Recording(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = handle.name
        self.store = HistoryStore(self.path)

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def test_schema_is_created(self):
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.summary(), [])

    def test_add_one(self):
        self.store.add(frame(0x180, b"\x01\x02", count=10))

        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.total_frames(), 10)

    def test_total_frames_uses_n_not_message_count(self):
        # 一条消息聚合了 101 帧，总帧数应当是 101 而不是 1
        self.store.add(frame(0x181, bytes(8), count=101))
        self.store.add(frame(0x181, bytes(8), count=100))

        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.total_frames(), 201)

    def test_add_many_is_one_transaction(self):
        items = [(frame(0x180, bytes([i])), 1000.0 + i) for i in range(20)]
        self.store.add_many(items)
        self.assertEqual(self.store.count(), 20)

    def test_summary_groups_by_id(self):
        self.store.add(frame(0x180, b"\x01", count=10), ts=1000.0)
        self.store.add(frame(0x180, b"\x02", count=10), ts=1001.0)
        self.store.add(frame(0x181, b"\x03", count=50), ts=1002.0)

        rows = {row["can_id"]: row for row in self.store.summary()}
        self.assertEqual(rows[0x180]["messages"], 2)
        self.assertEqual(rows[0x180]["frames"], 20)
        self.assertEqual(rows[0x181]["frames"], 50)

    def test_summary_keeps_latest_payload(self):
        self.store.add(frame(0x180, b"\xAA"), ts=1000.0)
        self.store.add(frame(0x180, b"\xBB"), ts=1005.0)
        self.store.add(frame(0x180, b"\xCC"), ts=1001.0)   # 更早，不该覆盖

        row = self.store.summary()[0]
        self.assertEqual(row["last_data"], b"\xBB")
        self.assertEqual(row["last_ts"], 1005.0)

    def test_since_filters(self):
        self.store.add(frame(0x180), ts=1000.0)
        self.store.add(frame(0x180), ts=2000.0)

        self.assertEqual(self.store.count(since=1500), 1)
        self.assertEqual(self.store.total_frames(since=1500), 1)

    def test_series_is_ordered(self):
        for ts, value in ((1002.0, 2), (1000.0, 0), (1001.0, 1)):
            self.store.add(frame(0x200, bytes([value])), ts=ts)

        series = self.store.series(0x200)
        self.assertEqual([ts for ts, _ in series], [1000.0, 1001.0, 1002.0])
        self.assertEqual([data for _, data in series], [b"\x00", b"\x01", b"\x02"])

    def test_series_limit(self):
        for i in range(10):
            self.store.add(frame(0x200, bytes([i])), ts=1000.0 + i)

        self.assertEqual(len(self.store.series(0x200, limit=3)), 3)

    def test_span(self):
        self.store.add(frame(0x180), ts=1000.0)
        self.store.add(frame(0x180), ts=1010.0)

        span = self.store.span()
        self.assertEqual(span["messages"], 2)
        self.assertEqual(span["first_ts"], 1000.0)
        self.assertEqual(span["last_ts"], 1010.0)

    def test_prune(self):
        self.store.add(frame(0x180), ts=1000.0)
        self.store.add(frame(0x180), ts=2000.0)

        removed = self.store.prune(1500)
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.count(), 1)

    def test_data_survives_reopen(self):
        self.store.add(frame(0x180, b"\xDE\xAD"), ts=1000.0)
        self.store.close()

        self.store = HistoryStore(self.path)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.series(0x180)[0][1], b"\xDE\xAD")

    def test_channel_is_kept(self):
        self.store.add(frame(0x180, channel="vcan1"), ts=1000.0)
        row = self.store._conn.execute("SELECT ch FROM frames").fetchone()
        self.assertEqual(row["ch"], "vcan1")


class RecorderTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = handle.name
        self.store = HistoryStore(self.path)

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def test_batches_until_threshold(self):
        recorder = Recorder(self.store, batch=5, flush_s=10)

        for _ in range(4):
            recorder.on_frame(frame())
        self.assertEqual(self.store.count(), 0, "没到批量阈值不该落盘")

        recorder.on_frame(frame())
        self.assertEqual(self.store.count(), 5)

    def test_flush_on_tick(self):
        recorder = Recorder(self.store, batch=100, flush_s=0.01)
        recorder.on_frame(frame())
        time.sleep(0.02)

        recorder.tick()
        self.assertEqual(self.store.count(), 1)

    def test_flush_is_idempotent(self):
        recorder = Recorder(self.store, batch=100)
        recorder.on_frame(frame())

        self.assertEqual(recorder.flush(), 1)
        self.assertEqual(recorder.flush(), 0)
        self.assertEqual(self.store.count(), 1)

    def test_explicit_flush(self):
        recorder = Recorder(self.store, batch=1000)
        recorder.on_frame(frame(count=7))
        recorder.flush()

        self.assertEqual(self.store.total_frames(), 7)
        self.assertEqual(recorder.stored, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
