import asyncio
import io
import logging
from datetime import datetime

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from config import TELEGRAM_BOT_TOKEN
from scraper import SearchManager, detect_platform, clean_query, PLATFORM_LABELS
from ai_agent import GeminiAgent
from database import (
    ensure_database_exists, init_db, close_pool,
    save_schedule, delete_schedule, get_all_schedules,
    get_user_settings, save_user_settings,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
search_manager = SearchManager()
agent = GeminiAgent()

# Per-user in-memory state (non-persistent)
user_tasks: dict[int, asyncio.Task] = {}
user_context: dict[int, bool] = {}
scheduled_tasks: dict[int, asyncio.Task] = {}

MODE_LABELS = {"chat": "💬 Чат", "excel": "📊 Excel"}

SEARCH_TRIGGERS = (
    "знайди", "знайдіть",
    "шукай", "шукайте",
    "пошукай", "пошук",
    "покажи", "покажіть",
    "де купити", "хочу купити",
    "ціна на", "ціни на",
    "скільки коштує", "скільки коштують",
    "починай пошук", "почни пошук",
    "найди",
)


def is_search_intent(text: str) -> bool:
    """True лише якщо є явний запит на пошук товару."""
    t = text.lower().strip()
    return any(kw in t for kw in SEARCH_TRIGGERS)


# ------------------------------------------------------------------ #
#  Keyboards                                                           #
# ------------------------------------------------------------------ #

def settings_keyboard(platform: str, output_mode: str) -> InlineKeyboardMarkup:
    """Inline keyboard: platform row + output mode row."""
    platform_row = [
        InlineKeyboardButton(
            text=("✅ " if platform == p else "") + label,
            callback_data=f"platform:{p}",
        )
        for p, label in [("prom", "🛒 Prom"), ("olx", "📦 OLX"), ("web", "🌐 Інтернет")]
    ]
    mode_row = [
        InlineKeyboardButton(
            text=("✅ " if output_mode == m else "") + label,
            callback_data=f"mode:{m}",
        )
        for m, label in MODE_LABELS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[platform_row, mode_row])


# ------------------------------------------------------------------ #
#  Output formatters                                                   #
# ------------------------------------------------------------------ #

def format_chat(query: str, platform: str, products: list[dict], ai_analysis: str) -> str:
    label = PLATFORM_LABELS.get(platform, platform)
    lines = [f'🔍 Результати пошуку: "{query}" [{label}]\n']
    if not products:
        lines.append("😔 Товари не знайдено. Спробуйте інший запит або платформу.")
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


def build_excel(query: str, platform: str, products: list[dict], ai_analysis: str) -> bytes:
    """Single sheet: info header → product table → AI analysis at the bottom."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Результати"

    col_widths = [5, 50, 18, 25, 55, 14]
    for col, w in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(1, col).column_letter].width = w

    blue_fill   = PatternFill("solid", fgColor="1F4E79")
    white_font  = Font(color="FFFFFF", bold=True)
    alt_fill    = PatternFill("solid", fgColor="D6E4F0")
    ai_fill     = PatternFill("solid", fgColor="E2EFDA")
    bold_font   = Font(bold=True)
    center      = Alignment(horizontal="center", vertical="center")
    wrap        = Alignment(wrap_text=True, vertical="top")

    # --- Row 1: title ---
    ws.merge_cells("A1:F1")
    title_cell = ws["A1"]
    title_cell.value = (
        f'Пошук: "{query}"  |  '
        f'Платформа: {PLATFORM_LABELS.get(platform, platform)}  |  '
        f'Дата: {datetime.now().strftime("%d.%m.%Y %H:%M")}'
    )
    title_cell.font = Font(bold=True, size=12, color="FFFFFF")
    title_cell.fill = blue_fill
    title_cell.alignment = center
    ws.row_dimensions[1].height = 22

    # --- Row 2: column headers ---
    headers = ["№", "Назва", "Ціна", "Продавець", "Посилання", "Платформа"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col, value=h)
        cell.font = white_font
        cell.fill = blue_fill
        cell.alignment = center

    # --- Rows 3+: products ---
    for i, p in enumerate(products, 3):
        ws.cell(row=i, column=1, value=i - 2).alignment = center
        ws.cell(row=i, column=2, value=p.get("name", "")).alignment = wrap
        ws.cell(row=i, column=3, value=p.get("price", "")).alignment = center
        ws.cell(row=i, column=4, value=p.get("seller", "")).alignment = wrap
        ws.cell(row=i, column=5, value=p.get("url", "")).alignment = wrap
        ws.cell(row=i, column=6, value=PLATFORM_LABELS.get(p.get("platform", platform), platform)).alignment = center

        if i % 2 == 0:
            for col in range(1, 7):
                ws.cell(row=i, column=col).fill = alt_fill

    # --- Separator row ---
    sep_row = len(products) + 3
    ws.row_dimensions[sep_row].height = 8

    # --- AI Analysis block ---
    ai_start = sep_row + 1

    ws.merge_cells(f"A{ai_start}:F{ai_start}")
    header_cell = ws.cell(row=ai_start, column=1, value="📊 AI Аналіз")
    header_cell.font = Font(bold=True, size=11, color="FFFFFF")
    header_cell.fill = blue_fill
    header_cell.alignment = center

    for offset, line in enumerate(ai_analysis.splitlines(), 1):
        row_idx = ai_start + offset
        ws.merge_cells(f"A{row_idx}:F{row_idx}")
        cell = ws.cell(row=row_idx, column=1, value=line)
        cell.fill = ai_fill
        cell.font = bold_font if line.strip().startswith("•") else Font()
        cell.alignment = Alignment(vertical="center", indent=1)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ #
#  Core search function                                                #
# ------------------------------------------------------------------ #

async def do_search(
    user_id: int, chat_id: int, query: str,
    platform: str, output_mode: str, status_msg=None,
) -> None:
    products = await asyncio.to_thread(search_manager.search, query, platform)
    ai_analysis = await asyncio.to_thread(agent.analyze_prices, user_id, query, products, platform)

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if output_mode == "excel":
        xlsx_bytes = await asyncio.to_thread(build_excel, query, platform, products, ai_analysis)
        filename = f"price_{query[:30].replace(' ', '_')}.xlsx"
        await bot.send_document(
            chat_id,
            BufferedInputFile(xlsx_bytes, filename=filename),
            caption=f'📊 Результати: "{query}" [{PLATFORM_LABELS.get(platform, platform)}]',
        )
    else:
        result = format_chat(query, platform, products, ai_analysis)
        if len(result) > 4096:
            result = result[:4090] + "\n..."
        await bot.send_message(chat_id, result, disable_web_page_preview=True)

    user_context[user_id] = True


def _start_schedule_task(
    user_id: int, chat_id: int, interval: int,
    query: str, platform: str, output_mode: str,
) -> asyncio.Task:
    async def loop():
        while True:
            await do_search(user_id, chat_id, query, platform, output_mode)
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

    schedules = await get_all_schedules()
    for s in schedules:
        settings = await get_user_settings(s["user_id"])
        _start_schedule_task(
            s["user_id"], s["chat_id"], s["interval_minutes"],
            s["query"], s.get("platform", "prom"), settings["output_mode"],
        )
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
    settings = await get_user_settings(user_id)
    await message.answer(
        "👋 Привіт! Я бот для пошуку цін.\n\n"
        "Оберіть платформу та формат виводу:",
        reply_markup=settings_keyboard(settings["platform"], settings["output_mode"]),
    )


@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    settings = await get_user_settings(message.from_user.id)
    plat = PLATFORM_LABELS.get(settings["platform"], settings["platform"])
    mode = MODE_LABELS.get(settings["output_mode"], settings["output_mode"])
    await message.answer(
        f"⚙️ Поточні налаштування:\n"
        f"• Платформа: *{plat}*\n"
        f"• Формат: *{mode}*\n\n"
        f"Змінити:",
        reply_markup=settings_keyboard(settings["platform"], settings["output_mode"]),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("platform:"))
async def on_platform_select(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    platform = callback.data.split(":")[1]
    await save_user_settings(user_id, platform=platform)
    settings = await get_user_settings(user_id)
    await callback.message.edit_reply_markup(
        reply_markup=settings_keyboard(settings["platform"], settings["output_mode"])
    )
    await callback.answer(f"Платформа: {PLATFORM_LABELS.get(platform, platform)}")


@dp.callback_query(F.data.startswith("mode:"))
async def on_mode_select(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    mode = callback.data.split(":")[1]
    await save_user_settings(user_id, output_mode=mode)
    settings = await get_user_settings(user_id)
    await callback.message.edit_reply_markup(
        reply_markup=settings_keyboard(settings["platform"], settings["output_mode"])
    )
    await callback.answer(f"Формат: {MODE_LABELS.get(mode, mode)}")


@dp.message(Command("platform"))
async def cmd_platform(message: Message) -> None:
    settings = await get_user_settings(message.from_user.id)
    await message.answer(
        "🛒 Оберіть платформу для пошуку:",
        reply_markup=settings_keyboard(settings["platform"], settings["output_mode"]),
    )


@dp.message(Command("reboot"))
async def cmd_reboot(message: Message) -> None:
    user_id = message.from_user.id
    task = user_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    agent.reset_chat(user_id)
    user_context.pop(user_id, None)
    await message.answer("🔄 AI перезапущено. Контекст очищено!")


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id
    args = message.text.split(maxsplit=2)

    if len(args) >= 2 and args[1].lower() == "stop":
        task = scheduled_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        await delete_schedule(user_id)
        await message.answer("⏹ Розклад зупинено.")
        return

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
    settings = await get_user_settings(user_id)
    platform = settings["platform"]
    output_mode = settings["output_mode"]

    old = scheduled_tasks.pop(user_id, None)
    if old and not old.done():
        old.cancel()

    await save_schedule(user_id, chat_id, interval, query, platform)
    _start_schedule_task(user_id, chat_id, interval, query, platform, output_mode)

    label_p = PLATFORM_LABELS.get(platform, platform)
    label_m = MODE_LABELS.get(output_mode, output_mode)
    await message.answer(
        f"⏰ Розклад: кожні *{interval} хв* шукатиму «{query}»\n"
        f"Платформа: *{label_p}* | Формат: *{label_m}*\n"
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

    # Ignore unknown slash-commands
    if text.startswith("/"):
        return

    # Search only when user explicitly asks; otherwise — AI chat
    if not is_search_intent(text):
        thinking = await message.answer("💭 Думаю...")
        reply = await asyncio.to_thread(agent.follow_up, user_id, text)
        await thinking.delete()
        await message.answer(reply)
        return

    # Detect platform from text; otherwise use saved setting
    detected = detect_platform(text)
    settings = await get_user_settings(user_id)

    if detected:
        platform = detected
        await save_user_settings(user_id, platform=platform)
    else:
        platform = settings["platform"]
    query = clean_query(text)

    output_mode = settings["output_mode"]
    label = PLATFORM_LABELS.get(platform, platform)
    status_msg = await message.answer(f"🔎 Шукаю «{query}» на {label}...")

    async def search_task():
        await do_search(user_id, message.chat.id, query, platform, output_mode, status_msg)

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
    settings = await get_user_settings(user_id)
    platform = settings["platform"]
    output_mode = settings["output_mode"]

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

    label = PLATFORM_LABELS.get(platform, platform)
    await status_msg.edit_text(
        f"✅ Визначено: *{product_name}*\n🔎 Шукаю на {label}...",
        parse_mode="Markdown",
    )
    await do_search(user_id, message.chat.id, product_name, platform, output_mode, status_msg)


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

async def main() -> None:
    logger.info("Bot is starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
