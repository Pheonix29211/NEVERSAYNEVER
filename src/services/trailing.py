from __future__ import annotations
from typing import List, Tuple
from ..config import Cfg

def ema(values: List[float], span: int) -> List[float]:
    if not values: return []
    k = 2.0/(span+1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k*(v - out[-1]))
    return out

def atr(high: List[float], low: List[float], close: List[float], window: int) -> List[float]:
    trs = []
    prev_close = close[0] if close else 0.0
    for h, l, c in zip(high, low, close):
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    return ema(trs, window)

def trailing_only_exits(prices: List[Tuple[float,float,float]]) -> Tuple[float, float, str]:
    """
    prices: list of (high, low, close)
    Returns: (net_pnl_pct, peak_runup_pct, exit_reason)
    """
    if not prices: return (0.0, 0.0, "no_data")
    H, L, C = zip(*prices)
    H, L, C = list(H), list(L), list(C)
    a = atr(H, L, C, Cfg.ATR_WINDOW)
    peak = C[0]
    trail = C[0]*(1.0 - Cfg.HARD_STOP_PCT)
    peak_run = 0.0

    for i in range(1, len(C)):
        peak = max(peak, H[i])
        peak_run = max(peak_run, (peak/C[0]-1.0)*100.0)
        at = a[i] if i < len(a) else a[-1] if a else 0.0
        dyn = Cfg.TRAIL_K * at
        candidate = peak - dyn
        if candidate > trail:
            trail = candidate

        # gap protection: if open (C[i] as proxy) dropped under trail by big gap
        if Cfg.GAP_PROTECT and C[i] < trail*(1.0 - Cfg.GAP_PCT):
            net = (C[i]/C[0]-1.0)*100.0
            return (net, peak_run, "gap")

        # trail hit inside bar: use low as exit proxy
        if L[i] <= trail <= H[i] or C[i] <= trail:
            # approximate exit at trail
            net = (trail/C[0]-1.0)*100.0
            return (net, peak_run, "trail")

        # rug sentinel (price cliff)
        if Cfg.RUG_ENABLED:
            drop = (C[i-1]-C[i])/max(1e-9, C[i-1])
            if drop >= Cfg.RUG_DROP_PCT:
                net = (C[i]/C[0]-1.0)*100.0
                return (net, peak_run, "rug")

    # if never exited, mark-to-market at last close
    net = (C[-1]/C[0]-1.0)*100.0
    return (net, peak_run, "hold")
