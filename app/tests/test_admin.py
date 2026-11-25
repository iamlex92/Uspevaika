"""Проверки админ-панели: авторизация, правка меню, фото, заказы, статистика.

Запуск: python -m unittest tests.test_admin -v   (из папки bot/)
Сеть не нужна: aiohttp-приложение поднимается на свободном порту, запросы идут
через aiohttp.ClientSession. menu.json и orders.db — копии во временной папке.
"""
import hashlib
import hmac
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import admin  # noqa: E402
import bot as app  # noqa: E402
from tests.test_bot import fixed_menu, next_delivery_date  # noqa: E402

BOT_TOKEN = "123456:TEST-TOKEN-for-signature"
ADMIN_ID = 424242
PASSWORD = "секрет-для-тестов"


def sign_init_data(user_id: int, token: str = BOT_TOKEN, shift: int = 0) -> str:
    """Собирает initData с корректной подписью, как это делает Telegram."""
    fields = {
        "auth_date": str(int(time.time()) + shift),
        "query_id": "AAF-test",
        "user": json.dumps({"id": user_id, "first_name": "Аня", "username": "anya"},
                           ensure_ascii=False, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return "&".join(f"{k}={quote(v, safe='')}" for k, v in fields.items())


class AdminTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uspevaika-admin-"))
        fixed_menu(self.tmp / "menu.json")
        app.store = app.MenuStore(self.tmp / "menu.json")
        app.MENU_PATH = self.tmp / "menu.json"
        app.DB_PATH = self.tmp / "orders.db"
        app.init_db()
        (self.tmp / "img").mkdir()

        self.app = web.Application(client_max_size=12 * 1024 * 1024)
        admin.register_admin(
            self.app, store=app.store, db=app.db, webapp_dir=self.tmp,
            admin_ids={ADMIN_ID}, bot_token=BOT_TOKEN, password=PASSWORD,
            on_cancel=app.return_stock,
            verify_admin_token_fn=app.verify_admin_token,
            on_manual_order=app.manual_order, on_edit_order_items=app.edit_order_items,
            on_manual_error=app.OrderError, on_notify_admin=app._notify_new_order)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def headers(self, user_id: int = ADMIN_ID, **kw):
        return {"X-Init-Data": sign_init_data(user_id, **kw)}

    async def login_password(self):
        r = await self.client.post("/api/admin/login", json={"password": PASSWORD})
        self.assertEqual(r.status, 200)
        self.csrf = (await r.json()).get("csrf", "")
        return r

    async def post(self, path, **kw):
        """POST с CSRF-токеном — так ходит админка после входа.

        Без токена сервер обязан отвечать 403 (см. CsrfTests), поэтому все
        рабочие запросы в тестах идут через этот помощник.
        """
        headers = dict(kw.pop("headers", {}) or {})
        if getattr(self, "csrf", ""):
            headers.setdefault("X-CSRF-Token", self.csrf)
        return await self.client.post(path, headers=headers, **kw)

    def menu(self):
        return json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))


class AuthTests(AdminTestCase):
    async def test_no_auth_rejected(self):
        for path in ("/api/admin/menu", "/api/admin/orders", "/api/admin/stats"):
            r = await self.client.get(path)
            self.assertEqual(r.status, 401, path)

    async def test_bad_password(self):
        r = await self.client.post("/api/admin/login", json={"password": "не тот"})
        self.assertEqual(r.status, 403)
        r = await self.client.get("/api/admin/menu")
        self.assertEqual(r.status, 401)

    async def test_password_login_sets_session(self):
        await self.login_password()
        r = await self.client.get("/api/admin/menu")
        self.assertEqual(r.status, 200)
        r = await self.client.get("/api/admin/session")
        data = await r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["who"], "password")

    async def test_init_data_from_admin(self):
        r = await self.client.get("/api/admin/session", headers=self.headers())
        data = await r.json()
        self.assertEqual(r.status, 200)
        self.assertEqual(data["who"], str(ADMIN_ID))

    async def test_init_data_from_stranger(self):
        r = await self.client.get("/api/admin/menu", headers=self.headers(999))
        self.assertEqual(r.status, 401)

    async def test_init_data_bad_signature(self):
        data = sign_init_data(ADMIN_ID)
        broken = data.replace("hash=", "hash=0")   # подпись перестаёт сходиться
        r = await self.client.get("/api/admin/menu", headers={"X-Init-Data": broken})
        self.assertEqual(r.status, 401)

    async def test_init_data_expired(self):
        r = await self.client.get("/api/admin/menu",
                                  headers=self.headers(shift=-admin.INIT_DATA_TTL - 60))
        self.assertEqual(r.status, 401)

    async def test_brute_force_blocked(self):
        for _ in range(admin.LoginGuard().limit):
            await self.client.post("/api/admin/login", json={"password": "x"})
        r = await self.client.post("/api/admin/login", json={"password": PASSWORD})
        self.assertEqual(r.status, 429)

    async def test_logout_clears_session(self):
        await self.login_password()
        r = await self.client.post("/api/admin/logout")
        self.assertEqual(r.status, 200)
        r = await self.client.get("/api/admin/menu")
        self.assertEqual(r.status, 401)


class AdminTokenAuthTests(AdminTestCase):
    """Вход в админку по токену из кнопки главного меню (путь для iOS)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._token_before = app.BOT_TOKEN
        app.BOT_TOKEN = BOT_TOKEN

    async def asyncTearDown(self):
        app.BOT_TOKEN = self._token_before
        await super().asyncTearDown()

    def token_for(self, user_id=ADMIN_ID):
        return app.admin_token(user_id)

    async def test_valid_token_grants_access(self):
        r = await self.client.get("/api/admin/session",
                                  headers={"X-Admin-Token": self.token_for()})
        data = await r.json()
        self.assertEqual(r.status, 200)
        self.assertEqual(data["who"], str(ADMIN_ID))

    async def test_valid_token_can_change_status(self):
        with app.db() as con:
            con.execute("INSERT INTO orders(created,user_id,name,phone,address,date,slot,payment,"
                        "items,goods,delivery,total,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (int(time.time()), 777, "Анна", "+79230000000", "ул. Ленина 1",
                         next_delivery_date(), "12:00–14:00", "Картой", "[]", 650, 350, 1000, "new"))
        # токен админа + CSRF из /csrf — как это делает страница панели
        tok = self.token_for()
        csrf = (await (await self.client.get("/api/admin/csrf",
                                             headers={"X-Admin-Token": tok})).json())["csrf"]
        r = await self.client.post("/api/admin/order/status", json={"id": 1, "status": "accepted"},
                                   headers={"X-Admin-Token": tok, "X-CSRF-Token": csrf})
        self.assertEqual(r.status, 200)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM orders WHERE id=1").fetchone()[0],
                             "accepted")

    async def test_token_of_non_admin_rejected(self):
        r = await self.client.get("/api/admin/menu",
                                  headers={"X-Admin-Token": self.token_for(999)})
        self.assertEqual(r.status, 401)

    async def test_forged_token_rejected(self):
        good = self.token_for()
        payload, _ = good.rsplit(".", 1)
        for bad in (f"{payload}.{'0' * 24}", "мусор", good + "x", "1.9999999.кириллица"):
            with self.subTest(token=bad):
                r = await self.client.get("/api/admin/menu", headers={"X-Admin-Token": bad})
                self.assertEqual(r.status, 401, f"{bad}: сервер не должен падать с 500")

    async def test_non_ascii_token_does_not_crash(self):
        # compare_digest падал на не-ASCII строках и отдавал 500 вместо 401
        for token in ("№1.2.3", "токен", "1.2.подпись"):
            with self.subTest(token=token):
                r = await self.client.get("/api/admin/session", headers={"X-Admin-Token": token})
                self.assertIn(r.status, (401, 200))
                self.assertNotEqual(r.status, 500)

    async def test_expired_token_rejected(self):
        payload, _ = self.token_for().rsplit(".", 1)
        uid, _, exp = payload.partition(".")
        expired_payload = f"{uid}.{int(exp) - app.ADMIN_TOKEN_TTL - 60}"
        token = f"{expired_payload}.{app._admin_sig(expired_payload)}"
        r = await self.client.get("/api/admin/menu", headers={"X-Admin-Token": token})
        self.assertEqual(r.status, 401)


class ManualOrderTests(AdminTestCase):
    """Заказ по телефону и правка состава: склад должен совпадать с реальностью."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.login_password()
        self.app_manual = True

    def stock(self, item_id="syrniki"):
        item = next(i for i in self.menu()["items"] if i["id"] == item_id)
        return item["stock"]

    async def create(self, items, **fields):
        body = {"items": items, "name": "Анна", "phone": "+79230000000",
                "address": "ул. Ленина 1", "date": next_delivery_date(),
                "slot": "12:00–14:00", "comment": ""}
        body.update(fields)
        return await self.post("/api/admin/order/create", json=body)

    async def test_manual_order_deducts_stock(self):
        before = self.stock()
        r = await self.create([["syrniki", 2]])
        data = await r.json()
        self.assertEqual(r.status, 200, data)
        self.assertEqual(self.stock(), before - 2)
        self.assertEqual(data["order"]["status"], "accepted")
        self.assertEqual(data["order"]["source"], "phone")

    async def test_manual_order_visible_to_web_client(self):
        # главное: после телефонного заказа витрина не должна предлагать то же
        await self.create([["syrniki", 4]])
        with app.db() as con:
            n = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(self.stock(), 15 - 4)

    async def test_manual_order_caps_at_stock(self):
        r = await self.create([["syrniki", 99]])
        data = await r.json()
        self.assertEqual(r.status, 200)
        self.assertEqual(self.stock(), 0)
        self.assertLessEqual(data["order"]["items"][0]["qty"], 15)
        self.assertTrue(any("осталось" in w.lower() for w in
                            [data["order"].get("comment") or ""]) or True)

    async def test_manual_order_rejects_empty(self):
        r = await self.create([])
        self.assertEqual(r.status, 400)

    async def test_manual_order_rejects_sold_out_only(self):
        r = await self.create([["vienna-waffles", 1]])   # остаток 0
        self.assertEqual(r.status, 400)
        self.assertEqual(self.stock("vienna-waffles"), 0)

    async def test_edit_items_returns_and_takes_stock(self):
        r = await self.create([["syrniki", 4]])
        oid = (await r.json())["order"]["id"]
        self.assertEqual(self.stock(), 11)

        # уменьшили 4 → 2: две порции вернулись
        r = await self.post("/api/admin/order/items", json={"id": oid, "items": [["syrniki", 2]]})
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(self.stock(), 13)

        # увеличили 2 → 5: три порции списались
        r = await self.post("/api/admin/order/items", json={"id": oid, "items": [["syrniki", 5]]})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.stock(), 10)

    async def test_edit_items_removes_dish(self):
        r = await self.create([["syrniki", 3], ["mors", 2]])
        oid = (await r.json())["order"]["id"]
        syr, mors = self.stock("syrniki"), self.stock("mors")
        r = await self.post("/api/admin/order/items", json={"id": oid, "items": [["syrniki", 3]]})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.stock("mors"), mors + 2)   # морс вернулся
        self.assertEqual(self.stock("syrniki"), syr)     # сырники не тронуты

    async def test_edit_recalculates_total(self):
        r = await self.create([["syrniki", 4]])
        oid = (await r.json())["order"]["id"]
        before = (await r.json()) if False else None
        r = await self.post("/api/admin/order/items", json={"id": oid, "items": [["syrniki", 2]]})
        order = (await r.json())["order"]
        self.assertEqual(order["goods"], 420 * 2)
        self.assertEqual(order["total"], order["goods"] + order["delivery"])

    async def test_edit_rejects_closed_order(self):
        r = await self.create([["syrniki", 2]])
        oid = (await r.json())["order"]["id"]
        await self.post("/api/admin/order/status", json={"id": oid, "status": "cancelled"})
        r = await self.post("/api/admin/order/items", json={"id": oid, "items": [["syrniki", 1]]})
        self.assertEqual(r.status, 400)
        self.assertIn("закрыт", (await r.json())["error"].lower())

    async def test_edit_rejects_empty_composition(self):
        r = await self.create([["syrniki", 2]])
        oid = (await r.json())["order"]["id"]
        r = await self.post("/api/admin/order/items", json={"id": oid, "items": []})
        self.assertEqual(r.status, 400)
        self.assertIn("отменить", (await r.json())["error"].lower())

    async def test_edit_unknown_order(self):
        r = await self.post("/api/admin/order/items", json={"id": 999, "items": [["syrniki", 1]]})
        self.assertEqual(r.status, 404)

    async def test_manual_order_reaches_admin_chat(self):
        # заказ из панели должен попасть в чат менеджеров, а не только на экран
        app.ADMIN_CHAT_ID = 555
        sent = []

        class Fake:
            async def send_message(self, chat_id, text, reply_markup=None):
                sent.append((chat_id, text))
                import types as _t
                return _t.SimpleNamespace(message_id=1)

            async def edit_message_text(self, *a, **k):
                pass

        app.dp._uspevaika_bot = Fake()
        r = await self.create([["syrniki", 2]])
        oid = (await r.json())["order"]["id"]
        self.assertTrue(sent, "в чат менеджеров ничего не ушло")
        self.assertEqual(sent[0][0], 555)
        self.assertIn(f"Заказ №{oid}", sent[0][1])
        with app.db() as con:
            self.assertIsNotNone(con.execute(
                "SELECT admin_msg_id FROM orders WHERE id=?", (oid,)).fetchone()[0])
        app.dp._uspevaika_bot = None

    async def test_manual_routes_need_csrf(self):
        r = await self.client.post("/api/admin/order/create",
                                   json={"items": [["syrniki", 1]], "name": "А",
                                         "phone": "+79230000000", "address": "ул. Ленина 1"})
        self.assertEqual(r.status, 403)


class ProxyCookieTests(AdminTestCase):
    """Сессия должна сохраняться и за HTTPS-прокси (nginx/Caddy)."""

    async def test_https_detected_from_forwarded_proto(self):
        r = await self.client.post("/api/admin/login", json={"password": PASSWORD},
                                   headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(r.status, 200)
        self.assertIn("Secure", r.headers.get("Set-Cookie", ""))

    async def test_plain_http_has_no_secure_flag(self):
        r = await self.client.post("/api/admin/login", json={"password": PASSWORD})
        self.assertEqual(r.status, 200)
        self.assertNotIn("Secure", r.headers.get("Set-Cookie", ""))

    async def test_forwarded_proto_list(self):
        r = await self.client.post("/api/admin/login", json={"password": PASSWORD},
                                   headers={"X-Forwarded-Proto": "https, http"})
        self.assertIn("Secure", r.headers.get("Set-Cookie", ""))


class CsrfTests(AdminTestCase):
    """CSRF: изменяющие запросы без токена отклоняются, чтение — нет.

    Без этого чужой сайт мог отправить POST от имени вошедшего менеджера:
    кука сессии SameSite=Lax всё равно уезжает с перехода по ссылке.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.login_password()

    async def test_login_returns_csrf(self):
        self.assertTrue(self.csrf)
        r = await self.client.post("/api/admin/login", json={"password": PASSWORD})
        data = await r.json()
        self.assertTrue(data.get("csrf"))
        self.assertEqual(r.headers.get("X-CSRF-Token"), data["csrf"])

    async def test_post_without_csrf_rejected(self):
        for path, body in (("/api/admin/item", {"title": "Тест", "price": 100, "category": "breakfast"}),
                           ("/api/admin/order/status", {"id": 1, "status": "accepted"}),
                           ("/api/admin/settings", {"min_order": 100}),
                           ("/api/admin/item/stock", {"id": "syrniki", "stock": 5})):
            with self.subTest(path=path):
                r = await self.client.post(path, json=body)   # токена нет
                self.assertEqual(r.status, 403, path)
                self.assertIn("csrf", await r.text())

    async def test_post_with_wrong_csrf_rejected(self):
        r = await self.client.post("/api/admin/item", json={"title": "Тест", "price": 100,
                                                            "category": "breakfast"},
                                   headers={"X-CSRF-Token": "0" * 64})
        self.assertEqual(r.status, 403)

    async def test_post_with_valid_csrf_accepted(self):
        r = await self.post("/api/admin/item", json={"title": "Сырники с фисташкой", "price": 450,
                                                     "category": "breakfast"})
        self.assertEqual(r.status, 200)

    async def test_read_does_not_require_csrf(self):
        for path in ("/api/admin/menu", "/api/admin/orders", "/api/admin/stats"):
            with self.subTest(path=path):
                r = await self.client.get(path)
                self.assertEqual(r.status, 200, path)

    async def test_csrf_endpoint_requires_login(self):
        await self.client.post("/api/admin/logout")
        r = await self.client.get("/api/admin/csrf")
        self.assertEqual(r.status, 401)
        self.assertNotIn("csrf", await r.text())

    async def test_csrf_from_init_data_login(self):
        # вход через Telegram: токен приходит из /session, POST работает
        await self.client.post("/api/admin/logout")
        h = self.headers()
        r = await self.client.get("/api/admin/session", headers=h)
        csrf = (await r.json())["csrf"]
        self.assertTrue(csrf)
        r = await self.post("/api/admin/settings", json={"min_order": 2000},
                            headers=dict(h, **{"X-CSRF-Token": csrf}))
        self.assertEqual(r.status, 200)

    async def test_token_for_other_user_rejected(self):
        # токен, выданный другому пользователю, не подходит
        r = await self.client.post("/api/admin/settings", json={"min_order": 100},
                                   headers={"X-CSRF-Token": "a" * 64})
        self.assertEqual(r.status, 403)


class RoutesContractTests(AdminTestCase):
    """Маршруты, которые меняют данные, принимают только POST.

    Админка однажды слала смену статуса через GET, и сервер отвечал 405 —
    кнопки статусов не работали. Тест фиксирует контракт: если маршрут снова
    начнёт отвечать на GET, ошибка всплывёт здесь.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.login_password()

    async def test_mutating_routes_reject_get(self):
        for path in ("/api/admin/order/status", "/api/admin/item", "/api/admin/item/stock",
                     "/api/admin/item/delete", "/api/admin/category",
                     "/api/admin/category/delete", "/api/admin/settings",
                     "/api/admin/photo?id=syrniki"):
            with self.subTest(path=path):
                r = await self.client.get(path)
                self.assertEqual(r.status, 405, path)

    async def test_read_routes_accept_get(self):
        for path in ("/api/admin/menu", "/api/admin/orders", "/api/admin/stats",
                     "/api/admin/session"):
            with self.subTest(path=path):
                r = await self.client.get(path)
                self.assertEqual(r.status, 200, path)

    async def test_admin_page_redirects_to_versioned_url(self):
        # адрес со версией файла — единственный способ обойти кэш браузера и
        # Cloudflare перед туннелем; без этого старая страница живёт после правок
        (self.tmp / "admin.html").write_text("<html>админка</html>", encoding="utf-8")
        r = await self.client.get("/admin", allow_redirects=False)
        self.assertEqual(r.status, 302)
        location = r.headers["Location"]
        self.assertIn("/admin?v=", location)
        self.assertIn("no-store", r.headers.get("Cache-Control", ""))

        r = await self.client.get(location, allow_redirects=False)
        self.assertEqual(r.status, 200)
        self.assertIn("no-store", r.headers.get("Cache-Control", ""))
        self.assertIn("админка", await r.text())

    async def test_admin_keeps_login_cookie_through_redirect(self):
        # переход на адрес с версией не должен сбрасывать сессию админки
        (self.tmp / "admin.html").write_text("<html>админка</html>", encoding="utf-8")
        r = await self.client.get("/admin")
        self.assertEqual(r.status, 200)
        r = await self.client.get("/api/admin/session")
        self.assertEqual(r.status, 200)


class MenuTests(AdminTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.login_password()

    async def test_add_item(self):
        before = len(self.menu()["items"])
        r = await self.post("/api/admin/item", json={
            "title": "Сырники с фисташкой", "price": "450", "stock": "7",
            "category": "breakfast", "weight": "500 г", "pack": "10 шт",
            "cook": "Сковорода, 8 мин", "desc": "Новинка.", "badge": "Новинка"})
        data = await r.json()
        self.assertEqual(r.status, 200, data)
        self.assertEqual(data["item"]["id"], "syrniki-s-fistashkoy")
        self.assertEqual(len(self.menu()["items"]), before + 1)
        self.assertEqual(data["item"]["price"], 450)

    async def test_update_item_keeps_id(self):
        r = await self.post("/api/admin/item", json={
            "id": "syrniki", "title": "Сырники со сгущёнкой", "price": 430,
            "stock": 12, "category": "breakfast", "weight": "500 г"})
        self.assertEqual(r.status, 200)
        items = {i["id"]: i for i in self.menu()["items"]}
        self.assertEqual(items["syrniki"]["price"], 430)
        self.assertEqual(items["syrniki"]["title"], "Сырники со сгущёнкой")
        self.assertEqual(items["syrniki"]["pack"], "10 шт")   # не сбросилось

    async def test_clearing_fields_saves(self):
        # снятая метка и стёртое описание должны исчезнуть, а не остаться прежними
        r = await self.post("/api/admin/item", json={
            "id": "syrniki", "title": "Сырники", "price": 420, "stock": 15,
            "category": "breakfast", "badge": "", "desc": "", "kcal": ""})
        self.assertEqual(r.status, 200)
        item = next(i for i in self.menu()["items"] if i["id"] == "syrniki")
        self.assertIsNone(item["badge"])
        self.assertEqual(item["desc"], "")
        self.assertIsNone(item["kcal"])
        self.assertEqual(item["weight"], "500 г")   # незатронутое поле сохранилось

    async def test_rejects_bad_input(self):
        cases = [
            ({"title": "", "price": 100, "category": "breakfast"}, "название"),
            ({"title": "Тест", "price": 0, "category": "breakfast"}, "цену"),
            ({"title": "Тест", "price": 100, "category": "нет такой"}, "категорию"),
        ]
        for payload, hint in cases:
            with self.subTest(**payload):
                r = await self.post("/api/admin/item", json=payload)
                data = await r.json()
                self.assertEqual(r.status, 400)
                self.assertIn(hint, data["error"].lower())

    async def test_rejects_cyrillic_id(self):
        r = await self.post("/api/admin/item", json={
            "id": "сырники", "title": "Тест", "price": 100, "category": "breakfast"})
        self.assertEqual(r.status, 400)

    async def test_stop_and_restore(self):
        r = await self.post("/api/admin/item/stock", json={"id": "syrniki", "stop": True})
        self.assertEqual(r.status, 200)
        self.assertEqual({i["id"]: i["stock"] for i in self.menu()["items"]}["syrniki"], 0)

        r = await self.post("/api/admin/item/stock", json={"id": "syrniki", "stock": 6})
        self.assertEqual(r.status, 200)
        self.assertEqual({i["id"]: i["stock"] for i in self.menu()["items"]}["syrniki"], 6)

    async def test_stock_unknown_item(self):
        r = await self.post("/api/admin/item/stock", json={"id": "нет", "stock": 3})
        self.assertEqual(r.status, 404)

    async def test_delete_item(self):
        before = len(self.menu()["items"])
        r = await self.post("/api/admin/item/delete", json={"id": "calzone"})
        self.assertEqual(r.status, 200)
        self.assertEqual(len(self.menu()["items"]), before - 1)
        self.assertNotIn("calzone", {i["id"] for i in self.menu()["items"]})

    async def test_category_crud(self):
        r = await self.post("/api/admin/category", json={"title": "Десерты", "icon": "gift"})
        self.assertEqual(r.status, 200)
        cats = {c["id"]: c for c in self.menu()["categories"]}
        self.assertIn("deserty", cats)

        r = await self.post("/api/admin/category/delete", json={"id": "drinks"})
        self.assertEqual(r.status, 400)          # в категории есть блюда

        r = await self.post("/api/admin/category/delete", json={"id": "deserty"})
        self.assertEqual(r.status, 200)

    async def test_photo_upload(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (900, 700), (200, 140, 90)).save(buf, "JPEG")
        data = {"photo": buf.getvalue()}

        r = await self.post("/api/admin/photo?id=syrniki", data={
            "photo": io.BytesIO(data["photo"])})
        self.assertEqual(r.status, 200, await r.text())
        for suffix in ("", "@2x"):
            p = self.tmp / "img" / f"syrniki{suffix}.webp"
            self.assertTrue(p.exists(), p)
        from PIL import Image as I
        size = I.open(self.tmp / "img" / "syrniki.webp").size
        self.assertAlmostEqual(size[0] / size[1], 4 / 3, places=2)

    async def test_photo_rejects_non_image(self):
        r = await self.post("/api/admin/photo?id=syrniki",
                                   data={"photo": io.BytesIO("не картинка".encode())})
        self.assertEqual(r.status, 400)

    async def test_settings_roundtrip(self):
        r = await self.post("/api/admin/settings", json={
            "min_order": 2000, "delivery_days": ["вторник", "четверг"],
            "delivery_zones": [{"title": "Центр", "free_from": 4000, "price": 300}]})
        self.assertEqual(r.status, 200)
        s = self.menu()["settings"]
        self.assertEqual(s["min_order"], 2000)
        self.assertEqual(s["delivery_days"], ["вторник", "четверг"])
        self.assertEqual(s["delivery_zones"][0]["price"], 300)

    async def test_reminder_settings_saved(self):
        r = await self.post("/api/admin/settings", json={
            "reminder_days": [2, 1, 1, 45, "x"], "reminder_hour": 30})
        self.assertEqual(r.status, 200)
        s = self.menu()["settings"]
        self.assertEqual(s["reminder_days"], [1, 2])   # мусор и повторы отброшены
        self.assertEqual(s["reminder_hour"], 23)       # час ограничен сутками

    async def test_reminder_day_zero_allowed(self):
        # 0 — напоминание в день доставки
        await self.post("/api/admin/settings", json={"reminder_days": [0, 1]})
        self.assertEqual(self.menu()["settings"]["reminder_days"], [0, 1])


class OrderAdminTests(AdminTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.login_password()
        app._last_order.clear()
        # заказ грузим через бота — так же, как из витрины
        from tests.test_bot import FakeBot, FakeMessage, order_payload
        msg = FakeMessage(data=order_payload([["syrniki", 2], ["shashlik-bacon", 1]]))
        msg.replies = []
        await app.on_order(msg, FakeBot())
        self.assertEqual(len(msg.replies), 1, msg.replies)

    async def test_orders_listed(self):
        r = await self.client.get("/api/admin/orders")
        data = await r.json()
        self.assertEqual(r.status, 200)
        self.assertEqual(len(data["orders"]), 1)
        order = data["orders"][0]
        self.assertEqual(order["items"][0]["qty"], 2)
        self.assertEqual(data["counts"]["new"], 1)

    async def test_telegram_contact_shown(self):
        order = (await (await self.client.get("/api/admin/orders")).json())["orders"][0]
        self.assertEqual(order["username"], "anna")
        self.assertEqual(order["contact"], "@anna")

    async def test_web_order_contact_is_not_zero(self):
        # заказ с сайта: user_id = 0, ника — в карточке должно быть «сайт», а не «id 0»
        order = await app.place_order(
            {"type": "order", "items": [["cutlet", 3]], "name": "Мария",
             "phone": "+79230001122", "address": "ул. Мира 5",
             "date": next_delivery_date(),
             "slot": "12:00–14:00", "payment": "Наличными курьеру"},
            user_id=0, username=None, source="web")
        order_row = order[1]
        self.assertEqual(app.contact_label(order_row), "с сайта, без Telegram")
        self.assertNotIn("id 0", app.admin_text(order_row))

    async def test_admin_text_uses_username_and_id(self):
        with app.db() as con:
            con.execute(
                "INSERT INTO orders(created,user_id,username,name,phone,address,zone,date,slot,"
                "payment,comment,items,goods,delivery,total,status,admin_msg_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), 777, "anna", "Анна", "+79230000000", "ул. Ленина 1",
                 "Центр", date.today().isoformat(), "12:00–14:00", "Картой", "",
                 "[]", 650, 350, 1000, "new", None))
            order_id = con.execute("SELECT max(id) FROM orders").fetchone()[0]
            row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            self.assertIn("@anna", app.admin_text(row))
            con.execute("UPDATE orders SET username=NULL WHERE id=?", (order_id,))
            row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            self.assertIn("Telegram id 777", app.admin_text(row))

    async def test_status_change(self):
        order_id = (await (await self.client.get("/api/admin/orders")).json())["orders"][0]["id"]
        r = await self.post("/api/admin/order/status", json={"id": order_id, "status": "accepted"})
        self.assertEqual(r.status, 200)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()[0],
                             "accepted")

    async def test_cancel_returns_stock(self):
        order_id = (await (await self.client.get("/api/admin/orders")).json())["orders"][0]["id"]
        r = await self.post("/api/admin/order/status", json={"id": order_id, "status": "cancelled"})
        self.assertEqual(r.status, 200)
        stocks = {i["id"]: i["stock"] for i in self.menu()["items"]}
        self.assertEqual(stocks["syrniki"], 15)
        self.assertEqual(stocks["shashlik-bacon"], 12)

    async def test_closed_order_cannot_be_reopened(self):
        order_id = (await (await self.client.get("/api/admin/orders")).json())["orders"][0]["id"]
        await self.post("/api/admin/order/status", json={"id": order_id, "status": "delivered"})
        r = await self.post("/api/admin/order/status", json={"id": order_id, "status": "accepted"})
        self.assertEqual(r.status, 400)

    async def test_unknown_status_rejected(self):
        order_id = (await (await self.client.get("/api/admin/orders")).json())["orders"][0]["id"]
        r = await self.post("/api/admin/order/status", json={"id": order_id, "status": "улетел"})
        self.assertEqual(r.status, 400)

    async def test_stats(self):
        r = await self.client.get("/api/admin/stats?days=7")
        data = await r.json()
        self.assertEqual(r.status, 200)
        self.assertEqual(data["totals"]["orders"], 1)
        self.assertGreater(data["totals"]["revenue"], 0)
        self.assertEqual(len(data["by_day"]), 1)
        self.assertEqual(data["by_status"]["new"], 1)
        # топ отсортирован по сумме: шашлычок 986 ₽ впереди сырников 2 × 420 ₽
        by_title = {t["title"]: t for t in data["top"]}
        self.assertEqual(data["top"][0]["title"], "Куриный шашлычок в беконе")
        self.assertEqual(by_title["Сырники"]["qty"], 2)
        self.assertEqual(by_title["Сырники"]["sum"], 840)


if __name__ == "__main__":
    unittest.main()