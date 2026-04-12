"""
Trading Engine — Core Logic
Ported from Code.gs + Strategy_v4/v5.gs
State is persisted via store.py (file / Vercel KV / memory).
"""

from __future__ import annotations
import json
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta, date
from typing import Optional

import requests

from strategies import (
    StrategyV4, StrategyV5,
    check_position_rules_v4, check_position_rules_v5,
    check_auto_stop_rules_v4, check_auto_stop_rules_v5,
    build_prompt_v4, build_prompt_v5,
    check_no_trade_day_v5, is_trend_trade_v5,
    extract_setup_type_v5, parse_trade_flags_v5,
)
from api.store import store_get, store_set, store_del, store_keys, backend_info

# ── API keys ──────────────────────────────────────────────────────────────────
FINNHUB_KEY      = os.environ.get("FINNHUB_KEY",      "")
NEWSAPI_KEY      = os.environ.get("NEWSAPI_KEY",      "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GROK_KEY         = os.environ.get("GROK_KEY",         "")
DEEPSEEK_KEY     = os.environ.get("DEEPSEEK_KEY",     "")

INITIAL_CASH = 10_000.0
MAX_TRADES_PER_DAY = 5

NYSE_HOLIDAYS = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}

# ─────────────────────────────────────────────────────────────────
# Store keys
# ─────────────────────────────────────────────────────────────────

def _state_key(provider: str) -> str:
    return f"trade_state:{provider}"

def _watchlist_key() -> str:
    return "watchlist"

def _session_log_key() -> str:
    return "session_log"


# ─────────────────────────────────────────────────────────────────
# Trade state
# ─────────────────────────────────────────────────────────────────

def new_trade_state(provider: str = "grok") -> dict:
    return {
        "cash": INITIAL_CASH,
        "holdings": {},
        "log": [],
        "dailyPnL": {},
        "todayTrades": {},
        "lastPrices": {},
        "lastPriceTimes": {},
        "prevDayPrices": {},
        "sessionPlans": [],
        "startDate": get_today_et(),
        "provider": provider,
        "noTradeDayDate": None,
        "spyOpenPrice": None,
        "maxTradesPerDay": MAX_TRADES_PER_DAY,
    }


def load_state(provider: str = "grok") -> dict:
    state = store_get(_state_key(provider))
    if not state:
        state = new_trade_state(provider)
    state["maxTradesPerDay"] = MAX_TRADES_PER_DAY
    state["provider"] = provider
    return state


def save_state(state: dict, provider: str = "grok") -> None:
    provider = provider or state.get("provider", "grok")
    if len(state.get("log", [])) > 300:
        state["log"] = state["log"][-300:]
    if len(state.get("sessionPlans", [])) > 40:
        state["sessionPlans"] = state["sessionPlans"][-40:]
    store_set(_state_key(provider), state)


def reset_state(provider: str = "grok") -> dict:
    store_del(_state_key(provider))
    return {"status": "reset", "provider": provider}


# ─────────────────────────────────────────────────────────────────
# Watchlist
# ─────────────────────────────────────────────────────────────────

def get_watchlist() -> list:
    return store_get(_watchlist_key(), default=[])


def save_watchlist(stocks: list) -> dict:
    filtered = [s for s in stocks if s.get("type") == "stock"]
    store_set(_watchlist_key(), filtered)
    return {"saved": len(filtered)}


# ─────────────────────────────────────────────────────────────────
# Session log
# ─────────────────────────────────────────────────────────────────

def load_session_log() -> list:
    return store_get(_session_log_key(), default=[])


def append_session_log(entry: dict) -> None:
    log = load_session_log()
    log.insert(0, entry)
    if len(log) > 100:
        log = log[:100]
    store_set(_session_log_key(), log)


def clear_session_log() -> None:
    store_del(_session_log_key())


# ─────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────

def _et_now() -> datetime:
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("America/New_York")
        return datetime.now(tz)
    except Exception:
        return datetime.now(timezone(timedelta(hours=-4)))


def get_today_et() -> str:
    return _et_now().strftime("%Y-%m-%d")


def get_now_et() -> str:
    return _et_now().strftime("%H:%M")


def is_trading_day() -> bool:
    now = _et_now()
    if now.weekday() >= 5:
        return False
    if get_today_et() in NYSE_HOLIDAYS:
        return False
    return True


def get_today_trade_count(state: dict, sym: str) -> int:
    return state.get("todayTrades", {}).get(f"{get_today_et()}:{sym}", 0)


def increment_trade_count(state: dict, sym: str) -> None:
    key = f"{get_today_et()}:{sym}"
    td = state.setdefault("todayTrades", {})
    td[key] = td.get(key, 0) + 1


# ─────────────────────────────────────────────────────────────────
# Market data
# ─────────────────────────────────────────────────────────────────

_price_cache: dict = {}
_PRICE_TTL = 300


def get_stock_quote(sym: str) -> Optional[dict]:
    cached = _price_cache.get(sym)
    if cached and (time.time() - cached[1]) < _PRICE_TTL:
        return cached[2]

    if not FINNHUB_KEY:
        return None
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol={sym}&token={FINNHUB_KEY}",
            timeout=6,
        )
        if r.status_code != 200:
            return None
        q = r.json()
        price = q.get("c") or q.get("pc")
        if not price:
            return None
        result = {
            "c": price, "d": q.get("d"), "dp": q.get("dp"),
            "h": q.get("h"), "l": q.get("l"), "o": q.get("o"),
            "pc": q.get("pc"), "isRealtime": bool(q.get("c") and q.get("c") > 0),
            "t": q.get("t"), "type": "stock",
        }
        _price_cache[sym] = (price, time.time(), result)
        return result
    except Exception:
        return None


def fetch_stock_news(symbol: str, limit: int = 5) -> list:
    news = []
    if FINNHUB_KEY:
        try:
            today = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            r = requests.get(
                f"https://finnhub.io/api/v1/company-news"
                f"?symbol={symbol}&from={yesterday}&to={today}&token={FINNHUB_KEY}",
                timeout=6,
            )
            if r.status_code == 200:
                for n in (r.json() or [])[:limit]:
                    news.append({
                        "source": n.get("source", "Finnhub"),
                        "headline": n.get("headline", ""),
                        "url": n.get("url", ""),
                        "datetime": n.get("datetime", 0) * 1000,
                        "sentiment": n.get("sentiment"),
                    })
        except Exception:
            pass

    if not news and NEWSAPI_KEY:
        try:
            frm = (date.today() - timedelta(days=1)).isoformat()
            r = requests.get(
                f"https://newsapi.org/v2/everything"
                f"?q={symbol}&from={frm}&sortBy=relevancy&pageSize={limit}&apiKey={NEWSAPI_KEY}",
                timeout=6,
            )
            if r.status_code == 200:
                for a in (r.json().get("articles") or []):
                    pub = a.get("publishedAt", "")
                    try:
                        ts = int(datetime.fromisoformat(
                            pub.replace("Z", "+00:00")).timestamp() * 1000)
                    except Exception:
                        ts = 0
                    news.append({
                        "source": (a.get("source") or {}).get("name", "NewsAPI"),
                        "headline": a.get("title", ""),
                        "url": a.get("url", ""),
                        "datetime": ts,
                        "sentiment": None,
                    })
        except Exception:
            pass

    news.sort(key=lambda x: x.get("datetime", 0), reverse=True)
    return news[:limit]


def get_news_for_items(items: list, limit: int = 5) -> dict:
    result = {}
    for item in items:
        if item.get("type") == "stock":
            result[f"{item['symbol']}:stock"] = fetch_stock_news(item["symbol"], limit)
    return result


# ─────────────────────────────────────────────────────────────────
# Portfolio helpers
# ─────────────────────────────────────────────────────────────────

def calc_nav(state: dict) -> float:
    nav = state.get("cash", INITIAL_CASH)
    for sym, h in state.get("holdings", {}).items():
        price = state.get("lastPrices", {}).get(sym, h["avgCost"])
        nav += price * h["shares"]
    return nav


def build_portfolio_summary(state: dict) -> str:
    holdings = state.get("holdings", {})
    last_prices = state.get("lastPrices", {})
    total_hold = 0
    parts = []
    for sym, h in holdings.items():
        price = last_prices.get(sym, h["avgCost"])
        val = price * h["shares"]
        pct = (price - h["avgCost"]) / h["avgCost"] * 100
        total_hold += val
        parts.append(f"{sym} {h['shares']}sh@{h['avgCost']:.0f}→{price:.0f}"
                     f"({'+'if pct>=0 else ''}{pct:.1f}%)")
    total = state.get("cash", 0) + total_hold
    net = total - INITIAL_CASH
    today_pnl = state.get("dailyPnL", {}).get(get_today_et(), 0)
    lines = [
        f"现金${state.get('cash',0):.0f} | 总${total:.0f}"
        f"({'+'if net>=0 else ''}${net:.0f}) | 今日{'+'if today_pnl>=0 else ''}${today_pnl:.0f}",
        "持仓: " + (" | ".join(parts) if parts else "空仓"),
    ]
    plans = state.get("sessionPlans", [])
    if plans:
        last = plans[-1]
        if len(last) > 80:
            last = last[:80] + "…"
        lines.append(f"上次计划: {last}")
    return "\n".join(lines)


def build_watchlist_context(state: dict) -> dict:
    items = [s for s in get_watchlist() if s.get("type") == "stock"]
    symbols = [s["symbol"] for s in items]
    now = time.time()
    stale = [s for s in symbols
             if not state.get("lastPrices", {}).get(s)
             or (now - state.get("lastPriceTimes", {}).get(s, 0)) > 300]
    for sym in stale:
        q = get_stock_quote(sym)
        if q and q.get("c"):
            state.setdefault("lastPrices", {})[sym] = q["c"]
            state.setdefault("lastPriceTimes", {})[sym] = now
            if q.get("pc") and not state.get("prevDayPrices", {}).get(sym):
                state.setdefault("prevDayPrices", {})[sym] = q["pc"]
    lines = []
    for sym in symbols:
        price = state.get("lastPrices", {}).get(sym)
        prev = state.get("prevDayPrices", {}).get(sym)
        chg = ""
        if price and prev and prev > 0:
            pct = (price - prev) / prev * 100
            chg = f" {'+'if pct>=0 else ''}{pct:.1f}%"
        lines.append(f"{sym} ${price:.2f}{chg}" if price else f"{sym} $?")
    return {"symbols": symbols, "text": "\n".join(lines)}


def build_log_summary(state: dict, limit: int = 8) -> str:
    today = get_today_et()
    recent = [e for e in state.get("log", []) if e.get("time", "").startswith(today)][-limit:]
    if not recent:
        return "今日暂无交易记录"
    parts = []
    for e in recent:
        pnl = e.get("realizedPnL")
        pnl_s = f" 盈亏{'+'if pnl>=0 else ''}${pnl:.2f}" if pnl is not None else ""
        parts.append(f"{e.get('action','').upper()} {e.get('sym','')} "
                     f"{e.get('shares',0)}股@${e.get('price',0):.2f}{pnl_s}")
    return "\n".join(parts)


def parse_analysis_confidence(text: str) -> dict:
    result = {}
    for line in text.split("\n"):
        t = line.strip()
        m = re.search(r"▸\s*([A-Z0-9]+)\s*\|\s*(看涨|看跌|中性)[^|]*\|.*?置信度\s*[：:]?\s*(\d+)",
                      t, re.IGNORECASE)
        if not m:
            m = re.search(r"▸\s*([A-Z0-9]+)\s*\|\s*(看涨|看跌|中性).*?(\d+)\s*/\s*10",
                          t, re.IGNORECASE)
        if m:
            score = int(m.group(3))
            if 0 <= score <= 10:
                result[m.group(1)] = {"direction": m.group(2), "score": score}
    return result


# ─────────────────────────────────────────────────────────────────
# AI call layer
# ─────────────────────────────────────────────────────────────────

def call_claude(prompt: str) -> dict:
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not set"}
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        data = r.json()
        if data.get("content"):
            return {"text": data["content"][0]["text"], "provider": "Claude"}
        return {"error": str(data)}
    except Exception as e:
        return {"error": str(e)}


def call_grok(prompt: str) -> dict:
    if not GROK_KEY:
        return {"error": "GROK_KEY not set"}
    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROK_KEY}",
                     "content-type": "application/json"},
            json={"model": "grok-3", "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        data = r.json()
        if data.get("choices"):
            return {"text": data["choices"][0]["message"]["content"], "provider": "Grok"}
        return {"error": str(data)}
    except Exception as e:
        return {"error": str(e)}


def call_deepseek(prompt: str) -> dict:
    if not DEEPSEEK_KEY:
        return {"error": "DEEPSEEK_KEY not set"}
    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}",
                     "content-type": "application/json"},
            json={"model": "deepseek-chat", "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        data = r.json()
        if data.get("choices"):
            return {"text": data["choices"][0]["message"]["content"], "provider": "DeepSeek"}
        return {"error": str(data)}
    except Exception as e:
        return {"error": str(e)}


def analyze_stock(prompt: str, provider: str = "grok") -> dict:
    if provider == "claude":
        return call_claude(prompt)
    if provider == "deepseek":
        return call_deepseek(prompt)
    return call_grok(prompt)


# ─────────────────────────────────────────────────────────────────
# Prompt router
# ─────────────────────────────────────────────────────────────────

def build_prompt(session: str, portfolio: str, watchlist_text: str,
                 news_summary: str, log_summary: str = "",
                 focus_note: str = "", strategy: str = "v5") -> str:
    if strategy == "v5":
        return build_prompt_v5(session, portfolio, watchlist_text,
                               news_summary, log_summary, focus_note)
    return build_prompt_v4(session, portfolio, watchlist_text,
                           news_summary, log_summary, focus_note)


# ─────────────────────────────────────────────────────────────────
# Decision parser (tolerant — markdown bold + standard multi-line)
# ─────────────────────────────────────────────────────────────────

def parse_ai_decisions(ai_text: str) -> list:
    block_lines = []
    in_block = False

    for line in ai_text.split("\n"):
        if re.search(r"DECISION\s*:", line, re.IGNORECASE):
            in_block = True
            rest = re.sub(r"\*{0,2}\s*DECISION\s*:\s*\*{0,2}", "",
                          line, flags=re.IGNORECASE).strip()
            if rest:
                block_lines.append(rest)
        elif in_block:
            stripped = line.strip().lstrip("*-\u2013 ")
            if stripped == "":
                if block_lines:
                    break
            elif re.match(r"^(NEXT_ACTION|##|【)", stripped):
                break
            else:
                block_lines.append(stripped)

    decisions = []
    for raw in block_lines:
        m = re.match(r"(BUY|SELL|HOLD)\|([A-Z0-9.]{0,12})\|(\d+)\|(.+)",
                     raw, re.IGNORECASE)
        if not m:
            continue
        action = m.group(1).upper()
        reason = m.group(4).strip()
        conf = 6
        cm = re.search(r"置信度[：:\s]*(\d+)", reason) or re.search(r"(\d+)\s*/\s*10", reason)
        if cm:
            conf = min(10, max(0, int(cm.group(1))))
        decisions.append({
            "action": action,
            "symbol": m.group(2).upper(),
            "shares": int(m.group(3)),
            "reason": reason,
            "confidence": conf,
        })
    return decisions


# ─────────────────────────────────────────────────────────────────
# SPY stop
# ─────────────────────────────────────────────────────────────────

def check_spy_stop_loss(state: dict, strategy: str) -> list:
    cfg = StrategyV5 if strategy == "v5" else StrategyV4
    spy_price = state.get("lastPrices", {}).get("SPY")
    spy_open = state.get("spyOpenPrice")

    if not spy_open:
        q = get_stock_quote("SPY")
        if q:
            spy_open = q.get("o") or q.get("pc")
            if spy_open:
                state["spyOpenPrice"] = spy_open
            if q.get("c"):
                state.setdefault("lastPrices", {})["SPY"] = q["c"]
                spy_price = q["c"]

    if not spy_price or not spy_open:
        return []
    chg = (spy_price - spy_open) / spy_open * 100
    if chg > -cfg.SPY_STOP_PCT:
        return []
    return [{"sym": sym, "shares": h["shares"],
             "reason": f"SPY当日跌幅{chg:.2f}%，触发大盘止损全平"}
            for sym, h in state.get("holdings", {}).items()]


# ─────────────────────────────────────────────────────────────────
# Auto stops
# ─────────────────────────────────────────────────────────────────

def run_auto_stops(state, session, strategy, executed, today, now_time, label):
    spy_sells = check_spy_stop_loss(state, strategy)
    stock_sells = []
    if not spy_sells:
        if strategy == "v5":
            stock_sells = check_auto_stop_rules_v5(state, session)
        else:
            stock_sells = check_auto_stop_rules_v4(state, session)

    merged = {}
    for sell in spy_sells + stock_sells:
        ex = merged.get(sell["sym"])
        if not ex or sell["shares"] > ex["shares"]:
            merged[sell["sym"]] = sell

    for sym, sell in merged.items():
        h = state.get("holdings", {}).get(sym)
        if not h or h["shares"] <= 0:
            continue
        sh = min(sell["shares"], h["shares"])
        price = state.get("lastPrices", {}).get(sym, h["avgCost"])
        realized = (price - h["avgCost"]) * sh
        state["cash"] = state.get("cash", 0) + price * sh
        state.setdefault("dailyPnL", {})[today] = state["dailyPnL"].get(today, 0) + realized
        h["shares"] -= sh
        if h["shares"] == 0:
            del state["holdings"][sym]
        increment_trade_count(state, sym)
        state.setdefault("log", []).append({
            "action": "sell", "sym": sym, "shares": sh, "price": price,
            "realizedPnL": realized, "reason": sell["reason"],
            "session": label, "time": f"{today} {now_time}",
        })
        sign = "+" if realized >= 0 else ""
        executed.append(f"🛑 [自动] 卖出 {sym} {sh}股 @${price:.2f} "
                        f"盈亏{sign}${realized:.2f}（{sell['reason']}）")


# ─────────────────────────────────────────────────────────────────
# Smart swap
# ─────────────────────────────────────────────────────────────────

def try_swap(state, buy_sym, buy_conf, needed, scores, executed, today, now_time, label):
    candidates = []
    for sym, h in state.get("holdings", {}).items():
        if sym == buy_sym:
            continue
        if get_today_trade_count(state, sym) >= MAX_TRADES_PER_DAY:
            continue
        price = state.get("lastPrices", {}).get(sym, h["avgCost"])
        info = scores.get(sym, {"score": 5, "direction": "中性"})
        if info.get("score", 5) < buy_conf or info.get("direction") == "看跌":
            candidates.append({"sym": sym, "score": info.get("score", 5),
                               "direction": info.get("direction", "中性"),
                               "shares": h["shares"], "price": price, "avgCost": h["avgCost"]})

    if not candidates:
        return False

    candidates.sort(key=lambda c: (c["direction"] != "看跌", c["score"]))
    swapped = False
    for c in candidates:
        if state.get("cash", 0) >= needed:
            break
        h2 = state.get("holdings", {}).get(c["sym"])
        if not h2 or h2["shares"] <= 0:
            continue
        shortfall = needed - state.get("cash", 0)
        sh = min(math.ceil(shortfall / c["price"]), h2["shares"])
        if sh <= 0:
            continue
        realized = (c["price"] - c["avgCost"]) * sh
        state["cash"] = state.get("cash", 0) + c["price"] * sh
        state.setdefault("dailyPnL", {})[today] = state["dailyPnL"].get(today, 0) + realized
        h2["shares"] -= sh
        if h2["shares"] == 0:
            del state["holdings"][c["sym"]]
        increment_trade_count(state, c["sym"])
        sign = "+" if realized >= 0 else ""
        state.setdefault("log", []).append({
            "action": "sell", "sym": c["sym"], "shares": sh, "price": c["price"],
            "realizedPnL": realized,
            "reason": f"换仓：为买入高置信度 {buy_sym}（{buy_conf}/10 > {c['score']}/10）",
            "session": label, "time": f"{today} {now_time}",
        })
        executed.append(f"🔄 换仓卖出 {c['sym']} {sh}股 @${c['price']:.2f} "
                        f"盈亏{sign}${realized:.2f}（为 {buy_sym} 置信度{buy_conf}/10 腾出资金）")
        swapped = True
    return swapped


# ─────────────────────────────────────────────────────────────────
# Execute decisions
# ─────────────────────────────────────────────────────────────────

def execute_decisions(state, ai_text, label, strategy):
    executed = []
    today = get_today_et()
    now_time = get_now_et()

    run_auto_stops(state, label, strategy, executed, today, now_time, label)

    pending = parse_ai_decisions(ai_text)
    pending.sort(key=lambda d: (0 if d["action"] == "SELL" else 1, -d.get("confidence", 6)))
    scores = parse_analysis_confidence(ai_text)

    for d in pending:
        action = d["action"]
        sym    = d["symbol"]
        shares = d["shares"]
        reason = d.get("reason", "")
        conf   = d.get("confidence", 6)

        if action == "HOLD" or not sym:
            continue
        if get_today_trade_count(state, sym) >= MAX_TRADES_PER_DAY:
            executed.append(f"⚠ {sym} 今日次数已满，跳过")
            continue

        price = state.get("lastPrices", {}).get(sym)
        if not price:
            q = get_stock_quote(sym)
            price = (q.get("c") or q.get("pc")) if q else None
        if not price:
            executed.append(f"⚠ {sym} 价格未知，跳过")
            continue

        if action == "BUY":
            if strategy == "v5":
                if state.get("noTradeDayDate") == today:
                    executed.append(f"🚫 [No Trade Day] 今日禁止交易，跳过 {sym}")
                    continue
                if conf < 7:
                    executed.append(f"⚠ [v5] {sym} 置信度{conf}/10 < 7，跳过")
                    continue
            if label in ("收尾", "closing"):
                executed.append(f"⚠ {sym} 收尾禁止新开仓")
                continue

            if strategy == "v5":
                is_trend = is_trend_trade_v5(reason)
                pos = check_position_rules_v5(state, sym, shares, price, conf, is_trend)
            else:
                pos = check_position_rules_v4(state, sym, shares, price)

            if pos["skip"]:
                executed.append(f"⚠ {pos['reason']}")
                continue
            shares = pos["shares"]

            needed = price * shares
            if needed > state.get("cash", 0):
                try_swap(state, sym, conf, needed, scores,
                         executed, today, now_time, label)
            if price * shares > state.get("cash", 0):
                shares = math.floor(state.get("cash", 0) / price)
            if shares <= 0:
                executed.append(f"⚠ 现金不足，无法买入 {sym}")
                continue

            cost = price * shares
            state["cash"] = state.get("cash", 0) - cost
            h = state.setdefault("holdings", {}).setdefault(sym, {"shares": 0, "avgCost": 0})
            h["avgCost"] = (h["avgCost"] * h["shares"] + cost) / (h["shares"] + shares)
            h["shares"] += shares
            if strategy == "v5":
                h["isTrendTrade"] = is_trend_trade_v5(reason)
                h["setupType"] = extract_setup_type_v5(reason)
            increment_trade_count(state, sym)
            trend_lbl = " [趋势单]" if strategy == "v5" and h.get("isTrendTrade") else ""
            state.setdefault("log", []).append({
                "action": "buy", "sym": sym, "shares": shares, "price": price,
                "reason": reason, "confidence": conf, "session": label,
                "time": f"{today} {now_time}",
                "isTrendTrade": h.get("isTrendTrade", False),
                "setupType": h.get("setupType", ""),
            })
            executed.append(f"✅ [置信度{conf}/10{trend_lbl}] 买入 {sym} {shares}股 "
                            f"@${price:.2f} 花费${cost:.2f}（{reason}）")

        elif action == "SELL":
            h = state.get("holdings", {}).get(sym)
            if not h or h["shares"] <= 0:
                executed.append(f"⚠ {sym} 无持仓")
                continue
            shares = min(shares, h["shares"]) if shares > 0 else h["shares"]
            proceeds = price * shares
            realized = (price - h["avgCost"]) * shares
            state["cash"] = state.get("cash", 0) + proceeds
            state.setdefault("dailyPnL", {})[today] = state["dailyPnL"].get(today, 0) + realized
            h["shares"] -= shares
            if h["shares"] == 0:
                del state["holdings"][sym]
            increment_trade_count(state, sym)
            state.setdefault("log", []).append({
                "action": "sell", "sym": sym, "shares": shares, "price": price,
                "realizedPnL": realized, "reason": reason,
                "session": label, "time": f"{today} {now_time}",
            })
            sign = "+" if realized >= 0 else ""
            executed.append(f"✅ 卖出 {sym} {shares}股 @${price:.2f} "
                            f"收入${proceeds:.2f} 盈亏{sign}${realized:.2f}（{reason}）")

    for line in ai_text.split("\n"):
        lt = line.strip()
        if re.match(r"^NEXT_ACTION\s*[：:]", lt):
            na = re.sub(r"^NEXT_ACTION\s*[：:]\s*", "", lt)
            state.setdefault("sessionPlans", []).append(f"[{label} {now_time}] {na}")

    return executed


# ─────────────────────────────────────────────────────────────────
# Focus note
# ─────────────────────────────────────────────────────────────────

def build_focused_note(symbols, state, news_map):
    lp = state.get("lastPrices", {})
    pp = state.get("prevDayPrices", {})
    active, quiet = [], []
    for sym in symbols:
        price = lp.get(sym)
        prev = pp.get(sym)
        chg = abs((price - prev) / prev * 100) if (price and prev and prev > 0) else 0
        if (sym in state.get("holdings", {})) or chg > 0.3 or bool(news_map.get(f"{sym}:stock")):
            active.append(sym)
        else:
            quiet.append(sym)
    note = f"\n⚡ 无明显变动/新闻（直接HOLD）：{', '.join(quiet)}\n" if quiet else ""
    return {"active": active, "quiet": quiet, "note": note}


# ─────────────────────────────────────────────────────────────────
# Full session runner
# ─────────────────────────────────────────────────────────────────

def run_trade_session(session: str, provider: str = "grok", strategy: str = "v5") -> dict:
    state = load_state(provider)
    state["provider"] = provider

    strat_cfg = StrategyV5 if strategy == "v5" else StrategyV4
    sess_map = {s["key"]: s["label"] for s in strat_cfg.SESSIONS}
    label = sess_map.get(session, session)

    portfolio = build_portfolio_summary(state)
    wl = build_watchlist_context(state)
    symbols = wl["symbols"]
    wl_text = wl["text"]

    items = [{"symbol": s, "type": "stock"} for s in symbols]
    news_map = get_news_for_items(items, limit=3)

    news_lines = []
    for sym in symbols:
        articles = news_map.get(f"{sym}:stock", [])
        if articles:
            news_lines.append(f"{sym}: {articles[0]['headline'][:80]}")
    news_summary = "\n".join(news_lines) or "暂无相关新闻"

    log_summary = build_log_summary(state)
    focus = build_focused_note(symbols, state, news_map)
    focus_note = focus.get("note", "")

    prompt = build_prompt(session, portfolio, wl_text,
                          news_summary, log_summary, focus_note, strategy)

    ai_result = analyze_stock(prompt, provider)
    if "error" in ai_result:
        save_state(state, provider)
        return {"error": ai_result["error"], "state": state}

    ai_text = ai_result.get("text", "")

    if strategy == "v5" and session == "premarket":
        if check_no_trade_day_v5(ai_text):
            state["noTradeDayDate"] = get_today_et()

    executed = execute_decisions(state, ai_text, label, strategy)
    save_state(state, provider)

    log_entry = {
        "id": int(time.time() * 1000),
        "date": get_today_et(),
        "time": get_now_et(),
        "session": session,
        "sessionLabel": label,
        "provider": provider,
        "strategy": strategy,
        "portfolio": portfolio,
        "watchlistText": wl_text,
        "aiText": ai_text,
        "executed": executed,
    }
    append_session_log(log_entry)

    return {**log_entry, "state": state}


# ─────────────────────────────────────────────────────────────────
# Quant metrics
# ─────────────────────────────────────────────────────────────────

def calc_quant_metrics(state: dict) -> dict:
    log = state.get("log", [])
    closed = [e for e in log if e.get("action", "").lower() == "sell"
              and e.get("realizedPnL") is not None]
    wins   = [e for e in closed if e["realizedPnL"] > 0]
    losses = [e for e in closed if e["realizedPnL"] <= 0]
    total  = len(closed)
    avg_win  = sum(e["realizedPnL"] for e in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(e["realizedPnL"] for e in losses) / len(losses)) if losses else 0
    p_win = len(wins) / total if total else 0
    ev = p_win * avg_win - (1 - p_win) * avg_loss
    gw = sum(e["realizedPnL"] for e in wins)
    gl = abs(sum(e["realizedPnL"] for e in losses))
    pf = gw / gl if gl > 0 else (99 if gw > 0 else 0)
    nav_curve = _nav_curve(state)
    return {
        "totalTrades":  total,
        "winRate":      round(p_win * 100, 1),
        "expectancy":   round(ev, 2),
        "avgWin":       round(avg_win, 2),
        "avgLoss":      round(avg_loss, 2),
        "profitFactor": round(pf, 2),
        "grossWin":     round(gw, 2),
        "grossLoss":    round(gl, 2),
        "maxDrawdown":  _max_drawdown(nav_curve),
        "navCurve":     nav_curve,
    }


def _nav_curve(state: dict) -> list:
    curve, cum = [], INITIAL_CASH
    for d in sorted(state.get("dailyPnL", {}).keys()):
        cum += state["dailyPnL"][d]
        curve.append({"date": d, "totalValue": round(cum, 2)})
    return curve


def _max_drawdown(curve: list) -> float:
    if len(curve) < 2:
        return 0.0
    peak, mx = curve[0]["totalValue"], 0.0
    for pt in curve:
        if pt["totalValue"] > peak:
            peak = pt["totalValue"]
        dd = (peak - pt["totalValue"]) / peak if peak > 0 else 0
        if dd > mx:
            mx = dd
    return round(mx * 100, 2)
