import os
import re
import sqlite3
import secrets
import threading
from time import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "economy.db")

START_BALANCE = 10_000
BONUS_AMOUNT = 5_000
BONUS_COOLDOWN = 4 * 60 * 60  # 4 сағат

DB_LOCK = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
with DB_LOCK:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance INTEGER NOT NULL DEFAULT 10000,
        last_bonus INTEGER NOT NULL DEFAULT 0
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS promo_codes(
        code TEXT PRIMARY KEY,
        max_uses INTEGER NOT NULL,
        used_count INTEGER NOT NULL DEFAULT 0,
        amount INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS promo_uses(
        code TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY(code,user_id)
    )""")
    db.commit()

# Рулетка: бұл жиынтықтар классикалық қызыл/қараға сәйкес,
# бірақ пайдаланушыға қызылдың орнына "ақ" көрсетіледі.
RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

# Бір пайдаланушыға бір уақытта бір мины ойыны.
games = {}


def ts():
    return int(time())


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def parse_amount(s):
    """3000, 3к, 30к, 3кк, 3м, 1.5к, 2.5кк."""
    s = str(s).strip().lower().replace(" ", "").replace(",", ".")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(кк|kk|к|k|м|млн)?", s)
    if not m:
        raise ValueError
    value = float(m.group(1))
    suffix = m.group(2) or ""
    mult = {
        "": 1,
        "к": 1_000, "k": 1_000,
        "кк": 1_000_000, "kk": 1_000_000,
        "м": 1_000_000, "млн": 1_000_000,
    }[suffix]
    amount = int(value * mult)
    if amount <= 0:
        raise ValueError
    return amount


def ensure_user(u):
    with DB_LOCK:
        db.execute("""INSERT INTO users(user_id,username,first_name,balance)
                      VALUES(?,?,?,?)
                      ON CONFLICT(user_id) DO UPDATE SET
                      username=excluded.username,
                      first_name=excluded.first_name""",
                   (u.id, u.username, u.first_name, START_BALANCE))
        db.commit()


def get_balance(uid):
    with DB_LOCK:
        row = db.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        return int(row["balance"]) if row else 0


def atomic_debit(uid, amount):
    if amount <= 0:
        return False
    with DB_LOCK:
        cur = db.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
            (amount, uid, amount)
        )
        db.commit()
        return cur.rowcount == 1


def credit(uid, amount):
    if amount <= 0:
        return False
    with DB_LOCK:
        cur = db.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (amount, uid)
        )
        db.commit()
        return cur.rowcount == 1


def atomic_transfer(sender, receiver, amount):
    if amount <= 0 or sender == receiver:
        return False
    with DB_LOCK:
        try:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute(
                "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                (amount, sender, amount)
            )
            if cur.rowcount != 1:
                db.rollback()
                return False
            cur = db.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (amount, receiver)
            )
            if cur.rowcount != 1:
                db.rollback()
                return False
            db.commit()
            return True
        except Exception:
            db.rollback()
            return False


def parse_bet(text):
    t = text.strip().lower()
    if t in {"ақ", "а"}:
        return ("color", "white")
    if t in {"қара", "қ"}:
        return ("color", "black")
    if t in {"жұп", "чет", "even"}:
        return ("parity", "even")
    if t in {"тақ", "нечет", "odd"}:
        return ("parity", "odd")

    if re.fullmatch(r"\d+", t):
        n = int(t)
        if 0 <= n <= 36:
            return ("number", n)

    m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 <= a <= b <= 36:
            return ("range", (a, b))
    return None


def multiplier(kind, value):
    if kind == "number":
        return 100 if value == 0 else 36
    if kind in {"color", "parity"}:
        return 2
    n = value[1] - value[0] + 1
    # Кішкене house edge. Диапазон ұлғайған сайын коэффициент азаяды.
    return max(1.01, min(35.0, (36 / n) * 0.95))


def bet_wins(kind, value, result):
    if kind == "number":
        return result == value
    if kind == "range":
        return value[0] <= result <= value[1]
    if kind == "color":
        if result == 0:
            return False
        return ("white" if result in RED_NUMBERS else "black") == value
    if kind == "parity":
        if result == 0:
            return False
        return ("even" if result % 2 == 0 else "odd") == value
    return False


def bet_name(kind, value):
    if kind == "number":
        return str(value)
    if kind == "range":
        return f"{value[0]}-{value[1]}"
    if kind == "color":
        return "ақ" if value == "white" else "қара"
    return "жұп" if value == "even" else "тақ"


def result_name(n):
    if n == 0:
        return "🟢 0"
    return f"🔴 {n}" if n in RED_NUMBERS else f"⚫ {n}"


async def start(update, context):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "🇰🇿 Сәлем! Теңгелік экономика ботына қош келдің!\n\n"
        f"💰 Бастапқы баланс: {fmt(START_BALANCE)} ₸\n"
        "🎁 Бонус: әр 4 сағат сайын 5 000 ₸\n\n"
        "💳 Баланс: баланс / б / бал / ақша\n"
        "🎁 Бонус: бонус / bonus / сыйлық\n"
        "💸 Аударым: бер 5к @username\n"
        "🎰 Рулетка: 2000 ақ, 2000 16, 2000 16-30\n"
        "💣 Мины: мины 1000 5\n"
        "🎟️ Промокод: /promo КОД"
    )


async def balance_cmd(update, context):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        f"💰 Балансың: {fmt(get_balance(update.effective_user.id))} ₸"
    )


async def bonus_cmd(update, context):
    ensure_user(update.effective_user)
    uid = update.effective_user.id

    with DB_LOCK:
        row = db.execute(
            "SELECT last_bonus FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        last = int(row["last_bonus"])

        left = BONUS_COOLDOWN - (ts() - last)
        if left > 0:
            h, rem = divmod(left, 3600)
            m = rem // 60
            await update.message.reply_text(
                f"⏳ Бонус дайын емес.\nҚайта алу үшін: {h} сағ {m} мин."
            )
            return

        # Бонус пен timestamp бір транзакцияда.
        db.execute(
            "UPDATE users SET balance=balance+?, last_bonus=? "
            "WHERE user_id=?",
            (BONUS_AMOUNT, ts(), uid)
        )
        db.commit()

    await update.message.reply_text(
        f"🎁 Бонус алынды: +{fmt(BONUS_AMOUNT)} ₸\n"
        "⏰ Келесі бонус 4 сағаттан кейін."
    )


async def transfer(update, amount_text, target):
    ensure_user(update.effective_user)
    sender = update.effective_user

    try:
        amount = parse_amount(amount_text)
    except ValueError:
        await update.message.reply_text(
            "❌ Сома қате. Мысалы: 5000, 5к немесе 2.5кк."
        )
        return

    if not target:
        await update.message.reply_text(
            "❌ Алушы көрсетілмеді.\n"
            "Жауап ретінде «бер 5к» жаз немесе «бер 5к @username»."
        )
        return

    if target.id == sender.id:
        await update.message.reply_text("❌ Өзіңе ақша жібере алмайсың.")
        return

    ensure_user(target)

    if not atomic_transfer(sender.id, target.id, amount):
        await update.message.reply_text(
            f"❌ Қаражатың жеткіліксіз.\n"
            f"Қолжетімді: {fmt(get_balance(sender.id))} ₸\n"
            f"Қажет: {fmt(amount)} ₸"
        )
        return

    await update.message.reply_text(
        f"💸 {fmt(amount)} ₸ {target.first_name} деген қолданушыға жіберілді."
    )


async def create_promo(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Бұл команда тек бот иесіне арналған.")
        return

    if len(context.args) != 3:
        await update.message.reply_text(
            "Формат:\n/createp код активация_саны сома\n\n"
            "Мысал:\n/createp gift1 20 30к"
        )
        return

    code = context.args[0].lower()
    try:
        uses = int(context.args[1])
        amount = parse_amount(context.args[2])
        if not (1 <= uses <= 1_000_000):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Активация саны немесе сома қате.")
        return

    with DB_LOCK:
        try:
            db.execute(
                "INSERT INTO promo_codes(code,max_uses,amount,created_at) "
                "VALUES(?,?,?,?)",
                (code, uses, amount, ts())
            )
            db.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text("❌ Бұл промокод бұрыннан бар.")
            return

    await update.message.reply_text(
        f"✅ Промокод жасалды!\n"
        f"Код: {code}\n"
        f"Активация: {uses}\n"
        f"Сыйлық: {fmt(amount)} ₸"
    )


async def use_promo(update, context):
    ensure_user(update.effective_user)
    if len(context.args) != 1:
        await update.message.reply_text("Формат: /promo КОД")
        return

    uid = update.effective_user.id
    code = context.args[0].lower()

    with DB_LOCK:
        try:
            db.execute("BEGIN IMMEDIATE")
            promo = db.execute(
                "SELECT * FROM promo_codes WHERE code=?", (code,)
            ).fetchone()

            if not promo:
                db.rollback()
                await update.message.reply_text("❌ Промокод табылмады.")
                return

            if promo["used_count"] >= promo["max_uses"]:
                db.rollback()
                await update.message.reply_text("❌ Промокодтың лимиті біткен.")
                return

            try:
                db.execute(
                    "INSERT INTO promo_uses(code,user_id) VALUES(?,?)",
                    (code, uid)
                )
            except sqlite3.IntegrityError:
                db.rollback()
                await update.message.reply_text(
                    "❌ Бұл промокодты бұрын қолдандың."
                )
                return

            db.execute(
                "UPDATE promo_codes SET used_count=used_count+1 WHERE code=?",
                (code,)
            )
            db.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (promo["amount"], uid)
            )
            db.commit()
        except Exception:
            db.rollback()
            await update.message.reply_text("❌ Промокодты қолдану кезінде қате болды.")
            return

    await update.message.reply_text(
        f"🎁 Промокод қабылданды!\n+{fmt(promo['amount'])} ₸"
    )


def mines_multiplier(safe, mines):
    if safe <= 0:
        return 1.0
    # Fair probability product with 6% house edge.
    mult = 1.0
    safe_total = 25 - mines
    for k in range(1, safe + 1):
        fair_step = (26 - k) / (safe_total - k + 1)
        mult *= fair_step * 0.94
    return max(1.01, mult)


async def mines_start(update, context):
    parts = update.message.text.split()
    if len(parts) != 3:
        return False

    try:
        bet = parse_amount(parts[1])
        mines = int(parts[2])
    except ValueError:
        return False

    if not (1 <= mines <= 24):
        await update.message.reply_text("❌ Миналар саны 1-24 аралығында болуы керек.")
        return True

    uid = update.effective_user.id
    ensure_user(update.effective_user)

    if uid in games:
        await update.message.reply_text("❌ Алдымен қазіргі ойынды аяқта.")
        return True

    if not atomic_debit(uid, bet):
        await update.message.reply_text(
            f"❌ Қаражатың жеткіліксіз.\n"
            f"Қолжетімді: {fmt(get_balance(uid))} ₸\n"
            f"Қажет: {fmt(bet)} ₸"
        )
        return True

    mine_positions = set(secrets.SystemRandom().sample(range(25), mines))
    games[uid] = {
        "bet": bet,
        "mines": mine_positions,
        "opened": set(),
        "safe": 0
    }
    await send_mines_board(update, uid)
    return True


async def send_mines_board(update_or_query, uid):
    game = games[uid]
    buttons = []

    for i in range(25):
        label = "💎" if i in game["opened"] else "⬜"
        buttons.append(
            InlineKeyboardButton(label, callback_data=f"mine:{uid}:{i}")
        )

    rows = [buttons[i:i+5] for i in range(0, 25, 5)]
    rows.append([
        InlineKeyboardButton(
            "💰 Ұтысты алу", callback_data=f"cash:{uid}"
        )
    ])

    text = (
        "💣 МИНАЛАР 5×5\n\n"
        f"Ставка: {fmt(game['bet'])} ₸\n"
        f"Қауіпсіз ұяшық: {game['safe']}\n"
        f"Мина саны: {len(game['mines'])}\n"
        f"Коэффициент: {mines_multiplier(game['safe'], len(game['mines'])):.2f}x"
    )

    markup = InlineKeyboardMarkup(rows)

    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=markup)
    else:
        await update_or_query.message.reply_text(text, reply_markup=markup)


async def mines_callback(update, context):
    q = update.callback_query
    await q.answer()

    _, uid_s, idx_s = q.data.split(":")
    uid, idx = int(uid_s), int(idx_s)

    if q.from_user.id != uid or uid not in games:
        await q.answer("❌ Бұл сенің ойының емес.", show_alert=True)
        return

    game = games[uid]

    if idx in game["opened"]:
        return

    if idx in game["mines"]:
        del games[uid]
        await q.edit_message_text(
            "💥 МИНА!\n\n"
            f"❌ Ставка жоғалды: {fmt(game['bet'])} ₸"
        )
        return

    game["opened"].add(idx)
    game["safe"] += 1

    safe_total = 25 - len(game["mines"])
    if game["safe"] >= safe_total:
        mult = mines_multiplier(game["safe"], len(game["mines"]))
        payout = int(game["bet"] * mult)
        credit(uid, payout)
        del games[uid]
        await q.edit_message_text(
            f"🏆 Барлық қауіпсіз ұяшық ашылды!\n\n"
            f"Коэффициент: {mult:.2f}x\n"
            f"Ұтыс: +{fmt(payout)} ₸"
        )
        return

    await send_mines_board(q, uid)


async def mines_cash_callback(update, context):
    q = update.callback_query
    await q.answer()

    _, uid_s = q.data.split(":")
    uid = int(uid_s)

    if q.from_user.id != uid or uid not in games:
        await q.answer("❌ Бұл сенің ойының емес.", show_alert=True)
        return

    game = games[uid]

    if game["safe"] <= 0:
        await q.answer("Алдымен бір қауіпсіз ұяшық аш.", show_alert=True)
        return

    mult = mines_multiplier(game["safe"], len(game["mines"]))
    payout = int(game["bet"] * mult)
    credit(uid, payout)
    del games[uid]

    await q.edit_message_text(
        f"💰 Ұтыс алынды!\n\n"
        f"Коэффициент: {mult:.2f}x\n"
        f"Төлем: +{fmt(payout)} ₸"
    )


async def roulette(update, amount_text, bet_text):
    ensure_user(update.effective_user)
    uid = update.effective_user.id

    try:
        amount = parse_amount(amount_text)
    except ValueError:
        await update.message.reply_text("❌ Сома қате.")
        return

    bet = parse_bet(bet_text)
    if not bet:
        await update.message.reply_text(
            "❌ Ставка түсініксіз.\n"
            "Мысал: 2000 ақ, 2000 16, 2000 16-30."
        )
        return

    if bet[0] == "number" and bet[1] == 0 and amount > 1_000:
        await update.message.reply_text(
            "❌ 0 санына ең көбі 1 000 ₸ тігуге болады."
        )
        return

    if not atomic_debit(uid, amount):
        await update.message.reply_text(
            f"❌ Қаражатың жеткіліксіз.\n"
            f"Қолжетімді: {fmt(get_balance(uid))} ₸\n"
            f"Қажет: {fmt(amount)} ₸"
        )
        return

    # Нәтиже тек ставканы сәтті шегергеннен кейін жасалады.
    # Әр ставкаға бір тәуелсіз 0..36 random нәтиже.
    result = secrets.randbelow(37)
    kind, value = bet
    won = bet_wins(kind, value, result)
    mult = multiplier(kind, value)

    if won:
        payout = int(amount * mult)
        credit(uid, payout)
        await update.message.reply_text(
            f"🎰 {result_name(result)}\n\n"
            f"🎉 Ұттың!\n"
            f"Ставка: {fmt(amount)} ₸\n"
            f"Таңдау: {bet_name(kind, value)}\n"
            f"Коэффициент: {mult:.2f}x\n"
            f"Төлем: +{fmt(payout)} ₸"
        )
    else:
        await update.message.reply_text(
            f"🎰 {result_name(result)}\n\n"
            f"❌ Ұтылдың.\n"
            f"Ставка: {fmt(amount)} ₸"
        )


async def text_router(update, context):
    text = update.message.text.strip()
    low = text.lower()

    if low in {"баланс", "б", "бал", "ақша", "қаражат"}:
        await balance_cmd(update, context)
        return

    if low in {"бонус", "bonus", "сыйлық", "сый", "күндік", "дейлик", "дб"}:
        await bonus_cmd(update, context)
        return

    if low in {"көмек", "көмектес"}:
        await help_cmd(update, context)
        return

    # Мины
    if low.startswith("мины ") or low.startswith("миналар "):
        if await mines_start(update, context):
            return

    # Аударым: жауаппен "бер 5к", немесе "бер 5к @user", немесе ID.
    m = re.match(
        r"^(бер|аудар|жібер|берем)\s+(\S+)(?:\s+(.+))?$",
        low
    )
    if m:
        target = None

        if update.message.reply_to_message:
            target = update.message.reply_to_message.from_user
        elif m.group(3):
            raw = m.group(3).strip()
            if raw.startswith("@"):
                username = raw[1:].lower()
                with DB_LOCK:
                    row = db.execute(
                        "SELECT * FROM users WHERE lower(username)=?",
                        (username,)
                    ).fetchone()
                if row:
                    class UserRef: pass
                    target = UserRef()
                    target.id = row["user_id"]
                    target.username = row["username"]
                    target.first_name = row["first_name"] or username
            elif raw.isdigit():
                uid = int(raw)
                with DB_LOCK:
                    row = db.execute(
                        "SELECT * FROM users WHERE user_id=?", (uid,)
                    ).fetchone()
                if row:
                    class UserRef: pass
                    target = UserRef()
                    target.id = uid
                    target.username = row["username"]
                    target.first_name = row["first_name"] or str(uid)

        await transfer(update, m.group(2), target)
        return

    # Рулетка: "2000 ақ", "2000 16", "2000 16-30".
    # "рулетка 2000 ақ" та түсініледі.
    m = re.match(r"^(?:рулетка\s+)?(\S+)\s+(.+)$", low)
    if m:
        amount_token = m.group(1)
        bet_token = m.group(2).strip()

        try:
            parse_amount(amount_token)
        except ValueError:
            return

        if parse_bet(bet_token):
            await roulette(update, amount_token, bet_token)


async def help_cmd(update, context):
    await update.message.reply_text(
        "🇰🇿 КӨМЕК\n\n"
        "💰 Баланс: баланс / б / бал\n"
        "🎁 Бонус: бонус / bonus (әр 4 сағат сайын)\n\n"
        "💸 Аударым:\n"
        "• хабарламаға жауап: бер 5к\n"
        "• бер 5к @username\n"
        "• бер 5к 123456789\n\n"
        "🎰 Рулетка:\n"
        "• 2000 ақ / 2000 а\n"
        "• 2000 қара / 2000 қ\n"
        "• 2000 16\n"
        "• 2000 16-30\n"
        "• 2000 жұп / 2000 тақ\n\n"
        "💣 Мины: мины 1000 5\n"
        "🎟️ Промокод: /promo КОД\n\n"
        "🔢 Сома: 3к = 3000, 30к = 30000, "
        "3кк = 3000000, 2.5кк = 2500000."
    )


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN орнатылмаған")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("bonus", bonus_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("createp", create_promo))
    app.add_handler(CommandHandler("promo", use_promo))

    app.add_handler(CallbackQueryHandler(
        mines_callback, pattern=r"^mine:"
    ))
    app.add_handler(CallbackQueryHandler(
        mines_cash_callback, pattern=r"^cash:"
    ))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, text_router
    ))

    print("Бот іске қосылды.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
