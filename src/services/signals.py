from __future__ import annotations
import aiohttp
from typing import Tuple, Dict, Any, Optional
from ..config import Cfg
from ..log import logger
from .marketdata import ds_token_overview

def _pick_momentum_for_horizon(price_change: Dict[str, Optional[float]], horizon: str) -> Optional[float]:
    if not price_change:
        return None
    val = price_change.get(horizon)
    return float(val) if isinstance(val, (int, float)) else None

def _momentum_ok(meta: Dict[str, Any], horizon: str) -> Tuple[bool, str, Optional[float]]:
    """
    Enforce momentum check on the chosen horizon only (h1/h6/h24).
    """
    if not Cfg.ENTRY_REQUIRE_POSITIVE_MOM:
        return True, "mom_skip", None

    mom = _pick_momentum_for_horizon(meta.get("priceChange") or {}, horizon)
    if mom is None:
        return False, f"mom_unknown({horizon})", None

    # parse optional cap
    cap = None
    try:
        if str(Cfg.ENTRY_PCHG_MAX).lower() != "none":
            cap = float(Cfg.ENTRY_PCHG_MAX)
    except Exception:
        cap = None

    if mom < Cfg.ENTRY_PCHG_MIN:
        return False, f"mom_low_{horizon} ({mom:+.1f}%)", mom
    if cap is not None and mom > cap:
        return False, f"mom_too_hot_{horizon} ({mom:+.1f}%)", mom
    return True, "mom_ok", mom

async def gate_existing_pair(session: aiohttp.ClientSession, router, mint: str, horizon: str = "h24") -> Tuple[bool, str, Dict[str,Any]]:
    """
    Gate for already-listed pairs (Dex) with fee/slip caps + horizon-specific momentum.
    Returns (ok, reason, meta_enriched).
    """
    meta = await ds_token_overview(session, mint)
    if not meta:
        return (False, "dex_overview_unavailable", {})

    mc   = float(meta.get("mc") or 0)
    lp   = float(meta.get("lp_usd") or 0)
    vol1h= float(meta.get("vol1h") or 0)
    age  = float(meta.get("age_min") or 0)

    if mc < Cfg.ENTRY_MC_MIN or mc > Cfg.ENTRY_MC_MAX:
        return (False, f"mc_out_of_band ({mc:.0f})", meta)
    if lp < Cfg.ENTRY_LP_MIN_USD:
        return (False, f"lp_too_low (${lp:.0f})", meta)
    if mc > 0 and (lp/mc) < Cfg.ENTRY_LP_TO_MCAP_MIN:
        return (False, f"lp_to_mc_low ({(lp/mc):.1%})", meta)
    if age < Cfg.ENTRY_POOL_AGE_MIN:
        return (False, f"pool_too_young ({age:.0f}m)", meta)
    if vol1h < Cfg.VOL1H_MIN:
        return (False, f"vol1h_low (${vol1h:.0f})", meta)

    ok_m, why_m, mom = _momentum_ok(meta, horizon)
    if not ok_m:
        return (False, why_m, meta)
    meta["momentum"] = mom
    meta["horizon"]  = horizon

    # Route/quote check (respect caps)
    async with aiohttp.ClientSession() as session2:
        q = await router.quote_buy(session2, mint, Cfg.PER_TRADE_USD_TARGET)
        if q.get("ok"):
            fee_ok  = (q.get("fee_pct", 0.0) <= Cfg.FEE_CAP_PCT)
            slip_ok = (q.get("slip_pct", 0.0) <= Cfg.MAX_SLIPPAGE_PCT)
            if fee_ok and slip_ok:
                meta.update({
                    "q_usd": Cfg.PER_TRADE_USD_TARGET,
                    "fee_pct": q.get("fee_pct", 0.0),
                    "slip_pct": q.get("slip_pct", 0.0),
                    "price": q.get("price", meta.get("price", 0.0)),
                    "route_info": q.get("route_info"),
                })
                return (True, "ok", meta)
            return (False, f"route_caps (fee {q.get('fee_pct',0):.2f}%, slip {q.get('slip_pct',0):.2f}%)", meta)
        return (False, "no_executable_route", meta)
