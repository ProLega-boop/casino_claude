#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_v4.py — RoyalDuel v4
Fixes:
  1. Profile — кнопки Пополнить + Вывести по центру под балансом
  2. Lobby clickRoom — надёжный join второго игрока
  3. Lobby lobby_join_result — ошибки видны не только в joinModal
  4. Emoji — 2 эмодзи при нажатии + счётчик под кнопкой
  5. Send Gift — server endpoint + JS fix
  6. /api/user/by-username — резолв для подарков
  7. Версия v4 в заголовке
"""
from pathlib import Path
import re, sys

BASE = Path("/root/ton-rolls-bot")
SRV  = BASE / "server.py"
HTML = BASE / "webapp/index.html"

def r(path): return path.read_text(encoding="utf-8", errors="replace")
def w(path, content):
    path.write_text(content, encoding="utf-8", errors="replace")
    print(f"✅ Saved {path}")

print("\n" + "="*60)
print("patch_v4.py — RoyalDuel")
print("="*60)

# ══════════════════════════════════════════════════════════════
# SERVER.PY
# ══════════════════════════════════════════════════════════════
print("\n── SERVER.PY ──")
srv = r(SRV)
srv_changed = False

GIFT_BLOCK = '''
@app.post("/api/admin/get-gifts")
async def admin_get_gifts(payload: dict):
    import aiohttp as _ah
    uid = int(payload.get("user_id", 0))
    if uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    try:
        async with _ah.ClientSession() as sess:
            async with sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/getAvailableGifts",
                timeout=_ah.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
        if not data.get("ok"):
            return {"ok": False, "gifts": [], "error": data.get("description","Ошибка")}
        return {"ok": True, "gifts": data.get("result",{}).get("gifts",[])}
    except Exception as e:
        return {"ok": False, "gifts": [], "error": str(e)}


@app.post("/api/admin/send-gift")
async def admin_send_gift(payload: dict):
    import aiohttp as _ah
    uid             = int(payload.get("user_id", 0))
    target_user_id  = payload.get("target_user_id")      # int preferred
    target_username = str(payload.get("target_username","")).strip().lstrip("@")
    gift_id         = str(payload.get("gift_id","")).strip()
    if uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    if not gift_id:
        raise HTTPException(400, "gift_id required")
    # Resolve target: prefer numeric user_id
    if not target_user_id and target_username:
        try:
            import database as _db
            row = _db._conn().execute(
                "SELECT user_id FROM users WHERE lower(username)=? LIMIT 1",
                (target_username.lower(),)
            ).fetchone()
            if row:
                target_user_id = row["user_id"]
        except Exception:
            pass
    if not target_user_id:
        raise HTTPException(404, f"Cannot resolve user @{target_username}")
    try:
        async with _ah.ClientSession() as sess:
            async with sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendGift",
                json={"user_id": int(target_user_id), "gift_id": gift_id},
                timeout=_ah.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
        if data.get("ok"):
            return {"ok": True, "message": f"Gift sent"}
        err = data.get("description","Unknown error")
        raise HTTPException(502, err)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))

'''

BY_USERNAME_BLOCK = '''
@app.get("/api/user/by-username/{username}")
async def get_user_by_username(username: str):
    uname = username.lstrip("@").lower()
    import database as _db2
    with _db2._conn() as conn:
        row = conn.execute(
            "SELECT user_id, username, first_name FROM users WHERE lower(username)=? LIMIT 1",
            (uname,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    return {"user_id": row["user_id"], "username": row["username"], "first_name": row["first_name"]}

'''

# Insert endpoints before startup event (or before last line)
INSERT_ANCHOR = '@app.on_event("startup")'
INSERT_ANCHOR_ALT = 'if __name__ == "__main__":'

def insert_before(text, anchor, block):
    if anchor in text:
        return text.replace(anchor, block + "\n" + anchor, 1)
    return text + block

if "/api/admin/send-gift" not in srv:
    srv = insert_before(srv, INSERT_ANCHOR, GIFT_BLOCK)
    srv_changed = True
    print("✅ send-gift + get-gifts endpoints added")
else:
    print("ℹ️  send-gift already present")

if "/api/admin/get-gifts" not in srv:
    # get-gifts missing but send-gift exists (v2/v3 partial)
    srv = insert_before(srv, INSERT_ANCHOR, '''
@app.post("/api/admin/get-gifts")
async def admin_get_gifts_v4(payload: dict):
    import aiohttp as _ah
    uid = int(payload.get("user_id", 0))
    if uid != config.ADMIN_ID:
        raise HTTPException(403, "Forbidden")
    try:
        async with _ah.ClientSession() as sess:
            async with sess.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/getAvailableGifts",
                timeout=_ah.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
        if not data.get("ok"):
            return {"ok": False, "gifts": [], "error": data.get("description","Ошибка")}
        return {"ok": True, "gifts": data.get("result",{}).get("gifts",[])}
    except Exception as e:
        return {"ok": False, "gifts": [], "error": str(e)}
''')
    srv_changed = True
    print("✅ get-gifts endpoint added")
else:
    print("ℹ️  get-gifts already present")

if "/api/user/by-username/" not in srv:
    srv = insert_before(srv, INSERT_ANCHOR, BY_USERNAME_BLOCK)
    srv_changed = True
    print("✅ /api/user/by-username endpoint added")
else:
    print("ℹ️  /api/user/by-username already present")

if srv_changed:
    w(SRV, srv)
else:
    print("ℹ️  server.py — no changes")


# ══════════════════════════════════════════════════════════════
# INDEX.HTML
# ══════════════════════════════════════════════════════════════
print("\n── INDEX.HTML ──")
html = r(HTML)
changed = False

# ── 1. Версия v4 ─────────────────────────────────────────────
for old_ver in ["v2.1","v2.0","v3.0","v3.1","v3"]:
    if old_ver in html and "v4" not in html:
        html = html.replace(old_ver, "v4", 1)
        changed = True
        print(f"✅ Version {old_ver} → v4")
        break


# ── 2. Profile card — кнопки по центру ───────────────────────
# Ищем блок с topup-btn и добавляем кнопку вывода рядом
if 'openWithdrawModal' not in html:
    OLD_TOPUP = '<div class="topup-btn" onclick="openDepositModal()">💳 Пополнить</div>'
    NEW_TOPUP = (
        '<div style="display:flex;flex-direction:column;gap:6px;width:100%;margin-top:6px">'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'
        '<button onclick="openDepositModal()" style="background:linear-gradient(135deg,rgba(0,152,234,.2),rgba(0,152,234,.07));border:1px solid rgba(0,152,234,.5);border-radius:12px;padding:10px 6px;color:#0098EA;font-size:13px;font-weight:800;cursor:pointer;width:100%">💳 Пополнить</button>'
        '<button onclick="openWithdrawModal()" style="background:linear-gradient(135deg,rgba(255,149,0,.2),rgba(255,96,0,.07));border:1px solid rgba(255,149,0,.5);border-radius:12px;padding:10px 6px;color:#ff9500;font-size:13px;font-weight:800;cursor:pointer;width:100%">📤 Вывести</button>'
        '</div>'
        '</div>'
    )
    if OLD_TOPUP in html:
        # First fix prof-card to be column layout
        html = html.replace(
            'class="prof-card"',
            'class="prof-card" style="flex-direction:column;align-items:stretch"',
            1
        )
        # Replace top row: make avatar+info take full width
        html = html.replace(
            '<div class="prof-info">',
            '<div class="prof-info" style="flex:1;min-width:0">',
            1
        )
        html = html.replace(OLD_TOPUP, NEW_TOPUP, 1)
        changed = True
        print("✅ Profile: Пополнить + Вывести buttons added (centered)")
    else:
        print("ℹ️  topup-btn pattern not found — buttons may already be updated")
else:
    print("ℹ️  openWithdrawModal already in HTML")


# ── 3. Withdraw modal ────────────────────────────────────────
if "withdrawModal" not in html:
    WMODAL = '''
<!-- WITHDRAW MODAL v4 -->
<div class="overlay" id="withdrawModal">
  <div class="sheet">
    <div class="handle"></div>
    <div class="m-head"><span class="m-title">📤 ВЫВОД TON</span>
    <div class="m-close" onclick="closeM('withdrawModal')">&#10005;</div></div>
    <div class="m-body">
      <div style="display:flex;flex-direction:column;gap:12px">
        <div style="background:rgba(255,149,0,.07);border:1px solid rgba(255,149,0,.25);border-radius:12px;padding:12px;font-size:12px;color:var(--muted)">
          ⏱ Ручная обработка до 24ч. Средства резервируются сразу.
        </div>
        <input id="wdAddress" class="adm-inp" placeholder="TON Адрес (UQ...)" style="width:100%;box-sizing:border-box"/>
        <div style="display:flex;gap:8px">
          <input id="wdAmount" type="number" min="0.5" step="0.1" class="adm-inp" placeholder="Сумма TON (мин. 0.5)" style="flex:1"/>
          <button onclick="wdSetMax()" style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--muted);font-size:12px;cursor:pointer;flex-shrink:0">MAX</button>
        </div>
        <button onclick="submitWithdraw()" style="width:100%;background:linear-gradient(135deg,#ff9500,#ff6000);border:none;border-radius:12px;padding:14px;color:#fff;font-size:14px;font-weight:900;cursor:pointer">📤 Отправить заявку</button>
        <div id="wdMsg" style="font-size:12px;text-align:center;min-height:16px"></div>
      </div>
    </div>
  </div>
</div>
'''
    html = html.replace("</body>", WMODAL + "</body>")
    changed = True
    print("✅ Withdraw modal added")
else:
    print("ℹ️  Withdraw modal already present")


# ── 4. Withdraw JS functions ─────────────────────────────────
if "function openWithdrawModal" not in html:
    WJS = '''
function openWithdrawModal(){
  const wl=document.getElementById('wdAmount');if(wl)wl.value='';
  const wa=document.getElementById('wdAddress');if(wa)wa.value='';
  const wm=document.getElementById('wdMsg');if(wm)wm.textContent='';
  openM('withdrawModal');
}
function wdSetMax(){
  const el=document.getElementById('wdAmount');
  if(el)el.value=parseFloat(S.balance||0).toFixed(2);
}
async function submitWithdraw(){
  const address=(document.getElementById('wdAddress')?.value||'').trim();
  const amount=parseFloat(document.getElementById('wdAmount')?.value||'0');
  const msg=document.getElementById('wdMsg');
  if(!address.startsWith('UQ')&&!address.startsWith('EQ')){
    if(msg){msg.style.color='#ff4444';msg.textContent='❌ Некорректный TON адрес (должен начинаться с UQ/EQ)';}return;
  }
  if(isNaN(amount)||amount<0.5){
    if(msg){msg.style.color='#ff4444';msg.textContent='❌ Минимум 0.5 TON';}return;
  }
  if(msg){msg.style.color='var(--muted)';msg.textContent='Отправляю заявку…';}
  try{
    const res=await fetch('/api/wallet/withdraw',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:MY_ID,amount,address})});
    const d=await res.json();
    if(d.ok){
      if(msg){msg.style.color='#00e676';msg.textContent='✅ Заявка принята! Обработка до 24ч.';}
      if(d.balance!==undefined){S.balance=d.balance;updateUI();}
      setTimeout(()=>closeM('withdrawModal'),2800);
    } else {
      if(msg){msg.style.color='#ff4444';msg.textContent='❌ '+(d.detail||d.error||'Ошибка');}
    }
  }catch(e){
    if(msg){msg.style.color='#ff4444';msg.textContent='❌ Ошибка соединения';}
  }
}
'''
    # Insert before first </script>
    html = html.replace("</script>", WJS + "\n</script>", 1)
    changed = True
    print("✅ Withdraw JS functions added")
else:
    print("ℹ️  Withdraw JS already present")


# ── 5. Emoji: 2 эмодзи + счётчик под кнопкой ────────────────
OLD_SPAWN = (
    "function spawnFloatingEmoji(emoji){\n"
    "  const feed=document.getElementById('emojiFeed');\n"
    "  if(!feed)return;\n"
    "  const el=document.createElement('div');\n"
    "  el.textContent=emoji;\n"
    "  el.style.cssText=`position:absolute;bottom:${10+Math.random()*60}%;left:4px;font-size:22px;animation:emojiFloat 1.4s ease forwards;`;\n"
    "  feed.appendChild(el);\n"
    "  setTimeout(()=>el.remove(),1500);\n"
    "}"
)

NEW_SPAWN = (
    "function spawnFloatingEmoji(emoji){\n"
    "  const feed=document.getElementById('emojiFeed');\n"
    "  if(!feed)return;\n"
    "  // Spawn 2 emojis with stagger\n"
    "  [0,1].forEach(function(i){\n"
    "    const el=document.createElement('div');\n"
    "    el.textContent=emoji;\n"
    "    const bot=(10+Math.random()*50)+'%';\n"
    "    const lft=(i===0?4:12)+'px';\n"
    "    el.style.cssText='position:absolute;bottom:'+bot+';left:'+lft+';font-size:22px;opacity:0;animation:emojiFloat 1.4s ease '+(i*160)+'ms forwards;';\n"
    "    feed.appendChild(el);\n"
    "    setTimeout(function(){el.remove();},1600+i*160);\n"
    "  });\n"
    "}\n"
    "function _updateEmojiCounter(emoji){\n"
    "  document.querySelectorAll('.emo-btn').forEach(function(btn){\n"
    "    if(btn.textContent.trim()===emoji){\n"
    "      var wrap=btn.parentElement;\n"
    "      var ctr=wrap.querySelector('.emc[data-e=\"'+emoji+'\"]');\n"
    "      if(!ctr){\n"
    "        ctr=document.createElement('div');\n"
    "        ctr.className='emc';\n"
    "        ctr.dataset.e=emoji;\n"
    "        ctr.style.cssText='font-size:9px;color:var(--muted);text-align:center;line-height:1;font-weight:700;margin-top:2px;min-height:10px;';\n"
    "        btn.insertAdjacentElement('afterend',ctr);\n"
    "      }\n"
    "      ctr.textContent=(_reactionCounts[emoji]||0)||'';\n"
    "      clearTimeout(ctr._t);\n"
    "      ctr._t=setTimeout(function(){ctr.textContent='';},8000);\n"
    "    }\n"
    "  });\n"
    "}"
)

if OLD_SPAWN in html:
    html = html.replace(OLD_SPAWN, NEW_SPAWN, 1)
    changed = True
    print("✅ spawnFloatingEmoji: 2 emojis + counter")
else:
    # Regex fallback
    m = re.search(r'function spawnFloatingEmoji\(emoji\)\{.*?\}', html, re.DOTALL)
    if m and "forEach" not in m.group(0):
        html = html[:m.start()] + NEW_SPAWN + html[m.end():]
        changed = True
        print("✅ spawnFloatingEmoji: updated (regex)")
    else:
        print("ℹ️  spawnFloatingEmoji already updated or not found")

# Hook _updateEmojiCounter into showReaction
OLD_SPAWN_CALL = "  spawnFloatingEmoji(emoji);\n  // update feed bar"
NEW_SPAWN_CALL = "  spawnFloatingEmoji(emoji);\n  if(typeof _updateEmojiCounter==='function')_updateEmojiCounter(emoji);\n  // update feed bar"
if OLD_SPAWN_CALL in html:
    html = html.replace(OLD_SPAWN_CALL, NEW_SPAWN_CALL, 1)
    changed = True
    print("✅ showReaction: counter hook added")
else:
    print("ℹ️  showReaction counter hook: already present or pattern changed")

# emo-btn wrapper: wrap each button in a div so counter can appear below
OLD_EMO_PANEL = (
    '    <div id="emojiPanel" style="display:flex;flex-direction:column;gap:6px;flex-shrink:0">\n'
    "      <button class=\"emo-btn\" onclick=\"sendEmoji('👍')\">👍</button>\n"
    "      <button class=\"emo-btn\" onclick=\"sendEmoji('🤬')\">🤬</button>\n"
    "      <button class=\"emo-btn\" onclick=\"sendEmoji('🤡')\">🤡</button>\n"
    "      <button class=\"emo-btn\" onclick=\"sendEmoji('🤣')\">🤣</button>\n"
    "      <button class=\"emo-btn\" onclick=\"sendEmoji('😭')\">😭</button>\n"
    "      <button class=\"emo-btn\" onclick=\"sendEmoji('👀')\">👀</button>\n"
    "    </div>"
)
NEW_EMO_PANEL = (
    '    <div id="emojiPanel" style="display:flex;flex-direction:column;gap:6px;flex-shrink:0">\n'
    "      <div style='display:flex;flex-direction:column;align-items:center'><button class=\"emo-btn\" onclick=\"sendEmoji('👍')\">👍</button></div>\n"
    "      <div style='display:flex;flex-direction:column;align-items:center'><button class=\"emo-btn\" onclick=\"sendEmoji('🤬')\">🤬</button></div>\n"
    "      <div style='display:flex;flex-direction:column;align-items:center'><button class=\"emo-btn\" onclick=\"sendEmoji('🤡')\">🤡</button></div>\n"
    "      <div style='display:flex;flex-direction:column;align-items:center'><button class=\"emo-btn\" onclick=\"sendEmoji('🤣')\">🤣</button></div>\n"
    "      <div style='display:flex;flex-direction:column;align-items:center'><button class=\"emo-btn\" onclick=\"sendEmoji('😭')\">😭</button></div>\n"
    "      <div style='display:flex;flex-direction:column;align-items:center'><button class=\"emo-btn\" onclick=\"sendEmoji('👀')\">👀</button></div>\n"
    "    </div>"
)
if OLD_EMO_PANEL in html:
    html = html.replace(OLD_EMO_PANEL, NEW_EMO_PANEL, 1)
    changed = True
    print("✅ Emoji panel: buttons wrapped for counter")
else:
    print("ℹ️  Emoji panel already wrapped")


# ── 6. Lobby — fix clickRoom + lobby_join_result ─────────────

# Добавляем _pendingRoomAction переменную
if "_pendingRoomAction" not in html:
    html = html.replace(
        "let _pendingRoomId=null;",
        "let _pendingRoomId=null;\nlet _pendingRoomAction=null;",
        1
    )
    changed = True
    print("✅ _pendingRoomAction variable added")
else:
    print("ℹ️  _pendingRoomAction already declared")

# Fix clickRoom: когда комнаты нет в кэше — пометить что хотим JOIN
OLD_PENDING = (
    "    _pendingRoomId=rid;\n"
    "    send({action:'lobby_subscribe',room_id:rid});\n"
    "    toast('Загружаем комнату…','');\n"
    "    return;"
)
NEW_PENDING = (
    "    _pendingRoomId=rid;\n"
    "    _pendingRoomAction='join';\n"
    "    send({action:'lobby_subscribe',room_id:rid});\n"
    "    toast('Загружаем комнату…','');\n"
    "    return;"
)
if OLD_PENDING in html:
    html = html.replace(OLD_PENDING, NEW_PENDING, 1)
    changed = True
    print("✅ clickRoom: _pendingRoomAction set on cache-miss")
else:
    print("ℹ️  clickRoom pending action: pattern not found")

# Fix lobby_room_update handler: после subscribe — join или enter
OLD_UPDATE_HANDLER = (
    "      } else if(!room.id && m.room && _pendingRoomId && _pendingRoomId===m.room.room_id){\n"
    "        // We subscribed to this room but haven't entered yet — enter now\n"
    "        _pendingRoomId=null;\n"
    "        enterRoom(m.room,null);\n"
    "      }break;"
)
NEW_UPDATE_HANDLER = (
    "      } else if(m.room && _pendingRoomId && _pendingRoomId===m.room.room_id){\n"
    "        var _pRid=_pendingRoomId, _pAct=_pendingRoomAction||'view', _pRoom=m.room;\n"
    "        _pendingRoomId=null; _pendingRoomAction=null;\n"
    "        var _alrIn=_pRoom.players&&_pRoom.players.some(function(p){return String(p.user_id)===String(MY_ID);});\n"
    "        var _isCreator=String(_pRoom.creator_id)===String(MY_ID);\n"
    "        if(_alrIn||_isCreator){\n"
    "          enterRoom(_pRoom,(_pRoom.is_private&&_isCreator)?_pRoom.private_key:null);\n"
    "        } else if(_pAct==='join'){\n"
    "          send({action:'lobby_join',user_id:MY_ID,username:MY_NAME,\n"
    "            first_name:(typeof TGU!=='undefined'&&TGU)?TGU.first_name||'':'',\n"
    "            room_id:_pRid,private_key:''});\n"
    "        } else if(!room.id){\n"
    "          enterRoom(_pRoom,null);\n"
    "        }\n"
    "      }break;"
)
if OLD_UPDATE_HANDLER in html:
    html = html.replace(OLD_UPDATE_HANDLER, NEW_UPDATE_HANDLER, 1)
    changed = True
    print("✅ lobby_room_update: smart join/enter after subscribe")
else:
    print("ℹ️  lobby_room_update handler: pattern not matched (may be updated)")

# Fix lobby_join_result: показывать ошибку через toast даже без joinModal
OLD_JOIN_RESULT = (
    "    case 'lobby_join_result':\n"
    "      if(m.ok){\n"
    "        closeM('joinModal');\n"
    "        if(m.room){\n"
    "          send({action:'lobby_subscribe',room_id:m.room.room_id});\n"
    "          enterRoom(m.room, m.private_key||null);\n"
    "        }\n"
    "      } else {\n"
    "        const e=document.getElementById('joinErrMsg');\n"
    "        if(e)e.textContent='❌ '+(m.error||'Комната не найдена');\n"
    "        toast(m.error||'Комната не найдена','red');\n"
    "      }\n"
    "      syncRoomControls();break;"
)
NEW_JOIN_RESULT = (
    "    case 'lobby_join_result':\n"
    "      if(m.ok){\n"
    "        closeM('joinModal');\n"
    "        if(m.room){\n"
    "          // Subscribe first so we receive broadcasts, then enter\n"
    "          send({action:'lobby_subscribe',room_id:m.room.room_id});\n"
    "          enterRoom(m.room, m.private_key||null);\n"
    "          send({action:'get_my_rooms',user_id:MY_ID});\n"
    "        }\n"
    "      } else {\n"
    "        const e=document.getElementById('joinErrMsg');\n"
    "        if(e)e.textContent='❌ '+(m.error||'Ошибка');\n"
    "        // Always show toast — user may not have joinModal open\n"
    "        toast('❌ '+(m.error||'Не удалось войти в комнату'),'red');\n"
    "      }\n"
    "      syncRoomControls();break;"
)
if OLD_JOIN_RESULT in html:
    html = html.replace(OLD_JOIN_RESULT, NEW_JOIN_RESULT, 1)
    changed = True
    print("✅ lobby_join_result: always show toast on error, refresh my_rooms on success")
else:
    print("ℹ️  lobby_join_result: pattern not matched")


# ── 7. adminSendGift JS fix ──────────────────────────────────
OLD_GIFT_JS = "async function adminSendGift(){"
NEW_GIFT_JS_FULL = '''async function adminSendGift(){
  const username=(document.getElementById('adGiftUsername')?.value||'').trim().replace(/^@/,'');
  const giftId=(document.getElementById('adGiftId')?.value||'').trim();
  const msg=document.getElementById('adGiftMsg');
  if(!username){if(msg){msg.style.color='#ff4444';msg.textContent='❌ Укажи @username получателя';}return;}
  if(!giftId){if(msg){msg.style.color='#ff4444';msg.textContent='❌ Выбери или введи ID подарка';}return;}
  if(msg){msg.style.color='var(--muted)';msg.textContent='Ищу пользователя и отправляю…';}
  try{
    // Try to resolve username → user_id via our DB
    var targetUserId=null;
    try{
      const ur=await fetch('/api/user/by-username/'+username);
      if(ur.ok){const ud=await ur.json();if(ud.user_id)targetUserId=ud.user_id;}
    }catch(e2){}
    const body={user_id:MY_ID,target_username:username,gift_id:giftId};
    if(targetUserId)body.target_user_id=targetUserId;
    const r=await fetch('/api/admin/send-gift',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const d=await r.json();
    if(msg){
      if(d.ok){msg.style.color='#00e676';msg.textContent='✅ Подарок отправлен → @'+username;}
      else{msg.style.color='#ff4444';msg.textContent='❌ '+(d.detail||d.error||'Ошибка');}
      setTimeout(()=>{if(msg)msg.textContent='';},5000);
    }
  }catch(e){
    if(msg){msg.style.color='#ff4444';msg.textContent='❌ Ошибка соединения';}
  }
}'''

# Count occurrences to avoid replacing wrong function
if html.count(OLD_GIFT_JS) == 1:
    # Find end of function
    start = html.find(OLD_GIFT_JS)
    depth = 0
    i = start
    while i < len(html):
        if html[i] == '{': depth += 1
        elif html[i] == '}':
            depth -= 1
            if depth == 0:
                html = html[:start] + NEW_GIFT_JS_FULL + html[i+1:]
                changed = True
                print("✅ adminSendGift JS replaced")
                break
        i += 1
else:
    print(f"ℹ️  adminSendGift: found {html.count(OLD_GIFT_JS)} occurrences, skipping")


# ── Сохраняем HTML ──────────────────────────────────────────
if changed:
    w(HTML, html)
else:
    print("ℹ️  index.html — no changes needed")


print("\n" + "="*60)
print("✅ patch_v4.py complete!")
print("="*60)
print("""
Что изменено:
  ✅ Profile: 2 кнопки (Пополнить + Вывести) по центру под балансом
  ✅ Withdraw modal + JS (если не было)
  ✅ Lobby clickRoom: при cache-miss → subscribe → потом join
  ✅ lobby_room_update: умный join/enter после subscribe
  ✅ lobby_join_result: ошибка всегда через toast (не только в joinModal)
                        + refresh my_rooms после успешного входа
  ✅ Emoji: 2 эмодзи с задержкой + счётчик под каждой кнопкой
  ✅ adminSendGift: резолв username → user_id из нашей БД
  ✅ send-gift endpoint: принимает target_user_id (числовой)
  ✅ get-gifts endpoint: список доступных подарков
  ✅ /api/user/by-username endpoint
  ✅ Версия v4

ПОДАРКИ — ВАЖНО:
  Telegram sendGift требует числовой user_id, не @username.
  Теперь при отправке подарка сначала ищем user_id в нашей БД.
  Получатель ДОЛЖЕН был запустить бота хотя бы раз.
  
  Для работы sendGift боту нужны Stars:
  → @BotFather → выбери бота → Bot Settings → Monetization → Buy Stars
  
  После patch_v4:
  cd /root/ton-rolls-bot && git pull origin main
  python3 patch_v4.py
  systemctl restart royalduel.service
  journalctl -u royalduel.service -n 20 --no-pager
""")
