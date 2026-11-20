# -*- coding: utf-8 -*-
"""成人分组标签判定单测（TDD：先红后绿）。

覆盖 02_normalize.is_adult 的 bookSourceGroup 分支：
- 命中：🔞 / 涩涩 / 成人 / r18 / 敏感资源 / H / H+SP / 绅士 / 刘备 / 独立 18
- 不命中：正常分组（免费/小说/漫画/正版/笔趣阁/3/a/HTML/HD）

运行：python -m unittest tests.test_adult_group -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "normalize_mod", os.path.join(ROOT, "scripts", "02_normalize.py"))
normalize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(normalize)


def adult(group="", name="某小说", url="https://example.com"):
    s = {"bookSourceGroup": group, "bookSourceName": name, "bookSourceUrl": url}
    return normalize.is_adult(s, [])[0]


class TestAdultGroup(unittest.TestCase):
    """组级整组剔除：任一分组标签命中即判成人。"""

    def test_group_emoji_18plus(self):
        self.assertTrue(adult("🔞"))

    def test_group_sese(self):
        self.assertTrue(adult("❤️涩涩"))

    def test_group_h_alone(self):
        self.assertTrue(adult("H"), "独立标签 H 应判成人")

    def test_group_h_plus_sp(self):
        self.assertTrue(adult("H+SP"))

    def test_group_hsp(self):
        self.assertTrue(adult("HSP"))

    def test_group_shenshi(self):
        self.assertTrue(adult("🚬 绅士"))

    def test_group_liubei(self):
        self.assertTrue(adult("🚬 刘备"), "刘备是黄书黑话，应判成人")

    def test_group_18_alone(self):
        self.assertTrue(adult("18"))

    def test_group_r18(self):
        self.assertTrue(adult("R18"))

    def test_group_in_multi_tag_group(self):
        self.assertTrue(adult("免费,小说,🔞"), "多标签中任一命中即整条剔除")

    def test_normal_free(self):
        self.assertFalse(adult("免费"))

    def test_normal_novel(self):
        self.assertFalse(adult("小说·全本 📚"))

    def test_normal_digit_tag(self):
        self.assertFalse(adult("3"))

    def test_normal_single_letter_a(self):
        self.assertFalse(adult("a"))

    def test_normal_biquge(self):
        self.assertFalse(adult("笔趣阁"))

    def test_no_false_positive_html(self):
        self.assertFalse(adult("HTML"), "H 只按独立标签匹配，不得命中 HTML")

    def test_no_false_positive_hd(self):
        self.assertFalse(adult("HD高清"))

    def test_no_false_positive_a_zhengban(self):
        self.assertFalse(adult("正版"))

    def test_empty_group(self):
        self.assertFalse(adult(""))

    # ---- xxx 规则边界（域名占位符不得误伤） ----
    def _adult_by_url(self, url, name="某站"):
        s = {"bookSourceGroup": "", "bookSourceName": name, "bookSourceUrl": url}
        return normalize.is_adult(s, normalize.load_adult_patterns())[0]

    def test_xxx_placeholder_domain_not_adult(self):
        self.assertFalse(self._adult_by_url("http://m.xxxbiquge.net"),
                         "xxxbiquge 里的 xxx 是占位符，不是成人标记")

    def test_xxx_hongshu_not_adult(self):
        self.assertFalse(self._adult_by_url("https://www.xxxhongshu.com/"))

    def test_xxx_standalone_still_adult(self):
        self.assertTrue(self._adult_by_url("https://hentai-cosplay-xxx.com/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
