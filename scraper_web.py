import logging
import re
import time
import random
import httpx
import asyncio
from urllib.parse import urlparse
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from config import PROXY_URL, PROXY_ROTATE_URL, PROXY_ROTATE_ENABLED, BROWSER_SESSION_PATH, PROXY_ENABLED, HEADLESS

logger = logging.getLogger(__name__)

PLATFORM = "web"


class WebScraper:
    """Super-Greedy Search (Collects RAW text blocks for AI parsing)."""

    def _rotate_proxy_ip(self) -> None:
        if PROXY_ENABLED and PROXY_ROTATE_ENABLED and PROXY_ROTATE_URL:
            try:
                httpx.get(PROXY_ROTATE_URL, timeout=10)
                time.sleep(2) 
            except: pass

    def open_google_manual(self, query: str) -> list[dict]:
        """Opens Google Shopping, scrolls, collects RAW product blocks and URLs."""
        logger.info("🚀 Starting RAW-BLOCK scraping for AI parsing: %s", query)
        raw_results = []
        try:
            self._rotate_proxy_ip()

            from pathlib import Path
            user_data_dir = Path(BROWSER_SESSION_PATH).resolve()
            user_data_dir.mkdir(parents=True, exist_ok=True)

            options = uc.ChromeOptions()
            options.add_argument(f"--user-data-dir={user_data_dir}")
            options.add_argument("--disable-notifications")
            options.add_argument("--lang=uk-UA")
            if HEADLESS:
                options.add_argument("--headless=new")
                options.add_argument("--window-size=1366,900")
            else:
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
                for char in query:
                    sb.send_keys(char)
                    time.sleep(random.uniform(0.05, 0.15))
                sb.submit()
                
                time.sleep(5)

                # Нескінченний скролінг
                last_height = driver.execute_script("return document.body.scrollHeight")
                for _ in range(10): # До 10 глибоких скролів
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(2.5)
                    try:
                        btn = driver.find_element(By.CSS_SELECTOR, "button.GN77nd, .m67it")
                        if btn.is_displayed(): btn.click()
                    except: pass
                    new_height = driver.execute_script("return document.body.scrollHeight")
                    if new_height == last_height: break
                    last_height = new_height

                # --- RAW EXTRACTION ---
                logger.info("Extracting RAW text blocks from all potential product cards...")
                
                # Шукаємо всі можливі контейнери товарів
                cards = driver.find_elements(By.XPATH, "//div[.//a[contains(@href, 'aclk') or contains(@href, '/shopping/product')]]")
                
                for card in cards:
                    try:
                        # Беремо весь текст з картки "як є"
                        raw_text = card.text.replace("\n", " | ").strip()
                        if not raw_text or len(raw_text) < 20: continue

                        # Посилання забираємо кодом, бо AI його не витягне з тексту
                        try:
                            url = card.find_element(By.TAG_NAME, "a").get_attribute("href")
                        except: url = ""

                        raw_results.append({
                            "raw_text": raw_text,
                            "url": url
                        })
                    except: continue

                # Унікалізація за текстом та URL
                unique_raw = []
                seen = set()
                for r in raw_results:
                    # Створюємо ключ з перших 50 символів тексту + URL
                    key = f"{r['raw_text'][:50]}_{r['url']}"
                    if key not in seen:
                        seen.add(key)
                        unique_raw.append(r)
                
                logger.info(f"✅ Collected {len(unique_raw)} raw blocks for AI.")
                return unique_raw

            finally:
                driver.quit()
        except Exception as e:
            logger.error("❌ Scraping error: %s", e)
        return raw_results

    def search_page(self, query: str, page: int) -> list[dict]:
        return [] # Not used for this method
