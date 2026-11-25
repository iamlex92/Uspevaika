#!/usr/bin/env bash
# Настройка Caddy как HTTPS-прокси для «Успевайки».
# Caddy сам получает и продлевает сертификат Let's Encrypt — проще, чем nginx.
#
#   sudo ./setup-caddy.sh uspevaika.example.ru
set -euo pipefail

DOMAIN="${1:-}"
PORT="${PORT:-8080}"
CADDYFILE="/etc/caddy/Caddyfile"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запускайте через sudo: sudo ./setup-caddy.sh ДОМЕН"

if [ -z "$DOMAIN" ]; then
  read -rp "Домен (например uspevaika.example.ru): " DOMAIN
fi
[ -n "$DOMAIN" ] || die "нужен домен"
case "$DOMAIN" in
  *://*|*/) die "укажите только домен, без https:// и без слэша" ;;
esac

command -v caddy >/dev/null 2>&1 || {
  say "Caddy не установлен — ставлю"
  apt-get update -qq
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
}

say "1/3 Проверяю, что бот отвечает"
if ! curl -fsS -m 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  printf '\033[33mБот не отвечает на 127.0.0.1:%s — сайт отдаст 502.\033[0m\n' "$PORT" >&2
  echo "Проверьте: sudo systemctl status uspevaika" >&2
fi

say "2/3 Пишу $CADDYFILE"
[ -f "$CADDYFILE" ] && cp "$CADDYFILE" "${CADDYFILE}.bak.$(date +%s)"
cat > "$CADDYFILE" <<EOF
# «Успевайка». Caddy сам получает сертификат для домена.
${DOMAIN} {
    reverse_proxy 127.0.0.1:${PORT}
    # загрузка фото блюд в админке
    request_body {
        max_size 12MB
    }
    encode gzip
}
EOF
echo "готово"

say "3/3 Проверяю и запускаю"
caddy validate --config "$CADDYFILE" --adapter caddyfile
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy
sleep 3

say "Проверка"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "https://${DOMAIN}/healthz" || echo 000)
echo "https://${DOMAIN}/healthz → ${code}"
if [ "$code" = "200" ]; then
  echo "сайт работает по HTTPS"
else
  echo "Не 200. Проверьте: sudo journalctl -u caddy -n 40" >&2
  echo "Частые причины: домен не указывает на этот сервер, порт 80 закрыт файрволом." >&2
fi

say "Осталось указать адрес боту"
echo "  sudo nano /opt/uspevaika/.env"
echo "    WEBAPP_URL=https://${DOMAIN}"
echo "    BIND_HOST=127.0.0.1"
echo "  sudo systemctl restart uspevaika"
