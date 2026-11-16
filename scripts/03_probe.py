# -*- coding: utf-8 -*-
"""书源探测：主指标用每个源自带的 checkKeyWord 测搜索链路；10 本书做书全交叉验证。

输入：runs/<run_id>/candidates.jsonl + probe_books.json（5 经典 + 5 热门）
输出：runs/<run_id>/matrix.jsonl（每源一行）

判活口径（与方案 §17.1 对齐）：
- 只对 risk_level==clean 的源执行（quarantine 不碰，绝不主动执行可疑 JS）。
- 主指标 checkKeyWord：取源自带校验词（缺省「我的」），构造搜索并判定响应里是否
  含该词 —— 这是源作者保证该站必能搜到的词，用它判「连接正常/搜索有效」最公平。
- 辅助指标：5 经典 + 5 热门逐本搜，记录命中率，只做书全交叉验证，不单独淘汰。
- 403/验证码/登录页 → BLOCKED（与 FAILED 分开，防机房 IP 冤杀）。
- 动态 JS / 无 key 占位 → UNKNOWN，不误记 FAILED。
- 超时 12s、重试 1、每源 10 本书 + 1 次校验词、限速 0.3s。
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

from lib import (DIRECT, UA, decode_body, dump_jsonl, load_json, load_jsonl,
                 parse_search_url, run_dir)

TIMEOUT = 12
MAX_WORKERS = 6
DELAY_BETWEEN = 0.3
DEFAULT_KEY = "我的"   # legado_NG CheckSource 默认校验词

BLOCKED_PAT = re.compile(
    r"验证码|captcha|访问验证|请开启javascript|cloudflare|just a moment|"
    r"安全验证|访问被拒绝|403 forbidden|请输入验证码", re.I)
LOGIN_PAT = re.compile(r"请先登录|登录查看|登录后|login\.php|user/login", re.I)


def probe_books(books_obj):
    """返回 [(tier, title)]，经典 + 热门各取配置全部。"""
    out = []
    for tier in ("classic", "hot"):
        for name in books_obj.get(tier, []):
            if str(name).strip():
                out.append((tier, str(name).strip()))
    return out


def check_keyword(source):
    """取源自带校验词，缺省「我的」。"""
    kw = str(source.get("checkKeyWord", "") or "").strip()
    return kw or DEFAULT_KEY


def main_book(title):
    t = re.split(r"[:：]", title)[0]
    t = re.sub(r"[\(（【\[].*?[\)）】\]]", "", t).strip()
    return t or title


def one_search(source, keyword):
    """执行一次搜索，返回 (status, hit, respond_ms, detail)。
    status: PASSED（拿到响应）/ BLOCKED / FAILED / UNKNOWN（无法构造）。
    hit: 响应文本是否含关键词。
    """
    parsed = parse_search_url(source, keyword)
    if parsed is None:
        return "UNKNOWN", False, 0, "dynamic_js_or_no_key"
    url, method, body = parsed
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    try:
        data = body.encode("utf-8") if body else None
        req = Request(url, method=method, data=data, headers=headers)
        t0 = time.time()
        resp = DIRECT.open(req, timeout=TIMEOUT)
        raw = resp.read(500 * 1024)
        ms = int((time.time() - t0) * 1000)
        text = decode_body(raw, resp.headers)
        if BLOCKED_PAT.search(text) or LOGIN_PAT.search(text):
            return "BLOCKED", False, ms, "captcha_or_login"
        main_name = main_book(keyword)
        hit = (keyword in text) or (main_name in text and len(main_name) >= 2)
        return "PASSED", hit, ms, "hit" if hit else "no_result"
    except HTTPError as e:
        if e.code in (403, 429, 302, 303):
            return "BLOCKED", False, 0, "http_%d" % e.code
        return "FAILED", False, 0, "http_%d" % e.code
    except Exception as e:
        return "FAILED", False, 0, type(e).__name__


def probe_one(rec, books):
    """探测单源，返回矩阵行。"""
    sid = rec["source_id"]
    source = rec["source"]
    kw = check_keyword(source)

    # 主指标：checkKeyWord 搜索
    st, hit, ms, detail = one_search(source, kw)
    search_ok = (st == "PASSED" and hit)
    if st == "PASSED" and not hit:
        st = "FAILED"   # 搜索通了但连自带校验词都搜不到 → 搜索规则失效
    search_status, search_detail, search_ms = st, detail, ms

    # 辅助指标：10 本书交叉验证（仅在搜索链路可用时做，避免浪费请求）
    tier_hits = {"classic": [0, 0], "hot": [0, 0]}
    titles_hit = []
    book_times = []
    if search_ok:
        for tier, title in books:
            bst, bhit, bms, _ = one_search(source, title)
            tier_hits[tier][1] += 1
            if bhit:
                tier_hits[tier][0] += 1
                titles_hit.append(title)
            if bms:
                book_times.append(bms)
            time.sleep(DELAY_BETWEEN)

    all_times = ([search_ms] if search_ms else []) + book_times
    overall = search_status  # 主指标决定整体状态

    def rate(t):
        h, a = tier_hits[t]
        return round(h / a, 3) if a else None

    return {
        "source_id": sid,
        "domain": rec["domain"],
        "bookSourceName": rec["bookSourceName"],
        "content_hash": rec["content_hash"],
        "risk_level": rec["risk_level"],
        "check_keyword": kw,
        "status": overall,
        "search_ok": search_ok,
        "search_status": search_status,
        "search_detail": search_detail,
        "fail_stage": "" if search_ok else ("SEARCH:" + search_detail),
        "respond_time_ms": int(sum(all_times) / len(all_times)) if all_times else 0,
        "search_time_ms": search_ms,
        "probe_count": 1 + sum(tier_hits[t][1] for t in tier_hits),
        "hit_classic": rate("classic"),
        "hit_hot": rate("hot"),
        "hits": {t: tier_hits[t][0] for t in tier_hits},
        "attempts": {t: tier_hits[t][1] for t in tier_hits},
        "hit_titles": titles_hit,
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    if len(sys.argv) < 2:
        print("用法: python 03_probe.py <run_id> [--limit N]")
        sys.exit(1)
    run_id = sys.argv[1]
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    out_dir = run_dir(run_id)
    cands = load_jsonl(os.path.join(out_dir, "candidates.jsonl"))
    books_obj = load_json(os.path.join(out_dir, "probe_books.json"))
    clean = [r for r in cands if r["risk_level"] == "clean" and r["source"].get("searchUrl")]
    if limit:
        clean = clean[:limit]
    books = probe_books(books_obj)
    print("[探测] 候选=%d（仅 clean + 有 searchUrl），探针书=%d（经典%d+热门%d），并发=%d"
          % (len(clean), len(books),
             len(books_obj.get("classic", [])), len(books_obj.get("hot", [])), MAX_WORKERS))
    if not clean or not books:
        print("[探测] 无可探测对象，终止")
        sys.exit(1)

    t_start = time.time()
    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(probe_one, rec, books): rec["source_id"] for rec in clean}
        for fut in as_completed(futs):
            try:
                rows.append(fut.result())
            except Exception as e:
                rows.append({"source_id": futs[fut], "status": "FAILED",
                             "search_ok": False, "fail_stage": "PROBE_EXCEPTION:" + type(e).__name__})
            done += 1
            if done % 10 == 0:
                print("  进度 %d/%d" % (done, len(clean)))
    elapsed = time.time() - t_start

    dump_jsonl(os.path.join(out_dir, "matrix.jsonl"), rows)
    from collections import Counter
    c = Counter(r.get("status") for r in rows)
    print("[探测] 完成：%s；用时 %.1fs（%.2fs/源）"
          % (dict(c), elapsed, elapsed / max(1, len(clean))))
    print("  输出:", os.path.join(out_dir, "matrix.jsonl"))


if __name__ == "__main__":
    main()
