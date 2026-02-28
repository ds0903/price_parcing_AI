import json
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

        lines = [f"{i}. {p['name']} | {p['price']}" + (f" | {p['seller']}" if p.get("seller") else "")
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

    def classify_intent(self, user_id: int, text: str) -> dict:
        """AI аналізує повідомлення і повертає структурований намір.

        Можливі дії:
          search        — знайти товар
          schedule_set  — встановити таймер автопошуку
          schedule_stop — зупинити таймер
          schedule_list — показати активні таймери
          chat          — звичайна розмова / запитання

        Повертає dict:
          {"action": str, "query": str, "interval_minutes": int|None, "platforms": list[str]}
        """
        prompt = (
            "Визнач намір користувача з повідомлення нижче.\n"
            "Поверни ЛИШЕ валідний JSON (без markdown, без ```) у форматі:\n"
            "{\n"
            '  "action": "search"|"schedule_set"|"schedule_stop"|"schedule_list"|"reboot"|"platform_switch"|"platform_info"|"chat",\n'
            '  "query": "чистий товарний запит (1-7 слів, без ввічливих слів, платформ, команд)",\n'
            '  "interval_minutes": число_або_null,\n'
            '  "platforms": [],\n'
            '  "filters": {\n'
            '    "weight_kg": число_або_null,\n'
            '    "price_min": число_або_null,\n'
            '    "price_max": число_або_null,\n'
            '    "brand": "рядок_або_null"\n'
            "  }\n"
            "}\n"
            "Правила:\n"
            "- platforms: лише явно згадані; якщо не згадано — []\n"
            "- query: лише назва товару, без 'будь ласка', назв платформ, команд\n"
            "- interval_minutes: хвилини між запусками (тільки для schedule_set)\n"
            "- weight_kg: якщо вказано вагу (напр. '14 кг' → 14, '500г' → 0.5)\n"
            "- price_min/price_max: якщо вказано ціну/діапазон у грн (без слова 'грн')\n"
            "- brand: якщо вказано конкретний бренд/модель (напр. 'Royal Canin', 'Nike Air Max')\n"
            "- reboot: якщо просить перезавантажити/перезапустити бота (перезавантаж, рестарт, restart, reboot тощо)\n"
            "- platform_switch: якщо просить змінити/переключити платформу (переключи на пром, шукай на олх, зміни на розетку тощо) — вкажи назву у полі platforms: [\"prom\"/\"olx\"/\"rozetka\"]\n"
            "- platform_info: якщо питає яка платформа зараз активна (яка платформа, де шукаємо тощо)\n"
            "- Якщо параметр не вказано — null\n"
            f'Повідомлення: "{text}"'
        )
        try:
            raw = self._query_once(user_id, prompt)
            # Прибираємо можливий markdown від моделі
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as e:
            logger.error("classify_intent error: %s | raw=%s", e, locals().get("raw", ""))
            return {"action": "chat", "query": "", "interval_minutes": None, "platforms": []}

    def extract_search_query(self, user_id: int, filter_hint: str = "") -> str:
        """Extract product name from conversation context, optionally with a filter hint."""
        hint = f" Користувач уточнив: '{filter_hint}'." if filter_hint else ""
        try:
            return self._query_once(
                user_id,
                f"Виходячи з нашої розмови, який саме товар потрібно знайти?{hint} "
                "Відповідь — лише назва товару для пошуку (1-7 слів), без пояснень і зайвого тексту. "
                "Прибери будь-які ввічливі слова (будь ласка, будьласка, прошу, дякую тощо), "
                "назви платформ, команди пошуку та слова про таймер/розклад. "
                "Лише сам товар.",
            )
        except Exception as e:
            logger.error("Gemini extract_search_query error: %s", e)
            return ""

    def add_user_message(self, user_id: int, text: str) -> None:
        """Inject user message into history without generating a response (for context setup)."""
        self._append(user_id, "user", text)
        self._append(user_id, "model", "Зрозумів, шукаю.")

    def filter_products_by_intent(
        self, user_id: int, products: list[dict], query: str,
        filter_intent: str = "", filters: dict | None = None,
    ) -> list[dict]:
        """AI жорстко фільтрує товари за наміром користувача.

        filters — структуровані вимоги з classify_intent:
          weight_kg, price_min, price_max, brand
        Всі вказані параметри є ОБОВ'ЯЗКОВИМИ умовами — товар без них відкидається.
        """
        if not products:
            return products

        lines = "\n".join(
            f"{i}. {p['name']} | {p.get('price', '')}"
            for i, p in enumerate(products, 1)
        )
        intent_line = f"Уточнення користувача: '{filter_intent}'.\n" if filter_intent else ""

        # Формуємо блок жорстких вимог із структурованих фільтрів
        hard_rules = []
        f = filters or {}
        if f.get("weight_kg") is not None:
            hard_rules.append(f"— Вага ОБОВ'ЯЗКОВО {f['weight_kg']} кг (будь-яке написання: кг, kg, кілограм тощо)")
        if f.get("price_min") is not None:
            hard_rules.append(f"— Ціна НЕ МЕНШЕ {f['price_min']} грн")
        if f.get("price_max") is not None:
            hard_rules.append(f"— Ціна НЕ БІЛЬШЕ {f['price_max']} грн")
        if f.get("brand"):
            hard_rules.append(f"— Бренд/модель ОБОВ'ЯЗКОВО: {f['brand']}")
        hard_block = ("ОБОВ'ЯЗКОВІ числові вимоги (відкидай якщо не відповідає):\n"
                      + "\n".join(hard_rules) + "\n") if hard_rules else ""

        # Чи є жорсткі уточнення (тип, підвид, вікова група тощо)?
        has_strict = bool(hard_rules) or bool(filter_intent)

        if has_strict:
            strictness = (
                "РЕЖИМ: СУВОРИЙ — користувач вказав конкретні вимоги.\n"
                "Залишай ЛИШЕ товари що точно відповідають усім вказаним умовам.\n"
                "ВИКЛЮЧАЙ:\n"
                "— Товари що не відповідають обов'язковим вимогам вище\n"
                "— Інші підвиди/вікові групи якщо вказано конкретний (напр. тільки для стерилізованих)\n"
                "— Запчастини, аксесуари, супутні товари (якщо шукали основний товар)\n"
                "— Інші бренди/моделі якщо вказано конкретний\n"
            )
        else:
            strictness = (
                "РЕЖИМ: ШИРОКИЙ — користувач не уточнив підвид/тип.\n"
                "Залишай ВСІ варіанти з правильної категорії товару:\n"
                "— Різні підвиди (для дорослих, кошенят, стерилізованих, з виведенням шерсті тощо)\n"
                "— Різні смаки, склади, форми випуску\n"
                "— Різні бренди якщо не вказано конкретний\n"
                "ВИКЛЮЧАЙ лише очевидно нерелевантне:\n"
                "— Товари з іншої категорії (корм для собак — якщо шукали для котів)\n"
                "— Запчастини, аксесуари замість самого товару\n"
                "— Послуги замість товарів\n"
            )

        prompt = (
            f"Запит: '{query}'.\n"
            f"{intent_line}"
            f"{hard_block}"
            f"{strictness}\n"
            f"Список оголошень (назва | ціна):\n{lines}\n\n"
            "Поверни ЛИШЕ номери відповідних товарів через кому.\n"
            "Якщо всі підходять — 'all'. Якщо жодного — '0'."
        )
        try:
            reply = self._query_once(user_id, prompt).strip().lower()
            if "all" in reply:
                return products
            if reply == "0" or not reply:
                return []
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
