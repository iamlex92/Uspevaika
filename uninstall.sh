#!/usr/bin/env bash
# Полное удаление «Успевайки» с сервера.
#
#   sudo ./uninstall.sh            # спросит, что удалять
#   sudo ./uninstall.sh --all      # удалить всё без вопросов (включая заказы)
#   sudo ./uninstall.sh --keep-data # удалить программу, сохранить заказы и .env
#
# Что удаляется: служба, файлы в /opt/uspevaika, пользователь uspevaika,
# конфиг nginx или Caddy, системные сертификаты. Порядок действий обратный
# установке, поэтому ничего не остаётся висеть.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/uspevaika}"
APP_USER="${APP_USER:-uspevaika}"
DOMAIN="${DOMAIN:-}"
# Куда сохранять архив с данными. Своё значение удобно и для тестов, и когда
# /root на маленьком разделе.
BACKUP_DIR="${BACKUP_DIR:-/root}"
UNIT_FILE="/etc/systemd/system/uspevaika.service"

if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; D=""; N=""
fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
info() { printf '  %s•%s %s\n' "$D" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%sОшибка:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запускайте через sudo:  sudo ./uninstall.sh"

MODE="${1:-}"
KEEP_DATA=0
REMOVE_WEB=0
REMOVE_DATA=0

printf '%s\n' "${B}Удаление «Успевайки»${N}"

# что вообще найдено
found=0
[ -f "$UNIT_FILE" ] && found=1
[ -d "$APP_DIR" ] && found=1
id "$APP_USER" >/dev/null 2>&1 && found=1
if [ "$found" = "0" ]; then
  printf '\n  %sПохоже, ничего не установлено. Завершаю.%s\n' "$Y" "$N"
  exit 0
fi

# домен — чтобы убрать его из прокси
if [ -z "$DOMAIN" ] && [ -f "$APP_DIR/.env" ]; then
  DOMAIN="$(grep -E '^WEBAPP_URL=' "$APP_DIR/.env" | cut -d= -f2- | sed 's|https\?://||' | cut -d/ -f1)"
fi
[ -n "$DOMAIN" ] && info "домен: $DOMAIN"

# ---------- объём удаления ----------
if [ "$MODE" = "--all" ]; then
  REMOVE_WEB=1; REMOVE_DATA=1; CONFIRM=1
elif [ "$MODE" = "--keep-data" ]; then
  REMOVE_WEB=1; REMOVE_DATA=0; CONFIRM=1
else
  printf '\n%sЧто удалить?%s\n' "$B" "$N"
  printf '  1) только программу, заказы и настройки оставить  %s(рекомендую)%s\n' "$D" "$N"
  printf '  2) программу, настройки прокси и сертификаты; заказы оставить\n'
  printf '  3) всё: программу, заказы, настройки, прокси, сертификаты\n'
  printf '  0) отмена\n'
  printf '\n  Выбор [1]: '
  read -rp '' choice || choice="1"
  case "${choice:-1}" in
    1) REMOVE_WEB=0; REMOVE_DATA=0 ;;
    2) REMOVE_WEB=1; REMOVE_DATA=0 ;;
    3) REMOVE_WEB=1; REMOVE_DATA=1 ;;
    0) echo "  Отменено."; exit 0 ;;
    *) die "неизвестный выбор: $choice" ;;
  esac
  CONFIRM=0
fi

# показываем, что именно будет удалено
printf '\n%sБудет удалено:%s\n' "$B" "$N"
printf '  • служба systemd uspevaika (остановлена и отключена)\n'
printf '  • программа в %s\n' "$APP_DIR"
printf '  • системный пользователь %s\n' "$APP_USER"
[ "$REMOVE_WEB" = "1" ] && printf '  • конфиг nginx/Caddy и сертификат для домена\n'
if [ "$REMOVE_DATA" = "1" ]; then
  printf '  %s• заказы и настройки (.env, orders.db) — БЕЗВОЗВРАТНО%s\n' "$R" "$N"
else
  printf '  %sСохраню:%s orders.db и .env (в %s/uspevaika-data-*.tar.gz)\n' "$G" "$N" "$BACKUP_DIR"
fi

if [ "$CONFIRM" = "0" ]; then
  printf '\n  Продолжить? [д/Н]: '
  read -rp '' go || go="н"
  case "$go" in [дДyY]*) ;; *) echo "  Отменено."; exit 0 ;; esac
fi

# ---------- 1. служба ----------
printf '\n%s[1/5] Останавливаю службу%s\n' "$B" "$N"
if [ -f "$UNIT_FILE" ]; then
  systemctl stop uspevaika 2>/dev/null || true
  systemctl disable uspevaika >/dev/null 2>&1 || true
  rm -f "$UNIT_FILE"
  systemctl daemon-reload
  systemctl reset-failed uspevaika 2>/dev/null || true
  ok "служба удалена"
else
  info "службы не было"
fi

# ---------- 2. данные ----------
printf '\n%s[2/5] Данные%s\n' "$B" "$N"
if [ "$REMOVE_DATA" = "1" ]; then
  info "удаляю заказы и настройки вместе с папкой"
else
  if [ -f "$APP_DIR/orders.db" ] || [ -f "$APP_DIR/.env" ]; then
    backup="$BACKUP_DIR/uspevaika-data-$(date +%Y%m%d-%H%M%S).tar.gz"
    # складываем только то, что есть: отсутствующий файл tar обрывает на полпути
    files=()
    for f in .env orders.db menu.json; do
      [ -f "$APP_DIR/$f" ] && files+=("$f")
    done
    if [ ${#files[@]} -gt 0 ]; then
      tar -czf "$backup" -C "$APP_DIR" "${files[@]}" 2>/dev/null || true
    fi
    if [ -f "$backup" ]; then
      chmod 600 "$backup"
      ok "сохранено в $backup"
      info "вернуть: tar -xzf $backup -C $APP_DIR"
    else
      warn "не удалось сохранить данные"
    fi
  fi
fi

# ---------- 3. прокси ----------
printf '\n%s[3/5] Веб-сервер%s\n' "$B" "$N"
if [ "$REMOVE_WEB" = "1" ]; then
  removed=0
  if [ -e /etc/nginx/sites-available/uspevaika ]; then
    rm -f /etc/nginx/sites-enabled/uspevaika /etc/nginx/sites-available/uspevaika
    removed=1
  fi
  # дефолтная заглушка: если мы её отключали, вернём — иначе nginx отдаёт 404
  if [ -d /etc/nginx/sites-enabled ] && [ ! -e /etc/nginx/sites-enabled/default ] \
     && [ -f /etc/nginx/sites-available/default ]; then
    ln -sfn /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
    info "вернул стандартную страницу nginx"
  fi
  if [ "$removed" = "1" ]; then
    nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
    ok "конфиг nginx удалён"
  fi

  if command -v caddy >/dev/null 2>&1 && [ -f /etc/caddy/Caddyfile ] && [ -n "$DOMAIN" ]; then
    if grep -q "$DOMAIN" /etc/caddy/Caddyfile 2>/dev/null; then
      backup_caddy="/etc/caddy/Caddyfile.bak.$(date +%Y%m%d-%H%M%S)"
      cp /etc/caddy/Caddyfile "$backup_caddy"
      # вырезаем блок с нашим доменом: от строки «домен {» до закрывающей }
      awk -v d="$DOMAIN" '
        index($0, d) && /\{/ {skip=1; depth=0}
        skip {
          n = gsub(/\{/, "{"); m = gsub(/\}/, "}"); depth += n - m
          if (depth <= 0 && m > 0) { skip=0 }
          next
        }
        {print}
      ' "$backup_caddy" > /etc/caddy/Caddyfile
      if caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
        systemctl reload caddy 2>/dev/null || systemctl restart caddy 2>/dev/null || true
        ok "конфиг Caddy очищен (резерв: $backup_caddy)"
      else
        cp "$backup_caddy" /etc/caddy/Caddyfile
        warn "не смог аккуратно вырезать домен — Caddyfile возвращён как был"
        info "уберите блок «$DOMAIN { … }» вручную и перезапустите caddy"
      fi
    fi
  fi

  if [ -n "$DOMAIN" ] && command -v certbot >/dev/null 2>&1; then
    certbot delete --cert-name "$DOMAIN" --non-interactive >/dev/null 2>&1 \
      && ok "сертификат Let's Encrypt удалён" || info "сертификата для $DOMAIN не было"
  fi
  [ "$removed" = "0" ] && info "настроек прокси не найдено"
else
  info "оставляю веб-сервер как есть"
fi

# ---------- 4. файлы и пользователь ----------
printf '\n%s[4/5] Файлы%s\n' "$B" "$N"
if [ "$REMOVE_DATA" = "1" ]; then
  rm -rf "$APP_DIR"
  ok "папка $APP_DIR удалена"
else
  # оставляем только данные, остальное стираем
  tmp="$(mktemp -d)"
  for f in .env orders.db menu.json; do
    [ -f "$APP_DIR/$f" ] && cp -a "$APP_DIR/$f" "$tmp/" 2>/dev/null || true
  done
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"
  cp -a "$tmp"/. "$APP_DIR"/ 2>/dev/null || true
  rm -rf "$tmp"
  chmod 600 "$APP_DIR/.env" 2>/dev/null || true
  ok "программа удалена, данные оставлены в $APP_DIR"
fi

printf '\n%s[5/5] Пользователь%s\n' "$B" "$N"
if id "$APP_USER" >/dev/null 2>&1; then
  if [ "$REMOVE_DATA" = "1" ] || [ ! -d "$APP_DIR" ]; then
    userdel "$APP_USER" 2>/dev/null || true
  fi
  if id "$APP_USER" >/dev/null 2>&1; then
    info "пользователь $APP_USER оставлен (владелец сохранённых данных)"
  else
    ok "пользователь $APP_USER удалён"
  fi
else
  info "пользователя не было"
fi

# ---------- итог ----------
printf '\n%s════════════════════════════════════════%s\n' "$B" "$N"
if [ "$REMOVE_DATA" = "1" ]; then
  printf '%sУдалено полностью.%s\n' "$G$B" "$N"
else
  printf '%sПрограмма удалена, данные сохранены.%s\n' "$G$B" "$N"
  [ -d "$APP_DIR" ] && info "данные в $APP_DIR"
  if compgen -G "$BACKUP_DIR/uspevaika-data-*.tar.gz" >/dev/null; then
    info "архив: $(ls -t "$BACKUP_DIR"/uspevaika-data-*.tar.gz | head -1)"
  fi
fi
cat <<EOF

  ${B}Если бот ещё в группе Telegram${N} — удалите его оттуда вручную:
  настройки группы → участники → бот → удалить.

  ${B}Проверить, что ничего не осталось:${N}
   systemctl status uspevaika     (не найдено = хорошо)
   ls $APP_DIR
   ss -ltn | grep 8080            (пусто = хорошо)
EOF
printf '%s════════════════════════════════════════%s\n' "$B" "$N"
