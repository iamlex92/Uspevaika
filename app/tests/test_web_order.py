"""Заказ из браузера: POST /api/order пишет в базу и уведомляет менеджера.

Проверяем то, чего не видно в тестах бота: HTTP-эндпоинт витрины, расчёт суммы
на сервере, списание остатков и то, что админ получает сообщение о новом заказе.
"""
import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import admin  # noqa: E402
import bot as app  # noqa: E402
from tests.test_bot import fixed_menu, next_delivery_date  # noqa: E402
from tests.test_admin import sign_init_data  # noqa: E402

PASSWORD = "пароль-админа"
BOT_TOKEN = "123:TEST"


class FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self.edited = []
        self.fail = fail

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("telegram недоступен")
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return types.SimpleNamespace(message_id=100 + len(self.sent))

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})


def payload(items, **kw):
    data = {
        "type": "order", "items": items, "name": "Анна", "phone": "+79230000000",
        "address": "ул. Ленина 1, кв. 5", "zone": "Железнодорожный, Советский, Центральный",
        "date": next_delivery_date(),
        "slot": "12:00–14:00", "payment": "Наличными курьеру", "comment": "", "total": 1,
    }
    data.update(kw)
    return data


class WebOrderBase(unittest.IsolatedAsyncioTestCase):
    """Общий стенд: временные menu.json и orders.db, aiohttp-приложение, поддельный бот."""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uspevaika-web-"))
        fixed_menu(self.tmp / "menu.json")
        app.store = app.MenuStore(self.tmp / "menu.json")
        app.MENU_PATH = self.tmp / "menu.json"
        app.DB_PATH = self.tmp / "orders.db"
        app.ADMIN_CHAT_ID = 555
        app.ADMIN_IDS = {1}
        app.BOT_TOKEN = BOT_TOKEN
        app._last_order.clear()
        app._ORDER_IPS.clear()
        app.init_db()

        self.fake = FakeBot()
        app.dp._uspevaika_bot = self.fake        # тот же бот, что в main()

        webapp = self.tmp / "webapp"
        (webapp / "img").mkdir(parents=True)
        self.app = web.Application(client_max_size=12 * 1024 * 1024)
        self.app.router.add_post("/api/order", app.serve_order)
        self.app.router.add_get("/menu.json", app.serve_menu)
        self.app.router.add_get("/img/{path:.*}", app.serve_img)
        self.app.router.add_get("/healthz", app.serve_health)
        admin.register_admin(
            self.app, store=app.store, db=app.db, webapp_dir=webapp,
            admin_ids=app.ADMIN_IDS, bot_token="123:TEST", password=PASSWORD,
            on_status_change=app._notify_status,     # как в make_web_app
            on_cancel=app.return_stock)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        app.dp._uspevaika_bot = None
        app.BOT_TOKEN = ""
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def admin_login(self):
        r = await self.client.post("/api/admin/login", json={"password": PASSWORD})
        self.assertEqual(r.status, 200)
        self.csrf = (await r.json()).get("csrf", "")

    async def admin_post(self, path, **kw):
        """POST в админку с CSRF-токеном — как это делает страница панели."""
        headers = dict(kw.pop("headers", {}) or {})
        headers.setdefault("X-CSRF-Token", getattr(self, "csrf", ""))
        return await self.client.post(path, headers=headers, **kw)

    def orders(self):
        import sqlite3
        con = sqlite3.connect(self.tmp / "orders.db")
        con.row_factory = sqlite3.Row
        try:
            return con.execute("SELECT * FROM orders ORDER BY id").fetchall()
        finally:
            con.close()

    def stocks(self):
        return {i["id"]: i["stock"]
                for i in json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))["items"]}


class WebOrderTests(WebOrderBase):
    async def test_order_saved_to_db(self):
        r = await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]))
        data = await r.json()
        self.assertEqual(r.status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["order_id"], 1)
        self.assertEqual(data["goods"], 420 * 2 + 986)
        self.assertEqual(data["total"], data["goods"] + 350)

        rows = self.orders()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "new")
        self.assertEqual(rows[0]["name"], "Анна")
        self.assertEqual(json.loads(rows[0]["items"])[0]["qty"], 2)

    async def test_stock_is_reserved(self):
        await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]))
        self.assertEqual(self.stocks()["syrniki"], 13)

    async def test_server_recomputes_total(self):
        # клиент присылает заниженный total — сервер считает сам
        r = await self.client.post("/api/order", json=payload([["set-mom", 1]], total=1))
        data = await r.json()
        self.assertEqual(data["total"], 3000)
        self.assertEqual(data["delivery"], 0)      # от 3000 — бесплатно

    async def test_admin_notified(self):
        await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]))
        self.assertEqual(len(self.fake.sent), 1)
        sent = self.fake.sent[0]
        self.assertEqual(sent["chat_id"], 555)
        self.assertIn("Заказ №1", sent["text"])
        self.assertIn("Анна", sent["text"])
        self.assertIsNotNone(sent["markup"])       # кнопки статусов
        self.assertIsNotNone(self.orders()[0]["admin_msg_id"])

    async def test_falls_back_to_admins_without_group_chat(self):
        app.ADMIN_CHAT_ID = 0
        await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]))
        self.assertEqual(self.fake.sent[0]["chat_id"], 1)   # личка админа

    async def test_order_saved_when_telegram_down(self):
        app.dp._uspevaika_bot = FakeBot(fail=True)
        r = await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]))
        self.assertEqual(r.status, 200)            # заказ важнее уведомления
        self.assertEqual(len(self.orders()), 1)

    async def test_works_without_bot_at_all(self):
        app.dp._uspevaika_bot = None
        r = await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]))
        self.assertEqual(r.status, 200)
        self.assertEqual(len(self.orders()), 1)

    async def test_rejects_short_order(self):
        r = await self.client.post("/api/order", json=payload([["mors", 1]]))
        data = await r.json()
        self.assertEqual(r.status, 400)
        self.assertIn("минимальной", data["error"])
        self.assertEqual(self.orders(), [])
        self.assertEqual(self.stocks()["mors"], 14)   # остатки не тронуты
        self.assertEqual(self.fake.sent, [])

    async def test_rejects_bad_contact(self):
        r = await self.client.post("/api/order", json=payload([["syrniki", 4]], phone="123"))
        self.assertEqual(r.status, 400)
        self.assertEqual(self.orders(), [])

    async def test_rejects_empty_cart_and_garbage(self):
        r = await self.client.post("/api/order", json=payload([]))
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/order", data="не json",
                                   headers={"Content-Type": "application/json"})
        self.assertEqual(r.status, 400)

    async def test_rate_limit(self):
        # наборов на складе 20, лимит заказов с адреса — 8: все пройдут проверки,
        # и девятый должен упереться именно в ограничение
        body = payload([["set-mom", 1]])
        for i in range(app.WEB_ORDER_LIMIT):
            r = await self.client.post("/api/order", json=body)
            self.assertEqual(r.status, 200, f"заказ {i + 1}: {await r.text()}")
        r = await self.client.post("/api/order", json=body)
        self.assertEqual(r.status, 429)
        self.assertEqual(len(self.orders()), app.WEB_ORDER_LIMIT)

    async def test_healthz_reports_version(self):
        # по этой версии автообновление понимает, что уже установлено
        r = await self.client.get("/healthz")
        data = await r.json()
        self.assertTrue(data["ok"])
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+$")

    async def test_images_are_cached(self):
        # без кэша браузер перепроверяет фото при каждой перерисовке карточки
        # и меню заметно моргает
        r = await self.client.get("/img/fallback.webp")
        self.assertEqual(r.status, 200)
        self.assertIn("max-age", r.headers.get("Cache-Control", ""))

    async def test_image_path_traversal_blocked(self):
        for bad in ("../bot.py", "..%2Fbot.py", "../../etc/passwd"):
            with self.subTest(path=bad):
                r = await self.client.get(f"/img/{bad}")
                self.assertIn(r.status, (400, 404))

    async def test_menu_serves_server_dates(self):
        # даты считает сервер: часы устройства могут быть в другом часовом поясе
        r = await self.client.get("/menu.json")
        data = await r.json()
        dates = data["settings"]["server_dates"]
        self.assertTrue(dates)
        allowed = [x.lower() for x in data["settings"]["delivery_days"]]
        for iso in dates:
            weekday = app.WEEKDAYS_RU[date.fromisoformat(iso).weekday()]
            self.assertIn(weekday, allowed)

    async def test_menu_dates_survive_broken_menu(self):
        (self.tmp / "menu.json").write_text("{ битый", encoding="utf-8")
        r = await self.client.get("/menu.json")
        self.assertEqual(r.status, 503)

    async def test_order_visible_in_admin_panel(self):
        await self.client.post("/api/order", json=payload([["cutlet", 2], ["shashlik-bacon", 1]]))
        await self.admin_login()
        r = await self.client.get("/api/admin/orders")
        data = await r.json()
        self.assertEqual(r.status, 200)
        self.assertEqual(len(data["orders"]), 1)
        order = data["orders"][0]
        self.assertEqual(order["status"], "new")
        self.assertEqual(order["name"], "Анна")
        titles = {i["title"] for i in order["items"]}
        self.assertIn("Куриные котлеты классические", titles)

    async def test_cancel_from_admin_returns_web_stock(self):
        await self.client.post("/api/order", json=payload([["syrniki", 3], ["shashlik-bacon", 1]]))
        self.assertEqual(self.stocks()["syrniki"], 12)
        await self.admin_login()
        r = await self.admin_post("/api/admin/order/status", json={"id": 1, "status": "cancelled"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.stocks()["syrniki"], 15)


class TelegramIdentityTests(WebOrderBase):
    """Заказ из Telegram, ушедший не через sendData, сохраняет Telegram id клиента."""

    CLIENT_ID = 987654321

    def init_data(self, user_id=None, token=BOT_TOKEN):
        return sign_init_data(user_id or self.CLIENT_ID, token=token)

    async def test_init_data_from_header_saves_user_id(self):
        r = await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]),
                                   headers={"X-Init-Data": self.init_data()})
        data = await r.json()
        self.assertEqual(r.status, 200, data)
        self.assertTrue(data["telegram"])

        row = self.orders()[0]
        self.assertEqual(row["user_id"], self.CLIENT_ID)
        self.assertEqual(row["username"], "anya")

    async def test_init_data_from_body_saves_user_id(self):
        body = payload([["syrniki", 2], ["shashlik-bacon", 1]], initData=self.init_data())
        r = await self.client.post("/api/order", json=body)
        self.assertEqual(r.status, 200)
        row = self.orders()[0]
        self.assertEqual(row["user_id"], self.CLIENT_ID)
        self.assertNotIn("initData", json.loads(row["items"] or "[]") or {})  # не попал в заказ

    async def test_forged_init_data_rejected(self):
        headers = {"X-Init-Data": self.init_data(token="999:ЧУЖОЙ-ТОКЕН")}
        r = await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]),
                                   headers=headers)
        self.assertEqual(r.status, 200)          # заказ принимаем
        self.assertEqual(self.orders()[0]["user_id"], 0)   # но id не подставлен
        self.assertFalse((await r.json())["telegram"])

    async def test_status_change_notifies_telegram_client(self):
        await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]),
                               headers={"X-Init-Data": self.init_data()})
        await self.admin_login()
        r = await self.admin_post("/api/admin/order/status", json={"id": 1, "status": "accepted"})
        self.assertEqual(r.status, 200)
        to_client = [m for m in self.fake.sent if m["chat_id"] == self.CLIENT_ID]
        self.assertEqual(len(to_client), 1)
        self.assertIn("принят", to_client[0]["text"])

    async def test_link_token_saves_user_id(self):
        # главный путь для iOS: initData нет вовсе, клиент опознан по токену из ссылки
        token = app.user_token(self.CLIENT_ID)
        r = await self.client.post("/api/order",
                                   json=payload([["syrniki", 2], ["shashlik-bacon", 1]], ut=token))
        data = await r.json()
        self.assertEqual(r.status, 200, data)
        self.assertTrue(data["telegram"])
        row = self.orders()[0]
        self.assertEqual(row["user_id"], self.CLIENT_ID)
        self.assertIsNone(row["username"])   # ника в токене нет — это нормально

    async def test_link_token_beats_empty_init_data(self):
        body = payload([["syrniki", 2], ["shashlik-bacon", 1]],
                       ut=app.user_token(self.CLIENT_ID), initData="")
        await self.client.post("/api/order", json=body)
        self.assertEqual(self.orders()[0]["user_id"], self.CLIENT_ID)

    async def test_forged_link_token_ignored(self):
        real = app.user_token(self.CLIENT_ID)
        forged = str(self.CLIENT_ID) + "." + "0" * 20
        for token in (forged, str(777), real + "x"):
            with self.subTest(token=token):
                app._ORDER_IPS.clear()
                r = await self.client.post(
                    "/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]], ut=token))
                self.assertEqual(r.status, 200)
                self.assertFalse((await r.json())["telegram"])
                self.assertEqual(self.orders()[-1]["user_id"], 0)

    async def test_username_restored_from_history(self):
        # клиент заказывал раньше как @anya — ник подтянется, чтобы в админке была ссылка
        await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]),
                               headers={"X-Init-Data": self.init_data()})
        await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]],
                                                          ut=app.user_token(self.CLIENT_ID)))
        rows = self.orders()
        self.assertEqual(rows[1]["user_id"], self.CLIENT_ID)
        self.assertEqual(rows[1]["username"], "anya")

    async def test_status_change_notifies_client_identified_by_token(self):
        await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]],
                                                          ut=app.user_token(self.CLIENT_ID)))
        await self.admin_login()
        await self.admin_post("/api/admin/order/status", json={"id": 1, "status": "onway"})
        to_client = [m for m in self.fake.sent if m["chat_id"] == self.CLIENT_ID]
        self.assertEqual(len(to_client), 1)
        self.assertIn("в пути", to_client[0]["text"])

    async def test_web_order_without_telegram_skips_notification(self):
        await self.client.post("/api/order", json=payload([["syrniki", 2], ["shashlik-bacon", 1]]))
        self.fake.sent.clear()                  # уведомление менеджеру уже ушло
        await self.admin_login()
        r = await self.admin_post("/api/admin/order/status", json={"id": 1, "status": "accepted"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.fake.sent, [])     # в чат 0 ничего не летит


if __name__ == "__main__":
    unittest.main()
