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
    """Super-Greedy Search (Scrapes literally EVERYTHING visible)."""

    def _rotate_proxy_ip(self) -> None:
        if PROXY_ENABLED and PROXY_ROTATE_ENABLED and PROXY_ROTATE_URL:
            try:
                httpx.get(PROXY_ROTATE_URL, timeout=10)
                time.sleep(2) 
            except: pass

    def open_google_manual(self, query: str) -> list[dict]:
        logger.info("🚀 Starting SUPER-GREEDY scraping for: %s", query)
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

                try:
                    sb = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, "q")))
                except:
                    sb = driver.find_element(By.CSS_SELECTOR, "textarea, input[type='text']")
                
                sb.click()
                sb.clear()
                # Вводимо запит 1в1
                for char in query:
                    sb.send_keys(char)
                    time.sleep(random.uniform(0.05, 0.15))
                sb.submit()
                
                time.sleep(5)

                # Глибокий скролінг
                for _ in range(5):
                    driver.execute_script("window.scrollBy(0, 1000);")
                    time.sleep(2)
                    try:
                        btn = driver.find_element(By.CSS_SELECTOR, "button.GN77nd, .m67it")
                        if btn.is_displayed(): btn.click()
                    except: pass

                # --- SUPER-GREEDY EXTRACTION ---
                # Шукаємо всі можливі картки товарів через набір різних паттернів Google
                logger.info("Scanning page for all product-like structures...")
                
                # 1. Спробуємо знайти всі блоки, що містять ціну (це найнадійніше)
                # Google Shopping зазвичай малює ціни в спанах або дівах з певними ознаками
                all_elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'грн') or contains(text(), '₴')]")
                
                for price_el in all_elements:
                    try:
                        price_text = price_el.text.strip()
                        # Перевірка що це дійсно ціна (є цифри)
                        if not any(d.isdigit() for d in price_text): continue
                        
                        # Піднімаємося вгору до контейнера (зазвичай 3-5 рівнів вгору)
                        parent = price_el
                        found_card = False
                        for _ in range(6):
                            parent = parent.find_element(By.XPATH, "..")
                            # Якщо в батьку є посилання і h3/heading - це наша картка
                            links = parent.find_elements(By.TAG_NAME, "a")
                            headings = parent.find_elements(By.XPATH, ".//h3 | .//*[@role='heading']")
                            if links and headings:
                                name = headings[0].text.strip()
                                url = links[0].get_attribute("href")
                                if name and url:
                                    # Шукаємо назву магазину поруч з ціною
                                    seller = "Інтернет-магазин"
                                    # Часто продавець в тому ж блоці де і ціна або поруч
                                    try:
                                        seller_el = parent.find_element(By.CSS_SELECTOR, ".I_9096, .aULzUe, .sh-np__seller-container, .E5uYIc")
                                        seller = seller_el.text.strip()
                                    except:
                                        # Резервний пошук тексту який не є ціною і назвою
                                        txt = parent.text.replace(price_text, "").replace(name, "").strip()
                                        if txt: seller = txt.split("\n")[0][:30]

                                    products.append({
                                        "name": name,
                                        "price": price_text,
                                        "seller": seller,
                                        "city": "",
                                        "url": url,
                                        "platform": "google_shopping",
                                    })
                                    found_card = True
                                    break
                        if found_card: continue
                    except: continue

                # Додатковий прохід за специфічними класами (якщо попередній щось упустив)
                specific_cards = driver.find_elements(By.CSS_SELECTOR, ".sh-dgr__content, .sh-np__click-target, .pla-unit, .pla-hovercard-container")
                for card in specific_cards:
                    try:
                        name = card.find_element(By.XPATH, ".//h3 | .//*[@role='heading']").text
                        price = "Ціна не вказана"
                        for p_sel in [".a83139c", ".OFFNJ", ".kYv3ub", "span[aria-hidden='true']"]:
                            try:
                                p_text = card.find_element(By.CSS_SELECTOR, p_sel).text
                                if any(d.isdigit() for d in p_text): price = p_text; break
                            except: pass
                        
                        url = card.find_element(By.TAG_NAME, "a").get_attribute("href")
                        products.append({
                            "name": name.strip(),
                            "price": price.strip(),
                            "seller": "Магазин", # Буде уточнено AI або з тексту
                            "city": "",
                            "url": url,
                            "platform": "google_shopping",
                        })
                    except: pass

                # Унікалізація
                unique = {}
                for p in products:
                    if not p['url'] or len(p['name']) < 5: continue
                    if p['url'] not in unique: unique[p['url']] = p
                
                logger.info(f"✅ FOUND {len(unique)} TOTAL ITEMS.")
                return list(unique.values())

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
