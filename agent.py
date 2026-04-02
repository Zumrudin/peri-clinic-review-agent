"""
Review Agent для PERI CLINIC v2
Улучшения:
1. Уведомление об истечении куки
2. Кнопка 🔄 Перегенерировать ответ
3. Свежий csrf_token при публикации
4. Сортировка: негативные отзывы (1-2★) первыми
5. Дата отзыва в сообщении
6. Еженедельная статистика по понедельникам
7. /check [рейтинг] — фильтр по звёздам
9. Проверка каждые 2.5 часа
10. /history — история опубликованных ответов
"""
import os
import json
import subprocess
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

from ya_business_api.sync_api import SyncAPI
from ya_business_api.reviews.dataclasses.requests import ReviewsRequest, AnswerRequest

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CallbackQueryHandler, ContextTypes,
    CommandHandler, MessageHandler, TypeHandler, filters
)

load_dotenv(Path(__file__).parent / ".env")

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID"))
PERMANENT_ID       = int(os.getenv("YANDEX_PERMANENT_ID"))
CLAUDE_CLI         = os.getenv("CLAUDE_CLI", "claude")
COOKIES_FILE       = Path(__file__).parent / "data/yandex_cookies.json"
SYSTEM_PROMPT_FILE = Path(__file__).parent / "data/system_prompt.txt"
SENT_REVIEWS_FILE  = Path(__file__).parent / "data/sent_reviews.json"
QUEUE_FILE         = Path(__file__).parent / "data/review_queue.json"
STATS_FILE         = Path(__file__).parent / "data/stats.json"
NOTIFIED_IDS_FILE  = Path(__file__).parent / "data/notified_ids.json"

COOKIE_MAX_AGE_DAYS = 12  # предупреждаем если куки старше этого

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "logs/agent.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ─── Куки ────────────────────────────────────────────────────────────────────

def load_cookies():
    if not COOKIES_FILE.exists():
        raise FileNotFoundError("Файл куки не найден. Запусти get_cookies_windows.py")
    with open(COOKIES_FILE) as f:
        return json.load(f)


def get_cookie_age_days():
    """Возвращает возраст куки в днях или None если нет файла/даты."""
    if not COOKIES_FILE.exists():
        return None
    try:
        with open(COOKIES_FILE) as f:
            d = json.load(f)
        updated_at = datetime.fromisoformat(d.get("updated_at", ""))
        return (datetime.now() - updated_at).total_seconds() / 86400
    except Exception:
        return None


def build_api():
    """Строит API объект из сохранённых куки."""
    cookies = load_cookies()
    return SyncAPI.build(
        session_id=cookies["Session_id"],
        session_id2=cookies["sessionid2"]
    )


# ─── История / статистика ─────────────────────────────────────────────────────

def load_stats():
    if not STATS_FILE.exists():
        return []
    with open(STATS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_stat_entry(entry: dict):
    stats = load_stats()
    stats.append(entry)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False)


def load_sent_reviews() -> set:
    if not SENT_REVIEWS_FILE.exists():
        return set()
    with open(SENT_REVIEWS_FILE) as f:
        data = json.load(f)
    result = set()
    for item in data:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict):
            result.add(item.get("review_id", ""))
    return result


def save_sent_review(review_id: str, author: str = "", rating: int = 0,
                     review_text: str = "", answer: str = ""):
    sent = load_sent_reviews()
    sent.add(review_id)
    with open(SENT_REVIEWS_FILE, "w") as f:
        json.dump(list(sent), f)
    save_stat_entry({
        "review_id": review_id,
        "author": author,
        "rating": rating,
        "review_text": review_text[:500],
        "answer": answer[:500],
        "published_at": datetime.now().isoformat(),
    })


# ─── Уведомлённые ID (для дедупликации при периодических проверках) ──────────

def load_notified_ids() -> set:
    if not NOTIFIED_IDS_FILE.exists():
        return set()
    with open(NOTIFIED_IDS_FILE) as f:
        return set(json.load(f))


def save_notified_ids(ids: set):
    with open(NOTIFIED_IDS_FILE, "w") as f:
        json.dump(list(ids), f)


# ─── Отзывы ──────────────────────────────────────────────────────────────────

def format_review_date(r) -> str:
    """Форматирует дату отзыва из объекта review."""
    try:
        for attr in ("created_at", "timestamp", "date"):
            val = getattr(r, attr, None)
            if val is None:
                continue
            if isinstance(val, (int, float)):
                return datetime.fromtimestamp(val).strftime("%d.%m.%Y")
            if isinstance(val, datetime):
                return val.strftime("%d.%m.%Y")
            if isinstance(val, str):
                return datetime.fromisoformat(val).strftime("%d.%m.%Y")
    except Exception:
        pass
    return ""


def get_unanswered_reviews(rating_filter=None):
    """
    Возвращает список отзывов без ответа.
    Сортировка: 1★ → 2★ → ... → 5★ (негативные первыми).
    """
    api = build_api()
    unanswered = []
    for page in range(1, 4):
        resp = api.reviews.get_reviews(ReviewsRequest(permanent_id=PERMANENT_ID, page=page))
        items = resp.list.items
        if not items:
            break
        for r in items:
            if not r.owner_comment:
                if rating_filter is None or (r.rating or 0) == rating_filter:
                    unanswered.append(r)

    unanswered.sort(key=lambda r: r.rating or 5)
    log.info(
        f"Найдено отзывов без ответа: {len(unanswered)}"
        + (f" (фильтр: {rating_filter}★)" if rating_filter else "")
    )
    return unanswered


def publish_answer(review_id: str, answer_text: str) -> bool:
    """
    Публикует ответ с АКТУАЛЬНЫМИ csrf-токенами (перезапрашивает с Яндекса).
    """
    api = build_api()

    # Ищем свежий answer_csrf_token для конкретного отзыва
    answer_csrf = None
    for page in range(1, 4):
        resp = api.reviews.get_reviews(ReviewsRequest(permanent_id=PERMANENT_ID, page=page))
        items = resp.list.items
        if not items:
            break
        for r in items:
            if str(r.id) == str(review_id):
                answer_csrf = r.business_answer_csrf_token
                break
        if answer_csrf is not None:
            break

    if answer_csrf is None:
        raise RuntimeError(
            f"Отзыв {review_id} не найден — возможно ответ уже опубликован"
        )

    result = api.reviews.send_answer(AnswerRequest(
        review_id=review_id,
        text=answer_text,
        reviews_csrf_token=api.csrf_token,
        answer_csrf_token=answer_csrf,
    ))
    return bool(result)


def generate_answer(author: str, rating: int, review_text: str,
                    extra_instruction: str = "") -> str:
    system_prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    extra = f"\n\n{extra_instruction}" if extra_instruction else ""
    prompt = (
        f"{system_prompt}\n\n"
        f"---\n"
        f"Напиши ответ на следующий отзыв клиента. "
        f"Верни ТОЛЬКО текст ответа, без пояснений.{extra}\n\n"
        f"Автор: {author}\n"
        f"Рейтинг: {rating}/5\n"
        f"Текст отзыва: {review_text}"
    )
    result = subprocess.run(
        [CLAUDE_CLI, "-p", prompt],
        capture_output=True, text=True, timeout=180
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI error: {result.stderr}")
    return result.stdout.strip()


# ─── Очередь отзывов (навигация) ─────────────────────────────────────────────

def save_queue(reviews_data: list):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews_data, f, ensure_ascii=False)


def load_queue() -> list:
    if not QUEUE_FILE.exists():
        return []
    with open(QUEUE_FILE, encoding="utf-8") as f:
        return json.load(f)


def build_review_message(item: dict, index: int, total: int):
    """Формирует текст и клавиатуру для одного отзыва."""
    stars = "⭐" * item["rating"] + "☆" * (5 - item["rating"])
    answer = item.get("answer", "⏳ Генерируется...")
    date_str = item.get("review_date", "")
    date_line = f"📅 *Дата:* {date_str}\n" if date_str else ""

    text = (
        f"📬 *Отзыв {index + 1} из {total}*\n\n"
        f"👤 *Автор:* {item['author']}\n"
        f"*Рейтинг:* {stars}\n"
        f"{date_line}"
        f"\n💬 *Отзыв:*\n{item['review_text']}\n\n"
        f"{'─' * 30}\n\n"
        f"🤖 *Предлагаемый ответ:*\n{answer}"
    )

    review_id = item["review_id"]
    nav_buttons = []
    if index > 0:
        nav_buttons.append(
            InlineKeyboardButton("◀️ Пред.", callback_data=f"nav:{index - 1}")
        )
    if index < total - 1:
        nav_buttons.append(
            InlineKeyboardButton("След. ▶️", callback_data=f"nav:{index + 1}")
        )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)

    if item.get("answer"):
        keyboard.append([
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve:{review_id}"),
            InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"regen:{review_id}"),
        ])
        keyboard.append([
            InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{review_id}"),
            InlineKeyboardButton("⏭ Пропустить", callback_data=f"skip:{review_id}"),
        ])

    return text, InlineKeyboardMarkup(keyboard)


# ─── Уведомление об истёкших куки ────────────────────────────────────────────

async def notify_cookie_expired(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "🍪 *Куки Яндекса истекли!*\n\n"
            "Для обновления на своём компьютере (Windows) запусти:\n"
            "`python get_cookies_windows.py`\n\n"
            "Скрипт откроет браузер, войдёт в Яндекс и автоматически "
            "отправит свежие куки на сервер.\n\n"
            "После обновления снова используй /check"
        ),
        parse_mode="Markdown"
    )


# ─── Команды ─────────────────────────────────────────────────────────────────

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    # Опциональный фильтр по рейтингу: /check 1  или  /check 5
    rating_filter = None
    if context.args:
        try:
            v = int(context.args[0])
            if 1 <= v <= 5:
                rating_filter = v
        except ValueError:
            pass

    filter_text = f" (фильтр: {rating_filter}★)" if rating_filter else ""
    msg = await update.message.reply_text(
        f"🔍 Загружаю отзывы без ответа{filter_text}..."
    )

    try:
        loop = asyncio.get_event_loop()
        reviews = await loop.run_in_executor(
            None, get_unanswered_reviews, rating_filter
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка загрузки: {e}")
        err = str(e).lower()
        if any(k in err for k in ("cookie", "auth", "401", "403", "captcha")):
            await notify_cookie_expired(context)
        return

    if not reviews:
        note = f" с рейтингом {rating_filter}★" if rating_filter else ""
        await msg.edit_text(f"✅ Отзывов без ответа{note} нет!")
        return

    await msg.edit_text(
        f"Найдено {len(reviews)} отзывов. Генерирую ответ на первый..."
    )

    queue = []
    for r in reviews:
        author = r.author.user if r.author and r.author.user else "Гость"
        text = str(r.full_text or r.snippet or "(нет текста)")
        queue.append({
            "review_id": str(r.id),
            "author": author,
            "rating": r.rating or 0,
            "review_text": text,
            "review_date": format_review_date(r),
            "answer_csrf_token": r.business_answer_csrf_token,
            "answer": None,
        })
    save_queue(queue)

    await show_review(update.message.chat_id, 0, context)


async def show_review(chat_id: int, index: int,
                      context: ContextTypes.DEFAULT_TYPE, message_id: int = None):
    """Показывает отзыв по индексу. Генерирует ответ если ещё нет."""
    queue = load_queue()
    if not queue or index >= len(queue):
        return

    item = queue[index]
    total = len(queue)

    if not item.get("answer"):
        date_str = item.get("review_date", "")
        date_line = f"📅 *Дата:* {date_str}\n" if date_str else ""
        placeholder = (
            f"📬 *Отзыв {index + 1} из {total}*\n\n"
            f"👤 *Автор:* {item['author']}\n"
            f"*Рейтинг:* {'⭐' * item['rating']}{'☆' * (5 - item['rating'])}\n"
            f"{date_line}"
            f"\n💬 *Отзыв:*\n{item['review_text']}\n\n"
            f"{'─' * 30}\n\n"
            f"🤖 *Генерирую ответ...*"
        )
        if message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=placeholder, parse_mode="Markdown"
            )
        else:
            sent = await context.bot.send_message(
                chat_id=chat_id, text=placeholder, parse_mode="Markdown"
            )
            message_id = sent.message_id

        loop = asyncio.get_event_loop()
        try:
            answer = await loop.run_in_executor(
                None, generate_answer,
                item["author"], item["rating"], item["review_text"]
            )
        except Exception as e:
            answer = f"❌ Ошибка генерации: {e}"

        queue = load_queue()
        queue[index]["answer"] = answer
        save_queue(queue)

        pending_file = Path(__file__).parent / f"data/pending_{item['review_id']}.json"
        with open(pending_file, "w", encoding="utf-8") as f:
            json.dump({
                "review_id": item["review_id"],
                "answer": answer,
                "answer_csrf_token": item["answer_csrf_token"],
                "author": item["author"],
                "rating": item["rating"],
                "review_text": item["review_text"],
            }, f, ensure_ascii=False)

        item = queue[index]

    text, keyboard = build_review_message(item, index, total)
    if message_id:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=text, parse_mode="Markdown", reply_markup=keyboard
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode="Markdown", reply_markup=keyboard
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    sent = load_sent_reviews()
    queue = load_queue()
    cookies_ok = COOKIES_FILE.exists()
    cookies_info = ""
    if cookies_ok:
        with open(COOKIES_FILE) as f:
            d = json.load(f)
        age = get_cookie_age_days()
        age_str = f"{age:.1f} дн." if age is not None else "?"
        warning = " ⚠️ *Скоро истекут!*" if (age and age > COOKIE_MAX_AGE_DAYS) else ""
        cookies_info = (
            f"\n🍪 Куки обновлены: {d.get('updated_at', '?')}\n"
            f"⏰ Возраст: {age_str}{warning}"
        )

    await update.message.reply_text(
        f"🤖 *Review Agent — PERI CLINIC*\n"
        f"{'✅ Куки в порядке' if cookies_ok else '❌ Куки не найдены'}"
        f"{cookies_info}\n"
        f"📊 Обработано отзывов: {len(sent)}\n"
        f"📋 В очереди сейчас: {len(queue)}",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text(
        "🤖 *Review Agent — PERI CLINIC*\n\n"
        "*Команды:*\n"
        "/check — проверить отзывы без ответа\n"
        "/check 1 — только 1★ отзывы\n"
        "/check 2 — только 2★ отзывы\n"
        "/status — статус агента и куки\n"
        "/history — последние опубликованные ответы\n"
        "/history 10 — последние 10 ответов\n"
        "/help — эта справка\n\n"
        "*Свободный чат:*\n"
        "Просто напиши любое сообщение — отвечу через Claude.\n\n"
        "_Примеры:_\n"
        "• «Покажи последние отзывы»\n"
        "• «Напиши ответ на негативный отзыв: ...»\n"
        "• «Придумай акцию для новых клиентов»",
        parse_mode="Markdown"
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    count = 5
    if context.args:
        try:
            count = min(int(context.args[0]), 20)
        except ValueError:
            pass

    stats = load_stats()
    if not stats:
        await update.message.reply_text(
            "📭 История пуста — ещё не было опубликованных ответов."
        )
        return

    recent = stats[-count:][::-1]
    lines = [f"📚 *Последние {len(recent)} опубликованных ответов:*\n"]
    for i, entry in enumerate(recent, 1):
        stars = "⭐" * entry.get("rating", 0)
        date = entry.get("published_at", "")[:10]
        author = entry.get("author", "?")
        preview = entry.get("answer", "")
        if len(preview) > 150:
            preview = preview[:150] + "..."
        lines.append(
            f"*{i}. {author}* {stars} | {date}\n"
            f"_{preview}_\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Обработчики кнопок ──────────────────────────────────────────────────────

async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info(f"UPDATE: {update.to_dict()}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    log.info(f"CALLBACK получен: {query.data}")
    try:
        await query.answer()
    except Exception as e:
        log.error(f"Ошибка query.answer(): {e}")

    data = query.data
    chat_id = query.message.chat_id
    message_id = query.message.message_id

    # ── Навигация ──────────────────────────────────────────────────────────
    if data.startswith("nav:"):
        index = int(data.split(":")[1])
        await show_review(chat_id, index, context, message_id)
        return

    action, review_id = data.split(":", 1)
    pending_file = Path(__file__).parent / f"data/pending_{review_id}.json"

    # ── Опубликовать ───────────────────────────────────────────────────────
    if action == "approve":
        if not pending_file.exists():
            await query.answer("⚠️ Данные не найдены", show_alert=True)
            return

        with open(pending_file, encoding="utf-8") as f:
            pdata = json.load(f)

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, publish_answer, review_id, pdata["answer"]
            )
        except Exception as e:
            log.exception("Ошибка публикации")
            await query.answer(f"❌ Ошибка: {e}", show_alert=True)
            err = str(e).lower()
            if any(k in err for k in ("cookie", "auth", "401", "403", "captcha")):
                await notify_cookie_expired(context)
            return

        if result:
            full_queue = load_queue()
            current_index = next(
                (i for i, r in enumerate(full_queue) if r["review_id"] == review_id), 0
            )

            save_sent_review(
                review_id,
                author=pdata.get("author", ""),
                rating=pdata.get("rating", 0),
                review_text=pdata.get("review_text", ""),
                answer=pdata["answer"],
            )
            queue = [r for r in full_queue if r["review_id"] != review_id]
            save_queue(queue)
            pending_file.unlink(missing_ok=True)
            log.info(f"Ответ на {review_id} опубликован")

            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            nav_buttons = []
            prev_candidates = [i for i, r in enumerate(queue) if i < current_index]
            if prev_candidates:
                nav_buttons.append(
                    InlineKeyboardButton(
                        "◀️ Пред.", callback_data=f"nav:{prev_candidates[-1]}"
                    )
                )
            next_candidates = [i for i, r in enumerate(queue) if i >= current_index]
            if next_candidates:
                nav_buttons.append(
                    InlineKeyboardButton(
                        "След. ▶️", callback_data=f"nav:{next_candidates[0]}"
                    )
                )

            keyboard = InlineKeyboardMarkup([nav_buttons]) if nav_buttons else None
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ *Ответ опубликован!*\n\n"
                    f"👤 Автор: {pdata.get('author', '')}\n"
                    f"💬 Ответ: {pdata['answer'][:300]}"
                    f"{'...' if len(pdata['answer']) > 300 else ''}\n\n"
                    f"📋 Осталось без ответа: *{len(queue)}*"
                ),
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        else:
            await query.answer(
                "❌ Яндекс отклонил публикацию. Попробуй ещё раз.",
                show_alert=True
            )
            log.error(f"Яндекс вернул False для отзыва {review_id}")

    # ── Перегенерировать ───────────────────────────────────────────────────
    elif action == "regen":
        queue = load_queue()
        item = next((r for r in queue if r["review_id"] == review_id), None)
        if not item:
            await query.answer("⚠️ Отзыв не найден в очереди", show_alert=True)
            return

        index = next(i for i, r in enumerate(queue) if r["review_id"] == review_id)
        total = len(queue)
        date_str = item.get("review_date", "")
        date_line = f"📅 *Дата:* {date_str}\n" if date_str else ""

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"📬 *Отзыв {index + 1} из {total}*\n\n"
                f"👤 *Автор:* {item['author']}\n"
                f"*Рейтинг:* {'⭐' * item['rating']}{'☆' * (5 - item['rating'])}\n"
                f"{date_line}"
                f"\n💬 *Отзыв:*\n{item['review_text']}\n\n"
                f"{'─' * 30}\n\n"
                f"🔄 *Генерирую новый вариант ответа...*"
            ),
            parse_mode="Markdown"
        )

        loop = asyncio.get_event_loop()
        try:
            answer = await loop.run_in_executor(
                None, generate_answer,
                item["author"], item["rating"], item["review_text"],
                "Сгенерируй ДРУГОЙ вариант ответа, отличающийся по стилю и формулировкам от предыдущего."
            )
        except Exception as e:
            answer = f"❌ Ошибка генерации: {e}"

        queue = load_queue()
        for q_item in queue:
            if q_item["review_id"] == review_id:
                q_item["answer"] = answer
        save_queue(queue)

        if pending_file.exists():
            with open(pending_file, encoding="utf-8") as f:
                pdata = json.load(f)
            pdata["answer"] = answer
            with open(pending_file, "w", encoding="utf-8") as f:
                json.dump(pdata, f, ensure_ascii=False)

        await show_review(chat_id, index, context, message_id)

    # ── Изменить ───────────────────────────────────────────────────────────
    elif action == "edit":
        context.user_data["awaiting_edit"] = review_id
        context.user_data["edit_message_id"] = message_id
        await query.edit_message_text(
            query.message.text + "\n\n✏️ *Жду твой вариант ответа...*\n"
                                  "_(просто напиши следующим сообщением)_",
            parse_mode="Markdown"
        )

    # ── Пропустить ─────────────────────────────────────────────────────────
    elif action == "skip":
        full_queue = load_queue()
        current_index = next(
            (i for i, r in enumerate(full_queue) if r["review_id"] == review_id), 0
        )

        save_sent_review(review_id)
        queue = [r for r in full_queue if r["review_id"] != review_id]
        save_queue(queue)
        pending_file.unlink(missing_ok=True)
        log.info(f"Отзыв {review_id} пропущен")

        nav_buttons = []
        prev_candidates = [i for i, r in enumerate(queue) if i < current_index]
        if prev_candidates:
            nav_buttons.append(
                InlineKeyboardButton(
                    "◀️ Пред.", callback_data=f"nav:{prev_candidates[-1]}"
                )
            )
        next_candidates = [i for i, r in enumerate(queue) if i >= current_index]
        if next_candidates:
            nav_buttons.append(
                InlineKeyboardButton(
                    "След. ▶️", callback_data=f"nav:{next_candidates[0]}"
                )
            )

        keyboard = InlineKeyboardMarkup([nav_buttons]) if nav_buttons else None
        await query.edit_message_text(
            query.message.text + f"\n\n⏭ *Пропущен* | Осталось: *{len(queue)}*",
            parse_mode="Markdown",
            reply_markup=keyboard
        )


# ─── Свободный чат ───────────────────────────────────────────────────────────

REVIEW_KEYWORDS = [
    "отзыв", "review", "последн", "покажи", "посмотр",
    "без ответ", "новые", "сколько"
]


def is_review_request(text: str) -> bool:
    return any(kw in text.lower() for kw in REVIEW_KEYWORDS)


def fetch_recent_reviews_text(count: int = 10) -> str:
    api = build_api()
    resp = api.reviews.get_reviews(ReviewsRequest(permanent_id=PERMANENT_ID))
    items = resp.list.items[:count]
    total = resp.list.pager.total
    lines = [f"Всего отзывов в базе: {total}\n"]
    for i, r in enumerate(items, 1):
        author = r.author.user if r.author and r.author.user else "Гость"
        stars = "⭐" * (r.rating or 0)
        text = str(r.full_text or r.snippet or "(нет текста)")[:300]
        has_answer = "✅ есть ответ" if r.owner_comment else "❌ без ответа"
        lines.append(f"{i}. {author} | {stars} | {has_answer}\n   {text}\n")
    return "\n".join(lines)


async def handle_free_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    user_text = update.message.text

    # Режим редактирования ответа
    if "awaiting_edit" in context.user_data:
        review_id = context.user_data.pop("awaiting_edit")
        context.user_data.pop("edit_message_id", None)
        pending_file = Path(__file__).parent / f"data/pending_{review_id}.json"

        if not pending_file.exists():
            await update.message.reply_text("⚠️ Данные отзыва не найдены.")
            return

        with open(pending_file, encoding="utf-8") as f:
            data = json.load(f)
        data["answer"] = user_text
        with open(pending_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        queue = load_queue()
        for item in queue:
            if item["review_id"] == review_id:
                item["answer"] = user_text
        save_queue(queue)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve:{review_id}"),
            InlineKeyboardButton("⏭ Пропустить", callback_data=f"skip:{review_id}"),
        ]])
        await update.message.reply_text(
            f"✏️ *Новый вариант ответа:*\n\n{user_text}\n\nПубликуем?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    thinking_msg = await update.message.reply_text("⏳ Думаю...")

    try:
        if is_review_request(user_text):
            loop = asyncio.get_event_loop()
            reviews_data = await loop.run_in_executor(
                None, fetch_recent_reviews_text, 10
            )
            prompt = (
                f"Ты помощник администратора клиники PERI CLINIC. "
                f"У тебя есть доступ к актуальным данным из Яндекс Бизнеса.\n\n"
                f"Актуальные данные об отзывах:\n{reviews_data}\n\n"
                f"Вопрос пользователя: {user_text}"
            )
        else:
            prompt = user_text

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [CLAUDE_CLI, "-p", prompt],
                capture_output=True, text=True, timeout=180
            )
        )
        answer = (
            result.stdout.strip()
            if result.returncode == 0
            else f"❌ Ошибка: {result.stderr}"
        )
    except Exception as e:
        answer = f"❌ Ошибка: {e}"

    await thinking_msg.delete()
    for i in range(0, len(answer), 4000):
        await update.message.reply_text(answer[i:i + 4000])


# ─── Автопроверка (каждые 2.5 часа) ─────────────────────────────────────────

async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    log.info("Плановая проверка отзывов...")

    # Проверяем возраст куки перед запросом
    age = get_cookie_age_days()
    if age is not None and age > COOKIE_MAX_AGE_DAYS:
        log.warning(f"Куки истекают: возраст {age:.1f} дн.")
        await notify_cookie_expired(context)
        return

    try:
        loop = asyncio.get_event_loop()
        reviews = await loop.run_in_executor(None, get_unanswered_reviews)
    except Exception as e:
        log.exception("Ошибка при плановой проверке")
        err = str(e).lower()
        if any(k in err for k in ("cookie", "auth", "401", "403", "captcha")):
            await notify_cookie_expired(context)
        else:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"❌ Ошибка проверки отзывов: {e}"
            )
        return

    if not reviews:
        save_notified_ids(set())
        return

    current_ids = {str(r.id) for r in reviews}
    notified_ids = load_notified_ids()
    new_ids = current_ids - notified_ids

    if not new_ids:
        log.info("Новых отзывов не появилось")
        return

    save_notified_ids(current_ids)

    # Обновляем очередь новыми данными
    queue = []
    for r in reviews:
        author = r.author.user if r.author and r.author.user else "Гость"
        text = str(r.full_text or r.snippet or "(нет текста)")
        queue.append({
            "review_id": str(r.id),
            "author": author,
            "rating": r.rating or 0,
            "review_text": text,
            "review_date": format_review_date(r),
            "answer_csrf_token": r.business_answer_csrf_token,
            "answer": None,
        })
    save_queue(queue)

    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            f"📬 Найдено *{len(new_ids)}* новых отзывов без ответа!\n"
            f"Всего без ответа: *{len(reviews)}*\n\n"
            f"Используй /check чтобы начать."
        ),
        parse_mode="Markdown"
    )


# ─── Еженедельная статистика (понедельник 10:00) ─────────────────────────────

async def weekly_stats(context: ContextTypes.DEFAULT_TYPE):
    log.info("Отправка еженедельной статистики...")
    stats = load_stats()

    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    prev_week_ago = (datetime.now() - timedelta(days=14)).isoformat()
    week_entries = [e for e in stats if e.get("published_at", "") >= week_ago]
    prev_entries = [
        e for e in stats
        if prev_week_ago <= e.get("published_at", "") < week_ago
    ]

    count = len(week_entries)

    if count == 0:
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="📊 *Статистика за неделю*\n\nЗа прошедшую неделю ответов опубликовано не было.",
            parse_mode="Markdown"
        )
        return

    avg_rating = sum(e.get("rating", 0) for e in week_entries) / count
    prev_count = len(prev_entries)

    dynamics = ""
    if prev_count > 0:
        diff = count - prev_count
        sign = "+" if diff >= 0 else ""
        dynamics = f"\n📈 Динамика: {sign}{diff} к прошлой неделе ({prev_count} отв.)"

    rating_counts = {}
    for e in week_entries:
        r = e.get("rating", 0)
        rating_counts[r] = rating_counts.get(r, 0) + 1

    rating_lines = [
        f"  {'⭐' * r}: {rating_counts[r]} отз."
        for r in sorted(rating_counts.keys())
    ]

    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            f"📊 *Статистика PERI CLINIC за неделю*\n\n"
            f"✅ Опубликовано ответов: *{count}*{dynamics}\n"
            f"⭐ Средний рейтинг: *{avg_rating:.1f}*\n\n"
            f"*Разбивка по рейтингу:*\n"
            + "\n".join(rating_lines)
        ),
        parse_mode="Markdown"
    )


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(TypeHandler(Update, log_all_updates), group=-1)

    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_message))

    # Проверка новых отзывов каждые 2.5 часа
    app.job_queue.run_repeating(
        scheduled_check,
        interval=timedelta(hours=2, minutes=30),
        first=timedelta(minutes=5),
        name="periodic_review_check"
    )

    # Еженедельная статистика по понедельникам в 10:00
    # В PTB: days=(1,) — понедельник (0=воскресенье, 1=понедельник, ..., 6=суббота)
    app.job_queue.run_daily(
        weekly_stats,
        time=datetime.strptime("10:00", "%H:%M").time(),
        days=(1,),
        name="weekly_stats"
    )

    log.info("Review Agent v2 запущен. Команды: /check /status /history /help")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"]
    )


if __name__ == "__main__":
    main()
