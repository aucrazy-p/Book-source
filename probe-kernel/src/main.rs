//! probe-kernel：书源四步探测（搜索 → 详情 → 目录 → 正文）+ L4 行为观测。
//!
//! 用法：
//!   probe-kernel <sources.json> <probe_books.json> <out.jsonl> [options]
//!
//! options:
//!   --concurrency N        并发源数，默认 4
//!   --timeout-secs N       单请求连接超时，默认 12
//!   --read-timeout-secs N  单请求读超时，默认 15（阅读器仍用 60s）
//!   --per-source-secs N    单源墙钟上限（gate + 30 本），默认 75；超时判 TIMEOUT
//!   --global-budget-secs N 整轮墙钟预算，默认 900；超出后剩余源直接判 TIMEOUT
//!   --min-chapters N       目录可读章节阈值，默认 3
//!   --min-content-chars N  正文字数阈值，默认 500
//!   --min-books-pass N     PASSED 门槛：至少 N 本书跑通四步，默认 1
//!   --min-head-books N     头部门槛：classic+hot 至少 N 本四步全过，默认 1（见 §17.1 B-2）
//!   --host-whitelist h1,h2 允许跨域的目标主机
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
    /// 方案 §17.1 B-2 门禁：头部命中率是否达标（供诊断 PARTIAL 原因）。
    head_gate_ok: bool,
    hits: HashMap<String, u32>,
    attempts: HashMap<String, u32>,
    hit_titles: Vec<String>,
    probed_at: String,
    steps: HashMap<String, StepResult>,
    steps_ok: u8,
    behavior_events: Vec<behavior::BehaviorEvent>,
    behavior_level: String,
    behavior_reasons: Vec<String>,
}

#[derive(Debug, Clone)]
struct Config {
    concurrency: usize,
    timeout_secs: u64,
    /// 单请求读超时。探测路径用短值（默认 15s），否则几十个源 × 上百个请求
    /// 按 60s 累加就能吃穿整轮墙钟。阅读器不受影响（仍 60s）。
    read_timeout_secs: u64,
    /// 单源墙钟上限：gate + 30 本探针书。超时判 `TIMEOUT`，保留已采部分结果。
    /// 依据历史均速（402s / 60 源 ≈ 27s/源）留 ~3 倍余量。
    per_source_secs: u64,
    /// 整轮墙钟预算：超预算的源不再探测，直接产 `TIMEOUT` 行占位。
    /// 默认 900s = CI 容器 `timeout 1800s` 的一半，留足安全垫。
    global_budget_secs: u64,
    min_chapters: usize,
    min_content_chars: usize,
    /// PASSED 门槛：至少 N 本探针书完整跑通四步。默认 1（证明链路真实可用即合格；
    /// 覆盖度由 hit_* 单独统计，不混进 PASSED 判定）。想更严可上调。
    min_books_pass: u32,
    /// 方案 §17.1 B-2：头部（classic+hot）四步全过的最少本数，用于排除坏源。
    /// 默认 1 —— 头部 20 本至少 1 本真跑通即可；非小说站/凑结构的站头部命中 0，
    /// 会被这道门禁挡住。实测用「命中率≥0.3」会误杀真源（机房 IP 下真源头部仅 2~4/20），
    /// 故按方案原意「头部排除坏源」改为本数门槛。设为 0 关闭。
    min_head_books: u32,
    host_whitelist: Vec<String>,
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
            min_head_books: 1,
            host_whitelist: Vec::new(),
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
        hits: HashMap::new(),
        attempts: HashMap::new(),
        hit_titles: Vec::new(),
        probed_at: now_iso(),
        steps: HashMap::new(),
        steps_ok: 0,
        behavior_events: Vec::new(),
        behavior_level: "unknown".into(),
        behavior_reasons: vec!["global_budget_exhausted".into()],
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
    let chain_ok = n_hit >= cfg.min_books_pass;
    // 方案 §17.1 B-2：头部层（classic+hot）用于排除坏源。
    // 头部门禁 = 头部四步全过的本数 >= min_head_books（默认 1）。
    // 非小说站头部命中 0 → 拦；真源头部普遍 2~4 本 → 过。
    // 长尾命中率仍是「书全」主指标，交给 04_select 算边际，不参与 PASSED 门禁。
    let head_pass = hits.get("classic").copied().unwrap_or(0)
        + hits.get("hot").copied().unwrap_or(0);
    let head_gate_ok = head_pass >= cfg.min_head_books;
    let mut status = if !gate_ok {
        gate_status.to_string()
    } else if chain_ok && head_gate_ok {
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
    // 方案 §17.1 A-4：respond_time_ms > 5000 降级，用单源平均耗时，
    // 不是 total_ms（50 本书累计必然超 5s，会把所有源误降 suspicious）。
    let respond_ms = if probe_count > 0 { total_ms / probe_count as u64 } else { 0 };
    let (mut level, reasons) = judge_with_timeout(&host, &events, respond_ms, cfg);
    if level == "clean" && status == "PARTIAL" { level = "suspicious".into(); }

    // fail_stage 决定下游日志的可读性；TIMEOUT 要能区分是单源超时还是整轮预算。
    let fail_stage = if status == "TIMEOUT" {
        format!("TIMEOUT:per_source:probed={}", probe_count)
    } else if !gate_ok {
        format!("SEARCH:{}", gate_status)
    } else if !head_gate_ok {
        format!("HEAD_GATE:head_pass={}<{}", head_pass, cfg.min_head_books)
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
        head_gate_ok,
        hits,
        attempts,
        hit_titles,
        probed_at: now_iso(),
        steps: steps_all,
        steps_ok,
        behavior_events: events,
        behavior_level: level.clone(),
        behavior_reasons: reasons,
    }
}

// ---------------------------------------------------------------- main

fn parse_args() -> (String, String, String, Config) {
    let args: Vec<String> = env::args().collect();
    if args.len() < 4 {
        eprintln!("用法: probe-kernel <sources.json> <probe_books.json> <out.jsonl> [--concurrency N] [--timeout-secs N] [--read-timeout-secs N] [--per-source-secs N] [--global-budget-secs N] [--min-chapters N] [--min-content-chars N] [--min-books-pass N] [--min-head-books N] [--host-whitelist h1,h2]");
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
            "--min-head-books" => cfg.min_head_books = val.parse().unwrap_or(1),
            "--host-whitelist" => cfg.host_whitelist = val.split(',').map(|s| s.trim().to_lowercase()).filter(|s| !s.is_empty()).collect(),
            _ => {}
        }
        i += 2;
    }
    (args[1].clone(), args[2].clone(), args[3].clone(), cfg)
}

fn main() {
    let (in_path, books_path, out_path, cfg) = parse_args();
    let start = Instant::now();

    let raw = fs::read_to_string(&in_path).expect("读取 sources.json 失败");
    let recs: Vec<SourceRec> = match serde_json::from_str::<Vec<SourceRec>>(&raw) {
        Ok(v) => v,
        Err(_) => {
            let arr: Vec<BookSource> = serde_json::from_str(&raw).expect("解析书源数组失败");
            arr.into_iter().map(|s| SourceRec {
                source_id: String::new(),
                content_hash: content_hash(&s),
                source: s,
            }).collect()
        }
    };
    let books_raw = fs::read_to_string(&books_path).expect("读取 probe_books.json 失败");
    let books: ProbeBooks = serde_json::from_str(&books_raw).expect("解析探针书单失败");
    eprintln!("[probe-kernel] 候选={} 探针=经典{}热门{}腰部{}长尾{} 并发={} 阈值章节>={}正文>={} 门禁头部>={}本 总命中>={}本",
        recs.len(), books.classic.len(), books.hot.len(), books.waist.len(), books.longtail.len(),
        cfg.concurrency, cfg.min_chapters, cfg.min_content_chars, cfg.min_head_books, cfg.min_books_pass);
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
