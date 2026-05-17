"""bot.py — Telegram bot for TON Rolls."""
from __future__ import annotations

import logging
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
)

import config
import database as db

log = logging.getLogger("bot")
bot = Bot(token=config.BOT_TOKEN, parse_mode=ParseMode.HTML)
dp  = Dispatcher()
router = Router()
dp.include_router(router)


def webapp_kb(user_id: int) -> InlineKeyboardMarkup:
    url = f"{config.WEBAPP_URL}?uid={user_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎡 Открыть TON Rolls", web_app=WebAppInfo(url=url))
    ]])


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id    = message.from_user.id
    username   = message.from_user.username or ""
    first_name = message.from_user.first_name or ""

    args      = message.text.split()
    ref_id    = None
    if len(args) > 1:
        try:
            r = int(args[1])
            if r != user_id:
                ref_id = r
        except ValueError:
            pass

    existing = db.get_user(user_id)
    if not existing:
        db.upsert_user(user_id, username, first_name, referrer_id=ref_id)

    user = db.get_user(user_id)
    bal  = user["balance"]

    await message.answer(
        f"👋 Привет, <b>{first_name or username}</b>!\n\n"
        f"🎡 <b>TON Rolls PvP</b> — реальные ставки, общее колесо!\n\n"
        f"💎 Твой баланс: <b>{bal:.2f} TON</b>\n\n"
        f"Нажми кнопку ниже, чтобы открыть игру 👇",
        reply_markup=webapp_kb(user_id),
    )
