//! probe-kernel：书源四步探测（搜索 → 详情 → 目录 → 正文）+ L4 行为观测。
//!
//! 用法：
//!   probe-kernel <sources.json> <probe_books.json> <out.jsonl> [options]
//!
//! options:
//!   --concurrency N        并发源数，默认 4
//!   --timeout-secs N       单请求超时，默认 12
//!   --min-chapters N       目录可读章节阈值，默认 3
//!   --min-content-chars N  正文字数阈值，默认 500
//!   --min-books-pass N     PASSED 门槛：至少 N 本书跑通四步，默认 1
//!   --host-whitelist h1,h2 允许跨域的目标主机
//!
//! 判定口径（方案 §17.1）：
//!   - 命中 = RuleEngine 从响应解析出真实结构化条目，不是字符串包含
//!   - 单本命中要求四步严格全过；源 PASSED = 至少 min_books_pass 本四步全过
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
}

#[derive(Debug, Deserialize, Clone)]
struct SourceRec {
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
    hit_classic: f64,
    hit_hot: f64,
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
    min_chapters: usize,
    min_content_chars: usize,
    /// PASSED 门槛：至少 N 本探针书完整跑通四步。默认 1（证明链路真实可用即合格；
    /// 覆盖度由 hit_* 单独统计，不混进 PASSED 判定）。想更严可上调。
    min_books_pass: u32,
    host_whitelist: Vec<String>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            concurrency: 4,
            timeout_secs: 12,
            min_chapters: 3,
            min_content_chars: 500,
            min_books_pass: 1,
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

fn name_match(actual: &str, expected: &str) -> bool {
    let a = main_book(actual.trim());
    let e = main_book(expected.trim());
    if a.is_empty() || e.is_empty() { return false; }
    a == e || (a.chars().count() >= 2 && e.chars().count() >= 2 && (a.contains(&e) || e.contains(&a)))
}

fn looks_blocked(text: &str) -> bool {
    let lower = text.to_lowercase();
    ["tcaptcha", "turing.captcha", "验证码", "captcha"].iter().any(|p| lower.contains(p))
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
    let matched: Vec<String> = books.iter().filter(|b| name_match(&b.name, &book.title)).map(|b| b.name.clone()).collect();
    if matched.is_empty() {
        let why = if books.is_empty() { "no_result_items" } else { "no_title_match" };
        steps.insert("search".into(), fail_step("SEARCH_PARSE", 0, why));
        return (steps, false, total_ms);
    }
    let picked = books.iter().find(|b| name_match(&b.name, &book.title)).cloned().unwrap();
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
    if info.name.trim().is_empty() || !name_match(&info.name, &book.title) {
        steps.insert("detail".into(), fail_step("DETAIL_PARSE", 0, &format!("bad_name:{}", info.name)));
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
    let content = pjs::with_book_context(Some(pjs::JsBookContext {
        book: serde_json::json!({"bookUrl": detail_url, "tocUrl": toc_url}),
        chapter: serde_json::json!({"title": readable[0].title, "url": chapter_url}),
    }), || with_source_ctx(source, || parser.content(source, &cresp.body, &chapter_url, Some(&detail_url))));
    let chars = content.chars().filter(|c| !c.is_whitespace()).count();
    if chars < cfg.min_content_chars {
        steps.insert("content".into(), fail_step("CONTENT_PARSE", 0, &format!("too_short:{}", chars)));
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

fn probe_source(
    rec: &SourceRec,
    books: &ProbeBooks,
    http: &HttpClient,
    rt: &tokio::runtime::Handle,
    parser: &RuleEngine,
    cfg: &Config,
) -> ProbeResult {
    let source = &rec.source;
    let url = source.book_source_url.clone();
    let host = host_of(&url);
    let kw = source.check_keyword();

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

    if gate_ok {
        let mut tier_books: Vec<(&str, &ProbeBook)> = Vec::new();
        for b in &books.classic { tier_books.push(("classic", b)); }
        for b in &books.hot { tier_books.push(("hot", b)); }
        for (tier, b) in tier_books {
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
    let hit_classic = if books.classic.is_empty() { 0.0 } else {
        (hits.get("classic").copied().unwrap_or(0) as f64 / books.classic.len() as f64 * 1000.0).round() / 1000.0
    };
    let hit_hot = if books.hot.is_empty() { 0.0 } else {
        (hits.get("hot").copied().unwrap_or(0) as f64 / books.hot.len() as f64 * 1000.0).round() / 1000.0
    };
    let chain_ok = n_hit >= cfg.min_books_pass;
    let status = if !gate_ok {
        gate_status.to_string()
    } else if chain_ok {
        "PASSED".into()
    } else {
        "PARTIAL".into()
    };
    // steps_ok：有书完整跑通四步即为 4；否则按命中书数给一个降级分（0..3）。
    let steps_ok = if chain_ok { 4 } else { std::cmp::min(3, n_hit) as u8 };

    let events = behavior::take();
    let (mut level, reasons) = judge_with_timeout(&host, &events, total_ms, cfg);
    if level == "clean" && status == "PARTIAL" { level = "suspicious".into(); }

    ProbeResult {
        source_id: source_id(&url),
        domain: host,
        book_source_name: source.book_source_name.clone(),
        content_hash: rec.content_hash.clone(),
        risk_level: level.clone(),
        check_keyword: kw,
        status,
        search_ok: gate_ok,
        search_status: gate_status.to_string(),
        search_detail: if gate_ok { "gate_ok".into() } else { gate.err.clone().unwrap_or_else(|| "gate_no_items".into()) },
        fail_stage: if gate_ok { String::new() } else { format!("SEARCH:{}", gate_status) },
        respond_time_ms: if probe_count > 0 { total_ms / probe_count as u64 } else { 0 },
        probe_count,
        hit_classic,
        hit_hot,
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
        eprintln!("用法: probe-kernel <sources.json> <probe_books.json> <out.jsonl> [--concurrency N] [--timeout-secs N] [--min-chapters N] [--min-content-chars N] [--min-books-pass N] [--host-whitelist h1,h2]");
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
            "--min-chapters" => cfg.min_chapters = val.parse().unwrap_or(3),
            "--min-content-chars" => cfg.min_content_chars = val.parse().unwrap_or(500),
            "--min-books-pass" => cfg.min_books_pass = val.parse().unwrap_or(1),
            "--host-whitelist" => cfg.host_whitelist = val.split(',').map(|s| s.trim().to_lowercase()).filter(|s| !s.is_empty()).collect(),
            _ => {}
        }
        i += 2;
    }
    (args[1].clone(), args[2].clone(), args[3].clone(), cfg)
}

fn main() {
    let (in_path, books_path, out_path, cfg) = parse_args();

    let raw = fs::read_to_string(&in_path).expect("读取 sources.json 失败");
    let recs: Vec<SourceRec> = match serde_json::from_str::<Vec<SourceRec>>(&raw) {
        Ok(v) => v,
        Err(_) => {
            let arr: Vec<BookSource> = serde_json::from_str(&raw).expect("解析书源数组失败");
            arr.into_iter().map(|s| SourceRec {
                content_hash: content_hash(&s),
                source: s,
            }).collect()
        }
    };
    let books_raw = fs::read_to_string(&books_path).expect("读取 probe_books.json 失败");
    let books: ProbeBooks = serde_json::from_str(&books_raw).expect("解析探针书单失败");
    eprintln!("[probe-kernel] 候选={} 探针=经典{}热门{} 并发={} 阈值章节>={}正文>={}",
        recs.len(), books.classic.len(), books.hot.len(), cfg.concurrency, cfg.min_chapters, cfg.min_content_chars);

    let http = Arc::new(HttpClient::new(cfg.timeout_secs, None).expect("初始化 HTTP 客户端失败"));
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
    for rec in recs {
        let sem_rx = sem_rx.clone();
        let sem_tx = sem_tx.clone();
        let http = http.clone();
        let parser = parser.clone();
        let out = out.clone();
        let cfg = cfg.clone();
        let books = books.clone();
        let handle = handle.clone();
        handles.push(std::thread::spawn(move || {
            let _permit = acquire_permit(&sem_rx, &sem_tx);
            let result = probe_source(&rec, &books, &http, &handle, &parser, &cfg);
            let line = serde_json::to_string(&result).expect("序列化结果失败");
            let mut f = out.lock().expect("输出锁被污染");
            writeln!(f, "{}", line).expect("写输出失败");
            eprintln!("[probe-kernel] {} {} status={} steps_ok={} risk={}",
                result.book_source_name, result.domain, result.status, result.steps_ok, result.risk_level);
        }));
    }
    for h in handles { let _ = h.join(); }
    eprintln!("[probe-kernel] 完成，输出 {}", out_path);
}
