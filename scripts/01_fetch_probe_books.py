# -*- coding: utf-8 -*-
"""把固定的探针书单（conf/probe_books.json：四层 40 本）复制到 run 目录。

早期版本曾抓豆瓣 Top250，已废弃：① 那是实体书榜，与网络小说全本错配；
② 起点/17K/纵横反爬；③ 番茄榜书名被字形替换（如「惹枝」），无法解析。
改为内置固定书单——跨源可比、长期稳定、不受榜单变化影响。

书单 v6（2026-11-22 用户拍板）：头 10 / 热 10 / 腰 10 / 尾 10 = 40 本，
另含 flagship 招牌层 5 本（命中 ≥2 才 PASSED）。

用法：python 01_fetch_probe_books.py <run_id>
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONF = os.path.join(ROOT, "conf", "probe_books.json")

from lib import dump_json, load_json, run_dir

TIERS = ("classic", "hot", "waist", "longtail")


def prepare(run_id):
    books = load_json(CONF)
    tiers = {k: books.get(k, []) for k in TIERS}
    # v6 书单（头10/热10/腰10/尾10）四层均需非空
    missing = [k for k in TIERS if not tiers[k]]
    if missing:
        print("[探针书单] conf 缺 %s，终止" % "/".join(missing))
        sys.exit(2)
    flagship = books.get("flagship", [])
    # flagship 为空时，--min-flagship>0 会让所有源恒 PARTIAL（无法入池），必须 fail fast
    if not flagship:
        print("[探针书单] conf 缺 flagship 招牌层（min-flagship>0 时会全灭），终止")
        sys.exit(2)
    d = run_dir(run_id)
    os.makedirs(d, exist_ok=True)
    books["_meta"] = {"prepared_at": datetime.now(timezone.utc).isoformat(),
                      "source": "conf/probe_books.json (fixed v6: 40 books, title+author)"}
    dump_json(os.path.join(d, "probe_books.json"), books)
    print("[探针书单] 经典=%d 热门=%d 腰部=%d 长尾=%d 招牌=%d（全部书名+作者双校验）→ %s"
          % (len(tiers["classic"]), len(tiers["hot"]), len(tiers["waist"]),
             len(tiers["longtail"]), len(flagship), os.path.join(d, "probe_books.json")))
    return books


def main():
    if len(sys.argv) < 2:
        print("用法: python 01_fetch_probe_books.py <run_id>")
        sys.exit(1)
    prepare(sys.argv[1])


if __name__ == "__main__":
    main()
