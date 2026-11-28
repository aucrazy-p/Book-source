# -*- coding: utf-8 -*-
"""06_pool.py 池内跨域重定向源过滤单测（TDD：先红后绿）。

覆盖（用户 2026-11-28 确认的需求）：
- same_site_host：同域 / www 规范化 / 子域 / 大小写 / 跨域（ybswa→yybsw）/ 空值
- search_final_host：跟随 301 后的最终 host；网络异常/无 searchUrl 返回空（不拦截）
- cross_domain_sources：并发检查只收跨域源，空 host 不记入
- commit 集成：跨域源进 retired 且不在 bookSources.json；网络全失败时不误删

运行：python -m unittest tests.test_cross_domain -v
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import lib  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "pool_cross_mod", os.path.join(ROOT, "scripts", "06_pool.py"))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class FakeResp:
    def __init__(self, final_url):
        self._final = final_url

    def geturl(self):
        return self._final

    def read(self, n):
        return b"x"

    def close(self):
        pass


class FakeOpener:
    """按请求 URL 的 host 返回预设落地 URL；raise_exc 时抛异常。"""

    def __init__(self, mapping=None, raise_exc=False):
        self.mapping = mapping or {}
        self.raise_exc = raise_exc

    def open(self, req, timeout=None):
        if self.raise_exc:
            raise TimeoutError("probe timeout")
        host = (urlparse(req.full_url).hostname or "").lower()
        return FakeResp(self.mapping.get(host, "https://" + host + "/"))


def pool_entry(sid, search_host):
    return {
        "source_id": sid,
        "domain": search_host,
        "bookSourceName": sid,
        "content_hash": "h" + sid,
        "source": {
            "bookSourceName": sid,
            "bookSourceUrl": "https://" + search_host,
            "searchUrl": "https://" + search_host + "/search?q={{key}}",
        },
        "added_round": 0, "fail_streak": 0, "blocked_streak": 0, "history": [],
    }


class TestSameSiteHost(unittest.TestCase):
    def test_identical_host(self):
        self.assertTrue(mod.same_site_host("foo.com", "foo.com"))

    def test_www_is_same_site(self):
        self.assertTrue(mod.same_site_host("www.foo.com", "foo.com"))

    def test_subdomain_is_same_site(self):
        self.assertTrue(mod.same_site_host("foo.com", "m.foo.com"))

    def test_case_and_trailing_dot_normalized(self):
        self.assertTrue(mod.same_site_host("WWW.Foo.COM.", "foo.com"))

    def test_different_registrable_domain_is_cross(self):
        self.assertFalse(mod.same_site_host("ybswa.com", "yybsw.com"))

    def test_empty_host_is_not_same(self):
        self.assertFalse(mod.same_site_host("", "foo.com"))
        self.assertFalse(mod.same_site_host("foo.com", ""))

    def test_suffix_without_dot_boundary_is_cross(self):
        # notfoo.com 不能因为包含 foo.com 就被当同站
        self.assertFalse(mod.same_site_host("foo.com", "notfoo.com"))


class TestSearchFinalHost(unittest.TestCase):
    def test_follows_redirect_to_new_domain(self):
        src = {"searchUrl": "https://www.ybswa.com/search?q={{key}}"}
        opener = FakeOpener({"www.ybswa.com": "https://www.yybsw.com/search?q=x"})
        self.assertEqual(mod.search_final_host(src, opener=opener), "yybsw.com")

    def test_www_redirect_stays_same_host_after_normalize(self):
        src = {"searchUrl": "https://www.foo.com/search?q={{key}}"}
        opener = FakeOpener({"www.foo.com": "https://foo.com/search?q=x"})
        self.assertEqual(mod.search_final_host(src, opener=opener), "foo.com")

    def test_network_error_returns_empty(self):
        src = {"searchUrl": "https://foo.com/search?q={{key}}"}
        self.assertEqual(mod.search_final_host(src, opener=FakeOpener(raise_exc=True)), "")

    def test_no_search_url_returns_empty(self):
        self.assertEqual(mod.search_final_host({}, opener=FakeOpener()), "")

    def test_js_search_url_returns_empty(self):
        src = {"searchUrl": "@js:return 'x'"}
        self.assertEqual(mod.search_final_host(src, opener=FakeOpener()), "")


class TestCrossDomainSources(unittest.TestCase):
    def test_only_cross_domain_sources_returned(self):
        pool = [pool_entry("bad", "ybswa.com"), pool_entry("good", "good.com")]
        opener = FakeOpener({
            "ybswa.com": "https://yybsw.com/search?q=x",
            "good.com": "https://good.com/search?q=x",
        })
        got = mod.cross_domain_sources(pool, opener=opener, workers=2)
        self.assertIn("bad", got)
        self.assertEqual(got["bad"]["final"], "yybsw.com")
        self.assertNotIn("good", got)

    def test_network_failure_yields_empty_dict(self):
        pool = [pool_entry("s1", "foo.com")]
        got = mod.cross_domain_sources(pool, opener=FakeOpener(raise_exc=True), workers=1)
        self.assertEqual(got, {})


class TestCommitCrossDomain(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.state_path = os.path.join(self.td, "_state", "pool.json")
        self.ctx = mock.patch.dict(os.environ, {"LEGADO_SY_RUNS_DIR": self.td}), \
                   mock.patch.object(mod, "STATE_PATH", self.state_path)
        for c in self.ctx:
            c.start()
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)

    def tearDown(self):
        for c in reversed(self.ctx):
            c.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def _setup_run(self, pool_list):
        d = lib.run_dir("crossrun")
        os.makedirs(d, exist_ok=True)
        mod.dump_json(os.path.join(d, "round_plan.json"),
                      {"mode": "fill", "size": 66, "targets": []})
        mod.dump_jsonl(os.path.join(d, "matrix.jsonl"), [])
        mod.dump_jsonl(os.path.join(d, "targets.jsonl"), [])
        mod.dump_json(self.state_path, {
            "schema": "pool-state-v1", "size": 66, "round": 1,
            "cursor": 0, "retired": {}, "pool": pool_list, "scanned_ids": [],
        })

    def test_cross_domain_source_removed_from_pool_and_export(self):
        pool = [pool_entry("bad", "ybswa.com"), pool_entry("good", "good.com")]
        self._setup_run(pool)
        cross = {"bad": {"orig": "ybswa.com", "final": "yybsw.com"}}
        with mock.patch.object(mod, "cross_domain_sources", return_value=cross), \
             mock.patch.object(mod, "_adult_blocker", return_value=(lambda s: (False, ""), 0)):
            mod.commit("crossrun", 2, 3)
        st = mod.load_state()
        ids = [p["source_id"] for p in st["pool"]]
        self.assertNotIn("bad", ids)
        self.assertIn("good", ids)
        self.assertIn("bad", st["retired"])
        self.assertTrue(st["retired"]["bad"]["reason"].startswith("cross_domain_redirect:"))
        exported = json.load(open(os.path.join(lib.run_dir("crossrun"), "bookSources.json"),
                                  encoding="utf-8"))
        self.assertEqual([s["bookSourceUrl"] for s in exported], ["https://good.com"])

    def test_network_failure_keeps_all_sources(self):
        pool = [pool_entry("s1", "foo.com"), pool_entry("s2", "bar.com")]
        self._setup_run(pool)
        with mock.patch.object(mod, "cross_domain_sources", return_value={}), \
             mock.patch.object(mod, "_adult_blocker", return_value=(lambda s: (False, ""), 0)):
            mod.commit("crossrun", 2, 3)
        st = mod.load_state()
        self.assertEqual({p["source_id"] for p in st["pool"]}, {"s1", "s2"})
        self.assertEqual(st["retired"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
