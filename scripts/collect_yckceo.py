# -*- coding: utf-8 -*-
"""从 yckceo 书源站采集「精选/校验/大数量」合集，产出一份可提交的静态基线。

数据源（实测 2026-11-20 可用，服务端渲染 + 官方 JSON 直链）：
  列表页  https://www.yckceo.com/yuedu/shuyuans/index.html?page=N
  合集JSON https://www.yckceo.com/yuedu/shuyuans/json/id/<id>.json   → 标准 legado 数组

设计（用户 2026-11-20 定）：
- 只抓第 1 页（--pages 1），不抓全部 9 页
- 只挑「精选 / 精品 / 校验 / 大数量」合集，小包（<50 条）排除
- 三级去重复用 lib.source_id / lib.content_hash，与 02_normalize 同口径
- 成人过滤「严格」：组标签整组剔除 + 名称/URL/备注四字段强规则
        
产物：conf/yck_baseline.json.gz，结构与 conf/candidate_seed.json.gz 完全一致，
由 collect_yiove.py 的 merge_yck_baseline() 在每轮采集时合并进候选池（daily.yml 零改动）。

用法：
  python scripts/collect_yckceo.py                          # 抓第1页，落 conf/yck_baseline.json.gz
  python scripts/collect_yckceo.py --pages 1 --top 10 --dry-run
  python scripts/collect_yckceo.py --out out/seeds/tmp.json.gz
"""
import gzip
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, HERE)
from lib import SYSTEM_OPENER, UA, decode_body, dump_json   # noqa: E402
import lib as _lib                                          # noqa: E402

LIST_URL = "https://www.yckceo.com/yuedu/shuyuans/index.html?page=%d"
JSON_URL = "https://www.yckceo.com/yuedu/shuyuans/json/id/%s.json"
DEFAULT_OUT = os.path.join(ROOT, "conf", "yck_baseline.json.gz")

REQUEST_DELAY = 0.8
RETRIES = 2
TIMEOUT = 40

# 合集标题筛选：精选类关键词
TITLE_KEYWORD = re.compile(r"精选|精品|校验|优选|去重|无18\+|去马|精简")
# 标题自带条数的下限：低于此值的明确小包直接排除（"21精选书源"）
MIN_TITLE_HINT = 50
# 下载后按真实条数二次过滤（标题无数字的包只能下载后判）
DEFAULT_MIN_SOURCES = 100

# ---- 严格成人过滤 ----------------------------------------------------------
# 中文/emoji 敏感词：四个字段全扫（名称/URL/备注/分组）
STRICT_CN = re.compile(
    r"里番|禁书|肉文|肉书|步兵|无码|无修|工口|エロ|福利姬|约炮|黄文|h漫|本子|"
    r"色情|情色|成人|r18|18禁|涩|🔞|敏感资源|绅士|刘备",
    re.I)
# 英文缩写：只扫名称/URL/分组，**绝不扫 bookSourceComment**
# —— 备注里常写 java.ajax() / java.md5Encode() 等规则说明，"jav" 会大面积误杀正常源
# 名称/分组按词边界（避免 java 之类误伤）；URL 按子串（javbus.com 这类连写域名必须命中）
STRICT_EN_TEXT = re.compile(r"(?<![a-z])jav(?![a-z])|(?<![a-z])av(?![a-z])", re.I)
STRICT_EN_URL = re.compile(r"jav|(?<![a-z])av(?![a-z])", re.I)
CN_FIELDS = ("bookSourceName", "bookSourceUrl", "bookSourceComment", "bookSourceGroup")
EN_TEXT_FIELDS = ("bookSourceName", "bookSourceGroup")

_normalize = None


def _normalize_mod():
    """惰性加载 02_normalize（文件名以数字开头，不能直接 import）。"""
    global _normalize
    if _normalize is None:
        import importlib.util
        path = os.path.join(HERE, "02_normalize.py")
        spec = importlib.util.spec_from_file_location("normalize_mod", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _normalize = mod
    return _normalize


def http_get_text(url):
    """GET 文本；失败返回 None。"""
    for attempt in range(RETRIES + 1):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
            resp = SYSTEM_OPENER.open(req, timeout=TIMEOUT)
            return decode_body(resp.read(16 * 1024 * 1024), resp.headers)
        except (HTTPError, Exception):
            if attempt < RETRIES:
                time.sleep(1.5)
    return None


def http_get_json(url):
    text = http_get_text(url)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def parse_list_page(html):
    """从列表页提取 [(id, 标题)]，按出现顺序、按 id 去重。"""
    out, seen = [], set()
    for m in re.finditer(r"/yuedu/shuyuans/content/id/(\d+)\.html\"[^>]*>([^<]{0,160})", html or ""):
        cid, title = m.group(1), _unescape(m.group(2))
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, title.strip()))
    return out


def _unescape(s):
    try:
        from html import unescape
        return unescape(s)
    except Exception:
        return s


def numeric_hint(title):
    """从标题抽「条数」提示值：优先 N万，其次最大数字；日期数字不计。"""
    t = title or ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*万", t)
    if m:
        return int(float(m.group(1)) * 10000)
    t2 = re.sub(r"\d+\s*[年月日]", "", t)          # 去掉 2026年11月07日
    nums = [int(x) for x in re.findall(r"\d+", t2)]
    return max(nums) if nums else 0


def pick_collections(items, top=10):
    """精选类合集筛选：大数量包 或 无数字的精选包，排除小包与泛泛标题。"""
    picked = []
    for cid, title in items:
        hint = numeric_hint(title)
        if hint >= MIN_TITLE_HINT:
            picked.append((cid, title))
        elif hint == 0 and TITLE_KEYWORD.search(title):
            picked.append((cid, title))
        if len(picked) >= top:
            break
    return picked


def dedupe_sources(rows):
    """两级去重：source_id（归一化后含 host+path，抓同站换皮） → content_hash（抓改名复用）。"""
    step1 = {}
    for s in rows:
        if not isinstance(s, dict) or not s.get("searchUrl"):
            continue
        key = _lib.source_id(s.get("bookSourceUrl") or "")
        if not key:
            continue
        step1.setdefault(key, s)
    step2 = {}
    for s in step1.values():
        step2.setdefault(_lib.content_hash(s), s)
    return list(step2.values())


def filter_adult(rows, strict=True):
    """成人过滤（strict 默认开）：组标签整组剔除 + 四字段强规则。

    返回 (kept, dropped)，dropped 元素为 (source, reason)。
    """
    mod = _normalize_mod()
    pats = mod.load_adult_patterns()
    kept, dropped = [], []
    for s in rows:
        hit, ev = mod.is_adult(s, pats)
        if hit:
            dropped.append((s, ev))
            continue
        if strict:
            cn = " ".join(str(s.get(f) or "") for f in CN_FIELDS)
            m = STRICT_CN.search(cn)
            if m:
                dropped.append((s, "strict_adult:" + m.group(0)))
                continue
            en = " ".join(str(s.get(f) or "") for f in EN_TEXT_FIELDS)
            m = STRICT_EN_TEXT.search(en)
            if m:
                dropped.append((s, "strict_adult:" + m.group(0)))
                continue
            m = STRICT_EN_URL.search(str(s.get("bookSourceUrl") or ""))
            if m:
                dropped.append((s, "strict_adult_url:" + m.group(0)))
                continue
        kept.append(s)
    return kept, dropped


def build_payload(sources, provenance, run_id):
    blob = json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {
        "run_id": run_id,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "upstream": "yckceo",
        "strategy": "manual_one_shot_top_collections",
        "source_count": len(sources),
        "sha256": hashlib.sha256(blob).hexdigest()[:16],
        "provenance": provenance,
        "sources": sources,
    }


def collect(pages=1, top=10, min_sources=DEFAULT_MIN_SOURCES,
            fetch_html=None, fetch_json=None, run_id=None, log=print):
    """采集主流程。fetch_html(page)->str / fetch_json(cid)->list 可注入（测试用）。"""
    fetch_html = fetch_html or (lambda p: http_get_text(LIST_URL % p))
    fetch_json = fetch_json or (lambda c: http_get_json(JSON_URL % c))
    run_id = run_id or datetime.now().strftime("yck-%Y%m%d")

    items = []
    for p in range(1, pages + 1):
        html = fetch_html(p)
        got = parse_list_page(html) if html else []
        log("[yckceo] 列表页 %d：%d 条" % (p, len(got)))
        items.extend(got)
        if not got:
            break
        time.sleep(REQUEST_DELAY)

    picked = pick_collections(items, top=top)
    log("[yckceo] 精选候选合集 %d 个 / 列表 %d 条" % (len(picked), len(items)))

    raw, provenance = [], []
    for cid, title in picked:
        arr = fetch_json(cid)
        n = len(arr) if isinstance(arr, list) else 0
        meta = {"kind": "collection", "id": cid, "name": title, "count": n,
                "url": JSON_URL % cid}
        if isinstance(arr, list) and n >= min_sources:
            for s in arr:
                if isinstance(s, dict):
                    s = dict(s)
                    s["_upstream"] = "yckceo:collection:%s" % cid
                    s["_upstream_name"] = title[:60]
                    raw.append(s)
            meta["taken"] = True
            log("  ✓ %-30s %4d 条" % (title[:30], n))
        elif isinstance(arr, list):
            meta["taken"] = False
            meta["skip"] = "低于 min_sources=%d" % min_sources
            log("  - %-30s %4d 条（低于阈值，跳过）" % (title[:30], n))
        else:
            meta["error"] = "非数组或请求失败"
            log("  ✗ %-30s 请求失败" % title[:30])
        provenance.append(meta)
        time.sleep(REQUEST_DELAY)

    log("[yckceo] 原始合并 %d 条" % len(raw))
    deduped = dedupe_sources(raw)
    log("[yckceo] 三级去重后 %d 条" % len(deduped))
    kept, dropped = filter_adult(deduped, strict=True)
    log("[yckceo] 成人过滤剔除 %d 条 → 保留 %d 条" % (len(dropped), len(kept)))
    for s, why in dropped[:10]:
        log("    - %-20s %s" % (str(s.get("bookSourceName"))[:20], why))

    return build_payload(kept, provenance, run_id)


def write_baseline(payload, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return path


def _arg(name, default, cast=int):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def main():
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
        return
    pages = _arg("--pages", 1)
    top = _arg("--top", 10)
    min_sources = _arg("--min-sources", DEFAULT_MIN_SOURCES)
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else DEFAULT_OUT
    dry = "--dry-run" in sys.argv

    payload = collect(pages=pages, top=top, min_sources=min_sources)
    if dry:
        print("[yckceo] --dry-run：不落盘，条数 %d" % payload["source_count"])
        return
    write_baseline(payload, out)
    print("[yckceo] 已写 %s（%d 条，sha256=%s，%.1f MB）"
          % (out, payload["source_count"], payload["sha256"], os.path.getsize(out) / 1e6))


if __name__ == "__main__":
    main()
