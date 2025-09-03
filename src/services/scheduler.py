from __future__ import annotations
import asyncio, datetime as dt, os
from ..config import Cfg
from ..log import logger

def _next_run_ist(now_utc: dt.datetime) -> dt.datetime:
    # parse HH:MM local (IST)
    hh, mm = [int(x) for x in Cfg.NIGHTLY_REPORT_LOCAL.split(":")]
    ist = now_utc + dt.timedelta(hours=5, minutes=30)
    target_ist = ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target_ist <= ist:
        target_ist += dt.timedelta(days=1)
    # convert back to UTC
    delta = dt.timedelta(hours=5, minutes=30)
    return target_ist - delta

async def nightly_report(bot_send, ledger):
    while True:
        try:
            now = dt.datetime.utcnow()
            nxt = _next_run_ist(now)
            wait = (nxt - now).total_seconds()
            logger.info(f"[Nightly] next report UTC {nxt} (in {int(wait)}s)")
            await asyncio.sleep(max(5, wait))
            # send
            await bot_send(ledger.portfolio_text().replace("📦 Paper Portfolio", "📦 Paper Portfolio (daily)"))
            # small delay before scheduling next
            await asyncio.sleep(5)
        except Exception as e:
            logger.warning(f"[Nightly] error: {e}")
            await asyncio.sleep(30)
