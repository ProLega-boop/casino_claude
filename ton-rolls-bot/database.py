"""database.py — SQLite persistence for TON Rolls / RoyalDuel."""
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
            user_id          INTEGER PRIMARY KEY,
            username         TEXT    DEFAULT '',
            first_name       TEXT    DEFAULT '',
            balance          REAL    DEFAULT 100.0,
            ref_balance      REAL    DEFAULT 0.0,
            referrer_id      INTEGER,
            used_promos      TEXT    DEFAULT '[]',
            games_played     INTEGER DEFAULT 0,
            games_won        INTEGER DEFAULT 0,
            total_ref_earned REAL    DEFAULT 0.0,
            created_at       TEXT    DEFAULT (datetime('now'))
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

        CREATE TABLE IF NOT EXISTS pvp_history (
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

        CREATE TABLE IF NOT EXISTS lobby_rooms (
            room_id      TEXT PRIMARY KEY,
            creator_id   INTEGER,
            game_type    TEXT,
            bet_amount   REAL,
            max_players  INTEGER,
            is_private   INTEGER DEFAULT 0,
            private_key  TEXT DEFAULT '',
            status       TEXT DEFAULT 'waiting',
            players      TEXT DEFAULT '[]',
            seed         TEXT DEFAULT '',
            seed_hash    TEXT DEFAULT '',
            result       TEXT DEFAULT '{}',
            created_at   TEXT DEFAULT (datetime('now')),
            ended_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS lobby_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id     TEXT,
            game_type   TEXT,
            winner_id   INTEGER,
            winner_name TEXT,
            pot         REAL,
            players     TEXT,
            seed        TEXT,
            seed_hash   TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
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
                   (round(delta, 6), user_id))


def add_ref_balance(user_id: int, amount: float) -> None:
    """Add to referral pending balance (not main balance)."""
    with _conn() as db:
        db.execute(
            "UPDATE users SET ref_balance = ref_balance + ?, "
            "total_ref_earned = total_ref_earned + ? WHERE user_id=?",
            (round(amount, 6), round(amount, 6), user_id),
        )


def claim_ref_balance(user_id: int) -> float:
    """Move referral balance → main balance. Returns amount claimed."""
    u = get_user(user_id)
    if not u or u["ref_balance"] < 0.001:
        return 0.0
    amount = round(u["ref_balance"], 6)
    with _conn() as db:
        db.execute(
            "UPDATE users SET balance = balance + ?, ref_balance = 0 WHERE user_id=?",
            (amount, user_id),
        )
    return amount


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
    if code not in used:
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
            "SELECT u.user_id, u.username, u.first_name, "
            "COALESCE(u.total_ref_earned, 0) as earned "
            "FROM users u WHERE u.referrer_id=?",
            (referrer_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── PvP Wheel ──────────────────────────────────────────────────────────────

def get_active_pvp_game() -> dict | None:
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


def create_pvp_game(seed: str, seed_hash: str) -> int:
    with _conn() as db:
        cur = db.execute(
            "INSERT INTO games (status,bets,seed,seed_hash) VALUES ('waiting','{}',?,?)",
            (seed, seed_hash),
        )
        return cur.lastrowid


def get_pvp_game(game_id: int) -> dict | None:
    with _conn() as db:
        row = db.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["bets"] = json.loads(d["bets"])
    return d


def add_pvp_bet(game_id: int, user_id: int, amount: float,
                username: str, first_name: str) -> None:
    with _conn() as db:
        row = db.execute("SELECT bets,total_pot FROM games WHERE game_id=?",
                         (game_id,)).fetchone()
        bets = json.loads(row["bets"])
        uid  = str(user_id)
        if uid in bets:
            bets[uid]["amount"] = round(bets[uid]["amount"] + amount, 6)
        else:
            bets[uid] = {"amount": amount, "username": username or "",
                         "first_name": first_name or ""}
        db.execute(
            "UPDATE games SET bets=?,total_pot=total_pot+? WHERE game_id=?",
            (json.dumps(bets), amount, game_id),
        )


def set_pvp_status(game_id: int, status: str, winner_id: int | None = None) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as db:
        if status == "betting":
            db.execute("UPDATE games SET status=?,started_at=? WHERE game_id=?",
                       (status, now, game_id))
        elif status == "ended":
            db.execute("UPDATE games SET status=?,winner_id=?,ended_at=? WHERE game_id=?",
                       (status, winner_id, now, game_id))
        else:
            db.execute("UPDATE games SET status=? WHERE game_id=?", (status, game_id))


def save_pvp_history(game_id, winner_id, winner_name, pot, winner_bet,
                     multiplier, chance, seed, seed_hash, bets) -> None:
    with _conn() as db:
        db.execute(
            """INSERT INTO pvp_history
               (game_id,winner_id,winner_name,pot,winner_bet,
                multiplier,chance,seed,seed_hash,bets_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (game_id, winner_id, winner_name, pot, winner_bet,
             multiplier, chance, seed, seed_hash, json.dumps(bets)),
        )


def get_pvp_history_all(limit=50) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM pvp_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_ph(r) for r in rows]


def get_pvp_history_lucky(limit=20) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM pvp_history ORDER BY chance ASC LIMIT ?", (limit,)
        ).fetchall()
    return [_ph(r) for r in rows]


def get_pvp_history_big(limit=20) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM pvp_history ORDER BY pot DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_ph(r) for r in rows]


def get_pvp_history_entry(game_id: int) -> dict | None:
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM pvp_history WHERE game_id=?", (game_id,)
        ).fetchone()
    return _ph(row) if row else None


def _ph(row) -> dict:
    d = dict(row)
    d["bets"] = json.loads(d["bets_json"])
    return d


# ── Lobby Rooms ────────────────────────────────────────────────────────────

def create_room(room_id: str, creator_id: int, game_type: str,
                bet_amount: float, max_players: int,
                is_private: bool, private_key: str,
                seed: str, seed_hash: str) -> None:
    with _conn() as db:
        db.execute(
            """INSERT INTO lobby_rooms
               (room_id,creator_id,game_type,bet_amount,max_players,
                is_private,private_key,seed,seed_hash,players)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (room_id, creator_id, game_type, bet_amount, max_players,
             1 if is_private else 0, private_key, seed, seed_hash, "[]"),
        )


def get_room(room_id: str) -> dict | None:
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM lobby_rooms WHERE room_id=?", (room_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["players"]    = json.loads(d["players"])
    d["result"]     = json.loads(d["result"])
    return d


def get_public_rooms() -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM lobby_rooms WHERE is_private=0 AND status='waiting' "
            "ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["players"] = json.loads(d["players"])
        d["result"]  = json.loads(d["result"])
        result.append(d)
    return result


def room_add_player(room_id: str, user_id: int, username: str,
                    first_name: str) -> None:
    with _conn() as db:
        row = db.execute("SELECT players FROM lobby_rooms WHERE room_id=?",
                         (room_id,)).fetchone()
        players = json.loads(row["players"])
        if not any(p["user_id"] == user_id for p in players):
            players.append({"user_id": user_id,
                            "username": username or "",
                            "first_name": first_name or ""})
        db.execute("UPDATE lobby_rooms SET players=? WHERE room_id=?",
                   (json.dumps(players), room_id))


def set_room_status(room_id: str, status: str, result: dict | None = None) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as db:
        if status == "ended":
            db.execute(
                "UPDATE lobby_rooms SET status=?,result=?,ended_at=? WHERE room_id=?",
                (status, json.dumps(result or {}), now, room_id),
            )
        else:
            db.execute("UPDATE lobby_rooms SET status=? WHERE room_id=?",
                       (status, room_id))


def save_lobby_history(room_id, game_type, winner_id, winner_name,
                       pot, players, seed, seed_hash) -> None:
    with _conn() as db:
        db.execute(
            """INSERT INTO lobby_history
               (room_id,game_type,winner_id,winner_name,pot,players,seed,seed_hash)
               VALUES (?,?,?,?,?,?,?,?)""",
            (room_id, game_type, winner_id, winner_name, pot,
             json.dumps(players), seed, seed_hash),
        )


def get_lobby_history(limit=50) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM lobby_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["players"] = json.loads(d["players"])
        result.append(d)
    return result


def get_lobby_history_entry(room_id: str) -> dict | None:
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM lobby_history WHERE room_id=?", (room_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["players"] = json.loads(d["players"])
    return d
