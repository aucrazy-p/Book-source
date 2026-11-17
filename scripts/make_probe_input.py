# -*- coding: utf-8 -*-
"""把本轮 targets.jsonl（06_pool plan 产出）转成 Rust probe-kernel 的输入。

Rust CLI 读一个 JSON 数组，元素为 {source_id, content_hash, source} 对象。source_id 必须
透传 Python 侧的 sha256 算法结果——06_pool commit 要用它把 matrix 关联回 targets，
两边 source_id 算法不同，绝不能让 Rust 重算。

用法: python make_probe_input.py <run_id>
输出: runs/<run_id>/probe_input.json（JSON 数组，非 JSONL——Rust CLI 只吃数组）
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

from lib import dump_json, load_jsonl, run_dir


def main():
    if len(sys.argv) < 2:
        print("用法: python make_probe_input.py <run_id>")
        sys.exit(1)
    d = run_dir(sys.argv[1])
    targets = load_jsonl(os.path.join(d, "targets.jsonl"))
    rows = []
    for t in targets:
        source = t.get("source")
        if not source or not source.get("searchUrl"):
            continue
        rows.append({
            "source_id": t["source_id"],
            "content_hash": t.get("content_hash", ""),
            "source": source,
        })
    # Rust CLI 第一步用 serde_json::from_str::<Vec<SourceRec>> 整体解析，
    # 只接受 JSON 数组，不接受 JSONL（逐行 map）。必须输出数组格式，否则报
    # "invalid type: map, expected a sequence"。
    dump_json(os.path.join(d, "probe_input.json"), rows)
    print("[Rust输入] %d 条 → %s" % (len(rows), os.path.join(d, "probe_input.json")))


if __name__ == "__main__":
    main()
