import os
import json
import threading
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest
import google.generativeai as genai
from PIL import Image, ImageDraw, ImageFont, ImageOps

# --- КОНФИГ ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PORT = int(os.environ.get("PORT", 8080))

# --- ВЕБ-СЕРВЕР (Health Check для Render) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, format, *args): return

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler); server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# --- ИНИЦИАЛИЗАЦИЯ ---
genai.configure(api_key=GEMINI_API_KEY)
# ВАЖНО: Используем актуальную модель, которая поддерживает generateContent
model = genai.GenerativeModel('gemini-3.5-flash-lite')
client = TelegramClient('mog_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

SYSTEM_PROMPT = """Ты — циничный судья Mogging Battle. Сравни двух пользователей (1 и 2) по 10-балльной шкале. Учитывай ID (OG статус), Аватарку, Bio и Наличие сторис (Активность).
Верни результат СТРОГО в формате JSON без кавычек ```json!
{
  "winner": 1,
  "gap": 1.5,
  "explanation": "Короткий вердикт",
  "u1": {
    "username": "user1", "overall": 7.5, "rank": "HTN",
    "avatar_score": 8, "avatar_text": "вайб",
    "og_score": 6, "og_text": "id",
    "bio_score": 7, "bio_text": "текст",
    "activity_score": 9, "activity_text": "актив"
  },
  "u2": {
    "username": "user2", "overall": 6.0, "rank": "LTN",
    "avatar_score": 5, "avatar_text": "вайб",
    "og_score": 8, "og_text": "id",
    "bio_score": 5, "bio_text": "текст",
    "activity_score": 3, "activity_text": "актив"
  }
}
"""

def get_font(size):
    try: return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except: return ImageFont.load_default()

def draw_round_avatar(img_path, size=(80, 80)):
    if img_path and os.path.exists(img_path):
        try:
            img = Image.open(img_path).convert("RGBA")
            img = ImageOps.fit(img, size, Image.Resampling.LANCZOS)
            mask = Image.new('L', size, 0); draw_mask = ImageDraw.Draw(mask); draw_mask.ellipse((0, 0) + size, fill=255)
            output = Image.new('RGBA', size, (0, 0, 0, 0)); output.paste(img, (0, 0), mask); return output
        except: pass
    placeholder = Image.new('RGBA', size, (60, 60, 60, 255)); draw = ImageDraw.Draw(placeholder); draw.ellipse((0, 0) + size, fill=(100, 100, 100, 255)); return placeholder

def draw_crown(draw, cx, cy):
    crown_points = [(cx - 22, cy + 8), (cx + 22, cy + 8), (cx + 24, cy - 6), (cx + 12, cy + 2), (cx, cy - 14), (cx - 12, cy + 2), (cx - 24, cy - 6)]
    draw.polygon(crown_points, fill=(255, 204, 0, 255), outline=(200, 150, 0, 255))
    draw.rectangle([cx - 22, cy + 8, cx + 22, cy + 13], fill=(255, 170, 0, 255))
    for px, py in [(cx - 24, cy - 6), (cx, cy - 14), (cx + 24, cy - 6)]: draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=(220, 20, 60, 255))

def generate_mog_card(data, u1_photo, u2_photo, output_path="mog_card.png"):
    width, height = 500, 950
    card = Image.new("RGBA", (width, height), (18, 18, 18, 255))
    draw = ImageDraw.Draw(card)

    font_title, font_bold, font_small = get_font(22), get_font(18), get_font(12)
    draw.text((width // 2, 25), "MOG BATTLE", fill="#FFCC00", font=font_title, anchor="mm")

    def draw_player_card(y_offset, p_data, photo_path, is_loser):
        draw.rounded_rectangle([20, y_offset, width - 20, y_offset + 370], radius=15, fill=(28, 28, 28, 255))
        av_x, av_y = width // 2 - 40, y_offset + 18
        av = draw_round_avatar(photo_path, size=(80, 80)); card.paste(av, (av_x, av_y), av)
        if not is_loser: draw_crown(draw, width // 2, av_y - 2)
        if is_loser:
            stamp_w, stamp_h = 110, 36; stamp_bg = Image.new("RGBA", (stamp_w, stamp_h), (0, 0, 0, 0))
            s_draw = ImageDraw.Draw(stamp_bg); s_draw.rounded_rectangle([0, 0, stamp_w, stamp_h], radius=6, fill=(220, 20, 60, 235))
            s_draw.text((stamp_w // 2, stamp_h // 2), "MOGGED", fill="#FFFFFF", font=get_font(18), stroke_width=1, stroke_fill="#000000", anchor="mm")
            rot = stamp_bg.rotate(-12, expand=True); rw, rh = rot.size; card.paste(rot, (width // 2 - rw // 2, y_offset + 58 - rh // 2), rot)
        draw.text((width // 2, y_offset + 115), f"@{p_data['username']}", fill="#FFFFFF", font=font_bold, anchor="mm")
        draw.text((width // 2, y_offset + 135), f"Rank: {p_data['rank']} | Overall: {p_data['overall']:.2f}", fill="#FFCC00", font=font_small, anchor="mm")
        categories = [("Аватар", p_data["avatar_score"]), ("OG Статус", p_data["og_score"]), ("Активность", p_data["activity_score"]), ("Bio/Style", p_data["bio_score"])]
        bar_y = y_offset + 160
        for label, score in categories:
            draw.text((40, bar_y), label, fill="#AAAAAA", font=font_small); draw.text((width - 40, bar_y), f"{score:.2f}", fill="#FFFFFF", font=font_small, anchor="ra")
            bar_x_start, bar_x_end = 40, width - 40; draw.rounded_rectangle([bar_x_start, bar_y + 18, bar_x_end, bar_y + 24], radius=3, fill=(45, 45, 45, 255))
            fill_width = bar_x_start + int((score / 10.0) * (bar_x_end - bar_x_start))
            if fill_width > bar_x_start: draw.rounded_rectangle([bar_x_start, bar_y + 18, fill_width, bar_y + 24], radius=3, fill=(255, 204, 0, 255))
            bar_y += 45

    p1_is_loser = (data.get("winner") == 2); draw_player_card(60, data["u1"], u1_photo, p1_is_loser)
    draw.text((width // 2, 462), "VS", fill="#FFFFFF", font=get_font(24), anchor="mm")
    p2_is_loser = (data.get("winner") == 1); draw_player_card(490, data["u2"], u2_photo, p2_is_loser)
    draw.text((width // 2, 880), f"Разрыв: {data.get('gap', 0.0):.2f} балла", fill="#FFCC00", font=font_bold, anchor="mm")
    card.save(output_path); return output_path

async def get_user_data(username_or_id):
    try:
        entity = await client.get_entity(username_or_id)
        full = await client(GetFullUserRequest(entity))
        photo_path = await client.download_profile_photo(entity, file=f"avatar_{entity.id}.jpg")
        try: stories = await client.get_stories(entity); has_stories = len(stories.stories) > 0
        except: has_stories = False
        return {"username": entity.username or "none", "name": entity.first_name or "", "bio": full.full_user.about or "", "photo_path": photo_path, "id": entity.id, "has_stories": has_stories}
    except: return None

@client.on(events.NewMessage(pattern=r'\.мог(?:\s+([^\s]+))?(?:\s+([^\s]+))?'))
async def mog_handler(event):
    args = event.pattern_match.groups(); sender = await event.get_sender()
    if not args[0]: await event.reply("Укажите пользователя: `.мог @user`"); return
    msg = await event.reply("⚙️ Анализ стиля...")
    u1, u2 = await get_user_data(args[0]), await get_user_data(args[1] if args[1] else f"@{sender.username}")
    if not u1 or not u2: await msg.edit("❌ Ошибка данных."); return
    prompt = f"{SYSTEM_PROMPT}\n\nU1: {u1['username']} (ID:{u1['id']}, Stories:{u1['has_stories']})\nU2: {u2['username']} (ID:{u2['id']}, Stories:{u2['has_stories']})\n\nОцени их по фото."
    res = model.generate_content([prompt, Image.open(u1['photo_path']) if u1['photo_path'] else "No Photo", Image.open(u2['photo_path']) if u2['photo_path'] else "No Photo"])
    data = json.loads(res.text.replace("```json", "").replace("```", "").strip())
    card_path = generate_mog_card(data, u1['photo_path'], u2['photo_path'])
    w_name = data['u1']['username'] if data['winner'] == 1 else data['u2']['username']
    await client.send_file(event.chat_id, card_path, caption=f"🔥 Победил @{w_name}!\n💡 {data['explanation']}")
    await msg.delete()

client.run_until_disconnected()
