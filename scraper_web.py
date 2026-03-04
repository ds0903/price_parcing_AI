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
            # Google Shopping детектує headless і блокує → завжди видимий браузер
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

                # --- RAW EXTRACTION via JavaScript ---
                current_url = driver.current_url
                logger.info(f"Extracting RAW text blocks | URL: {current_url[:120]}")

                # JS: два методи паралельно
                # Метод 1 — напряму шукаємо leaf-елементи з ціною (грн/₴) і підіймаємось до картки
                # Метод 2 — через посилання (запасний, як раніше)
                raw_results = driver.execute_script("""
                    var results = [];
                    var seenNorm = new Set();
                    var PRICE_RE = /\\d[\\d\\s]*[,.]?\\d*\\s*(\\u0433\\u0440\\u043d|\\u20B4|UAH)/i;

                    function bestUrl(el, fallback) {
                        // Спочатку шукаємо зовнішнє посилання (не google/gstatic)
                        var links = el.querySelectorAll('a[href]');
                        var shopUrl = '';
                        var googleUrl = '';
                        for (var j = 0; j < links.length; j++) {
                            var h = links[j].getAttribute('href') || '';
                            if (!h || h.startsWith('#') || h.startsWith('javascript')) continue;
                            if (h.indexOf('gstatic.') !== -1) continue;
                            if (h.indexOf('google.') === -1) { shopUrl = h; break; }
                            if (h.indexOf('/shopping/product') !== -1 && !googleUrl) {
                                googleUrl = 'https://www.google.com.ua' + h;
                            }
                        }
                        var url = shopUrl || fallback || googleUrl;
                        // Відносний URL → абсолютний
                        if (url && url.startsWith('/')) url = 'https://www.google.com.ua' + url;
                        return url;
                    }

                    function tryAdd(el, hintUrl) {
                        var text = (el.innerText || '').trim();
                        if (!PRICE_RE.test(text) || text.length < 20 || text.length > 800) return false;
                        var norm = text.replace(/[\\s\\W]+/g, '').slice(0, 60).toLowerCase();
                        if (seenNorm.has(norm)) return true;
                        seenNorm.add(norm);
                        var href = bestUrl(el, hintUrl);
                        results.push({ raw_text: text.replace(/\\n/g, ' | '), url: href });
                        return true;
                    }

                    // Метод 1: шукаємо всі текстові вузли з ціною → піднімаємось до картки
                    document.querySelectorAll('*').forEach(function(el) {
                        if (el.childElementCount > 0) return; // тільки leaf-елементи
                        var own = (el.innerText || '').trim();
                        if (!PRICE_RE.test(own) || own.length > 80) return;
                        // Знайшли leaf з ціною — піднімаємось до контейнера
                        var cur = el.parentElement;
                        for (var i = 0; i < 10; i++) {
                            if (!cur || cur === document.body) break;
                            var t = (cur.innerText || '').trim();
                            if (t.length >= 30 && t.length <= 800 && PRICE_RE.test(t)) {
                                // Перевіряємо що це не занадто великий блок (вся сторінка)
                                if (cur.childElementCount <= 30) { tryAdd(cur); break; }
                            }
                            cur = cur.parentElement;
                        }
                    });

                    // Метод 2: через посилання (ловить те що метод 1 пропустив)
                    document.querySelectorAll('a[href]').forEach(function(a) {
                        var href = a.getAttribute('href') || '';
                        if (!href || href.startsWith('#') || href.startsWith('javascript')) return;
                        if (href.indexOf('gstatic.') !== -1) return;
                        if (href.indexOf('google.') !== -1 && href.indexOf('/shopping/product') === -1) return;
                        // Для зовнішніх URL — передаємо як підказку
                        var hintUrl = (href.indexOf('google.') === -1) ? href : '';
                        var el = a;
                        for (var d = 0; d < 8; d++) {
                            el = el.parentElement;
                            if (!el || el === document.body) break;
                            var t = (el.innerText || '').trim();
                            if (PRICE_RE.test(t) && t.length >= 20 && t.length <= 800) {
                                tryAdd(el, hintUrl); break;
                            }
                        }
                    });

                    return results;
                """) or []

                logger.info(f"JS extraction: {len(raw_results)} блоків до дедупу")

                # Python дедуп (на випадок якщо JS не добив усі дублікати)
                unique_raw = []
                seen_py = set()
                for r in raw_results:
                    norm = re.sub(r'[\s\W]+', '', r['raw_text'])[:60].lower()
                    key = norm if norm else r['url']
                    if key not in seen_py:
                        seen_py.add(key)
                        unique_raw.append(r)
                
                logger.info(f"✅ RAW: {len(raw_results)} блоків → після дедупу: {len(unique_raw)} унікальних")
                print(f"\n{'='*60}")
                print(f"[WEB SCRAPER] Запит: '{query}'")
                print(f"[WEB SCRAPER] RAW: {len(raw_results)} | Унікальних: {len(unique_raw)}")
                for i, r in enumerate(unique_raw, 1):
                    preview = r['raw_text'][:120].replace('\n', ' ')
                    print(f"  {i:>3}. {preview}")
                print(f"{'='*60}\n")
                return unique_raw

            finally:
                driver.quit()
        except Exception as e:
            logger.error("❌ Scraping error: %s", e)
        return raw_results

    def search_page(self, query: str, page: int) -> list[dict]:
        return [] # Not used for this method
