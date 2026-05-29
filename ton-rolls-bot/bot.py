"""bot.py — Telegram bot for RoyalDuel."""
from __future__ import annotations
import logging
from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
import config
import database as db

log = logging.getLogger("bot")
from aiogram.client.default import DefaultBotProperties

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
    uid   = message.from_user.id
    uname = message.from_user.username or ""
    fname = message.from_user.first_name or ""
    args  = message.text.split()
    ref   = None
    if len(args) > 1:
        try:
            r = int(args[1])
            if r != uid:
                ref = r
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


@router.message(Command("me"))
async def cmd_me(message: Message):
    uid  = message.from_user.id
    user = db.get_user(uid)
    if not user:
        await message.answer("Вы ещё не зарегистрированы. Напишите /start")
        return

    # In group chats: only respond if bot is admin
    if message.chat.type in ("group", "supergroup"):
        try:
            bot_member = await bot.get_chat_member(message.chat.id,
                                                   (await bot.get_me()).id)
            if bot_member.status not in ("administrator", "creator"):
                return  # bot is not admin — stay silent
        except Exception:
            return

    # Referrals
    refs = db.get_referrals(uid)
    ref_count = len(refs)

    # Tournament position
    active_tournaments = db.get_active_tournaments()
    tourn_lines = ""
    for t in active_tournaments:
        lb   = db.get_tournament_leaderboard(t["id"])
        rank = next((i+1 for i, e in enumerate(lb) if e["user_id"] == uid), None)
        score = db.get_user_tournament_score(t["id"], uid)
        kind_unit = "реф." if t["kind"] == "refs" else "TON"
        if rank:
            score_fmt = f"{int(score)}" if t["kind"] == "refs" else f"{score:.2f}"
            tourn_lines += f"\n🏆 <b>{t['title']}</b>: #{rank} место ({score_fmt} {kind_unit})"
        else:
            tourn_lines += f"\n🏆 <b>{t['title']}</b>: не участвуете"

    name = f"@{user['username']}" if user.get("username") else user.get("first_name", "Игрок")
    wins = user.get("games_won", 0)
    played = user.get("games_played", 0)
    total_won = user.get("total_won", 0.0)
    ref_bal = user.get("ref_balance", 0.0)
    winrate = round(wins / played * 100, 1) if played else 0

    text = (
        f"👤 <b>{name}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💎 Баланс: <b>{user['balance']:.2f} TON</b>\n"
        f"💰 Реф. баланс: <b>{ref_bal:.2f} TON</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎮 Игр сыграно: <b>{played}</b>\n"
        f"🏅 Побед: <b>{wins}</b> ({winrate}%)\n"
        f"📈 Всего выиграно: <b>{total_won:.2f} TON</b>\n"
        f"👥 Рефералов: <b>{ref_count}</b>\n"
        f"{tourn_lines if tourn_lines else ''}"
        f"\n\n🎯 <a href='{config.WEBAPP_URL}?uid={uid}'>Открыть игру</a>"
    )

    # In groups — no inline keyboard (looks cleaner), just the text
    if message.chat.type == "private":
        await message.answer(text, reply_markup=webapp_kb(uid),
                             disable_web_page_preview=True)
    else:
        await message.answer(text, disable_web_page_preview=True)
