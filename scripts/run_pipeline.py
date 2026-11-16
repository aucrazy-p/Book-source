# -*- coding: utf-8 -*-
r"""一键编排 P1 流水线：01 书单 → 02 规范化去重 → 03 探测 → 04 边际选源。

用法：
  python run_pipeline.py <书源JSON路径> [--run-id ID] [--limit N] [--stop 0.02]

产物目录由 lib.runs_root() 决定：本地默认 WorkSync 产物目录；
CI 上设 LEGADO_SY_RUNS_DIR=<workspace>/runs 即写到 workspace。
输出 runs/<run_id>/：probe_books.json / candidates.jsonl / normalize_stats.json /
matrix.jsonl / white.json / selection_curve.json /
bookSources.json（可直接导入阅读 APP 的纯数组）+ import_meta.json
需要全部合格源时，单独运行：python 05_export.py <run_id> --all-passed

不联网抓上游（上游由 collect_yiove.py 单独产出快照）；本脚本只解释执行已有逻辑。
"""
import os
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    if len(sys.argv) < 2:
        print("用法: python run_pipeline.py <书源JSON路径> [--run-id ID] [--limit N] [--stop 0.02]")
        sys.exit(1)
    src_json = os.path.abspath(sys.argv[1])
    if not os.path.exists(src_json):
        print("书源文件不存在:", src_json)
        sys.exit(2)

    run_id = sys.argv[sys.argv.index("--run-id") + 1] if "--run-id" in sys.argv \
        else datetime.now().strftime("%Y%m%dT%H%M%S")
    py = sys.executable  # 用当前解释器，CI 上是 python3，避免硬编码 python 不存在
    steps = [
        [py, os.path.join(HERE, "01_fetch_probe_books.py"), run_id],
        [py, os.path.join(HERE, "02_normalize.py"), src_json, run_id],
        [py, os.path.join(HERE, "03_probe.py"), run_id],
        [py, os.path.join(HERE, "04_select.py"), run_id],
        [py, os.path.join(HERE, "05_export.py"), run_id],
    ]
    if "--limit" in sys.argv:
        steps[2] += ["--limit", sys.argv[sys.argv.index("--limit") + 1]]
    if "--stop" in sys.argv:
        steps[3] += ["--stop", sys.argv[sys.argv.index("--stop") + 1]]

    print("[编排] run_id=%s，书源=%s" % (run_id, src_json))
    for i, cmd in enumerate(steps, 1):
        print("\n=== step %d/%d: %s ===" % (i, len(steps), os.path.basename(cmd[1])))
        rc = subprocess.call(cmd, cwd=HERE)
        if rc != 0:
            print("[编排] 步骤 %d 失败（rc=%d），中止" % (i, rc))
            sys.exit(rc)
    print("\n[编排] 全部完成，产物在 runs/%s/" % run_id)


if __name__ == "__main__":
    main()
