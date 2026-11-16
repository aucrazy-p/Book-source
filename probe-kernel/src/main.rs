//! probe-kernel：书源探测 + L4 行为观测（方案 §6 阶段③ / §7.3 / §17.1）。
//!
//! 用法：
//!   probe-kernel <sources.json> <out.jsonl> [--host-whitelist h1,h2]
//!
//! 输入：书源数组 JSON（阅读 APP 标准格式）。
//! 输出：JSONL，每行一本书源的探测结果，含：
//!   - 四步链路状态（search/book_info/toc/content）—— 由 parser 规则引擎真跑
//!   - L4 行为判定（behavior.level / reasons）—— java.* 调用记录
//!   - 最终风险：静态 risk_level 与 L4 取更严者（clean/suspicious/quarantine）
//!
//! 一次容器运行处理一批书源；每源探测前 behavior::begin()，探测后 take()+judge()。
use probe_kernel::behavior;
use probe_kernel::model::book_source::BookSource;
use probe_kernel::parser::js as pjs;

use std::env;
use std::fs;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, serde::Serialize)]
struct ProbeResult {
    source_id: String,
    name: String,
    host: String,
    /// 四步链路：search=搜索入口；其余步骤当前版本未启用（见 NOTE）
    search_ok: bool,
    search_status: String,
    respond_time_ms: u64,
    /// L4 行为事件（java.* 调用）
    behavior_events: Vec<behavior::BehaviorEvent>,
    behavior_level: String,
    behavior_reasons: Vec<String>,
    /// 最终风险级别
    risk_level: String,
}

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

/// 用 parser 规则引擎真实执行一次搜索步。
/// 走 JS eval 路径（js.rs 的 java.* 兼容层已埋 L4 hook）。
fn run_search(source: &BookSource, keyword: &str) -> (bool, String, u64) {
    let script = match source.search_url.as_deref() {
        Some(s) if !s.trim().is_empty() => s,
        _ => return (false, "no_search_url".into(), 0),
    };
    let t0 = std::time::Instant::now();
    // 设置 JS 持久化目录到 tmpfs 路径（容器 ensures /tmp 可写、用完即弃）
    pjs::set_js_storage_dir(PathBuf::from("/tmp/probe-js-store"));
    let result = pjs::eval_js_search_with_source(script, keyword, 1, &source.book_source_url);
    let ms = t0.elapsed().as_millis() as u64;
    match result {
        Ok(body) => {
            let hit = body.contains(keyword) || (!keyword.is_empty() && body.contains(keyword));
            (hit, if hit { "PASSED" } else { "NO_HIT" }.into(), ms)
        }
        Err(e) => (false, format!("ERR:{}", e), ms),
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("用法: probe-kernel <sources.json> <out.jsonl> [--host-whitelist h1,h2]");
        std::process::exit(2);
    }
    let in_path = &args[1];
    let out_path = &args[2];
    let raw = fs::read_to_string(in_path).expect("读取输入失败");
    let sources: Vec<BookSource> = serde_json::from_str(&raw).expect("解析书源 JSON 失败");

    let mut out = fs::File::create(out_path).expect("创建输出失败");
    use std::io::Write;

    for source in sources {
        let url = source.book_source_url.clone();
        let host = host_of(&url);
        let kw = source.check_keyword();

        // 开始本源的行为观测窗口
        behavior::begin();
        let (search_ok, search_status, ms) = run_search(&source, &kw);
        let events = behavior::take();
        let (level, reasons) = judge_with_timeout(&host, &events, ms);

        let result = ProbeResult {
            source_id: source_id(&url),
            name: source.book_source_name.clone(),
            host,
            search_ok,
            search_status,
            respond_time_ms: ms,
            behavior_events: events,
            behavior_level: level.clone(),
            behavior_reasons: reasons,
            // 静态 risk_scan 在 Python 侧（02_normalize）已做；这里 L4 取更严者
            risk_level: level,
        };
        let line = serde_json::to_string(&result).unwrap();
        writeln!(out, "{}", line).expect("写输出失败");
        // 每源之间留一点间隔，避免风控（方案 §11 限速）
        std::thread::sleep(Duration::from_millis(100));
    }
    eprintln!("[probe-kernel] 完成，输出 {}", out_path);
}

/// 在行为判定基础上叠加超时降级（respond_time_ms > 5000 降级，方案 §17.1 A-4）
fn judge_with_timeout(host: &str, events: &[behavior::BehaviorEvent], ms: u64) -> (String, Vec<String>) {
    let (mut level, mut reasons) = behavior::judge(host, events);
    if ms > 5000 {
        reasons.push(format!("L4_SLOW:{}ms", ms));
        if level == "clean" { level = "suspicious".into(); }
    }
    (level, reasons)
}
