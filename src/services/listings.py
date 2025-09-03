import asyncio, random
from typing import AsyncIterator, Dict
EXCHANGES_T1 = ["binance","okx","kucoin","coinbase"]
EXCHANGES_T2 = ["mexc","bitget","bybit","gate"]

# Replace with real RSS/API watchers.
async def stream_listings() -> AsyncIterator[Dict]:
    while True:
        await asyncio.sleep(random.uniform(60, 180))
        yield {
            "exchange": random.choice(EXCHANGES_T1+EXCHANGES_T2),
            "token": random.choice(["FROG","PAW","WIF2"]),
            "ca": "SIMCA",
            "tier": 1 if random.random()<0.3 else 2,
        }
