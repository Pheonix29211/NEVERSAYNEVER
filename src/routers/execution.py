from __future__ import annotations
import os
import json
import base64
from typing import Optional, Dict, Any

import aiohttp

from ..config import Cfg
from ..log import logger

# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Resolve key file path priority:
# 1) SOLANA_KEY_PATH
# 2) /data/my_wallet_key.json
# 3) /data/phantom_key.json
KEY_FILE_PATH = (
    Cfg.SOLANA_KEY_PATH
    or os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")
    or os.path.join(Cfg.DATA_DIR, "phantom_key.json")
)


# -------------------------------------------------------------------
# Key loading helpers
# -------------------------------------------------------------------
def _decode_json_array(s: str) -> Optional[bytes]:
    """Parse a JSON array like [1,2,...] into bytes (expects len 32 or 64)."""
    try:
        arr = json.loads(s)
        if isinstance(arr, list) and all(isinstance(x, int) for x in arr) and len(arr) in (32, 64):
            return bytes(arr)
        return None
    except Exception:
        return None


def _decode_base58(s: str) -> Optional[bytes]:
    """Decode base58 into bytes. Expect 64 (preferred) or 32 bytes."""
    try:
        import base58  # pip install base58
    except Exception:
        logger.warning("[Exec] base58 module missing; pip install base58 to use base58 keys")
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


def _load_secret_from_env() -> Optional[bytes]:
    s = (Cfg.SOLANA_SECRET_KEY or "").strip()
    if not s:
        return None
    b = _decode_json_array(s)
    if b:
        return b
    b = _decode_base58(s)
    if b:
        return b
    logger.warning("[Exec] env SOLANA_SECRET_KEY present but not valid JSON array or base58")
    return None


def _load_secret_from_file() -> Optional[bytes]:
    path = KEY_FILE_PATH
    if not path:
        return None
    try:
        if not os.path.exists(path):
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
    """Unified secret loader: ENV first, then file."""
    return _load_secret_from_env() or _load_secret_from_file()


def _load_keypair():
    """
    Load solders.Keypair from 64-byte secret (preferred) or 32-byte seed (if supported).
    """
    try:
        from solders.keypair import Keypair  # lazy import
    except Exception as e:
        logger.warning(f"[Exec] solders not available: {e}")
        return None

    raw = _secret_bytes()
    if not raw:
        logger.warning("[Exec] no key material found (env/file)")
        return None

    if len(raw) == 64:
        try:
            return Keypair.from_bytes(raw)
        except Exception as e:
            logger.warning(f"[Exec] Keypair.from_bytes(64) failed: {e}")

    if len(raw) == 32:
        try:
            from_seed = getattr(Keypair, "from_seed", None)
            if callable(from_seed):
                return from_seed(raw)
            logger.warning("[Exec] 32-byte seed provided but Keypair.from_seed not available in this build")
        except Exception as e:
            logger.warning(f"[Exec] Keypair.from_seed failed: {e}")

    logger.warning(f"[Exec] unsupported secret length: {len(raw)} (need 64, or 32 with from_seed support)")
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


# -------------------------------------------------------------------
# Execution Engine
# -------------------------------------------------------------------
class ExecutionEngine:
    def __init__(self, base_quote_url: str, send_msg):
        self.base = base_quote_url.rstrip("/")
        self.send_msg = send_msg

    async def preflight(self) -> Dict[str, Any]:
        # Has any key source?
        if not (Cfg.SOLANA_SECRET_KEY or Cfg.SOLANA_KEY_PATH or os.path.exists(os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")) or os.path.exists(os.path.join(Cfg.DATA_DIR, "phantom_key.json"))):
            return {"ok": False, "reason": "no_wallet"}

        kp = _load_keypair()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        try:
            from solana.rpc.async_api import AsyncClient  # noqa
        except Exception:
            return {"ok": False, "reason": "solana_lib_missing"}

        sol = await _rpc_get_sol_balance(str(kp.pubkey()))
        if sol is None:
            return {"ok": False, "reason": "rpc_unavailable"}
        if sol < Cfg.LIVE_MIN_SOL_BUFFER:
            return {"ok": False, "reason": f"low_sol ({sol:.4f})"}

        # Probe quote endpoint
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

    async def _build_swap_tx(self, session: aiohttp.ClientSession, route_info: Dict[str, Any], user_pubkey: str) -> Optional[bytes]:
        """
        Ask Jupiter to build base64-serialized VersionedTransaction from a quote.
        """
        url = f"{self.base}/swap"
        payload = {"quoteResponse": route_info, "userPublicKey": user_pubkey, "wrapAndUnwrapSol": True}
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
        Live BUY execution path. In DRY_RUN, only simulates.
        """
        if Cfg.DRY_RUN:
            return {"ok": True, "simulated": True, "txsig": None}

        kp = _load_keypair()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        # Build swap transaction
        async with aiohttp.ClientSession() as session:
            raw = await self._build_swap_tx(session, route_info, str(kp.pubkey()))
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        # --- Sign with solders (since solana.transaction is not available) ---
        try:
            from solders.transaction import VersionedTransaction as SVT
            # Parse unsigned tx -> get message bytes
            tx_unsigned = SVT.from_bytes(raw)
            msg_obj = tx_unsigned.message

            # Get bytes of the message
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
                    return {"ok": False, "reason": "solders_msg_serialize_failed"}

            # Sign message with our Keypair
            sig = kp.sign_message(msg_bytes)  # solders.signature.Signature
            # Build a NEW signed transaction: SVT(message, [Signature])
            tx_signed = SVT(msg_obj, [sig])
            raw_signed = bytes(tx_signed)
        except Exception as e:
            return {"ok": False, "reason": f"solders_sign_fail: {e}"}

        # --- Send via solana AsyncClient ---
        try:
            from solana.rpc.async_api import AsyncClient
            from solana.rpc.types import TxOpts
            async with AsyncClient(Cfg.RPC_URL) as rpc:
                resp = await rpc.send_raw_transaction(raw_signed, opts=TxOpts(skip_preflight=True))
                sigstr = str(resp.value)
                await self.send_msg(f"🟢 Executed BUY (live) — tx: {sigstr}")
                return {"ok": True, "txsig": sigstr}
        except Exception as e:
            return {"ok": False, "reason": f"tx_send_{e}"}
