"""网关数据落库：把 MQTT 上行的聚合报文存进 SQLite。

为什么需要这一层：网关上报的是「变化量」——同一条报文数值不变就不报，
所以实时看板看到的是「最近一次变化」，看不到「过去一小时电压怎么走的」。
落库之后才有历史、有统计、有趋势。

表结构刻意保持扁平：一条上行消息一行。网关的 n（窗口内聚合帧数）单独存一列，
因为它是「这段时间总线上出现过多少帧」的唯一证据 —— 求平均周期、算丢帧率
都靠它。

SQLite 而不是时序库：单机、几万到几百万行、要能随手 `sqlite3 xxx.db` 查，
这三点都是 SQLite 的强项；引 InfluxDB 之类要先起服务，与「轻量闭环」相悖。
"""
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,   -- 收到消息的本地时刻（epoch 秒）
    ch      TEXT,               -- 网关上的通道名
    can_id  INTEGER NOT NULL,
    n       INTEGER NOT NULL DEFAULT 1,   -- 窗口内聚合帧数
    dlc     INTEGER NOT NULL,
    data    BLOB    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frames_ts     ON frames(ts);
CREATE INDEX IF NOT EXISTS idx_frames_can_id ON frames(can_id, ts);
"""


class HistoryStore:
    """一个 SQLite 文件。批量写入用事务，避免每条消息一次 fsync。"""

    def __init__(self, path, wal=True):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)

        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        if wal:
            # WAL 让「一边写一边查」不互相阻塞，界面读历史时采集还在写
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------- 写入 ----------------

    def add(self, frame, ts=None, commit=True):
        """存一条上行报文。frame 需要有 frame_id / data / count / channel。"""
        self._conn.execute(
            "INSERT INTO frames (ts, ch, can_id, n, dlc, data) VALUES (?,?,?,?,?,?)",
            (float(ts if ts is not None else time.time()),
             getattr(frame, "channel", "") or "",
             int(frame.frame_id),
             int(getattr(frame, "count", 1) or 1),
             len(frame.data),
             sqlite3.Binary(bytes(frame.data))),
        )
        if commit:
            self._conn.commit()

    def add_many(self, items):
        """items 是 [(frame, ts), ...]，一个事务写完。"""
        self._conn.executemany(
            "INSERT INTO frames (ts, ch, can_id, n, dlc, data) VALUES (?,?,?,?,?,?)",
            [(float(ts if ts is not None else time.time()),
              getattr(frame, "channel", "") or "",
              int(frame.frame_id),
              int(getattr(frame, "count", 1) or 1),
              len(frame.data),
              sqlite3.Binary(bytes(frame.data)))
             for frame, ts in items],
        )
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    # ---------------- 查询 ----------------

    def count(self, since=None):
        sql = "SELECT COUNT(*) FROM frames"
        args = ()
        if since is not None:
            sql += " WHERE ts >= ?"
            args = (float(since),)
        return int(self._conn.execute(sql, args).fetchone()[0])

    def total_frames(self, since=None):
        """总线上实际出现过的帧数（按 n 累加），与消息条数区分开。"""
        sql = "SELECT COALESCE(SUM(n), 0) FROM frames"
        args = ()
        if since is not None:
            sql += " WHERE ts >= ?"
            args = (float(since),)
        return int(self._conn.execute(sql, args).fetchone()[0])

    def summary(self, since=None):
        """按 ID 汇总：消息数、总帧数、最近时刻、最近数据。"""
        sql = ("SELECT can_id, COUNT(*) AS messages, COALESCE(SUM(n), 0) AS frames, "
               "MAX(ts) AS last_ts FROM frames")
        args = ()
        if since is not None:
            sql += " WHERE ts >= ?"
            args = (float(since),)
        sql += " GROUP BY can_id ORDER BY can_id"

        rows = []
        for row in self._conn.execute(sql, args):
            latest = self._conn.execute(
                "SELECT data, n FROM frames WHERE can_id = ? ORDER BY ts DESC LIMIT 1",
                (row["can_id"],),
            ).fetchone()
            rows.append({
                "can_id": row["can_id"],
                "messages": row["messages"],
                "frames": row["frames"],
                "last_ts": row["last_ts"],
                "last_data": bytes(latest["data"]) if latest else b"",
                "last_n": latest["n"] if latest else 1,
            })
        return rows

    def series(self, can_id, since=None, limit=None):
        """某个 ID 的历史数据，按时间升序：[(ts, data)]"""
        sql = "SELECT ts, data FROM frames WHERE can_id = ?"
        args = [int(can_id)]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(float(since))
        sql += " ORDER BY ts ASC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))

        return [(row["ts"], bytes(row["data"])) for row in self._conn.execute(sql, args)]

    def span(self):
        """库里的时间范围与消息数。"""
        row = self._conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM frames").fetchone()
        return {
            "messages": row[0],
            "first_ts": row[1],
            "last_ts": row[2],
        }

    # ---------------- 维护 ----------------

    def prune(self, before_ts):
        """删掉早于给定时刻的数据。长时间采集必须能裁剪，否则库无限长。"""
        cursor = self._conn.execute("DELETE FROM frames WHERE ts < ?", (float(before_ts),))
        self._conn.commit()
        return cursor.rowcount

    def close(self):
        try:
            self._conn.commit()
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Recorder:
    """把上行订阅接到库里。

    攒够 batch 条或超过 flush_s 秒提交一次：MQTT 上行一秒可能几十条，
    每条一次 commit 会把磁盘 IO 打满，而这里丢几秒数据无所谓。
    """

    def __init__(self, store, batch=50, flush_s=2.0):
        self.store = store
        self.batch = batch
        self.flush_s = flush_s
        self._pending = []
        self._last_flush = time.time()
        self.stored = 0

    def on_frame(self, frame):
        self._pending.append((frame, time.time()))
        if len(self._pending) >= self.batch:
            self.flush()

    def tick(self):
        """采集循环里定期调用，处理「条数不够但时间到了」的情况。"""
        if self._pending and time.time() - self._last_flush >= self.flush_s:
            self.flush()

    def flush(self):
        if not self._pending:
            self._last_flush = time.time()
            return 0

        items, self._pending = self._pending, []
        self.store.add_many(items)
        self.stored += len(items)
        self._last_flush = time.time()
        return len(items)
