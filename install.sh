#!/usr/bin/env bash
# Установка «Успевайки» на сервер (Debian 12 / Ubuntu 22.04+).
#
#   sudo ./install.sh
#
# Всё в одном: спросит нужные данные, поставит зависимости, поднимет бота,
# настроит HTTPS и проверит, что всё работает. Повторный запуск — это
# обновление: база заказов и настройки сохраняются.
#
# Полное удаление — ./uninstall.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/uspevaika}"
APP_USER="${APP_USER:-uspevaika}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
TOTAL=8
STEP=0

# ---------- вывод ----------
if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; D=""; N=""
fi
step() { STEP=$((STEP+1)); printf '\n%s[%s/%s] %s%s\n' "$B" "$STEP" "$TOTAL" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
info() { printf '  %s•%s %s\n' "$D" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%sОшибка:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
ask()  {  # ask "вопрос" -> печатает ответ
  local prompt="$1" answer=""
  read -rp "  $prompt: " answer || true
  printf '%s' "$answer"
}
ask_secret() {
  local prompt="$1" answer=""
  read -rsp "  $prompt: " answer || true; echo
  printf '%s' "$answer"
}

[ "$(id -u)" -eq 0 ] || die "запускайте через sudo:  sudo ./install.sh"
[ -d "$SRC_DIR/app" ] || die "рядом со скриптом нет папки app/ — распакуйте архив целиком"

CLEAN=0
UNATTENDED=0
for arg in "$@"; do
  case "$arg" in
    --fresh) CLEAN=1 ;;
    # --update: обновление без вопросов. Нужен автообновлению: оно запускается
    # из cron, где нет терминала и отвечать на «введите токен» некого.
    --update|-y) UNATTENDED=1 ;;
  esac
done

printf '%s\n' "${B}Установка «Успевайки»${N}"
printf '%sTelegram-бот с витриной, заказами и панелью управления%s\n' "$D" "$N"
if [ -f "$APP_DIR/.env" ] || [ -f "$APP_DIR/orders.db" ]; then
  printf '\n%sОбнаружена прежняя установка в %s%s\n' "$Y" "$APP_DIR" "$N"
  info "Заказы и настройки будут сохранены (это обновление)."
  info "Начать с нуля, удалив данные — запустите:  sudo ./install.sh --fresh"
fi

# ---------- 1. система ----------
step "Проверяю систему"
. /etc/os-release 2>/dev/null || true
info "${PRETTY_NAME:-Linux}"

PY_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY_BIN="$(command -v "$candidate")"; break
  fi
done
[ -n "$PY_BIN" ] || die "нужен Python 3.10+. Установите: apt install -y python3 python3-venv"
ok "Python $($PY_BIN -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"

missing=""
for pkg in python3-venv curl; do
  if ! dpkg -s "$pkg" >/dev/null 2>&1; then missing="$missing $pkg"; fi
done
if ! "$PY_BIN" -c 'import venv' 2>/dev/null; then missing="$missing python3-venv"; fi
if [ -n "$missing" ]; then
  info "ставлю:$missing"
  apt-get update -qq
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $missing >/dev/null
fi
ok "системные пакеты на месте"

# ---------- 2. данные ----------
step "Настройки"

current_env() {  # current_env КЛЮЧ — значение из существующего .env
  [ -f "$APP_DIR/.env" ] || { printf ''; return; }
  grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^["'\'']//; s/["'\'']$//' || true
}

BOT_TOKEN="$(current_env BOT_TOKEN)"
WEBAPP_URL="$(current_env WEBAPP_URL)"
ADMIN_IDS="$(current_env ADMIN_IDS)"
ADMIN_CHAT_ID="$(current_env ADMIN_CHAT_ID)"
DOMAIN=""

if [ "$UNATTENDED" = "1" ]; then
  # Обновление: настройки уже есть, спрашивать нечего и некого.
  [ -n "$BOT_TOKEN" ] || die "обновление без вопросов, но в $APP_DIR/.env нет BOT_TOKEN.
Запустите установщик вручную: sudo ./install.sh"
  [ -n "$WEBAPP_URL" ] || die "обновление без вопросов, но в $APP_DIR/.env нет WEBAPP_URL.
Запустите установщик вручную: sudo ./install.sh"
  info "обновление без вопросов: настройки беру из существующего .env"
fi

if [ -f "$APP_DIR/.env" ] && [ "$CLEAN" = "0" ]; then
  printf '  В .env уже есть настройки. Enter — оставить прежнее значение.\n\n'
fi

# --- токен ---
while [ -z "$BOT_TOKEN" ] && [ "$UNATTENDED" = "0" ]; do
  printf '  Токен бота от @BotFather (создать: /newbot).\n'
  printf '  %sВыглядит так: 8123456789:AAH...%s\n' "$D" "$N"
  BOT_TOKEN="$(ask 'Токен')"
  case "$BOT_TOKEN" in
    *:*) ;;
    *) warn "в токене должно быть двоеточие — попробуйте ещё раз"; BOT_TOKEN="" ;;
  esac
done
ok "токен задан"

# --- домен и https-адрес ---
if [ -z "$WEBAPP_URL" ] && [ "$UNATTENDED" = "0" ]; then
  printf '\n  Домен для витрины (Telegram открывает её только по https).\n'
  printf '  %sУзнать свой: A-запись домена должна указывать на этот сервер.%s\n' "$D" "$N"
  while [ -z "$WEBAPP_URL" ]; do
    DOMAIN="$(ask 'Домен (например uspevaika.ru)')"
    DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%/}"
    if [ -z "$DOMAIN" ]; then warn "домен обязателен"; continue; fi
    WEBAPP_URL="https://$DOMAIN"
  done
fi
if [ -n "$WEBAPP_URL" ]; then
  DOMAIN="${WEBAPP_URL#https://}"; DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN%%/*}"
  ok "адрес витрины: $WEBAPP_URL"
fi

# --- Telegram id владельца ---
if [ -z "$ADMIN_IDS" ] && [ "$UNATTENDED" = "0" ]; then
  printf '\n  Ваш Telegram id — под ним открывается панель управления.\n'
  printf '  %sКак узнать: напишите боту /id (после первого запуска) или @userinfobot.%s\n' "$D" "$N"
  while [ -z "$ADMIN_IDS" ]; do
    ADMIN_IDS="$(ask 'Ваш Telegram id')"
    ADMIN_IDS="$(printf '%s' "$ADMIN_IDS" | tr -d '[:space:]')"
    case "$ADMIN_IDS" in
      ''|*[!0-9,]*) warn "нужны только цифры (несколько — через запятую)"; ADMIN_IDS="" ;;
    esac
  done
fi
[ -n "$ADMIN_IDS" ] && ok "менеджеры: $ADMIN_IDS"

# --- группа для заказов (необязательно) ---
if [ -z "$ADMIN_CHAT_ID" ] && [ "$UNATTENDED" = "0" ]; then
  printf '\n  %sНеобязательно:%s группа, куда падают заказы.\n' "$Y" "$N"
  printf '  %sСоздайте группу, добавьте туда бота, напишите в ней /id и вставьте chat id.\n' "$D" "$N"
  printf '  %sEnter — пропустить: заказы будут приходить в личку менеджерам.%s\n' "$D" "$N"
  ADMIN_CHAT_ID="$(ask 'Chat id группы (или Enter)')"
  ADMIN_CHAT_ID="$(printf '%s' "$ADMIN_CHAT_ID" | tr -d '[:space:]')"
fi
[ -n "$ADMIN_CHAT_ID" ] && ok "заказы → чат $ADMIN_CHAT_ID" || info "заказы будут приходить в личку менеджерам"

# ---------- 3. файлы ----------
step "Копирую файлы в $APP_DIR"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  ok "пользователь $APP_USER создан"
else
  ok "пользователь $APP_USER уже есть"
fi

if [ "$CLEAN" = "1" ]; then
  info "режим --fresh: удаляю прежние данные"
  rm -f "$APP_DIR/orders.db" "$APP_DIR/orders.db-wal" "$APP_DIR/orders.db-shm" "$APP_DIR/.env"
fi

mkdir -p "$APP_DIR"

# menu.json — это НЕ файл программы, а данные: в нём остатки, стоп-листы и
# правки цен из админки. При обновлении его нельзя затирать версией из релиза,
# иначе склад «обнулится» до исходных цифр. Так же бережём базу и .env.
if [ -f "$APP_DIR/menu.json" ]; then
  stamp="$(date +%Y%m%d-%H%M%S)"
  cp -a "$APP_DIR/menu.json" "$APP_DIR/menu.json.bak.$stamp"
  info "сохраняю текущее меню (резерв: menu.json.bak.$stamp)"
fi
[ -f "$APP_DIR/orders.db" ] && cp -a "$APP_DIR/orders.db" "$APP_DIR/orders.db.bak.$stamp" 2>/dev/null || true

# Копируем код и статику. Данные исключаем: они уже на месте.
for item in "$SRC_DIR/app/"*; do
  name="$(basename "$item")"
  case "$name" in
    menu.json|orders.db) continue ;;         # данные, не трогаем
    webapp)                                  # фото — отдельно, ниже
      mkdir -p "$APP_DIR/webapp"
      for f in "$item"/*; do
        [ -e "$f" ] || continue
        [ "$(basename "$f")" = "img" ] && continue
        cp -r "$f" "$APP_DIR/webapp/"
      done
      continue ;;
  esac
  if [ -d "$item" ]; then
    cp -r "$item" "$APP_DIR"/
  else
    cp "$item" "$APP_DIR"/
  fi
done

# Фото: новые из релиза добавляем, существующие не трогаем — их могли
# заменить через админку, и затирать чужую работу нельзя.
if [ -d "$SRC_DIR/app/webapp/img" ]; then
  mkdir -p "$APP_DIR/webapp/img"
  cp -rn "$SRC_DIR/app/webapp/img/." "$APP_DIR/webapp/img/" 2>/dev/null || true
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 755 "$APP_DIR"
ok "код и витрина обновлены, данные сохранены"

# ---------- 4. окружение Python ----------
step "Ставлю зависимости Python"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  sudo -u "$APP_USER" "$PY_BIN" -m venv "$APP_DIR/.venv"
  ok "виртуальное окружение создано"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
ok "aiogram, aiohttp, Pillow установлены"

# ---------- 5. .env ----------
step "Сохраняю настройки"
umask 077

# Настройки, которых мы не спрашивали, но которые могли быть прописаны руками:
# стирать их при обновлении нельзя.
env_value() {  # env_value КЛЮЧ ЗНАЧЕНИЕ_ПО_УМОЛЧАНИЮ
  local v; v="$(current_env "$1")"
  printf '%s' "${v:-$2}"
}
ADMIN_PASSWORD="$(env_value ADMIN_PASSWORD "")"
PORT_VAL="$(env_value PORT "8080")"
BIND_HOST="$(env_value BIND_HOST "127.0.0.1")"
TELEGRAM_PROXY="$(env_value TELEGRAM_PROXY "")"
MENU_PATH="$(env_value MENU_PATH "")"
DB_PATH="$(env_value DB_PATH "")"

cat > "$APP_DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
WEBAPP_URL=$WEBAPP_URL
ADMIN_IDS=$ADMIN_IDS
ADMIN_CHAT_ID=$ADMIN_CHAT_ID
ADMIN_PASSWORD=$ADMIN_PASSWORD
PORT=$PORT_VAL
BIND_HOST=$BIND_HOST
TELEGRAM_PROXY=$TELEGRAM_PROXY
MENU_PATH=$MENU_PATH
DB_PATH=$DB_PATH
EOF
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
ok "$APP_DIR/.env (права 600, виден только владельцу)"

# ---------- 6. служба ----------
step "Ставлю службу systemd"

# Настройки автообновления. Репозиторий можно не задавать — тогда обновление
# работает только вручную (./autoupdate.sh скажет, что нужно его указать).
CONF="/etc/uspevaika-update.conf"
if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<EOF
# Автообновление «Успевайки» (используется ./autoupdate.sh и systemd-таймером).
# Репозиторий GitHub в виде "владелец/репозиторий".
GITHUB_REPO="${GITHUB_REPO:-}"
EOF
  chmod 644 "$CONF"
  info "создан $CONF — впишите GITHUB_REPO для автообновления"
fi

install -m 644 "$SRC_DIR/uspevaika.service" /etc/systemd/system/uspevaika.service

# Автообновление: скрипт в рабочую папку и таймер (включаем, только если
# репозиторий задан — иначе таймер каждую ночь писал бы ошибку в журнал).
install -m 755 "$SRC_DIR/autoupdate.sh" "$APP_DIR/autoupdate.sh"
install -m 644 "$SRC_DIR/uspevaika-update.service" /etc/systemd/system/uspevaika-update.service
install -m 644 "$SRC_DIR/uspevaika-update.timer" /etc/systemd/system/uspevaika-update.timer
GITHUB_REPO_VAL="$(grep -E '^GITHUB_REPO=' "$CONF" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
if [ -n "$GITHUB_REPO_VAL" ]; then
  systemctl enable --now uspevaika-update.timer >/dev/null 2>&1 && \
    ok "автообновление включено: раз в сутки, репозиторий $GITHUB_REPO_VAL"
else
  systemctl disable uspevaika-update.timer >/dev/null 2>&1 || true
  info "автообновление выключено: впишите GITHUB_REPO в $CONF и включите"
  info "  sudo systemctl enable --now uspevaika-update.timer"
fi

systemctl daemon-reload
systemctl enable uspevaika >/dev/null 2>&1
systemctl restart uspevaika
info "жду, пока бот подключится к Telegram…"
sleep 4
if systemctl is-active --quiet uspevaika; then
  ok "служба запущена"
else
  warn "служба не запустилась — вот что в журнале:"
  journalctl -u uspevaika -n 15 --no-pager | sed 's/^/    /'
fi

# ---------- 7. проверка бота ----------
step "Проверяю связь с Telegram"
if curl -fsS -m 5 "http://127.0.0.1:8080/healthz" >/dev/null 2>&1; then
  ok "веб-часть отвечает"
else
  warn "бот не отвечает на localhost:8080 — смотрите: journalctl -u uspevaika -n 30"
fi
if journalctl -u uspevaika -n 60 --no-pager 2>/dev/null | grep -q "на связи"; then
  ok "Telegram принял токен"
elif journalctl -u uspevaika -n 60 --no-pager 2>/dev/null | grep -qi "Unauthorized"; then
  die "Telegram отверг токен. Проверьте BOT_TOKEN в $APP_DIR/.env и перезапустите:
    sudo systemctl restart uspevaika"
elif journalctl -u uspevaika -n 60 --no-pager 2>/dev/null | grep -qi "NetworkError\|timeout"; then
  warn "нет связи с api.telegram.org"
  info "если сервер ходит в интернет через прокси — задайте TELEGRAM_PROXY в $APP_DIR/.env"
else
  info "проверка по журналу не удалась — это не обязательно ошибка"
fi

# ---------- 8. HTTPS ----------
step "Настраиваю HTTPS (домен $DOMAIN)"
https_ok=0
if curl -fsS -m 12 "https://$DOMAIN/healthz" >/dev/null 2>&1; then
  ok "HTTPS уже работает"
  https_ok=1
elif [ -f "$SRC_DIR/deploy/setup-caddy.sh" ] && command -v caddy >/dev/null 2>&1; then
  info "ставлю Caddy"
  if "$SRC_DIR/deploy/setup-caddy.sh" "$DOMAIN" >>/tmp/uspevaika-https.log 2>&1; then
    https_ok=1; ok "HTTPS настроен через Caddy, сертификат получен"
  else
    warn "Caddy не справился — подробности: /tmp/uspevaika-https.log"
  fi
else
  printf '  Поставить HTTPS автоматически (Caddy, сертификат Let\x27s Encrypt)? [Д/н] '
  read -rp '' do_https || do_https="д"
  case "$do_https" in
    [дДyY]*)
      info "ставлю Caddy и получаю сертификат…"
      if [ -f "$SRC_DIR/deploy/setup-caddy.sh" ] && \
         "$SRC_DIR/deploy/setup-caddy.sh" "$DOMAIN" >>/tmp/uspevaika-https.log 2>&1; then
        https_ok=1; ok "HTTPS настроен, сертификат получен"
      else
        warn "автонастройка не удалась — подробности: /tmp/uspevaika-https.log"
      fi
      ;;
    *) info "пропускаю. Без https кнопка витрины в Telegram не откроется." ;;
  esac
fi

if [ "$https_ok" = "0" ]; then
  warn "HTTPS не настроен. Telegram не откроет витрину, пока его нет."
  info "пошагово: см. DOCS.md, раздел «Настройте HTTPS»"
fi

# ---------- итог ----------
printf '\n%s════════════════════════════════════════%s\n' "$B" "$N"
if [ "$https_ok" = "1" ]; then
  printf '%sГотово. Бот работает.%s\n\n' "$G$B" "$N"
else
  printf '%sБот установлен, но HTTPS пока нет.%s\n\n' "$Y$B" "$N"
fi
cat <<EOF
  Витрина:   $WEBAPP_URL $([ "$https_ok" = "1" ] && echo "" || echo "(пока недоступна)")
  Настройки: $APP_DIR/.env
  Заказы:    $APP_DIR/orders.db
  Логи:      journalctl -u uspevaika -f

  ${B}Что сделать сейчас:${N}
   1. Откройте бота в Telegram и отправьте /start
   2. Нажмите «🛒 Меню и заказ» — витрина должна открыться
   3. Оформите тестовый заказ — он придёт $([ -n "$ADMIN_CHAT_ID" ] && echo "в чат" || echo "вам в личку")

  ${B}Полезное:${N}
   sudo ./deploy/doctor.sh $DOMAIN     диагностика любой проблемы
   sudo ./uninstall.sh                 удалить всё
   sudo ./install.sh                   обновить (данные сохранятся)
EOF
printf '%s════════════════════════════════════════%s\n' "$B" "$N"
