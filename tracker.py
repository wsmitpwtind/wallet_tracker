"""wallet-tracker 监控脚本

功能：定时查询清算所账户状态并在终端显示；当持仓发生变化时发送邮件通知；可查询币安数据以显示价格与短期变动。

使用方法：
- 在仓库根目录运行：python3 tracker.py
- 依赖：requests, colorama
- 配置：脚本顶部有可编辑的常量（SMTP、RPC_API、TARGET 等），也可以通过环境变量覆盖它们。

安全提示：请不要把包含真实 SMTP 授权码的脚本提交到公共仓库。推荐在生产环境使用环境变量或密钥管理服务保存凭据。
"""

import os
import time
import requests
import smtplib
from email.message import EmailMessage
from typing import Dict, Any
import math
import math
import json

import urllib.parse
import datetime
import io
import sys
import datetime
import re

from colorama import init as colorama_init, Fore, Style
import unicodedata

colorama_init(autoreset=True)


def print_kv(label: str, *values, indent: int = 4, label_width: int = 10, label_color=Fore.YELLOW, value_color=None, end="\n"):
    """统一的键值打印，保证左端对齐与冒号对齐。

    - label: 字段名（不包含冒号）
    - values: 值，可以包含带颜色的字符串
    - indent: 左侧缩进空格数
    - label_width: 标签字段宽度（用于冒号对齐）
    - label_color/value_color: colorama 前缀（可为 None）
    """
    try:

        def _display_width(s: str) -> int:
            w = 0
            for ch in s:
                ea = unicodedata.east_asian_width(ch)
                # 'W' (wide) and 'F' (fullwidth) -> width 2, others -> width 1
                w += 2 if ea in ("W", "F") else 1
            return w

        def _truncate_or_pad_to_width(s: str, width: int) -> str:
            cur_w = 0
            out_chars = []
            for ch in s:
                ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                if cur_w + ch_w > width:
                    break
                out_chars.append(ch)
                cur_w += ch_w
            # 如果不足宽度则填充空格
            if cur_w < width:
                out_chars.append(" " * (width - cur_w))
            return "".join(out_chars)

        prefix = " " * indent
        # 合并值为单一字符串
        val_parts = []
        for v in values:
            if v is None:
                val_parts.append("N/A")
            else:
                val_parts.append(str(v))
        val_str = " ".join(val_parts)
        lbl_core = f"{label}"
        if label_width and label_width > 0:
            # 使用显示宽度（考虑中文等全角字符）
            desired = int(label_width)
            core_w = _display_width(lbl_core)
            if core_w >= desired:
                lbl_body = _truncate_or_pad_to_width(lbl_core, desired)
            else:
                # 不足则补空格
                lbl_body = lbl_core + " " * (desired - core_w)
            lbl = lbl_body + ":"
        else:
            lbl = lbl_core
        if value_color:
            print(prefix + (label_color or "") + lbl + " " + value_color + val_str, end=end)
        else:
            print(prefix + (label_color or "") + lbl + " " + val_str, end=end)
    except Exception:
        # 回退到简单打印，避免因为格式化失败导致中断
        print(label + ":", " ".join([str(v) for v in values]))

# 配置
RPC_API = os.getenv("RPC_API", "https://api.hyperliquid.xyz/info")
TARGET = os.getenv("TARGET_ADDRESS", "0xc2a30212a8DdAc9e123944d6e29FADdCe994E5f2").lower()
#TARGET = os.getenv("TARGET_ADDRESS", "0xa650cbd841d3930df5adc53b9a35422fc558083b").lower()



POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))  # 秒

# SMTP / 邮件相关（可直接在此处配置，或使用环境变量覆盖）。
# 注意：163 邮箱需要使用 SMTP 授权码，请使用授权码而非登录密码；生产环境建议使用环境变量或密钥管理服务来保存凭据。
SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465
SMTP_USER = "wsmitpwtind@163.com"      # 通常与 EMAIL_FROM 相同
SMTP_PASS = "132132123123"  # 请使用 163 邮箱生成的授权码
EMAIL_FROM = "wsmitpwtind@163.com"
EMAIL_TO = "wsmitpwtind@163.com"
# EMAIL_TO = "wsmitpwtind@163.com, 1873696911@qq.com, 303493375@qq.com"  # 多个收件人用逗号分隔
SMTP_USE_SSL = True

# 如果希望通过环境变量配置 SMTP，请取消下面示例的注释并在运行环境中设置对应变量（更安全）：
# SMTP_HOST = os.getenv("SMTP_HOST", SMTP_HOST)
# SMTP_PORT = int(os.getenv("SMTP_PORT", SMTP_PORT))
# SMTP_USER = os.getenv("SMTP_USER", SMTP_USER)
# SMTP_PASS = os.getenv("SMTP_PASS", SMTP_PASS)
# EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_FROM)
# EMAIL_TO = os.getenv("EMAIL_TO", EMAIL_TO)


def fetch_state(user_address: str) -> Dict[str, Any]:
    body = {
        "type": "clearinghouseState",
        "user": user_address,
        "dex": ""
    }
    resp = requests.post(RPC_API, json=body)
    resp.raise_for_status()
    return resp.json()


# ---- Binance 数据获取（公共接口，无需 API Key） ----
BINANCE_API = "https://api.binance.com/api/v3"

# 全量 ticker 缓存（减少对 /ticker/price 单个请求失败的影响）
# 存储结构: { 'ts': <epoch>, 'data': { SYMBOL: price_float, ..  } }
TICKER_CACHE = {"ts": 0, "data": {}}
TICKER_TTL = 30  # seconds
BINANCE_BAN_UNTIL = 0  # epoch seconds, 如果被 ban 则设置为解封时间
# 记录上一次在哪个轮次打印过被封通知，避免在同一轮次重复打印
BINANCE_BAN_PRINTED_ITER = 0
# main loop 会将当前轮次写入这个全局变量，safe_get 以此判断是否要打印
GLOBAL_ITERATION = 0

# 用于记录每个键在上次哪个轮次已打印，配合 GLOBAL_ITERATION 做每轮限频
LAST_PRINTED_BY_KEY: Dict[str, int] = {}


def warn_once(key: str, msg: str, color=Fore.YELLOW):
    """按 key+轮次只打印一次消息（用于避免同一轮次重复日志刷屏）。"""
    try:
        global LAST_PRINTED_BY_KEY, GLOBAL_ITERATION
        last = LAST_PRINTED_BY_KEY.get(key)
        if last != GLOBAL_ITERATION:
            print((color or "") + msg)
            LAST_PRINTED_BY_KEY[key] = GLOBAL_ITERATION
    except Exception:
        # 回退到直接打印，确保至少有输出
        print((color or "") + msg)


# 历史持仓变更记录文件（每行一条 JSON）。
# 位于当前工作目录，便于审计和快速查看历史通知。
HISTORY_FILE = os.path.join(os.getcwd(), "position_changes.log")


def append_history(iteration: int, subject: str, body: str) -> None:
    """将一次持仓变更写入 HISTORY_FILE，每行一条 JSON 记录。"""
    try:
        record = {
            "ts": datetime.datetime.now().isoformat(),
            "iteration": iteration,
            "subject": subject,
            "body": body,
        }
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        warn_once("history_write_failed", f"[历史] 写入持仓变更文件失败: {e}")


def read_last_history(n: int = 3):
    """读取 HISTORY_FILE 最后 n 条记录，返回解析后的列表（按时间倒序）。"""
    out = []
    try:
        if not os.path.exists(HISTORY_FILE):
            return out
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        if not lines:
            return out
        selected = lines[-n:]
        for ln in reversed(selected):
            try:
                rec = json.loads(ln)
                out.append(rec)
            except Exception:
                # 如果解析失败，将原始行作为 body 返回
                out.append({"ts": None, "iteration": None, "subject": None, "body": ln})
        return out
    except Exception as e:
        warn_once("history_read_failed", f"[历史] 读取持仓变更文件失败: {e}")
        return out

# 全局 HTTP 会话与安全的 GET 封装说明：
# - 使用全局 Session 复用连接
# - 对 418/429/5xx 等异常状态进行指数退避重试
# - 对币安返回的 -1003（请求权重超限并被封禁）进行检测并在被封期间暂停后续请求以保护 IP
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; wallet-tracker/1.0)",
    "Accept": "application/json, text/plain, */*",
})


def safe_get(url: str, params: Dict[str, Any] = None, timeout: float = 6.0, retries: int = 1):
    """使用全局 Session 发起 GET 请求，带指数退避。

    - 对 418/429/5xx 做退避重试；遇到 418 时记录响应片段并适当延长 ticker 缓存以减少后续压力。
    - 返回 requests.Response 或 None（失败）。
    """
    global BINANCE_BAN_UNTIL, TICKER_TTL
    # 如果当前处于 Binance IP ban 期间，立即返回 None（避免继续触发）
    now = time.time()
    if BINANCE_BAN_UNTIL and now < BINANCE_BAN_UNTIL:
        remain = int(BINANCE_BAN_UNTIL - now)
        # 仅在当前轮次第一次遇到 ban 时打印，避免大量重复信息
        global BINANCE_BAN_PRINTED_ITER, GLOBAL_ITERATION
        try:
            if BINANCE_BAN_PRINTED_ITER != GLOBAL_ITERATION:
                print(Fore.YELLOW + f"[HTTP] 当前已被 Binance 限制，跳过请求 {url}，剩余秒: {remain}")
                BINANCE_BAN_PRINTED_ITER = GLOBAL_ITERATION
        except Exception:
            # 回退：如果任何问题，仍然安全地打印一次
            print(Fore.YELLOW + f"[HTTP] 当前已被 Binance 限制，跳过请求 {url}，剩余秒: {remain}")
        return None
    
    delay = 1.0
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
        except Exception as e:
            print(Fore.YELLOW + f"[HTTP] 请求异常 {url}: {e}")
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 200:
            return resp

        # 若响应体是 JSON 且包含 Binance 错误码（如 -1003），则尝试解析并设置 ban
        try:
            ct = resp.headers.get("content-type", "")
            if "application/json" in ct or resp.text.strip().startswith("{"):
                j = resp.json()
                if isinstance(j, dict) and j.get("code") == -1003:
                    # msg 里通常包含到期时间（ms），例如: "Way too much request weight used; IP banned until 1761372281648."
                    msg = j.get("msg") or j.get("message") or ""
                    m = re.search(r"(\d{10,13})", msg)
                    if m:
                        ts_ms = int(m.group(1))
                        ban_until = ts_ms / 1000.0
                        BINANCE_BAN_UNTIL = ban_until
                        # 延长 ticker 缓存，减少对 Binance 的进一步请求
                        now2 = time.time()
                        remain = max(0, ban_until - now2)
                        TICKER_TTL = max(TICKER_TTL, remain + 60)
                        print(Fore.RED + f"[HTTP] 发现 Binance 限制(code -1003)，IP 被封至 {ban_until} (剩余秒: {int(remain)})，已延长本地 ticker 缓存 {TICKER_TTL}s")
                        return None
        except Exception:
            pass

        # 处理特殊状态
        if resp.status_code == 418:
            snippet = resp.text[:800]
            print(Fore.YELLOW + f"[HTTP] 418 拒绝 {url}，响应片段: {snippet}")
            # 当全量 ticker 被拒绝时，延长本地缓存以减少压力
            if "/ticker/price" in url:
                TICKER_CACHE["ts"] = time.time()
            # 指数退避并重试
            time.sleep(max(10.0, delay))
            delay *= 2
            continue

        if resp.status_code == 429:
            print(Fore.YELLOW + f"[HTTP] 429 限流 {url}")
            time.sleep(delay)
            delay *= 2
            continue

        if 500 <= resp.status_code < 600:
            print(Fore.YELLOW + f"[HTTP] 5xx 错误 {resp.status_code} for {url}")
            time.sleep(delay)
            delay *= 2
            continue

        # 对于其它非 200 状态，记录并不再重试
        print(Fore.YELLOW + f"[HTTP] 非 200 状态 {resp.status_code} for {url}: {resp.text[:200]}")
        return None

    return None


def fetch_all_tickers(force: bool = False) -> Dict[str, float]:
    """一次性拉取 Binance /api/v3/ticker/price 的全部数据并缓存，返回 symbol->price 的字典。

    使用 TTL 缓存以减少请求频率。若 force=True 将强制刷新。
    """
    now = time.time()
    if not force and TICKER_CACHE["data"] and (now - TICKER_CACHE["ts"] < TICKER_TTL):
        return TICKER_CACHE["data"]

    try:
        url = f"{BINANCE_API}/ticker/price"
        r = safe_get(url, timeout=6, retries=1)
        if not r:
            warn_once("ticker_pull_failed", f"[币安] 全量 ticker 拉取失败（safe_get）")
            return TICKER_CACHE["data"]
        arr = r.json()
        mapping = {}
        if isinstance(arr, list):
            for item in arr:
                # item 示例: {"symbol":"BTCUSDT","price":"30000.00"}
                sym = item.get("symbol")
                price = safe_float(item.get("price"))
                if sym:
                    mapping[sym.upper()] = price
        TICKER_CACHE["data"] = mapping
        TICKER_CACHE["ts"] = now
        return mapping
    except Exception as e:
        warn_once("ticker_pull_exception", f"[币安] 拉取全量 ticker 异常: {e}")
        return TICKER_CACHE["data"]


def get_symbol_for_coin(coin: str) -> str:
    """根据 coin 名称尝试构建交易对符号（优先 USDT）"""
    if not coin:
        return None
    sym = coin.upper()
    # 常见直接映射
    candidates = [f"{sym}USDT", f"{sym}USD", f"{sym}BTC"]
    # 优先 USDT
    return candidates[0]


def fetch_current_price(symbol: str) -> float:
    """从 Binance 获取当前标记价格（ticker/price），返回 float 或 None"""
    if not symbol:
        return None
    try:
        url = f"{BINANCE_API}/ticker/price"
        r = safe_get(url, params={"symbol": symbol}, timeout=5, retries=1)
        if not r:
            return None
        data = r.json()
        return safe_float(data.get("price"))
    except Exception as e:
        print(Fore.YELLOW + f"[币安] 获取现价异常: {symbol} -> {e}")
        return None


def fetch_klines(symbol: str, interval: str, limit: int = 2):
    """从 Binance 获取 K 线，返回最近 limit 根 K 线列表或 None。每根 K 线为 list，index 4 为收盘价。"""
    if not symbol:
        return None
    try:
        url = f"{BINANCE_API}/klines"
        r = safe_get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=8, retries=1)
        if not r:
            print(Fore.YELLOW + f"[币安] 获取 K 线失败/超时: {symbol} {interval}")
            return None
        return r.json()
    except Exception as e:
        print(Fore.YELLOW + f"[币安] 获取 K 线异常: {symbol} {interval} -> {e}")
        return None


def compute_interval_changes(entry: float, current: float, past_close: float, leverage: float = 1.0):
    """基于 entry、当前价与区间过去收盘价计算：
    - unlevered_roi_now = (current-entry)/entry
    - unlevered_roi_past = (past_close-entry)/entry
    - roi_change = unlevered_roi_now - unlevered_roi_past
    返回 dict 包含百分比与方向
    """
    if entry is None or entry == 0 or current is None or past_close is None:
        return None
    try:
        un_now = (current - entry) / entry
        un_past = (past_close - entry) / entry
        delta = un_now - un_past
        # leverage applies multiplicatively
        leveraged_now = un_now * (leverage or 1.0)
        leveraged_delta = delta * (leverage or 1.0)
        return {
            "un_now": un_now,
            "un_past": un_past,
            "delta": delta,
            "leveraged_now": leveraged_now,
            "leveraged_delta": leveraged_delta,
            "pct": leveraged_delta * 100.0,
        }
    except Exception:
        return None

# ---- end Binance helper ----


def send_email(subject: str, body: str) -> None:
    """使用 SMTP 发送简单文本邮件。

    如果未配置 SMTP，将仅打印一条提示（不会抛错）。
    """
    if not (SMTP_HOST and SMTP_PORT and EMAIL_FROM and EMAIL_TO):
        print(Fore.YELLOW + "[邮件] SMTP 未配置完整，跳过发送邮件（请在代码中设置 SMTP_HOST/SMTP_PORT/EMAIL_FROM/EMAIL_TO）")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                if SMTP_USER and SMTP_PASS:
                    server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                if SMTP_USER and SMTP_PASS:
                    server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        print(Fore.GREEN + "[邮件] 已发送通知到:", EMAIL_TO)
    except Exception as e:
        print(Fore.RED + "[邮件] 发送失败:", str(e))


def format_position(pos: Dict[str, Any]) -> str:
    """将单个持仓格式化为可比较和可显示的字符串表示"""
    # 使用常见字段来构造唯一性描述，缺失字段用空串替代
    coin = pos.get("coin")
    size = pos.get("szi")
    entry = pos.get("entryPx")
    liq = pos.get("liquidationPx")
    unreal = pos.get("unrealizedPnl")
    margin_used = pos.get("marginUsed")
    leverage = None
    if "leverage" in pos and isinstance(pos.get("leverage"), dict):
        leverage = pos["leverage"].get("value")
    return f"coin={coin}|size={size}|entry={entry}|liq={liq}|unreal={unreal}|margin={margin_used}|lev={leverage}"


def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None


def build_position_summary(pos: Dict[str, Any]) -> Dict[str, Any]:
    """将原始 pos 转为数值化的 summary 用于比较与计算

    返回字段: size (float), entry (float), unreal (float), value (float, size*entry if可用), roi (float)
    """
    size = safe_float(pos.get("szi") or pos.get("size") or 0) or 0.0
    entry = safe_float(pos.get("entryPx") or pos.get("price") )
    unreal = safe_float(pos.get("unrealizedPnl")) or 0.0

    value = None
    if entry is not None:
        value = abs(size * entry)

    roi = None
    if value and value != 0:
        try:
            roi = unreal / value
        except Exception:
            roi = None
    leverage = None
    if "leverage" in pos and isinstance(pos.get("leverage"), dict):
        leverage = pos["leverage"].get("value")

    return {
        "size": size,
        "entry": entry,
        "unreal": unreal,
        "value": value or 0.0,
        "roi": roi,
        "leverage": leverage,
    }


def coin_to_symbol(coin: str) -> str:
    """尝试把币种名转换为交易所的交易对，例如 BTC -> BTCUSDT"""
    if not coin:
        return None
    c = coin.upper()
    # 如果已经带有分隔符或是稳定币，简单处理
    if c.endswith("USDT") or c.endswith("USD"):
        return c
    # 避免 USDTUSDT
    if c == "USDT":
        return None
    return f"{c}USDT"


def get_working_symbol(s: str) -> Any:
    """仅尝试将输入转换为 XXXUSDT 并在本地 ticker 缓存中查找，找不到就返回 None。"""
    if not s:
        return None
    # 规范化输入并移除分隔符
    s_up = s.strip().upper().replace('/', '').replace('-', '')
    # 若包含非字母数字字符（如地址 0x...），直接返回 None
    if not s_up.isalnum():
        warn_once(f"invalid_input:{s_up}", f"[币安] 输入币名包含非法字符，跳过: {s}")
        return None

    # 若已经以 USDT 结尾，则直接检查
    if s_up.endswith('USDT'):
        tickers = fetch_all_tickers()
        if s_up in tickers:
            return s_up
        # 强制刷新再试一次
        tickers = fetch_all_tickers(force=True)
        if s_up in tickers:
            return s_up
        warn_once(f"not_found:{s_up}", f"[币安] 未找到交易对: {s_up}")
        return None

    # 否则仅尝试添加 USDT 后缀
    cand = s_up + 'USDT'
    tickers = fetch_all_tickers()
    if cand in tickers:
        return cand
    tickers = fetch_all_tickers(force=True)
    if cand in tickers:
        return cand

    warn_once(f"not_found:{cand}", f"[币安] 未找到交易对: {cand}")
    return None


def fetch_binance_prices(symbol: str) -> Dict[str, Any]:
    """使用 Binance API 获取当前价和各 timeframe 的最近已收盘价（近似）

    返回 dict: { 'current': float, '5m': float, '15m': float, '1h': float, '4h': float, '1d': float }
    若无法获取则对应值为 None。
    """
    out = {"current": None, "5m": None, "15m": None, "1h": None, "4h": None, "1d": None}
    if not symbol:
        return out

    # 先从全量 ticker 缓存中查找当前价，避免对 /ticker/price 单独请求失败
    tickers = fetch_all_tickers()
    working = get_working_symbol(symbol)
    if not working:
        return out
    out["current"] = tickers.get(working)

    # 然后针对各 timeframe 仍需请求 klines（没有全量 K 线缓存），保持原有逻辑
    base = "https://api.binance.com"
    intervals = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
    for k, v in intervals.items():
        try:
            kr = safe_get(f"{base}/api/v3/klines", params={"symbol": working, "interval": v, "limit": 2}, timeout=5, retries=1)
            if not kr:
                out[k] = None
                continue
            data = kr.json()
            if isinstance(data, list) and len(data) >= 1:
                idx = 0 if len(data) >= 2 else 0
                close = safe_float(data[idx][4])
                out[k] = close
        except Exception:
            out[k] = None
    return out


def arrow_and_pct(delta: float) -> str:
    """根据 delta (绝对数) 返回带箭头和百分比的字符串（保留两位小数）"""
    if delta is None:
        return "N/A"
    sign = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
    return f"{sign}{abs(delta)*100:.2f}%"


def get_price_and_changes_binance(coin_symbol: str, timeout: float = 5.0):
    """尝试使用 Binance 公共 API 获取现价与 5m/15m/1h/4h 的变动百分比。

    约定：尝试用 SYMBOL + 'USDT' 作为交易对，例如 BTC -> BTCUSDT。返回 (current_price, {"5m": pct, ...})
    如果获取失败，返回 (None, {})
    """
    # 尝试找到可用的交易对并使用全量 ticker 缓存获取当前价
    pair = get_working_symbol(coin_symbol)
    if not pair:
        warn_once(f"no_pair:{coin_symbol}", f"[币安] 未找到可用交易对用于: {coin_symbol}")
        return None, {}

    tickers = fetch_all_tickers()
    current_price = tickers.get(pair)

    intervals = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}
    changes = {}
    base = "https://api.binance.com"
    for label, interval in intervals.items():
        try:
            url_klines = f"{base}/api/v3/klines"
            r = safe_get(url_klines, params={"symbol": pair, "interval": interval, "limit": 2}, timeout=timeout, retries=1)
            if not r:
                changes[label] = None
                continue
            k = r.json()
            if not k or len(k) < 2:
                changes[label] = None
                continue
            prev_close = safe_float(k[-2][4])
            last_close = safe_float(k[-1][4])
            if prev_close and last_close:
                pct = (last_close - prev_close) / prev_close
                changes[label] = pct
            else:
                changes[label] = None
        except Exception as e:
            print(Fore.YELLOW + f"[币安] klines 请求异常 for {pair} {interval}: {e}")
            changes[label] = None

    return current_price, changes


def format_change_icons(pct: float) -> str:
    """根据百分比变化返回 4 个箭头图标（用颜色）以及百分比字符串。这里只返回单个时间窗口的图标+数字。

    约定：如果 pct 为正，显示 ▲，否则显示 ▼；图标数量与幅度相关（简单映射）
    """
    if pct is None:
        return "N/A"
    try:
        pct_abs = abs(pct) * 100
        # 映射为 0-4 个图标
        if pct_abs >= 2.0:
            icons = 4
        elif pct_abs >= 1.0:
            icons = 3
        elif pct_abs >= 0.2:
            icons = 2
        elif pct_abs >= 0.01:
            icons = 1
        else:
            icons = 0

        arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "—")
        icon_str = arrow * icons if icons > 0 else "—"
        sign = "+" if pct > 0 else ("-" if pct < 0 else "")
        return f"{icon_str} {sign}{pct*100:.2f}%"
    except Exception:
        return "N/A"


def detect_changes(prev: Dict[str, Dict[str, Any]], current: Dict[str, Dict[str, Any]], total_portfolio_value: float, iteration: int) -> Dict[str, Any]:
    """比较之前与当前持仓（数值化），返回新增/移除/变更的详细摘要

    prev/current map: coin -> {size, entry, unreal, value, roi}
    返回包含 added/removed/changed，每项包含 (coin, old_summary, new_summary, delta_size, delta_value, ratio_within_coin, ratio_of_total)
    """
    added = []
    removed = []
    changed = []
    if iteration == 1:
        return {"added": added, "removed": removed, "changed": changed}
    # coins present in current
    for coin, cur in current.items():
        prev_sum = prev.get(coin)
        if prev_sum is None:
            delta_size = cur["size"]
            delta_value = cur["value"]
            ratio_within = 1.0 if cur["size"] != 0 else 0.0
            ratio_total = (abs(delta_value) / total_portfolio_value) if total_portfolio_value else None
            added.append((coin, None, cur, delta_size, delta_value, ratio_within, ratio_total))
        else:
            # compare sizes
            old_size = prev_sum.get("size", 0.0)
            new_size = cur.get("size", 0.0)
            # consider change if absolute difference > tiny epsilon
            eps = 1e-9
            if abs(new_size - old_size) > eps:
                delta_size = new_size - old_size
                # choose entry price for value calc: prefer current.entry then prev.entry
                entry = cur.get("entry") if cur.get("entry") is not None else prev_sum.get("entry")
                delta_value = (delta_size * entry) if entry is not None else 0.0
                # ratio within coin: 对于变动，用 abs(delta)/abs(new_size) if new_size !=0 else abs(delta)/abs(old_size)
                if abs(new_size) > eps:
                    ratio_within = abs(delta_size) / abs(new_size)
                elif abs(old_size) > eps:
                    ratio_within = abs(delta_size) / abs(old_size)
                else:
                    ratio_within = None
                ratio_total = (abs(delta_value) / total_portfolio_value) if total_portfolio_value else None
                changed.append((coin, prev_sum, cur, delta_size, delta_value, ratio_within, ratio_total))

    # coins removed
    for coin, prev_sum in prev.items():
        if coin not in current:
            delta_size = -prev_sum.get("size", 0.0)
            delta_value = -prev_sum.get("value", 0.0)
            ratio_within = 1.0 if abs(prev_sum.get("size", 0.0)) > 0 else None
            ratio_total = (abs(delta_value) / total_portfolio_value) if total_portfolio_value else None
            removed.append((coin, prev_sum, None, delta_size, delta_value, ratio_within, ratio_total))

    return {"added": added, "removed": removed, "changed": changed}


def parse_and_print(data: Dict[str, Any], prev_positions_map: Dict[str, str], iteration: int) -> Dict[str, str]:
    print(Style.BRIGHT + Fore.CYAN + f"=== 清算所账户快照: {TARGET} ===")

    margin = data.get("marginSummary", {})
    print(Fore.CYAN + "账户摘要  :")
    print_kv("账户价值    ", Fore.GREEN + f"{float(margin.get("accountValue", 0)):.1f}")
    print_kv("总NTL持仓   ", Fore.YELLOW + f"{float(margin.get("totalNtlPos")):.1f}")
    print_kv("总原始USD   ", Fore.GREEN + f"{float(margin.get("totalRawUsd")):.1f}")
    print_kv("已使用保证金", Fore.RED + f"{float(margin.get("totalMarginUsed")):.1f}")

    positions = data.get("assetPositions", [])
    current_map: Dict[str, Dict[str, Any]] = {}

    # 计算 total portfolio value，优先使用 marginSummary 中的 totalRawUsd（若可用），否则累加可计算的 pos value
    total_portfolio_value = 0.0
    margin_total_raw = safe_float(margin.get("totalRawUsd"))
    if margin_total_raw:
        total_portfolio_value = margin_total_raw

    if positions:
        print(Fore.CYAN + "持仓列表:")
        for pos_wrapper in positions:
            pos = pos_wrapper.get("position", {})
            coin = pos.get("coin") or pos.get("symbol") or str(pos.get("szi"))
            summary = build_position_summary(pos)
            current_map[coin] = summary
            # 如果没有从 marginSummary 读取到总仓位，则累加可计算的仓位价值
            if not margin_total_raw:
                total_portfolio_value += summary.get("value", 0.0)

            # 打印详情（包含 ROI 与仓位价值）
            print(Fore.MAGENTA + f"  {coin}")
            print_kv("大小      ", str(summary.get("size")), indent=8, value_color=Fore.YELLOW)
            print_kv("开仓价    ", str(summary.get("entry")), indent=8, value_color=Fore.YELLOW)
            roi = summary.get("roi")
            leverage = pos_wrapper.get("position", {}).get("leverage") if isinstance(pos_wrapper.get("position", {}), dict) else None
            lev_val = None
            if isinstance(leverage, dict):
                lev_val = safe_float(leverage.get("value")) or None
            # 当前收益率 a * b = c
            a = roi if roi is not None else None
            b = lev_val if lev_val is not None else None
            c = None
            if a is not None and b is not None:
                c = a * b

            # 打印当前收益率形式 a*b=c
            a_str = f"{a*100:.2f}%" if a is not None else "N/A"
            b_str = f"{b}x" if b is not None else "N/A"
            c_str = f"{c*100:.2f}%" if c is not None else "N/A"
            # c 颜色：提升为红色（正），否则蓝色（负或零）——按你的要求
            if c is not None and c > 0:
                c_col = Fore.RED
            else:
                c_col = Fore.GREEN

            print_kv("当前收益率", Fore.CYAN + f"{a_str} * {b_str} = " + c_col + f"{c_str}", indent=8)
            print_kv("仓位价值", Fore.YELLOW + f"{summary.get('value', 0.0):.1f}", indent=8)
            if summary.get("unreal") < 0:
                print_kv("未实现盈亏", Fore.GREEN + str(roi * summary.get('value', 0.0)), indent=8)
            else:
                print_kv("未实现盈亏", Fore.RED + str(summary.get("unreal")), indent=8)

            # 查询现价与历史价，计算各 timeframe 的 ROI 变动（以 ROI 的提升为红色，下降为绿色）
            coin_sym = coin_to_symbol(coin)
            price_map = fetch_binance_prices(coin_sym) if coin_sym else {"current": None}
            current_price = price_map.get("current")
            if current_price:
                print_kv("现价      ", Fore.YELLOW + f"{current_price:.1f}", indent=8, value_color=Fore.YELLOW)
            else:
                print_kv("现价      ", Fore.YELLOW + "N/A", indent=8, value_color=Fore.YELLOW)

            # 计算并打印 5m,15m,1h,4h,1d 的 ROI 变化
            time_keys = ["5m", "15m", "1h", "4h", "1d"]
            indicators = []
            for tk in time_keys:
                past_price = price_map.get(tk)
                if past_price is None or summary.get("entry") is None:
                    indicators.append("N/A")
                    continue
                # 过去该 timeframe 的 ROI（基于 entry）
                past_roi = (past_price - summary.get("entry")) / summary.get("entry") if summary.get("entry") != 0 else None
                cur_roi = (current_price - summary.get("entry")) / summary.get("entry") if summary.get("entry") != 0 else None
                if past_roi is None or cur_roi is None:
                    indicators.append("N/A")
                    continue
                delta = cur_roi - past_roi
                # if ROI 提升 -> 红色; 否则绿色
                col = Fore.RED if delta > 0 else Fore.GREEN
                sym = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
                indicators.append(col + f"{sym}{abs(delta)*100:.2f}%")

            # 获取并打印现价与短期价格变化（5m/15m/1h/4h）
            try:
                current_price, changes = get_price_and_changes_binance(coin)
                if current_price is not None:
                    print_kv("现价      ", Fore.GREEN + f"{current_price}", indent=8, value_color=Fore.GREEN)
                    order = ["5m", "15m", "1h", "4h"]
                    change_strs = [format_change_icons(changes.get(o)) for o in order]
                    print_kv("价格变动", Fore.CYAN + " | ".join(change_strs), indent=8)
                else:
                    print_kv("现价      ", Fore.YELLOW + "N/A", indent=8, value_color=Fore.YELLOW)
            except Exception:
                print("    现价      : ", Fore.YELLOW + "N/A")
    else:
        print(Fore.CYAN + "当前无持仓")

    # 比对持仓变化并在有变化时发送邮件
    diffs = detect_changes(prev_positions_map, current_map, total_portfolio_value, iteration)
    if diffs["added"] or diffs["removed"] or diffs["changed"] and not iteration == 1:
        print(Fore.RED + Style.BRIGHT + "检测到持仓变更:")
        body_lines = [f"持仓变更通知 - 账户: {TARGET}", f"总仓位(USD): {total_portfolio_value}", ""]

        if diffs["added"]:
            print(Fore.GREEN + "  新增持仓:")
            body_lines.append("新增持仓:")
            for coin, _, cur, delta_size, delta_value, ratio_within, ratio_total in diffs["added"]:
                roi_str = f"{(cur.get('roi')*100):.2f}%" if cur.get("roi") is not None else "N/A"
                cp, ch = get_price_and_changes_binance(coin)
                price_str = f"{cp}" if cp is not None else "N/A"
                print(Fore.GREEN + f"    {coin}: 大小={cur.get('size')}, 仓位={cur.get('value'):.1f}, ROI={roi_str}, 现价={price_str}")
                body_lines.append(f"  {coin}: 大小={cur.get('size')}, 仓位={cur.get('value'):.1f}, ROI={roi_str}, 现价={price_str}")
                if ratio_within is not None:
                    body_lines.append(f"    占该币种比例: {ratio_within*100:.2f}%")
                if ratio_total is not None:
                    body_lines.append(f"    占全仓比例: {ratio_total*100:.2f}%")
                if ch:
                    body_lines.append("    价格变动(5m,15m,1h,4h): " + " | ".join([format_change_icons(ch.get(o)) for o in ["5m", "15m", "1h", "4h"]]))

        if diffs["removed"]:
            print(Fore.YELLOW + "  平仓/移除持仓:")
            body_lines.append("移除持仓:")
            for coin, prev, _, delta_size, delta_value, ratio_within, ratio_total in diffs["removed"]:
                roi_str = f"{(prev.get('roi')*100):.2f}%" if prev.get("roi") is not None else "N/A"
                cp, ch = get_price_and_changes_binance(coin)
                price_str = f"{cp}" if cp is not None else "N/A"
                print(Fore.YELLOW + f"    {coin}: 原大小={prev.get('size')}, 原仓位={prev.get('value'):.1f}, ROI={roi_str}, 现价={price_str}")
                body_lines.append(f"  {coin}: 原大小={prev.get('size')}, 原仓位={prev.get('value'):.1f}, ROI={roi_str}, 现价={price_str}")
                if ratio_within is not None:
                    body_lines.append(f"    占该币种比例: {ratio_within*100:.2f}%")
                if ratio_total is not None:
                    body_lines.append(f"    占全仓比例: {ratio_total*100:.2f}%")
                if ch:
                    body_lines.append("    价格变动(5m,15m,1h,4h): " + " | ".join([format_change_icons(ch.get(o)) for o in ["5m", "15m", "1h", "4h"]]))

        if diffs["changed"]:
            print(Fore.MAGENTA + "  持仓变动（详情）:")
            body_lines.append("变动持仓:")
            for coin, prev, cur, delta_size, delta_value, ratio_within, ratio_total in diffs["changed"]:
                roi_old = f"{(prev.get('roi')*100):.2f}%" if prev.get("roi") is not None else "N/A"
                roi_new = f"{(cur.get('roi')*100):.2f}%" if cur.get("roi") is not None else "N/A"
                cp, ch = get_price_and_changes_binance(coin)
                price_str = f"{cp}" if cp is not None else "N/A"
                print(Fore.MAGENTA + f"    {coin}:")
                print(Fore.MAGENTA + f"      之前 - 大小={prev.get('size')}, 仓位={prev.get('value'):.1f}, ROI={roi_old}, 现价={price_str}")
                print(Fore.MAGENTA + f"      现在 - 大小={cur.get('size')}, 仓位={cur.get('value'):.1f}, ROI={roi_new}, 现价={price_str}")
                print(Fore.MAGENTA + f"      变化 - 数量 delta={delta_size}, 价值 delta={delta_value:.1f}")
                body_lines.append(f"  {coin} 原: 大小={prev.get('size')}, 仓位={prev.get('value'):.1f}, ROI={roi_old}, 现价={price_str}")
                body_lines.append(f"  {coin} 新: 大小={cur.get('size')}, 仓位={cur.get('value'):.1f}, ROI={roi_new}, 现价={price_str}")
                body_lines.append(f"    变化: 数量 delta={delta_size}, 价值 delta={delta_value:.1f}")
                if ratio_within is not None:
                    body_lines.append(f"    占该币种比例: {ratio_within*100:.2f}%")
                if ratio_total is not None:
                    body_lines.append(f"    占全仓比例: {ratio_total*100:.2f}%")
                if ch:
                    body_lines.append("    价格变动(5m,15m,1h,4h): " + " | ".join([format_change_icons(ch.get(o)) for o in ["5m", "15m", "1h", "4h"]]))

        # 发送邮件并写入历史记录文件
        subject = f"[通知] 账户 {TARGET} 持仓发生变化"
        body = "\n".join(body_lines)
        # 追加到本地历史记录（JSON 行格式）
        try:
            append_history(iteration, subject, body)
        except Exception:
            warn_once("history_append_fail", "[历史] 无法追加历史记录（内部错误）")
        send_email(subject, body)
    else:
        print(Fore.GREEN + "未检测到持仓变化。")

    # 打印最近三次历史持仓变化（若有）
    try:
        last = read_last_history(3)
        if last:
            print(Fore.CYAN + "最近历史持仓变化（最近 3 条）:")
            idx = 0
            for rec in last:
                idx += 1
                ts = rec.get("ts") or "N/A"
                itr = rec.get("iteration") or "N/A"
                subj = rec.get("subject") or ""
                print_kv(f"历史#{idx}", f"{ts} (轮次 {itr})", indent=4, label_width=16, value_color=Fore.CYAN)
                # 打印正文多行，逐行缩进
                body_text = rec.get("body") or ""
                for line in str(body_text).splitlines():
                    print(" " * 8 + line)
    except Exception:
        # 读取/打印历史失败不影响主流程
        pass

    return current_map


def main():
    print(Fore.CYAN + "启动监控，账户:", TARGET)
    prev_positions: Dict[str, str] = {}
    iteration = 0
    last_success_time = None
    while True:
        iteration += 1
        # 写入全局轮次，供 safe_get 等全局函数判断是否在本轮已打印过一次特定信息
        try:
            global GLOBAL_ITERATION
            GLOBAL_ITERATION = iteration
        except Exception:
            pass
        try:
            # 先在内存中准备要打印的内容，避免在等待网络时清屏造成空白
            buf = io.StringIO()
            old_stdout = sys.stdout
            success = False
            try:
                sys.stdout = buf
                state = fetch_state(TARGET)
                prev_positions = parse_and_print(state, prev_positions, iteration)
                # 如果没有异常，记录成功时间
                last_success_time = datetime.datetime.now()
                success = True
            except Exception as e:
                # 在缓冲区中写入错误信息，以便一次性显示
                buf.write(f"获取状态出错: {e}\n")
            finally:
                sys.stdout = old_stdout

            # 现在一次性清屏并打印 header + 缓冲内容，避免空屏
            try:
                os.system('clear')
            except Exception:
                pass
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            last_str = last_success_time.strftime("%Y-%m-%d %H:%M:%S") if last_success_time else "N/A"
            header = f"轮次: {iteration}    时间: {now}    上次成功更新时间: {last_str}    监控地址: {TARGET}"
            print(Style.BRIGHT + Fore.WHITE + header)
            print(buf.getvalue(), end="")
        except Exception as e:
            print(Fore.RED + "获取状态出错:", str(e))
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
