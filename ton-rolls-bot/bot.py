"""bot.py — Telegram bot for RoyalDuel."""
from __future__ import annotations
import logging
from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
import config, database as db

log = logging.getLogger("bot")
bot = Bot(token=config.BOT_TOKEN, parse_mode=ParseMode.HTML)
dp  = Dispatcher()
router = Router()
dp.include_router(router)

def webapp_kb(user_id: int) -> InlineKeyboardMarkup:
    url = f"{config.WEBAPP_URL}?uid={user_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Открыть RoyalDuel", web_app=WebAppInfo(url=url))
    ]])

@router.message(CommandStart())
async def cmd_start(message: Message):
    uid  = message.from_user.id
    uname= message.from_user.username or ""
    fname= message.from_user.first_name or ""
    args = message.text.split()
    ref  = None
    if len(args) > 1:
        try:
            r = int(args[1])
            if r != uid: ref = r
        except ValueError:
            pass
    if not db.get_user(uid):
        db.upsert_user(uid, uname, fname, referrer_id=ref)
    else:
        db.upsert_user(uid, uname, fname)
    user = db.get_user(uid)
    await message.answer(
        f"⚔️ <b>RoyalDuel</b> — PvP игры на TON!\n\n"
        f"💎 Баланс: <b>{user['balance']:.2f} TON</b>\n\n"
        f"Нажми кнопку и начни играть 👇",
        reply_markup=webapp_kb(uid)
    )
