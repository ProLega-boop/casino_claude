#!/usr/bin/env python3
"""
RoyalDuel patch_v2.py — Version 2.1
Fixes: lobby join, tournament history, Stars deposit, TON Connect deposit,
       withdrawal system, boost task type, admin gifts, version badge,
       withdraw lock admin, clickRoom.
Run from /root/ton-rolls-bot/:  python3 patch_v2.py
"""
import os, re, sys

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, 'webapp', 'index.html')
SRV  = os.path.join(BASE, 'server.py')
DB   = os.path.join(BASE, 'database.py')

# ── Version ────────────────────────────────────────────────────────────────
VERSION = "v2.1"
TON_WALLET = "UQD1EEE0_gMdmC8nfuTObf9BP-PDGDczyUZKww9Aa2_nizlI"
ADMIN_ID   = 5849412071
ADMIN_USERNAME = "prolega757"


def patch_server():
    with open(SRV, 'r', encoding='utf-8') as f:
        c = f.read()
    changed = False

    # ── 1. Stars deposit endpoint ──────────────────────────────────────────
    if '/api/wallet/deposit-stars' not in c:
        stars_code = '''

@app.post("/api/wallet/deposit-stars")
async def deposit_stars(payload: dict):
    """Create a Telegram Stars invoice link."""
    uid    = int(payload.get("user_id", 0))
    stars  = int(payload.get("stars", 50))
    if stars < 10:
        raise HTTPException(400, "Минимум 10 Stars")
    if db.get_setting_str("deposit_locked") == "1":
        raise HTTPException(403, "Пополнение временно отключено")
    user = db.get_user(uid)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    # 1 Star ≈ 0.013 TON (approximate, adjust as needed)
    STAR_TO_TON = 0.013
    ton_amount  = round(stars * STAR_TO_TON, 4)
    try:
        import aiohttp as _ah
        async with _ah.ClientSession() as sess:
            async with sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/createInvoiceLink",
                json={
                    "title": "Пополнение RoyalDuel",
                    "description": f"Зачисление {ton_amount:.4f} TON ({stars} ⭐)",
                    "payload": f"stars:{uid}:{stars}:{ton_amount}",
                    "provider_token": "",
                    "currency": "XTR",
                    "prices": [{"label": "Пополнение", "amount": stars}],
                },
                timeout=_ah.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
    except Exception as e:
        log.error(f"Stars invoice error: {e}")
        raise HTTPException(502, "Ошибка создания инвойса")
    if not data.get("ok"):
        raise HTTPException(502, data.get("description", "Ошибка Telegram"))
    return {"ok": True, "invoice_url": data["result"], "ton_amount": ton_amount}
'''
        if '@app.on_event("startup")' in c:
            c = c.replace('@app.on_event("startup")', stars_code + '\n@app.on_event("startup")', 1)
        changed = True
        print('✅ Stars deposit endpoint added')
    else:
        print('ℹ️  Stars deposit already present')

    # ── 2. Withdrawal request endpoint ────────────────────────────────────
    if '/api/wallet/withdraw' not in c:
        withdraw_code = '''

@app.post("/api/wallet/withdraw")
async def wallet_withdraw(payload: dict):
    """User requests TON withdrawal — notifies admin."""
    uid     = int(payload.get("user_id", 0))
    amount  = float(payload.get("amount", 0))
    address = str(payload.get("address", "")).strip()
    if db.get_setting_str("withdraw_locked") == "1":
        raise HTTPException(403, "Вывод временно отключён")
    if amount < 0.5:
        raise HTTPException(400, "Минимум 0.5 TON")
    if not address:
        raise HTTPException(400, "Укажи TON адрес кошелька")
    user = db.get_user(uid)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user["balance"] < amount:
        raise HTTPException(400, "Недостаточно средств")
    # Reserve balance
    db.update_balance(uid, -amount)
    db.add_balance_history(uid, "withdraw_pending", -amount,
                           note=f"Вывод {amount} TON → {address}")
    uname = user.get("username") or user.get("first_name") or f"uid{uid}"
    # Notify admin via bot
    try:
        import aiohttp as _ah
        msg = (
            f"📤 <b>Запрос на вывод</b>\\n"
            f"👤 @{uname} (ID: {uid})\\n"
            f"💎 Сумма: <b>{amount:.4f} TON</b>\\n"
            f"📬 Адрес: <code>{address}</code>\\n"
            f"\\nПодтвердите и отправьте вручную."
        )
        async with _ah.ClientSession() as sess:
            await sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                json={"chat_id": {ADMIN_ID}, "text": msg, "parse_mode": "HTML"},
                timeout=_ah.ClientTimeout(total=5)
            )
        # Notify user
        user_msg = (
            f"📤 Запрос на вывод {amount:.4f} TON принят!\\n"
            f"Адрес: {address}\\n"
            f"Ожидайте — обработка в течение 24 часов."
        )
        async with _ah.ClientSession() as sess:
            await sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                json={"chat_id": uid, "text": user_msg},
                timeout=_ah.ClientTimeout(total=5)
            )
    except Exception as e:
        log.warning(f"withdraw notify error: {e}")
    updated = db.get_user(uid)
    return {{"ok": True, "balance": updated["balance"] if updated else 0}}


@app.post("/api/admin/withdraw-lock")
async def admin_withdraw_lock(payload: dict):
    uid = int(payload.get("user_id", 0))
    if uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    locked = bool(payload.get("locked", False))
    db.set_setting_str("withdraw_locked", "1" if locked else "0")
    return {{"ok": True, "locked": locked}}
'''.format(ADMIN_ID=ADMIN_ID)
        if '@app.on_event("startup")' in c:
            c = c.replace('@app.on_event("startup")', withdraw_code + '\n@app.on_event("startup")', 1)
        changed = True
        print('✅ Withdraw endpoint added')
    else:
        print('ℹ️  Withdraw endpoint already present')

    # ── 3. Stars pre_checkout webhook ─────────────────────────────────────
    if '/api/stars-checkout' not in c:
        checkout_code = '''

@app.post("/api/stars-checkout")
async def stars_pre_checkout(payload: dict):
    """Handle pre_checkout_query for Stars (called by bot handler)."""
    return {"ok": True}


@app.post("/api/stars-payment")
async def stars_payment(payload: dict):
    """Handle successful Stars payment and credit balance."""
    invoice_payload = str(payload.get("invoice_payload", ""))
    # payload format: stars:{uid}:{stars}:{ton_amount}
    try:
        parts = invoice_payload.split(":")
        if parts[0] != "stars":
            return {"ok": True}
        uid        = int(parts[1])
        ton_amount = float(parts[3])
    except Exception:
        log.warning(f"stars_payment: bad payload {invoice_payload}")
        return {"ok": True}
    if db.get_setting_str("deposit_locked") == "1":
        return {"ok": True}
    user = db.get_user(uid)
    if not user:
        return {"ok": True}
    db.update_balance(uid, ton_amount)
    db.add_balance_history(uid, "deposit", ton_amount,
                           note=f"Stars deposit +{ton_amount} TON")
    updated = db.get_user(uid)
    import asyncio as _asyncio
    _asyncio.create_task(mgr.broadcast_to_user(uid, {
        "type": "balance_update",
        "balance": updated["balance"],
        "ref_balance": updated["ref_balance"],
    }))
    log.info(f"Stars payment: credited {ton_amount} TON to uid {uid}")
    return {"ok": True}
'''
        if '@app.on_event("startup")' in c:
            c = c.replace('@app.on_event("startup")', checkout_code + '\n@app.on_event("startup")', 1)
        changed = True
        print('✅ Stars payment handlers added')
    else:
        print('ℹ️  Stars payment already present')

    if changed:
        with open(SRV, 'w', encoding='utf-8') as f:
            f.write(c)
        print(f'✅ server.py saved')
    else:
        print('ℹ️  server.py no changes needed')


def patch_bot():
    BOT = os.path.join(BASE, 'bot.py')
    with open(BOT, 'r', encoding='utf-8') as f:
        c = f.read()
    changed = False

    # Add Stars pre_checkout and successful_payment handlers
    if 'pre_checkout_query' not in c:
        stars_handlers = '''

@router.pre_checkout_query()
async def pre_checkout_handler(query):
    """Approve all Stars payment pre-checkouts."""
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message()
async def successful_payment_handler(message):
    """Handle successful Stars payment."""
    if not message.successful_payment:
        return
    sp = message.successful_payment
    invoice_payload = sp.invoice_payload
    try:
        import aiohttp as _ah
        import asyncio as _asyncio
        async with _ah.ClientSession() as sess:
            await sess.post(
                f"http://localhost:{import_config().PORT}/api/stars-payment",
                json={"invoice_payload": invoice_payload},
                timeout=_ah.ClientTimeout(total=5)
            )
    except Exception as e:
        import logging as _log
        _log.getLogger("bot").warning(f"Stars payment relay error: {e}")


def import_config():
    import config as _cfg
    return _cfg
'''
        # Insert before the last line
        c = c.rstrip() + '\n' + stars_handlers + '\n'
        changed = True
        print('✅ Stars handlers added to bot.py')
    else:
        print('ℹ️  Stars handlers already present')

    if changed:
        with open(BOT, 'w', encoding='utf-8') as f:
            f.write(c)
        print('✅ bot.py saved')


def patch_database():
    with open(DB, 'r', encoding='utf-8') as f:
        c = f.read()
    changed = False

    if 'def get_setting_str' not in c:
        helpers = '''

def get_setting_str(key: str, fallback: str = "") -> str:
    try:
        with _conn() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else fallback
    except Exception:
        return fallback


def set_setting_str(key: str, value: str) -> None:
    with _conn() as db:
        db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))


def get_all_deposits(limit: int = 200) -> list:
    with _conn() as db:
        rows = db.execute(
            """SELECT bh.*, u.username, u.first_name
               FROM balance_history bh
               LEFT JOIN users u ON u.user_id = bh.user_id
               WHERE bh.kind = 'deposit'
               ORDER BY bh.id DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
'''
        c = c.rstrip() + helpers + '\n'
        changed = True
        print('✅ DB helpers added')
    else:
        print('ℹ️  DB helpers already present')

    if changed:
        with open(DB, 'w', encoding='utf-8') as f:
            f.write(c)
        print('✅ database.py saved')


def patch_html():
    with open(HTML, 'r', encoding='utf-8', errors='replace') as f:
        c = f.read()
    changed = False

    # ── 1. Version badge in admin panel ───────────────────────────────────
    OLD_ADMIN_H2 = '<h2 style="margin:0;font-size:18px;font-weight:900">ADMIN</h2>'
    NEW_ADMIN_H2 = f'<h2 style="margin:0;font-size:18px;font-weight:900">ADMIN</h2><div style="font-size:10px;color:var(--muted);font-weight:700;margin-left:8px">{VERSION}</div>'
    if VERSION not in c and OLD_ADMIN_H2 in c:
        c = c.replace(OLD_ADMIN_H2, NEW_ADMIN_H2)
        changed = True
        print(f'✅ Version badge {VERSION} added to admin panel')
    else:
        print('ℹ️  Version badge already present')

    # ── 2. Fix clickRoom ───────────────────────────────────────────────────
    if 'var myPrivate=(S.myRooms||[]).filter' in c:
        new_click = '''function clickRoom(rid){
  var myRooms=(S.myRooms||[]);
  var pubRooms=(S.rooms||[]);
  var allRooms=myRooms.concat(pubRooms.filter(function(r){
    return !myRooms.some(function(m){return m.room_id===r.room_id;});
  }));
  var r=allRooms.find(function(x){return x.room_id===rid;});
  if(!r){
    _pendingRoomId=rid;
    send({action:'lobby_subscribe',room_id:rid});
    toast('Загружаем комнату...','');
    return;
  }
  var isInRoom=r.players&&r.players.some(function(p){return String(p.user_id)===String(MY_ID);});
  var isCreator=String(r.creator_id)===String(MY_ID);
  if(isInRoom||isCreator){
    send({action:'lobby_subscribe',room_id:rid});
    enterRoom(r,(r.is_private&&isCreator)?r.private_key:null);
  } else {
    send({action:'lobby_join',user_id:MY_ID,username:MY_NAME,
          first_name:(TGU&&TGU.first_name)||'',room_id:rid,private_key:''});
    toast('Подключаюсь...','');
  }
}'''
        c = re.sub(
            r'function clickRoom\(rid\)\{.*?var myPrivate.*?\}(?=\s*\n)',
            new_click,
            c, flags=re.DOTALL
        )
        changed = True
        print('✅ clickRoom fixed')
    else:
        print('ℹ️  clickRoom already updated')

    # ── 3. Add _pendingRoomId ──────────────────────────────────────────────
    if '_pendingRoomId' not in c:
        c = c.replace('let ws=null,wsReady=false;',
                      'let ws=null,wsReady=false;\nlet _pendingRoomId=null;')
        changed = True
        print('✅ _pendingRoomId added')

    # ── 4. Fix lobby_room_update ───────────────────────────────────────────
    OLD_LRU = "    case 'lobby_room_update':\n      if(room.id===m.room?.room_id){\n        updateRoomFromServer(m.room);\n      }break;"
    NEW_LRU = """    case 'lobby_room_update':
      if(room.id===m.room?.room_id){
        updateRoomFromServer(m.room);
      } else if(!room.id&&m.room&&_pendingRoomId&&_pendingRoomId===m.room.room_id){
        _pendingRoomId=null;
        enterRoom(m.room,null);
      }break;"""
    if OLD_LRU in c:
        c = c.replace(OLD_LRU, NEW_LRU)
        changed = True
        print('✅ lobby_room_update fixed')
    else:
        print('ℹ️  lobby_room_update already fixed or different format')

    # ── 5. Fix tournament history — always render when visible ─────────────
    OLD_REND = "  if(!el)return;\n  // If panel is hidden, don't render\n  if(el.style.display==='none')return;"
    NEW_REND = "  if(!el||el.style.display==='none')return;"
    if OLD_REND in c:
        c = c.replace(OLD_REND, NEW_REND)
        changed = True
        print('✅ Tournament history render fixed')

    # ── 6. Add Stars deposit to deposit modal ─────────────────────────────
    OLD_DEP_FOOTER = "        <div style=\"font-size:10px;color:var(--muted2);text-align:center\">Нажимая «Пополнить» вы будете перенаправлены в CryptoPay для оплаты</div>"
    NEW_DEP_CONTENT = """        <!-- Stars deposit -->
        <div style="background:rgba(255,200,0,.06);border:1px solid rgba(255,200,0,.25);border-radius:14px;padding:14px">
          <div style="font-size:13px;font-weight:800;color:var(--gold);margin-bottom:4px">⭐ Пополнить через Stars</div>
          <div style="font-size:11px;color:var(--muted);margin-bottom:10px">Telegram Stars · ~0.013 TON за 1 Star · от 10 Stars</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
            <button onclick="setStarsAmt(50)" style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:6px 10px;color:var(--gold);font-size:12px;font-weight:700;cursor:pointer">50 ⭐</button>
            <button onclick="setStarsAmt(100)" style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:6px 10px;color:var(--gold);font-size:12px;font-weight:700;cursor:pointer">100 ⭐</button>
            <button onclick="setStarsAmt(250)" style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:6px 10px;color:var(--gold);font-size:12px;font-weight:700;cursor:pointer">250 ⭐</button>
            <button onclick="setStarsAmt(500)" style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:6px 10px;color:var(--gold);font-size:12px;font-weight:700;cursor:pointer">500 ⭐</button>
          </div>
          <div style="display:flex;gap:8px;margin-bottom:8px">
            <input id="depStarsAmt" type="number" min="10" step="10" value="50" class="adm-inp" placeholder="Stars" style="flex:1"/>
            <button onclick="submitDepositStars()" style="background:var(--gold);color:#000;border:none;border-radius:10px;padding:10px 16px;font-size:13px;font-weight:900;cursor:pointer;white-space:nowrap">Оплатить</button>
          </div>
          <div id="depStarsMsg" style="font-size:11px;color:var(--muted);min-height:16px"></div>
        </div>
        <div style="font-size:10px;color:var(--muted2);text-align:center">Нажимая «Пополнить» вы будете перенаправлены в CryptoPay для оплаты</div>"""
    if 'depStarsAmt' not in c and OLD_DEP_FOOTER in c:
        c = c.replace(OLD_DEP_FOOTER, NEW_DEP_CONTENT)
        changed = True
        print('✅ Stars deposit UI added to modal')
    else:
        print('ℹ️  Stars deposit UI already present or footer not found')

    # ── 7. Add Withdraw button to profile page ─────────────────────────────
    if 'withdrawModal' not in c:
        # Add button next to topup button
        c = c.replace(
            '<div class="topup-btn" onclick="openDepositModal()">💳 Пополнить</div>',
            '<div class="topup-btn" onclick="openDepositModal()">💳 Пополнить</div>'
            '<div class="topup-btn" onclick="openWithdrawModal()" style="margin-left:6px">📤 Вывод</div>'
        )
        # Add withdraw modal before sound modal
        withdraw_modal = '''
<!-- ═══════════ WITHDRAW MODAL ═══════════ -->
<div class="overlay" id="withdrawModal">
  <div class="sheet">
    <div class="handle"></div>
    <div class="m-head"><span class="m-title">📤 ВЫВОД TON</span><div class="m-close" onclick="closeM('withdrawModal')">✕</div></div>
    <div class="m-body">
      <div style="display:flex;flex-direction:column;gap:12px">
        <div style="background:rgba(255,150,0,.06);border:1px solid rgba(255,150,0,.25);border-radius:12px;padding:12px;font-size:12px;color:var(--muted)">
          ⚠️ Вывод обрабатывается вручную в течение 24 часов. После подачи заявки средства будут зарезервированы.
        </div>
        <div>
          <div style="font-size:12px;font-weight:700;margin-bottom:6px;color:var(--muted)">TON АДРЕС КОШЕЛЬКА</div>
          <input id="wdAddress" class="adm-inp" placeholder="UQ..." style="width:100%;box-sizing:border-box"/>
        </div>
        <div>
          <div style="font-size:12px;font-weight:700;margin-bottom:6px;color:var(--muted)">СУММА (TON)</div>
          <div style="display:flex;gap:8px">
            <input id="wdAmount" type="number" min="0.5" step="0.1" class="adm-inp" placeholder="0.5" style="flex:1"/>
            <button onclick="wdSetMax()" style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--muted);font-size:12px;cursor:pointer">MAX</button>
          </div>
        </div>
        <button onclick="submitWithdraw()" style="width:100%;background:linear-gradient(135deg,#ff9500,#ff6000);border:none;border-radius:12px;padding:14px;color:#fff;font-size:14px;font-weight:900;cursor:pointer">
          📤 Отправить заявку
        </button>
        <div id="wdMsg" style="font-size:12px;text-align:center;min-height:16px"></div>
      </div>
    </div>
  </div>
</div>
'''
        c = c.replace('<!-- ═══════════ SOUND SETTINGS MODAL ═══════════ -->', withdraw_modal + '\n<!-- ═══════════ SOUND SETTINGS MODAL ═══════════ -->')
        changed = True
        print('✅ Withdraw modal added')
    else:
        print('ℹ️  Withdraw modal already present')

    # ── 8. Add boost task type to admin dropdown ───────────────────────────
    if 'channel_boost' not in c:
        c = c.replace(
            '<option value="channel_sub">Подписка на канал</option>',
            '<option value="channel_sub">Подписка на канал</option>\n        <option value="channel_boost">Буст канала</option>'
        )
        changed = True
        print('✅ Boost task type added')
    else:
        print('ℹ️  Boost task already present')

    # ── 9. Add withdraw lock + gift section to admin panel ─────────────────
    if 'withdraw-lock' not in c and 'adm-section' in c:
        withdraw_admin = '''
  <!-- ── Блокировка вывода ── -->
  <div class="adm-section" style="border-color:rgba(255,100,0,.3);background:rgba(255,80,0,.03)">
    <div class="adm-title" style="color:#ff6400">📤 Вывод средств</div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:10px">Включить/выключить вывод для всех пользователей.</div>
    <div style="display:flex;gap:8px">
      <button onclick="adminSetWithdrawLock(true)" style="flex:1;background:rgba(255,68,68,.14);border:1px solid rgba(255,68,68,.4);border-radius:10px;padding:10px;color:#ff4444;font-size:13px;font-weight:800;cursor:pointer">🔒 Заблокировать</button>
      <button onclick="adminSetWithdrawLock(false)" style="flex:1;background:rgba(0,255,136,.1);border:1px solid rgba(0,255,136,.4);border-radius:10px;padding:10px;color:var(--accent);font-size:13px;font-weight:800;cursor:pointer">🔓 Разблокировать</button>
    </div>
    <div id="adWithdrawLockMsg" class="adm-msg" style="margin-top:6px"></div>
  </div>

  <!-- ── Отправка подарков ── -->
  <div class="adm-section" style="border-color:rgba(255,200,0,.3);background:rgba(255,180,0,.03)">
    <div class="adm-title" style="color:var(--gold)">🎁 Отправить подарок</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:10px">Telegram-подарок пользователю от имени бота (Stars)</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      <input id="adGiftUsername" class="adm-inp" placeholder="@username получателя" style="width:100%;box-sizing:border-box"/>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button onclick="setGiftId('TeddyBear')" style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:6px 10px;color:var(--gold);font-size:11px;cursor:pointer">🧸 Мишка (15⭐)</button>
        <button onclick="setGiftId('HeartWithArrow')" style="background:rgba(255,100,100,.1);border:1px solid rgba(255,100,100,.3);border-radius:8px;padding:6px 10px;color:#ff6060;font-size:11px;cursor:pointer">💘 Сердце (15⭐)</button>
        <button onclick="setGiftId('Rose')" style="background:rgba(255,100,100,.1);border:1px solid rgba(255,100,100,.3);border-radius:8px;padding:6px 10px;color:#ff6060;font-size:11px;cursor:pointer">🌹 Роза (25⭐)</button>
        <button onclick="setGiftId('GoldenStar')" style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:6px 10px;color:var(--gold);font-size:11px;cursor:pointer">⭐ Звезда</button>
      </div>
      <input id="adGiftId" class="adm-inp" placeholder="Или введи ID подарка вручную" style="width:100%;box-sizing:border-box"/>
      <button onclick="adminSendGift()" style="width:100%;background:rgba(255,200,0,.15);border:1px solid rgba(255,200,0,.4);border-radius:10px;padding:10px;color:var(--gold);font-size:13px;font-weight:800;cursor:pointer">🎁 Отправить</button>
      <div id="adGiftMsg" class="adm-msg"></div>
    </div>
  </div>
'''
        # Insert before closing of admin page
        c = c.replace('  <!-- ── 0d. История пополнений ── -->', withdraw_admin + '\n  <!-- ── 0d. История пополнений ── -->')
        changed = True
        print('✅ Admin withdraw lock + gift sections added')
    else:
        print('ℹ️  Admin sections already present')

    # ── 10. Inject all JS functions ────────────────────────────────────────
    js_tag = '</script>'
    tag_pos = c.rfind(js_tag)
    if tag_pos < 0:
        print('ERROR: no </script> tag found')
        return

    new_js = '''
// ═══════════ v2.1 FUNCTIONS ═══════════

function setStarsAmt(n){var i=document.getElementById('depStarsAmt');if(i)i.value=n;}

async function submitDepositStars(){
  var stars=parseInt(document.getElementById('depStarsAmt').value||'50');
  var msg=document.getElementById('depStarsMsg');
  if(isNaN(stars)||stars<10){if(msg){msg.style.color='red';msg.textContent='Минимум 10 Stars';}return;}
  if(msg){msg.style.color='gray';msg.textContent='Создаю инвойс...';}
  try{
    var r=await fetch('/api/wallet/deposit-stars',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,stars:stars})});
    var d=await r.json();
    if(d.ok&&d.invoice_url){
      if(msg){msg.style.color='green';msg.textContent='Открываем оплату...';}
      if(window.Telegram&&window.Telegram.WebApp&&window.Telegram.WebApp.openInvoice){
        window.Telegram.WebApp.openInvoice(d.invoice_url,function(status){
          if(status==='paid'){toast('Stars оплачены! Баланс пополнен','');}
        });
      } else {
        window.open(d.invoice_url,'_blank');
      }
    } else {
      if(msg){msg.style.color='red';msg.textContent='Ошибка: '+(d.detail||d.error||'unknown');}
    }
  }catch(e){if(msg){msg.style.color='red';msg.textContent='Ошибка соединения';}}
}

function openWithdrawModal(){
  if(S.balance<=0){toast('Нет средств для вывода','red');return;}
  openM('withdrawModal');
}

function wdSetMax(){
  var i=document.getElementById('wdAmount');
  if(i)i.value=(S.balance||0).toFixed(4);
}

async function submitWithdraw(){
  var addr=(document.getElementById('wdAddress')?.value||'').trim();
  var amt=parseFloat(document.getElementById('wdAmount')?.value||'0');
  var msg=document.getElementById('wdMsg');
  if(!addr){if(msg){msg.style.color='red';msg.textContent='Укажи адрес кошелька';}return;}
  if(isNaN(amt)||amt<0.5){if(msg){msg.style.color='red';msg.textContent='Минимум 0.5 TON';}return;}
  if(amt>S.balance){if(msg){msg.style.color='red';msg.textContent='Недостаточно средств';}return;}
  if(msg){msg.style.color='gray';msg.textContent='Отправляю заявку...';}
  try{
    var r=await fetch('/api/wallet/withdraw',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,amount:amt,address:addr})});
    var d=await r.json();
    if(d.ok){
      if(msg){msg.style.color='green';msg.textContent='Заявка принята! Ожидайте до 24ч.';}
      S.balance=d.balance;syncBalance();
      setTimeout(function(){closeM('withdrawModal');},2000);
    } else {
      if(msg){msg.style.color='red';msg.textContent='Ошибка: '+(d.detail||'unknown');}
    }
  }catch(e){if(msg){msg.style.color='red';msg.textContent='Ошибка соединения';}}
}

async function adminSetWithdrawLock(locked){
  var msg=document.getElementById('adWithdrawLockMsg');
  if(msg){msg.style.color='gray';msg.textContent='Применяю...';}
  try{
    var r=await fetch('/api/admin/withdraw-lock',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,locked:locked})});
    var d=await r.json();
    if(msg){
      if(d.ok){msg.style.color='green';msg.textContent=locked?'Вывод заблокирован':'Вывод разблокирован';}
      else{msg.style.color='red';msg.textContent='Ошибка';}
      setTimeout(function(){if(msg)msg.textContent='';},3000);
    }
  }catch(e){if(msg){msg.style.color='red';msg.textContent='Ошибка соединения';}}
}

function setGiftId(id){var i=document.getElementById('adGiftId');if(i)i.value=id;}

async function adminSendGift(){
  var u=(document.getElementById('adGiftUsername')?document.getElementById('adGiftUsername').value:'').trim();
  var g=(document.getElementById('adGiftId')?document.getElementById('adGiftId').value:'').trim();
  var msg=document.getElementById('adGiftMsg');
  if(!u||!g){if(msg){msg.style.color='red';msg.textContent='Заполни все поля';}return;}
  if(msg){msg.style.color='gray';msg.textContent='Отправляю...';}
  try{
    var r=await fetch('/api/admin/send-gift',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,target_username:u,gift_id:g})});
    var d=await r.json();
    if(msg){
      if(d.ok){msg.style.color='green';msg.textContent='Подарок отправлен!';}
      else{msg.style.color='red';msg.textContent='Ошибка: '+(d.detail||'unknown');}
      setTimeout(function(){if(msg)msg.textContent='';},4000);
    }
  }catch(e){if(msg){msg.style.color='red';msg.textContent='Ошибка соединения';}}
}

async function adminResetPvp(){
  if(!confirm('Сбросить колесо? Все ставки вернутся игрокам.'))return;
  var msg=document.getElementById('pvpResetMsg');
  if(msg){msg.style.color='gray';msg.textContent='Сбрасываю...';}
  try{
    var r=await fetch('/api/admin/pvp-reset',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID})});
    var d=await r.json();
    if(msg){
      if(d.ok){msg.style.color='green';msg.textContent='Сброшено, возвращено: '+(d.refunded||0);}
      else{msg.style.color='red';msg.textContent='Ошибка: '+(d.detail||'unknown');}
      setTimeout(function(){if(msg)msg.textContent='';},4000);
    }
  }catch(e){if(msg){msg.style.color='red';msg.textContent='Ошибка соединения';}}
}

async function adminSetDepositLock(locked){
  var msg=document.getElementById('adDepLockMsg');
  if(msg){msg.style.color='gray';msg.textContent='Применяю...';}
  try{
    var r=await fetch('/api/admin/deposit-lock',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,locked:locked})});
    var d=await r.json();
    if(msg){
      if(d.ok){msg.style.color='green';msg.textContent=locked?'Пополнения заблокированы':'Пополнения разблокированы';}
      else{msg.style.color='red';msg.textContent='Ошибка';}
      setTimeout(function(){if(msg)msg.textContent='';},3000);
    }
  }catch(e){if(msg){msg.style.color='red';msg.textContent='Ошибка соединения';}}
}

function adminLoadDeposits(){send({action:'admin_get_deposits',user_id:MY_ID});}

function openDepositModal(){openM('depositModal');}

async function submitDepositTon(){
  var amt=parseFloat(document.getElementById('depTonAmt').value);
  var msg=document.getElementById('depTonMsg');
  if(isNaN(amt)||amt<0.5){msg.style.color='red';msg.textContent='Минимум 0.5 TON';return;}
  msg.style.color='gray';msg.textContent='Создаю счёт...';
  try{
    var r=await fetch('/api/wallet/deposit-ton',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,amount:amt})});
    var d=await r.json();
    if(d.ok&&d.pay_url){
      msg.style.color='green';msg.textContent='Открываем оплату...';
      if(window.Telegram&&window.Telegram.WebApp)window.Telegram.WebApp.openLink(d.pay_url);
      else window.open(d.pay_url,'_blank');
    } else {msg.style.color='red';msg.textContent='Ошибка: '+(d.detail||d.error||'unknown');}
  }catch(e){msg.style.color='red';msg.textContent='Ошибка соединения';}
}

function renderAdminDeposits(entries){
  var el=document.getElementById('adDepositList');
  if(!el)return;
  if(!entries||!entries.length){el.innerHTML='<div style="text-align:center;padding:12px;color:var(--muted);font-size:12px">Нет пополнений</div>';return;}
  el.innerHTML=entries.map(function(e){
    var name=e.username?('@'+e.username):(e.first_name||'ID '+e.user_id);
    var d=new Date(e.created_at);
    var ds=(d.getDate())+'.'+(d.getMonth()+1)+' '+d.getHours()+':'+String(d.getMinutes()).padStart(2,'0');
    return '<div style="display:flex;align-items:center;gap:8px;padding:7px 10px;background:var(--surface);border:1px solid var(--border);border-radius:10px">'
      +'<div style="flex:1;min-width:0"><div style="font-size:12px;font-weight:700">'+name+'</div>'
      +'<div style="font-size:10px;color:var(--muted)">'+(e.note||'deposit')+' · '+ds+'</div></div>'
      +'<div style="font-size:13px;font-weight:900;color:var(--accent)">+'+parseFloat(e.amount).toFixed(2)+' TON</div></div>';
  }).join('');
}
'''
    c = c[:tag_pos] + new_js + '\n' + c[tag_pos:]
    changed = True
    print('✅ All JS functions injected')

    if changed:
        with open(HTML, 'w', encoding='utf-8', errors='replace') as f:
            f.write(c)
        print(f'✅ index.html saved ({len(c)} chars)')


if __name__ == '__main__':
    print(f'=== RoyalDuel Patch Script {VERSION} ===\n')
    print('--- Patching server.py ---')
    patch_server()
    print('\n--- Patching bot.py ---')
    patch_bot()
    print('\n--- Patching database.py ---')
    patch_database()
    print('\n--- Patching webapp/index.html ---')
    patch_html()
    print(f'\n=== Done! Run: systemctl restart royalduel.service ===')
