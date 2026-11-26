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


class TestPremiumRules(unittest.TestCase):
    """规则配置：域名级名单可解析 + 擦边词不加单字 + 误伤守卫。"""

    def test_adult_domains_file_exists_and_parseable(self):
        path = os.path.join(ROOT, "conf", "adult_domains.txt")
        self.assertTrue(os.path.exists(path), "域名级成人名单文件应存在")
        domains = cyv.load_adult_domains()
        self.assertIsInstance(domains, list)
        for d in domains:
            self.assertIsInstance(d, str)
            self.assertTrue(d.strip())
            self.assertFalse(d.startswith("#"))

    def test_no_single_char_adult_words(self):
        path = os.path.join(ROOT, "conf", "adult_filter.txt")
        singles = []
        for line in open(path, encoding="utf-8"):
            w = line.strip()
            if w and not w.startswith("#") and len(w) == 1:
                singles.append(w)
        self.assertEqual(singles, [], "擦边词不得用单字（历史误伤 java/xxx）")

    def test_java_api_not_flagged(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_norm02_rules", os.path.join(ROOT, "scripts", "02_normalize.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pats = mod.load_adult_patterns()
        # is_adult 只拼接 name+url——把误伤诱饵放真正读取的字段才有守卫意义
        s1 = src("java.ajax 书源规则", "https://a.com")
        s2 = src("甲", "https://www.xxxbiquge.net")
        self.assertFalse(mod.is_adult(s1, pats)[0], "java.ajax 在名称中不得被判成人")
        self.assertFalse(mod.is_adult(s2, pats)[0], "xxxbiquge 在域名中不得被判成人")


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

    def test_premium_filters_adult_domain(self):
        ok = src("甲", "https://a.com")
        bad = src("擦边站", "https://evil-adult.example.com")
        with mock.patch.object(cyv, "load_adult_domains", return_value=["evil-adult.example.com"]):
            out, meta = cyv.premium_filter([ok, bad], log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["bookSourceName"], "甲")
        self.assertEqual(meta["dropped_adult_domain"], 1)

    def test_premium_dedupe_by_domain(self):
        # 同站换皮：URL 尾部尾标不同，normalize_url 归一到同一 source_id
        a = src("甲-原版", "https://a.com", lut=100)
        b = src("甲-妍希", "https://a.com/#妍希", lut=200)
        out, meta = cyv.premium_filter([a, b], log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(meta["dropped_dup"], 1)

    def test_crosshost_duplicate_dropped_keeps_newest(self):
        # 不同域名、相同规则体（searchUrl 相同）→ content_hash 相同，保留更新时间新的
        import time
        now = int(time.time() * 1000)
        a = src("甲-旧域", "https://old-domain.com", lut=now - 86400000)
        b = src("甲-新域", "https://new-domain.com", lut=now)
        a["searchUrl"] = b["searchUrl"] = "https://shared.com/search?q={{key}}"
        out, meta = cyv.premium_filter([a, b], log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["bookSourceName"], "甲-新域")
        self.assertEqual(meta["dropped_crosshost_dup"], 1)

    def test_stale_source_dropped(self):
        import time
        old_ms = int((time.time() - 4 * 365 * 24 * 3600) * 1000)
        ok = src("新", "https://a.com", lut=int(time.time() * 1000))
        stale = src("超期", "https://b.com", lut=old_ms)
        out, meta = cyv.premium_filter([ok, stale], log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["bookSourceName"], "新")
        self.assertEqual(meta["dropped_stale"], 1)

    def test_mid_age_source_downgraded_not_dropped(self):
        import time
        mid_ms = int((time.time() - 2.5 * 365 * 24 * 3600) * 1000)
        fresh = src("新", "https://a.com", lut=int(time.time() * 1000))
        mid = src("老源", "https://b.com", lut=mid_ms)
        out, meta = cyv.premium_filter([mid, fresh], log=lambda *_: None)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["bookSourceName"], "新", "老源应排到队尾")
        self.assertEqual(meta["downgraded_age"], 1)

    def test_dirty_timestamp_survives(self):
        dirty = [src("无时间", "https://a.com"),
                 src("字符串", "https://b.com", lut="not-a-number"),
                 src("未来", "https://c.com", lut=4102444800001)]
        out, _ = cyv.premium_filter(dirty, log=lambda *_: None)
        self.assertEqual(len(out), 3, "脏时间戳不剔除任何源")

    def test_premium_words_do_not_affect_regular_normalize(self):
        """C1 回归锁：premium 专用擦边词不得写进全局 adult_filter.txt，
        否则常规候选的 02_normalize 会被连带过滤。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_norm02_c1", os.path.join(ROOT, "scripts", "02_normalize.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pats = mod.load_adult_patterns()  # 常规路径读取的全局词
        self.assertFalse(mod.is_adult(src("辣文精选", "https://a.com"), pats)[0],
                         "premium 专用词不得出现在全局规则中")
        # 但 premium 路径必须拦得住
        out, meta = cyv.premium_filter([src("辣文精选", "https://a.com")],
                                       log=lambda *_: None)
        self.assertEqual(len(out), 0)
        self.assertEqual(meta["dropped_adult"], 1)

    def test_adult_domain_matches_subdomain(self):
        """I3 回归锁：域名名单写主域，须拦住 www 等子域。"""
        with mock.patch.object(cyv, "load_adult_domains", return_value=["evil-adult.example.com"]):
            out, meta = cyv.premium_filter(
                [src("擦边", "https://www.evil-adult.example.com")],
                log=lambda *_: None)
        self.assertEqual(len(out), 0)
        self.assertEqual(meta["dropped_adult_domain"], 1)

    def test_dirty_future_timestamp_not_ranked_first(self):
        """I2 回归锁：未来脏时间戳不得排到最前。"""
        import time
        now = int(time.time() * 1000)
        dirty = src("未来脏", "https://a.com", lut=4102444800001)
        normal = src("正常", "https://b.com", lut=now)
        out, _ = cyv.premium_filter([dirty, normal], log=lambda *_: None)
        self.assertEqual(out[0]["bookSourceName"], "正常")

    def test_filter_meta_counts_present(self):
        _, meta = cyv.premium_filter([src("甲", "https://a.com")], log=lambda *_: None)
        for k in ("dropped_adult_domain", "dropped_crosshost_dup",
                  "dropped_stale", "downgraded_age"):
            self.assertIsInstance(meta.get(k), int, "计数键 %s 应为 int" % k)

    def test_premium_sorts_by_update_time(self):
        import time
        now = int(time.time() * 1000)
        day = 86400 * 1000
        a = src("旧", "https://a.com", lut=now - 3 * day)
        b = src("新", "https://b.com", lut=now)
        c = src("中", "https://c.com", lut=now - day)
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
        # premium 优先：2 条 premium 都进（含与常规"甲"同 source_id 的"甲-换皮"），
        # 常规的"甲"被去重丢弃 → added=2 / overlap=1。C1 修复前的旧语义是 added=1。
        self.assertEqual(meta["added"], 2)
        self.assertEqual(meta["overlap"], 1)
        self.assertEqual(out[0]["bookSourceName"], "甲-换皮", "premium 排在常规之前")

    def test_merged_item_marked_premium(self):
        out, _ = cyv.merge_premium_pool([], [src("乙", "https://b.com")],
                                        log=lambda *_: None)
        self.assertEqual(out[0]["_upstream"], "yiove:premium:" + cyv.PREMIUM_COLLECTION_ID)
        self.assertTrue(out[0].get("_is_valid"))

    def test_premium_sources_come_first(self):
        """回归锁（C1）：premium 必须排在候选前部，否则 plan 从头取 50 永远探不到。

        原实现 append 到常规候选之后 → premium 排在 1623 条常规之后，
        探测器 budget 内根本轮不到；排序白做，补给形同虚设。
        """
        regular = [src("常规%02d" % i, "https://old%d.com" % i) for i in range(1, 6)]
        premium = [src("精选%02d" % i, "https://new%d.com" % i,
                       lut=1000 - i) for i in range(1, 4)]
        out, meta = cyv.merge_premium_pool(regular, premium, log=lambda *_: None)
        names = [s["bookSourceName"] for s in out]
        self.assertEqual(names[:3], ["精选01", "精选02", "精选03"],
                         "premium 新源必须排在候选前部，先于常规旧源被探测")
        self.assertEqual(names[3:], ["常规01", "常规02", "常规03", "常规04", "常规05"])


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
