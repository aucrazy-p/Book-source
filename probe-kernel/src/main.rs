//! probe-kernel：书源四步探测（搜索 → 详情 → 目录 → 正文）+ L4 行为观测。
//!
//! 用法：
//!   probe-kernel <sources.json> <probe_books.json> <out.jsonl> [options]
//!
//! options:
//!   --concurrency N        并发源数，默认 4
//!   --timeout-secs N       单请求连接超时，默认 12
//!   --read-timeout-secs N  单请求读超时，默认 15（阅读器仍用 60s）
//!   --per-source-secs N    单源墙钟上限（gate + 40 本），默认 75；超时判 TIMEOUT
//!   --global-budget-secs N 整轮墙钟预算，默认 900；超出后剩余源直接判 TIMEOUT
//!   --min-chapters N       目录可读章节阈值，默认 3
//!   --min-content-chars N  正文字数阈值，默认 500
//!   --min-books-pass N     PASSED 底线：至少 N 本书跑通四步，默认 1
//!   --min-head-rate F      头部命中率门槛 hits.classic/classic.len，默认 0=关闭（生产 0.5）
//!   --min-hot-rate F       热门命中率门槛 hits.hot/hot.len，默认 0=关闭（第一轮采集用 0，第二轮 0.7）
//!   --min-waist-rate F     腰尾命中率门槛 (waist+longtail)/(waist+longtail).len，默认 0=关闭（生产 0.4）
//!   --min-flagship N       招牌命中本数门槛，默认 0=关闭（生产 2）
//!   --host-whitelist h1,h2 允许跨域的目标主机
//!   --host-policy <path>  出口白名单策略文件（平台域/托管域/手工放行）；不传则仅用声明域
//!
//! 出口白名单模式由环境变量 PROBE_EGRESS_MODE 控制（observe / semiblock / fullblock）：
//! 默认 observe——只记 would_block 不拦任何请求。新源首轮、内容变更源强制 observe 一轮。
//!
//! 判定口径（方案 §17.1）：
//!   - 命中 = RuleEngine 从响应解析出真实结构化条目，不是字符串包含
//!   - 单本命中要求四步严格全过；源 PASSED = 头部门槛达标 + 至少 min_books_pass 本四步全过
//!   - 头部层用于排除坏源（非小说站凑出 1 本时头部命中率极低 → PARTIAL）；
//!     长尾命中率是「书全」主指标，交给 04_select 算边际，不混进 PASSED 门禁
//!   - 正文步拦验证页 / 图片章 / 空章（§17.1 A-1、B-4）
//!   - 搜索层挂掉的源整源短路；覆盖度由 hit_* 单独统计（供 04_select 算边际）
//!   - java.* 调用由 behavior 观测，risk_level 取静态与 L4 更严者
//!
//! 线程模型：每个源一个 std 线程（behavior 是 thread_local，begin/take 必须同线程），
//! 线程内用共享 tokio Handle::block_on 驱动 reqwest 异步请求。
use probe_kernel::behavior;
use probe_kernel::crawler::fetcher::{self, FetchResponse, RequestSpec};
use probe_kernel::crawler::http_client::HttpClient;
use probe_kernel::crawler::url_analyzer::analyze_url;
use probe_kernel::model::book_chapter::BookChapter;
use probe_kernel::model::book_source::BookSource;
use probe_kernel::parser::js as pjs;
use probe_kernel::parser::rule_engine::{resolve_url_for_test, RuleEngine};

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::Write;
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

// ---------------------------------------------------------------- 输入

#[derive(Debug, Clone)]
struct ProbeBook {
    title: String,
    author: String,
}

// 兼容两种书单：["斗破苍穹"] 与 [{"title":"斗破苍穹","author":"天蚕土豆"}]
impl<'de> Deserialize<'de> for ProbeBook {
    fn deserialize<D>(d: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum Repr {
            Str(String),
            Obj { title: String, #[serde(default)] author: String },
        }
        match Repr::deserialize(d)? {
            Repr::Str(s) => Ok(ProbeBook { title: s, author: String::new() }),
            Repr::Obj { title, author } => Ok(ProbeBook { title, author }),
        }
    }
}

#[derive(Debug, Deserialize, Clone)]
struct ProbeBooks {
    #[serde(default)]
    classic: Vec<ProbeBook>,
    #[serde(default)]
    hot: Vec<ProbeBook>,
    #[serde(default)]
    waist: Vec<ProbeBook>,
    #[serde(default)]
    longtail: Vec<ProbeBook>,
    /// 招牌层：5 本国民级书，命中 ≥ min_flagship 才 PASSED。
    /// 必须 `#[serde(default)]`：旧书单（无此字段）解析不能 panic。
    #[serde(default)]
    flagship: Vec<ProbeBook>,
}

/// 门禁判定结果（纯数据，便于单测）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct GateOutcome {
    /// 头部命中率是否达标（只算 classic，不含 hot）。
    head_ok: bool,
    /// 热门命中率是否达标（min_hot_rate<=0 时恒真=关闭）。
    hot_ok: bool,
    /// 腰尾命中率是否达标。
    waist_ok: bool,
    /// 招牌命中数是否达标。
    flag_ok: bool,
}

/// 达标所需最小命中本数 = ceil(rate * len)。rate<=0 由各 *_ok 的关闭分支处理，
/// 本函数只负责非零比例；用 f64 转 ceil，书单长度都是整十，无浮点误差。
fn min_count(rate: f64, len: usize) -> u32 {
    if rate <= 0.0 || len == 0 {
        return 0;
    }
    (rate * len as f64).ceil() as u32
}

/// 门禁判定纯函数（方案 §4 伪代码落地）。
/// 口径写死：分母用书单实际长度；`min_*rate<=0` 表示该门关闭；head_ok 只算 classic。
#[allow(clippy::too_many_arguments)]
fn evaluate_gate(
    classic_hits: u32,
    classic_len: usize,
    hot_hits: u32,
    hot_len: usize,
    waist_hits: u32,
    longtail_hits: u32,
    waist_longtail_len: usize,
    flagship_hit: u32,
    min_head_rate: f64,
    min_hot_rate: f64,
    min_waist_rate: f64,
    min_flagship: u32,
) -> GateOutcome {
    let head_ok = min_head_rate <= 0.0
        || (classic_len > 0 && classic_hits >= min_count(min_head_rate, classic_len));
    let hot_ok = min_hot_rate <= 0.0
        || (hot_len > 0 && hot_hits >= min_count(min_hot_rate, hot_len));
    let waist_ok = min_waist_rate <= 0.0
        || (waist_longtail_len > 0
            && (waist_hits + longtail_hits) >= min_count(min_waist_rate, waist_longtail_len));
    let flag_ok = min_flagship == 0 || flagship_hit >= min_flagship;
    GateOutcome { head_ok, hot_ok, waist_ok, flag_ok }
}

#[derive(Debug, Deserialize, Clone)]
struct SourceRec {
    /// 透传 Python 侧的 source_id（sha256(normalize_url)[:16]）。
    /// 06_pool 按 source_id 把 matrix 关联回 targets，两边算法不同，必须透传，
    /// 否则新源一律 by_id 查不到、池永远填不满。纯书源数组输入时此项为空，回退到自算。
    #[serde(default)]
    source_id: String,
    #[serde(default)]
    content_hash: String,
    /// 本源声明可达的 host（由 make_probe_input.py 静态提取，运行时注入，不硬编码）。
    #[serde(default)]
    declared_hosts: Vec<String>,
    /// 新源首轮 / 内容变更源：强制 observe 一轮，只记不拦。
    #[serde(default)]
    is_new: bool,
    source: BookSource,
}

// ---------------------------------------------------------------- 输出

#[derive(Debug, Serialize, Default, Clone)]
struct StepResult {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    stage: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    results: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    matched: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    author: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    chapters: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    readable: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    chars: Option<usize>,
}

#[derive(Debug, Serialize)]
struct ProbeResult {
    source_id: String,
    domain: String,
    book_source_name: String,
    content_hash: String,
    risk_level: String,
    check_keyword: String,
    status: String,
    search_ok: bool,
    search_status: String,
    search_detail: String,
    fail_stage: String,
    respond_time_ms: u64,
    probe_count: u32,
    // Python 04_select / 06_pool 读的是驼峰字段名，Rust 内部惯用下划线，这里两者都输出。
    #[serde(rename = "bookSourceName")]
    book_source_name_camel: String,
    hit_classic: f64,
    hit_hot: f64,
    hit_waist: f64,
    hit_longtail: f64,
    /// 头部命中率是否达标（gate.head_ok，供诊断 PARTIAL 原因；旧字段名保持兼容）。
    head_gate_ok: bool,
    /// 招牌层命中本数（方案 §4）。下游可据此诊断招牌未过的源。
    flagship_hit: u32,
    hits: HashMap<String, u32>,
    attempts: HashMap<String, u32>,
    hit_titles: Vec<String>,
    probed_at: String,
    steps: HashMap<String, StepResult>,
    steps_ok: u8,
    behavior_events: Vec<behavior::BehaviorEvent>,
    behavior_level: String,
    behavior_reasons: Vec<String>,
    /// 出口白名单拦截记录（观察模式下为 would_block）。供 daily 日志汇总误伤。
    #[serde(default)]
    blocked_hosts: Vec<probe_kernel::crawler::egress::BlockedHost>,
}

#[derive(Debug, Clone)]
struct Config {
    concurrency: usize,
    timeout_secs: u64,
    /// 单请求读超时。探测路径用短值（默认 15s），否则几十个源 × 上百个请求
    /// 按 60s 累加就能吃穿整轮墙钟。阅读器不受影响（仍 60s）。
    read_timeout_secs: u64,
    /// 单源墙钟上限：gate + 40 本探针书。超时判 `TIMEOUT`，保留已采部分结果。
    /// 依据历史均速（402s / 60 源 ≈ 27s/源）留 ~3 倍余量。
    per_source_secs: u64,
    /// 整轮墙钟预算：超预算的源不再探测，直接产 `TIMEOUT` 行占位。
    /// 默认 900s = CI 容器 `timeout 1800s` 的一半，留足安全垫。
    global_budget_secs: u64,
    min_chapters: usize,
    min_content_chars: usize,
    /// PASSED 门槛：至少 N 本探针书完整跑通四步。默认 1（证明链路真实可用即合格）。
    min_books_pass: u32,
    /// 头部命中率门槛：hits.classic / classic.len >= 此值。默认 0.0=关闭。
    min_head_rate: f64,
    /// 热门命中率门槛：hits.hot / hot.len >= 此值。默认 0.0=关闭（第一轮只采集）。
    min_hot_rate: f64,
    /// 腰尾命中率门槛：(hits.waist+hits.longtail)/(waist+longtail).len >= 此值。默认 0.0=关闭。
    min_waist_rate: f64,
    /// 招牌命中本数门槛：flagship_hit >= 此值。默认 0=关闭。
    min_flagship: u32,
    host_whitelist: Vec<String>,
    /// 出口白名单策略文件路径（可选）。
    host_policy: Option<String>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            concurrency: 4,
            timeout_secs: 12,
            read_timeout_secs: 15,
            per_source_secs: 75,
            global_budget_secs: 900,
            min_chapters: 3,
            min_content_chars: 500,
            min_books_pass: 1,
            min_head_rate: 0.0,
            min_hot_rate: 0.0,
            min_waist_rate: 0.0,
            min_flagship: 0,
            host_whitelist: Vec::new(),
            host_policy: None,
        }
    }
}

// ---------------------------------------------------------------- 工具

fn source_id(url: &str) -> String {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    url.to_lowercase().hash(&mut h);
    format!("{:016x}", h.finish())
}

fn host_of(url: &str) -> String {
    let s = url.split('#').next().unwrap_or(url).trim();
    let rest = s.split_once("://").map(|(_, r)| r).unwrap_or(s);
    let host = rest.split('/').next().unwrap_or("").split('@').last().unwrap_or("");
    host.split(':').next().unwrap_or(host).to_lowercase()
}

fn content_hash(source: &BookSource) -> String {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    source.book_source_url.hash(&mut h);
    format!("{:?}", source.rule_search).hash(&mut h);
    format!("{:016x}", h.finish())
}

fn now_iso() -> String {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let days = (secs / 86400) as i64;
    let sod = secs % 86400;
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let mut y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    y += if m <= 2 { 1 } else { 0 };
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", y, m, d, sod / 3600, (sod % 3600) / 60, sod % 60)
}

fn main_book(title: &str) -> String {
    let t = title.split(|c| c == ':' || c == '：').next().unwrap_or(title);
    let mut out = String::new();
    let mut depth = 0i32;
    for c in t.chars() {
        match c {
            '(' | '（' | '[' | '【' => depth += 1,
            ')' | '）' | ']' | '】' if depth > 0 => depth -= 1,
            _ if depth == 0 => out.push(c),
            _ => {}
        }
    }
    let out = out.trim();
    if out.is_empty() { title.trim().to_string() } else { out.to_string() }
}

/// 书名归一化：沿用 main_book（剥冒号前缀与括号内容）再去掉空白与分隔标点，
/// 让「斗破 苍穹」「斗破苍穹精校版」与「斗破苍穹」可比。
fn norm_title(s: &str) -> String {
    let base = main_book(s);
    base.chars()
        .filter(|c| !c.is_whitespace()
            && !matches!(c, '：' | ':' | '－' | '—' | '–' | '·' | '，' | ',' | '。' | '.' | '！' | '!' | '？' | '?' | '《' | '》' | '「' | '」' | '_' | '-' | '｜' | '|'))
        .collect()
}

fn name_match(actual: &str, expected: &str) -> bool {
    let a = norm_title(actual.trim());
    let e = norm_title(expected.trim());
    if a.is_empty() || e.is_empty() { return false; }
    if a == e { return true; }
    // 仅放行「探针书名 + 干净版本后缀」：斗破苍穹精校版 等。
    // 不再允许双向子串——「综漫：从斗破苍穹特殊篇开始」这类同人必须判失败。
    match a.strip_prefix(&e) {
        Some(rest) => is_version_suffix(rest),
        None => false,
    }
}
/// 作者匹配：书单没配作者时跳过；配了则归一化（去空白/·/著）后要求一致。
/// 对短作者名双向 contains 足够安全（典型变体只有「天蚕土豆著」），
/// 能挡住同人作者（牛头老哥 != 天蚕土豆）。
fn author_match(actual: &str, expected: &str) -> bool {
    let norm = |s: &str| -> String {
        s.chars().filter(|c| !c.is_whitespace() && *c != '·' && *c != '著').collect()
    };
    let a = norm(actual.trim());
    let e = norm(expected.trim());
    if e.is_empty() { return true; }
    if a.is_empty() { return false; }
    a == e || a.contains(&e) || e.contains(&a)
}

/// 版本后缀白名单：去掉探针书名前缀后，剩余必须只由这些词构成（总长 ≤8）。
fn is_version_suffix(rest: &str) -> bool {
    const SUFFIXES: &[&str] = &[
        "精校版", "精校", "校对版", "校对", "完结版", "完结", "全集", "全本",
        "无删减", "无删节", "修订版", "典藏版", "整理版", "txt", "TXT",
        "下载", "在线阅读", "最新", "正文", "完整版",
    ];
    let r = rest.trim();
    if r.is_empty() || r.chars().count() > 8 { return false; }
    let mut remain = r;
    let mut changed = true;
    while changed && !remain.is_empty() {
        changed = false;
        for s in SUFFIXES {
            if let Some(stripped) = remain.strip_prefix(s) {
                remain = stripped.trim_start();
                changed = true;
            } else if let Some(stripped) = remain.strip_suffix(s) {
                remain = stripped.trim_end();
                changed = true;
            }
        }
    }
    remain.is_empty()
}

fn looks_blocked(text: &str) -> bool {
    let lower = text.to_lowercase();
    ["tcaptcha", "turing.captcha", "验证码", "captcha"].iter().any(|p| lower.contains(p))
}

/// 方案 §17.1 B-4：图片章识别。HTTP 200 且有字数，但正文几乎全靠图片撑起来
/// （漫画扫描页 / 课程页配图），解析出的可读文字远少于图片数 —— 不算有效正文。
/// 用「原始 HTML 的图片标签数」与「解析正文里的非空白非 ASCII 可读字数」比对，保守判定。
fn looks_image_only_chapter(raw_body: &str, content: &str) -> Option<String> {
    let readable = content.chars().filter(|c| !c.is_ascii() && !c.is_whitespace()).count();
    let img_raw = {
        let low = raw_body.to_lowercase();
        low.matches("<img").count() + low.matches("<image").count()
    };
    if img_raw >= 3 && readable * 3 < img_raw {
        return Some(format!("img={},readable={}", img_raw, readable));
    }
    let img_in_content = {
        let low = content.to_lowercase();
        low.matches("<img").count() + low.matches("<image").count()
    };
    if img_in_content >= 3 && readable * 3 < img_in_content {
        return Some(format!("placeholder_img={},readable={}", img_in_content, readable));
    }
    None
}

/// 用书源的 HTTP 上下文包住同步 JS 解析；拿不到上下文时降级为直接解析。
fn with_source_ctx<T>(source: &BookSource, f: impl FnOnce() -> T) -> T {
    match pjs::source_http_context(&source.book_source_url) {
        Ok(ctx) => pjs::with_http_context(ctx, f),
        Err(_) => f(),
    }
}

// ---------------------------------------------------------------- 单步

struct StepFetch {
    resp: Option<FetchResponse>,
    ms: u64,
    err: Option<String>,
    status_hint: Option<u16>,
}

fn fetch_step(
    http: &HttpClient,
    rt: &tokio::runtime::Handle,
    source: &BookSource,
    rule: &str,
    key: &str,
    base: &str,
) -> StepFetch {
    let t0 = Instant::now();
    let spec: RequestSpec = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        pjs::with_search_context(key, 1, || analyze_url(rule, key, 1, base, source))
    })) {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => return StepFetch { resp: None, ms: t0.elapsed().as_millis() as u64, err: Some(format!("URL_RULE:{}", e)), status_hint: None },
        Err(_) => return StepFetch { resp: None, ms: t0.elapsed().as_millis() as u64, err: Some("URL_RULE_PANIC".into()), status_hint: None },
    };
    match rt.block_on(fetcher::fetch(http, spec)) {
        Ok(resp) => StepFetch {
            status_hint: Some(resp.status),
            resp: Some(resp),
            ms: t0.elapsed().as_millis() as u64,
            err: None,
        },
        Err(e) => StepFetch { resp: None, ms: t0.elapsed().as_millis() as u64, err: Some(format!("FETCH:{}", e)), status_hint: None },
    }
}

fn fail_step(stage: &str, ms: u64, detail: &str) -> StepResult {
    StepResult { ok: false, ms: Some(ms), stage: Some(format!("{}:{}", stage, detail)), ..StepResult::default() }
}

// stable Rust 无 std::sync::Semaphore（nightly-only），用有界 channel 实现并发闸门。
// 预填 N 个 token，取到者为 Permit，Drop 时归还。
struct Permit {
    _token: (),
    tx: mpsc::Sender<()>,
}
impl Drop for Permit {
    fn drop(&mut self) {
        let _ = self.tx.send(());
    }
}
fn acquire_permit(rx: &Arc<Mutex<mpsc::Receiver<()>>>, tx: &mpsc::Sender<()>) -> Permit {
    let token = rx.lock().unwrap().recv().expect("semaphore channel 关闭");
    Permit { _token: token, tx: tx.clone() }
}

fn resp_or_fail(f: &mut StepFetch, prefix: &str) -> Result<FetchResponse, StepResult> {
    let ms = f.ms;
    match (f.resp.take(), f.err.take()) {
        (Some(r), _) if r.status == 403 || r.status == 429 => Err(fail_step(&format!("{}_BLOCKED", prefix), ms, &format!("http_{}", r.status))),
        (Some(r), _) => Ok(r),
        (None, Some(e)) => Err(fail_step(&format!("{}_FETCH", prefix), ms, &e)),
        (None, None) => Err(fail_step(&format!("{}_FETCH", prefix), ms, "empty")),
    }
}

// ---------------------------------------------------------------- 四步

fn run_four_steps_with_http(
    source: &BookSource,
    book: &ProbeBook,
    mut sf: StepFetch,
    http: &HttpClient,
    rt: &tokio::runtime::Handle,
    parser: &RuleEngine,
    cfg: &Config,
) -> (HashMap<String, StepResult>, bool, u64) {
    let mut steps: HashMap<String, StepResult> = HashMap::new();
    let mut total_ms = sf.ms;

    // ---- 1) search ----
    let resp = match resp_or_fail(&mut sf, "SEARCH") {
        Ok(r) => r,
        Err(e) => { steps.insert("search".into(), e); return (steps, false, total_ms); }
    };
    if looks_blocked(&resp.body) {
        steps.insert("search".into(), fail_step("SEARCH_BLOCKED", 0, "captcha"));
        return (steps, false, total_ms);
    }
    let books = pjs::with_search_context(&book.title, 1, || {
        with_source_ctx(source, || parser.search_books(source, &resp.body, &resp.url))
    });
    let matched: Vec<String> = books.iter()
        .filter(|b| name_match(&b.name, &book.title) && author_match(&b.author, &book.author))
        .map(|b| b.name.clone()).collect();
    if matched.is_empty() {
        let why = if books.is_empty() { "no_result_items" } else { "no_title_match" };
        steps.insert("search".into(), fail_step("SEARCH_PARSE", 0, why));
        return (steps, false, total_ms);
    }
    let picked = books.iter()
        .find(|b| name_match(&b.name, &book.title) && author_match(&b.author, &book.author))
        .cloned().unwrap();
    steps.insert("search".into(), StepResult { ok: true, ms: Some(total_ms), results: Some(books.len()), matched: Some(matched), ..StepResult::default() });

    // ---- 2) detail ----
    let detail_url = if picked.book_url.trim().is_empty() { String::new() } else { resolve_url_for_test(&resp.url, &picked.book_url) };
    if detail_url.is_empty() {
        steps.insert("detail".into(), fail_step("DETAIL_URL", 0, "empty_book_url"));
        return (steps, false, total_ms);
    }
    let mut df = fetch_step(http, rt, source, &detail_url, "", &source.book_source_url);
    total_ms += df.ms;
    let dresp = match resp_or_fail(&mut df, "DETAIL") {
        Ok(r) => r,
        Err(e) => { steps.insert("detail".into(), e); return (steps, false, total_ms); }
    };
    let info = pjs::with_book_context(Some(pjs::JsBookContext {
        book: serde_json::json!({"bookUrl": detail_url, "tocUrl": detail_url}),
        chapter: serde_json::json!({}),
    }), || with_source_ctx(source, || parser.book_info(source, &dresp.body, &dresp.url, &detail_url)));
    if info.name.trim().is_empty()
        || !name_match(&info.name, &book.title)
        || !author_match(&info.author, &book.author) {
        steps.insert("detail".into(), fail_step("DETAIL_PARSE", 0,
            &format!("bad_name:{}/bad_author:{}", info.name, info.author)));
        return (steps, false, total_ms);
    }
    steps.insert("detail".into(), StepResult { ok: true, ms: Some(df.ms), name: Some(info.name.clone()), author: Some(info.author.clone()), ..StepResult::default() });

    // ---- 3) toc ----
    let toc_url = match info.toc_url.as_deref() {
        Some(t) if !t.trim().is_empty() => resolve_url_for_test(&dresp.url, t),
        _ => detail_url.clone(),
    };
    let mut tf = fetch_step(http, rt, source, &toc_url, "", &source.book_source_url);
    total_ms += tf.ms;
    let tresp = match resp_or_fail(&mut tf, "TOC") {
        Ok(r) => r,
        Err(e) => { steps.insert("toc".into(), e); return (steps, false, total_ms); }
    };
    let chapters: Vec<BookChapter> = pjs::with_book_context(Some(pjs::JsBookContext {
        book: serde_json::json!({"bookUrl": detail_url, "tocUrl": toc_url}),
        chapter: serde_json::json!({}),
    }), || with_source_ctx(source, || parser.chapter_list(source, &tresp.body, &tresp.url, Some(&detail_url)))).0;
    let readable: Vec<&BookChapter> = chapters.iter()
        .filter(|c| !c.is_volume && !c.is_vip && !c.url.trim().is_empty())
        .collect();
    if readable.len() < cfg.min_chapters {
        steps.insert("toc".into(), fail_step("TOC_PARSE", 0, &format!("below_threshold:{}", readable.len())));
        return (steps, false, total_ms);
    }
    steps.insert("toc".into(), StepResult { ok: true, ms: Some(tf.ms), chapters: Some(chapters.len()), readable: Some(readable.len()), ..StepResult::default() });

    // ---- 4) content ----
    let chapter_url = resolve_url_for_test(&tresp.url, &readable[0].url);
    let mut cf = fetch_step(http, rt, source, &chapter_url, "", &source.book_source_url);
    total_ms += cf.ms;
    let cresp = match resp_or_fail(&mut cf, "CONTENT") {
        Ok(r) => r,
        Err(e) => { steps.insert("content".into(), e); return (steps, false, total_ms); }
    };
    // §17.1 A-1：HTTP 200 但返回验证页不算通过。
    if looks_blocked(&cresp.body) {
        steps.insert("content".into(), fail_step("CONTENT_BLOCKED", 0, "captcha"));
        return (steps, false, total_ms);
    }
    let content = pjs::with_book_context(Some(pjs::JsBookContext {
        book: serde_json::json!({"bookUrl": detail_url, "tocUrl": toc_url}),
        chapter: serde_json::json!({"title": readable[0].title, "url": chapter_url}),
    }), || with_source_ctx(source, || parser.content(source, &cresp.body, &chapter_url, Some(&detail_url))));
    let chars = content.chars().filter(|c| !c.is_whitespace()).count();
    if chars < cfg.min_content_chars {
        steps.insert("content".into(), fail_step("CONTENT_PARSE", 0, &format!("too_short:{}", chars)));
        return (steps, false, total_ms);
    }
    // §17.1 B-4：图片章/空壳正文不算通过。
    if let Some(why) = looks_image_only_chapter(&cresp.body, &content) {
        steps.insert("content".into(), fail_step("CONTENT_IMAGE_ONLY", 0, &why));
        return (steps, false, total_ms);
    }
    steps.insert("content".into(), StepResult { ok: true, ms: Some(cf.ms), chars: Some(chars), ..StepResult::default() });

    (steps, true, total_ms)
}

// ---------------------------------------------------------------- 单源

fn judge_with_timeout(host: &str, events: &[behavior::BehaviorEvent], ms: u64, cfg: &Config) -> (String, Vec<String>) {
    let (mut level, mut reasons) = behavior::judge(host, events);
    if !cfg.host_whitelist.is_empty() {
        reasons.retain(|r| {
            if let Some(rest) = r.strip_prefix("L4_EXFIL:") {
                let target = rest.split('→').nth(1).unwrap_or("").split('(').next().unwrap_or("");
                return !cfg.host_whitelist.iter().any(|w| w == target);
            }
            true
        });
        if reasons.is_empty() { level = "clean".into(); }
    }
    if ms > 5000 {
        reasons.push(format!("L4_SLOW:{}ms", ms));
        if level == "clean" { level = "suspicious".into(); }
    }
    (level, reasons)
}

/// 整轮预算耗尽时给未探测源生成占位行。
///
/// 为什么要产行而不是跳过：下游 06_pool / 04_select 按 source_id join，
/// 缺行会被当成异常数据；行数必须与 targets 对齐。`status=TIMEOUT` 的语义是
/// 「本轮没测」，因此不计失败、不改轮次、也不按 risk 移出池。
fn timeout_result(rec: &SourceRec, fail_stage: &str) -> ProbeResult {
    let source = &rec.source;
    let url = source.book_source_url.clone();
    let host = host_of(&url);
    let sid = if rec.source_id.trim().is_empty() { source_id(&url) } else { rec.source_id.clone() };
    ProbeResult {
        source_id: sid,
        domain: host,
        book_source_name: source.book_source_name.clone(),
        content_hash: rec.content_hash.clone(),
        risk_level: "unknown".into(),
        check_keyword: source.check_keyword(),
        status: "TIMEOUT".into(),
        search_ok: false,
        search_status: "TIMEOUT".into(),
        search_detail: fail_stage.to_string(),
        fail_stage: fail_stage.to_string(),
        respond_time_ms: 0,
        probe_count: 0,
        book_source_name_camel: source.book_source_name.clone(),
        hit_classic: 0.0,
        hit_hot: 0.0,
        hit_waist: 0.0,
        hit_longtail: 0.0,
        head_gate_ok: false,
        flagship_hit: 0,
        hits: HashMap::new(),
        attempts: HashMap::new(),
        hit_titles: Vec::new(),
        probed_at: now_iso(),
        steps: HashMap::new(),
        steps_ok: 0,
        behavior_events: Vec::new(),
        behavior_level: "unknown".into(),
        behavior_reasons: vec!["global_budget_exhausted".into()],
        blocked_hosts: Vec::new(),
    }
}

fn probe_source(
    rec: &SourceRec,
    books: &ProbeBooks,
    http: &HttpClient,
    rt: &tokio::runtime::Handle,
    parser: &RuleEngine,
    cfg: &Config,
    deadline: Instant,
) -> ProbeResult {
    let source = &rec.source;
    let url = source.book_source_url.clone();
    let host = host_of(&url);
    let kw = source.check_keyword();
    // 优先用 Python 侧透传的 source_id（保证与 targets.jsonl 关联）；无则自算。
    let sid = if rec.source_id.trim().is_empty() { source_id(&url) } else { rec.source_id.clone() };

    behavior::begin();
    // 出口白名单：登记本源声明域到 thread_local（同线程内的 JS 请求可见）。
    // 声明域由 make_probe_input.py 静态提取；is_new 由 06_pool.plan 标记。
    probe_kernel::crawler::egress::begin_source(rec.declared_hosts.clone(), sid.clone(), rec.is_new);
    // 跨平台 JS 持久化目录（Windows 本机调试 / Linux CI 通用），用完即弃。
    let js_dir = std::env::temp_dir().join("probe-js-store");
    let _ = std::fs::create_dir_all(&js_dir);
    pjs::set_js_storage_dir(js_dir);

    // 整源短路：先用 checkKeyWord 跑一次搜索，解析不出结果就跳过 10 本书。
    let gate = fetch_step(http, rt, source, source.search_url.as_deref().unwrap_or(""), &kw, &url);
    let gate_ok = match &gate.resp {
        Some(r) if r.status != 403 && r.status != 429 && !looks_blocked(&r.body) => {
            !pjs::with_search_context(&kw, 1, || with_source_ctx(source, || parser.search_books(source, &r.body, &r.url))).is_empty()
        }
        _ => false,
    };
    let mut total_ms = gate.ms;
    let gate_status = if gate_ok {
        "PASSED"
    } else if gate.status_hint == Some(403) || gate.status_hint == Some(429) || gate.resp.as_ref().map(|r| looks_blocked(&r.body)).unwrap_or(false) {
        "BLOCKED"
    } else if gate.err.as_deref().map(|e| e.starts_with("FETCH")).unwrap_or(false) {
        "FAILED"
    } else {
        "UNKNOWN"
    };

    let mut steps_all: HashMap<String, StepResult> = HashMap::new();
    let mut hits: HashMap<String, u32> = HashMap::new();
    let mut attempts: HashMap<String, u32> = HashMap::new();
    let mut hit_titles: Vec<String> = Vec::new();
    let mut probe_count: u32 = 1;
    // 单源墙钟超时标记。注意语义：TIMEOUT = 「这一轮没测完」，不是「源是坏的」——
    // 下游 06_pool 不会把它计入 fail_streak、也不会按 risk 移出池。
    let mut timed_out = false;

    if gate_ok {
        let books_total = books.classic.len() + books.hot.len() + books.waist.len() + books.longtail.len();
        let mut tier_books: Vec<(&str, &ProbeBook)> = Vec::new();
        for b in &books.classic { tier_books.push(("classic", b)); }
        for b in &books.hot { tier_books.push(("hot", b)); }
        for b in &books.waist { tier_books.push(("waist", b)); }
        for b in &books.longtail { tier_books.push(("longtail", b)); }
        for (tier, b) in tier_books {
            // 每本开头检查单源 deadline：慢源最坏 121 个请求 × 读超时，
            // 一旦撞线立刻收手，把已采到的 hits/steps 当部分结果交出去。
            if Instant::now() >= deadline {
                timed_out = true;
                eprintln!("[probe-kernel] {} 单源超时，已测 {}/{} 本，保留部分结果",
                    source.book_source_name, probe_count - 1, books_total);
                break;
            }
            *attempts.entry(tier.to_string()).or_insert(0) += 1;
            probe_count += 1;
            let sf = fetch_step(http, rt, source, source.search_url.as_deref().unwrap_or(""), &b.title, &url);
            std::thread::sleep(Duration::from_millis(100));
            let (steps, ok, ms) = run_four_steps_with_http(source, b, sf, http, rt, parser, cfg);
            total_ms += ms;
            if ok {
                *hits.entry(tier.to_string()).or_insert(0) += 1;
                hit_titles.push(b.title.clone());
            } else {
                for (k, v) in steps {
                    if !v.ok { steps_all.insert(format!("{}.{}.{}", tier, k, b.title), v); }
                }
            }
        }
    }

    let n_hit = hits.values().copied().sum::<u32>();
    let tier_rate = |tier: &str, total: usize| -> f64 {
        if total == 0 { 0.0 } else {
            (hits.get(tier).copied().unwrap_or(0) as f64 / total as f64 * 1000.0).round() / 1000.0
        }
    };
    let hit_classic = tier_rate("classic", books.classic.len());
    let hit_hot = tier_rate("hot", books.hot.len());
    let hit_waist = tier_rate("waist", books.waist.len());
    let hit_longtail = tier_rate("longtail", books.longtail.len());

    let classic_hits = hits.get("classic").copied().unwrap_or(0);
    let hot_hits = hits.get("hot").copied().unwrap_or(0);
    let waist_hits = hits.get("waist").copied().unwrap_or(0);
    let longtail_hits = hits.get("longtail").copied().unwrap_or(0);
    // 招牌命中 = 四层命中集合里有几本落在 flagship 列表内。
    // 注意：hit_titles 存的是探针书名 b.title（其命中已由上游 name_match 判定，
    // 上游放行的"精校版"等后缀在此不会再被区分）；norm_title 这里只做归一化比对，
    // 语义是「命中子集计数」，不是给招牌加独立精度层。
    let flagship_hit = books
        .flagship
        .iter()
        .filter(|fb| {
            let f = norm_title(fb.title.trim());
            !f.is_empty() && hit_titles.iter().any(|t| norm_title(t.trim()) == f)
        })
        .count() as u32;

    let chain_ok = n_hit >= cfg.min_books_pass;
    // 门禁判定（方案 §4）：头部只算 classic；热门/腰尾/招牌各自独立；比例分母用书单实际长度。
    // 命名为 verdict，避免遮蔽上面 let gate = fetch_step(...)（后面 search_detail 还要用 gate.err）。
    let verdict = evaluate_gate(
        classic_hits, books.classic.len(),
        hot_hits, books.hot.len(),
        waist_hits, longtail_hits, books.waist.len() + books.longtail.len(),
        flagship_hit,
        cfg.min_head_rate, cfg.min_hot_rate, cfg.min_waist_rate, cfg.min_flagship,
    );
    let gate_all_ok = verdict.head_ok && verdict.hot_ok && verdict.waist_ok && verdict.flag_ok;
    let mut status = if !gate_ok {
        gate_status.to_string()
    } else if chain_ok && gate_all_ok {
        "PASSED".into()
    } else {
        "PARTIAL".into()
    };
    // 撞了单源 deadline 且尚未达标 → 判 TIMEOUT（没测完，不是坏源）。
    // 已经达标的保留 PASSED：达标是好源，改判 TIMEOUT 反而把它挡在本轮池外。
    if timed_out && status != "PASSED" {
        status = "TIMEOUT".into();
    }
    // steps_ok：有书完整跑通四步即为 4；否则按命中书数给一个降级分（0..3）。
    let steps_ok = if chain_ok { 4 } else { std::cmp::min(3, n_hit) as u8 };

    let events = behavior::take();
    let blocked_hosts = probe_kernel::crawler::egress::take_blocked();
    if !blocked_hosts.is_empty() {
        let n_blocked = blocked_hosts.iter().filter(|b| b.action == "blocked").count();
        let n_would = blocked_hosts.len() - n_blocked;
        eprintln!("[egress] {} mode={} 拦截={} 本可拦={} hosts={:?}",
            sid, probe_kernel::crawler::egress::current_mode(), n_blocked, n_would,
            blocked_hosts.iter().take(5).map(|b| b.host.as_str()).collect::<Vec<_>>());
    }
    // 方案 §17.1 A-4：respond_time_ms > 5000 降级，用单源平均耗时，
    // 不是 total_ms（50 本书累计必然超 5s，会把所有源误降 suspicious）。
    let respond_ms = if probe_count > 0 { total_ms / probe_count as u64 } else { 0 };
    let (mut level, reasons) = judge_with_timeout(&host, &events, respond_ms, cfg);
    if level == "clean" && status == "PARTIAL" { level = "suspicious".into(); }

    // fail_stage 决定下游日志的可读性；TIMEOUT 要能区分是单源超时还是整轮预算。
    // 门禁未过时按 HEAD→HOT→WAIST→FLAG 顺序报第一个未过项，便于定位。
    let fail_stage = if status == "TIMEOUT" {
        format!("TIMEOUT:per_source:probed={}", probe_count)
    } else if !gate_ok {
        format!("SEARCH:{}", gate_status)
    } else if !verdict.head_ok {
        format!("HEAD_GATE:{}/{}<{}", classic_hits, books.classic.len(), cfg.min_head_rate)
    } else if !verdict.hot_ok {
        format!("HOT_GATE:{}/{}<{}", hot_hits, books.hot.len(), cfg.min_hot_rate)
    } else if !verdict.waist_ok {
        format!("WAIST_GATE:{}/{}<{}", waist_hits + longtail_hits,
            books.waist.len() + books.longtail.len(), cfg.min_waist_rate)
    } else if !verdict.flag_ok {
        format!("FLAGSHIP_GATE:{}<{}", flagship_hit, cfg.min_flagship)
    } else if !chain_ok {
        format!("CHAIN:n_hit={}<{}", n_hit, cfg.min_books_pass)
    } else {
        String::new()
    };

    ProbeResult {
        source_id: sid,
        domain: host,
        book_source_name: source.book_source_name.clone(),
        content_hash: rec.content_hash.clone(),
        risk_level: level.clone(),
        check_keyword: kw,
        status,
        search_ok: gate_ok,
        search_status: gate_status.to_string(),
        search_detail: if gate_ok { "gate_ok".into() } else { gate.err.clone().unwrap_or_else(|| "gate_no_items".into()) },
        fail_stage,
        respond_time_ms: respond_ms,
        probe_count,
        book_source_name_camel: source.book_source_name.clone(),
        hit_classic,
        hit_hot,
        hit_waist,
        hit_longtail,
        head_gate_ok: verdict.head_ok,
        flagship_hit,
        hits,
        attempts,
        hit_titles,
        probed_at: now_iso(),
        steps: steps_all,
        steps_ok,
        behavior_events: events,
        behavior_level: level.clone(),
        behavior_reasons: reasons,
        blocked_hosts,
    }
}

// ---------------------------------------------------------------- main

fn parse_args() -> (String, String, String, Config) {
    let args: Vec<String> = env::args().collect();
    if args.len() < 4 {
        eprintln!("用法: probe-kernel <sources.json> <probe_books.json> <out.jsonl> [--concurrency N] [--timeout-secs N] [--read-timeout-secs N] [--per-source-secs N] [--global-budget-secs N] [--min-chapters N] [--min-content-chars N] [--min-books-pass N] [--min-head-rate F] [--min-hot-rate F] [--min-waist-rate F] [--min-flagship N] [--host-whitelist h1,h2] [--host-policy <path>]");
        std::process::exit(2);
    }
    let mut cfg = Config::default();
    let mut i = 4;
    while i < args.len() {
        let key = args[i].as_str();
        let val = args.get(i + 1).map(|s| s.as_str()).unwrap_or("");
        match key {
            "--concurrency" => cfg.concurrency = val.parse().unwrap_or(4),
            "--timeout-secs" => cfg.timeout_secs = val.parse().unwrap_or(12),
            "--read-timeout-secs" => cfg.read_timeout_secs = val.parse().unwrap_or(15),
            "--per-source-secs" => cfg.per_source_secs = val.parse().unwrap_or(75),
            "--global-budget-secs" => cfg.global_budget_secs = val.parse().unwrap_or(900),
            "--min-chapters" => cfg.min_chapters = val.parse().unwrap_or(3),
            "--min-content-chars" => cfg.min_content_chars = val.parse().unwrap_or(500),
            "--min-books-pass" => cfg.min_books_pass = val.parse().unwrap_or(1),
            "--min-head-rate" => cfg.min_head_rate = val.parse().unwrap_or(0.0),
            "--min-hot-rate" => cfg.min_hot_rate = val.parse().unwrap_or(0.0),
            "--min-waist-rate" => cfg.min_waist_rate = val.parse().unwrap_or(0.0),
            "--min-flagship" => cfg.min_flagship = val.parse().unwrap_or(0),
            // 兼容旧参数：--min-head-books 已废弃（本数口径），保留解析不报错、不再参与判定。
            "--min-head-books" => {
                let _ = val.parse::<u32>();
                eprintln!("[probe-kernel] 警告：--min-head-books 已废弃，请改用 --min-head-rate（比例口径）");
            }
            "--host-whitelist" => cfg.host_whitelist = val.split(',').map(|s| s.trim().to_lowercase()).filter(|s| !s.is_empty()).collect(),
            "--host-policy" => cfg.host_policy = Some(val.to_string()),
            _ => {}
        }
        i += 2;
    }
    (args[1].clone(), args[2].clone(), args[3].clone(), cfg)
}

fn main() {
    let (in_path, books_path, out_path, cfg) = parse_args();
    let start = Instant::now();

    // 出口白名单：模式来自环境变量（默认 observe），策略文件可选。
    // 必须在任何 JS 求值前初始化，否则 thread_local CTX 未登记，全部请求放行。
    let egress_mode = env::var("PROBE_EGRESS_MODE").unwrap_or_default();
    probe_kernel::crawler::egress::init(cfg.host_policy.as_deref(), &egress_mode);
    eprintln!("[probe-kernel] 出口白名单模式={} 策略文件={:?}",
        probe_kernel::crawler::egress::current_mode(), cfg.host_policy);

    let raw = fs::read_to_string(&in_path).expect("读取 sources.json 失败");
    let recs: Vec<SourceRec> = match serde_json::from_str::<Vec<SourceRec>>(&raw) {
        Ok(v) => v,
        Err(_) => {
            let arr: Vec<BookSource> = serde_json::from_str(&raw).expect("解析书源数组失败");
            arr.into_iter().map(|s| SourceRec {
                source_id: String::new(),
                content_hash: content_hash(&s),
                declared_hosts: Vec::new(),
                is_new: false,
                source: s,
            }).collect()
        }
    };
    let books_raw = fs::read_to_string(&books_path).expect("读取 probe_books.json 失败");
    let books: ProbeBooks = serde_json::from_str(&books_raw).expect("解析探针书单失败");
    eprintln!("[probe-kernel] 候选={} 探针=经典{}热门{}腰部{}长尾{}招牌{} 并发={} 阈值章节>={}正文>={} 总命中>={}本",
        recs.len(), books.classic.len(), books.hot.len(), books.waist.len(), books.longtail.len(),
        books.flagship.len(),
        cfg.concurrency, cfg.min_chapters, cfg.min_content_chars, cfg.min_books_pass);
    eprintln!("[probe-kernel] 门禁：头部率>={} 热门率>={} 腰尾率>={} 招牌>={}本",
        cfg.min_head_rate, cfg.min_hot_rate, cfg.min_waist_rate, cfg.min_flagship);
    eprintln!("[probe-kernel] 超时策略：连接 {}s / 读 {}s / 单源 {}s / 整轮预算 {}s",
        cfg.timeout_secs, cfg.read_timeout_secs, cfg.per_source_secs, cfg.global_budget_secs);

    let http = Arc::new(
        HttpClient::new_with_read_timeout(cfg.timeout_secs, cfg.read_timeout_secs, None)
            .expect("初始化 HTTP 客户端失败"),
    );
    let parser = Arc::new(RuleEngine::new().expect("初始化 RuleEngine 失败"));
    let out = Arc::new(Mutex::new(fs::File::create(&out_path).expect("创建输出失败")));
    let (sem_tx, sem_rx) = mpsc::channel::<()>();
    for _ in 0..cfg.concurrency.max(1) {
        let _ = sem_tx.send(());
    }
    let sem_rx = Arc::new(Mutex::new(sem_rx));
    let cfg = Arc::new(cfg);
    let books = Arc::new(books);

    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(cfg.concurrency.max(2))
        .enable_all()
        .build()
        .expect("创建 tokio 运行时失败");
    let handle = rt.handle().clone();

    let mut handles = Vec::new();
    let n_timeout = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    for rec in recs {
        let sem_rx = sem_rx.clone();
        let sem_tx = sem_tx.clone();
        let http = http.clone();
        let parser = parser.clone();
        let out = out.clone();
        let cfg = cfg.clone();
        let books = books.clone();
        let handle = handle.clone();
        let n_timeout = n_timeout.clone();
        handles.push(std::thread::spawn(move || {
            let _permit = acquire_permit(&sem_rx, &sem_tx);
            // 整轮预算耗尽后不再开探测：直接出一行 TIMEOUT 占位，保证 matrix
            // 行数与 targets 对齐。排队等待 permit 的时间也算进预算，
            // 因为墙钟就是这么流逝的。
            let result = if start.elapsed() >= Duration::from_secs(cfg.global_budget_secs) {
                timeout_result(&rec, "TIMEOUT:global_budget")
            } else {
                let deadline = Instant::now() + Duration::from_secs(cfg.per_source_secs);
                probe_source(&rec, &books, &http, &handle, &parser, &cfg, deadline)
            };
            if result.status == "TIMEOUT" {
                n_timeout.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            }
            let line = serde_json::to_string(&result).expect("序列化结果失败");
            let mut f = out.lock().expect("输出锁被污染");
            writeln!(f, "{}", line).expect("写输出失败");
            eprintln!("[probe-kernel] {} {} status={} steps_ok={} risk={}",
                result.book_source_name, result.domain, result.status, result.steps_ok, result.risk_level);
        }));
    }
    for h in handles { let _ = h.join(); }
    eprintln!(
        "[probe-kernel] 完成，输出 {}；TIMEOUT={} 个（未算作失败，下一轮重测），总耗时 {}s",
        out_path,
        n_timeout.load(std::sync::atomic::Ordering::Relaxed),
        start.elapsed().as_secs()
    );
}

#[cfg(test)]
mod gate_tests {
    use super::{evaluate_gate, min_count, GateOutcome};

    /// 第二轮实际生效的阈值：头 0.5 / 热 0.7 / 腰尾 0.4 / 招牌 2。
    const HEAD: f64 = 0.5;
    const HOT: f64 = 0.7;
    const WAIST: f64 = 0.4;
    const FLAG: u32 = 2;

    fn gate(c: u32, h: u32, w: u32, l: u32, f: u32) -> GateOutcome {
        evaluate_gate(c, 10, h, 10, w, l, 20, f, HEAD, HOT, WAIST, FLAG)
    }

    // ---- min_count ----

    #[test]
    fn min_count_ceil_rounds_up() {
        assert_eq!(min_count(0.5, 10), 5);
        assert_eq!(min_count(0.7, 10), 7);
        assert_eq!(min_count(0.4, 20), 8);
        // 0.45*10=4.5 → ceil 5，不能取 4
        assert_eq!(min_count(0.45, 10), 5);
    }

    #[test]
    fn min_count_zero_rate_or_len_is_zero() {
        assert_eq!(min_count(0.0, 10), 0);
        assert_eq!(min_count(-0.1, 10), 0);
        assert_eq!(min_count(0.5, 0), 0);
    }

    // ---- 头部（只算 classic）----

    #[test]
    fn head_exactly_five_of_ten_passes() {
        let g = gate(5, 0, 0, 0, 0);
        assert!(g.head_ok, "5/10 = 0.5 达标");
    }

    #[test]
    fn head_four_of_ten_fails() {
        let g = gate(4, 0, 0, 0, 0);
        assert!(!g.head_ok, "4/10 = 0.4 < 0.5 应挡");
    }

    #[test]
    fn head_ignores_hot_hits() {
        // classic 只有 1 本，hot 打满 10 —— 头部依然不达标，证明 hot 不掺进头部
        let g = gate(1, 10, 0, 0, 0);
        assert!(!g.head_ok, "头部口径只算 classic，hot 打满也不能救");
    }

    // ---- 热门（第一轮 min_hot_rate=0 关闭；第二轮 0.7）----

    #[test]
    fn hot_zero_rate_disables_gate() {
        let g = evaluate_gate(0, 10, 0, 10, 0, 0, 20, 0, HEAD, 0.0, WAIST, FLAG);
        assert!(g.hot_ok, "min_hot_rate=0 时热门门禁恒真（第一轮只采集）");
    }

    #[test]
    fn hot_seven_of_ten_passes_six_fails() {
        assert!(gate(10, 7, 10, 10, 2).hot_ok, "7/10 = 0.7 达标");
        assert!(!gate(10, 6, 10, 10, 2).hot_ok, "6/10 = 0.6 < 0.7 应挡");
    }

    // ---- 腰尾（waist+longtail 合计 / 20）----

    #[test]
    fn waist_longtail_summed_against_twenty() {
        // waist 4 + longtail 4 = 8/20 = 0.4 达标（单看任一层都不够）
        assert!(gate(10, 10, 4, 4, 2).waist_ok);
        // 7/20 = 0.35 < 0.4 应挡
        assert!(!gate(10, 10, 4, 3, 2).waist_ok);
    }

    // ---- 招牌 ----

    #[test]
    fn flagship_two_passes_one_fails() {
        assert!(gate(10, 10, 10, 10, 2).flag_ok);
        assert!(!gate(10, 10, 10, 10, 1).flag_ok);
    }

    #[test]
    fn flagship_zero_required_disables_gate() {
        let g = evaluate_gate(0, 10, 0, 10, 0, 0, 20, 0, 0.0, 0.0, 0.0, 0);
        assert!(g.flag_ok && g.head_ok && g.hot_ok && g.waist_ok, "全 0 阈值 = 全关");
    }

    // ---- 组合：全过才算 PASSED 的语义 ----

    #[test]
    fn all_conditions_must_hold() {
        // 头部够、热门够、招牌够，但腰尾 7/20 不够 → 整体不过
        let g = gate(5, 7, 4, 3, 2);
        assert!(g.head_ok && g.hot_ok && g.flag_ok && !g.waist_ok);
    }
}
