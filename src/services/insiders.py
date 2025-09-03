from __future__ import annotations
import os, aiohttp, asyncio, time, math
from typing import Dict, Any, List, Tuple, Optional
from ..config import Cfg
from ..log import logger

"""
Insider Accumulation Radar (bias signal):
- WATCH: basic sanity on holder dispersion & age.
- ARMED: funding inflow to creator/cluster, bonding progress/steady net buys.
- LIVE: pair exists + executable route (handled by normal gates elsewhere).
We only *alert/bias*; we do not force entries.
"""

async def _http_json(session: aiohttp.ClientSession, url: str, params=None, timeout=10):
    try:
        async with session.get(url, params=params, timeout=timeout) as r:
            if r.status != 200:
                txt = await r.text()
                logger.warning(f"[Insider] {r.status} {url} => {txt[:140]}")
                return None
            ct = r.headers.get("content-type","")
            if "json" in ct:
                return await r.json()
            return None
    except Exception as e:
        logger.warning(f"[Insider] error {url}: {e}")
        return None

def _zscore(series: List[float]) -> float:
    if not series:
        return 0.0
    m = sum(series)/len(series)
    v = sum((x-m)*(x-m) for x in series)/max(1, len(series)-1)
    s = math.sqrt(v) if v>0 else 1.0
    return (series[-1]-m)/s

async def _helius_token_transfers(session: aiohttp.ClientSession, mint: str, minutes: int = 90) -> List[Dict[str,Any]]:
    if not Cfg.HELIUS_API_KEY:
        return []
    url = f"{Cfg.HELIUS_BASE}/v0/addresses/{mint}/transactions"
    # NOTE: For production you'd use Helius' "token mint" transfer endpoint with proper query;
    # here we keep a stub shape to avoid breaking if key is absent.
    # Treat absence as "no data" => no insider alerts.
    return []

def _score_insiders(buckets: List[Dict[str,Any]]) -> Tuple[float, Dict[str,Any]]:
    # Placeholder scoring — you can expand with your wallet graph later.
    # We use diversity + netflow z-score as the core.
    distinct_buyers = buckets[-1].get("distinct_buyers", 0) if buckets else 0
    netflow_series = [b.get("net_inflow", 0.0) for b in buckets]
    z = _zscore(netflow_series)
    score = 0.0
    score += min(4.0, distinct_buyers/3.0)        # up to ~4 points
    score += max(0.0, min(4.0, z))                # up to ~4 points
    meta = {"distinct_buyers": distinct_buyers, "netflow_z": z}
    return score, meta

async def run_insider_radar(bot_send):
    if not Cfg.INSIDER_ENABLED or not Cfg.HELIUS_API_KEY:
        logger.info("[Insider] disabled or no Helius key; skipping.")
        return
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # You can wire in the same universe builder from radar if you want to correlate.
                # For now, run quietly; integrate with your scanner later if desired.
                await asyncio.sleep(120)
            except Exception as e:
                logger.warning(f"[Insider] loop error: {e}")
                await asyncio.sleep(15)
