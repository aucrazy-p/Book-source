# -*- coding: utf-8 -*-
"""yckceo 采集脚本单测（TDD：先红后绿）。

全部离线：网络由注入的假 fetcher 替代，不碰真实站点。

覆盖：
- parse_list_page：从列表页 HTML 提取 (id, 标题)
- pick_collections：精选关键词筛选 + 小包排除
- numeric_hint：从标题抽最大数字（"601条"/"3万源"/"余590"）
- dedupe_sources：三级去重（source_id → content_hash）
- filter_adult：组级整组剔除
- build_payload：落盘结构与 candidate_seed 兼容

运行：python -m unittest tests.test_yckceo -v
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import collect_yckceo as cy


LIST_HTML = """
<div class="list">
  <a href="/yuedu/shuyuans/content/id/1340.html" title="x">自用整理·阅读Legado正常书源（无18+）合集（601条｜小说/漫画）</a>
  <a href="/yuedu/shuyuans/content/id/1336.html">校验400+</a>
  <a href="/yuedu/shuyuans/content/id/1329.html">自用</a>
  <a href="/yuedu/shuyuans/content/id/1308.html">手动去掉漏字、缺字书源，21精选书源</a>
  <a href="/yuedu/shuyuans/content/id/1208.html">260727极致精简</a>
  <a href="/yuedu/shuyuans/content/id/1244.html">从31436个源中，筛选出来的，去木马，专注读书！</a>
  <a href="/yuedu/shuyuans/content/id/1159.html">分享1607个源合集</a>
</div>
"""


def src(name, url, group="", search=None):
    return {"bookSourceName": name, "bookSourceUrl": url,
            "bookSourceGroup": group,
            "searchUrl": search if search is not None else url.rstrip("/") + "/s?q={k}"}


class TestParseListPage(unittest.TestCase):
    def test_extract_ids_and_titles(self):
        items = cy.parse_list_page(LIST_HTML)
        ids = [i[0] for i in items]
        self.assertEqual(ids, ["1340", "1336", "1329", "1308", "1208", "1244", "1159"])

    def test_title_html_unescaped(self):
        items = dict(cy.parse_list_page(LIST_HTML))
        self.assertIn("601条", items["1340"])

    def test_no_duplicate_ids(self):
        html = LIST_HTML + LIST_HTML
        items = cy.parse_list_page(html)
        self.assertEqual(len(items), len({i[0] for i in items}))


class TestNumericHint(unittest.TestCase):
    def test_plain_count(self):
        self.assertEqual(cy.numeric_hint("校验400+"), 400)

    def test_yu_prefix(self):
        self.assertEqual(cy.numeric_hint("6月18日校验，余590"), 590)

    def test_wan_unit(self):
        self.assertEqual(cy.numeric_hint("从31436个源中筛选"), 31436)

    def test_wan_chinese(self):
        self.assertEqual(cy.numeric_hint("3万源中筛选"), 30000)

    def test_no_number(self):
        self.assertEqual(cy.numeric_hint("极致精简"), 0)

    def test_ignores_18plus_marker(self):
        # "无18+ 601条" 应取 601，而不是 18
        self.assertEqual(cy.numeric_hint("（无18+）合集（601条）"), 601)


class TestPickCollections(unittest.TestCase):
    def setUp(self):
        self.items = cy.parse_list_page(LIST_HTML)

    def test_keyword_hit(self):
        picked = dict(cy.pick_collections(self.items, top=10))
        self.assertIn("1336", picked)   # 校验
        self.assertIn("1340", picked)   # 无18+
        self.assertIn("1244", picked)   # 去木马
        self.assertIn("1159", picked)   # 1607个源

    def test_drop_generic_title(self):
        picked = dict(cy.pick_collections(self.items, top=10))
        self.assertNotIn("1329", picked, "「自用」无精选关键词，应排除")

    def test_drop_small_pack_by_title(self):
        picked = dict(cy.pick_collections(self.items, top=10))
        self.assertNotIn("1308", picked, "21条小包应排除")

    def test_top_limit(self):
        picked = cy.pick_collections(self.items, top=2)
        self.assertEqual(len(picked), 2)


class TestDedupe(unittest.TestCase):
    def test_same_url_deduped(self):
        rows = [src("甲", "https://a.com"), src("乙", "https://a.com")]
        self.assertEqual(len(cy.dedupe_sources(rows)), 1)

    def test_same_content_deduped(self):
        a = src("甲", "https://a.com")
        b = dict(a, bookSourceUrl="https://b.com")
        self.assertEqual(len(cy.dedupe_sources([a, b])), 1, "同内容换域名应去重")

    def test_different_sources_kept(self):
        rows = [src("甲", "https://a.com"), src("乙", "https://b.com", search="https://b.com/s")]
        self.assertEqual(len(cy.dedupe_sources(rows)), 2)


class TestFilterAdult(unittest.TestCase):
    def test_group_h_removed(self):
        rows = [src("正常名", "https://a.com", group="H")]
        kept, dropped = cy.filter_adult(rows)
        self.assertEqual(len(kept), 0)
        self.assertEqual(len(dropped), 1)

    def test_group_18plus_removed(self):
        rows = [src("正常名", "https://a.com", group="🔞")]
        kept, _ = cy.filter_adult(rows)
        self.assertEqual(len(kept), 0)

    def test_whole_group_removed(self):
        """同组 3 条，1 条标成人 → 整组 3 条都不留？否：按标签判定，同组全中。"""
        rows = [src("一", "https://a.com", group="小说,🔞"),
                src("二", "https://b.com", group="小说,🔞")]
        kept, dropped = cy.filter_adult(rows)
        self.assertEqual((len(kept), len(dropped)), (0, 2))

    def test_normal_kept(self):
        rows = [src("正常", "https://a.com", group="免费,小说")]
        kept, _ = cy.filter_adult(rows)
        self.assertEqual(len(kept), 1)

    def test_java_api_comment_not_adult(self):
        """评论里写 java.ajax(...) 是正常规则说明，绝不能因 'jav' 判成人。"""
        s = src("我是盐神", "https://a.com", group="小说")
        s["bookSourceComment"] = 'org.jsoup.Jsoup.parse(java.ajax("https://on...'
        kept, dropped = cy.filter_adult([s])
        self.assertEqual(len(kept), 1, "java.ajax 被误判: %s" % (dropped and dropped[0][1]))

    def test_java_md5_comment_not_adult(self):
        s = src("星空小说", "https://a.com", group="小说")
        s["bookSourceComment"] = 'return String(java.md5Encode(s));'
        kept, _ = cy.filter_adult([s])
        self.assertEqual(len(kept), 1)

    def test_jav_keyword_in_name_still_caught(self):
        """真正的 JAV 站仍要拦（严格模式不能因噎废食）。"""
        rows = [src("JAV高清", "https://a.com", group="视频")]
        kept, _ = cy.filter_adult(rows)
        self.assertEqual(len(kept), 0)

    def test_javbus_domain_caught(self):
        rows = [src("某站", "https://javbus.com", group="视频")]
        kept, _ = cy.filter_adult(rows)
        self.assertEqual(len(kept), 0)


class TestBuildPayload(unittest.TestCase):
    def test_payload_shape(self):
        rows = [src("甲", "https://a.com")]
        p = cy.build_payload(rows, provenance=[{"id": "1"}], run_id="test")
        for k in ("source_count", "collected_at", "sha256", "provenance", "sources", "upstream"):
            self.assertIn(k, p)
        self.assertEqual(p["source_count"], 1)

    def test_roundtrip_json(self):
        rows = [src("甲", "https://a.com", group="小说")]
        p = cy.build_payload(rows, provenance=[], run_id="t")
        self.assertEqual(json.loads(json.dumps(p, ensure_ascii=False))["sources"][0]["bookSourceName"], "甲")


class TestCollectOffline(unittest.TestCase):
    def test_end_to_end_with_fake_fetcher(self):
        pages = {1: LIST_HTML}
        payloads = {
            "1340": [src("a", "https://a.com", group="小说")],
            "1336": [src("b", "https://b.com", group="小说")],
            "1244": [src("c", "https://c.com", group="H")],
            "1159": [src("d", "https://d.com", group="免费")],
        }

        def fetch_html(page):
            return pages.get(page, "")

        def fetch_json(cid):
            return payloads.get(cid)

        p = cy.collect(pages=1, top=10, min_sources=1,
                       fetch_html=fetch_html, fetch_json=fetch_json, run_id="t")
        names = sorted(s["bookSourceName"] for s in p["sources"])
        self.assertEqual(names, ["a", "b", "d"], "H 组源应被剔除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
