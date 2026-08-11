import os
import io
import json
import hashlib
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest

from google import genai
from google.genai import types

from PIL import Image, ImageDraw, ImageFont, ImageOps


# =========================================================
# CONFIG
# =========================================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

PORT = int(os.getenv("PORT", "10000"))

MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3-flash-preview"
)

DATA_FILE = "ratings.json"


# =========================================================
# ПРОВЕРКА
# =========================================================

if not API_ID:
    raise RuntimeError("Не указан API_ID")

if not API_HASH:
    raise RuntimeError("Не указан API_HASH")

if not BOT_TOKEN:
    raise RuntimeError("Не указан BOT_TOKEN")

if not GEMINI_API_KEY:
    raise RuntimeError("Не указан GEMINI_API_KEY")


# =========================================================
# RENDER HEALTH CHECK
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def health_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print(f"Health server: {PORT}")

    server.serve_forever()


threading.Thread(
    target=health_server,
    daemon=True
).start()


# =========================================================
# AI
# =========================================================

ai = genai.Client(
    api_key=GEMINI_API_KEY
)


# =========================================================
# TELEGRAM
# =========================================================

client = TelegramClient(
    "mog_bot_session",
    API_ID,
    API_HASH
)


# =========================================================
# DATABASE
# =========================================================

def load_db():

    if not os.path.exists(DATA_FILE):
        return {}

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as e:

        print("DB load error:", e)

        return {}


db = load_db()


def save_db():

    try:

        temp = DATA_FILE + ".tmp"

        with open(
            temp,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                db,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temp,
            DATA_FILE
        )

    except Exception as e:

        print("DB save error:", e)


# =========================================================
# RANK
# =========================================================

def get_rank(score):

    if score < 3:
        return "Sub-3"

    if score < 5:
        return "Sub-5"

    if score < 6:
        return "LTN"

    if score < 7:
        return "MTN"

    if score < 8:
        return "HTN"

    if score < 9:
        return "Chad"

    return "True Adam"


# =========================================================
# HELPERS
# =========================================================

def clean_username(username):

    if not username:
        return "unknown"

    return username.replace(
        "@",
        ""
    ).strip()


def profile_hash(user):

    data = {
        "username": user["username"],
        "name": user["name"],
        "bio": user["bio"],
        "avatar": bool(
            user["photo"]
        )
    }

    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def safe_score(value):

    try:

        value = float(value)

        return max(
            0,
            min(
                10,
                value
            )
        )

    except:

        return 0


def clean_json(text):

    text = text.strip()

    if "```" in text:

        text = (
            text
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        raise ValueError(
            "AI не вернул JSON"
        )

    return json.loads(
        text[start:end + 1]
    )


# =========================================================
# FONTS
# =========================================================

def font(size):

    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]

    for path in paths:

        if os.path.exists(path):

            try:
                return ImageFont.truetype(
                    path,
                    size
                )

            except:
                pass

    return ImageFont.load_default()


# =========================================================
# AVATAR
# =========================================================

def make_avatar(
    path,
    size=150
):

    if path and os.path.exists(path):

        try:

            img = Image.open(
                path
            ).convert("RGBA")

            img = ImageOps.fit(
                img,
                (size, size),
                Image.Resampling.LANCZOS
            )

            mask = Image.new(
                "L",
                (size, size),
                0
            )

            ImageDraw.Draw(
                mask
            ).ellipse(
                (0, 0, size, size),
                fill=255
            )

            result = Image.new(
                "RGBA",
                (size, size),
                (0, 0, 0, 0)
            )

            result.paste(
                img,
                (0, 0),
                mask
            )

            return result

        except:
            pass

    result = Image.new(
        "RGBA",
        (size, size),
        (70, 70, 75, 255)
    )

    draw = ImageDraw.Draw(
        result
    )

    draw.ellipse(
        (0, 0, size, size),
        fill=(100, 100, 105, 255)
    )

    return result


# =========================================================
# MOGGED
# =========================================================

def add_mogged(avatar):

    stamp = Image.new(
        "RGBA",
        (250, 70),
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(
        stamp
    )

    draw.text(
        (125, 35),
        "MOGGED",
        font=font(32),
        fill=(235, 20, 40),
        stroke_width=3,
        stroke_fill=(0, 0, 0),
        anchor="mm"
    )

    # Наклон 12 градусов
    stamp = stamp.rotate(
        12,
        expand=True,
        resample=Image.Resampling.BICUBIC
    )

    x = (
        avatar.width -
        stamp.width
    ) // 2

    y = (
        avatar.height -
        stamp.height
    ) // 2

    avatar.alpha_composite(
        stamp,
        (x, y)
    )

    return avatar


# =========================================================
# SCORE BAR
# =========================================================

def score_bar(
    draw,
    x,
    y,
    width,
    score
):

    draw.rounded_rectangle(
        (
            x,
            y,
            x + width,
            y + 10
        ),
        radius=5,
        fill=(55, 55, 60)
    )

    filled = int(
        width * score / 10
    )

    if filled > 0:

        draw.rounded_rectangle(
            (
                x,
                y,
                x + filled,
                y + 10
            ),
            radius=5,
            fill=(255, 204, 0)
        )


# =========================================================
# CARD
# =========================================================

def make_card(
    u1,
    u2,
    result
):

    W = 760
    H = 1050

    card = Image.new(
        "RGB",
        (W, H),
        (15, 15, 18)
    )

    draw = ImageDraw.Draw(
        card
    )

    draw.text(
        (W // 2, 40),
        "MOG BATTLE",
        font=font(32),
        fill=(255, 204, 0),
        anchor="mm"
    )

    draw.text(
        (W // 2, 78),
        "НИК • АВАТАР • BIO",
        font=font(17),
        fill=(170, 170, 175),
        anchor="mm"
    )

    winner = result["winner"]

    def player(
        y,
        user,
        data,
        loser
    ):

        draw.rounded_rectangle(
            (
                30,
                y,
                W - 30,
                y + 400
            ),
            radius=20,
            fill=(28, 28, 33),
            outline=(55, 55, 62),
            width=2
        )

        avatar = make_avatar(
            user["photo"],
            145
        )

        if loser:
            avatar = add_mogged(
                avatar
            )

        card.paste(
            avatar,
            (
                W // 2 - 72,
                y + 20
            ),
            avatar
        )

        username = (
            user["username"]
            or "без username"
        )

        draw.text(
            (W // 2, y + 185),
            "@" + username,
            font=font(23),
            fill=(245, 245, 245),
            anchor="mm"
        )

        draw.text(
            (W // 2, y + 218),
            f'{data["rank"]} • {data["overall"]:.1f}/10',
            font=font(20),
            fill=(255, 204, 0),
            anchor="mm"
        )

        rows = [
            ("НИК", data["nick_score"]),
            ("АВАТАР", data["avatar_score"]),
            ("BIO", data["bio_score"])
        ]

        yy = y + 255

        for label, score in rows:

            draw.text(
                (55, yy),
                label,
                font=font(14),
                fill=(170, 170, 175)
            )

            draw.text(
                (W - 55, yy),
                f"{score:.1f}",
                font=font(14),
                fill=(245, 245, 245),
                anchor="ra"
            )

            score_bar(
                draw,
                55,
                yy + 25,
                W - 110,
                score
            )

            yy += 47

    player(
        105,
        u1,
        result["u1"],
        winner == 2
    )

    draw.text(
        (W // 2, 525),
        "VS",
        font=font(30),
        fill=(255, 255, 255),
        anchor="mm"
    )

    player(
        570,
        u2,
        result["u2"],
        winner == 1
    )

    draw.text(
        (W // 2, 1000),
        f'РАЗРЫВ: {result["gap"]:.1f}',
        font=font(20),
        fill=(255, 204, 0),
        anchor="mm"
    )

    path = "mog_card.png"

    card.save(
        path,
        quality=95
    )

    return path


# =========================================================
# GET TELEGRAM USER
# =========================================================

async def get_user(identifier):

    try:

        entity = await client.get_entity(
            identifier
        )

        full = await client(
            GetFullUserRequest(entity)
        )

        photo = None

        try:

            photo = await client.download_profile_photo(
                entity,
                file=f"avatar_{entity.id}"
            )

        except:
            pass

        return {
            "id": int(entity.id),

            "username": clean_username(
                entity.username
            ),

            "name": (
                entity.first_name or ""
            ),

            "bio": (
                full.full_user.about
                or ""
            ),

            "photo": photo
        }

    except Exception as e:

        print(
            "Telegram user error:",
            e
        )

        return None


# =========================================================
# AI
# =========================================================

SYSTEM_PROMPT = """
Ты строгий и максимально нейтральный судья Telegram-профилей.

ОЦЕНИВАЙ ТОЛЬКО:

1. Ник / username — 30%
2. Аватар — 40%
3. Bio — 30%

НЕ ОЦЕНИВАЙ:
- Telegram ID
- дату регистрации
- количество подписчиков
- сторис
- активность
- личность человека
- возраст
- пол
- национальность
- внешность человека как характеристику личности.

Оценивай именно качество профиля.

Система рангов:

Sub-3 = 0.0-2.9
Sub-5 = 3.0-4.9
LTN = 5.0-5.9
MTN = 6.0-6.9
HTN = 7.0-7.9
Chad = 8.0-8.9
True Adam = 9.0-10.0

Будь строгим.
Не завышай оценки.
Не унижай пользователя.
Не используй оскорбления.

Для каждой характеристики обязательно объясни,
почему выставлена именно такая оценка.

Верни ТОЛЬКО JSON:

{
  "winner": 1,
  "gap": 1.2,
  "explanation": "Краткое нейтральное объяснение победы",

  "u1": {
    "username": "username",
    "nick_score": 7.0,
    "nick_text": "Объяснение",
    "avatar_score": 8.0,
    "avatar_text": "Объяснение",
    "bio_score": 6.0,
    "bio_text": "Объяснение",
    "overall": 7.1,
    "rank": "HTN"
  },

  "u2": {
    "username": "username",
    "nick_score": 6.0,
    "nick_text": "Объяснение",
    "avatar_score": 5.0,
    "avatar_text": "Объяснение",
    "bio_score": 6.0,
    "bio_text": "Объяснение",
    "overall": 5.7,
    "rank": "LTN"
  }
}
"""


async def ai_compare(
    u1,
    u2
):

    prompt = f"""
{SYSTEM_PROMPT}

ПРОФИЛЬ 1

Username:
@{u1["username"]}

Имя:
{u1["name"]}

Bio:
{u1["bio"]}


ПРОФИЛЬ 2

Username:
@{u2["username"]}

Имя:
{u2["name"]}

Bio:
{u2["bio"]}
"""

    contents = [
        prompt
    ]

    for user in (
        u1,
        u2
    ):

        if not user["photo"]:
            continue

        try:

            buffer = io.BytesIO()

            Image.open(
                user["photo"]
            ).convert(
                "RGB"
            ).save(
                buffer,
                "JPEG",
                quality=90
            )

            contents.append(
                types.Part.from_bytes(
                    data=buffer.getvalue(),
                    mime_type="image/jpeg"
                )
            )

        except Exception as e:

            print(
                "Image error:",
                e
            )

    response = await asyncio.to_thread(
        ai.models.generate_content,
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.15,
            response_mime_type="application/json"
        )
    )

    result = clean_json(
        response.text
    )

    # Принудительно пересчитываем итог,
    # чтобы AI не мог ошибиться в математике.
    for key in (
        "u1",
        "u2"
    ):

        p = result[key]

        p["nick_score"] = safe_score(
            p.get("nick_score")
        )

        p["avatar_score"] = safe_score(
            p.get("avatar_score")
        )

        p["bio_score"] = safe_score(
            p.get("bio_score")
        )

        p["overall"] = round(
            p["nick_score"] * 0.30
            + p["avatar_score"] * 0.40
            + p["bio_score"] * 0.30,
            2
        )

        p["rank"] = get_rank(
            p["overall"]
        )

    if (
        result["u1"]["overall"]
        >=
        result["u2"]["overall"]
    ):

        result["winner"] = 1

    else:

        result["winner"] = 2

    result["gap"] = round(
        abs(
            result["u1"]["overall"]
            -
            result["u2"]["overall"]
        ),
        2
    )

    return result


# =========================================================
# SAVE RATING
# =========================================================

def save_rating(
    user,
    result,
    chat_id
):

    uid = str(
        user["id"]
    )

    old = db.get(
        uid,
        {}
    )

    chats = old.get(
        "chats",
        []
    )

    if chat_id is not None:

        if str(chat_id) not in chats:

            chats.append(
                str(chat_id)
            )

    db[uid] = {
        "id": user["id"],
        "username": user["username"],
        "name": user["name"],
        "bio": user["bio"],

        "hash": profile_hash(
            user
        ),

        "overall": result["overall"],
        "rank": result["rank"],

        "nick_score": result["nick_score"],
        "avatar_score": result["avatar_score"],
        "bio_score": result["bio_score"],

        "chats": chats
    }

    save_db()


# =========================================================
# BREAKDOWN
# =========================================================

def breakdown(
    number,
    user,
    data
):

    return (
        f"👤 Игрок {number}: "
        f"@{user['username'] or 'без username'}\n"
        f"🏆 {data['rank']} — "
        f"{data['overall']:.1f}/10\n\n"

        f"🔹 НИК — {data['nick_score']:.1f}/10\n"
        f"{data['nick_text']}\n\n"

        f"🔹 АВАТАР — {data['avatar_score']:.1f}/10\n"
        f"{data['avatar_text']}\n\n"

        f"🔹 BIO — {data['bio_score']:.1f}/10\n"
        f"{data['bio_text']}"
    )


# =========================================================
# .мог
# =========================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.мог(?:\s+(\S+))?(?:\s+(\S+))?\s*$"
    )
)
async def mog(event):

    first = event.pattern_match.group(1)
    second = event.pattern_match.group(2)

    if not first:

        await event.reply(
            "Использование:\n\n"
            "`.мог @user`\n"
            "`.мог @user1 @user2`"
        )

        return

    status = await event.reply(
        "⚖️ Судья анализирует ник, аватар и Bio..."
    )

    sender = await event.get_sender()

    if not second:

        if sender.username:

            second = (
                "@"
                + sender.username
            )

        else:

            second = sender.id

    u1 = await get_user(
        first
    )

    u2 = await get_user(
        second
    )

    if not u1 or not u2:

        await status.edit(
            "❌ Не удалось получить один из профилей."
        )

        return

    try:

        result = await ai_compare(
            u1,
            u2
        )

        save_rating(
            u1,
            result["u1"],
            event.chat_id
        )

        save_rating(
            u2,
            result["u2"],
            event.chat_id
        )

        card = make_card(
            u1,
            u2,
            result
        )

        if result["winner"] == 1:

            winner = u1
            winner_data = result["u1"]

        else:

            winner = u2
            winner_data = result["u2"]

        winner_name = (
            winner["username"]
            or winner["name"]
            or "unknown"
        )

        caption = (
            f"🏆 ПОБЕДИТЕЛЬ: @{winner_name}\n"
            f"📊 {winner_data['rank']} — "
            f"{winner_data['overall']:.1f}/10\n"
            f"⚖️ Разрыв: {result['gap']:.1f}\n\n"
            f"{result['explanation']}"
        )

        await client.send_file(
            event.chat_id,
            card,
            caption=caption
        )

        text = (
            breakdown(
                1,
                u1,
                result["u1"]
            )
            +
            "\n\n"
            +
            breakdown(
                2,
                u2,
                result["u2"]
            )
            +
            "\n\n"
            +
            "🏆 ПОЧЕМУ ПОБЕДИЛ\n"
            +
            result["explanation"]
        )

        await event.reply(
            text
        )

        await status.delete()

    except Exception as e:

        print(
            "MOG ERROR:",
            repr(e)
        )

        await status.edit(
            "❌ Ошибка AI. Проверь GEMINI_API_KEY и попробуй ещё раз."
        )


# =========================================================
# .хелп
# =========================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.хелп(?:\s+(\S+))?\s*$"
    )
)
async def help_command(event):

    target = event.pattern_match.group(1)

    if target:

        user = await get_user(
            target
        )

    else:

        sender = await event.get_sender()

        if sender.username:

            user = await get_user(
                "@"
                + sender.username
            )

        else:

            user = await get_user(
                sender.id
            )

    if not user:

        await event.reply(
            "❌ Не удалось получить профиль."
        )

        return

    msg = await event.reply(
        "🧠 Анализирую профиль..."
    )

    prompt = f"""
Ты консультант по Telegram-профилям.

Оцени ТОЛЬКО:
- username / ник
- аватар
- Bio

Не обсуждай возраст, пол, национальность,
внешность человека или личные качества.

Username: @{user["username"]}
Имя: {user["name"]}
Bio: {user["bio"]}

Дай конкретный разбор:

1. Что уже хорошо.
2. Главные проблемы.
3. Как улучшить ник.
4. Как улучшить аватар.
5. Как улучшить Bio.
6. Три конкретных действия для улучшения профиля.

Стиль: серьёзный, нейтральный, конструктивный.
"""

    contents = [
        prompt
    ]

    if user["photo"]:

        try:

            buffer = io.BytesIO()

            Image.open(
                user["photo"]
            ).convert(
                "RGB"
            ).save(
                buffer,
                "JPEG",
                quality=90
            )

            contents.append(
                types.Part.from_bytes(
                    data=buffer.getvalue(),
                    mime_type="image/jpeg"
                )
            )

        except:
            pass

    try:

        response = await asyncio.to_thread(
            ai.models.generate_content,
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.25
            )
        )

        await msg.edit(
            "🧠 РАЗБОР ПРОФИЛЯ\n\n"
            +
            response.text
        )

    except Exception as e:

        print(
            "HELP ERROR:",
            repr(e)
        )

        await msg.edit(
            "❌ Не удалось получить совет."
        )


# =========================================================
# TOP
# =========================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.топ(?:\s+(все|чат|чата))?\s*$"
    )
)
async def top(event):

    mode = event.pattern_match.group(1)

    users = []

    for user in db.values():

        if mode != "все":

            if str(event.chat_id) not in [
                str(x)
                for x in user.get(
                    "chats",
                    []
                )
            ]:

                continue

        users.append(
            user
        )

    users.sort(
        key=lambda x: float(
            x.get(
                "overall",
                0
            )
        ),
        reverse=True
    )

    users = users[:10]

    if not users:

        await event.reply(
            "📭 Рейтингов пока нет."
        )

        return

    if mode == "все":

        title = "🌍 ТОП ВСЕХ"

    else:

        title = "🏠 ТОП ЧАТА"

    lines = [
        title,
        ""
    ]

    for i, user in enumerate(
        users,
        1
    ):

        name = (
            user.get("username")
            or user.get("name")
            or "unknown"
        )

        lines.append(
            f"{i}. @{name} — "
            f"{float(user.get('overall', 0)):.1f}/10 "
            f"({user.get('rank', 'Sub-3')})"
        )

    await event.reply(
        "\n".join(lines)
    )


# =========================================================
# .ранги
# =========================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.ранги$"
    )
)
async def ranks(event):

    await event.reply(
        "🏆 РАНГИ\n\n"
        "Sub-3 → 0.0–2.9\n"
        "Sub-5 → 3.0–4.9\n"
        "LTN → 5.0–5.9\n"
        "MTN → 6.0–6.9\n"
        "HTN → 7.0–7.9\n"
        "Chad → 8.0–8.9\n"
        "True Adam → 9.0–10.0"
    )


# =========================================================
# .команды
# =========================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.команды$"
    )
)
async def commands(event):

    await event.reply(
        "📚 КОМАНДЫ\n\n"
        "`.мог @user` — сравнение с тобой\n"
        "`.мог @user1 @user2` — сравнение двух\n\n"
        "`.хелп` — совет по своему профилю\n"
        "`.хелп @user` — совет по профилю\n\n"
        "`.топ` — топ текущего чата\n"
        "`.топ все` — общий топ\n\n"
        "`.ранги` — система рангов\n"
        "`.команды` — список команд"
    )


# =========================================================
# START
# =========================================================

async def main():

    print("Starting bot...")

    await client.start(
        bot_token=BOT_TOKEN
    )

    me = await client.get_me()

    print(
        "Logged in:",
        "@"
        + (
            me.username
            or str(me.id)
        )
    )

    print(
        "BOT READY"
    )

    await client.run_until_disconnected()


if __name__ == "__main__":

    asyncio.run(
        main()
        )
