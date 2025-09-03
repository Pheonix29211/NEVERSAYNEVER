# src/services/pumpportal.py
from __future__ import annotations
import os, asyncio, json, time
from typing import AsyncGenerator, Dict, Any
import aiohttp

from ..log import logger

PUMP_WS = os.environ.get("PUMP_WS", "wss://pumpportal.fun/api/data").strip()

async def stream_new_tokens() -> AsyncGenerator[Dict[str, Any], None]:
    """Yields: {"token": <symbol>, "ca": <mint>, "ts": <ms>, "liquidity_usd": 0.0}"""
    if not PUMP_WS:
        async def _empty():
            if False:  # never executes
                yield {}
        async for _ in _empty():
            pass
        return

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                logger.info(f"[PumpWS] connecting: {PUMP_WS}")
                async with session.ws_connect(PUMP_WS, heartbeat=20, max_msg_size=2**22) as ws:
                    await ws.send_json({"method": "subscribeNewToken"})
                    logger.info("[PumpWS] connected.")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                continue
                            token = None; ca = None
                            if isinstance(data, dict):
                                if "token" in data and isinstance(data["token"], dict):
                                    t = data["token"]
                                    token = t.get("symbol") or t.get("ticker") or t.get("name")
                                    ca    = t.get("mint") or t.get("address") or t.get("ca")
                                else:
                                    token = data.get("symbol") or data.get("ticker") or data.get("name")
                                    ca    = data.get("mint") or data.get("address") or data.get("ca")
                            if not ca:
                                continue
                            yield {
                                "token": token or "?", 
                                "ca": ca, 
                                "ts": int(time.time()*1000),
                                "liquidity_usd": 0.0
                            }
                        elif msg.type == aiohttp.WSMsgType.PING:
                            await ws.pong()
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
        except Exception as e:
            logger.warning(f"[PumpWS] error: {e}; reconnecting in 3s")
            await asyncio.sleep(3)
