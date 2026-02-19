import asyncio
import io
import logging
import math
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


FILTER_QUALIFIERS = {"лише", "тільки", "лиш", "саме", "тільки"}


def is_search_intent(text: str) -> bool:
    """True лише якщо є явний запит на пошук товару."""
    t = text.lower().strip()
    return any(kw in t for kw in SEARCH_TRIGGERS)


def has_filter_qualifier(query: str) -> bool:
    """True якщо запит містить уточнення-фільтр без конкретної назви товару."""
    words = set(query.lower().split())
    return bool(words & FILTER_QUALIFIERS)


# ------------------------------------------------------------------ #
#  Keyboards                                                           #
# ------------------------------------------------------------------ #

def settings_keyboard(platform: str, output_mode: str) -> InlineKeyboardMarkup:
    """Inline keyboard: platform row + output mode row."""
    platform_row_1 = [
        InlineKeyboardButton(
            text=("✅ " if platform == p else "") + label,
            callback_data=f"platform:{p}",
        )
        for p, label in [("prom", "🛒 Prom"), ("olx", "📦 OLX")]
    ]
    platform_row_2 = [
        InlineKeyboardButton(
            text=("✅ " if platform == p else "") + label,
            callback_data=f"platform:{p}",
        )
        for p, label in [("rozetka", "🔴 Rozetka"), ("web", "🌐 Інтернет")]
    ]
    mode_row = [
        InlineKeyboardButton(
            text=("✅ " if output_mode == m else "") + label,
            callback_data=f"mode:{m}",
        )
        for m, label in MODE_LABELS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[platform_row_1, platform_row_2, mode_row])


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
        if p.get("city"):
            lines.append(f"   📍 {p['city']}")
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

    # OLX не показує продавця — для решти платформ є колонка Продавець
    show_seller = platform != "olx"
    # Columns: №, Назва, Ціна, [Продавець], Місто, Посилання, Платформа
    if show_seller:
        col_widths = [5, 60, 28, 28, 22, 28, 16]
        headers    = ["№", "Назва", "Ціна", "Продавець", "Місто", "Посилання", "Платформа"]
        last_col   = "G"
        n_cols     = 7
    else:
        col_widths = [5, 65, 28, 25, 30, 16]
        headers    = ["№", "Назва", "Ціна", "Місто", "Посилання", "Платформа"]
        last_col   = "F"
        n_cols     = 6

    for col, w in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(1, col).column_letter].width = w

    blue_fill  = PatternFill("solid", fgColor="1F4E79")
    white_font = Font(color="FFFFFF", bold=True)
    alt_fill   = PatternFill("solid", fgColor="D6E4F0")
    ai_fill    = PatternFill("solid", fgColor="E2EFDA")
    bold_font  = Font(bold=True)
    link_font  = Font(color="0563C1", underline="single")
    center     = Alignment(horizontal="center", vertical="center")
    wrap       = Alignment(wrap_text=True, vertical="top")

    # --- Row 1: title ---
    ws.merge_cells(f"A1:{last_col}1")
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
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col, value=h)
        cell.font = white_font
        cell.fill = blue_fill
        cell.alignment = center

    # --- Rows 3+: products ---
    for i, p in enumerate(products, 3):
        col = 1
        ws.cell(row=i, column=col, value=i - 2).alignment = center;  col += 1
        ws.cell(row=i, column=col, value=p.get("name", "")).alignment = wrap;  col += 1
        ws.cell(row=i, column=col, value=p.get("price", "")).alignment = center;  col += 1
        if show_seller:
            ws.cell(row=i, column=col, value=p.get("seller", "")).alignment = wrap;  col += 1
        ws.cell(row=i, column=col, value=p.get("city", "")).alignment = wrap;  col += 1

        url = p.get("url", "")
        link_cell = ws.cell(row=i, column=col, value="Відкрити →" if url else "")
        if url:
            link_cell.hyperlink = url
            link_cell.font = link_font
        link_cell.alignment = center;  col += 1

        ws.cell(row=i, column=col, value=PLATFORM_LABELS.get(p.get("platform", platform), platform)).alignment = center

        if i % 2 == 0:
            for c in range(1, n_cols + 1):
                ws.cell(row=i, column=c).fill = alt_fill

    # --- Separator row ---
    sep_row = len(products) + 3
    ws.row_dimensions[sep_row].height = 8

    # --- AI Analysis block ---
    ai_start = sep_row + 1

    ws.merge_cells(f"A{ai_start}:{last_col}{ai_start}")
    header_cell = ws.cell(row=ai_start, column=1, value="📊 AI Аналіз")
    header_cell.font = Font(bold=True, size=11, color="FFFFFF")
    header_cell.fill = blue_fill
    header_cell.alignment = center

    for offset, line in enumerate(ai_analysis.splitlines(), 1):
        row_idx = ai_start + offset
        ws.merge_cells(f"A{row_idx}:{last_col}{row_idx}")
        cell = ws.cell(row=row_idx, column=1, value=line)
        cell.fill = ai_fill
        cell.font = bold_font if line.strip().startswith("•") else Font()
        cell.alignment = Alignment(wrap_text=True, vertical="center", indent=1)
        line_count = max(1, math.ceil(len(line) / 120))
        ws.row_dimensions[row_idx].height = line_count * 15

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ #
#  Core search function                                                #
# ------------------------------------------------------------------ #

async def _collect_excel_results(
    user_id: int, query: str, platform: str,
    filter_intent: str, target: int = 100, max_pages: int = 20,
) -> list[dict]:
    """Scrape page by page, filter each batch, accumulate until `target` results or no more pages."""
    collected: list[dict] = []
    should_filter = filter_intent or user_context.get(user_id)

    for page in range(1, max_pages + 1):
        raw = await asyncio.to_thread(search_manager.search_page, query, platform, page)
        if not raw:
            break  # no more pages
        if should_filter:
            batch = await asyncio.to_thread(
                agent.filter_products_by_intent, user_id, raw, query, filter_intent
            )
        else:
            batch = raw
        collected.extend(batch)
        if len(collected) >= target:
            break

    return collected[:target]


async def do_search(
    user_id: int, chat_id: int, query: str,
    platform: str, output_mode: str, status_msg=None,
    filter_intent: str = "",
) -> None:
    if output_mode == "excel":
        products = await _collect_excel_results(user_id, query, platform, filter_intent)
    else:
        products = await asyncio.to_thread(search_manager.search, query, platform, 10)
        if (filter_intent or user_context.get(user_id)) and products:
            products = await asyncio.to_thread(
                agent.filter_products_by_intent, user_id, products, query, filter_intent
            )
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

    # If query is empty or contains only filter qualifiers — extract full query from AI context
    if not query or has_filter_qualifier(query):
        thinking = await message.answer("💭 Уточнюю запит...")
        query = await asyncio.to_thread(agent.extract_search_query, user_id, query)
        await thinking.delete()
        if not query:
            await message.answer("❓ Що саме шукати? Уточніть, будь ласка.")
            return

    output_mode = settings["output_mode"]
    label = PLATFORM_LABELS.get(platform, platform)
    status_text = (
        f"🔎 Збираю всі оголошення «{query}» на {label}...\n⏳ Це може зайняти трохи більше часу."
        if output_mode == "excel"
        else f"🔎 Шукаю «{query}» на {label}..."
    )
    status_msg = await message.answer(status_text)

    async def search_task():
        await do_search(user_id, message.chat.id, query, platform, output_mode, status_msg,
                        filter_intent=text)

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

    status_msg = await message.answer("🖼 Аналізую фото за допомогою AI...")

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    # Caption = user's intent (e.g. "знайди мені продаж цього авто")
    caption = (message.caption or "").strip()

    product_name = await asyncio.to_thread(agent.identify_product_from_image, image_bytes)

    if not product_name:
        await status_msg.edit_text(
            "😔 Не вдалося визначити товар на фото. "
            "Спробуйте надіслати чіткіше фото або введіть назву вручну."
        )
        return

    # Inject caption into AI history so filter knows the exact intent
    if caption:
        await asyncio.to_thread(agent.add_user_message, user_id, caption)

    label = PLATFORM_LABELS.get(platform, platform)
    await status_msg.edit_text(
        f"✅ Визначено: *{product_name}*\n🔎 Шукаю на {label}...",
        parse_mode="Markdown",
    )
    await do_search(
        user_id, message.chat.id, product_name, platform, output_mode, status_msg,
        filter_intent=caption,
    )


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

async def main() -> None:
    logger.info("Bot is starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
