//! 无 GUI 环境下 `service` 的最小替身。
//!
//! probe-kernel 不链接 tauri / webview，但 `parser/js.rs` 与 `crawler/source_session.rs`
//! 里仍有 15 处对 `crate::service::*` 的引用，用途只有两类：
//!   1. `java.showBrowser` / `java.startBrowser` 记录一次「要开浏览器」的请求；
//!   2. 把当前源的 Cookie 快照成 `CookieRecord`（供验证窗口注入）。
//!
//! 无头探测里这两类能力都不存在，因此这里统一降级：
//! - 浏览器请求：照常记录到线程本地，不执行；调用方取走时能看到「有请求但无 GUI」；
//! - `await_browser` 等阻塞接口：直接返回错误，调用方按 `UNKNOWN` 处理
//!   （对应方案 §5.2：无 GUI 下 webView 降级判不出的源不得误记 `FAILED`）。
//!
//! 只实现被引用到的符号，不引入 tauri / sqlx / storage。

pub mod webview_cookies {
    /// 一条 Cookie 快照。
    ///
    /// `domain` 约定：带前导点 = 域级 Cookie，不带点 = host-only。
    /// 与 reader 的 `webview_cookies::CookieRecord` 字段保持一致，便于日后替换。
    #[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
    pub struct CookieRecord {
        pub name: String,
        pub value: String,
        pub domain: String,
        pub path: String,
        pub secure: bool,
        pub http_only: bool,
    }

    impl CookieRecord {
        /// 拼成 `name=value`，供 `Cookie` 请求头使用。
        pub fn pair(&self) -> String {
            format!("{}={}", self.name, self.value)
        }
    }
}

pub mod book_service {
    /// 书源 JS 调用 `java.showBrowser` / `java.startBrowser` 时捕获的请求。
    ///
    /// 字段与 reader 版完全一致（含 serde 属性），保证 js.rs 里的构造代码不用改。
    #[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
    #[serde(rename_all = "camelCase")]
    pub struct ShowBrowserRequest {
        pub url: String,
        pub html: Option<String>,
        pub preload_js: Option<String>,
        pub options: Option<serde_json::Value>,
        pub title: Option<String>,
        #[serde(default)]
        pub await_result: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        pub panel_id: Option<String>,
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        pub external_window: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pub book_source_url: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pub await_panel_id: Option<String>,
    }

    thread_local! {
        /// `showBrowser` 由 JS 同步调用，结果先落线程本地，由调用方取走。
        static SHOW_BROWSER_REQUEST: std::cell::RefCell<Option<ShowBrowserRequest>> =
            const { std::cell::RefCell::new(None) };
    }

    /// 供 js.rs 的原生 `__show_browser` 原语写入。
    pub fn set_show_browser_request(request: ShowBrowserRequest) {
        SHOW_BROWSER_REQUEST.with(|cell| *cell.borrow_mut() = Some(request));
    }

    /// 取走当前线程捕获的请求（取走即清空，不跨次调用泄漏）。
    pub fn take_show_browser_request() -> Option<ShowBrowserRequest> {
        SHOW_BROWSER_REQUEST.with(|cell| cell.borrow_mut().take())
    }
}

pub mod source_browser {
    use super::book_service::ShowBrowserRequest;

    /// 浏览器返回的一页内容。
    #[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
    pub struct BrowserPage {
        pub url: String,
        pub body: String,
    }

    pub type BrowserReply = Result<BrowserPage, String>;

    const NO_GUI: &str = "no_gui: headless probe has no webview";

    /// 打开面板并阻塞等待结果 —— 无头环境不可用。
    pub fn await_browser(_request: ShowBrowserRequest) -> BrowserReply {
        Err(NO_GUI.to_string())
    }

    /// 打开独立窗口并等待关闭 —— 无头环境不可用。
    pub fn await_external_window(_request: ShowBrowserRequest) -> BrowserReply {
        Err(NO_GUI.to_string())
    }

    /// 仅请求打开独立窗口、不等待 —— 无头环境不可用。
    pub fn request_external_window(_request: ShowBrowserRequest) -> Result<(), String> {
        Err(NO_GUI.to_string())
    }
}
