# -*- coding: utf-8 -*-
"""06_pool.py scanned_ids 游标防重单测（TDD：先红后绿）。

覆盖（用户 2026-11-26 确认的需求）：
- scanned_ids 记录已探 source_id，plan 跳过它们（含「重排不重复」）
- 候选数组顺序打乱后，第二轮不重复取已 scanned 的源（这是本次改动的核心）
- plan 后 state 里 scanned_ids 追加本轮 target 的 id
- 兼容旧 state（无 scanned_ids 字段）不报错
- cursor 字段仍保留写入（看板兼容），但不再用于选源判定

运行：python -m unittest tests.test_scanned_ids -v
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import lib  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "pool_scanned_mod", os.path.join(ROOT, "scripts", "06_pool.py"))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def cand(sid, name=""):
    """构造一条 clean 候选（plan 只收 risk_level=clean 且有 searchUrl）。"""
    return {
        "source_id": sid,
        "risk_level": "clean",
        "domain": sid + ".com",
        "bookSourceName": name or sid,
        "source": {
            "bookSourceName": name or sid,
            "bookSourceUrl": "https://" + sid + ".com",
            "searchUrl": "https://" + sid + ".com/search?q={{key}}",
        },
    }


class TestScannedIds(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.state_path = os.path.join(self.td, "_state", "pool.json")
        self.ctx = mock.patch.dict(os.environ, {"LEGADO_SY_RUNS_DIR": self.td}), \
                   mock.patch.object(mod, "STATE_PATH", self.state_path)
        for c in self.ctx:
            c.start()
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)

    def tearDown(self):
        for c in reversed(self.ctx):
            c.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def _write_candidates(self, run_id, rows):
        d = lib.run_dir(run_id)
        os.makedirs(d, exist_ok=True)
        mod.dump_jsonl(os.path.join(d, "candidates.jsonl"), rows)

    def _write_state(self, scanned=None, cursor=0, pool_list=None):
        st = {
            "schema": "pool-state-v1", "size": 50, "round": 1,
            "cursor": cursor, "retired": {}, "pool": pool_list or [],
        }
        if scanned is not None:
            st["scanned_ids"] = scanned
        mod.dump_json(self.state_path, st)

    def test_scanned_ids_skips_previously_picked(self):
        rows = [cand("s%02d" % i) for i in range(1, 61)]
        self._write_candidates("scan1", rows)
        self._write_state(scanned=["s%02d" % i for i in range(1, 51)])
        p = mod.plan("scan1", 50)
        targets = mod.load_jsonl(os.path.join(lib.run_dir("scan1"), "targets.jsonl"))
        ids = {t["source_id"] for t in targets}
        # 前 50 已 scanned，应跳过；只剩 s51..s60 共 10 条（池空时本轮目标=新候选）
        self.assertEqual(ids, {"s%02d" % i for i in range(51, 61)},
                         "scanned_ids 中的源必须被跳过")
        self.assertEqual(len(targets), 10)

    def test_reorder_does_not_repeat(self):
        # 第一轮：按 s01..s60 取前 50（scanned 记 s01..s50）
        rows1 = [cand("s%02d" % i) for i in range(1, 61)]
        self._write_candidates("round1", rows1)
        self._write_state(scanned=[])
        mod.plan("round1", 50)
        st1 = mod.load_state()
        self.assertEqual(len(st1["scanned_ids"]), 50)
        first_scanned = set(st1["scanned_ids"])

        # 第二轮：候选数组倒序 + 换新 run_id（模拟上游重排），scanned 带入
        rows2 = list(reversed(rows1))
        self._write_candidates("round2", rows2)
        mod.dump_json(self.state_path, st1)
        mod.plan("round2", 50)
        targets2 = mod.load_jsonl(os.path.join(lib.run_dir("round2"), "targets.jsonl"))
        # 有 searchUrl 的 clean 候选共 60 条，已 scanned 50 条 → 剩余 10 条
        ids2 = {t["source_id"] for t in targets2}
        self.assertFalse(ids2 & first_scanned,
                         "重排后也不得重复取已 scanned 的源")
        # 第一轮取走 s01..s50，未 scanned 的只剩 s51..s60（倒序不影响集合）
        self.assertEqual(ids2, {"s%02d" % i for i in range(51, 61)})

    def test_scanned_ids_persists_in_state(self):
        rows = [cand("s%02d" % i) for i in range(1, 11)]
        self._write_candidates("persist", rows)
        self._write_state(scanned=[])
        mod.plan("persist", 50)
        st = mod.load_state()
        self.assertEqual(set(st["scanned_ids"]), {"s%02d" % i for i in range(1, 11)},
                         "本轮 picked 的 id 必须写回 state.scanned_ids")
        self.assertIn("cursor", st, "cursor 字段保留供看板显示")

    def test_probe_batch_stays_50_when_pool_size_is_66(self):
        """回归锁：池目标提到 66 后，每轮探测数必须仍是 50，不能跟着涨。

        历史上 BATCH=size，池 50→66 会让每轮探 66 个，撞穿 1200s 预算
        （84 个就曾被 SIGKILL）。此用例锁死探测数与池容量解耦。
        """
        rows = [cand("s%03d" % i) for i in range(1, 201)]
        self._write_candidates("batch66", rows)
        self._write_state(scanned=[])
        p = mod.plan("batch66", 66)   # 池目标 66
        targets = mod.load_jsonl(os.path.join(lib.run_dir("batch66"), "targets.jsonl"))
        new_ids = [t["source_id"] for t in targets if t.get("is_new")]
        self.assertEqual(len(new_ids), 50,
                         "池目标 66 时每轮新探测仍须 50 个（预算约束）")
        self.assertEqual(len(mod.load_state()["scanned_ids"]), 50)

    def test_legacy_state_without_scanned_ids(self):
        rows = [cand("s%02d" % i) for i in range(1, 6)]
        self._write_candidates("legacy", rows)
        self._write_state(scanned=None)  # 旧 state 无该字段
        st = mod.load_state()
        self.assertEqual(st.get("scanned_ids"), [])
        p = mod.plan("legacy", 50)  # 不抛异常即可
        self.assertEqual(p["mode"], "fill")


if __name__ == "__main__":
    unittest.main(verbosity=2)
