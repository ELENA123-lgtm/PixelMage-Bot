import os
import asyncio
import sys

# ОТЛАДКА: Проверяем переменные перед запуском
print("=" * 50)
print("🚀 RAILWAY_RUN.PY - НАЧАЛО")
print("=" * 50)
print(f"BOT_TOKEN в окружении: {'BOT_TOKEN' in os.environ}")
print(f"AITUNNEL_API_KEY в окружении: {'AITUNNEL_API_KEY' in os.environ}")
print(f"Текущая директория: {os.getcwd()}")
print(f"Содержимое директории: {os.listdir('.')}")
print("=" * 50)

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(__file__))

# Импортируем и запускаем бота
try:
    from pixelmage_pro import main
    print("✅ PixelMage Pro загружен")
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

if __name__ == "__main__":
    print("🤖 Запускаю PixelMage Pro на Railway...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
