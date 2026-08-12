# Telegram Economy Bot 🇰🇿

Файлы:
- bot.py
- requirements.txt
- render.yaml

Render Environment Variables:
BOT_TOKEN = токен бота
OWNER_ID = твой Telegram ID

Для бесплатного Render НЕ указывай DB_PATH=/var/data/economy.db.
По умолчанию база: economy.db.

Важно: без Persistent Disk локальная SQLite-база может сбрасываться при пересоздании/перезапуске бесплатного сервиса. В течение обычной работы баланс сохраняется.
