from dataclasses import dataclass

@dataclass
class ProfitRules:
    tp1: float = 0.50
    tp2: float = 1.00
    tp3: float = 2.00
    trail_runner: float = -0.22
    trail_rocket: float = -0.28
    trail_after_lh: float = -0.24
    trail_exhaust: float = -0.18
    momentum_hold: bool = True
