# src/routers/execution.py
from __future__ import annotations

import os
import json
import base64
from typing import Optional, Dict, Any

import aiohttp

from ..config import Cfg
from ..log import logger

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# ---------- key helpers ----------

KEY_FILE_PATH = (
    Cfg.SOLANA_KEY_PATH
    or os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")
)

def _decode_json_array(s: str) -> Optional[bytes]:
    try:
        arr = json.loads(s)
        if isinstance(arr, list) and all(isinstance(x, int) for x in arr) and len(arr) in (32, 64):
            return bytes(arr)
    except Exception:
        pass
    return None

def _decode_base58(s: str) -> Optional[bytes]:
    try:
        import base58  # type: ignore
        raw = base58.b58decode(s.strip())
        return raw if len(raw) in (32, 64) else None
    except Exception:
        return None

def _secret_bytes_from_env() -> Optional[bytes]:
    val = (Cfg.SOLANA_SECRET_KEY or "").strip()
    if not val:
        return None
    b = _decode_json_array(val)
    if b:
        return b
    b = _decode_base58(val)
    if b:
        return b
    return None

def _secret_bytes_from_file() -> Optional[bytes]:
    path = KEY_FILE_PATH
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read().strip()
        b = _decode_json_array(s)
        if b:
            return b
        b = _decode_base58(s)
        if b:
            return b
    except Exception as e:
        logger.warning(f"[Exec] key file read error {path}: {e}")
    return None

def _secret_bytes() -> Optional[bytes]:
    return _secret_bytes_from_env() or _secret_bytes_from_file()

def _load_solders_keypair():
    """Return solders.Keypair or None."""
    try:
        from solders.keypair import Keypair  # type: ignore
    except Exception as e:
        logger.warning(f"[Exec] solders not available: {e}")
        return None
    raw = _secret_bytes()
    if not raw:
        return None
    if len(raw) == 64:
        try:
            return Keypair.from_bytes(raw)
        except Exception as e:
            logger.warning(f"[Exec] Keypair.from_bytes failed: {e}")
            return None
    if len(raw) == 32:
        fn = getattr(Keypair, "from_seed", None)
        if callable(fn):
            try:
                return fn(raw)
            except Exception as e:
                logger.warning(f"[Exec] Keypair.from_seed failed: {e}")
                return None
        logger.warning("[Exec] 32-byte seed provided but Keypair.from_seed not available")
        return None
    logger.warning(f"[Exec] unsupported key length: {len(raw)}")
    return None

async def _rpc_get_sol_balance(pubkey_str: str) -> Optional[float]:
    try:
        from solana.rpc.async_api import AsyncClient  # type: ignore
        from solders.pubkey import Pubkey  # type: ignore
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

# ---------- Execution Engine ----------

class ExecutionEngine:
    def __init__(self, base_quote_url: str, send_msg):
        self.base = base_quote_url.rstrip("/")
        self.send_msg = send_msg

    async def preflight(self) -> Dict[str, Any]:
        # key presence
        if not (_secret_bytes()):
            return {"ok": False, "reason": "no_wallet"}

        kp = _load_solders_keypair()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        # solana client
        try:
            from solana.rpc.async_api import AsyncClient  # noqa: F401
        except Exception:
            return {"ok": False, "reason": "solana_lib_missing"}

        # balance
        sol = await _rpc_get_sol_balance(str(kp.pubkey()))
        if sol is None:
            return {"ok": False, "reason": "rpc_unavailable"}
        if sol < Cfg.LIVE_MIN_SOL_BUFFER:
            return {"ok": False, "reason": f"low_sol ({sol:.4f})"}

        # quote probe
        async with aiohttp.ClientSession() as session:
            try:
                url = f"{self.base}/quote"
                params = {
                    "inputMint": USDC_MINT,
                    "outputMint": USDC_MINT,
                    "amount": "1000",
                    "slippageBps": "300",
                }
                async with session.get(url, params=params, timeout=8) as r:
                    if r.status != 200:
                        return {"ok": False, "reason": f"quote_http_{r.status}"}
            except Exception as e:
                return {"ok": False, "reason": f"quote_err_{e}"}
        return {"ok": True, "reason": "ok"}

    async def _build_swap_tx(
        self,
        session: aiohttp.ClientSession,
        route_info: Dict[str, Any],
        user_pubkey: str,
    ) -> Optional[bytes]:
        url = f"{self.base}/swap"
        payload = {
            "quoteResponse": route_info,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
        }
        try:
            async with session.post(url, json=payload, timeout=15) as r:
                if r.status != 200:
                    txt = await r.text()
                    logger.warning(f"[Exec] swap_http_{r.status}: {txt[:200]}")
                    return None
                js = await r.json()
                b64 = js.get("swapTransaction") or js.get("serializedTransaction")
                if not b64:
                    return None
                return base64.b64decode(b64)
        except Exception as e:
            logger.warning(f"[Exec] swap_build_error: {e}")
            return None

    def _sign_with_solders(self, raw: bytes, kp) -> Optional[bytes]:
        """
        solders==0.26.0 has no VersionedTransaction.sign().
        Workaround: sign the message and construct a new VersionedTransaction(msg, [sig]).
        """
        try:
            from solders.transaction import VersionedTransaction as SVT  # type: ignore
        except Exception as e:
            logger.warning(f"[Exec] solders import fail: {e}")
            return None

        try:
            tx_unsigned = SVT.from_bytes(raw)
            msg_obj = tx_unsigned.message  # VersionedMessage

            # Try to get message bytes
            msg_bytes = None
            for attr in ("serialize", "to_bytes"):
                fn = getattr(msg_obj, attr, None)
                if callable(fn):
                    try:
                        msg_bytes = fn()
                        break
                    except Exception:
                        pass
            if msg_bytes is None:
                try:
                    msg_bytes = bytes(msg_obj)
                except Exception:
                    logger.warning("[Exec] solders: cannot serialize message")
                    return None

            # Sign message and build a NEW signed tx
            sig = kp.sign_message(msg_bytes)  # returns solders.signature.Signature
            tx_signed = SVT(msg_obj, [sig])
            return bytes(tx_signed)
        except Exception as e:
            logger.warning(f"[Exec] solders sign path failed: {e}")
            return None

    async def execute_buy(self, mint: str, usd_amount: float, route_info: Dict[str, Any]) -> Dict[str, Any]:
        if Cfg.DRY_RUN:
            return {"ok": True, "simulated": True, "txsig": None}

        kp = _load_solders_keypair()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        # Build tx
        async with aiohttp.ClientSession() as session:
            raw = await self._build_swap_tx(session, route_info, str(kp.pubkey()))
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        # Sign with solders workaround
        raw_signed = self._sign_with_solders(raw, kp)
        if raw_signed is None:
            return {"ok": False, "reason": "solders_sign_fail"}

        # Send via solana AsyncClient
        try:
            from solana.rpc.async_api import AsyncClient  # type: ignore
            from solana.rpc.types import TxOpts  # type: ignore
            async with AsyncClient(Cfg.RPC_URL) as rpc:
                resp = await rpc.send_raw_transaction(raw_signed, opts=TxOpts(skip_preflight=True))
                sig = str(resp.value)
                await self.send_msg(f"🟢 Executed BUY (live) — tx: {sig}")
                return {"ok": True, "txsig": sig}
        except Exception as e:
            return {"ok": False, "reason": f"tx_send_{e}"}
