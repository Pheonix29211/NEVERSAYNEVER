# MemeSniper — Solana Memecoin Autopilot (Pump.fun → Raydium)

Production-ready skeleton with **DRY_RUN (paper)** and **LIVE** modes. Photon-first execution (Jupiter fallback), fee cap ≤ **3%**, Rug Sentinel, **liquidity-aware** entries, **Giant Mode**, **Insider Accumulation**, Listing Event Mode, **Portfolio**, **Backtesting**, **Daily Reports**, **Exit slippage chunking**, and a **cheerful vibe**.

This repository is designed to run on **Render** with a **persistent disk**. You can run locally as well.

---

## ✨ Highlights
- **Three entry lanes:** SAFE (rug-safe), GIANT (Potential Giant Score), INSIDER (Insider Accumulation).
- **Liquidity-aware split fills:** dust → core → add; size ≤ 0.25% of pool; min pool $20k.
- **Execution:** Photon router (configurable) with Jupiter fallback; **fee cap ≤ 3%** (LP+router+priority+base).
- **Safety:** Rug Sentinel (Tier-A panic exit), Holder clustering (MDS/CC).
- **Profit Engine:** TP +50/+100/+200; Momentum-Hold; Giant floors + wide trails.
- **Insider exits:** early trims (+30 / +70), adaptive 6–18h hold, cluster-dump exits.
- **Listings Radar:** CEX listing event handling; exit optimization only.
- **Portfolio Mode:** equity/floor/active, exposure caps, CSV exports.
- **DRY_RUN toggle:** /toggle dryrun ↔ /toggle live (when keys set).
- **Daily report:** 21:30 IST (configurable).

> Live trading requires filling your Solana RPC/WS endpoints and wallet key (base58) in Render secrets. Default boot is **DRY_RUN**.

---

## 🧱 Project Layout
```
src/
  app.py                 # Entry: starts Telegram + workers + FastAPI
  config.py              # Env + defaults
  log.py                 # Rotating logs
  state.py               # App state & shutdown
  storage.py             # SQLite + JSON snapshot
  portfolio.py           # Equity, floor, active stack, exposure caps
  scoring.py             # SAFE score, PGS, IAS, CC/MDS
  services/
    pumpportal.py        # Pump.fun stream (stub; replace with real WS)
    listings.py          # Listing radar (public-only, stub)
    scheduler.py         # Daily report scheduler
    notifier.py          # Telegram msgs + vibe system
    execution.py         # Wallet sign/send (skeleton)
  routers/
    base_router.py       # Interface
    jupiter.py           # Jupiter router (quote+execute skeleton)
    photon.py            # Photon router (quote+execute skeleton)
  strategies/
    profit_engine.py     # trims, trails, floors, momentum-hold
    giant.py             # Giant rules
    insider.py           # Insider exit rules
  sentinel/
    rug_sentinel.py      # Tier-A/B/C logic (skeleton hooks)
  telegram/
    bot.py               # Telegram command handlers
backtest/
  run.py                 # CLI
  loader.py              # Data connectors (plug Birdeye/Dexscreener)
  features.py            # Compute features for scores
  sim.py                 # Simulator
  report.py              # CSV & charts
render.yaml              # Render blueprint (disk + env vars)
Procfile                 # Start command
requirements.txt
.env.example
```

---

## ⚙️ Quick Start (local)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill TELEGRAM_BOT_TOKEN + ADMIN_CHAT_ID to get messages
python -m src.app
```

## ☁️ Deploy on Render
1. Push to GitHub. Use the included **render.yaml** (Blueprint).  
2. In Render: **New → Blueprint** → select this repo.  
3. Set env vars (see `.env.example`). Keep `DRY_RUN=true` first.  
4. Health check: `/healthz`. Disk mounts at `/data` for SQLite + snapshots.

---

## 🔐 Secrets (Render)
- **Now:** `TELEGRAM_BOT_TOKEN`, `ADMIN_CHAT_ID`
- **Later for LIVE:** `SOLANA_PRIVATE_KEY_B58`, `SOLANA_RPC`, `SOLANA_WS`, `PHOTON_API_KEY`

> Use a **fresh hot wallet** with tiny SOL for first live tests.

---

## 🧪 Backtesting
Run `python -m backtest.run --days 30`. Replace `backtest/loader.py` with your data sources (Birdeye/Dexscreener). Exports CSVs and prints cohort summaries.

---

## 📜 License
MIT
