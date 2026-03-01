import logging
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from config import HEADLESS

logger = logging.getLogger(__name__)

PLATFORM = "rozetka"
_MAX_PAGES = 5


class RozetkaScraper:
    BASE_URL = "https://rozetka.com.ua/ua/search/?text={}&page={}"

    def search_page(self, query: str, page: int) -> list[dict]:
        """Single page with scroll — used by Excel collector."""
        url = self.BASE_URL.format(quote(query), page)
        html = self._fetch_page_html(url)
        return self._parse(html) if html else []

    def search_products(self, query: str, limit: int = 10) -> list[dict]:
        """Multi-page search keeping ONE browser open for all pages."""
        products: list[dict] = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="chrome", headless=HEADLESS)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="uk-UA",
                )
                tab = context.new_page()
                
                def block_aggressively(route):
                    if route.request.resource_type in ["image", "stylesheet", "font", "media"]:
                        return route.abort()
                    url = route.request.url.lower()
                    if any(bad in url for bad in ["google", "facebook", "analytics", "hotjar"]):
                        return route.abort()
                    return route.continue_()

                tab.route("**/*", block_aggressively)

                for page_num in range(1, _MAX_PAGES + 1):
                    url = self.BASE_URL.format(quote(query), page_num)
                    try:
                        tab.goto(url, wait_until="commit", timeout=20_000)
                        tab.wait_for_timeout(600)
                        try:
                            tab.wait_for_selector(
                                "li.goods-tile, .goods-tile__inner", timeout=15_000
                            )
                        except PWTimeout:
                            logger.warning("Rozetka p%d: cards timeout", page_num)
                        self._scroll_to_bottom(tab)
                        html = tab.content()
                        batch = self._parse(html)
                        if not batch:
                            break
                        products.extend(batch)
                        if limit and len(products) >= limit:
                            break
                    except Exception as e:
                        logger.error("Rozetka page %d error: %s", page_num, e)
                        break

                browser.close()
        except Exception as e:
            logger.error("Rozetka Playwright error: %s", e)

        return products[:limit] if limit else products

    # ------------------------------------------------------------------ #

    def _fetch_page_html(self, url: str) -> str:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="chrome", headless=HEADLESS)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="uk-UA",
                )
                page = context.new_page()
                
                def block_aggressively(route):
                    if route.request.resource_type in ["image", "stylesheet", "font", "media"]:
                        return route.abort()
                    return route.continue_()

                page.route("**/*", block_aggressively)
                page.goto(url, wait_until="commit", timeout=20_000)
                page.wait_for_timeout(600)
                try:
                    page.wait_for_selector(
                        "li.goods-tile, .goods-tile__inner", timeout=15_000
                    )
                except PWTimeout:
                    logger.warning("Rozetka: cards timeout on %s", url)
                self._scroll_to_bottom(page)
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            logger.error("Rozetka Playwright error: %s", e)
            return ""

    @staticmethod
    def _scroll_to_bottom(page) -> None:
        try:
            page.evaluate("""
                () => new Promise(resolve => {
                    let total = 0;
                    const step = 400;
                    const delay = 150;
                    const timer = setInterval(() => {
                        window.scrollBy(0, step);
                        total += step;
                        if (total >= document.body.scrollHeight) {
                            clearInterval(timer);
                            resolve();
                        }
                    }, delay);
                })
            """)
            page.wait_for_timeout(500)
        except Exception:
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

                price_tag = (
                    card.select_one("span.goods-tile__price-value")
                    or card.select_one(".price__value")
                    or card.select_one("[class*='price-value']")
                )
                price_text = price_tag.get_text(strip=True) if price_tag else ""
                price = (
                    f"{price_text} грн"
                    if price_text and "грн" not in price_text.lower()
                    else (price_text or "Ціна не вказана")
                )

                link_tag = (
                    card.select_one("a.goods-tile__heading")
                    or card.select_one("a.goods-tile__title")
                    or card.select_one("a[href*='rozetka']")
                )
                url = link_tag["href"] if link_tag else ""
                if url and not url.startswith("http"):
                    url = "https://rozetka.com.ua" + url

                products.append({
                    "name": name,
                    "price": price,
                    "url": url,
                    "image_url": "",
                    "platform": PLATFORM,
                })
            except Exception:
                continue

        return products
