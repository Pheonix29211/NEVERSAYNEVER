from dataclasses import dataclass

@dataclass
class SentinelConfig:
    sensitivity: str = "DEFAULT"
    time_quorum_sec: int = 90
    max_exit_slippage_pct: float = 10.0
