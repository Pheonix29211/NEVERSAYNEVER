from dataclasses import dataclass
from typing import Dict

@dataclass
class Scores:
    safe_score: float
    pgs: float
    ias: float
    cc_pct: float
    mds: float

def compute_safe_score(f: Dict) -> float:
    score = 0.0
    score += 30 if f.get("lp_locked", False) else 0
    score += 20 if f.get("rugcheck_ok", True) else -40
    score += 15 if f.get("sells_ok", True) else -100
    score += 10 if f.get("top10_ok", True) else 0
    score += 10 if f.get("slippage_ok", True) else 0
    score += 5  if f.get("mint_freeze_none", True) else -100
    score += 5  if f.get("no_token2022_taxes", True) else -40
    return max(0.0, min(100.0, score))

def compute_pgs(f: Dict) -> float:
    score = 0.0
    score += 30 if f.get("holders_growth_6h", 0) >= 200 else 0
    score += 20 if f.get("sustained_volume", False) else 0
    score += 15 if f.get("liquidity_usd", 0) >= 200000 else 0
    score += 15 if f.get("top10_pct", 100) <= 50 else 0
    score += 10 if f.get("unique_buy_ratio", 1.0) >= 1.3 else 0
    score += 10 if f.get("vol3h_usd", 0) >= 150000 else 0
    return max(0.0, min(100.0, score))

def compute_ias(f: Dict) -> float:
    score = 0.0
    score += 30 if f.get("smart_net_buys", 0) > 0 else 0
    score += 20 if f.get("multiwindow_buys", False) else 0
    score += 15 if f.get("lp_locked", False) and f.get("liquidity_usd",0) >= 20000 else 0
    score += 15 if f.get("cc_pct", 100) <= 25 and f.get("mds", 100) <= 60 else 0
    score += 10 if f.get("unique_buyers_up", False) else 0
    score += 10 if f.get("funding_diverse", False) else 0
    return max(0.0, min(100.0, score))
