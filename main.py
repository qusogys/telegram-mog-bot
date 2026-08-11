import os
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest
import google.generativeai as genai
from PIL import Image

# 1. Чтение токенов из переменных окружения
API_ID = int(os.environ.get("API_ID", 0) or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PORT = int(os.environ.get("PORT", 8080))

# 2. Настройка фонового веб-сервера для хостинга Render (Health Check)
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        # Отключаем выводимые логи запросов от cron-job
        return

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    server.serve_forever()

# Запускаем веб-сервер в отдельном потоке
threading.Thread(target=run_health_server, daemon=True).start()

# 3. Настройка Gemini API
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash')

# 4. Инициализация Telethon (MTProto)
client = TelegramClient('mog_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

SYSTEM_PROMPT = """Ты — ироничный эксперт по оценке профилей в Telegram (Mogging judge).
Твоя задача — оценить двух пользователей по 10-балльной шкале (с точностью до десятых, например 2.3/10, 8.7/10) и определить, кто кого «замогал».

КРИТЕРИИ ОЦЕНКИ (каждый от 0.0 до 10.0):
1. Юзернейм: длина (чем короче — тем выше балл), осмысленность, отсутствие случайных цифр.
2. Аватарка: визуал, вайб, стиль, качество. (Это главный показатель).
3. Bio: стиль, лаконичность, отсутствие банальщины.
4. Подарки/Статус: наличие и ценность (Внимание: этот параметр ВТОРОСТЕПЕННЫЙ, он не должен перекрывать плохую аватарку или ужасный юзернейм).

РАНГИ ПО ИТОГОВОМУ СРЕДНЕМУ БАЛЛУ:
0.0-2.9: Sub-3 | 3.0-4.9: Sub-5 | 5.0-5.9: LTN | 6.0-6.9: MTN | 7.0-7.9: HTN | 8.0-8.9: Chadlite | 9.0-9.5: Chad | 9.6-10.0: True Adam

ФОРМАТ ОТВЕТА (Строго придерживайся этого шаблона):

🔥 @победитель могнул @проигравший!

📊 @user1:
• Юзернейм: X.X/10
• Аватарка: X.X/10
• Bio: X.X/10
• Подарки/Статус: X.X/10
🏆 Итог: X.X/10 — [Ранг]

📊 @user2:
• Юзернейм: X.X/10
• Аватарка: X.X/10
• Bio: X.X/10
• Подарки/Статус: X.X/10
🏆 Итог: X.X/10 — [Ранг]

💡 Объяснение: [1-2 коротких предложения, почему именно победитель забирает этот могг]."""

async def get_user_data(username_or_id):
    """Сбор информации о профиле пользователя."""
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
        print(f"Ошибка получения данных для {username_or_id}: {e}")
        return None

@client.on(events.NewMessage(pattern=r'^\.мог(?:\s+([^\s]+))?(?:\s+([^\s]+))?'))
async def mog_handler(event):
    args = event.pattern_match.groups()
    sender = await event.get_sender()
    
    # Определение участников
    if args[0] and args[1]:
        target1_name, target2_name = args[0], args[1]
    elif args[0]:
        target1_name = f"@{sender.username}" if sender.username else sender.id
        target2_name = args[0]
    else:
        await event.reply("Укажите хотя бы одного пользователя: `.мог @username` или `.мог @user1 @user2`")
        return

    msg = await event.reply("🔍 Анализирую профили, собираю данные...")

    user1 = await get_user_data(target1_name)
    user2 = await get_user_data(target2_name)

    if not user1 or not user2:
        await msg.edit("❌ Не удалось получить данные одного из пользователей. Убедитесь, что юзернеймы указаны верно и профили открыты.")
        return

    # Подготовка данных для Gemini
    prompt_text = f"{SYSTEM_PROMPT}\n\n"
    prompt_text += f"Игрок 1: @{user1['username']}\n- Имя: {user1['name']}\n- Длина юзернейма: {user1['len']} символов\n- Bio: {user1['bio']}\n- Подарков: {user1['gifts']}\n\n"
    prompt_text += f"Игрок 2: @{user2['username']}\n- Имя: {user2['name']}\n- Длина юзернейма: {user2['len']} символов\n- Bio: {user2['bio']}\n- Подарков: {user2['gifts']}\n\nУчитывай прикрепленные изображения. Первое фото - Игрок 1, второе - Игрок 2 (если есть)."

    contents = [prompt_text]
    opened_files = []

    try:
        # Загружаем аватарки в запрос
        for i, u in enumerate([user1, user2]):
            if u['photo_path'] and os.path.exists(u['photo_path']):
                img = Image.open(u['photo_path'])
                contents.append(f"Фото Игрока {i+1}:")
                contents.append(img)
                opened_files.append(u['photo_path'])
            else:
                contents.append(f"У Игрока {i+1} нет аватарки.")

        # Генерация ответа ИИ
        response = model.generate_content(contents)
        await msg.edit(response.text)
    except Exception as e:
        await msg.edit(f"❌ Ошибка при обращении к ИИ: {e}")
    finally:
        # Автоматическая очистка временных аватарок из диска
        for path in opened_files:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass

if __name__ == '__main__':
    if not all([API_ID, API_HASH, BOT_TOKEN, GEMINI_API_KEY]):
        print("ВНИМАНИЕ: Не все переменные окружения (API_ID, API_HASH, BOT_TOKEN, GEMINI_API_KEY) заданы!")
    else:
        print(f"✅ Фоновый веб-сервер запущен на порту {PORT}")
        print("✅ Бот успешно запущен. Ожидание команд...")
        client.run_until_disconnected()
