"""
Strategy v5.0 — Leader Only Advanced Mode
Corresponds to: Trade_Rule_5.0.md
Trading hours: 09:30–16:00 ET with Gap&Go special window
"""

from __future__ import annotations
import math
import re
from typing import Optional


class StrategyV5:
    version = "v5.0"
    name = "Leader Only 进阶版（Trade_Rule_5.0.md）"

    # Position management
    MIN_CASH_RATIO = 0.20
    MAX_SINGLE_RATIO = 0.30        # normal cap
    MAX_SINGLE_TREND = 0.40        # trend trade privilege (confidence ≥ 9)
    MAX_HOLDINGS = 3
    MIN_CONFIDENCE = 7             # raised from v4's 6
    NO_TRADE_DAY_THRESHOLD = 7     # if all scores < 7 → No Trade Day

    # Dynamic position by confidence
    POSITION_BY_CONF = {
        6: 0.10,
        7: 0.20,
        8: 0.30,
        9: 0.40,
        10: 0.40,
    }

    # Dual-track exit
    TAKE_PROFIT_1 = 2.0            # normal trade: +2% reduce 1/3
    TAKE_PROFIT_2 = 4.0            # normal trade: +4% reduce another 1/3
    STOP_LOSS_PCT = 1.5            # all trades: -1.5% full exit
    EARLY_REDUCE_PCT = 1.0         # -1% reduce 50%
    SPY_STOP_PCT = 1.5             # SPY drops > -1.5% → full exit
    GAP_GO_MIN_GAP_PCT = 5.0      # Gap&Go minimum gap

    # Session schedule (ET)
    SESSIONS = [
        {"key": "premarket",  "hour": 9,  "min": 15,  "label": "盘前分析",    "time": "9:15"},
        {"key": "gapgo",      "hour": 9,  "min": 35,  "label": "Gap&Go窗口",  "time": "9:35"},
        {"key": "opening",    "hour": 10, "min": 15,  "label": "黄金窗口入场","time": "10:15"},
        {"key": "mid",        "hour": 11, "min": 30,  "label": "中盘复盘",    "time": "11:30"},
        {"key": "afternoon",  "hour": 14, "min": 0,   "label": "尾盘窗口",    "time": "14:00"},
        {"key": "closing",    "hour": 15, "min": 45,  "label": "收尾",        "time": "15:45"},
    ]
    TIME_BADGES = ["盘前 9:15", "Gap&Go 9:35", "入场 10:15", "中盘 11:30", "尾盘 14:00", "收尾 15:45"]


VIOLATIONS = [
    "none", "early_entry", "over_size", "no_confirmation",
    "counter_trend", "random_trade", "late_entry",
]


def get_position_ratio_by_conf(conf: int, is_trend_trade: bool = False) -> float:
    cfg = StrategyV5
    mapping = cfg.POSITION_BY_CONF
    ratio = mapping.get(conf) or (0.40 if conf >= 9 else 0.30 if conf >= 8 else 0.20 if conf >= 7 else 0.10)
    if not is_trend_trade and ratio > cfg.MAX_SINGLE_RATIO:
        ratio = cfg.MAX_SINGLE_RATIO
    return ratio


def check_position_rules_v5(state: dict, sym: str, shares: int, price: float,
                              confidence: int = 7, is_trend_trade: bool = False) -> dict:
    cfg = StrategyV5
    holdings = state.get("holdings", {})
    last_prices = state.get("lastPrices", {})

    total_assets = state.get("cash", 0)
    for s, h in holdings.items():
        total_assets += last_prices.get(s, h["avgCost"]) * h["shares"]

    # Rule 1: max 3 holdings
    if sym not in holdings and len(holdings) >= cfg.MAX_HOLDINGS:
        return {"shares": 0, "skip": True,
                "reason": f"持仓已满 {cfg.MAX_HOLDINGS} 只，跳过 {sym}"}

    # Rule 2: dynamic single-stock cap by confidence
    max_ratio = get_position_ratio_by_conf(confidence, is_trend_trade)
    max_allowed = total_assets * max_ratio
    existing = holdings[sym]["shares"] * (last_prices.get(sym) or holdings[sym]["avgCost"]) \
               if sym in holdings else 0
    room = max_allowed - existing
    if room <= 0:
        return {"shares": 0, "skip": True,
                "reason": f"{sym} 已达置信度{confidence}/10对应仓位上限 {max_ratio*100:.0f}%"}
    shares = min(shares, math.floor(room / price))
    if shares <= 0:
        return {"shares": 0, "skip": True, "reason": f"{sym} 动态仓位限制，无法买入"}

    # Rule 3: cash floor 20%
    usable = state["cash"] - total_assets * cfg.MIN_CASH_RATIO
    if usable <= 0:
        return {"shares": 0, "skip": True, "reason": "现金低于20%底线，跳过"}
    shares = min(shares, math.floor(usable / price))
    if shares <= 0:
        return {"shares": 0, "skip": True,
                "reason": f"买入后现金低于20%底线，跳过 {sym}"}

    return {"shares": shares, "skip": False, "reason": "", "maxRatio": max_ratio}


def check_no_trade_day_v5(ai_text: str) -> bool:
    """Returns True if all confidence scores < 7 (No Trade Day)."""
    threshold = StrategyV5.NO_TRADE_DAY_THRESHOLD
    scores = []
    for line in ai_text.split("\n"):
        t = line.strip()
        m = re.search(r"▸\s*([A-Z]+)\s*\|.*?置信度\s*[：:]?\s*(\d+)", t, re.IGNORECASE)
        if not m:
            m = re.search(r"▸\s*([A-Z]+)\s*\|.*?(\d+)\s*/\s*10", t, re.IGNORECASE)
        if m:
            score = int(m.group(2) if "置信度" in t else m.group(2))
            if 0 <= score <= 10:
                scores.append(score)
    if not scores:
        return False
    return max(scores) < threshold


def is_trend_trade_v5(reason: str) -> bool:
    if not reason:
        return False
    r = reason.lower()
    return "趋势单" in r or "trend trade" in r or "trend_trade" in r


def extract_setup_type_v5(reason: str) -> str:
    if not reason:
        return "unknown"
    r = reason.lower()
    if "breakout" in r:
        return "breakout"
    if "pullback" in r:
        return "pullback"
    if "reversal" in r:
        return "reversal"
    if "trend" in r:
        return "trend"
    return "unknown"


def parse_trade_flags_v5(reason: str, session: str) -> dict:
    r = reason.lower() if reason else ""
    flags = {
        "is_plan_trade": None,
        "is_fomo": None,
        "violation": "none",
        "is_leader": None,
        "is_gap_trade": False,
    }

    m_plan = re.search(r"is_plan_trade\s*[：:]\s*(YES|NO)", reason or "", re.IGNORECASE)
    if m_plan:
        flags["is_plan_trade"] = m_plan.group(1).upper() == "YES"
    elif any(k in r for k in ["盘前计划", "plan trade", "计划内"]):
        flags["is_plan_trade"] = True

    m_fomo = re.search(r"is_fomo\s*[：:]\s*(YES|NO)", reason or "", re.IGNORECASE)
    if m_fomo:
        flags["is_fomo"] = m_fomo.group(1).upper() == "YES"
    elif "fomo" in r or "追涨" in r:
        flags["is_fomo"] = True

    m_viol = re.search(r"violation\s*[：:]\s*([a-z_]+)", reason or "", re.IGNORECASE)
    if m_viol:
        v = m_viol.group(1).lower()
        flags["violation"] = v if v in VIOLATIONS else "none"

    m_leader = re.search(r"is_leader\s*[：:]\s*(YES|NO)", reason or "", re.IGNORECASE)
    if m_leader:
        flags["is_leader"] = m_leader.group(1).upper() == "YES"
    elif re.search(r"龙头|leader", r) and not re.search(r"非龙头|not\s*leader", r):
        flags["is_leader"] = True
    elif re.search(r"非龙头|not\s*leader", r):
        flags["is_leader"] = False

    if session in ("gapgo", "Gap&Go窗口"):
        flags["is_gap_trade"] = True
    elif re.search(r"gap.?go|gap\s*trade", r):
        flags["is_gap_trade"] = True

    return flags


def check_auto_stop_rules_v5(state: dict, session: str) -> list[dict]:
    cfg = StrategyV5
    sells = []
    last_prices = state.get("lastPrices", {})

    for sym, h in list(state.get("holdings", {}).items()):
        price = last_prices.get(sym, h["avgCost"])
        pct = (price - h["avgCost"]) / h["avgCost"] * 100
        is_trend = h.get("isTrendTrade", False)

        # Hard stop -1.5% (all trade types)
        if pct <= -cfg.STOP_LOSS_PCT:
            sells.append({"sym": sym, "shares": h["shares"],
                          "reason": f"止损 {pct:.2f}%（-{cfg.STOP_LOSS_PCT}% 触发，全出）",
                          "isTrend": is_trend})
            continue

        # Early reduce -1%
        if pct <= -cfg.EARLY_REDUCE_PCT:
            sells.append({"sym": sym, "shares": math.ceil(h["shares"] / 2),
                          "reason": f"提前减仓 {pct:.2f}%（-{cfg.EARLY_REDUCE_PCT}% 触发，减50%）",
                          "isTrend": is_trend})
            continue

        # Normal trades: ladder take-profit
        if not is_trend:
            tp2_done = h.get("tp2Done", False)
            tp1_done = h.get("tp1Done", False)

            if not tp2_done and pct >= cfg.TAKE_PROFIT_2:
                sell_sh = math.ceil(h["shares"] / 3)
                sells.append({"sym": sym, "shares": sell_sh,
                              "reason": f"普通单第二止盈 +{pct:.2f}%（+{cfg.TAKE_PROFIT_2}% 触发，减1/3）"})
                h["tp2Done"] = True
                continue

            if not tp1_done and pct >= cfg.TAKE_PROFIT_1:
                sell_sh = math.ceil(h["shares"] / 3)
                sells.append({"sym": sym, "shares": sell_sh,
                              "reason": f"普通单第一止盈 +{pct:.2f}%（+{cfg.TAKE_PROFIT_1}% 触发，减1/3）"})
                h["tp1Done"] = True
                continue

        # Trend trades: no fixed profit-taking (VWAP-based exit by AI)
        # Closing: non-profitable positions are exited
        if session == "closing" and pct < 0 and not is_trend:
            sells.append({"sym": sym, "shares": h["shares"],
                          "reason": f"收尾普通单亏损平仓 {pct:.2f}%（不持亏损过夜）"})

    return sells


_CONF_V5 = (
    "## 置信度（0-10）\n"
    "0-6：观望 | 7：B仓20% | 8：A仓30% | 9-10：趋势单特权40%\n"
    "⚠️ v5.0核心规则：所有标的置信度 <7 → 当日 No Trade Day，禁止交易\n"
    "入场四要素（缺一不可）：①量价放量突破 ②站稳关键位 ③SPY/QQQ同向 ④动能确认(连续K线创新高/无回踩)\n\n"
)

_TREND_V5 = (
    "## 趋势单 vs 普通单\n"
    "普通单：执行阶梯止盈（+2%减1/3，+4%再减1/3），VWAP跌破剩余全出\n"
    "趋势单（需全部满足）：龙头股 + 板块共振 + 全天强趋势 + 放量\n"
    "  → 趋势单：❌ 不设固定止盈，跟随VWAP/结构位出场，让利润奔跑\n"
    "  → reason字段必须包含[趋势单]字样\n\n"
)

_SETUP_V5 = (
    "## Setup分类（每笔必须标注）\n"
    "reason中必须包含以下之一：breakout | pullback | trend | reversal\n\n"
)

_ERROR_V5 = (
    "## 错误交易标记（每笔必须标注）\n"
    "is_plan_trade: 是否属于盘前计划内交易（YES/NO）\n"
    "is_fomo: 是否为情绪/FOMO交易（YES/NO）\n"
    "violation: 违规类型（none | early_entry | over_size | no_confirmation | counter_trend | random_trade | late_entry）\n\n"
)


def build_prompt_v5(session: str, portfolio: str, watchlist_text: str,
                    news_summary: str, log_summary: str = "", focus_note: str = "") -> str:
    if session == "premarket":
        return (
            "你是专业量化交易员（v5.0 Leader Only模式），09:15 ET，盘前分析。初始资金$10,000。\n\n"
            f"## 账户\n{portfolio}\n\n"
            f"## 可交易股票（日均成交额>$5亿，Leader Only）\n{watchlist_text}{focus_note}\n\n"
            f"## 盘前新闻\n{news_summary}\n\n"
            "## 规则（v5.0）\n"
            "交易窗口：09:30–16:00 ET\n"
            "开盘30分钟（09:30-10:00）不开新仓，除非 Gap>5%（Gap&Go特例）\n"
            "止损：结构位 or -1.5%全出 | 大盘止损：SPY跌>-1.5%全平\n\n"
            + _CONF_V5 + _TREND_V5 +
            "## 今日盘前计划（Pre-Market Plan）\n"
            "1. 今日主线板块（宏观 + 板块 + 资金偏好risk-on/off）\n"
            "2. 今日龙头 Leader：板块1 → Leader: | 板块2 → Leader:\n"
            "3. 可交易性判断：YES/NO + 理由\n"
            "4. 风险事件：CPI/非农/Fed/财报\n"
            "5. 关键价位：SPY | QQQ | 重点标的\n"
            "6. 是否允许 No Trade Day：YES/NO\n\n"
            "逐股评分（只分析龙头和有催化剂的标的）：\n\n"
            "▸ SYM | 看涨/看跌/中性 | 置信度：X/10\n"
            "  催化剂：{1句} | 入场四要素预判 | 关键价位：{支撑/压力}\n\n"
            "⚠️ 如果所有标的置信度 <7，明确声明：【今日 No Trade Day，禁止交易】\n\n"
            "NEXT_ACTION: 今日龙头和首选标的"
        )

    if session == "gapgo":
        return (
            "你是专业量化交易员（v5.0），09:35 ET，Gap & Go 特例窗口。\n\n"
            f"## 账户\n{portfolio}\n\n"
            f"## 当前价格 vs 昨收（Gap检测）\n{watchlist_text}{focus_note}\n\n"
            f"## 盘前新闻\n{news_summary}\n\n"
            "## Gap & Go 入场条件（必须全部满足）\n"
            "1. Gap > +5%（正缺口方向做多）\n"
            "2. 有明确催化剂（财报超预期 / 行业爆发新闻）\n"
            "3. 开盘前5分钟成交量显著放大（放量确认，非缩量）\n"
            "4. 无明显回踩（强趋势，不是假突破）\n\n"
            + _CONF_V5 +
            "仓位：Gap & Go 允许直接用 B仓（20%）入场\n"
            "止损：开盘低点\n\n"
            "逐股评估：\n"
            "▸ SYM | Gap幅度: X% | 有无催化剂 | 放量: Y/N | 置信度：X/10\n"
            "  Gap&Go条件：①Gap{Y/N} ②催化剂{Y/N} ③放量{Y/N} ④无回踩{Y/N}\n"
            "  操作：{仅当4项全满足且置信度≥7时给BUY，否则HOLD}\n\n"
            "⚠️ Gap&Go是例外，不是常规操作。条件不满足宁可不做。\n\n"
            "DECISION:\n"
            "BUY|SYM|shares|Gap&Go breakout 置信度X/10 is_plan_trade:YES is_fomo:NO violation:none\n"
            "HOLD||0|Gap条件未满足\n\n"
            "NEXT_ACTION: 10:15黄金窗口策略"
        )

    if session == "opening":
        return (
            "你是专业量化交易员（v5.0），10:15 ET，黄金窗口入场决策。\n"
            "10:00开盘观察期解禁，10:15起正式做入场决策。\n\n"
            f"## 账户\n{portfolio}\n\n"
            f"## 可交易股票\n{watchlist_text}{focus_note}\n\n"
            f"## 开盘走势\n{news_summary}\n\n"
            "## 规则（v5.0）\n"
            "单股动态仓位：置信度7→20% | 8→30% | ≥9且趋势单→40%\n"
            "最多持3只 | 现金≥20% | 不做空/杠杆/期权\n\n"
            + _CONF_V5 + _TREND_V5 + _SETUP_V5 +
            "四要素全满足且置信度≥7才入场（否则No Trade Day）：\n\n"
            "▸ SYM | 看涨/看跌/中性 | 置信度：X/10\n"
            "  四要素：①量价{Y/N} ②方向{Y/N} ③大盘{Y/N} ④动能{Y/N}\n"
            "  是否趋势单：{YES/NO} | Setup: {breakout/pullback/trend/reversal}\n"
            "  仓位：{X%/股数} | 理由：{1-2句}\n\n"
            "DECISION:\n"
            "BUY|SYM|shares|[趋势单/普通单] breakout 置信度X/10 is_plan_trade:YES is_fomo:NO violation:none\n"
            "SELL|SYM|shares|理由\n"
            "HOLD||0|原因（若全部<7请注明：No Trade Day）\n\n"
            "NEXT_ACTION: 11:30中盘评估重点（趋势单持有计划）"
        )

    if session == "mid":
        return (
            "你是专业量化交易员（v5.0），11:30 ET，中盘复盘。\n\n"
            f"## 账户\n{portfolio}\n\n"
            f"## 今日交易\n{log_summary}\n\n"
            f"## 可交易股票\n{watchlist_text}{focus_note}\n\n"
            "## 止盈止损（v5.0双轨制）\n"
            "普通单：+2%减1/3 | +4%再减1/3 | VWAP跌破剩余全出\n"
            "趋势单：不设固定止盈，只在跌破VWAP/结构位时出场\n"
            "共同：-1%减50% | -1.5%全出 | SPY跌>-1.5%全平\n\n"
            + _CONF_V5 +
            "持仓股逐一评估：\n"
            "▸ SYM | N股 | 均$X | 现$Y | 盈亏Z% | 类型:{趋势单/普通单} | 置信度X/10\n"
            "  VWAP参考：{当前是否在VWAP上方}\n"
            "  趋势单：{趋势是否仍在？结构位在哪？} 普通单：{在哪个阶梯位置？}\n"
            "  建议：{继续持有/减仓/止损}\n\n"
            "DECISION:\n"
            "SELL|SYM|shares|理由（趋势单：VWAP跌破 / 普通单：第X止盈）\n"
            "BUY|SYM|shares|理由（置信度X/10，补仓/新机会）\n"
            "HOLD||0|说明\n\n"
            "NEXT_ACTION: 尾盘(14:00)策略，趋势单持有计划"
        )

    if session == "afternoon":
        return (
            "你是专业量化交易员（v5.0），14:00 ET，尾盘第二机会窗口。\n\n"
            f"## 账户\n{portfolio}\n\n"
            f"## 今日交易\n{log_summary}\n\n"
            f"## 可交易股票\n{watchlist_text}{focus_note}\n\n"
            "## 尾盘规则（v5.0）\n"
            "尾盘往往有方向性，是第二个入场机会\n"
            "持仓趋势单：继续跟踪VWAP，未破则持有\n"
            "新机会：仍需满足四要素 + 置信度≥7\n\n"
            + _CONF_V5 + _TREND_V5 +
            "持仓股状态 + 尾盘机会评估：\n"
            "▸ SYM | 盈亏Z% | 类型:{趋势单/普通单} | VWAP状态 | 置信度X/10 | 建议\n\n"
            "DECISION:\n"
            "SELL|SYM|shares|理由\n"
            "BUY|SYM|shares|理由（置信度X/10）\n"
            "HOLD||0|说明\n\n"
            "NEXT_ACTION: 15:45收尾策略，过夜持仓计划"
        )

    if session == "closing":
        return (
            "你是专业量化交易员（v5.0），15:45 ET，收尾，决定持仓过夜或平仓。\n\n"
            f"## 账户\n{portfolio}\n\n"
            f"## 今日交易\n{log_summary}\n\n"
            f"## 持仓\n{watchlist_text}\n\n"
            "## 过夜条件（v5.0）\n"
            "普通单过夜：全部满足→①当日收盘盈利 ②明日无重大宏观数据 ③无隔夜财报 ④SPY站5日均线上方\n"
            "趋势单过夜：若趋势极强可忽略部分限制，但必须仍处于盈利状态\n"
            "禁止过夜：持仓亏损 | 当天大盘跌>-1.5% | 持仓标的有隔夜财报\n\n"
            + _CONF_V5 +
            "逐仓判断（重点：趋势单评估趋势是否延续）：\n"
            "▸ SYM | N股 | 盈亏Z% | 类型:{趋势单/普通单} | VWAP上/下 | 过夜四项 | 决定\n\n"
            "⚠️ 趋势单判断重点：\n"
            "  - 今日趋势是否仍强？（收盘价是否在VWAP上方）\n"
            "  - 明日是否有催化剂延续？\n"
            "  - 趋势单过夜止损位：跌破今日低点全出\n\n"
            "DECISION:\n"
            "SELL|SYM|shares|理由（不满足过夜条件）\n"
            "HOLD||0|过夜原因（趋势单：趋势仍强/普通单：四项全满足）\n\n"
            "## 今日复盘（每日必填）\n"
            "1. 今日盈亏 vs 计划目标\n"
            "2. 遵守规则情况（No Trade Day / Gap&Go / 止损纪律）\n"
            "3. 趋势单执行情况（有没有过早止盈？）\n"
            "4. 明日盘前重点关注\n\n"
            "NEXT_ACTION: 明日盘前策略"
        )

    return ""
