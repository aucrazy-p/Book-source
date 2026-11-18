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

from lib import SYSTEM_OPENER, UA, decode_body, dump_json, run_dir

# 候选种子兜底：上游采集全失败（如 GitHub Actions 机房 IP 被 yiove 403）时，
# 用上次成功快照的 gzip 种子继续跑，避免池空转。种子只做兜底，正常采集优先。
SEED_PATH = os.path.join(ROOT, "conf", "candidate_seed.json.gz")

API = "https://shuyuan-api.yiove.com"
COLS = API + "/shuyuan/book-source-collections"
SINGLES = API + "/shuyuan/book-sources"
IMPORT = API + "/import/book-source-collection/"

REQUEST_DELAY = 0.8
RETRIES = 2
TIMEOUT = 30


def get_json(url):
    """GET 并解析 JSON，返回 (obj, meta)；失败返回 (None, meta)。"""
    last = ""
    for attempt in range(RETRIES + 1):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            t0 = time.time()
            resp = SYSTEM_OPENER.open(req, timeout=TIMEOUT)
            raw = resp.read(8 * 1024 * 1024)
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


def collect(run_id, n_cols, n_single, pages):
    d = run_dir(run_id)
    os.makedirs(d, exist_ok=True)
    merged, provenance = {}, []

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
        print("用法: python collect_yiove.py <run_id> [--collections 8] [--single 0] [--pages 1]")
        sys.exit(1)
    run_id = sys.argv[1]

    def arg(name, default):
        return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default

    collect(run_id, arg("--collections", 8), arg("--single", 0), arg("--pages", 1))


if __name__ == "__main__":
    main()
