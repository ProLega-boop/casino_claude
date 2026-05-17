"""game_logic.py — Provably-fair winner selection for TON Rolls."""
from __future__ import annotations

import hashlib
import os
from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 28
CENT = Decimal("0.01")


# ── Seed / Hash ────────────────────────────────────────────────────────────

def generate_seed() -> str:
    """Generate a cryptographically random 32-byte seed as hex string."""
    return os.urandom(32).hex()


def seed_to_hash(seed_hex: str) -> str:
    """SHA-256(seed_bytes) → hex string (commitment published before round)."""
    return hashlib.sha256(bytes.fromhex(seed_hex)).hexdigest()


# ── Winner selection ───────────────────────────────────────────────────────

def _build_ticket_list(bets: dict) -> list[tuple[str, int]]:
    """
    Sort bets by user_id (deterministic order) and convert TON amounts to
    integer ticket counts (1 ticket = 0.01 TON).
    """
    rows = []
    for uid, data in sorted(bets.items(), key=lambda x: x[0]):
        amt = data["amount"] if isinstance(data, dict) else data
        cents = int(
            (Decimal(str(amt)) / CENT).to_integral_value(ROUND_HALF_UP)
        )
        if cents > 0:
            rows.append((uid, cents))
    return rows


def pick_winner(bets: dict, seed_hex: str) -> tuple[str, int, int]:
    """
    Returns (winner_user_id_str, winning_ticket_index, total_tickets).
    Algorithm (v2, identical to verify_v2.py):
        rand_int = big-endian int from seed bytes
        ticket   = rand_int % total_tickets
    """
    ticket_list = _build_ticket_list(bets)
    total = sum(c for _, c in ticket_list)
    if total == 0:
        raise ValueError("No tickets in this round")

    rand_int = int.from_bytes(bytes.fromhex(seed_hex), "big")
    ticket = rand_int % total

    acc = 0
    for uid, cnt in ticket_list:
        acc += cnt
        if ticket < acc:
            return uid, ticket, total

    raise RuntimeError("Winner selection failed (internal error)")


# ── Prize calculation ──────────────────────────────────────────────────────

def calc_prize(winner_bet: float, total_pot: float, commission: float) -> tuple[float, float, float]:
    """
    Returns (prize, multiplier, commission_taken).
    commission is a fraction, e.g. 0.20 for 20 %.
    """
    losers_total   = total_pot - winner_bet
    comm_taken     = losers_total * commission
    prize          = winner_bet + losers_total - comm_taken
    multiplier     = round(prize / winner_bet, 4) if winner_bet > 0 else 1.0
    return round(prize, 6), multiplier, round(comm_taken, 6)


# ── Legit-check verification (mirrors verify_v2.py logic) ─────────────────

def verify_round(seed_hex: str, hash_hex: str, bets: dict) -> dict:
    """
    Verify that seed matches hash, then recompute winner.
    Returns a result dict.
    """
    computed = seed_to_hash(seed_hex)
    if computed.lower() != hash_hex.lower():
        return {"ok": False, "error": "HASH mismatch — seed has been tampered with"}

    try:
        winner_uid, ticket, total = pick_winner(bets, seed_hex)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok":         True,
        "winner_uid": winner_uid,
        "ticket":     ticket,
        "total":      total,
    }
