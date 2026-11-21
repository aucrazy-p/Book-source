//! 出口白名单（方案 §3）：能力封锁替代行为判分。
//!
//! 设计前提（威胁模型实测）：JS 沙箱可达的宿主原语里，`ajax/connect/...`
//! 是唯一出网通道，且全部汇流到 `parser/js.rs::js_request_with_bytes`。
//! 因此在该函数真正发请求之前做一次策略查询，JS 无法绕过——宿主没有第二条出网口。
//!
//! 三条硬约束：
//! 1. Rust 侧不硬编码任何业务域名。平台域/托管域/手工放行全部运行时从
//!    `host_policy.json` 读入（`--host-policy` 参数）；每源声明域从
//!    `probe_input.json` 的 `declared_hosts` 字段读入。加源不需要重新构建。
//! 2. 托管后缀（pages.dev / github.io 等）只用于「识别这是托管域」，
//!    绝不做 eTLD+1 放行——否则一个 `skybook.pages.dev` 会放行全世界 Pages。
//!    只有精确出现在 declared_hosts 里的托管 host 才放行。
//! 3. 观察模式（observe）下只记 `would_block`，永不放宽也永不拒绝；
//!    阶段②半拦只拦「陌生域 + 带数据」；阶段③才启用完整四档。

use std::cell::RefCell;
use std::sync::{OnceLock, RwLock};

/// 运行模式。默认 observe（最安全：只记录不拦截）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Observe,
    SemiBlock,
    FullBlock,
}

impl Mode {
    fn parse(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "semiblock" | "semi" | "half" => Mode::SemiBlock,
            "fullblock" | "full" | "block" => Mode::FullBlock,
            _ => Mode::Observe,
        }
    }
}

/// 跨源共享的策略表（运行时加载，进程内只读一次）。
#[derive(Debug, Default, serde::Deserialize)]
#[serde(default)]
pub struct HostPolicy {
    pub version: u32,
    /// 平台域后缀：只读放行 GET，禁止携带数据。
    pub platforms: Vec<String>,
    /// 托管域后缀：只识别，不放行通配；精确 host 仍需出现在 declared_hosts。
    pub hosted_suffixes: Vec<String>,
    /// 手工放行：source_id -> 额外允许的 host（精确匹配）。
    pub manual_allow: std::collections::HashMap<String, Vec<String>>,
}

/// 一条被拦/本可拦的记录。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct BlockedHost {
    pub host: String,
    pub method: String,
    pub has_body: bool,
    pub has_cookie: bool,
    /// "would_block"=观察模式本可拦；"blocked"=实际拦截。
    pub action: String,
    /// 命中的判定档位（4 档之一，或 0=放行）。
    pub tier: u8,
}

/// 当前源的声明域上下文（每源开始前由 main 设置，thread_local 隔离）。
#[derive(Debug, Clone, Default)]
struct SourceCtx {
    /// 是否已由 main 登记过。未登记（如 ajaxAll 的 scope.spawn 子线程）一律放行——
    /// 宁可漏拦也不误杀，符合灰度期「先观察」原则。
    initialized: bool,
    declared_hosts: Vec<String>,
    source_id: String,
    /// 新源首轮强制 observe；内容变更的源也降回 observe 一轮。
    force_observe: bool,
}

thread_local! {
    // 不能用 `const { ... SourceCtx::default() }`：derive(Default) 生成的 default()
    // 不是 const fn（rustc 报 E0015）。thread_local! 本就惰性初始化，直接写即可。
    static CTX: RefCell<SourceCtx> = RefCell::new(SourceCtx::default());
    static BLOCKED: RefCell<Vec<BlockedHost>> = const { RefCell::new(Vec::new()) };
}

/// 父线程上下文快照，供 ajaxAll 的 scope.spawn 子线程继承（方案 §3.1 全路径覆盖）。
///
/// thread_local 不会跨线程继承：子线程若拿到 default ctx（initialized=false）会在
/// `check` 里直接放行，导致 fullblock 阶段可被 `java.ajaxAll` 稳定绕过。父线程在
/// spawn 前 snapshot，子线程开头 adopt，结束前 drain 拦截记录归并回父线程。
#[derive(Debug, Clone)]
pub struct SourceCtxSnapshot {
    initialized: bool,
    declared_hosts: Vec<String>,
    source_id: String,
    force_observe: bool,
}

/// 取当前线程上下文快照（父线程在 spawn 前调用）。
pub fn snapshot_ctx() -> SourceCtxSnapshot {
    CTX.with(|c| {
        let c = c.borrow();
        SourceCtxSnapshot {
            initialized: c.initialized,
            declared_hosts: c.declared_hosts.clone(),
            source_id: c.source_id.clone(),
            force_observe: c.force_observe,
        }
    })
}

/// 子线程开头调用：把父线程快照写入本线程 thread_local，使 check 正常生效。
pub fn adopt_ctx(snap: &SourceCtxSnapshot) {
    CTX.with(|c| {
        *c.borrow_mut() = SourceCtx {
            initialized: snap.initialized,
            declared_hosts: snap.declared_hosts.clone(),
            source_id: snap.source_id.clone(),
            force_observe: snap.force_observe,
        };
    });
}

/// 在本线程临时装入快照执行 f，结束后恢复原上下文。
///
/// 供 `JsThreadContext::replay` 用：TOC 并行的子线程求值期间需要父线程的声明域，
/// 但求值完必须恢复原 ctx —— 同一工作线程后续还要处理别的源，不恢复会串味。
/// snap=None 时装入 default（initialized=false）→ check 走「未登记放行」兜底。
pub fn with_ctx<F, T>(snap: Option<&SourceCtxSnapshot>, f: F) -> T
where
    F: FnOnce() -> T,
{
    // 先 clone 出 saved，确保 borrow() 在 adopt_ctx 的 borrow_mut() 之前释放。
    let saved = CTX.with(|c| c.borrow().clone());
    match snap {
        Some(s) => adopt_ctx(s),
        None => CTX.with(|c| *c.borrow_mut() = SourceCtx::default()),
    }
    let result = f();
    CTX.with(|c| *c.borrow_mut() = saved);
    result
}

/// 子线程结束前调用：取走本线程拦截记录，由父线程归并（否则 take_blocked 收不到）。
pub fn drain_blocked() -> Vec<BlockedHost> {
    take_blocked()
}

/// 父线程归并子线程的拦截记录。
pub fn merge_blocked(records: Vec<BlockedHost>) {
    if records.is_empty() { return; }
    BLOCKED.with(|b| b.borrow_mut().extend(records));
}

static POLICY: OnceLock<HostPolicy> = OnceLock::new();
/// 用 RwLock 而非 OnceLock：生产只在 init 写一次，但单测要逐例切模式（observe/semiblock/fullblock）。
static MODE: RwLock<Mode> = RwLock::new(Mode::Observe);

/// 进程启动时调用一次：加载策略文件与模式。
///
/// `policy_path` 不存在或解析失败时退化为「空策略 + 仅声明域」，绝不 panic——
/// 观察模式下缺文件只是少一条平台域放行，不会误拦。
pub fn init(policy_path: Option<&str>, mode_str: &str) {
    *MODE.write().unwrap_or_else(|e| e.into_inner()) = Mode::parse(mode_str);
    let policy = policy_path
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str::<HostPolicy>(&s).ok())
        .unwrap_or_default();
    let _ = POLICY.set(policy);
}

/// 当前生效模式（供启动日志输出）。
pub fn current_mode() -> &'static str {
    match *MODE.read().unwrap_or_else(|e| e.into_inner()) {
        Mode::Observe => "observe",
        Mode::SemiBlock => "semiblock",
        Mode::FullBlock => "fullblock",
    }
}

/// 每源探测开始前调用：设置声明域并清空上一源的拦截记录。
pub fn begin_source(declared_hosts: Vec<String>, source_id: String, force_observe: bool) {
    CTX.with(|c| {
        *c.borrow_mut() = SourceCtx {
            initialized: true, declared_hosts, source_id, force_observe,
        };
    });
    BLOCKED.with(|b| b.borrow_mut().clear());
}

/// 每源探测结束后调用：取走并清空拦截记录。
pub fn take_blocked() -> Vec<BlockedHost> {
    BLOCKED.with(|b| std::mem::take(&mut *b.borrow_mut()))
}

/// 判定结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    /// 放行。
    Allow,
    /// 本可拦但观察模式放行（记 would_block）。
    WouldBlock,
    /// 实际拦截。
    Block,
}

/// 归一化 host：去协议、端口、路径、前导 www.，小写。
fn normalize_host(raw: &str) -> String {
    let s = raw.split('#').next().unwrap_or(raw).trim();
    let rest = s.split_once("://").map(|(_, r)| r).unwrap_or(s);
    let host = rest.split('/').next().unwrap_or("").split('@').last().unwrap_or("");
    let host = host.split(':').next().unwrap_or(host).trim_start_matches("www.").to_lowercase();
    host
}

/// 从绝对 URL 取 host；解析失败返回空串（空 host 一律放行，避免误伤）。
fn host_of(url: &str) -> String {
    let s = url.split('#').next().unwrap_or(url).trim();
    if s.is_empty() || s.starts_with("data:") || s.starts_with("javascript:") {
        return String::new();
    }
    normalize_host(s)
}

/// host 是否属于声明域：精确匹配，或作为其子域。
///
/// 注意 declared_hosts 里存的是精确 host（Python 静态提取时抓的就是精确值），
/// 因此即使声明了 `skybook.pages.dev`，也不会放行 `evil.pages.dev`。
fn in_declared(host: &str, declared: &[String]) -> bool {
    declared.iter().any(|d| {
        let d = d.trim().trim_start_matches("www.").to_lowercase();
        !d.is_empty() && (host == d || host.ends_with(&format!(".{}", d)))
    })
}

/// host 是否命中某个后缀（平台域/托管域识别）。
fn ends_with_suffix(host: &str, suffixes: &[String]) -> bool {
    suffixes.iter().any(|s| {
        let s = s.trim().trim_start_matches('.').to_lowercase();
        !s.is_empty() && (host == s || host.ends_with(&format!(".{}", s)))
    })
}

/// 请求是否携带「数据」——POST/PUT 等方法、非空 body、敏感请求头、URL 里的敏感参数。
///
/// 只查头名/参数名，不看值：外带必然要说明用途（cookie/token/auth），
/// 正常翻页请求的 URL 与头里不会出现这些键。
/// 键名列表是策略数据而非业务域名，放在代码里不违反「Rust 不硬编码域名」。
const DATA_KEYS: &[&str] = &["cookie", "token", "session", "auth", "authorization", "secret", "password", "apikey", "api_key", "x-api-key"];

fn has_sensitive_key(key: &str) -> bool {
    let k = key.to_ascii_lowercase();
    DATA_KEYS.iter().any(|d| k == *d || k.ends_with(&format!("_{}", d)) || k.ends_with(&format!("-{}", d)))
}

fn request_carries_data(method: &str, has_body: bool, header_names: &[String], url: &str) -> bool {
    let m = method.to_ascii_uppercase();
    if m != "GET" && m != "HEAD" { return true; }
    if has_body { return true; }
    if header_names.iter().any(|k| has_sensitive_key(k)) { return true; }
    if let Some((_, query)) = url.split_once('?') {
        return query.split(['&', ';']).any(|pair| {
            pair.split_once('=').map(|(k, _)| has_sensitive_key(k)).unwrap_or(false)
        });
    }
    false
}

/// 出口策略查询。由 `js_request_with_bytes` 在真正发请求前调用。
///
/// `header_names` 传合并后的请求头**键名**（调用方从 headers/default_headers 提取）。
/// `has_body` 传 body 或 body_bytes 是否非空。
pub fn check(url: &str, method: &str, has_body: bool, header_names: &[String]) -> Decision {
    let host = host_of(url);
    if host.is_empty() {
        return Decision::Allow;
    }

    let ctx = CTX.with(|c| c.borrow().clone());
    // 未登记上下文的线程（ajaxAll spawn 子线程）一律放行：宁可漏拦不误杀。
    if !ctx.initialized {
        return Decision::Allow;
    }
    let policy = POLICY.get();
    let mode = *MODE.read().unwrap_or_else(|e| e.into_inner());
    // 新源首轮 / 内容变更源：强制 observe，只记不拦。
    let effective_mode = if ctx.force_observe { Mode::Observe } else { mode };

    let carries_data = request_carries_data(method, has_body, header_names, url);
    // has_cookie 单独判定：carries_data 还含 POST/body/query 敏感键，
    // 直接当 has_cookie 会把「带 body 的 POST」误记成「带 cookie」（方案 §5 误伤定位）。
    let has_cookie = header_names.iter().any(|k| has_sensitive_key(k));

    // 第 1 档：主体域 / 资源域（声明域 + 手工放行）→ 完全放行。
    let manual_ok = policy
        .and_then(|p| p.manual_allow.get(&ctx.source_id))
        .map(|hosts| in_declared(&host, hosts))
        .unwrap_or(false);
    if in_declared(&host, &ctx.declared_hosts) || manual_ok {
        return Decision::Allow;
    }

    // 第 2 档：仅平台域（qq.com/baidu.com 等公共只读站）。
    // 托管域（pages.dev/github.io ...）**不在此档放行**：它们是多租户的，
    // 按后缀放行等于放行整个平台（方案 §3.2 反例）。精确声明的托管 host 已在
    // 第 1 档放行；没声明的托管 host 一律落第 3/4 档当陌生域处理。
    let is_platform = policy.map(|p| ends_with_suffix(&host, &p.platforms)).unwrap_or(false);
    if is_platform {
        if carries_data {
            return record(&host, method, has_body, has_cookie, Decision::Block, 2, effective_mode);
        }
        return Decision::Allow;
    }

    // 第 3 档：陌生域 + GET 且无数据 → 放行，但记 would_block（帮助发现静态漏抓域）。
    if !carries_data {
        return record(&host, method, has_body, has_cookie, Decision::WouldBlock, 3, effective_mode);
    }

    // 第 4 档：陌生域 + 带数据 → 拦截（观察模式降级为 would_block）。
    record(&host, method, has_body, has_cookie, Decision::Block, 4, effective_mode)
}

/// 按模式决定实际动作并记账。
fn record(
    host: &str,
    method: &str,
    has_body: bool,
    has_cookie: bool,
    intended: Decision,
    tier: u8,
    mode: Mode,
) -> Decision {
    let actual = match (mode, intended) {
        (Mode::Observe, _) => Decision::WouldBlock,
        // 阶段②只拦第 4 档；平台域带数据（第 2 档）放到全拦阶段再处理。
        (Mode::SemiBlock, Decision::Block) if tier == 4 => Decision::Block,
        (Mode::SemiBlock, Decision::Block) => Decision::WouldBlock,
        (Mode::SemiBlock, Decision::WouldBlock) => Decision::WouldBlock,
        (Mode::FullBlock, _) => intended,
        _ => Decision::WouldBlock,
    };
    BLOCKED.with(|b| {
        b.borrow_mut().push(BlockedHost {
            host: host.to_string(),
            method: method.to_string(),
            has_body,
            has_cookie,
            action: match actual {
                Decision::Block => "blocked".to_string(),
                _ => "would_block".to_string(),
            },
            tier,
        });
    });
    actual
}

#[cfg(test)]
mod tests {
    use super::*;

    fn setup(policy_json: &str, mode: &str, declared: &[&str], source_id: &str, force_observe: bool) {
        // OnceLock 只能设一次：单测里进程共享，这里用独立辅助函数直接落值。
        // 用 std::sync::OnceLock 的 get_or_init 覆盖首次、后续复用（测试间策略相同即可）。
        // POLICY 用 OnceLock：所有测试共用同一份策略（POLICY_JSON 内容相同），
        // 首次 set 生效即可；MODE 用 RwLock，每例独立切换。
        let _ = POLICY.set(serde_json::from_str(policy_json).unwrap());
        *MODE.write().unwrap_or_else(|e| e.into_inner()) = Mode::parse(mode);
        let d: Vec<String> = declared.iter().map(|s| s.to_string()).collect();
        begin_source(d, source_id.to_string(), force_observe);
    }

    const POLICY_JSON: &str = r#"{
        "version":1,
        "platforms":["qq.com","baidu.com"],
        "hosted_suffixes":["pages.dev","github.io"],
        "manual_allow":{"src1":["extra.example.net"]}
    }"#;

    #[test]
    fn declared_host_always_allowed() {
        setup(POLICY_JSON, "fullblock", &["skybook.mgz.la"], "src0", false);
        assert_eq!(check("https://skybook.mgz.la/api/book", "GET", false, &[]), Decision::Allow);
        // 子域也放行
        assert_eq!(check("https://img.skybook.mgz.la/cover.jpg", "GET", false, &[]), Decision::Allow);
    }

    #[test]
    fn hosted_suffix_never_wildcard() {
        setup(POLICY_JSON, "fullblock", &["skybook.pages.dev"], "src0", false);
        // 声明过的精确 host 放行
        assert_eq!(check("https://skybook.pages.dev/x.js", "GET", false, &[]), Decision::Allow);
        // 裁决1：声明域内的托管 host 完全放行（源自己声明的 jsLib 域，带登录态属正常功能）
        let hdr = vec!["Cookie".to_string()];
        assert_eq!(check("https://skybook.pages.dev/api", "POST", true, &hdr), Decision::Allow);
        // 同托管平台的其他 host 绝不因后缀放行；GET 走第 3 档 would_block
        assert_eq!(check("https://evil.pages.dev/collect", "GET", false, &[]), Decision::WouldBlock);
        // 带 cookie 外带 → 第 4 档拦截
        assert_eq!(check("https://evil.pages.dev/collect", "GET", false, &hdr), Decision::Block);
    }

    #[test]
    fn exfil_with_cookie_get_is_blocked() {
        setup(POLICY_JSON, "fullblock", &["site.com"], "src0", false);
        let hdr = vec!["X-Custom".to_string(), "Cookie".to_string()];
        assert_eq!(check("https://evil.com/c?d=x", "GET", false, &hdr), Decision::Block);
    }

    #[test]
    fn exfil_via_query_key_is_blocked() {
        setup(POLICY_JSON, "fullblock", &["site.com"], "src0", false);
        assert_eq!(check("https://evil.com/c?session=abc123", "GET", false, &[]), Decision::Block);
    }

    #[test]
    fn observe_mode_never_blocks() {
        setup(POLICY_JSON, "observe", &["site.com"], "src0", false);
        let hdr = vec!["Cookie".to_string()];
        assert_eq!(check("https://evil.com/collect", "POST", true, &hdr), Decision::WouldBlock);
        let blocked = take_blocked();
        assert_eq!(blocked.len(), 1);
        assert_eq!(blocked[0].action, "would_block");
        assert_eq!(blocked[0].tier, 4);
    }

    #[test]
    fn has_cookie_field_not_confused_with_body() {
        // I3：POST+body 但无 cookie 头，has_cookie 必须为 false（否则误伤定位失真）。
        setup(POLICY_JSON, "fullblock", &["site.com"], "src0", false);
        let _ = check("https://evil.com/collect", "POST", true, &[]);
        let blocked = take_blocked();
        assert_eq!(blocked.len(), 1);
        assert_eq!(blocked[0].has_body, true);
        assert_eq!(blocked[0].has_cookie, false);
    }

    #[test]
    fn semi_block_only_tier4() {
        setup(POLICY_JSON, "semiblock", &["site.com"], "src0", false);
        // 平台域带数据：半拦阶段只记不拦
        assert_eq!(check("https://api.qq.com/upload", "POST", true, &[]), Decision::WouldBlock);
        // 陌生域带数据：拦
        assert_eq!(check("https://evil.com/collect", "POST", true, &[]), Decision::Block);
    }

    #[test]
    fn new_source_forces_observe() {
        setup(POLICY_JSON, "fullblock", &["site.com"], "newsrc", true);
        assert_eq!(check("https://evil.com/collect", "POST", true, &[]), Decision::WouldBlock);
    }

    #[test]
    fn manual_allow_honored() {
        setup(POLICY_JSON, "fullblock", &["site.com"], "src1", false);
        // 手工放行命中，即使带数据也放行
        assert_eq!(check("https://extra.example.net/api", "POST", true, &[]), Decision::Allow);
    }

    #[test]
    fn normal_cross_get_only_would_block() {
        setup(POLICY_JSON, "fullblock", &["site.com"], "src0", false);
        // 图片 CDN 的普通 GET：放行 + 记账（第 3 档），不是拦截
        assert_eq!(check("https://cdn.example.com/cover.jpg", "GET", false, &[]), Decision::WouldBlock);
        let blocked = take_blocked();
        assert_eq!(blocked.len(), 1);
        assert_eq!(blocked[0].action, "would_block");
        assert_eq!(blocked[0].tier, 3);
    }

    // TOC 跨线程缺口：子线程 replay 时用 with_ctx 装入父线程快照，
    // 判定须按快照的声明域生效，退出后须恢复原上下文（不污染下一源）。
    #[test]
    fn with_ctx_restores_prior_context() {
        setup(POLICY_JSON, "fullblock", &["snap.com"], "src0", false);
        let snap = snapshot_ctx(); // 快照声明域 = snap.com
        // 把主线程 CTX 换成另一个声明域，使 saved(main.com) ≠ snap(snap.com)。
        // 这样退出后的断言才能真正检测到"有没有恢复"：
        // 若 with_ctx 未恢复（CTX 仍是 snap.com），退出后 snap.com 会被当声明域放行 → 测试失败。
        begin_source(vec!["main.com".to_string()], "src0".to_string(), false);
        // 作用域内按快照判定：snap.com 是快照的声明域 → Allow
        let inside = with_ctx(Some(&snap), || {
            check("https://snap.com/x", "GET", false, &[])
        });
        assert_eq!(inside, Decision::Allow);
        // 退出后必须按 main.com：snap.com 成陌生域 GET → 第 3 档 WouldBlock
        assert_eq!(check("https://snap.com/x", "GET", false, &[]), Decision::WouldBlock);
        assert_eq!(check("https://main.com/x", "GET", false, &[]), Decision::Allow);
    }

    #[test]
    fn with_ctx_none_falls_back_to_default() {
        setup(POLICY_JSON, "fullblock", &["site.com"], "src0", false);
        let r = with_ctx(None, || {
            // 传 None → 装入 default（initialized=false）→ check 一律放行兜底
            check("https://evil.com/collect", "POST", true, &["Cookie".to_string()])
        });
        assert_eq!(r, Decision::Allow);
    }
}
