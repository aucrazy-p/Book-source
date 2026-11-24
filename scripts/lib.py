# -*- coding: utf-8 -*-
"""公共库：书源归一化、去重哈希、搜索 URL 构造、编码解码。

复用 book-source-validator/scripts/step4_search.py 的 parse_search_url 与
decode_body 思路，改为独立可复用函数；不修改技能原文件。
纯标准库，Python 3.8+。
"""
import hashlib
import json
import os
import re
import ssl
import urllib.parse
from urllib.request import Request, build_opener, HTTPSHandler, ProxyHandler

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 运行产物根目录：CI 上设 LEGADO_SY_RUNS_DIR=<workspace>/runs；本地默认走 WorkSync 产物目录。
# 所有脚本统一调用本函数，避免把 D:\WorkSync 绝对路径写死导致 CI 上写飞。
def runs_root():
    env = os.environ.get("LEGADO_SY_RUNS_DIR")
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    return os.path.abspath(os.path.join(
        repo, os.pardir, os.pardir, "WorkSync", "projects", "legado-sy", "runs"))


def run_dir(run_id):
    return os.path.join(runs_root(), run_id)

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE
# 探测书源用：跳过证书校验、不走代理（对齐 legado 行为）
DIRECT = build_opener(ProxyHandler({}), HTTPSHandler(context=_ssl_ctx))
# 抓公开网页（如豆瓣书单）用：尊重系统/环境代理，适配本机与 CI 两种环境
from urllib.request import build_opener as _bo
SYSTEM_OPENER = _bo()

# 整理者尾标：##@遇知 / #妍希 / 已整理 / @某某 等
_TAIL_MARKS = re.compile(
    r"(?:\s*(?:##?|＃＃?|@)\s*[^\s/#@]{1,20}|\s*已整理|\s*整理)\s*$"
)


def extract_domain(url):
    """取主域名（小写，去掉 # 后的注释、去掉端口）。"""
    if not url:
        return ""
    u = str(url).split("#")[0].strip()
    m = re.match(r"https?://([^/:]+)", u, re.I)
    return m.group(1).lower() if m else ""


def normalize_url(url):
    """归一化书源 URL 作为去重主键基础。"""
    if not url:
        return ""
    u = str(url).split("#")[0].strip()
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = u.rstrip("/")
    u = _TAIL_MARKS.sub("", u)
    try:
        p = urllib.parse.urlsplit("http://" + u)
        q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        q = [(k, v) for k, v in q
             if k.lower() not in ("utm_source", "utm_medium", "utm_campaign",
                                  "from", "ref", "t", "timestamp")]
        path = "" if p.path in ("", "/") else p.path
        u = p.netloc.lower() + path
        if q:
            u += "?" + urllib.parse.urlencode(q)
    except Exception:
        u = u.lower()
    return u.strip()


def source_id(url):
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:16]


CONTENT_FIELDS = (
    "ruleSearch", "ruleBookInfo", "ruleToc", "ruleContent",
    "searchUrl", "exploreUrl", "header", "jsLib", "ruleExplore",
    "loginCheckJs", "bookUrlPattern",
)


def content_hash(source):
    """规范化规则字段集合的哈希，用于抓「换皮/改名复用」。"""
    obj = {}
    for k in CONTENT_FIELDS:
        v = source.get(k)
        if v not in (None, "", [], {}):
            obj[k] = re.sub(r"\s+", "", str(v))
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def parse_search_url(source, keyword):
    """解析 searchUrl 模板，返回 (url, method, body) 或 None。
    支持普通 URL / URL,{json配置} / {{key}} / {{page}}；动态 JS / webView 返回 None。
    """
    search_url = str(source.get("searchUrl", "") or "").strip()
    if not search_url or re.match(r"^(@js|js:|<js>|JS:)", search_url):
        return None
    method, body = "GET", None
    m = re.search(r",\s*(\{.*\})\s*$", search_url, re.S)
    if m:
        try:
            cfg = json.loads(m.group(1))
            if isinstance(cfg, dict):
                if str(cfg.get("method", "GET")).upper() in ("GET", "POST"):
                    method = str(cfg["method"]).upper()
                if cfg.get("body"):
                    body = str(cfg["body"])
                if cfg.get("webView"):
                    return None
        except Exception:
            pass
        search_url = search_url[:m.start()].strip()

    # 保护 {{key}} 与 {{page}}，移除其余 JS 模板块
    def _protect(text):
        text = re.sub(r"\{\{key\}\}", "\x00KEY\x00", text, flags=re.I)
        text = re.sub(r"\{\{page[^}]*\}\}", "\x00PAGE\x00", text, flags=re.I)
        return text

    def _restore(text):
        return text.replace("\x00KEY\x00", "{{key}}").replace("\x00PAGE\x00", "{{page}}")

    search_url = _protect(search_url)
    if body:
        body = _protect(body)
    search_url = re.sub(r"\{\{.*?\}\}", "", search_url, flags=re.S)
    search_url = search_url.replace("\n", "").replace("\r", "").strip()
    search_url = _restore(search_url)
    if body:
        body = _restore(body)
    if "{{key}}" not in search_url and (body is None or "{{key}}" not in body):
        return None

    keyword_enc = urllib.parse.quote(keyword)
    search_url = search_url.replace("{{key}}", keyword_enc)
    search_url = re.sub(r"\{\{page[^}]*\}\}", "1", search_url)
    if body:
        body = body.replace("{{key}}", urllib.parse.quote_plus(keyword))
        body = re.sub(r"\{\{page[^}]*\}\}", "1", body)
    # 替换 key/page 后若仍有 {{...}} 残留（其他 JS 模板），无法可靠构造
    if re.search(r"\{\{", search_url) or (body and re.search(r"\{\{", body)):
        return None

    base = str(source.get("bookSourceUrl", "")).split("#")[0].strip()
    if not base.startswith("http"):
        base = "http://" + base
    if search_url.startswith("http"):
        full = search_url
    else:
        full = base.rstrip("/") + "/" + search_url.lstrip("/")
    return full, method, body


def decode_body(raw, headers):
    """智能解码：header charset → meta charset → utf8 → gb18030。"""
    if not raw:
        return ""
    try:
        m = re.search(r"charset=([\w-]+)", headers.get("Content-Type", "") or "", re.I)
        if m:
            try:
                return raw.decode(m.group(1), errors="ignore")
            except Exception:
                pass
    except Exception:
        pass
    try:
        m = re.search(r'charset=["\']?([\w-]+)', raw[:4096].decode("ascii", errors="ignore"), re.I)
        if m:
            try:
                return raw.decode(m.group(1), errors="ignore")
            except Exception:
                pass
    except Exception:
        pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="ignore")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def dump_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def apply_measured_respond_time(source, respond_ms):
    """把探测实测响应时间写回书源 respondTime（毫秒，legado 原生字段）。

    历史问题：导出链路（05_export / 06_pool）此前全量透传上游原始 respondTime，
    探测实测的 respond_time_ms 从没写回，页面上的秒数与「待复测」都是陈旧/失实数据。

    只在实测值有效（能转成正整数）时覆盖：
    - None / 0 / 负数 / 非数字 → 保持原值不动。0 表示"没测到"，若写成 0，
      页面会显示成 0s 最快，比「待复测」更误导。
    返回是否写入。
    """
    try:
        ms = int(respond_ms)
    except (TypeError, ValueError, OverflowError):
        return False
    if ms <= 0:
        return False
    source["respondTime"] = ms
    return True
