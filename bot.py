import asyncio
import io
import logging
import math
import re
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
from scraper import SearchManager, detect_platforms, clean_query, PLATFORM_LABELS
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

FILTER_QUALIFIERS = {"лише", "тільки", "лиш", "саме"}


# ------------------------------------------------------------------ #
#  Keyboards                                                           #
# ------------------------------------------------------------------ #

def settings_keyboard(platforms: list[str]) -> InlineKeyboardMarkup:
    """Inline keyboard: одиночний вибір платформи (лише одна активна)."""
    row = [
        InlineKeyboardButton(
            text=("✅ " if p in platforms else "◻️ ") + label,
            callback_data=f"platform:{p}",
        )
        for p, label in [("prom", "Prom"), ("olx", "OLX"), ("rozetka", "Rozetka")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


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
    # Сортування від дешевших до дорожчих (товари без ціни — в кінець)
    def _price_key(p: dict) -> int:
        digits = re.sub(r"[^\d]", "", p.get("price", ""))
        return int(digits) if digits else 10 ** 9

    products = sorted(products, key=_price_key)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Результати"

    # Rozetka: тільки назва/ціна/посилання (без продавця і міста)
    # OLX: без продавця, але з містом
    # Решта: повний набір колонок
    show_seller = platform not in ("olx", "rozetka")
    show_city   = platform != "rozetka"
    # Columns: №, Назва, Ціна, [Продавець], [Місто], Посилання, Платформа
    if show_seller and show_city:
        col_widths = [5, 60, 28, 28, 22, 28, 16]
        headers    = ["№", "Назва", "Ціна", "Продавець", "Місто", "Посилання", "Платформа"]
        last_col   = "G"
        n_cols     = 7
    elif show_city:  # OLX: no seller, has city
        col_widths = [5, 65, 28, 25, 30, 16]
        headers    = ["№", "Назва", "Ціна", "Місто", "Посилання", "Платформа"]
        last_col   = "F"
        n_cols     = 6
    else:  # Rozetka: no seller, no city
        col_widths = [5, 70, 30, 32, 16]
        headers    = ["№", "Назва", "Ціна", "Посилання", "Платформа"]
        last_col   = "E"
        n_cols     = 5

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
        if show_city:
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
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _filter_by_price(products: list[dict], filters: dict) -> list[dict]:
    """Програмна фільтрація по ціні — до передачі в AI."""
    price_min = filters.get("price_min")
    price_max = filters.get("price_max")
    if price_min is None and price_max is None:
        return products
    result = []
    for p in products:
        digits = re.sub(r"[^\d]", "", p.get("price", ""))
        if not digits:
            result.append(p)  # ціна невідома — не відкидаємо
            continue
        price = int(digits)
        if price_min is not None and price < price_min:
            continue
        if price_max is not None and price > price_max:
            continue
        result.append(p)
    return result


# ------------------------------------------------------------------ #
#  Core search function                                                #
# ------------------------------------------------------------------ #

async def _collect_excel_results(
    user_id: int, query: str, platform: str,
    filter_intent: str, filters: dict | None = None,
    target: int = 100, max_pages: int = 25,
) -> list[dict]:
    """2 браузери паралельно + пайплайн: поки Gemini фільтрує пару N,
    браузери вже тягнуть пару N+1.

    AI працює в двох режимах:
      - ШИРОКИЙ: немає уточнень → бере всі варіанти категорії
      - СУВОРИЙ: є filter_intent або filters → тільки точні збіги
    """
    collected: list[dict] = []
    seen: set[str] = set()

    async def fetch_pair(p: int) -> list[dict]:
        """Два браузери одночасно: сторінки p і p+1."""
        if p + 1 <= max_pages:
            a, b = await asyncio.gather(
                asyncio.to_thread(search_manager.search_page, query, platform, p),
                asyncio.to_thread(search_manager.search_page, query, platform, p + 1),
            )
            return a + b
        return await asyncio.to_thread(search_manager.search_page, query, platform, p)

    page = 1
    # Запускаємо першу пару одразу
    fetch_task: asyncio.Task = asyncio.create_task(fetch_pair(page))

    while page <= max_pages:
        raw = await fetch_task
        page += 2

        if not raw:
            break

        # Пайплайн: запускаємо наступну пару ДО початку фільтрації
        if page <= max_pages:
            fetch_task = asyncio.create_task(fetch_pair(page))
        else:
            fetch_task = None

        # Програмна фільтрація по ціні (швидко, до AI)
        raw = _filter_by_price(raw, filters or {})

        # Фільтрація поточної пари (Gemini працює поки браузери тягнуть наступну)
        batch = await asyncio.to_thread(
            agent.filter_products_by_intent,
            user_id, raw, query, filter_intent, filters or {},
        )

        for product in batch:
            uid = product.get("product_id") or product.get("url") or ""
            if uid and uid in seen:
                continue
            if uid:
                seen.add(uid)
            collected.append(product)

        if len(collected) >= target or fetch_task is None:
            if fetch_task and not fetch_task.done():
                fetch_task.cancel()
            break

    return collected[:target]


async def _search_one_platform(
    user_id: int, chat_id: int, query: str,
    platform: str, filter_intent: str = "",
) -> None:
    """Збирає та надсилає Excel для однієї платформи."""
    products = await _collect_excel_results(user_id, query, platform, filter_intent)
    ai_analysis = await asyncio.to_thread(agent.analyze_prices, user_id, query, products, platform)
    xlsx_bytes = await asyncio.to_thread(build_excel, query, platform, products, ai_analysis)
    filename = f"price_{query[:25].replace(' ', '_')}_{platform}.xlsx"
    await bot.send_document(
        chat_id,
        BufferedInputFile(xlsx_bytes, filename=filename),
        caption=f'📊 {PLATFORM_LABELS.get(platform, platform)}: "{query}"',
    )


async def do_search(
    user_id: int, chat_id: int, query: str,
    platforms: list[str], status_msg=None,
    filter_intent: str = "", filters: dict | None = None,
) -> None:
    """Паралельний пошук по всіх платформах — окремий Excel на кожну."""
    gather_tasks = [
        _collect_excel_results(user_id, query, p, filter_intent, filters)
        for p in platforms
    ]
    results_list: list[list[dict]] = await asyncio.gather(*gather_tasks)

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    # Формуємо і надсилаємо Excel по черзі (щоб не флудити одночасно)
    for platform, products in zip(platforms, results_list):
        ai_analysis = await asyncio.to_thread(
            agent.analyze_prices, user_id, query, products, platform
        )
        xlsx_bytes = await asyncio.to_thread(build_excel, query, platform, products, ai_analysis)
        filename = f"price_{query[:25].replace(' ', '_')}_{platform}.xlsx"
        await bot.send_document(
            chat_id,
            BufferedInputFile(xlsx_bytes, filename=filename),
            caption=f'📊 {PLATFORM_LABELS.get(platform, platform)}: "{query}"',
        )

    user_context[user_id] = True


def _start_schedule_task(
    user_id: int, chat_id: int, interval: int,
    query: str, platforms: list[str],
) -> asyncio.Task:
    async def loop():
        while True:
            await do_search(user_id, chat_id, query, platforms)
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
        platforms = [p for p in s.get("platform", "prom").split(",") if p]
        _start_schedule_task(
            s["user_id"], s["chat_id"], s["interval_minutes"],
            s["query"], platforms,
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
        "Оберіть платформи для пошуку (можна кілька):",
        reply_markup=settings_keyboard(settings["platforms"]),
    )


@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    settings = await get_user_settings(message.from_user.id)
    plat_labels = ", ".join(PLATFORM_LABELS.get(p, p) for p in settings["platforms"])
    await message.answer(
        f"⚙️ Поточні налаштування:\n"
        f"Платформи: {plat_labels}\n\n"
        f"Натисніть щоб увімкнути/вимкнути платформу:",
        reply_markup=settings_keyboard(settings["platforms"]),
    )


@dp.callback_query(F.data.startswith("platform:"))
async def on_platform_select(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    platform = callback.data.split(":")[1]
    await save_user_settings(user_id, platforms=[platform])
    await callback.answer(f"✅ Платформа: {PLATFORM_LABELS.get(platform, platform)}")
    await callback.message.edit_reply_markup(
        reply_markup=settings_keyboard([platform])
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

    settings = await get_user_settings(user_id)

    # AI класифікує намір користувача
    intent = await asyncio.to_thread(agent.classify_intent, user_id, text)
    action = intent.get("action", "chat")

    # ------------------------------------------------------------------ #
    # platform_info: яка платформа зараз
    # ------------------------------------------------------------------ #
    if action == "platform_info":
        label = PLATFORM_LABELS.get(settings["platforms"][0], settings["platforms"][0])
        await message.answer(f"📍 Зараз шукаємо на: {label}")
        return

    # ------------------------------------------------------------------ #
    # platform_switch: переключити платформу
    # ------------------------------------------------------------------ #
    if action == "platform_switch":
        platform = (intent.get("platforms") or [""])[0]
        if platform in ("prom", "olx", "rozetka"):
            await save_user_settings(user_id, platforms=[platform])
            label = PLATFORM_LABELS.get(platform, platform)
            await message.answer(f"✅ Платформу змінено на {label}")
        else:
            await message.answer(
                "❓ Не розпізнав платформу. Доступні: Prom, OLX, Rozetka",
                reply_markup=settings_keyboard(settings["platforms"]),
            )
        return

    # ------------------------------------------------------------------ #
    # cancel_search: скасувати активний пошук
    # ------------------------------------------------------------------ #
    if action == "cancel_search":
        task = user_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
            await message.answer("⛔ Пошук скасовано.")
        else:
            await message.answer("Активного пошуку немає.")
        return

    # ------------------------------------------------------------------ #
    # reboot: перезавантажити AI
    # ------------------------------------------------------------------ #
    if action == "reboot":
        task = user_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        agent.reset_chat(user_id)
        user_context.pop(user_id, None)
        await message.answer("🔄 AI перезапущено. Контекст очищено!")
        return

    # ------------------------------------------------------------------ #
    # schedule_stop: зупинити таймер
    # ------------------------------------------------------------------ #
    if action == "schedule_stop":
        task = scheduled_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        await delete_schedule(user_id)
        await message.answer("⏹ Таймер зупинено. Автоматичний пошук вимкнено.")
        return

    # ------------------------------------------------------------------ #
    # schedule_list: показати активні таймери
    # ------------------------------------------------------------------ #
    if action == "schedule_list":
        if user_id in scheduled_tasks and not scheduled_tasks[user_id].done():
            schedules = await get_all_schedules()
            info = next((s for s in schedules if s["user_id"] == user_id), None)
            if info:
                plats = " + ".join(
                    PLATFORM_LABELS.get(p, p)
                    for p in info["platform"].split(",") if p
                )
                unit = (f"{info['interval_minutes']} хв"
                        if info["interval_minutes"] < 60
                        else f"{info['interval_minutes'] // 60} год")
                await message.answer(
                    f"⏰ Активний таймер:\n"
                    f"Запит: «{info['query']}»\n"
                    f"Інтервал: кожні {unit}\n"
                    f"Платформи: {plats}\n\n"
                    f"Щоб зупинити — напишіть «зупини таймер»."
                )
            else:
                await message.answer("Активних таймерів немає.")
        else:
            await message.answer("Активних таймерів немає.")
        return

    # ------------------------------------------------------------------ #
    # schedule_set: встановити таймер
    # ------------------------------------------------------------------ #
    if action == "schedule_set":
        query = intent.get("query", "").strip()
        interval = intent.get("interval_minutes")
        detected = intent.get("platforms") or []
        platforms = detected if detected else settings["platforms"]

        if not query or not interval:
            await message.answer(
                "❓ Не зрозумів. Вкажіть товар і інтервал, наприклад:\n"
                "«Кожні 30 хвилин шукати корм для собак на Prom»"
            )
            return

        old = scheduled_tasks.pop(user_id, None)
        if old and not old.done():
            old.cancel()

        platforms_str = ",".join(platforms)
        await save_schedule(user_id, message.chat.id, interval, query, platforms_str)
        _start_schedule_task(user_id, message.chat.id, interval, query, platforms)

        label_p = " + ".join(PLATFORM_LABELS.get(p, p) for p in platforms)
        unit = f"{interval} хв" if interval < 60 else f"{interval // 60} год"
        await message.answer(
            f"⏰ Таймер встановлено!\n"
            f"Кожні {unit} шукатиму «{query}» на {label_p}.\n"
            f"Щоб зупинити — напишіть «зупини таймер»."
        )
        return

    # ------------------------------------------------------------------ #
    # search: знайти товар
    # ------------------------------------------------------------------ #
    if action == "search":
        query = intent.get("query", "").strip()
        detected = intent.get("platforms") or []
        platforms = detected if detected else settings["platforms"]
        filters = intent.get("filters") or {}

        if not query:
            await message.answer("❓ Не вдалося визначити що шукати. Уточніть, будь ласка.")
            return

        # Формуємо підказку про активні фільтри
        filter_hints = []
        if filters.get("weight_kg") is not None:
            filter_hints.append(f"вага: {filters['weight_kg']} кг")
        if filters.get("price_min") is not None:
            filter_hints.append(f"від {filters['price_min']} грн")
        if filters.get("price_max") is not None:
            filter_hints.append(f"до {filters['price_max']} грн")
        if filters.get("brand"):
            filter_hints.append(f"бренд: {filters['brand']}")
        filter_str = f"\nФільтри: {', '.join(filter_hints)}" if filter_hints else ""

        label = " + ".join(PLATFORM_LABELS.get(p, p) for p in platforms)
        status_msg = await message.answer(
            f"🔎 Збираю «{query}» на {label}...{filter_str}\n⏳ Це може зайняти трохи часу."
        )

        # filter_intent передаємо лише якщо є реальні уточнення
        # (слова-квалфікатори АБО структуровані фільтри).
        # Інакше завжди вмикається СУВОРИЙ режим і AI відкидає половину товарів.
        has_qualifiers = (
            any(q in text.lower() for q in FILTER_QUALIFIERS)
            or bool(filters and any(v is not None for v in filters.values()))
        )
        filter_intent_param = text if has_qualifiers else ""

        async def search_task():
            await do_search(user_id, message.chat.id, query, platforms, status_msg,
                            filter_intent=filter_intent_param, filters=filters)

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
        return

    # ------------------------------------------------------------------ #
    # chat: звичайна розмова
    # ------------------------------------------------------------------ #
    thinking = await message.answer("💭 Думаю...")
    reply = await asyncio.to_thread(agent.follow_up, user_id, text)
    await thinking.delete()
    await message.answer(reply)


@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    user_id = message.from_user.id
    settings = await get_user_settings(user_id)
    platforms = settings["platforms"]

    status_msg = await message.answer("🖼 Аналізую фото за допомогою AI...")

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    caption = (message.caption or "").strip()

    product_name = await asyncio.to_thread(agent.identify_product_from_image, image_bytes)

    if not product_name:
        await status_msg.edit_text(
            "😔 Не вдалося визначити товар на фото. "
            "Спробуйте надіслати чіткіше фото або введіть назву вручну."
        )
        return

    # Витягуємо структуровані фільтри з підпису (якщо є)
    caption_filters: dict = {}
    if caption:
        await asyncio.to_thread(agent.add_user_message, user_id, caption)
        caption_intent = await asyncio.to_thread(agent.classify_intent, user_id, caption)
        caption_filters = caption_intent.get("filters") or {}

    label = " + ".join(PLATFORM_LABELS.get(p, p) for p in platforms)
    await status_msg.edit_text(
        f"✅ Визначено: *{product_name}*\n🔎 Шукаю на {label}...",
        parse_mode="Markdown",
    )
    await do_search(
        user_id, message.chat.id, product_name, platforms, status_msg,
        filter_intent=caption, filters=caption_filters,
    )


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

async def main() -> None:
    logger.info("Bot is starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
