# src/routers/execution.py
# src/routers/execution.py
from __future__ import annotations

import os
import json
import base64
from typing import Optional, Dict, Any, Tuple

import aiohttp

from ..config import Cfg
from ..log import logger

# ---- Constants ----
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Default key files we’ll check if SOLANA_KEY_PATH isn’t set
DEFAULT_KEY_FILES = ("my_wallet_key.json", "phantom_key.json")

# Pre-resolve a key file path for convenience
def _resolve_key_file_path() -> Optional[str]:
    # explicit SOLANA_KEY_PATH takes precedence
    if Cfg.SOLANA_KEY_PATH:
        return Cfg.SOLANA_KEY_PATH
    # otherwise check common names under DATA_DIR
    for name in DEFAULT_KEY_FILES:
        p = os.path.join(Cfg.DATA_DIR, name)
        if os.path.exists(p):
            return p
    return None

KEY_FILE_PATH: Optional[str] = _resolve_key_file_path()

# ---------- Key loading helpers ----------

def _decode_json_array(s: str) -> Optional[bytes]:
    """Parse a JSON array like [1,2,...] into bytes (expects length 32 or 64)."""
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

def _load_secret_bytes_from_env() -> Optional[bytes]:
    """Try to read key material from SOLANA_SECRET_KEY env (JSON array or base58)."""
    secret = (Cfg.SOLANA_SECRET_KEY or "").strip()
    if not secret:
        return None
    # Try JSON array first
    b = _decode_json_array(secret)
    if b:
        return b
    # Try base58 string
    b = _decode_base58(secret)
    if b:
        return b
    logger.warning("[Exec] env SOLANA_SECRET_KEY present but not valid JSON array or base58")
    return None

def _load_secret_bytes_from_file() -> Optional[bytes]:
    """Try to read key material from SOLANA_KEY_PATH (or default files) as JSON array or base58."""
    path = KEY_FILE_PATH
    if not path:
        return None
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        # JSON array?
        b = _decode_json_array(content)
        if b:
            return b
        # base58?
        b = _decode_base58(content)
        if b:
            return b
        logger.warning(f"[Exec] key file {path} not valid JSON array or base58")
        return None
    except Exception as e:
        logger.warning(f"[Exec] key file read error {path}: {e}")
        return None

def _secret_bytes() -> Optional[bytes]:
    """Unified key loader: env first, then file fallback."""
    b = _load_secret_bytes_from_env()
    if b:
        return b
    b = _load_secret_bytes_from_file()
    if b:
        return b
    return None

def _load_keypairs() -> Tuple[Optional[object], Optional[object]]:
    """
    Return (kp_solders, kp_solana) — one or both may be None.
    kp_solders: solders.keypair.Keypair
    kp_solana:  solana.keypair.Keypair
    """
    secret = _secret_bytes()
    if not secret:
        logger.warning("[Exec] no key material found (env/file)")
        return None, None

    kp_solders = None
    kp_solana  = None

    # solders
    try:
        from solders.keypair import Keypair as SKeypair  # type: ignore
        if len(secret) == 64:
            kp_solders = SKeypair.from_bytes(secret)
        elif len(secret) == 32 and hasattr(SKeypair, "from_seed"):
            kp_solders = SKeypair.from_seed(secret)  # some builds support this
    except Exception as e:
        logger.warning(f"[Exec] solders keypair load failed: {e}")

    # solana-py
    try:
        from solana.keypair import Keypair as PyKeypair  # type: ignore
        if len(secret) == 64:
            kp_solana = PyKeypair.from_secret_key(secret)
        elif len(secret) == 32 and hasattr(PyKeypair, "from_seed"):
            kp_solana = PyKeypair.from_seed(secret)
    except Exception as e:
        # Not fatal; we prefer solana-py for signing but can still work without
        logger.warning(f"[Exec] solana-py keypair load failed: {e}")

    return kp_solders, kp_solana

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

# ---------- Execution Engine ----------

class ExecutionEngine:
    def __init__(self, base_quote_url: str, send_msg):
        self.base = base_quote_url.rstrip("/")
        self.send_msg = send_msg

    async def preflight(self) -> Dict[str, Any]:
        # quick presence check
        if not (Cfg.SOLANA_SECRET_KEY or Cfg.SOLANA_KEY_PATH):
            if KEY_FILE_PATH is None:
                return {"ok": False, "reason": "no_wallet"}

        kp_solders, kp_solana = _load_keypairs()
        if not (kp_solders or kp_solana):
            return {"ok": False, "reason": "wallet_decode"}

        try:
            from solana.rpc.async_api import AsyncClient  # noqa
        except Exception:
            return {"ok": False, "reason": "solana_lib_missing"}

        # pick a pubkey to check balance
        pubkey_str = None
        if kp_solders is not None:
            try:
                pubkey_str = str(kp_solders.pubkey())
            except Exception:
                pubkey_str = None
        if pubkey_str is None and kp_solana is not None:
            try:
                pubkey_str = str(kp_solana.public_key)
            except Exception:
                pubkey_str = None

        if not pubkey_str:
            return {"ok": False, "reason": "wallet_decode"}

        sol = await _rpc_get_sol_balance(pubkey_str)
        if sol is None:
            return {"ok": False, "reason": "rpc_unavailable"}
        if sol < Cfg.LIVE_MIN_SOL_BUFFER:
            return {"ok": False, "reason": f"low_sol ({sol:.4f})"}

        # Probe quote endpoint
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
        """Live BUY execution. In DRY_RUN, only simulates."""
        if Cfg.DRY_RUN:
            return {"ok": True, "simulated": True, "txsig": None}

        kp_solders, kp_solana = _load_keypairs()
        if not (kp_solders or kp_solana):
            return {"ok": False, "reason": "wallet_decode"}

        # Choose pubkey string for swap build
        pubkey_str = None
        if kp_solders is not None:
            try:
                pubkey_str = str(kp_solders.pubkey())
            except Exception:
                pubkey_str = None
        if pubkey_str is None and kp_solana is not None:
            try:
                pubkey_str = str(kp_solana.public_key)
            except Exception:
                pubkey_str = None

        if not pubkey_str:
            return {"ok": False, "reason": "wallet_decode"}

        # Build swap transaction from Jupiter
        async with aiohttp.ClientSession() as session:
            raw = await self._build_swap_tx(session, route_info, pubkey_str)
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        # ---- Prefer solana-py signing (stable) ----
        try:
            from solana.transaction import VersionedTransaction as PyVT  # type: ignore
            from solana.keypair import Keypair as PyKeypair  # type: ignore
            from solana.rpc.async_api import AsyncClient
            from solana.rpc.types import TxOpts

            secret = _secret_bytes()
            if not secret:
                return {"ok": False, "reason": "wallet_decode"}

            if len(secret) == 64:
                py_kp = PyKeypair.from_secret_key(secret)
            elif len(secret) == 32 and hasattr(PyKeypair, "from_seed"):
                py_kp = PyKeypair.from_seed(secret)  # if supported by your build
            else:
                return {"ok": False, "reason": "wallet_decode"}

            # Some versions expose .deserialize, others .from_bytes
            if hasattr(PyVT, "deserialize"):
                tx_py = PyVT.deserialize(raw)
            else:
                tx_py = PyVT.from_bytes(raw)  # type: ignore

            tx_py.sign([py_kp])
            raw_signed = tx_py.serialize()

            async with AsyncClient(Cfg.RPC_URL) as rpc:
                resp = await rpc.send_raw_transaction(raw_signed, opts=TxOpts(skip_preflight=True))
                sig = str(resp.value)
                await self.send_msg(f"🟢 Executed BUY (live) — tx: {sig}")
                return {"ok": True, "txsig": sig}

        except Exception as e:
            solana_err = e  # fall through to solders

        # ---- Only use solders if it actually has a signer method ----
        try:
            from solders.transaction import VersionedTransaction as SVT  # type: ignore
            if hasattr(SVT, "sign"):
                # Newer solders builds: simple sign
                tx = SVT.from_bytes(raw)
                if kp_solders is None:
                    return {"ok": False, "reason": "wallet_decode"}
                tx.sign([kp_solders])  # type: ignore
                raw_signed = bytes(tx)

                from solana.rpc.async_api import AsyncClient
                from solana.rpc.types import TxOpts
                async with AsyncClient(Cfg.RPC_URL) as rpc:
                    resp = await rpc.send_raw_transaction(raw_signed, opts=TxOpts(skip_preflight=True))
                    sig = str(resp.value)
                    await self.send_msg(f"🟢 Executed BUY (live) — tx: {sig}")
                    return {"ok": True, "txsig": sig}
            else:
                return {"ok": False, "reason": "solders_tx_has_no_sign (use solana-py path)"}  # clear message
        except Exception as e:
            return {"ok": False, "reason": f"solders_sign_fail: {e} (solana error was: {solana_err})"}
