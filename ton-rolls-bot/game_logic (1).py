"""game_logic.py — Provably-fair logic for all game types."""
from __future__ import annotations

import hashlib
import os
import random
from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 28
CENT = Decimal("0.01")


# ── Seed / Hash ────────────────────────────────────────────────────────────

def generate_seed() -> str:
    return os.urandom(32).hex()


def seed_to_hash(seed_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(seed_hex)).hexdigest()


# ── PvP Wheel winner ───────────────────────────────────────────────────────

def _build_tickets(bets: dict) -> list[tuple[str, int]]:
    rows = []
    for uid, data in sorted(bets.items()):
        amt  = data["amount"] if isinstance(data, dict) else data
        cts  = int((Decimal(str(amt)) / CENT).to_integral_value(ROUND_HALF_UP))
        if cts > 0:
            rows.append((uid, cts))
    return rows


def pick_wheel_winner(bets: dict, seed_hex: str) -> tuple[str, int, int]:
    """Returns (winner_uid, ticket_index, total_tickets)."""
    tickets = _build_tickets(bets)
    total   = sum(c for _, c in tickets)
    if total == 0:
        raise ValueError("No tickets")
    rand_int = int.from_bytes(bytes.fromhex(seed_hex), "big")
    ticket   = rand_int % total
    acc = 0
    for uid, cnt in tickets:
        acc += cnt
        if ticket < acc:
            return uid, ticket, total
    raise RuntimeError("pick_wheel_winner failed")


def calc_wheel_prize(winner_bet: float, total_pot: float,
                     commission: float) -> tuple[float, float, float]:
    losers  = total_pot - winner_bet
    comm    = losers * commission
    prize   = winner_bet + losers - comm
    mult    = round(prize / winner_bet, 4) if winner_bet else 1.0
    return round(prize, 6), mult, round(comm, 6)


# ── Dice ───────────────────────────────────────────────────────────────────

def roll_dice_for_players(player_ids: list, seed_hex: str,
                           roll_index: int = 0) -> dict[str, int]:
    """
    Deterministic dice rolls.
    roll_index allows re-rolls on tie without changing seed.
    Returns {user_id_str: face_value 1-6}
    """
    results = {}
    seed_bytes = bytes.fromhex(seed_hex)
    for i, uid in enumerate(sorted(str(p) for p in player_ids)):
        h = hashlib.sha256(seed_bytes + i.to_bytes(2, "big") +
                           roll_index.to_bytes(2, "big")).digest()
        results[uid] = (int.from_bytes(h[:4], "big") % 6) + 1
    return results


def resolve_dice(rolls: dict[str, int]) -> tuple[list[str], int]:
    """
    Returns (winners_uid_list, max_value).
    Multiple winners = tie → needs re-roll.
    """
    max_val = max(rolls.values())
    winners = [uid for uid, v in rolls.items() if v == max_val]
    return winners, max_val


# ── Coin ───────────────────────────────────────────────────────────────────

def flip_coin(seed_hex: str) -> str:
    """Returns 'heads' (creator) or 'tails' (joiner)."""
    val = int.from_bytes(bytes.fromhex(seed_hex)[:4], "big")
    return "heads" if val % 2 == 0 else "tails"


# ── Lobby prize ────────────────────────────────────────────────────────────

def calc_lobby_prize(total_pot: float, commission: float) -> tuple[float, float]:
    """Returns (prize_to_winner, commission_taken)."""
    comm  = total_pot * commission
    prize = total_pot - comm
    return round(prize, 6), round(comm, 6)


# ── Verify ─────────────────────────────────────────────────────────────────

def verify_wheel(seed_hex: str, hash_hex: str, bets: dict) -> dict:
    if seed_to_hash(seed_hex).lower() != hash_hex.lower():
        return {"ok": False, "error": "HASH mismatch"}
    try:
        uid, ticket, total = pick_wheel_winner(bets, seed_hex)
        return {"ok": True, "winner_uid": uid, "ticket": ticket, "total": total}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def verify_lobby(seed_hex: str, hash_hex: str,
                 game_type: str, players: list) -> dict:
    if seed_to_hash(seed_hex).lower() != hash_hex.lower():
        return {"ok": False, "error": "HASH mismatch"}
    if game_type == "coin":
        side = flip_coin(seed_hex)
        return {"ok": True, "result": side}
    if game_type == "dice":
        player_ids = [str(p["user_id"]) for p in players]
        rolls = roll_dice_for_players(player_ids, seed_hex, 0)
        winners, max_val = resolve_dice(rolls)
        return {"ok": True, "rolls": rolls, "winners": winners, "max": max_val}
    return {"ok": False, "error": "Unknown game type"}
