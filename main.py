import os
import json
import threading
import re
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler

from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest

import google.generativeai as genai

from PIL import Image, ImageDraw, ImageFont, ImageOps


# ============================================================
# CONFIG
# ============================================================

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))

SESSION_NAME = "mog_bot_session"


# ============================================================
# VALIDATE CONFIG
# ============================================================

if not API_ID:
    print("WARNING: API_ID is not configured")

if not API_HASH:
    print("WARNING: API_HASH is not configured")

if not BOT_TOKEN:
    print("WARNING: BOT_TOKEN is not configured")

if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY is not configured")


# ============================================================
# RENDER HEALTH CHECK
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def run_health_server():
    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthCheckHandler
    )

    print(f"Health server started on port {PORT}")

    server.serve_forever()


threading.Thread(
    target=run_health_server,
    daemon=True
).start()


# ============================================================
# GEMINI
# ============================================================

genai.configure(
    api_key=GEMINI_API_KEY
)

# ВАЖНО:
# Если эта модель недоступна в твоём Gemini API,
# замени её на модель, доступную твоему проекту.
model = genai.GenerativeModel(
    "gemini-3.5-flash-lite"
)


# ============================================================
# TELEGRAM
# ============================================================

client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH
).start(
    bot_token=BOT_TOKEN
)


# ============================================================
# RANK SYSTEM
# ============================================================

def get_rank(score):
    """
    Единая система рейтингов.

    Sub-3
    Sub-5
    LTN
    MTN
    HTN
    Chad
    True Adam
    """

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


# ============================================================
# RANK EMOJI
# ============================================================

def rank_emoji(rank):

    rank = str(rank).lower()

    if rank == "true adam":
        return "👑"

    if rank == "chad":
        return "🔥"

    if rank == "htn":
        return "🟢"

    if rank == "mtn":
        return "🔵"

    if rank == "ltn":
        return "🟡"

    if rank == "sub-5":
        return "🟠"

    return "🔴"


# ============================================================
# AI PROMPT — MOG
# ============================================================

SYSTEM_PROMPT = r"""
Ты — судья Telegram Mogging Battle.

Твоя задача — сравнить ДВА публичных Telegram-профиля.

Оценивай исключительно профиль, а не человека.

Критерии:

1. Avatar
2. OG Status
3. Bio / Style
4. Activity

Каждая характеристика оценивается от 0 до 10.

------------------------------------------------------------
RANK SYSTEM
------------------------------------------------------------

Используй ТОЛЬКО эти ранги:

0.0–2.9 = Sub-3
3.0–4.9 = Sub-5
5.0–5.9 = LTN
6.0–6.9 = MTN
7.0–7.9 = HTN
8.0–8.9 = Chad
9.0–10.0 = True Adam

Никаких других рангов.

------------------------------------------------------------
IMPORTANT
------------------------------------------------------------

Не выдумывай информацию.

Если какой-то информации нет:
учитывай отсутствие информации в оценке.

Не делай выводы о:

- возрасте;
- расе;
- национальности;
- религии;
- здоровье;
- сексуальной ориентации;
- политических взглядах;
- других чувствительных характеристиках.

Оценивай только публичное оформление профиля.

Будь:

- циничным;
- мемным;
- прямым;
- остроумным;

но не превращай ответ в травлю человека.

------------------------------------------------------------
OVERALL SCORE
------------------------------------------------------------

Итоговая оценка должна быть логичной относительно
четырёх категорий.

Не ставь случайный Overall.

------------------------------------------------------------
RETURN FORMAT
------------------------------------------------------------

Верни ТОЛЬКО валидный JSON.

НЕ используй markdown.

НЕ используй ```json.

НЕ добавляй текст до или после JSON.

Формат:

{
  "winner": 1,
  "gap": 1.4,
  "explanation": "Короткий вердикт",

  "u1": {
    "username": "user1",
    "overall": 7.4,

    "avatar_score": 8.0,
    "avatar_text": "Почему такая оценка",

    "og_score": 6.5,
    "og_text": "Почему такая оценка",

    "bio_score": 7.0,
    "bio_text": "Почему такая оценка",

    "activity_score": 8.0,
    "activity_text": "Почему такая оценка",

    "strength": "Главная сильная сторона",
    "weakness": "Главная слабость"
  },

  "u2": {
    "username": "user2",
    "overall": 6.0,

    "avatar_score": 5.0,
    "avatar_text": "Почему такая оценка",

    "og_score": 8.0,
    "og_text": "Почему такая оценка",

    "bio_score": 5.0,
    "bio_text": "Почему такая оценка",

    "activity_score": 4.0,
    "activity_text": "Почему такая оценка",

    "strength": "Главная сильная сторона",
    "weakness": "Главная слабость"
  },

  "winner_reason": "Подробно объясни, почему победитель победил.",
  "loser_reason": "Подробно объясни, где проигравший уступил."
}
"""


# ============================================================
# AI PROMPT — HELP
# ============================================================

HELP_PROMPT = r"""
Ты — персональный Telegram Profile Coach.

Пользователь хочет улучшить свой Telegram-профиль
и приблизиться к уровню "True Adam".

Анализируй:

1. Avatar
2. Username
3. Bio
4. Activity
5. Общий Style

Оцени каждую категорию от 0 до 10.

Используй только эти ранги:

0.0–2.9 = Sub-3
3.0–4.9 = Sub-5
5.0–5.9 = LTN
6.0–6.9 = MTN
7.0–7.9 = HTN
8.0–8.9 = Chad
9.0–10.0 = True Adam

Не делай выводов о возрасте, расе, национальности,
религии, здоровье, сексуальности, политике или других
чувствительных характеристиках.

Оценивай исключительно публичный Telegram-профиль.

Советы должны быть:

- конкретными;
- практичными;
- короткими;
- реально выполнимыми.

Не говори просто "улучши аватар".

Объясни КАК именно.

------------------------------------------------------------
JSON
------------------------------------------------------------

Верни ТОЛЬКО JSON:

{
  "score": 7.2,
  "verdict": "Короткий диагноз",

  "avatar": {
    "score": 7.0,
    "analysis": "Разбор",
    "advice": "Что конкретно изменить"
  },

  "username": {
    "score": 8.0,
    "analysis": "Разбор",
    "advice": "Что изменить"
  },

  "bio": {
    "score": 5.0,
    "analysis": "Разбор",
    "advice": "Что изменить"
  },

  "activity": {
    "score": 8.0,
    "analysis": "Разбор",
    "advice": "Как улучшить"
  },

  "style": {
    "score": 6.0,
    "analysis": "Разбор",
    "advice": "Что сделать"
  },

  "top_3": [
    "Первое действие",
    "Второе действие",
    "Третье действие"
  ],

  "final_advice": "Итоговый совет"
}
"""


# ============================================================
# FONTS
# ============================================================

def get_font(size):

    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]

    for path in font_paths:
        try:
            return ImageFont.truetype(
                path,
                size
            )
        except Exception:
            pass

    return ImageFont.load_default()


# ============================================================
# AVATAR
# ============================================================

def draw_round_avatar(
    img_path,
    size=(120, 120)
):

    if img_path and os.path.exists(img_path):

        try:

            img = Image.open(
                img_path
            ).convert("RGBA")

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

            mask_draw = ImageDraw.Draw(
                mask
            )

            mask_draw.ellipse(
                (
                    0,
                    0,
                    size[0] - 1,
                    size[1] - 1
                ),
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

        except Exception as e:

            print(
                "Avatar error:",
                repr(e)
            )

    placeholder = Image.new(
        "RGBA",
        size,
        (40, 40, 45, 255)
    )

    draw = ImageDraw.Draw(
        placeholder
    )

    draw.ellipse(
        (
            0,
            0,
            size[0] - 1,
            size[1] - 1
        ),
        fill=(80, 80, 85, 255)
    )

    draw.text(
        (
            size[0] // 2,
            size[1] // 2
        ),
        "?",
        fill="white",
        font=get_font(50),
        anchor="mm"
    )

    return placeholder


# ============================================================
# CROWN
# ============================================================

def draw_crown(
    draw,
    cx,
    cy
):

    points = [
        (cx - 30, cy + 10),
        (cx + 30, cy + 10),
        (cx + 33, cy - 10),
        (cx + 17, cy + 2),
        (cx, cy - 22),
        (cx - 17, cy + 2),
        (cx - 33, cy - 10)
    ]

    draw.polygon(
        points,
        fill=(255, 204, 0),
        outline=(190, 145, 0)
    )

    draw.rectangle(
        [
            cx - 30,
            cy + 10,
            cx + 30,
            cy + 17
        ],
        fill=(255, 175, 0)
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

    draw.rounded_rectangle(
        [
            x1,
            y,
            x2,
            y + 12
        ],
        radius=6,
        fill=(55, 55, 60)
    )

    try:
        score = float(score)
    except:
        score = 0

    score = max(
        0,
        min(10, score)
    )

    fill_x = (
        x1
        + int(
            (x2 - x1)
            * score
            / 10
        )
    )

    if fill_x > x1:

        draw.rounded_rectangle(
            [
                x1,
                y,
                fill_x,
                y + 12
            ],
            radius=6,
            fill=(255, 204, 0)
        )


# ============================================================
# MOG CARD
# ============================================================

def generate_mog_card(
    data,
    u1_photo,
    u2_photo,
    output_path="mog_card.png"
):

    width = 700
    height = 1300

    card = Image.new(
        "RGB",
        (
            width,
            height
        ),
        (13, 13, 16)
    )

    draw = ImageDraw.Draw(
        card
    )

    title_font = get_font(38)
    subtitle_font = get_font(16)
    username_font = get_font(26)
    score_font = get_font(58)
    rank_font = get_font(20)
    small_font = get_font(15)

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    draw.text(
        (
            width // 2,
            45
        ),
        "MOGGING BATTLE",
        fill=(255, 204, 0),
        font=title_font,
        anchor="mm"
    )

    draw.text(
        (
            width // 2,
            82
        ),
        "WHO HAS THE BETTER PROFILE?",
        fill=(130, 130, 135),
        font=subtitle_font,
        anchor="mm"
    )

    winner = data.get(
        "winner",
        1
    )

    # --------------------------------------------------------
    # PLAYER
    # --------------------------------------------------------

    def player_card(
        y,
        player,
        photo,
        is_winner
    ):

        x1 = 30
        x2 = width - 30
        y2 = y + 500

        border = (
            (255, 204, 0)
            if is_winner
            else (45, 45, 50)
        )

        draw.rounded_rectangle(
            [
                x1,
                y,
                x2,
                y2
            ],
            radius=28,
            fill=(25, 25, 29),
            outline=border,
            width=4 if is_winner else 2
        )

        # winner badge
        if is_winner:

            draw.rounded_rectangle(
                [
                    x1 + 20,
                    y + 18,
                    x1 + 150,
                    y + 54
                ],
                radius=12,
                fill=(255, 204, 0)
            )

            draw.text(
                (
                    x1 + 85,
                    y + 36
                ),
                "WINNER",
                fill=(15, 15, 15),
                font=small_font,
                anchor="mm"
            )

        # avatar
        avatar = draw_round_avatar(
            photo,
            (120, 120)
        )

        avatar_x = (
            width // 2 - 60
        )

        avatar_y = y + 45

        card.paste(
            avatar,
            (
                avatar_x,
                avatar_y
            ),
            avatar
        )

        if is_winner:

            draw_crown(
                draw,
                width // 2,
                avatar_y - 5
            )

        # username
        username = player.get(
            "username",
            "unknown"
        )

        draw.text(
            (
                width // 2,
                y + 190
            ),
            "@" + username,
            fill="white",
            font=username_font,
            anchor="mm"
        )

        # overall
        overall = float(
            player.get(
                "overall",
                0
            )
        )

        draw.text(
            (
                width // 2,
                y + 245
            ),
            f"{overall:.1f}",
            fill=(255, 204, 0),
            font=score_font,
            anchor="mm"
        )

        # rank
        rank = get_rank(
            overall
        )

        draw.text(
            (
                width // 2,
                y + 290
            ),
            rank,
            fill=(220, 220, 225),
            font=rank_font,
            anchor="mm"
        )

        # categories
        categories = [
            (
                "AVATAR",
                player.get(
                    "avatar_score",
                    0
                )
            ),
            (
                "OG STATUS",
                player.get(
                    "og_score",
                    0
                )
            ),
            (
                "BIO / STYLE",
                player.get(
                    "bio_score",
                    0
                )
            ),
            (
                "ACTIVITY",
                player.get(
                    "activity_score",
                    0
                )
            )
        ]

        bar_y = y + 330

        for label, score in categories:

            draw.text(
                (
                    60,
                    bar_y
                ),
                label,
                fill=(160, 160, 165),
                font=small_font
            )

            draw.text(
                (
                    width - 60,
                    bar_y
                ),
                f"{float(score):.1f}",
                fill="white",
                font=small_font,
                anchor="ra"
            )

            draw_score_bar(
                draw,
                60,
                bar_y + 25,
                width - 60,
                score
            )

            bar_y += 42

        # strength
        strength = player.get(
            "strength",
            ""
        )

        draw.text(
            (
                60,
                y + 485
            ),
            "↑ " + strength[:70],
            fill=(150, 220, 160),
            font=small_font
        )

    # --------------------------------------------------------
    # U1
    # --------------------------------------------------------

    player_card(
        115,
        data["u1"],
        u1_photo,
        winner == 1
    )

    # VS
    draw.text(
        (
            width // 2,
            635
        ),
        "VS",
        fill="white",
        font=get_font(32),
        anchor="mm"
    )

    # --------------------------------------------------------
    # U2
    # --------------------------------------------------------

    player_card(
        680,
        data["u2"],
        u2_photo,
        winner == 2
    )

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    winner_name = (
        data["u1"]["username"]
        if winner == 1
        else data["u2"]["username"]
    )

    gap = float(
        data.get(
            "gap",
            0
        )
    )

    draw.rounded_rectangle(
        [
            50,
            1205,
            width - 50,
            1275
        ],
        radius=20,
        fill=(25, 25, 29)
    )

    draw.text(
        (
            width // 2,
            1230
        ),
        f"👑 @{winner_name} WINS",
        fill=(255, 204, 0),
        font=get_font(25),
        anchor="mm"
    )

    draw.text(
        (
            width // 2,
            1260
        ),
        f"Gap: {gap:.1f}",
        fill=(160, 160, 165),
        font=small_font,
        anchor="mm"
    )

    card.save(
        output_path,
        quality=95
    )

    return output_path


# ============================================================
# GET TELEGRAM USER
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
                "PHOTO ERROR:",
                repr(e)
            )

        has_stories = False

        try:

            stories = await client.get_stories(
                entity
            )

            has_stories = bool(
                stories
                and getattr(
                    stories,
                    "stories",
                    None
                )
            )

        except Exception:
            pass

        return {
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

            "id": entity.id,

            "has_stories": has_stories
        }

    except Exception as e:

        print(
            "USER ERROR:",
            repr(e)
        )

        return None


# ============================================================
# JSON PARSER
# ============================================================

def parse_ai_json(
    text
):

    if not text:
        raise ValueError(
            "AI returned empty response"
        )

    text = text.strip()

    text = re.sub(
        r"^```json\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"^```\s*",
        "",
        text
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:

        raise ValueError(
            "AI response doesn't contain JSON"
        )

    json_text = text[
        start:end + 1
    ]

    return json.loads(
        json_text
    )


# ============================================================
# NORMALIZE MOG DATA
# ============================================================

def normalize_mog_data(
    data
):

    for key in [
        "u1",
        "u2"
    ]:

        player = data.get(
            key,
            {}
        )

        try:
            player["overall"] = float(
                player.get(
                    "overall",
                    0
                )
            )
        except:
            player["overall"] = 0

        for score_key in [
            "avatar_score",
            "og_score",
            "bio_score",
            "activity_score"
        ]:

            try:

                player[score_key] = float(
                    player.get(
                        score_key,
                        0
                    )
                )

            except:

                player[score_key] = 0

        player["overall"] = max(
            0,
            min(
                10,
                player["overall"]
            )
        )

        # Rank is ALWAYS calculated by Python.
        player["rank"] = get_rank(
            player["overall"]
        )

        data[key] = player

    # winner based on overall
    u1_score = data["u1"]["overall"]
    u2_score = data["u2"]["overall"]

    if u1_score >= u2_score:
        data["winner"] = 1
    else:
        data["winner"] = 2

    data["gap"] = round(
        abs(
            u1_score - u2_score
        ),
        2
    )

    return data


# ============================================================
# SCORE EMOJI
# ============================================================

def score_emoji(
    score
):

    score = float(score)

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
# TEXT PLAYER ANALYSIS
# ============================================================

def format_player_analysis(
    player
):

    username = player.get(
        "username",
        "unknown"
    )

    overall = float(
        player.get(
            "overall",
            0
        )
    )

    rank = get_rank(
        overall
    )

    text = []

    text.append(
        f"👤 <b>@{username}</b>"
    )

    text.append(
        f"🎯 <b>{overall:.1f}/10</b> "
        f"• {rank_emoji(rank)} <b>{rank}</b>"
    )

    text.append("")

    # Avatar
    avatar = float(
        player.get(
            "avatar_score",
            0
        )
    )

    text.append(
        f"{score_emoji(avatar)} "
        f"<b>Аватар — {avatar:.1f}/10</b>"
    )

    text.append(
        f"└ {player.get('avatar_text', '-')}"
    )

    text.append("")

    # OG
    og = float(
        player.get(
            "og_score",
            0
        )
    )

    text.append(
        f"{score_emoji(og)} "
        f"<b>OG статус — {og:.1f}/10</b>"
    )

    text.append(
        f"└ {player.get('og_text', '-')}"
    )

    text.append("")

    # Bio
    bio = float(
        player.get(
            "bio_score",
            0
        )
    )

    text.append(
        f"{score_emoji(bio)} "
        f"<b>Bio / Style — {bio:.1f}/10</b>"
    )

    text.append(
        f"└ {player.get('bio_text', '-')}"
    )

    text.append("")

    # Activity
    activity = float(
        player.get(
            "activity_score",
            0
        )
    )

    text.append(
        f"{score_emoji(activity)} "
        f"<b>Активность — {activity:.1f}/10</b>"
    )

    text.append(
        f"└ {player.get('activity_text', '-')}"
    )

    text.append("")

    text.append(
        f"💪 <b>Сильная сторона:</b> "
        f"{player.get('strength', '-')}"
    )

    text.append(
        f"⚠️ <b>Слабая сторона:</b> "
        f"{player.get('weakness', '-')}"
    )

    return "\n".join(text)


# ============================================================
# BATTLE RESULT
# ============================================================

def format_battle_result(
    data
):

    winner = data.get(
        "winner",
        1
    )

    winner_name = (
        data["u1"]["username"]
        if winner == 1
        else data["u2"]["username"]
    )

    loser_name = (
        data["u2"]["username"]
        if winner == 1
        else data["u1"]["username"]
    )

    result = []

    result.append(
        "⚔️ <b>MOGGING BATTLE — ПОЛНЫЙ РАЗБОР</b>"
    )

    result.append("")

    result.append(
        format_player_analysis(
            data["u1"]
        )
    )

    result.append(
        "\n━━━━━━━━━━━━━━━━━━\n"
    )

    result.append(
        format_player_analysis(
            data["u2"]
        )
    )

    result.append(
        "\n━━━━━━━━━━━━━━━━━━\n"
    )

    result.append(
        f"👑 <b>ПОБЕДИТЕЛЬ — @{winner_name}</b>"
    )

    result.append("")

    result.append(
        f"⚔️ <b>Почему победил:</b>\n"
        f"{data.get('winner_reason', '-')}"
    )

    result.append("")

    result.append(
        f"💀 <b>Почему проиграл @{loser_name}:</b>\n"
        f"{data.get('loser_reason', '-')}"
    )

    result.append("")

    result.append(
        f"📊 <b>Разрыв:</b> "
        f"{float(data.get('gap', 0)):.2f}"
    )

    return "\n".join(
        result
    )


# ============================================================
# CLEAN FILE
# ============================================================

def safe_remove(
    path
):

    try:

        if path and os.path.exists(
            path
        ):
            os.remove(
                path
            )

    except Exception:
        pass


# ============================================================
# MOG COMMAND
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.мог(?:\s+([^\s]+))?(?:\s+([^\s]+))?$"
    )
)
async def mog_handler(
    event
):

    args = event.pattern_match.groups()

    sender = await event.get_sender()

    if not args[0]:

        await event.reply(
            "⚔️ <b>Как использовать:</b>\n\n"
            "<code>.мог @user</code>\n"
            "Сравнить пользователя с тобой.\n\n"
            "<code>.мог @user1 @user2</code>\n"
            "Сравнить двух пользователей.",
            parse_mode="html"
        )

        return

    msg = await event.reply(
        "⚙️ <b>Mog Judge запускается...</b>",
        parse_mode="html"
    )

    u1 = None
    u2 = None
    card_path = None

    try:

        # ----------------------------------------------------
        # USER 1
        # ----------------------------------------------------

        u1 = await get_user_data(
            args[0]
        )

        # ----------------------------------------------------
        # USER 2
        # ----------------------------------------------------

        if args[1]:

            second_target = args[1]

        else:

            if sender.username:

                second_target = (
                    "@"
                    + sender.username
                )

            else:

                second_target = sender.id

        u2 = await get_user_data(
            second_target
        )

        if not u1:

            await msg.edit(
                "❌ Не удалось найти первого пользователя."
            )

            return

        if not u2:

            await msg.edit(
                "❌ Не удалось найти второго пользователя."
            )

            return

        await msg.edit(
            "🔍 <b>Смотрю аватарки, bio, OG "
            "и активность...</b>",
            parse_mode="html"
        )

        # ----------------------------------------------------
        # PROMPT
        # ----------------------------------------------------

        prompt = f"""
{SYSTEM_PROMPT}

================ USER 1 ================

username:
{u1["username"]}

name:
{u1["name"]}

telegram_id:
{u1["id"]}

bio:
{u1["bio"]}

stories:
{u1["has_stories"]}

================ USER 2 ================

username:
{u2["username"]}

name:
{u2["name"]}

telegram_id:
{u2["id"]}

bio:
{u2["bio"]}

stories:
{u2["has_stories"]}

================ END ====================
"""

        content = [
            prompt
        ]

        # ----------------------------------------------------
        # PHOTO 1
        # ----------------------------------------------------

        if u1["photo_path"]:

            try:

                content.append(
                    Image.open(
                        u1["photo_path"]
                    )
                )

            except:

                content.append(
                    "USER 1 PHOTO UNAVAILABLE"
                )

        else:

            content.append(
                "USER 1 HAS NO PHOTO"
            )

        # ----------------------------------------------------
        # PHOTO 2
        # ----------------------------------------------------

        if u2["photo_path"]:

            try:

                content.append(
                    Image.open(
                        u2["photo_path"]
                    )
                )

            except:

                content.append(
                    "USER 2 PHOTO UNAVAILABLE"
                )

        else:

            content.append(
                "USER 2 HAS NO PHOTO"
            )

        # ----------------------------------------------------
        # GEMINI
        # ----------------------------------------------------

        response = await asyncio.to_thread(
            model.generate_content,
            content
        )

        data = parse_ai_json(
            response.text
        )

        data = normalize_mog_data(
            data
        )

        # ----------------------------------------------------
        # CARD
        # ----------------------------------------------------

        card_path = generate_mog_card(
            data,
            u1["photo_path"],
            u2["photo_path"]
        )

        winner_name = (
            data["u1"]["username"]
            if data["winner"] == 1
            else data["u2"]["username"]
        )

        winner_score = (
            data["u1"]["overall"]
            if data["winner"] == 1
            else data["u2"]["overall"]
        )

        winner_rank = get_rank(
            winner_score
        )

        # ----------------------------------------------------
        # SEND CARD
        # ----------------------------------------------------

        await client.send_file(
            event.chat_id,
            card_path,
            caption=(
                f"👑 <b>@{winner_name} ПОБЕЖДАЕТ!</b>\n\n"
                f"🏆 Rank: <b>{winner_rank}</b>\n"
                f"📊 Score: <b>{winner_score:.1f}/10</b>\n"
                f"⚔️ Gap: <b>{data['gap']:.2f}</b>\n\n"
                f"💡 {data.get('explanation', '')}"
            ),
            parse_mode="html"
        )

        # ----------------------------------------------------
        # SEND FULL ANALYSIS
        # ----------------------------------------------------

        await event.reply(
            format_battle_result(
                data
            ),
            parse_mode="html"
        )

        await msg.delete()

    except Exception as e:

        print(
            "MOG ERROR:",
            repr(e)
        )

        try:

            await msg.edit(
                "❌ <b>Ошибка при анализе.</b>\n\n"
                f"<code>{str(e)[:1000]}</code>",
                parse_mode="html"
            )

        except:
            pass

    finally:

        # cleanup
        if card_path:
            safe_remove(
                card_path
            )

        if u1:
            safe_remove(
                u1.get(
                    "photo_path"
                )
            )

        if u2:
            safe_remove(
                u2.get(
                    "photo_path"
                )
            )


# ============================================================
# HELP COMMAND
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.хелп(?:\s+([^\s]+))?$"
    )
)
async def help_handler(
    event
):

    args = event.pattern_match.groups()

    sender = await event.get_sender()

    if args[0]:

        target = args[0]

    else:

        if sender.username:

            target = (
                "@"
                + sender.username
            )

        else:

            target = sender.id

    msg = await event.reply(
        "🧠 <b>Profile Coach анализирует профиль...</b>",
        parse_mode="html"
    )

    user = None

    try:

        user = await get_user_data(
            target
        )

        if not user:

            await msg.edit(
                "❌ Не удалось получить профиль."
            )

            return

        prompt = f"""
{HELP_PROMPT}

================ PROFILE ================

username:
{user["username"]}

name:
{user["name"]}

telegram_id:
{user["id"]}

bio:
{user["bio"]}

stories:
{user["has_stories"]}

==========================================
"""

        content = [
            prompt
        ]

        if user["photo_path"]:

            try:

                content.append(
                    Image.open(
                        user["photo_path"]
                    )
                )

            except:

                content.append(
                    "PHOTO UNAVAILABLE"
                )

        else:

            content.append(
                "NO PHOTO"
            )

        response = await asyncio.to_thread(
            model.generate_content,
            content
        )

        data = parse_ai_json(
            response.text
        )

        # ----------------------------------------------------
        # NORMALIZE HELP SCORE
        # ----------------------------------------------------

        try:

            score = float(
                data.get(
                    "score",
                    0
                )
            )

        except:

            score = 0

        score = max(
            0,
            min(
                10,
                score
            )
        )

        rank = get_rank(
            score
        )

        result = []

        result.append(
            "🧠 <b>PROFILE COACH</b>"
        )

        result.append("")

        result.append(
            f"👤 <b>@{user['username']}</b>"
        )

        result.append(
            f"🎯 <b>{score:.1f}/10</b> "
            f"• {rank_emoji(rank)} <b>{rank}</b>"
        )

        result.append("")

        result.append(
            f"💬 <b>Диагноз:</b>\n"
            f"{data.get('verdict', '-')}"
        )

        result.append(
            "\n━━━━━━━━━━━━━━━━━━\n"
        )

        # ----------------------------------------------------
        # AVATAR
        # ----------------------------------------------------

        avatar = data.get(
            "avatar",
            {}
        )

        result.append(
            f"🖼 <b>Аватар — "
            f"{float(avatar.get('score', 0)):.1f}/10</b>"
        )

        result.append(
            f"└ {avatar.get('analysis', '-')}"
        )

        result.append(
            f"💡 <i>{avatar.get('advice', '-')}</i>"
        )

        result.append("")

        # ----------------------------------------------------
        # USERNAME
        # ----------------------------------------------------

        username = data.get(
            "username",
            {}
        )

        result.append(
            f"🔤 <b>Username — "
            f"{float(username.get('score', 0)):.1f}/10</b>"
        )

        result.append(
            f"└ {username.get('analysis', '-')}"
        )

        result.append(
            f"💡 <i>{username.get('advice', '-')}</i>"
        )

        result.append("")

        # ----------------------------------------------------
        # BIO
        # ----------------------------------------------------

        bio = data.get(
            "bio",
            {}
        )

        result.append(
            f"📝 <b>Bio — "
            f"{float(bio.get('score', 0)):.1f}/10</b>"
        )

        result.append(
            f"└ {bio.get('analysis', '-')}"
        )

        result.append(
            f"💡 <i>{bio.get('advice', '-')}</i>"
        )

        result.append("")

        # ----------------------------------------------------
        # ACTIVITY
        # ----------------------------------------------------

        activity = data.get(
            "activity",
            {}
        )

        result.append(
            f"⚡ <b>Активность — "
            f"{float(activity.get('score', 0)):.1f}/10</b>"
        )

        result.append(
            f"└ {activity.get('analysis', '-')}"
        )

        result.append(
            f"💡 <i>{activity.get('advice', '-')}</i>"
        )

        result.append("")

        # ----------------------------------------------------
        # STYLE
        # ----------------------------------------------------

        style = data.get(
            "style",
            {}
        )

        result.append(
            f"🎨 <b>Style — "
            f"{float(style.get('score', 0)):.1f}/10</b>"
        )

        result.append(
            f"└ {style.get('analysis', '-')}"
        )

        result.append(
            f"💡 <i>{style.get('advice', '-')}</i>"
        )

        result.append("")

        # ----------------------------------------------------
        # TOP 3
        # ----------------------------------------------------

        result.append(
            "━━━━━━━━━━━━━━━━━━"
        )

        result.append(
            "🔥 <b>ТОП-3 ДЕЙСТВИЯ</b>"
        )

        top_3 = data.get(
            "top_3",
            []
        )

        for index, tip in enumerate(
            top_3[:3],
            1
        ):

            result.append(
                f"{index}. {tip}"
            )

        result.append("")

        result.append(
            f"👑 <b>Как стать True Adam:</b>\n"
            f"{data.get('final_advice', '-')}"
        )

        await event.reply(
            "\n".join(result),
            parse_mode="html"
        )

        await msg.delete()

    except Exception as e:

        print(
            "HELP ERROR:",
            repr(e)
        )

        try:

            await msg.edit(
                "❌ <b>Profile Coach не смог "
                "проанализировать профиль.</b>\n\n"
                f"<code>{str(e)[:1000]}</code>",
                parse_mode="html"
            )

        except:
            pass

    finally:

        if user:

            safe_remove(
                user.get(
                    "photo_path"
                )
            )


# ============================================================
# COMMANDS
# ============================================================

@client.on(
    events.NewMessage(
        pattern=r"^\.команды$"
    )
)
async def commands_handler(
    event
):

    await event.reply(
        """
🤖 <b>MOG BOT</b>

⚔️ <code>.мог @user</code>
Сравнить пользователя с тобой.

⚔️ <code>.мог @user1 @user2</code>
Сравнить двух пользователей.

🧠 <code>.хелп</code>
AI-разбор твоего профиля.

🧠 <code>.хелп @user</code>
AI-разбор чужого профиля.

📖 <code>.команды</code>
Список команд.

━━━━━━━━━━━━━━━━━━

🏆 <b>RANK SYSTEM</b>

🔴 Sub-3 — 0.0–2.9
🟠 Sub-5 — 3.0–4.9
🟡 LTN — 5.0–5.9
🔵 MTN — 6.0–6.9
🟢 HTN — 7.0–7.9
🔥 Chad — 8.0–8.9
👑 True Adam — 9.0–10
""",
        parse_mode="html"
    )


# ============================================================
# START
# ============================================================

print(
    "================================="
)

print(
    "🤖 MOG BOT STARTED"
)

print(
    "🏆 Sub-3 → Sub-5 → LTN → MTN → HTN → Chad → True Adam"
)

print(
    "================================="
)

client.run_until_disconnected()
