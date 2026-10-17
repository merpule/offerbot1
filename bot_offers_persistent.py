"""
Admin Bot + NFT Offer Generator (Business Mode)
- Telegram Business Mode (/offers офферы)
- Веб админ-панель
- Одобрение пользователей по ID
"""

import asyncio
import logging
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BusinessConnection,
    LabeledPrice,
    PreCheckoutQuery,
)
from aiogram.filters import Command, CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError

import aiohttp
from aiohttp import web, ClientSession
import aiohttp_jinja2
import jinja2

# ==================== НАСТРОЙКИ ====================
# Все секреты читаются из переменных окружения (безопасно для хостинга)
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "8220751252").split(",") if x.strip()]
WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
DATA_FILE = Path(__file__).parent / "data" / "users.json"
OFFERS_FILE = Path(os.getenv("OFFERS_FILE", str(Path(__file__).parent / "data" / "offers.json")))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
try:
    import psycopg2
    from psycopg2.extras import Json
except ImportError:
    psycopg2 = None
    Json = None

# Оплата доступа через Telegram Stars
SUBSCRIPTION_STARS = int(os.getenv("SUBSCRIPTION_STARS", "555"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
PAYLOAD_PREFIX = "bot_access_"

# Оплата доступа через GRAM (нативный TON/GRAM в сети TON)
GRAM_WALLET = os.getenv("GRAM_WALLET", "UQD9qii_hUBsLpDi1HRj3oMTcZmwoVRbMBhcMi_DZVJJcykT")
SUBSCRIPTION_GRAM = float(os.getenv("SUBSCRIPTION_GRAM", "4.8"))
GRAM_PAYMENT_TTL_MINUTES = int(os.getenv("GRAM_PAYMENT_TTL_MINUTES", "30"))
TONCENTER_API_URL = "https://toncenter.com/api/v3/transactions"
# ==================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ---------- хранилища ----------
users_db: dict[int, dict] = {}
pending_users: dict[int, dict] = {}
sessions: dict[str, dict] = {}
offers_storage: dict[str, dict] = {}
# business_connection_id -> Telegram user id of the managed business account
business_owners: dict[str, int] = {}
stats = {"gifts": 0, "errors": 0, "stars": 0, "gram": 0, "online": 0}

# Ограничиваем частоту отправки офферов, чтобы не создавать резкие всплески запросов.
offer_send_lock = asyncio.Lock()
last_offer_send_at = 0.0
OFFER_SEND_INTERVAL_SECONDS = 2.0


def load_users_db() -> None:
    """Load approved/subscribed users from a small JSON file."""
    global users_db
    try:
        if DATA_FILE.exists():
            import json
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            users_db = {int(uid): info for uid, info in raw.items()}
    except Exception as exc:
        logger.exception("Не удалось загрузить базу пользователей: %s", exc)
        users_db = {}


def save_users_db() -> None:
    try:
        import json
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps(users_db, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.exception("Не удалось сохранить базу пользователей: %s", exc)


def init_offers_storage() -> None:
    """Load offers from PostgreSQL (recommended on Render) or local JSON fallback."""
    global offers_storage
    if DATABASE_URL:
        if psycopg2 is None:
            raise RuntimeError("DATABASE_URL задан, но psycopg2 не установлен. Добавь psycopg2-binary в requirements.txt")
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_offers (
                        offer_id TEXT PRIMARY KEY,
                        payload JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("SELECT offer_id, payload FROM bot_offers")
                offers_storage = {offer_id: payload for offer_id, payload in cur.fetchall()}
        logger.info("Загружено офферов из PostgreSQL: %s", len(offers_storage))
        return

    # Local fallback is convenient for development, but Render free filesystem is ephemeral.
    try:
        if OFFERS_FILE.exists():
            raw = json.loads(OFFERS_FILE.read_text(encoding="utf-8"))
            offers_storage = {str(k): v for k, v in raw.items()}
    except Exception:
        logger.exception("Не удалось загрузить сохранённые офферы")
        offers_storage = {}
    logger.info("Загружено офферов из локального файла: %s", len(offers_storage))


def save_offer(offer_id: str) -> None:
    """Persist one offer and its current status."""
    offer = offers_storage.get(offer_id)
    if offer is None:
        return
    try:
        if DATABASE_URL:
            if psycopg2 is None:
                raise RuntimeError("Для PostgreSQL установи пакет psycopg2-binary")
            with psycopg2.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO bot_offers (offer_id, payload, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (offer_id) DO UPDATE
                        SET payload = EXCLUDED.payload, updated_at = NOW()
                    """, (offer_id, Json(offer)))
            return
        OFFERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFERS_FILE.write_text(
            json.dumps(offers_storage, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        logger.exception("Не удалось сохранить оффер %s", offer_id)


# ---------- bot ----------
router = Router()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.include_router(router)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def delete_business_message(business_connection_id: str, message_id: int) -> None:
    """Delete one message from a connected Business chat.

    Use the Bot API method directly so deletion does not depend on the
    installed aiogram method wrapper. Telegram requires the Business bot
    connection to have permission to delete the relevant message.
    """
    await telegram_api("deleteBusinessMessages", {
        "business_connection_id": business_connection_id,
        "message_ids": [message_id],
    })


# ==================== BUSINESS MODE + /offers ====================

def parse_nft_name(url: str) -> str:
    match = re.search(r"https?://t\.me/nft/([a-zA-Z0-9\-_]+)", url)
    if not match:
        return "Unknown Gift"
    raw = match.group(1)
    if "-" in raw:
        name_part, number_part = raw.rsplit("-", 1)
    else:
        name_part, number_part = raw, ""
    name_formatted = re.sub(r"(?<!^)(?=[A-Z])", " ", name_part)
    return f"{name_formatted} #{number_part}" if number_part else name_formatted


def format_currency(currency: str) -> str:
    currency = currency.lower().strip()
    return {
        "stars": "STARS",
        "star": "STARS",
        "xtr": "STARS",
        "gram": "GRAM",
        "grams": "GRAM",
    }.get(currency, currency.upper())

def user_language_code(user) -> str:
    """Choose Russian, Simplified Chinese, or English from Telegram's language code."""
    code = (getattr(user, "language_code", None) or "").lower()
    if code.startswith("ru"):
        return "ru"
    if code.startswith(("zh", "cmn")):
        return "zh"
    return "en"


def tr(lang: str, ru: str, en: str, zh: str) -> str:
    """Small inline translation helper."""
    return {"ru": ru, "en": en, "zh": zh}.get(lang, en)


def build_offer_keyboard(offer_id: str) -> InlineKeyboardMarkup:
    # Callback не содержит ID пользователя: поэтому нажать кнопку может
    # любой участник чата, а не только автор команды /offers.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr(offers_storage.get(offer_id, {}).get("language", "en"), "Отклонить", "Decline", "拒绝"),
                    callback_data=f"decline_offer:{offer_id}",
                    icon_custom_emoji_id="5296678515536581003"
                ),
                InlineKeyboardButton(
                    text=tr(offers_storage.get(offer_id, {}).get("language", "en"), "Принять", "Accept", "接受"),
                    callback_data=f"accept_offer:{offer_id}",
                    icon_custom_emoji_id="5823537588186647980"
                ),
            ],
        ]
    )


def build_accepted_keyboard(buyer_username: str | None, nft_url: str, offer_id: str) -> InlineKeyboardMarkup:
    """Кнопки после принятия оффера.

    Telegram не поддерживает передачу конкретного NFT через send_gift deep-link,
    поэтому ссылка открывает экран передачи подарка получателю, а сам NFT
    пользователь выбирает в интерфейсе Telegram.
    """
    # Fixed recipient requested for gift transfer.
    transfer_url = "tg://send_gift?to=larpxbtj"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Передать подарок",
                    url=transfer_url,
                    icon_custom_emoji_id="5271604874419647061"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Проверить передачу",
                    callback_data=f"check_transfer:{offer_id}",
                    icon_custom_emoji_id="5188217332748527444"
                )
            ],
        ]
    )


def get_sender_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Пользователь"
    return user.full_name or (f"@{user.username}" if user.username else f"id{user.id}")


@router.business_connection()
async def on_business_connection(connection: BusinessConnection):
    status = "включено" if connection.is_enabled else "отключено"
    if connection.is_enabled and connection.user:
        business_owners[connection.id] = connection.user.id
    else:
        business_owners.pop(connection.id, None)
    logger.info(f"Business connection {connection.id} | {status}")


BUY_PATTERN = re.compile(
    r"^\s*/offers?(?:@\w+)?\s+(https?://t\.me/nft/[a-zA-Z0-9\-_]+)\s+([0-9]+(?:[.,][0-9]+)?)\s+(gram|grams|stars|star|xtr)(?:\s+(EN|CN|NG))?\s*$",
    re.IGNORECASE,
)


def buy_format_error_text() -> str:
    return (
        "❌ Формат: <code>/offer ссылка сумма валюта [EN/CN]</code>\n"
        "Русский: <code>/offer https://t.me/nft/HappyBrownie-225956 1200 gram</code>\n"
        "Английский: <code>/offer https://t.me/nft/HappyBrownie-225956 1200 gram EN</code>\n"
        "Китайский: <code>/offer https://t.me/nft/HappyBrownie-225956 1200 gram CN</code>"
    )



async def create_buy_offer(
    message: Message,
    *,
    business_connection_id: str | None = None,
    seller_chat_id: int | None = None,
) -> bool:
    """Create the same NFT offer for Business chats and normal bot chats.

    In a normal bot chat there is no business recipient, so the offer is sent
    to the first configured admin (the seller/operator), while the buyer
    receives a small status message.
    """
    text = message.text or ""
    match = BUY_PATTERN.search(text)
    if not match:
        # Ошибку формата всегда показываем тому, кто ввёл команду.
        await bot.send_message(
            chat_id=message.chat.id,
            text=tr(
                user_language_code(message.from_user),
                buy_format_error_text(),
                "❌ Format: <code>/offers NFT_LINK AMOUNT CURRENCY</code>\\nExample: <code>/offers https://t.me/nft/HappyBrownie-225956 1200 gram</code>\\nCurrency: <code>gram</code> or <code>stars</code>.",
                "❌ 格式：<code>/offers NFT链接 金额 币种</code>\\n示例：<code>/offers https://t.me/nft/HappyBrownie-225956 1200 gram</code>\\n币种可使用 <code>gram</code> 或 <code>stars</code>。",
            ),
            parse_mode=ParseMode.HTML,
            business_connection_id=business_connection_id,
        )
        return False

    nft_url, amount, currency_raw, lang_override = match.group(1), match.group(2), match.group(3), match.group(4)
    amount = amount.replace(",", ".")
    nft_name = parse_nft_name(nft_url)
    currency = format_currency(currency_raw)
    offer_id = secrets.token_urlsafe(9)
    order_id = f"TG-{secrets.token_hex(4).upper()}"
    lang = {"EN": "en", "CN": "zh", "NG": "zh"}.get((lang_override or "").upper(), "ru" if not lang_override else user_language_code(message.from_user))

    offer_text = tr(
        lang,
        f"Пользователь предлагает вам <b>{amount} {currency}</b> "
        f"<tg-emoji emoji-id='5267445215773367585'>💎</tg-emoji> за подарок <b>{nft_name}</b>.\\n\\n{nft_url}",
        f"The user offers you <b>{amount} {currency}</b> "
        f"<tg-emoji emoji-id='5267445215773367585'>💎</tg-emoji> for the gift <b>{nft_name}</b>.\\n\\n{nft_url}",
        f"用户出价 <b>{amount} {currency}</b> "
        f"<tg-emoji emoji-id='5267445215773367585'>💎</tg-emoji> 购买礼物 <b>{nft_name}</b>。\\n\\n{nft_url}",
    )

    # Business: предложение остаётся в исходном Business-чате.
    # В обычном боте предложение уходит оператору/продавцу (ADMIN_IDS[0]).
    target_chat = message.chat.id if business_connection_id else (seller_chat_id or ADMIN_IDS[0])

    # Отправляем офферы последовательно с небольшим интервалом. Это снижает
    # вероятность всплесков запросов, но не отменяет серверные ограничения Telegram.
    global last_offer_send_at
    try:
        async with offer_send_lock:
            loop = asyncio.get_running_loop()
            wait_for = OFFER_SEND_INTERVAL_SECONDS - (loop.time() - last_offer_send_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            sent = await bot.send_message(
                chat_id=target_chat,
                text=offer_text,
                reply_markup=build_offer_keyboard(offer_id),
                parse_mode=ParseMode.HTML,
                business_connection_id=business_connection_id,
                disable_web_page_preview=False,
            )
            last_offer_send_at = loop.time()
    except TelegramBadRequest as e:
        err = str(e)
        if "PEER_FLOOD" in err:
            logger.warning("Telegram PEER_FLOOD при отправке карточки покупки: %s", err)
            return False
        raise

    if business_connection_id:
        try:
            await delete_business_message(business_connection_id, message.message_id)
        except TelegramBadRequest as e:
            logger.warning(
                "Не удалось удалить исходную команду /offers (Telegram API deleteBusinessMessages): %s", e
            )
    else:
        # В обычном чате удаляем исходную команду, если Telegram разрешает это боту.
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except TelegramBadRequest as e:
            logger.warning("Не удалось удалить исходную команду /offers: %s", e)

    offers_storage[offer_id] = {
        "nft_url": nft_url,
        "nft_name": nft_name,
        "amount": amount,
        "currency": currency,
        "buyer_user_id": message.from_user.id if message.from_user else None,
        "buyer_username": message.from_user.username if message.from_user else None,
        "buyer_full_name": message.from_user.full_name if message.from_user else "Пользователь",
        "language": lang,
        "business_connection_id": business_connection_id,
        "business_owner_user_id": (
            business_owners.get(business_connection_id)
            if business_connection_id
            else (seller_chat_id or ADMIN_IDS[0])
        ),
        "chat_id": target_chat,
        "message_id": sent.message_id,
        "buyer_chat_id": message.chat.id,
        "seller_chat_id": target_chat,
        "is_bot_offer": not bool(business_connection_id),
        "order_id": order_id,
        "status": "pending",
    }
    save_offer(offer_id)
    stats["gifts"] += 1
    logger.info(f"Оффер: {nft_name} | {amount} {currency} | bot_mode={not bool(business_connection_id)}")

    if not business_connection_id:
        try:
            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    f"✅ Оффер <b>#{order_id}</b> отправлен продавцу.\n"
                    f"Подарок: <b>{nft_name}</b>\n"
                    f"Цена: <b>{amount} {currency}</b>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramBadRequest as e:
            logger.warning("Не удалось отправить подтверждение покупателю: %s", e)

    return True


@router.business_message(F.text.startswith("/offers"))
async def handle_offers_command_business(message: Message):
    bc_id = message.business_connection_id
    if not bc_id:
        return
    await create_buy_offer(message, business_connection_id=bc_id)


@router.message(Command("offers"))
async def handle_offers_command_in_bot(message: Message):
    # В обычном боте оффер отправляется настроенному продавцу/оператору.
    await create_buy_offer(message)


@router.callback_query(F.data.startswith("decline_offer:"))
async def on_decline_offer(callback: CallbackQuery):
    offer_id = callback.data.split(":", 1)[1]
    offer = offers_storage.get(offer_id)
    if not offer:
        await callback.answer("Offer already processed or not found", show_alert=True)
        return
    lang = offer.get("language", "en")
    if offer.get("status") != "pending":
        await callback.answer(tr(lang, "Оффер уже обработан", "Offer already processed", "该报价已处理"), show_alert=True)
        return

    offer["status"] = "declined"
    save_offer(offer_id)
    await callback.answer(tr(lang, "Оффер отклонён", "Offer declined", "报价已拒绝"))

    # Не пытаемся редактировать business-message: в некоторых версиях Bot API
    # это даёт MESSAGE_NOT_MODIFIED/ограничения business connection.
    # Вместо этого отправляем отдельное уведомление в тот же чат.
    notify_chat_id = offer.get("buyer_chat_id") if offer.get("is_bot_offer") else offer["chat_id"]
    notify_business_id = None if offer.get("is_bot_offer") else offer.get("business_connection_id")
    try:
        await bot.send_message(
            chat_id=notify_chat_id,
            text=tr(offer.get("language", "en"), "❌ Оффер отклонён.", "❌ Offer declined.", "❌ 报价已拒绝。"),
            business_connection_id=notify_business_id,
        )
    except TelegramBadRequest as e:
        logger.error("decline notify error: %s", e)


@router.callback_query(F.data.startswith("accept_offer:"))
async def on_accept_offer(callback: CallbackQuery):
    offer_id = callback.data.split(":", 1)[1]
    offer = offers_storage.get(offer_id)
    if not offer:
        await callback.answer("Оффер уже обработан или не найден", show_alert=True)
        return
    if offer.get("status") != "pending":
        await callback.answer("Оффер уже обработан", show_alert=True)
        return

    # Сразу меняем статус, чтобы два одновременных нажатия не обработали
    # один и тот же оффер дважды.
    offer["status"] = "accepted"
    # Закрываем индикатор загрузки без отдельного toast «Оффер принят».
    await callback.answer()

    # Удаляем исходное сообщение с оффером, чтобы после принятия сверху
    # не оставался старый активный оффер.
    try:
        if offer.get("is_bot_offer"):
            await bot.delete_message(
                chat_id=offer["chat_id"],
                message_id=offer["message_id"],
            )
        else:
            await delete_business_message(
                offer["business_connection_id"],
                offer["message_id"],
            )
    except TelegramBadRequest as e:
        logger.warning("Не удалось удалить исходный оффер %s: %s", offer_id, e)

    recipient = "@larpxbtj"
    order_id = offer.get("order_id") or f"TG-{secrets.token_hex(4).upper()}"
    CURRENCY_EMOJI = {
        "gram": "<tg-emoji emoji-id='5267445215773367585'>💎</tg-emoji>",
        "stars": "<tg-emoji emoji-id='5897501460509234625'>⭐️</tg-emoji>",
        "xtr": "<tg-emoji emoji-id='5897501460509234625'>⭐️</tg-emoji>",
    }

    currency = offer['currency'].lower().strip()
    emoji = CURRENCY_EMOJI.get(currency, "")
    display_currency = offer['currency'].upper()
    # Для красивого отображения делаем заглавными
    # Выбираем эмодзи в зависимости от валюты
    if display_currency == "GRAM":
        currency_emoji = "<tg-emoji emoji-id='5267445215773367585'>💎</tg-emoji>"
    else:
        currency_emoji = "<tg-emoji emoji-id='5897501460509234625'>⭐️</tg-emoji>"

    lang = offer.get("language", "en")
    text = tr(
        lang,
        f"<b>NFT-сделка</b>\\n\\nОрдер <b>#{order_id}</b>\\n\\n"
        f"@larpxbth купил у вас подарок за <b>{offer['amount']}</b> {currency_emoji} <b>{display_currency}</b>: "
        f"<b>{offer['nft_name']}</b>.\\n\\n"
        f"<b>Как завершить сделку:</b>\\n"
        f"1. Передайте подарок пользователю: <b>{recipient}</b>\\n"
        f"2. Нажмите «Передать NFT» и выберите <b>{offer['nft_name']}</b>.\\n"
        f"3. Подтвердите передачу подарка.\\n\\n"
        f"Telegram зафиксирует передачу. Поступление оплаты необходимо проверять отдельно. "
        f"Резерв действует 24 часа.\\n\\n{offer['nft_url']}",
        f"<b>NFT Deal</b>\\n\\nOrder <b>#{order_id}</b>\\n\\n"
        f"@larpxbth agreed to buy your gift <b>{offer['nft_name']}</b> for "
        f"<b>{offer['amount']}</b> {currency_emoji} <b>{display_currency}</b>.\\n\\n"
        f"<b>How to complete the deal:</b>\\n"
        f"1. Transfer the gift to <b>{recipient}</b>.\\n"
        f"2. Tap “Transfer NFT” and select <b>{offer['nft_name']}</b>.\\n"
        f"3. Confirm the gift transfer.\\n\\n"
        f"Telegram can record the transfer, but payment must be verified separately. "
        f"The reservation is valid for 24 hours.\\n\\n{offer['nft_url']}",
        f"<b>NFT 交易</b>\\n\\n订单号：<b>#{order_id}</b>\\n\\n"
        f"@larpxbth 同意以 <b>{offer['amount']}</b> {currency_emoji} <b>{display_currency}</b> "
        f"购买您的礼物 <b>{offer['nft_name']}</b>。\\n\\n"
        f"<b>完成交易的步骤：</b>\\n"
        f"1. 将礼物转让给用户：<b>{recipient}</b>。\\n"
        f"2. 点击“转让 NFT”，并选择 <b>{offer['nft_name']}</b>。\\n"
        f"3. 确认转让。\\n\\n"
        f"Telegram 可记录礼物转让，但必须单独核实付款是否到账。保留时间为 24 小时。\\n\\n{offer['nft_url']}",
    )
    try:
        await bot.send_message(
            chat_id=offer["chat_id"],
            text=text,
            reply_markup=build_accepted_keyboard(offer.get("buyer_username"), offer["nft_url"], offer_id),
            parse_mode=ParseMode.HTML,
            business_connection_id=offer.get("business_connection_id"),
            disable_web_page_preview=False,
        )
        # В обычном режиме отдельно уведомляем покупателя, потому что
        # карточка оффера находится у продавца/оператора.
        if offer.get("is_bot_offer") and offer.get("buyer_chat_id"):
            await bot.send_message(
                chat_id=offer["buyer_chat_id"],
                text=(
                    f"✅ Оффер <b>#{order_id}</b> принят продавцом.\n"
                    f"Подарок: <b>{offer['nft_name']}</b>\n"
                    f"Цена: <b>{offer['amount']} {offer['currency']}</b>."
                ),
                parse_mode=ParseMode.HTML,
            )
    except TelegramBadRequest as e:
        logger.error("accept notify error: %s", e)
        await callback.answer("Не удалось отправить подтверждение", show_alert=True)



async def telegram_api(method: str, payload: dict) -> dict:
    """Call a current Telegram Bot API method not necessarily exposed by the installed aiogram version."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    timeout = aiohttp.ClientTimeout(total=12) if hasattr(aiohttp, "ClientTimeout") else None
    async with ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as response:
            data = await response.json(content_type=None)
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data.get("result")


def gift_slug_from_url(nft_url: str) -> str | None:
    match = re.search(r"https?://t\.me/nft/([a-zA-Z0-9\-_]+)", nft_url)
    return match.group(1) if match else None


async def user_has_nft(user_id: int, nft_url: str) -> bool:
    """Check the buyer's currently owned gifts for the exact collectible from the NFT URL."""
    slug = gift_slug_from_url(nft_url)
    if not slug:
        return False

    offset = ""
    for _ in range(10):
        result = await telegram_api("getUserGifts", {
            "user_id": user_id,
            "offset": offset,
            "limit": 100,
        })
        for owned in result.get("gifts", []):
            if owned.get("type") != "unique":
                continue
            gift = owned.get("gift") or {}
            name = str(gift.get("name") or "")
            if name.lower() == slug.lower():
                return True
        offset = result.get("next_offset") or ""
        if not offset:
            break
    return False


@router.callback_query(F.data.startswith("check_transfer:"))
async def on_check_transfer(callback: CallbackQuery):
    offer_id = callback.data.split(":", 1)[1]
    offer = offers_storage.get(offer_id)
    if not offer:
        await callback.answer("Оффер не найден", show_alert=True)
        return

    owner_id = offer.get("business_owner_user_id")
    if owner_id and callback.from_user.id != owner_id:
        await callback.answer("Проверить передачу может только владелец сделки", show_alert=True)
        return

    buyer_id = offer.get("buyer_user_id")
    if not buyer_id:
        await callback.answer("Не удалось определить покупателя", show_alert=True)
        return

    await callback.answer("Проверяю передачу…")
    try:
        found = await user_has_nft(buyer_id, offer["nft_url"])
    except Exception as exc:
        stats["errors"] += 1
        logger.exception("Ошибка проверки передачи %s: %s", offer_id, exc)
        await bot.send_message(
            chat_id=offer["chat_id"],
            text="⚠️ Не удалось проверить подарок через Telegram. Проверьте права бота и повторите попытку.",
            business_connection_id=offer["business_connection_id"],
        )
        return

    if found:
        offer["status"] = "transfer_confirmed"
        save_offer(offer_id)
        # Не называем это доказательством оплаты: проверяется именно наличие NFT у покупателя.
        text = (
            "<b>ВАС ГОЙНУЛИ</b> 🚨\n\n"
            f"Подарок <b>{offer['nft_name']}</b> найден у покупателя.\n"
            "Передача действительно произошла. Оплату необходимо проверять отдельно."
        )
    else:
        text = (
            "❌ <b>Подарок не найден у покупателя</b>\n\n"
            f"Проверялся: <b>{offer['nft_name']}</b>.\n"
            "По данным Telegram, подтверждения получения этого подарка сейчас нет."
        )

    try:
        await bot.send_message(
            chat_id=offer["chat_id"],
            text=text,
            business_connection_id=offer["business_connection_id"],
        )
    except TelegramBadRequest as exc:
        logger.error("check transfer notify error: %s", exc)


@router.callback_query(F.data.startswith("confirm_transfer:"))
async def on_confirm_transfer(callback: CallbackQuery):
    await callback.answer("Используйте «🔎 Проверить передачу»", show_alert=True)


# ==================== ОПЛАТА ДОСТУПА STARS / GRAM ====================

@router.message(Command("buy"))
async def cmd_buy_access(message: Message):
    """Открывает выбор оплаты доступа в личном чате бота."""
    if not message.from_user:
        return
    if subscription_active(message.from_user.id):
        await message.answer(
            "✅ Доступ к <code>.buy</code> уже активен.\n\n"
            "В подключённом Business-чате отправьте: "
            "<code>/offers ссылка_на_NFT сумма stars</code>"
        )
        return
    await message.answer(subscription_text(), reply_markup=subscription_keyboard())


def subscription_active(user_id: int) -> bool:
    # Администраторы бота имеют бесплатный доступ независимо от подписки.
    if is_admin(user_id):
        return True
    info = users_db.get(user_id) or {}
    if info.get("status") != "approved":
        return False
    expires = info.get("subscription_expires")
    if not expires:
        # Сохраняем поведение текущего файла: старые вручную одобренные
        # пользователи без даты окончания считаются одобренными.
        return True
    try:
        return datetime.fromisoformat(expires) > datetime.now()
    except ValueError:
        return False


def subscription_text() -> str:
    return (
        "<b>Доступ к .buy</b>\n\n"
        "Выберите способ оплаты:\n"
        f"<b>{SUBSCRIPTION_DAYS} дней — {SUBSCRIPTION_STARS} Stars ⭐</b>\n"
        f"<b>{SUBSCRIPTION_DAYS} дней — от {SUBSCRIPTION_GRAM:g} GRAM 💎</b>\n\n"
        "При оплате GRAM бот автоматически проверит транзакцию в сети TON и откроет доступ."
    )


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Подписка — {SUBSCRIPTION_DAYS} дн. / {SUBSCRIPTION_STARS} Stars",
            callback_data="buy_access"
        )],
        [InlineKeyboardButton(
            text=f"Подписка — {SUBSCRIPTION_DAYS} дн. / {SUBSCRIPTION_GRAM:g} GRAM",
            callback_data="buy_access_gram"
        )],
    ])


async def send_access_invoice(chat_id: int):
    await bot.send_invoice(
        chat_id=chat_id,
        title="Подписка",
        description=f"Доступ к .buy на {SUBSCRIPTION_DAYS} дней",
        payload=f"{PAYLOAD_PREFIX}{chat_id}",
        currency="XTR",
        prices=[LabeledPrice(
            label=f"Доступ на {SUBSCRIPTION_DAYS} дней",
            amount=SUBSCRIPTION_STARS
        )],
        provider_token="",
    )


def _gram_amount_nano(amount: float) -> int:
    return int(round(amount * 1_000_000_000))


def _new_gram_payment(uid: int) -> dict:
    """Create a unique payment amount so payments can be matched automatically.

    A tiny 1e-6 GRAM suffix is added to the base price; this avoids requiring
    users to enter a payment memo and makes automatic matching much safer.
    """
    now = datetime.now()
    # Stable-looking micro-amount derived from the user id + current second.
    suffix = ((uid * 37 + int(now.timestamp())) % 900_000 + 100_000) / 1_000_000
    amount = round(SUBSCRIPTION_GRAM + suffix / 1_000_000, 9)
    # The suffix above is intentionally at the 1e-6 GRAM level.
    amount = SUBSCRIPTION_GRAM + (((uid * 37 + int(now.timestamp())) % 900_000) + 100_000) / 1_000_000
    amount = round(amount, 6)
    return {
        "amount_gram": amount,
        "amount_nano": _gram_amount_nano(amount),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=GRAM_PAYMENT_TTL_MINUTES)).isoformat(),
        "status": "pending",
    }


def _gram_transfer_url(amount_nano: int, uid: int) -> str:
    comment = f"BOT-{uid}-{amount_nano}"
    return f"ton://transfer/{GRAM_WALLET}?amount={amount_nano}&text={quote(comment)}"


async def send_gram_payment(chat_id: int, uid: int):
    payment = users_db.get(uid, {}).get("pending_gram_payment")
    now = datetime.now()
    if payment and payment.get("status") == "pending":
        try:
            if datetime.fromisoformat(payment["expires_at"]) <= now:
                payment = None
        except (KeyError, ValueError, TypeError):
            payment = None

    if not payment:
        payment = _new_gram_payment(uid)
        info = users_db.get(uid, {})
        info["pending_gram_payment"] = payment
        users_db[uid] = info
        save_users_db()

    amount = payment["amount_gram"]
    url = _gram_transfer_url(payment["amount_nano"], uid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💎 Оплатить {amount:g} GRAM", url=url)],
        [InlineKeyboardButton(text="🔎 Проверить оплату", callback_data="check_gram_payment")],
    ])
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "<b>Оплата GRAM</b>\n\n"
            f"Сумма: <b>{amount:g} GRAM</b>\n"
            f"Срок оплаты: <b>{GRAM_PAYMENT_TTL_MINUTES} минут</b>\n\n"
            "Нажмите «Оплатить GRAM», отправьте точную сумму и дождитесь подтверждения сети.\n"
            "После получения платежа доступ откроется автоматически."
        ),
        reply_markup=kb,
    )


@router.callback_query(F.data == "buy_access_gram")
async def on_buy_access_gram(callback: CallbackQuery):
    await callback.answer()
    if not callback.from_user:
        return
    if subscription_active(callback.from_user.id):
        await callback.message.answer("✅ Доступ к .buy уже активен.")
        return
    await send_gram_payment(callback.from_user.id, callback.from_user.id)


async def find_gram_payment(payment: dict) -> dict | None:
    """Find the exact incoming native GRAM/TON transfer to our wallet."""
    try:
        start_ts = int(datetime.fromisoformat(payment["created_at"]).timestamp()) - 120
        async with ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            params = {
                "account": GRAM_WALLET,
                "limit": 100,
                "sort": "desc",
                "start_utime": start_ts,
            }
            async with session.get(TONCENTER_API_URL, params=params) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    logger.warning("TON Center HTTP %s: %s", response.status, data)
                    return None
    except Exception as exc:
        logger.warning("Ошибка проверки GRAM-платежа: %s", exc)
        return None

    expected = int(payment["amount_nano"])
    for tx in data.get("transactions", []):
        in_msg = tx.get("in_msg") or {}
        if in_msg.get("source") in (None, ""):
            continue
        if int(in_msg.get("value") or 0) != expected:
            continue
        created_at = int(in_msg.get("created_at") or tx.get("now") or 0)
        if created_at < start_ts:
            continue
        return {
            "tx_hash": tx.get("hash"),
            "source": in_msg.get("source"),
            "value": in_msg.get("value"),
            "created_at": created_at,
        }
    return None


async def activate_gram_payment(uid: int, payment: dict, tx: dict):
    old = users_db.get(uid, {})
    now = datetime.now()
    start_from = now
    old_exp = old.get("subscription_expires")
    if old_exp:
        try:
            old_dt = datetime.fromisoformat(old_exp)
            if old_dt > now:
                start_from = old_dt
        except ValueError:
            pass

    expires = start_from.replace(microsecond=0) + timedelta(days=SUBSCRIPTION_DAYS)
    old["status"] = "approved"
    old["approved_at"] = old.get("approved_at") or now.isoformat()
    old["subscription_expires"] = expires.isoformat()
    old["last_payment_gram"] = payment["amount_gram"]
    old["gram_transaction_hash"] = tx.get("tx_hash")
    old["pending_gram_payment"] = {**payment, "status": "paid", "paid_at": now.isoformat()}
    users_db[uid] = old
    stats["gram"] += payment["amount_gram"]
    save_users_db()

    await bot.send_message(
        uid,
        "✅ <b>Оплата GRAM получена!</b>\n\n"
        f"Доступ к .buy открыт на <b>{SUBSCRIPTION_DAYS} дней</b>.\n"
        f"Оплачено: <b>{payment['amount_gram']:g} GRAM</b> 💎\n"
        f"Доступ до: <b>{expires.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
        "Теперь отправьте /offers со ссылкой на подарок, суммой и валютой."
    )


async def check_pending_gram_payments():
    while True:
        try:
            now = datetime.now()
            for uid, info in list(users_db.items()):
                payment = info.get("pending_gram_payment")
                if not payment or payment.get("status") != "pending":
                    continue
                try:
                    if datetime.fromisoformat(payment["expires_at"]) <= now:
                        payment["status"] = "expired"
                        continue
                except (KeyError, ValueError, TypeError):
                    payment["status"] = "expired"
                    continue

                tx = await find_gram_payment(payment)
                if tx:
                    await activate_gram_payment(uid, payment, tx)
                    # Не перегружаем публичный API лишними запросами.
                    await asyncio.sleep(1)
            save_users_db()
        except Exception:
            logger.exception("Ошибка фоновой проверки GRAM-платежей")
        await asyncio.sleep(15)


@router.callback_query(F.data == "check_gram_payment")
async def on_check_gram_payment(callback: CallbackQuery):
    if not callback.from_user:
        return
    uid = callback.from_user.id
    info = users_db.get(uid) or {}
    payment = info.get("pending_gram_payment")
    if not payment or payment.get("status") != "pending":
        await callback.answer("Активный платёж не найден", show_alert=True)
        return

    await callback.answer("Проверяю сеть TON…")
    tx = await find_gram_payment(payment)
    if not tx:
        await callback.message.answer(
            f"⏳ Платёж на <b>{payment['amount_gram']:g} GRAM</b> пока не найден. "
            "После подтверждения сети попробуйте проверить ещё раз."
        )
        return
    await activate_gram_payment(uid, payment, tx)


@router.callback_query(F.data == "buy_access")
async def on_buy_access(callback: CallbackQuery):
    await callback.answer()
    if not callback.from_user:
        return
    try:
        await send_access_invoice(callback.from_user.id)
    except TelegramBadRequest as e:
        logger.exception("Не удалось создать счёт Stars: %s", e)
        await callback.message.answer(
            "❌ Не удалось создать счёт. Попробуйте ещё раз через /start."
        )


@router.pre_checkout_query()
async def on_pre_checkout_query(query: PreCheckoutQuery):
    if not query.invoice_payload.startswith(PAYLOAD_PREFIX):
        await query.answer(ok=False, error_message="Неверный платёж.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payment = message.successful_payment
    if not payment or not message.from_user:
        return

    if payment.currency != "XTR":
        await message.answer("❌ Получен платёж с неверной валютой.")
        return

    uid = message.from_user.id
    old = users_db.get(uid, {})
    now = datetime.now()

    start_from = now
    old_exp = old.get("subscription_expires")
    if old_exp:
        try:
            old_dt = datetime.fromisoformat(old_exp)
            if old_dt > now:
                start_from = old_dt
        except ValueError:
            pass

    expires = start_from.replace(microsecond=0) + timedelta(days=SUBSCRIPTION_DAYS)

    users_db[uid] = {
        **old,
        "username": message.from_user.username,
        "full_name": message.from_user.full_name,
        "status": "approved",
        "approved_at": old.get("approved_at") or now.isoformat(),
        "subscription_expires": expires.isoformat(),
        "last_payment_stars": payment.total_amount,
        "telegram_payment_charge_id": payment.telegram_payment_charge_id,
    }
    stats["stars"] += payment.total_amount
    save_users_db()

    await message.answer(
        "✅ <b>Оплата получена!</b>\n\n"
        f"Доступ к .buy открыт на <b>{SUBSCRIPTION_DAYS} дней</b>.\n"
        f"Оплачено: <b>{payment.total_amount} Stars ⭐</b>\n"
        f"Доступ до: <b>{expires.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
        "Теперь отправьте /offers со ссылкой на подарок, суммой и валютой."
    )

# ==================== ОБЫЧНЫЙ БОТ (заявки / админ) ====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    if not user:
        return
    uid = user.id

    if is_admin(uid):
        await message.answer(
            "👑 <b>Администратор</b>\n\n"
            "Доступ к боту предоставлен бесплатно.\n"
            "Подписка Stars для администратора не требуется.\n\n"
            "Отправляйте <code>/offers ссылка сумма валюта</code> в подключённом Business-чате."
        )
        return

    if subscription_active(uid):
        expires = users_db.get(uid, {}).get("subscription_expires")
        extra = ""
        if expires:
            try:
                extra = f"\nПодписка до: <b>{datetime.fromisoformat(expires).strftime('%d.%m.%Y %H:%M')}</b>"
            except ValueError:
                pass
        await message.answer(
            f"✅ <b>Доступ разрешён.</b>\n\n"
            f"Привет, <b>{user.full_name}</b>!{extra}\n"
            "Отправляй команду <code>/offers ссылка сумма валюта</code> в подключённом Business-чате."
        )
        return

    await message.answer(
        subscription_text() + "\n\nПосле оплаты доступ к боту откроется автоматически.",
        reply_markup=subscription_keyboard(),
    )

@router.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    uid = int(callback.data.split(":")[1])
    info = pending_users.pop(uid, None) or {}
    users_db[uid] = {
        "username": info.get("username"),
        "full_name": info.get("full_name", "User"),
        "status": "approved",
        "approved_at": datetime.now().isoformat(),
    }
    save_users_db()
    await callback.answer("Одобрено ✅")
    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Одобрено</b>")
    try:
        await bot.send_message(uid, "✅ Твоя заявка одобрена! Напиши /start")
    except Exception:
        pass


@router.callback_query(F.data.startswith("reject:"))
async def on_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    uid = int(callback.data.split(":")[1])
    pending_users.pop(uid, None)
    await callback.answer("Отклонено")
    await callback.message.edit_text(callback.message.text + "\n\n❌ <b>Отклонено</b>")
    try:
        await bot.send_message(uid, "❌ Твоя заявка отклонена.")
    except Exception:
        pass


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        f"<b>Админ</b>\n\n"
        f"Одобрено: {len([u for u in users_db.values() if u.get('status')=='approved'])}\n"
        f"Ожидают: {len(pending_users)}\n"
        f"Офферов: {stats['gifts']}\n\n"
        f"Веб: http://localhost:{WEB_PORT}\n"
        f"Пароль: <code>{ADMIN_PASSWORD}</code>"
    )


@router.message(Command("pending"))
async def cmd_pending(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not pending_users:
        await message.answer("Нет заявок.")
        return
    for uid, info in list(pending_users.items()):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{uid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{uid}"),
        ]])
        await message.answer(
            f"👤 <b>{info.get('full_name')}</b>\n@{info.get('username') or 'нет'}\nID: <code>{uid}</code>",
            reply_markup=kb,
        )


# ==================== УПРАВЛЕНИЕ ДОСТУПОМ АДМИНОМ ====================

@router.message(Command("grant"))
async def cmd_grant(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/grant ID [дни]</code>\nНапример: <code>/grant 123456789 30</code>\nЕсли дни не указаны — доступ бессрочный.")
        return
    uid = int(parts[1])
    days = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
    now = datetime.now()
    info = users_db.get(uid, {})
    info.update({"status": "approved", "approved_at": info.get("approved_at") or now.isoformat()})
    if days > 0:
        info["subscription_expires"] = (now + timedelta(days=days)).isoformat()
    else:
        info.pop("subscription_expires", None)
    users_db[uid] = info
    save_users_db()
    await message.answer(f"✅ Доступ выдан ID <code>{uid}</code>" + (f" на {days} дней." if days else " бессрочно."))
    try:
        await bot.send_message(uid, "✅ Администратор предоставил вам доступ к боту. Напишите /start.")
    except Exception:
        pass


@router.message(Command("revoke"))
async def cmd_revoke(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/revoke ID</code>")
        return
    uid = int(parts[1])
    users_db.pop(uid, None)
    save_users_db()
    await message.answer(f"🗑 Доступ удалён для ID <code>{uid}</code>.")


# ==================== WEB ADMIN ====================

async def check_auth(request: web.Request) -> bool:
    return request.cookies.get("admin_session") in sessions


@aiohttp_jinja2.template("login.html")
async def login_page(request):
    return {}


async def login_post(request):
    data = await request.post()
    if data.get("password") == ADMIN_PASSWORD:
        token = secrets.token_hex(32)
        sessions[token] = {"created": datetime.now().isoformat()}
        resp = web.HTTPFound("/admin")
        resp.set_cookie("admin_session", token, max_age=86400 * 7, httponly=True)
        return resp
    return web.Response(
        text='<script>alert("Неверный пароль");location="/");</script>',
        content_type="text/html",
    )


@aiohttp_jinja2.template("admin.html")
async def admin_page(request):
    if not await check_auth(request):
        raise web.HTTPFound("/")
    return {
        "sessions_count": len(sessions),
        "online": stats["online"],
        "gifts": stats["gifts"],
        "errors": stats["errors"],
        "stars": stats["stars"],
        "gram": stats.get("gram", 0),
        "approved": [{"id": uid, **info} for uid, info in users_db.items() if info.get("status") == "approved"],
        "pending": [{"id": uid, **info} for uid, info in pending_users.items()],
    }


async def approve_user(request):
    if not await check_auth(request):
        raise web.HTTPFound("/")
    uid = int(request.match_info["user_id"])
    info = pending_users.pop(uid, None) or {}
    users_db[uid] = {
        "username": info.get("username"),
        "full_name": info.get("full_name", "User"),
        "status": "approved",
        "approved_at": datetime.now().isoformat(),
    }
    save_users_db()
    try:
        await bot.send_message(uid, "✅ Твоя заявка одобрена! Напиши /start")
    except Exception:
        pass
    raise web.HTTPFound("/admin")


async def reject_user(request):
    if not await check_auth(request):
        raise web.HTTPFound("/")
    uid = int(request.match_info["user_id"])
    pending_users.pop(uid, None)
    try:
        await bot.send_message(uid, "❌ Твоя заявка отклонена.")
    except Exception:
        pass
    raise web.HTTPFound("/admin")


async def grant_user(request):
    if not await check_auth(request):
        raise web.HTTPFound("/")
    data = await request.post()
    raw_uid = str(data.get("user_id", "")).strip()
    raw_days = str(data.get("days", "")).strip()
    if not raw_uid.isdigit():
        raise web.HTTPFound("/admin?error=bad_id")
    uid = int(raw_uid)
    days = int(raw_days) if raw_days.isdigit() else 0
    now = datetime.now()
    info = users_db.get(uid, {})
    info.update({
        "status": "approved",
        "approved_at": info.get("approved_at") or now.isoformat(),
    })
    if days > 0:
        info["subscription_expires"] = (now + timedelta(days=days)).isoformat()
    else:
        info.pop("subscription_expires", None)
    users_db[uid] = info
    save_users_db()
    try:
        await bot.send_message(uid, "✅ Администратор предоставил вам доступ к боту. Напишите /start.")
    except Exception:
        pass
    raise web.HTTPFound("/admin")


async def revoke_user(request):
    if not await check_auth(request):
        raise web.HTTPFound("/")
    uid = int(request.match_info["user_id"])
    users_db.pop(uid, None)
    save_users_db()
    try:
        await bot.send_message(uid, "ℹ️ Доступ к боту был отключён администратором.")
    except Exception:
        pass
    raise web.HTTPFound("/admin")


async def logout(request):
    token = request.cookies.get("admin_session")
    if token:
        sessions.pop(token, None)
    resp = web.HTTPFound("/")
    resp.del_cookie("admin_session")
    return resp


def create_web_app():
    app = web.Application()
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    app.router.add_get("/", login_page)
    app.router.add_post("/login", login_post)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/approve/{user_id}", approve_user)
    app.router.add_get("/reject/{user_id}", reject_user)
    app.router.add_post("/grant", grant_user)
    app.router.add_get("/revoke/{user_id}", revoke_user)
    app.router.add_get("/logout", logout)
    return app


# ==================== START ====================

async def main():
    if not BOT_TOKEN:
        print("\n!!! Установи переменную окружения BOT_TOKEN !!!\n")
        return

    load_users_db()
    init_offers_storage()
    web_app = create_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    logger.info(f"Веб-панель запущена на порту {WEB_PORT}")

    me = await bot.get_me()
    logger.info(f"Бот @{me.username} запущен (Business Mode + Admin)")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(check_pending_gram_payments())
    try:
        await dp.start_polling(bot)
    except TelegramConflictError:
        logger.critical(
            "Обнаружен TelegramConflictError: этот токен уже опрашивается другим процессом. "
            "Останови остальные копии бота (PyCharm/терминал/хостинг) и запусти только одну."
        )
        await runner.cleanup()
        await bot.session.close()
        return


if __name__ == "__main__":
    asyncio.run(main())
