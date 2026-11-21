# -*- coding: utf-8 -*-
"""搜索污染率门禁单测（TDD：先红后绿）。

覆盖 scripts/06_pool.py：
- mismatch_rate 计算（clean / sou78 样本 / 零分母 / 前缀容忍）
- _row_ok 门禁（dirty 拒 / clean 留 / 阈值边界）
- should_retire_by_mismatch（存量剔除 / TIMEOUT 保留 / 未测保留 / blocked 仍剔除）

运行：python -m unittest tests.test_gate_mismatch -v
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

_spec = importlib.util.spec_from_file_location(
    "pool_mod", os.path.join(ROOT, "scripts", "06_pool.py"))
pool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pool)


def row(nom_count, attempts, tier="classic", status="PASSED"):
    """构造一行 matrix：nom_count 个 no_title_match 失败步骤 + attempts 次探测。"""
    steps = {}
    for i in range(nom_count):
        steps["%s.search.book%d" % (tier, i)] = {
            "ok": False, "stage": "SEARCH_PARSE:no_title_match"}
    return {"steps": steps, "attempts": {tier: attempts}, "status": status}


class TestMismatchRate(unittest.TestCase):
    def test_clean_source(self):
        self.assertEqual(pool.mismatch_rate(row(0, 30)), 0.0)

    def test_sou78_sample(self):
        r = row(14, 30)
        self.assertAlmostEqual(pool.mismatch_rate(r), 14 / 30, places=4)

    def test_zero_attempts(self):
        self.assertEqual(pool.mismatch_rate(row(0, 0)), 0.0)

    def test_prefix_tolerant(self):
        r = {"steps": {"classic.search.x": {"stage": "SEARCH_PARSE:no_title_match:extra"}},
             "attempts": {"classic": 10}}
        self.assertAlmostEqual(pool.mismatch_rate(r), 0.1, places=4)


class TestRowOk(unittest.TestCase):
    def base(self, r):
        r.update({"search_ok": True, "status": "PASSED", "risk_level": "clean"})
        return r

    def test_row_ok_rejects_dirty(self):
        self.assertFalse(pool._row_ok(self.base(row(14, 30))))

    def test_row_ok_keeps_clean(self):
        self.assertTrue(pool._row_ok(self.base(row(3, 30))))

    def test_row_ok_boundary(self):
        # 0.25 恰好 → False；0.249 → True。用 attempts=1000 精确控制。
        self.assertFalse(pool._row_ok(self.base(row(250, 1000))))
        self.assertTrue(pool._row_ok(self.base(row(249, 1000))))


class TestRetireByMismatch(unittest.TestCase):
    def test_retire_dirty_pool_source(self):
        p = {"source_id": "x"}
        r = row(14, 30, status="PASSED")
        self.assertTrue(pool.should_retire_by_mismatch(p, r))

    def test_keep_timeout_source(self):
        p = {"source_id": "x"}
        r = row(14, 30, status="TIMEOUT")
        self.assertFalse(pool.should_retire_by_mismatch(p, r))

    def test_keep_unmeasured_source(self):
        p = {"source_id": "x"}
        self.assertFalse(pool.should_retire_by_mismatch(p, None))

    def test_retire_even_if_blocked(self):
        p = {"source_id": "x"}
        r = row(14, 30, status="PASSED")
        r["blocked_hosts"] = [{"action": "blocked", "host": "evil.com"}]
        self.assertTrue(pool.should_retire_by_mismatch(p, r))


class TestCommitIntegration(unittest.TestCase):
    """集成测试：守住 commit 二次过滤里"guard 清空 reason 后污染门禁重新置回"的真实交互。

    纯函数测试无法覆盖这条交互——must 用真实 commit 跑一遍，否则把污染判断
    挪到 guard 之前 / 改成 elif 挂守卫分支后，单测仍全绿而核心行为已失效。
    """

    def test_guard_cleared_reason_reinstated_by_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            run_id = "itest"
            # commit 依赖 runs_root()/STATE_PATH：用环境变量 + patch 指到临时目录
            with mock.patch.dict(os.environ, {"LEGADO_SY_RUNS_DIR": td}), \
                 mock.patch.object(pool, "STATE_PATH", os.path.join(td, "_state", "pool.json")):
                rd = os.path.join(td, run_id)
                os.makedirs(rd, exist_ok=True)

                # 池内一个源，本轮被测到
                os.makedirs(os.path.dirname(pool.STATE_PATH), exist_ok=True)
                pool_item = {
                    "source_id": "dirty1", "domain": "dirty.com",
                    "bookSourceName": "脏源", "content_hash": "h1",
                    "source": {"bookSourceName": "脏源", "bookSourceUrl": "https://dirty.com"},
                    "added_round": 0, "fail_streak": 0, "blocked_streak": 0,
                    "history": [],
                }
                state = {"schema": "pool-state-v1", "size": 50, "round": 1,
                         "cursor": 0, "pool": [pool_item], "retired": {}}
                pool.dump_json(pool.STATE_PATH, state)
                pool.dump_json(os.path.join(rd, "round_plan.json"),
                               {"run_id": run_id, "mode": "recheck"})

                # matrix 行：risk 非 clean（触发 risk_filter reason）+ blocked 非空
                # （触发 guard 清空 reason）+ 高污染（必须重新置回 mismatch_filter）
                r = row(14, 30, status="PASSED")
                r.update({"source_id": "dirty1", "risk_level": "suspicious",
                          "blocked_hosts": [{"action": "blocked", "host": "evil.com"}]})
                pool.dump_jsonl(os.path.join(rd, "matrix.jsonl"), [r])
                pool.dump_jsonl(os.path.join(rd, "targets.jsonl"),
                                [{"source_id": "dirty1"}])

                pool.commit(run_id, fail_limit=2, blocked_limit=3)

                st = pool.load_state()
                self.assertIn("dirty1", st["retired"],
                              "高污染源即使被白名单守卫清空 reason，仍须进 retired")
                self.assertTrue(
                    st["retired"]["dirty1"]["reason"].startswith("mismatch_filter:"),
                    "最终 reason 必须是 mismatch_filter，证明污染门禁在 guard 之后重新置回")


if __name__ == "__main__":
    unittest.main()
