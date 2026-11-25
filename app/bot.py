"""Telegram-бот «Успевайка»: витрина (Mini App), приём заказов, статусы, стоп-лист.

Запуск: python bot.py  (настройки в файле .env рядом, см. .env.example)

Всё, что приходит из витрины, пересчитывается на сервере: цены, остатки и сумма
берутся из menu.json. Клиентские значения total/price игнорируются.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo)
from aiohttp import web
from dotenv import load_dotenv

import admin

BASE = Path(__file__).resolve().parent


def read_version() -> str:
    """Версия из файла VERSION рядом с bot.py.

    Нужна, чтобы сервер сам понимал, есть ли обновление: без версии
    автообновление сравнивать нечего и каждый запуск тянул бы архив заново.
    """
    try:
        return (BASE / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


VERSION = read_version()
load_dotenv(BASE / ".env")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        logging.warning("Переменная %s=%r не число — использую %s", name, raw, default)
        return default


def _path_env(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()          # https-адрес, по которому открывается витрина
ADMIN_CHAT_ID = _int_env("ADMIN_CHAT_ID", 0)              # чат, куда падают заказы
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()  # вход в /admin из браузера
PORT = _int_env("PORT", 8080)
# На каком адресе слушать. По умолчанию все интерфейсы; за обратным прокси на
# том же сервере безопаснее 127.0.0.1 — тогда порт не виден снаружи.
BIND_HOST = os.getenv("BIND_HOST", "").strip() or "0.0.0.0"

MENU_PATH = _path_env("MENU_PATH", BASE / "menu.json")
DB_PATH = _path_env("DB_PATH", BASE / "orders.db")
WEBAPP_DIR = BASE / "webapp"

# Прокси для запросов к Telegram. aiogram/aiohttp не читают HTTPS_PROXY из
# окружения сами (в отличие от curl), поэтому адрес нужно передать явно.
# Без этого на машинах, где Telegram доступен только через прокси, polling
# висит до таймаута и бот молчит.
TELEGRAM_PROXY = (os.getenv("TELEGRAM_PROXY", "").strip()
                  or os.getenv("HTTPS_PROXY", "").strip()
                  or os.getenv("https_proxy", "").strip())

MAX_ITEMS = 50          # больше позиций в одном заказе не берём
ORDER_COOLDOWN = 5      # секунд между заказами одного пользователя

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("uspevaika")

STATUSES = {
    "new": "🆕 Новый",
    "accepted": "✅ Принят",
    "assembled": "📦 Собран",
    "onway": "🚗 В пути",
    "delivered": "🏁 Доставлен",
    "cancelled": "❌ Отменён",
}
FLOW = ["accepted", "assembled", "onway", "delivered"]
CLOSED = {"delivered", "cancelled"}
CLIENT_TEXT = {
    "accepted": "Ваш заказ №{id} принят. Готовим к отправке 💛",
    "assembled": "Заказ №{id} собран и ждёт курьера.",
    "onway": "Заказ №{id} в пути. Курьер позвонит перед приездом.",
    "delivered": "Заказ №{id} доставлен. Приятного аппетита! Будем рады отзыву 💛",
    "cancelled": "Заказ №{id} отменён. Если это ошибка — напишите нам или позвоните {phone}.",
}


# ---------- меню ----------
class MenuError(RuntimeError):
    """menu.json отсутствует или содержит битый JSON."""


class MenuStore:
    """menu.json: чтение + атомарная запись под общим asyncio.Lock."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as e:
            raise MenuError(f"не читается {self.path}: {e}") from e
        except json.JSONDecodeError as e:
            raise MenuError(f"битый JSON в {self.path}: {e}") from e

    def _write(self, menu: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(menu, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # замена атомарна: читатели видят либо старое, либо новое

    async def mutate(self, fn: Callable[[dict], tuple[Any, bool]]) -> Any:
        """fn меняет меню на месте и возвращает (результат, записывать_ли)."""
        async with self.lock:
            menu = self.read()
            result, changed = fn(menu)
            if changed:
                self._write(menu)
            return result


store = MenuStore(MENU_PATH)


def rub(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₽"


def find_item(menu: dict, query: str):
    """Возвращает позицию меню, список совпадений или None."""
    q = query.strip().lower()
    for it in menu["items"]:
        if it["id"] == q:
            return it
    hits = [it for it in menu["items"] if q in it["title"].lower()]
    return hits[0] if len(hits) == 1 else (hits or None)


# ---------- база ----------
@contextlib.contextmanager
def db() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created INTEGER, user_id INTEGER, username TEXT,
            name TEXT, phone TEXT, address TEXT, zone TEXT, date TEXT, slot TEXT, payment TEXT, comment TEXT,
            items TEXT, goods INTEGER, delivery INTEGER, total INTEGER,
            status TEXT, admin_msg_id INTEGER)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        # базы, созданные до появления районов доставки, колонку zone не имеют
        cols = {row["name"] for row in con.execute("PRAGMA table_info(orders)")}
        if "zone" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN zone TEXT")
        # source: откуда заказ — telegram, web или phone (принят менеджером).
        # Нужен, чтобы отличать заказы, оформленные вручную, и строить отчёты.
        if "source" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN source TEXT")
        # Менеджеры, добавленные из чата. Список в .env тоже действует, но его
        # нельзя изменить на ходу: файл читается только при старте.
        con.execute("""CREATE TABLE IF NOT EXISTS admins(
            user_id INTEGER PRIMARY KEY,
            name TEXT, username TEXT,
            added_by INTEGER, added_at INTEGER)""")


# ---------- кто открыл витрину ----------
# Telegram передаёт initData в витрину, и по нему можно узнать id клиента. Но на
# iOS initData нередко не появляется ни в SDK, ни в хэше адреса — тогда заказ
# уходил бы как «с сайта». Поэтому бот сам кладёт id в ссылку на витрину и
# подписывает его: Telegram лишь открывает готовый адрес, а подпись проверяется
# на сервере, так что id нельзя подменить руками.

def _webapp_secret() -> bytes:
    material = (BOT_TOKEN or ADMIN_PASSWORD or "").encode()
    return hashlib.sha256(b"uspevaika-webapp-user|" + material).digest() if material else b""


def user_token(user_id: int) -> str:
    """Подписанный токен пользователя для ссылки на витрину; '' — если подписывать нечем."""
    secret = _webapp_secret()
    if not secret or not user_id:
        return ""
    payload = str(int(user_id))
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:20]
    return f"{payload}.{sig}"


def verify_user_token(token: str | None) -> int:
    """Проверяет токен из ссылки. Возвращает id или 0, если токен битый/поддельный."""
    if not token or "." not in token:
        return 0
    payload, sig = token.rsplit(".", 1)
    if not payload.isdigit():
        return 0
    expected = user_token(int(payload))
    if not expected or not hmac.compare_digest(expected.encode(), token.encode()):
        return 0
    return int(payload)


def webapp_url(user_id: int | None = None) -> str:
    """Адрес витрины; для клиента — с его подписанным токеном."""
    token = user_token(user_id or 0)
    if not token:
        return WEBAPP_URL
    sep = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{sep}u={token}"


ADMIN_TOKEN_TTL = 30 * 86400      # месяц: дольше держать доступ по ссылке не стоит


def _admin_sig(payload: str) -> str:
    secret = _webapp_secret()
    if not secret:
        return ""
    return hmac.new(secret, b"admin|" + payload.encode(), hashlib.sha256).hexdigest()[:24]


def admin_token(user_id: int) -> str:
    """Подписанный токен доступа в админку. Отдельная подпись и свой срок:
    ссылка на админку даёт больше прав, чем ссылка на витрину."""
    if not _webapp_secret() or not user_id:
        return ""
    payload = f"{int(user_id)}.{int(time.time()) + ADMIN_TOKEN_TTL}"
    return f"{payload}.{_admin_sig(payload)}"


def verify_admin_token(token: str | None) -> int:
    """Проверяет токен админки: подпись и срок. 0 — если не годится."""
    if not token or token.count(".") != 2:
        return 0
    payload, sig = token.rsplit(".", 1)
    expected = _admin_sig(payload)
    # compare_digest не работает с не-ASCII строками: кириллица в подделанном
    # токене обрушила бы обработчик (500 вместо отказа). Сравниваем байты.
    if not expected or not hmac.compare_digest(expected.encode(), sig.encode()):
        return 0
    uid_text, _, exp_text = payload.partition(".")
    if not uid_text.isdigit() or not exp_text.isdigit() or int(exp_text) < time.time():
        return 0
    return int(uid_text)


def admin_webapp_url(user_id: int) -> str:
    """Адрес админки с токеном — для кнопки в главном меню."""
    base = WEBAPP_URL.rstrip("/")
    if not base:
        return ""
    token = admin_token(user_id)
    return f"{base}/admin?ut={token}" if token else f"{base}/admin"


def admin_order_url(order_id: int, user_id: int) -> str:
    """Адрес панели, сразу открывающий нужный заказ.

    Токен обязателен: ссылку открывают в браузере, где Telegram не передаёт
    initData, а пароль может быть не задан. Без токена панель не понимает, кто
    пришёл, и просит пароль, которого у менеджера может не быть. Токен тот же,
    что у кнопки «🛠 Админка»: подписан ключом бота, привязан к менеджеру,
    живёт 30 дней.
    """
    base = WEBAPP_URL.rstrip("/")
    if not base:
        return ""
    token = admin_token(user_id)
    url = f"{base}/admin?order={int(order_id)}"
    return f"{url}&ut={token}" if token else url


# ---------- клавиатуры ----------
def main_kb(user_id: int | None = None) -> ReplyKeyboardMarkup:
    rows = []
    if WEBAPP_URL:
        rows.append([KeyboardButton(text="🛒 Меню и заказ",
                                    web_app=WebAppInfo(url=webapp_url(user_id)))])
    rows.append([KeyboardButton(text="📋 Мои заказы"), KeyboardButton(text="🚚 Доставка и оплата")])
    rows.append([KeyboardButton(text="📞 Связаться")])
    # Кнопку админки видит только менеджер (id из ADMIN_IDS). Ссылка несёт его
    # подписанный токен: на iOS Telegram не передаёт витрине initData, и без
    # токена админка не пустила бы даже своего.
    if user_id and is_admin(user_id):
        rows.append([KeyboardButton(text="👔 Менеджер"), KeyboardButton(text="📜 История")])
        if WEBAPP_URL:
            rows.append([KeyboardButton(text="🛠 Админка",
                                        web_app=WebAppInfo(url=admin_webapp_url(user_id)))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def admin_kb(order_id: int, status: str) -> InlineKeyboardMarkup | None:
    if status in CLOSED:
        return None
    passed = FLOW.index(status) if status in FLOW else -1
    nxt = [s for s in FLOW if FLOW.index(s) > passed]
    row = [InlineKeyboardButton(text=STATUSES[s], callback_data=f"st:{order_id}:{s}") for s in nxt[:2]]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="✏️ Изменить состав", callback_data=f"ed:{order_id}:0")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"st:{order_id}:cancelled")]])


# ---------- заказ: разбор и проверка ----------
def parse_order(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("type") != "order":
        return None
    return data


def clip(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def wanted_items(data: dict) -> dict[str, int]:
    """id -> суммарное количество; дубли склеиваются, мусор отбрасывается."""
    out: dict[str, int] = {}
    raw = data.get("items")
    if not isinstance(raw, list):
        return out
    for pair in raw[:MAX_ITEMS]:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        iid = clip(pair[0], 64)
        try:
            qty = int(pair[1])
        except (TypeError, ValueError):
            continue
        if not iid or qty <= 0:
            continue
        out[iid] = min(out.get(iid, 0) + qty, 999)
    return out


WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def allowed_dates(settings: dict, today: date | None = None) -> list[str]:
    """Даты, которые можно выбрать: окно вперёд и дни недели из delivery_days.

    Если расписание пустое — разрешаем все дни окна (как раньше).
    """
    today = today or date.today()
    ahead = max(1, min(int(settings.get("delivery_days_ahead", 5) or 5), 30))
    days = [d.strip().lower() for d in settings.get("delivery_days") or []]
    out = []
    for i in range(0, ahead + 1):
        d = today + timedelta(days=i)
        if days and WEEKDAYS_RU[d.weekday()] not in days:
            continue
        out.append(d.isoformat())
    return out or [(today + timedelta(days=i)).isoformat() for i in range(0, ahead + 1)]


_REPEATED = re.compile(r"(\d)\1{9}")          # 9999999999 и подобное


def phone_problem(raw: Any) -> str | None:
    """Проверка телефона по российским правилам. None — телефон годен.

    Раньше хватало любых десяти цифр, поэтому проходили и «1234567890», и
    десять одинаковых. Такой номер нельзя набрать, а заказ уже ушёл курьеру.
    """
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    # +7 923 363-82-08 → 79233638208; 89233638208 → 79233638208
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        return "Укажите телефон в формате +7 900 000-00-00."
    # десять одинаковых цифр в самом номере — «9999999999», «7777777777»
    if _REPEATED.fullmatch(digits[1:]):
        return "Похоже на ошибку в телефоне — проверьте номер."
    # код оператора/региона: три цифры после семёрки, начинается с 3, 4, 8 или 9
    if digits[1] not in "3489":
        return "Проверьте код города или оператора в телефоне."
    return None


def validate_contact(data: dict) -> str | None:
    if not clip(data.get("name"), 60):
        return "Укажите имя."
    problem = phone_problem(data.get("phone"))
    if problem:
        return problem
    if len(clip(data.get("address"), 200)) < 5:
        return "Укажите адрес доставки."
    return None


def _norm(value: Any) -> str:
    """Сравнение вариантов, набранных разными тире/пробелами: «12:00–14:00» == «12:00-14:00»."""
    text = clip(value, 100).lower()
    for ch in ("‐", "‑", "‒", "–", "—", "―", "−"):
        text = text.replace(ch, "-")
    return " ".join(text.split())


def _pick(value: Any, options: list) -> str | None:
    """Возвращает вариант из меню, совпадающий с присланным, в исходном написании."""
    want = _norm(value)
    for opt in options or []:
        if _norm(opt) == want:
            return str(opt)
    return None


def validate_delivery(data: dict, settings: dict) -> str | None:
    dates = allowed_dates(settings)
    picked = clip(data.get("date"), 10)
    if picked not in dates:
        if settings.get("delivery_days"):
            return ("В этот день доставки нет. Выберите из списка: "
                    + ", ".join(settings["delivery_days"]).lower() + ".")
        return "Выберите день доставки из списка — откройте меню и оформите заказ заново."
    if _pick(data.get("slot"), settings.get("delivery_slots")) is None:
        return "Выберите интервал доставки из списка."
    if _pick(data.get("payment"), settings.get("payment_methods")) is None:
        return "Выберите способ оплаты из списка."
    return None


def delivery_zone(settings: dict, zone_title: str | None, goods: int) -> dict | None:
    """Подбирает зону доставки по названию из витрины; None — если зон нет."""
    zones = settings.get("delivery_zones") or []
    if not zones:
        return None
    want = _norm(zone_title)
    for z in zones:
        if _norm(z.get("title")) == want:
            return z
    return zones[0]


def delivery_cost(settings: dict, goods: int, zone: dict | None) -> int:
    """Стоимость доставки: 0 при достижении порога бесплатной доставки зоны."""
    if zone is None:
        free_from = int(settings.get("free_delivery_from", 0) or 0)
        return 0 if goods >= free_from else int(settings.get("delivery_price", 0) or 0)
    free_from = int(zone.get("free_from", settings.get("free_delivery_from", 0)) or 0)
    return 0 if goods >= free_from else int(zone.get("price", settings.get("delivery_price", 0)) or 0)


def order_lines_raw(order: sqlite3.Row) -> list[dict]:
    """Достаёт позиции заказа; понимает и старый формат [(title, qty, price)]."""
    try:
        raw = json.loads(order["items"])
    except (TypeError, ValueError):
        return []
    out: list[dict] = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, dict):
            out.append({"id": it.get("id"), "title": str(it.get("title", "")),
                        "price": int(it.get("price", 0)), "qty": int(it.get("qty", 0))})
        elif isinstance(it, (list, tuple)) and len(it) >= 3:
            out.append({"id": None, "title": str(it[0]), "price": int(it[2]), "qty": int(it[1])})
    return out


def order_lines(items: list[dict]) -> str:
    return "\n".join(f"• {html.escape(it['title'])} × {it['qty']} — {rub(it['price'] * it['qty'])}"
                     for it in items)


def human_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return html.escape(str(iso))
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    return f"{days[d.weekday()]}, {d.day} {months[d.month - 1]}"


def contact_label(o: sqlite3.Row) -> str:
    """Как показать, откуда пришёл заказ: @ник, Telegram id или «сайт».

    Заказы с сайта пишутся с user_id = 0 и без username — для них «id 0» было бы
    бессмысленным, а менеджеру важно понимать, что клиент пришёл не из Telegram
    и написать ему в Telegram не получится.
    """
    keys = o.keys()
    username = o["username"] if "username" in keys else None
    user_id = o["user_id"] if "user_id" in keys else 0
    if username:
        return f"@{username}"
    if user_id:
        return f"Telegram id {user_id}"
    return "с сайта, без Telegram"


TELEGRAM_TEXT_LIMIT = 4096


def admin_text(o: sqlite3.Row) -> str:
    """Карточка заказа для чата менеджеров.

    Telegram не примет сообщение длиннее 4096 символов и вернёт ошибку — заказ
    при этом сохранён, но менеджер его не увидит. Поэтому шапка, состав, сумма и
    комментарий собираются с учётом лимита: при нехватке места сокращается
    список позиций (и об этом говорится), а не теряется весь заказ.
    """
    user = contact_label(o)
    address = html.escape(o["address"] or "")
    zone = html.escape(o["zone"] or "") if "zone" in o.keys() else ""
    head = [
        f"<b>Заказ №{o['id']}</b> · {STATUSES[o['status']]}",
        f"{html.escape(o['name'] or '')}, {html.escape(o['phone'] or '')} ({user})",
        f"📍 {address}" + (f" · {zone}" if zone else ""),
        f"🕒 {human_date(o['date'])}, {html.escape(o['slot'] or '')}",
        f"💳 {html.escape(o['payment'] or '')}",
    ]
    foot = [
        f"Доставка: {rub(o['delivery']) if o['delivery'] else 'бесплатно'}",
        f"<b>Итого: {rub(o['total'])}</b>",
    ]
    comment = f"💬 {html.escape(o['comment'])}" if o["comment"] else ""

    text = "\n".join(head + [""] + [order_lines(order_lines_raw(o))] + foot
                     + ([comment] if comment else []))
    if len(text) <= TELEGRAM_TEXT_LIMIT:
        return text

    # Не влезло — сокращаем состав и комментарий
    lines = order_lines_raw(o)
    for keep_comment in (200, 120, 60, 0):
        com = f"💬 {html.escape(o['comment'][:keep_comment])}" if (comment and keep_comment) else ""
        for limit in (len(lines), 30, 20, 10, 5, 1):
            shown = lines[:limit]
            body = "\n".join(
                f"• {html.escape(it['title'][:80])} × {it['qty']} — {rub(it['price'] * it['qty'])}"
                for it in shown)
            hidden = len(lines) - len(shown)
            if hidden:
                body += f"\n…и ещё {hidden} поз. — полный состав в админке"
            parts = head + ["", body] + foot + ([com] if com else [])
            text = "\n".join(parts)
            if len(text) <= TELEGRAM_TEXT_LIMIT:
                return text
    return text[:TELEGRAM_TEXT_LIMIT]      # совсем край: гарантированно влезаем


# ---------- бот ----------
dp = Dispatcher()

_last_order: dict[int, float] = {}


def is_admin(user_id: int) -> bool:
    """Менеджер: из .env или добавленный из чата.

    Список из .env — «старшие»: их нельзя убрать через бота, чтобы случайно не
    остаться без доступа к управлению.
    """
    if user_id in ADMIN_IDS:
        return True
    try:
        with db() as con:
            row = con.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def admin_ids_all() -> list[tuple[int, str, str | None, int | None]]:
    """Все менеджеры: (id, имя, ник, откуда) — для списка в чате."""
    out: list[tuple[int, str, str | None, int | None]] = []
    seen: set[int] = set()
    try:
        with db() as con:
            for r in con.execute("SELECT user_id, name, username, added_at FROM admins "
                                 "ORDER BY added_at"):
                out.append((r["user_id"], r["name"] or "", r["username"], r["added_at"]))
                seen.add(r["user_id"])
    except sqlite3.Error:
        pass
    for uid in sorted(ADMIN_IDS):
        if uid not in seen:
            out.append((uid, "", None, None))
    return out


def add_admin(user_id: int, name: str = "", username: str | None = None,
              added_by: int | None = None) -> None:
    with db() as con:
        con.execute("INSERT OR REPLACE INTO admins(user_id,name,username,added_by,added_at) "
                    "VALUES(?,?,?,?,?)",
                    (user_id, clip(name, 60), clip(username, 64) or None,
                     added_by, int(time.time())))


def remove_admin(user_id: int) -> bool:
    """Убирает менеджера из базы. Тех, кто задан в .env, убрать нельзя."""
    if user_id in ADMIN_IDS:
        return False
    with db() as con:
        cur = con.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        return cur.rowcount > 0


def _cooled_down(user_id: int) -> bool:
    return time.monotonic() - _last_order.get(user_id, 0.0) >= ORDER_COOLDOWN


def _mark_order(user_id: int) -> None:
    now = time.monotonic()
    _last_order[user_id] = now
    if len(_last_order) > 1000:      # не даём словарю расти бесконечно
        cutoff = now - ORDER_COOLDOWN
        for uid in [u for u, t in _last_order.items() if t < cutoff]:
            _last_order.pop(uid, None)


@dp.message(CommandStart())
async def start(m: Message):
    try:
        s = store.read()["settings"]
    except MenuError as e:
        log.error("меню недоступно: %s", e)
        await m.answer("Меню временно недоступно, попробуйте через минуту.")
        return
    # Кнопка со своей ссылкой у каждого чата: заказ придёт с Telegram id клиента
    # даже на iOS, где Telegram не передаёт витрине initData. Клавиатура
    # отправляется при каждом /start — если она была прислана ботом, её нужно
    # перезаписать, иначе в ней останется ссылка без id.
    text = (
        "Здравствуйте! Это «Успевайка» — домашние полуфабрикаты шоковой заморозки 💛\n"
        "<i>Готовим мы. Живёте вы.</i>\n\n"
        "Нажмите <b>«🛒 Меню и заказ»</b> внизу: там все блюда с ценами, весом и способом приготовления. "
        f"Доставка по {html.escape(s['city'])}у, заказ от {rub(s['min_order'])}."
    )
    if not WEBAPP_URL:
        text += "\n\n⚠️ WEBAPP_URL не задан — кнопка меню появится после настройки."
    await m.answer(text, reply_markup=main_kb(m.from_user.id))


@dp.message(Command("id"))
async def chat_id(m: Message):
    await m.answer(f"chat id: <code>{m.chat.id}</code>\nваш user id: <code>{m.from_user.id}</code>")


@dp.message(F.text == "🚚 Доставка и оплата")
async def delivery_info(m: Message):
    try:
        s = store.read()["settings"]
    except MenuError as e:
        log.error("меню недоступно: %s", e)
        return await m.answer("Информация временно недоступна, попробуйте через минуту.")
    zones = s.get("delivery_zones") or []
    if zones:
        zone_lines = "\n".join(
            f"• {html.escape(z['title'])} — бесплатно от {rub(int(z['free_from']))}, "
            f"иначе {rub(int(z['price']))}" for z in zones)
        delivery_block = f"<b>Районы и стоимость</b>\n{zone_lines}"
    else:
        delivery_block = (f"Стоимость: {rub(s['delivery_price'])}, "
                          f"бесплатно от {rub(s['free_delivery_from'])}")
    days = s.get("delivery_days") or []
    await m.answer(
        f"<b>Доставка по {html.escape(s['city'])}у</b>\n"
        f"Минимальный заказ: {rub(s['min_order'])}\n"
        f"{delivery_block}\n"
        f"Интервалы: {', '.join(s['delivery_slots'])}\n"
        + (f"Дни доставки: {', '.join(days).lower()}\n" if days else "")
        + f"\n<b>Оплата</b>: {', '.join(s['payment_methods']).lower()}.\n\n"
        "Продукция шоковой заморозки: храните в морозилке при −18°, повторно не замораживайте."
    )


@dp.message(F.text == "👔 Менеджер")
async def manager_button(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("<b>Панель менеджера</b>\n\nВыберите раздел.",
                   reply_markup=admin_menu_kb())


@dp.message(F.text == "📜 История")
async def history_button(m: Message):
    if not is_admin(m.from_user.id):
        return
    await history_cmd(m, CommandObject(prefix="/", command="history", args=""))


@dp.message(F.text == "📞 Связаться")
async def contact(m: Message):
    try:
        s = store.read()["settings"]
    except MenuError:
        s = {"phone": "—"}
    await m.answer(f"Телефон: {s['phone']}\nИли напишите нам в личные сообщения канала @uspevayka_krsk.")


@dp.message(F.text == "📋 Мои заказы")
async def my_orders(m: Message):
    with db() as con:
        rows = con.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5",
                           (m.from_user.id,)).fetchall()
    if not rows:
        await m.answer("Заказов пока нет. Откройте «🛒 Меню и заказ» 💛")
        return
    out = [f"<b>№{r['id']}</b> · {human_date(r['date'])}, {html.escape(r['slot'] or '')} · "
           f"{rub(r['total'])} · {STATUSES.get(r['status'], r['status'])}" for r in rows]
    await m.answer("Ваши последние заказы:\n\n" + "\n".join(out))


class OrderError(Exception):
    """Заказ не принят: текст ошибки показываем клиенту как есть."""

    def __init__(self, message: str, code: str = "REJECTED"):
        super().__init__(message)
        self.code = code


async def place_order(data: dict | None, *, user_id: int, username: str | None,
                      source: str = "telegram", bot: Bot | None = None
                      ) -> tuple[int, sqlite3.Row, list[dict], list[str]]:
    """Единый путь приёма заказа: остатки, проверки, запись в БД, уведомление админа.

    Используется и Telegram-витриной (`web_app_data`), и веб-формой сайта.
    Все цены, остатки и сумма пересчитываются здесь по menu.json.

    Возвращает (номер заказа, строка из БД, состав заказа, предупреждения) или
    бросает OrderError с понятным клиенту текстом.
    """
    if data is None:
        raise OrderError("Не получилось прочитать заказ. Откройте меню и попробуйте ещё раз.", "BAD_PAYLOAD")

    wanted = wanted_items(data)
    if not wanted:
        raise OrderError("Корзина пуста. Выберите блюда в меню 💛", "EMPTY_CART")

    try:
        menu = store.read()
    except MenuError as e:
        log.error("меню недоступно: %s", e)
        raise OrderError("Меню временно недоступно. Попробуйте оформить заказ через минуту.",
                         "MENU_UNAVAILABLE") from e

    s = menu["settings"]
    problem = validate_contact(data) or validate_delivery(data, s)
    if problem:
        raise OrderError(problem, "VALIDATION")
    # сохраняем значения в точном написании из menu.json, а не как прислал клиент
    data["slot"] = _pick(data.get("slot"), s.get("delivery_slots"))
    data["payment"] = _pick(data.get("payment"), s.get("payment_methods"))

    # Проверка остатков и списание — одной операцией под блокировкой, чтобы
    # два одновременных заказа не продали одно и то же блюдо дважды.
    def reserve(current: dict) -> tuple[dict, bool]:
        by_id = {it["id"]: it for it in current["items"]}
        lines, problems, goods, known = [], [], 0, 0
        for iid, qty in wanted.items():
            it = by_id.get(iid)
            if it is None:
                continue
            known += 1
            if int(it.get("stock", 0)) <= 0:
                problems.append(f"{it['title']} — закончилось")
                continue
            if qty > int(it["stock"]):
                problems.append(f"{it['title']} — осталось только {it['stock']} шт, положили их")
                qty = int(it["stock"])
            lines.append({"id": iid, "title": it["title"], "price": int(it["price"]), "qty": qty})
            goods += int(it["price"]) * qty

        if not lines:
            # не осталось ни одной известной позиции: либо всё закончилось, либо меню разошлось с витриной
            return {"ok": False, "reason": "gone" if known else "unknown",
                    "lines": [], "problems": problems, "goods": 0}, False
        if goods < int(s["min_order"]):
            return {"ok": False, "reason": "min", "lines": lines, "problems": problems, "goods": goods}, False

        for line in lines:                       # остатки списываем только когда заказ точно принят
            by_id[line["id"]]["stock"] = int(by_id[line["id"]]["stock"]) - line["qty"]
        return {"ok": True, "lines": lines, "problems": problems, "goods": goods}, True

    try:
        result = await store.mutate(reserve)
    except MenuError as e:
        log.error("не удалось обновить остатки: %s", e)
        raise OrderError("Не получилось оформить заказ. Попробуйте ещё раз через минуту.",
                         "MENU_UNAVAILABLE") from e

    if not result["ok"]:
        if result["reason"] == "gone":
            # называем блюда по именам: клиент иначе не понимает, что именно
            # ушло, — особенно если заказ оформлял он сам пару минут назад
            what = ", ".join(f"«{p.split(' — ')[0]}»" for p in result["problems"]) or "Все блюда"
            raise OrderError(f"{what} уже закончилось — его успели заказать. "
                             "Выберите другое блюдо в меню 💛", "GONE")
        if result["reason"] == "unknown":
            raise OrderError("Меню обновилось, и этих блюд в нём больше нет. "
                             "Откройте меню и соберите заказ заново.", "UNKNOWN_ITEMS")
        raise OrderError(f"Сумма заказа {rub(result['goods'])} меньше минимальной "
                         f"({rub(s['min_order'])}). Добавьте блюд в меню и оформите снова.", "BELOW_MIN")

    lines = result["lines"]
    problems = result["problems"]
    goods = result["goods"]
    # стоимость и район доставки считаем на сервере: витрина присылает только название района
    zone = delivery_zone(s, data.get("zone"), goods)
    delivery = delivery_cost(s, goods, zone)
    total = goods + delivery
    if zone is not None:
        data["zone"] = zone.get("title", "")
        if zone.get("min_sum") and goods < int(zone["min_sum"]):
            await return_stock(lines)
            raise OrderError(f"Для доставки в район «{zone['title']}» минимальная сумма "
                             f"{rub(int(zone['min_sum']))}. Добавьте блюд в меню и оформите снова.",
                             "ZONE_MIN_SUM")

    try:
        order_id, order = save_order(user_id, username, data, lines, goods, delivery, total,
                                     source=source)
    except sqlite3.Error as e:
        log.exception("не удалось сохранить заказ: %s", e)
        await return_stock(lines)                # не теряем остатки, если БД недоступна
        raise OrderError("Не удалось сохранить заказ. Попробуйте ещё раз или позвоните нам.",
                         "DB_ERROR") from e

    order = _attach_warnings(order_id, problems)
    await notify_admins(order, bot)
    log.info("заказ №%s из %s: %s, %s", order_id, source, rub(total), data.get("phone"))
    return order_id, order, lines, problems


def _attach_warnings(order_id: int, problems: list[str]) -> sqlite3.Row:
    """Дописывает к заказу предупреждения (не хватило остатков) для админки."""
    if not problems:
        return _get_order(order_id)
    with db() as con:
        row = con.execute("SELECT comment FROM orders WHERE id=?", (order_id,)).fetchone()
        extra = " | ".join(problems)
        comment = ((row["comment"] or "") + " · " if row and row["comment"] else "") + "⚠ " + extra
        con.execute("UPDATE orders SET comment=? WHERE id=?", (clip(comment, 500), order_id))
    return _get_order(order_id)


def _get_order(order_id: int) -> sqlite3.Row:
    with db() as con:
        return con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


async def notify_client(order: sqlite3.Row, status: str, bot: Bot | None = None) -> bool:
    """Пишет клиенту о новом статусе заказа. Возвращает True, если сообщение ушло.

    Заказы с сайта без Telegram (user_id = 0) уведомить некуда: у них нет чата,
    куда писать, — менеджер связывается по телефону. Для них тихо выходим,
    а не пытаемся отправить сообщение в чат 0.
    """
    template = CLIENT_TEXT.get(status)
    if not template:
        return False
    try:
        uid = int(order["user_id"] or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid <= 0:
        log.info("заказ №%s оформлен без Telegram — уведомление клиенту не отправляем",
                 order["id"])
        return False
    bot = bot or getattr(dp, "_uspevaika_bot", None)
    if bot is None:
        return False
    try:
        await bot.send_message(uid, template.format(id=order["id"], phone=_phone()),
                               reply_markup=main_kb(uid) if status == "delivered" else None)
    except Exception as e:
        log.warning("клиенту %s не доставлено уведомление о заказе №%s: %s",
                    uid, order["id"], e)
        return False
    return True


async def notify_admins(order: sqlite3.Row, bot: Bot | None = None) -> None:
    """Сообщает о новом заказе: в чат менеджеров и лично админам."""
    bot = bot or getattr(dp, "_uspevaika_bot", None)
    text = admin_text(order)
    markup = admin_kb(order["id"], "new")

    if bot is None:
        return

    if ADMIN_CHAT_ID:
        try:
            sent = await bot.send_message(ADMIN_CHAT_ID, text, reply_markup=markup)
            with db() as con:
                con.execute("UPDATE orders SET admin_msg_id=? WHERE id=?",
                            (sent.message_id, order["id"]))
            return
        except Exception as e:
            log.error("Не удалось отправить заказ №%s в чат менеджеров: %s", order["id"], e)

    # Чата менеджеров нет (или он недоступен) — пишем лично админам:
    # у них кнопки статусов прямо в личке
    if not ADMIN_IDS:
        log.warning("ADMIN_CHAT_ID и ADMIN_IDS пусты — заказ №%s виден только в админке", order["id"])
        return
    for uid in ADMIN_IDS:
        try:
            sent = await bot.send_message(uid, text, reply_markup=markup)
            with db() as con:
                con.execute("UPDATE orders SET admin_msg_id=? WHERE id=?",
                            (sent.message_id, order["id"]))
            return
        except Exception as e:
            log.warning("админу %s не доставлено уведомление о заказе №%s: %s", uid, order["id"], e)


def save_order(user_id: int, username: str | None, data: dict, lines: list[dict],
               goods: int, delivery: int, total: int,
               status: str = "new", source: str | None = None) -> tuple[int, sqlite3.Row]:
    with db() as con:
        cur = con.execute(
            "INSERT INTO orders(created,user_id,username,name,phone,address,zone,date,slot,payment,comment,"
            "items,goods,delivery,total,status,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), user_id, username,
             clip(data.get("name"), 60), clip(data.get("phone"), 30), clip(data.get("address"), 200),
             clip(data.get("zone"), 120),
             clip(data.get("date"), 10), clip(data.get("slot"), 20), clip(data.get("payment"), 40),
             clip(data.get("comment"), 500), json.dumps(lines, ensure_ascii=False),
             goods, delivery, total, status, source))
        order_id = cur.lastrowid
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return order_id, order


@dp.message(F.web_app_data)
async def on_order(m: Message, bot: Bot):
    if not _cooled_down(m.from_user.id):
        await m.answer("Заказ уже принят. Если нужно оформить ещё один — подождите пару секунд.")
        return

    try:
        order_id, order, lines, problems = await place_order(
            parse_order(m.web_app_data.data), user_id=m.from_user.id,
            username=m.from_user.username, source="telegram", bot=bot)
    except OrderError as e:
        await m.answer(str(e), reply_markup=main_kb(m.from_user.id))
        return

    _mark_order(m.from_user.id)
    note = ("\n\n⚠️ " + "\n⚠️ ".join(html.escape(p) for p in problems)) if problems else ""
    await m.answer(
        f"<b>Заказ №{order_id} получен!</b>\n\n{order_lines(lines)}\n"
        f"Доставка: {rub(order['delivery']) if order['delivery'] else 'бесплатно'}\n"
        f"<b>Итого: {rub(order['total'])}</b>\n\n"
        f"🕒 {human_date(order['date'])}, {html.escape(order['slot'])}\n"
        f"📍 {html.escape(order['address'])}\n"
        f"💳 {html.escape(order['payment'])}{note}\n\nМенеджер подтвердит заказ, статус придёт сюда.",
        reply_markup=main_kb(m.from_user.id))


# ---------- ручной заказ и правка состава (админка) ----------
def delivery_for(settings: dict, goods: int, zone_title: str | None) -> tuple[dict | None, int]:
    """Район и стоимость доставки для суммы заказа."""
    zone = delivery_zone(settings, zone_title, goods)
    return zone, delivery_cost(settings, goods, zone)


async def manual_order(data: dict, *, status: str = "accepted") -> tuple[int, sqlite3.Row]:
    """Заказ, принятый менеджером по телефону.

    Ведёт себя как обычный: списывает остатки, попадает в базу и в статистику.
    Без этого телефонные заказы не отражались на складе, и менеджеру приходилось
    сводить остатки вручную — а клиент из витрины мог купить уже проданное.
    """
    wanted = wanted_items(data)
    if not wanted:
        raise OrderError("В заказе нет ни одного блюда.", "EMPTY_CART")
    menu = store.read()
    s = menu["settings"]

    # Подсчёт состава и списание — в одной операции под общим lock: иначе между
    # ними успел бы влезть заказ из витрины и продать то же блюдо.
    def reserve(current: dict) -> tuple[dict, bool]:
        by_id = {it["id"]: it for it in current["items"]}
        lines, warnings = [], []
        for iid, qty in wanted.items():
            it = by_id.get(iid)
            if it is None:
                continue
            have = int(it.get("stock", 0))
            if have <= 0:
                warnings.append(f"{it['title']} — уже нет в наличии, в заказ не добавлен")
                continue
            if qty > have:
                warnings.append(f"{it['title']} — осталось только {have} шт, поставили их")
                qty = have
            lines.append({"id": iid, "title": it["title"], "price": int(it["price"]), "qty": qty})
        if not lines:
            return {"ok": False, "warnings": warnings}, False
        for line in lines:                 # остаток не уходит в минус: qty уже ограничен
            by_id[line["id"]]["stock"] = int(by_id[line["id"]]["stock"]) - line["qty"]
        return {"ok": True, "lines": lines, "warnings": warnings}, True

    result = await store.mutate(reserve)
    if not result["ok"]:
        raise OrderError("; ".join(result["warnings"]) or "Блюда закончились.", "GONE")

    lines = result["lines"]
    goods = sum(l["price"] * l["qty"] for l in lines)
    zone, delivery = delivery_for(s, goods, data.get("zone"))
    if zone is not None:
        data["zone"] = zone.get("title", "")
    total = goods + delivery

    comment = clip(data.get("comment"), 500)
    if result["warnings"]:
        comment = (comment + " · " if comment else "") + "⚠ " + " | ".join(result["warnings"])
    data["comment"] = comment

    order_id, order = save_order(0, None, data, lines, goods, delivery, total,
                                 status=status, source="phone")
    log.info("заказ №%s принят по телефону: %s", order_id, rub(total))
    return order_id, order


async def edit_order_items(order_id: int, wanted: dict[str, int]) -> tuple[dict, list[str]]:
    """Меняет состав заказа и приводит склад в согласие с новым составом.

    Склад считаем по разнице: лишнее возвращаем, недостающее списываем. Так
    правка заказа не ломает остатки и работает для заказов любого происхождения.
    Возвращает (итог заказа, предупреждения).
    """
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        raise OrderError("Заказ не найден.", "NOT_FOUND")
    if row["status"] in CLOSED:
        raise OrderError("Заказ уже закрыт — состав менять нельзя.", "CLOSED")

    old = {l["id"]: int(l["qty"]) for l in order_lines_raw(row) if l.get("id")}
    warnings: list[str] = []

    # Состав и склад меняем в одной операции под общим lock: правка не должна
    # разойтись с одновременным заказом из витрины.
    def apply(current: dict) -> tuple[dict, bool]:
        by_id = {it["id"]: it for it in current["items"]}
        new_lines: list[dict] = []
        for iid, qty in wanted.items():
            it = by_id.get(iid)
            if it is None:
                warnings.append(f"«{iid}» больше нет в меню")
                continue
            delta = qty - old.get(iid, 0)
            have = int(it.get("stock", 0))
            if delta > 0 and delta > have:
                # не хватает на увеличение — берём столько, сколько есть
                qty = old.get(iid, 0) + max(0, have)
                if qty <= 0:
                    warnings.append(f"«{it['title']}» закончилось — в заказе не будет")
                    continue
                warnings.append(f"«{it['title']}» — больше {qty} шт сейчас нет")
            if qty > 0:
                new_lines.append({"id": iid, "title": it["title"],
                                  "price": int(it["price"]), "qty": qty})
        # Склад: возвращаем то, чего стало меньше, списываем то, чего стало больше
        new_qty = {l["id"]: l["qty"] for l in new_lines}
        for iid in set(old) | set(new_qty):
            it = by_id.get(iid)
            if it is None:
                continue
            it["stock"] = max(0, int(it.get("stock", 0)) + old.get(iid, 0) - new_qty.get(iid, 0))
        return {"lines": new_lines}, bool(old) or bool(new_lines)

    result = await store.mutate(apply)
    lines = result["lines"]

    if not lines:
        raise OrderError("В заказе не осталось ни одного блюда. Отмените заказ вместо правки.",
                         "EMPTY")

    menu = store.read()
    goods = sum(l["price"] * l["qty"] for l in lines)
    zone = delivery_zone(menu["settings"], row["zone"], goods)
    delivery = delivery_cost(menu["settings"], goods, zone)
    total = goods + delivery
    with db() as con:
        con.execute("UPDATE orders SET items=?, goods=?, delivery=?, total=?, zone=? WHERE id=?",
                    (json.dumps(lines, ensure_ascii=False), goods, delivery, total,
                     (zone or {}).get("title", row["zone"]), order_id))
        updated = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    log.info("заказ №%s изменён: %s", order_id, rub(total))
    await refresh_admin_message(updated)
    return {"order": updated, "warnings": warnings}, warnings


async def refresh_admin_message(order: sqlite3.Row) -> None:
    """Обновляет карточку заказа в чате менеджеров после правки."""
    bot = getattr(dp, "_uspevaika_bot", None)
    if bot is None or not order["admin_msg_id"] or not ADMIN_CHAT_ID:
        return
    try:
        await bot.edit_message_text(chat_id=ADMIN_CHAT_ID, message_id=order["admin_msg_id"],
                                    text=admin_text(order),
                                    reply_markup=admin_kb(order["id"], order["status"]))
    except Exception as e:
        log.warning("не удалось обновить сообщение заказа №%s: %s", order["id"], e)


async def return_stock(lines: list[dict]) -> None:
    """Возвращает списанные остатки (отмена заказа или сбой записи в БД)."""
    def restore(current: dict) -> tuple[None, bool]:
        by_id = {it["id"]: it for it in current["items"]}
        by_title = {it["title"]: it for it in current["items"]}
        for line in lines:
            it = by_id.get(line["id"]) or by_title.get(line["title"])
            if it is not None:
                it["stock"] = int(it.get("stock", 0)) + int(line["qty"])
        return None, True

    try:
        await store.mutate(restore)
    except MenuError as e:
        log.error("не удалось вернуть остатки: %s", e)


@dp.callback_query(F.data.startswith("st:"))
async def on_status(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Менять статус могут только менеджеры", show_alert=True)
        return
    try:
        _, oid, status = c.data.split(":")
        oid = int(oid)
    except (ValueError, AttributeError):
        return await c.answer()
    if status not in STATUSES or status == "new":
        return await c.answer()

    with db() as con:
        o = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] in CLOSED:
            return await c.answer("Заказ уже закрыт")
        con.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
        o = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()

    if status == "cancelled":
        await return_stock(order_lines_raw(o))

    try:
        await c.message.edit_text(admin_text(o), reply_markup=admin_kb(oid, status))
    except Exception as e:      # сообщение могли удалить или оно не изменилось
        log.warning("не удалось обновить сообщение заказа №%s: %s", oid, e)
        try:
            await c.message.answer(admin_text(o), reply_markup=admin_kb(oid, status))
        except Exception:
            pass
    await c.answer(STATUSES[status])
    await notify_client(o, status, bot)


def _phone() -> str:
    try:
        return store.read()["settings"].get("phone", "—")
    except MenuError:
        return "—"


# ---------- стоп-лист и остатки (только менеджеры) ----------
@dp.message(Command("admin"))
async def admin_panel(m: Message):
    """Открывает админку; в Telegram — как Mini App с проверкой initData."""
    if not is_admin(m.from_user.id):
        return
    url = (WEBAPP_URL.rstrip("/") + "/admin") if WEBAPP_URL else ""
    if url.startswith("https://"):
        # web_app-кнопка допустима только в reply-клавиатуре (та, что под полем
        # ввода). В inline-клавиатуре Telegram отвечает BUTTON_TYPE_INVALID и
        # сообщение не доходит вовсе.
        if is_admin(m.from_user.id):
            # ссылка с токеном: в браузере нет initData, и без токена панель
            # попросит пароль, которого может не быть
            personal = admin_webapp_url(m.from_user.id)
            await m.answer(
                f'Панель управления: меню, остатки, фото, заказы и статистика.\n\n'
                f'<a href="{html.escape(personal)}">Открыть админку</a>\n\n'
                "Ссылка действует 30 дней. Если перестанет пускать — /admin заново.",
                reply_markup=main_kb(m.from_user.id))
        return
    await m.answer(
        "WEBAPP_URL не задан (нужен https-адрес витрины), поэтому Mini App открыть нельзя.\n"
        f"Откройте админку в браузере: http://localhost:{PORT}/admin — вход по ADMIN_PASSWORD.")


@dp.message(Command("stock"))
async def stock(m: Message):
    if not is_admin(m.from_user.id):
        return
    try:
        menu = store.read()
    except MenuError as e:
        return await m.answer(f"Меню недоступно: {html.escape(str(e))}")
    lines = [f"{'🔴' if it['stock'] <= 0 else ('🟡' if it['stock'] <= 3 else '🟢')} "
             f"{html.escape(it['title'])} — {it['stock']} шт · <code>{it['id']}</code>"
             for it in menu["items"]]
    await m.answer("<b>Остатки</b>\n" + "\n".join(lines) +
                   "\n\n/stop &lt;блюдо&gt; — убрать в стоп\n/set &lt;блюдо&gt; &lt;кол-во&gt; — задать остаток")


@dp.message(Command("orders"))
async def list_orders(m: Message):
    if not is_admin(m.from_user.id):
        return
    with db() as con:
        rows = con.execute("SELECT * FROM orders WHERE status NOT IN ('delivered','cancelled') "
                           "ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        return await m.answer("Активных заказов нет.")
    lines = [f"<b>№{r['id']}</b> · {STATUSES.get(r['status'], r['status'])} · "
             f"{rub(r['total'])} · {human_date(r['date'])}, {html.escape(r['slot'] or '')}"
             for r in rows]
    await m.answer("<b>Активные заказы</b>\n" + "\n".join(lines))


# ---------- правка состава заказа прямо в чате ----------
# Менеджеру за телефонами удобнее не открывать панель: он видит карточку заказа
# и может сразу поправить состав. Состав хранится в БД, статус — в памяти,
# чтобы не гонять запись на каждый клик.
EDIT_PAGE = 8                 # блюд на странице: кнопка ✏️ занимает callback_data
EDIT_TTL = 30 * 60            # недоделанную правку забываем через полчаса
_edits: dict[int, dict] = {}


def _edit_state(admin_id: int, order_id: int, create: bool = False) -> dict | None:
    key = admin_id
    now = time.time()
    for k in [k for k, v in _edits.items() if now - v["ts"] > EDIT_TTL]:
        _edits.pop(k, None)
    state = _edits.get(key)
    if state and state["order"] == order_id:
        state["ts"] = now
        return state
    if not create:
        return None
    # заводим новую правку по текущему составу заказа
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row["status"] in CLOSED:
        return None
    lines = order_lines_raw(row)
    state = {"order": order_id, "page": 0, "ts": now,
             "items": {l["id"]: int(l["qty"]) for l in lines if l.get("id")},
             "titles": {l["id"]: l["title"] for l in lines if l.get("id")}}
    _edits[key] = state
    return state


def edit_text_and_kb(admin_id: int, state: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Редактор в чате только убирает: уменьшить количество или убрать блюдо.

    Добавить блюдо в чате неудобно — там нет ни фото, ни поиска, а выбор идёт
    по страницам. Для добавления есть кнопка на веб-панель, где это делается
    в один экран.
    """
    order_id = state["order"]
    lines = [f"<b>Заказ №{order_id}: убрать лишнее</b>",
             "Уменьшайте количество — убранное вернётся на склад.\n"]

    rows: list[list[InlineKeyboardButton]] = []
    for iid, qty in state["items"].items():
        title = state["titles"].get(iid, iid)
        lines.append(f"{'▫️' if qty <= 0 else '•'} {html.escape(title)} — {qty} шт")
        if qty > 0:
            rows.append([
                InlineKeyboardButton(text=f"− {title[:20]}", callback_data=f"ed:{order_id}:m:{iid}"),
                InlineKeyboardButton(text="✖️ убрать", callback_data=f"ed:{order_id}:p:{iid}"),
            ])
    if not any(q > 0 for q in state["items"].values()):
        lines.append("\nПусто — сохранить нельзя. Чтобы отменить весь заказ, нажмите «❌ Отменить».")

    lines.append("\nДобавить блюдо — в веб-панели (кнопка ниже).")
    rows.append([InlineKeyboardButton(text="✅ Сохранить", callback_data=f"ed:{order_id}:s"),
                 InlineKeyboardButton(text="↩️ Вернуть как было", callback_data=f"ed:{order_id}:x")])
    if WEBAPP_URL:
        # ВАЖНО: кнопку web_app нельзя ставить в inline-клавиатуру обычного
        # сообщения — Telegram отвечает BUTTON_TYPE_INVALID, и менеджер не видит
        # даже список блюд. Поэтому открываем панель текстом: страница сама
        # перенаправит на нужный заказ по ?order=N.
        lines.append(f'\n<a href="{html.escape(admin_order_url(order_id, admin_id))}">'
                     "➕ Добавить блюдо — открыть панель</a>")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("ed:"))
async def on_edit(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        return await c.answer("Менять заказ могут только менеджеры", show_alert=True)
    parts = c.data.split(":")
    if len(parts) < 3:
        return await c.answer()
    try:
        order_id = int(parts[1])
        action = parts[2]
        item_id = parts[3] if len(parts) > 3 else ""
    except (ValueError, IndexError):
        return await c.answer()

    if action == "0":                      # кнопка «✏️ Изменить состав» открывает редактор
        state = _edit_state(c.from_user.id, order_id, create=True)
        if state is None:
            return await c.answer("Заказ уже закрыт — править нельзя", show_alert=True)
        state["page"] = 0
    elif action in ("p", "m", "a", "n"):
        state = _edit_state(c.from_user.id, order_id, create=True)
        if state is None:
            return await c.answer("Заказ уже закрыт — править нельзя", show_alert=True)
        if action == "p":                      # переключить блюдо
            # в чате блюдо можно только убрать целиком
            state["items"][item_id] = 0
        else:
            # 'm' — минус одна порция; добавление в чате не поддерживается
            q = max(0, state["items"].get(item_id, 0) - 1)
            state["items"][item_id] = q
    elif action == "x":
        _edits.pop(c.from_user.id, None)
        order = _get_order(order_id)
        await _edit_or_send(c, admin_text(order), admin_kb(order_id, order["status"]))
        return await c.answer("Отменено")
    elif action == "s":
        await _save_edit(c, order_id)
        return
    else:
        return await c.answer()

    state = _edit_state(c.from_user.id, order_id)
    if state is None:
        return await c.answer()
    text, markup = edit_text_and_kb(c.from_user.id, state)
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except Exception as e:
        log.warning("не удалось показать правку заказа №%s: %s", order_id, e)
        await c.answer("Не удалось обновить сообщение", show_alert=True)
        return
    await c.answer()


async def _save_edit(c: CallbackQuery, order_id: int) -> None:
    state = _edit_state(c.from_user.id, order_id)
    if state is None:
        return await c.answer("Правка устарела", show_alert=True)
    # нули означают «убрать блюдо» — в заказ они не идут
    wanted = {iid: q for iid, q in state["items"].items() if q > 0}
    if not wanted:
        return await c.answer(
            "Нельзя убрать все блюда. Чтобы отменить заказ — нажмите «❌ Отменить»",
            show_alert=True)
    try:
        result, warnings = await edit_order_items(order_id, wanted)
    except OrderError as e:
        return await c.answer(str(e), show_alert=True)
    except Exception as e:
        log.exception("не удалось изменить заказ %s: %s", order_id, e)
        return await c.answer("Не удалось сохранить", show_alert=True)

    _edits.pop(c.from_user.id, None)
    order = result["order"]
    await _edit_or_send(c, admin_text(order), admin_kb(order_id, order["status"]))
    if warnings:
        await c.answer("; ".join(warnings)[:190], show_alert=True)
    else:
        await c.answer(f"Заказ №{order_id} изменён: {rub(order['total'])}")


# ---------- меню менеджера с кнопками ----------
# Кнопки дублируют команды, но не требуют помнить синтаксис: в чате менеджеров
# это важнее — сообщения перекрываются заказами, а команды забываются.
def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Активные заказы", callback_data="am:orders:0"),
         InlineKeyboardButton(text="📜 История", callback_data="am:history:0")],
        [InlineKeyboardButton(text="📦 Остатки", callback_data="am:stock:0"),
         InlineKeyboardButton(text="👥 Менеджеры", callback_data="am:admins:0")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← В меню", callback_data="am:menu:0")]])


HISTORY_PAGE = 6          # заказов на страницу истории


def _orders_text(rows: list, title: str) -> str:
    if not rows:
        return f"<b>{title}</b>\n\nПусто."
    lines = [f"<b>№{r['id']}</b> · {STATUSES.get(r['status'], r['status'])} · "
             f"{rub(r['total'])} · {human_date(r['date'])}, {html.escape(r['slot'] or '')}"
             f"{' · ' + html.escape(r['name']) if r['name'] else ''}"
             for r in rows]
    return f"<b>{title}</b>\n" + "\n".join(lines)


@dp.message(Command("menu"))
async def admin_menu(m: Message):
    """Панель менеджера в чате: всё то же, что в командах, но кнопками."""
    if not is_admin(m.from_user.id):
        return
    await m.answer(
        "<b>Панель менеджера</b>\n\nВыберите раздел. Команды тоже работают:\n"
        "<code>/orders</code>, <code>/stock</code>, <code>/admins</code>, "
        "<code>/history</code>, <code>/stop</code>, <code>/set</code>",
        reply_markup=admin_menu_kb())


@dp.callback_query(F.data.startswith("am:"))
async def admin_menu_action(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Доступно только менеджерам", show_alert=True)
        return
    try:
        _, action, page_text = c.data.split(":")
        page = max(0, int(page_text))
    except (ValueError, AttributeError):
        return await c.answer()

    if action == "menu":
        await _edit_or_send(c, "<b>Панель менеджера</b>\n\nВыберите раздел.",
                            admin_menu_kb())
        return await c.answer()

    if action == "orders":
        with db() as con:
            rows = con.execute("SELECT * FROM orders WHERE status NOT IN "
                               "('delivered','cancelled') ORDER BY id DESC LIMIT 15").fetchall()
        await _edit_or_send(c, _orders_text(rows, "Активные заказы"), back_kb())
        return await c.answer(f"Активных: {len(rows)}")

    if action == "history":
        with db() as con:
            total = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            rows = con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?",
                               (HISTORY_PAGE, page * HISTORY_PAGE)).fetchall()
        pages = max(1, (total + HISTORY_PAGE - 1) // HISTORY_PAGE)
        text = _orders_text(rows, f"История заказов · {total} всего · стр. {page + 1}/{pages}")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="← Новее", callback_data=f"am:history:{page - 1}"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="Старее →", callback_data=f"am:history:{page + 1}"))
        rows_kb = ([nav] if nav else []) + [
            [InlineKeyboardButton(text="← В меню", callback_data="am:menu:0")]]
        await _edit_or_send(c, text, InlineKeyboardMarkup(inline_keyboard=rows_kb))
        return await c.answer()

    if action == "stock":
        try:
            menu_data = store.read()
        except MenuError as e:
            return await c.answer("Меню недоступно", show_alert=True)
        lines = [f"{'🔴' if it['stock'] <= 0 else ('🟡' if it['stock'] <= 3 else '🟢')} "
                 f"{html.escape(it['title'])} — {it['stock']} шт"
                 for it in menu_data["items"]]
        await _edit_or_send(c, "<b>Остатки</b>\n" + "\n".join(lines), back_kb())
        return await c.answer()

    if action == "admins":
        await _edit_or_send(c, _admins_text(), admins_kb())
        return await c.answer()

    await c.answer()


def _admins_text() -> str:
    rows = admin_ids_all()
    lines = []
    for uid, name, username, added_at in rows:
        who = f"@{username}" if username else (name or "без имени")
        src = "из .env" if uid in ADMIN_IDS else "добавлен в чате"
        lines.append(f"• {html.escape(who)} · <code>{uid}</code> · {src}")
    return ("<b>Менеджеры</b>\n" + "\n".join(lines) +
            "\n\nДобавить: ответьте на сообщение человека командой <code>/addadmin</code> "
            "или пришлите <code>/addadmin &lt;id&gt;</code>.\n"
            "Убрать: <code>/deladmin &lt;id&gt;</code>. Тех, кто задан в .env, убрать нельзя.")


def admins_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Обновить", callback_data="am:admins:0")],
        [InlineKeyboardButton(text="← В меню", callback_data="am:menu:0")]])


async def _edit_or_send(c: CallbackQuery, text: str, markup) -> None:
    """Меняет текст сообщения, к которому привязана кнопка (или шлёт новое)."""
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except Exception:
        try:
            await c.message.answer(text, reply_markup=markup)
        except Exception:
            pass


@dp.message(Command("admins"))
async def list_admins(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer(_admins_text(), reply_markup=admins_kb())


@dp.message(Command("addadmin"))
async def add_admin_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    args = (command.args or "").strip()
    target = None

    # 1) ответ на сообщение человека — самый надёжный способ, id не нужно знать
    if m.reply_to_message and m.reply_to_message.from_user:
        target = m.reply_to_message.from_user
    elif args:
        raw = args.split()[0]
        # 2) id в команде: /addadmin 123456789
        if raw.lstrip("-").isdigit():
            uid = int(raw)
            if uid in ADMIN_IDS or any(u == uid for u, *_ in admin_ids_all()):
                return await m.answer(f"<code>{uid}</code> уже менеджер.")
            add_admin(uid, name=clip(" ".join(args.split()[1:]), 60),
                      added_by=m.from_user.id)
            log.info("менеджер %s добавлен пользователем %s", uid, m.from_user.id)
            return await m.answer(
                f"✅ <code>{uid}</code> теперь менеджер.\n\n"
                "Ему нужно открыть бота и нажать /start, чтобы появились кнопки.",
                reply_markup=admins_kb())
        # 3) @ник — Telegram не даёт боту узнать id по нику
        if raw.startswith("@"):
            return await m.answer(
                "По @нику добавить нельзя: Telegram не сообщает боту id по нику.\n\n"
                "Ответьте на любое сообщение человека в этом чате командой /addadmin "
                "или пришлите его числовой id.")

    if target is None:
        return await m.answer(
            "Как добавить менеджера:\n"
            "• ответьте на сообщение человека командой /addadmin,\n"
            "• или пришлите его id: /addadmin 123456789")

    uid = target.id
    if uid in ADMIN_IDS or any(u == uid for u, *_ in admin_ids_all()):
        return await m.answer(f"{html.escape(target.full_name)} уже менеджер.")
    add_admin(uid, name=target.full_name, username=target.username,
              added_by=m.from_user.id)
    log.info("менеджер %s добавлен пользователем %s", uid, m.from_user.id)
    await m.answer(
        f"✅ {html.escape(target.full_name)} теперь менеджер.\n\n"
        "Ему нужно открыть бота и нажать /start, чтобы появились кнопки.",
        reply_markup=admins_kb())


@dp.message(Command("deladmin"))
async def del_admin_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    args = (command.args or "").strip()
    target_id = None
    if m.reply_to_message and m.reply_to_message.from_user:
        target_id = m.reply_to_message.from_user.id
    elif args and args.split()[0].lstrip("-").isdigit():
        target_id = int(args.split()[0])
    if target_id is None:
        return await m.answer("Укажите id: <code>/deladmin 123456789</code> "
                              "или ответьте на сообщение человека.")
    if target_id in ADMIN_IDS:
        return await m.answer(
            "Этот менеджер задан в <code>.env</code> — убрать его через бота нельзя.\n"
            "Отредактируйте <code>ADMIN_IDS</code> в файле и перезапустите бота.")
    if target_id == m.from_user.id:
        return await m.answer("Нельзя убрать себя — попросите другого менеджера.")
    if remove_admin(target_id):
        log.info("менеджер %s убран пользователем %s", target_id, m.from_user.id)
        await m.answer(f"✅ <code>{target_id}</code> больше не менеджер.",
                       reply_markup=admins_kb())
    else:
        await m.answer("Такого менеджера нет в списке.")


@dp.message(Command("history"))
async def history_cmd(m: Message, command: CommandObject):
    """История заказов: /history или /history 3 — страница."""
    if not is_admin(m.from_user.id):
        return
    try:
        page = max(0, int((command.args or "0").split()[0]) - 1)
    except (ValueError, IndexError):
        page = 0
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        rows = con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?",
                           (HISTORY_PAGE, page * HISTORY_PAGE)).fetchall()
    pages = max(1, (total + HISTORY_PAGE - 1) // HISTORY_PAGE)
    text = _orders_text(rows, f"История заказов · {total} всего · стр. {page + 1}/{pages}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Новее", callback_data=f"am:history:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Старее →", callback_data=f"am:history:{page + 1}"))
    markup = InlineKeyboardMarkup(inline_keyboard=([nav] if nav else []) +
                                  [[InlineKeyboardButton(text="← В меню", callback_data="am:menu:0")]])
    await m.answer(text, reply_markup=markup)


async def _resolve(m: Message, query: str):
    try:
        menu = store.read()
    except MenuError as e:
        await m.answer(f"Меню недоступно: {html.escape(str(e))}")
        return None, None
    it = find_item(menu, query) if query else None
    if isinstance(it, list):
        await m.answer("Нашлось несколько: " + ", ".join(f"<code>{x['id']}</code>" for x in it))
        return menu, None
    if not it:
        await m.answer("Блюдо не найдено. Список: /stock")
    return menu, it


@dp.message(Command("stop"))
async def stop_item(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    _, it = await _resolve(m, command.args or "")
    if not it:
        return
    await _patch_stock(it["id"], 0)
    await m.answer(f"«{html.escape(it['title'])}» в стоп-листе. В меню показано «Нет в наличии».")


@dp.message(Command("set"))
async def set_stock(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    args = (command.args or "").rsplit(" ", 1)
    if len(args) != 2 or not args[1].isdigit():
        return await m.answer("Формат: /set тефтели 10")
    _, it = await _resolve(m, args[0])
    if not it:
        return
    qty = int(args[1])
    await _patch_stock(it["id"], qty)
    await m.answer(f"«{html.escape(it['title'])}»: остаток {qty} шт.")


async def _patch_stock(item_id: str, qty: int) -> None:
    def apply(menu: dict) -> tuple[bool, bool]:
        for it in menu["items"]:
            if it["id"] == item_id:
                it["stock"] = qty
                return True, True
        return False, False

    try:
        await store.mutate(apply)
    except MenuError as e:
        log.error("не удалось изменить остаток %s: %s", item_id, e)


# ---------- веб-сервер для витрины ----------
async def serve_index(_: web.Request) -> web.StreamResponse:
    # Витрину не кэшируем: Telegram и браузер иначе держат старую версию, и
    # исправления (например, передача id клиента) не доезжают до людей.
    resp = web.FileResponse(WEBAPP_DIR / "index.html")
    resp.headers.update({
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })
    return resp


async def serve_img(request: web.Request) -> web.StreamResponse:
    """Фото блюд с долгим кэшем.

    Без Cache-Control браузер перепроверял каждую картинку при любой
    перерисовке карточки, и меню заметно мигало. Имя файла меняется только
    при загрузке нового фото (админка добавляет ?v=…), поэтому кэш безопасен.
    """
    rel = request.match_info["path"]
    path = (WEBAPP_DIR / "img" / rel).resolve()
    img_root = (WEBAPP_DIR / "img").resolve()
    # защита от выхода за пределы папки через ../ в пути
    if not str(path).startswith(str(img_root)) or not path.is_file():
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    resp.headers["Cache-Control"] = "public, max-age=2592000"   # 30 дней
    return resp


async def serve_menu(_: web.Request) -> web.StreamResponse:
    try:
        menu = store.read()
    except MenuError as e:
        log.error("не читается menu.json: %s", e)
        return web.Response(status=503, text='{"error":"menu unavailable"}', content_type="application/json")

    # Даты доставки считает сервер: он знает свою дату, а часы устройства могут
    # быть в другом часовом поясе — тогда витрина предлагала бы не тот день и
    # заказ отклонялся бы как «в этот день доставки нет».
    try:
        menu.setdefault("settings", {})["server_dates"] = allowed_dates(menu["settings"])
    except (MenuError, KeyError, TypeError, ValueError) as e:
        log.warning("не удалось посчитать даты доставки: %s", e)

    return web.json_response(menu, headers={"Cache-Control": "no-store"})


async def serve_health(_: web.Request) -> web.StreamResponse:
    # version нужен автообновлению: по нему скрипт понимает, что уже установлено
    return web.json_response({"ok": True, "version": VERSION,
                              "items": len(store.read()["items"])})


# ---------- приём заказа из браузера ----------
# Внутри Telegram витрина отправляет заказ через sendData (web_app_data).
# В обычном браузере такого канала нет, поэтому тот же заказ принимает этот
# эндпоинт — с теми же проверками, списанием остатков и уведомлением админам.

_ORDER_IPS: dict[str, list[float]] = {}
WEB_ORDER_LIMIT = 8          # заказов с одного адреса
WEB_ORDER_WINDOW = 3600      # за час


def _web_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _ORDER_IPS.get(ip, []) if now - t < WEB_ORDER_WINDOW]
    _ORDER_IPS[ip] = hits
    return len(hits) < WEB_ORDER_LIMIT


def _remember_ip(ip: str) -> None:
    _ORDER_IPS.setdefault(ip, []).append(time.time())
    if len(_ORDER_IPS) > 2000:                 # не даём словарю расти бесконечно
        cutoff = time.time() - WEB_ORDER_WINDOW
        for key in [k for k, v in _ORDER_IPS.items() if not any(t > cutoff for t in v)]:
            _ORDER_IPS.pop(key, None)


def last_username(user_id: int) -> str | None:
    """Ник, под которым этот клиент уже заказывал, — если он есть в базе."""
    if not user_id:
        return None
    try:
        with db() as con:
            row = con.execute("SELECT username FROM orders WHERE user_id=? AND username IS NOT NULL "
                              "ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    except sqlite3.Error:
        return None
    return clip(row["username"], 64) if row and row["username"] else None


def telegram_identity(init_data: str | None) -> tuple[int, str | None]:
    """Достаёт Telegram id и ник из initData, если подпись сходится.

    Витрина внутри Telegram передаёт заказ через sendData, и там id берётся из
    самого обновления. Но если telegram-web-app.js не загрузился (бывает, когда
    telegram.org недоступен), витрина уходит на POST /api/order — и без этой
    проверки заказ записывался бы как «с сайта, без Telegram». Telegram при этом
    всё равно кладёт данные пользователя в initData, так что id можно взять
    оттуда, а подпись проверить ключом бота: подделать её нельзя.
    """
    if not init_data or not BOT_TOKEN:
        return 0, None
    verified = admin.verify_init_data(init_data, BOT_TOKEN)
    if not verified:
        return 0, None
    user = verified.get("user") or {}
    try:
        uid = int(user.get("id") or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid <= 0:
        return 0, None
    username = clip(user.get("username"), 64) or None
    return uid, username


async def serve_order(request: web.Request) -> web.StreamResponse:
    ip = request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0].strip()
    if not _web_rate_ok(ip):
        return web.json_response(
            {"error": "Слишком много заказов с этого адреса. Позвоните нам, оформим вручную."},
            status=429)
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"error": "Не получилось прочитать заказ."}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "Не получилось прочитать заказ."}, status=400)

    # Кто клиент, в порядке надёжности: токен из ссылки (его ставит бот при
    # нажатии кнопки — работает даже на iOS, где initData не приходит), затем
    # подписанный initData из витрины.
    user_id, username = 0, None
    token = clip(data.get("ut"), 64)
    token_id = verify_user_token(token)
    if token_id:
        user_id, how = token_id, "токен из ссылки"
    init_data = request.headers.get("X-Init-Data") or clip(data.get("initData"), 4096)
    if not user_id:
        user_id, username = telegram_identity(init_data)
        how = "initData" if user_id else ""
    if user_id and not username:
        username = last_username(user_id)   # ник из прошлых заказов, чтобы в админке была ссылка
    # В лог пишем, как опознали клиента: без этого невозможно понять, почему
    # заказ снова оказался «с сайта». Сам initData и токен в лог не попадают.
    log.info("клиент заказа: %s (токен %s, initData %s)",
             f"id {user_id}" if user_id else "не опознан — с сайта",
             "есть" if token else "нет", "есть" if init_data else "нет")
    if token and not token_id:
        log.warning("токен клиента не принят — подпись не сходится; "
                    "проверьте, что BOT_TOKEN не менялся после выдачи ссылки")
    data.pop("initData", None)      # в базу и в логи он не нужен
    data.pop("ut", None)

    try:
        order_id, order, lines, _ = await place_order(
            data, user_id=user_id, username=username,
            source="web" if not user_id else "web+telegram")
    except OrderError as e:
        return web.json_response({"error": str(e), "code": e.code}, status=400)

    _remember_ip(ip)
    return web.json_response({
        "ok": True,
        "order_id": order_id,
        "total": order["total"],
        "delivery": order["delivery"],
        "goods": order["goods"],
        "items": lines,
        "telegram": bool(user_id),
    })


def make_web_app() -> web.Application:
    app = web.Application(client_max_size=12 * 1024 * 1024)   # запас под загрузку фото
    app.router.add_get("/", serve_index)
    app.router.add_get("/menu.json", serve_menu)
    app.router.add_post("/api/order", serve_order)
    app.router.add_get("/healthz", serve_health)
    # фото блюд, логотип и иконка лежат рядом с index.html: витрина ссылается
    # на них относительными путями (img/…, favicon.png)
    app.router.add_get("/img/{path:.*}", serve_img)
    for name in ("favicon.png", "favicon.ico"):
        path = WEBAPP_DIR / name
        if path.exists():
            app.router.add_get(f"/{name}", lambda _r, p=path: web.FileResponse(p))
    app.router.add_static("/static/", WEBAPP_DIR)   # старый путь, для совместимости

    admin.register_admin(
        app, store=store, db=db, webapp_dir=WEBAPP_DIR,
        admin_ids=ADMIN_IDS, bot_token=BOT_TOKEN, password=ADMIN_PASSWORD,
        on_status_change=_notify_status, on_cancel=return_stock,
        verify_admin_token_fn=verify_admin_token,
        on_manual_order=manual_order, on_edit_order_items=edit_order_items,
        on_manual_error=OrderError, on_notify_admin=_notify_new_order,
    )
    return app


async def _notify_new_order(order_id: int) -> None:
    """Сообщает в чат менеджеров о заказе, созданном в панели."""
    order = _get_order(order_id)
    if order:
        await notify_admins(order)


async def _notify_status(order_id: int, status: str) -> None:
    """Смена статуса из админки: обновляем сообщение менеджерам и пишем клиенту."""
    with db() as con:
        o = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        return
    bot = getattr(dp, "_uspevaika_bot", None)
    if bot is None:
        return
    if o["admin_msg_id"] and ADMIN_CHAT_ID:
        try:
            await bot.edit_message_text(chat_id=ADMIN_CHAT_ID, message_id=o["admin_msg_id"],
                                        text=admin_text(o),
                                        reply_markup=admin_kb(order_id, status))
        except Exception as e:
            log.warning("не удалось обновить сообщение заказа №%s: %s", order_id, e)
    await notify_client(o, status, bot)


async def main():
    init_db()
    try:
        store.read()
    except MenuError as e:
        log.error("Проверьте menu.json: %s", e)

    if WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
        log.warning("WEBAPP_URL должен начинаться с https:// — иначе Telegram не откроет витрину")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — команды менеджера (/stock, /set, /stop) не будут работать ни у кого")
    if not ADMIN_PASSWORD:
        # это не ошибка: в Telegram вход идёт по подписанному токену, а ссылки
        # с токеном работают и в обычном браузере. Но если менеджер откроет
        # панель по голому адресу, пароля у него не будет — предупреждаем.
        log.info("ADMIN_PASSWORD не задан: вход в панель — по ссылке из бота "
                 "(/admin, кнопка «Админка»). Голый адрес /admin пустит только из Telegram.")
    if not ADMIN_PASSWORD and not ADMIN_IDS:
        log.warning("Админка /admin не защищена: задайте ADMIN_PASSWORD или ADMIN_IDS")

    runner = web.AppRunner(make_web_app())
    await runner.setup()
    await web.TCPSite(runner, BIND_HOST, PORT).start()
    log.info("Витрина: http://localhost:%s  (снаружи: %s)", PORT, WEBAPP_URL or "WEBAPP_URL не задан")

    if not BOT_TOKEN:
        log.warning("BOT_TOKEN не задан — работает только витрина в браузере. Ctrl+C для выхода.")
        await asyncio.Event().wait()
        return

    session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else AiohttpSession()
    if TELEGRAM_PROXY:
        log.info("Запросы к Telegram идут через прокси %s", TELEGRAM_PROXY)
    else:
        log.info("TELEGRAM_PROXY не задан — подключаюсь к Telegram напрямую")
    bot = Bot(BOT_TOKEN, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp._uspevaika_bot = bot          # админка меняет статусы и пишет клиентам тем же ботом
    try:
        try:
            me = await bot.get_me()
            log.info("Бот @%s на связи", me.username)
        except TelegramUnauthorizedError:
            log.error("Telegram отверг BOT_TOKEN — проверьте токен от @BotFather "
                      "(без пробелов и лишних символов). Витрина при этом работает: "
                      "http://localhost:%s", PORT)
            await asyncio.Event().wait()
            return
        except TelegramNetworkError as e:
            log.error("Нет связи с Telegram: %s. Если интернет требует прокси, "
                      "укажите его в TELEGRAM_PROXY (например, http://127.0.0.1:20172). "
                      "Витрина при этом работает: http://localhost:%s", e, PORT)
            await asyncio.Event().wait()
            return

        commands = [BotCommand(command="start", description="Меню и заказ"),
                    BotCommand(command="id", description="Показать chat id и user id")]
        if ADMIN_IDS:
            commands.extend([
                BotCommand(command="admin", description="Панель управления"),
                BotCommand(command="menu", description="Меню менеджера"),
                BotCommand(command="orders", description="Активные заказы"),
                BotCommand(command="history", description="История заказов"),
                BotCommand(command="stock", description="Остатки"),
                BotCommand(command="admins", description="Менеджеры"),
            ])
        await bot.set_my_commands(commands)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
