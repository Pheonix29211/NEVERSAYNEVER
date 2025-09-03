from __future__ import annotations
import aiohttp
from typing import Dict, Any

class PhotonRouter:
    name = "PHOTON"

    def __init__(self, base_url: str, fee_cap_pct: float):
        self.base = base_url.rstrip("/")
        self.fee_cap_pct = fee_cap_pct

    async def quote_buy(self, session: aiohttp.ClientSession, mint: str, usd_amount: float) -> Dict[str, Any]:
        USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        params = {
            "inputMint": USDC_MINT,
            "outputMint": mint,
            "amount": str(int(usd_amount * 1_000_000)),
            "slippageBps": "300",
            "onlyDirectRoutes": "false"
        }
        url = f"{self.base}/quote"
        try:
            async with session.get(url, params=params, timeout=10) as r:
                if r.status != 200:
                    return {"ok": False, "reason": f"quote_http_{r.status}"}
                data = await r.json()
        except Exception as e:
            return {"ok": False, "reason": f"quote_err_{e}"}

        route = data.get("data")[0] if isinstance(data.get("data"), list) and data["data"] else data
        if not route:
            return {"ok": False, "reason": "no_route"}

        slip_pct = float(route.get("priceImpactPct", 0)) * 100 if "priceImpactPct" in route else 0.0
        fee_pct  = float(route.get("platformFee", {}).get("amount", 0)) if isinstance(route.get("platformFee"), dict) else 0.0

        in_amount  = float(route.get("inAmount", 0)) / 1_000_000.0
        out_amount = float(route.get("outAmount", 0))
        price = (in_amount / out_amount) if out_amount else 0.0

        return {
            "ok": True,
            "fee_pct": fee_pct,
            "slip_pct": slip_pct,
            "price": price,
            "amount_out": out_amount,
            "route_info": route,
        }
