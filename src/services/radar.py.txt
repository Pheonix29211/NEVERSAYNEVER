# src/radar/radar.py
from __future__ import annotations

import os
import time
import asyncio
import aiohttp
from typing import Dict

from ..config import Cfg
from ..log import logger
from .marketdata import gt_trending_pools
from .signals import gate_existing_pair


# Optional seed mints to always include in the universe
SEED_MINTS = [
    m.strip() for m in os.environ.get("SEED_MINTS", "").split(",") if m.strip()
]


async def _universe(session: aiohttp.ClientSession) -> list[str]:
    """
    Build the scan universe from trending pools (DexScreener/your source),
    normalized to base mint, with optional SEED_MINTS appended.
    """
    mints: list[str] = []
    try:
        pools = await gt_trending_pools(session, limit=80)
        for p in pools:
            base = p.get("base_token_id")
            if not base:
                continue
            mint = base.split("_", 1)[-1] if "_" in base else base
            if mint:
                mints.append(mint)
    except Exception:
        # keep universe best-effort
        pass

    # Deduplicate while preserving order, then append seeds if missing
    seen = set()
    uniq = []
    for m in mints:
        if m and m not in seen:
            seen.add(m)
            uniq.append(m)
    for m in SEED_MINTS:
        if m not in seen:
            uniq.append(m)
            seen.add(m)

    return uniq[:120]


async def run_radar(bot, router, exec_engine, ledger, watchmgr=None):
    """
    Main discovery loop: build universe -> gate -> alert -> (paper buy/live buy)
    NOTE: signature matches app.boot call: run_radar(bot, router, exec_engine, ledger, watchmgr)
    """
    last_alert_at: Dict[str, float] = {}  # mint -> timestamp of last alert
    cooldown = max(60, Cfg.ALERT_COOLDOWN_MIN * 60)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                mints = await _universe(session)
                logger.info(f"[Radar] universe size: {len(mints)}")
                if not mints:
                    await asyncio.sleep(Cfg.SCAN_SLEEP_SEC)
                    continue

                now = time.time()
                seen_this_scan: set[str] = set()

                for mint in mints:
                    if mint in seen_this_scan:
                        continue
                    seen_this_scan.add(mint)

                    # cooldown per mint
                    last_ts = last_alert_at.get(mint, 0.0)
                    if (now - last_ts) < cooldown:
                        continue

                    # Gate check
                    ok, reason, meta = await gate_existing_pair(session, router, mint)

                    # Safe token label + metrics
                    base = meta.get("baseToken", {}) if isinstance(meta.get("baseToken"), dict) else {}
                    token = base.get("name") or base.get("symbol") or "token"
                    mc = float(meta.get("mc") or 0)
                    lp = float(meta.get("lp_usd") or 0)
                    lp_mc = (lp / mc) if mc else 0.0

                    if ok:
                        last_alert_at[mint] = now

                        msg = (
                            f"🔥 Candidate: {token} — MC ${mc:,.0f}, LP ${lp:,.0f} ({lp_mc:.1%}), "
                            f"route ok (≤{Cfg.FEE_CAP_PCT:.0f}% fee, ≤{Cfg.MAX_SLIPPAGE_PCT:.0f}% slip). "
                            f"{'Paper' if Cfg.DRY_RUN else 'Live'} mode."
                        )
                        await bot.safe_send(msg)

                        # PAPER AUTO-TRADE
                        if (
                            Cfg.DRY_RUN
                            and Cfg.PAPER_AUTOTRADE
                            and ledger.free_capacity_ok(meta.get("q_usd", Cfg.PER_TRADE_USD_TARGET))
                        ):
                            fee = float(meta.get("fee_pct", 0.0))
                            slip = float(meta.get("slip_pct", 0.0))
                            price = float(meta.get("price", 0.0))
                            reason_text = "gate_ok+route_ok"

                            ledger.open_paper(
                                token,
                                base.get("symbol") or "",
                                mint,
                                meta.get("q_usd", Cfg.PER_TRADE_USD_TARGET),
                                price,
                                fee,
                                slip,
                                router.name,
                                mc,
                                lp,
                                reason_text,
                            )

                            await bot.safe_send(
                                "🟢 BUY (paper) — "
                                f"{token} | entry ${meta.get('q_usd', Cfg.PER_TRADE_USD_TARGET):.2f} "
                                f"at ~{price:.10f} | fee {fee:.2f}% slip {slip:.2f}% | "
                                f"MC ${mc:,.0f} LP ${lp:,.0f}"
                            )

                        # LIVE BUY
                        if not Cfg.DRY_RUN and exec_engine and meta.get("route_info"):
                            res = await exec_engine.execute_buy(
                                mint,
                                meta.get("q_usd", Cfg.PER_TRADE_USD_MIN),
                                meta["route_info"],
                            )
                            if not res.get("ok"):
                                await bot.safe_send(f"⚠️ Exec buy failed: {res.get('reason')}")

                    else:
                        logger.info(f"[Radar] {mint[:6]}… skip: {reason}")

                # ⬅️ must be INSIDE the while/try loop
                await asyncio.sleep(Cfg.SCAN_SLEEP_SEC)

            except Exception as e:
                logger.warning(f"[Radar] loop error: {e}")
                await asyncio.sleep(10)
