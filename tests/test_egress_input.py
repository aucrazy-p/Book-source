# -*- coding: utf-8 -*-
"""出口白名单注入测试（方案 §3.5）：declared_hosts 提取 + is_new 冷启动。"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from make_probe_input import copy_host_policy, extract_declared_hosts


class TestDeclaredHosts(unittest.TestCase):
    def test_url_in_search_url(self):
        src = {"searchUrl": "https://skybook.mgz.la/search?q={{key}}"}
        self.assertEqual(extract_declared_hosts(src), ["skybook.mgz.la"])

    def test_url_buried_in_js_string(self):
        # bookSourceUrl 常是书源名，真域名埋在 searchUrl 的 JS 串里（方案 §3.2 实测）
        src = {
            "bookSourceUrl": "69-明月",
            "searchUrl": '@js:buildRequest("https://skybook.legado1.top/api?k={{key}}")',
            "jsLib": '{"utils":"https://cdn.example.com/js/utils.js"}',
        }
        hosts = extract_declared_hosts(src)
        self.assertIn("skybook.legado1.top", hosts)
        self.assertIn("cdn.example.com", hosts)
        self.assertNotIn("69-明月", hosts)

    def test_www_stripped_and_dedup(self):
        src = {
            "searchUrl": "https://www.example.com/a",
            "ruleBookInfo": "https://example.com/b",
            "header": "https://WWW.EXAMPLE.COM/c",
        }
        self.assertEqual(extract_declared_hosts(src), ["example.com"])

    def test_port_and_nested_structures(self):
        src = {
            "searchUrl": "https://api.example.com:8080/x",
            "ruleToc": ["https://img.example.com/cover", {"k": "https://cdn.other.net/z"}],
        }
        hosts = extract_declared_hosts(src)
        self.assertIn("api.example.com", hosts)   # 端口去掉（正则只抓 host 字符）
        self.assertIn("cdn.other.net", hosts)

    def test_no_url_returns_empty(self):
        self.assertEqual(extract_declared_hosts({"bookSourceUrl": "69-明月"}), [])

    def test_hosted_suffix_captured(self):
        src = {"searchUrl": "https://skybook.pages.dev/api"}
        self.assertEqual(extract_declared_hosts(src), ["skybook.pages.dev"])


class TestHostPolicyCopy(unittest.TestCase):
    def test_copy_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            ok = copy_host_policy(d)
            self.assertTrue(ok)
            dst = os.path.join(d, "host_policy.json")
            self.assertTrue(os.path.exists(dst))
            import json
            with open(dst, encoding="utf-8") as f:
                policy = json.load(f)
            self.assertIn("platforms", policy)
            self.assertIn("hosted_suffixes", policy)
            # 托管域必须逐 host 精确匹配 — 策略里只放后缀清单，不放通配规则
            self.assertTrue(all("*" not in s for s in policy["hosted_suffixes"]))


if __name__ == "__main__":
    unittest.main()
