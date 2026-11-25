#!/usr/bin/env bash
# Диагностика установки «Успевайки». Запускать на сервере:
#     sudo ./doctor.sh [домен]
#
# Проверяет по порядку: службу, порт, ответ бота, настройки, прокси, HTTPS.
# Показывает, где именно обрыв, вместо «что-то не работает».
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/uspevaika}"
DOMAIN="${1:-}"
PORT="8080"
ok=0; bad=0

good() { printf '  \033[32m✓\033[0m %s\n' "$*"; ok=$((ok+1)); }
bad_() { printf '  \033[31m✗\033[0m %s\n' "$*"; bad=$((bad+1)); }
note() { printf '    %s\n' "$*"; }

hdr() { printf '\n\033[1m%s\033[0m\n' "$*"; }

hdr "1. Служба бота"
if systemctl is-active --quiet uspevaika; then
  good "uspevaika запущена"
else
  bad_ "uspevaika не запущена"
  note "sudo systemctl start uspevaika"
  note "причина: sudo journalctl -u uspevaika -n 30 --no-pager"
fi

hdr "2. Порт"
listening=$(ss -ltn 2>/dev/null | grep -c ":${PORT} ")
if [ "$listening" -gt 0 ]; then
  good "порт ${PORT} слушается"
  addr=$(ss -ltn 2>/dev/null | awk -v p=":${PORT} " '$0 ~ p {print $4; exit}')
  note "адрес: ${addr}"
  case "$addr" in
    127.0.0.1:*|\[::1\]:*)
      note "только локально — это правильно за прокси" ;;
    *)
      note "слушает снаружи. Если есть nginx/Caddy, поставьте BIND_HOST=127.0.0.1" ;;
  esac
else
  bad_ "порт ${PORT} никто не слушает"
fi

hdr "3. Ответ бота"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 6 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || echo 000)
if [ "$code" = "200" ]; then
  good "бот отвечает на /healthz (200)"
else
  bad_ "бот не отвечает на 127.0.0.1:${PORT} (код ${code})"
fi

hdr "4. Настройки .env"
if [ -f "$APP_DIR/.env" ]; then
  perms=$(stat -c '%a' "$APP_DIR/.env" 2>/dev/null || echo "?")
  [ "$perms" = "600" ] && good ".env с правами 600" || bad_ ".env с правами ${perms} (нужно 600)"
  # значения не печатаем — только факт заполнения
  for var in BOT_TOKEN WEBAPP_URL ADMIN_IDS; do
    if grep -qE "^${var}=.+" "$APP_DIR/.env"; then
      good "${var} заполнен"
    else
      bad_ "${var} пуст"
    fi
  done
  value=$(grep -E '^WEBAPP_URL=' "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"'')
  case "$value" in
    https://*) [ "${value%/}" = "$value" ] && good "WEBAPP_URL без слэша на конце" \
                 || note "уберите слэш на конце WEBAPP_URL: ${value}" ;;
    "") : ;;
    *) bad_ "WEBAPP_URL не https: ${value} — Telegram не откроет витрину" ;;
  esac
else
  bad_ "нет $APP_DIR/.env"
fi

hdr "5. Прокси (nginx / Caddy)"
if systemctl is-active --quiet nginx 2>/dev/null; then
  good "nginx работает"
  if [ -e /etc/nginx/sites-enabled/default ]; then
    bad_ "включён дефолтный сайт nginx — он перехватывает домен"
    note "именно из-за него видно «Welcome to nginx!»"
    note "исправление: sudo rm -f /etc/nginx/sites-enabled/default && sudo systemctl reload nginx"
  fi
  if ls /etc/nginx/sites-enabled/ 2>/dev/null | grep -qv '^default$'; then
    good "есть свой конфиг сайта: $(ls /etc/nginx/sites-enabled | grep -v '^default$' | tr '\n' ' ')"
  else
    bad_ "нет ни одного подключённого сайта, кроме дефолтного"
    note "запустите: sudo ./setup-nginx.sh ВАШ_ДОМЕН"
  fi
elif systemctl is-active --quiet caddy 2>/dev/null; then
  good "Caddy работает"
else
  bad_ "ни nginx, ни Caddy не запущены — снаружи сайт недоступен"
  note "поставьте: sudo ./setup-caddy.sh ВАШ_ДОМЕН"
fi

hdr "6. Что отдаётся снаружи"
if [ -n "$DOMAIN" ]; then
  # Проверяем через реальный домен: Caddy держит 80 для редиректа на https,
  # а сайт отдаёт по TLS. Запрос к 127.0.0.1 по http вернул бы пустоту
  # даже на полностью рабочей установке.
  body=$(curl -sk -m 12 "https://${DOMAIN}/" 2>/dev/null | head -c 600)
  if [ -z "$body" ]; then
    # https не ответил — пробуем http, там хотя бы видно заглушку прокси
    body=$(curl -s -m 8 -H "Host: ${DOMAIN}" "http://127.0.0.1/" 2>/dev/null | head -c 400)
  fi

  if printf '%s' "$body" | grep -qi "Welcome to nginx"; then
    bad_ "отдаётся заглушка nginx — конфиг сайта не подключён"
    note "sudo rm -f /etc/nginx/sites-enabled/default && sudo systemctl reload nginx"
  elif printf '%s' "$body" | grep -qi "Успевайка"; then
    good "по домену ${DOMAIN} отдаётся витрина"
  elif [ -z "$body" ]; then
    bad_ "пустой ответ по домену ${DOMAIN}"
    note "проверьте: sudo journalctl -u caddy -n 20  (или -u nginx)"
  else
    note "ответ не похож ни на витрину, ни на заглушку"
  fi

  https=$(curl -sk -o /dev/null -w '%{http_code}' -m 15 "https://${DOMAIN}/healthz" 2>/dev/null || echo 000)
  if [ "$https" = "200" ]; then
    good "HTTPS работает"
  else
    bad_ "HTTPS не отвечает (код ${https})"
    note "сертификат: sudo certbot --nginx -d ${DOMAIN}  (или перезапустите Caddy)"
  fi
else
  note "домен не указан — пропускаю. Запустите: sudo ./doctor.sh ВАШ_ДОМЕН"
fi

hdr "Итог"
printf '  успешно: %s, проблем: %s\n' "$ok" "$bad"
[ "$bad" -eq 0 ] && echo "  установка в порядке" || echo "  исправьте пункты с ✗ выше"
