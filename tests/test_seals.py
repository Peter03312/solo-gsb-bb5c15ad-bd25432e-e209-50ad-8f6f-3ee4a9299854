"""端到端测试：时钟周期封存（seal，v1.2 新增）。

现场核对完成后封存单台设备的一个时钟周期，防止晚到记录改动已确认时间线：
- GET 周期详情返回由锚点+声段内容生成的稳定指纹与封存状态；
- 封存请求携带该指纹，在同一写事务内核对当前内容；
- 该周期全部声段均可定位时封存才生效，返回封存时间与最终指纹；
- 指纹过期（与当前内容不一致）或存在不可定位声段时拒绝，状态不变；
- 重复相同封存请求返回首次结果（幂等 200），封存不可撤销；
- 封存后拒绝该周期锚点新增/删除、锚点更正与声段新增/删除；
- 同内容声段重传仍幂等、异内容仍冲突（均优先于封存拦截）；
- 一次锚点更正涉及封存周期则整体拒绝，其他周期不受影响；
- 旧 v1.1 库在线迁移后周期默认未封存，已发布版本行为不变。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402
from app.logic import cycle_fingerprint  # noqa: E402

# v1.1（有周期、无封存列）建表脚本，仅用于构造待迁移的旧库
SCHEMA_V11 = """
CREATE TABLE devices (device_id TEXT PRIMARY KEY, name TEXT, created_at TEXT NOT NULL);
CREATE TABLE cycles (
    device_id TEXT NOT NULL, cycle_id TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (device_id, cycle_id));
CREATE TABLE anchors (
    device_id TEXT NOT NULL, cycle_id TEXT NOT NULL DEFAULT 'default',
    local_ms INTEGER NOT NULL, utc_ms INTEGER NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (device_id, cycle_id, local_ms));
CREATE TABLE segments (
    capture_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL DEFAULT 'default',
    local_start_ms INTEGER NOT NULL, local_end_ms INTEGER NOT NULL, note TEXT,
    content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE versions (
    version_no INTEGER PRIMARY KEY, note TEXT, created_at TEXT NOT NULL,
    format_rev INTEGER NOT NULL DEFAULT 1);
CREATE TABLE version_anchors (
    version_no INTEGER NOT NULL, device_id TEXT NOT NULL, cycle_id TEXT,
    local_ms INTEGER NOT NULL, utc_ms INTEGER NOT NULL,
    PRIMARY KEY (version_no, device_id, cycle_id, local_ms));
CREATE TABLE version_segments (
    version_no INTEGER NOT NULL, capture_id TEXT NOT NULL, device_id TEXT NOT NULL,
    cycle_id TEXT, local_start_ms INTEGER NOT NULL, local_end_ms INTEGER NOT NULL,
    note TEXT, status TEXT NOT NULL, utc_start_ms INTEGER, utc_end_ms INTEGER,
    created_at TEXT, PRIMARY KEY (version_no, capture_id));
"""


def request(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class SealApiTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        db.init_db(self.db_path)
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    # ------------------------------------------------------------ 夹具

    def make_device(self, device_id="D1"):
        return self.req("POST", "/devices", {"device_id": device_id})

    def reg_cycle(self, device_id, cycle_id, note=None):
        payload = {"cycle_id": cycle_id}
        if note is not None:
            payload["note"] = note
        return self.req("POST", f"/devices/{device_id}/cycles", payload)

    def add_anchor(self, device_id, local_ms, utc_ms, cycle_id="default"):
        return self.req("POST", f"/devices/{device_id}/anchors",
                        {"cycle_id": cycle_id, "local_ms": local_ms, "utc_ms": utc_ms})

    def submit(self, capture_id, device_id, start, end, cycle_id="default", note=None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "cycle_id": cycle_id, "local_start_ms": start, "local_end_ms": end}
        if note is not None:
            payload["note"] = note
        return self.req("POST", "/segments", payload)

    def cycle_detail(self, device_id="D1", cycle_id="default"):
        return self.req("GET", f"/devices/{device_id}/cycles/{cycle_id}")

    def fingerprint(self, device_id="D1", cycle_id="default"):
        return self.cycle_detail(device_id, cycle_id)[1]["cycle"]["fingerprint"]

    def seal(self, fp, device_id="D1", cycle_id="default"):
        return self.req("POST", f"/devices/{device_id}/cycles/{cycle_id}/seal",
                        {"fingerprint": fp})

    def seed_located_cycle(self, device_id="D1", cycle_id="default",
                           capture="C1"):
        """一设备 + 两锚点 + 一个可定位声段（全可定位、可封存）。"""
        self.make_device(device_id)
        if cycle_id != "default":
            self.reg_cycle(device_id, cycle_id)
        self.add_anchor(device_id, 1000, 100000, cycle_id)
        self.add_anchor(device_id, 2000, 101000, cycle_id)
        self.submit(capture, device_id, 1100, 1200, cycle_id)

    # ------------------------------------------------------------ 指纹与详情

    def test_cycle_detail_has_fingerprint_and_sealed_state(self):
        self.seed_located_cycle()
        status, body = self.cycle_detail()
        self.assertEqual(status, 200)
        c = body["cycle"]
        self.assertFalse(c["sealed"])
        self.assertIsNone(c["sealed_at"])
        self.assertRegex(c["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(c["anchor_count"], 2)
        self.assertEqual(c["segment_count"], 1)
        self.assertEqual(c["unlocatable_count"], 0)
        self.assertEqual(c["unlocatable_captures"], [])
        # 列表接口同样带封存状态与指纹
        _, lst = self.req("GET", "/devices/D1/cycles")
        entry = lst["cycles"][0]
        self.assertIn("sealed", entry)
        self.assertIn("sealed_at", entry)
        self.assertEqual(entry["fingerprint"], c["fingerprint"])

    def test_fingerprint_matches_pure_logic_and_is_stable(self):
        self.seed_located_cycle()
        fp_api = self.fingerprint()
        expected = cycle_fingerprint(
            "D1", "default",
            [{"local_ms": 1000, "utc_ms": 100000},
             {"local_ms": 2000, "utc_ms": 101000}],
            [{"capture_id": "C1", "local_start_ms": 1100, "local_end_ms": 1200,
              "note": None}])
        self.assertEqual(fp_api, expected)
        # 无写入时指纹稳定
        self.assertEqual(self.fingerprint(), expected)

    def test_fingerprint_changes_with_content(self):
        self.seed_located_cycle()
        before = self.fingerprint()
        # 新增声段 -> 指纹变化
        self.submit("C2", "D1", 1200, 1300)
        self.assertNotEqual(self.fingerprint(), before)
        self.req("DELETE", "/segments/C2")
        # 删除后内容回到最初 -> 指纹恢复
        self.assertEqual(self.fingerprint(), before)
        # 锚点 utc 更正 -> 指纹变化（声段内容不变）
        self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 100001},
            {"local_ms": 2000, "utc_ms": 101000}]})
        fp_after_anchor = self.fingerprint()
        self.assertNotEqual(fp_after_anchor, before)
        # 再放回原始锚点内容 -> 指纹再次恢复（指纹只锚定内容，不含投影/时间戳）
        self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 100000},
            {"local_ms": 2000, "utc_ms": 101000}]})
        self.assertEqual(self.fingerprint(), before)

    def test_cycle_detail_not_found(self):
        self.make_device()
        status, body = self.cycle_detail("D1", "ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")

    # ------------------------------------------------------------ 封存成功 / 幂等

    def test_seal_success_returns_time_and_final_fingerprint(self):
        self.seed_located_cycle()
        fp = self.fingerprint()
        status, body = self.seal(fp)
        self.assertEqual(status, 201)
        self.assertTrue(body["sealed"])
        self.assertFalse(body["idempotent_replay"])
        self.assertEqual(body["fingerprint"], fp)
        self.assertIn("sealed_at", body)
        self.assertIsInstance(body["sealed_at"], str)
        # 详情：已封存，指纹为落库的封存指纹
        _, detail = self.cycle_detail()
        c = detail["cycle"]
        self.assertTrue(c["sealed"])
        self.assertEqual(c["sealed_at"], body["sealed_at"])
        self.assertEqual(c["fingerprint"], fp)

    def test_seal_idempotent_same_request_returns_first_result(self):
        self.seed_located_cycle()
        fp = self.fingerprint()
        _, first = self.seal(fp)
        status, again = self.seal(fp)
        self.assertEqual(status, 200)
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(again["sealed_at"], first["sealed_at"])
        self.assertEqual(again["fingerprint"], first["fingerprint"])

    # ------------------------------------------------------------ 拒绝且状态不变

    def test_seal_stale_fingerprint_rejected_state_unchanged(self):
        self.seed_located_cycle()
        stale = self.fingerprint()
        # 晚到记录改动了时间线
        self.add_anchor("D1", 1500, 100500)
        self.assertNotEqual(self.fingerprint(), stale)
        status, body = self.seal(stale)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "fingerprint_mismatch")
        # 详情带回当前指纹，便于调用方重新核对
        self.assertEqual(body["error"]["details"]["current_fingerprint"],
                         self.fingerprint())
        self.assertEqual(body["error"]["details"]["received_fingerprint"], stale)
        # 状态不变
        self.assertFalse(self.cycle_detail()[1]["cycle"]["sealed"])

    def test_seal_unlocatable_segment_rejected_state_unchanged(self):
        self.make_device()
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        # 起点落在锚点覆盖范围之外 -> 无法定位
        self.submit("C1", "D1", 9000, 9100)
        _, detail = self.cycle_detail()
        self.assertEqual(detail["cycle"]["unlocatable_count"], 1)
        fp = detail["cycle"]["fingerprint"]
        status, body = self.seal(fp)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_not_sealable")
        self.assertEqual(body["error"]["details"]["unlocatable_captures"], ["C1"])
        self.assertFalse(self.cycle_detail()[1]["cycle"]["sealed"])
        # 补齐锚点使声段可定位后可正常封存
        self.add_anchor("D1", 9000, 108000)
        self.add_anchor("D1", 9200, 108200)
        self.assertEqual(self.cycle_detail()[1]["cycle"]["unlocatable_count"], 0)
        status, body = self.seal(self.fingerprint())
        self.assertEqual(status, 201)
        self.assertTrue(body["sealed"])

    def test_seal_unknown_cycle_and_device_rejected(self):
        self.make_device()
        status, body = self.seal("0" * 64, "D1", "ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        status, body = self.req("POST", "/devices/NOPE/cycles/default/seal",
                                {"fingerprint": "0" * 64})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

    def test_seal_invalid_fingerprint(self):
        self.make_device()
        for bad in (None, "", "abc", "0" * 63, "g" * 64, 123):
            status, body = self.req("POST", "/devices/D1/cycles/default/seal",
                                    {"fingerprint": bad})
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"]["code"], "validation_error")

    def test_sealed_replay_with_different_fingerprint_rejected(self):
        self.seed_located_cycle()
        fp = self.fingerprint()
        self.seal(fp)
        status, body = self.seal("0" * 64)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "fingerprint_mismatch")
        self.assertEqual(body["error"]["details"]["sealed_fingerprint"], fp)

    # ------------------------------------------------------------ 封存后写拦截

    def test_sealed_blocks_anchor_insert_delete(self):
        self.seed_located_cycle()
        self.seal(self.fingerprint())
        status, body = self.add_anchor("D1", 1500, 100500)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        status, body = self.req("DELETE", "/devices/D1/anchors/1000")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 内容确实未动
        _, lst = self.req("GET", "/devices/D1/anchors")
        self.assertEqual([a["local_ms"] for a in lst["anchors"]], [1000, 2000])

    def test_sealed_blocks_segment_insert_delete(self):
        self.seed_located_cycle()
        self.seal(self.fingerprint())
        # 新采集号 -> 拒绝新增
        status, body = self.submit("C2", "D1", 1200, 1300)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 删除 -> 拒绝
        status, body = self.req("DELETE", "/segments/C1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        _, lst = self.req("GET", "/segments")
        self.assertEqual([s["capture_id"] for s in lst["segments"]], ["C1"])

    def test_sealed_replay_same_content_still_idempotent(self):
        self.seed_located_cycle()
        self.seal(self.fingerprint())
        # 同内容（含省略 cycle_id=default）重传 -> 仍按幂等规则返回
        status, body = self.req("POST", "/segments",
                                {"capture_id": "C1", "device_id": "D1",
                                 "local_start_ms": 1100, "local_end_ms": 1200})
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        # 异内容 -> 仍按冲突规则返回
        status, body = self.req("POST", "/segments",
                                {"capture_id": "C1", "device_id": "D1",
                                 "local_start_ms": 1100, "local_end_ms": 1201})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")

    def test_put_anchors_touching_sealed_cycle_rejected_entirely(self):
        self.seed_located_cycle()
        self.reg_cycle("D1", "boot-2")
        self.seal(self.fingerprint())
        # 一次更正同时涉及 default（封存）与 boot-2（未封存）-> 整体拒绝
        status, body = self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"cycle_id": "default", "local_ms": 1000, "utc_ms": 100001},
            {"cycle_id": "default", "local_ms": 2000, "utc_ms": 101001},
            {"cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 200000},
        ]})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # default 锚点未变；boot-2 也未被写入（整体拒绝）
        _, lst = self.req("GET", "/devices/D1/anchors")
        self.assertEqual([(a["cycle_id"], a["local_ms"], a["utc_ms"]) for a in lst["anchors"]],
                         [("default", 1000, 100000), ("default", 2000, 101000)])

    def test_other_cycles_unaffected_by_seal(self):
        # default 封存，boot-2 行为完全不变
        self.seed_located_cycle(capture="C1")
        self.reg_cycle("D1", "boot-2")
        self.seal(self.fingerprint())
        self.assertEqual(self.add_anchor("D1", 1000, 200000, "boot-2")[0], 201)
        self.assertEqual(self.add_anchor("D1", 2000, 202000, "boot-2")[0], 201)
        self.assertEqual(self.submit("B2", "D1", 1100, 1200, "boot-2")[0], 201)
        # boot-2 可被更正、可删除锚点
        status, _ = self.req("PUT", "/devices/D1/anchors", {"cycle_id": "boot-2", "anchors": [
            {"local_ms": 1000, "utc_ms": 200500},
            {"local_ms": 2000, "utc_ms": 202500}]})
        self.assertEqual(status, 200)
        # boot-2 也可独立封存（全部声段可定位）
        status, body = self.seal(self.fingerprint("D1", "boot-2"),
                                 "D1", "boot-2")
        self.assertEqual(status, 201)
        self.assertTrue(body["sealed"])
        # 封存 boot-2 后再只更正 default 已无意义（default 也封了），
        # 但仅涉及未封存周期的更正不受已封存周期影响：新建设备验证
        self.make_device("D2")
        self.add_anchor("D2", 1000, 300000)
        self.add_anchor("D2", 2000, 301000)
        status, _ = self.req("PUT", "/devices/D2/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 300001},
            {"local_ms": 2000, "utc_ms": 301001}]})
        self.assertEqual(status, 200)

    def test_seal_other_device_same_cycle_id_independent(self):
        # 封存以 (设备, 周期) 为粒度：同 cycle_id 跨设备互不影响
        self.seed_located_cycle("D1", capture="C1")
        self.make_device("D2")
        self.add_anchor("D2", 1000, 500000)
        self.add_anchor("D2", 2000, 501000)
        self.submit("C9", "D2", 1100, 1200)
        self.assertEqual(self.seal(self.fingerprint("D1"))[0], 201)
        # D2/default 仍可写
        status, _ = self.add_anchor("D2", 1500, 500500)
        self.assertEqual(status, 201)
        self.assertFalse(self.cycle_detail("D2")[1]["cycle"]["sealed"])

    def test_empty_cycle_can_be_sealed_then_blocks_writes(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        # 无锚点无声段：无不可定位声段，可封存；封存后不可写入
        fp = self.fingerprint("D1", "boot-2")
        status, body = self.seal(fp, "D1", "boot-2")
        self.assertEqual(status, 201)
        self.assertTrue(body["sealed"])
        status, body = self.add_anchor("D1", 1000, 100000, "boot-2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # default 周期不受影响
        self.assertEqual(self.add_anchor("D1", 1000, 100000, "default")[0], 201)

    # ------------------------------------------------------------ 与发布共存

    def test_seal_does_not_change_publishing_and_versions(self):
        self.seed_located_cycle()
        self.seal(self.fingerprint())
        # 封存后仍可发布当前时间轴快照
        status, v1 = self.req("POST", "/versions", {"note": "封存后发布"})
        self.assertEqual(status, 201)
        self.assertEqual(v1["format_rev"], 2)
        self.assertEqual(len(v1["segments"]), 1)
        # 已发布版本查询行为不变
        _, got = self.req("GET", "/versions/1")
        self.assertEqual(got["segments"], v1["segments"])
        _, tl = self.req("GET", "/timeline/current")
        self.assertEqual(tl["segment_count"], 1)


# ------------------------------------------------------------ 旧 v1.1 库迁移

class SealMigrationTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        self._seed_v11()
        db.init_db(self.db_path)  # 在线迁移（补封存列）
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    def _seed_v11(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA_V11)
            conn.execute("INSERT INTO devices VALUES ('D1','录音笔','t')")
            conn.execute("INSERT INTO cycles VALUES ('D1','default',NULL,'t')")
            conn.execute("INSERT INTO anchors VALUES ('D1','default',1000,100000,'t')")
            conn.execute("INSERT INTO anchors VALUES ('D1','default',2000,101000,'t')")
            conn.execute(
                "INSERT INTO segments VALUES ('C1','D1','default',1100,1200,NULL,"
                "'x','t')")
            conn.commit()
        finally:
            conn.close()

    def test_v11_cycles_default_unsealed_after_migration(self):
        status, body = self.req("GET", "/devices/D1/cycles/default")
        self.assertEqual(status, 200)
        c = body["cycle"]
        self.assertFalse(c["sealed"])
        self.assertIsNone(c["sealed_at"])
        self.assertRegex(c["fingerprint"], r"^[0-9a-f]{64}$")
        # 数据保留且可写（未封存原有行为）
        self.assertEqual(c["anchor_count"], 2)
        status, _ = self.req("POST", "/devices/D1/anchors",
                             {"local_ms": 1500, "utc_ms": 100500})
        self.assertEqual(status, 201)
        # 核对无误后可正常封存
        fp = self.req("GET", "/devices/D1/cycles/default")[1]["cycle"]["fingerprint"]
        status, sealed = self.req("POST", "/devices/D1/cycles/default/seal",
                                  {"fingerprint": fp})
        self.assertEqual(status, 201)
        self.assertTrue(sealed["sealed"])

    def test_v11_seal_columns_present_after_restart(self):
        # 迁移幂等：再初始化一次不报错，封存列保留，状态不变
        db.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(cycles)")}
        finally:
            conn.close()
        self.assertEqual(cols, {"device_id", "cycle_id", "note", "created_at",
                                "sealed", "sealed_at", "fingerprint"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
