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

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0

# Для Render Persistent Disk:
# Mount Path: /var/data
DB_PATH = os.getenv(
    "DB_PATH",
    "/var/data/economy.db"
)

PORT = int(os.getenv("PORT", "10000"))

START_BALANCE = 10_000

BONUS_AMOUNT = 5_000
BONUS_COOLDOWN = 4 * 60 * 60  # 4 часа


# =========================================================
# СОЗДАЁМ ПАПКУ ДЛЯ БАЗЫ
# =========================================================

db_dir = os.path.dirname(DB_PATH)

if db_dir:
    os.makedirs(
        db_dir,
        exist_ok=True
    )


# =========================================================
# РУЛЕТКА
# =========================================================

# В нашем боте:
# ⚪ = ақ
# ⚫ = қара
# 🟢 = 0

WHITE_NUMBERS = {
    1, 3, 5, 7, 9,
    12, 14, 16, 18,
    19, 21, 23, 25, 27,
    30, 32, 34, 36
}

roulette_history = []


# =========================================================
# МИНЫ
# =========================================================

mines_games = {}


# =========================================================
# БЛОКИРОВКА БАЗЫ
# =========================================================

DB_LOCK = threading.RLock()


# =========================================================
# SQLITE
# =========================================================

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False,
    timeout=30
)

db.row_factory = sqlite3.Row

with DB_LOCK:

    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")

    # -------------------------
    # Пользователи
    # -------------------------

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance INTEGER NOT NULL DEFAULT 10000,
            last_bonus INTEGER NOT NULL DEFAULT 0
        )
    """)

    # -------------------------
    # Промокоды
    # -------------------------

    db.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            max_uses INTEGER NOT NULL,
            used_count INTEGER NOT NULL DEFAULT 0,
            amount INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

    # -------------------------
    # Использованные промокоды
    # -------------------------

    db.execute("""
        CREATE TABLE IF NOT EXISTS promo_uses (
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(code, user_id)
        )
    """)

    # -------------------------
    # История рулетки
    # -------------------------

    db.execute("""
        CREATE TABLE IF NOT EXISTS roulette_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

    db.commit()


# =========================================================
# ЗАГРУЗКА ИСТОРИИ РУЛЕТКИ
# =========================================================

with DB_LOCK:

    rows = db.execute("""
        SELECT number
        FROM roulette_history
        ORDER BY id DESC
        LIMIT 20
    """).fetchall()

    roulette_history = [
        int(row["number"])
        for row in reversed(rows)
    ]


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
    1млн
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

    amount = int(
        number * multipliers[suffix]
    )

    if amount <= 0:
        raise ValueError

    return amount


# =========================================================
# ПОЛЬЗОВАТЕЛЬ
# =========================================================

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
                user.first_name or "",
                START_BALANCE,
            )
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
            (user_id,)
        ).fetchone()

        if row is None:
            return 0

        return int(row["balance"])


# =========================================================
# ДЕНЬГИ
# =========================================================

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


def transfer_money(
    sender_id,
    receiver_id,
    amount
):

    if amount <= 0:
        return False

    if sender_id == receiver_id:
        return False

    with DB_LOCK:

        try:

            db.execute(
                "BEGIN IMMEDIATE"
            )

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
# РУЛЕТКА — СТАВКА
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
        return (
            "color",
            "white"
        )

    # Қара
    if value in {
        "қара",
        "қ",
        "кара",
        "черный",
        "black",
    }:
        return (
            "color",
            "black"
        )

    # Жұп
    if value in {
        "жұп",
        "чет",
        "even",
    }:
        return (
            "parity",
            "even"
        )

    # Тақ
    if value in {
        "тақ",
        "так",
        "нечет",
        "odd",
    }:
        return (
            "parity",
            "odd"
        )

    # Точное число
    if re.fullmatch(
        r"\d+",
        value
    ):

        number = int(value)

        if 0 <= number <= 36:

            return (
                "number",
                number
            )

    # Диапазон
    match = re.fullmatch(
        r"(\d+)\s*[-–]\s*(\d+)",
        value
    )

    if match:

        start = int(
            match.group(1)
        )

        end = int(
            match.group(2)
        )

        if (
            0 <= start <= 36
            and 0 <= end <= 36
            and start <= end
        ):

            return (
                "range",
                (start, end)
            )

    return None


# =========================================================
# РУЛЕТКА — КОЭФФИЦИЕНТ
# =========================================================

def roulette_multiplier(
    kind,
    value
):

    # 0
    if kind == "number":

        if value == 0:
            return 100.0

        return 36.0

    # Ақ / Қара
    if kind == "color":
        return 1.90

    # Жұп / Тақ
    if kind == "parity":
        return 1.90

    # Диапазон
    if kind == "range":

        count = (
            value[1]
            - value[0]
            + 1
        )

        # Небольшое преимущество бота.
        multiplier = (
            36 / count
        ) * 0.95

        # Не позволяем диапазону
        # давать огромные выплаты.
        return max(
            1.01,
            min(
                35.0,
                multiplier
            )
        )

    return 1.0


# =========================================================
# РУЛЕТКА — ПРОВЕРКА ПОБЕДЫ
# =========================================================

def roulette_win(
    kind,
    value,
    result
):

    if kind == "number":

        return result == value

    if kind == "range":

        return (
            value[0]
            <= result
            <= value[1]
        )

    if kind == "color":

        # 0 зелёный и не считается
        # ни ақ, ни қара.
        if result == 0:
            return False

        result_color = (
            "white"
            if result in WHITE_NUMBERS
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


# =========================================================
# РУЛЕТКА — НАЗВАНИЕ СТАВКИ
# =========================================================

def roulette_bet_name(
    kind,
    value
):

    if kind == "number":
        return str(value)

    if kind == "range":

        return (
            f"{value[0]}-{value[1]}"
        )

    if kind == "color":

        if value == "white":
            return "ақ"

        return "қара"

    if kind == "parity":

        if value == "even":
            return "жұп"

        return "тақ"

    return "?"


# =========================================================
# РУЛЕТКА — РЕЗУЛЬТАТ
# =========================================================

def roulette_result_text(number):

    if number == 0:
        return "🟢 0"

    if number in WHITE_NUMBERS:
        return f"⚪ {number}"

    return f"⚫ {number}"


# =========================================================
# ДОБАВЛЕНИЕ В ИСТОРИЮ
# =========================================================

def add_roulette_history(number):

    global roulette_history

    with DB_LOCK:

        db.execute(
            """
            INSERT INTO roulette_history (
                number,
                created_at
            )
            VALUES (?, ?)
            """,
            (
                number,
                now()
            )
        )

        # Оставляем в базе только последние 20.
        db.execute(
            """
            DELETE FROM roulette_history
            WHERE id NOT IN (
                SELECT id
                FROM roulette_history
                ORDER BY id DESC
                LIMIT 20
            )
            """
        )

        db.commit()

    roulette_history.append(number)

    if len(roulette_history) > 20:

        del roulette_history[:-20]


# =========================================================
# ИСТОРИЯ РУЛЕТКИ
# =========================================================

def roulette_history_text():

    if not roulette_history:

        return (
            "📜 РУЛЕТКАНЫҢ ТАРИХЫ\n\n"
            "Әзірге нәтиже жоқ."
        )

    results = []

    for number in roulette_history:

        results.append(
            roulette_result_text(number)
        )

    return (
        "📜 РУЛЕТКАНЫҢ СОҢҒЫ "
        f"{len(results)} НӘТИЖЕСІ\n\n"
        + "  ".join(results)
    )


# =========================================================
# МИНЫ — КОЭФФИЦИЕНТ
# =========================================================

def mines_multiplier(
    safe_opened,
    mine_count
):

    if safe_opened <= 0:
        return 1.0

    safe_total = (
        25 - mine_count
    )

    if safe_opened > safe_total:
        return 1.0

    multiplier = 1.0

    # Небольшой house edge.
    house_factor = 0.97

    for step in range(
        1,
        safe_opened + 1
    ):

        probability = (
            (safe_total - step + 1)
            /
            (26 - step)
        )

        multiplier *= (
            probability
            and (
                1 / probability
            )
            or 1
        )

        multiplier *= house_factor

    return max(
        1.01,
        multiplier
    )


# =========================================================
# МИНЫ — КНОПКИ
# =========================================================

def mines_board(
    game,
    reveal=False
):

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
                    f"mine:"
                    f"{game['user_id']}:"
                    f"{index}"
                )
            )
        )

    rows = [
        buttons[i:i + 5]
        for i in range(
            0,
            25,
            5
        )
    ]

    if not reveal:

        rows.append([
            InlineKeyboardButton(
                "💰 Ұтысты алу",
                callback_data=(
                    f"cash:"
                    f"{game['user_id']}"
                )
            )
        ])

    return InlineKeyboardMarkup(
        rows
    )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    ensure_user(user)

    await update.message.reply_text(
        "🇰🇿 Сәлем!\n\n"
        "💰 Теңге экономика ботына "
        "қош келдің!\n\n"

        f"💵 Бастапқы баланс: "
        f"{format_money(START_BALANCE)} ₸\n"

        f"🎁 Бонус: "
        f"{format_money(BONUS_AMOUNT)} ₸ "
        f"/ 4 сағат\n\n"

        "💰 БАЛАНС\n"
        "баланс / б / бал / ақша\n\n"

        "🎁 БОНУС\n"
        "бонус / bonus / сыйлық\n\n"

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
        "Reply жасап: бер 5к\n"
        "бер 5к @username\n"
        "бер 5к ID\n\n"

        "🎟️ ПРОМО\n"
        "/promo CODE"
    )


# =========================================================
# БАЛАНС
# =========================================================

async def balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    ensure_user(user)

    balance = get_balance(
        user.id
    )

    await update.message.reply_text(
        "💰 БАЛАНСЫҢ\n\n"
        f"{format_money(balance)} ₸"
    )


# =========================================================
# БОНУС
# =========================================================

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

        last_bonus = int(
            row["last_bonus"]
        )

        remaining = (
            BONUS_COOLDOWN
            -
            (
                now()
                - last_bonus
            )
        )

        if remaining > 0:

            hours = (
                remaining // 3600
            )

            minutes = (
                remaining % 3600
            ) // 60

            await update.message.reply_text(
                "⏳ Бонус әлі дайын емес.\n\n"
                f"Қалғаны: "
                f"{hours} сағ "
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
                user.id
            )
        )

        db.commit()

    await update.message.reply_text(
        "🎁 БОНУС АЛЫНДЫ!\n\n"
        f"+{format_money(BONUS_AMOUNT)} ₸\n\n"
        "⏰ Келесі бонус "
        "4 сағаттан кейін."
    )


# =========================================================
# HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🇰🇿 КОМАНДАЛАР\n\n"

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

        "📜 Рулетка тарихы:\n"
        "тарих / history\n\n"

        "🎟️ Промокод:\n"
        "/promo gift1\n\n"

        "👑 Создание промокода:\n"
        "/createp gift1 20 30к\n\n"

        "🔢 СОМАЛАР:\n"
        "3000\n"
        "3к\n"
        "30к\n"
        "3кк\n"
        "2.5кк"
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

        amount = parse_amount(
            amount_text
        )

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
            "• Reply жасап: бер 5к\n"
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
            f"Қажет: "
            f"{format_money(amount)} ₸"
        )

        return

    await update.message.reply_text(
        "💸 АУДАРЫМ ОРЫНДАЛДЫ!\n\n"
        f"Сома: "
        f"{format_money(amount)} ₸\n"
        f"Кімге: "
        f"{target_user.first_name or 'пайдаланушы'}"
    )


# =========================================================
# СОЗДАНИЕ ПРОМО
# =========================================================

async def create_promo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != OWNER_ID:

        await update.message.reply_text(
            "❌ Бұл команда тек "
            "бот иесіне арналған."
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

    code = (
        context.args[0]
        .strip()
        .lower()
    )

    if not re.fullmatch(
        r"[a-zA-Z0-9_-]{1,50}",
        code
    ):

        await update.message.reply_text(
            "❌ Промокодта тек "
            "латын әріптері, "
            "сандар, _ және - болсын."
        )

        return

    try:

        max_uses = int(
            context.args[1]
        )

        amount = parse_amount(
            context.args[2]
        )

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
                    used_count,
                    amount,
                    created_at
                )
                VALUES (?, ?, 0, ?, ?)
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
                "❌ Бұл промокод "
                "бұрыннан бар."
            )

            return

    await update.message.reply_text(
        "✅ ПРОМОКОД ЖАСАЛДЫ!\n\n"
        f"🎟️ Код: {code}\n"
        f"👥 Активация: {max_uses}\n"
        f"💰 Сыйлық: "
        f"{format_money(amount)} ₸"
    )


# =========================================================
# ИСПОЛЬЗОВАНИЕ ПРОМО
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

    code = (
        context.args[0]
        .strip()
        .lower()
    )

    with DB_LOCK:

        try:

            db.execute(
                "BEGIN IMMEDIATE"
            )

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
                    "❌ Бұл промокодтың "
                    "лимиті біткен."
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
                    "❌ Сен бұл промокодты "
                    "бұрын қолдандың."
                )

                return

            db.execute(
                """
                UPDATE promo_codes
                SET used_count =
                    used_count + 1
                WHERE code = ?
                """,
                (code,)
            )

            db.execute(
                """
                UPDATE users
                SET balance =
                    balance + ?
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
                "❌ Промокодты қолдану "
                "кезінде қате болды."
            )

            return

    await update.message.reply_text(
        "🎁 ПРОМОКОД ҚАБЫЛДАНДЫ!\n\n"
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

        amount = parse_amount(
            amount_text
        )

    except ValueError:

        return

    bet = parse_bet(
        bet_text
    )

    if not bet:
        return

    kind, value = bet

    # 0 ограничиваем,
    # чтобы 100x не разрушал экономику.
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

    # Сначала списываем.
    if not debit(
        user.id,
        amount
    ):

        await update.message.reply_text(
            "❌ Қаражатың жеткіліксіз.\n\n"
            f"Қолжетімді: "
            f"{format_money(get_balance(user.id))} ₸\n"
            f"Қажет: "
            f"{format_money(amount)} ₸"
        )

        return

    # Криптографически случайное число.
    result = secrets.randbelow(37)

    add_roulette_history(
        result
    )

    won = roulette_win(
        kind,
        value,
        result
    )

    multiplier = roulette_multiplier(
        kind,
        value
    )

    bet_name = roulette_bet_name(
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

        await update.message.reply_text(
            f"🎰 РУЛЕТКА\n\n"
            f"Нәтиже: "
            f"{roulette_result_text(result)}\n\n"
            "🎉 ҰТЫС!\n\n"
            f"Ставка: "
            f"{format_money(amount)} ₸\n"
            f"Таңдау: {bet_name}\n"
            f"Коэффициент: "
            f"{multiplier:.2f}x\n"
            f"Төлем: "
            f"+{format_money(payout)} ₸\n\n"
            f"{roulette_history_text()}"
        )

    else:

        await update.message.reply_text(
            f"🎰 РУЛЕТКА\n\n"
            f"Нәтиже: "
            f"{roulette_result_text(result)}\n\n"
            "❌ ҰТЫЛДЫҢ.\n\n"
            f"Ставка: "
            f"{format_money(amount)} ₸\n"
            f"Таңдау: {bet_name}\n\n"
            f"{roulette_history_text()}"
        )


# =========================================================
# ИСТОРИЯ
# =========================================================

async def history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        roulette_history_text()
    )


# =========================================================
# МИНЫ — СТАРТ
# =========================================================

async def start_mines(
    update: Update,
    parts
):

    if len(parts) not in (2, 3):
        return False

    try:

        bet = parse_amount(
            parts[1]
        )

        if len(parts) == 3:

            mine_count = int(
                parts[2]
            )

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

    if not (
        1 <= mine_count <= 24
    ):

        await update.message.reply_text(
            "❌ Миналар саны "
            "1-24 аралығында "
            "болуы керек."
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
            f"Қажет: "
            f"{format_money(bet)} ₸"
        )

        return True

    random_generator = secrets.SystemRandom()

    mine_positions = set(
        random_generator.sample(
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

    await update.message.reply_text(
        "💣 МИНАЛАР 5×5\n\n"
        f"💰 Ставка: "
        f"{format_money(bet)} ₸\n"
        f"💣 Миналар: "
        f"{mine_count}\n"
        "💎 Ашылғаны: 0\n"
        "📈 Коэффициент: 1.00x\n\n"
        "Ұяшықты таңда:",
        reply_markup=markup
    )

    return True


# =========================================================
# МИНЫ — КЛЕТКА
# =========================================================

async def mines_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split(":")

    if len(parts) != 3:
        return

    try:

        user_id = int(
            parts[1]
        )

        index = int(
            parts[2]
        )

    except ValueError:
        return

    if (
        query.from_user.id != user_id
        or user_id not in mines_games
    ):

        await query.answer(
            "❌ Бұл сенің ойының емес.",
            show_alert=True
        )

        return

    if not (
        0 <= index < 25
    ):
        return

    game = mines_games[user_id]

    if index in game["opened"]:
        return

    # -------------------------
    # МИНА
    # -------------------------

    if index in game["mines"]:

        markup = mines_board(
            game,
            reveal=True
        )

        del mines_games[user_id]

        await query.edit_message_text(
            "💥 МИНА!\n\n"
            f"❌ Сен "
            f"{format_money(game['bet'])} ₸ "
            f"жоғалттың.\n\n"
            "💣 МИНАЛАРДЫҢ ОРНАЛАСУЫ:",
            reply_markup=markup
        )

        return

    # -------------------------
    # БЕЗОПАСНАЯ КЛЕТКА
    # -------------------------

    game["opened"].add(
        index
    )

    game["safe"] += 1

    safe_total = (
        25
        - len(game["mines"])
    )

    # Все безопасные клетки.
    if game["safe"] >= safe_total:

        multiplier = mines_multiplier(
            game["safe"],
            len(game["mines"])
        )

        payout = int(
            game["bet"]
            * multiplier
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
            "🏆 БАРЛЫҚ ҚАУІПСІЗ "
            "ҰЯШЫҚТАР АШЫЛДЫ!\n\n"
            f"📈 Коэффициент: "
            f"{multiplier:.2f}x\n"
            f"💰 Ұтыс: "
            f"+{format_money(payout)} ₸\n\n"
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
        f"💰 Ставка: "
        f"{format_money(game['bet'])} ₸\n"
        f"💎 Ашылды: "
        f"{game['safe']}\n"
        f"📈 Коэффициент: "
        f"{multiplier:.2f}x\n\n"
        "Жалғастыр немесе ұтысты ал:",
        reply_markup=markup
    )


# =========================================================
# МИНЫ — ЗАБРАТЬ
# =========================================================

async def mines_cash(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split(":")

    if len(parts) != 2:
        return

    try:

        user_id = int(
            parts[1]
        )

    except ValueError:
        return

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
        game["bet"]
        * multiplier
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
        f"📈 Коэффициент: "
        f"{multiplier:.2f}x\n"
        f"💰 Төлем: "
        f"+{format_money(payout)} ₸\n\n"
        "💣 МИНАЛАРДЫҢ ОРНАЛАСУЫ:",
        reply_markup=markup
    )


# =========================================================
# ПОИСК ПОЛЬЗОВАТЕЛЯ В БД
# =========================================================

def find_user_by_username(
    username
):

    username = (
        username
        .lower()
        .lstrip("@")
    )

    with DB_LOCK:

        return db.execute(
            """
            SELECT *
            FROM users
            WHERE lower(username) = ?
            """,
            (username,)
        ).fetchone()


def find_user_by_id(
    user_id
):

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

    if (
        not update.message
        or not update.message.text
    ):
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
        "ақшам",
        "қаражат",
        "балансым",
    }:

        await balance_command(
            update,
            context
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
            context
        )

        return

    # =====================================================
    # ПОМОЩЬ
    # =====================================================

    if low in {
        "көмек",
        "помощь",
        "help",
        "командалар",
        "команда",
    }:

        await help_command(
            update,
            context
        )

        return

    # =====================================================
    # ИСТОРИЯ
    # =====================================================

    if low in {
        "тарих",
        "тарихы",
        "history",
        "рулетка тарихы",
    }:

        await history_command(
            update,
            context
        )

        return

    # =====================================================
    # МИНЫ
    # =====================================================

    if re.match(
        r"^(мины|миналар)\b",
        low
    ):

        parts = text.split()

        await start_mines(
            update,
            parts
        )

        return

    # =====================================================
    # ПЕРЕВОД
    # =====================================================

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
        )

        target_user = None

        # -------------------------
        # Reply
        # -------------------------

        if (
            update.message.reply_to_message
            and
            update.message.reply_to_message.from_user
        ):

            target_user = (
                update.message
                .reply_to_message
                .from_user
            )

        # -------------------------
        # @username / ID
        # -------------------------

        elif target_text:

            target_text = (
                target_text.strip()
            )

            username_match = re.fullmatch(
                r"@([A-Za-z0-9_]{3,32})",
                target_text
            )

            if username_match:

                username = (
                    username_match.group(1)
                )

                row = find_user_by_username(
                    username
                )

                if row:

                    try:

                        target_user = (
                            await context.bot.get_chat(
                                f"@{username}"
                            )
                        )

                    except Exception:

                        target_user = None

            elif re.fullmatch(
                r"-?\d+",
                target_text
            ):

                try:

                    target_id = int(
                        target_text
                    )

                    row = find_user_by_id(
                        target_id
                    )

                    if row:

                        try:

                            target_user = (
                                await context.bot.get_chat(
                                    target_id
                                )
                            )

                        except Exception:

                            target_user = None

                except ValueError:

                    target_user = None

        await transfer_command(
            update,
            amount_text,
            target_user
        )

        return

    # =====================================================
    # РУЛЕТКА
    #
    # Только:
    #
    # 2000 ақ
    # 2000 а
    # 2000 қара
    # 2000 қ
    # 2000 16
    # 2000 16-30
    # 2000 жұп
    # 2000 тақ
    # =====================================================

    roulette_match = re.fullmatch(
        r"(\S+)\s+(.+)",
        text
    )

    if roulette_match:

        amount_text = (
            roulette_match.group(1)
        )

        bet_text = (
            roulette_match.group(2)
            .strip()
        )

        # Если первое слово не сумма —
        # это обычное сообщение.
        try:

            parse_amount(
                amount_text
            )

        except ValueError:

            return

        # Если второе слово не ставка —
        # тоже просто игнорируем.
        if parse_bet(
            bet_text
        ) is None:

            return

        await play_roulette(
            update,
            amount_text,
            bet_text
        )

        return

    # =====================================================
    # НЕИЗВЕСТНЫЕ СООБЩЕНИЯ
    #
    # НИЧЕГО НЕ ДЕЛАЕМ.
    # =====================================================

    return


# =========================================================
# HEALTH CHECK ДЛЯ RENDER
# =========================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"OK"
        )

    def log_message(
        self,
        format,
        *args
    ):
        return


def start_health_server():

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )

    thread.start()

    print(
        f"Health server started on port {PORT}"
    )


# =========================================================
# ОБРАБОТКА ОШИБОК
# =========================================================

async def error_handler(
    update,
    context
):

    print(
        "BOT ERROR:",
        repr(context.error)
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "BOT_TOKEN не найден. "
            "Добавь переменную BOT_TOKEN "
            "в Environment Variables."
        )

    print(
        "================================"
    )

    print(
        "🇰🇿 Бот запускается..."
    )

    print(
        f"Database: {DB_PATH}"
    )

    print(
        f"Owner ID: {OWNER_ID}"
    )

    print(
        "================================"
    )

    # Health server нужен,
    # если Render использует Web Service.
    start_health_server()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    # =====================================================
    # КОМАНДЫ
    # =====================================================

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "history",
            history_command
        )
    )

    application.add_handler(
        CommandHandler(
            "promo",
            use_promo
        )
    )

    application.add_handler(
        CommandHandler(
            "createp",
            create_promo
        )
    )

    # =====================================================
    # МИНЫ
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            mines_callback,
            pattern=r"^mine:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            mines_cash,
            pattern=r"^cash:"
        )
    )

    # =====================================================
    # ТЕКСТ
    #
    # Команды Telegram сюда НЕ попадают.
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_router
        )
    )

    # =====================================================
    # ОШИБКИ
    # =====================================================

    application.add_error_handler(
        error_handler
    )

    print(
        "🇰🇿 Бот іске қосылды."
    )

    print(
        "💾 SQLite database active."
    )

    # =====================================================
    # POLLING
    # =====================================================

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
