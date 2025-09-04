# from __future__ import annotations   # (optional in 3.11+)
import os, json, base64, aiohttp
from typing import Optional, Dict, Any
from ..config import Cfg
from ..log import logger

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Allow key file override; default to /data/phantokey.json
KEY_FILE_PATH = os.environ.get("SOLANA_KEY_PATH", "/data/phantokey.json").strip()

# ---------- Key loading helpers ----------

def _decode_json_array(s: str) -> Optional[bytes]:
    """Parse a JSON array like [1,2,...] into bytes (expects 32 or 64 length)."""
    try:
        arr = json.loads(s)
        if isinstance(arr, list) and all(isinstance(x, int) for x in arr) and len(arr) in (32, 64):
            b = bytes(arr)
            return b
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
    """Unified loader: env first, then file."""
    b = _load_secret_bytes_from_env()
    if b:
        return b
    b = _load_secret_bytes_from_file()
    if b:
        return b
    return None

def _load_keypair():
    """
    Load solders.Keypair from either:
    - 64-byte secret key (preferred) or
    - 32-byte seed (if supported by library).
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

    # Try 64-byte secret first (private+public)
    if len(raw) == 64:
        try:
            kp = Keypair.from_bytes(raw)
            return kp
        except Exception as e:
            logger.warning(f"[Exec] Keypair.from_bytes(64) failed: {e}")

    # Some exports give 32-byte seed; attempt from_seed if available
    if len(raw) == 32:
        try:
            # Not all solders builds expose from_seed; try and report clearly
            from_seed = getattr(Keypair, "from_seed", None)
            if callable(from_seed):
                kp = from_seed(raw)
                return kp
            else:
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

# ---------- Execution Engine ----------

# ---------- Execution Engine ----------

class ExecutionEngine:
    """
    Live trade executor (Jupiter /swap).
    - Loads wallet key from env JSON, env base58, or /data/phantom_key.json (override with WALLET_KEY_FILE)
    - Probes /quote in preflight
    - Builds /swap tx, signs via solders (no .sign() method needed)
    """

    # ------------------------- lifecycle -------------------------

    def __init__(self, base_quote_url: str, send_msg):
        import os
        self.base = base_quote_url.rstrip("/")
        self.send_msg = send_msg
        # where to look for file-based key (JSON array or base58)
        self.key_file_path = os.environ.get("WALLET_KEY_FILE", "/data/phantom_key.json")

    # ------------------------- key handling -------------------------

    @staticmethod
    def _b58_ok(s: str) -> bool:
        return all(c.isalnum() or c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in s.strip())

    def _read_key_source(self) -> str | None:
        """Return raw key material from env or file (string)."""
        import os, json
        # Env first
        raw = os.environ.get("SOLANA_SECRET_KEY", "").strip()
        if raw:
            return raw
        # Then file
        try:
            if os.path.exists(self.key_file_path):
                with open(self.key_file_path, "r", encoding="utf-8") as f:
                    txt = f.read().strip()
                    if txt:
                        return txt
        except Exception:
            pass
        return None

    def _parse_keypair(self):
        """
        Return solders.Keypair or None.
        Accepts:
          - JSON array of 64 or 32 ints
          - base58-encoded 64-byte secret key
        """
        import json, base58
        from solders.keypair import Keypair

        raw = self._read_key_source()
        if not raw:
            return None

        # Try JSON array
        try:
            arr = json.loads(raw)
            if isinstance(arr, list) and all(isinstance(x, int) for x in arr):
                b = bytes(arr)
                if len(b) == 64:
                    return Keypair.from_bytes(b)
                if len(b) == 32:
                    # From seed (ed25519). solders supports from_seed for 32 bytes.
                    return Keypair.from_seed(b)
        except Exception:
            pass

        # Try base58
        if self._b58_ok(raw):
            try:
                b = base58.b58decode(raw)
                if len(b) == 64:
                    return Keypair.from_bytes(b)
                if len(b) == 32:
                    return Keypair.from_seed(b)
            except Exception:
                pass

        return None

    @staticmethod
    async def _rpc_get_sol_balance(pubkey_str: str, rpc_url: str) -> float | None:
        try:
            from solana.rpc.async_api import AsyncClient
            from solders.pubkey import Pubkey
            pk = Pubkey.from_string(pubkey_str)
            async with AsyncClient(rpc_url) as rpc:
                bal = await rpc.get_balance(pk)
                return bal.value / 1_000_000_000.0
        except Exception:
            return None

    # ------------------------- preflight -------------------------

    async def preflight(self, cfg) -> dict:
        """
        Ensure: wallet present & decodable, RPC reachable, SOL buffer ok, /quote ok.
        `cfg` must provide: RPC_URL, LIVE_MIN_SOL_BUFFER, SOLANA_SECRET_KEY (optional).
        """
        import aiohttp

        kp = self._parse_keypair()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        # RPC + balance
        sol = await self._rpc_get_sol_balance(str(kp.pubkey()), cfg.RPC_URL)
        if sol is None:
            return {"ok": False, "reason": "rpc_unavailable"}
        if sol < float(getattr(cfg, "LIVE_MIN_SOL_BUFFER", 0.02)):
            return {"ok": False, "reason": f"low_sol ({sol:.4f})"}

        # Probe Jupiter /quote (USDC->USDC dummy)
        USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        try:
            async with aiohttp.ClientSession() as session:
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

    # ------------------------- swap helpers -------------------------

    async def _build_swap_tx(self, session, route_info: dict, user_pubkey: str) -> bytes | None:
        import base64
        url = f"{self.base}/swap"
        payload = {
            "quoteResponse": route_info,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
        }
        try:
            async with session.post(url, json=payload, timeout=12) as r:
                if r.status != 200:
                    txt = await r.text()
                    # optional: log warning outside if you have a logger
                    return None
                js = await r.json()
                b64 = js.get("swapTransaction") or js.get("serializedTransaction")
                if not b64:
                    return None
                return base64.b64decode(b64)
        except Exception:
            return None

    # ------------------------- execute buy -------------------------

    async def execute_buy(self, mint: str, usd_amount: float, route_info: dict, cfg) -> dict:
        """
        Build swap via Jupiter, sign with solders, submit via solana-py.
        """
        import aiohttp
        from solders.transaction import VersionedTransaction
        from solders.signature import Signature
        from solana.rpc.async_api import AsyncClient
        from solana.rpc.types import TxOpts

        if getattr(cfg, "DRY_RUN", True):
            return {"ok": True, "simulated": True, "txsig": None}

        kp = self._parse_keypair()
        if not kp:
            return {"ok": False, "reason": "wallet_decode"}

        # Build tx
        async with aiohttp.ClientSession() as session:
            raw = await self._build_swap_tx(session, route_info, str(kp.pubkey()))
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        # Sign (no .sign() in solders VersionedTransaction)
        try:
            tx = VersionedTransaction.from_bytes(raw)        # deserialize bytes -> message + empty sigs
            msg_bytes = bytes(tx.message)                    # serialize message
            sig = kp.sign_message(msg_bytes)                 # ed25519 signature
            tx = VersionedTransaction(tx.message, [Signature.from_bytes(sig)])  # assemble signed tx
        except Exception as e:
            return {"ok": False, "reason": f"tx_sign_{e}"}

        # Send
        try:
            async with AsyncClient(cfg.RPC_URL) as rpc:
                resp = await rpc.send_raw_transaction(bytes(tx), opts=TxOpts(skip_preflight=True))
                sig_str = str(resp.value)
                if callable(self.send_msg):
                    await self.send_msg(f"🟢 Executed BUY (live) — tx: {sig_str}")
                return {"ok": True, "txsig": sig_str}
        except Exception as e:
            return {"ok": False, "reason": f"tx_send_{e}"}
