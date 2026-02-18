import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message

from config import TELEGRAM_BOT_TOKEN
from scraper import PromScraper
from ai_agent import GeminiAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scraper = PromScraper()
agent = GeminiAgent()


def format_products(query: str, products: list[dict], ai_analysis: str) -> str:
    """Build the final message text sent to the user."""
    lines = [f'🔍 Результати пошуку: "{query}"\n']

    if not products:
        lines.append("😔 Товари не знайдено. Спробуйте інший запит.")
        return "\n".join(lines)

    lines.append(f"📦 Знайдено {len(products)} товарів:\n")

    for i, p in enumerate(products, 1):
        name = p.get("name", "—")
        price = p.get("price", "Ціна не вказана")
        seller = p.get("seller", "—")
        url = p.get("url", "")

        lines.append(f"{i}. {name}")
        lines.append(f"   💰 {price}")
        lines.append(f"   🏪 {seller}")
        if url:
            lines.append(f"   🔗 {url}")
        lines.append("")

    lines.append("📊 AI Аналіз:")
    lines.append(ai_analysis)

    return "\n".join(lines)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привіт! Я бот для пошуку цін на prom.ua.\n\n"
        "📝 Надішли мені *назву товару* — і я знайду актуальні ціни.\n"
        "📷 Або надішли *фото товару* — і Gemini AI визначить, що це, а потім знайде ціни.\n\n"
        "Спробуй написати, наприклад: `iPhone 15`",
        parse_mode="Markdown",
    )


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    query = message.text.strip()
    if not query:
        return

    status_msg = await message.answer("🔎 Шукаю товар на prom.ua...")

    products = await asyncio.get_event_loop().run_in_executor(
        None, scraper.search_products, query
    )

    ai_analysis = await asyncio.get_event_loop().run_in_executor(
        None, agent.analyze_prices, query, products
    )

    result = format_products(query, products, ai_analysis)

    await status_msg.delete()
    # Telegram message length limit is 4096 chars; truncate gracefully
    if len(result) > 4096:
        result = result[:4090] + "\n..."
    await message.answer(result, disable_web_page_preview=True)


@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    status_msg = await message.answer("🖼 Аналізую фото за допомогою Gemini AI...")

    # Download the largest available photo size
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    product_name = await asyncio.get_event_loop().run_in_executor(
        None, agent.identify_product_from_image, image_bytes
    )

    if not product_name:
        await status_msg.edit_text(
            "😔 Не вдалося визначити товар на фото. "
            "Спробуйте надіслати чіткіше фото або введіть назву вручну."
        )
        return

    await status_msg.edit_text(f"✅ Визначено: *{product_name}*\n🔎 Шукаю ціни...", parse_mode="Markdown")

    products = await asyncio.get_event_loop().run_in_executor(
        None, scraper.search_products, product_name
    )

    ai_analysis = await asyncio.get_event_loop().run_in_executor(
        None, agent.analyze_prices, product_name, products
    )

    result = format_products(product_name, products, ai_analysis)

    await status_msg.delete()
    if len(result) > 4096:
        result = result[:4090] + "\n..."
    await message.answer(result, disable_web_page_preview=True)


async def main() -> None:
    logger.info("Bot is starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
