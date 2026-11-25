#!/usr/bin/env bash
# Запуск прототипа «Успевайка».
#
# Настройки читаются из .env. Чего в нём нет — спрашивается здесь и запоминается
# в .env, чтобы в следующий раз не вводить (файл создаётся с правами 600 и
# исключён из git).
#
#   ./start.sh                 # витрина + бот
#   ./start.sh --web-only      # только витрина, без бота
set -euo pipefail
cd "$(dirname "$0")"

WEB_ONLY=0
[ "${1:-}" = "--web-only" ] && WEB_ONLY=1

PY=""
for candidate in .venv-linux/bin/python .venv/bin/python python3; do
  if [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Не найден python. Создайте окружение: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [ -f .env ]; then
  chmod 600 .env 2>/dev/null || true
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

save_env() {  # save_env ПЕРЕМЕННАЯ ЗНАЧЕНИЕ — запомнить в .env
  local var="$1" value="$2" tmp
  tmp="$(mktemp .env.tmp.XXXXXX)"
  if [ -f .env ]; then
    grep -v "^${var}=" .env > "$tmp" || true
  fi
  printf '%s=%s\n' "$var" "$value" >> "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" .env
}

ask() {  # ask ПЕРЕМЕННАЯ "Подсказка" [секретно]
  local var="$1" prompt="$2" secret="${3:-}"
  [ -n "${!var:-}" ] && return 0

  # Без терминала (автозапуск, планировщик, systemd) спрашивать некого: любой
  # read вернул бы ошибку, а set -e из-за неё завершил бы скрипт — бот не
  # запустился бы вовсе из-за необязательного поля вроде ADMIN_CHAT_ID.
  if [ ! -t 0 ]; then
    echo "Пропускаю «${var}»: нет терминала для ввода (задайте в .env)."
    return 0
  fi

  if [ -n "$secret" ]; then
    read -rsp "$prompt" "$var" || true; echo
  else
    read -rp "$prompt" "$var" || true
  fi
  if [ -n "${!var:-}" ]; then
    export "${var?}"
    save_env "$var" "${!var}"
  fi
}

if [ "$WEB_ONLY" = "0" ]; then
  ask BOT_TOKEN "Токен бота от @BotFather (Enter — если уже сохранён): " secret
  ask WEBAPP_URL "HTTPS-адрес витрины (например, https://xxx.trycloudflare.com): "
  ask ADMIN_CHAT_ID "Чат менеджеров (id, можно пусто): "
  ask ADMIN_IDS "user id менеджеров через запятую (можно пусто): "
fi

# aiogram не читает HTTPS_PROXY сам — передаём прокси боту явно.
# Если Telegram доступен напрямую, переменная просто игнорируется.
if [ -z "${TELEGRAM_PROXY:-}" ] && [ -n "${HTTPS_PROXY:-}" ]; then
  export TELEGRAM_PROXY="$HTTPS_PROXY"
fi

PORT="${PORT:-8080}"
export PORT

if [ "$WEB_ONLY" = "0" ]; then
  echo
  echo "Витрина:  http://localhost:${PORT}"
  echo "Для Telegram нужен HTTPS — держите рядом запущенным:"
  echo "  cloudflared tunnel --url http://127.0.0.1:${PORT}"
  echo "и укажите выданный https-адрес в WEBAPP_URL."
  echo
fi

if [ -n "${TELEGRAM_PROXY:-}" ]; then
  echo "Telegram: через прокси ${TELEGRAM_PROXY}"
fi

# --- порт ---
# Частая поломка: предыдущий запуск не завершили, порт занят, и bot.py падает
# стектрейсом «address already in use». Вместо этого показываем, кто держит
# порт, и предлагаем остановить его.

# Порядок способов важен: ss показывает чужие процессы без прав root не всегда,
# поэтому, если pid не нашёлся, пробуем lsof и fuser — они знают больше.
port_pid() {  # port_pid ПОРТ — печатает pid слушающего процесса
  local port="$1" pid=""
  if command -v ss >/dev/null 2>&1; then
    pid=$(ss -ltnpH "sport = :$port" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  fi
  if [ -z "$pid" ] && command -v lsof >/dev/null 2>&1; then
    pid=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1)
  fi
  if [ -z "$pid" ] && command -v fuser >/dev/null 2>&1; then
    pid=$(fuser "$port"/tcp 2>/dev/null | tr -dc '0-9')
  fi
  echo "${pid:-}"
}

pid_cmd() {  # pid_cmd PID — командная строка процесса
  if [ -r "/proc/$1/cmdline" ]; then
    tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | sed 's/ *$//' || echo "неизвестный процесс"
  else
    ps -p "$1" -o command= 2>/dev/null || echo "неизвестный процесс"
  fi
}

# Наш ли это бот: запущен из этой папки и это bot.py.
# Останавливать без спроса можно только свой — чужой процесс трогать нельзя.
is_our_bot() {  # is_our_bot PID
  local pid="$1" cwd
  case "$(pid_cmd "$pid")" in
    *bot.py*) ;;
    *) return 1 ;;
  esac
  if [ -e "/proc/$pid/cwd" ]; then
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
    [ "$cwd" = "$(pwd -P)" ] || return 1
  fi
  return 0
}

wait_port_free() {  # wait_port_free ПОРТ — ждёт, пока порт освободится
  local port="$1" waited=0
  while [ "$waited" -lt 10 ]; do
    [ -z "$(port_pid "$port")" ] && return 0
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

PID="$(port_pid "$PORT")"
if [ -n "$PID" ]; then
  CMD="$(pid_cmd "$PID")"
  echo
  echo "Порт ${PORT} занят: процесс ${PID}"
  echo "  ${CMD}"
  if is_our_bot "$PID"; then
    echo "Это прежний запуск бота из этой папки — он работает со старым кодом."
    ANSWER=""
    if [ -t 0 ]; then
      read -rp "Остановить его и запустить заново? [Д/н] " ANSWER
      ANSWER="${ANSWER:-д}"
    else
      ANSWER="д"
      echo "Останавливаю автоматически (запуск без терминала)."
    fi
    case "$ANSWER" in
      [дДyY]*)
        # Сначала вежливо (SIGTERM — бот закроет соединения и завершится сам),
        # и только если не помогло, принудительно.
        kill "$PID" 2>/dev/null || true
        if ! wait_port_free "$PORT"; then
          echo "Процесс ${PID} не завершился сам — принудительно останавливаю."
          kill -9 "$PID" 2>/dev/null || true
          wait_port_free "$PORT" || {
            echo "Порт ${PORT} всё ещё занят. Остановите процесс вручную: kill -9 $PID" >&2
            exit 1
          }
        fi
        echo "Прежний процесс остановлен, порт ${PORT} свободен."
        ;;
      *)
        echo "Ничего не меняю. Остановите процесс сами: kill $PID" >&2
        exit 1
        ;;
    esac
  else
    echo "Это не наш бот. Порт нужен свободным, а останавливать чужой процесс"
    echo "без спроса нельзя. Варианты:" >&2
    echo "  • остановить его: kill $PID" >&2
    echo "  • запустить на другом порту: PORT=8090 ./start.sh" >&2
    exit 1
  fi
fi

exec "$PY" bot.py
