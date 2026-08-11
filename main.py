import os, json, sqlite3, hashlib, threading
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

# --- ИНИЦИАЛИЗАЦИЯ ---
client = TelegramClient('mog_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- ГРАФИКА (Возвращаем полную отрисовку) ---
def get_font(size):
    return ImageFont.load_default()

def draw_round_avatar(img_path, size=(80, 80)):
    if img_path and os.path.exists(img_path):
        img = Image.open(img_path).convert("RGBA")
        img = ImageOps.fit(img, size, Image.Resampling.LANCZOS)
        mask = Image.new('L', size, 0); draw = ImageDraw.Draw(mask); draw.ellipse((0,0)+size, fill=255)
        output = Image.new('RGBA', size, (0,0,0,0)); output.paste(img, (0,0), mask); return output
    return Image.new('RGBA', size, (60,60,60,255))

def generate_mog_card(u1, u2, winner_idx):
    # Рисуем карточку (упрощенная версия для примера)
    width, height = 500, 400
    card = Image.new("RGB", (width, height), (20, 20, 20))
    draw = ImageDraw.Draw(card)
    
    # Вставляем аватары
    av1 = draw_round_avatar(u1['photo_path'])
    av2 = draw_round_avatar(u2['photo_path'])
    card.paste(av1, (50, 100))
    card.paste(av2, (370, 100))
    
    # Текст результатов
    draw.text((150, 250), f"Overall: {u1['scores']['overall']}", fill="white")
    draw.text((370, 250), f"Overall: {u2['scores']['overall']}", fill="white")
    
    card.save("result.png")
    return "result.png"

# --- ЛОГИКА ---
async def get_user_data(username_or_id):
    try:
        entity = await client.get_entity(username_or_id)
        full = await client(GetFullUserRequest(entity))
        photo = await client.download_profile_photo(entity, file=f"avatar_{entity.id}.jpg")
        try: stories = await client.get_stories(entity); has_stories = len(stories.stories) > 0
        except: has_stories = False
        return {"username": entity.username or "user", "id": entity.id, "bio": full.full_user.about or "", "has_stories": has_stories, "has_channel": full.full_user.personal_channel_id is not None, "photo_path": photo}
    except: return None

@client.on(events.NewMessage(pattern=r'\.мог(?:\s+([^\s]+))?(?:\s+([^\s]+))?'))
async def handler(event):
    args = event.pattern_match.groups()
    u1 = await get_user_data(args[0])
    u2 = await get_user_data(args[1] if args[1] else (await event.get_sender()).username)
    
    # Оценка AI
    for u in [u1, u2]:
        prompt = f"Оцени профиль (ID:{u['id']}, Stories:{u['has_stories']}). JSON: {{'overall': 0.0, 'rank': '...'}}"
        contents = [prompt, Image.open(u['photo_path']) if u['photo_path'] else "No photo"]
        res = model.generate_content(contents)
        u['scores'] = json.loads(res.text.replace("```json", "").replace("```", "").strip())
        u['scores']['username'] = u['username']
    
    winner = 1 if u1['scores']['overall'] >= u2['scores']['overall'] else 2
    card = generate_mog_card(u1, u2, winner)
    await client.send_file(event.chat_id, card, caption=f"Победил @{u1['username'] if winner==1 else u2['username']}")

client.run_until_disconnected()
