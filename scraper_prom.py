import json
import logging
import re
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from config import HEADLESS

logger = logging.getLogger(__name__)

PLATFORM = "prom"
_MAX_PAGES = 10


class PromScraper:
    BASE_URL = "https://prom.ua/ua/search"

    def search_page(self, query: str, page: int) -> list[dict]:
        """Single page with scroll — used by Excel collector."""
        url = f"{self.BASE_URL}?search_term={quote(query)}"
        if page > 1:
            url += f"&page={page}"
        html = self._fetch_page_html(url)
        return (self._parse_apollo_cache(html) or self._parse_next_data(html) or self._parse_html_cards(html)) if html else []

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
                
                # Покращене блокування ресурсів
                def block_aggressively(route):
                    bad_resource_types = ["image", "stylesheet", "font", "media", "other"]
                    bad_urls = ["google-analytics", "doubleclick", "facebook", "hotjar", "amplitude", "fullstory"]
                    
                    if route.request.resource_type in bad_resource_types:
                        return route.abort()
                    
                    url = route.request.url.lower()
                    if any(bad in url for bad in bad_urls):
                        return route.abort()
                        
                    return route.continue_()

                tab.route("**/*", block_aggressively)

                for page_num in range(1, _MAX_PAGES + 1):
                    url = f"{self.BASE_URL}?search_term={quote(query)}"
                    if page_num > 1:
                        url += f"&page={page_num}"
                    try:
                        # Чекаємо лише завантаження DOM, не чекаючи на всі скрипти
                        tab.goto(url, wait_until="commit", timeout=20_000)
                        try:
                            tab.wait_for_selector(
                                "[data-qaid='product_block'], article", timeout=10_000
                            )
                        except PWTimeout:
                            logger.warning("Prom p%d: cards timeout", page_num)
                        self._scroll_to_bottom(tab)
                        html = tab.content()
                        batch = self._parse_apollo_cache(html) or self._parse_next_data(html) or self._parse_html_cards(html)
                        if not batch:
                            break
                        products.extend(batch)
                        if limit and len(products) >= limit:
                            break
                    except Exception as e:
                        logger.error("Prom page %d error: %s", page_num, e)
                        break

                browser.close()
        except Exception as e:
            logger.error("Prom Playwright error: %s", e)

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
                try:
                    page.wait_for_selector(
                        "[data-qaid='product_block'], article", timeout=10_000
                    )
                except PWTimeout:
                    logger.warning("Prom: cards timeout on %s", url)
                self._scroll_to_bottom(page)
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            logger.error("Prom Playwright error: %s", e)
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

    def _parse_apollo_cache(self, html: str) -> list[dict]:
        """Parse Prom's Apollo GraphQL cache (window.ApolloCacheState)."""
        match = re.search(
            r'window\.ApolloCacheState\s*=\s*(.+?);\s*(?:window\.|</script>)',
            html, re.DOTALL,
        )
        if not match:
            return []
        try:
            data = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            return []

        products = []
        try:
            # Key looks like: "SearchListingQuery{\"variables\":...}"
            for key, value in data.items():
                if "SearchListingQuery" not in key:
                    continue
                raw_products = (
                    (value.get("result") or {})
                    .get("listing", {})
                    .get("page", {})
                    .get("products", [])
                )
                for item in raw_products:
                    p = self._extract_apollo_item(item)
                    if p:
                        products.append(p)
                if products:
                    break  # found the right query key
        except Exception as e:
            logger.warning("Prom Apollo cache parse error: %s", e)
        return products

    def _extract_apollo_item(self, item: dict) -> dict | None:
        """Extract product from Apollo cache item structure."""
        try:
            product = item.get("product") or {}
            company = item.get("company") or {}
            # Normalize to the same shape _extract_item expects
            normalized = {
                "name": product.get("name") or "",
                "price": product.get("price") or product.get("discountedPrice") or 0,
                "company": {"name": company.get("name", "")},
                "url": product.get("url") or product.get("href") or "",
                "images": product.get("images") or [],
            }
            return self._extract_item(normalized)
        except Exception:
            return None

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
                (item.get("company") or {}).get("name")
                or (item.get("seller") or {}).get("name")
                or (item.get("shop") or {}).get("name")
                or (item.get("merchant") or {}).get("name")
                or item.get("company_name")
                or "Невідомий продавець"
            )
            url = item.get("url") or item.get("href") or ""
            if url and not url.startswith("http"):
                url = "https://prom.ua" + url
            # Витягуємо унікальний ID товару з URL: /p1835650547-name.html → "1835650547"
            product_id = ""
            if url:
                m = re.search(r'/p(\d+)-', url)
                if m:
                    product_id = m.group(1)
            image_url = (
                (item.get("images") or [{}])[0].get("url", "")
                if item.get("images") else item.get("image", "")
            )
            return {"name": name, "price": price, "seller": seller, "city": "",
                    "url": url, "image_url": image_url, "platform": PLATFORM,
                    "product_id": product_id}
        except Exception:
            return None

    def _parse_html_cards(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        products = []
        cards = soup.select("[data-qaid='product_block']") or soup.select("article")
        for card in cards:
            try:
                name_tag = (
                    card.select_one("[data-qaid='product_name']")
                    or card.select_one("a[title]")
                    or card.select_one("h2") or card.select_one("h3")
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
                    or card.select_one("a[data-qaid='company_link']")
                    or card.select_one("[class*='company']")
                    or card.select_one("[class*='seller']")
                    or card.select_one("[class*='shop']")
                )
                seller = seller_tag.get_text(strip=True) if seller_tag else "Невідомий продавець"
                link_tag = card.select_one("a[href]")
                url = link_tag["href"] if link_tag else ""
                if url and not url.startswith("http"):
                    url = "https://prom.ua" + url
                img_tag = card.select_one("img[src]")
                image_url = img_tag.get("src", "") if img_tag else ""
                product_id = ""
                if url:
                    m = re.search(r'/p(\d+)-', url)
                    if m:
                        product_id = m.group(1)
                products.append({"name": name, "price": price, "seller": seller, "city": "",
                                 "url": url, "image_url": image_url, "platform": PLATFORM,
                                 "product_id": product_id})
            except Exception:
                continue
        return products

    @staticmethod
    def _fmt(v) -> str:
        if isinstance(v, (int, float)):
            return f"{v:,.0f} грн".replace(",", " ")
        return str(v).strip() or "Ціна не вказана"
