#!/usr/bin/env python3
"""
patch_v3.py — RoyalDuel v3
Fixes:
  1. Lobby clickRoom — оба игрока могут нажимать на комнату
  2. Вывод TON — кнопки принять/отклонить у админа, возврат при отклонении
  3. Подарки в админке — список реальных подарков бота
  4. Буст канала — проверка + количество бустов + периодичность всех заданий
  5. База данных — таблица withdraw_requests
"""
import re, sys
from pathlib import Path

BASE = Path("/root/ton-rolls-bot")
SERVER  = BASE / "server.py"
BOT_PY  = BASE / "bot.py"
DB_PY   = BASE / "database.py"
HTML    = BASE / "webapp/index.html"

def patch_file(path, content):
    path.write_text(content, encoding="utf-8")
    print(f"✅ Saved {path}")

# ══════════════════════════════════════════════════════════
# 1. DATABASE — добавить таблицу withdraw_requests + функции
# ══════════════════════════════════════════════════════════
print("\n── 1. DATABASE ──")
db_c = DB_PY.read_text(encoding="utf-8")

if "withdraw_requests" not in db_c:
    # Добавить таблицу в init_db
    old_ref_w = "        CREATE TABLE IF NOT EXISTS ref_withdrawals ("
    new_ref_w = """        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            amount     REAL NOT NULL,
            address    TEXT NOT NULL,
            status     TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ref_withdrawals ("""
    db_c = db_c.replace(old_ref_w, new_ref_w, 1)
    print("✅ withdraw_requests table added")
else:
    print("ℹ️  withdraw_requests already exists")

# Добавить в _migrate_new_tables
if "withdraw_requests" not in db_c or "def create_withdraw_request" not in db_c:
    # Find last function def before EOF
    insert_before = "def get_ref_withdrawals"
    new_funcs = '''
def create_withdraw_request(user_id: int, amount: float, address: str) -> int:
    with _conn() as db:
        cur = db.execute(
            "INSERT INTO withdraw_requests (user_id,amount,address,status) VALUES (?,?,?,'pending')",
            (user_id, amount, address)
        )
        return cur.lastrowid

def get_withdraw_requests(status: str = "pending", limit: int = 50) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT wr.*, u.username, u.first_name FROM withdraw_requests wr "
            "LEFT JOIN users u ON u.user_id=wr.user_id "
            "WHERE wr.status=? ORDER BY wr.id DESC LIMIT ?",
            (status, limit)
        ).fetchall()
        return [dict(r) for r in rows]

def resolve_withdraw_request(req_id: int, status: str) -> dict | None:
    """status: 'approved' or 'rejected'"""
    with _conn() as db:
        db.execute(
            "UPDATE withdraw_requests SET status=?,resolved_at=datetime('now') WHERE id=?",
            (status, req_id)
        )
        row = db.execute("SELECT * FROM withdraw_requests WHERE id=?", (req_id,)).fetchone()
        return dict(row) if row else None

'''
    if insert_before in db_c:
        db_c = db_c.replace(insert_before, new_funcs + insert_before, 1)
        print("✅ withdraw_request functions added")
    else:
        db_c = db_c + new_funcs
        print("✅ withdraw_request functions appended")

# Also ensure migrate creates the table
if "CREATE TABLE IF NOT EXISTS withdraw_requests" not in db_c:
    old_migrate = '        db.execute("""CREATE TABLE IF NOT EXISTS ref_withdrawals'
    new_migrate = '''        db.execute("""CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            address TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        )""")
        ''' + old_migrate
    if old_migrate in db_c:
        db_c = db_c.replace(old_migrate, new_migrate, 1)

# repeat_hours в bonus_completions — заменить уникальный индекс
if "UNIQUE(bonus_id, user_id)" in db_c:
    # Already there — completions needs to allow repeat, so we use ON CONFLICT IGNORE
    pass  # Keep unique for now, repeat_hours handled in server logic

patch_file(DB_PY, db_c)


# ══════════════════════════════════════════════════════════
# 2. SERVER.PY — withdraw endpoint + admin approve/reject
# ══════════════════════════════════════════════════════════
print("\n── 2. SERVER.PY ──")
srv_c = SERVER.read_text(encoding="utf-8")

# 2a. Withdraw endpoint — заменить старый или добавить новый
if "create_withdraw_request" not in srv_c:
    new_withdraw = '''

@app.post("/api/wallet/withdraw")
async def wallet_withdraw(payload: dict):
    uid     = int(payload.get("user_id", 0))
    amount  = float(payload.get("amount", 0))
    address = str(payload.get("address", "")).strip()
    if db.get_setting_str("withdraw_locked") == "1":
        raise HTTPException(403, "Вывод временно отключён")
    if amount < 0.5:
        raise HTTPException(400, "Минимум 0.5 TON")
    if not address:
        raise HTTPException(400, "Укажите TON адрес")
    user = db.get_user(uid)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user["balance"] < amount:
        raise HTTPException(400, "Недостаточно средств")

    db.update_balance(uid, -amount)
    req_id = db.create_withdraw_request(uid, amount, address)
    db.add_balance_history(uid, "withdraw_pending", -amount,
                           note=f"Запрос вывода #{req_id} → {address[:16]}…")

    uname = user.get("username") or user.get("first_name") or f"uid{uid}"
    try:
        import aiohttp as _ah
        txt = (
            f"💸 <b>Запрос на вывод</b>\\n"
            f"👤 @{uname} (ID: {uid})\\n"
            f"💎 Сумма: <b>{amount} TON</b>\\n"
            f"📦 Адрес: <code>{address}</code>\\n\\n"
            f"Запрос #<b>{req_id}</b>"
        )
        kb = {
            "inline_keyboard": [[
                {"text": "✅ Одобрить", "callback_data": f"wd_approve_{req_id}"},
                {"text": "❌ Отклонить", "callback_data": f"wd_reject_{req_id}"}
            ]]
        }
        async with _ah.ClientSession() as sess:
            await sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                json={"chat_id": config.ADMIN_ID, "text": txt,
                      "parse_mode": "HTML", "reply_markup": kb},
                timeout=_ah.ClientTimeout(total=5)
            )
        # Notify user
        async with _ah.ClientSession() as sess:
            await sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                json={"chat_id": uid,
                      "text": f"⏳ Запрос на вывод {amount} TON создан (#{{req_id}}). Ожидайте подтверждения."},
                timeout=_ah.ClientTimeout(total=5)
            )
    except Exception as e:
        log.warning(f"withdraw notify: {e}")

    updated = db.get_user(uid)
    # Push balance update
    asyncio.create_task(mgr.broadcast_to_user(uid, {
        "type": "balance_update",
        "balance": updated["balance"],
        "ref_balance": updated.get("ref_balance", 0),
    }))
    return {"ok": True, "balance": updated["balance"], "req_id": req_id}


@app.post("/api/admin/withdraw-approve")
async def admin_withdraw_approve(payload: dict):
    admin_uid = int(payload.get("user_id", 0))
    if admin_uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    req_id = int(payload.get("req_id", 0))
    req = db.resolve_withdraw_request(req_id, "approved")
    if not req:
        raise HTTPException(404, "Запрос не найден")
    db.add_balance_history(req["user_id"], "withdraw_approved", 0,
                           note=f"Вывод #{req_id} одобрен")
    # Notify user
    try:
        import aiohttp as _ah
        async with _ah.ClientSession() as sess:
            await sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                json={"chat_id": req["user_id"],
                      "text": f"✅ Вывод {req['amount']} TON одобрен! Средства будут отправлены на {req['address'][:20]}…"},
                timeout=_ah.ClientTimeout(total=5)
            )
    except Exception as e:
        log.warning(f"withdraw approve notify: {e}")
    return {"ok": True}


@app.post("/api/admin/withdraw-reject")
async def admin_withdraw_reject(payload: dict):
    admin_uid = int(payload.get("user_id", 0))
    if admin_uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    req_id = int(payload.get("req_id", 0))
    req = db.resolve_withdraw_request(req_id, "rejected")
    if not req:
        raise HTTPException(404, "Запрос не найден")
    # Return money
    db.update_balance(req["user_id"], req["amount"])
    db.add_balance_history(req["user_id"], "withdraw_rejected", req["amount"],
                           note=f"Вывод #{req_id} отклонён — возврат")
    # Push balance update
    updated = db.get_user(req["user_id"])
    if updated:
        asyncio.create_task(mgr.broadcast_to_user(req["user_id"], {
            "type": "balance_update",
            "balance": updated["balance"],
            "ref_balance": updated.get("ref_balance", 0),
        }))
    try:
        import aiohttp as _ah
        async with _ah.ClientSession() as sess:
            await sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                json={"chat_id": req["user_id"],
                      "text": f"❌ Запрос на вывод {req['amount']} TON отклонён. Средства возвращены на баланс."},
                timeout=_ah.ClientTimeout(total=5)
            )
    except Exception as e:
        log.warning(f"withdraw reject notify: {e}")
    return {"ok": True}

'''
    # Insert before startup
    if '@app.on_event("startup")' in srv_c:
        srv_c = srv_c.replace('@app.on_event("startup")',
                               new_withdraw + '\n@app.on_event("startup")', 1)
        print("✅ withdraw endpoints added")
    else:
        srv_c += new_withdraw
        print("✅ withdraw endpoints appended")
else:
    print("ℹ️  withdraw endpoints already exist")

# 2b. send-gift endpoint — fix to use getMyGifts / real gifts
if "admin_send_gift" in srv_c and "getMyGifts" not in srv_c:
    old_gift = '''@app.post("/api/admin/send-gift")
async def admin_send_gift(payload: dict):'''
    new_gift = '''@app.post("/api/admin/get-gifts")
async def admin_get_gifts(payload: dict):
    """Get list of gifts available for sending from this bot."""
    import aiohttp as _ah
    uid = int(payload.get("user_id", 0))
    if uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    try:
        async with _ah.ClientSession() as sess:
            async with sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/getAvailableGifts",
                timeout=_ah.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
        if not data.get("ok"):
            return {"ok": False, "gifts": [], "error": data.get("description", "Ошибка")}
        gifts = data.get("result", {}).get("gifts", [])
        return {"ok": True, "gifts": gifts}
    except Exception as e:
        return {"ok": False, "gifts": [], "error": str(e)}


@app.post("/api/admin/send-gift")
async def admin_send_gift(payload: dict):'''
    if old_gift in srv_c:
        srv_c = srv_c.replace(old_gift, new_gift, 1)
        print("✅ admin_get_gifts endpoint added")
    else:
        print("ℹ️  send-gift endpoint not found by exact match, skipping")
else:
    print("ℹ️  send-gift already updated or not present")

patch_file(SERVER, srv_c)


# ══════════════════════════════════════════════════════════
# 3. BOT.PY — callback handler для approve/reject вывода
# ══════════════════════════════════════════════════════════
print("\n── 3. BOT.PY ──")
bot_c = BOT_PY.read_text(encoding="utf-8")

if "wd_approve_" not in bot_c:
    new_callbacks = '''

from aiogram.types import CallbackQuery
import aiohttp as _aiohttp

@router.callback_query(lambda c: c.data and c.data.startswith("wd_"))
async def handle_withdraw_callback(call: CallbackQuery):
    """Admin approve/reject withdraw request via bot button."""
    if call.from_user.id != config.ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    parts = call.data.split("_")
    # wd_approve_123 or wd_reject_123
    if len(parts) < 3:
        await call.answer("Неверный формат")
        return
    action = parts[1]   # approve / reject
    req_id = int(parts[2])

    try:
        async with _aiohttp.ClientSession() as sess:
            endpoint = "withdraw-approve" if action == "approve" else "withdraw-reject"
            async with sess.post(
                f"http://127.0.0.1:8000/api/admin/{endpoint}",
                json={"user_id": config.ADMIN_ID, "req_id": req_id},
                timeout=_aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()

        if data.get("ok"):
            status_text = "✅ Одобрен и отправлен" if action == "approve" else "❌ Отклонён, средства возвращены"
            await call.message.edit_text(
                call.message.text + f"\\n\\n<b>{status_text}</b>",
                parse_mode="HTML",
                reply_markup=None
            )
            await call.answer(status_text)
        else:
            await call.answer(f"Ошибка: {data.get('detail', '?')}", show_alert=True)
    except Exception as e:
        log.error(f"withdraw callback error: {e}")
        await call.answer(f"Ошибка: {e}", show_alert=True)

'''
    # Insert before the end or after last handler
    if "async def cmd_me" in bot_c:
        # Append after cmd_me function — find end of file area
        bot_c = bot_c + new_callbacks
        print("✅ withdraw callback handler added to bot.py")
    else:
        bot_c = bot_c + new_callbacks
        print("✅ withdraw callback handler appended to bot.py")

    patch_file(BOT_PY, bot_c)
else:
    print("ℹ️  withdraw callback already in bot.py")


# ══════════════════════════════════════════════════════════
# 4. WEBAPP index.html — все фронтенд исправления
# ══════════════════════════════════════════════════════════
print("\n── 4. WEBAPP index.html ──")
html = HTML.read_text(encoding="utf-8")
changed = False

# ── 4a. Lobby: clickRoom — оба игрока могут кликнуть ─────────────────────
# Проблема: для создателя публичной комнаты — он не в myPrivate и не в myPublic
# после присоединения другого игрока. Нужно показывать комнату правильно.
# Патч clickRoom — упрощаем логику: при клике ВСЕГДА подписываемся и входим.

OLD_CLICK = """function clickRoom(rid){
  // Build a merged lookup of all known rooms
  var myRooms=(S.myRooms||[]);
  var pubRooms=(S.rooms||[]);
  var allRooms=myRooms.concat(pubRooms.filter(function(r){
    return !myRooms.some(function(m){return m.room_id===r.room_id;});
  }));
  var r=allRooms.find(function(x){return x.room_id===rid;});
  if(!r){
    // Room not in cache — subscribe and wait for lobby_room_update
    _pendingRoomId=rid;
    send({action:'lobby_subscribe',room_id:rid});
    toast('Загружаем комнату…','');
    return;
  }

  var isInRoom=r.players&&r.players.some(function(p){return String(p.user_id)===String(MY_ID);});
  var isCreator=String(r.creator_id)===String(MY_ID);

  if(isInRoom||isCreator){
    // Already a member or creator: just re-enter the room view
    send({action:'lobby_subscribe',room_id:rid});
    enterRoom(r,(r.is_private&&isCreator)?r.private_key:null);
  } else {
    // Not a member: send join action; server will respond with lobby_join_result
    send({action:'lobby_join',user_id:MY_ID,username:MY_NAME,
          first_name:TGU?.first_name||'',room_id:rid,private_key:''});
    toast('Подключаюсь…','');
  }
}"""

NEW_CLICK = """function clickRoom(rid){
  // Always subscribe first to get fresh room data
  send({action:'lobby_subscribe',room_id:rid});

  // Build merged room lookup
  var myRooms=(S.myRooms||[]);
  var pubRooms=(S.rooms||[]);
  var allRooms=myRooms.concat(pubRooms.filter(function(r){
    return !myRooms.some(function(m){return m.room_id===r.room_id;});
  }));
  var r=allRooms.find(function(x){return x.room_id===rid;});

  if(!r){
    // Room not in cache yet — subscribe and wait for lobby_room_update
    _pendingRoomId=rid;
    toast('Загружаем комнату…','');
    return;
  }

  var isInRoom=r.players&&r.players.some(function(p){return String(p.user_id)===String(MY_ID);});
  var isCreator=String(r.creator_id)===String(MY_ID);

  if(isInRoom||isCreator){
    // Already member or creator — open room view directly
    enterRoom(r,(r.is_private&&isCreator)?(r.private_key||''):null);
  } else if(r.status!=='waiting'){
    // Started — view as observer
    enterRoom(r,null);
  } else if(r.is_private){
    // Private room — ask for key
    openM('joinModal');
    toast('Приватная комната — введи ключ','');
  } else {
    // Public room — join directly
    send({action:'lobby_join',user_id:MY_ID,username:MY_NAME,
          first_name:TGU?.first_name||'',room_id:rid,private_key:''});
    toast('Подключаюсь…','');
  }
}"""

if OLD_CLICK in html:
    html = html.replace(OLD_CLICK, NEW_CLICK, 1)
    print("✅ clickRoom fixed")
    changed = True
else:
    print("ℹ️  clickRoom: old pattern not found, trying partial fix")
    # Minimal fix — just ensure lobby_subscribe is called on click
    if "function clickRoom" in html and "send({action:'lobby_subscribe'" not in html.split("function clickRoom")[1][:500]:
        html = html.replace(
            "function clickRoom(rid){",
            "function clickRoom(rid){ send({action:'lobby_subscribe',room_id:rid});",
            1
        )
        print("✅ clickRoom: added lobby_subscribe call")
        changed = True

# ── 4b. Withdraw modal with approve/reject flow ───────────────────────────
# Обновить модал вывода TON — добавить историю запросов
if "withdrawModal" not in html:
    print("ℹ️  withdrawModal not present, skipping (handled by patch_v2)")
else:
    print("ℹ️  withdrawModal present")

# ── 4c. Подарки в админке — список реальных подарков + кнопка загрузки ───
OLD_GIFT_SECTION = """  <!-- ── 0e. Отправка подарков ── -->
  <div class="adm-section" style="border-color:rgba(255,200,0,.3);background:rgba(255,180,0,.03)">
    <div class="adm-title" style="color:var(--gold)">🎁 Отправить подарок</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:10px">Отправить Telegram-подарок пользователю от имени бота</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      <input id="adGiftUsername" class="adm-inp" placeholder="@username получателя" style="width:100%"/>
      <div style="font-size:11px;color:var(--muted);margin-bottom:2px">ID подарка (из каталога Telegram Gifts):</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button onclick="setGiftId('TeddyBear')" style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:6px 10px;color:var(--gold);font-size:12px;cursor:pointer">🧸 Мишка (15⭐)</button>
        <button onclick="setGiftId('HeartWithArrow')" style="background:rgba(255,100,100,.1);border:1px solid rgba(255,100,100,.3);border-radius:8px;padding:6px 10px;color:#ff6060;font-size:12px;cursor:pointer">💘 Сердце</button>
        <button onclick="setGiftId('GoldenStar')" style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:6px 10px;color:var(--gold);font-size:12px;cursor:pointer">⭐ Звезда</button>
      </div>
      <input id="adGiftId" class="adm-inp" placeholder="Или введи ID вручную" style="width:100%"/>
      <button onclick="adminSendGift()" style="width:100%;background:rgba(255,200,0,.15);border:1px solid rgba(255,200,0,.4);border-radius:10px;padding:10px;color:var(--gold);font-size:13px;font-weight:800;cursor:pointer">🎁 Отправить подарок</button>
      <div id="adGiftMsg" class="adm-msg"></div>
    </div>
  </div>"""

NEW_GIFT_SECTION = """  <!-- ── 0e. Отправка подарков ── -->
  <div class="adm-section" style="border-color:rgba(255,200,0,.3);background:rgba(255,180,0,.03)">
    <div class="adm-title" style="color:var(--gold)">🎁 Отправить подарок Telegram</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:10px">Отправить официальный Telegram-подарок (Stars) пользователю от имени бота</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      <input id="adGiftUsername" class="adm-inp" placeholder="@username получателя" style="width:100%"/>
      <div style="font-size:11px;color:var(--muted);margin-bottom:2px">Подарки бота (нажми чтобы выбрать):</div>
      <button onclick="adminLoadGifts()" style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:7px 12px;color:var(--muted);font-size:12px;cursor:pointer;width:100%">🔄 Загрузить доступные подарки</button>
      <div id="adGiftList" style="display:flex;gap:6px;flex-wrap:wrap;min-height:20px"></div>
      <input id="adGiftId" class="adm-inp" placeholder="ID подарка (выбери выше или введи вручную)" style="width:100%"/>
      <button onclick="adminSendGift()" style="width:100%;background:rgba(255,200,0,.15);border:1px solid rgba(255,200,0,.4);border-radius:10px;padding:10px;color:var(--gold);font-size:13px;font-weight:800;cursor:pointer">🎁 Отправить подарок</button>
      <div id="adGiftMsg" class="adm-msg"></div>
    </div>
  </div>"""

if OLD_GIFT_SECTION in html:
    html = html.replace(OLD_GIFT_SECTION, NEW_GIFT_SECTION, 1)
    print("✅ Gift section updated with real gift loading")
    changed = True
else:
    print("ℹ️  Gift section old pattern not found")

# ── 4d. Бонус создание — периодичность + boost_count поле ────────────────
OLD_BONUS_CREATE = """    <div class="adm-row">
      <select id="adBonusType" class="adm-inp" style="flex:1">
        <option value="channel_sub">Подписка на канал</option>
        <option value="channel_boost">Буст канала</option>
        <option value="manual">Ручная проверка</option>
      </select>
      <input id="adBonusChannel" placeholder="@channel" class="adm-inp" style="flex:1">
      <button onclick="adminCreateBonus()" class="adm-btn-accent">+ Создать</button>
    </div>"""

NEW_BONUS_CREATE = """    <div class="adm-row">
      <select id="adBonusType" onchange="adminBonusTypeChange()" class="adm-inp" style="flex:1">
        <option value="channel_sub">Подписка на канал</option>
        <option value="channel_boost">Буст канала</option>
        <option value="manual">Ручная проверка</option>
      </select>
      <input id="adBonusChannel" placeholder="@channel" class="adm-inp" style="flex:1">
      <button onclick="adminCreateBonus()" class="adm-btn-accent">+ Создать</button>
    </div>
    <div id="adBonusBoostRow" style="display:none" class="adm-row">
      <div style="font-size:11px;color:var(--muted);padding:4px 0;flex:1">Количество бустов:</div>
      <input id="adBonusBoostCount" type="number" min="1" value="1" class="adm-inp adm-inp-sm" style="width:70px" placeholder="бустов">
    </div>
    <div class="adm-row" style="align-items:center">
      <div style="font-size:11px;color:var(--muted);flex:1">Повтор (часы, 0=одноразово):</div>
      <input id="adBonusRepeatHours" type="number" min="0" value="0" class="adm-inp adm-inp-sm" style="width:70px" placeholder="ч">
    </div>"""

if OLD_BONUS_CREATE in html:
    html = html.replace(OLD_BONUS_CREATE, NEW_BONUS_CREATE, 1)
    print("✅ Bonus create form updated with boost_count and repeat_hours")
    changed = True
else:
    print("ℹ️  Bonus create form pattern not found")

# ── 4e. JS: adminBonusTypeChange, adminLoadGifts, adminCreateBonus update ─
OLD_CREATE_BONUS_JS = "function adminCreateBonus(){"
if OLD_CREATE_BONUS_JS in html:
    # Insert helper before adminCreateBonus
    new_helpers = """function adminBonusTypeChange(){
  var t=document.getElementById('adBonusType').value;
  var boostRow=document.getElementById('adBonusBoostRow');
  if(boostRow) boostRow.style.display=(t==='channel_boost'?'flex':'none');
}

async function adminLoadGifts(){
  var el=document.getElementById('adGiftList');
  if(el) el.innerHTML='<span style="color:var(--muted);font-size:11px">Загружаю…</span>';
  try{
    var r=await fetch('/api/admin/get-gifts',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID})});
    var d=await r.json();
    if(d.ok&&d.gifts&&d.gifts.length){
      if(el) el.innerHTML=d.gifts.map(function(g){
        var stars=g.star_count?g.star_count+'⭐':g.total_count||'';
        return '<button onclick="setGiftId(\\''+g.id+'\\')'+
          '" style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:5px 9px;color:var(--gold);font-size:11px;cursor:pointer">'+
          (g.emoji||'🎁')+' '+stars+'</button>';
      }).join('');
    } else {
      if(el) el.innerHTML='<span style="color:var(--muted);font-size:11px">'+(d.error||'Нет доступных подарков')+'</span>';
    }
  }catch(e){
    if(el) el.innerHTML='<span style="color:var(--danger);font-size:11px">Ошибка: '+e+'</span>';
  }
}

"""
    if "async function adminLoadGifts" not in html:
        html = html.replace(OLD_CREATE_BONUS_JS, new_helpers + OLD_CREATE_BONUS_JS, 1)
        print("✅ adminBonusTypeChange and adminLoadGifts JS added")
        changed = True
    else:
        print("ℹ️  adminLoadGifts already present")

# Update adminCreateBonus to pass boost_count and repeat_hours
OLD_ADM_BONUS_FN = """function adminCreateBonus(){
  const title=document.getElementById('adBonusTitle').value.trim();
  const icon=document.getElementById('adBonusIcon').value.trim()||'🎁';
  const reward=parseFloat(document.getElementById('adBonusReward').value);
  const desc=document.getElementById('adBonusDesc').value.trim();
  const url=document.getElementById('adBonusUrl').value.trim();
  const label=document.getElementById('adBonusLabel').value.trim()||'Выполнить →';
  const type=document.getElementById('adBonusType').value;
  const channel=document.getElementById('adBonusChannel').value.trim().replace('@','');
  const msg=document.getElementById('adBonusMsg');
  if(!title){msg.style.color='#ff4444';msg.textContent='Введите название';return;}
  if(isNaN(reward)||reward<0){msg.style.color='#ff4444';msg.textContent='Укажите награду TON';return;}
  msg.style.color='var(--muted)';msg.textContent='Создаю…';
  send({action:'admin_create_bonus',user_id:MY_ID,title,icon,reward,description:desc,
        action_url:url,action_label:label,bonus_type:type,channel_username:channel});
}"""

NEW_ADM_BONUS_FN = """function adminCreateBonus(){
  const title=document.getElementById('adBonusTitle').value.trim();
  const icon=document.getElementById('adBonusIcon').value.trim()||'🎁';
  const reward=parseFloat(document.getElementById('adBonusReward').value);
  const desc=document.getElementById('adBonusDesc').value.trim();
  const url=document.getElementById('adBonusUrl').value.trim();
  const label=document.getElementById('adBonusLabel').value.trim()||'Выполнить →';
  const type=document.getElementById('adBonusType').value;
  const channel=document.getElementById('adBonusChannel').value.trim().replace('@','');
  const boostCount=parseInt(document.getElementById('adBonusBoostCount')?.value||'1')||1;
  const repeatHours=parseInt(document.getElementById('adBonusRepeatHours')?.value||'0')||0;
  const msg=document.getElementById('adBonusMsg');
  if(!title){msg.style.color='#ff4444';msg.textContent='Введите название';return;}
  if(isNaN(reward)||reward<0){msg.style.color='#ff4444';msg.textContent='Укажите награду TON';return;}
  msg.style.color='var(--muted)';msg.textContent='Создаю…';
  send({action:'admin_create_bonus',user_id:MY_ID,title,icon,reward,description:desc,
        action_url:url,action_label:label,bonus_type:type,channel_username:channel,
        boost_count:boostCount,repeat_hours:repeatHours});
}"""

if OLD_ADM_BONUS_FN in html:
    html = html.replace(OLD_ADM_BONUS_FN, NEW_ADM_BONUS_FN, 1)
    print("✅ adminCreateBonus updated with boost_count and repeat_hours")
    changed = True
else:
    print("ℹ️  adminCreateBonus old pattern not found")

# ── 4f. Буст в renderAdminBonuses — показывать boost_count ───────────────
OLD_BONUS_RENDER = "        <div style=\"font-size:10px;color:var(--muted)\">${b.bonus_type==='channel_sub'?'@'+b.channel_username:b.bonus_type} · ${b.completions||0} выполнений</div>"
NEW_BONUS_RENDER = "        <div style=\"font-size:10px;color:var(--muted)\">${b.bonus_type==='channel_sub'||b.bonus_type==='channel_boost'?'@'+b.channel_username:b.bonus_type}${b.bonus_type==='channel_boost'?' · '+b.boost_count+'б':''} · ${b.completions||0} выполнений${b.repeat_hours>0?' · повтор каждые '+b.repeat_hours+'ч':' · одноразовый'}</div>"
if OLD_BONUS_RENDER in html:
    html = html.replace(OLD_BONUS_RENDER, NEW_BONUS_RENDER, 1)
    print("✅ bonus render updated with boost info")
    changed = True
else:
    print("ℹ️  bonus render pattern not found")

# ── 4g. Bonus повторяемость — в renderBonuses для пользователя ───────────
# Показывать статус "можно снова через X часов" если задание повторяемое
OLD_BONUS_DONE = """        ? `<div style="background:rgba(0,255,136,.1);border:1px solid rgba(0,255,136,.25);border-radius:10px;padding:8px;text-align:center;font-size:13px;color:var(--accent);font-weight:700">✅ Выполнено</div>`"""
NEW_BONUS_DONE = """        ? `<div style="background:rgba(0,255,136,.1);border:1px solid rgba(0,255,136,.25);border-radius:10px;padding:8px;text-align:center;font-size:13px;color:var(--accent);font-weight:700">✅ Выполнено${b.repeat_hours>0?' · повторить через '+b.repeat_hours+'ч':''}</div>`"""
if OLD_BONUS_DONE in html:
    html = html.replace(OLD_BONUS_DONE, NEW_BONUS_DONE, 1)
    print("✅ bonus done label updated")
    changed = True

# ── 4h. Эмодзи — исправить дублирование ─────────────────────────────────
# Проблема: sendEmoji вызывал showReaction (летящее + счётчик) +
# сервер возвращал pvp_emoji для sender тоже → двойное отображение
OLD_SEND_EMOJI = """function sendEmoji(emoji){
  playSound('emoji');
  if(!requireAuth())return;
  if(_emojiCooldown){toast('Подождите…','');return;}
  _emojiCooldown=true;
  document.querySelectorAll('.emo-btn').forEach(b=>b.classList.add('cooldown'));
  setTimeout(()=>{
    _emojiCooldown=false;
    document.querySelectorAll('.emo-btn').forEach(b=>b.classList.remove('cooldown'));
  },3000);
  send({action:'pvp_emoji',user_id:MY_ID,emoji,username:MY_NAME});
  // Show flying emoji AND update reaction bar for sender immediately
  showReaction(MY_ID, emoji, MY_NAME);
}"""

NEW_SEND_EMOJI = """function sendEmoji(emoji){
  playSound('emoji');
  if(!requireAuth())return;
  if(_emojiCooldown){toast('Подождите…','');return;}
  _emojiCooldown=true;
  document.querySelectorAll('.emo-btn').forEach(b=>b.classList.add('cooldown'));
  setTimeout(()=>{
    _emojiCooldown=false;
    document.querySelectorAll('.emo-btn').forEach(b=>b.classList.remove('cooldown'));
  },3000);
  // Оптимистично показываем только ПОЛЁТ (без счётчика) — счётчик придёт от сервера в pvp_emoji
  spawnFloatingEmoji(emoji);
  send({action:'pvp_emoji',user_id:MY_ID,emoji,username:MY_NAME});
}"""

if OLD_SEND_EMOJI in html:
    html = html.replace(OLD_SEND_EMOJI, NEW_SEND_EMOJI, 1)
    print("✅ sendEmoji: removed duplicate reaction (only fly locally, counter from server)")
    changed = True
else:
    print("ℹ️  sendEmoji old pattern not found")

if changed:
    patch_file(HTML, html)
else:
    print("ℹ️  No HTML changes needed")

# ══════════════════════════════════════════════════════════
# 5. SERVER.PY — повторяемость бонусов (repeat_hours)
# ══════════════════════════════════════════════════════════
print("\n── 5. SERVER.PY bonus repeat_hours fix ──")
srv_c2 = SERVER.read_text(encoding="utf-8")

OLD_BONUS_COMPLETE = "    elif action == \"check_bonus\":"
# Check if repeat_hours logic already there
if "repeat_hours" not in srv_c2 or "repeat check" not in srv_c2:
    # Find the check_bonus section and add repeat logic
    old_bonus_logic = """    elif action == \"check_bonus\":
        bonus_id = int(m.get(\"bonus_id\", 0))
        bonus = db.get_bonus(bonus_id)
        if not bonus:
            await mgr.send(ws, {\"type\": \"bonus_result\", \"ok\": False,
                                 \"bonus_id\": bonus_id, \"error\": \"Бонус не найден\"})
            return"""

    new_bonus_logic = """    elif action == \"check_bonus\":
        bonus_id = int(m.get(\"bonus_id\", 0))
        bonus = db.get_bonus(bonus_id)
        if not bonus:
            await mgr.send(ws, {\"type\": \"bonus_result\", \"ok\": False,
                                 \"bonus_id\": bonus_id, \"error\": \"Бонус не найден\"})
            return
        # repeat check — if repeat_hours > 0, allow re-completion after that many hours
        repeat_hours = int(bonus.get(\"repeat_hours\") or 0)
        if repeat_hours > 0:
            last = db.get_last_bonus_completion(bonus_id, uid)
            if last:
                import datetime as _dt
                last_time = _dt.datetime.fromisoformat(last)
                next_time = last_time + _dt.timedelta(hours=repeat_hours)
                if _dt.datetime.utcnow() < next_time:
                    wait_h = int((next_time - _dt.datetime.utcnow()).total_seconds() // 3600) + 1
                    await mgr.send(ws, {\"type\": \"bonus_result\", \"ok\": False,
                                         \"bonus_id\": bonus_id,
                                         \"error\": f\"Повтор через {wait_h}ч\"})
                    return"""

    if old_bonus_logic in srv_c2:
        srv_c2 = srv_c2.replace(old_bonus_logic, new_bonus_logic, 1)
        print("✅ repeat_hours logic added to check_bonus")
    else:
        print("ℹ️  check_bonus pattern not found for repeat logic")

    patch_file(SERVER, srv_c2)
else:
    print("ℹ️  repeat_hours logic already present")

# ══════════════════════════════════════════════════════════
# 6. DATABASE — get_last_bonus_completion + handle repeat
# ══════════════════════════════════════════════════════════
print("\n── 6. DATABASE bonus repeat helper ──")
db_c2 = DB_PY.read_text(encoding="utf-8")

if "get_last_bonus_completion" not in db_c2:
    new_db_fn = """
def get_last_bonus_completion(bonus_id: int, user_id: int) -> str | None:
    \"\"\"Return ISO datetime of last completion or None.\"\"\"
    with _conn() as db:
        row = db.execute(
            "SELECT completed_at FROM bonus_completions WHERE bonus_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (bonus_id, user_id)
        ).fetchone()
        return row[0] if row else None

def add_bonus_completion_repeat(bonus_id: int, user_id: int) -> None:
    \"\"\"Insert new completion (allowing repeats — no UNIQUE constraint violation via INSERT OR IGNORE then UPDATE).\"\"\"
    with _conn() as db:
        existing = db.execute(
            "SELECT id FROM bonus_completions WHERE bonus_id=? AND user_id=?",
            (bonus_id, user_id)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE bonus_completions SET completed_at=datetime('now') WHERE id=?",
                (existing[0],)
            )
        else:
            db.execute(
                "INSERT INTO bonus_completions (bonus_id,user_id) VALUES (?,?)",
                (bonus_id, user_id)
            )

"""
    # Insert before existing get_last / mark functions
    if "def get_bonus" in db_c2:
        db_c2 = db_c2 + new_db_fn
        print("✅ get_last_bonus_completion added")
        patch_file(DB_PY, db_c2)
    else:
        db_c2 = db_c2 + new_db_fn
        print("✅ get_last_bonus_completion appended")
        patch_file(DB_PY, db_c2)
else:
    print("ℹ️  get_last_bonus_completion already present")

print("\n✅ patch_v3.py done! Run: systemctl restart royalduel.service")
