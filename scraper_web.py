import logging
import re
import time
import random
import httpx
from urllib.parse import urlparse
from duckduckgo_search import DDGS
from playwright.sync_api import sync_playwright
from config import PROXY_URL, PROXY_ROTATE_URL, PROXY_ROTATE_ENABLED

logger = logging.getLogger(__name__)

PLATFORM = "web"


class WebScraper:
    """Search across the whole internet via DuckDuckGo — no API key required."""

    def _rotate_proxy_ip(self) -> None:
        """Calls the rotate URL to change mobile proxy IP if configured and enabled."""
        if PROXY_ROTATE_ENABLED and PROXY_ROTATE_URL:
            try:
                logger.info("Rotating proxy IP...")
                response = httpx.get(PROXY_ROTATE_URL, timeout=10)
                logger.info("IP rotation response: %s", response.text.strip())
                time.sleep(2) # Wait a bit for IP to actually change
            except Exception as e:
                logger.error("Failed to rotate IP: %s", e)
        elif not PROXY_ROTATE_ENABLED:
            logger.info("Proxy rotation is disabled by config")

    def open_google_manual(self, query: str) -> None:
        """Opens Google in a non-headless browser, types query with delay and waits."""
        try:
            self._rotate_proxy_ip()

            proxy_config = None
            if PROXY_URL:
                parsed = urlparse(PROXY_URL)
                proxy_config = {
                    "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                    "username": parsed.username,
                    "password": parsed.password,
                }
                logger.info("Using proxy: %s:%s", parsed.hostname, parsed.port)

            # Спробуємо використати Camoufox
            try:
                from camoufox.sync_api import Camoufox
                logger.info("Using Camoufox for manual search")
                with Camoufox(headless=False, proxy=proxy_config) as browser:
                    self._run_google_search(browser, query)
            except (ImportError, Exception) as e:
                if "Camoufox" not in str(e):
                    logger.warning("Camoufox error: %s. Falling back to Firefox", e)
                
                # Фоллбек на стандартний Firefox
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    browser = pw.firefox.launch(headless=False, proxy=proxy_config)
                    self._run_google_search(browser, query)
                    browser.close()
        except Exception as e:
            logger.error("Error in open_google_manual: %s", e)

    def _run_google_search(self, browser, query: str) -> None:
        """Internal logic to perform the search once browser is ready."""
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
            viewport={'width': 1280, 'height': 720}
        )
        page = context.new_page()
        
        # Імітація рухів миші при завантаженні
        page.goto("https://www.google.com", wait_until="networkidle")
        
        # Випадкові рухи мишкою
        for _ in range(3):
            page.mouse.move(random.randint(100, 500), random.randint(100, 500))
            time.sleep(random.uniform(0.2, 0.5))

        # Обробка вікна згоди
        try:
            accept_btn = page.locator('button:has-text("Accept all"), button:has-text("Прийняти всі")')
            if accept_btn.is_visible(timeout=3000):
                accept_btn.click()
        except Exception:
            pass

        # Пошук поля
        search_box = page.locator('textarea[name="q"], input[name="q"]').first
        search_box.wait_for(state="visible")
        
        # Клік в поле перед вводом
        search_box.click()
        time.sleep(random.uniform(0.3, 0.8))

        # Humanize typing
        for char in query:
            search_box.type(char, delay=random.randint(50, 250))
        
        time.sleep(random.uniform(0.5, 1.2))
        search_box.press("Enter")
        
        # Легкий скрол після пошуку (імітація перегляду)
        time.sleep(2)
        page.mouse.wheel(0, random.randint(300, 600))
        
        # Чекаємо 10 хвилин
        try:
            page.wait_for_timeout(600_000) 
        except Exception:
            pass

    def search_page(self, query: str, page: int) -> list[dict]:
        """DuckDuckGo has no true pagination — returns all on page 1, empty otherwise."""
        return self.search_products(query, limit=100) if page == 1 else []

    def search_products(self, query: str, limit: int = 10) -> list[dict]:
        search_query = f"{query} ціна купити Україна грн"
        try:
            raw = list(DDGS().text(search_query, max_results=limit or 100))
        except Exception as e:
            logger.error("DuckDuckGo search error: %s", e)
            return []

        products = []
        for r in raw:
            title = r.get("title", "").strip()
            if not title:
                continue
            body = r.get("body", "")
            url = r.get("href", "")
            products.append({
                "name": title,
                "price": self._extract_price(body),
                "seller": self._domain(url),
                "city": "",
                "url": url,
                "image_url": "",
                "platform": PLATFORM,
            })
        return products

    @staticmethod
    def _extract_price(text: str) -> str:
        # Match patterns like "1 500 грн", "25999 грн", "від 999 грн"
        match = re.search(r'(\d[\d\s]{0,8}\d)\s*грн', text, re.IGNORECASE)
        return f"{match.group(1).strip()} грн" if match else "Ціна не вказана"

    @staticmethod
    def _domain(url: str) -> str:
        try:
            return urlparse(url).netloc.replace("www.", "")
        except Exception:
            return "Інтернет"
