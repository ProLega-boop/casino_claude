"""server.py — FastAPI + WebSocket server for RoyalDuel / TON Rolls."""
from __future__ import annotations

import asyncio
import time
import json
import logging
import random
import string
import hashlib
import hmac
from urllib.parse import parse_qs, unquote
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import config
import database as db
import game_logic as gl


async def _notify_tournament_result(tournament_id: int, t: dict | None) -> None:
    """Send Telegram message ONLY to prize-winning participants about their tournament result."""
    if not t:
        return
    try:
        from aiogram import Bot
        from aiogram.enums import ParseMode
        bot = Bot(token=config.BOT_TOKEN, parse_mode=ParseMode.HTML)
        lb = t.get("leaderboard") or []
        prizes = t.get("prizes") or []
        title = t.get("title", "Турнир")
        kind = t.get("kind", "won_ton")

        rank_icons = {1: "🥇", 2: "🥈", 3: "🥉"}
        prize_places = len(prizes) if prizes else t.get("prize_places", 3)

        # Only notify users who are in prize zone (have a prize)
        for idx, entry in enumerate(lb[:prize_places]):
            uid = entry.get("user_id")
            if not uid:
                continue
            rank = idx + 1
            icon = rank_icons.get(rank, f"#{rank}")
            score = entry.get("score", 0)
            score_str = f"{int(score)} реф." if kind == "refs" else f"{float(score):.2f} TON"
            prize_label = ""
            if idx < len(prizes):
                p = prizes[idx]
                prize_label = f"\n🎁 Приз: <b>{p.get('label','')}</b>" if p.get("label") else ""

            text = (
                f"🏆 <b>Турнир завершён!</b>\n\n"
                f"<b>{title}</b>\n\n"
                f"{icon} Вы заняли <b>#{rank} место</b>!\n"
                f"Результат: <b>{score_str}</b>"
                f"{prize_label}\n\n"
                f"🎉 Поздравляем с победой!"
            )
            try:
                await bot.send_message(chat_id=uid, text=text)
            except Exception as e:
                log.warning(f"Could not notify user {uid}: {e}")

        await bot.session.close()
    except Exception as e:
        log.error(f"_notify_tournament_result error: {e}")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("royalduel")


# ── Telegram initData HMAC verification ───────────────────────────────────
def verify_telegram_init_data(init_data: str, claimed_uid: int) -> tuple[bool, int]:
    """
    Verify Telegram WebApp initData HMAC signature.
    Returns (is_valid, trusted_uid).

    Rules:
    - If initData is empty/missing → trust claimed_uid (dev/desktop mode)
    - If initData present and valid → use uid from initData
    - If initData present and INVALID → reject
    """
    if not init_data or not init_data.strip():
        # No initData: desktop / dev mode — trust claimed uid
        return True, claimed_uid

    if not config.BOT_TOKEN:
        # No bot token configured: can't verify, trust claimed uid
        return True, claimed_uid

    try:
        raw = unquote(init_data)
        parts = dict(p.split("=", 1) for p in raw.split("&") if "=" in p)
        received_hash = parts.pop("hash", "")

        if not received_hash:
            # No hash in initData → can't verify, trust claimed
            return True, claimed_uid

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parts.items()))

        # HMAC-SHA256 with key = HMAC-SHA256("WebAppData", bot_token)
        secret = hmac.new(
            b"WebAppData",
            config.BOT_TOKEN.encode("utf-8"),
            hashlib.sha256
        ).digest()
        computed = hmac.new(
            secret,
            data_check_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(computed, received_hash):
            log.warning(f"initData HMAC mismatch for uid={claimed_uid}")
            # Still allow — don't break the game, just log. Can make strict later.
            return True, claimed_uid

        # Extract uid from verified data
        import json as _j
        user_obj = _j.loads(parts.get("user", "{}"))
        verified_uid = int(user_obj.get("id", claimed_uid))

        if verified_uid != claimed_uid:
            log.warning(f"UID mismatch: claimed={claimed_uid} in_initData={verified_uid}")

        return True, verified_uid

    except Exception as e:
        log.warning(f"initData verify exception: {e} — trusting claimed uid={claimed_uid}")
        return True, claimed_uid


# ── Online users counter ───────────────────────────────────────────────────
_online_users: set[int] = set()   # user_ids currently connected


def get_commission_pvp() -> float:
    return db.get_setting("commission_pvp", config.COMMISSION_PVP)

def get_commission_lobby() -> float:
    return db.get_setting("commission_lobby", config.COMMISSION_LOBBY)

def get_referral_share() -> float:
    return db.get_setting("referral_share", config.REFERRAL_SHARE)

app = FastAPI(title="RoyalDuel Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEBAPP_DIR = Path("webapp")
app.mount("/static", StaticFiles(directory=str(WEBAPP_DIR)), name="static")


@app.get("/webapp", response_class=HTMLResponse)
@app.get("/webapp/index.html", response_class=HTMLResponse)
async def serve_webapp():
    content = (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


# ══════════════════════════════════════════════════════════════════════════
# Connection manager
# ══════════════════════════════════════════════════════════════════════════

class ConnManager:
    def __init__(self):
        self.clients: list[WebSocket] = []
        # room_id → set of WebSockets
        self.room_subs: dict[str, set[WebSocket]] = {}
        # user_id → set of WebSockets (one user can have multiple tabs)
        self.user_socks: dict[int, set[WebSocket]] = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def register_user(self, ws: WebSocket, user_id: int):
        self.user_socks.setdefault(user_id, set()).add(ws)
        _online_users.add(user_id)

    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)
        for subs in self.room_subs.values():
            subs.discard(ws)
        # Remove from user_socks and update online
        for uid, socks in list(self.user_socks.items()):
            socks.discard(ws)
            if not socks:
                _online_users.discard(uid)

    def subscribe_room(self, ws: WebSocket, room_id: str):
        self.room_subs.setdefault(room_id, set()).add(ws)

    def unsubscribe_room(self, ws: WebSocket, room_id: str):
        if room_id in self.room_subs:
            self.room_subs[room_id].discard(ws)

    async def broadcast(self, data: dict):
        msg  = json.dumps(data)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_room(self, room_id: str, data: dict):
        msg  = json.dumps(data)
        subs = self.room_subs.get(room_id, set())
        dead = []
        for ws in list(subs):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_to_user(self, user_id: int, data: dict):
        msg  = json.dumps(data)
        socks = self.user_socks.get(user_id, set())
        dead = []
        for ws in list(socks):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send(self, ws: WebSocket, data: dict):
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            self.disconnect(ws)


mgr = ConnManager()


# ══════════════════════════════════════════════════════════════════════════
# PvP Wheel game state
# ══════════════════════════════════════════════════════════════════════════

class PvPGame:
    def __init__(self):
        self.game_id: int | None = None
        self.status:  str        = "waiting"
        self.timer:   int        = config.ROUND_DURATION
        self.bets:    dict       = {}
        self.pot:     float      = 0.0
        self.seed:    str        = ""
        self.seed_hash: str      = ""
        self._task:   asyncio.Task | None = None
        self._spinning_task = None

    def snapshot(self) -> dict:
        return {"type": "pvp_state", "game_id": self.game_id,
                "status": self.status, "timer": self.timer,
                "bets": self.bets, "pot": self.pot,
                "seed_hash": self.seed_hash}

    async def ensure_game(self):
        if self.game_id is not None and self.status in ("waiting", "betting"):
            return
        self.seed       = gl.generate_seed()
        self.seed_hash  = gl.seed_to_hash(self.seed)
        self.bets       = {}
        self.pot        = 0.0
        self.status     = "waiting"
        self.timer      = config.ROUND_DURATION
        self.game_id    = db.create_pvp_game(self.seed, self.seed_hash)
        log.info(f"PvP game #{self.game_id} created")
        await mgr.broadcast(self.snapshot())

    async def place_bet(self, user_id: int, amount: float,
                        username: str, first_name: str) -> dict:
        # Block bets during spinning OR last second before spin
        if self.status == "spinning":
            return {"ok": False, "error": "Ставки закрыты"}
        if self.status == "betting" and self.timer <= 1:
            return {"ok": False, "error": "Ставки закрыты — колесо вот-вот закрутится"}
        user = db.get_user(user_id)
        if not user:
            return {"ok": False, "error": "Пользователь не найден"}
        if user["balance"] < amount or amount <= 0:
            return {"ok": False, "error": "Недостаточно средств"}

        db.update_balance(user_id, -amount)
        uid = str(user_id)
        if uid in self.bets:
            self.bets[uid]["amount"] = round(self.bets[uid]["amount"] + amount, 6)
        else:
            self.bets[uid] = {"amount": amount, "username": username or "",
                               "first_name": first_name or ""}
        self.pot = round(self.pot + amount, 6)
        db.add_pvp_bet(self.game_id, user_id, amount, username, first_name)

        # ── Balance history: record bet ──
        db.add_balance_history(user_id, "game", -amount,
                               game_id=f"pvp#{self.game_id}",
                               note=f"PvP ставка #{self.game_id}")

        if self.status == "waiting" and len(self.bets) >= 2:
            self.status = "betting"
            db.set_pvp_status(self.game_id, "betting")
            self._start_timer()

        new_bal = db.get_user(user_id)["balance"]
        await mgr.broadcast({**self.snapshot(), "type": "pvp_bet_placed"})
        return {"ok": True, "balance": new_bal}

    def _start_timer(self):
        if self._task:
            self._task.cancel()
        self._task = asyncio.create_task(self._countdown())

    async def _countdown(self):
        while self.timer > 0:
            await asyncio.sleep(1)
            self.timer -= 1
            await mgr.broadcast({"type": "pvp_tick", "timer": self.timer,
                                  "game_id": self.game_id})
        await self._spin()

    async def _spin(self):
        if self.status == "spinning":
            return
        if len(self.bets) < 2:
            await self.ensure_game()
            return

        self.status = "spinning"
        db.set_pvp_status(self.game_id, "spinning")
        await mgr.broadcast({"type": "pvp_spinning", "game_id": self.game_id})
        await asyncio.sleep(config.FREEZE_DURATION)

        winner_uid, ticket, total = gl.pick_wheel_winner(self.bets, self.seed)
        winner_data = self.bets[winner_uid]
        winner_id   = int(winner_uid)
        prize, mult, comm = gl.calc_wheel_prize(
            winner_data["amount"], self.pot, get_commission_pvp())
        chance = round((winner_data["amount"] / self.pot) * 100, 2)

        db.update_balance(winner_id, prize)
        db.set_pvp_status(self.game_id, "ended", winner_id=winner_id)
        for uid in self.bets:
            db.increment_stats(int(uid), won=(uid == winner_uid))

        # ── Balance history: record winnings ──
        net_win = round(prize - winner_data["amount"], 6)
        db.add_balance_history(winner_id, "game", net_win,
                               game_id=f"pvp#{self.game_id}",
                               note=f"PvP победа #{self.game_id}")

        ref_id = db.get_referrer_id(winner_id)
        if ref_id:
            db.add_ref_balance(ref_id, round(comm * get_referral_share(), 6))

        # Track commission profit
        db.add_commission_profit(round(comm * (1 - get_referral_share()), 6))

        # ── Tournament: credit won_ton for winner ──
        won_amount = round(prize - winner_data["amount"], 6)  # net profit only
        if won_amount > 0:
            for t in db.get_active_tournaments():
                if t["kind"] == "won_ton":
                    db.add_tournament_score(t["id"], winner_id, won_amount)

        db.save_pvp_history(
            game_id=self.game_id, winner_id=winner_id,
            winner_name=winner_data.get("username") or winner_data.get("first_name", ""),
            pot=self.pot, winner_bet=winner_data["amount"],
            multiplier=mult, chance=chance,
            seed=self.seed, seed_hash=self.seed_hash, bets=self.bets)

        result = {"type": "pvp_result", "game_id": self.game_id,
                  "winner_uid": winner_uid,
                  "winner_name": winner_data.get("username") or winner_data.get("first_name", ""),
                  "winner_bet": winner_data["amount"], "prize": prize,
                  "multiplier": mult, "chance": chance, "pot": self.pot,
                  "ticket": ticket, "total_tickets": total,
                  "seed": self.seed, "seed_hash": self.seed_hash, "bets": self.bets}
        await mgr.broadcast(result)

        # Send balance_update to every player who participated
        for uid_str in self.bets:
            uid_int = int(uid_str)
            user = db.get_user(uid_int)
            if user:
                await mgr.broadcast_to_user(uid_int, {
                    "type": "balance_update",
                    "balance": user["balance"],
                    "ref_balance": user["ref_balance"],
                })

        self.status  = "ended"
        self.game_id = None
        await asyncio.sleep(3)
        await self.ensure_game()

    async def force_reset(self) -> dict:
        """Admin: cancel current game, refund all bets, start fresh."""
        refunded = []
        if self._task:
            self._task.cancel()
            self._task = None
        # Refund all bets in memory
        for uid_str, data in self.bets.items():
            uid_int = int(uid_str)
            amt = data["amount"] if isinstance(data, dict) else data
            db.update_balance(uid_int, amt)
            db.add_balance_history(uid_int, "game", amt,
                                   game_id=f"pvp#{self.game_id}",
                                   note="PvP сброс (возврат ставки)")
            user = db.get_user(uid_int)
            if user:
                await mgr.broadcast_to_user(uid_int, {
                    "type": "balance_update",
                    "balance": user["balance"],
                    "ref_balance": user["ref_balance"],
                })
            refunded.append({"user_id": uid_int, "amount": amt})
        # Cancel game in DB
        if self.game_id:
            db.set_pvp_status(self.game_id, "cancelled")
        # Reset state
        self.bets     = {}
        self.pot      = 0.0
        self.status   = "ended"
        self.game_id  = None
        await mgr.broadcast({"type": "pvp_reset", "refunded": len(refunded)})
        await asyncio.sleep(1)
        await self.ensure_game()
        return {"ok": True, "refunded": refunded}


pvp_game = PvPGame()


# ══════════════════════════════════════════════════════════════════════════
# Lobby room manager
# ══════════════════════════════════════════════════════════════════════════

class LobbyManager:
    """In-memory room state (backed by DB)."""

    def _room_key(self, n=8) -> str:
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))

    def _priv_key(self, n=6) -> str:
        return "".join(random.choices(string.digits, k=n))

    async def create_room(self, ws: WebSocket, msg: dict) -> dict:
        user_id    = int(msg["user_id"])
        game_type  = msg["game_type"]   # wheel | dice | coin
        bet_amount = float(msg["bet_amount"])
        max_players= int(msg.get("max_players", 2))
        is_private = bool(msg.get("is_private", False))
        username   = msg.get("username", "")
        first_name = msg.get("first_name", "")

        user = db.get_user(user_id)
        if not user or user["balance"] < bet_amount:
            return {"ok": False, "error": "Недостаточно средств"}

        # Validate limits
        if game_type == "coin"  and max_players != 2:
            max_players = 2
        if game_type == "dice"  and max_players > 4:
            max_players = 4
        if game_type == "wheel" and max_players > 10:
            max_players = 10
        if game_type == "darts" and max_players > 8:
            max_players = 8
        if game_type not in ("coin", "dice", "wheel", "darts"):
            return {"ok": False, "error": "Неизвестный тип игры"}
        if max_players < 2:
            max_players = 2

        room_id = self._room_key()
        priv    = self._priv_key() if is_private else ""
        seed    = gl.generate_seed()
        s_hash  = gl.seed_to_hash(seed)

        db.update_balance(user_id, -bet_amount)
        db.create_room(room_id, user_id, game_type, bet_amount,
                       max_players, is_private, priv, seed, s_hash)
        db.room_add_player(room_id, user_id, username, first_name)

        mgr.subscribe_room(ws, room_id)

        room = db.get_room(room_id)
        await mgr.broadcast({"type": "lobby_rooms_update",
                              "rooms": db.get_public_rooms()})
        return {"ok": True, "room": room, "private_key": priv if is_private else None}

    async def join_room(self, ws: WebSocket, msg: dict) -> dict:
        user_id    = int(msg["user_id"])
        room_id    = msg.get("room_id", "")
        priv_key   = msg.get("private_key", "")
        username   = msg.get("username", "")
        first_name = msg.get("first_name", "")

        room = db.get_room(room_id)
        if not room:
            return {"ok": False, "error": "Комната не найдена"}
        if room["status"] != "waiting":
            return {"ok": False, "error": "Игра уже началась или завершена"}
        if room["is_private"] and room["private_key"] != priv_key:
            return {"ok": False, "error": "Неверный ключ"}
        if any(p["user_id"] == user_id for p in room["players"]):
            mgr.subscribe_room(ws, room_id)
            return {"ok": True, "room": room, "rejoined": True}
        if len(room["players"]) >= room["max_players"]:
            return {"ok": False, "error": "Комната заполнена"}

        user = db.get_user(user_id)
        if not user or user["balance"] < room["bet_amount"]:
            return {"ok": False, "error": "Недостаточно средств"}

        db.update_balance(user_id, -room["bet_amount"])
        db.room_add_player(room_id, user_id, username, first_name)
        mgr.subscribe_room(ws, room_id)

        room = db.get_room(room_id)
        await mgr.broadcast_room(room_id, {"type": "lobby_room_update", "room": room})
        await mgr.broadcast({"type": "lobby_rooms_update",
                              "rooms": db.get_public_rooms()})

        if len(room["players"]) >= room["max_players"]:
            asyncio.create_task(self._start_game(room_id))

        return {"ok": True, "room": room}

    async def _start_game(self, room_id: str):
        room = db.get_room(room_id)
        if not room or room["status"] != "waiting":
            return

        db.set_room_status(room_id, "starting")
        await mgr.broadcast_room(room_id,
                                  {"type": "lobby_countdown", "room_id": room_id, "seconds": 5})
        for i in range(4, -1, -1):
            await asyncio.sleep(1)
            await mgr.broadcast_room(room_id,
                                      {"type": "lobby_tick", "room_id": room_id, "seconds": i})

        room    = db.get_room(room_id)
        players = room["players"]
        seed    = room["seed"]

        if room["game_type"] == "coin":
            await self._resolve_coin(room_id, room, players, seed)
        elif room["game_type"] == "dice":
            await self._resolve_dice(room_id, room, players, seed, roll_index=0)
        elif room["game_type"] == "wheel":
            await self._resolve_wheel(room_id, room, players, seed)
        elif room["game_type"] == "darts":
            await self._resolve_darts(room_id, room, players, seed, round_num=0)

    async def _resolve_coin(self, room_id, room, players, seed):
        side        = gl.flip_coin(seed)
        creator_id  = str(room["creator_id"])
        joiner_id   = str([p["user_id"] for p in players
                           if str(p["user_id"]) != creator_id][0])

        winner_uid  = creator_id if side == "heads" else joiner_id
        winner_data = next(p for p in players if str(p["user_id"]) == winner_uid)

        await self._finish_room(room_id, room, players, seed, winner_uid,
                                {"side": side, "heads_uid": creator_id,
                                 "tails_uid": joiner_id},
                                game_type="coin")

    async def _resolve_dice(self, room_id, room, players, seed, roll_index=0):
        player_ids = [str(p["user_id"]) for p in players]
        rolls      = gl.roll_dice_for_players(player_ids, seed, roll_index)
        winners, max_val = gl.resolve_dice(rolls)

        await mgr.broadcast_room(room_id,
                                  {"type": "lobby_dice_rolled",
                                   "room_id": room_id, "rolls": rolls,
                                   "roll_index": roll_index})

        if len(winners) > 1:
            # Tie — re-roll only tied players after 3 seconds
            await asyncio.sleep(3)
            tied_players = [p for p in players if str(p["user_id"]) in winners]
            await mgr.broadcast_room(room_id,
                                      {"type": "lobby_dice_reroll",
                                       "room_id": room_id,
                                       "tied": [str(p["user_id"]) for p in tied_players]})
            await self._resolve_dice(room_id, room, tied_players, seed, roll_index + 1)
            return

        winner_uid = winners[0]
        await self._finish_room(room_id, room, players, seed, winner_uid,
                                {"rolls": rolls, "max": max_val},
                                game_type="dice")

    async def _resolve_darts(self, room_id, room, players, seed, round_num=0):
        player_ids = [str(p["user_id"]) for p in players]
        is_bonus = round_num > 0

        # Announce round start
        await mgr.broadcast_room(room_id, {
            "type": "darts_round_start",
            "room_id": room_id,
            "round": round_num,
            "is_bonus": is_bonus,
            "player_ids": player_ids,
        })
        await asyncio.sleep(1.5)

        # Compute all throws upfront
        throws = gl.throw_darts(player_ids, seed, round_num)

        # Send throws one dart at a time (3 per player)
        # Order: player1 throw1, player1 throw2, player1 throw3,
        #        player2 throw1, player2 throw2, player2 throw3, ...
        for uid in player_ids:
            player_data = throws[uid]
            running_total = 0
            for t_idx, throw in enumerate(player_data["throws"]):
                running_total += throw["score"]
                await mgr.broadcast_room(room_id, {
                    "type": "darts_throw",
                    "room_id": room_id,
                    "round": round_num,
                    "uid": uid,
                    "throw_index": t_idx,
                    "throw": throw,
                    "running_total": running_total,
                })
                await asyncio.sleep(1.2)  # delay between throws

            # Brief pause between players
            await asyncio.sleep(0.6)

        # All throws done - send summary
        outcome = gl.resolve_darts(throws)
        await mgr.broadcast_room(room_id, {
            "type": "darts_round_end",
            "room_id": room_id,
            "round": round_num,
            "throws": throws,
            "final_winner": outcome["final_winner"],
            "round_restart": outcome["round_restart"],
            "tie_uids": outcome["tie_uids"] if outcome["round_restart"] else [],
        })

        if outcome["round_restart"]:
            await asyncio.sleep(3)
            tied_players = [p for p in players if str(p["user_id"]) in outcome["tie_uids"]]
            await self._resolve_darts(room_id, room, tied_players, seed, round_num + 1)
            return

        winner_uid = outcome["final_winner"]
        await asyncio.sleep(2.5)
        await self._finish_room(room_id, room, players, seed, winner_uid,
                                {"throws": throws, "round": round_num},
                                game_type="darts")

    async def _resolve_wheel(self, room_id, room, players, seed):
        bets = {str(p["user_id"]): {"amount": room["bet_amount"],
                                     "username": p["username"],
                                     "first_name": p["first_name"]}
                for p in players}
        winner_uid, ticket, total = gl.pick_wheel_winner(bets, seed)
        # Broadcast spin event so all clients animate identically
        await mgr.broadcast_room(room_id, {
            "type": "lobby_wheel_spin",
            "room_id": room_id,
            "winner_uid": str(winner_uid),
            "seed": seed,
            "players": [{"user_id": p["user_id"], "username": p["username"],
                          "first_name": p["first_name"]} for p in players],
        })
        # Wait for animation (6.8s) then finish
        await asyncio.sleep(7)
        await self._finish_room(room_id, room, players, seed, winner_uid,
                                {"ticket": ticket, "total": total},
                                game_type="wheel")

    async def _finish_room(self, room_id, room, players, seed,
                            winner_uid, extra_result, game_type):
        winner      = next((p for p in players if str(p["user_id"]) == winner_uid),
                           players[0])
        winner_id   = int(winner_uid)
        total_pot   = room["bet_amount"] * len(players)
        prize, comm = gl.calc_lobby_prize(total_pot, get_commission_lobby())

        db.update_balance(winner_id, prize)

        for p in players:
            db.increment_stats(p["user_id"], won=(str(p["user_id"]) == winner_uid))

        ref_id = db.get_referrer_id(winner_id)
        if ref_id:
            db.add_ref_balance(ref_id, round(comm * get_referral_share(), 6))

        # Track commission profit
        db.add_commission_profit(round(comm * (1 - get_referral_share()), 6))

        # Balance history for all players
        for p in players:
            uid_int = p["user_id"]
            if str(uid_int) == winner_uid:
                net_win = round(prize - room["bet_amount"], 6)
                db.add_balance_history(uid_int, "game", net_win,
                                       game_id=f"lobby#{room_id}",
                                       note=f"Lobby победа #{room_id} ({game_type})")
            else:
                db.add_balance_history(uid_int, "game", -room["bet_amount"],
                                       game_id=f"lobby#{room_id}",
                                       note=f"Lobby проигрыш #{room_id} ({game_type})")

        result = {"winner_uid": winner_uid,
                  "winner_name": winner.get("username") or winner.get("first_name", ""),
                  "prize": prize, "total_pot": total_pot,
                  "seed": seed, "seed_hash": room["seed_hash"],
                  **extra_result}
        db.set_room_status(room_id, "ended", result=result)
        db.save_lobby_history(room_id, game_type, winner_id,
                               result["winner_name"], total_pot,
                               players, seed, room["seed_hash"])

        await mgr.broadcast_room(room_id, {"type": "lobby_result",
                                            "room_id": room_id, "result": result})
        await mgr.broadcast({"type": "lobby_rooms_update",
                              "rooms": db.get_public_rooms()})

        # Send balance_update to each player
        for p in players:
            user = db.get_user(p["user_id"])
            if user:
                await mgr.broadcast_to_user(p["user_id"], {
                    "type": "balance_update",
                    "balance": user["balance"],
                    "ref_balance": user["ref_balance"],
                })


lobby_mgr = LobbyManager()


def _strip_history(records: list) -> list:
    """Remove heavy fields from history records to keep WS frame under 64KB."""
    light = []
    for r in records:
        light.append({k: v for k, v in r.items()
                       if k not in ("bets_json", "bets", "seed", "seed_hash")})
    return light


# ══════════════════════════════════════════════════════════════════════════
# WebSocket endpoint
# ══════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await mgr.connect(websocket)
    await mgr.send(websocket, pvp_game.snapshot())
    await mgr.send(websocket, {
        "type": "history",
        "all":   _strip_history(db.get_pvp_history_all(10)),
        "lucky": _strip_history(db.get_pvp_history_lucky(20)),
        "big":   _strip_history(db.get_pvp_history_big(20)),
    })
    await mgr.send(websocket, {"type": "lobby_rooms_update",
                                "rooms": db.get_public_rooms()})
    try:
        while True:
            raw = await websocket.receive_text()
            await handle(websocket, json.loads(raw))
    except WebSocketDisconnect:
        mgr.disconnect(websocket)
        await mgr.broadcast({"type": "online_update", "count": len(_online_users)})
    except Exception as e:
        log.error(f"WS error: {e}")
        mgr.disconnect(websocket)


async def handle(ws: WebSocket, m: dict):
    action = m.get("action")

    # ── Auth ──
    if action == "auth":
        uid_claimed = int(m.get("user_id", 0))
        init_data   = m.get("init_data", "") or ""

        # Security: verify Telegram signature
        valid, uid = verify_telegram_init_data(init_data, uid_claimed)
        if not valid:
            await mgr.send(ws, {"type": "error", "error": "Unauthorized"})
            return
        # uid=0 means no Telegram context (desktop preview etc) — reject gracefully
        if not uid:
            await mgr.send(ws, {"type": "error", "error": "No user ID"})
            return

        ref  = int(m["ref_id"]) if m.get("ref_id") else None
        is_new = not db.get_user(uid)
        if is_new:
            db.upsert_user(uid, m.get("username",""), m.get("first_name",""),
                           referrer_id=ref)
            if ref:
                for t in db.get_active_tournaments():
                    if t["kind"] == "refs":
                        db.add_tournament_score(t["id"], ref, 1)
        else:
            db.upsert_user(uid, m.get("username",""), m.get("first_name",""))
        mgr.register_user(ws, uid)
        user = db.get_user(uid)
        refs = db.get_referrals(uid)
        online_count = len(_online_users)
        await mgr.send(ws, {"type": "auth_ok",
                             "user_id":      uid,
                             "balance":      user["balance"],
                             "ref_balance":  user["ref_balance"],
                             "games_played": user["games_played"],
                             "games_won":    user["games_won"],
                             "refs":         len(refs),
                             "is_admin":     uid == config.ADMIN_ID,
                             "online":       online_count})
        await mgr.broadcast({"type": "online_update", "count": online_count})

    # ── PvP emoji reaction (broadcast to all connected) ──
    elif action == "pvp_emoji":
        ALLOWED_EMOJI = {"👍","🤬","🤡","🤣","👀"}
        emoji = str(m.get("emoji",""))
        if emoji in ALLOWED_EMOJI:
            await mgr.broadcast({
                "type": "pvp_emoji",
                "user_id": m.get("user_id"),
                "username": m.get("username",""),
                "emoji": emoji,
            })

    # ── PvP bet ──
    elif action == "pvp_bet":
        res = await pvp_game.place_bet(
            int(m["user_id"]), float(m["amount"]),
            m.get("username",""), m.get("first_name",""))
        await mgr.send(ws, {"type": "pvp_bet_result", **res})

    # ── Promo ──
    elif action == "promo":
        uid  = int(m["user_id"])
        code = m.get("code", "").strip()
        result = db.use_promo(code, uid)
        if result["ok"]:
            db.add_balance_history(uid, "promo", result["ton"], note=f"Промокод {code}")
            await mgr.send(ws, {"type": "promo_result", "ok": True,
                                 "bonus": result["ton"], "balance": result["balance"]})
        else:
            await mgr.send(ws, {"type": "promo_result", "ok": False,
                                 "error": result["error"]})

    # ── Claim referral balance ──
    elif action == "claim_ref":
        uid    = int(m["user_id"])
        user   = db.get_user(uid)
        if not user or user["ref_balance"] < config.REFERRAL_MIN_WITHDRAW:
            await mgr.send(ws, {"type": "claim_ref_result", "ok": False,
                                  "error": f"Минимум {config.REFERRAL_MIN_WITHDRAW} TON"})
            return
        claimed = db.claim_ref_balance(uid)
        db.save_ref_withdrawal(uid, claimed)
        db.add_balance_history(uid, "ref_claim", claimed, note=f"Вывод реф. баланса")
        user    = db.get_user(uid)
        await mgr.send(ws, {"type": "claim_ref_result", "ok": True,
                             "claimed": claimed, "balance": user["balance"],
                             "ref_balance": user["ref_balance"]})

    # ── History ──
    elif action == "get_pvp_history":
        await mgr.send(ws, {"type": "history",
                             "all":   db.get_pvp_history_all(50),
                             "lucky": db.get_pvp_history_lucky(20),
                             "big":   db.get_pvp_history_big(20)})

    elif action == "get_lobby_history":
        await mgr.send(ws, {"type": "lobby_history",
                             "entries": db.get_lobby_history(50)})

    # ── Legit check ──
    elif action == "pvp_legit":
        gid   = int(m["game_id"])
        entry = db.get_pvp_history_entry(gid)
        if not entry:
            await mgr.send(ws, {"type": "pvp_legit_result", "ok": False,
                                  "error": "Игра не найдена"})
            return
        v = gl.verify_wheel(entry["seed"], entry["seed_hash"], entry["bets"])
        await mgr.send(ws, {"type": "pvp_legit_result", "ok": v["ok"],
                             "game_id": gid, **entry, **v})

    elif action == "lobby_legit":
        rid   = m["room_id"]
        entry = db.get_lobby_history_entry(rid)
        if not entry:
            await mgr.send(ws, {"type": "lobby_legit_result", "ok": False,
                                  "error": "Комната не найдена"})
            return
        v = gl.verify_lobby(entry["seed"], entry["seed_hash"],
                            entry["game_type"], entry["players"])
        await mgr.send(ws, {"type": "lobby_legit_result", "ok": v["ok"],
                             "room_id": rid, **entry, **v})

    # ── Referrals ──
    elif action == "get_referrals":
        uid  = int(m["user_id"])
        refs = db.get_referrals(uid)
        user = db.get_user(uid)
        await mgr.send(ws, {"type": "referrals", "refs": refs,
                             "ref_balance": user["ref_balance"] if user else 0})

    # ── Balance ──
    elif action == "get_top":
        users = db.get_top(100)
        await mgr.send(ws, {"type": "top_data", "users": users})

    # ── Tournaments ──
    elif action == "get_tournaments":
        uid = int(m.get("user_id", 0))
        # Return active/pending AND last 10 finished (for history strip)
        all_t = db.get_all_tournaments()
        result = []
        finished_count = 0
        for t in all_t:
            is_finished = t["status"] == "finished"
            if is_finished:
                if finished_count >= 10:
                    continue
                finished_count += 1
            lb = db.get_tournament_leaderboard(t["id"])
            my_score = db.get_user_tournament_score(t["id"], uid)
            my_rank = next((i+1 for i,e in enumerate(lb) if e["user_id"]==uid), None)
            result.append({**t, "leaderboard": lb[:30],
                           "my_score": my_score, "my_rank": my_rank})
        await mgr.send(ws, {"type": "tournaments_data", "tournaments": result})

    elif action == "get_tournament":
        uid = int(m.get("user_id", 0))
        tid = int(m.get("tournament_id", 0))
        t = db.get_tournament(tid)
        if t:
            lb = db.get_tournament_leaderboard(tid)
            my_score = db.get_user_tournament_score(tid, uid)
            my_rank = next((i+1 for i,e in enumerate(lb) if e["user_id"]==uid), None)
            await mgr.send(ws, {"type": "tournament_detail",
                                 **t, "leaderboard": lb,
                                 "my_score": my_score, "my_rank": my_rank})

    elif action == "admin_create_tournament":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        tid = db.create_tournament(
            title=str(m.get("title","Турнир")),
            kind=str(m.get("kind","won_ton")),
            prize_places=int(m.get("prize_places",3)),
            duration_min=int(m.get("duration_min",60)),
            prizes=m.get("prizes",[]),
        )
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "tournament_action": "created", "tournament_id": tid})

    elif action == "admin_start_tournament":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        tid = int(m.get("tournament_id", 0))
        t = db.start_tournament(tid)
        if t:
            # Schedule auto-finish
            async def _auto_finish(t_id, delay_s):
                await asyncio.sleep(delay_s)
                result = db.finish_tournament(t_id)
                await mgr.broadcast({"type": "tournament_finished",
                                      "tournament_id": t_id})
                # Notify participants via bot
                await _notify_tournament_result(t_id, result)
            dur_s = t["duration_min"] * 60
            asyncio.create_task(_auto_finish(tid, dur_s))
            await mgr.send(ws, {"type": "admin_result", "ok": True,
                                 "tournament_action": "started"})
        else:
            await mgr.send(ws, {"type": "admin_result", "ok": False,
                                 "error": "Нельзя запустить"})

    elif action == "admin_finish_tournament":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        tid = int(m.get("tournament_id", 0))
        result = db.finish_tournament(tid)
        await mgr.broadcast({"type": "tournament_finished",
                              "tournament_id": m.get("tournament_id")})
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "tournament_action": "finished"})
        asyncio.create_task(_notify_tournament_result(tid, result))

    elif action == "admin_delete_tournament":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        db.delete_tournament(int(m.get("tournament_id", 0)))
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "tournament_action": "deleted"})

    elif action == "admin_adjust_tournament_score":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        tid = int(m.get("tournament_id", 0))
        target = int(m.get("target_uid", 0))
        op = m.get("op", "+")
        val = float(m.get("value", 0))
        if op == "=":
            db.set_tournament_score(tid, target, val)
        elif op == "-":
            db.add_tournament_score(tid, target, -val)
        else:
            db.add_tournament_score(tid, target, val)
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "tournament_action": "score_adjusted"})

    elif action == "admin_update_tournament_prizes":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        db.update_tournament_prizes(int(m.get("tournament_id",0)), m.get("prizes",[]))
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "tournament_action": "prizes_updated"})

    elif action == "get_balance":
        uid  = int(m["user_id"])
        user = db.get_user(uid)
        if user:
            await mgr.send(ws, {"type": "balance",
                                  "balance": user["balance"],
                                  "ref_balance": user["ref_balance"]})

    elif action == "get_stars_rate":
        # Fetch TON/USD price and compute Stars rate (1 Star ≈ $0.013)
        import aiohttp as _ah
        try:
            async with _ah.ClientSession() as sess:
                async with sess.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd",
                    timeout=_ah.ClientTimeout(total=4)
                ) as r:
                    cg = await r.json()
            ton_usd = cg["the-open-network"]["usd"]
            star_usd = 0.013
            rate = round(star_usd / ton_usd, 6)
        except Exception:
            rate = 0.013  # fallback static rate
        await mgr.send(ws, {"type": "stars_rate", "rate": rate})

    # ── Lobby create / join ──
    elif action == "lobby_create":
        res = await lobby_mgr.create_room(ws, m)
        await mgr.send(ws, {"type": "lobby_create_result", **res})

    elif action == "lobby_join":
        res = await lobby_mgr.join_room(ws, m)
        await mgr.send(ws, {"type": "lobby_join_result", **res})

    elif action == "lobby_join_by_key":
        pk = str(m.get("private_key", "")).strip().upper()
        if not pk:
            await mgr.send(ws, {"type": "lobby_join_result", "ok": False,
                                  "error": "Введи ключ комнаты"})
            return
        # Find room by private key
        room = db.get_room_by_key(pk)
        if not room:
            await mgr.send(ws, {"type": "lobby_join_result", "ok": False,
                                  "error": "Комната не найдена — проверь ключ"})
            return
        m["room_id"] = room["room_id"]
        m["private_key"] = pk
        res = await lobby_mgr.join_room(ws, m)
        await mgr.send(ws, {"type": "lobby_join_result", **res})

    elif action == "lobby_leave":
        uid = int(m.get("user_id", 0))
        room_id = m.get("room_id", "")
        removed = db.room_remove_player(room_id, uid)
        if removed:
            # Broadcast updated room list (room may be deleted now)
            remaining = db.get_room(room_id)
            if remaining:
                await mgr.broadcast_room(room_id, {
                    "type": "lobby_room_update", "room": remaining
                })
            await mgr.broadcast({
                "type": "lobby_rooms_update",
                "rooms": db.get_public_rooms()
            })
            user = db.get_user(uid)
            await mgr.send(ws, {
                "type": "lobby_leave_result", "ok": True,
                "balance": user["balance"] if user else 0
            })
        else:
            await mgr.send(ws, {
                "type": "lobby_leave_result", "ok": False,
                "error": "Невозможно покинуть (игра уже начата или вы не в комнате)"
            })

    elif action == "lobby_subscribe":
        room_id = m.get("room_id","")
        mgr.subscribe_room(ws, room_id)
        room = db.get_room(room_id)
        if room:
            await mgr.send(ws, {"type": "lobby_room_update", "room": room})

    elif action == "get_lobby_rooms":
        await mgr.send(ws, {"type": "lobby_rooms_update",
                             "rooms": db.get_public_rooms()})

    elif action == "get_my_rooms":
        uid = int(m.get("user_id", 0))
        my_rooms = db.get_user_rooms(uid)
        await mgr.send(ws, {"type": "my_rooms_update", "rooms": my_rooms})

    elif action == "admin_pvp_reset":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"})
            return
        result = await pvp_game.force_reset()
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "pvp_action": "reset", **result})

    # ── Admin ──
    elif action == "admin_get_data":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"})
            return
        users = db.get_all_users()
        await mgr.send(ws, {
            "type": "admin_data",
            "users": users,
            "promos": db.get_all_promos(),
            "bonuses": db.get_bonuses(active_only=False),
            "tournaments": db.get_all_tournaments(),
            "settings": {
                "commission_pvp":   db.get_setting("commission_pvp",   config.COMMISSION_PVP),
                "commission_lobby": db.get_setting("commission_lobby",  config.COMMISSION_LOBBY),
                "referral_share":   db.get_setting("referral_share",    config.REFERRAL_SHARE),
            }
        })

    elif action == "admin_set_balance":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"})
            return
        target_raw = str(m.get("target", "")).strip()
        op  = str(m.get("op", "="))   # "+" | "-" | "="
        val = float(m.get("value", 0))
        # resolve target (username or id)
        if target_raw.lstrip("@").isdigit():
            target = db.get_user(int(target_raw.lstrip("@")))
        else:
            target = db.get_user_by_username(target_raw)
        if not target:
            await mgr.send(ws, {"type": "admin_result", "ok": False,
                                  "error": "Пользователь не найден"})
            return
        cur = target["balance"]
        if op == "+":
            new_bal = cur + val
        elif op == "-":
            new_bal = max(0.0, cur - val)
        else:
            new_bal = val
        db.set_user_balance(target["user_id"], new_bal)
        # push live update to the target user if online
        await mgr.broadcast_to_user(target["user_id"], {
            "type": "balance_update",
            "balance": new_bal,
            "ref_balance": target.get("ref_balance", 0),
        })
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "user_id": target["user_id"],
                             "new_balance": new_bal})

    elif action == "admin_set_setting":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"})
            return
        key = m.get("key", "")
        val = float(m.get("value", 0))
        if key not in ("commission_pvp", "commission_lobby", "referral_share"):
            await mgr.send(ws, {"type": "admin_result", "ok": False,
                                  "error": "Неизвестный ключ"})
            return
        db.set_setting(key, val)
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "key": key, "value": val})

    elif action == "admin_set_referrer":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"})
            return
        target_uid = int(m.get("target_uid", 0))
        new_ref_raw = m.get("new_referrer_id")
        new_ref = int(new_ref_raw) if new_ref_raw else None
        db.set_referrer(target_uid, new_ref)
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "target_uid": target_uid, "new_referrer_id": new_ref})

    elif action == "get_bonuses":
        uid = int(m.get("user_id", 0))
        bonuses = db.get_bonuses(active_only=True)
        completed = db.get_user_completed_bonuses(uid)
        await mgr.send(ws, {"type": "bonuses_data",
                             "bonuses": bonuses, "completed": completed})

    elif action == "check_bonus":
        uid = int(m.get("user_id", 0))
        bonus_id = int(m.get("bonus_id", 0))
        bonuses = db.get_bonuses(active_only=False)
        bonus = next((b for b in bonuses if b["id"] == bonus_id), None)
        if not bonus:
            await mgr.send(ws, {"type": "bonus_result", "ok": False,
                                 "bonus_id": bonus_id, "error": "Задание не найдено"})
            return
        # Check if already completed (handles cooldown logic in db)
        completed = db.get_user_completed_bonuses(uid)
        if bonus_id in completed:
            await mgr.send(ws, {"type": "bonus_result", "ok": False,
                                 "bonus_id": bonus_id, "error": "Задание уже выполнено"})
            return
        # Verify by type
        if bonus["bonus_type"] == "channel_sub":
            channel = bonus["channel_username"]
            import aiohttp as _ah
            try:
                async with _ah.ClientSession() as sess:
                    async with sess.get(
                        f"https://api.telegram.org/bot{config.BOT_TOKEN}/getChatMember",
                        params={"chat_id": "@" + channel, "user_id": uid},
                        timeout=_ah.ClientTimeout(total=5)
                    ) as r:
                        data = await r.json()
                status = data.get("result", {}).get("status", "")
                if status not in ("member", "administrator", "creator"):
                    await mgr.send(ws, {"type": "bonus_result", "ok": False,
                                         "bonus_id": bonus_id,
                                         "error": "Подпишитесь на канал и попробуйте снова"})
                    return
            except Exception as e:
                log.warning(f"Bonus check error: {e}")
                await mgr.send(ws, {"type": "bonus_result", "ok": False,
                                     "bonus_id": bonus_id,
                                     "error": "Ошибка проверки, попробуйте позже"})
                return

        elif bonus["bonus_type"] == "channel_boost":
            channel = bonus["channel_username"]
            need = int(bonus.get("boost_count") or 1)
            import aiohttp as _ah
            try:
                async with _ah.ClientSession() as sess:
                    # getUserChatBoosts — requires bot to be admin in channel
                    async with sess.get(
                        f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUserChatBoosts",
                        params={"chat_id": "@" + channel, "user_id": uid},
                        timeout=_ah.ClientTimeout(total=5)
                    ) as r:
                        data = await r.json()
                boosts = data.get("result", {}).get("boosts", [])
                count = len(boosts)
                if count < need:
                    await mgr.send(ws, {"type": "bonus_result", "ok": False,
                                         "bonus_id": bonus_id,
                                         "error": f"Нужно {need} буст(а) канала, у вас {count}"})
                    return
            except Exception as e:
                log.warning(f"Boost check error: {e}")
                await mgr.send(ws, {"type": "bonus_result", "ok": False,
                                     "bonus_id": bonus_id,
                                     "error": "Ошибка проверки бустов, попробуйте позже"})
                return
        # Award
        ok = db.complete_bonus(bonus_id, uid, bonus["reward"])
        if ok:
            db.add_balance_history(uid, "bonus", bonus["reward"],
                                   note=f"Бонус: {bonus['title']}")
            user = db.get_user(uid)
            await mgr.send(ws, {"type": "bonus_result", "ok": True,
                                 "bonus_id": bonus_id, "reward": bonus["reward"],
                                 "balance": user["balance"] if user else 0})
        else:
            await mgr.send(ws, {"type": "bonus_result", "ok": False,
                                 "bonus_id": bonus_id, "error": "Задание уже выполнено"})

    elif action == "admin_create_bonus":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        bid = db.create_bonus(
            title=str(m.get("title","")).strip(),
            description=str(m.get("description","")).strip(),
            icon=str(m.get("icon","🎁")),
            reward=float(m.get("reward",0)),
            bonus_type=str(m.get("bonus_type","channel_sub")),
            channel_username=str(m.get("channel_username","")).replace("@","").strip(),
            action_url=str(m.get("action_url","")),
            action_label=str(m.get("action_label","Выполнить →")),
            boost_count=int(m.get("boost_count", 1) or 1),
            repeat_hours=int(m.get("repeat_hours", 0) or 0),
        )
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "bonus_action": "created", "bonus_id": bid})

    elif action == "admin_delete_bonus":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        db.delete_bonus(int(m.get("bonus_id", 0)))
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "bonus_action": "deleted"})

    elif action == "admin_create_promo":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"})
            return
        code = str(m.get("code", "")).strip().upper()
        ton = float(m.get("ton", 0))
        max_uses = int(m.get("max_uses", 1))
        promo_type = str(m.get("promo_type", "once_per_user"))
        cooldown_sec = int(m.get("cooldown_sec", 0))
        if not code or ton <= 0 or max_uses <= 0:
            await mgr.send(ws, {"type": "admin_result", "ok": False,
                                 "promo_action": "created", "error": "Неверные параметры"})
            return
        ok = db.create_promo(code, ton, max_uses, promo_type, cooldown_sec)
        if ok:
            await mgr.send(ws, {"type": "admin_result", "ok": True,
                                 "promo_action": "created", "code": code})
        else:
            await mgr.send(ws, {"type": "admin_result", "ok": False,
                                 "promo_action": "created", "error": f"Код {code} уже существует"})

    elif action == "admin_delete_promo":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"})
            return
        code = str(m.get("code", "")).upper()
        db.delete_promo(code)
        await mgr.send(ws, {"type": "admin_result", "ok": True,
                             "promo_action": "deleted", "code": code})

    # ── Transfer TON ──
    elif action == "transfer_ton":
        uid = int(m.get("user_id", 0))
        target_raw = str(m.get("target", "")).strip()
        amount = float(m.get("amount", 0))
        # Resolve target
        if target_raw.lstrip("@").isdigit():
            to_user = db.get_user(int(target_raw.lstrip("@")))
        else:
            to_user = db.get_user_by_username(target_raw)
        if not to_user:
            await mgr.send(ws, {"type": "transfer_result", "ok": False,
                                 "error": "Пользователь не найден"})
            return
        result = db.transfer_ton(uid, to_user["user_id"], amount)
        if result["ok"]:
            user = db.get_user(uid)
            await mgr.send(ws, {"type": "transfer_result", "ok": True,
                                 "amount": result["amount"], "net": result["net"],
                                 "fee": result["fee"], "balance": result["from_balance"],
                                 "to_username": result["to_username"]})
            # Notify recipient if online
            await mgr.broadcast_to_user(to_user["user_id"], {
                "type": "balance_update",
                "balance": db.get_user(to_user["user_id"])["balance"],
                "ref_balance": to_user.get("ref_balance", 0),
            })
        else:
            await mgr.send(ws, {"type": "transfer_result", "ok": False,
                                 "error": result["error"]})

    # ── Balance History ──
    elif action == "get_balance_history":
        uid = int(m.get("user_id", 0))
        history = db.get_balance_history(uid, 100)
        await mgr.send(ws, {"type": "balance_history", "entries": history})

    # ── Ref Withdrawal History ──
    elif action == "get_ref_withdrawals":
        uid = int(m.get("user_id", 0))
        withdrawals = db.get_ref_withdrawals(uid, 50)
        await mgr.send(ws, {"type": "ref_withdrawals", "entries": withdrawals})

    # ── Admin Profit ──
    elif action == "admin_get_profit":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        profit = db.get_profit()
        await mgr.send(ws, {"type": "admin_profit", **profit})

    elif action == "admin_adjust_profit":
        uid = int(m.get("user_id", 0))
        if uid != config.ADMIN_ID:
            await mgr.send(ws, {"type": "error", "error": "Forbidden"}); return
        delta = float(m.get("delta", 0))
        profit = db.adjust_profit(delta)
        await mgr.send(ws, {"type": "admin_profit", **profit})


# ══════════════════════════════════════════════════════════════════════════
# REST
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/avatar/{user_id}")
async def api_avatar(user_id: int):
    """Proxy Telegram profile photo to avoid CORS / token exposure."""
    import aiohttp
    from fastapi.responses import Response as FR
    bot_token = config.BOT_TOKEN
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{bot_token}/getUserProfilePhotos",
                params={"user_id": user_id, "limit": 1}
            ) as r:
                data = await r.json()
            if not data.get("ok") or not data["result"]["total_count"]:
                raise HTTPException(404)
            file_id = data["result"]["photos"][0][-1]["file_id"]
            async with session.get(
                f"https://api.telegram.org/bot{bot_token}/getFile",
                params={"file_id": file_id}
            ) as r:
                fdata = await r.json()
            file_path = fdata["result"]["file_path"]
            async with session.get(
                f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            ) as r:
                content = await r.read()
                return FR(content=content, media_type="image/jpeg",
                          headers={"Cache-Control": "public, max-age=3600"})
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"Avatar fetch error for {user_id}: {e}")
        raise HTTPException(404)


@app.get("/api/user/{user_id}")
def api_user(user_id: int):
    u = db.get_user(user_id)
    if not u:
        raise HTTPException(404)
    return u


@app.get("/api/pvp/history")
def api_pvp_history():
    return {"all": db.get_pvp_history_all(20),
            "lucky": db.get_pvp_history_lucky(20),
            "big": db.get_pvp_history_big(20)}


@app.get("/api/lobby/rooms")
def api_lobby_rooms():
    return db.get_public_rooms()


# ══════════════════════════════════════════════════════════════════════════
# Startup
# ══════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    db.init_db()
    db._ensure_settings()
    db._migrate()
    db._migrate_new_tables()
    db._ensure_profit_row()
    refunds = db.refund_all_active_bets()
    if refunds:
        log.warning(f"Server restart: refunded {len(refunds)} bets to players")
    # Seed default bonus if none exist
    if not db.get_bonuses(active_only=False):
        db.create_bonus(
            title="Подписка на RoyalDuel",
            description="Подпишись на официальный канал и получи бонус",
            icon="📢",
            reward=100.0,
            bonus_type="channel_sub",
            channel_username="RoyalDuel",
            action_url="https://t.me/RoyalDuel",
            action_label="Перейти в канал →",
        )
        log.info("Seeded default channel subscription bonus")
    log.info("DB ready")
    await pvp_game.ensure_game()
    log.info(f"Server ready on :{config.PORT}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=config.PORT, reload=False)
