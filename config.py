import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
DATABASE_URL = os.getenv("DATABASE_URL")

# Proxy settings
PROXY_URL = os.getenv("PROXY_URL") # e.g. socks5://user:pass@host:port
PROXY_ROTATE_URL = os.getenv("PROXY_ROTATE_URL") # URL for changing IP
PROXY_ROTATE_ENABLED = os.getenv("PROXY_ROTATE_ENABLED", "true").lower() != "false"

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is not set in .env")
