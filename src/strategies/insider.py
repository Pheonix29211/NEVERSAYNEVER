from dataclasses import dataclass

@dataclass
class InsiderExitRules:
    trim1 = 0.30   # +30%
    trim2 = 0.70   # +70%
    trail_high_vol = -0.25
    trail_low_vol = -0.15
    hold_min_hours = 6
    hold_max_hours = 18
