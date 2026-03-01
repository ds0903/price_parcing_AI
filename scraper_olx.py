import logging
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from config import HEADLESS

logger = logging.getLogger(__name__)

PLATFORM = "olx"
_MAX_PAGES = 10


class OLXScraper:
    BASE_URL = "https://www.olx.ua/uk/list/q-{}/"

    def search_page(self, query: str, page: int) -> list[dict]:
        """Single page — used by the Excel collector (browser-per-page, but with scroll)."""
        base = self.BASE_URL.format(quote(query))
        url = base if page == 1 else f"{base}?page={page}"
        html = self._fetch_page_html(url)
        return self._parse(html) if html else []

    def search_products(self, query: str, limit: int = 10) -> list[dict]:
        """Multi-page search keeping ONE browser open for all pages."""
        products: list[dict] = []
        base = self.BASE_URL.format(quote(query))

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
                    if any(bad in url for bad in ["analytics", "facebook", "google", "hotjar"]):
                        return route.abort()
                    return route.continue_()

                tab.route("**/*", block_aggressively)

                for page_num in range(1, _MAX_PAGES + 1):
                    url = base if page_num == 1 else f"{base}?page={page_num}"
                    try:
                        tab.goto(url, wait_until="commit", timeout=20_000)
                        try:
                            tab.wait_for_selector(
                                "[data-cy='l-card'], [data-testid='listing-grid']",
                                timeout=10_000,
                            )
                        except PWTimeout:
                            logger.warning("OLX p%d: cards timeout", page_num)

                        # Scroll to bottom so lazy-loaded cards appear
                        self._scroll_to_bottom(tab)

                        html = tab.content()
                        batch = self._parse(html)
                        if not batch:
                            break
                        products.extend(batch)
                        if limit and len(products) >= limit:
                            break
                    except Exception as e:
                        logger.error("OLX page %d error: %s", page_num, e)
                        break

                browser.close()
        except Exception as e:
            logger.error("OLX Playwright error: %s", e)

        return products[:limit] if limit else products

    # ------------------------------------------------------------------ #

    def _fetch_page_html(self, url: str) -> str:
        """Open browser, load page, scroll, return HTML, close."""
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
                try:
                    page.wait_for_selector(
                        "[data-cy='l-card'], [data-testid='listing-grid']",
                        timeout=10_000,
                    )
                except PWTimeout:
                    logger.warning("OLX: cards timeout on %s", url)
                self._scroll_to_bottom(page)
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            logger.error("OLX Playwright error: %s", e)
            return ""

    @staticmethod
    def _scroll_to_bottom(page) -> None:
        """Scroll gradually to trigger lazy-loaded cards."""
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
            soup.select("[data-cy='l-card']")
            or soup.select("li[data-testid]")
            or soup.select("div[class*='offer']")
        )

        for card in cards:
            try:
                name_tag = (
                    card.select_one("[data-cy='ad-card-title']")
                    or card.select_one("h4") or card.select_one("h6")
                    or card.select_one("a[title]")
                )
                name = name_tag.get_text(strip=True) if name_tag else ""
                if not name:
                    continue

                price_tag = (
                    card.select_one("[data-testid='ad-price']")
                    or card.select_one("p[data-testid='ad-price']")
                    or card.select_one("[class*='price']")
                )
                price = price_tag.get_text(strip=True) if price_tag else "Ціна не вказана"

                location_tag = card.select_one("[data-testid='location-date']")
                city = ""
                if location_tag:
                    loc_text = location_tag.get_text(strip=True)
                    city = loc_text.split("-")[0].strip()

                seller_tag = (
                    card.select_one("[data-testid='seller-link']")
                    or card.select_one("span[class*='Username']")
                    or card.select_one("p[class*='user-card']")
                    or card.select_one("[class*='userName']")
                )
                seller = seller_tag.get_text(strip=True) if seller_tag else "Приватна особа"

                link_tag = card.select_one("a[href]")
                url = link_tag["href"] if link_tag else ""
                if url and not url.startswith("http"):
                    url = "https://www.olx.ua" + url
                # Skip cross-border listings (Polish OLX, etc.)
                if url and "olx.ua" not in url:
                    continue

                img_tag = card.select_one("img[src]")
                image_url = img_tag.get("src", "") if img_tag else ""

                products.append({
                    "name": name, "price": price, "seller": seller, "city": city,
                    "url": url, "image_url": image_url, "platform": PLATFORM,
                })
            except Exception:
                continue

        return products
