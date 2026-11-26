# -*- coding: utf-8 -*-
"""从 Yiove 书源仓库采集候选源快照（不做全量 3 万条）。

数据源（实测 2026-11-16 可用）：
  合集列表 GET https://shuyuan-api.yiove.com/shuyuan/book-source-collections?page=1&page_size=N
  合集直链 GET https://shuyuan-api.yiove.com/import/book-source-collection/<uuid>  → 标准 legado JSON 数组
  单源列表 GET https://shuyuan-api.yiove.com/shuyuan/book-sources?page=1&page_size=N
    字段：{id,name,url,description,is_valid,create_time,view_total}

策略（用户要求：不测 3 万源、少量够用）：
- 默认只取「合集」：按 view_total 排序取前 N 个合集（默认 8），每个合集取其直链 JSON。
- 可选 --single N：补取单源列表 is_valid=true 且 view_total 高的前 N 条（默认 0，不取）。
- 遵守速率限制：每次请求间隔 REQUEST_DELAY，失败重试 2 次。
- 落快照 runs/<run_id>/sources_snapshot.json，附每个上游的 URL/条数/sha256/抓取时间/HTTP 状态。

用法：python collect_yiove.py <run_id> [--collections 8] [--single 0] [--pages 1]
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

from lib import SYSTEM_OPENER, UA, decode_body, dump_json, run_dir, source_id

# 候选种子兜底：上游采集全失败（如 GitHub Actions 机房 IP 被 yiove 403）时，
# 用上次成功快照的 gzip 种子继续跑，避免池空转。种子只做兜底，正常采集优先。
SEED_PATH = os.path.join(ROOT, "conf", "candidate_seed.json.gz")
# yckceo 精选源静态基线：一次性采集后提交仓库，每轮直接读，不重抓。
# 由 scripts/collect_yckceo.py 生成；文件不存在时合并逻辑自动跳过（零影响）。
YCK_BASELINE = os.path.join(ROOT, "conf", "yck_baseline.json.gz")

API = "https://shuyuan-api.yiove.com"
COLS = API + "/shuyuan/book-source-collections"
SINGLES = API + "/shuyuan/book-sources"
IMPORT = API + "/import/book-source-collection/"

# 精品源补给：固定大合集 UUID（16724 条，2025-08；按 view_total 排序时排十几名被漏采）。
# 23041 条那个本机代理 180s 超时放弃，不采；单源库接口 78% 被本合集覆盖，不接。
PREMIUM_COLLECTION_ID = "df898047-f6f3-4437-93c6-147edc8559b3"
PREMIUM_MAX_BYTES = 256 * 1024 * 1024   # 大合集导出实测 89MB
PREMIUM_TIMEOUT = 180                   # 实测下载约 84s，留余量
ADULT_FILTER_PY = os.path.join(HERE, "02_normalize.py")

REQUEST_DELAY = 0.8
RETRIES = 2
TIMEOUT = 30


def get_json(url, max_bytes=8 * 1024 * 1024, timeout=TIMEOUT):
    """GET 并解析 JSON，返回 (obj, meta)；失败返回 (None, meta)。

    默认参数保持常规采集行为不变（8MB / 30s）。大合集导出（89MB / 84s）
    由 premium 专用路径显式放大 max_bytes / timeout，不影响普通合集列表。
    """
    last = ""
    for attempt in range(RETRIES + 1):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            t0 = time.time()
            resp = SYSTEM_OPENER.open(req, timeout=timeout)
            raw = resp.read(max_bytes)
            text = decode_body(raw, resp.headers)
            obj = json.loads(text)
            return obj, {"url": url, "http": resp.status, "bytes": len(raw),
                         "at": datetime.now(timezone.utc).isoformat(),
                         "ms": int((time.time() - t0) * 1000)}
        except HTTPError as e:
            last = "HTTP %d" % e.code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:80])
        if attempt < RETRIES:
            time.sleep(1.5)
        time.sleep(REQUEST_DELAY)
    return None, {"url": url, "error": last, "at": datetime.now(timezone.utc).isoformat()}


def load_seed():
    """读 gzip 种子，返回 (sources, meta)；种子缺失/损坏返回 ([], meta)。"""
    meta = {"kind": "seed", "path": SEED_PATH, "at": datetime.now(timezone.utc).isoformat()}
    if not os.path.exists(SEED_PATH):
        meta["error"] = "seed file missing"
        return [], meta
    try:
        with gzip.open(SEED_PATH, "rb") as f:
            obj = json.loads(f.read().decode("utf-8"))
        sources = obj.get("sources", []) if isinstance(obj, dict) else []
        sources = [s for s in sources if isinstance(s, dict) and s.get("bookSourceUrl")]
        for s in sources:
            s["_upstream_original"] = s.get("_upstream", "")
            s["_upstream"] = "yiove:seed"
            s["_seed_fallback"] = True
        meta.update({"count": len(sources), "collected_at": obj.get("collected_at"),
                     "sha256": hashlib.sha256(
                         json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
                     ).hexdigest()[:16]})
        return sources, meta
    except Exception as e:
        meta["error"] = "%s: %s" % (type(e).__name__, str(e)[:80])
        return [], meta


def dedupe_by_source_id(sources, log=print):
    """按 source_id 去重，合并「同站换皮包」。

    上游合集里大量存在同站不同署名的重复打包（URL 尾部只差 #妍希 / #md 之类），
    normalize_url 会把它们归一到同一个 source_id。不合并的话探测预算会浪费在
    同一个站上——实测 yiove 1740 条里有 681 条属此类（占 39%）。

    保留先出现者：合集按 view_total 降序排列，靠前的合集质量更好。
    返回 (去重后列表, [(被丢弃的源, 证据)])；无 URL 的条目原样保留、不参与去重。
    """
    seen, out, dropped = {}, [], []
    for s in sources:
        if not isinstance(s, dict):
            out.append(s)
            continue
        url = str(s.get("bookSourceUrl") or "").strip()
        if not url:
            out.append(s)            # 无 URL 无法判定同一性，不丢数据
            continue
        key = source_id(url)
        prev = seen.get(key)
        if prev is not None:
            dropped.append((s, "same_source_id:%s~%s" % (
                str(prev.get("bookSourceName") or "")[:16],
                str(s.get("bookSourceName") or "")[:16])))
            continue
        seen[key] = s
        out.append(s)
    if dropped:
        log("[采集] 按 source_id 去重 -%d 条（同站换皮，保留先出现者）" % len(dropped))
    return out, dropped


def merge_yck_baseline(sources, baseline_path=None, log=print):
    """把 yckceo 静态基线并入候选源列表，按 source_id 去重。

    基线缺失/损坏一律不报错、原样返回 —— 这是可选增强，不能成为采集的单点故障。
    返回 (新列表, meta)；meta 进 provenance，带基线 sha256 便于溯源。
    """
    path = baseline_path or YCK_BASELINE
    meta = {"kind": "yck_baseline", "path": path,
            "at": datetime.now(timezone.utc).isoformat(),
            "baseline_count": 0, "added": 0, "overlap": 0}
    if not os.path.exists(path):
        meta["error"] = "baseline missing"
        return sources, meta
    try:
        with gzip.open(path, "rb") as f:
            obj = json.loads(f.read().decode("utf-8"))
    except Exception as e:
        meta["error"] = "%s: %s" % (type(e).__name__, str(e)[:80])
        return sources, meta

    rows = obj.get("sources", []) if isinstance(obj, dict) else []
    if isinstance(obj, dict):
        meta["sha256"] = obj.get("sha256", "")
        meta["collected_at"] = obj.get("collected_at", "")
    meta["baseline_count"] = len(rows)

    seen = {source_id(s.get("bookSourceUrl") or "") for s in sources}
    out = list(sources)
    for s in rows:
        if not isinstance(s, dict):
            continue
        key = source_id(s.get("bookSourceUrl") or "")
        if not key:
            continue
        if key in seen:
            meta["overlap"] += 1
            continue
        seen.add(key)
        s = dict(s)
        s["_upstream"] = "yckceo:baseline"
        s["_is_valid"] = True
        out.append(s)
        meta["added"] += 1
    meta["total"] = len(out)
    log("[采集] 合并 yckceo 基线 +%d 条（基线 %d / 重叠 %d）"
        % (meta["added"], meta["baseline_count"], meta["overlap"]))
    return out, meta


def _premium_adult_filter():
    """复用 02_normalize 的成人规则（单一真值），加载失败则恒 False 不拖垮筛选。"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_norm02_premium", ADULT_FILTER_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pats = mod.load_adult_patterns()
        return (lambda s: mod.is_adult(s or {}, pats)[0]), len(pats)
    except Exception as e:
        print("[premium] 警告：成人规则加载失败（%s），本轮跳过成人过滤" % e)
        return (lambda s: False), 0


def premium_filter(sources, log=print):
    """大合集导出 → 精选标准源：过滤 → 成人剔除 → 域名去重 → 按更新时间降序。

    只留 searchUrl 非空 + bookSourceUrl 以 http 开头 + 无 jsLib 的标准源
    （探测内核只执行 HTTP+选择器，跑不了 JS 源）；成人命中 02_normalize 规则剔除；
    同站换皮按 source_id 去重；按 lastUpdateTime 降序让新源先探。
    返回 (精选列表, meta)，任何一步失败都降级返回已处理结果，不抛异常。
    """
    meta = {"before": len(sources), "after": 0, "dropped_js": 0,
            "dropped_nosearch": 0, "dropped_nonhttp": 0,
            "dropped_adult": 0, "dropped_dup": 0}
    if not isinstance(sources, list):
        meta["error"] = "payload not a list"
        return [], meta

    # 1) 结构过滤
    kept = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        url = str(s.get("bookSourceUrl") or "").strip()
        if not url.lower().startswith("http"):
            meta["dropped_nonhttp"] += 1
            continue
        if s.get("jsLib"):
            meta["dropped_js"] += 1
            continue
        if not str(s.get("searchUrl") or "").strip():
            meta["dropped_nosearch"] += 1
            continue
        kept.append(s)

    # 2) 成人过滤（复用真实规则）
    is_adult, n_rules = _premium_adult_filter()
    meta["adult_rules"] = n_rules
    filtered = []
    for s in kept:
        if is_adult(s):
            meta["dropped_adult"] += 1
        else:
            filtered.append(s)

    # 3) 同站换皮去重（复用现成 source_id 口径）
    deduped, dropped = dedupe_by_source_id(filtered, log=log)
    meta["dropped_dup"] = len(dropped)

    # 4) 按更新时间降序：新源在前，max() 兜底脏时间戳（字符串/None/浮点）
    def lut(s):
        try:
            return int(s.get("lastUpdateTime") or 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    deduped.sort(key=lut, reverse=True)
    meta["after"] = len(deduped)
    log("[premium] 筛选 %d→%d（JS %d / 无搜索 %d / 非HTTP %d / 成人 %d / 换皮 %d）"
        % (meta["before"], meta["after"], meta["dropped_js"],
           meta["dropped_nosearch"], meta["dropped_nonhttp"],
           meta["dropped_adult"], meta["dropped_dup"]))
    return deduped, meta


def load_premium_pool(path):
    """从 premium_pool.jsonl 读回缓存；文件缺失/损坏返回 []（不报错，触发重下）。"""
    if not os.path.exists(path):
        return []
    try:
        from lib import load_jsonl
        return [r for r in load_jsonl(path) if isinstance(r, dict) and r.get("bookSourceUrl")]
    except Exception as e:
        print("[premium] 缓存读取失败（%s），将重新下载" % str(e)[:80])
        return []


def fetch_premium_collection(log=print):
    """下载固定大合集 → premium_filter 精选；失败返回 ([], meta) 降级不报错。"""
    url = IMPORT + PREMIUM_COLLECTION_ID
    meta = {"kind": "premium", "collection_id": PREMIUM_COLLECTION_ID, "url": url}
    obj, fetch_meta = get_json(url, max_bytes=PREMIUM_MAX_BYTES, timeout=PREMIUM_TIMEOUT)
    meta.update(fetch_meta)
    if not isinstance(obj, list):
        meta["error"] = meta.get("error") or "payload not a list"
        log("[premium] 大合集下载失败/非数组，跳过补给：%s" % meta["error"])
        return [], meta
    sources, filter_meta = premium_filter(obj, log=log)
    meta["filter"] = filter_meta
    meta["count"] = len(sources)
    log("[premium] 大合集精选 %d 条（原始 %d）" % (len(sources), len(obj)))
    return sources, meta


def merge_premium_pool(sources, premium, log=print):
    """把 premium 源并入常规候选，按 source_id 去重（常规优先，premium 只补新的）。

    打 _upstream=yiove:premium:<uuid> + _is_valid=True 便于 provenance 溯源。
    返回 (合并后列表, meta)。
    """
    meta = {"kind": "premium_merge", "regular": len(sources),
            "premium": len(premium), "added": 0, "overlap": 0}
    # premium 必须排在前面：探测器 06_pool.plan 按数组顺序从头取 BATCH 条，
    # 且池满后转 recheck 不再读候选。若 premium 追加在常规候选之后，
    # 1623 条常规旧源会把 6587 条 premium 新源挡在后面 → 预算内永远探不到，
    # lastUpdateTime 排序白做、补给形同虚设。故 premium 优先，常规让位。
    premium_out = []
    seen = set()
    for s in premium:
        if not isinstance(s, dict):
            continue
        key = source_id(s.get("bookSourceUrl") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        s = dict(s)
        s["_upstream"] = "yiove:premium:" + PREMIUM_COLLECTION_ID
        s["_is_valid"] = True
        premium_out.append(s)
        meta["added"] += 1
    regular_out = []
    for s in sources:
        key = source_id(s.get("bookSourceUrl") or "")
        if key and key in seen:
            meta["overlap"] += 1
            continue
        if key:
            seen.add(key)
        regular_out.append(s)
    out = premium_out + regular_out
    meta["total"] = len(out)
    log("[premium] 并入候选池（premium %d 条前置 / 与常规重叠 %d 条）"
        % (meta["added"], meta["overlap"]))
    return out, meta


def collect(run_id, n_cols, n_single, pages, premium=False):
    d = run_dir(run_id)
    os.makedirs(d, exist_ok=True)
    merged, provenance = {}, []
    premium_path = os.path.join(d, "premium_pool.jsonl")

    # 1) 合集列表 → 取高访问量的前 n_cols 个
    col_items = []
    for p in range(1, pages + 1):
        obj, meta = get_json("%s?page=%d&page_size=20" % (COLS, p))
        if obj and isinstance(obj.get("items"), list):
            col_items.extend(obj["items"])
        provenance.append({"kind": "collection_list", **meta, "count": len(obj.get("items", [])) if obj else 0})
        if not obj:
            break
    col_items.sort(key=lambda x: x.get("view_total") or 0, reverse=True)
    col_items = col_items[:n_cols]
    # 把上游错误打到 stdout（原先只写进 provenance，日志静默难排查）
    list_errors = [p.get("error") for p in provenance
                   if p.get("kind") == "collection_list" and p.get("error")]
    if list_errors:
        print("[采集] 警告：合集列表请求失败 → %s" % "; ".join(list_errors))
    print("[采集] 合集候选 %d 个（按访问量取前 %d）" % (len(col_items), n_cols))

    for c in col_items:
        cid = c.get("id")
        name = str(c.get("name", ""))[:60]
        obj, meta = get_json(IMPORT + str(cid))
        n = len(obj) if isinstance(obj, list) else 0
        meta.update({"kind": "collection", "id": cid, "name": name, "total_claim": c.get("total")})
        provenance.append(meta)
        if isinstance(obj, list):
            for s in obj:
                if not isinstance(s, dict):
                    continue
                url = str(s.get("bookSourceUrl", "") or "").strip()
                if not url:
                    continue
                s["_upstream"] = "yiove:collection:%s" % cid
                s["_upstream_name"] = name
                s["_is_valid"] = True   # 合集为站方收录，视为已判活候选
                merged[url] = s
            print("  ✓ %-40s 取回 %d 条" % (name[:40], n))
        else:
            print("  ✗ %-40s 失败: %s" % (name[:40], meta.get("error", "非数组")))

    # 2) 可选：单源列表里 is_valid=true 的高访问量条目（仅元数据，无规则，需另行取直链）
    if n_single > 0:
        obj, meta = get_json("%s?page=1&page_size=50" % SINGLES)
        provenance.append({"kind": "single_list", **meta})
        if obj and isinstance(obj.get("items"), list):
            singles = [x for x in obj["items"] if x.get("is_valid") is True]
            singles.sort(key=lambda x: x.get("view_total") or 0, reverse=True)
            meta["valid_count"] = len(singles)
            meta["taken"] = len(singles[:n_single])
            print("[采集] 单源 is_valid=%d，仅记录元数据前 %d（无规则体，不直接探测）"
                  % (len(singles), n_single))

    sources = list(merged.values())
    strategy = "top_collections_by_view_total"

    # 精品源补给（--premium）：从固定大合集筛出的精选源并入候选池。
    # 优先读 Actions cache 落盘的 premium_pool.jsonl；缺失才联网下载并落盘。
    # 任何失败都降级（返回 []），常规采集照常，不成为 daily 单点故障。
    if premium:
        premium_sources = load_premium_pool(premium_path)
        if premium_sources:
            pmeta = {"kind": "premium_cache", "path": premium_path,
                     "count": len(premium_sources), "from_cache": True}
            print("[premium] 命中缓存 %d 条 → %s" % (len(premium_sources), premium_path))
        else:
            premium_sources, pmeta = fetch_premium_collection()
            if premium_sources:
                from lib import dump_jsonl
                dump_jsonl(premium_path, premium_sources)
                print("[premium] 大合集结果落盘 %d 条 → %s"
                      % (len(premium_sources), premium_path))
        provenance.append(pmeta)
        if premium_sources:
            sources, merge_meta = merge_premium_pool(sources, premium_sources)
            provenance.append(merge_meta)
            strategy += "+premium"

    if not sources:
        # 上游全失败（如 CI 机房 IP 被 403）：回退种子，保证池不空转、流程能跑通。
        seed_sources, seed_meta = load_seed()
        provenance.append(seed_meta)
        if seed_sources:
            sources = seed_sources
            strategy = "seed_fallback"
            print("[采集] 上游 0 条 → 回退种子 %d 条（采于 %s）"
                  % (len(sources), seed_meta.get("collected_at", "?")))
        else:
            print("[采集] 上游 0 条，且种子不可用：%s" % seed_meta.get("error", "未知"))

    # 3) 按 source_id 去重：合并同站换皮包（与第 1 步的 URL 精确去重是两回事）
    n_before = len(sources)
    sources, dup_dropped = dedupe_by_source_id(sources)
    provenance.append({"kind": "dedupe_source_id", "before": n_before,
                       "after": len(sources), "dropped": len(dup_dropped),
                       "samples": [ev for _, ev in dup_dropped[:10]]})

    # 4) 合并 yckceo 静态基线（本地文件，不联网；缺失则自动跳过）
    sources, yck_meta = merge_yck_baseline(sources)
    provenance.append(yck_meta)
    if yck_meta.get("added"):
        strategy += "+yck_baseline"

    payload = {
        "run_id": run_id,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "upstream": "yiove",
        "strategy": strategy,
        "source_count": len(sources),
        "provenance": provenance,
        "sources": sources,
    }
    path = os.path.join(d, "sources_snapshot.json")
    dump_json(path, payload)
    blob = json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
    print("[采集] 合并去重后 %d 条 → %s" % (len(sources), path))
    print("  snapshot sha256=%s" % hashlib.sha256(blob).hexdigest()[:16])
    return path


def main():
    if len(sys.argv) < 2:
        print("用法: python collect_yiove.py <run_id> [--collections 8] [--single 0] "
              "[--pages 1] [--premium]")
        sys.exit(1)
    run_id = sys.argv[1]

    def arg(name, default):
        return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default

    collect(run_id, arg("--collections", 8), arg("--single", 0), arg("--pages", 1),
            premium="--premium" in sys.argv)


if __name__ == "__main__":
    main()
