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
    """Ultra-Greedy Search across Google Shopping (Scrapes EVERYTHING)."""

    def _rotate_proxy_ip(self) -> None:
        if PROXY_ENABLED and PROXY_ROTATE_ENABLED and PROXY_ROTATE_URL:
            try:
                httpx.get(PROXY_ROTATE_URL, timeout=10)
                time.sleep(2) 
            except: pass

    def open_google_manual(self, query: str) -> list[dict]:
        logger.info("🚀 Starting ULTRA-GREEDY Google Shopping scraping for: %s", query)
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
            options.add_argument("--start-maximized")
            
            if PROXY_ENABLED and PROXY_URL:
                from urllib.parse import urlparse
                parsed = urlparse(PROXY_URL)
                options.add_argument(f'--proxy-server={parsed.hostname}:{parsed.port}')

            import subprocess
            def get_chrome_version():
                try:
                    cmd = 'reg query "HKEY_CURRENT_USER\\Software\\Google\\Chrome\\BLBeacon" /v version'
                    output = subprocess.check_output(cmd, shell=True).decode()
                    return int(re.search(r'(\d+)\.', output).group(1))
                except: return None

            driver = uc.Chrome(options=options, use_subprocess=True, version_main=get_chrome_version())
            
            try:
                driver.get("https://www.google.com.ua/shopping")
                time.sleep(3)

                # Пошук та ввід
                try:
                    sb = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, "q")))
                except:
                    sb = driver.find_element(By.CSS_SELECTOR, "textarea, input[type='text']")
                
                sb.click()
                sb.clear()
                for char in query:
                    sb.send_keys(char)
                    time.sleep(random.uniform(0.05, 0.15))
                sb.submit()
                
                time.sleep(5)

                # --- Глибокий скролінг ---
                logger.info("Scrolling to the deep end...")
                for _ in range(6):
                    driver.execute_script("window.scrollBy(0, 1200);")
                    time.sleep(1.5)
                    try:
                        btn = driver.find_element(By.CSS_SELECTOR, "button:contains('Показати більше'), .GN77nd")
                        if btn.is_displayed(): btn.click()
                    except: pass

                # --- ULTRA-GREEDY EXTRACTION ---
                logger.info("Extracting EVERYTHING that looks like a product...")
                
                # Ми шукаємо всі блоки, в яких є посилання на товар та ціна
                # Google використовує aclk для реклами і /shopping/product для звичайних
                elements = driver.find_elements(By.XPATH, "//div[.//a[contains(@href, 'aclk') or contains(@href, '/shopping/product')]]")
                
                logger.info(f"Analyzing {len(elements)} potential blocks...")

                for el in elements:
                    try:
                        text = el.text.replace("\n", " ").strip()
                        if not text: continue

                        # 1. Шукаємо ціну через Regex (цифри + грн/₴)
                        price_match = re.search(r'(\d[\d\s,]{0,10})\s*(грн|₴|грн\.)', text, re.I)
                        if not price_match: continue # Якщо немає ціни - це не товар
                        
                        price = price_match.group(0).strip()

                        # 2. Шукаємо посилання
                        try:
                            link_el = el.find_element(By.XPATH, ".//a[contains(@href, 'aclk') or contains(@href, '/shopping/product')]")
                            url = link_el.get_attribute("href")
                        except: continue

                        # 3. Назва (беремо найбільший текстовий блок всередині посилання або h3)
                        name = ""
                        try:
                            name = el.find_element(By.TAG_NAME, "h3").text
                        except:
                            # Якщо h3 немає, беремо текст посилання, відсікаючи ціну
                            name = link_el.text.split("\n")[0].strip()
                        
                        if len(name) < 10: # Спробуємо знайти довший текст в блоці
                            parts = [p.strip() for p in text.split("  ") if len(p.strip()) > 15]
                            if parts: name = parts[0]

                        if not name or len(name) < 5: continue

                        # 4. Продавець (шукаємо текст після ціни або в окремих мітках)
                        seller = "Магазин"
                        # Часто продавець йде після ціни або має окремі класи, спробуємо витягти залишок
                        clean_text = text.replace(price, "").replace(name, "").strip()
                        if clean_text:
                            seller_parts = [p.strip() for p in clean_text.split("·") if p.strip()]
                            if seller_parts: seller = seller_parts[0]

                        products.append({
                            "name": name[:150].strip(), # Обмежуємо довжину
                            "price": price,
                            "seller": seller[:50],
                            "city": "",
                            "url": url,
                            "platform": "google_shopping",
                        })
                    except: continue

                # Фінальна очистка
                unique_products = {}
                for p in products:
                    # Ключ унікальності - назва + ціна (щоб бачити різні магазини з однаковим товаром)
                    key = f"{p['name']}_{p['price']}_{p['seller']}"
                    if key not in unique_products:
                        unique_products[key] = p
                
                logger.info(f"✅ ULTRA-GREEDY complete! Found {len(unique_products)} unique items.")
                return list(unique_products.values())

            finally:
                driver.quit()
        except Exception as e:
            logger.error("❌ Critical scraping error: %s", e)
        
        return products

    def search_page(self, query: str, page: int) -> list[dict]:
        return self.search_products(query, limit=100) if page == 1 else []

    def search_products(self, query: str, limit: int = 10) -> list[dict]:
        search_query = f"{query} ціна купити Україна грн"
        try:
            raw = list(DDGS().text(search_query, max_results=limit or 100))
        except Exception as e:
            logger.error("DuckDuckGo error: %s", e)
            return []

        products = []
        for r in raw:
            products.append({
                "name": r.get("title", "").strip(),
                "price": self._extract_price(r.get("body", "")),
                "seller": self._domain(r.get("href", "")),
                "city": "",
                "url": r.get("href", ""),
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
