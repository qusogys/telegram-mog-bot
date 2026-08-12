import os
import re
import sqlite3
import secrets
import threading
from time import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# =========================================================
# НАСТРОЙКИ
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не установлен")

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0

PORT = int(os.getenv("PORT", "10000"))

# Бесплатный вариант: обычный файл SQLite.
DB_PATH = os.getenv("DB_PATH", "economy.db")

START_BALANCE = 10_000

BONUS_AMOUNT = 5_000
BONUS_COOLDOWN = 4 * 60 * 60


# =========================================================
# РУЛЕТКА
# =========================================================

# Здесь "ақ" — красный сектор,
# "қара" — черный.
RED_NUMBERS = {
    1, 3, 5, 7, 9,
    12, 14, 16, 18,
    19, 21, 23, 25, 27,
    30, 32, 34, 36,
}

roulette_history = []

roulette_lock = threading.RLock()


# =========================================================
# МИНЫ
# =========================================================

mines_games = {}

mines_lock = threading.RLock()


# =========================================================
# БАЗА
# =========================================================

DB_LOCK = threading.RLock()

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False,
)

db.row_factory = sqlite3.Row

with DB_LOCK:
    db.execute("PRAGMA journal_mode=WAL")

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance INTEGER NOT NULL DEFAULT 10000,
            last_bonus INTEGER NOT NULL DEFAULT 0
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            max_uses INTEGER NOT NULL,
            used_count INTEGER NOT NULL DEFAULT 0,
            amount INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS promo_uses (
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(code, user_id)
        )
    """)

    db.commit()


# =========================================================
# УТИЛИТЫ
# =========================================================

def now():
    return int(time())


def format_money(amount):
    return f"{int(amount):,}".replace(",", " ")


def parse_amount(value):
    """
    Примеры:

    1000
    1к
    10к
    3кк
    2.5к
    2.5кк
    1м
    1млн
    """

    value = str(value).strip().lower()
    value = value.replace(" ", "")
    value = value.replace(",", ".")

    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)(кк|kk|к|k|млн|м)?",
        value,
    )

    if not match:
        raise ValueError

    number = float(match.group(1))
    suffix = match.group(2) or ""

    multipliers = {
        "": 1,
        "к": 1_000,
        "k": 1_000,
        "кк": 1_000_000,
        "kk": 1_000_000,
        "м": 1_000_000,
        "млн": 1_000_000,
    }

    amount = int(number * multipliers[suffix])

    if amount <= 0:
        raise ValueError

    return amount


def ensure_user(user):
    if user is None:
        return

    with DB_LOCK:
        db.execute(
            """
            INSERT INTO users (
                user_id,
                username,
                first_name,
                balance
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (
                user.id,
                user.username,
                user.first_name or "",
                START_BALANCE,
            ),
        )

        db.commit()


def get_balance(user_id):
    with DB_LOCK:
        row = db.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        return int(row["balance"]) if row else 0


def debit(user_id, amount):
    if amount <= 0:
        return False

    with DB_LOCK:
        cursor = db.execute(
            """
            UPDATE users
            SET balance = balance - ?
            WHERE user_id = ?
              AND balance >= ?
            """,
            (
                amount,
                user_id,
                amount,
            ),
        )

        db.commit()

        return cursor.rowcount == 1


def credit(user_id, amount):
    if amount <= 0:
        return False

    with DB_LOCK:
        cursor = db.execute(
            """
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
            """,
            (
                amount,
                user_id,
            ),
        )

        db.commit()

        return cursor.rowcount == 1


def transfer_money(sender_id, receiver_id, amount):
    if amount <= 0:
        return False

    if sender_id == receiver_id:
        return False

    with DB_LOCK:
        try:
            db.execute("BEGIN IMMEDIATE")

            cursor = db.execute(
                """
                UPDATE users
                SET balance = balance - ?
                WHERE user_id = ?
                  AND balance >= ?
                """,
                (
                    amount,
                    sender_id,
                    amount,
                ),
            )

            if cursor.rowcount != 1:
                db.rollback()
                return False

            cursor = db.execute(
                """
                UPDATE users
                SET balance = balance + ?
                WHERE user_id = ?
                """,
                (
                    amount,
                    receiver_id,
                ),
            )

            if cursor.rowcount != 1:
                db.rollback()
                return False

            db.commit()
            return True

        except Exception:
            db.rollback()
            return False


# =========================================================
# РУЛЕТКА
# =========================================================

def parse_bet(value):
    value = value.strip().lower()

    # АҚ
    if value in {
        "ақ",
        "а",
        "ак",
        "white",
    }:
        return "color", "white"

    # ҚАРА
    if value in {
        "қара",
        "қ",
        "кара",
        "черный",
        "black",
    }:
        return "color", "black"

    # ЖҰП
    if value in {
        "жұп",
        "чет",
        "even",
        "жуп",
    }:
        return "parity", "even"

    # ТАҚ
    if value in {
        "тақ",
        "так",
        "нечет",
        "odd",
    }:
        return "parity", "odd"

    # ТОЧНОЕ ЧИСЛО
    if re.fullmatch(r"\d+", value):
        number = int(value)

        if 0 <= number <= 36:
            return "number", number

    # ДИАПАЗОН
    match = re.fullmatch(
        r"(\d+)\s*[-–]\s*(\d+)",
        value,
    )

    if match:
        start = int(match.group(1))
        end = int(match.group(2))

        if (
            0 <= start <= 36
            and 0 <= end <= 36
            and start <= end
        ):
            return "range", (start, end)

    return None


def roulette_multiplier(kind, value):
    if kind == "number":
        if value == 0:
            return 100.0

        return 36.0

    if kind == "color":
        return 2.0

    if kind == "parity":
        return 2.0

    if kind == "range":
        count = value[1] - value[0] + 1

        # Диапазон:
        # вероятность примерно count/37.
        # Небольшой house edge.
        multiplier = (37 / count) * 0.95

        return max(1.01, min(35.0, multiplier))

    return 1.0


def roulette_win(kind, value, result):
    if kind == "number":
        return result == value

    if kind == "range":
        return value[0] <= result <= value[1]

    if kind == "color":
        if result == 0:
            return False

        result_color = (
            "white"
            if result in RED_NUMBERS
            else "black"
        )

        return result_color == value

    if kind == "parity":
        if result == 0:
            return False

        result_parity = (
            "even"
            if result % 2 == 0
            else "odd"
        )

        return result_parity == value

    return False


def roulette_bet_name(kind, value):
    if kind == "number":
        return str(value)

    if kind == "range":
        return f"{value[0]}-{value[1]}"

    if kind == "color":
        return "ақ" if value == "white" else "қара"

    if kind == "parity":
        return "жұп" if value == "even" else "тақ"

    return "?"


def roulette_result_text(number):
    if number == 0:
        return "🟢 0"

    if number in RED_NUMBERS:
        return f"⚪ {number}"

    return f"⚫ {number}"


def add_roulette_history(number):
    with roulette_lock:
        roulette_history.append(number)

        if len(roulette_history) > 20:
            del roulette_history[:-20]


def roulette_history_text():
    with roulette_lock:
        if not roulette_history:
            return "📜 Әзірге рулетка нәтижелері жоқ."

        results = [
            roulette_result_text(number)
            for number in roulette_history
        ]

        return (
            "📜 РУЛЕТКАНЫҢ СОҢҒЫ 20 НӘТИЖЕСІ\n\n"
            + "  ".join(results)
        )


# =========================================================
# МИНЫ
# =========================================================

def mines_multiplier(safe_opened, mine_count):
    """
    Коэффициент с небольшим преимуществом бота.

    Важно:
    чем больше мин — тем быстрее растёт коэффициент.
    """

    if safe_opened <= 0:
        return 1.0

    safe_total = 25 - mine_count

    multiplier = 1.0

    for step in range(1, safe_opened + 1):
        probability = (
            (safe_total - step + 1)
            / (26 - step)
        )

        multiplier *= 1 / probability

    # Небольшой house edge.
    multiplier *= 0.96

    return max(1.01, multiplier)


def mines_board(game, reveal=False):
    buttons = []

    for index in range(25):

        if reveal:
            if index in game["mines"]:
                text = "💣"
            elif index in game["opened"]:
                text = "💎"
            else:
                text = "🟩"

        else:
            if index in game["opened"]:
                text = "💎"
            else:
                text = "⬜"

        buttons.append(
            InlineKeyboardButton(
                text,
                callback_data=(
                    f"mine:{game['user_id']}:{index}"
                ),
            )
        )

    rows = [
        buttons[i:i + 5]
        for i in range(0, 25, 5)
    ]

    if not reveal:
        rows.append([
            InlineKeyboardButton(
                "💰 Ұтысты алу",
                callback_data=(
                    f"cash:{game['user_id']}"
                ),
            )
        ])

    return InlineKeyboardMarkup(rows)


# =========================================================
# /START
# =========================================================

async def start(update, context):
    user = update.effective_user

    ensure_user(user)

    await update.message.reply_text(
        "🇰🇿 Сәлем!\n\n"
        "💰 Теңге экономика ботына қош келдің!\n\n"
        f"💵 Бастапқы баланс: "
        f"{format_money(START_BALANCE)} ₸\n"
        f"🎁 Бонус: "
        f"{format_money(BONUS_AMOUNT)} ₸ / 4 сағат\n\n"

        "💰 БАЛАНС\n"
        "баланс\n"
        "б\n"
        "бал\n"
        "ақша\n\n"

        "🎁 БОНУС\n"
        "бонус\n"
        "bonus\n"
        "сыйлық\n\n"

        "🎰 РУЛЕТКА\n"
        "2000 ақ\n"
        "2000 а\n"
        "2000 қара\n"
        "2000 қ\n"
        "2000 16\n"
        "2000 16-30\n"
        "2000 жұп\n"
        "2000 тақ\n\n"

        "💣 МИНЫ\n"
        "мины 1000\n"
        "мины 1000 5\n\n"

        "💸 АУДАРЫМ\n"
        "Reply: бер 5к\n"
        "бер 5к @username\n"
        "бер 5к ID\n\n"

        "🎟️ ПРОМО\n"
        "/promo gift1"
    )


# =========================================================
# БАЛАНС
# =========================================================

async def balance_command(update, context):
    user = update.effective_user

    ensure_user(user)

    balance = get_balance(user.id)

    await update.message.reply_text(
        "💰 Балансың:\n\n"
        f"{format_money(balance)} ₸"
    )


# =========================================================
# БОНУС
# =========================================================

async def bonus_command(update, context):
    user = update.effective_user

    ensure_user(user)

    with DB_LOCK:
        row = db.execute(
            """
            SELECT last_bonus
            FROM users
            WHERE user_id = ?
            """,
            (user.id,),
        ).fetchone()

        last_bonus = int(row["last_bonus"])

        remaining = (
            BONUS_COOLDOWN
            - (now() - last_bonus)
        )

        if remaining > 0:
            hours = remaining // 3600
            minutes = (
                remaining % 3600
            ) // 60

            await update.message.reply_text(
                "⏳ Бонус әлі дайын емес.\n\n"
                f"Қалғаны: {hours} сағ "
                f"{minutes} мин."
            )

            return

        db.execute(
            """
            UPDATE users
            SET
                balance = balance + ?,
                last_bonus = ?
            WHERE user_id = ?
            """,
            (
                BONUS_AMOUNT,
                now(),
                user.id,
            ),
        )

        db.commit()

    await update.message.reply_text(
        "🎁 Бонус алынды!\n\n"
        f"+{format_money(BONUS_AMOUNT)} ₸\n\n"
        "⏰ Келесі бонус 4 сағаттан кейін."
    )


# =========================================================
# HELP
# =========================================================

async def help_command(update, context):
    await update.message.reply_text(
        "🇰🇿 КӨМЕК\n\n"

        "💰 Баланс:\n"
        "баланс / б / бал / ақша\n\n"

        "🎁 Бонус:\n"
        "бонус / bonus / сыйлық\n"
        "Әр 4 сағат сайын.\n\n"

        "🎰 Рулетка:\n"
        "2000 ақ\n"
        "2000 а\n"
        "2000 қара\n"
        "2000 қ\n"
        "2000 16\n"
        "2000 16-30\n"
        "2000 жұп\n"
        "2000 тақ\n\n"

        "💣 Мины:\n"
        "мины 1000\n"
        "мины 1000 5\n\n"

        "💸 Аударым:\n"
        "Reply жасап: бер 5к\n"
        "бер 5к @username\n"
        "бер 5к 123456789\n\n"

        "📜 Тарих:\n"
        "тарих\n"
        "/history\n\n"

        "🎟️ Промокод:\n"
        "/promo gift1\n\n"

        "🔢 Сомалар:\n"
        "3к = 3 000\n"
        "30к = 30 000\n"
        "3кк = 3 000 000\n"
        "2.5кк = 2 500 000"
    )


# =========================================================
# ПОИСК ПОЛЬЗОВАТЕЛЯ
# =========================================================

def find_user_by_username(username):
    username = username.lower().lstrip("@")

    with DB_LOCK:
        return db.execute(
            """
            SELECT *
            FROM users
            WHERE lower(username) = ?
            """,
            (username,),
        ).fetchone()


def find_user_by_id(user_id):
    with DB_LOCK:
        return db.execute(
            """
            SELECT *
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()


async def get_transfer_target(update, target_text):
    """
    Ищем получателя:

    1. Reply
    2. @username
    3. Telegram ID
    """

    message = update.message

    # Reply
    if message.reply_to_message:
        target = message.reply_to_message.from_user

        if target:
            ensure_user(target)
            return target

    if not target_text:
        return None

    target_text = target_text.strip()

    # @username
    if target_text.startswith("@"):
        row = find_user_by_username(target_text)

        if row:
            class FakeUser:
                pass

            target = FakeUser()
            target.id = int(row["user_id"])
            target.username = row["username"]
            target.first_name = row["first_name"]

            return target

        return None

    # ID
    if target_text.isdigit():
        user_id = int(target_text)

        row = find_user_by_id(user_id)

        if row:
            class FakeUser:
                pass

            target = FakeUser()
            target.id = int(row["user_id"])
            target.username = row["username"]
            target.first_name = row["first_name"]

            return target

    return None


# =========================================================
# ПЕРЕВОД
# =========================================================

async def transfer_command(update, amount_text, target_text):
    sender = update.effective_user

    ensure_user(sender)

    try:
        amount = parse_amount(amount_text)
    except ValueError:
        await update.message.reply_text(
            "❌ Сома қате.\n\n"
            "Мысал:\n"
            "5000\n"
            "5к\n"
            "2.5кк"
        )
        return

    target = await get_transfer_target(
        update,
        target_text,
    )

    if target is None:
        await update.message.reply_text(
            "❌ Алушы табылмады.\n\n"
            "Мысал:\n"
            "Reply жасап: бер 5к\n"
            "бер 5к @username\n"
            "бер 5к 123456789"
        )
        return

    if target.id == sender.id:
        await update.message.reply_text(
            "❌ Өзіңе ақша жібере алмайсың."
        )
        return

    ensure_user(target)

    success = transfer_money(
        sender.id,
        target.id,
        amount,
    )

    if not success:
        await update.message.reply_text(
            "❌ Қаражатың жеткіліксіз.\n\n"
            f"Қолжетімді: "
            f"{format_money(get_balance(sender.id))} ₸\n"
            f"Қажет: "
            f"{format_money(amount)} ₸"
        )
        return

    await update.message.reply_text(
        "💸 Аударым орындалды!\n\n"
        f"Сома: {format_money(amount)} ₸\n"
        f"Кімге: {target.first_name}"
    )


# =========================================================
# ПРОМОКОД СОЗДАНИЕ
# =========================================================

async def create_promo(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text(
            "❌ Бұл команда тек бот иесіне арналған."
        )
        return

    if len(context.args) != 3:
        await update.message.reply_text(
            "Формат:\n\n"
            "/createp код саны сома\n\n"
            "Мысал:\n"
            "/createp gift1 20 30к"
        )
        return

    code = context.args[0].lower()

    if not re.fullmatch(
        r"[a-zA-Zа-яА-Яәіңғүұқөһ0-9_-]+",
        code,
    ):
        await update.message.reply_text(
            "❌ Кодта рұқсат етілмеген таңба бар."
        )
        return

    try:
        max_uses = int(context.args[1])
        amount = parse_amount(context.args[2])

        if not 1 <= max_uses <= 1_000_000:
            raise ValueError

    except ValueError:
        await update.message.reply_text(
            "❌ Саны немесе сома қате."
        )
        return

    with DB_LOCK:
        try:
            db.execute(
                """
                INSERT INTO promo_codes (
                    code,
                    max_uses,
                    amount,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    code,
                    max_uses,
                    amount,
                    now(),
                ),
            )

            db.commit()

        except sqlite3.IntegrityError:
            await update.message.reply_text(
                "❌ Бұл промокод бұрыннан бар."
            )
            return

    await update.message.reply_text(
        "✅ Промокод жасалды!\n\n"
        f"🎟️ Код: {code}\n"
        f"👥 Активация: {max_uses}\n"
        f"💰 Сыйлық: "
        f"{format_money(amount)} ₸"
    )


# =========================================================
# ПРОМОКОД АКТИВАЦИЯ
# =========================================================

async def use_promo(update, context):
    user = update.effective_user

    ensure_user(user)

    if len(context.args) != 1:
        await update.message.reply_text(
            "Формат:\n"
            "/promo КОД"
        )
        return

    code = context.args[0].lower()

    with DB_LOCK:
        try:
            db.execute("BEGIN IMMEDIATE")

            promo = db.execute(
                """
                SELECT *
                FROM promo_codes
                WHERE code = ?
                """,
                (code,),
            ).fetchone()

            if promo is None:
                db.rollback()

                await update.message.reply_text(
                    "❌ Промокод табылмады."
                )
                return

            if promo["used_count"] >= promo["max_uses"]:
                db.rollback()

                await update.message.reply_text(
                    "❌ Бұл промокодтың лимиті біткен."
                )
                return

            try:
                db.execute(
                    """
                    INSERT INTO promo_uses (
                        code,
                        user_id
                    )
                    VALUES (?, ?)
                    """,
                    (
                        code,
                        user.id,
                    ),
                )

            except sqlite3.IntegrityError:
                db.rollback()

                await update.message.reply_text(
                    "❌ Сен бұл промокодты бұрын қолдандың."
                )
                return

            db.execute(
                """
                UPDATE promo_codes
                SET used_count = used_count + 1
                WHERE code = ?
                """,
                (code,),
            )

            db.execute(
                """
                UPDATE users
                SET balance = balance + ?
                WHERE user_id = ?
                """,
                (
                    promo["amount"],
                    user.id,
                ),
            )

            db.commit()

        except Exception:
            db.rollback()

            await update.message.reply_text(
                "❌ Промокодты қолдану кезінде қате."
            )
            return

    await update.message.reply_text(
        "🎁 Промокод қабылданды!\n\n"
        f"+{format_money(promo['amount'])} ₸"
    )


# =========================================================
# РУЛЕТКА
# =========================================================

async def play_roulette(update, amount_text, bet_text):
    user = update.effective_user

    ensure_user(user)

    try:
        amount = parse_amount(amount_text)
    except ValueError:
        await update.message.reply_text(
            "❌ Сома қате."
        )
        return

    bet = parse_bet(bet_text)

    if not bet:
        await update.message.reply_text(
            "❌ Ставка түсініксіз.\n\n"
            "Мысал:\n"
            "2000 ақ\n"
            "2000 16\n"
            "2000 16-30\n"
            "2000 жұп"
        )
        return

    kind, value = bet

    # Защита экономики:
    # на 0 максимум 1000 ₸.
    if (
        kind == "number"
        and value == 0
        and amount > 1_000
    ):
        await update.message.reply_text(
            "❌ 0 санына ең көбі "
            "1 000 ₸ тігуге болады."
        )
        return

    if not debit(user.id, amount):
        await update.message.reply_text(
            "❌ Қаражатың жеткіліксіз.\n\n"
            f"Қолжетімді: "
            f"{format_money(get_balance(user.id))} ₸\n"
            f"Қажет: "
            f"{format_money(amount)} ₸"
        )
        return

    # ИСТИННЫЙ СЛУЧАЙНЫЙ РЕЗУЛЬТАТ.
    result = secrets.randbelow(37)

    add_roulette_history(result)

    won = roulette_win(
        kind,
        value,
        result,
    )

    multiplier = roulette_multiplier(
        kind,
        value,
    )

    if won:
        payout = int(
            amount * multiplier
        )

        credit(
            user.id,
            payout,
        )

        message = (
            f"🎰 {roulette_result_text(result)}\n\n"
            "🎉 ҰТЫС!\n\n"
            f"Ставка: {format_money(amount)} ₸\n"
            f"Таңдау: "
            f"{roulette_bet_name(kind, value)}\n"
            f"Коэффициент: {multiplier:.2f}x\n"
            f"Төлем: +{format_money(payout)} ₸\n\n"
            f"{roulette_history_text()}"
        )

    else:
        message = (
            f"🎰 {roulette_result_text(result)}\n\n"
            "❌ Ұтылдың.\n\n"
            f"Ставка: {format_money(amount)} ₸\n"
            f"Таңдау: "
            f"{roulette_bet_name(kind, value)}\n\n"
            f"{roulette_history_text()}"
        )

    await update.message.reply_text(message)


# =========================================================
# ИСТОРИЯ
# =========================================================

async def history_command(update, context):
    await update.message.reply_text(
        roulette_history_text()
    )


# =========================================================
# МИНЫ
# =========================================================

async def start_mines(update, parts):
    if len(parts) not in (2, 3):
        await update.message.reply_text(
            "❌ Формат:\n\n"
            "мины 1000\n"
            "мины 1000 5"
        )
        return True

    try:
        bet = parse_amount(parts[1])

        if len(parts) == 3:
            mine_count = int(parts[2])
        else:
            # По умолчанию 5 мин.
            mine_count = 5

    except ValueError:
        await update.message.reply_text(
            "❌ Формат:\n\n"
            "мины 1000\n"
            "мины 1000 5"
        )
        return True

    if not 1 <= mine_count <= 24:
        await update.message.reply_text(
            "❌ Миналар саны 1-24 "
            "аралығында болуы керек."
        )
        return True

    user = update.effective_user

    ensure_user(user)

    with mines_lock:
        if user.id in mines_games:
            await update.message.reply_text(
                "❌ Сенде қазір аяқталмаған "
                "Мины ойыны бар."
            )
            return True

    if not debit(user.id, bet):
        await update.message.reply_text(
            "❌ Қаражатың жеткіліксіз.\n\n"
            f"Қолжетімді: "
            f"{format_money(get_balance(user.id))} ₸\n"
            f"Қажет: "
            f"{format_money(bet)} ₸"
        )
        return True

    mine_positions = set(
        secrets.SystemRandom().sample(
            range(25),
            mine_count,
        )
    )

    game = {
        "user_id": user.id,
        "bet": bet,
        "mines": mine_positions,
        "opened": set(),
        "safe": 0,
    }

    with mines_lock:
        mines_games[user.id] = game

    markup = mines_board(
        game,
        reveal=False,
    )

    await update.message.reply_text(
        "💣 МИНАЛАР 5×5\n\n"
        f"💰 Ставка: {format_money(bet)} ₸\n"
        f"💣 Миналар: {mine_count}\n"
        "💎 Ашылғаны: 0\n"
        "📈 Қазіргі коэффициент: 1.00x\n\n"
        "Ұяшықты таңда:",
        reply_markup=markup,
    )

    return True


async def mines_callback(update, context):
    query = update.callback_query

    await query.answer()

    parts = query.data.split(":")

    if len(parts) != 3:
        return

    user_id = int(parts[1])
    index = int(parts[2])

    if query.from_user.id != user_id:
        await query.answer(
            "❌ Бұл сенің ойының емес.",
            show_alert=True,
        )
        return

    with mines_lock:
        game = mines_games.get(user_id)

    if game is None:
        await query.answer(
            "❌ Бұл ойын аяқталған.",
            show_alert=True,
        )
        return

    if index in game["opened"]:
        return

    # МИНА
    if index in game["mines"]:

        markup = mines_board(
            game,
            reveal=True,
        )

        with mines_lock:
            mines_games.pop(user_id, None)

        await query.edit_message_text(
            "💥 МИНА!\n\n"
            f"❌ Сен "
            f"{format_money(game['bet'])} ₸ жоғалттың.\n\n"
            "💣 МИНАЛАРДЫҢ ОРНАЛАСУЫ:",
            reply_markup=markup,
        )

        return

    # БЕЗОПАСНАЯ КЛЕТКА
    game["opened"].add(index)
    game["safe"] += 1

    safe_total = 25 - len(game["mines"])

    if game["safe"] >= safe_total:

        multiplier = mines_multiplier(
            game["safe"],
            len(game["mines"]),
        )

        payout = int(
            game["bet"] * multiplier
        )

        credit(
            user_id,
            payout,
        )

        markup = mines_board(
            game,
            reveal=True,
        )

        with mines_lock:
            mines_games.pop(user_id, None)

        await query.edit_message_text(
            "🏆 БАРЛЫҚ ҚАУІПСІЗ ҰЯШЫҚ АШЫЛДЫ!\n\n"
            f"📈 Коэффициент: "
            f"{multiplier:.2f}x\n"
            f"💰 Ұтыс: "
            f"+{format_money(payout)} ₸\n\n"
            "💣 МИНАЛАРДЫҢ ОРНАЛАСУЫ:",
            reply_markup=markup,
        )

        return

    multiplier = mines_multiplier(
        game["safe"],
        len(game["mines"]),
    )

    markup = mines_board(
        game,
        reveal=False,
    )

    await query.edit_message_text(
        "💣 МИНАЛАР 5×5\n\n"
        f"💰 Ставка: "
        f"{format_money(game['bet'])} ₸\n"
        f"💎 Ашылды: {game['safe']}\n"
        f"📈 Коэффициент: "
        f"{multiplier:.2f}x\n\n"
        "Жалғастыр немесе ұтысты ал:",
        reply_markup=markup,
    )


async def mines_cash(update, context):
    query = update.callback_query

    await query.answer()

    parts = query.data.split(":")

    if len(parts) != 2:
        return

    user_id = int(parts[1])

    if query.from_user.id != user_id:
        await query.answer(
            "❌ Бұл сенің ойының емес.",
            show_alert=True,
        )
        return

    with mines_lock:
        game = mines_games.get(user_id)

    if game is None:
        await query.answer(
            "❌ Ойын аяқталған.",
            show_alert=True,
        )
        return

    if game["safe"] <= 0:
        await query.answer(
            "Алдымен бір қауіпсіз "
            "ұяшық аш.",
            show_alert=True,
        )
        return

    multiplier = mines_multiplier(
        game["safe"],
        len(game["mines"]),
    )

    payout = int(
        game["bet"] * multiplier
    )

    credit(
        user_id,
        payout,
    )

    markup = mines_board(
        game,
        reveal=True,
    )

    with mines_lock:
        mines_games.pop(user_id, None)

    await query.edit_message_text(
        "💰 ҰТЫС АЛЫНДЫ!\n\n"
        f"📈 Коэффициент: "
        f"{multiplier:.2f}x\n"
        f"💰 Төлем: "
        f"+{format_money(payout)} ₸\n\n"
        "💣 МИНАЛАРДЫҢ ОРНАЛАСУЫ:",
        reply_markup=markup,
    )


# =========================================================
# ТЕКСТОВЫЙ РОУТЕР
# =========================================================

async def text_router(update, context):
    if not update.message:
        return

    text = update.message.text.strip()

    if not text:
        return

    low = text.lower()

    # =====================================================
    # БАЛАНС
    # =====================================================

    if low in {
        "баланс",
        "б",
        "бал",
        "ақша",
        "қаражат",
    }:
        await balance_command(
            update,
            context,
        )
        return

    # =====================================================
    # БОНУС
    # =====================================================

    if low in {
        "бонус",
        "bonus",
        "сыйлық",
        "сый",
        "күндік",
        "дейлик",
        "дб",
    }:
        await bonus_command(
            update,
            context,
        )
        return

    # =====================================================
    # ИСТОРИЯ
    # =====================================================

    if low in {
        "тарих",
        "тарихы",
        "history",
    }:
        await history_command(
            update,
            context,
        )
        return

    # =====================================================
    # МИНЫ
    # =====================================================

    if re.match(
        r"^(мины|миналар)\s+",
        low,
    ):
        parts = text.split()

        await start_mines(
            update,
            parts,
        )
        return

    # =====================================================
    # ПЕРЕВОД
    # =====================================================

    transfer_match = re.match(
        r"^(бер|аудар|жібер|берем)\s+(\S+)(?:\s+(.+))?$",
        low,
    )

    if transfer_match:
        amount_text = transfer_match.group(2)
        target_text = transfer_match.group(3)

        await transfer_command(
            update,
            amount_text,
            target_text,
        )
        return

    # =====================================================
    # РУЛЕТКА
    # =====================================================

    roulette_match = re.fullmatch(
        r"(\S+)\s+(\S+)",
        low,
    )

    if roulette_match:
        amount_text = roulette_match.group(1)
        bet_text = roulette_match.group(2)

        # Только если первое действительно является
        # суммой, а второе — допустимой ставкой.
        try:
            parse_amount(amount_text)
            bet = parse_bet(bet_text)
        except ValueError:
            bet = None

        if bet:
            await play_roulette(
                update,
                amount_text,
                bet_text,
            )
            return

    # =====================================================
    # ВАЖНО:
    # НЕ отвечаем на каждое непонятное слово.
    # =====================================================

    return


# =========================================================
# HTTP-СЕРВЕР ДЛЯ RENDER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )
        self.end_headers()

        self.wfile.write(
            b"Telegram bot is running."
        )

    def log_message(self, format, *args):
        return


def run_http_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler,
    )

    print(
        f"HTTP server started on port {PORT}"
    )

    server.serve_forever()


# =========================================================
# ОСНОВНЫЕ КОМАНДЫ
# =========================================================

def setup_handlers(application):

    # Команды
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "balance",
            balance_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "bonus",
            bonus_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "history",
            history_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "createp",
            create_promo,
        )
    )

    application.add_handler(
        CommandHandler(
            "promo",
            use_promo,
        )
    )

    # Кнопки мин
    application.add_handler(
        CallbackQueryHandler(
            mines_callback,
            pattern=r"^mine:",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            mines_cash,
            pattern=r"^cash:",
        )
    )

    # Обычный текст
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_router,
        )
    )


# =========================================================
# ЗАПУСК
# =========================================================

def main():

    print("Запуск бота...")

    # HTTP нужен Render Web Service,
    # чтобы он видел открытый порт.
    http_thread = threading.Thread(
        target=run_http_server,
        daemon=True,
    )

    http_thread.start()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    setup_handlers(application)

    print("Бот іске қосылды.")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
