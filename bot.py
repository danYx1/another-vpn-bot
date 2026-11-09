import asyncio
import html
import logging
import os
import time
import traceback
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import keys_source
import storage

load_dotenv(Path(__file__).with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
STARS_PRICE = int(os.getenv("STARS_PRICE", "50"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ISSUE_KEY = os.getenv("ISSUE_KEY", "").strip()
MIN_WITHDRAW = 1000
SOURCE_URLS = tuple(
    url.strip()
    for url in os.getenv("KEY_SOURCE_URLS", "").split(",")
    if url.strip()
) or keys_source.DEFAULT_SOURCES

PLANS = {
    "1m": {"title": "1 месяц", "price": STARS_PRICE, "days": 30},
}

router = Router()
pool: keys_source.KeyPool | None = None
_sub_cache = {"at": 0.0, "ok": False}
QR_PATH = Path(__file__).with_name("qr.png")


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Купить VPN — {STARS_PRICE} Stars", callback_data="buy")
    kb.button(text="Пробный доступ — бесплатно", callback_data="trial")
    kb.button(text="Мой ключ", callback_data="mykey")
    kb.button(text="Обновить серверы", callback_data="refresh_keys")
    kb.button(text="О боте", callback_data="about")
    kb.adjust(1)
    return kb.as_markup()


def plans_menu():
    kb = InlineKeyboardBuilder()
    for pid, plan in PLANS.items():
        kb.button(
            text=f"{plan['title']} — {plan['price']} Stars",
            callback_data=f"plan:{pid}",
        )
    kb.button(text="Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def connect_hint() -> str:
    return (
        "<b>Как подключиться</b>\n"
        "1. Установи Happ или Hiddify.\n"
        "2. Скопируй ссылку или ключ выше.\n"
        "3. Ссылку-подписку добавляй через «Добавить подписку», "
        "обычный ключ — из буфера обмена.\n"
        "4. Если сервер не подключается, попробуй следующий ключ."
    )


def key_text(record: dict) -> str:
    unlimited = storage.is_unlimited(record)
    left = storage.days_left(record)
    expires = storage.parse_dt(record.get("expires_at"))
    date = expires.strftime("%d.%m.%Y") if expires else "—"

    keys = record.get("keys")
    if not isinstance(keys, list) or not keys:
        keys = [{"url": record["key"], "name": record.get("key_title", "VPN")}]

    blocks = []
    for i, item in enumerate(keys[:5], 1):
        url = str(item.get("url") or "")
        if url.startswith("http"):
            title = "Подписка (ссылка)"
        else:
            name = html.escape(str(item.get("name") or f"Сервер {i}"))
            ms = item.get("ms")
            title = f"Сервер {i} — {name}" + (f" ({ms} мс)" if ms else "")
        blocks.append(f"{title}\n<code>{html.escape(url)}</code>")
        if sum(len(block) for block in blocks) > 3200:
            break

    if unlimited:
        validity = "Действует: бессрочно"
        status = ""
    else:
        validity = f"Действует до: {date} (осталось {left} дн.)"
        status = "" if left else "\n\n<b>Срок действия истёк — продли подписку.</b>"
    return (
        "Твой доступ к VPN:\n\n"
        + "\n\n".join(blocks)
        + f"\n\nТариф: {record['plan_title']}\n"
        f"{validity}{status}\n\n"
        f"{connect_hint()}"
    )


async def subscription_ok(url: str) -> bool:
    now = time.monotonic()
    if now - _sub_cache["at"] > 600:
        _sub_cache["ok"] = await keys_source.subscription_alive(url, PROXY_URL)
        _sub_cache["at"] = now
        logging.info("Подписка ISSUE_KEY доступна: %s", _sub_cache["ok"])
    return _sub_cache["ok"]


async def current_servers(limit: int = 5) -> list[dict]:
    if ISSUE_KEY:
        if ISSUE_KEY.startswith(("http://", "https://")):
            if not await subscription_ok(ISSUE_KEY):
                logging.warning("ISSUE_KEY не отвечает, всё равно выдаю его")
            name = "Подписка"
        else:
            name = "VPN"
        return [{"url": ISSUE_KEY, "name": name}]
    if pool:
        return await pool.get_servers(limit)
    return []


async def issue_key(user_id: int, pid: str) -> dict | None:
    plan = PLANS[pid].copy()
    plan["id"] = pid
    servers = await current_servers(5)
    if not servers:
        return None
    logging.info(
        "Ключи для %s: %s",
        user_id,
        ", ".join(
            f"{s.get('name', 'VPN')}"
            + (f" {s['ms']}ms" if s.get("ms") else "")
            for s in servers
        ),
    )
    return storage.extend_subscription(
        user_id=user_id,
        plan_id=pid,
        plan_title=plan["title"],
        days=plan["days"],
        keys=servers,
    )


async def trial_access(user_id: int) -> tuple[str, dict | None]:
    """Выдаёт бессрочный пробный доступ один раз на пользователя."""
    record = storage.get_user(user_id)
    if record:
        return "exists", record
    servers = await current_servers(5)
    if not servers:
        return "error", None
    record = storage.extend_subscription(
        user_id=user_id,
        plan_id="trial",
        plan_title="Пробный доступ",
        days=0,
        keys=servers,
    )
    logging.info("Пробный доступ выдан пользователю %s", user_id)
    return "ok", record


async def send_home(message: Message) -> None:
    await message.answer(
        "<b>VPN-бот</b>\n\n"
        "Быстрый доступ к интернету без ограничений.\n"
        f"Ключ выдаётся сразу после оплаты — {STARS_PRICE} Stars за 30 дней.\n"
        "Есть бесплатный бессрочный пробный доступ.\n"
        "Работает в Happ и Hiddify.\n\n"
        "Выбери действие:",
        reply_markup=main_menu(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await send_home(message)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды</b>\n"
        "/start — меню\n"
        "/buy — купить VPN\n"
        "/trial — бесплатный пробный доступ\n"
        "/mykey — мой ключ\n"
        "/help — эта справка"
    )


async def send_key(
    message: Message,
    record: dict,
    old: Message | None = None,
    prefix: str = "",
) -> None:
    """Отправляет ключ вместе с QR-кодом для быстрого подключения."""
    text = prefix + key_text(record)
    if old is not None:
        try:
            await old.delete()
        except Exception:
            pass
    if QR_PATH.exists():
        photo = FSInputFile(QR_PATH)
        caption = text if len(text) <= 1024 else "QR-код для быстрого подключения"
        try:
            await message.answer_photo(
                photo, caption=caption, reply_markup=main_menu()
            )
        except Exception:
            logging.exception("Не удалось отправить QR-код")
        else:
            if len(text) > 1024:
                await message.answer(text)
            return
    await message.answer(text, reply_markup=main_menu())


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer("Выбери тариф:", reply_markup=plans_menu())


@router.message(Command("trial"))
async def cmd_trial(message: Message) -> None:
    status, record = await trial_access(message.from_user.id)
    if status == "error":
        await message.answer(
            "Ключи сейчас обновляются, попробуй через минуту.",
            reply_markup=main_menu(),
        )
        return
    prefix = "У тебя уже есть доступ.\n\n" if status == "exists" else ""
    await send_key(message, record, prefix=prefix)


@router.message(Command("mykey"))
async def cmd_mykey(message: Message) -> None:
    record = storage.get_user(message.from_user.id)
    if not record:
        await message.answer(
            "У тебя пока нет ключа.\nНажми «Купить VPN», чтобы получить его.",
            reply_markup=main_menu(),
        )
        return
    await send_key(message, record)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    summary = storage.payments_summary()
    await message.answer(
        "<b>Статистика</b>\n"
        f"Платежей: {summary['count']}\n"
        f"Покупателей: {summary['buyers']}\n"
        f"Получено: {summary['stars']} Stars"
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    summary = storage.payments_summary()
    await message.answer(
        "<b>Твои Stars</b>\n"
        f"Получено: {summary['stars']} Stars\n"
        f"Платежей: {summary['count']}\n\n"
        "<b>Как вывести</b>\n"
        "Stars зачисляются на аккаунт владельца бота автоматически.\n"
        f"Вывод: Fragment (fragment.com) → Connect wallet → Withdraw.\n"
        f"Минимум {MIN_WITHDRAW} Stars, холд 21 день, выплата в TON."
    )


@router.callback_query(F.data == "home")
async def cb_home(cq: CallbackQuery) -> None:
    await cq.message.edit_text(
        "<b>VPN-бот</b>\n\n"
        "Быстрый доступ к интернету без ограничений.\n"
        f"Ключ выдаётся сразу после оплаты — {STARS_PRICE} Stars за 30 дней.\n"
        "Есть бесплатный бессрочный пробный доступ.\n"
        "Работает в Happ и Hiddify.\n\n"
        "Выбери действие:",
        reply_markup=main_menu(),
    )
    await cq.answer()


@router.callback_query(F.data == "buy")
async def cb_buy(cq: CallbackQuery) -> None:
    await cq.message.edit_text("Выбери тариф:", reply_markup=plans_menu())
    await cq.answer()


@router.callback_query(F.data == "trial")
async def cb_trial(cq: CallbackQuery) -> None:
    status, record = await trial_access(cq.from_user.id)
    if status == "error":
        await cq.answer("Ключи обновляются, попробуй позже", show_alert=True)
        return
    prefix = "У тебя уже есть доступ.\n\n" if status == "exists" else ""
    await send_key(cq.message, record, old=cq.message, prefix=prefix)
    await cq.answer()


@router.callback_query(F.data == "about")
async def cb_about(cq: CallbackQuery) -> None:
    text = (
        "<b>О боте</b>\n\n"
        f"VPN на {PLANS['1m']['days']} дней за {STARS_PRICE} Stars.\n"
        "Оплата через Telegram Stars.\n"
        "Ключ выдаётся автоматически и поддерживает Happ и Hiddify.\n"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data="home")
    await cq.message.edit_text(text, reply_markup=kb.as_markup())
    await cq.answer()


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(cq: CallbackQuery) -> None:
    pid = cq.data.split(":", 1)[1]
    plan = PLANS.get(pid)
    if not plan:
        await cq.answer("Тариф не найден", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text=f"Оплатить {STARS_PRICE} Stars", callback_data=f"pay:{pid}")
    kb.button(text="Назад", callback_data="buy")
    kb.adjust(1)

    text = (
        f"<b>{plan['title']}</b>\n"
        f"Срок: {plan['days']} дней\n"
        f"Стоимость: {STARS_PRICE} Stars\n\n"
        "Оплата через Telegram Stars. Ключ выдаётся сразу после платежа."
    )
    await cq.message.edit_text(text, reply_markup=kb.as_markup())
    await cq.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cq: CallbackQuery) -> None:
    pid = cq.data.split(":", 1)[1]
    plan = PLANS.get(pid)
    if not plan:
        await cq.answer("Тариф не найден", show_alert=True)
        return

    payload = f"vpn:{cq.from_user.id}:{pid}:{uuid.uuid4().hex}"
    try:
        await cq.bot.send_invoice(
            chat_id=cq.message.chat.id,
            title=f"VPN на {plan['days']} дней",
            description=(
                f"Доступ к VPN на {plan['days']} дней. "
                "Ключ выдаётся сразу после оплаты."
            ),
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(label=f"VPN {plan['title']}", amount=STARS_PRICE)
            ],
        )
    except Exception:
        logging.exception("Не удалось выставить счёт")
        await cq.answer("Не удалось выставить счёт, попробуй позже", show_alert=True)
        return
    await cq.answer("Счёт выставлен")


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    parts = (query.invoice_payload or "").split(":")
    valid = len(parts) == 4 and parts[0] == "vpn" and parts[2] in PLANS
    if valid:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Неизвестный тариф")


@router.message(F.successful_payment)
async def on_paid(message: Message) -> None:
    payment = message.successful_payment
    parts = (payment.invoice_payload or "").split(":")
    pid = parts[2] if len(parts) >= 3 else "1m"
    if pid not in PLANS:
        await message.answer("Оплата получена, но тариф не распознан. Напиши в поддержку.")
        return

    placeholder = await message.answer("Оплата получена. Выдаю ключ...")
    record = await issue_key(message.from_user.id, pid)
    if record is None:
        await placeholder.edit_text(
            "Оплата получена, но не удалось получить ключ. "
            "Напиши в поддержку, ключ выдадим вручную."
        )
        return

    storage.add_payment(
        user_id=message.from_user.id,
        plan_id=pid,
        amount=payment.total_amount,
        currency=payment.currency,
        charge_id=payment.telegram_payment_charge_id or "",
        payload=payment.invoice_payload or "",
    )
    if ADMIN_ID:
        username = message.from_user.username
        buyer = f"@{username}" if username else message.from_user.full_name
        try:
            await message.bot.send_message(
                ADMIN_ID,
                f"Новая оплата: {payment.total_amount} Stars\n"
                f"Покупатель: {buyer} (id {message.from_user.id})",
            )
        except Exception:
            logging.exception("Не удалось уведомить админа")
    await send_key(message, record, old=placeholder)


@router.callback_query(F.data == "mykey")
async def cb_mykey(cq: CallbackQuery) -> None:
    record = storage.get_user(cq.from_user.id)
    if not record:
        await cq.answer("Ключа пока нет", show_alert=True)
        return
    await send_key(cq.message, record, old=cq.message)
    await cq.answer()


@router.callback_query(F.data == "refresh_keys")
async def cb_refresh_keys(cq: CallbackQuery) -> None:
    record = storage.get_user(cq.from_user.id)
    if not record:
        await cq.answer("Сначала купи VPN", show_alert=True)
        return
    await cq.answer("Обновляю ключи")
    servers = await current_servers(5)
    if not servers:
        await cq.message.answer(
            "Ключи сейчас обновляются, попробуй через минуту.",
            reply_markup=main_menu(),
        )
        return
    record = storage.set_keys(cq.from_user.id, servers)
    await send_key(cq.message, record, old=cq.message)


async def bg_refresh() -> None:
    while True:
        await asyncio.sleep(keys_source.CACHE_TTL)
        if pool is None:
            continue
        try:
            await pool.refresh()
        except Exception:
            logging.exception("Не удалось обновить пул ключей")


async def start_health_server():
    """HTTP-порт для хостингов (Koyeb/Render), чтобы сервис считался живым."""
    port = int(os.getenv("PORT", "0") or 0)
    if not port:
        return None
    from aiohttp import web

    async def health(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Health-сервер слушает порт %s", port)
    return runner


async def main() -> None:
    global pool

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    if not ISSUE_KEY:
        pool = keys_source.KeyPool(SOURCE_URLS, proxy=PROXY_URL)
        pool.start_refresh()
    refresh_task = asyncio.create_task(bg_refresh())
    health_runner = await start_health_server()

    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    bot = Bot(
        BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    dp.include_router(router)

    me = await bot.get_me()
    logging.info(
        "Бот @%s запущен. Stars: %s. ISSUE_KEY: %s",
        me.username,
        STARS_PRICE,
        bool(ISSUE_KEY),
    )

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        refresh_task.cancel()
        if health_runner is not None:
            await health_runner.cleanup()


async def run_forever() -> None:
    """Держит бота запущенным: перезапускает при падении или недоступном прокси."""
    while True:
        try:
            await main()
            return
        except Exception:
            logging.exception("Бот упал, перезапуск через 15 секунд")
            await asyncio.sleep(15)


if __name__ == "__main__":
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        print("Остановлено пользователем.")
    except Exception:
        traceback.print_exc()
        print("\nБот упал. Проверь BOT_TOKEN и PROXY_URL")
        if os.name == "nt":
            input("Нажми Enter, чтобы закрыть...")
