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


# ---------- Key loading helpers ----------

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


def _env_secret_bytes() -> Optional[bytes]:
    """Try to read key material from SOLANA_SECRET_KEY env (JSON array or base58)."""
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


def _key_file_candidates() -> list[str]:
    """Candidate key file paths (first existing wins)."""
    out = []
    if Cfg.SOLANA_KEY_PATH:
        out.append(Cfg.SOLANA_KEY_PATH)
    # common fallbacks
    out.append(os.path.join(Cfg.DATA_DIR, "my_wallet_key.json"))
    out.append(os.path.join(Cfg.DATA_DIR, "phantom_key.json"))
    return out


def _file_secret_bytes() -> Optional[bytes]:
    """Try to read key material from file (JSON array or base58)."""
    for path in _key_file_candidates():
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            b = _decode_json_array(content)
            if b:
                return b
            b = _decode_base58(content)
            if b:
                return b
            logger.warning(f"[Exec] key file {path} not valid JSON array or base58")
        except Exception as e:
            logger.warning(f"[Exec] key file read error {path}: {e}")
    return None


def _secret_bytes() -> Optional[bytes]:
    """Unified loader: env first, then file."""
    b = _env_secret_bytes()
    if b:
        return b
    return _file_secret_bytes()


def _load_keypairs():
    """
    Return (solders_kp, solana_kp) — whichever can be constructed from the secret bytes.
    - 64-byte secret is preferred.
    - 32-byte seed only works if library exposes 'from_seed' (often not present).
    """
    raw = _secret_bytes()
    if not raw:
        logger.warning("[Exec] no key material found (env/file)")
        return None, None

    # solders
    kp_solders = None
    try:
        from solders.keypair import Keypair as SKeypair  # type: ignore
        if len(raw) == 64:
            kp_solders = SKeypair.from_bytes(raw)
        elif len(raw) == 32 and hasattr(SKeypair, "from_seed"):
            kp_solders = SKeypair.from_seed(raw)  # type: ignore
    except Exception as e:
        logger.warning(f"[Exec] solders keypair load failed: {e}")

    # solana-py
    kp_solana = None
    try:
        from solana.keypair import Keypair as PyKeypair  # type: ignore
        if len(raw) == 64:
            kp_solana = PyKeypair.from_secret_key(raw)
        # (solana-py usually doesn't support from seed directly)
    except Exception as e:
        logger.warning(f"[Exec] solana-py keypair load failed: {e}")

    return kp_solders, kp_solana


async def _rpc_get_sol_balance(pubkey_str: str) -> Optional[float]:
    try:
        from solana.rpc.async_api import AsyncClient  # lazy
        from solders.pubkey import Pubkey            # type: ignore
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

# ---------- Execution Engine ----------

class ExecutionEngine:
    def __init__(self, base_quote_url: str, send_msg):
        self.base = base_quote_url.rstrip("/")
        self.send_msg = send_msg

    async def preflight(self) -> Dict[str, Any]:
        # Presence check (env/file)
        if not (Cfg.SOLANA_SECRET_KEY or Cfg.SOLANA_KEY_PATH):
            if not os.path.exists(os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")) and \
               not os.path.exists(os.path.join(Cfg.DATA_DIR, "phantom_key.json")):
                return {"ok": False, "reason": "no_wallet"}

        kp_solders, _kp_solana_unused = _load_keypairs()
        if not kp_solders:
            return {"ok": False, "reason": "wallet_decode"}

        try:
            from solana.rpc.async_api import AsyncClient  # just to ensure package exists
        except Exception:
            return {"ok": False, "reason": "solana_lib_missing"}

        # check balance using solders pubkey
        try:
            pubkey_str = str(kp_solders.pubkey())
        except Exception:
            return {"ok": False, "reason": "wallet_decode"}

        sol = await _rpc_get_sol_balance(pubkey_str)
        if sol is None:
            return {"ok": False, "reason": "rpc_unavailable"}
        if sol < Cfg.LIVE_MIN_SOL_BUFFER:
            return {"ok": False, "reason": f"low_sol ({sol:.4f})"}

        # Probe quote endpoint quickly
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

    async def _build_swap_tx(
        self,
        session: aiohttp.ClientSession,
        route_info: Dict[str, Any],
        user_pubkey: str
    ) -> Optional[bytes]:
        """
        Ask Jupiter to build serialized (base64) swap tx from a quote.
        """
        url = f"{self.base}/swap"
        payload = {
            "quoteResponse": route_info,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True
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
        Live BUY execution path. In DRY_RUN, only simulates.
        """
        if Cfg.DRY_RUN:
            return {"ok": True, "simulated": True, "txsig": None}

        # Load solders keypair (we only need one working path)
        kp_solders, _kp_solana_unused = _load_keypairs()
        if not kp_solders:
            return {"ok": False, "reason": "wallet_decode"}

        try:
            user_pubkey = str(kp_solders.pubkey())
        except Exception:
            return {"ok": False, "reason": "wallet_decode"}

        # Build raw unsigned tx from Jupiter
        async with aiohttp.ClientSession() as session:
            raw = await self._build_swap_tx(session, route_info, user_pubkey)
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        # --- Sign with solders (no .sign() on VersionedTransaction in 0.26) ---
        try:
            from solders.transaction import VersionedTransaction as SVT
            from solders.signature import Signature

            # Parse the unsigned tx to get its message
            tx_unsigned = SVT.from_bytes(raw)
            msg_obj = tx_unsigned.message  # VersionedMessage

            # Get bytes of the message (try serialize / to_bytes / bytes())
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

            # Produce signature and build a NEW signed VersionedTransaction
            sig = kp_solders.sign_message(msg_bytes)  # -> solders.signature.Signature
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
                sig = str(resp.value)
                await self.send_msg(f"🟢 Executed BUY (live) — tx: {sig}")
                return {"ok": True, "txsig": sig}
        except Exception as e:
            return {"ok": False, "reason": f"tx_send_{e}"}
