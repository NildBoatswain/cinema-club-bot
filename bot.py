"""
🎬 Cinema Club Bot
Telegram-бот для управления кино-клубом.
"""

import os
import json
import random
import logging
import asyncio
from datetime import datetime, timedelta

import aiohttp
from googletrans import Translator
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters, PollAnswerHandler
)

# ─── Логирование ────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Конфиг из переменных окружения ─────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]          # токен от @BotFather
CHAT_ID     = int(os.environ["CHAT_ID"])        # ID вашего чата (отрицательное число)
OMDB_KEY    = os.environ.get("OMDB_KEY", "")   # ключ OMDb (опционально)

# ─── Состояние (хранится в памяти + сериализуется в Telegram Saved Messages) ─
DEFAULT_STATE = {
    "phase": "idle",          # idle | date_poll | suggest | film_poll | announced
    "show_date": None,        # "20.07.2025 19:00"
    "date_poll_id": None,
    "date_poll_msg_id": None,
    "date_options": [],
    "suggestions": {},        # { "user_id": {"name": "Имя", "films": ["Film1","Film2"]} }
    "pool": {},               # { "user_id": "Film" }  — по 1 фильму после рандома
    "film_poll_id": None,
    "film_poll_msg_id": None,
    "film_options": [],
    "winner": None,
    "announce_msg_id": None,
    "remind_24_done": False,
    "remind_2_done": False,
}

state = dict(DEFAULT_STATE)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# ─── Сохранение / загрузка состояния ────────────────────────────────────────
STATE_FILE = "state.json"

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния: {e}")

def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
                state.update(loaded)
            logger.info("Состояние загружено из файла.")
        except Exception as e:
            logger.error(f"Ошибка загрузки состояния: {e}")

# ─── Проверка прав админа ────────────────────────────────────────────────────
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(CHAT_ID, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

async def admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Только для админов.")
        return False
    return True

# ─── Перевод текста на русский ───────────────────────────────────────────────
async def translate_to_ru(text: str) -> str:
    """Переводит текст на русский через Google Translate (без API-ключа)."""
    if not text or text == "N/A":
        return text
    try:
        translator = Translator()
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: translator.translate(text, dest="ru")
        )
        return result.text
    except Exception as e:
        logger.warning(f"Перевод не удался: {e}")
        return text  # возвращаем оригинал если перевод упал

# ─── OMDb: получить инфо о фильме ───────────────────────────────────────────
async def fetch_movie_info(title: str) -> dict | None:
    if not OMDB_KEY:
        return None
    url = f"http://www.omdbapi.com/?t={title}&apikey={OMDB_KEY}&plot=short&r=json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                if data.get("Response") == "True":
                    # Переводим описание и имя режиссёра на русский
                    data["Plot"] = await translate_to_ru(data.get("Plot", ""))
                    data["Director"] = await translate_to_ru(data.get("Director", ""))
                    return data
    except Exception as e:
        logger.error(f"OMDb ошибка: {e}")
    return None

# ─── /start ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *Кино-клуб бот* готов к работе!\n\n"
        "Команды для участников:\n"
        "• /suggest <Название> — предложить фильм\n"
        "• /suggestions — список предложений\n"
        "• /status — текущий статус цикла\n\n"
        "Команды для админов:\n"
        "• /newcycle <дата1> <дата2> ... — запустить цикл\n"
        "• /closedatepoll — закрыть опрос за дату\n"
        "• /opensuggest — открыть приём фильмов\n"
        "• /closesuggest — закрыть приём\n"
        "• /randomize — выбрать по 1 фильму от каждого\n"
        "• /startpoll — запустить голосование за фильм\n"
        "• /closepoll — закрыть голосование\n"
        "• /announce — опубликовать анонс победителя\n"
        "• /settime <ДД.ММ.ГГГГ ЧЧ:ММ> — установить время показа\n"
        "• /reset — сбросить текущий цикл",
        parse_mode="Markdown"
    )

# ─── /status ─────────────────────────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phase_names = {
        "idle": "😴 Ожидание нового цикла",
        "date_poll": "📅 Голосование за дату",
        "suggest": "🎥 Приём предложений фильмов",
        "film_poll": "🗳 Голосование за фильм",
        "announced": "🎬 Фильм выбран, ждём показа",
    }
    phase = phase_names.get(state["phase"], state["phase"])
    date = state.get("show_date") or "не определена"
    winner = state.get("winner") or "не выбран"
    total = sum(len(v["films"]) for v in state["suggestions"].values())

    await update.message.reply_text(
        f"📊 *Статус кино-клуба*\n\n"
        f"Фаза: {phase}\n"
        f"Дата показа: {date}\n"
        f"Фильм: {winner}\n"
        f"Предложений: {total}",
        parse_mode="Markdown"
    )

# ─── /newcycle ───────────────────────────────────────────────────────────────
async def cmd_newcycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if not context.args:
        await update.message.reply_text(
            "Укажи даты: /newcycle 15.07 18.07 20.07\n"
            "Или с временем: /newcycle \"15.07 19:00\" \"18.07 20:00\""
        )
        return

    # Сброс состояния
    state.update(dict(DEFAULT_STATE))
    dates = context.args
    state["date_options"] = dates
    state["phase"] = "date_poll"
    save_state()

    msg = await context.bot.send_poll(
        chat_id=CHAT_ID,
        question="📅 Когда смотрим кино?",
        options=dates,
        is_anonymous=False,
        allows_multiple_answers=False,
    )
    state["date_poll_id"] = msg.poll.id
    state["date_poll_msg_id"] = msg.message_id
    save_state()

    await context.bot.send_message(
        CHAT_ID,
        "🗳 Голосуем за дату! Опрос выше ☝️\n"
        "Когда все проголосуют, админ закроет опрос командой /closedatepoll"
    )

# ─── /closedatepoll ───────────────────────────────────────────────────────────
async def cmd_closedatepoll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if state["phase"] != "date_poll":
        await update.message.reply_text("Нет активного опроса за дату.")
        return

    try:
        poll_msg = await context.bot.stop_poll(CHAT_ID, state["date_poll_msg_id"])
        # Находим вариант с наибольшим числом голосов
        winner_option = max(poll_msg.options, key=lambda o: o.voter_count)
        state["show_date"] = winner_option.text
        state["phase"] = "idle"
        save_state()

        await context.bot.send_message(
            CHAT_ID,
            f"✅ Дата показа определена: *{winner_option.text}*\n\n"
            f"Теперь предлагайте фильмы командой /suggest\n"
            f"_(когда будете готовы, админ откроет приём командой /opensuggest)_",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# ─── /opensuggest ─────────────────────────────────────────────────────────────
async def cmd_opensuggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    state["phase"] = "suggest"
    state["suggestions"] = {}
    save_state()
    await context.bot.send_message(
        CHAT_ID,
        f"🎥 *Приём фильмов открыт!*\n\n"
        f"Предлагайте фильмы командой:\n"
        f"`/suggest Название фильма`\n\n"
        f"Каждый может предложить от 1 до 5 фильмов.\n"
        f"Показ: *{state.get('show_date', 'TBD')}*",
        parse_mode="Markdown"
    )

# ─── /suggest ────────────────────────────────────────────────────────────────
async def cmd_suggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if state["phase"] != "suggest":
        await update.message.reply_text("⏸ Сейчас приём фильмов закрыт.")
        return
    if not context.args:
        await update.message.reply_text("Укажи название: /suggest Inception 2010")
        return

    user = update.effective_user
    uid = str(user.id)
    film_title = " ".join(context.args)

    if uid not in state["suggestions"]:
        state["suggestions"][uid] = {
            "name": user.first_name or user.username or "Участник",
            "films": []
        }

    films = state["suggestions"][uid]["films"]

    if len(films) >= 5:
        await update.message.reply_text("У тебя уже 5 фильмов — максимум достигнут. 🎬")
        return

    if film_title.lower() in [f.lower() for f in films]:
        await update.message.reply_text("Ты уже предлагал этот фильм.")
        return

    # Проверяем дубль по всем участникам
    all_films = [f.lower() for v in state["suggestions"].values() for f in v["films"]]
    if film_title.lower() in all_films:
        await update.message.reply_text(f"⚠️ Фильм «{film_title}» уже предложен другим участником.")
        return

    films.append(film_title)
    save_state()

    count = len(films)
    remaining = 5 - count
    await update.message.reply_text(
        f"✅ Принято: *{film_title}*\n"
        f"Твои фильмы ({count}/5): {', '.join(films)}\n"
        f"{'Ещё можешь предложить: ' + str(remaining) if remaining > 0 else 'Лимит исчерпан.'}",
        parse_mode="Markdown"
    )

# ─── /suggestions ────────────────────────────────────────────────────────────
async def cmd_suggestions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state["suggestions"]:
        await update.message.reply_text("Пока никто ничего не предложил.")
        return

    lines = ["🎬 *Предложения участников:*\n"]
    for uid, data in state["suggestions"].items():
        films_str = "\n  • ".join(data["films"])
        lines.append(f"*{data['name']}:*\n  • {films_str}")

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

# ─── /closesuggest ────────────────────────────────────────────────────────────
async def cmd_closesuggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if state["phase"] != "suggest":
        await update.message.reply_text("Приём фильмов сейчас не активен.")
        return
    state["phase"] = "idle"
    save_state()

    total = sum(len(v["films"]) for v in state["suggestions"].values())
    await context.bot.send_message(
        CHAT_ID,
        f"🔒 Приём фильмов закрыт. Получено предложений: {total}\n"
        f"Используй /randomize для выбора по одному фильму от каждого."
    )

# ─── /randomize ───────────────────────────────────────────────────────────────
async def cmd_randomize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if not state["suggestions"]:
        await update.message.reply_text("Нет предложений для жеребьёвки.")
        return

    pool = {}
    lines = ["🎲 *Жребий брошен!*\n"]

    for uid, data in state["suggestions"].items():
        films = data["films"]
        if not films:
            continue
        chosen = random.choice(films) if len(films) > 1 else films[0]
        pool[uid] = chosen
        marker = "🎯" if len(films) > 1 else "☑️"
        lines.append(f"{marker} *{data['name']}* → {chosen}")
        if len(films) > 1:
            others = [f for f in films if f != chosen]
            lines.append(f"  _отброшены: {', '.join(others)}_")

    state["pool"] = pool
    save_state()

    lines.append(f"\n📋 В финале {len(pool)} фильм(ов). Запусти /startpoll для голосования.")
    await context.bot.send_message(CHAT_ID, "\n".join(lines), parse_mode="Markdown")

# ─── /startpoll ───────────────────────────────────────────────────────────────
async def cmd_startpoll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if not state["pool"]:
        await update.message.reply_text("Сначала запусти /randomize.")
        return

    film_options = list(state["pool"].values())
    if len(film_options) < 2:
        await update.message.reply_text("Нужно минимум 2 фильма для голосования.")
        return

    state["film_options"] = film_options
    state["phase"] = "film_poll"
    save_state()

    msg = await context.bot.send_poll(
        chat_id=CHAT_ID,
        question="🎬 Какой фильм смотрим?",
        options=film_options,
        is_anonymous=True,
        allows_multiple_answers=False,
    )
    state["film_poll_id"] = msg.poll.id
    state["film_poll_msg_id"] = msg.message_id
    save_state()

    await context.bot.send_message(
        CHAT_ID,
        "🗳 Голосуем за фильм! Опрос выше ☝️\n"
        "Закрыть голосование: /closepoll"
    )

# ─── /closepoll ───────────────────────────────────────────────────────────────
async def cmd_closepoll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if state["phase"] != "film_poll":
        await update.message.reply_text("Нет активного голосования за фильм.")
        return

    try:
        poll_msg = await context.bot.stop_poll(CHAT_ID, state["film_poll_msg_id"])
        winner_option = max(poll_msg.options, key=lambda o: o.voter_count)
        state["winner"] = winner_option.text
        state["phase"] = "announced"
        save_state()

        await context.bot.send_message(
            CHAT_ID,
            f"🏆 Победитель голосования: *{winner_option.text}*\n\n"
            f"Используй /announce для публикации анонса.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# ─── /announce ────────────────────────────────────────────────────────────────
async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if not state.get("winner"):
        await update.message.reply_text("Фильм-победитель не определён. Запусти /closepoll.")
        return

    await _send_announce(context, state["winner"], state.get("show_date"))

# ─── Формирование анонса ──────────────────────────────────────────────────────
async def _send_announce(context: ContextTypes.DEFAULT_TYPE, title: str, show_date: str = None):
    info = await fetch_movie_info(title)

    if info:
        year      = info.get("Year", "")
        director  = info.get("Director", "")
        plot      = info.get("Plot", "")
        poster    = info.get("Poster", "")
        rating    = info.get("imdbRating", "")

        text = (
            f"🎬 *{title}*" + (f" ({year})" if year else "") + "\n"
            + (f"🎥 Режиссёр: {director}\n" if director else "")
            + (f"⭐ IMDb: {rating}\n" if rating and rating != "N/A" else "")
            + (f"\n📝 {plot}\n" if plot else "")
            + (f"\n📅 Смотрим: *{show_date}*" if show_date else "")
        )

        if poster and poster != "N/A":
            msg = await context.bot.send_photo(
                chat_id=CHAT_ID,
                photo=poster,
                caption=text,
                parse_mode="Markdown"
            )
        else:
            msg = await context.bot.send_message(CHAT_ID, text, parse_mode="Markdown")
    else:
        text = (
            f"🎬 *{title}*\n"
            + (f"📅 Смотрим: *{show_date}*" if show_date else "")
        )
        msg = await context.bot.send_message(CHAT_ID, text, parse_mode="Markdown")

    state["announce_msg_id"] = msg.message_id
    save_state()
    return msg

# ─── /settime ─────────────────────────────────────────────────────────────────
async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Укажи дату и время: /settime 20.07.2025 19:00"
        )
        return

    datetime_str = f"{context.args[0]} {context.args[1]}"
    try:
        show_dt = datetime.strptime(datetime_str, "%d.%m.%Y %H:%M")
        state["show_date"] = datetime_str
        state["remind_24_done"] = False
        state["remind_2_done"] = False
        save_state()

        # Планируем напоминания
        _schedule_reminders(show_dt)

        await update.message.reply_text(
            f"✅ Время показа установлено: *{datetime_str}*\n"
            f"Напоминания запланированы за 24ч и за 2ч.",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("Неверный формат. Используй: 20.07.2025 19:00")

# ─── Планировщик напоминаний ──────────────────────────────────────────────────
def _schedule_reminders(show_dt: datetime):
    remind_24 = show_dt - timedelta(hours=24)
    remind_2  = show_dt - timedelta(hours=2)
    now = datetime.now()

    if remind_24 > now:
        scheduler.add_job(
            _remind_24h,
            "date",
            run_date=remind_24,
            id="remind_24",
            replace_existing=True,
        )
        logger.info(f"Напоминание за 24ч запланировано на {remind_24}")

    if remind_2 > now:
        scheduler.add_job(
            _remind_2h,
            "date",
            run_date=remind_2,
            id="remind_2",
            replace_existing=True,
        )
        logger.info(f"Напоминание за 2ч запланировано на {remind_2}")

async def _remind_24h():
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    winner = state.get("winner", "фильм")
    show_date = state.get("show_date", "")
    await bot.send_message(
        CHAT_ID,
        f"🔔 *Напоминание!*\n\n"
        f"Завтра смотрим *{winner}*\n"
        f"📅 {show_date}\n\n"
        f"Не забудьте! 🍿",
        parse_mode="Markdown"
    )

async def _remind_2h():
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    winner = state.get("winner", "фильм")
    show_date = state.get("show_date", "")
    await bot.send_message(
        CHAT_ID,
        f"⏰ *Через 2 часа смотрим!*\n\n"
        f"🎬 *{winner}*\n"
        f"📅 {show_date}\n\n"
        f"Готовьте попкорн! 🍿",
        parse_mode="Markdown"
    )

# ─── /reset ───────────────────────────────────────────────────────────────────
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    state.update(dict(DEFAULT_STATE))
    save_state()
    await context.bot.send_message(CHAT_ID, "🔄 Цикл сброшен. Можно начинать заново с /newcycle")

# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    load_state()

    # Восстанавливаем напоминания если дата установлена
    if state.get("show_date") and state["phase"] in ("announced", "film_poll"):
        try:
            show_dt = datetime.strptime(state["show_date"], "%d.%m.%Y %H:%M")
            _schedule_reminders(show_dt)
        except Exception:
            pass

    scheduler.start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("newcycle",       cmd_newcycle))
    app.add_handler(CommandHandler("closedatepoll",  cmd_closedatepoll))
    app.add_handler(CommandHandler("opensuggest",    cmd_opensuggest))
    app.add_handler(CommandHandler("suggest",        cmd_suggest))
    app.add_handler(CommandHandler("suggestions",    cmd_suggestions))
    app.add_handler(CommandHandler("closesuggest",   cmd_closesuggest))
    app.add_handler(CommandHandler("randomize",      cmd_randomize))
    app.add_handler(CommandHandler("startpoll",      cmd_startpoll))
    app.add_handler(CommandHandler("closepoll",      cmd_closepoll))
    app.add_handler(CommandHandler("announce",       cmd_announce))
    app.add_handler(CommandHandler("settime",        cmd_settime))
    app.add_handler(CommandHandler("reset",          cmd_reset))

    logger.info("🎬 Cinema Bot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
