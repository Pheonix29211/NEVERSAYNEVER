# src/routers/execution.py
from __future__ import annotations

import os
import json
import base64
from typing import Optional, Dict, Any

import aiohttp

from ..config import Cfg
from ..log import logger

# ---- Constants ----
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Resolve a default key-file path (used if SOLANA_KEY_PATH not set)
def _default_key_path() -> str:
    # Prefer explicit env
    if Cfg.SOLANA_KEY_PATH:
        return Cfg.SOLANA_KEY_PATH
    # Try common filenames under DATA_DIR
    for name in ("my_wallet_key.json", "phantom_key.json"):
        p = os.path.join(Cfg.DATA_DIR, name)
        if os.path.exists(p):
            return p
    # Fallback to DATA_DIR/my_wallet_key.json (may or may not exist)
    return os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")

KEY_FILE_PATH = _default_key_path()


# ---- Key loading helpers (export _secret_bytes for /wallet) ----
def _decode_json_array(s: str) -> Optional[bytes]:
    """Parse a JSON array like [1,2,...] into bytes (expects 32 or 64 length)."""
    try:
        arr = json.loads(s)
        if isinstance(arr, list) and all(isinstance(x, int) for x in arr) and len(arr) in (32, 64):
            return bytes(arr)
        return None
    except Exception:
        return None


def _decode_base58(s: str) -> Optional[bytes]:
    """Decode base58 into bytes (expect 32 or 64)."""
    try:
        import base58  # pip install base58
    except Exception:
        logger.warning("[Exec] base58 module missing; install base58 to use base58 keys")
        return None
    try:
        raw = base58.b58decode(s.strip())
        if len(raw) in (32, 64):
            return raw
        logger.warning(f"[Exec] base58 decoded length {len(raw)} not in (32, 64)")
        return None
    except Exception as e:
        logger.warning(f"[Exec] base58 decode error: {e}")
        return None


def _load_secret_bytes_from_env() -> Optional[bytes]:
    secret = (Cfg.SOLANA_SECRET_KEY or "").strip()
    if not secret:
        return None
    b = _decode_json_array(secret)
    if b:
        return b
    b = _decode_base58(secret)
    if b:
        return b
    logger.warning("[Exec] env SOLANA_SECRET_KEY present but not valid JSON array or base58")
    return None


def _load_secret_bytes_from_file() -> Optional[bytes]:
    path = KEY_FILE_PATH
    try:
        if not path or not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        b = _decode_json_array(content)
        if b:
            return b
        b = _decode_base58(content)
        if b:
            return b
        logger.warning(f"[Exec] key file {path} not valid JSON array or base58")
        return None
    except Exception as e:
        logger.warning(f"[Exec] key file read error {path}: {e}")
        return None


def _secret_bytes() -> Optional[bytes]:
    """Unified loader: env first, then file (exported for /wallet)."""
    return _load_secret_bytes_from_env() or _load_secret_bytes_from_file()


async def _rpc_get_sol_balance(pubkey_str: str) -> Optional[float]:
    try:
        from solana.publickey import PublicKey
        from solana.rpc.async_api import AsyncClient
    except Exception as e:
        logger.warning(f"[Exec] solana client not available: {e}")
        return None
    try:
        pk = PublicKey(pubkey_str)
        async with AsyncClient(Cfg.RPC_URL) as rpc:
            bal = await rpc.get_balance(pk)
            return (bal.value or 0) / 1_000_000_000.0
    except Exception as e:
        logger.warning(f"[Exec] rpc balance error: {e}")
        return None


# ---- Execution Engine (solana-py only signing) ----
class ExecutionEngine:
    def __init__(self, base_quote_url: str, send_msg):
        self.base = base_quote_url.rstrip("/")
        self.send_msg = send_msg

    async def preflight(self) -> Dict[str, Any]:
        # Check key presence
        if not (_secret_bytes() or os.path.exists(KEY_FILE_PATH)):
            return {"ok": False, "reason": "no_wallet"}

        # Build a solana-py Keypair to fetch pubkey & check balance
        try:
            from solana.keypair import Keypair as PyKeypair
        except Exception as e:
            return {"ok": False, "reason": f"solana_lib_missing: {e}"}

        b = _secret_bytes()
        if not b:
            return {"ok": False, "reason": "wallet_decode"}

        try:
            if len(b) == 64:
                kp = PyKeypair.from_secret_key(b)
            elif len(b) == 32 and hasattr(PyKeypair, "from_seed"):
                kp = PyKeypair.from_seed(b)  # rarely used
            else:
                return {"ok": False, "reason": f"unsupported_secret_len_{len(b)}"}
        except Exception as e:
            return {"ok": False, "reason": f"wallet_decode: {e}"}

        pubkey_str = str(kp.public_key)
        sol = await _rpc_get_sol_balance(pubkey_str)
        if sol is None:
            return {"ok": False, "reason": "rpc_unavailable"}
        if sol < Cfg.LIVE_MIN_SOL_BUFFER:
            return {"ok": False, "reason": f"low_sol ({sol:.4f})"}

        # Probe Jupiter quote
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
        """
        Ask Jupiter to build serialized (base64) swap tx from a quote.
        """
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

    async def execute_buy(self, mint: str, usd_amount: float, route_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Live BUY execution. In DRY_RUN, only simulates.
        """
        if Cfg.DRY_RUN:
            return {"ok": True, "simulated": True, "txsig": None}

        # Load key once
        b = _secret_bytes()
        if not b:
            return {"ok": False, "reason": "wallet_decode"}

        try:
            from solana.keypair import Keypair as PyKeypair
            from solana.transaction import VersionedTransaction as PyVT
            from solana.rpc.async_api import AsyncClient
            from solana.rpc.types import TxOpts
        except Exception as e:
            return {"ok": False, "reason": f"solana_lib_missing: {e}"}

        # Build Keypair
        try:
            if len(b) == 64:
                kp = PyKeypair.from_secret_key(b)
            elif len(b) == 32 and hasattr(PyKeypair, "from_seed"):
                kp = PyKeypair.from_seed(b)
            else:
                return {"ok": False, "reason": f"unsupported_secret_len_{len(b)}"}
        except Exception as e:
            return {"ok": False, "reason": f"wallet_decode: {e}"}

        pubkey_str = str(kp.public_key)

        # Build swap tx from Jupiter
        async with aiohttp.ClientSession() as session:
            raw = await self._build_swap_tx(session, route_info, pubkey_str)
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        # Deserialize + sign with solana-py
        try:
            tx = PyVT.deserialize(raw) if hasattr(PyVT, "deserialize") else PyVT.from_bytes(raw)  # type: ignore
            tx.sign([kp])
            raw_signed = tx.serialize()
        except Exception as e:
            return {"ok": False, "reason": f"tx_sign_{e}"}

        # Submit
        try:
            async with AsyncClient(Cfg.RPC_URL) as rpc:
                resp = await rpc.send_raw_transaction(raw_signed, opts=TxOpts(skip_preflight=True))
                sig = str(resp.value)
                await self.send_msg(f"🟢 Executed BUY (live) — tx: {sig}")
                return {"ok": True, "txsig": sig}
        except Exception as e:
            return {"ok": False, "reason": f"tx_send_{e}"}
