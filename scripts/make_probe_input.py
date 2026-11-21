# -*- coding: utf-8 -*-
"""把本轮 targets.jsonl（06_pool plan 产出）转成 Rust probe-kernel 的输入。

Rust CLI 读一个 JSON 数组，元素为 {source_id, content_hash, source,
declared_hosts, is_new}。source_id 必须透传 Python 侧的 sha256 算法结果——
06_pool commit 要用它把 matrix 关联回 targets，两边 source_id 算法不同，
绝不能让 Rust 重算。

出口白名单（方案 §3.5）：
- declared_hosts：从书源 JSON 所有字符串字段静态提取的 URL host（实测 100% 覆盖，
  93% 的源 ≤3 个域）。Rust 侧不硬编码域名，全靠这个字段注入。
- is_new：新源首轮 / content_hash 变更源，强制 observe 一轮只记不拦。
- 同时把 conf/host_policy.json 复制到 runs/<run_id>/，容器已有 runs 挂载，
  不用新增挂载或改镜像。

用法: python make_probe_input.py <run_id>
输出: runs/<run_id>/probe_input.json（JSON 数组，非 JSONL——Rust CLI 只吃数组）
"""
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

from lib import dump_json, load_jsonl, run_dir

# 静态提取 URL host：必须遍历所有字符串字段，不能只取 bookSourceUrl——
# 实测该字段常是书源名（如 "69-明月"），真域名埋在 searchUrl/jsLib 的 JS 串里。
URL_HOST_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)", re.I)
HOSTED_SUFFIXES = ("github.io", "pages.dev", "vercel.app", "netlify.app",
                   "workers.dev", "web.app", "firebaseapp.com")


def extract_declared_hosts(source: dict) -> list:
    """递归遍历书源所有字符串字段，提取去 www. 的小写 host，去重保序。"""
    found = []

    def walk(v):
        if isinstance(v, str):
            for m in URL_HOST_RE.finditer(v):
                h = m.group(1).lower().rstrip(".")
                if h.startswith("www."):
                    h = h[4:]
                if h and h not in found:
                    found.append(h)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(source)
    return found


def copy_host_policy(run_d: str) -> bool:
    """复制 conf/host_policy.json 到 runs/<run_id>/，供容器 --host-policy 读取。"""
    src = os.path.join(ROOT, "conf", "host_policy.json")
    dst = os.path.join(run_d, "host_policy.json")
    if not os.path.exists(src):
        return False
    shutil.copyfile(src, dst)
    return True


def main():
    if len(sys.argv) < 2:
        print("用法: python make_probe_input.py <run_id>")
        sys.exit(1)
    run_id = sys.argv[1]
    d = run_dir(run_id)
    targets = load_jsonl(os.path.join(d, "targets.jsonl"))
    rows = []
    for t in targets:
        source = t.get("source")
        if not source or not source.get("searchUrl"):
            continue
        rows.append({
            "source_id": t["source_id"],
            "content_hash": t.get("content_hash", ""),
            "declared_hosts": extract_declared_hosts(source),
            "is_new": bool(t.get("is_new", False)),
            "source": source,
        })
    # Rust CLI 第一步用 serde_json::from_str::<Vec<SourceRec>> 整体解析，
    # 只接受 JSON 数组，不接受 JSONL（逐行 map）。必须输出数组格式，否则报
    # "invalid type: map, expected a sequence"。
    dump_json(os.path.join(d, "probe_input.json"), rows)
    copied = copy_host_policy(d)

    n_hosted = sum(1 for r in rows for h in r["declared_hosts"]
                   if any(h.endswith(s) for s in HOSTED_SUFFIXES))
    n_empty = sum(1 for r in rows if not r["declared_hosts"])
    n_new = sum(1 for r in rows if r["is_new"])
    print("[Rust输入] %d 条 → %s" % (len(rows), os.path.join(d, "probe_input.json")))
    print("[出口白名单] 声明域提取失败 %d 条 / 托管域 host %d 个 / 新源(强制observe) %d 条 / 策略文件复制=%s"
          % (n_empty, n_hosted, n_new, "OK" if copied else "缺失"))


if __name__ == "__main__":
    main()
