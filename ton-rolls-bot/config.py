BOT_TOKEN   = "7867459328:AAHWCEGPiYz6OZneV-BFyTeZF-I-S9Iyuj0"

# URL of your Replit server — edit after deploy
# Example: "https://ton-rolls.username.repl.co"
SERVER_URL  = "https://YOUR_REPLIT_URL"
WEBAPP_URL  = SERVER_URL + "/webapp"

# Game
ROUND_DURATION   = 20    # seconds of betting
FREEZE_DURATION  = 1     # freeze before spin
COMMISSION       = 0.20  # 20 % taken from losers
REFERRAL_SHARE   = 0.10  # 10 % of commission → referrer
START_BALANCE    = 100.0

PROMO_CODES: dict[str, float] = {
    "testbot": 100.0,
}

COLORS = [
    "#39FF14", "#FF3131", "#3D9EFF", "#FF7A00",
    "#BF40FF", "#00E5FF", "#FFE000", "#FF1493",
]

PORT = 8080   # Replit exposes this port
