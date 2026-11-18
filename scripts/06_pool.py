# -*- coding: utf-8 -*-
"""精选池维护：跨轮累积优质源，恒定维持 POOL_SIZE 个，死源淘汰后补新。

规则（用户定义）：
- 精选池固定 N 个（默认 50）。
- 每轮测一批候选：把「合格」的源并入池，直到池满。
- 池满后，下一轮改为「复测池内源」；连续失败达阈值的移出池，再用新候选补满。
- 每个源记判定历史（轮次、时间、状态、失败计数），保证「死得快」能被发现。

两种模式（由 pool.json 的 pool 长度决定）：
  fill    : 池未满 → 本轮探测新候选（跳过已在池/已淘汰/游标之前的），合格者入池
  recheck : 池已满 → 本轮探测池内全部源，失败者累计，达阈值则移出

状态文件 state/pool.json（跨轮持久化，路径独立于 run_id）：
  pool[]        当前精选源，每条含完整 source 规则体 + 判定历史
  retired{}     已淘汰 source_id → 原因/时间（避免反复选回）
  cursor        候选池扫描游标（fill 模式从此处继续取新源）

子命令：
  python 06_pool.py plan <run_id> [--size 50]
      → 依据 pool 状态决定本轮模式，写 runs/<run_id>/round_plan.json
        {mode, targets:[{source_id, source, domain, ...}], size}
  python 06_pool.py commit <run_id> [--fail 2] [--blocked 3]
      → 读 matrix.jsonl + round_plan.json，把结果并入池，写回 state/pool.json，
        导出 bookSources.json（池内全部源，可直接导入阅读 APP）
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

from lib import (dump_json, dump_jsonl, load_json, load_jsonl,
                 run_dir, runs_root, source_id)

POOL_SIZE = 50
FAIL_LIMIT = 2      # 连续 FAILED 达此值移出（真失效）
BLOCKED_LIMIT = 3   # 连续 BLOCKED 达此值移出（机房 IP 长期连不上，等价于不可用）

# 成人过滤规则单一真值：复用 02_normalize 的 load_adult_patterns/is_adult。
# 用 importlib 按路径加载（文件名以数字开头，不能用常规 import）。
ADULT_FILTER_PY = os.path.join(HERE, "02_normalize.py")


def _adult_blocker():
    """返回 (is_adult_fn, 规则数)；加载失败则返回恒 False，绝不因过滤拖垮导出。"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_norm02", ADULT_FILTER_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pats = mod.load_adult_patterns()
        return (lambda s: mod.is_adult(s or {}, pats)), len(pats)
    except Exception as e:
        print("[池] 警告：成人过滤规则加载失败（%s），本轮跳过二次过滤" % e)
        return (lambda s: (False, "")), 0

# 状态文件：放在 runs 根目录，跨 run_id 共享
STATE_PATH = os.path.join(runs_root(), "_state", "pool.json")


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_state(size):
    return {
        "schema": "pool-state-v1", "size": size, "updated_at": _now(),
        "round": 0, "cursor": 0, "pool": [], "retired": {},
    }


def load_state(size=None):
    """size 不为 None 时覆盖容量（plan 阶段）；否则沿用状态文件里的值（commit 阶段）。"""
    if os.path.exists(STATE_PATH):
        try:
            st = load_json(STATE_PATH)
            st.setdefault("retired", {})
            st.setdefault("cursor", 0)
            st.setdefault("pool", [])
            st.setdefault("round", 0)
            st.setdefault("size", POOL_SIZE)
            if size is not None:
                st["size"] = size
            return st
        except Exception as e:
            print("[池] 状态文件损坏(%s)，重建" % str(e)[:60])
    return init_state(size if size is not None else POOL_SIZE)


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    st["updated_at"] = _now()
    dump_json(STATE_PATH, st)


def plan(run_id, size):
    d = run_dir(run_id)
    os.makedirs(d, exist_ok=True)
    st = load_state(size)
    retired = st["retired"]
    pool_ids = {p["source_id"] for p in st["pool"]}
    cands = load_jsonl(os.path.join(d, "candidates.jsonl"))
    clean = [r for r in cands if r.get("risk_level") == "clean"
             and r.get("source", {}).get("searchUrl")]

    if len(st["pool"]) < size:
        mode = "fill"
        # 每轮固定取 BATCH 个新候选（用户规则：每次测 50 个），从游标处往后、
        # 跳过已在池 / 已淘汰的；合格者由 commit 挑最优秀的补进池直到满。
        BATCH = size  # 每轮探测量 = 池容量（默认 50）
        picked = []
        i = st["cursor"]
        while i < len(clean) and len(picked) < BATCH:
            r = clean[i]
            sid = r["source_id"]
            if sid not in pool_ids and sid not in retired:
                picked.append(r)
            i += 1
        targets = list(picked)
        # 池统一清理（方案 §19.3）：判定规则变更后（书名精确+作者校验），
        # 存量池源必须尽快用新规则复检，不能靠 10 个/轮慢慢轮转——否则
        # 旧假阳性源（如只能搜到同人的站）会一直占位发布。池未满时源数有限，
        # 全量复测成本可控；池满 50 后走 recheck 模式天然全量。
        RECHECK_BATCH = len(st["pool"])
        due = sorted(st["pool"], key=lambda p: p.get("last_checked_round", -1))
        for p in due[:RECHECK_BATCH]:
            sid = p["source_id"]
            if sid in {t["source_id"] for t in targets}:
                continue
            targets.append({
                "source_id": sid, "domain": p.get("domain", ""),
                "bookSourceName": p.get("bookSourceName", ""),
                "content_hash": p.get("content_hash", ""),
                "risk_level": "clean", "source": p["source"],
            })
        st["cursor"] = i  # 推进游标，即使没选够也跳过扫描过的
    else:
        mode = "recheck"
        # 复测池内全部源：用池内存的完整 source 构造目标
        targets = [
            {"source_id": p["source_id"], "domain": p.get("domain", ""),
             "bookSourceName": p.get("bookSourceName", ""),
             "content_hash": p.get("content_hash", ""),
             "risk_level": "clean", "source": p["source"]}
            for p in st["pool"]
        ]

    save_state(st)
    plan_obj = {"run_id": run_id, "mode": mode, "planned_at": _now(),
                "pool_before": len(st["pool"]), "size": size,
                "target_count": len(targets), "cursor": st["cursor"]}
    dump_json(os.path.join(d, "round_plan.json"), plan_obj)
    dump_jsonl(os.path.join(d, "targets.jsonl"), targets)
    print("[池] 模式=%s，池内=%d/%d，本轮目标=%d，游标=%d"
          % (mode, len(st["pool"]), size, len(targets), st["cursor"]))
    return plan_obj


def _row_ok(r):
    # 与 04_select 同口径：只有 clean 源能进池/算合格。
    # quarantine（L4 观测到 java.* 危险调用）/suspicious 一律不许入池。
    return (r.get("search_ok") is True and r.get("status") == "PASSED"
            and r.get("risk_level") == "clean")


def _merge_new(st, rows, by_id, size):
    """fill 模式：把合格且不在池的新源并入，直到池满。返回新入池数。"""
    added = 0
    # 书全评分（§17.1 B-2）：长尾主指标 > 腰部 > 头部，其次响应快。
    def fullness(r):
        return (r.get("hit_longtail") or 0, r.get("hit_waist") or 0,
                (r.get("hit_classic") or 0) + (r.get("hit_hot") or 0))

    ranked = sorted([r for r in rows if _row_ok(r)],
                    key=lambda r: (fullness(r), -(r.get("respond_time_ms") or 99999)),
                    reverse=True)
    cur_ids = {p["source_id"] for p in st["pool"]}
    for r in ranked:
        if len(st["pool"]) >= size:
            break
        sid = r["source_id"]
        if sid in cur_ids or sid in st["retired"]:
            continue
        rec = by_id.get(sid)
        if not rec or not rec.get("source"):
            continue
        st["pool"].append({
            "source_id": sid, "domain": r.get("domain", rec.get("domain", "")),
            "bookSourceName": r.get("bookSourceName", rec.get("bookSourceName", "")),
            "content_hash": r.get("content_hash", rec.get("content_hash", "")),
            "source": rec["source"], "added_round": st["round"], "added_at": _now(),
            "fail_streak": 0, "blocked_streak": 0,
            "history": [{"round": st["round"], "at": _now(),
                         "status": r.get("status"), "search_ok": True,
                         "hit_classic": r.get("hit_classic"), "hit_hot": r.get("hit_hot"),
                         "hit_waist": r.get("hit_waist"), "hit_longtail": r.get("hit_longtail"),
                         "steps_ok": r.get("steps_ok"),
                         "respond_time_ms": r.get("respond_time_ms")}],
        })
        cur_ids.add(sid)
        added += 1
    return added


def _recheck(st, rows, fail_limit, blocked_limit):
    """recheck 模式：更新池内源的判定历史，连续失败/被拦达阈值则移出。"""
    by_id = {r["source_id"]: r for r in rows}
    removed = []
    kept = []
    for p in st["pool"]:
        r = by_id.get(p["source_id"])
        if not r:
            # 本轮没测到（异常），保留不动
            kept.append(p)
            continue
        status = r.get("status")
        p.setdefault("history", []).append({
            "round": st["round"], "at": _now(), "status": status,
            "search_ok": r.get("search_ok"),
            "hit_classic": r.get("hit_classic"), "hit_hot": r.get("hit_hot"),
            "hit_waist": r.get("hit_waist"), "hit_longtail": r.get("hit_longtail"),
            "steps_ok": r.get("steps_ok"),
            "respond_time_ms": r.get("respond_time_ms"),
        })
        p["history"] = p["history"][-10:]  # 只留最近 10 次
        if _row_ok(r):
            p["fail_streak"] = 0
            p["blocked_streak"] = 0
        elif status == "BLOCKED":
            p["blocked_streak"] = p.get("blocked_streak", 0) + 1
            p["fail_streak"] = 0
        else:  # FAILED / UNKNOWN
            p["fail_streak"] = p.get("fail_streak", 0) + 1
            p["blocked_streak"] = 0

        reason = None
        if p.get("fail_streak", 0) >= fail_limit:
            reason = "fail_streak>=%d" % fail_limit
        elif p.get("blocked_streak", 0) >= blocked_limit:
            reason = "blocked_streak>=%d" % blocked_limit
        if reason:
            st["retired"][p["source_id"]] = {
                "reason": reason, "name": p.get("bookSourceName"),
                "domain": p.get("domain"), "at": _now(), "round": st["round"]}
            removed.append((p.get("bookSourceName"), reason))
        else:
            kept.append(p)
    st["pool"] = kept
    return removed


def commit(run_id, fail_limit, blocked_limit):
    d = run_dir(run_id)
    st = load_state()
    plan_obj = load_json(os.path.join(d, "round_plan.json"))
    mode = plan_obj["mode"]
    rows = load_jsonl(os.path.join(d, "matrix.jsonl"))
    targets = load_jsonl(os.path.join(d, "targets.jsonl"))
    by_id = {t["source_id"]: t for t in targets}

    st["round"] += 1
    added = removed = 0
    if mode == "fill":
        added = _merge_new(st, rows, by_id, st["size"])
        print("[池] fill：新入池 %d 个（池内 %d/%d）" % (added, len(st["pool"]), st["size"]))
    else:
        gone = _recheck(st, rows, fail_limit, blocked_limit)
        removed = len(gone)
        for name, reason in gone:
            print("[池]   淘汰 %-20s (%s)" % (str(name)[:20], reason))
        print("[池] recheck：淘汰 %d 个，池内剩 %d" % (removed, len(st["pool"])))

    # 导出前二次安全过滤：源头 02_normalize 已筛过新候选，但池内可能残留
    # 旧规则漏网的源（成人新变体域名），或历史版本放过的高低风险源。
    # 命中即移出池并记入 retired，确保 docs/sources.json 只含 clean 源。
    # 风险来源：matrix 的 risk_level（L4 行为观测）写回池内 history，取最近一次。
    is_adult, n_rules = _adult_blocker()
    risk_by_id = {r["source_id"]: r.get("risk_level") for r in rows}
    kept = []
    safety_removed = []
    for p in st["pool"]:
        hit, ev = is_adult(p.get("source", {}))
        if hit:
            reason = "adult_filter:" + ev
            risk = None
        else:
            # 只在本轮测到该源且有明确非 clean 判定时才移出；本轮没测到的保留不动。
            risk = risk_by_id.get(p["source_id"])
            # 本轮被复测到的池内源，登记轮次供 plan 轮转（避免每轮重复测同一批）。
            if risk is not None:
                p["last_checked_round"] = st["round"]
            reason = ("risk_filter:" + str(risk)) if risk is not None and risk != "clean" else None
        if reason:
            st["retired"][p["source_id"]] = {
                "reason": reason, "name": p.get("bookSourceName"),
                "domain": p.get("domain"), "at": _now(), "round": st["round"]}
            safety_removed.append((p.get("bookSourceName"), reason))
        else:
            kept.append(p)
    if safety_removed:
        st["pool"] = kept
        for name, reason in safety_removed:
            print("[池]   安全过滤移出 %s (%s)" % (name, reason))
        # 分开统计命中原因：成人规则命中的是源，n_rules 只是规则库规模，
        # 混在一句里容易被误读成"移出 N 个成人源"（历史日志就出现过这个歧义）。
        n_adult = sum(1 for _, r in safety_removed if r.startswith("adult_filter:"))
        n_risk = len(safety_removed) - n_adult
        print("[池] 二次安全过滤：移出 %d 个（成人规则命中 %d 个 / risk 过滤 %d 个；"
              "规则库 %d 条），池内剩 %d"
              % (len(safety_removed), n_adult, n_risk, n_rules, len(st["pool"])))

    save_state(st)

    # 导出池内全部源（可直接导入阅读 APP）
    sources = [p["source"] for p in st["pool"]]
    out = os.path.join(d, "bookSources.json")
    dump_json(out, sources)
    dump_json(os.path.join(d, "pool_report.json"), {
        "run_id": run_id, "round": st["round"], "mode": mode,
        "pool_size": len(st["pool"]), "target_size": st["size"],
        "added": added, "removed": removed, "cursor": st["cursor"],
        "retired_total": len(st["retired"]), "at": _now(),
    })
    print("[池] 已导出 %d 条 → %s" % (len(sources), out))
    print("  状态 → %s" % STATE_PATH)


def main():
    if len(sys.argv) < 3:
        print("用法: python 06_pool.py <plan|commit> <run_id> [--size N] [--fail N] [--blocked N]")
        sys.exit(1)
    cmd, run_id = sys.argv[1], sys.argv[2]

    def arg(name, default):
        return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default

    if cmd == "plan":
        plan(run_id, arg("--size", POOL_SIZE))
    elif cmd == "commit":
        commit(run_id, arg("--fail", FAIL_LIMIT), arg("--blocked", BLOCKED_LIMIT))
    else:
        print("未知命令:", cmd)
        sys.exit(1)


if __name__ == "__main__":
    main()
