import logging
from pathlib import Path
import yaml
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

client = genai.Client(api_key=GEMINI_API_KEY)

# Load prompts from YAML once at startup
_PROMPTS: dict = yaml.safe_load(
    (Path(__file__).parent / "prompts.yaml").read_text(encoding="utf-8")
)

MAX_HISTORY_PAIRS = 15


class GeminiAgent:
    def __init__(self):
        self._history: dict[int, list] = {}

    # ------------------------------------------------------------------ #
    #  History management                                                  #
    # ------------------------------------------------------------------ #

    def reset_chat(self, user_id: int) -> None:
        self._history.pop(user_id, None)

    def _get_history(self, user_id: int) -> list:
        return self._history.setdefault(user_id, [])

    def _append(self, user_id: int, role: str, text: str) -> None:
        history = self._get_history(user_id)
        history.append(
            types.Content(role=role, parts=[types.Part.from_text(text=text)])
        )
        limit = MAX_HISTORY_PAIRS * 2
        if len(history) > limit:
            self._history[user_id] = history[-limit:]

    def _send(self, user_id: int, user_text: str) -> str:
        history = self._get_history(user_id)
        contents = history + [
            types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
        ]
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_PROMPTS["system_prompt"]
            ),
        )
        reply = response.text.strip()
        self._append(user_id, "user", user_text)
        self._append(user_id, "model", reply)
        return reply

    def _query_once(self, user_id: int, prompt: str) -> str:
        """One-shot query using conversation history as context but without saving the reply."""
        history = self._get_history(user_id)
        contents = history + [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        ]
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_PROMPTS["system_prompt"]
            ),
        )
        return response.text.strip()

    # ------------------------------------------------------------------ #
    #  Public methods                                                      #
    # ------------------------------------------------------------------ #

    def identify_product_from_image(self, image_bytes: bytes) -> str:
        try:
            image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[_PROMPTS["identify_product"], image_part],
            )
            return response.text.strip()
        except Exception as e:
            logger.error("Gemini Vision error: %s", e)
            return ""

    def analyze_prices(self, user_id: int, query: str, products: list[dict], platform: str = "prom") -> str:
        if not products:
            return "Не вдалося знайти товари для аналізу."

        lines = [f"{i}. {p['name']} | {p['price']} | {p['seller']}"
                 for i, p in enumerate(products, 1)]

        prompt = _PROMPTS["analyze_prices"].format(
            query=query,
            platform=platform,
            products="\n".join(lines),
        )
        try:
            return self._send(user_id, prompt)
        except Exception as e:
            logger.error("Gemini analyze error: %s", e)
            return "Помилка AI-аналізу. Перевірте GEMINI_API_KEY."

    def follow_up(self, user_id: int, message: str) -> str:
        try:
            return self._send(user_id, message)
        except Exception as e:
            logger.error("Gemini follow-up error: %s", e)
            return "Помилка AI. Спробуйте /reboot"

    def extract_search_query(self, user_id: int, filter_hint: str = "") -> str:
        """Extract product name from conversation context, optionally with a filter hint."""
        hint = f" Користувач уточнив: '{filter_hint}'." if filter_hint else ""
        try:
            return self._query_once(
                user_id,
                f"Виходячи з нашої розмови, який саме товар потрібно знайти?{hint} "
                "Відповідь — лише назва товару для пошуку (1-7 слів), без пояснень і зайвого тексту.",
            )
        except Exception as e:
            logger.error("Gemini extract_search_query error: %s", e)
            return ""

    def filter_products_by_intent(self, user_id: int, products: list[dict], query: str) -> list[dict]:
        """Filter product list to only items matching user's actual intent from conversation."""
        if not products:
            return products
        lines = "\n".join(f"{i}. {p['name']}" for i, p in enumerate(products, 1))
        prompt = (
            f"Запит: '{query}'. Список знайдених товарів:\n{lines}\n\n"
            "Виходячи з нашої розмови, поверни номери товарів (через кому), "
            "які ТОЧНО відповідають тому що шукає користувач. "
            "Якщо всі підходять — поверни 'all'. Відповідь: лише числа або 'all', без пояснень."
        )
        try:
            reply = self._query_once(user_id, prompt).strip().lower()
            if "all" in reply:
                return products
            indices = []
            for part in reply.replace(";", ",").split(","):
                part = part.strip()
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(products):
                        indices.append(idx)
            return [products[i] for i in indices] if indices else products
        except Exception as e:
            logger.error("filter_products_by_intent error: %s", e)
            return products
