from __future__ import annotations
import random
from ..config import Cfg
from ..log import logger

VIBE = {
    "entry":[
        "We’re in! Small step now—let’s see if this turns into a sprint 🚀",
        "Position opened, rules tight, eyes wide. Let’s ride.",
        "Bag planted, let’s see if it blossoms 🌱🚀"
    ],
    "hold":[
        "Momentum alive, no reason to jump ship—holding firm.",
        "Buy pressure strong, trend intact. Keep it running.",
        "No early exits—this bag is breathing fire 🔥"
    ],
    "trim":[
        "Trimmed a little—risk managed, upside alive.",
        "Locked some profit, runner still running.",
        "Laddered profits—chef’s kiss 🍲"
    ],
    "giant":[
        "⚡ Giant Mode on—floors locked, wide trails. No early exits here.",
        "This one smells like a big bag—letting it breathe."
    ],
    "rug":[
        "⚠️ Rug signals—cutting fast. Capital safety first.",
        "LP looks shady—full exit. Clean out."
    ],
    "skip":[
        "Fees too high (>3%). Smart pass.",
        "Slippage ugly—standing down."
    ],
    "recap":[
        "Wrap-up: steady gains, equity climbing, floor safe.",
        "Day done. Risk managed, profits banked."
    ]
}

async def send(chat_id, bot, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"notify error: {e}")

def vibe(key: str) -> str:
    return random.choice(VIBE.get(key, [""])) if key in VIBE else ""

async def send_daily_report(bot):
    msg = "Daily Report: (stub) Equity up, rugs avoided, fees contained. Use /portfolio or /results."
    await send(Cfg.ADMIN_CHAT_ID, bot, msg)
