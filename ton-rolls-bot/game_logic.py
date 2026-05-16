import random
from config import COMMISSION, REFERRAL_SHARE
from database import (
    get_game, set_game_status, save_history,
    update_balance, get_referrer, get_user
)

def pick_winner(bets: dict) -> str:
    """Weighted random pick. bets = {user_id: {amount, username}}"""
    total = sum(v["amount"] for v in bets.values())
    r = random.uniform(0, total)
    cumulative = 0
    for uid, data in bets.items():
        cumulative += data["amount"]
        if r <= cumulative:
            return uid
    return list(bets.keys())[-1]  # fallback

def resolve_game(game_id: int) -> dict:
    """
    Resolves a game: picks winner, distributes funds, handles referrals.
    Returns result dict for the bot to announce.
    """
    game = get_game(game_id)
    if not game or game["status"] != "spinning":
        return None

    bets = game["bets"]
    if not bets:
        set_game_status(game_id, "ended")
        return None

    total_pot = game["total_pot"]

    # Single player → refund
    if len(bets) == 1:
        uid = list(bets.keys())[0]
        update_balance(int(uid), bets[uid]["amount"])
        set_game_status(game_id, "ended")
        return {"solo": True, "user_id": int(uid)}

    winner_uid = pick_winner(bets)
    winner_data = bets[winner_uid]
    winner_bet = winner_data["amount"]
    winner_id = int(winner_uid)

    losers_total = total_pot - winner_bet
    commission = losers_total * COMMISSION
    winner_prize = winner_bet + losers_total - commission

    # Pay winner
    update_balance(winner_id, winner_prize)

    # Referral payout
    referrer_id = get_referrer(winner_id)
    if referrer_id:
        ref_bonus = commission * REFERRAL_SHARE
        update_balance(referrer_id, ref_bonus)

    # Chance & multiplier
    chance = round((winner_bet / total_pot) * 100, 2)
    multiplier = round(winner_prize / winner_bet, 2) if winner_bet > 0 else 1.0

    # Mark game done
    set_game_status(game_id, "ended", winner_id=winner_id)

    # Save to history
    save_history(
        game_id=game_id,
        winner_id=winner_id,
        winner_name=winner_data["username"],
        pot=total_pot,
        multiplier=multiplier,
        chance=chance,
    )

    return {
        "solo": False,
        "winner_id": winner_id,
        "winner_name": winner_data["username"],
        "winner_bet": winner_bet,
        "prize": round(winner_prize, 4),
        "pot": total_pot,
        "multiplier": multiplier,
        "chance": chance,
        "bets": bets,
    }
