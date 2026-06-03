#!/usr/bin/env python3
"""
RoyalDuel patch script.
Run: python3 patch.py
Patches webapp/index.html and server.py in place.
"""
import os, re, sys

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, 'webapp', 'index.html')
SRV  = os.path.join(BASE, 'server.py')

def patch_html():
    with open(HTML, 'r', encoding='utf-8') as f:
        c = f.read()

    changed = False

    # ── 1. Fix clickRoom ──────────────────────────────────────────────────
    OLD_CLICK = '''function clickRoom(rid){
  var myPrivate=(S.myRooms||[]).filter(function(r){return r.is_private&&String(r.creator_id)===String(MY_ID);});
  var allRooms=myPrivate.concat(S.rooms||[]);
  var r=allRooms.find(function(x){return x.room_id===rid;});
  if(!r)return;
  send({action:'lobby_subscribe',room_id:rid});
  enterRoom(r,(r.is_private&&String(r.creator_id)===String(MY_ID))?r.private_key:null);
}'''

    NEW_CLICK = '''function clickRoom(rid){
  var myRooms=(S.myRooms||[]);
  var pubRooms=(S.rooms||[]);
  var allRooms=myRooms.concat(pubRooms.filter(function(r){
    return !myRooms.some(function(m){return m.room_id===r.room_id;});
  }));
  var r=allRooms.find(function(x){return x.room_id===rid;});
  if(!r){
    _pendingRoomId=rid;
    send({action:'lobby_subscribe',room_id:rid});
    toast('Загружаем комнату\u2026','');
    return;
  }
  var isInRoom=r.players&&r.players.some(function(p){return String(p.user_id)===String(MY_ID);});
  var isCreator=String(r.creator_id)===String(MY_ID);
  if(isInRoom||isCreator){
    send({action:'lobby_subscribe',room_id:rid});
    enterRoom(r,(r.is_private&&isCreator)?r.private_key:null);
  } else {
    send({action:'lobby_join',user_id:MY_ID,username:MY_NAME,
          first_name:TGU&&TGU.first_name||'',room_id:rid,private_key:''});
    toast('Подключаюсь\u2026','');
  }
}'''

    if OLD_CLICK in c:
        c = c.replace(OLD_CLICK, NEW_CLICK)
        print('✅ clickRoom — fixed')
        changed = True
    elif 'var myPrivate=(S.myRooms||[]).filter' in c:
        # Try regex fallback
        c = re.sub(
            r'function clickRoom\(rid\)\{.*?\}(?=\s*function enterRoom)',
            NEW_CLICK,
            c, flags=re.DOTALL
        )
        print('✅ clickRoom — fixed (regex)')
        changed = True
    else:
        print('ℹ️  clickRoom already patched or not found')

    # ── 2. Add _pendingRoomId variable ────────────────────────────────────
    if '_pendingRoomId' not in c:
        c = c.replace(
            'let ws=null,wsReady=false;',
            'let ws=null,wsReady=false;\nlet _pendingRoomId=null;'
        )
        print('✅ _pendingRoomId — added')
        changed = True
    else:
        print('ℹ️  _pendingRoomId already present')

    # ── 3. Fix lobby_room_update to enter pending room ────────────────────
    OLD_LRU = '''    case 'lobby_room_update':
      if(room.id===m.room?.room_id){
        updateRoomFromServer(m.room);
      }break;'''

    NEW_LRU = '''    case 'lobby_room_update':
      if(room.id===m.room?.room_id){
        updateRoomFromServer(m.room);
      } else if(!room.id&&m.room&&_pendingRoomId&&_pendingRoomId===m.room.room_id){
        _pendingRoomId=null;
        enterRoom(m.room,null);
      }break;'''

    if OLD_LRU in c:
        c = c.replace(OLD_LRU, NEW_LRU)
        print('✅ lobby_room_update — fixed')
        changed = True
    else:
        print('ℹ️  lobby_room_update already patched')

    # ── 4. Fix lobby_join_result ──────────────────────────────────────────
    OLD_LJR = "      if(m.ok){closeM('joinModal');if(m.room)enterRoom(m.room,m.private_key);}"
    NEW_LJR = """      if(m.ok){
        closeM('joinModal');
        if(m.room){send({action:'lobby_subscribe',room_id:m.room.room_id});enterRoom(m.room,m.private_key||null);}
      }"""
    if OLD_LJR in c:
        c = c.replace(OLD_LJR, NEW_LJR)
        print('✅ lobby_join_result — fixed')
        changed = True
    else:
        print('ℹ️  lobby_join_result already patched')

    # ── 5. Fix emoji — call showReaction instead of spawnFloatingEmoji ───
    OLD_EMOJI = '  // Show own emoji flying immediately\n  spawnFloatingEmoji(emoji);'
    NEW_EMOJI = '  // Show flying emoji AND update reaction bar\n  showReaction(MY_ID, emoji, MY_NAME);'
    if OLD_EMOJI in c:
        c = c.replace(OLD_EMOJI, NEW_EMOJI)
        print('✅ emoji showReaction — fixed')
        changed = True
    else:
        print('ℹ️  emoji already patched')

    # ── 6. Fix PvP reset button to use REST ──────────────────────────────
    OLD_RESET_FN = '''function adminResetPvp(){
  if(!confirm(\'Сбросить колесо? Все ставки вернутся игрокам.\'))return;
  const msg=document.getElementById(\'pvpResetMsg\');
  if(msg){msg.style.color=\'var(--muted)\';msg.textContent=\'Сбрасываю...\';}
  send({action:\'admin_pvp_reset\',user_id:MY_ID});
}'''
    NEW_RESET_FN = '''async function adminResetPvp(){
  if(!confirm('Сбросить колесо? Все ставки вернутся игрокам.'))return;
  const msg=document.getElementById('pvpResetMsg');
  if(msg){msg.style.color='var(--muted)';msg.textContent='Сбрасываю...';}
  try{
    const r=await fetch('/api/admin/pvp-reset',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID})});
    const d=await r.json();
    if(msg){
      if(d.ok){msg.style.color='#00e676';msg.textContent='\u2705 \u0421\u0431\u0440\u043e\u0448\u0435\u043d\u043e, \u0432\u043e\u0437\u0432\u0440\u0430\u0449\u0435\u043d\u043e \u0441\u0442\u0430\u0432\u043e\u043a: '+(d.refunded||0);}
      else{msg.style.color='#ff4444';msg.textContent='\u274c '+(d.detail||'\u041e\u0448\u0438\u0431\u043a\u0430');}
      setTimeout(()=>{if(msg)msg.textContent='';},4000);
    }
  }catch(e){if(msg){msg.style.color='#ff4444';msg.textContent='\u274c \u041e\u0448\u0438\u0431\u043a\u0430 \u0441\u043e\u0435\u0434\u0438\u043d\u0435\u043d\u0438\u044f';}}
}'''

    # flexible match
    if "send({action:'admin_pvp_reset'" in c:
        c = re.sub(
            r'function adminResetPvp\(\)\{.*?send\(\{action:\'admin_pvp_reset\'.*?\}\);\s*\}',
            NEW_RESET_FN,
            c, flags=re.DOTALL
        )
        print('✅ adminResetPvp — REST fix applied')
        changed = True
    else:
        print('ℹ️  adminResetPvp already patched')

    # ── 7. Fix deposit lock buttons to use REST ───────────────────────────
    OLD_DEP_LOCK = "function adminSetDepositLock(locked){\n  const msg=document.getElementById('adDepLockMsg');\n  msg.style.color='var(--muted)';msg.textContent='Применяю\u2026';\n  send({action:'admin_set_deposit_lock',user_id:MY_ID,locked});\n}"
    NEW_DEP_LOCK = """async function adminSetDepositLock(locked){
  const msg=document.getElementById('adDepLockMsg');
  if(msg){msg.style.color='var(--muted)';msg.textContent='\u041f\u0440\u0438\u043c\u0435\u043d\u044f\u044e\u2026';}
  try{
    const r=await fetch('/api/admin/deposit-lock',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,locked})});
    const d=await r.json();
    if(msg){
      if(d.ok){msg.style.color='#00e676';msg.textContent=locked?'\ud83d\udd12 \u041f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u044f \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u044b':'\ud83d\udd13 \u041f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u044f \u0440\u0430\u0437\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u044b';}
      else{msg.style.color='#ff4444';msg.textContent='\u274c '+(d.detail||'\u041e\u0448\u0438\u0431\u043a\u0430');}
      setTimeout(()=>{if(msg)msg.textContent='';},3000);
    }
  }catch(e){if(msg){msg.style.color='#ff4444';msg.textContent='\u274c \u041e\u0448\u0438\u0431\u043a\u0430 \u0441\u043e\u0435\u0434\u0438\u043d\u0435\u043d\u0438\u044f';}}
}"""
    if "send({action:'admin_set_deposit_lock'" in c:
        c = re.sub(
            r'function adminSetDepositLock\(locked\)\{.*?send\(\{action:\'admin_set_deposit_lock\'.*?\}\);\s*\}',
            NEW_DEP_LOCK,
            c, flags=re.DOTALL
        )
        print('✅ adminSetDepositLock — REST fix applied')
        changed = True
    else:
        print('ℹ️  adminSetDepositLock already patched')

    # ── 8. Add channel_boost option to bonus type selector ────────────────
    OLD_OPT = '<option value="channel_sub">Подписка на канал</option>\n        <option value="manual">Ручная проверка</option>'
    NEW_OPT = '<option value="channel_sub">Подписка на канал</option>\n        <option value="channel_boost">Буст канала</option>\n        <option value="manual">Ручная проверка</option>'
    if 'channel_boost' not in c and OLD_OPT in c:
        c = c.replace(OLD_OPT, NEW_OPT)
        print('✅ channel_boost bonus type — added')
        changed = True
    else:
        print('ℹ️  channel_boost already present')

    # ── 9. Add deposit modal if missing ──────────────────────────────────
    if 'id="depositModal"' not in c:
        deposit_modal = '''
<!-- ═══════════ DEPOSIT MODAL ═══════════ -->
<div class="overlay" id="depositModal">
  <div class="sheet">
    <div class="handle"></div>
    <div class="m-head"><span class="m-title">💳 ПОПОЛНЕНИЕ</span><div class="m-close" onclick="closeM(\'depositModal\')">✕</div></div>
    <div class="m-body">
      <div style="display:flex;flex-direction:column;gap:14px">
        <div style="background:rgba(0,200,255,.06);border:1px solid rgba(0,200,255,.25);border-radius:14px;padding:14px">
          <div style="font-size:13px;font-weight:800;color:var(--blue);margin-bottom:4px">💎 Пополнить через TON</div>
          <div style="font-size:11px;color:var(--muted);margin-bottom:10px">Оплата через CryptoPay · мгновенное зачисление</div>
          <div style="display:flex;gap:8px;margin-bottom:8px">
            <input id="depTonAmt" type="number" min="0.5" step="0.1" value="5" class="adm-inp" placeholder="Сумма TON" style="flex:1"/>
            <button onclick="submitDepositTon()" style="background:var(--blue);color:#000;border:none;border-radius:10px;padding:10px 16px;font-size:13px;font-weight:900;cursor:pointer;white-space:nowrap">Пополнить</button>
          </div>
          <div id="depTonMsg" style="font-size:11px;color:var(--muted);min-height:16px"></div>
        </div>
        <div style="font-size:10px;color:var(--muted2);text-align:center">Нажимая «Пополнить» вы будете перенаправлены в CryptoPay для оплаты</div>
      </div>
    </div>
  </div>
</div>'''
        # insert before closing body
        c = c.replace('</body>', deposit_modal + '\n</body>')
        print('✅ depositModal — added')
        changed = True
    else:
        print('ℹ️  depositModal already present')

    # ── 10. Fix topup button ──────────────────────────────────────────────
    OLD_TOPUP = "onclick=\"toast('Скоро 🔒')\">🔒 Пополнить</div>"
    NEW_TOPUP = "onclick=\"openDepositModal()\">💳 Пополнить</div>"
    if OLD_TOPUP in c:
        c = c.replace(OLD_TOPUP, NEW_TOPUP)
        print('✅ topup button — fixed')
        changed = True
    else:
        print('ℹ️  topup button already patched')

    # ── 11. Add deposit JS functions if missing ───────────────────────────
    if 'function openDepositModal' not in c:
        dep_js = '''
function openDepositModal(){
  if(!requireAuth())return;
  openM('depositModal');
}
async function submitDepositTon(){
  if(!requireAuth())return;
  const amt=parseFloat(document.getElementById('depTonAmt').value);
  const msg=document.getElementById('depTonMsg');
  if(isNaN(amt)||amt<0.5){msg.style.color='var(--danger)';msg.textContent='Минимум 0.5 TON';return;}
  msg.style.color='var(--muted)';msg.textContent='Создаю счёт…';
  try{
    const resp=await fetch('/api/wallet/deposit-ton',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,amount:amt})});
    const data=await resp.json();
    if(data.ok&&data.pay_url){
      msg.style.color='var(--accent)';msg.textContent='✅ Открываем оплату…';
      if(window.Telegram&&window.Telegram.WebApp&&window.Telegram.WebApp.openLink){
        window.Telegram.WebApp.openLink(data.pay_url);
      } else {window.open(data.pay_url,'_blank');}
    } else {msg.style.color='var(--danger)';msg.textContent='❌ '+(data.detail||data.error||'Ошибка');}
  }catch(e){msg.style.color='var(--danger)';msg.textContent='❌ Ошибка соединения';}
}'''
        c = c.replace('</script>', dep_js + '\n</script>', 1)
        print('✅ deposit JS functions — added')
        changed = True
    else:
        print('ℹ️  deposit JS functions already present')

    # ── 12. Add gift admin section if missing ─────────────────────────────
    if 'adminSendGift' not in c:
        gift_js = '''
function setGiftId(id){const inp=document.getElementById('adGiftId');if(inp)inp.value=id;}
async function adminSendGift(){
  const username=(document.getElementById('adGiftUsername')?.value||'').trim();
  const giftId=(document.getElementById('adGiftId')?.value||'').trim();
  const msg=document.getElementById('adGiftMsg');
  if(!username){if(msg){msg.style.color='#ff4444';msg.textContent='❌ Укажи @username';}return;}
  if(!giftId){if(msg){msg.style.color='#ff4444';msg.textContent='❌ Укажи ID подарка';}return;}
  if(msg){msg.style.color='var(--muted)';msg.textContent='Отправляю…';}
  try{
    const r=await fetch('/api/admin/send-gift',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,target_username:username,gift_id:giftId})});
    const d=await r.json();
    if(msg){
      if(d.ok){msg.style.color='#00e676';msg.textContent='✅ Подарок отправлен → @'+username.replace('@','');}
      else{msg.style.color='#ff4444';msg.textContent='❌ '+(d.detail||'Ошибка');}
      setTimeout(()=>{if(msg)msg.textContent='';},4000);
    }
  }catch(e){if(msg){msg.style.color='#ff4444';msg.textContent='❌ Ошибка соединения';}}
}'''
        c = c.replace('</script>', gift_js + '\n</script>', 1)
        print('✅ adminSendGift JS — added')
        changed = True

    if changed:
        with open(HTML, 'w', encoding='utf-8') as f:
            f.write(c)
        print(f'\n✅ {HTML} saved.')
    else:
        print('\nℹ️  No changes needed in HTML.')


def patch_server():
    with open(SRV, 'r', encoding='utf-8') as f:
        c = f.read()

    changed = False

    # ── 1. Add force_reset to PvPGame ────────────────────────────────────
    if 'async def force_reset' not in c:
        # find end of _end_game or similar method and insert after class
        insert_after = '        await asyncio.sleep(3)\n        await self.ensure_game()'
        new_method = '''        await asyncio.sleep(3)
        await self.ensure_game()

    async def force_reset(self) -> dict:
        """Admin: cancel current game, refund all bets, start fresh."""
        refunded = []
        if self.game_id is not None:
            for uid_str, data in self.bets.items():
                amt = data["amount"] if isinstance(data, dict) else data
                try:
                    db.update_balance(int(uid_str), amt)
                    refunded.append({"user_id": int(uid_str), "amount": amt})
                except Exception as e:
                    log.warning(f"force_reset refund uid={uid_str}: {e}")
            try: db.set_pvp_status(self.game_id, "cancelled")
            except Exception: pass
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task = None
        self.game_id = None
        self.bets    = {}
        self.pot     = 0.0
        self.status  = "waiting"
        self.timer   = config.ROUND_DURATION
        try: await mgr.broadcast({"type": "pvp_reset", "refunded": len(refunded)})
        except Exception: pass
        for r in refunded:
            try:
                user = db.get_user(r["user_id"])
                if user:
                    await mgr.broadcast_to_user(r["user_id"], {
                        "type": "balance_update",
                        "balance": user["balance"],
                        "ref_balance": user["ref_balance"],
                    })
            except Exception: pass
        await asyncio.sleep(1)
        await self.ensure_game()
        return {"refunded": refunded}'''
        if insert_after in c:
            c = c.replace(insert_after, new_method, 1)
            print('✅ force_reset — added to PvPGame')
            changed = True
        else:
            print('⚠️  Could not find insertion point for force_reset')
    else:
        print('ℹ️  force_reset already present')

    # ── 2. Add REST endpoints ─────────────────────────────────────────────
    if '/api/admin/pvp-reset' not in c:
        rest_code = '''

@app.post("/api/admin/pvp-reset")
async def admin_pvp_reset_rest(payload: dict):
    uid = int(payload.get("user_id", 0))
    if uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    result = await pvp_game.force_reset()
    return {"ok": True, "refunded": len(result.get("refunded", []))}


@app.post("/api/admin/deposit-lock")
async def admin_deposit_lock_rest(payload: dict):
    uid = int(payload.get("user_id", 0))
    if uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    locked = bool(payload.get("locked", False))
    db.set_setting_str("deposit_locked", "1" if locked else "0")
    return {"ok": True, "locked": locked}


@app.post("/api/admin/send-gift")
async def admin_send_gift(payload: dict):
    import aiohttp as _ah
    uid = int(payload.get("user_id", 0))
    if uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    target_username = str(payload.get("target_username", "")).replace("@", "").strip()
    gift_id = str(payload.get("gift_id", "")).strip()
    if not target_username or not gift_id:
        raise HTTPException(400, "Укажи username и gift_id")
    try:
        async with _ah.ClientSession() as sess:
            async with sess.get(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/getChat",
                params={"chat_id": "@" + target_username},
                timeout=_ah.ClientTimeout(total=5)
            ) as r:
                chat_data = await r.json()
        if not chat_data.get("ok"):
            raise HTTPException(404, f"Пользователь @{target_username} не найден")
        target_id = chat_data["result"]["id"]
        async with _ah.ClientSession() as sess:
            async with sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendGift",
                json={"user_id": target_id, "gift_id": gift_id},
                timeout=_ah.ClientTimeout(total=10)
            ) as r:
                result = await r.json()
        if not result.get("ok"):
            raise HTTPException(400, result.get("description", "Ошибка отправки"))
        return {"ok": True, "target_id": target_id}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/wallet/deposit-ton")
async def deposit_ton(payload: dict):
    import aiohttp as _ah
    uid    = int(payload.get("user_id", 0))
    amount = float(payload.get("amount", 5.0))
    if amount < 0.5:
        raise HTTPException(400, "Минимальная сумма 0.5 TON")
    if db.get_setting_str("deposit_locked") == "1":
        raise HTTPException(403, "Пополнение временно отключено")
    user = db.get_user(uid)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    CRYPTO_BOT_TOKEN = "587912:AA7uAzHSoljwESDQPDOTlTD14Lj6L6ITMbz"
    try:
        async with _ah.ClientSession() as session:
            async with session.post(
                "https://pay.crypt.bot/api/createInvoice",
                headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
                json={
                    "asset": "TON",
                    "amount": str(round(amount, 2)),
                    "description": f"Пополнение RoyalDuel · UID {uid}",
                    "payload": str(uid),
                    "allow_comments": False,
                    "allow_anonymous": True,
                    "expires_in": 3600,
                },
                timeout=_ah.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
    except Exception as e:
        log.error(f"CryptoPay error: {e}")
        raise HTTPException(502, "Ошибка платёжного сервиса")
    if not data.get("ok"):
        raise HTTPException(502, data.get("error", {}).get("name", "Ошибка CryptoPay"))
    result = data["result"]
    return {"ok": True, "pay_url": result["pay_url"], "invoice_id": result["invoice_id"]}


@app.post("/api/crypto-webhook")
async def crypto_webhook(payload: dict):
    update_type = payload.get("update_type")
    if update_type != "invoice_paid":
        return {"ok": True}
    invoice = payload.get("payload", payload)
    if not isinstance(invoice, dict):
        invoice = payload
    invoice_payload = invoice.get("payload") or ""
    amount_str      = invoice.get("amount") or "0"
    try:
        uid    = int(str(invoice_payload).strip())
        amount = float(amount_str)
    except Exception:
        return {"ok": True}
    if db.get_setting_str("deposit_locked") == "1":
        return {"ok": True}
    user = db.get_user(uid)
    if not user:
        return {"ok": True}
    db.update_balance(uid, amount)
    db.add_balance_history(uid, "deposit", amount, note=f"CryptoPay TON +{amount}")
    updated = db.get_user(uid)
    import asyncio as _asyncio
    _asyncio.create_task(mgr.broadcast_to_user(uid, {
        "type": "balance_update",
        "balance": updated["balance"],
        "ref_balance": updated["ref_balance"],
    }))
    return {"ok": True}
'''
        # Insert before @app.on_event("startup") or at end before if __name__
        if '@app.on_event("startup")' in c:
            c = c.replace('@app.on_event("startup")', rest_code + '\n@app.on_event("startup")', 1)
        else:
            c = c.rstrip() + rest_code
        print('✅ REST endpoints — added')
        changed = True
    else:
        print('ℹ️  REST endpoints already present')

    # ── 3. Add get_setting_str to database if missing ────────────────────
    # (handled separately in database.py)

    # ── 4. Add PvP watchdog ───────────────────────────────────────────────
    if '_pvp_watchdog' not in c:
        watchdog = '''

async def _pvp_watchdog():
    while True:
        await asyncio.sleep(60)
        try:
            if pvp_game.status == "spinning":
                log.warning("PvP watchdog: stuck spinning — auto-reset")
                await pvp_game.force_reset()
            elif pvp_game.status == "waiting" and pvp_game.game_id is None:
                log.warning("PvP watchdog: no game — creating")
                await pvp_game.ensure_game()
        except Exception as e:
            log.error(f"PvP watchdog: {e}")
'''
        if 'pvp_game = PvPGame()' in c:
            c = c.replace('pvp_game = PvPGame()', 'pvp_game = PvPGame()' + watchdog, 1)
            print('✅ PvP watchdog — added')
            changed = True
    else:
        print('ℹ️  watchdog already present')

    # ── 5. Start watchdog in startup ──────────────────────────────────────
    if '_pvp_watchdog' in c and 'create_task(_pvp_watchdog' not in c:
        c = c.replace(
            'await pvp_game.ensure_game()',
            'await pvp_game.ensure_game()\n    asyncio.create_task(_pvp_watchdog())',
            1
        )
        print('✅ watchdog started in startup')
        changed = True

    if changed:
        with open(SRV, 'w', encoding='utf-8') as f:
            f.write(c)
        print(f'✅ {SRV} saved.')
    else:
        print('ℹ️  No changes needed in server.py.')


def patch_database():
    DB = os.path.join(BASE, 'database.py')
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
               WHERE bh.kind = \'deposit\'
               ORDER BY bh.id DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
'''
        # insert before end of file
        c = c.rstrip() + helpers + '\n'
        print('✅ database helpers — added')
        changed = True
    else:
        print('ℹ️  database helpers already present')

    if changed:
        with open(DB, 'w', encoding='utf-8') as f:
            f.write(c)
        print(f'✅ {DB} saved.')


if __name__ == '__main__':
    print('=== RoyalDuel Patch Script ===\n')
    print('--- Patching webapp/index.html ---')
    patch_html()
    print('\n--- Patching server.py ---')
    patch_server()
    print('\n--- Patching database.py ---')
    patch_database()
    print('\n=== Done! Run: systemctl restart royalduel.service ===')
