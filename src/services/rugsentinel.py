# src/services/rugsentinel.py
from __future__ import annotations
import os, asyncio
from typing import Tuple, Optional, Dict, Any
import aiohttp

from ..config import Cfg
from ..log import logger

BIRDEYE_BASE = os.environ.get("BIRDEYE_BASE", "https://public-api.birdeye.so").rstrip("/")
BIRDEYE_KEY  = os.environ.get("BIRDEYE_API_KEY", "").strip()
RUGCHECK_API = os.environ.get("RUGCHECK_API", "").rstrip("/")

# ---------------------------
# Helpers to query providers
# ---------------------------
async def _get_birdeye_overview(session: aiohttp.ClientSession, ca: str) -> Optional[Dict[str, Any]]:
    """Birdeye token overview: may contain liquidity, taxes, flags (varies by plan)."""
    if not BIRDEYE_KEY:
        return None
    url = f"{BIRDEYE_BASE}/defi/token_overview"
    headers = {"x-api-key": BIRDEYE_KEY}
    params  = {"address": ca, "chain": "solana"}
    try:
        async with session.get(url, headers=headers, params=params, timeout=12) as r:
            if r.status != 200:
                txt = await r.text()
                logger.warning(f"[Rug] Birdeye overview {ca} status {r.status}: {txt[:160]}")
                return None
            js = await r.json()
            return js.get("data") or js
    except Exception as e:
        logger.warning(f"[Rug] Birdeye overview error {ca}: {e}")
        return None

async def _get_rugcheck(session: aiohttp.ClientSession, ca: str) -> Optional[Dict[str, Any]]:
    """Optional RugCheck-style API: /v1/tokens/{ca} returning {score, flags:[...] }."""
    if not RUGCHECK_API:
        return None
    url = f"{RUGCHECK_API}/v1/tokens/{ca}"
    try:
        async with session.get(url, timeout=10) as r:
            if r.status != 200:
                txt = await r.text()
                logger.warning(f"[Rug] RugCheck {ca} status {r.status}: {txt[:160]}")
                return None
            return await r.json()
    except Exception as e:
        logger.warning(f"[Rug] RugCheck error {ca}: {e}")
        return None

# ---------------------------
# Scoring / veto rules
# ---------------------------
def _strictness() -> str:
    s = (Cfg.RUG_STRICT or "balanced").lower().strip()
    return s if s in ("hard", "balanced", "degen") else "balanced"

def _tax_limit_for_entry() -> float:
    """
    Max allowed buy/sell tax (%) at entry.
    'hard'     →  1.5%
    'balanced' →  2.5%
    'degen'    →  5.0%
    """
    mode = _strictness()
    if mode == "hard": return 1.5
    if mode == "degen": return 5.0
    return 2.5

def _holder_concentration_limit() -> float:
    """
    Max allowed top holder percentage (if available).
    'hard'     →  15%
    'balanced' →  25%
    'degen'    →  40%
    """
    mode = _strictness()
    if mode == "hard": return 15.0
    if mode == "degen": return 40.0
    return 25.0

def _min_liq_usd_gate() -> float:
    # Use MIN_POOL_USD for new tokens; radar/old lanes use their own gates.
    return max(0.0, float(Cfg.MIN_POOL_USD))

def _has_bad_flag(flags: Any) -> Optional[str]:
    """Scan generic flags list/dict for obvious rug markers."""
    if not flags: return None
    txt = " ".join(map(str, flags)).lower() if isinstance(flags, (list, tuple)) else str(flags).lower()
    bad_words = ["honeypot", "trading_disabled", "blacklist", "mint_authority", "freeze_authority", "owner_can_mint", "owner_can_freeze"]
    for w in bad_words:
        if w in txt:
            return w
    return None

# ---------------------------
# Public: pre-entry check
# ---------------------------
async def rug_pre_entry_check(session: aiohttp.ClientSession, ca: str) -> Tuple[bool, str]:
    """
    Returns (allow, reason).
    allow = True  → safe to proceed
    allow = False → veto with reason
    """
    # 1) Optional RugCheck service
    rc = await _get_rugcheck(session, ca)
    if rc:
        # Expect rc like: {"score": 78, "flags": ["owner_can_mint", ...]}
        score = float(rc.get("score", 0.0))
        flags = rc.get("flags")
        bad = _has_bad_flag(flags)
        if bad:
            return False, f"rugcheck_flag:{bad}"
        # If service scores risk inversely, tweak here if needed. We assume higher = safer.
        if score and score < 50:
            return False, f"rugcheck_low_score:{score:.0f}"

    # 2) Birdeye overview (taxes/liquidity/flags if available on your plan)
    be = await _get_birdeye_overview(session, ca)
    if be:
        # Taxes (names vary by plan; try a few)
        buy_tax  = be.get("buyTax")  or be.get("buy_tax")  or be.get("tradeBuyTax")
        sell_tax = be.get("sellTax") or be.get("sell_tax") or be.get("tradeSellTax")
        try:
            buy_tax = float(buy_tax) if buy_tax is not None else None
            sell_tax = float(sell_tax) if sell_tax is not None else None
        except Exception:
            buy_tax = buy_tax if isinstance(buy_tax,(int,float)) else None
            sell_tax = sell_tax if isinstance(sell_tax,(int,float)) else None

        tax_cap = _tax_limit_for_entry()
        if buy_tax is not None and buy_tax > tax_cap:
            return False, f"buy_tax>{tax_cap}%"
        if sell_tax is not None and sell_tax > tax_cap:
            return False, f"sell_tax>{tax_cap}%"

        # LP presence (if Birdeye provides)
        liq = be.get("liquidityUSD") or be.get("liquidity_usd") or be.get("liquidity")
        try:
            liq = float(liq) if liq is not None else None
        except Exception:
            liq = None
        min_liq = _min_liq_usd_gate()
        if liq is not None and liq < min_liq:
            return False, f"lp<{min_liq:,.0f}"

        # Holder concentration (some plans return top holder % or owner percent)
        top_holder_pct = be.get("topHolderPct") or be.get("owner_pct") or be.get("top10HolderPct")
        try:
            top_holder_pct = float(top_holder_pct) if top_holder_pct is not None else None
        except Exception:
            top_holder_pct = None
        limit = _holder_concentration_limit()
        if top_holder_pct is not None and top_holder_pct > limit:
            return False, f"holder_concentration>{limit}%"

        # Obvious flags (honeypot, freeze, blacklist)
        be_flags = be.get("flags") or be.get("warnings") or be.get("riskLabels")
        bad = _has_bad_flag(be_flags)
        if bad:
            return False, f"birdeye_flag:{bad}"

    # 3) If both sources unavailable, we allow but warn (don’t brick entries during provider hiccups)
    if not rc and not be:
        logger.warning(f"[Rug] No provider data for {ca}; proceeding due to graceful mode.")
        return True, "providers_unavailable"

    return True, "ok"

# ---------------------------
# (Optional) in-position watch
# ---------------------------
async def rug_live_watch_hint(
    *,
    liq_drop_pct: float,
    fee_now_pct: float,
    sell_dominance_pct: float
) -> Optional[str]:
    """
    Returns a short reason string if we should emergency-cut a live position.
    This is used by the execution engine heuristics; feel free to expand.
    """
    if liq_drop_pct >= max(5.0, Cfg.LIQ_DRAIN_EXIT_PCT):
        return f"lp_drain_{liq_drop_pct:.0f}%"
    if fee_now_pct >= max(2.0, Cfg.FEE_SPIKE_EXIT_PCT):
        return f"fee_spike_{fee_now_pct:.2f}%"
    if sell_dominance_pct >= max(60.0, Cfg.SELL_DOMINANCE_EXIT):
        return f"sellers_{sell_dominance_pct:.0f}%"
    return None
