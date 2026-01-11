import os
import asyncio
import sys
import logging

# Настраиваем логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

print("=" * 50)
print("🤖 PIXELMAGE PRO - RAILWAY LAUNCHER")
print("=" * 50)

# Проверяем переменные
BOT_TOKEN = os.getenv("BOT_TOKEN")
AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY")

print(f"✓ BOT_TOKEN: {'***УСТАНОВЛЕН***' if BOT_TOKEN else '❌ НЕ НАЙДЕН'}")
print(f"✓ AITUNNEL_API_KEY: {'***УСТАНОВЛЕН***' if AITUNNEL_API_KEY else '❌ НЕ НАЙДЕН'}")

if not BOT_TOKEN or not AITUNNEL_API_KEY:
    print("❌ ОШИБКА: Отсутствуют необходимые переменные!")
    sys.exit(1)

# Импортируем и запускаем бота
sys.path.insert(0, os.path.dirname(__file__))

try:
    from pixelmage_pro import main as bot_main
    print("✅ Бот загружен успешно")
    
    # Запускаем бота
    print("🚀 Запускаю бота...")
    print("=" * 50)
    print("📱 Отправьте /start в Telegram")
    print("=" * 50)
    
    asyncio.run(bot_main())
    
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
except Exception as e:
    print(f"❌ Ошибка запуска: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
