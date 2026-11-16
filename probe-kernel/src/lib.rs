// 探测内核：只导出书源解析/探测所需模块。
// 不导出 api / app / server / export / service / storage：
//   它们是 GUI 与 HTTP 服务层（依赖 tauri/axum/sqlx），判活由 parser + crawler 直接驱动。
pub mod crawler;
pub mod error;
pub mod model;
pub mod parser;
pub mod util;

// L4 行为观测：把 java.* 兼容层的调用记录下来，供容器外层判定 clean/quarantine。
pub mod behavior;
