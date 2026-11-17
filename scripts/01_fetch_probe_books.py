# -*- coding: utf-8 -*-
"""把固定的探针书单（conf/probe_books.json：四层 50 本）复制到 run 目录。

早期版本曾抓豆瓣 Top250，已废弃：① 那是实体书榜，与网络小说全本错配；
② 起点/17K/纵横反爬；③ 番茄榜书名被字形替换（如「惹枝」），无法解析。
改为内置固定书单——跨源可比、长期稳定、不受榜单变化影响。

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


def prepare(run_id):
    books = load_json(CONF)
    tiers = {k: books.get(k, []) for k in ("classic", "hot", "waist", "longtail")}
    if not tiers["classic"] or not tiers["hot"]:
        print("[探针书单] conf 缺 classic/hot，终止")
        sys.exit(2)
    if not tiers["waist"] or not tiers["longtail"]:
        print("[探针书单] 警告：conf 缺 waist/longtail，长尾「书全」主指标无法计算")
    d = run_dir(run_id)
    os.makedirs(d, exist_ok=True)
    books["_meta"] = {"prepared_at": datetime.now(timezone.utc).isoformat(),
                      "source": "conf/probe_books.json (fixed)"}
    dump_json(os.path.join(d, "probe_books.json"), books)
    print("[探针书单] 经典=%d 热门=%d 腰部=%d 长尾=%d → %s"
          % (len(tiers["classic"]), len(tiers["hot"]), len(tiers["waist"]),
             len(tiers["longtail"]), os.path.join(d, "probe_books.json")))
    return books


def main():
    if len(sys.argv) < 2:
        print("用法: python 01_fetch_probe_books.py <run_id>")
        sys.exit(1)
    prepare(sys.argv[1])


if __name__ == "__main__":
    main()
