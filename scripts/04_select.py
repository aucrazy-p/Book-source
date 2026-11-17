# -*- coding: utf-8 -*-
"""边际贡献贪心选源：从探测矩阵中选出少量优质源。

算法：
1. 过滤：只保留 search_ok==True、status==PASSED、risk_level==clean 的源
   （search_ok 由源自带 checkKeyWord 判定，是「连接正常/搜索有效」主指标）。
2. 排序候选按书全先验：长尾命中率（方案 §17.1 B-2「书全主指标」）优先，
   其次腰部、头部，最后响应快。
3. 贪心：每轮选「边际增量命中书最多」的源；增量 = 该源命中书中未被已选集合覆盖的。
4. 停止：当最佳源的 marginal_gain < 阈值（默认 0.02）即停。
5. 输出精选白名单 white.json + 覆盖率曲线。

「连接正常」主指标 = 四步链路真跑通（Rust probe-kernel 的 status=PASSED，
  已含头部命中率门禁，见 §17.1 B-2）；
「书籍齐全」主指标 = 长尾命中率命中率，头部/腰部作次序 Tie-break。

输入矩阵兼容两代探测：新一代 Rust CLI 输出 hit_longtail/hit_waist 四层字段，
老一代 03_probe.py 只有 hit_classic/hit_hot —— 缺失的层按 0 处理，不报错。
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

from lib import dump_json, load_jsonl, run_dir

STOP_GAIN = 0.02


def eligible(rows):
    ok = [r for r in rows
          if r.get("search_ok") is True
          and r.get("status") == "PASSED"
          and r.get("risk_level") == "clean"]
    return ok


def fullness(r):
    """书全评分（方案 §17.1 B-2）：长尾是主指标，腰部次之，头部作 tie-break。"""
    return (r.get("hit_longtail") or 0, r.get("hit_waist") or 0,
            (r.get("hit_classic") or 0) + (r.get("hit_hot") or 0))


def preemptive_score(r):
    """可用性先验排序键：书全（长尾>腰部>头部）优先，其次响应快。"""
    rt = r.get("respond_time_ms") or 99999
    return fullness(r) + (-rt,)


def select(matrix, total_books, stop_gain=STOP_GAIN):
    pool = sorted(eligible(matrix), key=preemptive_score, reverse=True)
    covered = set()
    chosen = []
    curve = []
    while pool:
        best, best_gain, best_new = None, -1, set()
        for r in pool:
            new = set(r.get("hit_titles", [])) - covered
            gain = len(new) / total_books if total_books else 0
            # 同增益时书全（长尾优先）命中率高者优先
            quality = fullness(r)
            key = (gain, quality)
            if key > (best_gain, fullness(best) if best else (-1, -1, -1)):
                best, best_gain, best_new = r, gain, new
        if best is None or best_gain < stop_gain:
            break
        covered |= best_new
        chosen.append({
            "source_id": best["source_id"],
            "domain": best["domain"],
            "bookSourceName": best["bookSourceName"],
            "content_hash": best.get("content_hash"),
            "status": best["status"],
            "check_keyword": best.get("check_keyword"),
            "hit_classic": best.get("hit_classic"),
            "hit_hot": best.get("hit_hot"),
            "hit_waist": best.get("hit_waist"),
            "hit_longtail": best.get("hit_longtail"),
            "steps_ok": best.get("steps_ok"),
            "respond_time_ms": best.get("respond_time_ms"),
            "marginal_gain": round(best_gain, 3),
            "new_books": sorted(best_new),
        })
        curve.append({"n_sources": len(chosen),
                      "coverage": round(len(covered) / total_books, 4) if total_books else 0})
        pool.remove(best)
    return chosen, curve, covered


def main():
    if len(sys.argv) < 2:
        print("用法: python 04_select.py <run_id> [--stop 0.02]")
        sys.exit(1)
    run_id = sys.argv[1]
    stop_gain = STOP_GAIN
    if "--stop" in sys.argv:
        stop_gain = float(sys.argv[sys.argv.index("--stop") + 1])

    out_dir = run_dir(run_id)
    matrix = load_jsonl(os.path.join(out_dir, "matrix.jsonl"))
    books = load_jsonl(os.path.join(out_dir, "candidates.jsonl"))  # 仅用于路径存在性
    books_obj = json.load(open(os.path.join(out_dir, "probe_books.json"), encoding="utf-8"))
    total_books = sum(len(books_obj.get(t, [])) for t in ("classic", "hot", "waist", "longtail"))

    chosen, curve, covered = select(matrix, total_books, stop_gain)
    white = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": "white-list-v1",
        "criteria": {"stop_gain": stop_gain, "status": "PASSED",
                     "risk_level": "clean", "primary_metric": "four_step_chain+head_gate",
                     "secondary_metric": "longtail_hit(book_fullness)"},
        "total_probe_books": total_books,
        "covered_books": len(covered),
        "coverage": round(len(covered) / total_books, 4) if total_books else 0,
        "sources": chosen,
    }
    dump_json(os.path.join(out_dir, "white.json"), white)
    dump_json(os.path.join(out_dir, "selection_curve.json"), curve)

    print("[选源] 合格池=%d → 精选=%d，覆盖 %d/%d 探针书 (%.1f%%，停止阈值 gain<%.0f%%)"
          % (len(eligible(matrix)), len(chosen), len(covered), total_books,
             white["coverage"] * 100, stop_gain * 100))
    for i, s in enumerate(chosen, 1):
        print("  %2d. %-22s 边际=%.3f 长尾=%s 腰部=%s 头部=%s/%s 响应=%dms"
              % (i, str(s["bookSourceName"])[:22], s["marginal_gain"],
                 s["hit_longtail"], s["hit_waist"], s["hit_classic"], s["hit_hot"],
                 s["respond_time_ms"]))
    print("  输出:", os.path.join(out_dir, "white.json"))


if __name__ == "__main__":
    main()
