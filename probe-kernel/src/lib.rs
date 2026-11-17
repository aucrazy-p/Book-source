// 探测内核：只导出书源解析/探测所需模块。
// 不导出 api / app / server / export / storage：
//   它们是 GUI 与 HTTP 服务层（依赖 tauri/axum/sqlx），判活由 parser + crawler 直接驱动。
pub mod crawler;
pub mod error;
pub mod model;
pub mod parser;
pub mod util;

// `service` 不链接真正的 GUI 服务层，改用最小 shim：
// parser/js.rs 与 crawler/source_session.rs 只用到「记录开浏览器请求」和
// 「Cookie 快照」两类能力，无头探测里一律降级（webView 源最终判 UNKNOWN）。
#[path = "service_shim.rs"]
pub mod service;

// L4 行为观测：把 java.* 兼容层的调用记录下来，供容器外层判定 clean/quarantine。
pub mod behavior;
