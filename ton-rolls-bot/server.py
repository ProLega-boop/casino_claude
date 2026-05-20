"""server.py — FastAPI + WebSocket server for RoyalDuel / TON Rolls."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import string
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import config
import database as db
import game_logic as gl

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("royalduel")


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
    return (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")


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

    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)
        for subs in self.room_subs.values():
            subs.discard(ws)
        for socks in self.user_socks.values():
            socks.discard(ws)

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
        if self.status == "spinning":
            return {"ok": False, "error": "Ставки закрыты"}
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

        ref_id = db.get_referrer_id(winner_id)
        if ref_id:
            db.add_ref_balance(ref_id, round(comm * get_referral_share(), 6))

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

    async def _resolve_wheel(self, room_id, room, players, seed):
        bets = {str(p["user_id"]): {"amount": room["bet_amount"],
                                     "username": p["username"],
                                     "first_name": p["first_name"]}
                for p in players}
        winner_uid, ticket, total = gl.pick_wheel_winner(bets, seed)
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


# ══════════════════════════════════════════════════════════════════════════
# WebSocket endpoint
# ══════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await mgr.connect(websocket)
    await mgr.send(websocket, pvp_game.snapshot())
    await mgr.send(websocket, {
        "type": "history",
        "all":   db.get_pvp_history_all(10),
        "lucky": db.get_pvp_history_lucky(20),
        "big":   db.get_pvp_history_big(20),
    })
    await mgr.send(websocket, {"type": "lobby_rooms_update",
                                "rooms": db.get_public_rooms()})
    try:
        while True:
            raw = await websocket.receive_text()
            await handle(websocket, json.loads(raw))
    except WebSocketDisconnect:
        mgr.disconnect(websocket)
    except Exception as e:
        log.error(f"WS error: {e}")
        mgr.disconnect(websocket)


async def handle(ws: WebSocket, m: dict):
    action = m.get("action")

    # ── Auth ──
    if action == "auth":
        uid  = int(m["user_id"])
        ref  = int(m["ref_id"]) if m.get("ref_id") else None
        if not db.get_user(uid):
            db.upsert_user(uid, m.get("username",""), m.get("first_name",""),
                           referrer_id=ref)
        else:
            db.upsert_user(uid, m.get("username",""), m.get("first_name",""))
        mgr.register_user(ws, uid)
        user = db.get_user(uid)
        refs = db.get_referrals(uid)
        await mgr.send(ws, {"type": "auth_ok",
                             "user_id":      uid,
                             "balance":      user["balance"],
                             "ref_balance":  user["ref_balance"],
                             "games_played": user["games_played"],
                             "games_won":    user["games_won"],
                             "refs":         len(refs),
                             "is_admin":     uid == config.ADMIN_ID})

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

    elif action == "lobby_leave":
        uid = int(m.get("user_id", 0))
        room_id = m.get("room_id", "")
        removed = db.room_remove_player(room_id, uid)
        if removed:
            room = db.get_room(room_id)
            if room:
                await mgr.broadcast_room(room_id, {"type": "lobby_room_update", "room": room})
                await mgr.broadcast({"type": "lobby_rooms_update",
                                     "rooms": db.get_public_rooms()})
            user = db.get_user(uid)
            await mgr.send(ws, {"type": "lobby_leave_result", "ok": True,
                                 "balance": user["balance"] if user else 0})
        else:
            await mgr.send(ws, {"type": "lobby_leave_result", "ok": False,
                                 "error": "Невозможно покинуть (игра уже начата или вы не в комнате)"})

    elif action == "lobby_subscribe":
        room_id = m.get("room_id","")
        mgr.subscribe_room(ws, room_id)
        room = db.get_room(room_id)
        if room:
            await mgr.send(ws, {"type": "lobby_room_update", "room": room})

    elif action == "get_lobby_rooms":
        await mgr.send(ws, {"type": "lobby_rooms_update",
                             "rooms": db.get_public_rooms()})

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
    refunds = db.refund_all_active_bets()
    if refunds:
        log.warning(f"Server restart: refunded {len(refunds)} bets to players")
    log.info("DB ready")
    await pvp_game.ensure_game()
    log.info(f"Server ready on :{config.PORT}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=config.PORT, reload=False)
