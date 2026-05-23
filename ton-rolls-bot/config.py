"""config.py — RoyalDuel configuration.
NEVER hardcode secrets here. Use Replit Secrets (or .env).
"""
import os

# ── Security ──────────────────────────────────────────────────────────────
# Set BOT_TOKEN in Replit Secrets (not in code!)
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "RoyalDuel_bot")

if not BOT_TOKEN:
    import sys
    print("CRITICAL: BOT_TOKEN not set. Add it to Replit Secrets.", file=sys.stderr)

# ── URLs ──────────────────────────────────────────────────────────────────
SERVER_URL = os.environ.get("SERVER_URL", "https://YOUR_REPLIT_URL")
WEBAPP_URL = SERVER_URL + "/webapp"

# ── PvP timing ────────────────────────────────────────────────────────────
ROUND_DURATION  = 20
FREEZE_DURATION = 1

# ── Default commissions (overridable via admin panel in DB) ───────────────
COMMISSION_PVP        = 0.20   # 20% from losers
COMMISSION_LOBBY      = 0.10   # 10% from winner
REFERRAL_SHARE        = 0.10   # 10% of commission → referrer
REFERRAL_MIN_WITHDRAW = 0.10

START_BALANCE = 100.0
PROMO_CODES: dict = {}

COLORS = [
    "#39FF14", "#FF3131", "#3D9EFF", "#FF7A00",
    "#BF40FF", "#00E5FF", "#FFE000", "#FF1493",
]

PORT     = 8080
ADMIN_ID = 5849412071

# ── Telegram initData verification ────────────────────────────────────────
VERIFY_INIT_DATA = os.environ.get("VERIFY_INIT_DATA", "true").lower() != "false"
