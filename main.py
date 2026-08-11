import os
import io
import json
import time
import asyncio
import hashlib
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telethon import TelegramClient, events
from google import genai
from google.genai import types

from PIL import Image, ImageDraw, ImageFont, ImageOps


# ============================================================
# CONFIG
# ============================================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

PORT = int(os.getenv("PORT", "10000"))

# Быстрая Flash-модель.
# Можно переопределить через Render Environment Variables.
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3-flash-preview"
)

DB_FILE = "ratings.json"

# Размер картинки, которую отправляем Gemini.
# 256 обычно более чем достаточно для оценки аватара.
AI_IMAGE_SIZE = 256

# Не пересчитываем абсолютно одинаковый профиль.
CACHE_TTL = 60 * 60 * 24 * 30


# ============================================================
# VALIDATION
# ============================================================

if not API_ID:
    raise RuntimeError("API_ID не указан")

if not API_HASH:
    raise RuntimeError("API_HASH не указан")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не указан")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY не указан")


# ============================================================
# RENDER HEALTH CHECK
# ============================================================

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


def run_health_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print(f"[WEB] Health server on {PORT}")

    server.serve_forever()


threading.Thread(
    target=run_health_server,
    daemon=True
).start()


# ============================================================
# GEMINI
# ============================================================

ai = genai.Client(
    api_key=GEMINI_API_KEY
)


# ============================================================
# TELEGRAM
# ============================================================

client = TelegramClient(
    "mog_bot_session",
    API_ID,
    API_HASH
)


# ============================================================
# DATABASE
# ============================================================

def load_db():

    if not os.path.exists(DB_FILE):
        return {}

    try:

        with open(
            DB_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as e:

        print("[DB] load error:", e)

        return {}


db = load_db()


def save_db():

    try:

        tmp = DB_FILE + ".tmp"

        with open(
            tmp,
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
            tmp,
            DB_FILE
        )

    except Exception as e:

        print("[DB] save error:", e)


# ============================================================
# LOCK
# ============================================================

db_lock = asyncio.Lock()


# ============================================================
# RANKS
# ============================================================

def get_rank(score):

    score = float(score)

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


# ============================================================
# HELPERS
# ============================================================

def username_clean(value):

    if not value:
        return ""

    return str(value).replace(
        "@",
        ""
    ).strip()


def clamp_score(value):

    try:

        value = float(value)

    except:

        value = 0

    return round(
        max(
            0,
            min(
                10,
                value
            )
        ),
        2
    )


def profile_hash(user):

    """
    Хэш только тех данных, которые реально оцениваются.
    """

    data = {
        "username": user.get("username", ""),
        "name": user.get("name", ""),
        "bio": user.get("bio", ""),
        "avatar_hash": user.get(
            "avatar_hash",
            ""
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


def pair_hash(u1, u2):

    h1 = profile_hash(u1)
    h2 = profile_hash(u2)

    # Порядок сохраняем.
    raw = h1 + ":" + h2

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


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
            "Gemini не вернул JSON"
        )

    return json.loads(
        text[start:end + 1]
    )


# ============================================================
# FONTS
# ============================================================

def get_font(size):

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


# ============================================================
# FAST AVATAR PROCESSING
# ============================================================

def prepare_avatar(path):

    """
    Быстро превращает аватар в маленький JPEG.
    Это сильно уменьшает время/объём запроса к Gemini.
    """

    if not path:
        return None

    if not os.path.exists(path):
        return None

    try:

        img = Image.open(
            path
        ).convert("RGB")

        img.thumbnail(
            (
                AI_IMAGE_SIZE,
                AI_IMAGE_SIZE
            ),
            Image.Resampling.LANCZOS
        )

        buffer = io.BytesIO()

        img.save(
            buffer,
            format="JPEG",
            quality=75,
            optimize=True
        )

        return buffer.getvalue()

    except Exception as e:

        print(
            "[IMAGE] error:",
            e
        )

        return None


def avatar_hash(path):

    if not path:
        return ""

    try:

        # Хэшируем сам файл.
        # Для маленьких Telegram-аватаров это быстро.
        with open(
            path,
            "rb"
        ) as f:

            return hashlib.md5(
                f.read()
            ).hexdigest()

    except:

        return ""


# ============================================================
# AVATAR FOR CARD
# ============================================================

def make_avatar(
    path,
    size=145
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

    ImageDraw.Draw(
        result
    ).ellipse(
        (0, 0, size, size),
        fill=(100, 100, 105, 255)
    )

    return result


# ============================================================
# MOGGED
# ============================================================

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
        font=get_font(31),
        fill=(235, 20, 40),
        stroke_width=3,
        stroke_fill=(0, 0, 0),
        anchor="mm"
    )

    # Ровно 12 градусов.
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


# ============================================================
# SCORE BAR
# ============================================================

def draw_bar(
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
            y + 9
        ),
        radius=5,
        fill=(55, 55, 60)
    )

    fill = int(
        width *
        clamp_score(score) /
        10
    )

    if fill > 0:

        draw.rounded_rectangle(
            (
                x,
                y,
                x + fill,
                y + 9
            ),
            radius=5,
            fill=(255, 204, 0)
        )


# ============================================================
# CARD
# ============================================================

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

    title_font = get_font(32)
    sub_font = get_font(16)
    username_font = get_font(22)
    rank_font = get_font(20)
    small_font = get_font(14)

    draw.text(
        (W // 2, 38),
        "MOG BATTLE",
        font=title_font,
        fill=(255, 204, 0),
        anchor="mm"
    )

    draw.text(
        (W // 2, 74),
        "НИК • АВАТАР • BIO",
        font=sub_font,
        fill=(165, 165, 170),
        anchor="mm"
    )

    winner = int(
        result["winner"]
    )

    def draw_player(
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
            user.get("photo"),
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
                y + 18
            ),
            avatar
        )

        name = (
            user.get("username")
            or user.get("name")
            or "unknown"
        )

        draw.text(
            (W // 2, y + 180),
            "@" + name,
            font=username_font,
            fill=(245, 245, 245),
            anchor="mm"
        )

        draw.text(
            (
                W // 2,
                y + 213
            ),
            f'{data["rank"]} • {data["overall"]:.1f}/10',
            font=rank_font,
            fill=(255, 204, 0),
            anchor="mm"
        )

        rows = [
            (
                "НИК",
                data["nick_score"]
            ),
            (
                "АВАТАР",
                data["avatar_score"]
            ),
            (
                "BIO",
                data["bio_score"]
            )
        ]

        yy = y + 252

        for label, score in rows:

            draw.text(
                (55, yy),
                label,
                font=small_font,
                fill=(170, 170, 175)
            )

            draw.text(
                (W - 55, yy),
                f"{score:.1f}",
                font=small_font,
                fill=(245, 245, 245),
                anchor="ra"
            )

            draw_bar(
                draw,
                55,
                yy + 22,
                W - 110,
                score
            )

            yy += 47

    draw_player(
        105,
        u1,
        result["u1"],
        winner == 2
    )

    draw.text(
        (W // 2, 525),
        "VS",
        font=get_font(30),
        fill=(255, 255, 255),
        anchor="mm"
    )

    draw_player(
        570,
        u2,
        result["u2"],
        winner == 1
    )

    draw.text(
        (W // 2, 1000),
        f'РАЗРЫВ: {result["gap"]:.1f}',
        font=get_font(20),
        fill=(255, 204, 0),
        anchor="mm"
    )

    path = "mog_card.png"

    card.save(
        path,
        "PNG",
        optimize=True
    )

    return path


# ============================================================
# TELEGRAM USER
# ============================================================

async def get_user(
    identifier,
    cached=None
):

    """
    Минимум Telegram-запросов.

    GetFullUserRequest не используется.
    Bio берём из обычного entity/full entity,
    если Telethon его уже получил.
    """

    try:

        entity = await client.get_entity(
            identifier
        )

        # entity обычно содержит bio только если
        # Telethon уже получил соответствующую информацию.
        # Если нет — один дополнительный запрос.
        bio = getattr(
            entity,
            "about",
            None
        )

        if bio is None:

            try:

                full = await client.get_entity(
                    entity
                )

                bio = getattr(
                    full,
                    "about",
                    ""
                ) or ""

            except:

                bio = ""

        username = username_clean(
            getattr(
                entity,
                "username",
                ""
            )
        )

        name = (
            getattr(
                entity,
                "first_name",
                ""
            )
            or ""
        )

        photo_path = None

        # Сначала смотрим локальный кэш.
        if cached:

            old_path = cached.get(
                "photo"
            )

            if (
                old_path
                and
                os.path.exists(old_path)
            ):

                photo_path = old_path

        # Если локального файла нет — скачиваем.
        if not photo_path:

            try:

                photo_path = (
                    await client.download_profile_photo(
                        entity,
                        file=f"avatar_{entity.id}"
                    )
                )

            except:

                photo_path = None

        result = {
            "id": int(entity.id),
            "username": username,
            "name": name,
            "bio": bio or "",
            "photo": photo_path,
            "avatar_hash": avatar_hash(
                photo_path
            )
        }

        return result

    except Exception as e:

        print(
            "[TG] user error:",
            e
        )

        return None


# ============================================================
# FAST GET TWO USERS
# ============================================================

async def get_two_users(
    first,
    second
):

    """
    Главное ускорение:
    два Telegram-запроса выполняются одновременно.
    """

    cached1 = None
    cached2 = None

    try:

        # Если это username — пытаемся найти старую запись.
        first_name = username_clean(
            first
        )

        second_name = username_clean(
            second
        )

        for value in db.values():

            if value.get("username") == first_name:
                cached1 = value

            if value.get("username") == second_name:
                cached2 = value

    except:
        pass

    return await asyncio.gather(
        get_user(
            first,
            cached1
        ),
        get_user(
            second,
            cached2
        )
    )


# ============================================================
# AI PROMPT
# ============================================================

SYSTEM_PROMPT = """
Ты строгий, серьёзный и нейтральный судья Telegram-профилей.

ОЦЕНИВАЙ ТОЛЬКО ТРИ ВЕЩИ:

1. Ник / username — 30%
2. Аватар — 40%
3. Bio — 30%

НИКОГДА НЕ ОЦЕНИВАЙ:
- Telegram ID
- дату регистрации
- количество подписчиков
- сторис
- активность
- возраст
- пол
- национальность
- личность человека
- социальный статус человека.

Оценивается исключительно качество Telegram-профиля.

Ранги:

Sub-3 = 0.0–2.9
Sub-5 = 3.0–4.9
LTN = 5.0–5.9
MTN = 6.0–6.9
HTN = 7.0–7.9
Chad = 8.0–8.9
True Adam = 9.0–10.0

Будь строгим и объективным.
Не завышай оценки.
Не унижай людей.

Для каждого игрока объясни:
- почему такая оценка ника;
- почему такая оценка аватара;
- почему такая оценка Bio.

Верни ТОЛЬКО JSON.

Формат:

{
  "winner": 1,
  "gap": 1.2,
  "explanation": "Краткое объяснение победы",

  "u1": {
    "username": "username",
    "nick_score": 7.0,
    "nick_text": "Причина",
    "avatar_score": 8.0,
    "avatar_text": "Причина",
    "bio_score": 6.0,
    "bio_text": "Причина",
    "overall": 7.1,
    "rank": "HTN"
  },

  "u2": {
    "username": "username",
    "nick_score": 6.0,
    "nick_text": "Причина",
    "avatar_score": 5.0,
    "avatar_text": "Причина",
    "bio_score": 6.0,
    "bio_text": "Причина",
    "overall": 5.7,
    "rank": "LTN"
  }
}
"""


# ============================================================
# BUILD AI CONTENT
# ============================================================

def build_ai_content(
    u1,
    u2=None
):

    if u2 is None:

        prompt = f"""
{SYSTEM_PROMPT}

ПРОФИЛЬ:

Username:
@{u1["username"]}

Имя:
{u1["name"]}

Bio:
{u1["bio"]}
"""

    else:

        prompt = f"""
{SYSTEM_PROMPT}

ПРОФИЛЬ 1:

Username:
@{u1["username"]}

Имя:
{u1["name"]}

Bio:
{u1["bio"]}


ПРОФИЛЬ 2:

Username:
@{u2["username"]}

Имя:
{u2["name"]}

Bio:
{u2["bio"]}
"""

    contents = [prompt]

    for user in (
        [u1]
        if u2 is None
        else [u1, u2]
    ):

        image_data = prepare_avatar(
            user.get("photo")
        )

        if image_data:

            contents.append(
                types.Part.from_bytes(
                    data=image_data,
                    mime_type="image/jpeg"
                )
            )

    return contents


# ============================================================
# RECALCULATE RESULT
# ============================================================

def normalize_result(
    result
):

    for key in (
        "u1",
        "u2"
    ):

        data = result[key]

        data["nick_score"] = clamp_score(
            data.get("nick_score")
        )

        data["avatar_score"] = clamp_score(
            data.get("avatar_score")
        )

        data["bio_score"] = clamp_score(
            data.get("bio_score")
        )

        data["overall"] = round(
            data["nick_score"] * 0.30
            +
            data["avatar_score"] * 0.40
            +
            data["bio_score"] * 0.30,
            2
        )

        data["rank"] = get_rank(
            data["overall"]
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


# ============================================================
# FAST GEMINI
# ============================================================

async def call_gemini(
    contents
):

    response = await asyncio.to_thread(
        ai.models.generate_content,
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            max_output_tokens=1200
        )
    )

    return clean_json(
        response.text
    )


# ============================================================
# CACHE SAVE
# ============================================================

async def save_user_rating(
    user,
    data,
    chat_id=None
):

    uid = str(
        user["id"]
    )

    async with db_lock:

        old = db.get(
            uid,
            {}
        )

        chats = old.get(
            "chats",
            []
        )

        if chat_id is not None:

            chat = str(
                chat_id
            )

            if chat not in chats:

                chats.append(
                    chat
                )

        db[uid] = {
            "id": user["id"],
            "username": user["username"],
            "name": user["name"],
            "bio": user["bio"],
            "photo": user["photo"],
            "avatar_hash": user["avatar_hash"],
            "hash": profile_hash(user),

            "overall": data["overall"],
            "rank": data["rank"],

            "nick_score": data["nick_score"],
            "avatar_score": data["avatar_score"],
            "bio_score": data["bio_score"],

            "nick_text": data.get(
                "nick_text",
                ""
            ),

            "avatar_text": data.get(
                "avatar_text",
                ""
            ),

            "bio_text": data.get(
                "bio_text",
                ""
            ),

            "chats": chats,

            "updated": int(
                time.time()
            )
        }

        save_db()


# ============================================================
# BREAKDOWN
# ============================================================

def make_breakdown(
    number,
    user,
    data
):

    username = (
        user["username"]
        or user["name"]
        or "unknown"
    )

    return (
        f"👤 ИГРОК {number}: @{username}\n"
        f"🏆 {data['rank']} — "
        f"{data['overall']:.1f}/10\n\n"

        f"🔹 НИК — {data['nick_score']:.1f}/10\n"
        f"{data.get('nick_text', '')}\n\n"

        f"🔹 АВАТАР — {data['avatar_score']:.1f}/10\n"
        f"{data.get('avatar_text', '')}\n\n"

        f"🔹 BIO — {data['bio_score']:.1f}/10\n"
        f"{data.get('bio_text', '')}"
    )


# ============================================================
# .МOG
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.мог(?:\s+(\S+))?(?:\s+(\S+))?\s*$"
    )
)
async def mog_handler(event):

    first = event.pattern_match.group(1)
    second = event.pattern_match.group(2)

    if not first:

        await event.reply(
            "Использование:\n"
            "`.мог @user`\n"
            "`.мог @user1 @user2`"
        )

        return

    status = await event.reply(
        "⚡ Быстрый анализ..."
    )

    try:

        sender = await event.get_sender()

        if not second:

            if sender.username:

                second = (
                    "@"
                    +
                    sender.username
                )

            else:

                second = sender.id

        # ====================================================
        # Telegram: оба пользователя одновременно.
        # ====================================================

        u1, u2 = await get_two_users(
            first,
            second
        )

        if not u1 or not u2:

            await status.edit(
                "❌ Не удалось получить профиль."
            )

            return

        # ====================================================
        # Проверяем кэш пары.
        # ====================================================

        p_hash = pair_hash(
            u1,
            u2
        )

        cached_pair = db.get(
            "_pairs",
            {}
        )

        cached = cached_pair.get(
            p_hash
        )

        if cached:

            age = (
                time.time()
                -
                cached.get(
                    "created",
                    0
                )
            )

            if age < CACHE_TTL:

                print(
                    "[CACHE] pair hit"
                )

                result = cached["result"]

            else:

                cached = None

        # ====================================================
        # Gemini только если нет кэша.
        # ====================================================

        if not cached:

            print(
                "[AI] analyzing pair..."
            )

            contents = build_ai_content(
                u1,
                u2
            )

            result = await call_gemini(
                contents
            )

            result = normalize_result(
                result
            )

            if "_pairs" not in db:

                db["_pairs"] = {}

            db["_pairs"][p_hash] = {
                "result": result,
                "created": int(
                    time.time()
                )
            }

            save_db()

        # ====================================================
        # Сохраняем рейтинги.
        # ====================================================

        await asyncio.gather(
            save_user_rating(
                u1,
                result["u1"],
                event.chat_id
            ),
            save_user_rating(
                u2,
                result["u2"],
                event.chat_id
            )
        )

        # ====================================================
        # Карточка.
        # ====================================================

        card = await asyncio.to_thread(
            make_card,
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

        # ====================================================
        # Полный разбор.
        # ====================================================

        text = (
            make_breakdown(
                1,
                u1,
                result["u1"]
            )
            +
            "\n\n"
            +
            make_breakdown(
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
            "[MOG ERROR]",
            repr(e)
        )

        try:

            await status.edit(
                "❌ Ошибка при анализе. Попробуй ещё раз."
            )

        except:
            pass


# ============================================================
# .ХЕЛП
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.хелп(?:\s+(\S+))?\s*$"
    )
)
async def help_handler(event):

    target = event.pattern_match.group(1)

    msg = await event.reply(
        "⚡ Анализирую профиль..."
    )

    try:

        if target:

            user = await get_user(
                target
            )

        else:

            sender = await event.get_sender()

            if sender.username:

                user = await get_user(
                    "@"
                    +
                    sender.username
                )

            else:

                user = await get_user(
                    sender.id
                )

        if not user:

            await msg.edit(
                "❌ Профиль не найден."
            )

            return

        # ====================================================
        # Ищем существующий профильный кэш.
        # ====================================================

        uid = str(
            user["id"]
        )

        cached = db.get(
            uid
        )

        current_hash = profile_hash(
            user
        )

        if cached:

            if (
                cached.get("hash")
                ==
                current_hash
            ):

                cached_help = cached.get(
                    "help"
                )

                if cached_help:

                    print(
                        "[CACHE] help hit"
                    )

                    await msg.edit(
                        "🧠 РАЗБОР ПРОФИЛЯ\n\n"
                        +
                        cached_help
                    )

                    return

        # ====================================================
        # AI.
        # ====================================================

        prompt = f"""
Ты серьёзный консультант по Telegram-профилям.

Оцени ТОЛЬКО:
- username / ник
- аватар
- Bio

Не обсуждай:
- возраст
- пол
- национальность
- внешность человека
- личные качества.

Username:
@{user["username"]}

Имя:
{user["name"]}

Bio:
{user["bio"]}

Дай короткий, конкретный и полезный совет:

1. Что уже хорошо.
2. Что плохо.
3. Как улучшить ник.
4. Как улучшить аватар.
5. Как улучшить Bio.
6. Три конкретных действия.

Стиль: серьёзный, нейтральный, конструктивный.
Без лишней воды.
"""

        contents = [
            prompt
        ]

        image_data = prepare_avatar(
            user.get("photo")
        )

        if image_data:

            contents.append(
                types.Part.from_bytes(
                    data=image_data,
                    mime_type="image/jpeg"
                )
            )

        answer = await call_help_ai(
            contents
        )

        # ====================================================
        # Сохраняем кэш.
        # ====================================================

        async with db_lock:

            if uid not in db:

                db[uid] = {}

            db[uid].update({
                "id": user["id"],
                "username": user["username"],
                "name": user["name"],
                "bio": user["bio"],
                "photo": user["photo"],
                "avatar_hash": user["avatar_hash"],
                "hash": current_hash,
                "help": answer,
                "help_updated": int(
                    time.time()
                )
            })

            save_db()

        await msg.edit(
            "🧠 РАЗБОР ПРОФИЛЯ\n\n"
            +
            answer
        )

    except Exception as e:

        print(
            "[HELP ERROR]",
            repr(e)
        )

        await msg.edit(
            "❌ Не удалось получить совет."
        )


# ============================================================
# HELP AI
# ============================================================

async def call_help_ai(
    contents
):

    response = await asyncio.to_thread(
        ai.models.generate_content,
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.15,
            max_output_tokens=700
        )
    )

    return response.text.strip()


# ============================================================
# .ТОП
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.топ(?:\s+(все|чат|чата))?\s*$"
    )
)
async def top_handler(event):

    mode = event.pattern_match.group(1)

    if mode == "все":

        title = "🌍 ТОП ВСЕХ"

    else:

        title = "🏠 ТОП ЧАТА"

    users = []

    for key, user in db.items():

        if key == "_pairs":
            continue

        if not isinstance(
            user,
            dict
        ):
            continue

        if mode != "все":

            chats = [
                str(x)
                for x in user.get(
                    "chats",
                    []
                )
            ]

            if str(
                event.chat_id
            ) not in chats:

                continue

        if "overall" not in user:
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
            f"{float(user['overall']):.1f}/10 "
            f"({user.get('rank', 'Sub-3')})"
        )

    await event.reply(
        "\n".join(lines)
    )


# ============================================================
# .РАНГИ
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.ранги$"
    )
)
async def ranks_handler(event):

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


# ============================================================
# .КОМАНДЫ
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.команды$"
    )
)
async def commands_handler(event):

    await event.reply(
        "📚 КОМАНДЫ\n\n"

        "`.мог @user`\n"
        "Сравнить пользователя с тобой.\n\n"

        "`.мог @user1 @user2`\n"
        "Сравнить двух пользователей.\n\n"

        "`.хелп`\n"
        "Получить советы по своему профилю.\n\n"

        "`.хелп @user`\n"
        "Получить советы для другого профиля.\n\n"

        "`.топ`\n"
        "Топ текущего чата.\n\n"

        "`.топ все`\n"
        "Общий топ.\n\n"

        "`.ранги`\n"
        "Система рангов."
    )


# ============================================================
# START
# ============================================================

async def main():

    print(
        "[BOT] Starting..."
    )

    await client.start(
        bot_token=BOT_TOKEN
    )

    me = await client.get_me()

    print(
        "[BOT] Logged in:",
        "@"
        +
        (
            me.username
            or str(me.id)
        )
    )

    print(
        "[BOT] MODEL:",
        GEMINI_MODEL
    )

    print(
        "[BOT] READY"
    )

    await client.run_until_disconnected()


if __name__ == "__main__":

    asyncio.run(
        main()
    )
