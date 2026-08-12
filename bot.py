import os
import re
import sqlite3
import secrets
import threading
from time import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DB_PATH = os.getenv("DB_PATH", "economy.db")
PORT = int(os.getenv("PORT", "10000"))

START_BALANCE = 10_000

BONUS_AMOUNT = 5_000
BONUS_COOLDOWN = 4 * 60 * 60  # 4 часа

# Рулетка
RED_NUMBERS = {
    1, 3, 5, 7, 9,
    12, 14, 16, 18,
    19, 21, 23, 25, 27,
    30, 32, 34, 36
}

# Последние 20 результатов рулетки
roulette_history = []

# Игры Мины
mines_games = {}

# Защита БД
DB_LOCK = threading.RLock()


# =========================================================
# БАЗА ДАННЫХ
# =========================================================

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
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
    Поддерживает:

    3000
    3к
    30к
    3кк
    2.5к
    2.5кк
    1м
    """

    value = str(value).strip().lower()
    value = value.replace(" ", "")
    value = value.replace(",", ".")

    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)(кк|kk|к|k|млн|м)?",
        value
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
                user.first_name,
                START_BALANCE,
            )
        )

        db.commit()


def get_balance(user_id):
    with DB_LOCK:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row is None:
            return 0

        return int(row["balance"])


# =========================================================
# ДЕНЬГИ
# =========================================================

def debit(user_id, amount):
    """
    Атомарно списывает деньги.
    Никогда не позволит уйти балансу ниже 0.
    """

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
            )
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
            )
        )

        db.commit()

        return cursor.rowcount == 1


def transfer_money(sender_id, receiver_id, amount):
    """
    Полностью атомарный перевод.

    Если денег недостаточно —
    ничего не списывается.
    """

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
                )
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
                )
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

    # Ақ
    if value in {
        "ақ",
        "а",
        "ак",
        "white",
    }:
        return ("color", "white")

    # Қара
    if value in {
        "қара",
        "қ",
        "кара",
        "черный",
        "black",
    }:
        return ("color", "black")

    # Жұп
    if value in {
        "жұп",
        "чет",
        "even",
    }:
        return ("parity", "even")

    # Тақ
    if value in {
        "тақ",
        "так",
        "нечет",
        "odd",
    }:
        return ("parity", "odd")

    # Точное число
    if re.fullmatch(r"\d+", value):
        number = int(value)

        if 0 <= number <= 36:
            return ("number", number)

    # Диапазон
    match = re.fullmatch(
        r"(\d+)\s*[-–]\s*(\d+)",
        value
    )

    if match:
        start = int(match.group(1))
        end = int(match.group(2))

        if (
            0 <= start <= 36
            and 0 <= end <= 36
            and start <= end
        ):
            return ("range", (start, end))

    return None


def roulette_multiplier(kind, value):
    # 0 = 100x
    if kind == "number":
        if value == 0:
            return 100

        return 36

    # Ақ / Қара
    if kind == "color":
        return 2

    # Жұп / Тақ
    if kind == "parity":
        return 2

    # Диапазоны
    if kind == "range":
        count = value[1] - value[0] + 1

        # Небольшой house edge.
        multiplier = (36 / count) * 0.95

        return max(1.01, min(35, multiplier))

    return 1


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
    roulette_history.append(number)

    if len(roulette_history) > 20:
        del roulette_history[:-20]


def roulette_history_text():
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
    Сәл жоғары коэффициент.

    0.97 house factor.
    """

    if safe_opened <= 0:
        return 1.0

    safe_total = 25 - mine_count

    multiplier = 1.0

    for step in range(1, safe_opened + 1):
        probability_step = (
            (26 - step)
            / (safe_total - step + 1)
        )

        multiplier *= probability_step * 0.97

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
                callback_data=f"mine:{game['user_id']}:{index}"
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
                callback_data=f"cash:{game['user_id']}"
            )
        ])

    return InlineKeyboardMarkup(rows)


# =========================================================
# КОМАНДЫ
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    ensure_user(user)

    await update.message.reply_text(
        "🇰🇿 Сәлем!\n\n"
        "💰 Теңге экономика ботына қош келдің!\n\n"
        f"💵 Бастапқы баланс: {format_money(START_BALANCE)} ₸\n"
        f"🎁 Бонус: {format_money(BONUS_AMOUNT)} ₸ / 4 сағат\n\n"

        "💰 Баланс:\n"
        "баланс / б / бал / ақша\n\n"

        "🎁 Бонус:\n"
        "бонус / bonus / сыйлық\n\n"

        "🎰 Рулетка:\n"
        "2000 ақ\n"
        "2000 а\n"
        "2000 қара\n"
        "2000 қ\n"
        "2000 16\n"
        "2000 16-30\n\n"

        "💣 Мины:\n"
        "мины 1000\n"
        "мины 1000 5\n\n"

        "💸 Аударым:\n"
        "бер 5000 — reply арқылы\n"
        "бер 5к @username\n"
        "бер 5к ID"
    )


async def balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    ensure_user(update.effective_user)

    balance = get_balance(
        update.effective_user.id
    )

    await update.message.reply_text(
        f"💰 Балансың:\n"
        f"{format_money(balance)} ₸"
    )


async def bonus_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    ensure_user(user)

    with DB_LOCK:

        row = db.execute(
            """
            SELECT last_bonus
            FROM users
            WHERE user_id = ?
            """,
            (user.id,)
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

        # Бонус және время выдачи
        # меняются одной операцией.
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
            )
        )

        db.commit()

    await update.message.reply_text(
        "🎁 Бонус алынды!\n\n"
        f"+{format_money(BONUS_AMOUNT)} ₸\n\n"
        "⏰ Келесі бонус 4 сағаттан кейін."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "🇰🇿 КӨМЕК\n\n"

        "💰 Баланс:\n"
        "баланс / б / бал / ақша\n\n"

        "🎁 Бонус:\n"
        "бонус / bonus / сыйлық\n"
        "Әр 4 сағат сайын.\n\n"

        "💸 Аударым:\n"
        "Reply жасап: бер 5к\n"
        "бер 5к @username\n"
        "бер 5к 123456789\n\n"

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

        "📜 Рулетка тарихы:\n"
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
# ПЕРЕВОДЫ
# =========================================================

async def transfer_command(
    update: Update,
    amount_text,
    target_user
):
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

    if target_user is None:
        await update.message.reply_text(
            "❌ Алушы табылмады.\n\n"
            "Мысал:\n"
            "• хабарламаға reply жасап: бер 5к\n"
            "• бер 5к @username\n"
            "• бер 5к ID"
        )

        return

    if target_user.id == sender.id:
        await update.message.reply_text(
            "❌ Өзіңе ақша жібере алмайсың."
        )

        return

    ensure_user(target_user)

    success = transfer_money(
        sender.id,
        target_user.id,
        amount
    )

    if not success:
        await update.message.reply_text(
            "❌ Қаражатың жеткіліксіз.\n\n"
            f"Қолжетімді: "
            f"{format_money(get_balance(sender.id))} ₸\n"
            f"Қажет: {format_money(amount)} ₸"
        )

        return

    await update.message.reply_text(
        "💸 Аударым орындалды!\n\n"
        f"Сома: {format_money(amount)} ₸\n"
        f"Кімге: {target_user.first_name}"
    )


# =========================================================
# ПРОМОКОД СОЗДАНИЕ
# =========================================================

async def create_promo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
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

    try:
        max_uses = int(context.args[1])
        amount = parse_amount(context.args[2])

        if not (
            1 <= max_uses <= 1_000_000
        ):
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
                    now()
                )
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
        f"👥 Активаций: {max_uses}\n"
        f"💰 Сыйлық: {format_money(amount)} ₸"
    )


# =========================================================
# ПРОМОКОД ИСПОЛЬЗОВАНИЕ
# =========================================================

async def use_promo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
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
                (code,)
            ).fetchone()

            if promo is None:

                db.rollback()

                await update.message.reply_text(
                    "❌ Промокод табылмады."
                )

                return

            if (
                promo["used_count"]
                >= promo["max_uses"]
            ):

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
                        user.id
                    )
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
                (code,)
            )

            db.execute(
                """
                UPDATE users
                SET balance = balance + ?
                WHERE user_id = ?
                """,
                (
                    promo["amount"],
                    user.id
                )
            )

            db.commit()

        except Exception:

            db.rollback()

            await update.message.reply_text(
                "❌ Қате кезінде промокод қолданылмады."
            )

            return

    await update.message.reply_text(
        "🎁 Промокод қабылданды!\n\n"
        f"+{format_money(promo['amount'])} ₸"
    )


# =========================================================
# РУЛЕТКА
# =========================================================

async def play_roulette(
    update: Update,
    amount_text,
    bet_text
):
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
            "2000 16-30"
        )

        return

    kind, value = bet

    # Ограничение на 0.
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

    # Сначала списываем деньги.
    # Если денег нет — random даже не запускается.
    if not debit(user.id, amount):

        await update.message.reply_text(
            "❌ Қаражатың жеткіліксіз.\n\n"
            f"Қолжетімді: "
            f"{format_money(get_balance(user.id))} ₸\n"
            f"Қажет: {format_money(amount)} ₸"
        )

        return

    # Независимый случайный результат 0-36.
    result = secrets.randbelow(37)

    add_roulette_history(result)

    won = roulette_win(
        kind,
        value,
        result
    )

    multiplier = roulette_multiplier(
        kind,
        value
    )

    if won:

        payout = int(
            amount * multiplier
        )

        credit(
            user.id,
            payout
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

    await update.message.reply_text(
        message
    )


async def history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        roulette_history_text()
    )


# =========================================================
# МИНЫ
# =========================================================

async def start_mines(
    update: Update,
    parts
):
    """
    Поддерживает:

    мины 1000
    мины 1000 5

    Если число мин не указано —
    автоматически 5.
    """

    if len(parts) not in (2, 3):
        return False

    try:

        bet = parse_amount(parts[1])

        if len(parts) == 3:
            mine_count = int(parts[2])
        else:
            mine_count = 5

    except ValueError:

        await update.message.reply_text(
            "❌ Формат:\n\n"
            "мины 1000\n"
            "мины 1000 5"
        )

        return True

    if not (
        1 <= mine_count <= 24
    ):

        await update.message.reply_text(
            "❌ Миналар саны 1-24 "
            "аралығында болуы керек."
        )

        return True

    user = update.effective_user

    ensure_user(user)

    if user.id in mines_games:

        await update.message.reply_text(
            "❌ Сенде қазір аяқталмаған "
            "Мины ойыны бар."
        )

        return True

    if not debit(
        user.id,
        bet
    ):

        await update.message.reply_text(
            "❌ Қаражатың жеткіліксіз.\n\n"
            f"Қолжетімді: "
            f"{format_money(get_balance(user.id))} ₸\n"
            f"Қажет: {format_money(bet)} ₸"
        )

        return True

    # Случайно выбираем мины.
    mine_positions = set(
        secrets.SystemRandom().sample(
            range(25),
            mine_count
        )
    )

    game = {
        "user_id": user.id,
        "bet": bet,
        "mines": mine_positions,
        "opened": set(),
        "safe": 0,
    }

    mines_games[user.id] = game

    markup = mines_board(
        game,
        reveal=False
    )

    multiplier = mines_multiplier(
        0,
        mine_count
    )

    await update.message.reply_text(
        "💣 МИНАЛАР 5×5\n\n"
        f"💰 Ставка: {format_money(bet)} ₸\n"
        f"💣 Миналар: {mine_count}\n"
        "💎 Ашылғаны: 0\n"
        f"📈 Қазіргі коэффициент: {multiplier:.2f}x\n\n"
        "Ұяшықты таңда:",
        reply_markup=markup
    )

    return True


async def mines_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    _, user_id_text, index_text = (
        query.data.split(":")
    )

    user_id = int(user_id_text)
    index = int(index_text)

    if (
        query.from_user.id != user_id
        or user_id not in mines_games
    ):

        await query.answer(
            "❌ Бұл сенің ойының емес.",
            show_alert=True
        )

        return

    game = mines_games[user_id]

    if index in game["opened"]:
        return

    # Попал на мину.
    if index in game["mines"]:

        markup = mines_board(
            game,
            reveal=True
        )

        del mines_games[user_id]

        await query.edit_message_text(
            "💥 МИНА!\n\n"
            f"❌ Сен {format_money(game['bet'])} ₸ жоғалттың.\n\n"
            "💣 МИНАЛАРДЫҢ ОРНАЛАСУЫ:",
            reply_markup=markup
        )

        return

    # Безопасная клетка.
    game["opened"].add(index)
    game["safe"] += 1

    safe_total = (
        25 - len(game["mines"])
    )

    # Открыты все безопасные.
    if game["safe"] >= safe_total:

        multiplier = mines_multiplier(
            game["safe"],
            len(game["mines"])
        )

        payout = int(
            game["bet"] * multiplier
        )

        credit(
            user_id,
            payout
        )

        markup = mines_board(
            game,
            reveal=True
        )

        del mines_games[user_id]

        await query.edit_message_text(
            "🏆 ВСЕ БЕЗОПАСНЫЕ КЛЕТКИ ОТКРЫТЫ!\n\n"
            f"📈 Коэффициент: {multiplier:.2f}x\n"
            f"💰 Ұтыс: +{format_money(payout)} ₸\n\n"
            "💣 МИНАЛАРДЫҢ ОРНАЛАСУЫ:",
            reply_markup=markup
        )

        return

    multiplier = mines_multiplier(
        game["safe"],
        len(game["mines"])
    )

    markup = mines_board(
        game,
        reveal=False
    )

    await query.edit_message_text(
        "💣 МИНАЛАР 5×5\n\n"
        f"💰 Ставка: {format_money(game['bet'])} ₸\n"
        f"💎 Ашылды: {game['safe']}\n"
        f"📈 Коэффициент: {multiplier:.2f}x\n\n"
        "Жалғастыр немесе ұтысты ал:",
        reply_markup=markup
    )


async def mines_cash(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    _, user_id_text = (
        query.data.split(":")
    )

    user_id = int(user_id_text)

    if (
        query.from_user.id != user_id
        or user_id not in mines_games
    ):

        await query.answer(
            "❌ Бұл сенің ойының емес.",
            show_alert=True
        )

        return

    game = mines_games[user_id]

    if game["safe"] <= 0:

        await query.answer(
            "Алдымен кемінде бір "
            "қауіпсіз ұяшық аш.",
            show_alert=True
        )

        return

    multiplier = mines_multiplier(
        game["safe"],
        len(game["mines"])
    )

    payout = int(
        game["bet"] * multiplier
    )

    credit(
        user_id,
        payout
    )

    markup = mines_board(
        game,
        reveal=True
    )

    del mines_games[user_id]

    await query.edit_message_text(
        "💰 ҰТЫС АЛЫНДЫ!\n\n"
        f"📈 Коэффициент: {multiplier:.2f}x\n"
        f"💰 Төлем: +{format_money(payout)} ₸\n\n"
        "💣 МИНАЛАРДЫҢ ОРНАЛАСУЫ:",
        reply_markup=markup
    )


# =========================================================
# ПОИСК ПОЛЬЗОВАТЕЛЯ
# =========================================================

def find_user_by_username(username):
    username = username.lower().lstrip("@")

    with DB_LOCK:

        row = db.execute(
            """
            SELECT *
            FROM users
            WHERE lower(username) = ?
            """,
            (username,)
        ).fetchone()

    return row


def find_user_by_id(user_id):
    with DB_LOCK:

        return db.execute(
            """
            SELECT *
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()


# =========================================================
# ТЕКСТОВОЙ РОУТЕР
# =========================================================

async def text_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text.strip()

    low = text.lower()

    # -------------------------
    # Баланс
    # -------------------------

    if low in {
        "баланс",
        "б",
        "бал",
        "ақша",
        "қаражат",
    }:

        await balance_command(
            update,
            context
        )

        return

    # -------------------------
    # Бонус
    # -------------------------

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
            context
        )

        return

    # -------------------------
    # История рулетки
    # -------------------------

    if low in {
        "тарих",
        "тарихы",
        "history",
    }:

        await history_command(
            update,
            context
        )

        return

    # -------------------------
    # Мины
    # -------------------------

    if (
        low.startswith("мины ")
        or low.startswith("миналар ")
    ):

        parts = text.split()

        handled = await start_mines(
            update,
            parts
        )

        if handled:
            return

    # -------------------------
    # Переводы
    # -------------------------

    transfer_match = re.match(
        r"^(бер|аудар|жібер|берем)\s+(\S+)(?:\s+(.+))?$",
        low
    )

    if transfer_match:

        amount_text = (
            transfer_match.group(2)
        )

        target_text = (
            transfer_match.group(3)
       
