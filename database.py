import sqlite3
import json
from datetime import datetime

DB_PATH = "rolls.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            avatar_url  TEXT,
            balance     REAL DEFAULT 100.0,
            referrer_id INTEGER DEFAULT NULL,
            used_promos TEXT DEFAULT '[]',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            status      TEXT DEFAULT 'betting',
            winner_id   INTEGER DEFAULT NULL,
            total_pot   REAL DEFAULT 0.0,
            bets        TEXT DEFAULT '{}',
            created_at  TEXT DEFAULT (datetime('now')),
            ended_at    TEXT DEFAULT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id     INTEGER,
            winner_id   INTEGER,
            winner_name TEXT,
            pot         REAL,
            multiplier  REAL,
            chance      REAL,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()

# ── Users ──────────────────────────────────────────────

def get_user(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def create_user(user_id: int, username: str, first_name: str, referrer_id: int = None):
    from config import START_BALANCE
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, balance, referrer_id) VALUES (?,?,?,?,?)",
        (user_id, username or "", first_name or "", START_BALANCE, referrer_id)
    )
    conn.commit()
    conn.close()

def update_balance(user_id: int, delta: float):
    conn = get_conn()
    conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    conn.close()

def set_balance(user_id: int, amount: float):
    conn = get_conn()
    conn.execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()

def get_referrer(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT referrer_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row["referrer_id"] if row else None

def mark_promo_used(user_id: int, code: str):
    conn = get_conn()
    row = conn.execute("SELECT used_promos FROM users WHERE user_id=?", (user_id,)).fetchone()
    used = json.loads(row["used_promos"]) if row else []
    used.append(code)
    conn.execute("UPDATE users SET used_promos=? WHERE user_id=?", (json.dumps(used), user_id))
    conn.commit()
    conn.close()

def promo_used(user_id: int, code: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT used_promos FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return False
    return code in json.loads(row["used_promos"])

# ── Games ──────────────────────────────────────────────

def create_game() -> int:
    conn = get_conn()
    cur = conn.execute("INSERT INTO games (status, bets) VALUES ('betting', '{}')")
    game_id = cur.lastrowid
    conn.commit()
    conn.close()
    return game_id

def get_game(game_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["bets"] = json.loads(d["bets"])
    return d

def get_active_game():
    conn = get_conn()
    row = conn.execute("SELECT * FROM games WHERE status='betting' ORDER BY game_id DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["bets"] = json.loads(d["bets"])
    return d

def add_bet(game_id: int, user_id: int, amount: float, username: str):
    conn = get_conn()
    row = conn.execute("SELECT bets, total_pot FROM games WHERE game_id=?", (game_id,)).fetchone()
    bets = json.loads(row["bets"])
    uid = str(user_id)
    if uid in bets:
        bets[uid]["amount"] += amount
    else:
        bets[uid] = {"amount": amount, "username": username}
    total = row["total_pot"] + amount
    conn.execute("UPDATE games SET bets=?, total_pot=? WHERE game_id=?",
                 (json.dumps(bets), total, game_id))
    conn.commit()
    conn.close()

def set_game_status(game_id: int, status: str, winner_id: int = None):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    if winner_id:
        conn.execute("UPDATE games SET status=?, winner_id=?, ended_at=? WHERE game_id=?",
                     (status, winner_id, now, game_id))
    else:
        conn.execute("UPDATE games SET status=? WHERE game_id=?", (status, game_id))
    conn.commit()
    conn.close()

def save_history(game_id, winner_id, winner_name, pot, multiplier, chance):
    conn = get_conn()
    conn.execute(
        "INSERT INTO history (game_id, winner_id, winner_name, pot, multiplier, chance) VALUES (?,?,?,?,?,?)",
        (game_id, winner_id, winner_name, pot, multiplier, chance)
    )
    conn.commit()
    conn.close()

def get_last_games(limit=10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_top_game():
    conn = get_conn()
    row = conn.execute("SELECT * FROM history ORDER BY pot DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None
