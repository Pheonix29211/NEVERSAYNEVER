from __future__ import annotations
import os, aiohttp, time
from typing import Optional, Dict, Any, List
from ..log import logger

DEX_BASE = os.environ.get("DEXSCREENER_BASE", "https://api.dexscreener.com").rstrip("/")
GT_BASE  = os.environ.get("GECKOTERMINAL_BASE", "https://api.geckoterminal.com").rstrip("/")

async def _http_json(session: aiohttp.ClientSession, url: str, params=None, timeout=10):
    try:
        async with session.get(url, params=params, timeout=timeout) as r:
            if r.status == 200:
                ct = r.headers.get("content-type","")
                if "json" in ct:
                    return await r.json()
                txt = await r.text()
                logger.warning(f"[MD] 200 non-json {url}: {txt[:120]}")
                return None
            txt = await r.text()
            logger.warning(f"[MD] {r.status} {url}: {txt[:160]}")
            return None
    except Exception as e:
        logger.warning(f"[MD] error {url}: {e}")
        return None

def _f(x) -> Optional[float]:
    try: return float(x)
    except: return None

async def ds_token_overview(session: aiohttp.ClientSession, mint: str) -> Dict[str, Any]:
    """
    Returns best pair snapshot with liquidity/MC/vol/age and priceChange.
    """
    url = f"{DEX_BASE}/latest/dex/tokens/{mint}"
    js = await _http_json(session, url)
    if not js:
        return {}
    pairs = js.get("pairs") or js.get("data") or []
    best = None; best_lp = -1.0
    for p in pairs:
        liq = p.get("liquidity", {})
        lp_usd = _f(liq.get("usd")) or 0.0
        mc = _f(p.get("fdv") or p.get("marketCap"))
        vol1h = _f(p.get("volume", {}).get("h1")) or _f(p.get("v1h")) or 0.0
        price_ch = p.get("priceChange") or {}
        age_min = None
        if p.get("pairCreatedAt"):
            age_min = max(0.0, (time.time()*1000 - float(p["pairCreatedAt"])) / 1000.0 / 60.0)
        item = {
            "pairAddress": p.get("pairAddress"),
            "dexId": p.get("dexId"),
            "baseToken": p.get("baseToken", {}),
            "quoteToken": p.get("quoteToken", {}),
            "lp_usd": lp_usd,
            "mc": mc,
            "vol1h": vol1h,
            "age_min": age_min,
            "priceChange": {
                "h1": _f(price_ch.get("h1")),
                "h6": _f(price_ch.get("h6")),
                "h24": _f(price_ch.get("h24")),
            }
        }
        if lp_usd > best_lp:
            best_lp = lp_usd; best = item
    return best or {}

async def gt_trending_pools(session: aiohttp.ClientSession, limit: int = 50) -> List[Dict[str,Any]]:
    url = f"{GT_BASE}/api/v2/networks/solana/trending_pools"
    js = await _http_json(session, url, params={"page": 1})
    pools = []
    try:
        data = js.get("data") or []
        for d in data[:limit]:
            attrs = d.get("attributes", {})
            base  = (d.get("relationships", {}).get("base_token", {}).get("data") or {}).get("id")
            pools.append({
                "pool_id": d.get("id"),
                "base_token_id": base,
                "lp_usd": _f(attrs.get("liquidity_usd")) or 0.0,
                "fdv_usd": _f(attrs.get("fdv_usd")) or None,
                "vol1h": _f(attrs.get("volume_usd", {}).get("h1") if isinstance(attrs.get("volume_usd"), dict) else None) or 0.0
            })
    except Exception:
        return []
    return pools
