from __future__ import annotations
import os, asyncio, time
from typing import List, Dict, Any, Optional
import aiohttp, pandas as pd
from ..config import Cfg
from ..log import logger
from .marketdata import get_liquidity_usd
from .signals import compute_promo_signal

DEXSCREENER_RECENT = os.environ.get("DEXSCREENER_RECENT", "https://api.dexscreener.com/latest/dex/pairs/solana").strip()
BIRDEYE_BASE = os.environ.get("BIRDEYE_BASE", "https://public-api.birdeye.so")
BIRDEYE_KEY  = os.environ.get("BIRDEYE_API_KEY", "").strip()

async def _birdeye_ohlc(session: aiohttp.ClientSession, ca: str, minutes: int = 360) -> pd.DataFrame:
    if not BIRDEYE_KEY: return pd.DataFrame()
    now = int(time.time()); start = now - minutes*60
    url = f"{BIRDEYE_BASE}/defi/ohlcv"
    headers = {"x-api-key": BIRDEYE_KEY}
    params = {"address": ca, "type": "1m", "time_from": start, "time_to": now, "chain": "solana"}
    async with session.get(url, headers=headers, params=params, timeout=15) as r:
        if r.status != 200: return pd.DataFrame()
        js = await r.json()
    rows = js.get("data", {}).get("items") or js.get("data") or []
    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows)
    for k in ("o","h","l","c","v"): 
        if k in df.columns: df[k] = pd.to_numeric(df[k], errors="coerce")
    if "timestamp" in df.columns: df["ts"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    elif "t" in df.columns:       df["ts"] = pd.to_datetime(df["t"], unit="s", utc=True)
    else:                         df["ts"] = pd.to_datetime(df.index, unit="s", utc=True)
    return df.dropna(subset=["c"])[["ts","o","h","l","c","v"]]

async def _fetch_recent_pairs(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    try:
        async with session.get(DEXSCREENER_RECENT, timeout=15) as r:
            if r.status != 200: return []
            data = await r.json()
            return data.get("pairs") or []
    except Exception as e:
        logger.error(f"[OldScan] Dex error: {e}"); return []

def _normalize(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if p.get("chainId") != "solana": return None
    base = p.get("baseToken") or {}
    ca = base.get("address"); sym = base.get("symbol") or base.get("name")
    if not ca or not sym: return None
    age_min = p.get("ageMinutes") or p.get("age_minutes") or 0
    liq_usd = ((p.get("liquidity") or {}).get("usd")) or 0
    vol24 = p.get("volume", {}).get("h24") or p.get("volume24h") or 0
    return {"token": str(sym), "ca": str(ca), "age_min": int(age_min or 0), "liq_usd": float(liq_usd or 0.0), "vol24_usd": float(vol24 or 0.0), "dex": p.get("dexId") or ""}

async def _scan_once(bot) -> int:
    alerts = 0
    async with aiohttp.ClientSession() as session:
        pairs = await _fetch_recent_pairs(session)
        if not pairs: return 0
        normalized = [x for x in (_normalize(p) for p in pairs) if x]
        oldish = [x for x in normalized if x["age_min"] >= Cfg.OLD_MIN_AGE_MIN]
        oldish.sort(key=lambda z: z["vol24_usd"], reverse=True)
        candidates = oldish[: max(5, Cfg.OLD_TOP_N)]

        for c in candidates:
            ca, token = c["ca"], c["token"]
            liq = await get_liquidity_usd(session, ca) or c.get("liq_usd") or 0.0
            vol24 = float(c["vol24_usd"] or 0.0)
            if vol24 < Cfg.OLD_MIN_VOL24H_USD: 
                continue

            ohlc = await _birdeye_ohlc(session, ca, minutes=360)
            if ohlc.empty: 
                continue

            sig = compute_promo_signal(ohlc, liq_usd_now=float(liq), vol24_usd_now=vol24, min_liq_usd=Cfg.OLD_MIN_LIQ_USD, score_ready=70.0)
            if not sig.ready:
                continue

            msg = (
                "🔎 Old Gem: READY TO PUMP\n"
                f"{token} ({ca})\n"
                f"Score: {sig.score:.1f}/100 ({sig.reason})\n"
                f"Liquidity: ${float(liq):,.0f} | 24h Vol: ${vol24:,.0f}\n"
                f"Projected short-horizon return: ~{sig.expected_return_pct:.0f}%"
            )
            try:
                await bot.updater.bot.send_message(chat_id=Cfg.ADMIN_CHAT_ID, text=msg)
                alerts += 1
            except Exception as e:
                logger.error(f"[OldScan] telegram error: {e}")

    return alerts

async def run_oldscanner(bot):
    if not Cfg.SCAN_OLD_ENABLED:
        logger.info("[OldScan] disabled"); return
    interval = max(5, Cfg.SCAN_OLD_INTERVAL_MIN) * 60
    logger.info(f"[OldScan] every {Cfg.SCAN_OLD_INTERVAL_MIN}m | liq≥${Cfg.OLD_MIN_LIQ_USD:,.0f}, vol24≥${Cfg.OLD_MIN_VOL24H_USD:,.0f}, age≥{Cfg.OLD_MIN_AGE_MIN}m")
    while True:
        try:
            count = await _scan_once(bot)
            logger.info(f"[OldScan] alerts: {count}")
        except Exception as e:
            logger.error(f"[OldScan] error: {e}")
        await asyncio.sleep(interval)
