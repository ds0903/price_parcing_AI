import logging
import re
import asyncio
import random
import httpx
from urllib.parse import urlparse
from duckduckgo_search import DDGS
from playwright.async_api import async_playwright
from config import PROXY_URL, PROXY_ROTATE_URL, PROXY_ROTATE_ENABLED, BROWSER_SESSION_PATH, HEADLESS, PROXY_ENABLED

logger = logging.getLogger(__name__)

PLATFORM = "web"


class WebScraper:
    """Search across the whole internet via DuckDuckGo — no API key required."""

    async def _rotate_proxy_ip(self) -> None:
        """Calls the rotate URL to change mobile proxy IP if configured and enabled."""
        if PROXY_ENABLED and PROXY_ROTATE_ENABLED and PROXY_ROTATE_URL:
            try:
                logger.info("Rotating proxy IP...")
                async with httpx.AsyncClient() as client:
                    response = await client.get(PROXY_ROTATE_URL, timeout=10)
                    logger.info("IP rotation response: %s", response.text.strip())
                await asyncio.sleep(2) # Wait a bit for IP to actually change
            except Exception as e:
                logger.error("Failed to rotate IP: %s", e)
        elif not PROXY_ENABLED:
            logger.info("Proxy is disabled by config")
        elif not PROXY_ROTATE_ENABLED:
            logger.info("Proxy rotation is disabled by config")

    async def open_google_manual(self, query: str) -> None:
        """Opens Google in a non-headless browser using a persistent session, types query and waits."""
        logger.info("Starting manual Google search for: %s", query)
        try:
            await self._rotate_proxy_ip()

            proxy_config = None
            if PROXY_ENABLED and PROXY_URL:
                parsed = urlparse(PROXY_URL)
                proxy_config = {
                    "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                    "username": parsed.username,
                    "password": parsed.password,
                }
                logger.info("Using proxy: %s:%s", parsed.hostname, parsed.port)
            else:
                logger.info("Proxy is disabled or PROXY_URL is missing")

            from pathlib import Path
            user_data_dir = Path(BROWSER_SESSION_PATH).resolve()
            user_data_dir.mkdir(parents=True, exist_ok=True)

            logger.info("Using session directory: %s", user_data_dir)
            
            async with async_playwright() as pw:
                logger.info("Launching browser...")
                # Використовуємо Chromium (як в інших твоїх скраперах)
                browser_context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=False,
                    proxy=proxy_config,
                    channel="chrome",
                    viewport={'width': 1280, 'height': 720},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                
                await self._run_google_search_in_context(browser_context, query)
                logger.info("Closing browser context...")
                await browser_context.close()
                
        except Exception as e:
            logger.error("Error in open_google_manual: %s", e)

    async def _run_google_search_in_context(self, context, query: str) -> None:
        """Internal logic to perform the search using a persistent context."""
        page = context.pages[0] if context.pages else await context.new_page()
        
        logger.info("Navigating to Google...")
        await page.goto("https://www.google.com.ua", wait_until="networkidle")
        
        search_box = page.locator('textarea[name="q"], input[name="q"]').first
        
        try:
            await search_box.wait_for(state="visible", timeout=10000)
        except Exception:
            logger.info("Search box not visible (maybe CAPTCHA). Waiting for user up to 60s...")
            try:
                await search_box.wait_for(state="visible", timeout=60000)
            except Exception:
                logger.warning("Search box never appeared. Exiting.")
                return

        await search_box.click()
        await asyncio.sleep(random.uniform(0.5, 1.5))
        
        logger.info("Typing query...")
        for char in query:
            await search_box.type(char, delay=random.randint(50, 250))
            if random.random() < 0.05:
                await asyncio.sleep(random.uniform(0.5, 1.0))
        
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await search_box.press("Enter")
        
        await asyncio.sleep(3)
        await page.mouse.wheel(0, random.randint(400, 800))
        
        logger.info("Search complete. Keeping browser open for 10 minutes.")
        try:
            await asyncio.sleep(600) 
        except asyncio.CancelledError:
            logger.info("Manual search task cancelled.")
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
