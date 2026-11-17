//! L4 行为观测（方案 §7.3 / §11）：记录书源 JS 通过 java.* 兼容层触发的
//! 一切外部行为，供容器外层判定 clean / quarantine。
//!
//! 设计要点（对齐方案 §7.2 威胁模型）：
//! - 唯一埋点：java.* 兼容层的所有危险能力都从 parser/js.rs 过，在那里调用本模块的 record()。
//! - 结构化：每条带 api / url / method / body_len / source_id，可回放（evidence）。
//! - 线程级累积、按需取走：一次探测结束后 take() 清空，避免跨源串台。
//!   用 thread_local 而非全局 Mutex：并发探测时每个源一个线程，事件必须隔离，
//!   否则 A 源的 java.* 会被算到 B 源头上（L4 判定串味）。
//! - 零 I/O：只写内存，落盘/上报由 main 决定，避免观测本身引入副作用。
use std::cell::RefCell;
use std::time::{SystemTime, UNIX_EPOCH};

/// 一条行为事件。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct BehaviorEvent {
    /// 事件时间（Unix 毫秒）
    pub ts_ms: u64,
    /// 触发的 java.* API 名，如 java.ajax / java.getCookie / js_store.put
    pub api: String,
    /// 目标 URL（若有）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    /// HTTP 方法（若有）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub method: Option<String>,
    /// 请求体长度（若有）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body_len: Option<usize>,
    /// 补充证据（key/域名/片段），用于判定与回放
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence: Option<String>,
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

thread_local! {
    static EVENTS: RefCell<Vec<BehaviorEvent>> = const { RefCell::new(Vec::new()) };
}

fn with_events<T>(f: impl FnOnce(&mut Vec<BehaviorEvent>) -> T) -> T {
    EVENTS.with(|cell| f(&mut cell.borrow_mut()))
}

/// 由 java.* 兼容层调用：记录一次危险能力触发。
pub fn record(
    api: impl Into<String>,
    url: Option<impl Into<String>>,
    method: Option<impl Into<String>>,
    body_len: Option<usize>,
    evidence: Option<impl Into<String>>,
) {
    let ev = BehaviorEvent {
        ts_ms: now_ms(),
        api: api.into(),
        url: url.map(|s| s.into()),
        method: method.map(|s| s.into()),
        body_len,
        evidence: evidence.map(|s| s.into()),
    };
    with_events(|v| v.push(ev));
}

/// 开始一轮观测（清空上一轮残留）。每个书源探测前调用。
pub fn begin() {
    EVENTS.with(|cell| cell.borrow_mut().clear());
}

/// 取走并清空当前所有事件。探测结束后调用。
pub fn take() -> Vec<BehaviorEvent> {
    EVENTS.with(|cell| std::mem::take(&mut *cell.borrow_mut()))
}

/// 基于方案 §7.3 L4 规则的行为判定。
/// 返回 (level, reasons)：level ∈ clean / suspicious / quarantine。
///
/// 注意：这是"执行期行为"判定，与静态 risk_scan 互补；两者取更严者作为最终风险级别。
/// §7.4 误报控制：java.ajax 翻页、getCookie 保登录是正常功能，
/// 只有"跨域 + 带凭据外发"才判最高危。这里只标可疑证据，最终阈值由外层（main/网关）定。
pub fn judge(source_host: &str, events: &[BehaviorEvent]) -> (String, Vec<String>) {
    let mut score = 0i32;
    let mut reasons = Vec::new();
    let host = source_host.trim().trim_start_matches("www.").to_lowercase();

    for ev in events {
        let api = ev.api.as_str();
        let target_host = ev
            .url
            .as_deref()
            .and_then(|u| extract_host(u))
            .unwrap_or_default();
        let is_cross = !target_host.is_empty()
            && !target_host.trim_start_matches("www.").eq(&host)
            && !target_host.ends_with(&format!(".{}", host));

        // 数据外带：跨域 POST / 带 body 外发 → 最高危
        if matches!(api, "java.ajax" | "java.post" | "java.get") {
            if is_cross {
                let post = ev.method.as_deref().map(|m| m.eq_ignore_ascii_case("POST")).unwrap_or(false);
                if post || ev.body_len.unwrap_or(0) > 0 {
                    score += 60;
                    reasons.push(format!("L4_EXFIL:{}→{}({}B)", api, target_host, ev.body_len.unwrap_or(0)));
                } else {
                    score += 10;
                    reasons.push(format!("L4_CROSS_GET:{}→{}", api, target_host));
                }
            }
        }
        // 读凭据
        if matches!(api, "java.getCookie" | "java.removeCookie" | "java.read_all_cookies") {
            score += 15;
            reasons.push(format!("L4_CREDENTIAL:{}", api));
        }
        // 拉浏览器 / WebView / 子进程
        if matches!(api, "java.startBrowser" | "java.startBrowserAwait" | "java.webView" | "java.__binary") {
            score += 40;
            reasons.push(format!("L4_LAUNCH:{}", api));
        }
        // 持久化写
        if matches!(api, "js_store.put" | "java.putCache") {
            score += 20;
            reasons.push(format!("L4_PERSIST:{}", api));
        }
        // 环境变量 / 仓库文件读取
        if matches!(api, "java.getenv" | "java.read_file" | "java.env") {
            score += 80;
            reasons.push(format!("L4_SENSITIVE_READ:{}", api));
        }
        // 解码载荷（混淆特征，权重低，需与其他特征叠加）
        if matches!(api, "java.base64Decode" | "java.aesDecodeToString") {
            score += 5;
            reasons.push(format!("L4_DECODE:{}", api));
        }
    }

    let level = if score >= 60 {
        "quarantine"
    } else if score >= 15 {
        "suspicious"
    } else {
        "clean"
    };
    (level.to_string(), reasons)
}

fn extract_host(url: &str) -> Option<String> {
    let s = url.split('#').next().unwrap_or(url).trim();
    let rest = s.split_once("://").map(|(_, r)| r).unwrap_or(s);
    let host = rest.split('/').next().unwrap_or("").split('@').last().unwrap_or("");
    let host = host.split(':').next().unwrap_or(host);
    if host.is_empty() { None } else { Some(host.to_lowercase()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn judge_exfil_quarantines() {
        let evs = vec![BehaviorEvent {
            ts_ms: 0, api: "java.ajax".into(),
            url: Some("https://evil.com/collect".into()),
            method: Some("POST".into()), body_len: Some(2048),
            evidence: None,
        }];
        let (level, _) = judge("mydomain.com", &evs);
        assert_eq!(level, "quarantine");
    }
    #[test]
    fn judge_same_host_get_is_clean() {
        let evs = vec![BehaviorEvent {
            ts_ms: 0, api: "java.ajax".into(),
            url: Some("https://mydomain.com/api/book".into()),
            method: Some("GET".into()), body_len: None, evidence: None,
        }];
        let (level, _) = judge("mydomain.com", &evs);
        assert_eq!(level, "clean");
    }
}
