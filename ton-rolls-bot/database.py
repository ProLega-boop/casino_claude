"""database.py — SQLite persistence layer for TON Rolls."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("rolls.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db() -> None:
    with _conn() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT    DEFAULT '',
            first_name   TEXT    DEFAULT '',
            balance      REAL    DEFAULT 100.0,
            referrer_id  INTEGER,
            used_promos  TEXT    DEFAULT '[]',
            games_played INTEGER DEFAULT 0,
            games_won    INTEGER DEFAULT 0,
            created_at   TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS games (
            game_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            status     TEXT DEFAULT 'waiting',
            winner_id  INTEGER,
            total_pot  REAL DEFAULT 0,
            bets       TEXT DEFAULT '{}',
            seed       TEXT DEFAULT '',
            seed_hash  TEXT DEFAULT '',
            started_at TEXT,
            ended_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id      INTEGER,
            winner_id    INTEGER,
            winner_name  TEXT,
            pot          REAL,
            winner_bet   REAL,
            multiplier   REAL,
            chance       REAL,
            seed         TEXT,
            seed_hash    TEXT,
            bets_json    TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        """)


# ── Users ──────────────────────────────────────────────────────────────────

def get_user(user_id: int) -> dict | None:
    with _conn() as db:
        row = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def upsert_user(user_id: int, username: str, first_name: str,
                referrer_id: int | None = None) -> dict:
    with _conn() as db:
        db.execute(
            """INSERT INTO users (user_id, username, first_name, referrer_id)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username   = excluded.username,
                   first_name = excluded.first_name""",
            (user_id, username or "", first_name or "", referrer_id),
        )
    return get_user(user_id)


def update_balance(user_id: int, delta: float) -> None:
    with _conn() as db:
        db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?",
                   (delta, user_id))


def increment_stats(user_id: int, won: bool) -> None:
    with _conn() as db:
        db.execute(
            "UPDATE users SET games_played = games_played + 1,"
            " games_won = games_won + ? WHERE user_id=?",
            (1 if won else 0, user_id),
        )


def mark_promo(user_id: int, code: str) -> None:
    u = get_user(user_id)
    if not u:
        return
    used = json.loads(u["used_promos"])
    used.append(code)
    with _conn() as db:
        db.execute("UPDATE users SET used_promos=? WHERE user_id=?",
                   (json.dumps(used), user_id))


def promo_used(user_id: int, code: str) -> bool:
    u = get_user(user_id)
    return code in json.loads(u["used_promos"]) if u else False


def get_referrer_id(user_id: int) -> int | None:
    u = get_user(user_id)
    return u["referrer_id"] if u else None


def get_referrals(referrer_id: int) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT user_id, username, first_name FROM users WHERE referrer_id=?",
            (referrer_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Active game ────────────────────────────────────────────────────────────

def get_active_game() -> dict | None:
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM games WHERE status IN ('waiting','betting') "
            "ORDER BY game_id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["bets"] = json.loads(d["bets"])
    return d


def create_game(seed: str, seed_hash: str) -> int:
    with _conn() as db:
        cur = db.execute(
            "INSERT INTO games (status, bets, seed, seed_hash) VALUES ('waiting','{}',?,?)",
            (seed, seed_hash),
        )
        return cur.lastrowid


def get_game(game_id: int) -> dict | None:
    with _conn() as db:
        row = db.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["bets"] = json.loads(d["bets"])
    return d


def add_bet_to_game(game_id: int, user_id: int, amount: float,
                    username: str, first_name: str) -> None:
    with _conn() as db:
        row = db.execute(
            "SELECT bets, total_pot FROM games WHERE game_id=?", (game_id,)
        ).fetchone()
        bets: dict = json.loads(row["bets"])
        uid = str(user_id)
        if uid in bets:
            bets[uid]["amount"] = round(bets[uid]["amount"] + amount, 6)
        else:
            bets[uid] = {
                "amount":     amount,
                "username":   username or "",
                "first_name": first_name or "",
            }
        db.execute(
            "UPDATE games SET bets=?, total_pot=total_pot+? WHERE game_id=?",
            (json.dumps(bets), amount, game_id),
        )


def set_game_status(game_id: int, status: str,
                    winner_id: int | None = None) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as db:
        if status == "betting":
            db.execute(
                "UPDATE games SET status=?, started_at=? WHERE game_id=?",
                (status, now, game_id),
            )
        elif status == "ended":
            db.execute(
                "UPDATE games SET status=?, winner_id=?, ended_at=? WHERE game_id=?",
                (status, winner_id, now, game_id),
            )
        else:
            db.execute(
                "UPDATE games SET status=? WHERE game_id=?", (status, game_id)
            )


# ── History ────────────────────────────────────────────────────────────────

def save_history(
    game_id: int, winner_id: int, winner_name: str,
    pot: float, winner_bet: float, multiplier: float, chance: float,
    seed: str, seed_hash: str, bets: dict,
) -> None:
    with _conn() as db:
        db.execute(
            """INSERT INTO history
               (game_id,winner_id,winner_name,pot,winner_bet,
                multiplier,chance,seed,seed_hash,bets_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (game_id, winner_id, winner_name, pot, winner_bet,
             multiplier, chance, seed, seed_hash, json.dumps(bets)),
        )


def get_history_all(limit: int = 50) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["bets"] = json.loads(d["bets_json"])
        result.append(d)
    return result


def get_history_lucky(limit: int = 20) -> list[dict]:
    """Top 20 lowest-chance wins."""
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM history ORDER BY chance ASC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["bets"] = json.loads(d["bets_json"])
        result.append(d)
    return result


def get_history_big(limit: int = 20) -> list[dict]:
    """Top 20 biggest pots."""
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM history ORDER BY pot DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["bets"] = json.loads(d["bets_json"])
        result.append(d)
    return result


def get_history_entry(game_id: int) -> dict | None:
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM history WHERE game_id=?", (game_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["bets"] = json.loads(d["bets_json"])
    return d
