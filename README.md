# legado-sy 书源自动选源流水线

从 Yiove 书源仓库采集候选源，经规范化去重 + 静态防毒 + 动态探测 + 边际选源，产出**少量够用**的优质书源白名单。全程在 GitHub Actions 上运行，本机不参与。

## 它解决什么

书源不是越多越好：多数源重复覆盖同一批书，且大量源已失效或被风控。本流水线把「几千个源逐个判活」换成「按质量选少量」：

- **连接正常**：用每个源自带的 `checkKeyWord` 真跑搜索链路判定（403/验证码记 `BLOCKED`，不算失效，避免机房 IP 冤杀）。
- **书够全**：5 本经典 + 5 本热门网文做命中交叉验证；按边际贡献贪心选源，覆盖饱和（增益 < 2%）即停。
- **防毒**：三级去重（URL 归一化 / 同 host / 内容哈希）抓换皮；495 条毒域名种子 + 危险字段扫描（`jsLib` / `loginUi` / Cookie 外带特征）；可疑源隔离，绝不主动执行。

## 每天怎么跑

`.github/workflows/daily.yml`：

- 定时：每天 UTC 16:23（北京时间 00:23），避开整点拥塞。
- 也可在 Actions 页面手动 `Run workflow`。
- **无 push 触发**：推送代码不会引发构建。
- 流程：采集 8 个高访问量合集 → 规范化去重 → 探测 50 个候选（约 7 分钟）→ 选源 → 上传 artifact（`source-pick-<编号>`，保留 30 天）。

产物在每次运行的 artifact 里：`white.json`（精选白名单）、`matrix.jsonl`（每源判定明细）、`normalize_stats.json`、`selection_curve.json`、`sources_snapshot.json`。

## 本地跑

```bash
python3 scripts/collect_yiove.py local --collections 8 --pages 1
python3 scripts/run_pipeline.py runs/local/sources_snapshot.json --run-id local --limit 50
```

产物默认写到 WorkSync 产物目录；设 `LEGADO_SY_RUNS_DIR` 可改到任意目录（CI 就是这么做的）。

## 已知边界

- **出口 IP**：GitHub Actions 是境外机房 IP，部分国内小说站会返回 403/验证码，记为 `BLOCKED`。这类源不是失效，只是云端连不上——真要解决需把探测 job 换成 self-hosted runner（家宽出口），GitHub 侧编排无需改动。本机测出的结果不代表云上结果。
- **不做完整规则引擎**：`searchUrl` 为动态 JS / webView 的源记 `UNKNOWN`，不判死。真正跑规则需要 port reader 的 Rust 引擎（后续阶段）。
- **不生产书源**：本仓库只筛选第三方公开书源，不收录、不存储书源内容，产物仅为指向原站的规则配置。

## 目录

- `scripts/`：7 个纯标准库 Python 脚本（采集 / 规范化 / 探测 / 选源 / 编排 / 公共库）。
- `conf/probe_books.json`：5 经典 + 5 热门探针书单。
- `conf/blacklist_seeds.txt`：495 条毒域名种子（取自 book-source-validator 的杀软标记）。
- `.github/workflows/daily.yml`：定时编排。

详细设计与待办见方案文档 `plan-github-vps-automation.md`（本地产物目录，未随仓库发布）。
