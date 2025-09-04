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
KEY_FILE_PATH = (Cfg.SOLANA_KEY_PATH or os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")).strip()


# ---------- helpers: key loading ----------

def _decode_json_array(s: str) -> Optional[bytes]:
    """Parse a JSON array like [1,2,...] into bytes (expects length 32 or 64)."""
    try:
        arr = json.loads(s)
        if isinstance(arr, list) and all(isinstance(x, int) for x in arr) and len(arr) in (32, 64):
            return bytes(arr)
    except Exception:
        pass
    return None


def _decode_base58(s: str) -> Optional[bytes]:
    """Decode base58 into bytes (expects length 32 or 64)."""
    try:
        import base58  # pip install base58
        raw = base58.b58decode(s.strip())
        if len(raw) in (32, 64):
            return raw
        logger.warning(f"[Exec] base58 decoded length {len(raw)} not in (32,64)")
    except Exception as e:
        logger.warning(f"[Exec] base58 decode error: {e}")
    return None


def _load_secret_bytes_from_env() -> Optional[bytes]:
    secret = (Cfg.SOLANA_SECRET_KEY or "").strip()
    if not secret:
        return None
    # JSON array first
    b = _decode_json_array(secret)
    if b:
        return b
    # base58 fallback
    b = _decode_base58(secret)
    if b:
        return b
    logger.warning("[Exec] env SOLANA_SECRET_KEY present but not valid JSON array or base58")
    return None


def _load_secret_bytes_from_file() -> Optional[bytes]:
    path = KEY_FILE_PATH
    try:
        if not path or not os.path.exists(path):
            # default phantom_key.json fallback
            alt = os.path.join(Cfg.DATA_DIR, "phantom_key.json")
            if os.path.exists(alt):
                path = alt
            else:
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
    except Exception as e:
        logger.warning(f"[Exec] key file read error {path}: {e}")
    return None


def _secret_bytes() -> Optional[bytes]:
    """Unified loader: env first, then file (/data/my_wallet_key.json or phantom_key.json)."""
    return _load_secret_bytes_from_env() or _load_secret_bytes_from_file()


def _load_keypair_solders():
    """Return solders.Keypair from secret bytes (64 preferred, 32 if from_seed exists)."""
    try:
        from solders.keypair import Keypair
    except Exception as e:
        logger.warning(f"[Exec] solders not available: {e}")
        return None

    raw = _secret_bytes()
    if not raw:
        logger.warning("[Exec] no key material found (env/file)")
        return None

    # 64-byte secret (private+public)
    if len(raw) == 64:
        try:
            return Keypair.from_bytes(raw)
        except Exception as e:
            logger.warning(f"[Exec] Keypair.from_bytes(64) failed: {e}")

    # 32-byte seed (if from_seed is available)
    if len(raw) == 32:
        try:
            if hasattr(Keypair, "from_seed"):
                return Keypair.from_seed(raw)  # type: ignore[attr-defined]
            logger.warning("[Exec] 32-byte seed provided but Keypair.from_seed not available")
        except Exception as e:
            logger.warning(f"[Exec] Keypair.from_seed failed: {e}")

    logger.warning(f"[Exec] unsupported secret length: {len(raw)} (need 64, or 32 with from_seed support)")
    return None


async def _rpc_get_sol_balance(pubkey_str: str) -> Optional[float]:
    try:
        from solana.rpc.async_api import AsyncClient  # solana-py
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


# ---------- Execution Engine ----------

class ExecutionEngine:
    def __init__(self, base_quote_url: str, send_msg):
        self.base = base_quote_url.rstrip("/")
        self.send_msg = send_msg

    async def preflight(self) -> Dict[str, Any]:
        # Presence check: env or file
        if not (Cfg.SOLANA_SECRET_KEY or Cfg.SOLANA_KEY_PATH):
            if not (os.path.exists(os.path.join(Cfg.DATA_DIR, "my_wallet_key.json"))
                    or os.path.exists(os.path.join(Cfg.DATA_DIR, "phantom_key.json"))):
                return {"ok": False, "reason": "no_wallet"}

        kp = _load_keypair_solders()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        # solana client present?
        try:
            from solana.rpc.async_api import AsyncClient  # noqa: F401
        except Exception:
            return {"ok": False, "reason": "solana_lib_missing"}

        # balance check
        try:
            pubkey_str = str(kp.pubkey())
        except Exception:
            return {"ok": False, "reason": "wallet_decode"}

        sol = await _rpc_get_sol_balance(pubkey_str)
        if sol is None:
            return {"ok": False, "reason": "rpc_unavailable"}
        if sol < Cfg.LIVE_MIN_SOL_BUFFER:
            return {"ok": False, "reason": f"low_sol ({sol:.4f})"}

        # Quick quote probe
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
        """Ask Jupiter to build serialized (base64) swap tx from a quote."""
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
        """Live BUY. In DRY_RUN, just simulate."""
        if Cfg.DRY_RUN:
            return {"ok": True, "simulated": True, "txsig": None}

        # Load signer (solders.Keypair)
        kp = _load_keypair_solders()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        # Build tx from Jupiter quote
        async with aiohttp.ClientSession() as session:
            try:
                pubkey_str = str(kp.pubkey())
            except Exception:
                return {"ok": False, "reason": "wallet_decode"}

            raw = await self._build_swap_tx(session, route_info, pubkey_str)
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        # Sign & send with solana-py (uses solders types internally)
        try:
            from solana.transaction import VersionedTransaction as PyVT
            from solana.rpc.async_api import AsyncClient
            from solana.rpc.types import TxOpts
        except Exception as e:
            return {"ok": False, "reason": f"solana_lib_missing: {e}"}

        try:
            # Some solana-py builds have deserialize; others have from_bytes
            if hasattr(PyVT, "deserialize"):
                tx = PyVT.deserialize(raw)
            else:
                tx = PyVT.from_bytes(raw)  # type: ignore[attr-defined]

            # IMPORTANT: sign expects solders.Keypair list in modern solana-py
            tx.sign([kp])  # kp is solders.Keypair
            raw_signed = tx.serialize()

            async with AsyncClient(Cfg.RPC_URL) as rpc:
                resp = await rpc.send_raw_transaction(raw_signed, opts=TxOpts(skip_preflight=True))
                sig = str(resp.value)
                await self.send_msg(f"🟢 Executed BUY (live) — tx: {sig}")
                return {"ok": True, "txsig": sig}
        except Exception as e:
            return {"ok": False, "reason": f"tx_send_{e}"}
