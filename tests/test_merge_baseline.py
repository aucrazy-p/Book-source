# -*- coding: utf-8 -*-
"""collect_yiove.merge_yck_baseline 单测（TDD：先红后绿）。

覆盖：
- 基线文件不存在 → 原样返回，meta.error 有说明，不抛异常
- 基线存在 → 按 source_id 并入，重复的不重复进入
- 合并项带 _upstream=yckceo:baseline
- 返回 meta 含基线 sha256 / 条数，供 provenance 溯源
- 空基线 / 损坏文件 → 不抛异常

运行：python -m unittest tests.test_merge_baseline -v
"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import collect_yiove as cyv


def src(name, url, group="小说", search=None):
    return {"bookSourceName": name, "bookSourceUrl": url, "bookSourceGroup": group,
            "searchUrl": search if search is not None else url.rstrip("/") + "/s?q={k}"}


class TestMergeBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "yck_baseline.json.gz")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, sources):
        payload = {"source_count": len(sources), "sha256": "abcd1234",
                   "collected_at": "2026-11-20T00:00:00+00:00", "sources": sources}
        with gzip.open(self.path, "wb") as f:
            f.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def test_missing_file_passthrough(self):
        base = [src("甲", "https://a.com")]
        out, meta = cyv.merge_yck_baseline(base, self.path, log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertIn("error", meta)
        self.assertEqual(meta["added"], 0)

    def test_merge_adds_new(self):
        base = [src("甲", "https://a.com")]
        self._write([src("乙", "https://b.com"), src("丙", "https://c.com")])
        out, meta = cyv.merge_yck_baseline(base, self.path, log=lambda *_: None)
        self.assertEqual(len(out), 3)
        self.assertEqual(meta["added"], 2)
        self.assertEqual(meta["baseline_count"], 2)

    def test_merge_dedupes_by_source_id(self):
        base = [src("甲", "https://a.com")]
        self._write([src("甲-重名", "https://a.com"), src("乙", "https://b.com")])
        out, meta = cyv.merge_yck_baseline(base, self.path, log=lambda *_: None)
        self.assertEqual(meta["added"], 1)
        self.assertEqual(meta["overlap"], 1)
        self.assertEqual(len(out), 2)

    def test_merged_item_marked(self):
        self._write([src("乙", "https://b.com")])
        out, _ = cyv.merge_yck_baseline([], self.path, log=lambda *_: None)
        self.assertEqual(out[0]["_upstream"], "yckceo:baseline")
        self.assertTrue(out[0].get("_is_valid"))

    def test_empty_sources_in_baseline(self):
        self._write([])
        out, meta = cyv.merge_yck_baseline([src("甲", "https://a.com")], self.path,
                                           log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(meta["added"], 0)

    def test_corrupt_file_no_raise(self):
        with open(self.path, "wb") as f:
            f.write(b"not gzip at all")
        out, meta = cyv.merge_yck_baseline([src("甲", "https://a.com")], self.path,
                                           log=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertIn("error", meta)

    def test_meta_has_sha256(self):
        self._write([src("乙", "https://b.com")])
        _, meta = cyv.merge_yck_baseline([], self.path, log=lambda *_: None)
        self.assertEqual(meta["sha256"], "abcd1234")
        self.assertEqual(meta["path"], self.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
