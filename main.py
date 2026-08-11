import os
import json
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest
import google.generativeai as genai

# 1. ВЕБ-СЕРВЕР ДЛЯ RENDER (Health Check)
PORT = int(os.environ.get("PORT", 8080))

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# 2. КОНФИГ И ИНИЦИАЛИЗАЦИЯ
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')
client = TelegramClient('mog_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# 3. ОСНОВНАЯ ЛОГИКА
SYSTEM_PROMPT = """Ты — эксперт по оценке профилей в Telegram (Mogging Battle).
Оценивай по шкале от 0 до 10 по критериям: 
1. Аватарка (стиль)
2. ID (возраст аккаунта: чем меньше ID, тем круче)
3. Био
4. Активность (сторис)

Ответь строго в формате JSON:
{"overall": 0.0, "avatar_score": 0.0, "og_score": 0.0, "bio_score": 0.0, "comment": "Короткая фраза"}"""

async def get_user_data(username_or_id):
    try:
        entity = await client.get_entity(username_or_id)
        full = await client(GetFullUserRequest(entity))
        try: 
            stories = await client.get_stories(entity)
            has_stories = len(stories.stories) > 0
        except: has_stories = False
        return {
            "username": entity.username or "user",
            "id": entity.id,
            "bio": full.full_user.about or "Нет био",
            "has_stories": has_stories
        }
    except Exception as e:
        print(f"Ошибка получения данных: {e}")
        return None

@client.on(events.NewMessage(pattern=r'\.мог(?:\s+([^\s]+))?'))
async def handler(event):
    target = event.pattern_match.group(1)
    if not target:
        sender = await event.get_sender()
        target = sender.username
    
    data = await get_user_data(target)
    if not data:
        await event.reply("Не могу найти такого пользователя.")
        return

    # Оценка нейросетью
    prompt = f"{SYSTEM_PROMPT}\nДанные: {data}"
    response = model.generate_content(prompt)
    
    try:
        res_text = response.text.replace("```json", "").replace("```", "").strip()
        scores = json.loads(res_text)
        
        reply = (f"🔥 **Mogging Report для @{data['username']}**\n\n"
                 f"Общий балл: {scores['overall']}/10\n"
                 f"Аватар: {scores['avatar_score']}\n"
                 f"OG Статус: {scores['og_score']}\n"
                 f"Био: {scores['bio_score']}\n\n"
                 f"Вердикт: {scores['comment']}")
        await event.reply(reply)
    except Exception as e:
        await event.reply("Ошибка при генерации оценки.")
        print(e)

print("Бот запущен...")
client.run_until_disconnected()
