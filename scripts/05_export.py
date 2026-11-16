# -*- coding: utf-8 -*-
"""导出 legado 阅读 APP 可直接导入的书源 JSON 数组。

原理：white.json（精选 source_id 列表）与 candidates.jsonl（含完整原始书源对象
的 source 字段）按 source_id join，还原出带全套规则字段的书源，清理内部辅助
字段后输出标准 JSON 数组——这就是"导入书源"对话框/书源直链要的格式。

用法：
  python 05_export.py <run_id> [--all-passed] [--out <输出路径>]

  --all-passed  导出所有 clean + PASSED + search_ok 的源（默认只导出 white 精选）
  --out         指定输出路径（默认 runs/<run_id>/bookSources.json）

输出：纯 JSON 数组，可直接拖进阅读 APP 的"导入书源"，或放静态服务器生成
legado://import/bookSource?src=<url> 一键导入链接。
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

from lib import dump_json, load_json, load_jsonl, run_dir


def clean_source(src):
    """去掉采集/规范化阶段加的内部字段，只留 legado 标准书源字段。"""
    return {k: v for k, v in src.items() if not k.startswith("_")}


def export(run_id, all_passed=False, out_path=None):
    d = run_dir(run_id)
    cands = load_jsonl(os.path.join(d, "candidates.jsonl"))
    by_id = {c["source_id"]: c for c in cands}

    if all_passed:
        matrix = load_jsonl(os.path.join(d, "matrix.jsonl"))
        ids = [r["source_id"] for r in matrix
               if r.get("search_ok") is True and r.get("status") == "PASSED"
               and r.get("risk_level") == "clean"]
        label = "all-passed"
    else:
        white = load_json(os.path.join(d, "white.json"))
        ids = [s["source_id"] for s in white.get("sources", [])]
        label = "white"

    sources, missing = [], []
    for sid in ids:
        c = by_id.get(sid)
        if not c:
            missing.append(sid)
            continue
        sources.append(clean_source(c["source"]))

    if out_path:
        path = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
    else:
        path = os.path.join(d, "bookSources.json")
    dump_json(path, sources)

    # 附带一个导入信息文件（不含规则体，便于核对来源与判据）
    meta = {
        "schema": "legado-book-sources-array",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "mode": label,
        "count": len(sources),
        "missing_source_ids": missing,
        "note": "bookSources.json 为纯数组，可直接导入阅读 APP；本文件仅为核对信息。",
    }
    dump_json(os.path.join(os.path.dirname(path), "import_meta.json"), meta)

    print("[导出] 模式=%s，输出 %d 条书源 → %s" % (label, len(sources), path))
    if missing:
        print("  [警告] %d 个 source_id 在 candidates 中找不到，已跳过" % len(missing))
    for s in sources[:10]:
        print("   - %-24s %s" % (str(s.get("bookSourceName", ""))[:24],
                                str(s.get("bookSourceUrl", ""))[:60]))
    if len(sources) > 10:
        print("   ... 其余 %d 条" % (len(sources) - 10))
    return path


def main():
    if len(sys.argv) < 2:
        print("用法: python 05_export.py <run_id> [--all-passed] [--out <路径>]")
        sys.exit(1)
    run_id = sys.argv[1]
    out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    export(run_id, "--all-passed" in sys.argv, out_path)


if __name__ == "__main__":
    main()
