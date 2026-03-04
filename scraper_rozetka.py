import logging
import time
import random
import subprocess
import re
import threading
from pathlib import Path
from urllib.parse import quote
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from config import HEADLESS, BROWSER_SESSION_PATH, PROXY_ENABLED, PROXY_URL

logger = logging.getLogger(__name__)

PLATFORM = "rozetka"
_MAX_PAGES = 5


class RozetkaScraper:
    BASE_URL = "https://rozetka.com.ua/ua/search/?text={}&page={}"
    _driver_lock = threading.Lock()

    def _get_driver(self):
        """Ініціалізація undetected-chromedriver (Selenium)."""
        options = uc.ChromeOptions()
        
        # Використовуємо сесію, якщо вказано в налаштуваннях
        if BROWSER_SESSION_PATH:
            user_data_dir = Path(BROWSER_SESSION_PATH).resolve()
            user_data_dir.mkdir(parents=True, exist_ok=True)

        options.add_argument("--disable-notifications")
        options.add_argument("--lang=uk-UA")
        options.add_argument("--window-size=1366,900")
        
        if HEADLESS:
            options.add_argument("--headless")
        
        def get_chrome_version():
            try:
                cmd = 'reg query "HKEY_CURRENT_USER\\Software\\Google\\Chrome\\BLBeacon" /v version'
                output = subprocess.check_output(cmd, shell=True).decode()
                return int(re.search(r'(\d+)\.', output).group(1))
            except: 
                return None

        # Додаємо випадковий порт, щоб уникнути конфліктів при паралельному запуску
        port = random.randint(9222, 9888)
        
        # Використовуємо lock, щоб уникнути WinError 183 при одночасному патчингу драйвера
        with self._driver_lock:
            driver = uc.Chrome(
                options=options, 
                use_subprocess=True, 
                version_main=get_chrome_version(),
                port=port
            )
            # Примусово ставимо розмір вікна
            try:
                driver.set_window_size(1366, 900)
            except:
                pass
                
        return driver

    def search_page(self, query: str, page: int) -> list[dict]:
        """Одиночна сторінка — для Excel-колектора."""
        url = self.BASE_URL.format(quote(query), page)
        html = self._fetch_page_html(url)
        return self._parse(html) if html else []

    def search_products(self, query: str, limit: int = 10) -> list[dict]:
        """Пошук на кількох сторінках (Selenium)."""
        products: list[dict] = []
        driver = None
        try:
            driver = self._get_driver()
            for page_num in range(1, _MAX_PAGES + 1):
                url = self.BASE_URL.format(quote(query), page_num)
                try:
                    driver.get(url)
                    time.sleep(random.uniform(3, 5)) # Даємо час завантажитись
                    
                    try:
                        WebDriverWait(driver, 15).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "li.goods-tile, .goods-tile__inner"))
                        )
                    except Exception:
                        logger.warning("Rozetka p%d: таймаут очікування карток товарів", page_num)
                    
                    self._scroll_to_bottom(driver)
                    html = driver.page_source
                    batch = self._parse(html)
                    
                    if not batch:
                        logger.info("Rozetka p%d: товари не знайдені", page_num)
                        break
                    
                    logger.info("Rozetka p%d: знайдено %d товарів", page_num, len(batch))
                    for item in batch:
                        logger.info("  [FOUND] %s | %s", item['name'][:60], item['price'])
                        
                    products.extend(batch)
                    if limit and len(products) >= limit:
                        break
                except Exception as e:
                    logger.error("Rozetka page %d error: %s", page_num, e)
                    break
        except Exception as e:
            logger.error("Rozetka Selenium error: %s", e)
        finally:
            if driver:
                driver.quit()

        return products[:limit] if limit else products

    def _fetch_page_html(self, url: str) -> str:
        driver = None
        try:
            driver = self._get_driver()
            driver.get(url)
            time.sleep(random.uniform(3, 5))
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "li.goods-tile, .goods-tile__inner"))
                )
            except Exception:
                logger.warning("Rozetka timeout on %s", url)
            
            self._scroll_to_bottom(driver)
            html = driver.page_source
            
            # Логування знайденого
            batch = self._parse(html)
            logger.info("Rozetka (single page): знайдено %d товарів", len(batch))
            for item in batch:
                logger.info("  [FOUND] %s | %s", item['name'][:60], item['price'])
                
            return html
        except Exception as e:
            logger.error("Rozetka Selenium error: %s", e)
            return ""
        finally:
            if driver:
                driver.quit()

    def _scroll_to_bottom(self, driver):
        """Поступовий скрол для підвантаження лінивих картинок."""
        try:
            last_height = driver.execute_script("return document.body.scrollHeight")
            for _ in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.5)
                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height:
                    break
                last_height = new_height
        except:
            pass

    def _parse(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        products = []

        cards = (
            soup.select("li.goods-tile")
            or soup.select("div.goods-tile")
            or soup.select("[class*='goods-tile']")
        )

        for card in cards:
            try:
                name_tag = (
                    card.select_one("span.goods-tile__title")
                    or card.select_one(".goods-tile__title")
                    or card.select_one("a.goods-tile__heading")
                )
                name = name_tag.get_text(strip=True) if name_tag else ""
                if not name:
                    continue

                # Перевірка наявності (пропускаємо, якщо немає в наявності)
                availability_tag = card.select_one(".goods-tile__availability")
                if availability_tag:
                    availability_text = availability_tag.get_text(strip=True).lower()
                    if "немає в наявності" in availability_text or "немає" in availability_text:
                        continue

                price_tag = (
                    card.select_one("span.goods-tile__price-value")
                    or card.select_one(".price__value")
                    or card.select_one("[class*='price-value']")
                )
                price_text = price_tag.get_text(strip=True) if price_tag else ""
                
                # Якщо ціни немає або замість неї текст про відсутність
                if not price_text or "немає" in price_text.lower():
                    continue

                # Вичищаємо ціну від зайвих символів
                price_text = re.sub(r'[^\d\s]', '', price_text).strip()
                price = f"{price_text} грн" if price_text else "Ціна не вказана"
                if not price_text: # Ще раз перевіряємо після вичистки
                    continue

                link_tag = (
                    card.select_one("a.goods-tile__heading")
                    or card.select_one("a.goods-tile__title")
                    or card.select_one("a[href*='rozetka']")
                )
                url = link_tag["href"] if link_tag else ""
                if url and not url.startswith("http"):
                    url = "https://rozetka.com.ua" + url

                # Картинка
                img_tag = card.select_one("img.goods-tile__picture") or card.select_one("img")
                image_url = ""
                if img_tag:
                    image_url = img_tag.get("src") or img_tag.get("data-src") or ""

                # Продавець
                seller_tag = card.select_one(".goods-tile__seller")
                seller = seller_tag.get_text(strip=True) if seller_tag else "Rozetka"
                
                # ID товару
                product_id = card.get("data-goods-id") or ""

                products.append({
                    "name": name,
                    "price": price,
                    "seller": seller,
                    "city": "",
                    "url": url,
                    "image_url": image_url,
                    "platform": PLATFORM,
                    "product_id": product_id
                })
            except Exception:
                continue

        return products
