from __future__ import annotations
import os, time, csv, math
from typing import Dict, Any, List, Optional
from ..config import Cfg
from ..log import logger

DATA_DIR = Cfg.DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)

class TradeLedger:
    def __init__(self):
        self.balance = Cfg.PAPER_START_BAL_USD if Cfg.PAPER_MODE_BAL_ENABLED else 0.0
        self.positions: Dict[str, Dict[str, Any]] = {}  # mint -> pos
        self.history_csv = os.path.join(DATA_DIR, "paper_trades.csv")
        if not os.path.exists(self.history_csv):
            with open(self.history_csv, "w", newline="") as f:
                csv.writer(f).writerow([
                    "ts","side","mode","name","symbol","mint","entry_usd","entry_price",
                    "fee_pct","slip_pct","router","mc","lp","lp_mc","reason","exit_usd","exit_reason","pnl_pct"
                ])

    def free_capacity_ok(self, next_entry_usd: float) -> bool:
        open_count = len(self.positions)
        if open_count >= Cfg.MAX_OPEN_POSITIONS:
            return False
        exposure_now = sum(p["entry_usd"] for p in self.positions.values()) / max(1e-9, self.balance if self.balance>0 else Cfg.PAPER_START_BAL_USD)
        if exposure_now + (next_entry_usd/max(1e-9, self.balance if self.balance>0 else Cfg.PAPER_START_BAL_USD)) > Cfg.TOTAL_EXPOSURE_CAP_PCT:
            return False
        if next_entry_usd < Cfg.PER_TRADE_USD_MIN:
            return False
        return True

    def open_paper(self, token: str, symbol: str, mint: str, entry_usd: float, price: float,
                   fee_pct: float, slip_pct: float, router: str, mc: float, lp: float, reason: str):
        ts = int(time.time())
        lp_mc = (lp/mc) if mc else 0.0
        self.positions[mint] = {
            "token": token, "symbol": symbol, "mint": mint, "entry_ts": ts,
            "entry_usd": entry_usd, "entry_price": price, "fee_pct": fee_pct, "slip_pct": slip_pct,
            "router": router, "mc": mc, "lp": lp, "lp_mc": lp_mc, "reason": reason
        }
        logger.info(f"[Paper] OPEN {token} {mint[:6]}… ${entry_usd:.2f} @ ~{price:.10f}")

        with open(self.history_csv, "a", newline="") as f:
            csv.writer(f).writerow([ts,"BUY","paper",token,symbol,mint,entry_usd,price,fee_pct,slip_pct,router,mc,lp,lp_mc,reason,"","", ""])

    def close_paper(self, mint: str, exit_usd: float, exit_reason: str):
        pos = self.positions.pop(mint, None)
        if not pos:
            return
        ts = int(time.time())
        pnl_pct = (exit_usd/pos["entry_usd"] - 1.0)*100.0
        self.balance += (exit_usd - pos["entry_usd"])

        with open(self.history_csv, "a", newline="") as f:
            csv.writer(f).writerow([ts,"SELL","paper",pos["token"],pos["symbol"],pos["mint"],pos["entry_usd"],pos["entry_price"],
                                    pos["fee_pct"],pos["slip_pct"],pos["router"],pos["mc"],pos["lp"],pos["lp_mc"],pos["reason"],
                                    exit_usd,exit_reason,pnl_pct])

    def portfolio_text(self) -> str:
        lines = [f"📦 Paper Portfolio:\nBalance: ${self.balance:.2f} | Open: {len(self.positions)}"]
        if not self.positions:
            lines.append("No open positions.")
            return "\n".join(lines)
        for p in self.positions.values():
            lines.append(
                f"• {p['token']} ({p['mint'][:6]}…)"
                f" — entry ${p['entry_usd']:.2f} @ {p['entry_price']:.10f} | fee {p['fee_pct']:.2f}% slip {p['slip_pct']:.2f}%"
            )
        return "\n".join(lines)
