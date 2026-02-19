"""
scraper.py — unified SearchManager.
Routes search requests to the correct platform scraper.
"""
import re
from scraper_prom import PromScraper
from scraper_olx import OLXScraper
from scraper_web import WebScraper
from scraper_rozetka import RozetkaScraper

# Platform keyword detection
_PLATFORM_KEYWORDS: dict[str, list[str]] = {
    "prom":    ["prom", "prom.ua", "пром", "промюа"],
    "olx":     ["olx", "олх", "олекс", "олх.юа"],
    "web":     ["інтернет", "internet", "гугл", "google", "скрізь", "всюди", "мережа", "всіх"],
    "rozetka": ["rozetka", "розетка", "rozetka.ua", "розетка.юа"],
}

# Words to strip when cleaning the query
_STRIP_WORDS = {
    "шукай", "шукайте", "знайди", "знайдіть",
    "пошукай", "пошукуй", "покажи", "покажіть",
    "найди", "починай", "почни", "пошук",
    "на", "в", "по", "через",
}
# Also strip platform keywords themselves
_ALL_STRIP = _STRIP_WORDS | {kw for kws in _PLATFORM_KEYWORDS.values() for kw in kws}

PLATFORM_LABELS = {
    "prom":    "Prom.ua 🛒",
    "olx":     "OLX 📦",
    "rozetka": "Rozetka 🔴",
    "web":     "Інтернет 🌐",
}


def detect_platform(text: str) -> str | None:
    """Return platform key if text contains a platform keyword, else None."""
    t = text.lower()
    for platform, keywords in _PLATFORM_KEYWORDS.items():
        if any(re.search(rf'\b{re.escape(kw)}\b', t) for kw in keywords):
            return platform
    return None


def clean_query(text: str) -> str:
    """Remove platform/navigation words from text to get the clean product query."""
    words = [w for w in text.split() if w.lower() not in _ALL_STRIP]
    return " ".join(words).strip()


class SearchManager:
    def __init__(self):
        self._scrapers = {
            "prom":    PromScraper(),
            "olx":     OLXScraper(),
            "rozetka": RozetkaScraper(),
            "web":     WebScraper(),
        }

    def search(self, query: str, platform: str = "prom", limit: int = 10) -> list[dict]:
        scraper = self._scrapers.get(platform, self._scrapers["prom"])
        return scraper.search_products(query, limit=limit)
