"""
Flask API — Vercel entry point
All /api/* routes + static frontend serving.
"""
from __future__ import annotations
import os
import sys
import time

# Ensure project root is on path (needed for Vercel)
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from api.engine import (
    load_state, save_state, reset_state,
    get_watchlist, save_watchlist,
    get_stock_quote, get_news_for_items,
    analyze_stock, run_trade_session,
    build_portfolio_summary, build_watchlist_context,
    get_today_et, get_now_et, is_trading_day,
    calc_quant_metrics, load_session_log, clear_session_log,
    INITIAL_CASH,
)
from api.store import backend_info
from strategies import StrategyV4, StrategyV5

_STATIC = os.path.join(_root, "static")

app = Flask(__name__, static_folder=_STATIC)
CORS(app)


# ─── helpers ─────────────────────────────────────────────────────────────────

def ok(data):
    return jsonify({"ok": True, "data": data})

def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


# ─── Static frontend ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(_STATIC, "index.html")

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(_STATIC, filename)


# ─── Watchlist ───────────────────────────────────────────────────────────────

@app.route("/api/watchlist", methods=["GET"])
def api_get_watchlist():
    return ok(get_watchlist())

@app.route("/api/watchlist", methods=["POST"])
def api_save_watchlist():
    body = request.get_json(force=True) or {}
    return ok(save_watchlist(body.get("stocks", [])))


# ─── Market data ─────────────────────────────────────────────────────────────

@app.route("/api/quote/<symbol>")
def api_quote(symbol):
    q = get_stock_quote(symbol.upper())
    return ok(q) if q else err("Quote not available", 404)

@app.route("/api/news", methods=["POST"])
def api_news():
    body = request.get_json(force=True) or {}
    return ok(get_news_for_items(body.get("items", []), body.get("limit", 5)))


# ─── AI analysis ─────────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body = request.get_json(force=True) or {}
    prompt   = body.get("prompt", "")
    provider = body.get("provider", "grok")
    if not prompt:
        return err("prompt required")
    return ok(analyze_stock(prompt, provider))


# ─── Trade state ─────────────────────────────────────────────────────────────

@app.route("/api/state/<provider>", methods=["GET"])
def api_get_state(provider):
    return ok(load_state(provider))

@app.route("/api/state/<provider>/reset", methods=["POST"])
def api_reset_state(provider):
    return ok(reset_state(provider))

@app.route("/api/state/<provider>/save", methods=["POST"])
def api_save_state(provider):
    """Allow frontend to push a local state back to server."""
    body = request.get_json(force=True) or {}
    state = body.get("state")
    if not state:
        return err("state required")
    save_state(state, provider)
    return ok({"saved": True})


# ─── Session execution ───────────────────────────────────────────────────────

@app.route("/api/session/run", methods=["POST"])
def api_run_session():
    body     = request.get_json(force=True) or {}
    session  = body.get("session")
    provider = body.get("provider", "grok")
    strategy = body.get("strategy", "v5")
    if not session:
        return err("session required")
    result = run_trade_session(session, provider, strategy)
    return ok(result)


# ─── Session log (persisted) ─────────────────────────────────────────────────

@app.route("/api/log", methods=["GET"])
def api_get_log():
    provider = request.args.get("provider", "")
    log = load_session_log()
    if provider:
        log = [e for e in log if e.get("provider") == provider]
    return ok(log)

@app.route("/api/log/clear", methods=["POST"])
def api_clear_log():
    clear_session_log()
    return ok({"cleared": True})


# ─── Metrics ─────────────────────────────────────────────────────────────────

@app.route("/api/metrics/<provider>", methods=["GET"])
def api_metrics(provider):
    state = load_state(provider)
    return ok(calc_quant_metrics(state))


# ─── Strategy configs ────────────────────────────────────────────────────────

@app.route("/api/strategies", methods=["GET"])
def api_strategies():
    return ok({
        "v4": {
            "version": StrategyV4.version,
            "name": StrategyV4.name,
            "sessions": StrategyV4.SESSIONS,
            "timeBadges": StrategyV4.TIME_BADGES,
        },
        "v5": {
            "version": StrategyV5.version,
            "name": StrategyV5.name,
            "sessions": StrategyV5.SESSIONS,
            "timeBadges": StrategyV5.TIME_BADGES,
        },
    })


# ─── Helpers ─────────────────────────────────────────────────────────────────

@app.route("/api/time", methods=["GET"])
def api_time():
    return ok({
        "todayET": get_today_et(),
        "nowET": get_now_et(),
        "isTradingDay": is_trading_day(),
    })

@app.route("/api/context", methods=["POST"])
def api_context():
    body = request.get_json(force=True) or {}
    provider = body.get("provider", "grok")
    state = load_state(provider)
    portfolio = build_portfolio_summary(state)
    wl = build_watchlist_context(state)
    return ok({"portfolio": portfolio, "watchlistText": wl["text"], "symbols": wl["symbols"]})

@app.route("/api/health")
def health():
    return ok({"status": "ok", "time": get_today_et(),
               "store": backend_info()})


# ─── Vercel entry ────────────────────────────────────────────────────────────

# Vercel looks for a module-level `app` (WSGI callable) — done above.

if __name__ == "__main__":
    # Local dev: load .env then run
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_root, ".env"))
    except ImportError:
        pass
    app.run(debug=True, port=5000)
