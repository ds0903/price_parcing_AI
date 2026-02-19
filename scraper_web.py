import logging
import re
from urllib.parse import urlparse
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

PLATFORM = "web"


class WebScraper:
    """Search across the whole internet via DuckDuckGo — no API key required."""

    def search_products(self, query: str, limit: int = 10) -> list[dict]:
        search_query = f"{query} ціна купити Україна грн"
        try:
            raw = list(DDGS().text(search_query, max_results=limit or 100))
        except Exception as e:
            logger.error("DuckDuckGo search error: %s", e)
            return []

        products = []
        for r in raw:
            title = r.get("title", "").strip()
            if not title:
                continue
            body = r.get("body", "")
            url = r.get("href", "")
            products.append({
                "name": title,
                "price": self._extract_price(body),
                "seller": self._domain(url),
                "city": "",
                "url": url,
                "image_url": "",
                "platform": PLATFORM,
            })
        return products

    @staticmethod
    def _extract_price(text: str) -> str:
        # Match patterns like "1 500 грн", "25999 грн", "від 999 грн"
        match = re.search(r'(\d[\d\s]{0,8}\d)\s*грн', text, re.IGNORECASE)
        return f"{match.group(1).strip()} грн" if match else "Ціна не вказана"

    @staticmethod
    def _domain(url: str) -> str:
        try:
            return urlparse(url).netloc.replace("www.", "")
        except Exception:
            return "Інтернет"
