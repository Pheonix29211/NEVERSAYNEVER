# src/services/trending.py
from __future__ import annotations
import pandas as pd
import numpy as np

def _zscore(s: pd.Series, win: int) -> pd.Series:
    r = s.rolling(win, min_periods=max(3, win//3))
    m = r.mean()
    sd = r.std(ddof=0).replace(0, np.nan)
    return (s - m) / sd

def _pct_change(s: pd.Series, win: int) -> pd.Series:
    return s.pct_change(win).replace([np.inf, -np.inf], np.nan)

def compute_ets_series(df: pd.DataFrame) -> pd.Series:
    """
    Early Trend Score (0-100-ish): combines short-term momentum, volume surge,
    and intrabar strength, scaled to a readable range.
    Expects df with columns: ['ts','o','h','l','c','v'].
    """
    if df is None or len(df) < 10:
        return pd.Series([0.0]* (0 if df is None else len(df)))

    px = df["c"].astype(float)
    vol = df["v"].astype(float).clip(lower=0)

    # Momentum components
    mom_5  = _pct_change(px, 5) * 100.0           # ~5m return
    mom_15 = _pct_change(px, 15) * 100.0          # ~15m return
    mom_z  = (_zscore(px, 10) + _zscore(px, 30))  # price zscore blend

    # Volume acceleration (fast vs base)
    v_fast = vol.rolling(5,  min_periods=1).mean()
    v_base = vol.rolling(45, min_periods=1).mean().replace(0, np.nan)
    accel  = (v_fast / v_base).clip(0, 12)

    # Intrabar strength (close near high)
    rng = (df["h"] - df["l"]).replace(0, np.nan)
    pos = (df["c"] - df["l"]) / rng
    pos = pos.clip(0, 1)

    # Compose score (weights tuned to emphasize early thrust)
    # Normalize pieces to comparable ranges
    mom_term = (mom_5.fillna(0) * 0.6 + mom_15.fillna(0) * 0.4).clip(-50, 200) / 2.0
    z_term   = (mom_z.fillna(0).clip(-3, 6)) * 8.0
    acc_term = (accel.fillna(0).clip(0, 12)) * 6.0
    pos_term = (pos.fillna(0)) * 20.0

    ets = mom_term + z_term + acc_term + pos_term

    # Smooth a touch and clamp to [0, 100+] so thresholds like 70 work well.
    ets = ets.rolling(3, min_periods=1).mean()
    ets = ets.clip(lower=0)
    return ets.fillna(0.0)
