# src/config.py
import os
import json

class Cfg:
    # --- mode / routing ---
    DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
    ROUTER  = os.environ.get("ROUTER", "PHOTON").upper()

    PHOTON_BASE = os.environ.get("PHOTON_BASE", "https://quote-api.jup.ag/v6").rstrip("/")
    JUP_BASE    = os.environ.get("JUP_BASE",    "https://quote-api.jup.ag/v6").rstrip("/")

    # caps
    FEE_CAP_PCT      = float(os.environ.get("FEE_CAP_PCT", "3.0"))
    MAX_SLIPPAGE_PCT = float(os.environ.get("MAX_SLIPPAGE_PCT", "3.0"))

    # paper bankroll
    PAPER_MODE_BAL_ENABLED = os.environ.get("PAPER_MODE_BAL_ENABLED", "true").lower() == "true"
    PAPER_START_BAL_USD    = float(os.environ.get("PAPER_START_BAL_USD", "40"))
    PER_TRADE_USD_TARGET   = float(os.environ.get("PER_TRADE_USD_TARGET", "10"))
    PER_TRADE_USD_MIN      = float(os.environ.get("PER_TRADE_USD_MIN", "8"))
    MAX_OPEN_POSITIONS     = int(os.environ.get("MAX_OPEN_POSITIONS", "3"))
    TOTAL_EXPOSURE_CAP_PCT = float(os.environ.get("TOTAL_EXPOSURE_CAP_PCT", "0.75"))
    CASH_RESERVE_PCT       = float(os.environ.get("CASH_RESERVE_PCT", "0.25"))

    # entry gates (quality)
    ENTRY_MC_MIN         = float(os.environ.get("ENTRY_MC_MIN", "75000"))
    ENTRY_MC_MAX         = float(os.environ.get("ENTRY_MC_MAX", "2000000"))
    ENTRY_LP_MIN_USD     = float(os.environ.get("ENTRY_LP_MIN_USD", "30000"))
    ENTRY_LP_TO_MCAP_MIN = float(os.environ.get("ENTRY_LP_TO_MCAP_MIN", "0.15"))
    ENTRY_POOL_AGE_MIN   = int(os.environ.get("ENTRY_POOL_AGE_MIN", "60"))
    VOL1H_MIN            = float(os.environ.get("VOL1H_MIN", "50000"))
    ACCEL_MIN            = float(os.environ.get("ACCEL_MIN", "0.8"))

    # momentum windows (optional hard filter)
    ENTRY_REQUIRE_POSITIVE_MOM = os.environ.get("ENTRY_REQUIRE_POSITIVE_MOM", "true").lower() == "true"
    ENTRY_PCHG_MIN = float(os.environ.get("ENTRY_PCHG_MIN", "10"))      # require >= +10% on horizon
    ENTRY_PCHG_MAX = os.environ.get("ENTRY_PCHG_MAX", "400")            # cap blow-offs (set to 'none' to disable)

    # trailing-only engine
    ATR_WINDOW    = int(os.environ.get("ATR_WINDOW", "12"))
    TRAIL_K       = float(os.environ.get("TRAIL_K", "2.8"))
    HARD_STOP_PCT = float(os.environ.get("HARD_STOP_PCT", "0.20"))
    GAP_PROTECT   = os.environ.get("GAP_PROTECT", "true").lower() == "true"
    GAP_PCT       = float(os.environ.get("GAP_PCT", "0.08"))
    RUG_ENABLED   = os.environ.get("RUG_ENABLED", "true").lower() == "true"
    RUG_DROP_PCT  = float(os.environ.get("RUG_DROP_PCT", "0.35"))

    # alert dedupe / discovery scan
    ALERT_COOLDOWN_MIN = int(os.environ.get("ALERT_COOLDOWN_MIN", "20"))
    SCAN_SLEEP_SEC     = int(os.environ.get("SCAN_SLEEP_SEC", "60"))  # discovery loop

    # high-frequency watch (1s)
    WATCH_TICK_SEC         = float(os.environ.get("WATCH_TICK_SEC", "1.0"))
    WATCHLIST_MAX          = int(os.environ.get("WATCHLIST_MAX", "10"))
    WATCH_ENTRY_ACCEL_PCT  = float(os.environ.get("WATCH_ENTRY_ACCEL_PCT", "2.5"))
    WATCH_QUOTE_INTERVAL_S = float(os.environ.get("WATCH_QUOTE_INTERVAL_S", "10"))
    WATCH_ROUTE_FAILS_EXIT = int(os.environ.get("WATCH_ROUTE_FAILS_EXIT", "2"))

    # admin / telegram
    ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))

    # data
    DATA_DIR = os.environ.get("DATA_DIR", "/data")

    # live trading (wallet)
    SOLANA_SECRET_KEY = os.environ.get("SOLANA_SECRET_KEY", "").strip()  # JSON array OR base58 string OR empty
    SOLANA_KEY_PATH   = os.environ.get("SOLANA_KEY_PATH", "").strip()    # optional: path to key file
    RPC_URL           = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
    LIVE_MIN_SOL_BUFFER = float(os.environ.get("LIVE_MIN_SOL_BUFFER", "0.02"))
    MODE_SWITCH_PIN   = os.environ.get("MODE_SWITCH_PIN", "").strip()

    # insiders (off for now)
    HELIUS_API_KEY   = os.environ.get("HELIUS_API_KEY", "").strip()
    HELIUS_BASE      = os.environ.get("HELIUS_BASE", "https://api.helius.xyz").rstrip("/")
    INSIDER_ENABLED  = os.environ.get("INSIDER_ENABLED", "false").lower() == "true"

    # paper auto-trade + nightly summary
    PAPER_AUTOTRADE      = os.environ.get("PAPER_AUTOTRADE", "true").lower() == "true"
    NIGHTLY_REPORT_LOCAL = os.environ.get("NIGHTLY_REPORT_LOCAL", "23:45")  # IST

    @staticmethod
    def has_live_key() -> bool:
        """
        True if a usable key is available via:
        - JSON array in SOLANA_SECRET_KEY (len 32 or 64)
        - Base58 string in SOLANA_SECRET_KEY (decoded len 32 or 64)
        - A local key file at SOLANA_KEY_PATH or /data/phantom_key.json
        """
        try:
            # 1) File path (explicit or default)
            key_path = Cfg.SOLANA_KEY_PATH or os.path.join(Cfg.DATA_DIR, "phantom_key.json")
            if os.path.exists(key_path):
                try:
                    with open(key_path, "r", encoding="utf-8") as f:
                        raw = f.read().strip()
                    # Try JSON array
                    try:
                        arr = json.loads(raw)
                        if isinstance(arr, list) and len(arr) in (32, 64):
                            return True
                    except Exception:
                        pass
                    # Try base58
                    try:
                        import base58  # lazy import
                        b = base58.b58decode(raw)
                        if len(b) in (32, 64):
                            return True
                    except Exception:
                        pass
                except Exception:
                    pass

            # 2) Environment variable
            if not Cfg.SOLANA_SECRET_KEY:
                return False

            # JSON array in env
            try:
                arr = json.loads(Cfg.SOLANA_SECRET_KEY)
                if isinstance(arr, list) and len(arr) in (32, 64):
                    return True
            except Exception:
                pass

            # Base58 in env
            try:
                import base58  # lazy import
                raw = base58.b58decode(Cfg.SOLANA_SECRET_KEY)
                return len(raw) in (32, 64)
            except Exception:
                pass

            return False
        except Exception:
            return False
