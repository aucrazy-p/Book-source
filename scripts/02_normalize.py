# -*- coding: utf-8 -*-
"""书源规范化 + 三级去重 + 静态风险过滤。

输入：书源 JSON（数组）路径；输出：runs/<run_id>/candidates.jsonl + 去重统计。
不修改输入文件。纯标准库。

三级去重：source_id 精确 → 同 host 聚合 → content_hash 抓换皮。
静态风险：复用 book-source-validator 的黑名单作种子（只读）；字段白名单拒收
非空 jsLib / loginUi / loginCheckJs / 带 Cookie 的 header（标 quarantine，不进探测）。
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 仓库内种子优先（CI 上唯一可用），本地技能文件作为补充——两处都读、合并去重。
SEED_LOCAL_SKILL = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills", "book-source-validator",
    "scripts", "blacklist.py")
SEED_REPO = os.path.join(ROOT, "conf", "blacklist_seeds.txt")
# 成人/色情源过滤规则（名称+URL 强特征，分组弱特征）
ADULT_FILTER = os.path.join(ROOT, "conf", "adult_filter.txt")

from lib import (CONTENT_FIELDS, content_hash, dump_jsonl, dump_json,
                 extract_domain, load_json, normalize_url, run_dir, source_id)

# 已知平台（阅读 APP 内置，不需要自建源）——与技能 is_known_platform 对齐的精简版
KNOWN_PLATFORMS = (
    "qidian.com", "qq.com", "yuewen.com", "tom.com", "jd.com", "dangdang.com",
    "douban.com", "zhihu.com", "weixin.qq.com", "xiaomi.com", "baidu.com",
)

RISK_HEADER = re.compile(r"cookie|authorization|token|bearer", re.I)
DANGEROUS_FIELDS = ("jsLib", "loginUi", "loginCheckJs")


def load_seed_blacklist():
    """合并仓库内 conf 种子 + 本地技能 blacklist.py（只读抽取，不 import）。"""
    domains = set()
    # 1) 仓库内种子（每行一个域名，# 开头为注释）——CI 与本地都可读
    if os.path.exists(SEED_REPO):
        try:
            for line in open(SEED_REPO, encoding="utf-8"):
                d = line.strip().lower()
                if d and not d.startswith("#") and "." in d and " " not in d:
                    domains.add(d)
        except Exception:
            pass
    # 2) 本地技能 blacklist.py 的字符串字面量——仅补充，缺失不影响
    if os.path.exists(SEED_LOCAL_SKILL):
        try:
            text = open(SEED_LOCAL_SKILL, encoding="utf-8").read()
            for m in re.finditer(r"""['"]([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})['"]""", text, re.I):
                d = m.group(1).lower()
                if "." in d and not d.startswith("http"):
                    domains.add(d)
        except Exception:
            pass
    return domains, bool(domains)


def is_known(domain):
    return any(domain.endswith(k) for k in KNOWN_PLATFORMS)


def is_blacklisted(domain, seeds):
    return any(domain == b or domain.endswith("." + b) for b in seeds)


# 分组标签里的成人标记——只对 bookSourceGroup 判定，避免误伤正文小说
# 说明：按分隔符拆成单个标签后逐条判定，命中任一标签即整条源剔除（组级整组剔除）。
WEAK_GROUP = re.compile(r"涩|🔞|成人|r18|敏感资源|绅士|刘备|里番|禁书|肉文|po18|海棠|废文", re.I)
# 独立短标签：必须整标签相等，避免 H 误伤 HTML/HD、18 误伤 1800
EXACT_GROUP = {"h", "h+sp", "hsp", "18", "18+", "18禁", "r18", "成人"}
GROUP_SEP = re.compile(r"[,，;；|/、\s]+")


def load_adult_patterns():
    """读 conf/adult_filter.txt 里的正则（# 注释、空行忽略）。"""
    pats = []
    if os.path.exists(ADULT_FILTER):
        for line in open(ADULT_FILTER, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line)
    return pats


def is_adult(s, pats):
    """(是否成人源, 命中证据)。名称+URL 命中强规则，或分组命中弱规则。

    分组按分隔符拆成单个标签逐条判定：任一标签命中成人标记，整条源即剔除。
    """
    blob = (str(s.get("bookSourceName", "") or "") + " " +
            str(s.get("bookSourceUrl", "") or "")).lower()
    for p in pats:
        if re.search(p, blob, re.I):
            return True, "adult_pattern:" + p
    grp = str(s.get("bookSourceGroup", "") or "")
    if grp:
        for tag in GROUP_SEP.split(grp):
            tag = tag.strip()
            if not tag:
                continue
            if tag.lower() in EXACT_GROUP:
                return True, "adult_group:" + tag
            m = WEAK_GROUP.search(tag)
            if m:
                return True, "adult_group:" + m.group(0)
    return False, ""


def risk_scan(s):
    """静态字段风险扫描，返回 (risk_level, findings[])。"""
    findings = []
    for f in DANGEROUS_FIELDS:
        if str(s.get(f, "") or "").strip():
            findings.append({"rule_id": "L1_DANGEROUS_FIELD", "field": f,
                             "evidence": str(s.get(f))[:120]})
    hdr = str(s.get("header", "") or "")
    if RISK_HEADER.search(hdr):
        findings.append({"rule_id": "L1_CREDENTIAL_HEADER", "field": "header",
                         "evidence": hdr[:120]})
    # 规则字段里的高危 API（外带/拉起/解码执行）
    joined = " ".join(str(s.get(k, "") or "") for k in CONTENT_FIELDS)
    for pat, rid in (
        (r"getCookie", "L2_COOKIE_READ"),
        (r"startBrowser", "L2_START_BROWSER"),
        (r"webView", "L2_WEBVIEW"),
        (r"base64Decode|aesDecodeToString", "L2_DECODE"),
        (r"\beval\s*\(", "L2_EVAL"),
    ):
        if re.search(pat, joined, re.I):
            findings.append({"rule_id": rid, "evidence": "matched pattern " + pat})
    if findings:
        return "quarantine", findings
    return "clean", findings


def normalize(input_path, run_id):
    raw = load_json(input_path)
    if isinstance(raw, dict):
        raw = raw.get("sources") or raw.get("bookSources") or []
    seeds, seed_ok = load_seed_blacklist()
    adult_pats = load_adult_patterns()
    print("[规范化] 输入=%d 条，种子黑名单=%d 条（%s），成人规则=%d 条"
          % (len(raw), len(seeds), "已加载" if seed_ok else "未加载", len(adult_pats)))

    seen_sid, seen_hash, host_groups = set(), set(), defaultdict(list)
    candidates, stats = [], defaultdict(int)

    for s in raw:
        if not isinstance(s, dict):
            stats["skip_nonobj"] += 1
            continue
        if str(s.get("bookSourceType", "0")) not in ("0", ""):
            stats["skip_type"] += 1
            continue
        adult, evidence = is_adult(s, adult_pats)
        if adult:
            stats["adult_filtered"] += 1
            continue
        url = str(s.get("bookSourceUrl", "") or "").strip()
        sid = source_id(url)
        if not sid or sid in seen_sid:
            stats["dup_source_id"] += 1
            continue
        seen_sid.add(sid)
        domain = extract_domain(url)
        if not domain:
            stats["no_domain"] += 1
            continue
        if is_blacklisted(domain, seeds):
            stats["blacklist"] += 1
            continue
        if is_known(domain):
            stats["known_platform"] += 1
            continue

        chash = content_hash(s)
        risk, findings = risk_scan(s)
        rec = {
            "source_id": sid,
            "normalized_url": normalize_url(url),
            "domain": domain,
            "content_hash": chash,
            "bookSourceName": s.get("bookSourceName", ""),
            "bookSourceUrl": url,
            "risk_level": risk,
            "findings": findings,
            "source": s,
        }
        # 同 host 聚合：记录但不立即丢弃，保留第一条（后面按 hash 再筛）
        host_groups[domain].append(rec)
        candidates.append(rec)
        stats["kept"] += 1

    # content_hash 抓换皮：同 host 内相同 hash 只留一条
    final, seen_pair = [], set()
    for rec in candidates:
        key = (rec["domain"], rec["content_hash"])
        if key in seen_pair:
            stats["dup_content_hash"] += 1
            continue
        seen_pair.add(key)
        final.append(rec)

    out_dir = run_dir(run_id)
    os.makedirs(out_dir, exist_ok=True)
    dump_jsonl(os.path.join(out_dir, "candidates.jsonl"), final)
    dump_json(os.path.join(out_dir, "normalize_stats.json"), {
        "run_id": run_id, "input": len(raw), "output": len(final),
        "stats": dict(stats), "at": datetime.now(timezone.utc).isoformat(),
    })
    clean = sum(1 for r in final if r["risk_level"] == "clean")
    quar = len(final) - clean
    print("[规范化] 去重后=%d（clean=%d quarantine=%d）"
          % (len(final), clean, quar))
    for k in sorted(stats):
        print("   %s: %d" % (k, stats[k]))
    print("  输出:", os.path.join(out_dir, "candidates.jsonl"))
    print("  [提示] quarantine 源不进探测队列；改判定规则需人工复核放行。")
    return final


def main():
    if len(sys.argv) < 3:
        print("用法: python 02_normalize.py <书源JSON路径> <run_id>")
        sys.exit(1)
    normalize(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
