from __future__ import annotations
import asyncio, os
from fastapi import FastAPI
import uvicorn
from .log import logger
from .config import Cfg
from .telegram.bot import TGBot
from .routers.photon import PhotonRouter
from .routers.jupiter import JupiterRouter
from .routers.execution import ExecutionEngine
from .services.radar import run_radar
from .services.watch import WatchManager
from .services.ledger import TradeLedger
from .services.scheduler import nightly_report

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "service": "memesniper", "mode": ("DRY_RUN" if Cfg.DRY_RUN else "LIVE")}

def _mk_router():
    if Cfg.ROUTER == "PHOTON":
        return PhotonRouter(Cfg.PHOTON_BASE, Cfg.FEE_CAP_PCT)
    return JupiterRouter(Cfg.JUP_BASE, Cfg.FEE_CAP_PCT)

async def boot():
    ledger = TradeLedger()
    bot = TGBot(ledger)
    bot.run_async()

    router = _mk_router()
    exec_engine = ExecutionEngine(Cfg.PHOTON_BASE, bot.safe_send)

    # High-frequency watch (1s tick)
    watchmgr = WatchManager(bot, router, ledger)
    asyncio.create_task(watchmgr.run())

    # Discovery radar (adds to watch + instant entries)
    asyncio.create_task(run_radar(bot, router, exec_engine, ledger, watchmgr))

    # Nightly portfolio summary (IST)
    asyncio.create_task(nightly_report(bot.safe_send, ledger))

@app.on_event("startup")
async def _startup():
    asyncio.create_task(boot())

if __name__ == "__main__":
    uvicorn.run("src.app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), reload=False)


