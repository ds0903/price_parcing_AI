import json
import logging
import re
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from config import HEADLESS

logger = logging.getLogger(__name__)


class PromScraper:
    BASE_URL = "https://prom.ua/ua/search"

    def search_products(self, query: str) -> list[dict]:
        """Search products on prom.ua using a real browser and return top-10 results."""
        url = f"{self.BASE_URL}?search_term={query}"
        html = self._fetch_html(url)
        if not html:
            return []

        products = self._parse_next_data(html)
        if not products:
            products = self._parse_html_cards(html)

        return products[:10]

    def _fetch_html(self, url: str) -> str:
        """Open the page in Playwright and return rendered HTML."""
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=HEADLESS)
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

                # Wait for product cards to appear (or timeout gracefully)
                try:
                    page.wait_for_selector(
                        "[data-qaid='product_block'], article",
                        timeout=10_000,
                    )
                except PWTimeout:
                    logger.warning("Product cards did not appear in time, continuing anyway")

                html = page.content()
                browser.close()
                return html
        except Exception as e:
            logger.error("Playwright error: %s", e)
            return ""

    # ------------------------------------------------------------------ #
    #  Parsers                                                             #
    # ------------------------------------------------------------------ #

    def _parse_next_data(self, html: str) -> list[dict]:
        """Try to extract products from __NEXT_DATA__ JSON embedded in the page."""
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not match:
            return []

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []

        products = []
        try:
            page_props = data.get("props", {}).get("pageProps", {})
            items = (
                page_props.get("products")
                or page_props.get("items")
                or page_props.get("searchResults", {}).get("products")
                or []
            )
            for item in items:
                product = self._extract_from_next_item(item)
                if product:
                    products.append(product)
        except Exception as e:
            logger.warning("Error parsing __NEXT_DATA__: %s", e)

        return products

    def _extract_from_next_item(self, item: dict) -> dict | None:
        try:
            name = item.get("name") or item.get("title") or ""
            if not name:
                return None

            price_raw = (
                item.get("price")
                or item.get("minPrice")
                or item.get("prices", {}).get("price")
                or 0
            )
            price = self._format_price(price_raw)

            seller = (
                item.get("company", {}).get("name")
                or item.get("seller", {}).get("name")
                or item.get("shop", {}).get("name")
                or "Невідомий продавець"
            )

            url = item.get("url") or item.get("href") or ""
            if url and not url.startswith("http"):
                url = "https://prom.ua" + url

            image_url = ""
            if item.get("images"):
                image_url = item["images"][0].get("url", "")
            else:
                image_url = item.get("image") or item.get("mainImage") or ""

            return {"name": name, "price": price, "seller": seller, "url": url, "image_url": image_url}
        except Exception:
            return None

    def _parse_html_cards(self, html: str) -> list[dict]:
        """Fallback: parse product cards directly from rendered HTML."""
        soup = BeautifulSoup(html, "lxml")
        products = []

        cards = soup.select("[data-qaid='product_block']") or soup.select("article")

        for card in cards:
            try:
                name_tag = (
                    card.select_one("[data-qaid='product_name']")
                    or card.select_one("a[title]")
                    or card.select_one("h2")
                    or card.select_one("h3")
                )
                name = name_tag.get_text(strip=True) if name_tag else ""
                if not name:
                    continue

                price_tag = (
                    card.select_one("[data-qaid='product_price']")
                    or card.select_one(".price")
                )
                price = price_tag.get_text(strip=True) if price_tag else "Ціна не вказана"

                seller_tag = (
                    card.select_one("[data-qaid='company_name']")
                    or card.select_one(".company-name")
                )
                seller = seller_tag.get_text(strip=True) if seller_tag else "Невідомий продавець"

                link_tag = card.select_one("a[href]")
                url = link_tag["href"] if link_tag else ""
                if url and not url.startswith("http"):
                    url = "https://prom.ua" + url

                img_tag = card.select_one("img[src]")
                image_url = img_tag.get("src", "") if img_tag else ""

                products.append({"name": name, "price": price, "seller": seller, "url": url, "image_url": image_url})
            except Exception:
                continue

        return products

    @staticmethod
    def _format_price(price_raw) -> str:
        if isinstance(price_raw, (int, float)):
            return f"{price_raw:,.0f} грн".replace(",", " ")
        return str(price_raw).strip() or "Ціна не вказана"
