# «Успевайка» — установка в продакшен

Telegram-бот с витриной (Mini App), приёмом заказов, панелью управления и статистикой.
Ниже — всё, чтобы поднять его на сервере с нуля.

---

## Что нужно

- Сервер с Debian 12 / Ubuntu 22.04+ (хватит 1 CPU, 1 ГБ RAM, 10 ГБ диска)
- Домен, направленный A-записью на IP сервера
- Бот в Telegram, созданный у [@BotFather](https://t.me/BotFather)
- Python 3.10+ (на Debian 12 и Ubuntu 22.04 уже есть)

Почему домен обязателен: Telegram открывает Mini App только по `https://`. Заказы при этом принимаются и по http, но кнопка меню работать не будет.

---

## Установка за 2 шага

### 1. Скопируйте релиз на сервер

```bash
scp uspevaika-release-*.tar.gz user@server:/tmp/
ssh user@server
mkdir -p ~/uspevaika && tar -xzf /tmp/uspevaika-release-*.tar.gz -C ~/uspevaika
cd ~/uspevaika
```

После распаковки должны быть: `install.sh`, `uninstall.sh`, `deploy/`, `DOCS.md`, `ЧИТАЙ-МЕНЯ.md` и папка `app/`.

### 2. Запустите установщик и ответьте на вопросы

```bash
sudo ./install.sh
```

Он **спросит всё по ходу установки** — ничего не придётся искать в файлах потом:

```
1. Токен бота от @BotFather       (выглядит так: 8123456789:AAH...)
2. Домен для витрины              (например uspevaika.ru)
3. Ваш Telegram id                (узнать: /id в боте или у @userinfobot)
4. Chat id группы — необязательно (Enter — заказы пойдут вам в личку)
```

Дальше скрипт всё делает сам: зависимости, файлы, `.env`, служба, **HTTPS через Caddy** (спросит подтверждение) и финальная проверка, что бот вышел на связь.

Проверки ввода встроены:

- домен, введённый с `https://` или со слэшем на конце, очищается сам;
- токен без двоеточия — переспросит с объяснением;
- id с буквами — переспросит;
- при повторном запуске (это обновление) подставляет прежние значения: Enter оставляет как было.

### 3. Проверьте результат

```bash
sudo ./deploy/doctor.sh ваш-домен.ru
```

Должно быть «установка в порядке». В Telegram: `/start` → «🛒 Меню и заказ» → тестовый заказ.

---

## Полное удаление

```bash
sudo ./uninstall.sh            # спросит, что удалять
sudo ./uninstall.sh --all      # удалить всё, включая заказы (без вопросов)
sudo ./uninstall.sh --keep-data # снять программу, данные сохранить
```

Скрипт спрашивает объём:

```
1) только программу, заказы и настройки оставить    (рекомендую)
2) программу, настройки прокси и сертификаты; заказы оставить
3) всё: программу, заказы, настройки, прокси, сертификаты
```

Что удаляется: служба systemd, файлы в `/opt/uspevaika`, системный пользователь,
конфиг nginx или Caddy, сертификат Let's Encrypt, а также **возвращается
стандартная страница nginx**, если мы её отключали.

Перед удалением данных **создаётся архив**: `/root/uspevaika-data-<дата>.tar.gz`
(в него попадают `.env`, `orders.db`, `menu.json`). Вернуть:

```bash
sudo mkdir -p /opt/uspevaika && sudo tar -xzf /root/uspevaika-data-*.tar.gz -C /opt/uspevaika
```

Бота из группы Telegram нужно убрать вручную — скрипт этого сделать не может.

---

## HTTPS и домен

Выберите один вариант. **Оба оформлены скриптами** — руками конфиг править не нужно, и не будет забытого шага вроде подключения сайта.

**Вариант А — Caddy (проще: сертификат сам, продлевается сам)**

```bash
cd ~/uspevaika
sudo ./deploy/setup-caddy.sh ваш-домен.ru
```

Скрипт поставит Caddy, создаст конфиг, получит сертификат Let's Encrypt и проверит ответ по https. Затем пропишите адрес боту:

```bash
sudo nano /opt/uspevaika/.env
#   WEBAPP_URL=https://ваш-домен.ru
#   BIND_HOST=127.0.0.1
sudo systemctl restart uspevaika
```

**Вариант Б — nginx**

```bash
cd ~/uspevaika
sudo ./deploy/setup-nginx.sh ваш-домен.ru
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d ваш-домен.ru
```

> **Почему видно «Welcome to nginx!».** У nginx после установки включён сайт по умолчанию
> (`/etc/nginx/sites-enabled/default`), и он перехватывает любой домен, отвечая своей
> страницей-заглушкой. Недостаточно положить конфиг в `sites-available` — его надо
> **подключить** симлинком в `sites-enabled`, а дефолтный — **отключить**. Скрипт
> `setup-nginx.sh` делает и то, и другое. Если настраиваете вручную:
>
> ```bash
> sudo ln -s /etc/nginx/sites-available/uspevaika /etc/nginx/sites-enabled/uspevaika
> sudo rm /etc/nginx/sites-enabled/default        # ← без этого будет заглушка
> sudo nginx -t && sudo systemctl reload nginx
> ```

Что должно быть в конфиге (ключевое):

```nginx
client_max_body_size 12m;                          # иначе фото из админки → 413
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header X-Forwarded-Proto $scheme;    # по нему бот понимает, что сайт на https
    proxy_set_header Host $host;
}
```

### Если что-то не открывается — запустите диагностику

```bash
cd ~/uspevaika
sudo ./deploy/doctor.sh ваш-домен.ru
```

Скрипт идёт по цепочке (служба → порт → ответ бота → `.env` → прокси → домен → HTTPS) и показывает пункт, где обрыв, вместе с готовой командой исправления.

### Ручной перезапуск и логи

```bash
sudo systemctl restart uspevaika
sudo systemctl status uspevaika
journalctl -u uspevaika -f
```

В логе должно быть:

```
Бот @ваш_бот на связи
Start polling
```

Проверка снаружи:

```bash
curl https://ваш-домен/healthz      # {"ok": true, "items": 24}
```

В Telegram: `/start` → кнопка «🛒 Меню и заказ» → оформите тестовый заказ.

---

## Обновление

Три команды:

```bash
# 1. бэкап данных (на всякий случай — установщик и сам страхует)
sudo tar -czf ~/uspevaika-backup-$(date +%F).tar.gz \
    -C /opt/uspevaika .env orders.db menu.json

# 2. распаковать новый релиз и запустить установщик
mkdir -p ~/uspevaika-new && tar -xzf новый-релиз.tar.gz -C ~/uspevaika-new
cd ~/uspevaika-new && sudo ./install.sh

# 3. проверить
sudo ./deploy/doctor.sh ваш-домен.ru
```

Установщик **спросит то же, что при первой установке** — просто жмите Enter: прежние
значения подставлены и сохранятся. Перезапуск службы делается автоматически.

### Что обновляется, а что нет

| | Обновляется | Сохраняется |
|---|---|---|
| `bot.py`, `admin.py` | ✅ | |
| `webapp/index.html`, `admin.html` | ✅ | |
| `.env` (токен, домен, менеджеры) | | ✅ |
| `orders.db` (заказы, менеджеры из чата) | | ✅ |
| `menu.json` (остатки, цены, стоп-листы) | | ✅ |
| `webapp/img/` — ваши фото из админки | | ✅ |

**`menu.json` — это данные, а не часть программы.** Остатки, которые вы ведёте в
админке, при обновлении не сбрасываются. Перед обновлением создаются резервные
копии с датой:

```
/opt/uspevaika/menu.json.bak.20261125-084430
/opt/uspevaika/orders.db.bak.20261125-084430
```

Если что-то пошло не так — вернуть:

```bash
sudo systemctl stop uspevaika
sudo cp /opt/uspevaika/menu.json.bak.20261125-084430 /opt/uspevaika/menu.json
sudo systemctl start uspevaika
```

Фото из релиза добавляются только те, которых у вас нет; уже загруженные через
админку не перезаписываются.

### Начать с нуля

Если нужно сбросить остатки и заказы к исходным из релиза:

```bash
sudo ./install.sh --fresh
```

⚠️ Удаляет `orders.db`, `.env` и вернёт исходный `menu.json` — бэкапа не будет,
сделайте его вручную заранее.

---

## Автообновление из GitHub

Сервер может сам проверять релизы на GitHub и ставить новые версии ночью.

### Как это устроено

1. В приложении есть файл `VERSION` (сейчас `1.0.0`), и бот отдаёт его в `/healthz`.
2. `autoupdate.sh` спрашивает у GitHub последний релиз.
3. Если версия новее — скачивает архив, **сверяет sha256**, распаковывает.
4. Запускает `install.sh --update` (без вопросов, данные сохраняются).
5. Ждёт, пока `/healthz` ответит с новой версией.
6. Если что-то не так — **откатывается** на прежнюю версию автоматически.

### Настройка

**1. Создайте репозиторий на GitHub** (можно приватный — см. ниже) и впишите его:

```bash
sudo nano /etc/uspevaika-update.conf
#   GITHUB_REPO="ваш-логин/uspevaika"
```

**2. Включите таймер** (проверка раз в сутки в 04:17):

```bash
sudo systemctl enable --now uspevaika-update.timer
systemctl list-timers uspevaika-update.timer
```

**3. Проверьте вручную, что всё работает:**

```bash
sudo /opt/uspevaika/autoupdate.sh --check
```

### Как выпускать релиз

```bash
# 1. поднимите версию — иначе сервер не увидит обновление
echo "1.0.1" > app/VERSION

# 2. соберите архив и контрольную сумму
python3 make-release.py
sha256sum uspevaika-release-*.tar.gz > uspevaika-release.sha256

# 3. загрузите на GitHub как релиз с тегом v1.0.1
gh release create v1.0.1 \
    uspevaika-release-2026-11-25.tar.gz \
    uspevaika-release.sha256 \
    --title "v1.0.1" --notes "Что изменилось"
```

**Имя тега обязательно вида `v1.0.1`**, а к релизу нужно приложить **два файла**:
архив `.tar.gz` и `.sha256` рядом с ним. Без `.sha256` обновление работает, но
только с проверкой распаковки — предупреждение появится в журнале.

### Команды

| Команда | Что делает |
|---|---|
| `sudo autoupdate.sh` | проверить и обновить, если есть новее |
| `sudo autoupdate.sh --check` | только проверить (код 1 = есть новее) |
| `sudo autoupdate.sh --force` | переустановить текущую версию |
| `sudo autoupdate.sh --to 1.0.0` | поставить конкретную версию (откат) |

`--check` удобно вешать на мониторинг: код возврата 1 означает «доступно обновление».

### Что защищает от поломок

- **Контрольная сумма.** Подменённый или битый архив не установится.
- **Резерв данных** перед установкой: `/var/lib/uspevaika-update/backup-*.tar.gz`.
- **Копия прежней версии** для отката: `/var/lib/uspevaika-update/prev-*`.
- **Проверка после обновления.** Не поднялся бот — автоматический откат.
- **Чистка старых резервов** — держатся последние 3, диск не забьётся.

### Журнал и ручное управление

```bash
journalctl -u uspevaika-update -f        # что делает автообновление
systemctl list-timers uspevaika-update   # когда следующая проверка
sudo systemctl disable --now uspevaika-update.timer   # выключить
```

### Ограничения, о которых стоит знать

- **Обновление идёт при остановке бота на 10–30 секунд.** Ночью это незаметно,
  днём — может оборвать оформление заказа. Таймер по умолчанию на 04:17.
- **Приватный репозиторий требует токена.** Создайте read-only токен на GitHub
  и добавьте в конфиг: `GITHUB_TOKEN="ghp_..."`, затем в `autoupdate.sh`
  используйте `-H "Authorization: Bearer $GITHUB_TOKEN"` в запросах к API.
- **Обновление не откатывает схему базы.** SQLite-миграции в проекте только
  добавляют колонки — откат на старую версию не сломает заказы.
- **Автообновление — не замена бэкапу.** Данные защищены, но если новая версия
  сама содержит ошибку, откат вернёт код, а не исправит логику. Периодически
  копируйте `orders.db` и `menu.json`.

---

## Резервная копия

Всё состояние — это два файла:

```bash
sudo tar -czf ~/uspevaika-$(date +%F).tar.gz \
    -C /opt/uspevaika .env orders.db menu.json webapp/img
```

Восстановление — распаковать поверх. Живая база работает в режиме WAL, поэтому останавливайте службу перед копированием или используйте:

```bash
sudo -u uspevaika sqlite3 /opt/uspevaika/orders.db ".backup /tmp/orders-copy.db"
```

Проверьте восстановление на копии, прежде чем на нём понадеяться.

---

## Перенос накопленных данных

Если бот уже работал (например, на ноутбуке через туннель), перенесите:

```bash
# на старой машине
tar -czf data.tar.gz orders.db menu.json webapp/img .env

# на сервере
sudo systemctl stop uspevaika
sudo tar -xzf data.tar.gz -C /opt/uspevaika
sudo chown -R uspevaika:uspevaika /opt/uspevaika
sudo systemctl start uspevaika
```

`menu.json` содержит остатки, `orders.db` — заказы и менеджеров, добавленных из чата.

---

## Ежедневная работа

| Что | Команда |
|---|---|
| Логи | `journalctl -u uspevaika -f` |
| Логи за сегодня | `journalctl -u uspevaika --since today` |
| Перезапуск после правки `.env` | `sudo systemctl restart uspevaika` |
| Остановить | `sudo systemctl stop uspevaika` |
| Проверка живости | `curl -s localhost:8080/healthz` |

В Telegram менеджеру доступны: `/menu` (кнопки), `/orders`, `/history`, `/stock`, `/admins`, `/addadmin`, `/admin`.

Панель управления: ссылка из `/admin` или кнопка «🛠 Админка».

---

## Если что-то не так

**Бот молчит, в логе `TelegramNetworkError`**
Сервер не достаёт до Telegram. Проверьте: `curl -s -m 5 https://api.telegram.org`. Если закрыт — нужен прокси, задайте `TELEGRAM_PROXY=http://…` и перезапустите.

**По адресу сайта видно «Welcome to nginx!»**
nginx отдаёт свою страницу-заглушку: ваш конфиг не подключён либо включён сайт по умолчанию.
```bash
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sfn /etc/nginx/sites-available/uspevaika /etc/nginx/sites-enabled/uspevaika
sudo nginx -t && sudo systemctl reload nginx
```
Или запустите `sudo ./deploy/setup-nginx.sh ваш-домен.ru` — он делает это сам.

**Кнопка «Меню и заказ» не открывается**
`WEBAPP_URL` не по https, со слэшем на конце или не совпадает с доменом. Должно быть точно `https://uspevaika.ru`.

**Сайт отдаёт 502 Bad Gateway**
Прокси работает, а бот — нет. `sudo systemctl status uspevaika`, затем `journalctl -u uspevaika -n 30`.
Часто причина — `BIND_HOST`: если в `.env` стоит `127.0.0.1`, а прокси на другом хосте (не на этом сервере), бот недоступен.

**Заказы приходят, но статусы не меняются**
Ваш id не в `ADMIN_IDS`. Узнать: `/id`, вписать, перезапустить.

**В админке не запоминается вход**
Прокси не передаёт `X-Forwarded-Proto: https` — бот не знает, что сайт на https, и ставит куку без флага Secure. Добавьте заголовок в nginx (см. вариант Б).

**Фото не загружается, ошибка 413**
В nginx нет `client_max_body_size 12m`.

**Порт занят при запуске**
`ss -ltnp | grep 8080` покажет процесс. Если это прежний запуск бота — `sudo systemctl kill uspevaika` и запуск заново.

---

## Что внутри

| Файл | Назначение |
|---|---|
| `bot.py` | бот, веб-сервер витрины, приём заказов |
| `admin.py` | панель управления и её API |
| `webapp/index.html` | витрина для клиентов |
| `webapp/admin.html` | панель управления |
| `webapp/img/` | фото блюд (WebP + retina) |
| `menu.json` | меню, цены, остатки, условия доставки |
| `orders.db` | заказы и менеджеры (SQLite, создаётся сам) |
| `start.sh` | ручной запуск (для разработки) |
| `tests/` | 182 проверки, запускаются без сети |

Полное описание функций — `ОПИСАНИЕ-БОТА.md`.
