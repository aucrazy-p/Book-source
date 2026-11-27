# -*- coding: utf-8 -*-
"""premium 大合集缓存 key 回归锁（用户 2026-11-27 定：一次下载，不重复采集）。

防止有人把固定 key 改回周级（date +%G-W%V）——那样每周会重下 89MB。
daily 每天 restore 缓存即算访问，GitHub 的「7 天未访问才删」规则不会触发，
所以固定 key 可永久复用；真要拿上游新版，手动升 -v2。

运行：python -m unittest tests.test_premium_cache_key -v
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "daily.yml")


class TestPremiumCacheKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as f:
            cls.text = f.read()

    def _key_line(self):
        for line in self.text.splitlines():
            if "key=premium-" in line and "GITHUB_OUTPUT" in line:
                return line
        self.fail("daily.yml 未找到 premium cache key 赋值行")

    def test_key_is_fixed_version(self):
        """key 必须是固定版本号，形如 premium-<uuid>-v1。"""
        line = self._key_line()
        self.assertIn("premium-df898047-f6f3-4437-93c6-147edc8559b3-v1", line,
                      "premium key 必须是固定版本号（当前行：%s）" % line.strip())

    def test_key_has_no_weekly_rotation(self):
        """禁止任何 date/周数表达式回退——那会导致每周重下 89MB。"""
        line = self._key_line()
        for banned in ("$(date", "%G-W%V", "date -u"):
            self.assertNotIn(banned, line,
                             "premium key 不得含周级表达式 %s（当前行：%s）"
                             % (banned, line.strip()))

    def test_no_restore_keys(self):
        """premium cache 步骤不得设 restore-keys（前缀命中会破坏版本升级语义）。"""
        seg = self.text.split("Restore premium pool from cache", 1)[1].split("Collect candidate", 1)[0]
        self.assertNotIn("restore-keys", seg,
                         "premium cache 步骤不得设 restore-keys")

    def test_cache_path_unchanged(self):
        """缓存路径仍是 premium_pool.jsonl（collect_yiove.py 读取路径）。"""
        self.assertIn("path: runs/ci/premium_pool.jsonl", self.text)


if __name__ == "__main__":
    unittest.main()
