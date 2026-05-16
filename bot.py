import asyncio
import json
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, WebAppInfo
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from database import (
    init_db, get_user, create_user, update_balance,
    get_active_game, create_game, add_bet,
    set_game_status, get_last_games, get_top_game,
    mark_promo_used, promo_used, get_game
)
from game_logic import resolve_game

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Track active timers so we don't double-start
active_timers: dict[int, bool] = {}

# ── Helpers ────────────────────────────────────────────

def fmt(val: float) -> str:
    return f"{val:.2f}"

def main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 PvP игра", callback_data="pvp")
    kb.button(text="👤 Профиль", callback_data="profile")
    kb.button(text="🏛 Lobby", callback_data="lobby")
    kb.adjust(2, 1)
    return kb.as_markup()

def profile_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Рефералка", callback_data="referral")
    kb.button(text="🎟 Промокод", callback_data="promo")
    kb.button(text="💬 Поддержка", url="https://t.me/support")
    kb.button(text="🔙 Назад", callback_data="back_main")
    kb.adjust(2, 1, 1)
    return kb.as_markup()

def pvp_keyboard(game_id: int):
    kb = InlineKeyboardBuilder()
    for amt in [0.1, 0.5, 1.0]:
        kb.button(text=f"💎 {amt} TON", callback_data=f"bet:{game_id}:{amt}")
    kb.button(text="💰 All In", callback_data=f"bet:{game_id}:allin")
    kb.adjust(3, 1)
    return kb.as_markup()

# ── /start ─────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    args = message.text.split()
    referrer_id = None

    if len(args) > 1:
        try:
            ref = int(args[1])
            if ref != user_id:
                referrer_id = ref
        except ValueError:
            pass

    existing = get_user(user_id)
    if not existing:
        create_user(
            user_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            referrer_id=referrer_id,
        )
        user = get_user(user_id)
        await message.answer(
            f"🎉 Добро пожаловать в <b>TON Rolls PvP</b>!\n\n"
            f"💎 На твой счёт начислено <b>{fmt(user['balance'])} TON</b>\n\n"
            f"Выбери действие:",
            reply_markup=main_keyboard()
        )
    else:
        user = existing
        await message.answer(
            f"👋 Привет, <b>{message.from_user.first_name}</b>!\n"
            f"💎 Баланс: <b>{fmt(user['balance'])} TON</b>",
            reply_markup=main_keyboard()
        )

# ── Lobby ──────────────────────────────────────────────

@router.callback_query(F.data == "lobby")
async def cb_lobby(call: CallbackQuery):
    await call.message.edit_text(
        "🏛 <b>Lobby</b>\n\n🚧 Coming Soon...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")
        ]])
    )

# ── Profile ────────────────────────────────────────────

@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user:
        await call.answer("Сначала напиши /start")
        return

    ref_link = f"https://t.me/{(await bot.get_me()).username}?start={call.from_user.id}"
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 Username: @{user['username'] or 'нет'}\n"
        f"💎 Баланс: <b>{fmt(user['balance'])} TON</b>\n\n"
        f"🔗 Реферальная ссылка:\n<code>{ref_link}</code>"
    )
    await call.message.edit_text(text, reply_markup=profile_keyboard())

@router.callback_query(F.data == "referral")
async def cb_referral(call: CallbackQuery):
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={call.from_user.id}"
    await call.message.edit_text(
        f"👥 <b>Реферальная программа</b>\n\n"
        f"Ты получаешь <b>10%</b> от комиссии проекта каждый раз, когда твой реферал <b>выигрывает</b>.\n\n"
        f"🔗 Твоя ссылка:\n<code>{ref_link}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад", callback_data="profile")
        ]])
    )

@router.callback_query(F.data == "promo")
async def cb_promo(call: CallbackQuery):
    await call.message.edit_text(
        "🎟 <b>Промокод</b>\n\nВведи промокод командой:\n<code>/promo КОД</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад", callback_data="profile")
        ]])
    )

@router.message(Command("promo"))
async def cmd_promo(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("❗ Использование: /promo КОД")
        return

    code = parts[1].strip().lower()
    if code not in config.PROMO_CODES:
        await message.answer("❌ Промокод не найден.")
        return

    bonus = config.PROMO_CODES[code]
    update_balance(message.from_user.id, bonus)
    await message.answer(f"✅ Промокод активирован! Начислено <b>{fmt(bonus)} TON</b> 🎉")

# ── PvP Game ───────────────────────────────────────────

@router.callback_query(F.data == "pvp")
async def cb_pvp(call: CallbackQuery):
    game = get_active_game()

    if not game:
        game_id = create_game()
        game = get_game(game_id)
        # Start countdown
        asyncio.create_task(game_countdown(game_id, call.message.chat.id))

    last = get_last_games(1)
    top = get_top_game()

    last_text = f"🕹 Последняя: <b>{fmt(last[0]['pot'])} TON</b> → @{last[0]['winner_name']}" if last else "🕹 Последняя: нет игр"
    top_text = f"🏆 Рекорд: <b>{fmt(top['pot'])} TON</b> → @{top['winner_name']}" if top else "🏆 Рекорд: нет игр"

    bets = game["bets"]
    pot = game["total_pot"]
    players = len(bets)

    bet_lines = "\n".join(
        f"  • @{v['username'] or uid}: {fmt(v['amount'])} TON"
        for uid, v in bets.items()
    ) or "  Ставок пока нет"

    text = (
        f"🎡 <b>TON Rolls PvP</b>  |  Игра #{game['game_id']}\n"
        f"{last_text}\n{top_text}\n\n"
        f"💰 Банк: <b>{fmt(pot)} TON</b>  |  👥 Игроков: {players}\n\n"
        f"<b>Ставки:</b>\n{bet_lines}\n\n"
        f"⏳ <i>Сделай ставку за 20 сек!</i>"
    )
    await call.message.edit_text(text, reply_markup=pvp_keyboard(game["game_id"]))

@router.callback_query(F.data.startswith("bet:"))
async def cb_bet(call: CallbackQuery):
    _, game_id_str, amount_str = call.data.split(":")
    game_id = int(game_id_str)
    user_id = call.from_user.id

    game = get_game(game_id)
    if not game or game["status"] != "betting":
        await call.answer("⏰ Ставки закрыты!", show_alert=True)
        return

    user = get_user(user_id)
    if not user:
        await call.answer("Сначала /start", show_alert=True)
        return

    if amount_str == "allin":
        amount = user["balance"]
    else:
        amount = float(amount_str)

    if amount <= 0:
        await call.answer("❌ Недостаточно средств!", show_alert=True)
        return

    if user["balance"] < amount:
        await call.answer(f"❌ Нужно {fmt(amount)} TON, у тебя {fmt(user['balance'])} TON", show_alert=True)
        return

    # Deduct balance and save bet
    update_balance(user_id, -amount)
    add_bet(game_id, user_id, amount, user["username"] or user["first_name"])

    await call.answer(f"✅ Ставка {fmt(amount)} TON принята!")

    # Refresh the PvP screen
    game = get_game(game_id)
    bets = game["bets"]
    pot = game["total_pot"]
    players = len(bets)

    bet_lines = "\n".join(
        f"  • @{v['username'] or uid}: {fmt(v['amount'])} TON"
        for uid, v in bets.items()
    )

    await call.message.edit_text(
        f"🎡 <b>TON Rolls PvP</b>  |  Игра #{game_id}\n\n"
        f"💰 Банк: <b>{fmt(pot)} TON</b>  |  👥 Игроков: {players}\n\n"
        f"<b>Ставки:</b>\n{bet_lines}\n\n"
        f"⏳ <i>Идёт приём ставок...</i>",
        reply_markup=pvp_keyboard(game_id)
    )

# ── Game Countdown ─────────────────────────────────────

async def game_countdown(game_id: int, chat_id: int):
    if game_id in active_timers:
        return
    active_timers[game_id] = True

    await asyncio.sleep(config.ROUND_DURATION)

    game = get_game(game_id)
    if not game or game["status"] != "betting":
        active_timers.pop(game_id, None)
        return

    # Freeze
    set_game_status(game_id, "spinning")
    await bot.send_message(chat_id, "🔒 <b>Ставки закрыты!</b> Крутим колесо... 🎡")
    await asyncio.sleep(config.FREEZE_DURATION + 3)  # spin animation time

    result = resolve_game(game_id)
    active_timers.pop(game_id, None)

    if not result:
        await bot.send_message(chat_id, "❌ Игра отменена (нет ставок).")
        return

    if result.get("solo"):
        await bot.send_message(chat_id, "👤 Только один игрок — ставка возвращена.")
        return

    # Announce winner
    winner_text = (
        f"🏆 <b>Победитель Ролла #{game_id}!</b>\n\n"
        f"👤 @{result['winner_name']}\n"
        f"💰 Выиграл: <b>{fmt(result['prize'])} TON</b>\n"
        f"📈 Множитель: <b>x{result['multiplier']}</b>\n"
        f"🎯 Шанс победы: <b>{result['chance']}%</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 Новая игра", callback_data="pvp")
    ]])
    await bot.send_message(chat_id, winner_text, reply_markup=kb)

# ── Back to main ───────────────────────────────────────

@router.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery):
    user = get_user(call.from_user.id)
    await call.message.edit_text(
        f"🎮 <b>TON Rolls PvP</b>\n\n"
        f"💎 Баланс: <b>{fmt(user['balance']) if user else '0.00'} TON</b>\n\n"
        f"Выбери действие:",
        reply_markup=main_keyboard()
    )

# ── Main ───────────────────────────────────────────────

async def main():
    init_db()
    log.info("Database initialized")
    log.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
