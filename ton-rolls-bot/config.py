BOT_TOKEN    = "8705993863:AAEZzInwvxxF_PTMl1pgeX3RBg0GVrssPcw"
BOT_USERNAME = "RoyalDuel_bot"

# Set this to your Replit URL after first deploy
SERVER_URL  = "https://YOUR_REPLIT_URL"
WEBAPP_URL  = SERVER_URL + "/webapp"

# PvP Wheel
ROUND_DURATION   = 20
FREEZE_DURATION  = 1

# Commissions (can be overridden at runtime via admin panel — stored in DB)
COMMISSION_PVP   = 0.20   # 20% taken from losers
COMMISSION_LOBBY = 0.10   # 10% taken from winner in Lobby games
REFERRAL_SHARE   = 0.10   # 10% of commission → referral balance
REFERRAL_MIN_WITHDRAW = 0.10

START_BALANCE = 100.0

PROMO_CODES: dict[str, float] = {}  # e.g. {"promo1": 50.0}

COLORS = [
    "#39FF14", "#FF3131", "#3D9EFF", "#FF7A00",
    "#BF40FF", "#00E5FF", "#FFE000", "#FF1493",
]

PORT = 8080

# Admin
ADMIN_ID = 5849412071
