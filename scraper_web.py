import logging
import re
import time
import random
import httpx
from urllib.parse import urlparse
from duckduckgo_search import DDGS
import undetected_chromedriver as uc
from config import PROXY_URL, PROXY_ROTATE_URL, PROXY_ROTATE_ENABLED, BROWSER_SESSION_PATH, PROXY_ENABLED

logger = logging.getLogger(__name__)

PLATFORM = "web"


class WebScraper:
    """Search across the whole internet via DuckDuckGo and Google (Manual)."""

    def _rotate_proxy_ip(self) -> None:
        """Calls the rotate URL to change mobile proxy IP if configured and enabled."""
        if PROXY_ENABLED and PROXY_ROTATE_ENABLED and PROXY_ROTATE_URL:
            try:
                logger.info("Rotating proxy IP...")
                response = httpx.get(PROXY_ROTATE_URL, timeout=10)
                logger.info("IP rotation response: %s", response.text.strip())
                time.sleep(2) # Wait a bit for IP to actually change
            except Exception as e:
                logger.error("Failed to rotate IP: %s", e)

    def open_google_manual(self, query: str) -> None:
        """Opens Google in a non-headless browser using Undetected Chromedriver."""
        logger.info("Starting manual Google search via Selenium UC for: %s", query)
        try:
            self._rotate_proxy_ip()

            from pathlib import Path
            user_data_dir = Path(BROWSER_SESSION_PATH).resolve()
            user_data_dir.mkdir(parents=True, exist_ok=True)

            options = uc.ChromeOptions()
            options.add_argument(f"--user-data-dir={user_data_dir}")
            
            if PROXY_ENABLED and PROXY_URL:
                from urllib.parse import urlparse
                parsed = urlparse(PROXY_URL)
                proxy_addr = f"{parsed.hostname}:{parsed.port}"
                options.add_argument(f'--proxy-server={proxy_addr}')
                logger.info("Using proxy: %s", proxy_addr)

            logger.info("Launching Undetected Chromedriver...")
            
            # Спробуємо автоматично визначити версію Chrome, щоб уникнути конфліктів
            import subprocess
            import re
            
            def get_chrome_version():
                try:
                    # Для Windows
                    cmd = 'reg query "HKEY_CURRENT_USER\\Software\\Google\\Chrome\\BLBeacon" /v version'
                    output = subprocess.check_output(cmd, shell=True).decode()
                    version = re.search(r'(\d+)\.', output)
                    return int(version.group(1)) if version else None
                except Exception:
                    return None

            chrome_version = get_chrome_version()
            if chrome_version:
                logger.info(f"Detected Chrome version: {chrome_version}")
            
            driver = uc.Chrome(options=options, use_subprocess=True, version_main=chrome_version)
            
            try:
                logger.info("Navigating to Google Shopping...")
                driver.get("https://www.google.com.ua/shopping")
                
                # Рандомна затримка після завантаження
                time.sleep(random.uniform(2, 4))

                # Пошук поля (в Shopping воно зазвичай має назву 'q' або 'textarea')
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC

                try:
                    # Шукаємо поле пошуку
                    search_box = WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((By.NAME, "q"))
                    )
                except Exception:
                    logger.info("Search box not found on Shopping page, trying alternative selectors...")
                    search_box = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "textarea, input[type='text']"))
                    )

                # Імітація кліку
                search_box.click()
                time.sleep(random.uniform(0.8, 1.8))

                # Друкуємо як людина
                logger.info(f"Typing query in Shopping: {query}")
                for char in query:
                    search_box.send_keys(char)
                    time.sleep(random.uniform(0.1, 0.35))
                
                time.sleep(random.uniform(0.7, 1.5))
                search_box.submit()

                # Скролінг результатів покупок
                time.sleep(random.uniform(3, 5))
                for _ in range(random.randint(2, 4)):
                    driver.execute_script(f"window.scrollBy(0, {random.randint(400, 900)});")
                    time.sleep(random.uniform(1, 2))
                
                logger.info("Shopping search complete. Keeping browser open.")
                time.sleep(600)

            finally:
                driver.quit()
                
        except Exception as e:
            logger.error("Error in open_google_manual (Selenium): %s", e)

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
        match = re.search(r'(\d[\d\s]{0,8}\d)\s*грн', text, re.IGNORECASE)
        return f"{match.group(1).strip()} грн" if match else "Ціна не вказана"

    @staticmethod
    def _domain(url: str) -> str:
        try:
            return urlparse(url).netloc.replace("www.", "")
        except Exception:
            return "Інтернет"
