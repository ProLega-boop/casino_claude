"""server.py — FastAPI + WebSocket real-time game server for TON Rolls."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
import database as db
import game_logic as gl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rolls")

app = FastAPI(title="TON Rolls Server")

# ── Serve webapp ───────────────────────────────────────────────────────────
WEBAPP_DIR = Path("webapp")
app.mount("/static", StaticFiles(directory=str(WEBAPP_DIR)), name="static")


@app.get("/webapp", response_class=HTMLResponse)
async def serve_webapp():
    return (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/webapp/index.html", response_class=HTMLResponse)
async def serve_webapp_html():
    return (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")


# ── Connection manager ─────────────────────────────────────────────────────

class ConnManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws) if ws in self.active else None

    async def broadcast(self, data: dict):
        msg = json.dumps(data)
        dead = []
        for ws in self.active:
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

# ── Game state ─────────────────────────────────────────────────────────────

class GameState:
    def __init__(self):
        self.game_id: int | None = None
        self.status: str = "waiting"   # waiting | betting | spinning | ended
        self.timer: int  = config.ROUND_DURATION
        self.bets: dict  = {}
        self.pot: float  = 0.0
        self.seed: str   = ""
        self.seed_hash: str = ""
        self._timer_task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        return {
            "type":      "state",
            "game_id":   self.game_id,
            "status":    self.status,
            "timer":     self.timer,
            "bets":      self.bets,
            "pot":       self.pot,
            "seed_hash": self.seed_hash,
        }

    async def ensure_game(self):
        """Create a new game if none is active."""
        if self.game_id is not None and self.status in ("waiting", "betting"):
            return
        self.seed      = gl.generate_seed()
        self.seed_hash = gl.seed_to_hash(self.seed)
        self.bets      = {}
        self.pot       = 0.0
        self.status    = "waiting"
        self.timer     = config.ROUND_DURATION
        self.game_id   = db.create_game(self.seed, self.seed_hash)
        log.info(f"New game #{self.game_id} hash={self.seed_hash[:12]}…")
        await mgr.broadcast(self.snapshot())

    async def place_bet(self, user_id: int, amount: float,
                        username: str, first_name: str) -> dict:
        if self.status == "spinning":
            return {"ok": False, "error": "Ставки закрыты — колесо крутится"}

        user = db.get_user(user_id)
        if not user:
            return {"ok": False, "error": "Пользователь не найден"}
        if user["balance"] < amount:
            return {"ok": False, "error": "Недостаточно средств"}
        if amount <= 0:
            return {"ok": False, "error": "Некорректная сумма"}

        # Deduct balance
        db.update_balance(user_id, -amount)

        # Merge into game bets
        uid = str(user_id)
        if uid in self.bets:
            self.bets[uid]["amount"] = round(self.bets[uid]["amount"] + amount, 6)
        else:
            self.bets[uid] = {
                "amount":     amount,
                "username":   username or "",
                "first_name": first_name or "",
            }
        self.pot = round(self.pot + amount, 6)

        db.add_bet_to_game(self.game_id, user_id, amount, username, first_name)

        new_balance = db.get_user(user_id)["balance"]

        # Start timer when 2+ distinct players
        if self.status == "waiting" and len(self.bets) >= 2:
            self.status = "betting"
            db.set_game_status(self.game_id, "betting")
            self._start_timer()

        await mgr.broadcast({**self.snapshot(), "type": "bet_placed",
                              "uid": uid, "amount": amount})
        return {"ok": True, "balance": new_balance}

    def _start_timer(self):
        if self._timer_task:
            self._timer_task.cancel()
        self._timer_task = asyncio.create_task(self._countdown())
        log.info(f"Timer started for game #{self.game_id}")

    async def _countdown(self):
        while self.timer > 0:
            await asyncio.sleep(1)
            self.timer -= 1
            await mgr.broadcast({"type": "tick", "timer": self.timer,
                                  "game_id": self.game_id})
        await self._spin()

    async def _spin(self):
        if self.status == "spinning":
            return
        if len(self.bets) < 2:
            await self.ensure_game()
            return

        self.status = "spinning"
        db.set_game_status(self.game_id, "spinning")
        await mgr.broadcast({"type": "spinning", "game_id": self.game_id})

        await asyncio.sleep(config.FREEZE_DURATION)

        # Pick winner
        winner_uid, ticket, total_tickets = gl.pick_winner(self.bets, self.seed)
        winner_data  = self.bets[winner_uid]
        winner_bet   = winner_data["amount"]
        winner_id    = int(winner_uid)
        prize, mult, comm = gl.calc_prize(winner_bet, self.pot, config.COMMISSION)
        chance       = round((winner_bet / self.pot) * 100, 2)

        # Update DB
        db.update_balance(winner_id, prize)
        db.set_game_status(self.game_id, "ended", winner_id=winner_id)
        db.increment_stats(winner_id, won=True)
        for uid in self.bets:
            if int(uid) != winner_id:
                db.increment_stats(int(uid), won=False)

        # Referral payout
        ref_id = db.get_referrer_id(winner_id)
        if ref_id:
            ref_bonus = round(comm * config.REFERRAL_SHARE, 6)
            db.update_balance(ref_id, ref_bonus)

        # Save history
        db.save_history(
            game_id     = self.game_id,
            winner_id   = winner_id,
            winner_name = winner_data.get("username") or winner_data.get("first_name", ""),
            pot         = self.pot,
            winner_bet  = winner_bet,
            multiplier  = mult,
            chance      = chance,
            seed        = self.seed,
            seed_hash   = self.seed_hash,
            bets        = self.bets,
        )

        result = {
            "type":          "result",
            "game_id":       self.game_id,
            "winner_uid":    winner_uid,
            "winner_name":   winner_data.get("username") or winner_data.get("first_name", ""),
            "winner_bet":    winner_bet,
            "prize":         prize,
            "multiplier":    mult,
            "chance":        chance,
            "pot":           self.pot,
            "ticket":        ticket,
            "total_tickets": total_tickets,
            "seed":          self.seed,
            "seed_hash":     self.seed_hash,
            "bets":          self.bets,
        }
        await mgr.broadcast(result)
        log.info(f"Game #{self.game_id} finished. Winner uid={winner_uid} prize={prize}")

        self.status  = "ended"
        self.game_id = None

        # Start next game after short delay
        await asyncio.sleep(3)
        await self.ensure_game()


game = GameState()


# ── WebSocket endpoint ─────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await mgr.connect(websocket)
    # Send current state immediately
    await mgr.send(websocket, game.snapshot())
    # Send last 10 history entries
    await mgr.send(websocket, {
        "type":    "history",
        "all":     db.get_history_all(10),
        "lucky":   db.get_history_lucky(20),
        "big":     db.get_history_big(20),
    })
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            await handle_message(websocket, msg)
    except WebSocketDisconnect:
        mgr.disconnect(websocket)
    except Exception as e:
        log.error(f"WS error: {e}")
        mgr.disconnect(websocket)


async def handle_message(ws: WebSocket, msg: dict):
    action = msg.get("action")

    if action == "auth":
        user_id    = int(msg["user_id"])
        username   = msg.get("username", "")
        first_name = msg.get("first_name", "")
        ref_id     = msg.get("ref_id")

        existing = db.get_user(user_id)
        if not existing:
            db.upsert_user(user_id, username, first_name,
                           referrer_id=int(ref_id) if ref_id else None)
        else:
            db.upsert_user(user_id, username, first_name)

        user = db.get_user(user_id)
        refs = db.get_referrals(user_id)
        await mgr.send(ws, {
            "type":         "auth_ok",
            "user_id":      user_id,
            "balance":      user["balance"],
            "games_played": user["games_played"],
            "games_won":    user["games_won"],
            "refs":         len(refs),
        })

    elif action == "bet":
        user_id    = int(msg["user_id"])
        amount     = float(msg["amount"])
        username   = msg.get("username", "")
        first_name = msg.get("first_name", "")
        result = await game.place_bet(user_id, amount, username, first_name)
        await mgr.send(ws, {"type": "bet_result", **result})

    elif action == "promo":
        user_id = int(msg["user_id"])
        code    = msg.get("code", "").strip().lower()
        if code not in config.PROMO_CODES:
            await mgr.send(ws, {"type": "promo_result", "ok": False,
                                 "error": "Промокод не найден"})
            return
        if db.promo_used(user_id, code):
            await mgr.send(ws, {"type": "promo_result", "ok": False,
                                 "error": "Промокод уже использован"})
            return
        bonus = config.PROMO_CODES[code]
        db.update_balance(user_id, bonus)
        db.mark_promo(user_id, code)
        user = db.get_user(user_id)
        await mgr.send(ws, {"type": "promo_result", "ok": True,
                             "bonus": bonus, "balance": user["balance"]})

    elif action == "get_history":
        await mgr.send(ws, {
            "type":  "history",
            "all":   db.get_history_all(50),
            "lucky": db.get_history_lucky(20),
            "big":   db.get_history_big(20),
        })

    elif action == "legit_check":
        game_id = int(msg["game_id"])
        entry   = db.get_history_entry(game_id)
        if not entry:
            await mgr.send(ws, {"type": "legit_result", "ok": False,
                                 "error": "Игра не найдена"})
            return
        verify  = gl.verify_round(entry["seed"], entry["seed_hash"], entry["bets"])
        await mgr.send(ws, {
            "type":        "legit_result",
            "ok":          verify["ok"],
            "game_id":     game_id,
            "seed":        entry["seed"],
            "seed_hash":   entry["seed_hash"],
            "pot":         entry["pot"],
            "winner_name": entry["winner_name"],
            "winner_id":   entry["winner_id"],
            "chance":      entry["chance"],
            "multiplier":  entry["multiplier"],
            "bets":        entry["bets"],
            "ticket":      verify.get("ticket"),
            "total":       verify.get("total"),
        })

    elif action == "get_balance":
        user_id = int(msg["user_id"])
        user    = db.get_user(user_id)
        if user:
            await mgr.send(ws, {"type": "balance", "balance": user["balance"]})

    elif action == "get_referrals":
        user_id = int(msg["user_id"])
        refs    = db.get_referrals(user_id)
        await mgr.send(ws, {"type": "referrals", "refs": refs})


# ── REST API (for bot.py) ──────────────────────────────────────────────────

@app.get("/api/user/{user_id}")
def api_get_user(user_id: int):
    u = db.get_user(user_id)
    if not u:
        raise HTTPException(404, "User not found")
    return u


@app.get("/api/history")
def api_history():
    return {
        "all":   db.get_history_all(20),
        "lucky": db.get_history_lucky(20),
        "big":   db.get_history_big(20),
    }


@app.get("/api/legit/{game_id}")
def api_legit(game_id: int):
    entry = db.get_history_entry(game_id)
    if not entry:
        raise HTTPException(404, "Game not found")
    result = gl.verify_round(entry["seed"], entry["seed_hash"], entry["bets"])
    return {**entry, "verify": result}


# ── Startup ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    db.init_db()
    log.info("Database initialised")
    await game.ensure_game()
    log.info(f"Server ready on port {config.PORT}")


# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=config.PORT, reload=False)
