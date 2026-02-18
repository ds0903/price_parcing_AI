import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from config import TELEGRAM_BOT_TOKEN
from scraper import PromScraper
from ai_agent import GeminiAgent
from database import ensure_database_exists, init_db, close_pool, save_schedule, delete_schedule, get_all_schedules

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scraper = PromScraper()
agent = GeminiAgent()

# Per-user state (in-memory, reset on restart — intentional)
user_tasks: dict[int, asyncio.Task] = {}       # активний пошук
user_context: dict[int, bool] = {}             # чи є контекст для follow-up
scheduled_tasks: dict[int, asyncio.Task] = {}  # активні asyncio-таски розкладу

QUESTION_STARTERS = (
    "яка", "який", "яке", "яких", "де", "чи", "чому", "як",
    "скільки", "що", "коли", "хто", "навіщо", "порівняй",
    "розкажи", "поясни", "варто", "краще",
)


def is_followup(text: str) -> bool:
    t = text.lower().strip()
    return t.endswith("?") or any(t.startswith(kw) for kw in QUESTION_STARTERS)


def format_products(query: str, products: list[dict], ai_analysis: str) -> str:
    lines = [f'🔍 Результати пошуку: "{query}"\n']
    if not products:
        lines.append("😔 Товари не знайдено. Спробуйте інший запит.")
        return "\n".join(lines)

    lines.append(f"📦 Знайдено {len(products)} товарів:\n")
    for i, p in enumerate(products, 1):
        lines.append(f"{i}. {p['name']}")
        lines.append(f"   💰 {p['price']}")
        lines.append(f"   🏪 {p['seller']}")
        if p.get("url"):
            lines.append(f"   🔗 {p['url']}")
        lines.append("")

    lines.append("📊 AI Аналіз:")
    lines.append(ai_analysis)
    return "\n".join(lines)


async def do_search(user_id: int, chat_id: int, query: str, status_msg=None) -> None:
    """Run search + AI analysis and send result to the user."""
    products = await asyncio.to_thread(scraper.search_products, query)
    ai_analysis = await asyncio.to_thread(agent.analyze_prices, user_id, query, products)

    result = format_products(query, products, ai_analysis)
    if len(result) > 4096:
        result = result[:4090] + "\n..."

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    await bot.send_message(chat_id, result, disable_web_page_preview=True)
    user_context[user_id] = True


def _start_schedule_task(user_id: int, chat_id: int, interval: int, query: str) -> asyncio.Task:
    """Create and register an asyncio task for a scheduled search."""
    async def loop():
        while True:
            await do_search(user_id, chat_id, query)
            await asyncio.sleep(interval * 60)

    task = asyncio.create_task(loop())
    scheduled_tasks[user_id] = task
    return task


# ------------------------------------------------------------------ #
#  Startup / shutdown                                                  #
# ------------------------------------------------------------------ #

@dp.startup()
async def on_startup() -> None:
    await ensure_database_exists()
    await init_db()

    # Restore scheduled tasks saved in PostgreSQL
    schedules = await get_all_schedules()
    for s in schedules:
        _start_schedule_task(s["user_id"], s["chat_id"], s["interval_minutes"], s["query"])

    if schedules:
        logger.info("Restored %d scheduled task(s) from DB", len(schedules))


@dp.shutdown()
async def on_shutdown() -> None:
    await close_pool()


# ------------------------------------------------------------------ #
#  Commands                                                            #
# ------------------------------------------------------------------ #

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    agent.reset_chat(user_id)
    user_context.pop(user_id, None)
    await message.answer(
        "👋 Привіт! Я бот для пошуку цін на prom.ua.\n\n"
        "📝 Напиши назву товару — знайду ціни\n"
        "📷 Відправ фото — визначу товар і знайду ціни\n"
        "💬 Задавай уточнюючі питання після пошуку — я пам'ятаю контекст\n\n"
        "⚙️ *Команди:*\n"
        "/reboot — перезапустити AI (якщо завис)\n"
        "/schedule 5 назва — шукати кожні N хвилин\n"
        "/schedule stop — зупинити розклад",
        parse_mode="Markdown",
    )


@dp.message(Command("reboot"))
async def cmd_reboot(message: Message) -> None:
    user_id = message.from_user.id

    task = user_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()

    agent.reset_chat(user_id)
    user_context.pop(user_id, None)

    await message.answer("🔄 AI перезапущено. Контекст очищено — починаємо з нуля!")


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id
    args = message.text.split(maxsplit=2)

    # /schedule stop
    if len(args) >= 2 and args[1].lower() == "stop":
        task = scheduled_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        await delete_schedule(user_id)
        await message.answer("⏹ Розклад зупинено.")
        return

    # /schedule N запит
    if len(args) < 3:
        await message.answer(
            "❌ Формат: `/schedule <хвилини> <запит>`\n"
            "Приклад: `/schedule 5 кросівки Nike`\n"
            "Зупинити: `/schedule stop`",
            parse_mode="Markdown",
        )
        return

    try:
        interval = int(args[1])
        if interval < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Кількість хвилин має бути цілим числом ≥ 1")
        return

    query = args[2].strip()

    # Cancel previous schedule if any
    old = scheduled_tasks.pop(user_id, None)
    if old and not old.done():
        old.cancel()

    # Save to DB and start the loop
    await save_schedule(user_id, chat_id, interval, query)
    _start_schedule_task(user_id, chat_id, interval, query)

    await message.answer(
        f"⏰ Розклад встановлено: кожні *{interval} хв* шукатиму «{query}»\n"
        "Зупинити: /schedule stop",
        parse_mode="Markdown",
    )


# ------------------------------------------------------------------ #
#  Message handlers                                                    #
# ------------------------------------------------------------------ #

@dp.message(F.text)
async def handle_text(message: Message) -> None:
    user_id = message.from_user.id
    text = message.text.strip()
    if not text:
        return

    # Follow-up question — use existing context
    if user_context.get(user_id) and is_followup(text):
        thinking = await message.answer("💭 Думаю...")
        reply = await asyncio.to_thread(agent.follow_up, user_id, text)
        await thinking.delete()
        await message.answer(reply)
        return

    # New search
    status_msg = await message.answer("🔎 Шукаю товар на prom.ua...")

    async def search_task():
        await do_search(user_id, message.chat.id, text, status_msg)

    task = asyncio.create_task(search_task())
    user_tasks[user_id] = task
    try:
        await task
    except asyncio.CancelledError:
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer("⛔ Пошук скасовано.")
    finally:
        user_tasks.pop(user_id, None)


@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    user_id = message.from_user.id
    status_msg = await message.answer("🖼 Аналізую фото за допомогою Gemini AI...")

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    product_name = await asyncio.to_thread(agent.identify_product_from_image, image_bytes)

    if not product_name:
        await status_msg.edit_text(
            "😔 Не вдалося визначити товар на фото. "
            "Спробуйте надіслати чіткіше фото або введіть назву вручну."
        )
        return

    await status_msg.edit_text(
        f"✅ Визначено: *{product_name}*\n🔎 Шукаю ціни...",
        parse_mode="Markdown",
    )
    await do_search(user_id, message.chat.id, product_name, status_msg)


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

async def main() -> None:
    logger.info("Bot is starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
