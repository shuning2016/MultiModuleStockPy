"""
Strategy v4.0 — Full Day Trading
Corresponds to: Trade_Rule_4.0.md
Trading hours: 09:30–16:00 ET
"""

from __future__ import annotations
import math
from typing import Optional


class StrategyV4:
    version = "v4.0"
    name = "全天交易版（Trade_Rule_4.0.md）"

    # Position management
    MIN_CASH_RATIO = 0.20
    MAX_SINGLE_RATIO = 0.30       # tightened from v3's 40%
    MAX_HOLDINGS = 3              # tightened from v3's 5
    MIN_CONFIDENCE = 6

    # Ladder take-profit / stop-loss
    TAKE_PROFIT_1 = 2.0           # +2% → reduce 1/3
    TAKE_PROFIT_2 = 4.0           # +4% → reduce another 1/3
    EARLY_REDUCE_PCT = 1.0        # -1% → reduce 50%
    STOP_LOSS_PCT = 1.5           # -1.5% → full exit
    SPY_STOP_PCT = 1.5            # SPY drops >-1.5% → full exit

    # Session schedule (ET)
    SESSIONS = [
        {"key": "premarket",  "hour": 9,  "min": 15,  "label": "盘前分析",    "time": "9:15"},
        {"key": "opening",    "hour": 10, "min": 15,  "label": "黄金入场",    "time": "10:15"},
        {"key": "mid",        "hour": 11, "min": 30,  "label": "中盘复盘",    "time": "11:30"},
        {"key": "afternoon",  "hour": 14, "min": 0,   "label": "尾盘窗口",    "time": "14:00"},
        {"key": "closing",    "hour": 15, "min": 45,  "label": "收尾",        "time": "15:45"},
    ]
    TIME_BADGES = ["盘前 9:15", "入场 10:15", "中盘 11:30", "尾盘 14:00", "收尾 15:45"]


def check_position_rules_v4(state: dict, sym: str, shares: int, price: float) -> dict:
    cfg = StrategyV4
    holdings = state.get("holdings", {})

    total_assets = state.get("cash", 0)
    for s, h in holdings.items():
        total_assets += state.get("lastPrices", {}).get(s, h["avgCost"]) * h["shares"]

    # Rule 1: max holdings
    if sym not in holdings and len(holdings) >= cfg.MAX_HOLDINGS:
        return {"shares": 0, "skip": True,
                "reason": f"持仓已满 {cfg.MAX_HOLDINGS} 只，跳过 {sym}"}

    # Rule 2: single-stock cap 30%
    max_allowed = total_assets * cfg.MAX_SINGLE_RATIO
    existing = holdings[sym]["shares"] * (state.get("lastPrices", {}).get(sym) or holdings[sym]["avgCost"]) \
               if sym in holdings else 0
    room = max_allowed - existing
    if room <= 0:
        return {"shares": 0, "skip": True,
                "reason": f"{sym} 已达单股上限 {cfg.MAX_SINGLE_RATIO*100:.0f}%"}
    shares = min(shares, math.floor(room / price))

    # Rule 3: cash floor 20%
    usable = state["cash"] - total_assets * cfg.MIN_CASH_RATIO
    if usable <= 0:
        return {"shares": 0, "skip": True, "reason": "现金低于20%底线，跳过"}
    shares = min(shares, math.floor(usable / price))
    if shares <= 0:
        return {"shares": 0, "skip": True, "reason": f"买入后现金低于20%底线，跳过 {sym}"}

    return {"shares": shares, "skip": False, "reason": ""}


def check_auto_stop_rules_v4(state: dict, session: str) -> list[dict]:
    cfg = StrategyV4
    sells = []

    for sym, h in list(state.get("holdings", {}).items()):
        price = state.get("lastPrices", {}).get(sym, h["avgCost"])
        avg_cost = h["avgCost"]
        pct = (price - avg_cost) / avg_cost * 100 if avg_cost else 0.0

        # Hard stop -1.5%
        if pct <= -cfg.STOP_LOSS_PCT:
            sells.append({"sym": sym, "shares": h["shares"],
                          "reason": f"止损 {pct:.2f}%（-{cfg.STOP_LOSS_PCT}% 触发，全出）"})
            continue

        # Early reduce -1%
        if pct <= -cfg.EARLY_REDUCE_PCT:
            sells.append({"sym": sym, "shares": math.ceil(h["shares"] / 2),
                          "reason": f"提前减仓 {pct:.2f}%（-{cfg.EARLY_REDUCE_PCT}% 触发，减50%）"})
            continue

        # Ladder take-profit
        tp2_flag = h.get("tp2Done", False)
        tp1_flag = h.get("tp1Done", False)

        if not tp2_flag and pct >= cfg.TAKE_PROFIT_2:
            sell_sh = math.ceil(h["shares"] / 3)
            sells.append({"sym": sym, "shares": sell_sh,
                          "reason": f"第二止盈 +{pct:.2f}%（+{cfg.TAKE_PROFIT_2}% 触发，减1/3）"})
            h["tp2Done"] = True
            continue

        if not tp1_flag and pct >= cfg.TAKE_PROFIT_1:
            sell_sh = math.ceil(h["shares"] / 3)
            sells.append({"sym": sym, "shares": sell_sh,
                          "reason": f"第一止盈 +{pct:.2f}%（+{cfg.TAKE_PROFIT_1}% 触发，减1/3）"})
            h["tp1Done"] = True
            continue

        # Closing: sell any position below break-even
        if session == "closing" and pct < 0:
            sells.append({"sym": sym, "shares": h["shares"],
                          "reason": f"收尾亏损平仓 {pct:.2f}%（不持亏损过夜）"})

    return sells


def build_prompt_v4(session: str, portfolio: str, watchlist_text: str,
                    news_summary: str, log_summary: str = "", focus_note: str = "") -> str:
    cfg = StrategyV4

    CONF = ("## 置信度（0-10）\n"
            "0-5：观望（HOLD）｜ 6-7：B仓（≤20%）｜ 8-9：A仓（≤30%）｜ 10：A仓满仓\n"
            "规则：≥6才允许操作，<6一律HOLD\n"
            "入场三要素（缺一不可）：①量价放量突破 ②站稳关键位（开盘30min后）③SPY/QQQ同向\n\n")

    RULES = ("## 策略规则（v4.0）\n"
             "仓位：A仓≤30%（高置信），B仓≤20%（中置信），现金≥20%，最多3只\n"
             "止盈：+2%减1/3 → +4%再减1/3 → 剩余VWAP保护\n"
             "止损：-1%减50% → -1.5%全出 → SPY跌>-1.5%全平\n"
             "开盘等待：09:30–10:00不开新仓（等假突破消化，方向确认后入场）\n\n")

    DEC = ("DECISION:\n"
           "BUY|SYM|shares|理由（三要素确认，置信度X/10，A仓/B仓，setup类型）\n"
           "SELL|SYM|shares|理由\n"
           "HOLD||0|原因\n\n")

    if session == "premarket":
        return (f"你是专业量化交易员（v4.0全天策略），09:15 ET 盘前分析。\n\n"
                f"## 账户状态\n{portfolio}\n\n"
                f"## 可交易股票（日均成交额>$5亿）\n{watchlist_text}{focus_note}\n\n"
                f"## 盘前新闻\n{news_summary}\n\n"
                + CONF + RULES +
                "## 今日任务（盘前 — 只分析不交易）\n"
                "1. 今日强势板块判断（板块 + 资金偏向 risk-on/off）\n"
                "2. 从watchlist中选1-2只今日重点标的（龙头 + 催化剂）\n"
                "3. 风险提示（财报/CPI/Fed/非农等关键事件）\n"
                "4. 关键价位：SPY | QQQ | 重点标的支撑/压力\n\n"
                "逐股评分（重点标的）：\n"
                "▸ SYM | 看涨/看跌/中性 | 置信度：X/10\n"
                "  三要素预判：①量价{Y/N} ②方向{Y/N} ③大盘{Y/N}\n"
                "  催化剂：{1句} | 关键价位：{支撑/压力}\n\n"
                "NEXT_ACTION: 10:15黄金窗口入场策略")

    if session == "opening":
        return (f"你是专业量化交易员（v4.0），10:15 ET 黄金窗口入场。\n"
                "开盘30分钟等待期已过（10:00解禁），现在做入场决策。\n\n"
                f"## 账户状态\n{portfolio}\n\n"
                f"## 可交易股票\n{watchlist_text}{focus_note}\n\n"
                f"## 新闻 & 开盘走势\n{news_summary}\n\n"
                + CONF + RULES +
                "逐股评估：\n"
                "▸ SYM | 看涨/看跌/中性 | 置信度：X/10\n"
                "  三要素：①量价{Y/N} ②方向{Y/N} ③大盘{Y/N}\n"
                "  仓位：{A仓30%/B仓20%} | 理由：{1-2句}\n\n"
                + DEC +
                "NEXT_ACTION: 11:30中盘评估重点")

    if session == "mid":
        return (f"你是专业量化交易员（v4.0），11:30 ET 中盘复盘。\n\n"
                f"## 账户状态\n{portfolio}\n\n"
                f"## 今日交易记录\n{log_summary}\n\n"
                f"## 当前报价\n{watchlist_text}\n\n"
                + CONF + RULES +
                "持仓评估（逐一说明）：\n"
                "▸ SYM | N股 | 均价$X | 现价$Y | 盈亏Z%\n"
                "  阶梯位置：{在第几止盈台阶/接近止损线}\n"
                "  建议：{继续持有/减仓/止损}\n\n"
                "DECISION:\n"
                "SELL|SYM|shares|理由（第X止盈/止损/减仓）\n"
                "BUY|SYM|shares|理由（新机会，置信度X/10）\n"
                "HOLD||0|说明\n\n"
                "NEXT_ACTION: 14:00尾盘策略")

    if session == "afternoon":
        return (f"你是专业量化交易员（v4.0），14:00 ET 尾盘第二机会窗口。\n\n"
                f"## 账户状态\n{portfolio}\n\n"
                f"## 今日交易记录\n{log_summary}\n\n"
                f"## 当前报价\n{watchlist_text}\n\n"
                + CONF + RULES +
                "持仓状态 + 尾盘机会评估：\n"
                "▸ SYM | 盈亏Z% | 建议（持有/减仓/止损/新机会）\n\n"
                "DECISION:\n"
                "SELL|SYM|shares|理由\n"
                "BUY|SYM|shares|理由（置信度X/10）\n"
                "HOLD||0|说明\n\n"
                "NEXT_ACTION: 15:45收尾策略，过夜计划")

    if session == "closing":
        return (f"你是专业量化交易员（v4.0），15:45 ET 收尾，禁止新开仓。\n\n"
                f"## 账户状态\n{portfolio}\n\n"
                f"## 今日交易记录\n{log_summary}\n\n"
                f"## 持仓报价\n{watchlist_text}\n\n"
                "## 过夜4条件（必须全部满足）\n"
                "①当日收盘仍盈利 ②明日无重大宏观数据（非农/CPI/Fed） "
                "③无隔夜财报 ④SPY站5日均线上方\n\n"
                + CONF +
                "逐仓判断：\n"
                "▸ SYM | N股 | 盈亏Z% | 4项条件 | 决定:过夜/平仓\n\n"
                "DECISION:\n"
                "SELL|SYM|shares|理由（不满足过夜条件）\n"
                "HOLD||0|过夜原因（四项全满足）\n\n"
                "NEXT_ACTION: 明日盘前重点")

    return ""
