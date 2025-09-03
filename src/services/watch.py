from __future__ import annotations
import time, asyncio, aiohttp
from typing import Dict, Any
from ..config import Cfg
from ..log import logger
from .marketdata import ds_token_overview
from ..routers.photon import PhotonRouter

class WatchManager:
    def __init__(self, bot, router, ledger):
        self.bot = bot
        self.router = router
        self.ledger = ledger
        self.watch: Dict[str, Dict[str, Any]] = {}  # mint -> state

    def add_candidate(self, meta: Dict[str,Any]):
        base = meta.get("baseToken", {}) if isinstance(meta.get("baseToken"), dict) else {}
        mint = (base.get("address") or base.get("id") or "").strip()
        if not mint:
            return
        if mint in self.watch:
            return
        if len(self.watch) >= Cfg.WATCHLIST_MAX:
            return
        name = base.get("name") or base.get("symbol") or "token"
        price = meta.get("price") or 0.0
        self.watch[mint] = {
            "name": name, "symbol": base.get("symbol") or "", "mint": mint,
            "added_ts": time.time(), "base_price": price or 0.0,
            "last_price": price or 0.0,
            "vol_ema": 0.0, "peak": price or 0.0, "trail": (price or 0.0)*(1.0-Cfg.HARD_STOP_PCT),
            "last_quote_ts": 0.0, "route_fail_streak": 0
        }
        logger.info(f"[Watch] add {name} {mint[:6]}… base_price={price}")

    async def _maybe_chase_entry(self, session: aiohttp.ClientSession, mint: str, st: Dict[str,Any]):
        # Chase rule: if free slot and price moved up WATCH_ENTRY_ACCEL_PCT% since added
        if not Cfg.DRY_RUN or not Cfg.PAPER_AUTOTRADE:
            return
        if not self.ledger.free_capacity_ok(Cfg.PER_TRADE_USD_TARGET):
            return
        base = st.get("base_price") or 0.0
        nowp = st.get("last_price") or 0.0
        if base <= 0 or nowp <= 0:
            return
        move = (nowp/base - 1.0) * 100.0
        if move < Cfg.WATCH_ENTRY_ACCEL_PCT:
            return

        # quick route check
        q = await self.router.quote_buy(session, mint, Cfg.PER_TRADE_USD_TARGET)
        if not q.get("ok"):
            return
        if q.get("fee_pct",0) > Cfg.FEE_CAP_PCT or q.get("slip_pct",0) > Cfg.MAX_SLIPPAGE_PCT:
            return

        # open paper position instantly
        name = st["name"]; sym = st.get("symbol","")
        price = q.get("price", nowp)
        meta = await ds_token_overview(session, mint) or {}
        mc = meta.get("mc") or 0.0; lp = meta.get("lp_usd") or 0.0
        self.ledger.open_paper(name, sym, mint, Cfg.PER_TRADE_USD_TARGET, price, q.get("fee_pct",0.0), q.get("slip_pct",0.0), self.router.name, mc, lp, "watch_chase")
        await self.bot.safe_send(
            f"🟢 BUY (paper chase) — {name} | entry ${Cfg.PER_TRADE_USD_TARGET:.2f} at ~{price:.10f} | "
            f"fee {q.get('fee_pct',0):.2f}% slip {q.get('slip_pct',0):.2f}% | Δ since add {move:+.1f}%"
        )
        # Once entered, we can keep it in watch for trailing exits via portfolio monitor.

    async def _route_health(self, session: aiohttp.ClientSession, mint: str, st: Dict[str,Any]) -> bool:
        # Periodically check routing health; two consecutive fails => treat as rug/fishy
        now = time.time()
        if (now - st["last_quote_ts"]) < Cfg.WATCH_QUOTE_INTERVAL_S:
            return True
        st["last_quote_ts"] = now
        q = await self.router.quote_buy(session, mint, max(Cfg.PER_TRADE_USD_MIN, 8.0))
        if not q.get("ok"):
            st["route_fail_streak"] += 1
        else:
            st["route_fail_streak"] = 0
        return st["route_fail_streak"] < Cfg.WATCH_ROUTE_FAILS_EXIT

    async def _tick_position(self, session: aiohttp.ClientSession, mint: str, pos: Dict[str,Any], st: Dict[str,Any]):
        # Refresh latest price & mark PnL
        meta = await ds_token_overview(session, mint) or {}
        price = meta.get("price") or st.get("last_price") or pos["entry_price"]
        st["last_price"] = price
        self.ledger.mark(mint, price)

        # Update vol estimate (EMA of absolute returns)
        prev = st.get("prev_price") or price
        ret = abs(price - prev)
        alpha = 2.0/(Cfg.ATR_WINDOW+1.0)
        st["vol_ema"] = st.get("vol_ema", 0.0)*(1.0-alpha) + ret*alpha
        st["prev_price"] = price

        # Peak & trail
        st["peak"]  = max(st.get("peak", pos["entry_price"]), price)
        dyn = Cfg.TRAIL_K * st["vol_ema"]
        candidate = st["peak"] - dyn
        if candidate > st.get("trail", pos["entry_price"]*(1.0-Cfg.HARD_STOP_PCT)):
            st["trail"] = candidate

        # Gap/cliff rug checks
        if Cfg.RUG_ENABLED:
            if price <= st["trail"] * (1.0 - Cfg.GAP_PCT):
                # gap below trail -> exit at current price
                exit_usd = pos["entry_usd"] * (price/pos["entry_price"])
                self.ledger.close_paper(mint, exit_usd, "gap")
                await self.bot.safe_send(f"🛡️ Gap exit — {pos['token']} {mint[:6]}… at ~{price:.10f} | net {((price/pos['entry_price']-1.0)*100):+.1f}%")
                return
            # single-tick cliff
            prevp = prev if prev>0 else price
            if prevp>0 and ((prevp-price)/prevp) >= Cfg.RUG_DROP_PCT:
                exit_usd = pos["entry_usd"] * (price/pos["entry_price"])
                self.ledger.close_paper(mint, exit_usd, "rug")
                await self.bot.safe_send(f"🛡️ Rug guard — {pos['token']} cut at ~{price:.10f} | net {((price/pos['entry_price']-1.0)*100):+.1f}%")
                return

        # Trail hit?
        if price <= st["trail"]:
            exit_usd = pos["entry_usd"] * (st["trail"]/pos["entry_price"])
            self.ledger.close_paper(mint, exit_usd, "trail")
            await self.bot.safe_send(f"🔻 Trailed out — {pos['token']} at ~{st['trail']:.10f} | net {((st['trail']/pos['entry_price']-1.0)*100):+.1f}%")
            return

        # Route health (rare, every WATCH_QUOTE_INTERVAL_S)
        ok = await self._route_health(session, mint, st)
        if not ok:
            # treat as fishy/routeless -> exit on best mark
            exit_usd = pos["entry_usd"] * (price/pos["entry_price"])
            self.ledger.close_paper(mint, exit_usd, "route_fail")
            await self.bot.safe_send(f"🛡️ Route fail exit — {pos['token']} ({st['route_fail_streak']}x) | net {((price/pos['entry_price']-1.0)*100):+.1f}%")

    async def run(self):
        # One session reused for efficiency
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    # Tick open positions first (safety)
                    for mint, pos in list(self.ledger.positions.items()):
                        st = self.watch.setdefault(mint, {
                            "name": pos["token"], "symbol": pos.get("symbol",""), "mint": mint,
                            "added_ts": time.time(), "base_price": pos["entry_price"],
                            "last_price": pos["entry_price"], "prev_price": pos["entry_price"],
                            "vol_ema": 0.0, "peak": pos["entry_price"], "trail": pos["entry_price"]*(1.0-Cfg.HARD_STOP_PCT),
                            "last_quote_ts": 0.0, "route_fail_streak": 0
                        })
                        await self._tick_position(session, mint, pos, st)

                    # Tick watchlist (chase entries if accelerate)
                    for mint, st in list(self.watch.items()):
                        if mint in self.ledger.positions:
                            continue  # already open, handled above
                        meta = await ds_token_overview(session, mint) or {}
                        price = meta.get("price") or st.get("last_price") or st.get("base_price") or 0.0
                        st["last_price"] = price
                        await self._maybe_chase_entry(session, mint, st)

                except Exception as e:
                    logger.warning(f"[Watch] loop error: {e}")
                await asyncio.sleep(max(0.2, Cfg.WATCH_TICK_SEC))
