# -*- coding: utf-8 -*-
"""collect_yiove premium 预筛单测（TDD：先红后绿）。

覆盖：
- JS 源 / 无 searchUrl / 非 HTTP 地址被过滤，只留标准源
- 成人源用真实 02_normalize.adult_filter.txt 规则剔除
- 同站换皮按 source_id 去重，只留一个
- 按 lastUpdateTime 降序（新源在前）
- premium 并入常规候选按 source_id 去重，不产生重复
- 降级：读缓存缺失 / 网络失败 / 解析失败 → 返回空 + 不抛异常
- fetch_premium_collection 用参数化下载（大 max_bytes + 长 timeout）

运行：python -m unittest tests.test_premium -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import collect_yiove as cyv


def src(name, url, search=None, js=None, lut=None):
    """构造标准 legado 源，可选字段按需覆盖。"""
    s = {
        "bookSourceName": name,
        "bookSourceUrl": url,
        "bookSourceGroup": "小说",
        "searchUrl": search if search is not None else url.rstrip("/") + "/s?q={{key}}",
    }
    if js is not None:
        s["jsLib"] = js
    if lut is not None:
        s["lastUpdateTime"] = lut
    return s


class TestPremiumFilter(unittest.TestCase):
    def test_premium_filters_js_and_no_search(self):
        std = src("甲", "https://a.com")
        js = src("乙", "https://b.com", js="java.ajax(...)")
        nosearch = src("丙", "https://c.com", search="")
        out, meta = cyv.premium_filter([std, js, nosearch], log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["bookSourceName"], "甲")
        self.assertEqual(meta["before"], 3)
        self.assertEqual(meta["after"], 1)
        self.assertEqual(meta["dropped_js"], 1)

    def test_premium_filters_non_http(self):
        out, meta = cyv.premium_filter(
            [src("甲", "https://a.com"), src("乙", "听雪聚合")],
            log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["bookSourceName"], "甲")
        self.assertGreaterEqual(meta.get("dropped_nonhttp", 0), 1)

    def test_premium_filters_adult(self):
        ok = src("甲", "https://a.com")
        adult = src("成人色情小说", "https://adult.com")
        out, meta = cyv.premium_filter([ok, adult], log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["bookSourceName"], "甲")
        self.assertEqual(meta["dropped_adult"], 1)

    def test_premium_dedupe_by_domain(self):
        # 同站换皮：URL 尾部尾标不同，normalize_url 归一到同一 source_id
        a = src("甲-原版", "https://a.com", lut=100)
        b = src("甲-妍希", "https://a.com/#妍希", lut=200)
        out, meta = cyv.premium_filter([a, b], log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(meta["dropped_dup"], 1)

    def test_premium_sorts_by_update_time(self):
        a = src("旧", "https://a.com", lut=100)
        b = src("新", "https://b.com", lut=300)
        c = src("中", "https://c.com", lut=200)
        out, _ = cyv.premium_filter([a, b, c], log=lambda *_: None)
        self.assertEqual([s["bookSourceName"] for s in out], ["新", "中", "旧"])


class TestPremiumMerge(unittest.TestCase):
    def test_merge_premium_dedupes_against_regular(self):
        regular = [src("甲", "https://a.com")]
        premium = [src("甲-换皮", "https://a.com/#妍希"), src("乙", "https://b.com")]
        out, meta = cyv.merge_premium_pool(regular, premium, log=lambda *_: None)
        ids = [cyv.source_id(s["bookSourceUrl"]) for s in out]
        self.assertEqual(len(ids), len(set(ids)), "合并后不应有重复 source_id")
        self.assertEqual(len(out), 2)
        self.assertEqual(meta["added"], 1)
        self.assertEqual(meta["overlap"], 1)

    def test_merged_item_marked_premium(self):
        out, _ = cyv.merge_premium_pool([], [src("乙", "https://b.com")],
                                        log=lambda *_: None)
        self.assertEqual(out[0]["_upstream"], "yiove:premium:" + cyv.PREMIUM_COLLECTION_ID)
        self.assertTrue(out[0].get("_is_valid"))


class TestPremiumIO(unittest.TestCase):
    def test_load_premium_pool_missing_returns_empty(self):
        self.assertEqual(cyv.load_premium_pool(
            os.path.join(tempfile.gettempdir(), "no_such_premium_pool.jsonl")), [])

    def test_load_premium_pool_reads_jsonl(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "premium_pool.jsonl")
            rows = [src("甲", "https://a.com"), src("乙", "https://b.com")]
            with open(p, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            self.assertEqual(len(cyv.load_premium_pool(p)), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_fetch_premium_uses_large_download_params(self):
        with mock.patch.object(cyv, "get_json", return_value=([], {"url": "x"})) as gj:
            cyv.fetch_premium_collection(log=lambda *_: None)
            _, kwargs = gj.call_args
            self.assertGreaterEqual(kwargs.get("max_bytes", 0), 128 * 1024 * 1024,
                                    "大合集 89MB，max_bytes 必须放大")
            self.assertGreaterEqual(kwargs.get("timeout", 0), 120,
                                    "大合集下载约 84s，timeout 必须放大")

    def test_fetch_premium_failure_degrades(self):
        with mock.patch.object(cyv, "get_json", return_value=(None, {"error": "timeout"})):
            out, meta = cyv.fetch_premium_collection(log=lambda *_: None)
            self.assertEqual(out, [])
            self.assertIn("error", meta)

    def test_fetch_premium_bad_payload_degrades(self):
        with mock.patch.object(cyv, "get_json", return_value=({"bad": 1}, {"url": "x"})):
            out, meta = cyv.fetch_premium_collection(log=lambda *_: None)
            self.assertEqual(out, [])
            self.assertIn("error", meta)


if __name__ == "__main__":
    unittest.main(verbosity=2)
