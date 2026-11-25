#!/usr/bin/env bash
# Настройка nginx как HTTPS-прокси для «Успевайки».
#
#   sudo ./setup-nginx.sh uspevaika.example.ru
#
# Что делает:
#   1. создаёт /etc/nginx/sites-available/uspevaika с проксированием на бота;
#   2. подключает его симлинком в sites-enabled;
#   3. отключает дефолтную заглушку (иначе она перехватывает домен);
#   4. проверяет конфиг и перезагружает nginx;
#   5. предлагает получить сертификат Let's Encrypt.
#
# Без аргумента домен спросит.
set -euo pipefail

DOMAIN="${1:-}"
PORT="${PORT:-8080}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запускайте через sudo: sudo ./setup-nginx.sh ДОМЕН"

if [ -z "$DOMAIN" ]; then
  read -rp "Домен (например uspevaika.example.ru): " DOMAIN
fi
[ -n "$DOMAIN" ] || die "нужен домен"
case "$DOMAIN" in
  *://*|*/) die "укажите только домен, без https:// и без слэша: uspevaika.example.ru" ;;
esac

command -v nginx >/dev/null 2>&1 || {
  say "nginx не установлен — ставлю"
  apt-get update -qq && apt-get install -y -qq nginx
}

say "1/5 Проверяю, что бот отвечает"
if ! curl -fsS -m 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  printf '\033[33mБот не отвечает на 127.0.0.1:%s\033[0m\n' "$PORT" >&2
  echo "Проверьте: sudo systemctl status uspevaika" >&2
  echo "и что в .env стоит BIND_HOST=127.0.0.1 (или пусто)." >&2
  echo "Продолжаю — nginx настроится, но сайт будет отдавать 502, пока бот не поднимется." >&2
else
  echo "бот на месте"
fi

say "2/5 Создаю конфиг /etc/nginx/sites-available/uspevaika"
cat > /etc/nginx/sites-available/uspevaika <<EOF
# «Успевайка» — проксирование на бота (порт ${PORT})
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    client_max_body_size 12m;          # загрузка фото блюд в админке

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 60s;
    }
}
EOF
echo "готово"

say "3/5 Подключаю и отключаю дефолтную заглушку"
ln -sfn /etc/nginx/sites-available/uspevaika /etc/nginx/sites-enabled/uspevaika
# Именно из-за этой заглушки и видно «Welcome to nginx!»
rm -f /etc/nginx/sites-enabled/default
echo "симлинк создан, default отключён"

say "4/5 Проверяю конфиг"
nginx -t

say "5/5 Перезагружаю nginx"
systemctl reload nginx
systemctl enable nginx >/dev/null 2>&1 || true

say "Проверка"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 8 -H "Host: ${DOMAIN}" "http://127.0.0.1/healthz" || echo 000)
echo "http://${DOMAIN}/healthz → ${code}"
if [ "$code" = "200" ]; then
  echo "сайт отвечает"
else
  echo "не 200: смотрите журнал sudo journalctl -u nginx -n 30" >&2
fi

say "Дальше — HTTPS"
echo "Telegram открывает витрину только по https. Получите сертификат:"
echo
echo "  sudo apt install -y certbot python3-certbot-nginx"
echo "  sudo certbot --nginx -d ${DOMAIN}"
echo
echo "Затем впишите в /opt/uspevaika/.env:"
echo "  WEBAPP_URL=https://${DOMAIN}"
echo "и перезапустите бота: sudo systemctl restart uspevaika"
