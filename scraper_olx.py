import logging
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from config import HEADLESS

logger = logging.getLogger(__name__)

PLATFORM = "olx"


class OLXScraper:
    BASE_URL = "https://www.olx.ua/uk/list/q-{}/"

    def search_products(self, query: str) -> list[dict]:
        url = self.BASE_URL.format(quote(query))
        html = self._fetch_html(url)
        if not html:
            return []
        return self._parse(html)[:10]

    def _fetch_html(self, url: str) -> str:
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
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_selector("[data-cy='l-card'], [data-testid='listing-grid']", timeout=10_000)
                except PWTimeout:
                    logger.warning("OLX: product cards did not appear in time")
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            logger.error("OLX Playwright error: %s", e)
            return ""

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
                seller = location_tag.get_text(strip=True).split("-")[0].strip() if location_tag else "OLX"

                link_tag = card.select_one("a[href]")
                url = link_tag["href"] if link_tag else ""
                if url and not url.startswith("http"):
                    url = "https://www.olx.ua" + url

                img_tag = card.select_one("img[src]")
                image_url = img_tag.get("src", "") if img_tag else ""

                products.append({
                    "name": name, "price": price, "seller": seller,
                    "url": url, "image_url": image_url, "platform": PLATFORM,
                })
            except Exception:
                continue

        return products
