"""Админ-панель «Успевайки»: меню, остатки, фото, заказы, статистика.

Панель живёт на том же aiohttp-сервере, что и витрина (`/admin`), и работает
в двух режимах:

* внутри Telegram — Mini App присылает `initData`, подпись проверяется
  ключом бота, доступ получает только user id из `ADMIN_IDS`;
* в браузере — вход по паролю `ADMIN_PASSWORD` из `.env`; сессия хранится
  в подписанной HttpOnly-куке.

Все изменения menu.json идут через `MenuStore.mutate` — тот же lock, что и
у бота, поэтому остатки не разъедутся при одновременном заказе.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode

from aiohttp import web

log = logging.getLogger("uspevaika.admin")

SESSION_COOKIE = "usp_admin"
SESSION_TTL = 12 * 3600          # 12 часов
INIT_DATA_TTL = 24 * 3600        # подпись initData старше суток не принимаем
MAX_PHOTO_BYTES = 8 * 1024 * 1024
PHOTO_SIZE, PHOTO_SIZE2 = (720, 540), (1080, 810)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
BADGES = ["", "Хит", "Новинка", "Предзаказ", "Ограничено"]

# транс-лит: id блюда из русского названия, если админ не задал свой
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def slugify(text: str) -> str:
    out = "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return (out or "item")[:40].strip("-") or "item"


# ---------- авторизация ----------
def is_https(request) -> bool:
    """Пришёл ли запрос по HTTPS — в том числе через обратный прокси.

    За nginx/Caddy бот видит обычный HTTP на localhost. Если ставить флаг
    secure только по request.secure, браузер не сохранит куку сессии на
    https-сайте, и вход будет «не запоминаться». Поэтому смотрим на заголовки,
    которые проставляет прокси.
    """
    if request.secure:
        return True
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.headers.get("X-Forwarded-Ssl", "").lower() == "on"


def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверяет подпись Telegram WebApp initData. Возвращает данные или None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    except ValueError:
        return None
    received = pairs.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), received.encode()):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date and time.time() - auth_date > INIT_DATA_TTL:
        return None
    user = {}
    if pairs.get("user"):
        try:
            user = json.loads(pairs["user"])
        except ValueError:
            user = {}
    return {"user": user, "auth_date": auth_date}


class Sessions:
    """Подписанные токены сессии: base64(payload).hmac."""

    def __init__(self, secret: bytes):
        self.secret = secret

    def issue(self, uid: str) -> str:
        payload = base64.urlsafe_b64encode(
            json.dumps({"uid": uid, "exp": int(time.time()) + SESSION_TTL}).encode()).decode()
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str | None) -> str | None:
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        # сравниваем байты: не-ASCII в подделанном токене иначе даёт исключение
        if not hmac.compare_digest(self._sign(payload).encode(), sig.encode()):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            return None
        if int(data.get("exp", 0)) < time.time():
            return None
        return str(data.get("uid") or "") or None

    def _sign(self, payload: str) -> str:
        return base64.urlsafe_b64encode(
            hmac.new(self.secret, payload.encode(), hashlib.sha256).digest()).decode()


class LoginGuard:
    """Простое ограничение попыток входа с одного адреса."""

    def __init__(self, limit: int = 8, window: int = 300):
        self.limit, self.window = limit, window
        self.hits: dict[str, list[float]] = {}

    def blocked(self, key: str) -> bool:
        now = time.time()
        hits = [t for t in self.hits.get(key, []) if now - t < self.window]
        self.hits[key] = hits
        return len(hits) >= self.limit

    def fail(self, key: str) -> None:
        self.hits.setdefault(key, []).append(time.time())


# ---------- фото ----------
def process_photo(raw: bytes, photos_dir: Path, item_id: str) -> str:
    """Приводит загруженное фото к 4:3 WebP (обычный и retina). Возвращает путь для menu.json."""
    from PIL import Image, ImageEnhance, UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(raw))
        im.verify()
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise ValueError("файл не похож на изображение") from e

    w, h = im.size
    target = 4 / 3
    if w / h > target:                      # шире 4:3 — режем бока
        cw, ch = h * target, h
    else:
        cw, ch = w, w / target
    left, top = (w - cw) / 2, (h - ch) / 2
    im = im.crop((round(left), round(top), round(left + cw), round(top + ch)))

    photos_dir.mkdir(parents=True, exist_ok=True)
    for size, suffix in ((PHOTO_SIZE, ""), (PHOTO_SIZE2, "@2x")):
        pic = im.resize(size, Image.LANCZOS)
        pic = ImageEnhance.Color(pic).enhance(1.03)
        pic.save(photos_dir / f"{item_id}{suffix}.webp", "WEBP", quality=82, method=6)
    return f"img/{item_id}.webp"


def delete_photo(photos_dir: Path, item_id: str) -> None:
    for suffix in ("", "@2x"):
        p = photos_dir / f"{item_id}{suffix}.webp"
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                log.warning("не удалось удалить %s: %s", p, e)


# ---------- валидация позиции ----------
def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _price(value: Any) -> int:
    try:
        n = int(round(float(str(value).replace(" ", "").replace(",", "."))))
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 1_000_000))


def _stock(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 99_999))


def clean_item(data: dict, menu: dict, base: dict | None = None) -> tuple[dict | None, str | None]:
    """Нормализует позицию из админки. Возвращает (позиция, ошибка).

    Поля, которых нет в запросе, берутся из base — при частичном обновлении
    (например, только остаток) ничего не затирается. Присланное пустое значение
    считается командой «очистить»: снятая метка или удалённое описание
    исчезнут, а не останутся от прошлой версии.
    """
    if base:
        merged = dict(base)
        merged.update({k: v for k, v in data.items() if v is not None})
        data = merged
    categories = {c["id"] for c in menu.get("categories", [])}
    item_id = _text(data.get("id"), 40).lower()
    title = _text(data.get("title"), 80)
    if not title:
        return None, "Укажите название блюда."
    if not item_id:
        item_id = slugify(title)
        base, n = item_id, 2
        taken = {it["id"] for it in menu.get("items", [])}
        while item_id in taken:
            item_id = f"{base}-{n}"
            n += 1
    if not ID_RE.match(item_id):
        return None, "id может содержать только латиницу, цифры и дефис."
    category = _text(data.get("category"), 40)
    if category not in categories:
        return None, "Выберите категорию."
    price = _price(data.get("price"))
    if price <= 0:
        return None, "Укажите цену больше нуля."
    badge = _text(data.get("badge"), 20)
    if badge not in BADGES:
        badge = "" if not badge else badge[:20]
    return {
        "id": item_id,
        "category": category,
        "title": title,
        "price": price,
        "price_source": _text(data.get("price_source"), 20) or "admin",
        "weight": _text(data.get("weight"), 40),
        "pack": _text(data.get("pack"), 60),
        "cook": _text(data.get("cook"), 80),
        "kcal": _text(data.get("kcal"), 40) or None,
        "shelf": _text(data.get("shelf"), 40),
        "desc": _text(data.get("desc"), 600),
        "badge": badge or None,
        "stock": _stock(data.get("stock")),
        "color": _text(data.get("color"), 20) or "#e9a07f",
        "image": _text(data.get("image"), 120) or f"img/{item_id}.webp",
    }, None


# ---------- регистрация маршрутов ----------
def register_admin(app: web.Application, *,
                   store, db, webapp_dir: Path,
                   admin_ids: set[int], bot_token: str, password: str,
                   on_status_change: Callable[[int, str], Any] | None = None,
                   on_cancel: Callable[[list[dict]], Any] | None = None,
                   verify_admin_token_fn: Callable[[str], int] | None = None,
                   on_manual_order: Callable[..., Any] | None = None,
                   on_edit_order_items: Callable[..., Any] | None = None,
                   on_manual_error: type[Exception] | None = None,
                   on_notify_admin: Callable[[int], Any] | None = None) -> bool:
    """Добавляет /admin и /api/admin/*. Возвращает False, если админка не настроена.

    verify_admin_token_fn — проверка токена из кнопки «🛠 Админка» в главном меню
    бота (его выписывает bot.py). Без неё вход возможен только по initData и паролю.

    on_manual_order / on_edit_order_items — обработчики заказа по телефону и
    правки состава; их пишет bot.py, потому что они трогают склад и menu.json.
    on_manual_error — исключение, которым они сообщают клиенту о проблеме.
    """
    photos_dir = webapp_dir / "img"
    secret_material = (password or bot_token or "").encode()
    if not secret_material:
        log.warning("Админка выключена: нет ни ADMIN_PASSWORD, ни BOT_TOKEN")
        return False
    sessions = Sessions(hashlib.sha256(b"uspevaika-admin|" + secret_material).digest())
    guard = LoginGuard()

    def current_user(request: web.Request) -> str | None:
        """Кто смотрит админку: telegram id (initData или токен из меню) или 'password'."""
        header = request.headers.get("X-Init-Data", "")
        if header and bot_token:
            data = verify_init_data(header, bot_token)
            if data:
                uid = str((data.get("user") or {}).get("id") or "")
                if uid and int(uid) in admin_ids:
                    return uid
        # Кнопка «🛠 Админка» в главном меню несёт подписанный токен: на iOS
        # Telegram не передаёт initData, и без него админ не попал бы в панель.
        token_uid = (verify_admin_token_fn(request.headers.get("X-Admin-Token", ""))
                     if verify_admin_token_fn else 0)
        if token_uid and token_uid in admin_ids:
            return str(token_uid)
        return sessions.verify(request.cookies.get(SESSION_COOKIE))

    # ---------- CSRF ----------
    # Сессия живёт в SameSite=Lax куке, но этого мало: при переходе по ссылке
    # из чужого сайта браузер может отправить куку вместе с POST. Поэтому на
    # каждый изменяющий запрос требуем токен, который знает только наша
    # страница. Токен не хранится на сервере: он подписан секретом и привязан
    # к пользователю — так он переживает перезапуск бота и не копится в памяти.

    def _csrf_days() -> tuple[str, str]:
        """Сегодня и вчера по UTC — одинаково при выдаче и проверке."""
        now = time.time()
        return (time.strftime("%Y-%m-%d", time.gmtime(now)),
                time.strftime("%Y-%m-%d", time.gmtime(now - 86400)))

    def csrf_for(who: str) -> str:
        day, _ = _csrf_days()
        return hmac.new(sessions.secret, f"csrf|{who}|{day}".encode(), hashlib.sha256).hexdigest()

    def csrf_ok(request: web.Request, who: str) -> bool:
        given = request.headers.get("X-CSRF-Token", "")
        if not given:
            return False
        # допускаем токен за вчера: сессия может длиться через полночь
        valid = {hmac.new(sessions.secret, f"csrf|{who}|{d}".encode(), hashlib.sha256).hexdigest()
                 for d in _csrf_days()}
        return any(hmac.compare_digest(v.encode(), given.encode()) for v in valid)

    def need_auth(handler):
        async def wrapper(request: web.Request):
            who = current_user(request)
            if not who:
                return web.json_response({"error": "auth"}, status=401)
            # Меняющие запросы — только с токеном. Чтение токена не требует:
            # это ничего не меняет и позволяет странице загрузиться.
            if request.method not in ("GET", "HEAD", "OPTIONS") and not csrf_ok(request, who):
                return web.json_response(
                    {"error": "Сессия истекла, обновите страницу", "csrf": csrf_for(who)},
                    status=403)
            request["admin"] = who
            resp = await handler(request)
            # Свежий токен в каждом ответе: страница всегда знает актуальный
            if isinstance(resp, web.Response):
                resp.headers["X-CSRF-Token"] = csrf_for(who)
            return resp
        wrapper.__name__ = getattr(handler, "__name__", "handler")
        return wrapper

    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # --- сессия ---
    async def login(request: web.Request) -> web.Response:
        ip = request.remote or "?"
        if guard.blocked(ip):
            return web.json_response({"error": "Слишком много попыток. Подождите 5 минут."}, status=429)
        data = await body(request)
        given = str(data.get("password") or "")
        # compare_digest умеет только ASCII — сравниваем байты, чтобы пароль
        # мог быть на кириллице
        if not password or not hmac.compare_digest(given.encode(), password.encode()):
            guard.fail(ip)
            return web.json_response({"error": "Неверный пароль"}, status=403)
        token = sessions.issue("password")
        csrf = csrf_for("password")
        resp = web.json_response({"ok": True, "who": "password", "csrf": csrf})
        resp.headers["X-CSRF-Token"] = csrf
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True,
                        samesite="Lax", secure=is_https(request), path="/")
        log.info("вход в админку по паролю с %s", ip)
        return resp

    async def logout(request: web.Request) -> web.Response:
        resp = web.json_response({"ok": True})
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    async def csrf_endpoint(request: web.Request) -> web.Response:
        """Выдаёт странице её CSRF-токен. Без входа возвращает 401 без токена:
        иначе любой сайт мог бы получить токен для чужой сессии."""
        who = current_user(request)
        if not who:
            return web.json_response({"error": "auth"}, status=401)
        csrf = csrf_for(who)
        return web.json_response({"ok": True, "csrf": csrf}, headers={"X-CSRF-Token": csrf})

    @need_auth
    async def session_info(request: web.Request) -> web.Response:
        who = request["admin"]
        return web.json_response({"ok": True, "who": who, "telegram": who.isdigit(),
                                  "password_only": not bool(bot_token),
                                  "csrf": csrf_for(who)})

    # --- меню ---
    @need_auth
    async def get_menu(request: web.Request) -> web.Response:
        try:
            return web.json_response(await _read_menu(store))
        except Exception as e:
            return web.json_response({"error": f"Меню недоступно: {e}"}, status=503)

    @need_auth
    async def save_item(request: web.Request) -> web.Response:
        data = await body(request)
        try:
            menu_now = await _read_menu(store)
        except Exception as e:
            return web.json_response({"error": f"Меню недоступно: {e}"}, status=503)
        existing = next((it for it in menu_now.get("items", [])
                         if it["id"] == _text(data.get("id"), 40).lower()), None)
        item, problem = clean_item(data, menu_now, base=existing)
        if problem:
            return web.json_response({"error": problem}, status=400)

        def apply(menu: dict) -> tuple[dict, bool]:
            items = menu.setdefault("items", [])
            for i, existing in enumerate(items):
                if existing["id"] == item["id"]:
                    items[i] = item
                    return item, True
            items.append(item)
            return item, True

        result = await store.mutate(apply)
        log.info("админка: позиция %s сохранена", item["id"])
        return web.json_response({"ok": True, "item": result})

    @need_auth
    async def delete_item(request: web.Request) -> web.Response:
        data = await body(request)
        item_id = _text(data.get("id"), 40)
        drop_photo = bool(data.get("drop_photo"))

        def apply(menu: dict) -> tuple[bool, bool]:
            before = len(menu.get("items", []))
            menu["items"] = [it for it in menu.get("items", []) if it["id"] != item_id]
            return len(menu["items"]) != before, len(menu["items"]) != before

        removed = await store.mutate(apply)
        if removed and drop_photo:
            delete_photo(photos_dir, item_id)
        return web.json_response({"ok": True, "removed": removed})

    @need_auth
    async def set_stock(request: web.Request) -> web.Response:
        data = await body(request)
        item_id = _text(data.get("id"), 40)
        stop = bool(data.get("stop"))
        stock = _stock(data.get("stock"))

        def apply(menu: dict) -> tuple[int | None, bool]:
            for it in menu.get("items", []):
                if it["id"] == item_id:
                    it["stock"] = 0 if stop else stock
                    return it["stock"], True
            return None, False

        value = await store.mutate(apply)
        if value is None:
            return web.json_response({"error": "Блюдо не найдено"}, status=404)
        return web.json_response({"ok": True, "stock": value})

    # --- категории ---
    @need_auth
    async def save_category(request: web.Request) -> web.Response:
        data = await body(request)
        cid = _text(data.get("id"), 40).lower() or slugify(_text(data.get("title"), 40))
        title = _text(data.get("title"), 60)
        icon = _text(data.get("icon"), 20) or "leaf"
        if not title or not ID_RE.match(cid):
            return web.json_response({"error": "Нужны название и латинский id"}, status=400)

        def apply(menu: dict) -> tuple[bool, bool]:
            cats = menu.setdefault("categories", [])
            for c in cats:
                if c["id"] == cid:
                    c["title"], c["icon"] = title, icon
                    return True, True
            cats.append({"id": cid, "title": title, "icon": icon})
            return True, True

        await store.mutate(apply)
        return web.json_response({"ok": True})

    @need_auth
    async def delete_category(request: web.Request) -> web.Response:
        data = await body(request)
        cid = _text(data.get("id"), 40)
        try:
            menu_now = await _read_menu(store)
        except Exception:
            menu_now = {}
        if any(it["category"] == cid for it in menu_now.get("items", [])):
            return web.json_response({"error": "В категории есть блюда — сначала перенесите их"}, status=400)

        def apply(menu: dict) -> tuple[bool, bool]:
            before = len(menu.get("categories", []))
            menu["categories"] = [c for c in menu.get("categories", []) if c["id"] != cid]
            return len(menu["categories"]) != before, len(menu["categories"]) != before

        await store.mutate(apply)
        return web.json_response({"ok": True})

    # --- фото ---
    @need_auth
    async def upload_photo(request: web.Request) -> web.Response:
        item_id = _text(request.query.get("id"), 40).lower()
        if not ID_RE.match(item_id):
            return web.json_response({"error": "Сначала сохраните блюдо"}, status=400)
        try:
            post = await request.post()
        except Exception:
            return web.json_response({"error": "Не удалось прочитать файл"}, status=400)
        field = post.get("photo")
        if field is None or not hasattr(field, "file"):
            return web.json_response({"error": "Файл не приложен"}, status=400)
        raw = field.file.read(MAX_PHOTO_BYTES + 1)
        if len(raw) > MAX_PHOTO_BYTES:
            return web.json_response({"error": "Фото больше 8 МБ"}, status=413)
        try:
            rel = process_photo(raw, photos_dir, item_id)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            log.exception("не удалось обработать фото: %s", e)
            return web.json_response({"error": "Не удалось обработать фото"}, status=500)

        def apply(menu: dict) -> tuple[bool, bool]:
            for it in menu.get("items", []):
                if it["id"] == item_id:
                    it["image"] = rel
                    return True, True
            return False, False

        await store.mutate(apply)
        log.info("админка: фото для %s обновлено", item_id)
        return web.json_response({"ok": True, "image": rel, "v": int(time.time())})

    # --- заказы ---
    @need_auth
    async def list_orders(request: web.Request) -> web.Response:
        status = _text(request.query.get("status"), 20)
        limit = 200
        where, params = "", ()
        if status and status != "all":
            where, params = "WHERE status=?", (status,)
        with db() as con:
            rows = con.execute(
                f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT {limit}", params).fetchall()
            counts = {r["status"]: r["n"] for r in con.execute(
                "SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}
        return web.json_response({
            "orders": [order_json(r) for r in rows],
            "counts": counts,
            "statuses": _status_list(),
        })

    @need_auth
    async def set_status(request: web.Request) -> web.Response:
        data = await body(request)
        try:
            order_id = int(data.get("id"))
        except (TypeError, ValueError):
            return web.json_response({"error": "Нет номера заказа"}, status=400)
        status = _text(data.get("status"), 20)
        if status not in STATUS_KEYS:
            return web.json_response({"error": "Неизвестный статус"}, status=400)
        with db() as con:
            row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                return web.json_response({"error": "Заказ не найден"}, status=404)
            closed = row["status"] in CLOSED_STATUSES
            if closed and status != row["status"]:
                return web.json_response({"error": "Заказ уже закрыт"}, status=400)
            con.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        if status == "cancelled" and row["status"] != "cancelled" and on_cancel:
            await on_cancel(order_json(row)["items"])
        if on_status_change:
            try:
                await on_status_change(order_id, status)
            except Exception as e:
                log.warning("уведомление клиента по заказу %s не отправлено: %s", order_id, e)
        log.info("админка: заказ %s → %s", order_id, status)
        return web.json_response({"ok": True})

    # --- заказ по телефону и правка состава ---
    # Телефонный заказ раньше жил только в блокноте менеджера: склад о нём не
    # знал, и клиент из витрины мог купить уже проданное. Теперь такой заказ
    # оформляется здесь и ведёт себя как обычный.

    @need_auth
    async def create_order(request: web.Request) -> web.Response:
        data = await body(request)
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return web.json_response({"error": "Добавьте хотя бы одно блюдо"}, status=400)
        try:
            order_id, row = await on_manual_order({**data, "type": "order", "items": items})
        except on_manual_error as e:
            return web.json_response({"error": str(e), "code": getattr(e, "code", "REJECTED")},
                                     status=400)
        except Exception as e:
            log.exception("не удалось оформить заказ по телефону: %s", e)
            return web.json_response({"error": "Не удалось сохранить заказ"}, status=500)
        # Заказ, оформленный прямо в панели, тоже должен появиться в чате
        # менеджеров: иначе его увидит только тот, кто его создал, и второй
        # менеджер начнёт собирать по устаревшему списку.
        if on_notify_admin:
            try:
                await on_notify_admin(order_id)
            except Exception as e:
                log.warning("заказ %s не отправлен в чат менеджеров: %s", order_id, e)
        log.info("админка: заказ №%s принят по телефону", order_id)
        return web.json_response({"ok": True, "order": order_json(row)})

    @need_auth
    async def edit_items(request: web.Request) -> web.Response:
        data = await body(request)
        try:
            order_id = int(data.get("id"))
        except (TypeError, ValueError):
            return web.json_response({"error": "Нет номера заказа"}, status=400)
        raw = data.get("items")
        if not isinstance(raw, list):
            return web.json_response({"error": "Нет состава заказа"}, status=400)
        wanted: dict[str, int] = {}
        for pair in raw[:50]:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                qty = int(pair[1])
            except (TypeError, ValueError):
                continue
            if qty > 0 and pair[0]:
                wanted[_text(pair[0], 64)] = min(qty, 999)
        if not wanted:
            return web.json_response(
                {"error": "Оставьте хотя бы одно блюдо. Убрать весь заказ — значит отменить его"},
                status=400)
        try:
            result, warnings = await on_edit_order_items(order_id, wanted)
        except on_manual_error as e:
            status = 404 if getattr(e, "code", "") == "NOT_FOUND" else 400
            return web.json_response({"error": str(e), "code": getattr(e, "code", "")}, status=status)
        except Exception as e:
            log.exception("не удалось изменить заказ %s: %s", order_id, e)
            return web.json_response({"error": "Не удалось изменить заказ"}, status=500)
        log.info("админка: состав заказа %s изменён", order_id)
        return web.json_response({"ok": True, "order": order_json(result["order"]),
                                  "warnings": warnings})

    # --- статистика ---
    @need_auth
    async def stats(request: web.Request) -> web.Response:
        days = max(1, min(int(request.query.get("days", 14) or 14), 365))
        since = int(time.time()) - days * 86400
        with db() as con:
            totals = con.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(total),0) revenue, COALESCE(AVG(total),0) avg "
                "FROM orders WHERE status != 'cancelled'").fetchone()
            today = con.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(total),0) revenue FROM orders "
                "WHERE status != 'cancelled' AND date(created,'unixepoch') = date('now')").fetchone()
            by_day = con.execute(
                "SELECT date(created,'unixepoch') d, COUNT(*) n, COALESCE(SUM(total),0) revenue "
                "FROM orders WHERE status != 'cancelled' AND created >= ? "
                "GROUP BY d ORDER BY d", (since,)).fetchall()
            by_status = {r["status"]: r["n"] for r in con.execute(
                "SELECT status, COUNT(*) n FROM orders GROUP BY status")}
            top_items: dict[str, dict] = {}
            for (items_json,) in con.execute(
                    "SELECT items FROM orders WHERE status != 'cancelled' AND created >= ?", (since,)):
                try:
                    for it in json.loads(items_json) or []:
                        t = it.get("title") or it.get("id") or "—"
                        rec = top_items.setdefault(t, {"title": t, "qty": 0, "sum": 0})
                        rec["qty"] += int(it.get("qty") or 0)
                        rec["sum"] += int(it.get("price") or 0) * int(it.get("qty") or 0)
                except (TypeError, ValueError):
                    continue
            top = sorted(top_items.values(), key=lambda x: x["sum"], reverse=True)[:8]
        return web.json_response({
            "totals": {"orders": totals["n"], "revenue": totals["revenue"],
                       "avg": round(totals["avg"])},
            "today": {"orders": today["n"], "revenue": today["revenue"]},
            "by_day": [{"date": r["d"], "orders": r["n"], "revenue": r["revenue"]} for r in by_day],
            "by_status": by_status,
            "top": top,
            "days": days,
        })

    # --- настройки доставки ---
    @need_auth
    async def save_settings(request: web.Request) -> web.Response:
        data = await body(request)
        allowed = {
            "city", "phone", "min_order", "delivery_price", "free_delivery_from",
            "delivery_days_ahead", "delivery_slots", "delivery_days", "delivery_zones",
            "payment_methods", "pickup", "storage", "brand", "slogan",
        }

        def apply(menu: dict) -> tuple[list[str], bool]:
            settings = menu.setdefault("settings", {})
            changed = []
            for key in allowed & set(data):
                value = data[key]
                if key in ("min_order", "delivery_price", "free_delivery_from", "delivery_days_ahead"):
                    value = _price(value)
                elif key in ("delivery_slots", "delivery_days", "payment_methods"):
                    value = [_text(x, 60) for x in (value or []) if _text(x, 60)][:40]
                elif key == "delivery_zones":
                    zones = []
                    for z in (value or [])[:20]:
                        if not isinstance(z, dict):
                            continue
                        zones.append({
                            "title": _text(z.get("title"), 200),
                            "free_from": _price(z.get("free_from")),
                            "price": _price(z.get("price")),
                            **({"min_sum": _price(z.get("min_sum"))} if z.get("min_sum") else {}),
                        })
                    value = [z for z in zones if z["title"]]
                else:
                    value = _text(value, 200)
                settings[key] = value
                changed.append(key)
            return changed, bool(changed)

        changed = await store.mutate(apply)
        log.info("админка: настройки обновлены (%s)", ", ".join(changed) or "без изменений")
        return web.json_response({"ok": True, "changed": changed})

    # --- статика админки ---
    def _no_cache_headers() -> dict[str, str]:
        return {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache", "Expires": "0"}

    async def admin_page(request: web.Request) -> web.StreamResponse:
        path = webapp_dir / "admin.html"
        try:
            version = str(int(path.stat().st_mtime))
        except OSError:
            version = "0"
        # Ссылка без версии → перенаправляем на адрес с версией файла. У каждой
        # сборки свой адрес, поэтому ни браузер, ни Cloudflare перед туннелем не
        # могут подсунуть старую страницу. Иначе исправления не доезжают, а
        # старая версия продолжает слать запросы неверным методом — это
        # выглядит как «исправление не помогло».
        if request.query.get("v") != version:
            params = {k: v for k, v in request.query.items() if k != "v"}
            params["v"] = version
            raise web.HTTPFound("/admin?" + urlencode(params), headers=_no_cache_headers())
        resp = web.FileResponse(path)
        resp.headers.update(_no_cache_headers())
        return resp

    app.router.add_get("/admin", admin_page)
    app.router.add_get("/admin/", admin_page)
    app.router.add_get("/admin.html", admin_page)
    app.router.add_post("/api/admin/login", login)
    app.router.add_post("/api/admin/logout", logout)
    app.router.add_get("/api/admin/csrf", csrf_endpoint)
    app.router.add_get("/api/admin/session", session_info)
    app.router.add_get("/api/admin/menu", get_menu)
    app.router.add_post("/api/admin/item", save_item)
    app.router.add_post("/api/admin/item/delete", delete_item)
    app.router.add_post("/api/admin/item/stock", set_stock)
    app.router.add_post("/api/admin/category", save_category)
    app.router.add_post("/api/admin/category/delete", delete_category)
    app.router.add_post("/api/admin/photo", upload_photo)
    app.router.add_get("/api/admin/orders", list_orders)
    app.router.add_post("/api/admin/order/status", set_status)
    if on_manual_order:
        app.router.add_post("/api/admin/order/create", create_order)
    if on_edit_order_items:
        app.router.add_post("/api/admin/order/items", edit_items)
    app.router.add_get("/api/admin/stats", stats)
    app.router.add_post("/api/admin/settings", save_settings)
    log.info("Админка доступна: /admin")
    return True


# ---------- вспомогательное ----------
STATUS_KEYS = ["new", "accepted", "assembled", "onway", "delivered", "cancelled"]
CLOSED_STATUSES = {"delivered", "cancelled"}
STATUS_TITLES = {
    "new": "Новый", "accepted": "Принят", "assembled": "Собран",
    "onway": "В пути", "delivered": "Доставлен", "cancelled": "Отменён",
}


def _status_list() -> list[dict]:
    return [{"key": k, "title": STATUS_TITLES[k]} for k in STATUS_KEYS]


async def _read_menu(store) -> dict:
    """Читает меню через store, не падая на битом JSON."""
    return store.read()


def _contact_label(row) -> str:
    """Откуда пришёл заказ: @ник, Telegram id или «сайт»."""
    keys = row.keys()
    username = row["username"] if "username" in keys else None
    user_id = row["user_id"] if "user_id" in keys else 0
    if username:
        return f"@{username}"
    if user_id:
        return f"Telegram id {user_id}"
    return "с сайта, без Telegram"


def order_json(row) -> dict:
    keys = row.keys()
    items = []
    try:
        items = json.loads(row["items"]) if row["items"] else []
    except (TypeError, ValueError):
        items = []
    return {
        "id": row["id"],
        "created": row["created"],
        "created_text": datetime.fromtimestamp(row["created"] or 0).strftime("%d.%m %H:%M"),
        "user_id": row["user_id"],
        "username": row["username"],
        "contact": _contact_label(row),
        "name": row["name"],
        "phone": row["phone"],
        "address": row["address"],
        "zone": row["zone"] if "zone" in keys else "",
        "date": row["date"],
        "slot": row["slot"],
        "payment": row["payment"],
        "comment": row["comment"],
        "items": items,
        "goods": row["goods"],
        "delivery": row["delivery"],
        "total": row["total"],
        "status": row["status"],
        "status_title": STATUS_TITLES.get(row["status"], row["status"]),
        "source": row["source"] if "source" in keys else None,
    }
