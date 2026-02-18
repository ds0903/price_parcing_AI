import logging
import google.generativeai as genai
from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)


class GeminiAgent:
    def __init__(self):
        self.vision_model = genai.GenerativeModel(GEMINI_MODEL)
        self.text_model = genai.GenerativeModel(GEMINI_MODEL)

    def identify_product_from_image(self, image_bytes: bytes) -> str:
        """Use Gemini Vision to identify a product name from a photo."""
        prompt = (
            "Подивись на це зображення та визнач, який товар на ньому зображено. "
            "Надай коротку назву товару українською мовою (1–5 слів), "
            "придатну для пошуку в інтернет-магазині. "
            "Відповідь — лише назва товару, без пояснень."
        )
        try:
            image_part = {"mime_type": "image/jpeg", "data": image_bytes}
            response = self.vision_model.generate_content([prompt, image_part])
            return response.text.strip()
        except Exception as e:
            logger.error("Gemini Vision error: %s", e)
            return ""

    def analyze_prices(self, query: str, products: list[dict]) -> str:
        """Use Gemini to analyse collected prices and provide a recommendation."""
        if not products:
            return "Не вдалося знайти товари для аналізу."

        product_lines = []
        for i, p in enumerate(products, 1):
            product_lines.append(
                f"{i}. {p['name']} | {p['price']} | {p['seller']}"
            )
        products_text = "\n".join(product_lines)

        prompt = (
            f"Ти — експерт з онлайн-шопінгу. Проаналізуй ціни на товар \"{query}\" "
            f"зібрані з prom.ua:\n\n{products_text}\n\n"
            "Надай аналіз у такому форматі (лише цей блок, без зайвого тексту):\n"
            "• Мінімальна ціна: ...\n"
            "• Максимальна ціна: ...\n"
            "• Середня ціна: ...\n"
            "• ✅ Рекомендація: ...\n"
            "• 💡 Висновок: ...\n\n"
            "Якщо ціни вказані не числами, зроби висновок на основі доступної інформації."
        )

        try:
            response = self.text_model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            logger.error("Gemini text error: %s", e)
            return "Помилка AI-аналізу. Перевірте GEMINI_API_KEY."
