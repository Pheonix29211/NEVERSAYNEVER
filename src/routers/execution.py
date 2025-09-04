from __future__ import annotations
import os, json, base64, aiohttp
from typing import Optional, Dict, Any, Tuple
from ..config import Cfg
from ..log import logger

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
KEY_FILE_PATH = Cfg.SOLANA_KEY_PATH or os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")

# ---------- Key loading helpers ----------

def _decode_json_array(s: str) -> Optional[bytes]:
    try:
        arr = json.loads(s)
        if isinstance(arr, list) and all(isinstance(x, int) for x in arr) and len(arr) in (32, 64):
            return bytes(arr)
        return None
    except Exception:
        return None

def _decode_base58(s: str) -> Optional[bytes]:
    try:
        import base58
        b = base58.b58decode(s.strip())
        if len(b) in (32, 64):
            return b
    except Exception:
        return None
    return None

def _load_secret_bytes_from_env() -> Optional[bytes]:
    secret = (Cfg.SOLANA_SECRET_KEY or "").strip()
    if not secret:
        return None
    b = _decode_json_array(secret)
    if b: return b
    b = _decode_base58(secret)
    if b: return b
    return None

def _load_secret_bytes_from_file() -> Optional[bytes]:
    path = KEY_FILE_PATH
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        b = _decode_json_array(content)
        if b: return b
        b = _decode_base58(content)
        if b: return b
    except Exception:
        return None
    return None

def _secret_bytes() -> Optional[bytes]:
    return _load_secret_bytes_from_env() or _load_secret_bytes_from_file()

def _load_keypairs() -> Tuple[Optional[object], Optional[object]]:
    """
    Return (kp_solders, kp_solana) from the secret bytes.
    """
    b = _secret_bytes()
    if not b:
        return (None, None)

    kp_solders, kp_solana = None, None

    # solders first
    try:
        from solders.keypair import Keypair as SKeypair
        if len(b) == 64:
            kp_solders = SKeypair.from_bytes(b)
        elif len(b) == 32 and hasattr(SKeypair, "from_seed"):
            kp_solders = SKeypair.from_seed(b)
    except Exception:
        kp_solders = None

    # solana-py fallback
    try:
        from solana.keypair import Keypair as PyKeypair
        if len(b) == 64:
            kp_solana = PyKeypair.from_secret_key(b)
        elif len(b) == 32 and hasattr(PyKeypair, "from_seed"):
            kp_solana = PyKeypair.from_seed(b)
    except Exception:
        kp_solana = None

    return (kp_solders, kp_solana)

async def _rpc_get_sol_balance(pubkey_str: str) -> Optional[float]:
    try:
        from solana.rpc.async_api import AsyncClient
        from solders.pubkey import Pubkey
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
        if not (Cfg.SOLANA_SECRET_KEY or Cfg.SOLANA_KEY_PATH):
            if not os.path.exists(os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")) and \
               not os.path.exists(os.path.join(Cfg.DATA_DIR, "phantom_key.json")):
                return {"ok": False, "reason": "no_wallet"}

        kp_solders, kp_solana = _load_keypairs()
        if not (kp_solders or kp_solana):
            return {"ok": False, "reason": "wallet_decode"}

        try:
            from solana.rpc.async_api import AsyncClient  # noqa
        except Exception:
            return {"ok": False, "reason": "solana_lib_missing"}

        pubkey_str = None
        if kp_solders is not None:
            try: pubkey_str = str(kp_solders.pubkey())
            except Exception: pass
        if not pubkey_str and kp_solana is not None:
            try: pubkey_str = str(kp_solana.public_key)
            except Exception: pass

        if not pubkey_str:
            return {"ok": False, "reason": "wallet_decode"}

        sol = await _rpc_get_sol_balance(pubkey_str)
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

    async def _build_swap_tx(self, session: aiohttp.ClientSession, route_info: Dict[str, Any], user_pubkey: str) -> Optional[bytes]:
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
        if Cfg.DRY_RUN:
            return {"ok": True, "simulated": True, "txsig": None}

        kp_solders, kp_solana = _load_keypairs()
        if not (kp_solders or kp_solana):
            return {"ok": False, "reason": "wallet_decode"}

        pubkey_str = None
        if kp_solders is not None:
            try: pubkey_str = str(kp_solders.pubkey())
            except Exception: pass
        if not pubkey_str and kp_solana is not None:
            try: pubkey_str = str(kp_solana.public_key)
            except Exception: pass
        if not pubkey_str:
            return {"ok": False, "reason": "wallet_decode"}

        async with aiohttp.ClientSession() as session:
            raw = await self._build_swap_tx(session, route_info, pubkey_str)
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        # --- Sign with solders (manual path) ---
        try:
            from solders.transaction import VersionedTransaction as SVT
            tx_unsigned = SVT.from_bytes(raw)
            msg_obj = tx_unsigned.message
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
                msg_bytes = bytes(msg_obj)

            sig = kp_solders.sign_message(msg_bytes)
            tx_signed = SVT(msg_obj, [sig])
            raw_signed = bytes(tx_signed)
        except Exception as e:
            return {"ok": False, "reason": f"solders_sign_fail: {e}"}

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
