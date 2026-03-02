import logging
import re
import time
import random
import httpx
import asyncio
from urllib.parse import urlparse
from duckduckgo_search import DDGS
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from config import PROXY_URL, PROXY_ROTATE_URL, PROXY_ROTATE_ENABLED, BROWSER_SESSION_PATH, PROXY_ENABLED

logger = logging.getLogger(__name__)

PLATFORM = "web"


class WebScraper:
    """Search across the whole internet via DuckDuckGo and Google Shopping (Scraping)."""

    def _rotate_proxy_ip(self) -> None:
        """Calls the rotate URL to change mobile proxy IP if configured and enabled."""
        if PROXY_ENABLED and PROXY_ROTATE_ENABLED and PROXY_ROTATE_URL:
            try:
                logger.info("Rotating proxy IP...")
                response = httpx.get(PROXY_ROTATE_URL, timeout=10)
                logger.info("IP rotation response: %s", response.text.strip())
                time.sleep(2) 
            except Exception as e:
                logger.error("Failed to rotate IP: %s", e)

    def open_google_manual(self, query: str) -> list[dict]:
        """Opens Google Shopping, scrolls to bottom, parses ALL items and returns them."""
        logger.info("Starting manual Google Shopping scraping for: %s", query)
        products = []
        try:
            self._rotate_proxy_ip()

            from pathlib import Path
            user_data_dir = Path(BROWSER_SESSION_PATH).resolve()
            user_data_dir.mkdir(parents=True, exist_ok=True)

            options = uc.ChromeOptions()
            options.add_argument(f"--user-data-dir={user_data_dir}")
            options.add_argument("--disable-notifications")
            options.add_argument("--lang=uk-UA")
            
            if PROXY_ENABLED and PROXY_URL:
                from urllib.parse import urlparse
                parsed = urlparse(PROXY_URL)
                proxy_addr = f"{parsed.hostname}:{parsed.port}"
                options.add_argument(f'--proxy-server={proxy_addr}')

            import subprocess
            def get_chrome_version():
                try:
                    cmd = 'reg query "HKEY_CURRENT_USER\\Software\\Google\\Chrome\\BLBeacon" /v version'
                    output = subprocess.check_output(cmd, shell=True).decode()
                    return int(re.search(r'(\d+)\.', output).group(1))
                except: return None

            driver = uc.Chrome(options=options, use_subprocess=True, version_main=get_chrome_version())
            
            try:
                logger.info("Navigating to Google Shopping...")
                driver.get("https://www.google.com.ua/shopping")
                time.sleep(random.uniform(2, 4))

                try:
                    search_box = WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((By.NAME, "q"))
                    )
                except:
                    search_box = driver.find_element(By.CSS_SELECTOR, "textarea, input[type='text']")

                search_box.click()
                time.sleep(random.uniform(0.5, 1.0))
                for char in query:
                    search_box.send_keys(char)
                    time.sleep(random.uniform(0.05, 0.2))
                search_box.submit()
                
                time.sleep(random.uniform(4, 6))

                # --- Поступовий скролінг та збір ---
                logger.info("Scrolling and parsing items...")
                
                for _ in range(5): # Зробимо 5 великих скролів
                    driver.execute_script("window.scrollBy(0, 1000);")
                    time.sleep(2)
                    
                    # Спробуємо натиснути "Показати більше" якщо є
                    try:
                        more_btn = driver.find_element(By.CSS_SELECTOR, "button.GN77nd, .m67it")
                        if more_btn.is_displayed():
                            more_btn.click()
                            time.sleep(2)
                    except: pass

                # Парсимо всі знайдені картки
                cards = driver.find_elements(By.CSS_SELECTOR, "div.sh-dgr__content, div.sh-dgr__grid-result, .sh-np__click-target")
                
                for card in cards:
                    try:
                        name = card.find_element(By.TAG_NAME, "h3").text
                        if not name: continue

                        # Розширені селектори для ціни
                        price = "Ціна не вказана"
                        for p_sel in [".a83139c", ".OFFNJ", ".kYv3ub", "span[aria-hidden='true']"]:
                            try:
                                p_text = card.find_element(By.CSS_SELECTOR, p_sel).text
                                if "грн" in p_text or any(d.isdigit() for d in p_text):
                                    price = p_text
                                    break
                            except: pass

                        # Розширені селектори для продавця
                        seller = "Інтернет-магазин"
                        for s_sel in [".I_9096", ".aULzUe", ".sh-np__seller-container", ".E5uYIc"]:
                            try:
                                s_text = card.find_element(By.CSS_SELECTOR, s_sel).text
                                if s_text:
                                    seller = s_text
                                    break
                            except: pass

                        url = card.find_element(By.TAG_NAME, "a").get_attribute("href")

                        products.append({
                            "name": name.strip(),
                            "price": price.strip(),
                            "seller": seller.strip(),
                            "city": "",
                            "url": url,
                            "image_url": "",
                            "platform": "google_shopping",
                        })
                    except: continue

                # Видаляємо дублікати за URL
                unique_products = {p['url']: p for p in products if p.get('url')}.values()
                products = list(unique_products)
                logger.info(f"Successfully parsed {len(products)} unique items")

            finally:
                driver.quit()
        except Exception as e:
            logger.error("Scraping error: %s", e)
        
        return products

    def search_page(self, query: str, page: int) -> list[dict]:
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
            products.append({
                "name": r.get("title", "").strip(),
                "price": self._extract_price(r.get("body", "")),
                "seller": self._domain(r.get("href", "")),
                "city": "",
                "url": r.get("href", ""),
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
        try: return urlparse(url).netloc.replace("www.", "")
        except: return "Інтернет"
