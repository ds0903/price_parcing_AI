import logging
import re
from urllib.parse import urlparse
from duckduckgo_search import DDGS

from playwright.sync_api import sync_playwright
import time
import random

logger = logging.getLogger(__name__)

PLATFORM = "web"


class WebScraper:
    """Search across the whole internet via DuckDuckGo — no API key required."""

    def open_google_manual(self, query: str) -> None:
        """Opens Google in a non-headless browser, types query with delay and waits."""
        try:
            # Спробуємо використати Camoufox
            try:
                from camoufox.sync_api import Camoufox
                logger.info("Using Camoufox for manual search")
                with Camoufox(headless=False) as browser:
                    self._run_google_search(browser, query)
            except (ImportError, Exception) as e:
                if "Camoufox" not in str(e):
                    logger.warning("Camoufox error: %s. Falling back to Firefox", e)
                
                # Фоллбек на стандартний Firefox
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    browser = pw.firefox.launch(headless=False)
                    self._run_google_search(browser, query)
                    browser.close()
        except Exception as e:
            logger.error("Error in open_google_manual: %s", e)

    def _run_google_search(self, browser, query: str) -> None:
        """Internal logic to perform the search once browser is ready."""
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"
        )
        page = context.new_page()
        
        # Перехід на Google
        page.goto("https://www.google.com", wait_until="networkidle")
        
        # Обробка вікна згоди
        try:
            page.click('button:has-text("Accept all"), button:has-text("Прийняти всі")', timeout=3000)
        except Exception:
            pass

        # Пошук поля
        search_box = page.locator('textarea[name="q"], input[name="q"]').first
        search_box.wait_for(state="visible")
        
        # Humanize typing
        import time, random
        for char in query:
            search_box.type(char)
            time.sleep(random.uniform(0.1, 0.3))
        
        search_box.press("Enter")
        
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
