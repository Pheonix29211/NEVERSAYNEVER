from dataclasses import dataclass
from typing import Dict

@dataclass
class Quote:
    price: float
    slippage_pct: float
    fees_pct: float
    route: str

class BaseRouter:
    name = "BASE"
    async def quote_buy(self, ca: str, amount_usd: float) -> Quote:
        raise NotImplementedError
    async def quote_sell(self, ca: str, qty: float, price_hint: float) -> Quote:
        raise NotImplementedError
    async def execute(self, tx: Dict) -> str:
        return "SIMULATED"
