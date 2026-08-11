import os
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest
import google.generativeai as genai
from PIL import Image, ImageDraw, ImageFont, ImageOps

# 1. Чтение токенов из переменных окружения
API_ID = int(os.environ.get("API_ID", 0) or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PORT = int(os.environ.get("PORT", 8080))

# 2. Настройка фонового веб-сервера для хостинга Render
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# 3. Настройка Gemini API (температура 0.0 для стабильности оценок)
genai.configure(api_key=GEMINI_API_KEY)
generation_config = {"temperature": 0.0}
model = genai.GenerativeModel('gemini-3.5-flash-lite', generation_config=generation_config)

# 4. Инициализация Telethon
client = TelegramClient('mog_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

SYSTEM_PROMPT = """Ты — ироничный, но строгий эксперт по оценке профилей в Telegram (Mogging judge).
Сравни двух пользователей по 10-балльной шкале (с точностью до сотых, например: 6.62, 5.18, 3.97).

При оценке юзернейма учитывай как его ДЛИНУ (короткие лучше), так и СМЫСЛ/красоту слова.

Верни результат СТРОГО в формате JSON без кавычек ```json!

Формат JSON:
{
  "winner": 1,
  "gap": 0.03,
  "explanation": "1-2 предложения общего вердикта, почему победил именно он.",
  "u1": {
    "username": "имя_юзера1",
    "overall": 3.97,
    "rank": "LTN",
    "avatar_score": 6.62,
    "avatar_text": "вайб и качество",
    "username_score": 5.18,
    "username_text": "длина и смысл",
    "bio_score": 2.34,
    "bio_text": "лаконичность текста",
    "gifts_score": 2.21,
    "gifts_text": "наличие подарков"
  },
  "u2": {
    "username": "имя_юзера2",
    "overall": 3.94,
    "rank": "LTN",
    "avatar_score": 4.40,
    "avatar_text": "вайб и качество",
    "username_score": 8.80,
    "username_text": "длина и смысл",
    "bio_score": 3.80,
    "bio_text": "лаконичность текста",
    "gifts_score": 1.20,
    "gifts_text": "наличие подарков"
  }
}
"""

def get_font(size):
    """Безопасная загрузка шрифтов для отрисовки"""
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except:
        try:
            return ImageFont.truetype("arial.ttf", size)
        except:
            return ImageFont.load_default()

def draw_round_avatar(img_path, size=(80, 80)):
    """Создает круглую аватарку"""
    if img_path and os.path.exists(img_path):
        try:
            img = Image.open(img_path).convert("RGBA")
            img = ImageOps.fit(img, size, Image.Resampling.LANCZOS)
            mask = Image.new('L', size, 0)
            draw_mask = ImageDraw.Draw(mask)
            draw_mask.ellipse((0, 0) + size, fill=255)
            output = Image.new('RGBA', size, (0, 0, 0, 0))
            output.paste(img, (0, 0), mask)
            return output
        except Exception as e:
            print(f"Ошибка обработки фото: {e}")
    
    # Заглушка, если нет фото
    placeholder = Image.new('RGBA', size, (60, 60, 60, 255))
    draw = ImageDraw.Draw(placeholder)
    draw.ellipse((0, 0) + size, fill=(100, 100, 100, 255))
    return placeholder

def draw_crown(draw, cx, cy):
    """Отрисовка золотой короны с рубинами над аватаркой победителя"""
    crown_points = [
        (cx - 22, cy + 8),   # левый нижний угол
        (cx + 22, cy + 8),   # правый нижний угол
        (cx + 24, cy - 6),   # правый пик
        (cx + 12, cy + 2),   # правая впадина
        (cx, cy - 14),       # центральный пик (самый высокий)
        (cx - 12, cy + 2),   # левая впадина
        (cx - 24, cy - 6),   # левый пик
    ]
    # Тело короны
    draw.polygon(crown_points, fill=(255, 204, 0, 255), outline=(200, 150, 0, 255))
    # Нижнее ободок-основание
    draw.rectangle([cx - 22, cy + 8, cx + 22, cy + 13], fill=(255, 170, 0, 255))
    
    # Рубины на верхушках короны
    for px, py in [(cx - 24, cy - 6), (cx, cy - 14), (cx + 24, cy - 6)]:
        draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=(220, 20, 60, 255))

def generate_mog_card(data, u1_photo, u2_photo, output_path="mog_card.png"):
    """Генерация графической карточки MOG BATTLE"""
    width, height = 500, 920
    card = Image.new("RGBA", (width, height), (18, 18, 18, 255))
    draw = ImageDraw.Draw(card)

    font_title = get_font(22)
    font_bold = get_font(18)
    font_small = get_font(12)

    # Заголовок
    draw.text((width // 2, 25), "MOG BATTLE", fill="#FFCC00", font=font_title, anchor="mm")

    def draw_player_card(y_offset, p_data, photo_path, is_loser):
        # Фоновая подложка игрока
        draw.rounded_rectangle([20, y_offset, width - 20, y_offset + 370], radius=15, fill=(28, 28, 28, 255))
        
        # Аватарка
        av_x = width // 2 - 40
        av_y = y_offset + 18
        av = draw_round_avatar(photo_path, size=(80, 80))
        card.paste(av, (av_x, av_y), av)

        # Корона для ПОБЕДИТЕЛЯ
        if not is_loser:
            draw_crown(draw, width // 2, av_y - 2)

        # Штамп MOGGED для ПРОИГРАВШЕГО
        if is_loser:
            stamp_w, stamp_h = 110, 36
            stamp_bg = Image.new("RGBA", (stamp_w, stamp_h), (0, 0, 0, 0))
            s_draw = ImageDraw.Draw(stamp_bg)
            
            s_draw.rounded_rectangle([0, 0, stamp_w, stamp_h], radius=6, fill=(220, 20, 60, 235))
            
            s_font = get_font(18)
            s_draw.text((stamp_w // 2, stamp_h // 2), "MOGGED", fill="#FFFFFF", font=s_font, 
                        stroke_width=1, stroke_fill="#000000", anchor="mm")

            rotated_stamp = stamp_bg.rotate(-12, expand=True, resample=Image.Resampling.BICUBIC)
            rw, rh = rotated_stamp.size
            card.paste(rotated_stamp, (width // 2 - rw // 2, y_offset + 58 - rh // 2), rotated_stamp)

        # Юзернейм и Ранг
        draw.text((width // 2, y_offset + 115), f"@{p_data['username']}", fill="#FFFFFF", font=font_bold, anchor="mm")
        draw.text((width // 2, y_offset + 135), f"Rank: {p_data['rank']} | Overall: {p_data['overall']:.2f}", fill="#FFCC00", font=font_small, anchor="mm")

        # Прогресс-бары
        categories = [
            ("Аватар", p_data.get("avatar_score", 0)),
            ("Юзернейм", p_data.get("username_score", 0)),
            ("О себе (Bio)", p_data.get("bio_score", 0)),
            ("Подарки", p_data.get("gifts_score", 0))
        ]

        bar_y = y_offset + 160
        for label, score in categories:
            draw.text((40, bar_y), label, fill="#AAAAAA", font=font_small)
            draw.text((width - 40, bar_y), f"{score:.2f}", fill="#FFFFFF", font=font_small, anchor="ra")
            
            bar_x_start, bar_x_end = 40, width - 40
            bar_width = bar_x_end - bar_x_start
            draw.rounded_rectangle([bar_x_start, bar_y + 18, bar_x_end, bar_y + 24], radius=3, fill=(45, 45, 45, 255))
            
            fill_width = bar_x_start + int((score / 10.0) * bar_width)
            if fill_width > bar_x_start:
                draw.rounded_rectangle([bar_x_start, bar_y + 18, fill_width, bar_y + 24], radius=3, fill=(255, 204, 0, 255))
            
            bar_y += 45

    # Игрок 1
    p1_is_loser = (data.get("winner") == 2)
    draw_player_card(60, data["u1"], u1_photo, p1_is_loser)

    # Разделитель VS
    draw.text((width // 2, 462), "VS", fill="#FFFFFF", font=get_font(24), anchor="mm")

    # Игрок 2
    p2_is_loser = (data.get("winner") == 1)
    draw_player_card(490, data["u2"], u2_photo, p2_is_loser)

    # Нижняя плашка
    gap = data.get("gap", 0.0)
    draw.text((width // 2, 880), f"Разрыв: {gap:.2f} балла", fill="#FFCC00", font=font_bold, anchor="mm")

    card.save(output_path)
    return output_path

async def get_user_data(username_or_id):
    """Сбор информации о профиле пользователя"""
    try:
        entity = await client.get_entity(username_or_id)
        full_user = await client(GetFullUserRequest(entity))
        photo_path = await client.download_profile_photo(entity, file=f"avatar_{entity.id}.jpg")
        
        bio = full_user.full_user.about or "Отсутствует"
        username_str = entity.username or "Нет юзернейма"
        username_len = len(username_str) if entity.username else 99
        gifts_count = getattr(full_user.full_user, 'stargifts_count', 0)
        
        return {
            "name": entity.first_name or "Без имени",
            "username": username_str,
            "len": username_len,
            "bio": bio,
            "gifts": gifts_count,
            "photo_path": photo_path,
            "id": entity.id
        }
    except Exception as e:
        print(f"Ошибка получения данных: {e}")
        return None

@client.on(events.NewMessage(pattern=r'^\.мог(?:\s+([^\s]+))?(?:\s+([^\s]+))?'))
async def mog_handler(event):
    args = event.pattern_match.groups()
    sender = await event.get_sender()
    
    if args[0] and args[1]:
        target1_name, target2_name = args[0], args[1]
    elif args[0]:
        target1_name = f"@{sender.username}" if sender.username else sender.id
        target2_name = args[0]
    else:
        await event.reply("Укажите пользователей: `.мог @username` или `.мог @user1 @user2`")
        return

    msg = await event.reply("⚙️ Генерирую MOG BATTLE карточку...")

    user1 = await get_user_data(target1_name)
    user2 = await get_user_data(target2_name)

    if not user1 or not user2:
        await msg.edit("❌ Ошибка при сборе данных пользователей.")
        return

    prompt_text = f"{SYSTEM_PROMPT}\n\n"
    prompt_text += f"Игрок 1: @{user1['username']}\n- Имя: {user1['name']}\n- Bio: {user1['bio']}\n- Подарки: {user1['gifts']}\n\n"
    prompt_text += f"Игрок 2: @{user2['username']}\n- Имя: {user2['name']}\n- Bio: {user2['bio']}\n- Подарки: {user2['gifts']}\n\nУчитывай фото."

    contents = [prompt_text]
    opened_files = []

    try:
        for i, u in enumerate([user1, user2]):
            if u['photo_path'] and os.path.exists(u['photo_path']):
                img = Image.open(u['photo_path'])
                contents.append(f"Фото Игрока {i+1}:")
                contents.append(img)
                opened_files.append(u['photo_path'])

        response = model.generate_content(contents)
        clean_json_str = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json_str)

        card_file = generate_mog_card(data, user1['photo_path'], user2['photo_path'])

        winner_tag = f"@{data['u1']['username']}" if data['winner'] == 1 else f"@{data['u2']['username']}"
        loser_tag = f"@{data['u2']['username']}" if data['winner'] == 1 else f"@{data['u1']['username']}"

        caption_text = f"🔥 {winner_tag} могнул {loser_tag}!\n\n"
        caption_text += f"📊 @{data['u1']['username']} ({data['u1']['overall']:.2f} — {data['u1']['rank']}):\n"
        caption_text += f"• Аватарка: {data['u1']['avatar_score']:.2f} — {data['u1']['avatar_text']}\n"
        caption_text += f"• Юзернейм: {data['u1']['username_score']:.2f} — {data['u1']['username_text']}\n"
        caption_text += f"• Bio: {data['u1']['bio_score']:.2f} — {data['u1']['bio_text']}\n\n"

        caption_text += f"📊 @{data['u2']['username']} ({data['u2']['overall']:.2f} — {data['u2']['rank']}):\n"
        caption_text += f"• Аватарка: {data['u2']['avatar_score']:.2f} — {data['u2']['avatar_text']}\n"
        caption_text += f"• Юзернейм: {data['u2']['username_score']:.2f} — {data['u2']['username_text']}\n"
        caption_text += f"• Bio: {data['u2']['bio_score']:.2f} — {data['u2']['bio_text']}\n\n"

        caption_text += f"💡 Вердикт: {data.get('explanation', '')}"

        await client.send_file(event.chat_id, card_file, caption=caption_text)
        await msg.delete()

    except Exception as e:
        await msg.edit(f"❌ Ошибка генерации: {e}")
    finally:
        for path in opened_files + ["mog_card.png"]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass

if __name__ == '__main__':
    print(f"✅ Сервер запущен на порту {PORT}")
    client.run_until_disconnected()
