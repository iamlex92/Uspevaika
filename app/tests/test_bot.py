"""Проверки логики бота без сети и без Telegram.

Запуск: python -m unittest discover -s tests -v   (из папки bot/)

Каждый тест работает на копии menu.json во временной папке, поэтому настоящие
menu.json и orders.db не меняются.
"""
import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

DELIVERY = 350  # из menu.json: ближайший район 350 ₽, бесплатно от 3000


def next_delivery_date() -> str:
    """Ближайший разрешённый день доставки.

    Раньше тесты брали «завтра» — и падали целым набором, когда завтрашний день
    не входит в delivery_days. Дата теста не должна зависеть от дня запуска.
    """
    settings = json.loads((Path(bot.BASE) / "menu.json").read_text(encoding="utf-8"))["settings"]
    days = [d.lower() for d in settings.get("delivery_days") or []]
    for i in range(0, 30):
        d = date.today() + timedelta(days=i)
        if not days or bot.WEEKDAYS_RU[d.weekday()] in days:
            return d.isoformat()
    return (date.today() + timedelta(days=1)).isoformat()


def order_payload(items, **kw):
    data = {"type": "order", "items": items, "name": "Анна", "phone": "+79230000000",
            "address": "ул. Ленина 1, кв. 5",
            "date": next_delivery_date(),
            "slot": "12:00–14:00", "payment": "Наличными курьеру", "comment": "", "total": 1}
    data.update(kw)
    return json.dumps(data, ensure_ascii=False)


class FakeMessage:
    def __init__(self, user_id=77, username="anna", data=None, text=""):
        self.from_user = types.SimpleNamespace(id=user_id, username=username)
        self.chat = types.SimpleNamespace(id=user_id)
        self.web_app_data = types.SimpleNamespace(data=data) if data is not None else None
        self.text = text
        self.replies = []
        self.reply_to_message = None      # ответ на сообщение — для /addadmin

    async def answer(self, text, reply_markup=None):
        self.replies.append(text)
        self.kb = reply_markup     # клавиатуру проверяют тесты ссылки на витрину
        return types.SimpleNamespace(message_id=1)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return types.SimpleNamespace(message_id=10)


class FakeCallback:
    def __init__(self, user_id, data):
        self.from_user = types.SimpleNamespace(id=user_id)
        self.data = data
        self.alerts = []
        self.edited = []          # что показали после нажатия кнопки
        self.message = types.SimpleNamespace(edit_text=self._edit, answer=self._answer)

    async def _edit(self, text=None, reply_markup=None, *a, **k):
        self.edited.append((text, reply_markup))

    async def _answer(self, text=None, reply_markup=None, *a, **k):
        self.edited.append((text, reply_markup))

    async def answer(self, text=None, *args, **kwargs):
        self.alerts.append(text)


class Cmd:
    def __init__(self, args):
        self.args = args


# Остатки в рабочем menu.json меняются от реальных заказов, поэтому тесты
# работают на фиксированном срезе: копия меню с известными значениями.
FIXED_STOCK = {
    "shashlik-bacon": 12, "shepherds-pie": 6, "meatballs-tomato": 3,
    "stuffed-pepper": 9, "cutlet": 14, "nests": 7, "calzone": 10,
    "mini-meatballs": 16, "syrniki": 15, "zapekanka": 7, "vienna-waffles": 0,
    "potato-waffles": 8, "bliny-meat": 10, "roti": 9, "chicken-soup": 18,
    "borscht": 12, "solyanka": 14, "cheese-soup": 11, "mushroom-soup": 9,
    "tom-yam": 6, "set-mom": 20, "set-breakfast": 5, "mors": 14, "sauce-tomato": 20,
}


def fixed_menu(path: Path) -> None:
    """Кладёт по пути копию menu.json с предсказуемыми остатками."""
    menu = json.loads((Path(bot.BASE) / "menu.json").read_text(encoding="utf-8"))
    for item in menu["items"]:
        if item["id"] in FIXED_STOCK:
            item["stock"] = FIXED_STOCK[item["id"]]
    path.write_text(json.dumps(menu, ensure_ascii=False, indent=2), encoding="utf-8")


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uspevaika-test-"))
        fixed_menu(self.tmp / "menu.json")
        bot.store = bot.MenuStore(self.tmp / "menu.json")
        bot.MENU_PATH = self.tmp / "menu.json"
        bot.DB_PATH = self.tmp / "orders.db"
        bot.ADMIN_CHAT_ID = 555
        bot.ADMIN_IDS = {1}
        bot._last_order.clear()
        bot.init_db()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stocks(self):
        menu = json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))
        return {item["id"]: item["stock"] for item in menu["items"]}

    def total_of(self, text):
        for line in text.split("\n"):
            if "Итого" in line:
                return int("".join(ch for ch in line if ch.isdigit()))
        return None

    def order_count(self):
        con = sqlite3.connect(self.tmp / "orders.db")
        try:
            return con.execute("select count(*) from orders").fetchone()[0]
        finally:
            con.close()

    async def place_order(self, payload, user_id=77):
        bot._last_order.clear()
        message = FakeMessage(user_id=user_id, data=payload)
        message.replies = []
        fake_bot = FakeBot()
        await bot.on_order(message, fake_bot)
        return message.replies[-1] if message.replies else "", fake_bot


class OrderTests(BotTestCase):
    async def test_valid_order(self):
        text, fake = await self.place_order(order_payload([["syrniki", 2], ["shashlik-bacon", 1]]))
        self.assertIn("получен", text)
        self.assertEqual(self.total_of(text), 420 * 2 + 986 + DELIVERY)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["chat_id"], 555)
        self.assertEqual(self.stocks()["syrniki"], 13)
        self.assertEqual(self.stocks()["shashlik-bacon"], 11)
        self.assertEqual(self.order_count(), 1)

    async def test_admin_message_has_buttons(self):
        _, fake = await self.place_order(order_payload([["syrniki", 4]]))
        markup = fake.sent[0]["markup"]
        self.assertIsNotNone(markup)
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any(cb.startswith("st:") for cb in callbacks))

    async def test_below_minimum(self):
        text, fake = await self.place_order(order_payload([["mors", 1]]))
        self.assertIn("меньше минимальной", text)
        self.assertEqual(self.stocks()["mors"], 14)
        self.assertEqual(fake.sent, [])
        self.assertEqual(self.order_count(), 0)

    async def test_stock_shortage(self):
        text, _ = await self.place_order(
            order_payload([["syrniki", 3], ["vienna-waffles", 2], ["shashlik-bacon", 99]]))
        self.assertIn("Венские вафли — закончилось", text)
        self.assertIn("осталось только 12 шт", text)
        self.assertEqual(self.stocks()["shashlik-bacon"], 0)
        self.assertEqual(self.stocks()["vienna-waffles"], 0)
        self.assertIn("Доставка: бесплатно", text)   # 13 092 ≥ 3 000
        self.assertEqual(self.total_of(text), 420 * 3 + 986 * 12)

    async def test_stock_reserved_before_manager_accepts(self):
        # Заказ в статусе new — менеджер ещё не нажал «Принят», а остаток уже
        # списан. Иначе двое успели бы купить одно и то же блюдо.
        await self.place_order(order_payload([["shashlik-bacon", 12]], name="Первый"))
        self.assertEqual(self.stocks()["shashlik-bacon"], 0)
        with sqlite3.connect(self.tmp / "orders.db") as con:
            status = con.execute("SELECT status FROM orders").fetchone()[0]
        self.assertEqual(status, "new")

    async def test_sold_out_names_the_dish(self):
        # второй клиент получает отказ с названием блюда, а не «все блюда»
        await self.place_order(order_payload([["shashlik-bacon", 12]], name="Первый"))
        text, _ = await self.place_order(order_payload([["shashlik-bacon", 2]], name="Второй"))
        self.assertIn("Куриный шашлычок в беконе", text)
        self.assertIn("закончилось", text)
        self.assertIn("успели заказать", text)

    async def test_sold_out_error_has_code(self):
        from tests.test_web_order import payload as web_payload
        # сервер отдаёт код GONE, чтобы витрина перечитала остатки
        await self.place_order(order_payload([["shashlik-bacon", 12]], name="Первый"))
        try:
            await bot.place_order(web_payload([["shashlik-bacon", 2]]),
                                  user_id=0, username=None, source="web", bot=FakeBot())
        except bot.OrderError as e:
            self.assertEqual(e.code, "GONE")
            self.assertIn("Куриный шашлычок", str(e))
        else:
            self.fail("заказ должен быть отклонён")

    async def test_duplicate_items_merged(self):
        text, _ = await self.place_order(order_payload([["mors", 5], ["mors", 5]]))
        self.assertEqual(text.count("• Домашний морс"), 1)
        self.assertEqual(self.total_of(text), 2500 + DELIVERY)

    async def test_client_total_ignored(self):
        text, _ = await self.place_order(
            order_payload([["syrniki", 4], ["shashlik-bacon", 1]], total=1))
        self.assertEqual(self.total_of(text), 420 * 4 + 986 + DELIVERY)

    async def test_free_delivery_threshold(self):
        text, _ = await self.place_order(order_payload([["set-mom", 1]]))
        self.assertIn("Доставка: бесплатно", text)
        self.assertEqual(self.total_of(text), 3000)

    async def test_slot_variants_normalized(self):
        text, _ = await self.place_order(
            order_payload([["syrniki", 2], ["shashlik-bacon", 2]], slot="12:00-14:00"))
        self.assertIn("получен", text)
        self.assertIn("12:00–14:00", text)
        text, _ = await self.place_order(
            order_payload([["syrniki", 2], ["shashlik-bacon", 2]], payment="  наличными   курьеру "))
        self.assertIn("получен", text)

    async def test_rejects_bad_delivery_fields(self):
        cases = [
            ({"date": "2030-01-01"}, "день доставки"),
            ({"slot": "03:00-04:00"}, "интервал доставки"),
            ({"payment": "Биткоином"}, "способ оплаты"),
        ]
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                text, _ = await self.place_order(
                    order_payload([["syrniki", 2], ["shashlik-bacon", 2]], **kwargs))
                self.assertIn(expected, text)

    async def test_last_day_of_window_accepted(self):
        settings = json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))["settings"]
        last = bot.allowed_dates(settings)[-1]
        text, _ = await self.place_order(
            order_payload([["syrniki", 2], ["shashlik-bacon", 2]], date=last))
        self.assertIn("получен", text)

    async def test_contact_validation(self):
        cases = [({"name": " "}, "Укажите имя"),
                 ({"phone": "123"}, "телефон"),
                 ({"address": "ул"}, "адрес")]
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                text, _ = await self.place_order(order_payload([["syrniki", 4]], **kwargs))
                self.assertIn(expected, text)
        self.assertEqual(self.order_count(), 0)

    async def test_garbage_payloads(self):
        self.assertIn("прочитать", (await self.place_order("не json"))[0])
        self.assertIn("прочитать", (await self.place_order('{"type":"ping"}'))[0])
        self.assertIn("Корзина пуста", (await self.place_order(order_payload("abcdef")))[0])
        self.assertIn("больше нет", (await self.place_order(order_payload([["nope", 5]])))[0])

    async def test_garbage_items_filtered(self):
        text, _ = await self.place_order(
            order_payload([[None, "x"], [{"a": 1}, 2], ["nope", 5], ["syrniki", 4]]))
        self.assertIn("получен", text)
        self.assertEqual(self.total_of(text), 420 * 4 + DELIVERY)

    async def test_huge_quantity_capped(self):
        text, _ = await self.place_order(order_payload([["shashlik-bacon", 10 ** 9]]))
        self.assertIn("осталось только 12 шт", text)
        self.assertEqual(self.stocks()["shashlik-bacon"], 0)

    async def test_antiflood(self):
        payload = order_payload([["syrniki", 4], ["shashlik-bacon", 1]])
        bot._last_order.clear()
        fake = FakeBot()
        first = FakeMessage(data=payload)
        first.replies = []
        await bot.on_order(first, fake)
        second = FakeMessage(data=payload)
        second.replies = []
        await bot.on_order(second, fake)
        self.assertIn("подождите", second.replies[-1])
        self.assertEqual(self.stocks()["syrniki"], 11)
        self.assertEqual(self.order_count(), 1)

    async def test_menu_corrupted(self):
        (self.tmp / "menu.json").write_text("{ битый", encoding="utf-8")
        bot._last_order.clear()
        message = FakeMessage(data=order_payload([["syrniki", 4]]))
        message.replies = []
        await bot.on_order(message, FakeBot())
        self.assertIn("временно недоступно", message.replies[-1])

        message = FakeMessage(user_id=1)
        message.replies = []
        await bot.stock(message)
        self.assertIn("недоступно", message.replies[-1].lower())

        message = FakeMessage()
        message.replies = []
        await bot.start(message)
        self.assertIn("недоступно", message.replies[-1].lower())

    async def test_concurrent_orders_do_not_oversell(self):
        await bot.set_stock(FakeMessage(user_id=1), Cmd("тефтели 3"))
        await bot.set_stock(FakeMessage(user_id=1), Cmd("с 7"))  # остатки для прохождения минимума
        payload = order_payload([["meatballs-tomato", 3], ["syrniki", 4]])

        async def buy(user_id):
            bot._last_order.clear()
            message = FakeMessage(user_id=user_id, data=payload)
            message.replies = []
            await bot.on_order(message, FakeBot())

        await asyncio.gather(buy(101), buy(102), buy(103))
        con = sqlite3.connect(self.tmp / "orders.db")
        sold = 0
        for (items,) in con.execute("select items from orders"):
            for item in json.loads(items):
                if item["id"] == "meatballs-tomato":
                    sold += item["qty"]
        con.close()
        self.assertLessEqual(sold, 3)


class DeliveryZoneTests(BotTestCase):
    """Районы доставки и дни недели из menu.json."""

    def next_delivery_day(self):
        """Ближайший день, разрешённый в delivery_days (начиная с сегодня)."""
        days = [d.lower() for d in json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))
                ["settings"]["delivery_days"]]
        for i in range(0, 30):
            d = date.today() + timedelta(days=i)
            if bot.WEEKDAYS_RU[d.weekday()] in days:
                return d.isoformat()
        raise AssertionError("нет дней доставки в ближайший месяц")

    def non_delivery_day(self):
        days = [d.lower() for d in json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))
                ["settings"]["delivery_days"]]
        for i in range(1, 30):
            d = date.today() + timedelta(days=i)
            if bot.WEEKDAYS_RU[d.weekday()] not in days:
                return d.isoformat()
        raise AssertionError("все дни в окне — дни доставки")

    async def test_allowed_dates_only_delivery_days(self):
        menu = json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))
        dates = bot.allowed_dates(menu["settings"])
        self.assertTrue(dates)
        for iso in dates:
            self.assertIn(bot.WEEKDAYS_RU[date.fromisoformat(iso).weekday()],
                          [d.lower() for d in menu["settings"]["delivery_days"]])

    async def test_order_on_wrong_weekday_rejected(self):
        text, _ = await self.place_order(
            order_payload([["syrniki", 2], ["shashlik-bacon", 2]], date=self.non_delivery_day()))
        self.assertIn("доставки нет", text)
        self.assertEqual(self.order_count(), 0)

    async def test_order_on_delivery_day_accepted(self):
        text, _ = await self.place_order(
            order_payload([["syrniki", 2], ["shashlik-bacon", 2]], date=self.next_delivery_day()))
        self.assertIn("получен", text)

    async def test_zone_sets_delivery_price(self):
        menu = json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))
        zones = menu["settings"]["delivery_zones"]
        # вторая зона дороже: бесплатно только от 5000
        text, _ = await self.place_order(
            order_payload([["syrniki", 2], ["shashlik-bacon", 1]], zone=zones[1]["title"]))
        self.assertEqual(self.total_of(text), 420 * 2 + 986 + zones[1]["price"])

    async def test_zone_free_threshold(self):
        menu = json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))
        zones = menu["settings"]["delivery_zones"]
        # 2 300 ₽ — ниже порога второй зоны (5 000), но выше первой (3 000)? нет: берём сумму ≥ 5000
        text, _ = await self.place_order(
            order_payload([["shashlik-bacon", 6]], zone=zones[1]["title"]))  # 5 916 ₽
        self.assertIn("бесплатно", text)

    async def test_unknown_zone_falls_back_to_first(self):
        text, _ = await self.place_order(
            order_payload([["syrniki", 2], ["shashlik-bacon", 1]], zone="Марс"))
        self.assertEqual(self.total_of(text), 420 * 2 + 986 + DELIVERY)

    async def test_zone_min_sum_enforced(self):
        menu = json.loads((self.tmp / "menu.json").read_text(encoding="utf-8"))
        zones = menu["settings"]["delivery_zones"]
        far = next(z for z in zones if z.get("min_sum"))
        text, _ = await self.place_order(
            order_payload([["syrniki", 4]], zone=far["title"]))  # 1 680 ₽ < 3 000
        self.assertIn("минимальная сумма", text)
        self.assertEqual(self.order_count(), 0)
        self.assertEqual(self.stocks()["syrniki"], 15)   # остатки не списаны


class StatusTests(BotTestCase):
    async def make_order(self):
        await self.place_order(order_payload([["syrniki", 4], ["mors", 5]]))
        con = sqlite3.connect(self.tmp / "orders.db")
        order_id = con.execute("select max(id) from orders").fetchone()[0]
        con.close()
        return order_id

    def status(self, order_id):
        con = sqlite3.connect(self.tmp / "orders.db")
        try:
            return con.execute("select status from orders where id=?", (order_id,)).fetchone()[0]
        finally:
            con.close()

    async def test_cancel_returns_stock(self):
        order_id = await self.make_order()
        before = self.stocks()
        await bot.on_status(FakeCallback(1, f"st:{order_id}:cancelled"), FakeBot())
        after = self.stocks()
        self.assertEqual(self.status(order_id), "cancelled")
        self.assertEqual(after["syrniki"], before["syrniki"] + 4)
        self.assertEqual(after["mors"], before["mors"] + 5)

    async def test_progress_status_notifies_client(self):
        order_id = await self.make_order()
        fake = FakeBot()
        await bot.on_status(FakeCallback(1, f"st:{order_id}:accepted"), fake)
        self.assertEqual(self.status(order_id), "accepted")
        self.assertTrue(any("принят" in msg["text"] for msg in fake.sent))

    async def test_cannot_revive_closed_order(self):
        order_id = await self.make_order()
        await bot.on_status(FakeCallback(1, f"st:{order_id}:delivered"), FakeBot())
        callback = FakeCallback(1, f"st:{order_id}:accepted")
        await bot.on_status(callback, FakeBot())
        self.assertEqual(callback.alerts, ["Заказ уже закрыт"])
        self.assertEqual(self.status(order_id), "delivered")

    async def test_non_admin_rejected(self):
        order_id = await self.make_order()
        callback = FakeCallback(999, f"st:{order_id}:accepted")
        await bot.on_status(callback, FakeBot())
        self.assertIn("только менеджеры", " ".join(str(x) for x in callback.alerts))
        self.assertEqual(self.status(order_id), "new")

    async def test_malformed_callback_survives(self):
        for data in ("st:abc", "st:1:new", "st:", "st:1:unknown"):
            with self.subTest(data=data):
                await bot.on_status(FakeCallback(1, data), FakeBot())

    async def test_double_cancel_does_not_double_restore(self):
        order_id = await self.make_order()
        await bot.on_status(FakeCallback(1, f"st:{order_id}:cancelled"), FakeBot())
        after_first = self.stocks()
        await bot.on_status(FakeCallback(1, f"st:{order_id}:cancelled"), FakeBot())
        self.assertEqual(self.stocks(), after_first)


class PhoneValidationTests(BotTestCase):
    """Телефон проверяется по-настоящему: набрать «1234567890» невозможно."""

    async def test_valid_numbers(self):
        for phone in ("+7 923 363-82-08", "+79233638208", "89233638208",
                      "9233638208", "+7 (923) 363-82-08"):
            with self.subTest(phone=phone):
                self.assertIsNone(bot.phone_problem(phone))

    async def test_garbage_rejected(self):
        for phone in ("", None, "123", "1234567890", "9999999999",
                      "7777777777", "+7 111 222-33-44", "abcdefghij"):
            with self.subTest(phone=phone):
                self.assertIsNotNone(bot.phone_problem(phone))

    async def test_order_with_bad_phone_rejected(self):
        text, _ = await self.place_order(order_payload([["syrniki", 4]], phone="1234567890"))
        self.assertIn("телефон", text.lower())
        self.assertEqual(self.order_count(), 0)


class AdminTextTests(BotTestCase):
    """Сообщение в чат менеджеров не должно превышать лимит Telegram.

    Больше 4096 символов Telegram не примет, и менеджер не увидит заказ вовсе —
    при том что заказ уже сохранён и остатки списаны.
    """

    def row(self, items, comment=""):
        return {"id": 999, "status": "new", "name": "А" * 60, "phone": "+79233638208",
                "username": "a" * 32, "user_id": 1, "address": "А" * 200,
                "zone": "Ж" * 120, "date": "2026-11-27", "slot": "12:00–14:00",
                "payment": "П" * 40, "items": json.dumps(items, ensure_ascii=False),
                "goods": 1, "delivery": 650, "total": 1, "comment": comment}

    async def test_normal_order_intact(self):
        text = bot.admin_text(self.row([
            {"id": "a", "title": "Сырники", "price": 420, "qty": 2},
            {"id": "b", "title": "Шашлычок", "price": 986, "qty": 1}]))
        self.assertLessEqual(len(text), bot.TELEGRAM_TEXT_LIMIT)
        self.assertIn("Сырники", text)
        self.assertIn("Итого", text)

    async def test_full_menu_fits(self):
        menu = json.loads((Path(bot.BASE) / "menu.json").read_text(encoding="utf-8"))
        text = bot.admin_text(self.row(
            [{"id": it["id"], "title": it["title"], "price": it["price"], "qty": 999}
             for it in menu["items"]], comment="я" * 500))
        self.assertLessEqual(len(text), bot.TELEGRAM_TEXT_LIMIT)
        self.assertIn("Итого", text)      # сумма не потерялась

    async def test_huge_order_truncated_not_broken(self):
        items = [{"id": f"i{i}", "title": ("Блюдо " + str(i)).ljust(80, "ъ"),
                  "price": 999, "qty": 9} for i in range(50)]
        text = bot.admin_text(self.row(items, comment="я" * 500))
        self.assertLessEqual(len(text), bot.TELEGRAM_TEXT_LIMIT)
        self.assertIn("Итого", text)
        self.assertIn("поз.", text)       # сказано, что состав сокращён
        self.assertEqual(text.count("<b>"), text.count("</b>"))   # теги парны

    async def test_keeps_contact_and_status(self):
        items = [{"id": f"i{i}", "title": "Б" * 80, "price": 999, "qty": 9} for i in range(50)]
        text = bot.admin_text(self.row(items))
        self.assertIn("Заказ №999", text)
        self.assertIn("+79233638208", text)


class ChatEditOrderTests(BotTestCase):
    """В чате заказ можно только уменьшить/убрать; добавление — в веб-панели."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        bot._edits.clear()
        # Тесты не должны зависеть от .env машины: на сервере его может не быть
        self._token, bot.BOT_TOKEN = bot.BOT_TOKEN, "123:TEST"
        self._url, bot.WEBAPP_URL = bot.WEBAPP_URL, "https://shop.example.com/"

    async def asyncTearDown(self):
        bot.BOT_TOKEN, bot.WEBAPP_URL = self._token, self._url
        await super().asyncTearDown()

    def tearDown(self):
        bot._edits.clear()
        super().tearDown()

    async def make_order(self):
        await self.place_order(order_payload([["syrniki", 4], ["mors", 2]]))
        with sqlite3.connect(self.tmp / "orders.db") as con:
            return con.execute("select max(id) from orders").fetchone()[0]

    async def open_editor(self, order_id):
        c = FakeCallback(1, f"ed:{order_id}:0")
        await bot.on_edit(c, FakeBot())
        return c

    async def test_card_has_edit_button(self):
        await self.make_order()
        _, fake = await self.place_order(order_payload([["syrniki", 4]]))
        cbs = [b.callback_data for row in fake.sent[0]["markup"].inline_keyboard for b in row]
        self.assertTrue(any(cb.startswith("ed:") for cb in cbs))

    async def test_editor_lists_only_order_items(self):
        # никаких чужих блюд из меню — только то, что в заказе
        order_id = await self.make_order()
        c = await self.open_editor(order_id)
        text = c.edited[-1][0]
        self.assertIn("убрать лишнее", text)
        self.assertIn("Сырники — 4 шт", text)
        self.assertIn("Домашний морс — 2 шт", text)
        self.assertNotIn("Куриный шашлычок", text)

    async def test_no_plus_button_in_chat(self):
        order_id = await self.make_order()
        c = await self.open_editor(order_id)
        buttons = [b for row in c.edited[-1][1].inline_keyboard for b in row]
        # у обычных кнопок есть callback_data, у кнопки панели — web_app
        cbs = [b.callback_data for b in buttons if b.callback_data]
        self.assertFalse(any(":a:" in cb for cb in cbs), "добавления в чате быть не должно")

    async def test_panel_link_carries_token(self):
        # без токена браузер не опознаётся (нет initData) и просит пароль
        order_id = await self.make_order()
        c = await self.open_editor(order_id)
        text = c.edited[-1][0]
        import re
        href = re.search(r'href="([^"]+)"', text).group(1)
        token = href.split("ut=")[1]
        self.assertEqual(bot.verify_admin_token(token), 1)
        self.assertIn(f"order={order_id}", href)

    async def test_panel_link_has_order_and_token_together(self):
        url = bot.admin_order_url(42, 1)
        self.assertIn("order=42", url)
        self.assertIn("ut=", url)
        self.assertEqual(bot.verify_admin_token(url.split("ut=")[1]), 1)

    async def test_link_to_panel_for_adding(self):
        # web_app-кнопка в inline-клавиатуре недопустима (Telegram:
        # BUTTON_TYPE_INVALID), поэтому даём обычную ссылку в тексте
        order_id = await self.make_order()
        c = await self.open_editor(order_id)
        text, markup = c.edited[-1]
        self.assertIn(f"order={order_id}", text)
        self.assertIn("открыть панель", text)
        for row in markup.inline_keyboard:
            for b in row:
                self.assertIsNone(b.web_app, "web_app в inline-кнопке сломает сообщение")

    async def test_editor_message_is_valid_for_telegram(self):
        # ни одной web_app-кнопки — иначе Telegram не покажет вообще ничего
        order_id = await self.make_order()
        c = await self.open_editor(order_id)
        self.assertTrue(c.edited, "сообщение не отправилось")

    async def test_minus_reduces(self):
        order_id = await self.make_order()
        await self.open_editor(order_id)
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:m:syrniki"), FakeBot())
        self.assertEqual(bot._edit_state(1, order_id)["items"]["syrniki"], 3)

    async def test_minus_at_zero_stays_zero(self):
        order_id = await self.make_order()
        await self.open_editor(order_id)
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:m:mors"), FakeBot())
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:m:mors"), FakeBot())
        self.assertEqual(bot._edit_state(1, order_id)["items"]["mors"], 0)

    async def test_remove_dish_marks_zero(self):
        order_id = await self.make_order()
        await self.open_editor(order_id)
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:p:mors"), FakeBot())
        self.assertEqual(bot._edit_state(1, order_id)["items"]["mors"], 0)

    async def test_save_returns_stock(self):
        order_id = await self.make_order()
        before = self.stocks()
        await self.open_editor(order_id)
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:m:syrniki"), FakeBot())
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:p:mors"), FakeBot())
        c = FakeCallback(1, f"ed:{order_id}:s")
        await bot.on_edit(c, FakeBot())
        after = self.stocks()
        self.assertEqual(after["syrniki"], before["syrniki"] + 1)
        self.assertEqual(after["mors"], before["mors"] + 2)   # убран целиком
        with sqlite3.connect(self.tmp / "orders.db") as con:
            con.row_factory = sqlite3.Row
            row = con.execute("select items,total from orders where id=?", (order_id,)).fetchone()
        items = {i["id"]: i["qty"] for i in json.loads(row["items"])}
        self.assertEqual(items, {"syrniki": 3})                # морса больше нет
        self.assertIn("изменён", str(c.alerts[-1]))

    async def test_save_rejects_empty(self):
        order_id = await self.make_order()
        await self.open_editor(order_id)
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:p:syrniki"), FakeBot())
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:p:mors"), FakeBot())
        c = FakeCallback(1, f"ed:{order_id}:s")
        await bot.on_edit(c, FakeBot())
        self.assertIn("Отменить", " ".join(str(a) for a in c.alerts))
        self.assertEqual(self.stocks()["syrniki"], 11)   # ничего не вернулось

    async def test_cancel_discards_changes(self):
        order_id = await self.make_order()
        await self.open_editor(order_id)
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:p:mors"), FakeBot())
        await bot.on_edit(FakeCallback(1, f"ed:{order_id}:x"), FakeBot())
        self.assertIsNone(bot._edit_state(1, order_id))
        with sqlite3.connect(self.tmp / "orders.db") as con:
            con.row_factory = sqlite3.Row
            items = {i["id"]: i["qty"] for i in json.loads(
                con.execute("select items from orders where id=?", (order_id,)).fetchone()["items"])}
        self.assertEqual(items["mors"], 2)     # заказ не тронут

    async def test_closed_order_cannot_be_edited(self):
        order_id = await self.make_order()
        await bot.on_status(FakeCallback(1, f"st:{order_id}:delivered"), FakeBot())
        c = FakeCallback(1, f"ed:{order_id}:0")
        await bot.on_edit(c, FakeBot())
        self.assertIsNone(bot._edit_state(1, order_id))
        self.assertIn("закрыт", " ".join(str(a) for a in c.alerts).lower())

    async def test_client_cannot_edit(self):
        order_id = await self.make_order()
        c = FakeCallback(999, f"ed:{order_id}:0")
        await bot.on_edit(c, FakeBot())
        self.assertIsNone(bot._edit_state(999, order_id))
        self.assertIn("только менеджеры", " ".join(str(a) for a in c.alerts))

    async def test_garbage_callback_survives(self):
        for data in ("ed:", "ed:abc:0", "ed:1", "ed:999999:0", "ed:1:zzz"):
            with self.subTest(data=data):
                await bot.on_edit(FakeCallback(1, data), FakeBot())


class AdminChatMenuTests(BotTestCase):
    """Кнопки в чате: меню менеджера, история заказов, управление менеджерами."""

    def callback(self, data, user_id=1):
        return FakeCallback(user_id, data)

    async def test_manager_menu_for_admin(self):
        message = FakeMessage(user_id=1)
        message.replies = []
        await bot.admin_menu(message)
        self.assertIn("Панель менеджера", message.replies[-1])
        buttons = [b.text for row in message.kb.inline_keyboard for b in row]
        self.assertIn("📜 История", buttons)
        self.assertIn("👥 Менеджеры", buttons)

    async def test_manager_menu_hidden_from_clients(self):
        message = FakeMessage(user_id=77)
        message.replies = []
        await bot.admin_menu(message)
        self.assertEqual(message.replies, [])

    async def test_history_button(self):
        await self.place_order(order_payload([["syrniki", 4]]))
        c = self.callback("am:history:0")
        await bot.admin_menu_action(c)
        text = c.edited[-1][0]
        self.assertIn("История заказов", text)
        self.assertIn("№1", text)

    async def test_history_paginates(self):
        for i in range(8):
            await self.place_order(order_payload([["syrniki", 4]], name=f"Клиент {i}"))
        c = self.callback("am:history:1")
        await bot.admin_menu_action(c)
        text, markup = c.edited[-1]
        self.assertIn("стр. 2/", text)
        buttons = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("← Новее", buttons)

    async def test_orders_button_lists_active(self):
        await self.place_order(order_payload([["syrniki", 4]]))
        c = self.callback("am:orders:0")
        await bot.admin_menu_action(c)
        self.assertIn("Активные заказы", c.edited[-1][0])

    async def test_stock_button(self):
        c = self.callback("am:stock:0")
        await bot.admin_menu_action(c)
        self.assertIn("Остатки", c.edited[-1][0])

    async def test_client_cannot_use_buttons(self):
        c = self.callback("am:history:0", user_id=77)
        await bot.admin_menu_action(c)
        self.assertEqual(c.edited, [])
        self.assertIn("только менеджерам", " ".join(str(x) for x in c.alerts))

    async def test_back_button_returns_to_menu(self):
        c = self.callback("am:menu:0")
        await bot.admin_menu_action(c)
        self.assertIn("Панель менеджера", c.edited[-1][0])


class AdminRosterTests(BotTestCase):
    """Добавление менеджеров из чата."""

    async def test_add_by_id(self):
        message = FakeMessage(user_id=1)
        message.replies = []
        await bot.add_admin_cmd(message, Cmd("555111 размер"))
        self.assertTrue(bot.is_admin(555111))
        self.assertIn("теперь менеджер", message.replies[-1])

    async def test_add_by_reply(self):
        message = FakeMessage(user_id=1)
        message.replies = []
        message.reply_to_message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=777888, full_name="Пётр", username="petr"))
        await bot.add_admin_cmd(message, Cmd(""))
        self.assertTrue(bot.is_admin(777888))

    async def test_add_twice_is_idempotent(self):
        bot.add_admin(555111)
        message = FakeMessage(user_id=1)
        message.replies = []
        await bot.add_admin_cmd(message, Cmd("555111"))
        self.assertIn("уже менеджер", message.replies[-1])

    async def test_nickname_cannot_be_resolved(self):
        message = FakeMessage(user_id=1)
        message.replies = []
        await bot.add_admin_cmd(message, Cmd("@petr"))
        self.assertIn("id по нику", message.replies[-1])

    async def test_new_admin_gets_rights(self):
        bot.add_admin(555111)
        message = FakeMessage(user_id=555111)
        message.replies = []
        await bot.admin_menu(message)
        self.assertIn("Панель менеджера", message.replies[-1])

    async def test_remove_added_admin(self):
        bot.add_admin(555111)
        message = FakeMessage(user_id=1)
        message.replies = []
        await bot.del_admin_cmd(message, Cmd("555111"))
        self.assertFalse(bot.is_admin(555111))

    async def test_cannot_remove_env_admin(self):
        message = FakeMessage(user_id=1)
        message.replies = []
        await bot.del_admin_cmd(message, Cmd("1"))       # 1 — из ADMIN_IDS
        self.assertTrue(bot.is_admin(1))
        self.assertIn(".env", message.replies[-1])

    async def test_cannot_remove_self(self):
        bot.add_admin(555111)
        message = FakeMessage(user_id=555111)
        message.replies = []
        await bot.del_admin_cmd(message, Cmd("555111"))
        self.assertTrue(bot.is_admin(555111))
        self.assertIn("себя", message.replies[-1])

    async def test_non_admin_cannot_manage(self):
        message = FakeMessage(user_id=77)
        message.replies = []
        await bot.add_admin_cmd(message, Cmd("555111"))
        self.assertFalse(bot.is_admin(555111))

    async def test_menu_keyboard_for_new_admin(self):
        bot.add_admin(555111)
        bot.WEBAPP_URL = "https://shop.example.com/"
        message = FakeMessage(user_id=555111)
        message.replies = []
        await bot.start(message)
        labels = [b.text for row in message.kb.keyboard for b in row]
        self.assertIn("👔 Менеджер", labels)


class WebappTokenTests(BotTestCase):
    """Ссылка на витрину с подписанным id — путь для iOS, где нет initData."""

    def setUp(self):
        super().setUp()
        self._token, bot.BOT_TOKEN = bot.BOT_TOKEN, "123:TEST"
        self._url, bot.WEBAPP_URL = bot.WEBAPP_URL, "https://shop.example.com/"

    def tearDown(self):
        bot.BOT_TOKEN, bot.WEBAPP_URL = self._token, self._url
        super().tearDown()

    async def test_token_roundtrip(self):
        token = bot.user_token(424242)
        self.assertTrue(token)
        self.assertEqual(bot.verify_user_token(token), 424242)

    async def test_forged_tokens_rejected(self):
        real = bot.user_token(424242)
        for token in ("", None, "424242", "424242.00000000000000000000",
                      str(777) + real[real.index("."):], real + "0"):
            with self.subTest(token=token):
                self.assertEqual(bot.verify_user_token(token), 0)

    async def test_token_changes_with_secret(self):
        token = bot.user_token(424242)
        bot.BOT_TOKEN = "988:ДРУГОЙ"
        self.assertEqual(bot.verify_user_token(token), 0)

    async def test_webapp_url_carries_token(self):
        url = bot.webapp_url(424242)
        self.assertIn("u=424242.", url)
        self.assertTrue(url.startswith("https://shop.example.com/"))
        self.assertIn("?", url)

    async def test_start_button_url_is_personal(self):
        message = FakeMessage(user_id=424242)
        message.replies = []
        await bot.start(message)
        button = message.kb.keyboard[0][0]
        self.assertEqual(bot.verify_user_token(button.web_app.url.split("u=")[1]), 424242)

    async def test_url_without_token(self):
        bot.BOT_TOKEN = ""
        self.assertEqual(bot.webapp_url(424242), "https://shop.example.com/")


class AdminButtonTests(BotTestCase):
    """Кнопка «🛠 Админка» в главном меню — только у менеджеров."""

    def setUp(self):
        super().setUp()
        self._token, bot.BOT_TOKEN = bot.BOT_TOKEN, "123:TEST"
        self._url, bot.WEBAPP_URL = bot.WEBAPP_URL, "https://shop.example.com/"

    def tearDown(self):
        bot.BOT_TOKEN, bot.WEBAPP_URL = self._token, self._url
        super().tearDown()

    def buttons(self, message):
        return [b for row in message.kb.keyboard for b in row]

    async def start_for(self, user_id):
        message = FakeMessage(user_id=user_id)
        message.replies = []
        await bot.start(message)
        return message

    async def test_admin_sees_button(self):
        message = await self.start_for(1)          # 1 — админ из ADMIN_IDS
        labels = [b.text for b in self.buttons(message)]
        self.assertIn("🛠 Админка", labels)

    async def test_client_does_not_see_button(self):
        message = await self.start_for(77)          # обычный клиент
        labels = [b.text for b in self.buttons(message)]
        self.assertNotIn("🛠 Админка", labels)
        self.assertIn("🛒 Меню и заказ", labels)

    async def test_admin_button_opens_admin_with_token(self):
        message = await self.start_for(1)
        button = next(b for b in self.buttons(message) if b.text == "🛠 Админка")
        url = button.web_app.url
        self.assertIn("/admin?ut=", url)
        token = url.split("ut=")[1]
        self.assertEqual(bot.verify_admin_token(token), 1)

    async def test_admin_button_without_webapp_url(self):
        bot.WEBAPP_URL = ""
        message = await self.start_for(1)
        labels = [b.text for b in self.buttons(message)]
        self.assertNotIn("🛠 Админка", labels)   # некуда открывать

    async def test_admin_token_expires(self):
        token = bot.admin_token(1)
        self.assertEqual(bot.verify_admin_token(token), 1)
        payload, sig = token.rsplit(".", 1)
        uid, _, exp = payload.partition(".")
        expired = f"{uid}.{int(exp) - bot.ADMIN_TOKEN_TTL - 60}"
        self.assertEqual(bot.verify_admin_token(f"{expired}.{sig}"), 0)

    async def test_admin_token_forgery(self):
        token = bot.admin_token(1)
        payload, _ = token.rsplit(".", 1)
        bad_tokens = (f"{payload}.{'0' * 24}", "1.9999999999.abc", "", None,
                      "1.9999999999.кириллица", "токен.с.точками", "№1.2.3")
        for bad in bad_tokens:
            with self.subTest(token=bad):
                self.assertEqual(bot.verify_admin_token(bad), 0)

    async def test_user_token_survives_non_ascii(self):
        # compare_digest на не-ASCII строках бросал исключение
        for bad in ("1.кириллица", "текст", "1.2.3.№"):
            with self.subTest(token=bad):
                self.assertEqual(bot.verify_user_token(bad), 0)

    async def test_admin_token_differs_from_client_token(self):
        # токен витрины не должен открывать админку
        self.assertEqual(bot.verify_admin_token(bot.user_token(1)), 0)


class StockCommandTests(BotTestCase):
    async def test_set_and_stop(self):
        await bot.set_stock(FakeMessage(user_id=1), Cmd("сырники 7"))
        self.assertEqual(self.stocks()["syrniki"], 7)
        await bot.stop_item(FakeMessage(user_id=1), Cmd("морс"))
        self.assertEqual(self.stocks()["mors"], 0)

    async def test_unknown_and_malformed(self):
        message = FakeMessage(user_id=1)
        message.replies = []
        await bot.set_stock(message, Cmd("нет такого 3"))
        self.assertIn("не найдено", message.replies[-1])
        message.replies = []
        await bot.set_stock(message, Cmd("сырники"))
        self.assertIn("Формат", message.replies[-1])

    async def test_non_admin_cannot_change_stock(self):
        message = FakeMessage(user_id=999)
        message.replies = []
        await bot.set_stock(message, Cmd("сырники 100"))
        self.assertEqual(self.stocks()["syrniki"], 15)
        self.assertEqual(message.replies, [])

    async def test_stock_listing(self):
        message = FakeMessage(user_id=1)
        message.replies = []
        await bot.stock(message)
        self.assertIn("Остатки", message.replies[-1])
        self.assertIn("syrniki", message.replies[-1])


if __name__ == "__main__":
    unittest.main()
