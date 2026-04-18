"""
Скрипт для ручного логіну в Google через Undetected Chromedriver (Selenium).
Створює сесію, яку бот потім використовуватиме для пошуку.
"""
import time
import os
from pathlib import Path
import undetected_chromedriver as uc
from dotenv import load_dotenv

# Завантажуємо оточення
load_dotenv()

try:
    from config import BROWSER_SESSION_PATH, PROXY_URL, PROXY_ENABLED
except ImportError:
    BROWSER_SESSION_PATH = os.getenv("BROWSER_SESSION_PATH", "browser_session")
    PROXY_URL = os.getenv("PROXY_URL")
    PROXY_ENABLED = os.getenv("PROXY_ENABLED", "true").lower() != "false"

def main():
    print("=" * 70)
    print("🚀 ПІДГОТОВКА СЕСІЇ GOOGLE (Undetected Chromedriver)")
    print("=" * 70)

    user_data_dir = Path(BROWSER_SESSION_PATH).resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"📁 Папка сесії: {user_data_dir}")

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={user_data_dir}")
    
    if PROXY_ENABLED and PROXY_URL:
        # Для Selenium проксі додається через аргумент
        # Примітка: якщо проксі з паролем, Chrome може запитати його при вході
        from urllib.parse import urlparse
        parsed = urlparse(PROXY_URL)
        proxy_addr = f"{parsed.hostname}:{parsed.port}"
        options.add_argument(f'--proxy-server={proxy_addr}')
        print(f"🌐 Проксі активовано: {proxy_addr}")

    print("\nЗапуск браузера... Зачекайте.")
    
    import subprocess
    import re
    def get_chrome_version():
        try:
            cmd = 'reg query "HKEY_CURRENT_USER\\Software\\Google\\Chrome\\BLBeacon" /v version'
            output = subprocess.check_output(cmd, shell=True).decode()
            version = re.search(r'(\d+)\.', output)
            return int(version.group(1)) if version else None
        except Exception:
            return None

    chrome_version = get_chrome_version()

    try:
        driver = uc.Chrome(options=options, use_subprocess=True, version_main=chrome_version)
        
        print("\n🌐 Переходжу на Google Shopping...")
        driver.get("https://www.google.com.ua/shopping")
        
        print("\n" + "!" * 70)
        print("  ДІЇ У БРАУЗЕРІ:")
        print("  1. Залогінься у свій Google-акаунт.")
        print("  2. Розв'яжи капчу, якщо вона з'явиться.")
        print("  3. Зроби 1-2 пошукових запити вручну.")
        print("  ВАЖЛИВО: Не закривай вікно браузера сам!")
        print("!" * 70)
        
        input("\nПісля того, як все зробиш, натисни ENTER тут, щоб зберегти сесію...")
        
        print("Зберігаю стан та закриваю...")
        driver.quit()
        
        print("\n" + "=" * 70)
        print("✅ ГОТОВО! Сесія збережена.")
        print("Тепер можеш запускати бота.")
        print("=" * 70)

    except Exception as e:
        print(f"\n❌ Помилка: {e}")

if __name__ == "__main__":
    main()
