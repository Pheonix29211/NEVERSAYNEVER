# src/telegram/bot.py
from __future__ import annotations

import os
import asyncio
from typing import Optional, List, Dict, Any

from telegram import Update, ParseMode
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    Filters,
)

from ..config import Cfg
from ..log import logger
from ..services.backtest.runner import run_backtest
from ..routers.execution import _secret_bytes, ExecutionEngine  # <- unified key loader + engine

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

HELP_TEXT = (
    "🧭 *Commands*\n"
    "/start — hello\n"
    "/help — show commands\n"
    "/status — mode & router\n"
    "/mode — show / switch mode (/mode live [PIN], /mode paper)\n"
    "/preflight — live-readiness checks\n"
    "/wallet — public key & balances\n"
    "/backtest [h] — Dex backtest snapshot (tokens included)\n"
    "/portfolio — paper balance & open positions\n"
    "/trades — show paper trade history CSV path\n"
    "/autopaper on|off — toggle paper auto-trading\n"
    "/export — show latest CSV paths\n"
    "/ping — check bot is alive\n"
)

def _fmt_tokens(picked: List[Dict[str, Any]], max_items: int = 15) -> str:
    if not picked:
        return "(no tokens)"
    rows = []
    for t in picked[:max_items]:
        name = t.get("name") or "token"
        mint = (t.get("mint") or "")
        mint_short = f"{mint[:6]}…" if mint else ""
        mc = t.get("mc") or 0
        lp = t.get("lp") or 0
        pchg = t.get("pchg")
        rows.append(
            f"• {name} ({mint_short}) — MC ${mc:,.0f}, LP ${lp:,.0f}, dP≈{pchg if pchg is not None else 'n/a'}%"
        )
    more = len(picked) - min(len(picked), max_items)
    if more > 0:
        rows.append(f"…and {more} more")
    return "\n".join(rows)


class TGBot:
    def __init__(self, ledger):
        if not BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
        self.app: Optional[Updater] = None
        self.ledger = ledger

    def run_async(self):
        self.app = Updater(token=BOT_TOKEN, use_context=True)
        bot = self.app.bot
        try:
            bot.delete_webhook(drop_pending_updates=False)
        except Exception as e:
            logger.warning(f"[TG] delete_webhook warn: {e}")

        dp = self.app.dispatcher
        dp.add_handler(CommandHandler("start", self._start))
        dp.add_handler(CommandHandler("help", self._help))
        dp.add_handler(CommandHandler("status", self._status))
        dp.add_handler(CommandHandler("mode", self._mode))
        dp.add_handler(CommandHandler("preflight", self._preflight))
        dp.add_handler(CommandHandler("wallet", self._wallet))
        dp.add_handler(CommandHandler("backtest", self._backtest))
        dp.add_handler(CommandHandler("portfolio", self._portfolio))
        dp.add_handler(CommandHandler("trades", self._trades))
        dp.add_handler(CommandHandler("autopaper", self._autopaper))
        dp.add_handler(CommandHandler("export", self._export))
        dp.add_handler(CommandHandler("ping", self._ping))
        dp.add_handler(MessageHandler(Filters.command, self._unknown))
        dp.add_error_handler(self._on_error)

        self.app.start_polling(timeout=60, read_latency=10.0)
        logger.info("Telegram bot polling started (PTB v13).")

    @property
    def updater(self):
        return self.app

    async def safe_send(self, text: str):
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self.app.bot.send_message, Cfg.ADMIN_CHAT_ID, text
            )
        except Exception as e:
            logger.warning(f"[TG] send failed: {e}")

    # ---- commands ----

    def _start(self, u: Update, c: CallbackContext):
        u.message.reply_text("✨ I’m awake! Use /help to see commands.")

    def _help(self, u: Update, c: CallbackContext):
        u.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    def _status(self, u: Update, c: CallbackContext):
        mode = "DRY_RUN" if Cfg.DRY_RUN else "LIVE"
        trail = f"Trailing-only: ON (ATR={Cfg.ATR_WINDOW}, K={Cfg.TRAIL_K})"
        u.message.reply_text(
            f"⚡️ Status:\n"
            f"Mode: {mode}\n"
            f"Router: {Cfg.ROUTER}\n"
            f"Per-trade target: ${Cfg.PER_TRADE_USD_TARGET:.2f}\n"
            f"Max slots: {Cfg.MAX_OPEN_POSITIONS}\n"
            f"Fee cap: {Cfg.FEE_CAP_PCT:.1f}% | Slippage cap: {Cfg.MAX_SLIPPAGE_PCT:.1f}%\n"
            f"{trail}"
        )

    def _mode(self, u: Update, c: CallbackContext):
        args = c.args or []
        if not args:
            u.message.reply_text(
                f"Mode is currently {'DRY_RUN' if Cfg.DRY_RUN else 'LIVE'}.\n"
                f"Use /mode live [PIN] or /mode paper.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        target = args[0].lower()
        if target == "paper":
            Cfg.DRY_RUN = True
            u.message.reply_text("🔧 Switched to PAPER mode (no real trades).")
            return

        if target == "live":
            pin = os.environ.get("MODE_SWITCH_PIN", "").strip()
            if pin and (len(args) < 2 or args[1] != pin):
                u.message.reply_text(
                    "⛔️ PIN required. Usage: /mode live 1234",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            Cfg.DRY_RUN = False
            u.message.reply_text(
                "🟢 Switched to LIVE mode (make sure wallet & funds are configured)."
            )
            return

        u.message.reply_text(
            "Usage: /mode, /mode live [PIN], /mode paper", parse_mode=ParseMode.MARKDOWN
        )

    def _preflight(self, u: Update, c: CallbackContext):
        async def run():
            ee = ExecutionEngine(Cfg.PHOTON_BASE, lambda m: self.app.bot.send_message(Cfg.ADMIN_CHAT_ID, m))
            res = await ee.preflight()
            if res.get("ok"):
                await self.safe_send("✅ Preflight OK: wallet & routing healthy.")
            else:
                await self.safe_send(f"⚠️ Preflight failed: {res.get('reason')}")

        asyncio.get_event_loop().create_task(run())
        u.message.reply_text("🔎 Running preflight…")

    def _wallet(self, u: Update, c: CallbackContext):
        try:
            b = _secret_bytes()  # unified env/file loader -> bytes (32 or 64)
            if not b:
                u.message.reply_text(
                    "No wallet configured. Set SOLANA_SECRET_KEY (JSON/base58) "
                    "or SOLANA_KEY_PATH to a key file."
                )
                return

            pub = None

            # Try solders first
            try:
                from solders.keypair import Keypair as SKeypair  # type: ignore
                kp = None
                if len(b) == 64:
                    kp = SKeypair.from_bytes(b)
                elif len(b) == 32 and hasattr(SKeypair, "from_seed"):
                    kp = SKeypair.from_seed(b)
                if kp:
                    pub = str(kp.pubkey())
            except Exception:
                pass

            # Fallback to solana-py
            if not pub:
                try:
                    from solana.keypair import Keypair as PyKeypair  # type: ignore
                    kp2 = None
                    if len(b) == 64:
                        kp2 = PyKeypair.from_secret_key(b)
                    elif len(b) == 32 and hasattr(PyKeypair, "from_seed"):
                        kp2 = PyKeypair.from_seed(b)
                    if kp2:
                        pub = str(kp2.public_key)
                except Exception:
                    pass

            if not pub:
                u.message.reply_text(
                    "Wallet decode error: unsupported key format (need 64-byte secret or 32-byte seed)."
                )
                return

            u.message.reply_text(
                f"🔑 Wallet: `{pub}`\n(RPC: {Cfg.RPC_URL})",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            u.message.reply_text(f"Wallet decode error: {e}")

    def _portfolio(self, u: Update, c: CallbackContext):
        u.message.reply_text(self.ledger.portfolio_text())

    def _trades(self, u: Update, c: CallbackContext):
        path = os.path.join(Cfg.DATA_DIR, "paper_trades.csv")
        u.message.reply_text(f"📄 Trades CSV: {path}")

    def _autopaper(self, u: Update, c: CallbackContext):
        args = c.args or []
        if not args:
            u.message.reply_text(
                f"PAPER_AUTOTRADE is {'ON' if Cfg.PAPER_AUTOTRADE else 'OFF'}.\n"
                f"Use /autopaper on or /autopaper off."
            )
            return
        v = args[0].lower()
        if v in ("on", "true", "1"):
            Cfg.PAPER_AUTOTRADE = True
            u.message.reply_text("✅ Paper auto-trading enabled.")
        elif v in ("off", "false", "0"):
            Cfg.PAPER_AUTOTRADE = False
            u.message.reply_text("⛔️ Paper auto-trading disabled.")
        else:
            u.message.reply_text("Usage: /autopaper on|off")

    def _export(self, u: Update, c: CallbackContext):
        try:
            tokens_glob = [
                p for p in os.listdir(Cfg.DATA_DIR) if p.startswith("tokens_")
            ]
            trades_glob = [
                p for p in os.listdir(Cfg.DATA_DIR) if p.startswith("trades_")
            ]
            tokens_glob.sort()
            trades_glob.sort()
            tokens = tokens_glob[-1] if tokens_glob else "(none)"
            trades = trades_glob[-1] if trades_glob else "(none)"
            u.message.reply_text(
                f"📦 Latest CSVs:\n"
                f"- Tokens: {os.path.join(Cfg.DATA_DIR, tokens)}\n"
                f"- Trades: {os.path.join(Cfg.DATA_DIR, trades)}"
            )
        except Exception as e:
            u.message.reply_text(f"Export error: {e}")

    def _backtest(self, u: Update, c: CallbackContext):
        try:
            hours = int(c.args[0]) if c.args else 24
        except Exception:
            hours = 24
        u.message.reply_text(f"🧪 Running Dex backtest for ~{hours}h…")
        try:
            # If run_backtest is sync, this is fine; if it's async, adapt accordingly.
            res = asyncio.run(run_backtest(hours=hours))
            picked = res.get("picked") or []
            tokens_block = _fmt_tokens(picked, max_items=15)
            msg = (
                "📊 Backtest (Dex + trailing)\n"
                f"- Window: ~{hours}h (horizon={res.get('horizon')})\n"
                f"- Tokens tested: {res.get('tokens_tested')}\n"
                f"- Entries: {res.get('entries')}\n"
                f"- Winners/Losers: {res.get('wins')}/{res.get('losses')}\n"
                f"- Avg PnL: {res.get('avg_pnl')}%\n"
                f"- Top5 Avg: {res.get('top5_avg')}%\n"
                f"- Tokens CSV: {res.get('tokens_csv')}\n"
                f"- Trades CSV: {res.get('trades_csv')}\n\n"
                f"🧾 Picked tokens:\n{tokens_block}"
            )
            u.message.reply_text(msg)
        except Exception as e:
            logger.warning(f"/backtest error: {e}")
            u.message.reply_text(f"⚠️ Backtest failed: {e}")

    def _ping(self, u: Update, c: CallbackContext):
        u.message.reply_text("🏓 pong")

    def _unknown(self, u: Update, c: CallbackContext):
        u.message.reply_text("🤖 Unknown command. Try /help")

    def _on_error(self, update: Optional[Update], context: CallbackContext):
        logger.warning(f"[TG] error: {context.error}")
