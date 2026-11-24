# -*- coding: utf-8 -*-
"""实测响应时间写回 respondTime 单测（TDD：先红后绿）。

背景：页面「待复测」是前端兜底标签（docs/index.html:261），触发条件是
respondTime 缺失 / 为 0 / >60s。但此前 05_export / 06_pool 导出全量透传
上游原始 respondTime，探测实测的 respond_time_ms 从没写回，导致页面数字
陈旧、「待复测」名不副实。

覆盖：
- lib.apply_measured_respond_time 的有效/无效值分支
- 06_pool._refresh_respond_times：全池刷新、TIMEOUT/BLOCKED 不覆盖
- commit 集成：**fill 模式**（池未满，不走 _recheck）也必须刷新——这是初版
  把逻辑放进 _recheck 导致 29 个源一个没刷新的真实回归点

运行：python -m unittest tests.test_respond_time -v
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import lib  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "pool_mod", os.path.join(ROOT, "scripts", "06_pool.py"))
pool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pool)


def make_row(sid, ms=1234, **kw):
    """构造一条合格 matrix 行（默认各门禁都过）。"""
    r = {"source_id": sid, "search_ok": True, "status": "PASSED",
         "risk_level": "clean", "respond_time_ms": ms, "domain": "a.com",
         "bookSourceName": "src-" + sid, "content_hash": "h",
         "hit_longtail": 6, "hit_waist": 9, "hit_classic": 9, "hit_hot": 9,
         "steps": {}, "attempts": {"classic": 10}}
    r.update(kw)
    return r


class TestApplyMeasured(unittest.TestCase):
    def test_writes_int(self):
        src = {"respondTime": 999999}
        self.assertTrue(lib.apply_measured_respond_time(src, 1234))
        self.assertEqual(src["respondTime"], 1234)

    def test_missing_key_gets_set(self):
        src = {"bookSourceName": "x"}
        self.assertTrue(lib.apply_measured_respond_time(src, 800))
        self.assertEqual(src["respondTime"], 800)

    def test_none_keeps_original(self):
        src = {"respondTime": 999999}
        self.assertFalse(lib.apply_measured_respond_time(src, None))
        self.assertEqual(src["respondTime"], 999999)

    def test_zero_keeps_original(self):
        # 0 表示"没测到"，不能覆盖——否则页面会显示成 0s 最快，比待复测更误导
        src = {"respondTime": 999999}
        self.assertFalse(lib.apply_measured_respond_time(src, 0))
        self.assertEqual(src["respondTime"], 999999)

    def test_negative_keeps_original(self):
        src = {"respondTime": 999999}
        self.assertFalse(lib.apply_measured_respond_time(src, -5))
        self.assertEqual(src["respondTime"], 999999)

    def test_garbage_keeps_original(self):
        src = {"respondTime": 999999}
        self.assertFalse(lib.apply_measured_respond_time(src, "timeout"))
        self.assertEqual(src["respondTime"], 999999)

    def test_numeric_string_coerced(self):
        src = {}
        self.assertTrue(lib.apply_measured_respond_time(src, "2345"))
        self.assertEqual(src["respondTime"], 2345)


class TestRefreshPool(unittest.TestCase):
    """commit 里的全池刷新：fill 模式不进 _recheck，必须单独兜住。"""

    def st(self, *items):
        return {"round": 1, "retired": {}, "pool": list(items)}

    def item(self, sid, respond):
        return {"source_id": sid, "source": {"respondTime": respond}}

    def test_refresh_all_pool_members(self):
        st = self.st(self.item("s1", 999999), self.item("s2", 888888))
        rows = [make_row("s1", 1500), make_row("s2", 2345)]
        n = pool._refresh_respond_times(st, rows)
        self.assertEqual(n, 2)
        self.assertEqual(st["pool"][0]["source"]["respondTime"], 1500)
        self.assertEqual(st["pool"][1]["source"]["respondTime"], 2345)

    def test_missing_matrix_row_keeps_original(self):
        st = self.st(self.item("s1", 777))
        self.assertEqual(pool._refresh_respond_times(st, []), 0)
        self.assertEqual(st["pool"][0]["source"]["respondTime"], 777)

    def test_timeout_positive_ms_keeps_original(self):
        # kernel 对 TIMEOUT 行给的不是 0，而是"已跑完那几次的平均耗时"（真实值如 4387）。
        # 必须按 status 拦住，否则超时源会显示成 4.4s「一般」。
        st = self.st(self.item("s1", 4321))
        rows = [make_row("s1", 4387, status="TIMEOUT", search_ok=False)]
        self.assertEqual(pool._refresh_respond_times(st, rows), 0)
        self.assertEqual(st["pool"][0]["source"]["respondTime"], 4321)

    def test_blocked_positive_ms_keeps_original(self):
        st = self.st(self.item("s1", 5000))
        rows = [make_row("s1", 714, status="BLOCKED", search_ok=False)]
        self.assertEqual(pool._refresh_respond_times(st, rows), 0)
        self.assertEqual(st["pool"][0]["source"]["respondTime"], 5000)

    def test_zero_keeps_original(self):
        st = self.st(self.item("s1", 4321))
        self.assertEqual(pool._refresh_respond_times(st, [make_row("s1", 0)]), 0)
        self.assertEqual(st["pool"][0]["source"]["respondTime"], 4321)

    def test_source_not_dict_skipped(self):
        st = self.st({"source_id": "s1", "source": None})
        self.assertEqual(pool._refresh_respond_times(st, [make_row("s1", 100)]), 0)


class TestCommitFillMode(unittest.TestCase):
    """回归锁：写回必须在 fill 模式也生效。

    初版把逻辑放进 _recheck，单测直调 _refresh 仍全绿，但 fill 模式
    （池未满不走 _recheck）真实跑出来 29 个源一个都没刷新。这条用例
    必须走真实 commit，否则同样挪回去也不会有测试变红。
    """

    def test_fill_mode_refreshes_pool_respond_time(self):
        with tempfile.TemporaryDirectory() as td:
            run_id = "rt-fill"
            with mock.patch.dict(os.environ, {"LEGADO_SY_RUNS_DIR": td}), \
                 mock.patch.object(pool, "STATE_PATH",
                                   os.path.join(td, "_state", "pool.json")):
                rd = os.path.join(td, run_id)
                os.makedirs(rd, exist_ok=True)
                os.makedirs(os.path.dirname(pool.STATE_PATH), exist_ok=True)
                pool.dump_json(pool.STATE_PATH, {
                    "schema": "pool-state-v1", "size": 50, "round": 1,
                    "cursor": 0, "retired": {},
                    "pool": [{"source_id": "s1", "domain": "a.com",
                              "bookSourceName": "src-s1", "content_hash": "h",
                              "source": {"bookSourceName": "src-s1",
                                         "bookSourceUrl": "https://a.com",
                                         "respondTime": 999999},
                              "added_round": 0, "fail_streak": 0,
                              "blocked_streak": 0, "history": []}],
                })
                # mode=fill：池 1/50 未满
                pool.dump_json(os.path.join(rd, "round_plan.json"),
                               {"run_id": run_id, "mode": "fill"})
                pool.dump_jsonl(os.path.join(rd, "matrix.jsonl"),
                                [make_row("s1", 1500)])
                pool.dump_jsonl(os.path.join(rd, "targets.jsonl"),
                                [{"source_id": "s1", "source": {}}])

                pool.commit(run_id, fail_limit=2, blocked_limit=3)

                st = pool.load_state()
                self.assertEqual(st["pool"][0]["source"]["respondTime"], 1500,
                                 "fill 模式必须把实测响应时间写回 respondTime")


if __name__ == "__main__":
    unittest.main()
