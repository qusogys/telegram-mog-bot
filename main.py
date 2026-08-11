import os
import io
import json
import sqlite3
import hashlib
import threading
import asyncio
import re
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest

from PIL import Image, ImageDraw, ImageFont, ImageOps

from google import genai
from google.genai import types


# ============================================================
# CONFIG
# ============================================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

PORT = int(os.getenv("PORT", "8080"))

# Можно поменять через Environment Variables на Render.
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash"
)

DB_PATH = os.getenv(
    "DB_PATH",
    "mogging.db"
)

SESSION_NAME = os.getenv(
    "SESSION_NAME",
    "mog_bot_session"
)


# ============================================================
# VALIDATION
# ============================================================

if not API_ID:
    print("WARNING: API_ID is not set")

if not API_HASH:
    print("WARNING: API_HASH is not set")

if not BOT_TOKEN:
    print("WARNING: BOT_TOKEN is not set")

if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY is not set")


# ============================================================
# HEALTH SERVER FOR RENDER
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):

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

    def log_message(self, format, *args):
        return


def run_health_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthCheckHandler
    )

    print(
        f"Health server started on port {PORT}"
    )

    server.serve_forever()


threading.Thread(
    target=run_health_server,
    daemon=True
).start()


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.Lock()


def db_connect():

    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


db = db_connect()


def init_db():

    with db_lock:

        db.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,

                username TEXT,
                first_name TEXT,
                bio TEXT,

                avatar_hash TEXT,
                profile_hash TEXT,

                has_stories INTEGER DEFAULT 0,

                overall REAL DEFAULT 0,

                avatar_score REAL DEFAULT 0,
                og_score REAL DEFAULT 0,
                bio_score REAL DEFAULT 0,
                activity_score REAL DEFAULT 0,

                avatar_text TEXT DEFAULT '',
                og_text TEXT DEFAULT '',
                bio_text TEXT DEFAULT '',
                activity_text TEXT DEFAULT '',

                strength TEXT DEFAULT '',
                weakness TEXT DEFAULT '',

                rank TEXT DEFAULT 'Sub-3',

                updated_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id INTEGER,
                user_id INTEGER,

                last_seen TEXT,

                PRIMARY KEY (
                    chat_id,
                    user_id
                )
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_profiles_overall
            ON profiles(overall DESC)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_chat_members_chat
            ON chat_members(chat_id)
        """)

        db.commit()


init_db()


# ============================================================
# GEMINI
# ============================================================

gemini_client = genai.Client(
    api_key=GEMINI_API_KEY
)


# ============================================================
# RANK SYSTEM
# ============================================================

def get_rank(score):

    try:
        score = float(score)
    except Exception:
        score = 0.0

    score = max(
        0.0,
        min(10.0, score)
    )

    if score < 3.0:
        return "Sub-3"

    if score < 5.0:
        return "Sub-5"

    if score < 6.0:
        return "LTN"

    if score < 7.0:
        return "MTN"

    if score < 8.0:
        return "HTN"

    if score < 9.0:
        return "Chad"

    return "True Adam"


def rank_emoji(rank):

    return {
        "Sub-3": "🔴",
        "Sub-5": "🟠",
        "LTN": "🟡",
        "MTN": "🔵",
        "HTN": "🟢",
        "Chad": "🔥",
        "True Adam": "👑"
    }.get(rank, "⚪")


# ============================================================
# TELEGRAM CLIENT
# ============================================================

client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH
).start(
    bot_token=BOT_TOKEN
)


# ============================================================
# FONTS
# ============================================================

def get_font(size, bold=True):

    if bold:

        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ]

    else:

        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        ]

    for path in paths:

        try:
            return ImageFont.truetype(
                path,
                size
            )
        except Exception:
            pass

    return ImageFont.load_default()


# ============================================================
# HASH
# ============================================================

def sha256_file(path):

    if not path or not os.path.exists(path):
        return ""

    try:

        h = hashlib.sha256()

        with open(path, "rb") as f:

            while True:

                chunk = f.read(1024 * 1024)

                if not chunk:
                    break

                h.update(chunk)

        return h.hexdigest()

    except Exception:

        return ""


def calculate_profile_hash(
    user,
    avatar_hash
):

    raw = "|".join([
        str(user["id"]),
        str(user.get("username") or ""),
        str(user.get("bio") or ""),
        str(user.get("has_stories")),
        str(avatar_hash)
    ])

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_saved_profile(user_id):

    with db_lock:

        row = db.execute(
            """
            SELECT *
            FROM profiles
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

    return row


def save_profile(
    user,
    profile_hash,
    avatar_hash,
    ai_data
):

    overall = float(
        ai_data["overall"]
    )

    rank = get_rank(
        overall
    )

    with db_lock:

        db.execute(
            """
            INSERT INTO profiles (
                user_id,
                username,
                first_name,
                bio,

                avatar_hash,
                profile_hash,

                has_stories,

                overall,

                avatar_score,
                og_score,
                bio_score,
                activity_score,

                avatar_text,
                og_text,
                bio_text,
                activity_text,

                strength,
                weakness,

                rank,
                updated_at
            )

            VALUES (
                ?, ?, ?, ?,
                ?, ?,
                ?,
                ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?
            )

            ON CONFLICT(user_id)
            DO UPDATE SET

                username = excluded.username,
                first_name = excluded.first_name,
                bio = excluded.bio,

                avatar_hash = excluded.avatar_hash,
                profile_hash = excluded.profile_hash,

                has_stories = excluded.has_stories,

                overall = excluded.overall,

                avatar_score = excluded.avatar_score,
                og_score = excluded.og_score,
                bio_score = excluded.bio_score,
                activity_score = excluded.activity_score,

                avatar_text = excluded.avatar_text,
                og_text = excluded.og_text,
                bio_text = excluded.bio_text,
                activity_text = excluded.activity_text,

                strength = excluded.strength,
                weakness = excluded.weakness,

                rank = excluded.rank,
                updated_at = excluded.updated_at
            """,
            (
                user["id"],
                user.get("username") or "none",
                user.get("name") or "",
                user.get("bio") or "",

                avatar_hash,
                profile_hash,

                1 if user.get("has_stories") else 0,

                overall,

                float(ai_data["avatar_score"]),
                float(ai_data["og_score"]),
                float(ai_data["bio_score"]),
                float(ai_data["activity_score"]),

                ai_data.get(
                    "avatar_text",
                    ""
                ),

                ai_data.get(
                    "og_text",
                    ""
                ),

                ai_data.get(
                    "bio_text",
                    ""
                ),

                ai_data.get(
                    "activity_text",
                    ""
                ),

                ai_data.get(
                    "strength",
                    ""
                ),

                ai_data.get(
                    "weakness",
                    ""
                ),

                rank,

                datetime.utcnow().isoformat()
            )
        )

        db.commit()


def register_chat_member(
    chat_id,
    user_id
):

    if chat_id is None:
        return

    with db_lock:

        db.execute(
            """
            INSERT INTO chat_members (
                chat_id,
                user_id,
                last_seen
            )

            VALUES (?, ?, ?)

            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET
                last_seen = excluded.last_seen
            """,
            (
                chat_id,
                user_id,
                datetime.utcnow().isoformat()
            )
        )

        db.commit()


# ============================================================
# TELEGRAM USER DATA
# ============================================================

async def get_user_data(
    username_or_id
):

    try:

        entity = await client.get_entity(
            username_or_id
        )

        full = await client(
            GetFullUserRequest(
                entity
            )
        )

        photo_path = None

        try:

            photo_path = (
                await client.download_profile_photo(
                    entity,
                    file=f"avatar_{entity.id}.jpg"
                )
            )

        except Exception as e:

            print(
                "Avatar download error:",
                repr(e)
            )

        has_stories = False

        try:

            stories = await client.get_stories(
                entity
            )

            has_stories = bool(
                getattr(
                    stories,
                    "stories",
                    None
                )
            )

        except Exception:

            has_stories = False

        user = {
            "id": entity.id,

            "username": (
                entity.username
                or "none"
            ),

            "name": (
                entity.first_name
                or ""
            ),

            "bio": (
                full.full_user.about
                or ""
            ),

            "photo_path": photo_path,

            "has_stories": has_stories
        }

        avatar_hash = sha256_file(
            photo_path
        )

        user["avatar_hash"] = avatar_hash

        user["profile_hash"] = calculate_profile_hash(
            user,
            avatar_hash
        )

        return user

    except Exception as e:

        print(
            "get_user_data error:",
            repr(e)
        )

        return None


# ============================================================
# CHECK IF PROFILE NEEDS AI
# ============================================================

def profile_needs_update(
    user,
    force=False
):

    if force:
        return True

    saved = get_saved_profile(
        user["id"]
    )

    if not saved:
        return True

    if (
        saved["profile_hash"]
        != user["profile_hash"]
    ):
        return True

    return False


# ============================================================
# GEMINI JSON SCHEMA
# ============================================================

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {

        "overall": {
            "type": "number"
        },

        "avatar_score": {
            "type": "number"
        },

        "avatar_text": {
            "type": "string"
        },

        "og_score": {
            "type": "number"
        },

        "og_text": {
            "type": "string"
        },

        "bio_score": {
            "type": "number"
        },

        "bio_text": {
            "type": "string"
        },

        "activity_score": {
            "type": "number"
        },

        "activity_text": {
            "type": "string"
        },

        "strength": {
            "type": "string"
        },

        "weakness": {
            "type": "string"
        }
    },

    "required": [
        "overall",

        "avatar_score",
        "avatar_text",

        "og_score",
        "og_text",

        "bio_score",
        "bio_text",

        "activity_score",
        "activity_text",

        "strength",
        "weakness"
    ]
}


# ============================================================
# SERIOUS JUDGE PROMPT
# ============================================================

JUDGE_PROMPT = """
Ты — профессиональный нейтральный аналитик Telegram-профилей.

Твоя задача — оценить ОДИН профиль.

Оценка должна быть:

- максимально серьёзной;
- нейтральной;
- последовательной;
- аргументированной;
- одинаковой при повторном анализе того же профиля.

Не используй оскорбления.
Не унижай пользователя.
Не называй человека "некрасивым", "жалким" и т.п.

Оценивай ТОЛЬКО оформление профиля.

Не делай выводы о:

- возрасте;
- расе;
- национальности;
- религии;
- здоровье;
- сексуальной ориентации;
- политике;
- социальном статусе;
- других чувствительных характеристиках.

КРИТЕРИИ:

1. Аватар
2. OG Status
3. Bio / Style
4. Activity

--------------------------------------------------
АВАТАР
--------------------------------------------------

Учитывай:

- качество изображения;
- читаемость;
- композицию;
- визуальную целостность;
- оригинальность оформления;
- то, насколько хорошо изображение работает как маленькая Telegram-аватарка.

--------------------------------------------------
OG STATUS
--------------------------------------------------

Используй Telegram ID как один из объективных сигналов.

Не утверждай точный возраст аккаунта, если его нельзя достоверно установить.

Более низкий Telegram ID может указывать на более раннюю регистрацию,
но это только косвенный сигнал.

--------------------------------------------------
BIO / STYLE
--------------------------------------------------

Учитывай:

- содержание bio;
- краткость;
- читаемость;
- индивидуальность;
- соответствие визуальному образу;
- отсутствие лишнего текста.

--------------------------------------------------
ACTIVITY
--------------------------------------------------

Учитывай наличие доступных stories как сигнал активности.

Если stories отсутствуют или недоступны,
не придумывай дополнительную активность.

--------------------------------------------------
OVERALL
--------------------------------------------------

Overall — итоговая оценка от 0 до 10.

Оценки должны быть логически связаны с категориями.

Используй десятичные значения.

Не меняй оценку только потому, что профиль сравнивается
с другим пользователем.

Один и тот же профиль должен получать примерно одну и ту же
оценку при одинаковых входных данных.

ВАЖНО:

Ты анализируешь только этот профиль.
Не сравнивай его с другим человеком.

Верни ТОЛЬКО JSON.
"""


# ============================================================
# GEMINI ANALYSIS
# ============================================================

async def analyze_profile_with_ai(
    user
):

    prompt = f"""
{JUDGE_PROMPT}

ДАННЫЕ ПРОФИЛЯ:

Telegram ID:
{user["id"]}

Username:
@{user["username"]}

Name:
{user["name"]}

Bio:
{user["bio"]}

Stories available:
{user["has_stories"]}

Поставь оценки от 0 до 10.

Текст каждого объяснения должен быть коротким:
1–2 предложения.

Не используй markdown.

Не используй emoji.

Не добавляй ничего кроме JSON.
"""

    contents = [
        prompt
    ]

    if user.get("photo_path"):

        try:

            with open(
                user["photo_path"],
                "rb"
            ) as f:

                image_bytes = f.read()

            contents.append(
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg"
                )
            )

        except Exception as e:

            print(
                "Image input error:",
                repr(e)
            )

    else:

        contents.append(
            "Аватар отсутствует."
        )

    response = await asyncio.to_thread(
        gemini_client.models.generate_content,
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_json_schema=PROFILE_SCHEMA
        )
    )

    if not response.text:

        raise RuntimeError(
            "Gemini returned empty response"
        )

    data = json.loads(
        response.text
    )

    # --------------------------------------------------------
    # NORMALIZE
    # --------------------------------------------------------

    score_keys = [
        "overall",
        "avatar_score",
        "og_score",
        "bio_score",
        "activity_score"
    ]

    for key in score_keys:

        try:

            value = float(
                data.get(
                    key,
                    0
                )
            )

        except Exception:

            value = 0

        data[key] = max(
            0,
            min(
                10,
                value
            )
        )

    # --------------------------------------------------------
    # Recalculate overall from category scores.
    #
    # This makes scoring more consistent.
    # --------------------------------------------------------

    calculated_overall = (
        data["avatar_score"]
        + data["og_score"]
        + data["bio_score"]
        + data["activity_score"]
    ) / 4

    # Use calculated score instead of
    # trusting an inconsistent AI overall.
    data["overall"] = round(
        calculated_overall,
        2
    )

    return data


# ============================================================
# GET OR UPDATE PROFILE
# ============================================================

async def get_or_update_profile(
    user,
    force=False
):

    needs_update = profile_needs_update(
        user,
        force=force
    )

    saved = get_saved_profile(
        user["id"]
    )

    if not needs_update and saved:

        return saved, False

    ai_data = await analyze_profile_with_ai(
        user
    )

    save_profile(
        user,
        user["profile_hash"],
        user["avatar_hash"],
        ai_data
    )

    return get_saved_profile(
        user["id"]
    ), True


# ============================================================
# SCORE HELPERS
# ============================================================

def clamp_score(value):

    try:
        value = float(value)
    except:
        value = 0

    return max(
        0,
        min(
            10,
            value
        )
    )


def score_emoji(score):

    score = clamp_score(
        score
    )

    if score >= 9:
        return "👑"

    if score >= 8:
        return "🔥"

    if score >= 7:
        return "🟢"

    if score >= 6:
        return "🔵"

    if score >= 5:
        return "🟡"

    if score >= 3:
        return "🟠"

    return "🔴"


# ============================================================
# AVATAR DRAW
# ============================================================

def draw_round_avatar(
    img_path,
    size=(130, 130)
):

    if img_path and os.path.exists(
        img_path
    ):

        try:

            img = Image.open(
                img_path
            ).convert(
                "RGBA"
            )

            img = ImageOps.fit(
                img,
                size,
                Image.Resampling.LANCZOS
            )

            mask = Image.new(
                "L",
                size,
                0
            )

            md = ImageDraw.Draw(
                mask
            )

            md.ellipse(
                [
                    0,
                    0,
                    size[0] - 1,
                    size[1] - 1
                ],
                fill=255
            )

            output = Image.new(
                "RGBA",
                size,
                (0, 0, 0, 0)
            )

            output.paste(
                img,
                (0, 0),
                mask
            )

            return output

        except Exception:
            pass

    # placeholder

    img = Image.new(
        "RGBA",
        size,
        (45, 45, 50, 255)
    )

    d = ImageDraw.Draw(
        img
    )

    d.ellipse(
        [
            0,
            0,
            size[0] - 1,
            size[1] - 1
        ],
        fill=(75, 75, 80)
    )

    d.text(
        (
            size[0] // 2,
            size[1] // 2
        ),
        "?",
        fill="white",
        font=get_font(50),
        anchor="mm"
    )

    return img


# ============================================================
# MOGGED STAMP
# ============================================================

def make_mogged_stamp():

    # Большой холст, чтобы текст не обрезался
    stamp_w = 310
    stamp_h = 90

    stamp = Image.new(
        "RGBA",
        (
            stamp_w,
            stamp_h
        ),
        (0, 0, 0, 0)
    )

    d = ImageDraw.Draw(
        stamp
    )

    # Красная рамка
    d.rounded_rectangle(
        [
            4,
            4,
            stamp_w - 4,
            stamp_h - 4
        ],
        radius=10,
        outline=(220, 30, 30, 255),
        width=7
    )

    font = get_font(
        43,
        bold=True
    )

    d.text(
        (
            stamp_w // 2,
            stamp_h // 2
        ),
        "MOGGED",
        fill=(225, 30, 30, 255),
        stroke_width=2,
        stroke_fill=(90, 0, 0, 255),
        font=font,
        anchor="mm"
    )

    # ИМЕННО 12°
    stamp = stamp.rotate(
        12,
        resample=Image.Resampling.BICUBIC,
        expand=True
    )

    return stamp


# ============================================================
# CROWN
# ============================================================

def draw_crown(
    draw,
    cx,
    cy
):

    points = [
        (cx - 34, cy + 12),
        (cx + 34, cy + 12),

        (cx + 38, cy - 13),
        (cx + 19, cy + 2),

        (cx, cy - 30),

        (cx - 19, cy + 2),
        (cx - 38, cy - 13)
    ]

    draw.polygon(
        points,
        fill=(255, 204, 0),
        outline=(210, 160, 0)
    )

    draw.rectangle(
        [
            cx - 34,
            cy + 12,
            cx + 34,
            cy + 20
        ],
        fill=(255, 180, 0)
    )


# ============================================================
# SCORE BAR
# ============================================================

def draw_score_bar(
    draw,
    x1,
    y,
    x2,
    score
):

    score = clamp_score(
        score
    )

    draw.rounded_rectangle(
        [
            x1,
            y,
            x2,
            y + 14
        ],
        radius=7,
        fill=(52, 52, 58)
    )

    fill_x = x1 + int(
        (x2 - x1)
        * score
        / 10
    )

    if fill_x > x1:

        draw.rounded_rectangle(
            [
                x1,
                y,
                fill_x,
                y + 14
            ],
            radius=7,
            fill=(255, 204, 0)
        )


# ============================================================
# DRAW PLAYER CARD
# ============================================================

def draw_player_card(
    card,
    draw,
    y,
    profile,
    photo_path,
    is_winner,
    is_loser,
    width
):

    card_x1 = 32
    card_x2 = width - 32
    card_y1 = y
    card_y2 = y + 510

    # border

    if is_winner:

        border_color = (
            255,
            204,
            0
        )

        border_width = 4

    else:

        border_color = (
            52,
            52,
            58
        )

        border_width = 2

    draw.rounded_rectangle(
        [
            card_x1,
            card_y1,
            card_x2,
            card_y2
        ],
        radius=28,
        fill=(24, 24, 28),
        outline=border_color,
        width=border_width
    )

    # --------------------------------------------------------
    # STATUS LABEL
    # --------------------------------------------------------

    if is_winner:

        draw.rounded_rectangle(
            [
                card_x1 + 22,
                card_y1 + 20,
                card_x1 + 205,
                card_y1 + 62
            ],
            radius=14,
            fill=(255, 204, 0)
        )

        draw.text(
            (
                card_x1 + 113,
                card_y1 + 41
            ),
            "ПОБЕДИТЕЛЬ",
            fill=(20, 20, 20),
            font=get_font(18),
            anchor="mm"
        )

    # --------------------------------------------------------
    # AVATAR
    # --------------------------------------------------------

    avatar = draw_round_avatar(
        photo_path,
        size=(130, 130)
    )

    avatar_x = (
        width // 2 - 65
    )

    avatar_y = card_y1 + 45

    card.paste(
        avatar,
        (
            avatar_x,
            avatar_y
        ),
        avatar
    )

    # --------------------------------------------------------
    # CROWN
    # --------------------------------------------------------

    if is_winner:

        draw_crown(
            draw,
            width // 2,
            avatar_y - 5
        )

    # --------------------------------------------------------
    # MOGGED
    # --------------------------------------------------------

    if is_loser:

        stamp = make_mogged_stamp()

        # Уменьшаем, чтобы не закрывал весь профиль
        max_w = 210

        if stamp.width > max_w:

            ratio = (
                max_w
                / stamp.width
            )

            stamp = stamp.resize(
                (
                    int(stamp.width * ratio),
                    int(stamp.height * ratio)
                ),
                Image.Resampling.LANCZOS
            )

        stamp_x = (
            width // 2
            - stamp.width // 2
        )

        stamp_y = (
            avatar_y
            + 35
            - stamp.height // 2
        )

        card.paste(
            stamp,
            (
                stamp_x,
                stamp_y
            ),
            stamp
        )

    # --------------------------------------------------------
    # USERNAME
    # --------------------------------------------------------

    username = (
        profile["username"]
        or "none"
    )

    # Ограничиваем длину username,
    # чтобы длинный username не ломал дизайн.

    if len(username) > 22:

        username = (
            username[:20]
            + "..."
        )

    draw.text(
        (
            width // 2,
            card_y1 + 195
        ),
        "@" + username,
        fill=(245, 245, 247),
        font=get_font(25),
        anchor="mm"
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    overall = clamp_score(
        profile["overall"]
    )

    draw.text(
        (
            width // 2,
            card_y1 + 250
        ),
        f"{overall:.1f}",
        fill=(255, 204, 0),
        font=get_font(58),
        anchor="mm"
    )

    # --------------------------------------------------------
    # RANK
    # --------------------------------------------------------

    rank = get_rank(
        overall
    )

    draw.text(
        (
            width // 2,
            card_y1 + 300
        ),
        rank,
        fill=(220, 220, 225),
        font=get_font(21),
        anchor="mm"
    )

    # --------------------------------------------------------
    # METRICS
    # --------------------------------------------------------

    metrics = [
        (
            "АВАТАР",
            profile["avatar_score"]
        ),
        (
            "OG СТАТУС",
            profile["og_score"]
        ),
        (
            "BIO / СТИЛЬ",
            profile["bio_score"]
        ),
        (
            "АКТИВНОСТЬ",
            profile["activity_score"]
        )
    ]

    bar_y = card_y1 + 350

    for label, score in metrics:

        draw.text(
            (
                65,
                bar_y
            ),
            label,
            fill=(175, 175, 180),
            font=get_font(15)
        )

        draw.text(
            (
                width - 65,
                bar_y
            ),
            f"{float(score):.1f}",
            fill=(245, 245, 247),
            font=get_font(15),
            anchor="ra"
        )

        draw_score_bar(
            draw,
            65,
            bar_y + 27,
            width - 65,
            score
        )

        bar_y += 40


# ============================================================
# GENERATE MOG CARD
# ============================================================

def generate_mog_card(
    p1,
    p2,
    u1_photo,
    u2_photo,
    winner
):

    width = 720
    height = 1260

    card = Image.new(
        "RGB",
        (
            width,
            height
        ),
        (11, 11, 14)
    )

    draw = ImageDraw.Draw(
        card
    )

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    draw.text(
        (
            width // 2,
            40
        ),
        "MOG БАТТЛ",
        fill=(255, 204, 0),
        font=get_font(38),
        anchor="mm"
    )

    draw.text(
        (
            width // 2,
            78
        ),
        "КТО ИМЕЕТ ЛУЧШИЙ ПРОФИЛЬ?",
        fill=(135, 135, 140),
        font=get_font(16),
        anchor="mm"
    )

    # --------------------------------------------------------
    # PLAYER 1
    # --------------------------------------------------------

    draw_player_card(
        card,
        draw,
        110,
        p1,
        u1_photo,
        winner == 1,
        winner == 2,
        width
    )

    # --------------------------------------------------------
    # VS
    # --------------------------------------------------------

    vs_y = 640

    draw.line(
        [
            50,
            vs_y,
            310,
            vs_y
        ],
        fill=(65, 65, 70),
        width=2
    )

    draw.line(
        [
            width - 310,
            vs_y,
            width - 50,
            vs_y
        ],
        fill=(65, 65, 70),
        width=2
    )

    draw.text(
        (
            width // 2,
            vs_y
        ),
        "VS",
        fill=(245, 245, 247),
        font=get_font(31),
        anchor="mm"
    )

    # --------------------------------------------------------
    # PLAYER 2
    # --------------------------------------------------------

    draw_player_card(
        card,
        draw,
        670,
        p2,
        u2_photo,
        winner == 2,
        winner == 1,
        width
    )

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    winner_profile = (
        p1
        if winner == 1
        else p2
    )

    winner_name = (
        winner_profile["username"]
        or "none"
    )

    gap = abs(
        float(p1["overall"])
        - float(p2["overall"])
    )

    draw.rounded_rectangle(
        [
            45,
            1190,
            width - 45,
            1240
        ],
        radius=15,
        fill=(25, 25, 29)
    )

    draw.text(
        (
            width // 2,
            1210
        ),
        f"@{winner_name} ПОБЕЖДАЕТ",
        fill=(255, 204, 0),
        font=get_font(21),
        anchor="mm"
    )

    draw.text(
        (
            width // 2,
            1245
        ),
        f"РАЗНИЦА: {gap:.2f} БАЛЛА",
        fill=(140, 140, 145),
        font=get_font(13),
        anchor="mm"
    )

    output_path = (
        f"mog_card_{os.getpid()}_{threading.get_ident()}.png"
    )

    card.save(
        output_path,
        quality=95
    )

    return output_path


# ============================================================
# FORMAT PROFILE ANALYSIS
# ============================================================

def format_profile_analysis(
    profile
):

    overall = float(
        profile["overall"]
    )

    rank = get_rank(
        overall
    )

    lines = []

    lines.append(
        f"👤 <b>@{profile['username']}</b>"
    )

    lines.append(
        f"🎯 <b>{overall:.2f}/10</b> "
        f"• {rank_emoji(rank)} <b>{rank}</b>"
    )

    lines.append("")

    lines.append(
        f"{score_emoji(profile['avatar_score'])} "
        f"<b>Аватар — "
        f"{float(profile['avatar_score']):.1f}/10</b>"
    )

    lines.append(
        f"└ {profile['avatar_text']}"
    )

    lines.append("")

    lines.append(
        f"{score_emoji(profile['og_score'])} "
        f"<b>OG статус — "
        f"{float(profile['og_score']):.1f}/10</b>"
    )

    lines.append(
        f"└ {profile['og_text']}"
    )

    lines.append("")

    lines.append(
        f"{score_emoji(profile['bio_score'])} "
        f"<b>Bio / стиль — "
        f"{float(profile['bio_score']):.1f}/10</b>"
    )

    lines.append(
        f"└ {profile['bio_text']}"
    )

    lines.append("")

    lines.append(
        f"{score_emoji(profile['activity_score'])} "
        f"<b>Активность — "
        f"{float(profile['activity_score']):.1f}/10</b>"
    )

    lines.append(
        f"└ {profile['activity_text']}"
    )

    lines.append("")

    lines.append(
        f"💪 <b>Сильная сторона:</b>\n"
        f"{profile['strength']}"
    )

    lines.append("")

    lines.append(
        f"⚠️ <b>Что снижает оценку:</b>\n"
        f"{profile['weakness']}"
    )

    return "\n".join(
        lines
    )


# ============================================================
# MOG COMMAND
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.мог(?:\s+([^\s]+))?(?:\s+([^\s]+))?$"
    )
)
async def mog_handler(event):

    args = event.pattern_match.groups()

    if not args[0]:

        await event.reply(
            """
⚔️ <b>MOG БАТТЛ</b>

<code>.мог @user</code>
Сравнить пользователя с тобой.

<code>.мог @user1 @user2</code>
Сравнить двух пользователей.

<code>.обновить</code>
Принудительно обновить свой рейтинг.
""",
            parse_mode="html"
        )

        return

    status = await event.reply(
        "⚙️ <b>Проверяю профили...</b>",
        parse_mode="html"
    )

    u1 = None
    u2 = None
    card_path = None

    try:

        # ----------------------------------------------------
        # GET USER 1
        # ----------------------------------------------------

        u1 = await get_user_data(
            args[0]
        )

        if not u1:

            await status.edit(
                "❌ Не удалось получить первый профиль."
            )

            return

        # ----------------------------------------------------
        # GET USER 2
        # ----------------------------------------------------

        if args[1]:

            target2 = args[1]

        else:

            sender = await event.get_sender()

            if sender.username:

                target2 = (
                    "@"
                    + sender.username
                )

            else:

                target2 = sender.id

        u2 = await get_user_data(
            target2
        )

        if not u2:

            await status.edit(
                "❌ Не удалось получить второй профиль."
            )

            return

        # ----------------------------------------------------
        # REGISTER CHAT
        # ----------------------------------------------------

        await asyncio.to_thread(
            register_chat_member,
            event.chat_id,
            u1["id"]
        )

        await asyncio.to_thread(
            register_chat_member,
            event.chat_id,
            u2["id"]
        )

        # ----------------------------------------------------
        # UPDATE PROFILES IF NEEDED
        # ----------------------------------------------------

        await status.edit(
            "🔍 <b>Проверяю актуальность рейтингов...</b>",
            parse_mode="html"
        )

        p1, updated1 = (
            await get_or_update_profile(
                u1
            )
        )

        p2, updated2 = (
            await get_or_update_profile(
                u2
            )
        )

        # ----------------------------------------------------
        # WINNER
        # ----------------------------------------------------

        if float(p1["overall"]) >= float(
            p2["overall"]
        ):

            winner = 1

        else:

            winner = 2

        # ----------------------------------------------------
        # CARD
        # ----------------------------------------------------

        card_path = generate_mog_card(
            p1,
            p2,
            u1["photo_path"],
            u2["photo_path"],
            winner
        )

        winner_profile = (
            p1
            if winner == 1
            else p2
        )

        winner_name = (
            winner_profile["username"]
            or "none"
        )

        winner_score = float(
            winner_profile["overall"]
        )

        winner_rank = get_rank(
            winner_score
        )

        gap = abs(
            float(p1["overall"])
            - float(p2["overall"])
        )

        # ----------------------------------------------------
        # SEND CARD
        # ----------------------------------------------------

        await client.send_file(
            event.chat_id,
            card_path,
            caption=(
                f"👑 <b>@{winner_name} ПОБЕЖДАЕТ</b>\n\n"
                f"Рейтинг: <b>{winner_score:.2f}/10</b>\n"
                f"Ранг: <b>{winner_rank}</b>\n"
                f"Разница: <b>{gap:.2f}</b>"
            ),
            parse_mode="html"
        )

        # ----------------------------------------------------
        # FULL ANALYSIS
        # ----------------------------------------------------

        result = []

        result.append(
            "⚖️ <b>НЕЙТРАЛЬНЫЙ РАЗБОР</b>"
        )

        result.append("")

        result.append(
            format_profile_analysis(
                p1
            )
        )

        result.append(
            "\n━━━━━━━━━━━━━━━━━━\n"
        )

        result.append(
            format_profile_analysis(
                p2
            )
        )

        result.append(
            "\n━━━━━━━━━━━━━━━━━━\n"
        )

        winner_name = (
            p1["username"]
            if winner == 1
            else p2["username"]
        )

        loser_name = (
            p2["username"]
            if winner == 1
            else p1["username"]
        )

        result.append(
            f"🏆 <b>ПОБЕДИТЕЛЬ: "
            f"@{winner_name}</b>"
        )

        result.append("")

        result.append(
            "📌 <b>Почему победил:</b>"
        )

        # Сравниваем сохранённые категории.
        reasons = []

        categories = [
            (
                "аватар",
                "avatar_score"
            ),
            (
                "OG статус",
                "og_score"
            ),
            (
                "bio / стиль",
                "bio_score"
            ),
            (
                "активность",
                "activity_score"
            )
        ]

        winner_profile = (
            p1
            if winner == 1
            else p2
        )

        loser_profile = (
            p2
            if winner == 1
            else p1
        )

        for name, key in categories:

            w_score = float(
                winner_profile[key]
            )

            l_score = float(
                loser_profile[key]
            )

            if w_score > l_score:

                diff = (
                    w_score
                    - l_score
                )

                reasons.append(
                    f"• {name.capitalize()}: "
                    f"{w_score:.1f} против "
                    f"{l_score:.1f} "
                    f"(+{diff:.1f})"
                )

        if reasons:

            result.extend(
                reasons
            )

        else:

            result.append(
                "• Победа определена небольшим "
                "преимуществом по совокупной оценке."
            )

        result.append("")

        result.append(
            f"📉 <b>Почему уступил "
            f"@{loser_name}:</b>"
        )

        weaker = []

        for name, key in categories:

            w_score = float(
                winner_profile[key]
            )

            l_score = float(
                loser_profile[key]
            )

            if l_score < w_score:

                weaker.append(
                    f"• {name.capitalize()}: "
                    f"{l_score:.1f} против "
                    f"{w_score:.1f}"
                )

        if weaker:

            result.extend(
                weaker
            )

        else:

            result.append(
                "• Разница минимальна; "
                "профили практически равны."
            )

        result.append("")

        result.append(
            f"📊 <b>Разница:</b> "
            f"{gap:.2f} балла"
        )

        await event.reply(
            "\n".join(result),
            parse_mode="html"
        )

        await status.delete()

    except Exception as e:

        print(
            "MOG ERROR:",
            repr(e)
        )

        try:

            await status.edit(
                "❌ <b>Ошибка анализа.</b>\n\n"
                f"<code>{str(e)[:1200]}</code>",
                parse_mode="html"
            )

        except Exception:
            pass

    finally:

        if card_path:

            try:

                if os.path.exists(
                    card_path
                ):

                    os.remove(
                        card_path
                    )

            except Exception:
                pass

        for user in [u1, u2]:

            if user:

                try:

                    path = user.get(
                        "photo_path"
                    )

                    if path and os.path.exists(
                        path
                    ):

                        os.remove(
                            path
                        )

                except Exception:
                    pass


# ============================================================
# UPDATE COMMAND
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.обновить$"
    )
)
async def update_handler(event):

    status = await event.reply(
        "🔄 <b>Принудительно обновляю твой профиль...</b>",
        parse_mode="html"
    )

    user = None

    try:

        sender = await event.get_sender()

        if sender.username:

            target = (
                "@"
                + sender.username
            )

        else:

            target = sender.id

        user = await get_user_data(
            target
        )

        if not user:

            await status.edit(
                "❌ Не удалось получить профиль."
            )

            return

        register_chat_member(
            event.chat_id,
            user["id"]
        )

        profile, updated = (
            await get_or_update_profile(
                user,
                force=True
            )
        )

        rank = get_rank(
            profile["overall"]
        )

        await status.edit(
            f"""
✅ <b>Профиль обновлён</b>

👤 @{profile["username"]}

🎯 <b>{float(profile["overall"]):.2f}/10</b>
{rank_emoji(rank)} <b>{rank}</b>

Аватар: {float(profile["avatar_score"]):.1f}
OG статус: {float(profile["og_score"]):.1f}
Bio / стиль: {float(profile["bio_score"]):.1f}
Активность: {float(profile["activity_score"]):.1f}
""",
            parse_mode="html"
        )

    except Exception as e:

        print(
            "UPDATE ERROR:",
            repr(e)
        )

        await status.edit(
            f"❌ Ошибка: <code>{str(e)[:1000]}</code>",
            parse_mode="html"
        )

    finally:

        if user:

            try:

                path = user.get(
                    "photo_path"
                )

                if path and os.path.exists(
                    path
                ):

                    os.remove(
                        path
                    )

            except Exception:
                pass


# ============================================================
# HELP COMMAND
# ============================================================

HELP_SCHEMA = {
    "type": "object",

    "properties": {

        "verdict": {
            "type": "string"
        },

        "top_3": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },

        "final_advice": {
            "type": "string"
        }
    },

    "required": [
        "verdict",
        "top_3",
        "final_advice"
    ]
}


HELP_PROMPT = """
Ты — нейтральный консультант по улучшению Telegram-профиля.

Проанализируй профиль и дай практические рекомендации.

Не унижай пользователя.
Не используй токсичность.
Не оценивай личность человека.

Оцени только:

- визуальное оформление;
- username;
- bio;
- активность;
- целостность стиля.

Цель — помочь улучшить профиль.

Дай конкретные действия.

Не говори:
"сделай лучше".

Говори конкретно:
"замени аватар на изображение с более сильным контрастом..."
и т.п.

Верни JSON.
"""


@client.on(
    events.NewMessage(
        pattern=r"^\.хелп(?:\s+([^\s]+))?$"
    )
)
async def help_handler(event):

    args = event.pattern_match.groups()

    status = await event.reply(
        "🧠 <b>Анализирую профиль...</b>",
        parse_mode="html"
    )

    user = None

    try:

        if args[0]:

            target = args[0]

        else:

            sender = await event.get_sender()

            if sender.username:

                target = (
                    "@"
                    + sender.username
                )

            else:

                target = sender.id

        user = await get_user_data(
            target
        )

        if not user:

            await status.edit(
                "❌ Не удалось получить профиль."
            )

            return

        register_chat_member(
            event.chat_id,
            user["id"]
        )

        profile, updated = (
            await get_or_update_profile(
                user
            )
        )

        prompt = f"""
{HELP_PROMPT}

Профиль:

Username:
@{user["username"]}

Bio:
{user["bio"]}

Stories:
{user["has_stories"]}

Текущая оценка:
{float(profile["overall"]):.2f}

Ранг:
{get_rank(profile["overall"])}

Категории:

Аватар:
{float(profile["avatar_score"]):.1f}

OG:
{float(profile["og_score"]):.1f}

Bio:
{float(profile["bio_score"]):.1f}

Activity:
{float(profile["activity_score"]):.1f}
"""

        contents = [
            prompt
        ]

        if user["photo_path"]:

            try:

                with open(
                    user["photo_path"],
                    "rb"
                ) as f:

                    image_bytes = f.read()

                contents.append(
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type="image/jpeg"
                    )
                )

            except Exception:
                pass

        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                response_json_schema=HELP_SCHEMA
            )
        )

        data = json.loads(
            response.text
        )

        rank = get_rank(
            profile["overall"]
        )

        result = []

        result.append(
            "🧠 <b>PROFILE COACH</b>"
        )

        result.append("")

        result.append(
            f"👤 <b>@{profile['username']}</b>"
        )

        result.append(
            f"🎯 <b>{float(profile['overall']):.2f}/10</b> "
            f"• {rank_emoji(rank)} <b>{rank}</b>"
        )

        result.append("")

        result.append(
            f"📌 <b>Диагноз:</b>\n"
            f"{data.get('verdict', '-')}"
        )

        result.append("")

        result.append(
            "🔥 <b>Что делать:</b>"
        )

        top_3 = data.get(
            "top_3",
            []
        )

        for i, item in enumerate(
            top_3[:3],
            1
        ):

            result.append(
                f"{i}. {item}"
            )

        result.append("")

        result.append(
            f"👑 <b>Итог:</b>\n"
            f"{data.get('final_advice', '-')}"
        )

        await event.reply(
            "\n".join(result),
            parse_mode="html"
        )

        await status.delete()

    except Exception as e:

        print(
            "HELP ERROR:",
            repr(e)
        )

        await status.edit(
            f"❌ Ошибка: <code>{str(e)[:1000]}</code>",
            parse_mode="html"
        )

    finally:

        if user:

            try:

                path = user.get(
                    "photo_path"
                )

                if path and os.path.exists(
                    path
                ):

                    os.remove(
                        path
                    )

            except Exception:
                pass


# ============================================================
# TOP COMMAND
# ============================================================

def format_top_rows(
    rows,
    title
):

    lines = []

    lines.append(
        f"🏆 <b>{title}</b>"
    )

    lines.append("")

    if not rows:

        lines.append(
            "Пока нет оценённых профилей."
        )

        return "\n".join(lines)

    for index, row in enumerate(
        rows,
        1
    ):

        username = (
            row["username"]
            or "none"
        )

        score = float(
            row["overall"]
        )

        rank = get_rank(
            score
        )

        lines.append(
            f"<b>{index}.</b> "
            f"{rank_emoji(rank)} "
            f"<b>@{username}</b> — "
            f"<b>{score:.2f}</b> "
            f"• {rank}"
        )

    return "\n".join(
        lines
    )


@client.on(
    events.NewMessage(
        pattern=r"^\.топ(?:\s+(все|чата))?$"
    )
)
async def top_handler(event):

    args = event.pattern_match.groups()

    mode = (
        args[0]
        if args
        else None
    )

    try:

        # ----------------------------------------------------
        # GLOBAL
        # ----------------------------------------------------

        if mode == "все":

            with db_lock:

                rows = db.execute(
                    """
                    SELECT
                        username,
                        overall,
                        rank
                    FROM profiles
                    ORDER BY overall DESC
                    LIMIT 20
                    """
                ).fetchall()

            await event.reply(
                format_top_rows(
                    rows,
                    "ТОП — ВСЕ ПРОФИЛИ"
                ),
                parse_mode="html"
            )

            return

        # ----------------------------------------------------
        # CHAT
        # ----------------------------------------------------

        chat_id = event.chat_id

        if chat_id is None:

            await event.reply(
                "❌ Эта команда работает в чате."
            )

            return

        with db_lock:

            rows = db.execute(
                """
                SELECT
                    p.username,
                    p.overall,
                    p.rank

                FROM profiles p

                INNER JOIN chat_members cm
                    ON cm.user_id = p.user_id

                WHERE cm.chat_id = ?

                ORDER BY p.overall DESC

                LIMIT 20
                """,
                (
                    chat_id,
                )
            ).fetchall()

        title = (
            "ТОП — ЧАТ"
            if mode in (None, "чата")
            else "ТОП"
        )

        await event.reply(
            format_top_rows(
                rows,
                title
            ),
            parse_mode="html"
        )

    except Exception as e:

        print(
            "TOP ERROR:",
            repr(e)
        )

        await event.reply(
            f"❌ Ошибка: <code>{str(e)[:1000]}</code>",
            parse_mode="html"
        )


# ============================================================
# COMMANDS
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.команды$"
    )
)
async def commands_handler(event):

    await event.reply(
        """
🤖 <b>MOG БОТ</b>

⚔️ <code>.мог @user</code>
Сравнить пользователя с тобой.

⚔️ <code>.мог @user1 @user2</code>
Сравнить двух пользователей.

🧠 <code>.хелп</code>
Советы по улучшению своего профиля.

🧠 <code>.хелп @user</code>
Разбор профиля пользователя.

🔄 <code>.обновить</code>
Принудительно пересчитать свой рейтинг.

🏆 <code>.топ</code>
Топ текущего чата.

🏆 <code>.топ чата</code>
Топ текущего чата.

🌐 <code>.топ все</code>
Глобальный топ всех сохранённых профилей.

━━━━━━━━━━━━━━━━━━

<b>СИСТЕМА РАНГОВ</b>

🔴 Sub-3 — 0.0–2.9
🟠 Sub-5 — 3.0–4.9
🟡 LTN — 5.0–5.9
🔵 MTN — 6.0–6.9
🟢 HTN — 7.0–7.9
🔥 Chad — 8.0–8.9
👑 True Adam — 9.0–10.0
""",
        parse_mode="html"
    )


# ============================================================
# START
# ============================================================

print(
    "============================================"
)

print(
    "MOG BOT STARTED"
)

print(
    "Rank system:"
)

print(
    "Sub-3 -> Sub-5 -> LTN -> MTN -> HTN -> Chad -> True Adam"
)

print(
    f"Gemini model: {GEMINI_MODEL}"
)

print(
    f"Database: {DB_PATH}"
)

print(
    "============================================"
)


client.run_until_disconnected()
