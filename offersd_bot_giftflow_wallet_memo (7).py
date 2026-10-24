"""
Admin Bot + NFT / Username / Number Offer Generator (Business Mode)
- Telegram Business Mode (/offers офферы)
- Офферы на NFT-подарки, уникальные юзернеймы и анонимные номера
- Передача username/number на TON-кошелёк покупателя
- Веб админ-панель
- Одобрение пользователей по ID
"""

import asyncio
import logging
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
    LinkPreviewOptions,
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
ORDERS_FILE = Path(__file__).parent / "data" / "orders.json"

# Оплата доступа через Telegram Stars
SUBSCRIPTION_STARS = int(os.getenv("SUBSCRIPTION_STARS", "666"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
PAYLOAD_PREFIX = "bot_access_"

# Оплата доступа через GRAM (нативный TON/GRAM в сети TON)
GRAM_WALLET = os.getenv("GRAM_WALLET", "UQD9qii_hUBsLpDi1HRj3oMTcZmwoVRbMBhcMi_DZVJJcykT")
SUBSCRIPTION_GRAM = float(os.getenv("SUBSCRIPTION_GRAM", "6"))
GRAM_PAYMENT_TTL_MINUTES = int(os.getenv("GRAM_PAYMENT_TTL_MINUTES", "30"))
TONCENTER_API_URL = "https://toncenter.com/api/v3/transactions"

# Кошелёк, НА КОТОРЫЙ продавец передаёт username / number после принятия оффера.
# По умолчанию = твой GRAM_WALLET. Можно задать отдельно: TRANSFER_WALLET=UQ...
TRANSFER_WALLET = (os.getenv("TRANSFER_WALLET") or GRAM_WALLET).strip()

# Реферальная система: % от оплаты приглашённого друга
REFERRAL_PERCENT = float(os.getenv("REFERRAL_PERCENT", "40"))

# Восстановление доступа по коду (если аккаунт снесли)
RECOVERY_STARS = int(os.getenv("RECOVERY_STARS", "111"))
RECOVERY_GRAM = float(os.getenv("RECOVERY_GRAM", "1"))

# Обязательная подписка на канал
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "offerworkk").lstrip("@")
REQUIRED_CHANNEL_URL = f"https://t.me/{REQUIRED_CHANNEL}"
# ==================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ---------- хранилища ----------
users_db: dict[int, dict] = {}
pending_users: dict[int, dict] = {}
sessions: dict[str, dict] = {}
offers_storage: dict[str, dict] = {}
# recovery_code (upper) -> user_id владельца подписки
recovery_index: dict[str, int] = {}
# business_connection_id -> Telegram user id of the managed business account
business_owners: dict[str, int] = {}
stats = {"gifts": 0, "errors": 0, "stars": 0, "gram": 0, "online": 0}

# Ограничиваем частоту отправки офферов, чтобы не создавать резкие всплески запросов.
offer_send_lock = asyncio.Lock()
last_offer_send_at = 0.0
OFFER_SEND_INTERVAL_SECONDS = 2.0

# Состояние пошаговой рассылки в личном чате с ботом (только администраторы).
broadcast_states: dict[int, dict] = {}

# ---------- TON wallet helpers ----------
TON_WALLET_RE = re.compile(r"^(UQ|EQ)[A-Za-z0-9_-]{46}$")


def is_valid_ton_wallet(address: str) -> bool:
    """Базовая проверка адреса TON (UQ/EQ + 46 символов)."""
    if not address:
        return False
    return bool(TON_WALLET_RE.match(address.strip()))


def get_user_ton_wallet(user_id: int) -> str | None:
    info = users_db.get(user_id) or {}
    wallet = (info.get("ton_wallet") or "").strip()
    return wallet if is_valid_ton_wallet(wallet) else None


def set_user_ton_wallet(user_id: int, wallet: str, user=None) -> None:
    info = users_db.get(user_id) or {}
    info["ton_wallet"] = wallet.strip()
    info["ton_wallet_set_at"] = datetime.now().isoformat()
    if user:
        info.setdefault("username", getattr(user, "username", None))
        info.setdefault("full_name", getattr(user, "full_name", None))
    users_db[user_id] = info
    save_users_db()


def load_users_db() -> None:
    """Load approved/subscribed users from a small JSON file."""
    global users_db, recovery_index
    try:
        if DATA_FILE.exists():
            import json
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            users_db = {int(uid): info for uid, info in raw.items()}
        else:
            users_db = {}
    except Exception as exc:
        logger.exception("Не удалось загрузить базу пользователей: %s", exc)
        users_db = {}
    recovery_index = {}
    for uid, info in users_db.items():
        code = (info.get("recovery_code") or "").strip().upper()
        if code:
            recovery_index[code] = uid


def save_users_db() -> None:
    try:
        import json
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps(users_db, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.exception("Не удалось сохранить базу пользователей: %s", exc)


def load_offers_storage() -> None:
    """Restore offers/orders after a restart."""
    global offers_storage
    try:
        if ORDERS_FILE.exists():
            import json
            raw = json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
            offers_storage = {str(oid): item for oid, item in raw.items() if isinstance(item, dict)}
    except Exception as exc:
        logger.exception("Не удалось загрузить историю заказов: %s", exc)
        offers_storage = {}


def save_offers_storage() -> None:
    """Persist order state to disk."""
    try:
        import json
        ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ORDERS_FILE.write_text(json.dumps(offers_storage, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.exception("Не удалось сохранить историю заказов: %s", exc)


# ---------- referral ----------
_bot_username: str | None = None


async def get_bot_username() -> str:
    global _bot_username
    if _bot_username:
        return _bot_username
    me = await bot.get_me()
    _bot_username = me.username or ""
    return _bot_username


def referral_link(user_id: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def bind_referrer(uid: int, referrer_id: int, user) -> bool:
    """Привязать реферера к новому пользователю. Возвращает True, если привязка успешна."""
    if referrer_id == uid:
        return False
    if referrer_id not in users_db and referrer_id not in ADMIN_IDS:
        # Реферер должен хотя бы раз зайти в бота (есть в базе) или быть админом
        pass  # всё равно разрешаем: реферер может быть ещё не в users_db
    info = users_db.get(uid, {})
    if info.get("referred_by"):
        return False  # уже привязан
    if info.get("status") == "approved" and subscription_active(uid):
        return False  # уже оплатил — поздно
    info["referred_by"] = referrer_id
    info["referred_at"] = datetime.now().isoformat()
    if user:
        info.setdefault("username", user.username)
        info.setdefault("full_name", user.full_name)
    users_db[uid] = info
    # У реферера считаем приглашённых
    ref_info = users_db.get(referrer_id, {})
    invited = ref_info.get("invited_ids") or []
    if uid not in invited:
        invited.append(uid)
    ref_info["invited_ids"] = invited
    users_db[referrer_id] = ref_info
    save_users_db()
    return True


async def credit_referral(payer_id: int, amount: float, currency: str) -> None:
    """Начислить рефереру REFERRAL_PERCENT% от суммы оплаты друга."""
    info = users_db.get(payer_id) or {}
    referrer_id = info.get("referred_by")
    if not referrer_id or referrer_id == payer_id:
        return

    reward = round(amount * REFERRAL_PERCENT / 100.0, 6)
    if reward <= 0:
        return

    currency = currency.upper()
    ref = users_db.get(referrer_id, {})
    balances = ref.get("referral_balance") or {}
    balances[currency] = round(float(balances.get(currency, 0)) + reward, 6)
    ref["referral_balance"] = balances

    history = ref.get("referral_history") or []
    history.append({
        "from_user_id": payer_id,
        "amount": reward,
        "currency": currency,
        "base_amount": amount,
        "percent": REFERRAL_PERCENT,
        "at": datetime.now().isoformat(),
    })
    ref["referral_history"] = history[-100:]  # храним последние 100
    users_db[referrer_id] = ref
    save_users_db()

    cur_mark = (
        "⭐" if currency == "STARS"
        else "💎" if currency == "GRAM"
        else "☺️" if currency == "RUB"
        else currency
    )
    try:
        await bot.send_message(
            referrer_id,
            f"🎁 <b>Реферальное вознаграждение!</b>\n\n"
            f"Ваш друг оплатил подписку.\n"
            f"Вам начислено: <b>{reward:g} {currency}</b> {cur_mark}\n"
            f"({REFERRAL_PERCENT:g}% от {amount:g} {currency})\n\n"
            f"Баланс: <b>{balances.get(currency, 0):g} {currency}</b>\n"
            f"Вывод — напишите администратору или /ref",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить реферера %s: %s", referrer_id, e)

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"👥 Реферал: пользователь <code>{payer_id}</code> оплатил "
                f"<b>{amount:g} {currency}</b>\n"
                f"Рефереру <code>{referrer_id}</code> начислено "
                f"<b>{reward:g} {currency}</b> ({REFERRAL_PERCENT:g}%)",
            )
        except Exception:
            pass


# ---------- bot ----------
router = Router()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.include_router(router)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def check_channel_subscription(user_id: int) -> tuple[bool, str]:
    """
    Проверка подписки на REQUIRED_CHANNEL.
    Возвращает (ok, reason). Админы всегда ok.
    """
    if is_admin(user_id):
        return True, "admin"

    chat_id = f"@{REQUIRED_CHANNEL}"
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as exc:
        err = str(exc).lower()
        logger.warning("channel sub check failed for %s: %s", user_id, exc)
        if "member list is inaccessible" in err or "chat not found" in err or "not enough rights" in err:
            return False, "bot_not_admin"
        if "user not found" in err:
            return False, "not_subscribed"
        return False, f"error:{exc}"

    status = getattr(member, "status", None)
    raw = getattr(status, "value", None) or str(status).split(".")[-1].lower()
    # aiogram 3: ChatMemberStatus.MEMBER → "member"
    if raw in ("creator", "administrator", "member", "restricted"):
        return True, raw
    # left / kicked / banned
    return False, "not_subscribed"


async def is_subscribed_to_channel(user_id: int) -> bool:
    ok, _ = await check_channel_subscription(user_id)
    return ok


def channel_subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался — проверить", callback_data="check_channel_sub")],
    ])


async def require_channel(message_or_callback) -> bool:
    """True если доступ разрешён. Иначе отправляет просьбу подписаться."""
    user = message_or_callback.from_user
    if not user:
        return False
    if await is_subscribed_to_channel(user.id):
        return True
    text = (
        f"🔒 Чтобы пользоваться ботом, подпишитесь на канал:\n"
        f"<b>@{REQUIRED_CHANNEL}</b>\n\n"
        f"После подписки нажмите «Я подписался»."
    )
    if hasattr(message_or_callback, "answer") and hasattr(message_or_callback, "message"):
        # CallbackQuery
        try:
            await message_or_callback.answer("Нужна подписка на канал", show_alert=True)
        except Exception:
            pass
        await message_or_callback.message.answer(text, reply_markup=channel_subscribe_keyboard())
    else:
        await message_or_callback.answer(text, reply_markup=channel_subscribe_keyboard())
    return False


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


def normalize_username(raw: str) -> str:
    """@name или name → @name (без смены регистра, как введён)."""
    name = (raw or "").strip().lstrip("@")
    return f"@{name}" if name else ""


def username_preview_url(username: str) -> str:
    """Ссылка для превью карточки юзернейма (как у NFT)."""
    name = (username or "").strip().lstrip("@")
    # Fragment даёт карточку с описанием; t.me — нативный превью Telegram
    return f"https://t.me/{name}" if name else "https://fragment.com"


def username_fragment_url(username: str) -> str:
    name = (username or "").strip().lstrip("@")
    return f"https://fragment.com/username/{name}" if name else "https://fragment.com"


def normalize_anon_number(raw: str) -> str:
    """Приводит анонимный номер к виду +888..."""
    digits = re.sub(r"[^\d]", "", raw or "")
    if digits.startswith("888"):
        return f"+{digits}"
    if digits:
        return f"+{digits}"
    return (raw or "").strip()


def number_preview_url(number: str) -> str:
    """Ссылка для превью анонимного номера."""
    digits = re.sub(r"[^\d]", "", number or "")
    if digits:
        return f"https://fragment.com/number/{digits}"
    return "https://fragment.com"


def offer_asset_label(offer: dict) -> str:
    """Человекочитаемое название актива для карточек."""
    otype = offer.get("offer_type") or "nft"
    if otype == "username":
        return offer.get("target_name") or "username"
    if otype == "number":
        return offer.get("target_name") or "number"
    return offer.get("nft_name") or "NFT"


def format_currency(currency: str) -> str:
    currency = currency.lower().strip()
    return {
        "stars": "STARS",
        "star": "STARS",
        "xtr": "STARS",
        "gram": "GRAM",
        "grams": "GRAM",
        "rub": "RUB",
        "rubs": "RUB",
        "ruble": "RUB",
        "rubles": "RUB",
        "руб": "RUB",
        "рубль": "RUB",
        "рубли": "RUB",
        "рублях": "RUB",
        "₽": "RUB",
    }.get(currency, currency.upper())

def currency_emoji(currency: str) -> str:
    """Premium Telegram emoji for the offer currency."""
    normalized = format_currency(currency)
    if normalized == "STARS":
        return "<tg-emoji emoji-id='5897501460509234625'>⭐️</tg-emoji>"
    if normalized == "GRAM":
        return "<tg-emoji emoji-id='5267445215773367585'>💎</tg-emoji>"
    if normalized == "RUB":
        return "<tg-emoji emoji-id='5215629789716565456'>☺️</tg-emoji>"
    return ""


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


def build_offer_keyboard(offer_id: str, lang: str | None = None) -> InlineKeyboardMarkup:
    # Callback не содержит ID пользователя: поэтому нажать кнопку может
    # любой участник чата, а не только автор команды /offers.
    # Язык передаём явно: при создании оффера записи в offers_storage ещё нет.
    if not lang:
        lang = offers_storage.get(offer_id, {}).get("language") or "ru"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr(lang, "Отклонить", "Decline", "拒绝"),
                    callback_data=f"decline_offer:{offer_id}",
                    icon_custom_emoji_id="5296678515536581003"
                ),
                InlineKeyboardButton(
                    text=tr(lang, "Принять", "Accept", "接受"),
                    callback_data=f"accept_offer:{offer_id}",
                    icon_custom_emoji_id="5823537588186647980"
                ),
            ],
        ]
    )


def build_accepted_keyboard(lang: str, offer_id: str) -> InlineKeyboardMarkup:
    """Кнопки карточки после принятия оффера."""
    offer = offers_storage.get(offer_id) or {}
    otype = offer.get("offer_type") or "nft"

    if otype == "nft":
        transfer_url = "tg://send_gift?to=larpxbtj"
        transfer_btn = InlineKeyboardButton(
            text=tr(lang, "Передать NFT", "Transfer NFT", "转让 NFT"),
            url=transfer_url,
            icon_custom_emoji_id="5271604874419647061",
        )
    elif otype == "username":
        frag = username_fragment_url(offer.get("target_name") or "")
        transfer_btn = InlineKeyboardButton(
            text=tr(lang, "Открыть Fragment", "Open Fragment", "打开 Fragment"),
            url=frag,
            icon_custom_emoji_id="5271604874419647061",
        )
    else:
        # number
        num_url = number_preview_url(offer.get("target_name") or "")
        transfer_btn = InlineKeyboardButton(
            text=tr(lang, "Открыть Fragment", "Open Fragment", "打开 Fragment"),
            url=num_url,
            icon_custom_emoji_id="5271604874419647061",
        )

    return InlineKeyboardMarkup(inline_keyboard=[
        [transfer_btn],
        [InlineKeyboardButton(
            text=tr(lang, "Как передать?", "How to transfer?", "如何转让？"),
            callback_data=f"how_transfer:{offer_id}",
            icon_custom_emoji_id="5188217332748527444",
        )],
    ])


def get_sender_name(message: Message) -> str:
    """@username if available, otherwise full name."""
    user = message.from_user
    if not user:
        return "User"
    if user.username:
        return f"@{user.username}"
    return user.full_name or f"id{user.id}"


def buyer_display_name(offer: dict) -> str:
    """@username if available, otherwise full name from stored offer."""
    username = offer.get("buyer_username")
    if username:
        return f"@{username}"
    return offer.get("buyer_full_name") or "User"


@router.business_connection()
async def on_business_connection(connection: BusinessConnection):
    status = "включено" if connection.is_enabled else "отключено"
    if connection.is_enabled and connection.user:
        business_owners[connection.id] = connection.user.id
    else:
        business_owners.pop(connection.id, None)
    logger.info(f"Business connection {connection.id} | {status}")


# NFT: /offers https://t.me/nft/... 1200 gram [EN]
BUY_PATTERN_NFT = re.compile(
    r"^\s*/offers?(?:@\w+)?\s+(https?://t\.me/nft/[a-zA-Z0-9\-_]+)\s+([0-9]+(?:[.,][0-9]+)?)\s+(gram|grams|stars|star|xtr|rub|rubs|ruble|rubles|руб|рубль|рубли|₽)(?:\s+(EN|CN|NG))?\s*$",
    re.IGNORECASE,
)

# Username: /offers username @name 50 gram [EN]
BUY_PATTERN_USERNAME = re.compile(
    r"^\s*/offers?(?:@\w+)?\s+username\s+@?([A-Za-z0-9_]{4,32})\s+([0-9]+(?:[.,][0-9]+)?)\s+(gram|grams|stars|star|xtr|rub|rubs|ruble|rubles|руб|рубль|рубли|₽)(?:\s+(EN|CN|NG))?\s*$",
    re.IGNORECASE,
)

# Number: /offers number +888... 25 gram [EN]
BUY_PATTERN_NUMBER = re.compile(
    r"^\s*/offers?(?:@\w+)?\s+number\s+(\+?888[\d\s\-()]{5,20}|\+?\d{8,15})\s+([0-9]+(?:[.,][0-9]+)?)\s+(gram|grams|stars|star|xtr|rub|rubs|ruble|rubles|руб|рубль|рубли|₽)(?:\s+(EN|CN|NG))?\s*$",
    re.IGNORECASE,
)

# Старый паттерн оставляем для совместимости с broadcast
BUY_PATTERN = BUY_PATTERN_NFT


def buy_format_error_text() -> str:
    return (
        "❌ Формат команды:\n\n"
        "<b>NFT-подарок:</b>\n"
        "<code>/offers https://t.me/nft/HappyBrownie-225956 1200 gram</code>\n\n"
        "<b>Юзернейм:</b>\n"
        "<code>/offers username @coolname 50 gram</code>\n\n"
        "<b>Анонимный номер:</b>\n"
        "<code>/offers number +88812345678 25 gram</code>\n\n"
        "Валюта: <code>gram</code> / <code>stars</code> / <code>rub</code>\n"
        "Язык карточки (опционально): <code>EN</code> / <code>CN</code>"
    )


def parse_offer_command(text: str) -> dict | None:
    """
    Парсит /offers ... и возвращает dict с полями:
    offer_type, target, amount, currency_raw, lang_override
    или None если формат неверный.
    """
    text = (text or "").strip()

    m = BUY_PATTERN_NFT.match(text)
    if m:
        return {
            "offer_type": "nft",
            "target": m.group(1),
            "amount": m.group(2),
            "currency_raw": m.group(3),
            "lang_override": m.group(4),
        }

    m = BUY_PATTERN_USERNAME.match(text)
    if m:
        return {
            "offer_type": "username",
            "target": m.group(1),
            "amount": m.group(2),
            "currency_raw": m.group(3),
            "lang_override": m.group(4),
        }

    m = BUY_PATTERN_NUMBER.match(text)
    if m:
        return {
            "offer_type": "number",
            "target": m.group(1),
            "amount": m.group(2),
            "currency_raw": m.group(3),
            "lang_override": m.group(4),
        }

    return None



async def create_buy_offer(
    message: Message,
    *,
    business_connection_id: str | None = None,
    seller_chat_id: int | None = None,
) -> bool:
    """Create offer for NFT / username / anonymous number (Business + normal bot)."""
    # Без активной подписки офферы недоступны (в т.ч. в Business Mode).
    buyer = message.from_user
    if not buyer:
        return False
    if not subscription_active(buyer.id) and not is_admin(buyer.id):
        await bot.send_message(
            chat_id=message.chat.id,
            text=(
                "🔒 <b>Нужна подписка</b>\n\n"
                "Чтобы отправлять офферы, купите доступ к боту.\n"
                "Откройте бота в личке и нажмите /buy или /start."
            ),
            parse_mode=ParseMode.HTML,
            business_connection_id=business_connection_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="⭐ Купить доступ",
                    url=f"https://t.me/{(await get_bot_username()) or 'bot'}?start=buy",
                )],
            ]),
        )
        return False

    text = message.text or ""
    parsed = parse_offer_command(text)
    if not parsed:
        await bot.send_message(
            chat_id=message.chat.id,
            text=tr(
                user_language_code(message.from_user),
                buy_format_error_text(),
                "❌ Format:\n<code>/offers https://t.me/nft/... AMOUNT CURRENCY</code>\n"
                "<code>/offers username @name AMOUNT CURRENCY</code>\n"
                "<code>/offers number +888... AMOUNT CURRENCY</code>",
                "❌ 格式：\n<code>/offers https://t.me/nft/... 金额 币种</code>\n"
                "<code>/offers username @name 金额 币种</code>\n"
                "<code>/offers number +888... 金额 币种</code>",
            ),
            parse_mode=ParseMode.HTML,
            business_connection_id=business_connection_id,
        )
        return False

    offer_type = parsed["offer_type"]
    amount = parsed["amount"].replace(",", ".")
    currency = format_currency(parsed["currency_raw"])
    lang_override = parsed["lang_override"]
    offer_id = secrets.token_urlsafe(9)
    order_id = f"TG-{secrets.token_hex(4).upper()}"
    lang = {"EN": "en", "CN": "zh", "NG": "zh"}.get(
        (lang_override or "").upper(),
        "ru" if not lang_override else user_language_code(message.from_user),
    )
    sender = get_sender_name(message)
    currency_mark = currency_emoji(currency)
    buyer_id = message.from_user.id if message.from_user else None

    # --- подготовка полей актива ---
    nft_url = None
    nft_name = None
    target_name = None
    preview_url = None
    link_preview = None
    # Username / number всегда передаются на ТВОЙ фиксированный кошелёк
    transfer_wallet = TRANSFER_WALLET

    if offer_type == "nft":
        nft_url = parsed["target"]
        nft_name = parse_nft_name(nft_url)
        target_name = nft_name
        preview_url = nft_url
        asset_ru = f"подарок <b>{nft_name}</b>"
        asset_en = f"the gift <b>{nft_name}</b>"
        asset_zh = f"礼物 <b>{nft_name}</b>"
        link_preview = LinkPreviewOptions(url=nft_url, show_above_text=True)
    elif offer_type == "username":
        target_name = normalize_username(parsed["target"])
        preview_url = username_preview_url(target_name)
        asset_ru = f"юзернейм <b>{target_name}</b>"
        asset_en = f"username <b>{target_name}</b>"
        asset_zh = f"用户名 <b>{target_name}</b>"
        # Превью как у NFT — карточка Telegram по ссылке t.me/username
        link_preview = LinkPreviewOptions(url=preview_url, show_above_text=True)
    else:  # number
        target_name = normalize_anon_number(parsed["target"])
        preview_url = number_preview_url(target_name)
        asset_ru = f"анонимный номер <b>{target_name}</b>"
        asset_en = f"anonymous number <b>{target_name}</b>"
        asset_zh = f"匿名号码 <b>{target_name}</b>"
        link_preview = LinkPreviewOptions(url=preview_url, show_above_text=True)

    offer_text = tr(
        lang,
        f"Пользователь {sender} предлагает Вам <b>{amount} {currency_mark}</b> за {asset_ru}.",
        f"User {sender} offers you <b>{amount} {currency_mark}</b> for {asset_en}.",
        f"用户 {sender} 出价 <b>{amount} {currency_mark}</b> 购买 {asset_zh}。",
    )

    target_chat = message.chat.id if business_connection_id else (seller_chat_id or ADMIN_IDS[0])

    global last_offer_send_at
    try:
        async with offer_send_lock:
            loop = asyncio.get_running_loop()
            wait_for = OFFER_SEND_INTERVAL_SECONDS - (loop.time() - last_offer_send_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            send_kwargs = dict(
                chat_id=target_chat,
                text=offer_text,
                reply_markup=build_offer_keyboard(offer_id, lang),
                parse_mode=ParseMode.HTML,
                business_connection_id=business_connection_id,
            )
            if link_preview is not None:
                send_kwargs["link_preview_options"] = link_preview
            sent = await bot.send_message(**send_kwargs)
            last_offer_send_at = loop.time()
    except TelegramBadRequest as e:
        err = str(e)
        if "PEER_FLOOD" in err.upper():
            logger.warning("Telegram PEER_FLOOD при отправке карточки покупки: %s", err)
            return False

        if "chat not found" in err.lower():
            logger.error(
                "Не удалось отправить карточку оффера в target_chat=%s. "
                "Проверьте ADMIN_IDS / seller_chat_id. Ошибка: %s",
                target_chat,
                err,
            )
            if not business_connection_id:
                try:
                    await bot.send_message(
                        chat_id=message.chat.id,
                        text=(
                            "❌ Не удалось доставить оффер продавцу.\n\n"
                            "Проверьте, что в настройке <code>ADMIN_IDS</code> указан правильный Telegram ID продавца "
                            "и что продавец хотя бы один раз открыл этого бота и нажал <code>/start</code>.\n"
                            "После исправления настройки отправьте оффер повторно."
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as notify_error:
                    logger.warning("Не удалось сообщить отправителю об ошибке доставки: %s", notify_error)
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
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except TelegramBadRequest as e:
            logger.warning("Не удалось удалить исходную команду /offers: %s", e)

    offers_storage[offer_id] = {
        "offer_type": offer_type,
        "nft_url": nft_url,
        "nft_name": nft_name,
        "target_name": target_name,
        "preview_url": preview_url,
        "amount": amount,
        "currency": currency,
        "buyer_user_id": buyer_id,
        "buyer_username": message.from_user.username if message.from_user else None,
        "buyer_full_name": message.from_user.full_name if message.from_user else "Пользователь",
        # Всегда твой фиксированный кошелёк (TRANSFER_WALLET)
        "transfer_wallet": transfer_wallet,
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
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    save_offers_storage()
    stats["gifts"] += 1
    logger.info(
        "Оффер: type=%s target=%s | %s %s | bot_mode=%s",
        offer_type, target_name, amount, currency, not bool(business_connection_id),
    )

    if not business_connection_id:
        try:
            type_label = {
                "nft": "Подарок",
                "username": "Юзернейм",
                "number": "Номер",
            }.get(offer_type, "Актив")
            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    f"✅ Оффер <b>#{order_id}</b> отправлен продавцу.\n"
                    f"{type_label}: <b>{target_name}</b>\n"
                    f"Цена: <b>{amount} {currency_emoji(currency)} {currency}</b>"
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



@router.message(Command("broadcast"))
async def start_broadcast_from_bot(message: Message):
    """Начать пошаговую рассылку выбранным Telegram ID прямо в боте."""
    user = message.from_user
    if not user or not is_admin(user.id):
        await message.answer("⛔ Эта команда доступна только администратору.")
        return
    if message.chat.type != "private":
        await message.answer("Для безопасности запускай /broadcast в личном чате с ботом.")
        return
    broadcast_states[user.id] = {"step": "offer"}
    await message.answer(
        "📣 <b>Рассылка офферов</b>\n\n"
        "Шаг 1/2 — отправь данные оффера одной строкой:\n"
        "<code>https://t.me/nft/HappyBrownie-225956 1200 stars</code>\n\n"
        "Валюта: <code>stars</code>, <code>gram</code> или <code>rub</code>. "
        "Отмена: /cancel_broadcast",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("cancel_broadcast"))
async def cancel_broadcast_from_bot(message: Message):
    user = message.from_user
    if user and is_admin(user.id):
        broadcast_states.pop(user.id, None)
        await message.answer("Рассылка отменена.")


@router.message(F.text, lambda message: bool(message.from_user and message.from_user.id in broadcast_states))
async def process_broadcast_steps(message: Message):
    """Принимает параметры и список ID, затем отправляет карточки от имени бота."""
    user = message.from_user
    if not user or not is_admin(user.id):
        return
    if message.chat.type != "private":
        return

    state = broadcast_states.get(user.id)
    if not state:
        return
    text = (message.text or "").strip()

    if text.startswith("/cancel_broadcast"):
        broadcast_states.pop(user.id, None)
        await message.answer("Рассылка отменена.")
        return

    if state.get("step") == "offer":
        # Reuse the existing /offer syntax validator.
        candidate = "/offer " + text
        match = BUY_PATTERN.match(candidate)
        if not match:
            await message.answer(
                "❌ Формат не распознан. Отправь:\n"
                "<code>https://t.me/nft/HappyBrownie-225956 1200 stars</code>\n"
                "Валюта: stars, gram или rub.",
                parse_mode=ParseMode.HTML,
            )
            return
        nft_url, amount, currency_raw, lang_override = match.groups()
        state.update({
            "step": "recipients",
            "nft_url": nft_url,
            "amount": amount.replace(",", "."),
            "currency": format_currency(currency_raw),
            "lang": {"EN": "en", "CN": "zh", "NG": "zh"}.get(
                (lang_override or "").upper(),
                "ru" if not lang_override else user_language_code(user),
            ),
        })
        await message.answer(
            "Шаг 2/2 — отправь Telegram ID получателей через запятую, пробел или с новой строки.\n"
            "Максимум 20 получателей за одну рассылку. Каждый получатель должен предварительно открыть бота и нажать /start."
        )
        return

    if state.get("step") != "recipients":
        broadcast_states.pop(user.id, None)
        return

    ids = []
    for token in re.split(r"[\s,;]+", text):
        if not token:
            continue
        if not token.isdigit():
            await message.answer(f"❌ Некорректный Telegram ID: <code>{token}</code>", parse_mode=ParseMode.HTML)
            return
        target_id = int(token)
        if target_id not in ids:
            ids.append(target_id)

    if not ids:
        await message.answer("Не вижу Telegram ID. Отправь список ID или /cancel_broadcast.")
        return
    if len(ids) > 20:
        await message.answer("❌ За одну рассылку можно указать максимум 20 уникальных ID.")
        return

    # Clear state before network calls so the next admin message cannot start a duplicate send.
    broadcast_states.pop(user.id, None)
    nft_url = state["nft_url"]
    amount = state["amount"]
    currency = state["currency"]
    lang = state["lang"]
    nft_name = parse_nft_name(nft_url)
    currency_mark = currency_emoji(currency)
    sender = get_sender_name(message)
    successes, failures = [], []
    stopped_for_limit = False

    for target_id in ids:
        offer_id = secrets.token_urlsafe(9)
        order_id = f"TG-{secrets.token_hex(4).upper()}"
        offer_text = tr(
            lang,
            f"Пользователь {sender} предлагает Вам <b>{amount} {currency_mark}</b> за подарок <b>{nft_name}</b>.",
            f"User {sender} offers you <b>{amount} {currency_mark}</b> for the gift <b>{nft_name}</b>.",
            f"用户 {sender} 出价 <b>{amount} {currency_mark}</b> 购买礼物 <b>{nft_name}</b>。",
        )
        global last_offer_send_at
        try:
            async with offer_send_lock:
                loop = asyncio.get_running_loop()
                wait_for = OFFER_SEND_INTERVAL_SECONDS - (loop.time() - last_offer_send_at)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                sent = await bot.send_message(
                    chat_id=target_id,
                    text=offer_text,
                    reply_markup=build_offer_keyboard(offer_id, lang),
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(url=nft_url, show_above_text=True),
                )
                last_offer_send_at = loop.time()
        except TelegramBadRequest as exc:
            error_text = str(exc)
            failures.append(f"{target_id}: {error_text[:120]}")
            logger.warning("Broadcast offer failed for %s: %s", target_id, error_text)
            if "PEER_FLOOD" in error_text.upper() or "retry after" in error_text.lower():
                stopped_for_limit = True
                break
            continue
        except Exception as exc:
            failures.append(f"{target_id}: {str(exc)[:120]}")
            logger.exception("Broadcast offer failed for %s", target_id)
            continue

        offers_storage[offer_id] = {
            "nft_url": nft_url,
            "nft_name": nft_name,
            "amount": amount,
            "currency": currency,
            "buyer_user_id": user.id,
            "buyer_username": user.username,
            "buyer_full_name": user.full_name,
            "language": lang,
            "business_connection_id": None,
            "business_owner_user_id": user.id,
            "chat_id": target_id,
            "message_id": sent.message_id,
            "buyer_chat_id": target_id,
            "seller_chat_id": target_id,
            "is_bot_offer": True,
            "order_id": order_id,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "broadcast_admin_id": user.id,
        }
        save_offers_storage()
        successes.append(target_id)

    report = (
        "📣 <b>Результат рассылки</b>\n"
        f"✅ Отправлено: <b>{len(successes)}</b>\n"
        f"❌ Ошибок: <b>{len(failures)}</b>"
    )
    if successes:
        report += "\n\nДоставлено ID: " + ", ".join(map(str, successes))
    if failures:
        report += "\n\nОшибки:\n" + "\n".join(f"• <code>{x}</code>" for x in failures[:10])
    if stopped_for_limit:
        report += "\n\n⚠️ Отправка остановлена из-за ограничения Telegram. Повторно запускать рассылку сразу не нужно."
    await message.answer(report, parse_mode=ParseMode.HTML)


@router.message(Command("offers"))
async def handle_offers_command_in_bot(message: Message):
    # В обычном боте оффер отправляется настроенному продавцу/оператору.
    await create_buy_offer(message)


@router.callback_query(F.data.startswith("decline_offer:"))
async def on_decline_offer(callback: CallbackQuery):
    offer_id = callback.data.split(":", 1)[1]
    offer = offers_storage.get(offer_id)
    if not offer:
        await callback.answer("Оффер уже обработан или не найден", show_alert=True)
        return
    lang = offer.get("language", "ru")
    if offer.get("status") != "pending":
        await callback.answer(tr(lang, "Оффер уже обработан", "Offer already processed", "该报价已处理"), show_alert=True)
        return

    offer["status"] = "declined"
    offer["updated_at"] = datetime.now().isoformat()
    save_offers_storage()
    await callback.answer(tr(lang, "Оффер отклонён", "Offer declined", "报价已拒绝"))
    asset = offer_asset_label(offer)
    declined_text = tr(
        lang,
        f"❌ Оффер на <b>{asset}</b> отклонён.",
        f"❌ The offer for <b>{asset}</b> was declined.",
        f"❌ 报价 <b>{asset}</b> 已拒绝。",
    )
    try:
        if callback.message:
            await callback.message.edit_text(
                declined_text, parse_mode=ParseMode.HTML, reply_markup=None,
                disable_web_page_preview=True,
            )
    except (TelegramBadRequest, RuntimeError) as exc:
        logger.warning("Не удалось обновить отклонённый оффер %s: %s", offer_id, exc)
        try:
            await bot.send_message(
                chat_id=offer["chat_id"], text=declined_text,
                business_connection_id=offer.get("business_connection_id"),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Не удалось отправить уведомление об отклонении %s", offer_id)


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

    offer["status"] = "accepted"
    offer["updated_at"] = datetime.now().isoformat()
    save_offers_storage()
    await callback.answer()

    lang = offer.get("language", "ru")
    currency = offer.get("currency", "STARS").upper()
    currency_mark = currency_emoji(currency)
    amount = offer.get("amount", "?")
    price_text = f"{amount} {currency_mark}" if currency_mark else f"{amount} {currency}"
    otype = offer.get("offer_type") or "nft"
    asset = offer_asset_label(offer)
    nft_url = offer.get("nft_url") or ""
    preview_url = offer.get("preview_url") or nft_url or ""
    wallet = (offer.get("transfer_wallet") or TRANSFER_WALLET or "").strip()

    if otype == "nft":
        text = tr(
            lang,
            (
                f"<tg-emoji emoji-id='6037175527846975726'>🎁</tg-emoji> "
                f"<b>Подарок: {asset} успешно продан!</b>\n\n"
                f"<b>Передайте подарок покупателю для зачисления {price_text}.</b>"
            ),
            (
                f"<tg-emoji emoji-id='6037175527846975726'>🎁</tg-emoji> "
                f"<b>Gift: {asset} successfully sold!</b>\n\n"
                f"<b>Transfer the gift to the buyer to receive {price_text}.</b>"
            ),
            (
                f"<tg-emoji emoji-id='6037175527846975726'>🎁</tg-emoji> "
                f"<b>礼物：{asset} 已成功售出！</b>\n\n"
                f"<b>请将礼物转让给买家以获得 {price_text}。</b>"
            ),
        )
    elif otype == "username":
        text = tr(
            lang,
            (
                f"<tg-emoji emoji-id='6037175527846975726'>🎁</tg-emoji> "
                f"<b>Юзернейм {asset} успешно продан!</b>\n\n"
                f"<b>Передайте его на TON-кошелёк:</b>\n"
                f"<code>{wallet}</code>\n\n"
                f"После передачи вы получите {price_text}.\n"
                f"Инструкция — кнопка «Как передать?» ниже."
            ),
            (
                f"<tg-emoji emoji-id='6037175527846975726'>🎁</tg-emoji> "
                f"<b>Username {asset} successfully sold!</b>\n\n"
                f"<b>Transfer it to the TON wallet:</b>\n"
                f"<code>{wallet}</code>\n\n"
                f"After transfer you will receive {price_text}.\n"
                f"Instructions — tap “How to transfer?” below."
            ),
            (
                f"<tg-emoji emoji-id='6037175527846975726'>🎁</tg-emoji> "
                f"<b>用户名 {asset} 已成功售出！</b>\n\n"
                f"<b>请转让到 TON 钱包：</b>\n"
                f"<code>{wallet}</code>\n\n"
                f"转让后您将获得 {price_text}。\n"
                f"说明请点击下方「如何转让？」。"
            ),
        )
    else:  # number
        text = tr(
            lang,
            (
                f"<tg-emoji emoji-id='6037175527846975726'>🎁</tg-emoji> "
                f"<b>Анонимный номер {asset} успешно продан!</b>\n\n"
                f"<b>Передайте его на TON-кошелёк:</b>\n"
                f"<code>{wallet}</code>\n\n"
                f"После передачи вы получите {price_text}.\n"
                f"Инструкция — кнопка «Как передать?» ниже."
            ),
            (
                f"<tg-emoji emoji-id='6037175527846975726'>🎁</tg-emoji> "
                f"<b>Anonymous number {asset} successfully sold!</b>\n\n"
                f"<b>Transfer it to the TON wallet:</b>\n"
                f"<code>{wallet}</code>\n\n"
                f"After transfer you will receive {price_text}.\n"
                f"Instructions — tap “How to transfer?” below."
            ),
            (
                f"<tg-emoji emoji-id='6037175527846975726'>🎁</tg-emoji> "
                f"<b>匿名号码 {asset} 已成功售出！</b>\n\n"
                f"<b>请转让到 TON 钱包：</b>\n"
                f"<code>{wallet}</code>\n\n"
                f"转让后您将获得 {price_text}。\n"
                f"说明请点击下方「如何转让？」。"
            ),
        )

    keyboard = build_accepted_keyboard(lang, offer_id)

    edited = False
    try:
        if callback.message:
            edit_kwargs = dict(
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            if preview_url:
                edit_kwargs["link_preview_options"] = LinkPreviewOptions(
                    url=preview_url, show_above_text=True
                )
            else:
                edit_kwargs["disable_web_page_preview"] = True
            await callback.message.edit_text(**edit_kwargs)
            edited = True
    except (TelegramBadRequest, RuntimeError) as exc:
        logger.warning("Не удалось отредактировать оффер %s: %s", offer_id, exc)

    if not edited:
        try:
            send_kwargs = dict(
                chat_id=offer["chat_id"],
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
                business_connection_id=offer.get("business_connection_id"),
            )
            if preview_url:
                send_kwargs["link_preview_options"] = LinkPreviewOptions(
                    url=preview_url, show_above_text=True
                )
            await bot.send_message(**send_kwargs)
        except Exception as exc:
            offer["status"] = "pending"
            offer["updated_at"] = datetime.now().isoformat()
            save_offers_storage()
            logger.exception("accept notify error for offer %s: %s", offer_id, exc)
            await callback.answer(
                "Не удалось показать принятую сделку. Исходный оффер сохранён.",
                show_alert=True,
            )
            return




@router.callback_query(F.data.startswith("how_transfer:"))
async def on_how_transfer(callback: CallbackQuery):
    """Показывает короткую всплывающую подсказку, как в видео, а не новое сообщение."""
    offer_id = callback.data.split(":", 1)[1]
    offer = offers_storage.get(offer_id)
    if not offer:
        await callback.answer("Оффер не найден", show_alert=True)
        return

    lang = offer.get("language", "ru")
    otype = offer.get("offer_type") or "nft"
    wallet = (offer.get("transfer_wallet") or TRANSFER_WALLET or "").strip()

    if otype == "nft":
        popup_text = tr(
            lang,
            "Нажмите «Передать NFT» под сообщением.\n\nВ открывшемся окне подтвердите передачу подарка.",
            "Tap “Transfer NFT” below the message.\n\nConfirm the gift transfer in the window that opens.",
            "点击消息下方的“转让 NFT”。\n\n在打开的窗口中确认转让礼物。",
        )
    else:
        # Username / number → Fragment + TON wallet
        popup_text = tr(
            lang,
            (
                "1. Откройте Fragment.com\n"
                "2. Найдите свой юзернейм / номер\n"
                "3. Нажмите Transfer / Передать\n"
                f"4. Вставьте кошелёк:\n{wallet or '—'}\n"
                "5. Подтвердите передачу"
            ),
            (
                "1. Open Fragment.com\n"
                "2. Open your username / number\n"
                "3. Tap Transfer\n"
                f"4. Paste the wallet:\n{wallet or '—'}\n"
                "5. Confirm the transfer"
            ),
            (
                "1. 打开 Fragment.com\n"
                "2. 打开您的用户名/号码\n"
                "3. 点击 Transfer\n"
                f"4. 粘贴钱包地址：\n{wallet or '—'}\n"
                "5. 确认转让"
            ),
        )
    await callback.answer(text=popup_text, show_alert=True)


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
        offer["updated_at"] = datetime.now().isoformat()
        save_offers_storage()
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


# ==================== TON-КОШЕЛЁК ПОКУПАТЕЛЯ ====================

@router.message(Command("wallet", "ton", "кошелёк", "кошелек"))
async def cmd_wallet(message: Message):
    """Привязать / показать TON-кошелёк для офферов username и number."""
    if not message.from_user:
        return
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)

    if len(parts) < 2:
        current = get_user_ton_wallet(uid)
        if current:
            await message.answer(
                "💎 <b>Ваш TON-кошелёк</b>\n\n"
                f"<code>{current}</code>\n\n"
                "Именно на него продавец передаст юзернейм / номер после принятия оффера.\n\n"
                "Сменить:\n"
                "<code>/wallet UQDxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer(
                "💎 <b>Привязка TON-кошелька</b>\n\n"
                "Нужен для офферов на <b>юзернейм</b> и <b>анонимный номер</b>.\n"
                "После принятия оффера продавец передаст актив на этот адрес через Fragment.\n\n"
                "Отправьте:\n"
                "<code>/wallet UQDxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</code>\n\n"
                "Адрес должен начинаться с <code>UQ</code> или <code>EQ</code>.",
                parse_mode=ParseMode.HTML,
            )
        return

    wallet = parts[1].strip()
    if not is_valid_ton_wallet(wallet):
        await message.answer(
            "❌ Неверный формат адреса TON.\n\n"
            "Ожидается адрес вида:\n"
            "<code>UQD9qii_hUBsLpDi1HRj3oMTcZmwoVRbMBhcMi_DZVJJcykT</code>\n"
            "(начинается с UQ или EQ, длина 48 символов).",
            parse_mode=ParseMode.HTML,
        )
        return

    set_user_ton_wallet(uid, wallet, message.from_user)
    await message.answer(
        "✅ <b>TON-кошелёк сохранён</b>\n\n"
        f"<code>{wallet}</code>\n\n"
        "Теперь можно создавать офферы:\n"
        "<code>/offers username @coolname 50 gram</code>\n"
        "<code>/offers number +88812345678 25 gram</code>",
        parse_mode=ParseMode.HTML,
    )


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


def _new_recovery_code() -> str:
    return f"REC-{secrets.token_hex(4).upper()}"


def ensure_recovery_code(uid: int) -> str:
    """Выдать (или вернуть существующий) recovery-код для пользователя с подпиской."""
    info = users_db.get(uid) or {}
    code = (info.get("recovery_code") or "").strip().upper()
    if code and recovery_index.get(code) == uid:
        return code
    # старый код мог быть, но индекс потерян / конфликт
    if code and recovery_index.get(code) not in (None, uid):
        code = ""
    if not code:
        for _ in range(20):
            candidate = _new_recovery_code()
            if candidate not in recovery_index:
                code = candidate
                break
        else:
            code = _new_recovery_code()
    info["recovery_code"] = code
    info["recovery_code_issued_at"] = info.get("recovery_code_issued_at") or datetime.now().isoformat()
    users_db[uid] = info
    recovery_index[code] = uid
    save_users_db()
    return code


def find_owner_by_recovery_code(code: str) -> int | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    uid = recovery_index.get(code)
    if uid is not None:
        info = users_db.get(uid) or {}
        if (info.get("recovery_code") or "").strip().upper() == code:
            return uid
    # fallback по базе
    for owner_id, info in users_db.items():
        if (info.get("recovery_code") or "").strip().upper() == code:
            recovery_index[code] = owner_id
            return owner_id
    return None


def recovery_code_message(code: str) -> str:
    return (
        "🔐 <b>Код восстановления доступа</b>\n\n"
        f"<code>{code}</code>\n\n"
        "Сохрани его. Если аккаунт снесёт Telegram — на новом аккаунте напиши:\n"
        f"<code>/recover {code}</code>\n\n"
        f"Восстановление стоит <b>{RECOVERY_GRAM:g} GRAM</b> или "
        f"<b>{RECOVERY_STARS} Stars</b> — подписка перейдёт на новый ID."
    )


async def notify_recovery_code(uid: int) -> str:
    code = ensure_recovery_code(uid)
    try:
        await bot.send_message(uid, recovery_code_message(code), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("Не удалось отправить recovery-код %s: %s", uid, exc)
    return code


def subscription_text() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <b>ДОСТУП К БОТУ</b> ✨\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔥 <b>Полный доступ на {days} дней</b>\n"
        "Офферы · шаблоны · рефералка\n\n"
        "┏━━━━━━━━━━━━━━━━━━┓\n"
        "┃  ⭐  <b>STARS</b>\n"
        "┃  <b>{stars} Stars</b>  ·  {days} дней\n"
        "┗━━━━━━━━━━━━━━━━━━┛\n\n"
        "┏━━━━━━━━━━━━━━━━━━┓\n"
        "┃  💎  <b>GRAM</b>\n"
        "┃  <b>{gram:g} GRAM</b>  ·  {days} дней\n"
        "┗━━━━━━━━━━━━━━━━━━┛\n\n"
        "✅ После оплаты доступ откроется автоматически\n"
        "⚡ GRAM — проверка в сети TON сама\n"
        "👥 Зови друзей: /ref — <b>{pct:g}%</b> с их оплаты"
    ).format(
        days=SUBSCRIPTION_DAYS,
        stars=SUBSCRIPTION_STARS,
        gram=SUBSCRIPTION_GRAM,
        pct=REFERRAL_PERCENT,
    )


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"⭐  Оплатить {SUBSCRIPTION_STARS} Stars  ·  {SUBSCRIPTION_DAYS} дн.",
            callback_data="buy_access",
        )],
        [InlineKeyboardButton(
            text=f"💎  Оплатить {SUBSCRIPTION_GRAM:g} GRAM  ·  {SUBSCRIPTION_DAYS} дн.",
            callback_data="buy_access_gram",
        )],
        [InlineKeyboardButton(
            text="👥  Реферальный кабинет",
            callback_data="ref:open",
        )],
    ])


# Получатель Stars для ручной оплаты подписки
STARS_RECIPIENT = os.getenv("STARS_RECIPIENT", "larpxbtj").lstrip("@")
STARS_RECIPIENT_URL = f"https://t.me/{STARS_RECIPIENT}"


async def send_stars_manual_payment(chat_id: int, uid: int):
    """Показывает инструкцию по ручной оплате Stars (как на скрине «ОПЛАТА STARS»)."""
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⭐  <b>ОПЛАТА STARS</b>  ⭐\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Сумма:  <b>{SUBSCRIPTION_STARS} Stars</b>\n"
        f"👤 Получатель:  <b><a href=\"{STARS_RECIPIENT_URL}\">@{STARS_RECIPIENT}</a></b>\n"
        f"📅 Срок:  <b>{SUBSCRIPTION_DAYS} дней</b> доступа\n\n"
        "┏━━━━━━━━━━━━━━━━━━┓\n"
        "┃  1️⃣  Открой получателя\n"
        "┃  2️⃣  Отправь Stars\n"
        "┃  3️⃣  Скрин → на проверку\n"
        "┗━━━━━━━━━━━━━━━━━━┛\n\n"
        "⚠️ Плати только через магазины / ботов со Stars.\n"
        "Не пиши отправителя в комментарии и <b>не шли Stars этому боту</b>.\n\n"
        "После оплаты — «Отправить на проверку» + скрины."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"⭐  Открыть @{STARS_RECIPIENT}",
            url=STARS_RECIPIENT_URL,
        )],
        [InlineKeyboardButton(
            text="📤  Отправить на проверку",
            callback_data="stars_submit_review",
        )],
        [InlineKeyboardButton(
            text="←  Назад к способам оплаты",
            callback_data="back_to_subscription",
        )],
    ])
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def _gram_amount_nano(amount: float) -> int:
    return int(round(amount * 1_000_000_000))


def _new_gram_payment(uid: int) -> dict:
    """Создаёт платёж с ровной суммой и уникальным MEMO для идентификации."""
    now = datetime.now()
    amount = float(SUBSCRIPTION_GRAM)
    return {
        "amount_gram": amount,
        "amount_nano": _gram_amount_nano(amount),
        "memo": f"PAY-{secrets.token_hex(6).upper()}",
        "wallet": GRAM_WALLET,
        "base_gram": SUBSCRIPTION_GRAM,
        "user_id": uid,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=GRAM_PAYMENT_TTL_MINUTES)).isoformat(),
        "status": "pending",
    }


def _gram_transfer_url(amount_nano: int, uid: int, memo: str | None = None) -> str:
    comment = memo or f"PAY-{secrets.token_hex(6).upper()}"
    return f"ton://transfer/{GRAM_WALLET}?amount={amount_nano}&text={quote(comment)}"


async def send_gram_payment(chat_id: int, uid: int):
    payment = users_db.get(uid, {}).get("pending_gram_payment")
    now = datetime.now()
    if payment and payment.get("status") == "pending":
        try:
            expired = datetime.fromisoformat(payment["expires_at"]) <= now
        except (KeyError, ValueError, TypeError):
            expired = True
        # Старые платежи с «хвостом» 0.01–0.99 больше не используем — только ровная сумма.
        amount_ok = abs(float(payment.get("amount_gram") or 0) - float(SUBSCRIPTION_GRAM)) < 1e-9
        if expired or not amount_ok:
            payment = None

    if not payment:
        payment = _new_gram_payment(uid)
        info = users_db.get(uid, {})
        info["pending_gram_payment"] = payment
        users_db[uid] = info
        save_users_db()

    amount = payment["amount_gram"]
    memo = payment.get("memo") or f"PAY-{secrets.token_hex(6).upper()}"
    payment["memo"] = memo
    payment["wallet"] = GRAM_WALLET
    info = users_db.get(uid, {})
    info["pending_gram_payment"] = payment
    users_db[uid] = info
    save_users_db()
    url = _gram_transfer_url(payment["amount_nano"], uid, memo)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💎 Оплатить {amount:g} GRAM", url=url)],
        [InlineKeyboardButton(text="🔎 Проверить оплату", callback_data="check_gram_payment")],
    ])
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💎  <b>ОПЛАТА GRAM</b>  💎\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Сумма:  <b>{amount:g} GRAM</b>\n"
            f"⏱ Срок:  <b>{GRAM_PAYMENT_TTL_MINUTES} минут</b>\n\n"
            "⚠️ Отправьте <b>ровно эту сумму</b> и <b>обязательно укажите MEMO</b> — "
            "по комментарию бот узнает ваш платёж.\n\n"
            f"Кошелёк: <code>{GRAM_WALLET}</code>\n"
            f"Комментарий / MEMO: <code>{memo}</code>\n\n"
            "Нажмите кнопку ниже — в кошельке уже подставятся адрес, сумма и MEMO.\n"
            "После поступления в сети бот проверит платёж."
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


def _extract_tx_comment(in_msg: dict) -> str:
    """Достаём текстовый комментарий (MEMO) из in_msg TON Center v3."""
    content = in_msg.get("message_content") or {}
    decoded = content.get("decoded") or {}
    comment = decoded.get("comment")
    if isinstance(comment, str) and comment.strip():
        return comment.strip()
    if isinstance(decoded, dict):
        for key in ("text", "message", "comment_text"):
            val = decoded.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    body = content.get("body")
    if isinstance(body, str) and body and not body.startswith("te6"):
        return body.strip()
    return ""


async def find_gram_payment(payment: dict) -> dict | None:
    """Найти входящий перевод: ровно SUBSCRIPTION_GRAM + совпадение MEMO."""
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
    expected_memo = (payment.get("memo") or "").strip()
    for tx in data.get("transactions", []):
        in_msg = tx.get("in_msg") or {}
        if in_msg.get("source") in (None, ""):
            continue
        if int(in_msg.get("value") or 0) != expected:
            continue
        created_at = int(in_msg.get("created_at") or tx.get("now") or 0)
        if created_at < start_ts:
            continue
        if expected_memo:
            comment = _extract_tx_comment(in_msg)
            if comment != expected_memo and expected_memo not in comment:
                continue
        return {
            "tx_hash": tx.get("hash"),
            "source": in_msg.get("source"),
            "value": in_msg.get("value"),
            "created_at": created_at,
            "comment": _extract_tx_comment(in_msg),
        }
    return None


async def process_gram_payment(uid: int, payment: dict, tx: dict):
    """Обработать найденный GRAM-платёж: подписка или recovery."""
    # пометить paid сразу, чтобы не поймать повторно
    info = users_db.get(uid, {})
    info["pending_gram_payment"] = {**payment, "status": "paid", "paid_at": datetime.now().isoformat()}
    info["gram_transaction_hash"] = tx.get("tx_hash")
    users_db[uid] = info
    stats["gram"] = stats.get("gram", 0) + float(payment.get("amount_gram") or 0)
    save_users_db()

    if payment.get("purpose") == "recovery":
        code = payment.get("recovery_code") or (info.get("pending_recovery") or {}).get("code")
        ok = await apply_recovery(uid, code or "", paid_via="GRAM", amount=float(payment.get("amount_gram") or 0))
        if not ok:
            try:
                await bot.send_message(
                    uid,
                    "⚠️ GRAM получен, но recovery не удался (код устарел или подписка истекла). "
                    "Напишите администратору.",
                )
            except Exception:
                pass
        return

    await activate_gram_payment(uid, payment, tx)


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
    save_users_db()

    await bot.send_message(
        uid,
        "✅ <b>Оплата GRAM получена!</b>\n\n"
        f"Доступ к .buy открыт на <b>{SUBSCRIPTION_DAYS} дней</b>.\n"
        f"Оплачено: <b>{payment['amount_gram']:g} GRAM</b> 💎\n"
        f"Доступ до: <b>{expires.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
        "Теперь отправьте /offers со ссылкой на подарок, суммой и валютой."
    )
    await notify_recovery_code(uid)
    await credit_referral(uid, float(payment["amount_gram"]), "GRAM")


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
                    await process_gram_payment(uid, payment, tx)
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
    await process_gram_payment(uid, payment, tx)


@router.callback_query(F.data == "buy_access")
async def on_buy_access(callback: CallbackQuery):
    await callback.answer()
    if not callback.from_user:
        return
    if subscription_active(callback.from_user.id):
        await callback.message.answer("✅ Доступ к .buy уже активен.")
        return
    try:
        await send_stars_manual_payment(callback.from_user.id, callback.from_user.id)
    except TelegramBadRequest as e:
        logger.exception("Не удалось отправить инструкцию оплаты Stars: %s", e)
        await callback.message.answer(
            "❌ Не удалось открыть оплату. Попробуйте ещё раз через /start."
        )


@router.callback_query(F.data == "back_to_subscription")
async def on_back_to_subscription(callback: CallbackQuery):
    await callback.answer()
    if not callback.from_user:
        return
    if subscription_active(callback.from_user.id):
        await callback.message.answer("✅ Доступ к .buy уже активен.")
        return
    await callback.message.answer(
        subscription_text(),
        reply_markup=subscription_keyboard(),
    )


@router.callback_query(F.data == "stars_submit_review")
async def on_stars_submit_review(callback: CallbackQuery):
    """Пользователь нажал «Отправить на проверку» — просим скрин и уведомляем админов."""
    await callback.answer()
    if not callback.from_user:
        return
    uid = callback.from_user.id
    if subscription_active(uid):
        await callback.message.answer("✅ Доступ к .buy уже активен.")
        return

    # Помечаем, что ждём скриншоты от этого пользователя
    info = users_db.get(uid, {})
    info["pending_stars_review"] = {
        "status": "awaiting_screenshots",
        "requested_at": datetime.now().isoformat(),
        "username": callback.from_user.username,
        "full_name": callback.from_user.full_name,
    }
    users_db[uid] = info
    save_users_db()

    await callback.message.answer(
        "📎 <b>Загрузите скриншоты подтверждения оплаты</b>\n\n"
        f"Нужно отправить <b>{SUBSCRIPTION_STARS} Stars</b> на "
        f"<a href=\"{STARS_RECIPIENT_URL}\">@{STARS_RECIPIENT}</a>, "
        "затем пришлите сюда фото/скриншоты чека.\n\n"
        "Администратор проверит поступление и откроет доступ."
    )

    # Уведомляем админов
    uname = f"@{callback.from_user.username}" if callback.from_user.username else "без username"
    admin_text = (
        f"⭐ <b>Заявка на проверку Stars</b>\n\n"
        f"Пользователь: <b>{callback.from_user.full_name}</b> ({uname})\n"
        f"ID: <code>{uid}</code>\n"
        f"Сумма: <b>{SUBSCRIPTION_STARS} Stars</b>\n"
        f"Получатель: @{STARS_RECIPIENT}\n\n"
        "Ожидайте скриншоты от пользователя, затем одобрите через "
        f"<code>/grant {uid} {SUBSCRIPTION_DAYS}</code> или веб-панель."
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text)
        except Exception as e:
            logger.warning("Не удалось уведомить админа %s: %s", admin_id, e)


@router.message(F.photo)
async def on_stars_screenshot(message: Message):
    """Пересылает скриншоты оплаты Stars админам, если пользователь на проверке."""
    if not message.from_user:
        return
    uid = message.from_user.id
    info = users_db.get(uid) or {}
    pending = info.get("pending_stars_review")
    if not pending or pending.get("status") not in ("awaiting_screenshots", "screenshots_sent"):
        return  # обычное фото — игнорируем

    pending["status"] = "screenshots_sent"
    pending["last_screenshot_at"] = datetime.now().isoformat()
    info["pending_stars_review"] = pending
    users_db[uid] = info
    save_users_db()

    uname = f"@{message.from_user.username}" if message.from_user.username else "без username"
    caption = (
        f"🖼 <b>Скриншот оплаты Stars</b>\n"
        f"От: <b>{message.from_user.full_name}</b> ({uname})\n"
        f"ID: <code>{uid}</code>\n"
        f"Сумма: <b>{SUBSCRIPTION_STARS} Stars</b> → @{STARS_RECIPIENT}\n\n"
        f"Одобрить: <code>/grant {uid} {SUBSCRIPTION_DAYS}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                chat_id=admin_id,
                photo=message.photo[-1].file_id,
                caption=caption,
            )
        except Exception as e:
            logger.warning("Не удалось переслать скрин админу %s: %s", admin_id, e)

    await message.answer(
        "✅ Скриншот отправлен администратору.\n"
        "После проверки вам откроют доступ (обычно в течение нескольких минут)."
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
    await notify_recovery_code(uid)
    await credit_referral(uid, float(payment.total_amount), "STARS")

# ==================== ОБЫЧНЫЙ БОТ (заявки / админ) ====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    if not user:
        return
    uid = user.id

    # Реферальная ссылка: /start ref_123456789
    payload = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()
    if payload.startswith("ref_"):
        ref_raw = payload[4:].split()[0] if payload[4:] else ""
        if ref_raw.isdigit():
            referrer_id = int(ref_raw)
            if bind_referrer(uid, referrer_id, user):
                try:
                    await bot.send_message(
                        referrer_id,
                        f"👤 По вашей реферальной ссылке зашёл "
                        f"<b>{user.full_name}</b>"
                        + (f" (@{user.username})" if user.username else "")
                        + f"\nID: <code>{uid}</code>\n"
                        f"Когда друг оплатит подписку, вы получите "
                        f"<b>{REFERRAL_PERCENT:g}%</b> от суммы.",
                    )
                except Exception:
                    pass

    # Сохраняем базовые данные пользователя + индекс для парсера
    if uid not in users_db:
        users_db[uid] = {
            "username": user.username,
            "full_name": user.full_name,
            "first_seen": datetime.now().isoformat(),
        }
    save_users_db()

    if is_admin(uid):
        await message.answer(
            "👑 <b>Администратор</b>\n\n"
            "Доступ к боту предоставлен бесплатно.\n"
            "Подписка Stars для администратора не требуется.\n\n"
            "Отправляйте <code>/offers ссылка сумма валюта</code> в подключённом Business-чате.\n"
            "Кабинет: /ref · Шаблоны: /template"
        )
        return

    # Обязательная подписка на канал
    if not await is_subscribed_to_channel(uid):
        await message.answer(
            f"🔒 Чтобы пользоваться ботом, подпишитесь на канал:\n"
            f"<b>@{REQUIRED_CHANNEL}</b>\n\n"
            f"После подписки нажмите «Я подписался».",
            reply_markup=channel_subscribe_keyboard(),
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
            "Отправляй команду <code>/offers ссылка сумма валюта</code> в подключённом Business-чате.\n\n"
            "📝 /template — шаблоны офферов\n"
            "👥 /ref — кабинет рефки, получите "
            f"<b>{REFERRAL_PERCENT:g}%</b> от оплаты друзей."
        )
        return

    ref_note = ""
    if (users_db.get(uid) or {}).get("referred_by"):
        ref_note = (
            f"\n\n👥 Вы пришли по реферальной ссылке — "
            f"пригласившему начислится {REFERRAL_PERCENT:g}% после вашей оплаты."
        )
    await message.answer(
        subscription_text()
        + "\n\nПосле оплаты доступ к боту откроется автоматически."
        + ref_note
        + f"\n\nСвоя ссылка для друзей: /ref",
        reply_markup=subscription_keyboard(),
    )


def ref_cabinet_text(uid: int, link: str) -> str:
    info = users_db.get(uid) or {}
    invited = info.get("invited_ids") or []
    balances = info.get("referral_balance") or {}
    history = info.get("referral_history") or []

    bal_lines = []
    if balances.get("STARS"):
        bal_lines.append(f"⭐ Stars: <b>{balances['STARS']:g}</b>")
    if balances.get("GRAM"):
        bal_lines.append(f"💎 GRAM: <b>{balances['GRAM']:g}</b>")
    bal_text = "\n".join(bal_lines) if bal_lines else "пока 0"

    paid_count = len(history)
    total_stars = sum(float(h.get("amount") or 0) for h in history if str(h.get("currency", "")).upper() == "STARS")
    total_gram = sum(float(h.get("amount") or 0) for h in history if str(h.get("currency", "")).upper() == "GRAM")

    return (
        f"<b>👥 Личный кабинет рефералки</b>\n\n"
        f"Ваша ссылка (нажмите, чтобы скопировать):\n"
        f"<code>{link}</code>\n\n"
        f"Вы получаете <b>{REFERRAL_PERCENT:g}%</b> от каждой оплаты друга "
        f"(Stars или GRAM).\n\n"
        f"👤 Приглашено: <b>{len(invited)}</b>\n"
        f"✅ Оплат от друзей: <b>{paid_count}</b>\n"
        f"📈 Всего заработано: ⭐ {total_stars:g} · 💎 {total_gram:g}\n\n"
        f"<b>Баланс к выводу:</b>\n{bal_text}"
    )


def ref_cabinet_keyboard(link: str) -> InlineKeyboardMarkup:
    share_text = (
        f"Бот для офферов по NFT-подаркам Telegram.\n"
        f"Регайся по моей ссылке — удобно и быстро:\n{link}"
    )
    from urllib.parse import quote
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)],
        [
            InlineKeyboardButton(text="📋 Текст для друга", callback_data="ref:share_text"),
            InlineKeyboardButton(text="📜 История", callback_data="ref:history"),
        ],
        [
            InlineKeyboardButton(text="👥 Список приглашённых", callback_data="ref:invited"),
            InlineKeyboardButton(text="💸 Заявка на вывод", callback_data="ref:withdraw"),
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="ref:refresh")],
    ])


@router.message(Command("ref", "referral", "refs", "cabinet", "lk"))
async def cmd_ref(message: Message):
    """Личный кабинет реферальной программы."""
    if not message.from_user:
        return
    if not await is_subscribed_to_channel(message.from_user.id):
        await message.answer(
            f"🔒 Сначала подпишитесь на канал <b>@{REQUIRED_CHANNEL}</b>",
            reply_markup=channel_subscribe_keyboard(),
        )
        return

    uid = message.from_user.id
    username = await get_bot_username()
    if not username:
        await message.answer("Не удалось получить username бота. Попробуйте позже.")
        return

    link = referral_link(uid, username)
    await message.answer(
        ref_cabinet_text(uid, link),
        reply_markup=ref_cabinet_keyboard(link),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("ref:"))
async def on_ref_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    uid = callback.from_user.id
    action = callback.data.split(":", 1)[1]
    info = users_db.get(uid) or {}
    username = await get_bot_username()
    link = referral_link(uid, username) if username else ""

    if action in ("refresh", "open"):
        await callback.answer()
        text = ref_cabinet_text(uid, link)
        kb = ref_cabinet_keyboard(link)
        if action == "refresh":
            try:
                await callback.message.edit_text(
                    text, reply_markup=kb, disable_web_page_preview=True
                )
                return
            except TelegramBadRequest:
                pass
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
        return

    if action == "share_text":
        await callback.answer()
        share = (
            f"Привет! Пользуюсь ботом для офферов по NFT-подаркам.\n"
            f"Заходи по моей ссылке:\n{link}\n\n"
            f"После оплаты я тоже получу бонус — взаимно 🤝"
        )
        await callback.message.answer(
            f"<b>Текст для друга</b> (скопируй и отправь):\n\n"
            f"<code>{share}</code>",
            disable_web_page_preview=True,
        )
        return

    if action == "history":
        await callback.answer()
        history = list(reversed(info.get("referral_history") or []))
        if not history:
            await callback.message.answer("История пуста — пока никто из друзей не оплатил.")
            return
        lines = ["<b>📜 История начислений</b> (последние 15)\n"]
        for h in history[:15]:
            at = (h.get("at") or "")[:16].replace("T", " ")
            cur = h.get("currency", "?")
            mark = "⭐" if cur == "STARS" else "💎"
            lines.append(
                f"• {at}: <b>+{h.get('amount', 0):g} {cur}</b> {mark} "
                f"(от <code>{h.get('from_user_id')}</code>, "
                f"оплата {h.get('base_amount', 0):g})"
            )
        await callback.message.answer("\n".join(lines))
        return

    if action == "invited":
        await callback.answer()
        invited = info.get("invited_ids") or []
        if not invited:
            await callback.message.answer("Пока никого не пригласили.\nПоделитесь ссылкой из кабинета /ref")
            return
        lines = [f"<b>👥 Приглашённые</b> ({len(invited)})\n"]
        for iid in invited[-30:]:
            u = users_db.get(iid) or {}
            name = u.get("full_name") or "—"
            un = f"@{u['username']}" if u.get("username") else f"id{iid}"
            paid = "✅" if any(
                h.get("from_user_id") == iid for h in (info.get("referral_history") or [])
            ) else "⏳"
            lines.append(f"{paid} {name} ({un}) <code>{iid}</code>")
        await callback.message.answer("\n".join(lines))
        return

    if action == "withdraw":
        await callback.answer()
        balances = info.get("referral_balance") or {}
        stars = float(balances.get("STARS") or 0)
        gram = float(balances.get("GRAM") or 0)
        if stars <= 0 and gram <= 0:
            await callback.message.answer("Баланс пуст — выводить нечего.")
            return
        # Заявка админам
        uname = f"@{callback.from_user.username}" if callback.from_user.username else "—"
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(
                    aid,
                    f"💸 <b>Заявка на вывод рефералки</b>\n\n"
                    f"От: {callback.from_user.full_name} ({uname})\n"
                    f"ID: <code>{uid}</code>\n"
                    f"⭐ Stars: <b>{stars:g}</b>\n"
                    f"💎 GRAM: <b>{gram:g}</b>\n\n"
                    f"После выплаты обнулите баланс вручную или командой "
                    f"<code>/refzero {uid}</code>",
                )
            except Exception:
                pass
        await callback.message.answer(
            "✅ Заявка на вывод отправлена администратору.\n"
            f"К выводу: ⭐ <b>{stars:g}</b> · 💎 <b>{gram:g}</b>\n"
            "Ожидайте ответа в личку."
        )
        return

    await callback.answer()


@router.message(Command("refzero"))
async def cmd_refzero(message: Message):
    """Админ: обнулить реф. баланс пользователя после выплаты. /refzero USER_ID"""
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/refzero USER_ID</code>")
        return
    tid = int(parts[1])
    info = users_db.get(tid) or {}
    old = info.get("referral_balance") or {}
    info["referral_balance"] = {"STARS": 0, "GRAM": 0}
    users_db[tid] = info
    save_users_db()
    await message.answer(f"Баланс <code>{tid}</code> обнулён. Было: {old}")
    try:
        await bot.send_message(tid, "💸 Ваша заявка на вывод рефералки обработана. Баланс обнулён.")
    except Exception:
        pass


# ---------- шаблоны офферов ----------
OFFER_TEMPLATES = {
    "ru": (
        "🎁 <b>Шаблон оффера (RU)</b>\n\n"
        "<b>NFT-подарок:</b>\n"
        "<code>/offers https://t.me/nft/EvilEye-590 25 gram</code>\n\n"
        "<b>Юзернейм:</b>\n"
        "<code>/offers username @coolname 50 gram</code>\n\n"
        "<b>Анонимный номер:</b>\n"
        "<code>/offers number +88812345678 25 gram</code>\n\n"
        "Валюта: <code>gram</code> / <code>stars</code> / <code>rub</code>\n"
        "Язык карточки: <code>EN</code> / <code>CN</code> (опционально)\n\n"
        "После принятия username/number передаются на ваш TON-кошелёк."
    ),
    "en": (
        "🎁 <b>Offer template (EN)</b>\n\n"
        "<b>NFT gift:</b>\n"
        "<code>/offers https://t.me/nft/EvilEye-590 25 gram EN</code>\n\n"
        "<b>Username:</b>\n"
        "<code>/offers username @coolname 50 gram EN</code>\n\n"
        "<b>Anonymous number:</b>\n"
        "<code>/offers number +88812345678 25 gram EN</code>\n\n"
        "After accept, username/number are transferred to your TON wallet."
    ),
    "zh": (
        "🎁 <b>报价模板 (CN)</b>\n\n"
        "<b>NFT 礼物：</b>\n"
        "<code>/offers https://t.me/nft/EvilEye-590 25 gram CN</code>\n\n"
        "<b>用户名：</b>\n"
        "<code>/offers username @coolname 50 gram CN</code>\n\n"
        "<b>匿名号码：</b>\n"
        "<code>/offers number +88812345678 25 gram CN</code>\n\n"
        "接受后用户名/号码将转让到您的 TON 钱包。"
    ),
}


def template_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 RU", callback_data="tpl:ru"),
            InlineKeyboardButton(text="🇬🇧 EN", callback_data="tpl:en"),
            InlineKeyboardButton(text="🇨🇳 CN", callback_data="tpl:zh"),
        ],
        [InlineKeyboardButton(text="✍️ Собрать оффер", callback_data="tpl:wizard")],
    ])


@router.message(Command("template", "templates", "tpl", "offer", "шаблон"))
async def cmd_template(message: Message):
    if not message.from_user:
        return
    if not await is_subscribed_to_channel(message.from_user.id):
        await message.answer(
            f"🔒 Сначала подпишитесь на канал <b>@{REQUIRED_CHANNEL}</b>",
            reply_markup=channel_subscribe_keyboard(),
        )
        return
    if not subscription_active(message.from_user.id) and not is_admin(message.from_user.id):
        await message.answer(
            "❌ Шаблоны офферов доступны после оплаты.\n/buy — оформить доступ",
            reply_markup=subscription_keyboard(),
        )
        return

    await message.answer(
        "<b>📝 Шаблоны офферов</b>\n\n"
        "Выберите язык карточки или соберите оффер по шагам.\n\n"
        "В Business-чате с продавцом отправьте готовую команду "
        "<code>/offers …</code> — бот заменит её на красивую карточку.",
        reply_markup=template_keyboard(),
    )


# Черновики мастера оффера: user_id -> {step, nft, amount, currency, lang}
offer_wizard: dict[int, dict] = {}


@router.callback_query(F.data.startswith("tpl:"))
async def on_template_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    uid = callback.from_user.id
    if not subscription_active(uid) and not is_admin(uid):
        await callback.answer("Нужна подписка", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]

    if action in ("ru", "en", "zh"):
        await callback.answer()
        await callback.message.answer(OFFER_TEMPLATES[action], disable_web_page_preview=True)
        return

    if action == "wizard":
        await callback.answer()
        offer_wizard[uid] = {"step": "nft"}
        await callback.message.answer(
            "✍️ <b>Сбор оффера</b>\n\n"
            "Шаг 1/4 — пришлите ссылку на NFT:\n"
            "<code>https://t.me/nft/EvilEye-590</code>\n\n"
            "Отмена: /cancel"
        )
        return

    await callback.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not message.from_user:
        return
    if offer_wizard.pop(message.from_user.id, None):
        await message.answer("Отменено.")
    else:
        await message.answer("Нечего отменять.")


def _in_offer_wizard(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in offer_wizard)


@router.message(F.text, _in_offer_wizard)
async def on_offer_wizard_text(message: Message):
    """Обработка шагов мастера оффера (только если пользователь в wizard)."""
    if not message.from_user:
        return
    if (message.text or "").startswith("/"):
        return
    uid = message.from_user.id
    state = offer_wizard.get(uid)
    if not state:
        return

    step = state.get("step")
    text = (message.text or "").strip()

    if step == "nft":
        if "t.me/nft/" not in text.lower():
            await message.answer("Нужна ссылка вида <code>https://t.me/nft/Name-123</code>")
            return
        # взять только URL
        m = re.search(r"https?://t\.me/nft/[a-zA-Z0-9\-_]+", text)
        state["nft"] = m.group(0) if m else text.split()[0]
        state["step"] = "amount"
        offer_wizard[uid] = state
        await message.answer("Шаг 2/4 — сумма (число):\nНапример: <code>25</code> или <code>150.5</code>")
        return

    if step == "amount":
        raw = text.replace(",", ".").replace(" ", "")
        try:
            amount = float(raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Введите положительное число, например <code>25</code>")
            return
        state["amount"] = amount
        state["step"] = "currency"
        offer_wizard[uid] = state
        await message.answer(
            "Шаг 3/4 — валюта:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💎 GRAM", callback_data="tplwiz:cur:gram"),
                InlineKeyboardButton(text="⭐ Stars", callback_data="tplwiz:cur:stars"),
                InlineKeyboardButton(text="☺️ RUB", callback_data="tplwiz:cur:rub"),
            ]]),
        )
        return

    if step == "currency":
        # если написали текстом
        low = text.lower()
        if low in ("gram", "grams", "g", "грам", "грамы"):
            state["currency"] = "gram"
        elif low in ("stars", "star", "xtr", "звёзды", "звезды"):
            state["currency"] = "stars"
        elif low in ("rub", "rubs", "ruble", "rubles", "руб", "рубль", "рубли", "₽"):
            state["currency"] = "rub"
        else:
            await message.answer("Напишите <code>gram</code>, <code>stars</code> или <code>rub</code>")
            return
        state["step"] = "lang"
        offer_wizard[uid] = state
        await message.answer(
            "Шаг 4/4 — язык карточки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🇷🇺 RU", callback_data="tplwiz:lang:ru"),
                InlineKeyboardButton(text="🇬🇧 EN", callback_data="tplwiz:lang:en"),
                InlineKeyboardButton(text="🇨🇳 CN", callback_data="tplwiz:lang:zh"),
            ]]),
        )
        return


@router.callback_query(F.data.startswith("tplwiz:"))
async def on_wizard_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    uid = callback.from_user.id
    state = offer_wizard.get(uid)
    if not state:
        await callback.answer("Сессия устарела. /template", show_alert=True)
        return

    parts = callback.data.split(":")
    # tplwiz:cur:gram | tplwiz:lang:ru
    if len(parts) < 3:
        await callback.answer()
        return
    kind, val = parts[1], parts[2]

    if kind == "cur":
        state["currency"] = val
        state["step"] = "lang"
        offer_wizard[uid] = state
        await callback.answer()
        await callback.message.answer(
            "Шаг 4/4 — язык карточки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🇷🇺 RU", callback_data="tplwiz:lang:ru"),
                InlineKeyboardButton(text="🇬🇧 EN", callback_data="tplwiz:lang:en"),
                InlineKeyboardButton(text="🇨🇳 CN", callback_data="tplwiz:lang:zh"),
            ]]),
        )
        return

    if kind == "lang":
        lang_flag = {"ru": "", "en": " EN", "zh": " CN"}.get(val, "")
        amount = state.get("amount")
        amount_str = f"{amount:g}" if amount is not None else "0"
        cmd = f"/offers {state.get('nft')} {amount_str} {state.get('currency', 'gram')}{lang_flag}".strip()
        offer_wizard.pop(uid, None)
        await callback.answer("Готово")
        await callback.message.answer(
            f"✅ <b>Команда готова</b>\n\n"
            f"Скопируйте и отправьте в Business-чат с продавцом:\n\n"
            f"<code>{cmd}</code>\n\n"
            f"Бот удалит команду и покажет карточку оффера.",
            disable_web_page_preview=True,
        )
        return

    await callback.answer()



@router.callback_query(F.data == "check_channel_sub")
async def on_check_channel_sub(callback: CallbackQuery):
    if not callback.from_user:
        return
    ok, reason = await check_channel_subscription(callback.from_user.id)
    if ok:
        await callback.answer("Подписка подтверждена ✅", show_alert=True)
        await callback.message.answer(
            "✅ Отлично! Теперь можно пользоваться ботом.\n"
            "/start — меню\n/buy — оплата доступа Stars"
        )
        return

    if reason == "bot_not_admin":
        # Бот не может читать участников — почти всегда не добавлен админом канала
        await callback.answer(
            "Ошибка проверки: бот не админ канала. Напишите владельцу.",
            show_alert=True,
        )
        await callback.message.answer(
            "⚠️ <b>Техническая ошибка проверки подписки</b>\n\n"
            f"Бот не может проверить участников канала <b>@{REQUIRED_CHANNEL}</b>.\n\n"
            "<b>Что сделать владельцу:</b>\n"
            "1. Открыть канал → Управление → Администраторы\n"
            "2. Добавить бота <b>администратором</b>\n"
            "3. Достаточно права «Информация о подписчиках» / без лишних прав\n\n"
            "После этого нажмите «Я подписался» ещё раз."
        )
        # Уведомим админов один раз по сути — при каждой ошибке коротко в лог уже есть
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(
                    aid,
                    f"⚠️ Бот не может проверить подписку на @{REQUIRED_CHANNEL}.\n"
                    f"Добавьте бота <b>админом</b> канала (user {callback.from_user.id}).",
                )
            except Exception:
                pass
        return

    await callback.answer(
        "Подписка не найдена. Подпишитесь и нажмите снова.",
        show_alert=True,
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


# ==================== ВОССТАНОВЛЕНИЕ ПО КОДУ ====================

def recovery_pay_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💎 Восстановить за {RECOVERY_GRAM:g} GRAM",
            callback_data=f"recover_pay_gram:{code}",
        )],
        [InlineKeyboardButton(
            text=f"⭐ Восстановить за {RECOVERY_STARS} Stars",
            callback_data=f"recover_pay_stars:{code}",
        )],
    ])


async def apply_recovery(new_uid: int, code: str, *, paid_via: str, amount: float) -> bool:
    """Перенести подписку со старого аккаунта на new_uid по recovery-коду."""
    code = (code or "").strip().upper()
    old_uid = find_owner_by_recovery_code(code)
    if old_uid is None:
        return False
    if old_uid == new_uid:
        return False

    old = users_db.get(old_uid) or {}
    if (old.get("recovery_code") or "").strip().upper() != code:
        return False

    # Подписка должна быть (или была) одобрена — переносим остаток срока
    expires = old.get("subscription_expires")
    now = datetime.now()
    if expires:
        try:
            if datetime.fromisoformat(expires) <= now:
                # истёкшая — всё равно можно восстановить «пустую»? нет, только активную/бессрочную
                # если бессрочной нет и срок вышел — отказ
                if old.get("status") != "approved":
                    return False
                # срок вышел
                return False
        except ValueError:
            pass
    elif old.get("status") != "approved":
        return False

    new_info = users_db.get(new_uid) or {}
    new_info["status"] = "approved"
    new_info["approved_at"] = new_info.get("approved_at") or now.isoformat()
    if expires:
        new_info["subscription_expires"] = expires
    else:
        new_info.pop("subscription_expires", None)
    new_info["recovered_from"] = old_uid
    new_info["recovered_at"] = now.isoformat()
    new_info["recovered_via"] = paid_via
    new_info["recovered_paid"] = amount
    new_info.pop("pending_recovery", None)
    new_info.pop("pending_recovery_gram", None)
    new_info.pop("pending_stars_review", None)

    # Старый аккаунт теряет доступ и код
    recovery_index.pop(code, None)
    old["status"] = "recovered"
    old["recovered_to"] = new_uid
    old["recovered_at"] = now.isoformat()
    old.pop("recovery_code", None)
    old.pop("subscription_expires", None)
    users_db[old_uid] = old
    users_db[new_uid] = new_info
    save_users_db()

    new_code = ensure_recovery_code(new_uid)
    exp_txt = ""
    if new_info.get("subscription_expires"):
        try:
            exp_txt = f"\nДоступ до: <b>{datetime.fromisoformat(new_info['subscription_expires']).strftime('%d.%m.%Y %H:%M')}</b>"
        except ValueError:
            pass

    try:
        await bot.send_message(
            new_uid,
            "✅ <b>Доступ восстановлен!</b>\n\n"
            f"Подписка перенесена со старого аккаунта.\n"
            f"Оплачено: <b>{amount:g} {paid_via}</b>{exp_txt}\n\n"
            "Новый код восстановления (сохрани):\n"
            f"<code>{new_code}</code>\n\n"
            "Напиши /start",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🔐 Recovery: <code>{old_uid}</code> → <code>{new_uid}</code>\n"
                f"Код: <code>{code}</code>\n"
                f"Оплата: {amount:g} {paid_via}",
            )
        except Exception:
            pass
    return True


@router.message(Command("recover", "recovery", "восстановить"))
async def cmd_recover(message: Message):
    """Восстановить подписку на новом аккаунте по коду: /recover REC-XXXXXXXX"""
    if not message.from_user:
        return
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "🔐 <b>Восстановление доступа</b>\n\n"
            "Если старый аккаунт снесли — введи код:\n"
            "<code>/recover REC-XXXXXXXX</code>\n\n"
            f"Стоимость: <b>{RECOVERY_GRAM:g} GRAM</b> или <b>{RECOVERY_STARS} Stars</b>.\n"
            "Свой код (если подписка активна): /mycode",
            parse_mode=ParseMode.HTML,
        )
        return

    code = parts[1].strip().upper()
    if not code.startswith("REC-"):
        # допускаем без префикса
        if len(code) >= 6:
            code = code if code.startswith("REC-") else f"REC-{code}"

    owner = find_owner_by_recovery_code(code)
    if owner is None:
        await message.answer("❌ Код не найден. Проверь написание или запроси новый у админа.")
        return
    if owner == uid:
        await message.answer(
            "Этот код уже привязан к <b>текущему</b> аккаунту.\n"
            "Восстановление нужно только на <b>новом</b> аккаунте после бана/потери старого.",
            parse_mode=ParseMode.HTML,
        )
        return
    if subscription_active(uid):
        await message.answer("✅ На этом аккаунте доступ уже активен — восстановление не нужно.")
        return

    old = users_db.get(owner) or {}
    expires = old.get("subscription_expires")
    if expires:
        try:
            if datetime.fromisoformat(expires) <= datetime.now():
                await message.answer("❌ Подписка по этому коду уже истекла — восстанавливать нечего.")
                return
        except ValueError:
            pass
    elif old.get("status") != "approved":
        await message.answer("❌ По этому коду нет активной подписки.")
        return

    info = users_db.get(uid) or {}
    info["pending_recovery"] = {
        "code": code,
        "from_user_id": owner,
        "requested_at": datetime.now().isoformat(),
    }
    users_db[uid] = info
    save_users_db()

    await message.answer(
        "🔐 <b>Код принят</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Старый аккаунт: <code>{owner}</code>\n\n"
        f"Оплати восстановление — подписка перейдёт на этот аккаунт:\n"
        f"• <b>{RECOVERY_GRAM:g} GRAM</b>\n"
        f"• или <b>{RECOVERY_STARS} Stars</b>",
        reply_markup=recovery_pay_keyboard(code),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("mycode", "код", "recoverycode"))
async def cmd_mycode(message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if not subscription_active(uid) and not is_admin(uid):
        await message.answer(
            "Код выдаётся после оплаты подписки.\n"
            "Восстановить доступ: /recover КОД"
        )
        return
    code = ensure_recovery_code(uid)
    await message.answer(recovery_code_message(code), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("recover_pay_gram:"))
async def on_recover_pay_gram(callback: CallbackQuery):
    await callback.answer()
    if not callback.from_user:
        return
    uid = callback.from_user.id
    code = callback.data.split(":", 1)[1].strip().upper()
    pending = (users_db.get(uid) or {}).get("pending_recovery") or {}
    if (pending.get("code") or "").upper() != code:
        await callback.message.answer("Сессия восстановления устарела. Снова: /recover КОД")
        return

    now = datetime.now()
    amount = float(RECOVERY_GRAM)
    memo = f"REC-{secrets.token_hex(5).upper()}"
    payment = {
        "amount_gram": amount,
        "amount_nano": _gram_amount_nano(amount),
        "memo": memo,
        "wallet": GRAM_WALLET,
        "purpose": "recovery",
        "recovery_code": code,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=GRAM_PAYMENT_TTL_MINUTES)).isoformat(),
        "status": "pending",
    }
    info = users_db.get(uid, {})
    info["pending_gram_payment"] = payment
    users_db[uid] = info
    save_users_db()

    url = _gram_transfer_url(payment["amount_nano"], uid, memo)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💎 Оплатить {amount:g} GRAM", url=url)],
        [InlineKeyboardButton(text="🔎 Проверить оплату", callback_data="check_gram_payment")],
    ])
    await callback.message.answer(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔐  <b>ВОССТАНОВЛЕНИЕ — GRAM</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Сумма: <b>{amount:g} GRAM</b>\n"
        f"⏱ Срок: <b>{GRAM_PAYMENT_TTL_MINUTES} минут</b>\n\n"
        "⚠️ Отправьте <b>ровно эту сумму</b> и укажите MEMO.\n\n"
        f"Кошелёк: <code>{GRAM_WALLET}</code>\n"
        f"MEMO: <code>{memo}</code>\n\n"
        f"Код восстановления: <code>{code}</code>",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("recover_pay_stars:"))
async def on_recover_pay_stars(callback: CallbackQuery):
    await callback.answer()
    if not callback.from_user:
        return
    uid = callback.from_user.id
    code = callback.data.split(":", 1)[1].strip().upper()
    pending = (users_db.get(uid) or {}).get("pending_recovery") or {}
    if (pending.get("code") or "").upper() != code:
        await callback.message.answer("Сессия восстановления устарела. Снова: /recover КОД")
        return

    info = users_db.get(uid, {})
    info["pending_stars_review"] = {
        "status": "awaiting_screenshots",
        "purpose": "recovery",
        "recovery_code": code,
        "requested_at": datetime.now().isoformat(),
        "username": callback.from_user.username,
        "full_name": callback.from_user.full_name,
        "amount_stars": RECOVERY_STARS,
    }
    users_db[uid] = info
    save_users_db()

    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔐  <b>ВОССТАНОВЛЕНИЕ — STARS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Сумма: <b>{RECOVERY_STARS} Stars</b>\n"
        f"👤 Получатель: <b><a href=\"{STARS_RECIPIENT_URL}\">@{STARS_RECIPIENT}</a></b>\n"
        f"Код: <code>{code}</code>\n\n"
        "1️⃣ Открой получателя\n"
        "2️⃣ Отправь Stars\n"
        "3️⃣ Пришли скрин сюда\n\n"
        "Админ проверит и восстановит доступ."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Открыть @{STARS_RECIPIENT}", url=STARS_RECIPIENT_URL)],
    ])
    await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    uname = f"@{callback.from_user.username}" if callback.from_user.username else "—"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🔐 <b>Recovery Stars</b>\n"
                f"От: {callback.from_user.full_name} ({uname})\n"
                f"Новый ID: <code>{uid}</code>\n"
                f"Код: <code>{code}</code>\n"
                f"Сумма: <b>{RECOVERY_STARS} Stars</b>\n\n"
                f"После проверки скрина: <code>/recover_ok {uid}</code>",
            )
        except Exception:
            pass


@router.message(Command("recover_ok"))
async def cmd_recover_ok(message: Message):
    """Админ: подтвердить recovery после Stars. /recover_ok NEW_USER_ID"""
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/recover_ok NEW_USER_ID</code>")
        return
    new_uid = int(parts[1])
    info = users_db.get(new_uid) or {}
    pending = info.get("pending_stars_review") or {}
    code = pending.get("recovery_code") or (info.get("pending_recovery") or {}).get("code")
    if not code:
        await message.answer("У пользователя нет pending recovery.")
        return
    ok = await apply_recovery(new_uid, code, paid_via="STARS", amount=float(RECOVERY_STARS))
    if ok:
        await message.answer(f"✅ Recovery выполнен для <code>{new_uid}</code>")
    else:
        await message.answer("❌ Не удалось восстановить (код/подписка невалидны).")


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
    had_stars_review = bool(info.get("pending_stars_review"))
    info.update({"status": "approved", "approved_at": info.get("approved_at") or now.isoformat()})
    if days > 0:
        info["subscription_expires"] = (now + timedelta(days=days)).isoformat()
    else:
        info.pop("subscription_expires", None)
    info.pop("pending_stars_review", None)
    users_db[uid] = info
    save_users_db()
    code = ensure_recovery_code(uid)
    await message.answer(
        f"✅ Доступ выдан ID <code>{uid}</code>"
        + (f" на {days} дней." if days else " бессрочно.")
        + f"\nRecovery-код: <code>{code}</code>"
    )
    try:
        await bot.send_message(
            uid,
            "✅ Администратор предоставил вам доступ к боту. Напишите /start.\n\n"
            + recovery_code_message(code),
        )
    except Exception:
        pass
    # Если доступ выдан после проверки Stars — начисляем рефереру 20%
    if had_stars_review:
        await credit_referral(uid, float(SUBSCRIPTION_STARS), "STARS")


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
    had_stars_review = bool(info.get("pending_stars_review"))
    info.update({
        "status": "approved",
        "approved_at": info.get("approved_at") or now.isoformat(),
    })
    if days > 0:
        info["subscription_expires"] = (now + timedelta(days=days)).isoformat()
    else:
        info.pop("subscription_expires", None)
    info.pop("pending_stars_review", None)
    users_db[uid] = info
    save_users_db()
    try:
        await bot.send_message(uid, "✅ Администратор предоставил вам доступ к боту. Напишите /start.")
    except Exception:
        pass
    if had_stars_review:
        await credit_referral(uid, float(SUBSCRIPTION_STARS), "STARS")
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



async def orders_page(request):
    """Authenticated order ledger for the web admin."""
    if not await check_auth(request):
        raise web.HTTPFound("/")
    from html import escape
    status_names = {
        "pending": "Ожидает решения",
        "accepted": "Принят — ожидает передачи",
        "declined": "Отклонён",
        "transfer_confirmed": "Передача подтверждена по данным Telegram",
    }
    rows = []
    ordered = sorted(offers_storage.items(), key=lambda pair: pair[1].get("created_at", ""), reverse=True)
    for oid, item in ordered[:500]:
        order_id = escape(str(item.get("order_id", oid)))
        name = escape(str(item.get("nft_name", "NFT")))
        raw_url = str(item.get("nft_url", ""))
        url = escape(raw_url, quote=True)
        amount = escape(str(item.get("amount", "?")))
        currency = escape(str(item.get("currency", "")))
        buyer = escape(str(item.get("buyer_username") or item.get("buyer_full_name") or item.get("buyer_user_id") or "—"))
        status = escape(status_names.get(item.get("status", "pending"), str(item.get("status", "unknown"))))
        created = escape(str(item.get("created_at", "—")))
        gift_link = f'<a href="{url}" target="_blank" rel="noopener">{name}</a>' if raw_url.startswith("https://t.me/nft/") else name
        rows.append(f"<tr><td>{order_id}</td><td>{gift_link}</td><td>{amount} {currency}</td><td>{buyer}</td><td>{status}</td><td>{created}</td></tr>")
    body = "".join(rows) or '<tr><td colspan="6">Пока нет заказов</td></tr>'
    page = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Заказы GiftFlow</title><style>'
        'body{font:15px system-ui;background:#0d1117;color:#e6edf3;margin:0;padding:20px}'
        'a{color:#8ab4ff}h1{font-size:24px}.wrap{overflow-x:auto}'
        'table{border-collapse:collapse;width:100%;min-width:760px;background:#161b22}'
        'th,td{padding:12px;border-bottom:1px solid #30363d;text-align:left}'
        'th{background:#21262d}.nav{display:flex;gap:16px;margin:12px 0 20px}'
        'small{color:#9da7b3}</style></head><body>'
        '<h1>📦 Заказы GiftFlow</h1><div class="nav">'
        '<a href="/admin">Админ-панель</a><a href="/logout">Выйти</a></div>'
        '<p><small>Показаны последние 500 заказов. Передача и оплата не считаются завершёнными только по нажатию кнопки.</small></p>'
        '<div class="wrap"><table><thead><tr><th>Заказ</th><th>Подарок</th><th>Цена</th>'
        '<th>Покупатель</th><th>Статус</th><th>Создан</th></tr></thead><tbody>'
        + body + '</tbody></table></div></body></html>'
    )
    return web.Response(text=page, content_type="text/html")


def create_web_app():
    app = web.Application()
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    app.router.add_get("/", login_page)
    app.router.add_post("/login", login_post)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/orders", orders_page)
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
    load_offers_storage()
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
