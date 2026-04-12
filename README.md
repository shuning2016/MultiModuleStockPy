# Stock Trader Pro — Python / Vercel

AI-powered quantitative stock simulation trading system.  
Ported from Google Apps Script → **Python + Flask + Vercel**.

## Architecture

```
trader/
├── api/
│   ├── index.py          ← Flask app (Vercel entry point)
│   └── engine.py         ← Core trading engine
├── strategies/
│   ├── strategy_v4.py    ← v4.0 Full-day strategy
│   └── strategy_v5.py    ← v5.0 Leader Only / Gap&Go
├── static/
│   └── index.html        ← Single-page frontend
├── requirements.txt
├── vercel.json
└── .env.example
```

## Strategies

| Version | Style | Sessions |
|---------|-------|---------|
| **v4.0** | Full-day, ladder take-profit | Premarket · Opening · Mid · Afternoon · Closing |
| **v5.0** | Leader Only, trend trades, Gap&Go | + Gap&Go 9:35 special window |

### v4.0 Rules
- Trading window: 09:30–16:00 ET
- Max 3 holdings, single-stock cap 30%
- Cash floor 20%
- Ladder take-profit: +2% reduce ⅓ → +4% reduce another ⅓
- Stop-loss: -1% reduce 50% → -1.5% full exit
- SPY stop: SPY drops >-1.5% → full exit

### v5.0 Rules (on top of v4.0)
- **No Trade Day**: all scores < 7 → skip entire day
- **Gap & Go** (9:35 special): Gap >5% + catalyst + volume + no pull-back
- **Trend trade** mode: no fixed take-profit, VWAP-based exit
- **Dynamic sizing**: confidence 7→20%, 8→30%, ≥9 trend→40%
- **Setup tagging**: breakout / pullback / trend / reversal
- **Error flags**: is_plan_trade / is_fomo / violation

## Local Development

```bash
# 1. Clone & setup
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure keys
cp .env.example .env
# Edit .env with your actual keys

# 3. Run
python api/index.py
# → http://localhost:5000
```

## Deploy to Vercel

```bash
npm i -g vercel
vercel login
vercel --prod
```

Add environment variables in Vercel dashboard → Settings → Environment Variables:
- `FINNHUB_KEY`
- `NEWSAPI_KEY`
- `GROK_KEY`
- `CLAUDE_KEY`
- `DEEPSEEK_KEY`

## Key Design Decisions

### AI Response Parser (tolerant regex)
The DECISION block parser handles both formats:
```
# Standard multi-line
DECISION:
BUY|NVDA|10|breakout 置信度8/10

# Markdown bold (Grok/Claude common output)
**DECISION:** BUY|NVDA|10|breakout 置信度8/10
```

### State Storage
- **Current**: in-memory dict (resets on Vercel cold start)
- **Production upgrade**: swap `_STATE_STORE` in `engine.py` for Redis / Vercel KV
  ```python
  import vercel_kv  # pip install vercel-kv
  ```

### AI Providers
Switch provider per session via the UI tabs or API:
```json
POST /api/session/run
{"session": "opening", "provider": "grok", "strategy": "v5"}
```

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Get stock watchlist |
| POST | `/api/watchlist` | Save watchlist `{"stocks":[...]}` |
| GET | `/api/quote/:symbol` | Get stock quote |
| POST | `/api/news` | Get news `{"items":[{symbol,type}]}` |
| POST | `/api/analyze` | AI analysis `{"prompt","provider"}` |
| GET | `/api/state/:provider` | Get trade state |
| POST | `/api/state/:provider/reset` | Reset trade state |
| POST | `/api/session/run` | Run session `{"session","provider","strategy"}` |
| GET | `/api/strategies` | Get strategy configs |
| GET | `/api/metrics/:provider` | Get quant metrics |
| GET | `/api/time` | Get ET time + market status |

## Upgrading State Persistence

For production, replace the in-memory store with Vercel KV:

```python
# engine.py
import json
from vercel_kv import kv

def load_state(provider):
    raw = kv.get(f"trade_state:{provider}")
    return json.loads(raw) if raw else new_trade_state(provider)

def save_state(state, provider):
    kv.set(f"trade_state:{provider}", json.dumps(state))
```
