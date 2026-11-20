# -*- coding: utf-8 -*-
"""按 source_id 去重（同站换皮包合并）的单元测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from collect_yiove import dedupe_by_source_id  # noqa: E402


def src(name, url):
    return {"bookSourceName": name, "bookSourceUrl": url,
            "searchUrl": "https://x.com/s?q={k}"}


class DedupeBySourceIdTest(unittest.TestCase):
    def test_variants_collapsed(self):
        """#署名 变体、http/https、尾斜杠都应归一到同一条。"""
        rows = [
            src("A#妍希", "https://api-bc.wtzw.com/api/v1/search?q={k}#妍希"),
            src("A#md", "https://api-bc.wtzw.com/api/v1/search?q={k}#md"),
            src("A", "http://api-bc.wtzw.com/api/v1/search?q={k}"),
        ]
        out, dropped = dedupe_by_source_id(rows, log=lambda *a: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(dropped), 2)

    def test_keeps_first_occurrence(self):
        """保留先出现者——上游合集按访问量降序，靠前的质量更好。"""
        rows = [src("首个", "https://a.com/s#x"), src("后来", "https://a.com/s#y")]
        out, _ = dedupe_by_source_id(rows, log=lambda *a: None)
        self.assertEqual(out[0]["bookSourceName"], "首个")

    def test_distinct_hosts_preserved(self):
        rows = [src("甲", "https://a.com/s"), src("乙", "https://b.com/s")]
        out, dropped = dedupe_by_source_id(rows, log=lambda *a: None)
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, [])

    def test_distinct_path_same_host_preserved(self):
        """同站不同接口路径不是换皮，不能合并。"""
        rows = [src("甲", "https://a.com/api/v1/s"), src("乙", "https://a.com/api/v2/s")]
        out, _ = dedupe_by_source_id(rows, log=lambda *a: None)
        self.assertEqual(len(out), 2)

    def test_empty_url_kept_not_merged(self):
        """无 URL 的条目原样保留，不参与去重也不丢数据。"""
        rows = [src("空1", ""), src("空2", ""), src("正常", "https://a.com/s")]
        out, dropped = dedupe_by_source_id(rows, log=lambda *a: None)
        self.assertEqual(len(out), 3)
        self.assertEqual(dropped, [])

    def test_dropped_carries_evidence(self):
        rows = [src("首个", "https://a.com/s#x"), src("换皮", "https://a.com/s#y")]
        _, dropped = dedupe_by_source_id(rows, log=lambda *a: None)
        self.assertEqual(len(dropped), 1)
        s, ev = dropped[0]
        self.assertEqual(s["bookSourceName"], "换皮")
        self.assertTrue(ev.startswith("same_source_id:"))

    def test_order_stable(self):
        rows = [src("甲", "https://a.com/s"), src("乙", "https://b.com/s"),
                src("丙", "https://c.com/s")]
        out, _ = dedupe_by_source_id(rows, log=lambda *a: None)
        self.assertEqual([s["bookSourceName"] for s in out], ["甲", "乙", "丙"])


if __name__ == "__main__":
    unittest.main()
