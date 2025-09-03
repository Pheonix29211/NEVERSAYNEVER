from __future__ import annotations
import time, aiohttp, csv, os, math, random
from typing import Dict, Any, List, Tuple
from ...config import Cfg
from ...log import logger
from ..marketdata import gt_trending_pools, ds_token_overview
from ...routers.photon import PhotonRouter
from ..trailing import trailing_only_exits
from ..signals import gate_existing_pair

DATA_DIR = Cfg.DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)

def _choose_horizon(hours: int) -> str:
    # FIX: 12h should use h6, not h24
    if hours <= 1:  return "h1"
    if hours <= 12: return "h6"
    return "h24"

async def _universe_snapshot(session: aiohttp.ClientSession, hours: int) -> List[str]:
    try:
        pools = await gt_trending_pools(session, limit=120)
        mints = []
        for p in pools:
            base = p.get("base_token_id")
            if not base: continue
            mint = base.split("_", 1)[-1] if "_" in base else base
            mints.append(mint)
        return list(dict.fromkeys(mints))[:120]
    except Exception:
        return []

def _make_hf_path(target_change_pct: float, steps: int = 120) -> List[Tuple[float,float,float]]:
    start = 1.0
    end = max(0.10, 1.0 + target_change_pct/100.0)
    bars: List[Tuple[float,float,float]] = []
    cur = start
    burst_center = random.uniform(0.25, 0.75)
    burst_width  = random.uniform(0.08, 0.18)
    for i in range(steps):
        t = i/(steps-1)
        s = t*t*(3-2*t)
        base = start + (end-start)*s
        burst = math.exp(-((t-burst_center)**2)/(2*burst_width**2))
        bump = burst * (abs(end-start)) * 0.15
        noise = random.uniform(-0.003, 0.003)
        nxt = max(0.0000001, base + bump + noise)
        high = max(nxt, cur) * (1.0 + random.uniform(0.002, 0.01))
        low  = min(nxt, cur) * (1.0 - random.uniform(0.002, 0.01))
        bars.append((high, low, nxt))
        cur = nxt
    return bars

def _first_accel_entry(bars: List[Tuple[float,float,float]], accel_pct: float) -> int:
    if not bars: return -1
    p0 = bars[0][2]
    threshold = p0 * (1.0 + accel_pct/100.0)
    for i, (_, _, c) in enumerate(bars):
        if c >= threshold:
            return i
    return -1

async def run_backtest(hours: int = 24) -> Dict[str, Any]:
    random.seed(42)
    start = time.time()
    tokens_csv = os.path.join(DATA_DIR, f"tokens_{int(start)}.csv")
    trades_csv = os.path.join(DATA_DIR, f"trades_{int(start)}.csv")

    router = PhotonRouter(Cfg.PHOTON_BASE, Cfg.FEE_CAP_PCT)
    tokens_tested = entries = wins = losses = 0
    pnls: List[float] = []
    picked: List[Dict[str, Any]] = []
    horizon = _choose_horizon(hours)

    max_slots = Cfg.MAX_OPEN_POSITIONS
    per_trade = Cfg.PER_TRADE_USD_TARGET
    bankroll  = Cfg.PAPER_START_BAL_USD if Cfg.PAPER_MODE_BAL_ENABLED else per_trade*max_slots
    exposure_cap = Cfg.TOTAL_EXPOSURE_CAP_PCT
    open_slots = 0
    exposure   = 0.0

    async with aiohttp.ClientSession() as session:
        mints = await _universe_snapshot(session, hours)
        tokens_tested = len(mints)

        with open(tokens_csv, "w", newline="") as f:
            csv.writer(f).writerow(["mint","name","symbol","mc","lp_usd","vol1h","age_min","pchg","gate","reason"])
        with open(trades_csv, "w", newline="") as f:
            csv.writer(f).writerow(["mint","entry_bar","entry_usd","exit_usd","pnl_pct","exit_reason","peak_run_pct"])

        seen = set()
        for mint in mints:
            if mint in seen: continue
            seen.add(mint)

            meta = await ds_token_overview(session, mint)
            if not meta:
                continue

            # GATE with horizon-specific momentum
            ok, reason, meta = await gate_existing_pair(session, router, mint, horizon=horizon)
            base = meta.get("baseToken", {}) if isinstance(meta.get("baseToken"), dict) else {}
            name = base.get("name") or base.get("symbol") or "token"
            symb = base.get("symbol") or ""
            mc = meta.get("mc") or 0; lp = meta.get("lp_usd") or 0; vol1h = meta.get("vol1h") or 0; age = meta.get("age_min") or 0
            pchg = (meta.get("priceChange") or {}).get(horizon)

            with open(tokens_csv, "a", newline="") as f:
                csv.writer(f).writerow([mint, name, symb, mc, lp, vol1h, age, pchg, "ok" if ok else "skip", reason])

            if not ok:
                continue

            # HF synthetic path based on the horizon move
            steps = 240 if hours <= 1 else 180 if hours <= 12 else 120
            bars = _make_hf_path(target_change_pct=pchg if isinstance(pchg,(int,float)) else 15.0, steps=steps)

            # watch/chase entry
            idx = _first_accel_entry(bars, Cfg.WATCH_ENTRY_ACCEL_PCT)
            entry_idx = max(0, idx)
            entry_price = bars[entry_idx][2]

            # capacity check
            need = per_trade / max(1e-9, bankroll)
            if open_slots >= max_slots or (exposure + need) > exposure_cap:
                continue

            open_slots += 1
            exposure   += need
            picked.append({"mint": mint, "name": name, "symbol": symb, "mc": mc, "lp": lp, "vol1h": vol1h, "age_min": age, "pchg": pchg})
            entries += 1

            # trailing-only exit on the remaining path
            pnl_pct, peak_pct, exit_reason = trailing_only_exits(bars[entry_idx:])
            exit_v = per_trade * (1.0 + pnl_pct/100.0)
            pnls.append(pnl_pct)
            if pnl_pct >= 0: wins += 1
            else: losses += 1

            with open(trades_csv, "a", newline="") as f:
                csv.writer(f).writerow([mint, entry_idx, per_trade, exit_v, pnl_pct, exit_reason, peak_pct])

            # release slot (snapshot sim)
            open_slots -= 1
            exposure   -= need

    avg = round(sum(pnls)/len(pnls), 2) if pnls else 0.0
    top5 = round(sum(sorted(pnls, reverse=True)[:5]) / (min(5, len(pnls)) or 1), 2) if pnls else 0.0

    return {
        "tokens_tested": tokens_tested,
        "entries": entries,
        "wins": wins,
        "losses": losses,
        "avg_pnl": avg,
        "top5_avg": top5,
        "tokens_csv": tokens_csv,
        "trades_csv": trades_csv,
        "picked": picked,
        "horizon": horizon
    }
