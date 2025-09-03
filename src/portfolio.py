from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict
from .config import Cfg

@dataclass
class Holding:
    token: str
    ca: str
    qty: float = 0.0
    avg: float = 0.0
    last: float = 0.0
    mode: str = "NORMAL"  # NORMAL/INSIDER/GIANT
    meta: dict = field(default_factory=dict)

@dataclass
class Portfolio:
    equity: float = Cfg.FLOOR_USD
    floor: float = Cfg.FLOOR_USD
    cash: float = 0.0
    holdings: Dict[str, Holding] = field(default_factory=dict)

    def active_stack(self) -> float:
        return max(0.0, self.equity - self.floor)

    def next_size(self, pool_liq_usd: float) -> float:
        # Size ≤ risk fraction of Active Stack and ≤ pool cap
        stack = self.active_stack()
        cap_pool = pool_liq_usd * Cfg.POOL_SIZE_PCT_CAP if Cfg.POOL_SIZE_PCT_CAP < 1 else pool_liq_usd * (Cfg.POOL_SIZE_PCT_CAP/100.0)
        return max(1.0, min(Cfg.RISK_FRACTION * stack, cap_pool))

    def apply_fill(self, ca: str, token: str, side: str, qty: float, px: float, mode: str):
        h = self.holdings.get(ca)
        if side == "BUY":
            if h is None:
                self.holdings[ca] = Holding(token=token, ca=ca, qty=qty, avg=px, mode=mode)
            else:
                new_qty = h.qty + qty
                h.avg = (h.avg*h.qty + px*qty)/max(1e-9, new_qty)
                h.qty = new_qty
        else:
            if h:
                h.qty -= qty
                if h.qty <= 1e-9:
                    del self.holdings[ca]

    def mark_to_market(self, prices: Dict[str, float]):
        total = 0.0
        for h in self.holdings.values():
            p = prices.get(h.ca, h.last or h.avg)
            h.last = p
            total += h.qty * p
        self.equity = self.floor + self.cash + total
