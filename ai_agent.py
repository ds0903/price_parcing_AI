import logging
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = (
    "Ти — розумний асистент для пошуку товарів на prom.ua. "
    "Ти допомагаєш користувачам знаходити товари, аналізувати ціни та робити вигідні покупки. "
    "Відповідай українською мовою. Будь конкретним і корисним. "
    "Пам'ятай попередні результати пошуку в розмові."
)

# Maximum number of user↔model exchange pairs kept in memory per user.
# 15 pairs = 30 Content objects → ~3-5k tokens of history, enough for context
# without slowing down or increasing cost after long conversations.
MAX_HISTORY_PAIRS = 15


class GeminiAgent:
    def __init__(self):
        # user_id -> list[types.Content]  (alternating user / model)
        self._history: dict[int, list] = {}

    # ------------------------------------------------------------------ #
    #  History management                                                  #
    # ------------------------------------------------------------------ #

    def reset_chat(self, user_id: int) -> None:
        """Clear conversation history (called by /reboot and /start)."""
        self._history.pop(user_id, None)

    def _get_history(self, user_id: int) -> list:
        return self._history.setdefault(user_id, [])

    def _append(self, user_id: int, role: str, text: str) -> None:
        history = self._get_history(user_id)
        history.append(
            types.Content(role=role, parts=[types.Part.from_text(text=text)])
        )
        # Trim to last MAX_HISTORY_PAIRS pairs (2 Content objects per pair)
        limit = MAX_HISTORY_PAIRS * 2
        if len(history) > limit:
            self._history[user_id] = history[-limit:]

    def _send(self, user_id: int, user_text: str) -> str:
        """Send a message with rolling history and store the reply."""
        history = self._get_history(user_id)
        contents = history + [
            types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
        ]
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
        reply = response.text.strip()
        self._append(user_id, "user", user_text)
        self._append(user_id, "model", reply)
        return reply

    # ------------------------------------------------------------------ #
    #  Public methods                                                      #
    # ------------------------------------------------------------------ #

    def identify_product_from_image(self, image_bytes: bytes) -> str:
        """Use Gemini Vision to identify a product name from a photo."""
        prompt = (
            "Подивись на це зображення та визнач, який товар на ньому зображено. "
            "Надай коротку назву товару українською мовою (1–5 слів), "
            "придатну для пошуку в інтернет-магазині. "
            "Відповідь — лише назва товару, без пояснень."
        )
        try:
            image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[prompt, image_part],
            )
            return response.text.strip()
        except Exception as e:
            logger.error("Gemini Vision error: %s", e)
            return ""

    def analyze_prices(self, user_id: int, query: str, products: list[dict]) -> str:
        """Analyse prices and inject results into rolling history for follow-ups."""
        if not products:
            return "Не вдалося знайти товари для аналізу."

        lines = [f"{i}. {p['name']} | {p['price']} | {p['seller']}"
                 for i, p in enumerate(products, 1)]
        products_text = "\n".join(lines)

        prompt = (
            f"Користувач шукає: \"{query}\". Ось результати з prom.ua:\n\n{products_text}\n\n"
            "Проаналізуй ціни у такому форматі (лише цей блок):\n"
            "• Мінімальна ціна: ...\n"
            "• Максимальна ціна: ...\n"
            "• Середня ціна: ...\n"
            "• ✅ Рекомендація: ...\n"
            "• 💡 Висновок: ..."
        )
        try:
            return self._send(user_id, prompt)
        except Exception as e:
            logger.error("Gemini analyze error: %s", e)
            return "Помилка AI-аналізу. Перевірте GEMINI_API_KEY."

    def follow_up(self, user_id: int, message: str) -> str:
        """Answer a follow-up question using the existing rolling history."""
        try:
            return self._send(user_id, message)
        except Exception as e:
            logger.error("Gemini follow-up error: %s", e)
            return "Помилка AI. Спробуйте /reboot"
