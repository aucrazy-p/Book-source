//! 从独立登录窗口读回 Cookie 与 localStorage，写进书源的会话。
//!
//! 独立窗口打开的是**上游真实站点**，页面在那个域下写的 Cookie 只存在于
//! WebView 自己的存储里；书源后续请求走的是我们自己维护的 cookie jar，两边
//! 不通 —— 表现就是「窗口里登录成功了，回到面板还是显示未登录」。
//!
//! 手机端 legado 也是同一原理，只是 Android 提供同步的
//! `CookieManager.getCookie(url)`。
//!
//! ## Cookie：用 Tauri 的跨平台接口，不再手写 WebView2 COM
//!
//! 早先这里用 `ICoreWebView2CookieManager` + `GetCookiesCompletedHandler` 手动读
//! Cookie，并把整块关在 `#[cfg(target_os = "windows")]` 里。那套写法的代价是
//! macOS / Linux 没有实现（登录态不会回传），而调用方又无条件引用这些符号，
//! 非 Windows 直接编译失败。
//!
//! Tauri 2 的 `WebviewWindow::cookies()` 已经跨平台，底层由 wry 分平台实现：
//!   - Windows: `ICoreWebView2_2::CookieManager().GetCookies`
//!   - macOS:   `WKHTTPCookieStore.getAllCookies`
//!   - Linux:   WebKitGTK `CookieManager::all_cookies`
//!
//! 三者在等待异步回调时都会自己泵消息循环（`with_with_bump` /
//! `wait_for_blocking_operation` / `gtk::main_iteration`），因此可以从
//! `on_window_event` 这种同步回调里直接调，不会死锁 —— 和原先手写 COM 的
//! 做法本质相同，但一份代码覆盖三平台。
//!
//! `document.cookie` 那条路不可行：登录态最常见的是 `HttpOnly` Cookie。

/// 一条 Cookie 的必需字段。
#[derive(Debug, Clone)]
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
