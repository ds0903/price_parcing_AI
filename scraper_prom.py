import json
import logging
import math
import re
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from config import HEADLESS

logger = logging.getLogger(__name__)

PLATFORM = "prom"
_PER_PAGE = 36   # Prom shows ~36 items per page
_MAX_PAGES = 10  # safety cap


class PromScraper:
    BASE_URL = "https://prom.ua/ua/search"

    def search_page(self, query: str, page: int) -> list[dict]:
        url = f"{self.BASE_URL}?search_term={quote(query)}"
        if page > 1:
            url += f"&page={page}"
        html = self._fetch_html(url)
        return (self._parse_next_data(html) or self._parse_html_cards(html)) if html else []

    def search_products(self, query: str, limit: int = 10) -> list[dict]:
        products: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self.search_page(query, page)
            if not batch:
                break
            products.extend(batch)
            if limit and len(products) >= limit:
                break
        return products[:limit] if limit else products

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
                    page.wait_for_selector("[data-qaid='product_block'], article", timeout=10_000)
                except PWTimeout:
                    logger.warning("Prom: product cards did not appear in time")
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            logger.error("Prom Playwright error: %s", e)
            return ""

    def _parse_next_data(self, html: str) -> list[dict]:
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html, re.DOTALL,
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
                p = self._extract_item(item)
                if p:
                    products.append(p)
        except Exception as e:
            logger.warning("Prom __NEXT_DATA__ parse error: %s", e)
        return products

    def _extract_item(self, item: dict) -> dict | None:
        try:
            name = item.get("name") or item.get("title") or ""
            if not name:
                return None
            price_raw = (
                item.get("price") or item.get("minPrice")
                or item.get("prices", {}).get("price") or 0
            )
            price = self._fmt(price_raw)
            seller = (
                item.get("company", {}).get("name")
                or item.get("seller", {}).get("name")
                or item.get("shop", {}).get("name")
                or "Невідомий продавець"
            )
            url = item.get("url") or item.get("href") or ""
            if url and not url.startswith("http"):
                url = "https://prom.ua" + url
            image_url = (item.get("images") or [{}])[0].get("url", "") if item.get("images") else item.get("image", "")
            return {"name": name, "price": price, "seller": seller, "city": "",
                    "url": url, "image_url": image_url, "platform": PLATFORM}
        except Exception:
            return None

    def _parse_html_cards(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        products = []
        cards = soup.select("[data-qaid='product_block']") or soup.select("article")
        for card in cards:
            try:
                name_tag = (card.select_one("[data-qaid='product_name']")
                            or card.select_one("a[title]")
                            or card.select_one("h2") or card.select_one("h3"))
                name = name_tag.get_text(strip=True) if name_tag else ""
                if not name:
                    continue
                price_tag = card.select_one("[data-qaid='product_price']") or card.select_one(".price")
                price = price_tag.get_text(strip=True) if price_tag else "Ціна не вказана"
                seller_tag = card.select_one("[data-qaid='company_name']") or card.select_one(".company-name")
                seller = seller_tag.get_text(strip=True) if seller_tag else "Невідомий продавець"
                link_tag = card.select_one("a[href]")
                url = link_tag["href"] if link_tag else ""
                if url and not url.startswith("http"):
                    url = "https://prom.ua" + url
                img_tag = card.select_one("img[src]")
                image_url = img_tag.get("src", "") if img_tag else ""
                products.append({"name": name, "price": price, "seller": seller, "city": "",
                                 "url": url, "image_url": image_url, "platform": PLATFORM})
            except Exception:
                continue
        return products

    @staticmethod
    def _fmt(v) -> str:
        if isinstance(v, (int, float)):
            return f"{v:,.0f} грн".replace(",", " ")
        return str(v).strip() or "Ціна не вказана"
