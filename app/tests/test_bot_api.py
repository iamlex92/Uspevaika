"""Интеграционная проверка: настоящий aiogram-бот против локального поддельного Bot API.

Проверяет то, чего не видно в юнит-тестах: регистрацию хендлеров, polling,
реальные HTTP-вызовы sendMessage / editMessageText / answerCallbackQuery и то,
что parse_mode=HTML действительно применяется к исходящим сообщениям.

Запуск: python -m unittest tests.test_bot_api -v   (из папки bot/)
Сеть не нужна: Bot API подменяется адресом http://127.0.0.1:<порт>.
"""
import asyncio
import json
import shutil
import socket
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

import bot as app
from tests.test_bot import fixed_menu, next_delivery_date


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def message_update(text=None, web_app=None, user_id=777):
    message = {"message_id": 1, "date": 0,
               "chat": {"id": user_id, "type": "private"},
               "from": {"id": user_id, "is_bot": False, "first_name": "Анна", "username": "anna"}}
    if text is not None:
        message["text"] = text
    if web_app is not None:
        message["web_app_data"] = {"data": web_app, "button_text": "Меню"}
    return {"update_id": 1, "message": message}


def callback_update(data, user_id=1):
    return {"update_id": 2,
            "callback_query": {"id": "cb1", "chat_instance": "x", "data": data,
                               "from": {"id": user_id, "is_bot": False, "first_name": "Менеджер"},
                               "message": {"message_id": 500, "date": 0,
                                           "chat": {"id": 555, "type": "group"},
                                           "text": "старый текст"}}}


def order_payload(items):
    return json.dumps({"type": "order", "items": items, "name": "Анна", "phone": "+79230000000",
                       "address": "ул. Ленина 1, кв. 5",
                       "date": next_delivery_date(),
                       "slot": "12:00–14:00", "payment": "Наличными курьеру",
                       "comment": "домофон не работает"}, ensure_ascii=False)


class BotApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uspevaika-api-"))
        fixed_menu(self.tmp / "menu.json")
        app.store = app.MenuStore(self.tmp / "menu.json")
        app.MENU_PATH = self.tmp / "menu.json"
        app.DB_PATH = self.tmp / "orders.db"
        app.ADMIN_CHAT_ID = 555
        app.ADMIN_IDS = {1}
        app._last_order.clear()
        app.init_db()

        self.calls = []
        self.updates = asyncio.Queue()
        self.next_msg_id = [100]

        web_app = web.Application()
        web_app.router.add_post("/bot{token}/{method}", self.api_handler)
        self.runner = web.AppRunner(web_app)
        await self.runner.setup()
        port = free_port()
        await web.TCPSite(self.runner, "127.0.0.1", port).start()

        session = AiohttpSession(api=TelegramAPIServer.from_base(f"http://127.0.0.1:{port}"))
        self.bot = Bot("123:TEST", session=session,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await self.bot.set_my_commands([BotCommand(command="start", description="Меню и заказ")])
        self.polling = asyncio.create_task(app.dp.start_polling(self.bot, handle_signals=False))
        await self.wait_for(lambda: any(m == "getUpdates" for m, _ in self.calls))

    async def asyncTearDown(self):
        self.polling.cancel()
        await asyncio.gather(self.polling, return_exceptions=True)
        await self.bot.session.close()
        await self.runner.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def api_handler(self, request):
        method = request.match_info["method"]
        payload = await request.json() if request.content_type == "application/json" else dict(await request.post())
        self.calls.append((method, payload))

        if method == "getMe":
            return web.json_response({"ok": True, "result": {
                "id": 1, "is_bot": True, "first_name": "Успевайка", "username": "uspev_test_bot"}})
        if method == "getUpdates":
            collected = []
            try:
                collected.append(await asyncio.wait_for(self.updates.get(), timeout=0.2))
            except asyncio.TimeoutError:
                pass
            return web.json_response({"ok": True, "result": collected})
        if method in ("sendMessage", "editMessageText"):
            self.next_msg_id[0] += 1
            return web.json_response({"ok": True, "result": {
                "message_id": self.next_msg_id[0], "date": 0,
                "chat": {"id": payload.get("chat_id"), "type": "private"},
                "text": payload.get("text", "")}})
        return web.json_response({"ok": True, "result": True})

    async def wait_for(self, predicate, timeout=8.0):
        for _ in range(int(timeout / 0.05)):
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return False

    def sent(self, method):
        return [p for m, p in self.calls if m == method]

    def sent_to(self, chat_id):
        return [p for p in self.sent("sendMessage") if str(p.get("chat_id")) == str(chat_id)]

    def db(self):
        import sqlite3
        return sqlite3.connect(self.tmp / "orders.db")

    async def test_full_order_flow(self):
        self.assertTrue(await self.wait_for(lambda: any(m == "setMyCommands" for m, _ in self.calls)))

        await self.updates.put(message_update("/start"))
        self.assertTrue(await self.wait_for(lambda: self.sent_to(777)))
        self.assertIn("Успевайка", self.sent_to(777)[-1]["text"])

        await self.updates.put(message_update(web_app=order_payload([["syrniki", 2], ["shashlik-bacon", 1]])))
        self.assertTrue(await self.wait_for(
            lambda: any("Заказ №" in str(p.get("text")) for p in self.sent_to(777))))
        self.assertTrue(await self.wait_for(
            lambda: any("Итого" in str(p.get("text")) for p in self.sent_to(555))))

        admin_msg = self.sent_to(555)[0]
        self.assertIn("callback_data", str(admin_msg.get("reply_markup")))
        self.assertEqual(admin_msg.get("parse_mode"), "HTML")

        con = self.db()
        order_id, status, total, admin_msg_id = con.execute(
            "select id, status, total, admin_msg_id from orders order by id desc limit 1").fetchone()
        con.close()
        self.assertEqual(status, "new")
        self.assertEqual(total, 420 * 2 + 986 + 350)
        self.assertIsNotNone(admin_msg_id)

        await self.updates.put(callback_update(f"st:{order_id}:accepted"))
        self.assertTrue(await self.wait_for(lambda: self.sent("editMessageText")))
        self.assertTrue(await self.wait_for(
            lambda: any("принят" in str(p.get("text")) for p in self.sent_to(777))))
        self.assertTrue(await self.wait_for(lambda: self.sent("answerCallbackQuery")))

        stocks_before = {i["id"]: i["stock"] for i in
                         json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))["items"]}
        await self.updates.put(callback_update(f"st:{order_id}:cancelled"))
        ok = await self.wait_for(lambda: self.status_of(order_id) == "cancelled")
        self.assertTrue(ok)
        stocks_after = {i["id"]: i["stock"] for i in
                        json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))["items"]}
        self.assertEqual(stocks_after["syrniki"], stocks_before["syrniki"] + 2)

        await self.updates.put(callback_update(f"st:{order_id}:accepted", user_id=999))
        self.assertTrue(await self.wait_for(
            lambda: any("только менеджеры" in str(p.get("text")) for p in self.sent("answerCallbackQuery"))))

        await self.updates.put(message_update("/stock", user_id=1))
        self.assertTrue(await self.wait_for(
            lambda: any("Остатки" in str(p.get("text")) for p in self.sent_to(1))))

    def status_of(self, order_id):
        con = self.db()
        try:
            row = con.execute("select status from orders where id=?", (order_id,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
