import os
import json
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest

from google import genai
from google.genai import types

from PIL import Image, ImageDraw, ImageFont, ImageOps


# =========================================================
# НАСТРОЙКИ
# =========================================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

PORT = int(os.getenv("PORT", "8080"))

MODEL = "gemini-3.5-flash"

DATA_FILE = "ratings.json"


# =========================================================
# HEALTH CHECK
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def health_server():
    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    server.serve_forever()


threading.Thread(
    target=health_server,
    daemon=True
).start()


# =========================================================
# GEMINI
# =========================================================

if not GEMINI_API_KEY:
    raise RuntimeError("Не указан GEMINI_API_KEY")

ai = genai.Client(
    api_key=GEMINI_API_KEY
)


# =========================================================
# TELEGRAM
# =========================================================

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError(
        "Нужно указать API_ID, API_HASH и BOT_TOKEN"
    )


client = TelegramClient(
    "mog_bot_session",
    API_ID,
    API_HASH
)


# =========================================================
# РЕЙТИНГИ
# =========================================================

RANKS = [
    ("Sub-3", 0.0),
    ("Sub-5", 3.0),
    ("LTN", 5.0),
    ("MTN", 6.0),
    ("HTN", 7.0),
    ("Chad", 8.0),
    ("True Adam", 9.0),
]


def get_rank(score):
    result = "Sub-3"

    for name, minimum in RANKS:
        if score >= minimum:
            result = name

    return result


# =========================================================
# СОХРАНЕНИЕ РЕЙТИНГОВ
# =========================================================

def load_ratings():

    if not os.path.exists(DATA_FILE):
        return {}

    try:
        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception:
        return {}


ratings = load_ratings()


def save_ratings():

    with open(
        DATA_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            ratings,
            f,
            ensure_ascii=False,
            indent=2
        )


# =========================================================
# ШРИФТЫ
# =========================================================

def font(size):

    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]

    for path in paths:

        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except:
                pass

    return ImageFont.load_default()


# =========================================================
# АВАТАР
# =========================================================

def avatar(path, size=100):

    if path and os.path.exists(path):

        try:

            image = Image.open(path).convert("RGBA")

            image = ImageOps.fit(
                image,
                (size, size),
                Image.Resampling.LANCZOS
            )

            mask = Image.new(
                "L",
                (size, size),
                0
            )

            d = ImageDraw.Draw(mask)

            d.ellipse(
                (0, 0, size, size),
                fill=255
            )

            result = Image.new(
                "RGBA",
                (size, size),
                (0, 0, 0, 0)
            )

            result.paste(
                image,
                (0, 0),
                mask
            )

            return result

        except:
            pass

    result = Image.new(
        "RGBA",
        (size, size),
        (60, 60, 60, 255)
    )

    d = ImageDraw.Draw(result)

    d.ellipse(
        (0, 0, size, size),
        fill=(90, 90, 90, 255)
    )

    return result


# =========================================================
# MOGGED НА АВАТАРКЕ
# =========================================================

def add_mogged(image):

    width, height = image.size

    stamp = Image.new(
        "RGBA",
        (180, 55),
        (0, 0, 0, 0)
    )

    d = ImageDraw.Draw(stamp)

    d.text(
        (90, 27),
        "MOGGED",
        fill=(230, 30, 50, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
        font=font(25),
        anchor="mm"
    )

    # Наклон ровно примерно 12 градусов
    stamp = stamp.rotate(
        12,
        expand=True,
        resample=Image.Resampling.BICUBIC
    )

    x = width // 2 - stamp.width // 2
    y = height // 2 - stamp.height // 2

    image.alpha_composite(
        stamp,
        (x, y)
    )

    return image


# =========================================================
# ПРОГРЕСС-БАР
# =========================================================

def bar(draw, x, y, width, score):

    height = 12

    draw.rounded_rectangle(
        [
            x,
            y,
            x + width,
            y + height
        ],
        radius=6,
        fill=(55, 55, 60)
    )

    filled = int(
        width * max(0, min(10, score)) / 10
    )

    if filled > 0:

        draw.rounded_rectangle(
            [
                x,
                y,
                x + filled,
                y + height
            ],
            radius=6,
            fill=(255, 204, 0)
        )


# =========================================================
# КАРТОЧКА
# =========================================================

def make_card(
    u1,
    u2,
    winner,
    gap,
    output="mog_card.png"
):

    WIDTH = 700

    PLAYER_HEIGHT = 430

    HEIGHT = 1000

    image = Image.new(
        "RGB",
        (WIDTH, HEIGHT),
        (14, 14, 17)
    )

    draw = ImageDraw.Draw(image)

    # -----------------------------------------------------
    # HEADER
    # -----------------------------------------------------

    draw.text(
        (WIDTH // 2, 35),
        "MOGGING BATTLE",
        fill=(255, 204, 0),
        font=font(34),
        anchor="ma"
    )

    draw.text(
        (WIDTH // 2, 78),
        "КТО ИМЕЕТ БОЛЕЕ СИЛЬНЫЙ ПРОФИЛЬ?",
        fill=(150, 150, 155),
        font=font(17),
        anchor="ma"
    )

    # -----------------------------------------------------
    # ИГРОК
    # -----------------------------------------------------

    def player_card(data, y, loser):

        draw.rounded_rectangle(
            (
                25,
                y,
                WIDTH - 25,
                y + PLAYER_HEIGHT
            ),
            radius=25,
            fill=(25, 25, 29),
            outline=(
                (255, 204, 0)
                if not loser
                else (70, 70, 75)
            ),
            width=3
        )

        # Аватар

        av = avatar(
            data.get("photo"),
            120
        )

        if loser:
            av = add_mogged(av)

        image.paste(
            av,
            (
                WIDTH // 2 - 60,
                y + 25
            ),
            av
        )

        # username

        username = data.get(
            "username",
            "unknown"
        )

        draw.text(
            (
                WIDTH // 2,
                y + 165
            ),
            f"@{username}",
            fill="white",
            font=font(25),
            anchor="ma"
        )

        # SCORE

        draw.text(
            (
                WIDTH // 2,
                y + 210
            ),
            f"{data['overall']:.1f}",
            fill=(255, 204, 0),
            font=font(50),
            anchor="ma"
        )

        draw.text(
            (
                WIDTH // 2,
                y + 270
            ),
            data["rank"],
            fill="white",
            font=font(24),
            anchor="ma"
        )

        # Характеристики

        categories = [
            ("Аватар", data["avatar_score"]),
            ("OG статус", data["og_score"]),
            ("Bio / стиль", data["bio_score"]),
            ("Активность", data["activity_score"]),
        ]

        current_y = y + 315

        for label, score in categories:

            draw.text(
                (55, current_y),
                label,
                fill=(185, 185, 190),
                font=font(16)
            )

            draw.text(
                (
                    WIDTH - 55,
                    current_y
                ),
                f"{score:.1f}",
                fill="white",
                font=font(16),
                anchor="ra"
            )

            bar(
                draw,
                55,
                current_y + 25,
                WIDTH - 110,
                score
            )

            current_y += 48

    # -----------------------------------------------------
    # ДВА ИГРОКА
    # -----------------------------------------------------

    player_card(
        u1,
        115,
        winner != 1
    )

    player_card(
        u2,
        555,
        winner != 2
    )

    # -----------------------------------------------------
    # VS
    # -----------------------------------------------------

    draw.text(
        (
            WIDTH // 2,
            535
        ),
        "VS",
        fill="white",
        font=font(28),
        anchor="mm"
    )

    # -----------------------------------------------------
    # ИТОГ
    # -----------------------------------------------------

    winner_name = (
        u1["username"]
        if winner == 1
        else u2["username"]
    )

    draw.rounded_rectangle(
        (
            80,
            930,
            WIDTH - 80,
            980
        ),
        radius=20,
        fill=(25, 25, 29)
    )

    draw.text(
        (
            WIDTH // 2,
            950
        ),
        f"Победитель: @{winner_name}  •  Разрыв: {gap:.1f}",
        fill=(255, 204, 0),
        font=font(17),
        anchor="mm"
    )

    image.save(
        output,
        quality=95
    )

    return output


# =========================================================
# ПОЛУЧЕНИЕ ДАННЫХ TELEGRAM
# =========================================================

async def get_user(username):

    try:

        username = username.strip()

        if username.startswith("@"):
            username = username[1:]

        entity = await client.get_entity(username)

        full = await client(
            GetFullUserRequest(entity)
        )

        photo_path = None

        try:

            photo_path = await client.download_profile_photo(
                entity,
                file=f"avatar_{entity.id}.jpg"
            )

        except:
            pass

        username_real = (
            entity.username
            or str(entity.id)
        )

        bio = (
            full.full_user.about
            or ""
        )

        # Сторис не обязательны.
        # Если Telegram не позволяет получить их,
        # просто считаем активность по доступным данным.

        has_stories = False

        try:

            stories = await client.get_stories(
                entity
            )

            has_stories = bool(
                stories
                and stories.stories
            )

        except:
            pass

        return {
            "id": entity.id,
            "username": username_real,
            "name": (
                entity.first_name
                or ""
            ),
            "bio": bio,
            "photo": photo_path,
            "has_stories": has_stories
        }

    except Exception as e:

        print(
            "Ошибка получения пользователя:",
            e
        )

        return None


# =========================================================
# ОТПРАВКА В GEMINI
# =========================================================

async def ask_ai(prompt, images=None):

    contents = [prompt]

    if images:

        for img_path in images:

            if img_path and os.path.exists(img_path):

                try:

                    img = Image.open(
                        img_path
                    )

                    contents.append(img)

                except:
                    pass

    response = await asyncio.to_thread(
        ai.models.generate_content,
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.15,
            max_output_tokens=2500,
            response_mime_type="application/json"
        )
    )

    return response.text


# =========================================================
# АНАЛИЗ
# =========================================================

async def analyze_user(user):

    old = ratings.get(
        str(user["id"])
    )

    prompt = f"""
Ты — профессиональный нейтральный судья рейтинга Telegram-профилей.

Твоя задача — оценивать профиль строго по предоставленным данным.

Никаких шуток.
Никакой симпатии.
Никаких субъективных предпочтений по личным вкусам.
Не завышай оценку только потому, что аватар красивый.
Не занижай оценку без причины.

Оцени четыре категории:

1. avatar_score — качество, целостность и уместность аватарки.
2. og_score — условный OG-статус на основании Telegram ID.
3. bio_score — качество bio, стиль и заполненность профиля.
4. activity_score — наличие признаков активности, включая сторис.

Каждая категория от 0 до 10.

Итоговая оценка — взвешенное среднее.

Ранги:

0-2.9 = Sub-3
3-4.9 = Sub-5
5-5.9 = LTN
6-6.9 = MTN
7-7.9 = HTN
8-8.9 = Chad
9-10 = True Adam

ВАЖНО:

Если предыдущая оценка существует, НЕ нужно автоматически сохранять её.

Если профиль изменился, новая оценка должна измениться.

Но если предоставленные данные практически такие же,
оценка должна оставаться близкой к предыдущей.

Предыдущая оценка:
{json.dumps(old, ensure_ascii=False) if old else "нет"}

Данные:

Username: {user["username"]}
Telegram ID: {user["id"]}
Bio: {user["bio"]}
Есть сторис: {user["has_stories"]}

Верни ТОЛЬКО JSON:

{{
  "overall": 6.0,
  "rank": "MTN",

  "avatar_score": 6.0,
  "avatar_reason": "Краткое объективное объяснение.",

  "og_score": 6.0,
  "og_reason": "Краткое объективное объяснение.",

  "bio_score": 6.0,
  "bio_reason": "Краткое объективное объяснение.",

  "activity_score": 6.0,
  "activity_reason": "Краткое объективное объяснение.",

  "summary": "Общий объективный вывод.",

  "strength": "Главная сильная сторона.",
  "weakness": "Главный недостаток.",

  "advice": "Что конкретно улучшить."
}}
"""

    text = await ask_ai(
        prompt,
        [user.get("photo")]
    )

    try:

        result = json.loads(
            text.strip()
        )

    except Exception:

        text = (
            text
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )

        result = json.loads(text)

    result["username"] = user["username"]

    result["id"] = user["id"]

    result["photo"] = user.get("photo")

    # Защита от неправильных значений

    for key in [
        "overall",
        "avatar_score",
        "og_score",
        "bio_score",
        "activity_score"
    ]:

        try:
            result[key] = float(
                result[key]
            )

        except:
            result[key] = 0.0

        result[key] = max(
            0,
            min(
                10,
                result[key]
            )
        )

    result["overall"] = round(
        (
            result["avatar_score"]
            + result["og_score"]
            + result["bio_score"]
            + result["activity_score"]
        ) / 4,
        1
    )

    result["rank"] = get_rank(
        result["overall"]
    )

    # Сохраняем

    ratings[str(user["id"])] = result

    save_ratings()

    return result


# =========================================================
# РАЗБОР БИТВЫ
# =========================================================

async def battle_analysis(u1, u2):

    prompt = f"""
Ты — максимально серьезный и нейтральный судья профилей Telegram.

Сравни двух пользователей.

Пользователь 1:
{json.dumps(u1, ensure_ascii=False, indent=2)}

Пользователь 2:
{json.dumps(u2, ensure_ascii=False, indent=2)}

Твоя задача:

- сравнить каждую характеристику;
- объяснить, почему у каждого такая оценка;
- объяснить, кто лучше по каждой категории;
- определить победителя;
- объяснить разницу;
- не использовать оскорбления;
- не использовать мемный стиль;
- не придумывать факты.

Верни только JSON:

{{
    "winner": 1,
    "gap": 0.5,

    "explanation": "Почему победил именно этот пользователь.",

    "u1_comment": "Разбор пользователя 1.",

    "u2_comment": "Разбор пользователя 2.",

    "category_result": {{
        "avatar": "Сравнение аватарок.",
        "og": "Сравнение OG статуса.",
        "bio": "Сравнение Bio.",
        "activity": "Сравнение активности."
    }}
}}
"""

    text = await ask_ai(prompt)

    text = (
        text
        .replace("```json", "")
        .replace("```", "")
        .strip()
    )

    return json.loads(text)


# =========================================================
# ФОРМАТ РАЗБОРА
# =========================================================

def format_battle(u1, u2, battle):

    winner = (
        u1["username"]
        if battle["winner"] == 1
        else u2["username"]
    )

    lines = []

    lines.append(
        "⚖️ **РАЗБОР СУДЬИ**"
    )

    lines.append("")

    lines.append(
        f"🏆 **Победитель:** @{winner}"
    )

    lines.append(
        f"📊 **Разрыв:** {battle['gap']:.1f}"
    )

    lines.append("")

    lines.append(
        "👤 **@" + u1["username"] + "**"
    )

    lines.append(
        battle["u1_comment"]
    )

    lines.append("")

    lines.append(
        "👤 **@" + u2["username"] + "**"
    )

    lines.append(
        battle["u2_comment"]
    )

    lines.append("")

    lines.append("📋 **По категориям:**")

    lines.append(
        f"🖼 Аватар — {battle['category_result']['avatar']}"
    )

    lines.append(
        f"🆔 OG статус — {battle['category_result']['og']}"
    )

    lines.append(
        f"📝 Bio / стиль — {battle['category_result']['bio']}"
    )

    lines.append(
        f"⚡ Активность — {battle['category_result']['activity']}"
    )

    lines.append("")

    lines.append(
        "⚖️ **Итог:** " + battle["explanation"]
    )

    return "\n".join(lines)


# =========================================================
# .МОГ
# =========================================================

@client.on(
    events.NewMessage(
        pattern=r"(?i)^\.мог(?:\s+(\S+))?(?:\s+(\S+))?$"
    )
)
async def mog_handler(event):

    first = event.pattern_match.group(1)
    second = event.pattern_match.group(2)

    if not first:

        await event.reply(
            "Использование:\n\n"
            "`.мог @user`\n"
            "— сравнить пользователя с вами\n\n"
            "`.мог @user1 @user2`\n"
            "— сравнить двух пользователей"
        )

        return

    status = await event.reply(
        "⚖️ Судья анализирует профили..."
    )

    # Первый

    u1 = await get_user(first)

    if not u1:

        await status.edit(
            "❌ Не удалось найти первого пользователя."
        )

        return

    # Второй

    if second:

        u2 = await get_user(second)

    else:

        sender = await event.get_sender()

        if not sender.username:

            await status.edit(
                "❌ У вас нет username. "
                "Укажите второго пользователя."
            )

            return

        u2 = await get_user(
            sender.username
        )

    if not u2:

        await status.edit(
            "❌ Не удалось найти второго пользователя."
        )

        return

    try:

        # Анализируем оба профиля

        result1 = await analyze_user(u1)

        result2 = await analyze_user(u2)

        # Баттл

        battle = await battle_analysis(
            result1,
            result2
        )

        # Нормализуем winner

        winner = int(
            battle.get("winner", 1)
        )

        gap = abs(
            result1["overall"]
            - result2["overall"]
        )

        card_u1 = dict(result1)

        card_u2 = dict(result2)

        card_path = make_card(
            card_u1,
            card_u2,
            winner,
            gap
        )

        await status.delete()

        await client.send_file(
            event.chat_id,
            card_path,
            caption=(
                f"⚖️ **Итог оценки**\n\n"
                f"@{result1['username']} — "
                f"{result1['overall']:.1f} "
                f"({result1['rank']})\n"
                f"@{result2['username']} — "
                f"{result2['overall']:.1f} "
                f"({result2['rank']})"
            )
        )

        await event.reply(
            format_battle(
                result1,
                result2,
                battle
            )
        )

    except Exception as e:

        print("MOG ERROR:", e)

        await status.edit(
            "❌ Произошла ошибка при анализе."
        )


# =========================================================
# .ХЕЛП
# =========================================================

@client.on(
    events.NewMessage(
        pattern=r"(?i)^\.хелп(?:\s+(\S+))?$"
    )
)
async def help_handler(event):

    username = event.pattern_match.group(1)

    if not username:

        sender = await event.get_sender()

        if not sender.username:

            await event.reply(
                "Укажите пользователя:\n"
                "`.хелп @username`"
            )

            return

        username = sender.username

    status = await event.reply(
        "🔎 Анализирую профиль..."
    )

    user = await get_user(username)

    if not user:

        await status.edit(
            "❌ Пользователь не найден."
        )

        return

    try:

        result = await analyze_user(user)

        prompt = f"""
Ты — серьезный консультант по улучшению Telegram-профиля.

Профиль:
{json.dumps(result, ensure_ascii=False, indent=2)}

Составь короткий, конкретный план улучшения.

Не используй оскорбления.
Не обещай невозможного.
Не говори общими фразами.

Дай:

1. Что уже хорошо.
2. Что сильнее всего портит профиль.
3. Что изменить в аватарке.
4. Что изменить в Bio.
5. Что сделать для активности.
6. Как поднять рейтинг на следующий уровень.

Ранги:

Sub-3 → Sub-5 → LTN → MTN → HTN → Chad → True Adam

Ответ максимум 1200 символов.
"""

        advice = await ask_ai(prompt)

        await status.edit(
            f"🧠 **РАЗБОР ПРОФИЛЯ @{result['username']}**\n\n"
            f"📊 Оценка: **{result['overall']:.1f}/10**\n"
            f"🏷 Ранг: **{result['rank']}**\n\n"
            f"{advice}"
        )

    except Exception as e:

        print("HELP ERROR:", e)

        await status.edit(
            "❌ Не удалось получить совет."
        )


# =========================================================
# .ТОП
# =========================================================

def sorted_ratings():

    users = list(
        ratings.values()
    )

    users.sort(
        key=lambda x: x.get(
            "overall",
            0
        ),
        reverse=True
    )

    return users


@client.on(
    events.NewMessage(
        pattern=r"(?i)^\.топ(?:\s+(все|чата))?$"
    )
)
async def top_handler(event):

    mode = (
        event.pattern_match.group(1)
        or "чата"
    )

    all_users = sorted_ratings()

    # -----------------------------------------------------
    # ТОП ВСЕ
    # -----------------------------------------------------

    if mode.lower() == "все":

        users = all_users[:10]

    # -----------------------------------------------------
    # ТОП ЧАТА
    # -----------------------------------------------------

    else:

        users = []

        try:

            participants = await client.get_participants(
                event.chat_id,
                limit=500
            )

            ids = {
                str(user.id)
                for user in participants
            }

            for user in all_users:

                if str(user.get("id")) in ids:

                    users.append(user)

                if len(users) >= 10:
                    break

        except Exception as e:

            print(
                "TOP CHAT ERROR:",
                e
            )

            users = all_users[:10]

    if not users:

        await event.reply(
            "📊 Пока рейтингов нет."
        )

        return

    lines = []

    if mode.lower() == "все":

        lines.append(
            "🏆 **ТОП MOGGING — ВСЕ**"
        )

    else:

        lines.append(
            "🏆 **ТОП MOGGING — ЧАТ**"
        )

    lines.append("")

    medals = [
        "🥇",
        "🥈",
        "🥉"
    ]

    for index, user in enumerate(users):

        medal = (
            medals[index]
            if index < 3
            else f"{index + 1}."
        )

        lines.append(
            f"{medal} "
            f"@{user.get('username', 'unknown')} — "
            f"**{user.get('overall', 0):.1f}** "
            f"({user.get('rank', 'Sub-3')})"
        )

    await event.reply(
        "\n".join(lines)
    )


# =========================================================
# .РАНГИ
# =========================================================

@client.on(
    events.NewMessage(
        pattern=r"(?i)^\.ранги$"
    )
)
async def ranks_handler(event):

    await event.reply(
        "🏷 **Система рейтингов**\n\n"
        "Sub-3 — 0.0–2.9\n"
        "Sub-5 — 3.0–4.9\n"
        "LTN — 5.0–5.9\n"
        "MTN — 6.0–6.9\n"
        "HTN — 7.0–7.9\n"
        "Chad — 8.0–8.9\n"
        "True Adam — 9.0–10.0"
    )


# =========================================================
# .СТАРТ / .ХЕЛП КОМАНД
# =========================================================

@client.on(
    events.NewMessage(
        pattern=r"(?i)^\.команды$"
    )
)
async def commands_handler(event):

    await event.reply(
        "🤖 **Команды бота**\n\n"
        "⚔️ `.мог @user`\n"
        "Сравнить пользователя с вами.\n\n"
        "⚔️ `.мог @user1 @user2`\n"
        "Сравнить двух пользователей.\n\n"
        "🧠 `.хелп @user`\n"
        "Получить рекомендации по профилю.\n\n"
        "🏆 `.топ`\n"
        "Топ текущего чата.\n\n"
        "🌍 `.топ все`\n"
        "Общий топ.\n\n"
        "📊 `.ранги`\n"
        "Список рангов."
    )


# =========================================================
# ЗАПУСК
# =========================================================

async def main():

    print("Bot starting...")

    await client.start(
        bot_token=BOT_TOKEN
    )

    me = await client.get_me()

    print(
        f"Bot started: @{me.username}"
    )

    print(
        f"Ratings loaded: {len(ratings)}"
    )

    await client.run_until_disconnected()


if __name__ == "__main__":

    asyncio.run(main())
