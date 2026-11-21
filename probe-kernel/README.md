# probe-kernel（探测内核 + L4 行为观测）

从 `D:\Code\reader`（reader-rust 1.6.5）剥离的 Linux 探测二进制，用于方案 §6 阶段③ / §7.3 L4：
在一次性容器里**真跑书源规则**，同时通过 `java.*` 兼容层 hook 记录外部行为，判定 clean / suspicious / quarantine。

## 来源与改动

- 源码 `src/` 全量拷贝自 `D:\Code\reader\src`（102 个 .rs），**原 reader 仓库未改动**。
- `Cargo.toml`：去掉 tauri / tauri-plugin-* / axum / tower-http（GUI 与 HTTP 服务层依赖 Linux 系统库）；其余依赖沿用。
- `src/lib.rs`：只导出 `crawler / error / model / parser / util` + 新增 `behavior`；不导出 `api / app / server / export / service / storage`。
- `src/service/webview_cookies.rs`：裁掉 4 个 tauri 函数，只保留纯数据结构 `CookieRecord`（source_session 需要它）。
- **`src/behavior.rs`（新增）**：L4 行为事件模型 + `record/begin/take` + §7.3 判定规则 + 单测。
- **`src/parser/js.rs`（改动 5 处埋点）**：`js_connect`(java.ajax/connect)、`cookie.getCookie/removeCookie`、`__binary`、`store_write`(js_store) 调用 `behavior::record()`。
- **`src/main.rs`（新增）**：CLI 入口，读书源数组 → 跑搜索步 → 收行为 → 判定 → 输出 JSONL。

## L4 埋点清单（防木马核心）

| API | 位置 | 检测意图 |
|---|---|---|
| java.ajax / connect | js.rs `js_connect` | 跨域 POST / 带 body 外带（§7.2 数据外带手法）|
| java.getCookie / removeCookie | js.rs cookie_obj | 读凭据 |
| java.__binary | js.rs globals `__binary` | 宿主 native 桥 / 解码载荷 |
| js_store.put | js.rs `store_write` | 持久化落盘 |

判定见 `behavior::judge`：跨域带凭据外发 = quarantine；拉浏览器/子进程、敏感读高权重；同域 GET 属正常翻页降权（§7.4 误报控制）。

## 状态：首版，待 CI 编译验证

本机无 C 编译器（rquickjs 需要），按方案 §8 走 Linux CI 预编译。首版可能有编译错误，靠 CI 日志迭代修正。
构建 workflow：`.github/workflows/build-kernel.yml`（仅 `workflow_dispatch`，需人工确认触发）。

## 已知缺口（不隐藏，按方案逐步补）

1. 当前 main 只跑搜索步；方案 §17.1 A-1 的四步链路（搜索→详情→目录→正文）尚未全接，留待编译通过后在 main 里用 parser 规则引擎逐段补齐。
2. `eval_js_search_with_source` 的 script 参数需要的是 JS 规则文本，`search_url` 可能是 URL 模板——运行正确性待实测，首版目标先是编译通过 + hook 工作。
3. 容器层 `docker run` 的资源限制（内存/PID/超时，方案 §11）在 workflow 里配，不在本 crate 内。
   **网络不做容器级白名单**：书源域名每源不同且会换，docker 层拿不到 source_id，做不到动态白名单。
   出口由内核层 host 策略管控（`crawler/egress.rs`，见方案 §3）——唯一出网函数
   `js_request_with_bytes` 在发请求前查策略，JS 无第二条出网通道可绕过。

## 出口白名单（方案 §3）

- 唯一出网口 `js_request_with_bytes`：所有 `ajax`/`connect`/`ajaxAll`/字节路径汇流于此，
  策略查询在真正发请求前执行。Rust 侧不硬编码任何域名。
- 数据来源运行时注入：`probe_input.json` 每源的 `declared_hosts`（Python 静态提取），
  平台域/托管域/手工放行来自 `--host-policy <path>`（CI 挂载 `runs/ci/host_policy.json`）。
- 模式由环境变量 `PROBE_EGRESS_MODE` 控制：`observe`（默认，只记 would_block 不拦）/
  `semiblock`（拦陌生域带数据）/ `fullblock`（全量四档）。
- 新源首轮、content_hash 变更源强制 observe 一轮。托管后缀（pages.dev 等）只识别类别，
  必须逐 host 精确匹配，绝不按 eTLD+1 放行。
- 误伤回滚：`PROBE_EGRESS_MODE=observe` 一键回退；单源放行写 `conf/host_policy.json`
  的 `manual_allow[source_id]`，下一轮生效，无需重建。
