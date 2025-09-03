from __future__ import annotations
import json, aiohttp, base64
from typing import Optional, Dict, Any
from ..config import Cfg
from ..log import logger

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

def _load_keypair():
    try:
        from solders.keypair import Keypair  # lazy import
        arr = json.loads(Cfg.SOLANA_SECRET_KEY)
        return Keypair.from_bytes(bytes(arr))
    except Exception as e:
        logger.warning(f"[Exec] key load failed: {e}")
        return None

async def _rpc_get_sol_balance(pubkey_str: str) -> Optional[float]:
    try:
        from solana.rpc.async_api import AsyncClient  # lazy
        from solders.pubkey import Pubkey
    except Exception as e:
        logger.warning(f"[Exec] solana client not available: {e}")
        return None
    try:
        pk = Pubkey.from_string(pubkey_str)
        async with AsyncClient(Cfg.RPC_URL) as rpc:
            bal = await rpc.get_balance(pk)
            return bal.value / 1_000_000_000.0
    except Exception as e:
        logger.warning(f"[Exec] rpc balance error: {e}")
        return None

class ExecutionEngine:
    def __init__(self, base_quote_url: str, send_msg):
        self.base = base_quote_url.rstrip("/")
        self.send_msg = send_msg

    async def preflight(self) -> Dict[str, Any]:
        if not Cfg.has_live_key():
            return {"ok": False, "reason": "no_wallet"}
        kp = _load_keypair()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        try:
            from solana.rpc.async_api import AsyncClient  # noqa
            from solders.keypair import Keypair  # noqa
        except Exception:
            return {"ok": False, "reason": "solana_lib_missing"}

        sol = await _rpc_get_sol_balance(str(kp.pubkey()))
        if sol is None:
            return {"ok": False, "reason": "rpc_unavailable"}
        if sol < Cfg.LIVE_MIN_SOL_BUFFER:
            return {"ok": False, "reason": f"low_sol ({sol:.4f})"}

        async with aiohttp.ClientSession() as session:
            try:
                url = f"{self.base}/quote"
                params = {"inputMint": USDC_MINT, "outputMint": USDC_MINT, "amount": "1000", "slippageBps": "300"}
                async with session.get(url, params=params, timeout=8) as r:
                    if r.status != 200:
                        return {"ok": False, "reason": f"quote_http_{r.status}"}
            except Exception as e:
                return {"ok": False, "reason": f"quote_err_{e}"}
        return {"ok": True, "reason": "ok"}

    async def _build_swap_tx(self, session: aiohttp.ClientSession, route_info: Dict[str,Any], user_pubkey: str) -> Optional[bytes]:
        url = f"{self.base}/swap"
        payload = {"quoteResponse": route_info, "userPublicKey": user_pubkey, "wrapAndUnwrapSol": True}
        try:
            async with session.post(url, json=payload, timeout=12) as r:
                if r.status != 200:
                    txt = await r.text()
                    logger.warning(f"[Exec] swap_http_{r.status}: {txt[:160]}")
                    return None
                js = await r.json()
                b64 = js.get("swapTransaction") or js.get("serializedTransaction")
                if not b64:
                    return None
                return base64.b64decode(b64)
        except Exception as e:
            logger.warning(f"[Exec] swap_build_error: {e}")
            return None

    async def execute_buy(self, mint: str, usd_amount: float, route_info: Dict[str,Any]) -> Dict[str,Any]:
        if Cfg.DRY_RUN:
            return {"ok": True, "simulated": True, "txsig": None}
        kp = _load_keypair()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        try:
            from solders.transaction import VersionedTransaction
            from solana.rpc.async_api import AsyncClient
            from solana.rpc.types import TxOpts
        except Exception as e:
            return {"ok": False, "reason": f"solana_lib_missing: {e}"}

        async with aiohttp.ClientSession() as session:
            raw = await self._build_swap_tx(session, route_info, str(kp.pubkey()))
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        try:
            tx = VersionedTransaction.deserialize(raw)
            tx.sign([kp])
        except Exception as e:
            return {"ok": False, "reason": f"tx_sign_{e}"}

        try:
            async with AsyncClient(Cfg.RPC_URL) as rpc:
                resp = await rpc.send_raw_transaction(bytes(tx), opts=TxOpts(skip_preflight=True))
                sig = str(resp.value)
                await self.send_msg(f"🟢 Executed BUY (live) — tx: {sig}")
                return {"ok": True, "txsig": sig}
        except Exception as e:
            return {"ok": False, "reason": f"tx_send_{e}"}
