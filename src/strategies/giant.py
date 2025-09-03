from dataclasses import dataclass

@dataclass
class GiantRules:
    floors = [1.0, 3.0, 6.0]  # +1x, +3x, +6x
    trails = [-0.35, -0.30, -0.28]
