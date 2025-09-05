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
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# ---- key source resolution ----
KEY_FILE_PATH = (
    Cfg.SOLANA_KEY_PATH
    or os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")
    if os.path.exists(os.path.join(Cfg.DATA_DIR, "my_wallet_key.json"))
    else os.path.join(Cfg.DATA_DIR, "phantom_key.json")
)

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
    """Unified loader: env first, then file."""
    b = _load_secret_bytes_from_env()
    if b:
        return b
    b = _load_secret_bytes_from_file()
    if b:
        return b
    return None

def _load_keypair_solders():
    """
    Load a solders.Keypair from 64-byte secret (preferred) or 32-byte seed (if supported).
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

def _load_keypairs() -> Tuple[Optional[object], Optional[object]]:
    """Return (kp_solders, kp_solana). We only use solders path here; solana path kept as None."""
    kp_solders = _load_keypair_solders()
    kp_solana = None  # not used (solana.transaction not available on 0.36.9)
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

# ---------- ATA helpers ----------
async def _account_exists_rpc(session: aiohttp.ClientSession, address_str: str) -> bool:
    """Return True if account exists (via getAccountInfo)."""
    try:
        async with session.post(Cfg.RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [address_str, {"encoding": "base64"}]
        }, timeout=8) as r:
            if r.status != 200:
                return False
            js = await r.json()
            return bool(js.get("result") and js["result"].get("value"))
    except Exception as e:
        logger.warning(f"[Exec] rpc getAccountInfo error for {address_str}: {e}")
        return False

def _gather_target_mints_from_route(route_info: Dict[str, Any]) -> Set[str]:
    """
    Extract relevant token mints that we expect tokens for after swap.
    Jupiter quote JSON shapes vary; this tries to be robust:
      - Look for 'outputMint' on the top-level quote / legs.
      - Look for route_info['otherMints'] or any 'mint' fields.
    """
    mints: Set[str] = set()
    try:
        # common: route_info['outAmount'] etc. check explicit fields
        top_out = route_info.get("outputMint") or route_info.get("outMint")
        if top_out:
            mints.add(top_out)
    except Exception:
        pass

    # search in nested legs
    try:
        legs = route_info.get("routes") or route_info.get("swapLegs") or route_info.get("legs") or []
        if isinstance(legs, list):
            for leg in legs:
                # leg may contain 'outputMint' or 'mint'
                if isinstance(leg, dict):
                    o = leg.get("outputMint") or leg.get("mint") or leg.get("mintAddress")
                    if o:
                        mints.add(o)
                    # Some Jupiter responses put 'market' or 'inputMint'/'outputMint'
                    i = leg.get("inputMint") or leg.get("inputMintAddress")
                    if i:
                        mints.add(i)
    except Exception:
        pass

    # fallback: any string-looking mint in route_info values
    try:
        def walk(v):
            if isinstance(v, str) and len(v) >= 32:
                # crude: treat anything 32+ chars as a mint candidate
                mints.add(v)
            elif isinstance(v, dict):
                for vv in v.values(): walk(vv)
            elif isinstance(v, list):
                for vv in v: walk(vv)
        walk(route_info)
    except Exception:
        pass

    # remove SOL / USDC token pair (we don't create ATA for native SOL)
    mints = {m for m in mints if m and not m.lower().startswith("sol")}
    return mints

async def ensure_atas_before_swap(session: aiohttp.ClientSession, owner_pubkey_str: str, route_info: Dict[str,Any]) -> Tuple[bool, str]:
    """
    Ensure associated token accounts exist for mints in route_info for owner_pubkey_str.
    Returns (ok, reason). ok=True if nothing failed (or nothing needed); ok=False on fatal failure.
    This function tries solana-py + spl.token if available; otherwise it only checks existence and warns.
    """
    try:
        mints = _gather_target_mints_from_route(route_info)
        if not mints:
            return True, "no_mints_found"

        # convert owner Pubkey
        owner = owner_pubkey_str

        # Check each ATA existence via RPC; gather missing
        missing: List[Tuple[str,str]] = []
        for mint in mints:
            try:
                # compute associated token address (try spl helper)
                ata_addr = None
                try:
                    # prefer spl-token helper if installed
                    from spl.token.instructions import get_associated_token_address
                    from solders.pubkey import Pubkey as SoldersPubkey
                    ata_addr = str(get_associated_token_address(SoldersPubkey.from_string(owner), SoldersPubkey.from_string(mint)))
                except Exception:
                    # fallback: derive ATA via RPC helper (ask Jupiter?). If we can't compute, skip.
                    # Best-effort: try using solana-py PublicKey helper
                    try:
                        from solana.publickey import PublicKey as PyPubKey
                        from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID
                        from spl.token.instructions import get_associated_token_address as _get_ata
                        ata_addr = str(_get_ata(PyPubKey(owner), PyPubKey(mint)))
                    except Exception:
                        # give up computing ATA for this mint
                        logger.warning(f"[Exec] cannot compute ATA for mint {mint}; skipping ATA create check")
                        continue

                exists = await _account_exists_rpc(session, ata_addr)
                if not exists:
                    missing.append((mint, ata_addr))
            except Exception as e:
                logger.warning(f"[Exec] ensure_atas check error for {mint}: {e}")
                continue

        if not missing:
            return True, "atas_ok"

        # If nothing to create or solana-py not available, we still return ok but warn
        try:
            # import solana-py signing + transaction helpers
            from solana.transaction import Transaction
            from solana.rpc.async_api import AsyncClient
            from solana.system_program import SYS_PROGRAM_ID
            from solana.publickey import PublicKey as PyPubKey
            from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID
            from spl.token.instructions import create_associated_token_account
        except Exception as e:
            logger.warning(f"[Exec] solana/spl missing => cannot auto-create ATAs: {e}")
            # not fatal; the swap may still fail but we avoid blocking here
            return False, "solana_spl_missing"

        # create each missing ATA in separate small transactions (safe)
        for mint, ata in missing:
            try:
                tx = Transaction()
                payer = PyPubKey(owner)
                mint_pub = PyPubKey(mint)
                # create_associated_token_account(payer, owner, mint) returns Instruction
                instr = create_associated_token_account(payer, payer, mint_pub)
                tx.add(instr)
                async with AsyncClient(Cfg.RPC_URL) as rpc:
                    # build recent blockhash & send
                    recent = await rpc.get_latest_blockhash()
                    tx.recent_blockhash = recent.value.blockhash
                    tx.fee_payer = payer
                    # signing requires a Keypair object from environment
                    # Try to load solana-py Keypair
                    kp_py = None
                    try:
                        from solana.keypair import Keypair as PyKeypair
                        b = _secret_bytes()
                        if b and len(b) == 64:
                            kp_py = PyKeypair.from_secret_key(bytes(b))
                        elif b and len(b) == 32 and hasattr(PyKeypair, "from_seed"):
                            kp_py = PyKeypair.from_seed(bytes(b))
                    except Exception:
                        kp_py = None

                    if not kp_py:
                        logger.warning("[Exec] cannot sign ATA create: solana.keypair not available or key unsupported")
                        return False, "no_solana_keypair"

                    tx.sign(kp_py)
                    raw = tx.serialize()
                    resp = await rpc.send_raw_transaction(raw, opts={"skip_preflight": False})
                    logger.info(f"[Exec] created ATA {ata} for mint {mint}; tx {resp.value}")
                    # small sleep to let state settle
                    await asyncio.sleep(0.25)
            except Exception as e:
                logger.warning(f"[Exec] ATA create failed for mint {mint}: {e}")
                return False, f"ata_create_fail_{mint}"

        return True, "atas_created"
    except Exception as e:
        logger.warning(f"[Exec] ensure_atas error: {e}")
        return False, "ensure_atas_exception"


# ---------- Execution Engine (patched) ----------
class ExecutionEngine:
    def init(self, base_quote_url: str, send_msg):
        self.base = base_quote_url.rstrip("/")
        self.send_msg = send_msg

    async def preflight(self) -> Dict[str, Any]:
        # quick presence check (unchanged)
        if not (Cfg.SOLANA_SECRET_KEY or Cfg.SOLANA_KEY_PATH):
            if not os.path.exists(os.path.join(Cfg.DATA_DIR, "my_wallet_key.json")) and \
               not os.path.exists(os.path.join(Cfg.DATA_DIR, "phantom_key.json")):
                return {"ok": False, "reason": "no_wallet"}

        kp_solders, _ = _load_keypairs()
        if not kp_solders:
            return {"ok": False, "reason": "wallet_decode"}

        try:
            from solana.rpc.async_api import AsyncClient  # noqa
        except Exception:
            return {"ok": False, "reason": "solana_lib_missing"}

        try:
            user_pub = str(kp_solders.pubkey())
        except Exception:
            return {"ok": False, "reason": "wallet_decode"}

        sol = await _rpc_get_sol_balance(user_pub)
        if sol is None:
            return {"ok": False, "reason": "rpc_unavailable"}
        if sol < Cfg.LIVE_MIN_SOL_BUFFER:
            return {"ok": False, "reason": f"low_sol ({sol:.4f})"}

        # probe quote endpoint (use your configured JUPITER_SLIPPAGE_BPS if you set it)
        async with aiohttp.ClientSession() as session:
            try:
                url = f"{self.base}/quote"
                params = {"inputMint": USDC_MINT, "outputMint": USDC_MINT, "amount": "1000", "slippageBps": str(getattr(Cfg, 'JUPITER_SLIPPAGE_BPS', 300))}
                async with session.get(url, params=params, timeout=8) as r:
                    if r.status != 200:
                        return {"ok": False, "reason": f"quote_http_{r.status}"}
            except Exception as e:
                return {"ok": False, "reason": f"quote_err_{e}"}
        return {"ok": True, "reason": "ok"}

    async def _build_swap_tx(self, session: aiohttp.ClientSession, route_info: Dict[str, Any], user_pubkey: str) -> Optional[bytes]:
        """
        Ask Jupiter to build serialized (base64) swap tx from a quote.  We call ensure_atas_before_swap
        first to pre-create any missing ATAs.
        """
        # Pre-create ATAs (best-effort)
        ok, why = await ensure_atas_before_swap(session, user_pubkey, route_info)
        if not ok:
            logger.warning(f"[Exec] ATA creation step returned non-ok: {why} (continuing to build swap; swap may fail)")

        url = f"{self.base}/swap"
        payload = {
            "quoteResponse": route_info,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "slippageBps": getattr(Cfg, "JUPITER_SLIPPAGE_BPS", 600),
            "prioritizationFeeLamports": getattr(Cfg, "PRIORITY_FEE_LAMPORTS", 5000),
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
                    logger.warning(f"[Exec] swap build returned no serialized tx: {js}")
                    return None
                return base64.b64decode(b64)
        except Exception as e:
            logger.warning(f"[Exec] swap_build_error: {e}")
            return None

    async def execute_buy(self, mint: str, usd_amount: float, route_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Live BUY execution path. In DRY_RUN, only simulates.
        (I left the signing / send logic unchanged from your working solders-only path.)
        """
        if Cfg.DRY_RUN:
            return {"ok": True, "simulated": True, "txsig": None}

        kp_solders, _ = _load_keypairs()
        if not kp_solders:
            return {"ok": False, "reason": "wallet_decode"}

        try:
            user_pubkey = str(kp_solders.pubkey())
        except Exception:
            return {"ok": False, "reason": "wallet_decode"}

        async with aiohttp.ClientSession() as session:
            raw = await self._build_swap_tx(session, route_info, user_pubkey)
            if not raw:
                return {"ok": False, "reason": "swap_build"}

        try:
            from solders.transaction import VersionedTransaction as SVT
            tx_unsigned = SVT.from_bytes(raw)
            msg_obj = tx_unsigned.message
            tx_signed = SVT(msg_obj, [kp_solders])
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