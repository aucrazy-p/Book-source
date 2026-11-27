# -*- coding: utf-8 -*-
"""meta.json 双时间戳字段回归锁（用户 2026-11-27 定：拆成两字段，页面都显示）。

语义：
  updated  —— 仅 sources.json 内容变化时写（内容变更时间）
  last_run —— 每天巡检都写（哪怕书源无变化，页面也能看到今天跑过）

防止回退：
  - 有人把 last_run 塞进 sources 变化分支 → 无变化日页面又停在旧日期
  - 有人把 updated 每轮覆盖 → 内容没变却显示今天，失去"内容更新"语义
  - 页面只读一个字段 / 只显示一个字段

运行：python -m unittest tests.test_meta_fields -v
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "daily.yml")
INDEX = os.path.join(ROOT, "docs", "index.html")
META = os.path.join(ROOT, "docs", "meta.json")


def _workflow_meta_python():
    """抽出 daily.yml 中写 meta.json 的 heredoc python 源码。"""
    with open(WORKFLOW, encoding="utf-8") as f:
        text = f.read()
    blocks = re.findall(r"python3 - <<'PY'\n(.*?)\n\s*PY", text, re.S)
    # 可能有多处 heredoc（探测统计、写 meta 等），取写 meta 的那段
    m = next((b for b in blocks if "meta['last_run']" in b), None)
    if m is None:
        raise AssertionError("daily.yml 未找到写 meta.json 的 python heredoc")
    # 去掉 workflow 的缩进（heredoc 内容整体缩进了 10 空格）
    lines = m.splitlines()
    indents = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
    cut = min(indents) if indents else 0
    return "\n".join(ln[cut:] if ln.strip() else "" for ln in lines)


class TestMetaFieldsWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as f:
            cls.text = f.read()

    def test_both_fields_written(self):
        """workflow 必须同时写 updated 与 last_run。"""
        self.assertIn("meta['updated']", self.text)
        self.assertIn("meta['last_run']", self.text)

    def test_last_run_unconditional(self):
        """last_run 必须在 sources 变化判断之外无条件写。"""
        src = _workflow_meta_python()
        last_run_line = next((ln for ln in src.splitlines()
                              if "meta['last_run']" in ln), None)
        self.assertIsNotNone(last_run_line, "未找到 last_run 赋值")
        self.assertNotIn("if ", last_run_line,
                         "last_run 不得写在条件分支内（当前行：%s）" % last_run_line.strip())

    def test_updated_gated_by_sources_changed(self):
        """updated 必须仅当 SOURCES_CHANGED==1 才刷新。"""
        src = _workflow_meta_python()
        self.assertIn("SOURCES_CHANGED", src)
        m = re.search(r"if\s+.*SOURCES_CHANGED.*?:\s*\n\s+(meta\['updated'\]\s*=\s*now)", src)
        self.assertIsNotNone(m, "updated 赋值未受 SOURCES_CHANGED 条件保护")

    def test_changed_flag_uses_pathspec(self):
        """sources 变化判断必须带 -- "$DST" 路径限定，避免 meta 自身干扰。"""
        self.assertIn('-- "$DST"', self.text)

    def test_runtime_behavior(self):
        """实际执行 workflow 片段：无变化时 updated 不变、last_run 变；有变化时两者都变。"""
        src = _workflow_meta_python()
        with tempfile.TemporaryDirectory() as d:
            meta_path = os.path.join(d, "meta.json")
            old = {"updated": "2026-01-01T00:00:00Z", "last_run": "2026-01-01T00:00:00Z"}
            with open(meta_path, "w") as f:
                json.dump(old, f)

            env = dict(os.environ)
            env["SOURCES_CHANGED"] = "0"
            # 片段里 open('docs/meta.json') 是相对路径 → 造一层 docs/
            docs = os.path.join(d, "docs")
            os.mkdir(docs)
            with open(os.path.join(docs, "meta.json"), "w") as f:
                json.dump(old, f)
            subprocess.run([sys.executable, "-c", src], cwd=d, env=env, check=True)
            with open(os.path.join(docs, "meta.json")) as f:
                no_change = json.load(f)
            self.assertEqual(no_change["updated"], old["updated"],
                             "无内容变化时 updated 不得被覆盖")
            self.assertNotEqual(no_change["last_run"], old["last_run"],
                                "无内容变化时 last_run 也应刷新")

            env["SOURCES_CHANGED"] = "1"
            subprocess.run([sys.executable, "-c", src], cwd=d, env=env, check=True)
            with open(os.path.join(docs, "meta.json")) as f:
                changed = json.load(f)
            self.assertNotEqual(changed["updated"], old["updated"],
                                "有内容变化时 updated 必须刷新")


class TestMetaFieldsPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(INDEX, encoding="utf-8") as f:
            cls.text = f.read()

    def test_page_reads_both_fields(self):
        self.assertIn("meta.updated", self.text)
        self.assertIn("meta.last_run", self.text)

    def test_page_displays_both_fields(self):
        # 内容更新留在统计卡片区；巡检移到右上角徽章；统计区不再放第 4 张巡检卡
        self.assertIn('id="statUpdate"', self.text)
        self.assertIn('id="topLastRun"', self.text)
        self.assertIn('class="run-badge"', self.text)
        self.assertNotIn('id="statRun"', self.text)
        # 页脚仍同时显示两者，保证"都显示"
        self.assertIn("内容更新：", self.text)
        self.assertIn("最近检测：", self.text)
        self.assertNotIn("最近巡检：", self.text)
        # 徽章文案用「检测」，不用「巡检」
        self.assertIn('>检测 <b id="topLastRun"', self.text)
        self.assertNotIn(">巡检 <b id=\"topLastRun\"", self.text)

    def test_top_last_run_in_header(self):
        """巡检徽章必须落在 header 区块内（右上角），不能散在别处。"""
        header = self.text.split('<header>', 1)[1].split('</header>', 1)[0]
        self.assertIn('id="topLastRun"', header)
        self.assertIn('run-badge', header)

    def test_js_populates_top_last_run(self):
        self.assertIn("getElementById('topLastRun').textContent=fmtDate(LAST_RUN)", self.text)

    def test_seed_meta_has_both_fields(self):
        with open(META, encoding="utf-8") as f:
            meta = json.load(f)
        self.assertIn("updated", meta)
        self.assertIn("last_run", meta)

    def test_subtitle_single_line_on_desktop(self):
        """副标题文案在桌面端应单行显示：max-width 需容得下整句（约 640px）。"""
        m = re.search(r"\.hero p\.sub\{([^}]*)\}", self.text)
        self.assertIsNotNone(m, "未找到 .hero p.sub 样式规则")
        rule = m.group(1)
        wm = re.search(r"max-width:\s*(\d+)px", rule)
        self.assertIsNotNone(wm, "副标题未设 max-width")
        self.assertGreaterEqual(int(wm.group(1)), 800,
                                "副标题 max-width 太窄会换行（当前 %s）" % wm.group(1))


if __name__ == "__main__":
    unittest.main()
