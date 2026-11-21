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
import unittest

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


if __name__ == "__main__":
    unittest.main()
