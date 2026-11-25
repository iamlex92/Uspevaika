#!/usr/bin/env bash
# Автообновление «Успевайки» из GitHub Releases.
#
#   sudo ./autoupdate.sh            # проверить и обновить, если есть новая версия
#   sudo ./autoupdate.sh --check    # только проверить (код возврата 1 = есть новее)
#   sudo ./autoupdate.sh --force    # переустановить даже ту же версию
#   sudo ./autoupdate.sh --to 1.2.0 # поставить конкретную версию (откат)
#
# Как работает:
#   1. узнаёт установленную версию (файл VERSION в /opt/uspevaika);
#   2. спрашивает у GitHub последний релиз;
#   3. если новее — скачивает архив, проверяет контрольную сумму, распаковывает;
#   4. запускает install.sh (он сохраняет данные) и проверяет, что бот поднялся;
#   5. при неудаче откатывается на прежнюю версию.
#
# Настройка — в /etc/uspevaika-update.conf (создаётся install.sh):
#   GITHUB_REPO="владелец/репозиторий"
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/uspevaika}"
CONF="${CONF:-/etc/uspevaika-update.conf}"
WORK="${WORK:-/var/lib/uspevaika-update}"
SERVICE="uspevaika"
KEEP_RELEASES=3

if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; D=""; N=""
fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
info() { printf '  %s•%s %s\n' "$D" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%sОшибка:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

MODE="update"; TARGET_VERSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE="check" ;;
    --force) MODE="force" ;;
    --to) shift; TARGET_VERSION="${1:-}" ;;
    -h|--help)
      sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "неизвестный аргумент: $1" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "запускайте через sudo:  sudo ./autoupdate.sh"

# ---------- настройки ----------
REPO=""
[ -f "$CONF" ] && . "$CONF"
REPO="${GITHUB_REPO:-$REPO}"
[ -n "$REPO" ] || die "не указан репозиторий.
Задайте его в $CONF:
    GITHUB_REPO=\"ваш-логин/uspevaika\"
"

command -v curl >/dev/null 2>&1 || die "нужен curl: apt install curl"

# ---------- какая версия стоит сейчас ----------
current_version() {
  if [ -r "$APP_DIR/VERSION" ]; then
    tr -d '[:space:]' < "$APP_DIR/VERSION"
  elif curl -fsS -m 5 http://127.0.0.1:8080/healthz 2>/dev/null | grep -q version; then
    curl -fsS -m 5 http://127.0.0.1:8080/healthz 2>/dev/null \
      | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
  else
    echo "0.0.0"
  fi
}

# Сравнение версий: возвращает 0, если $1 новее $2 (или $2 пустая)
is_newer() {
  local a="${1#v}" b="${2#v}"
  [ "$a" = "$b" ] && return 1
  # sort -V правильно сравнивает 1.10 > 1.9, в отличие от строкового порядка
  [ "$(printf '%s\n%s\n' "$a" "$b" | sort -V | tail -1)" = "$a" ]
}

CURRENT="$(current_version)"
printf '%sАвтообновление «Успевайки»%s\n' "$B" "$N"
info "установлено: $CURRENT"
info "репозиторий: $REPO"

# ---------- что на GitHub ----------
printf '\n%sПроверяю релизы%s\n' "$B" "$N"
api="https://api.github.com/repos/$REPO/releases"
if [ -n "$TARGET_VERSION" ]; then
  api="https://api.github.com/repos/$REPO/releases/tags/v${TARGET_VERSION#v}"
fi

json="$(curl -fsS -m 20 -H 'Accept: application/vnd.github+json' "$api" 2>/dev/null || true)"
[ -n "$json" ] || die "не удалось получить релизы с GitHub.
Проверьте: репозиторий $REPO существует и публичный.
Приватный репозиторий требует токена — см. DOCS.md, раздел «Автообновление»."

# Последний релиз — первый элемент массива (GitHub отдаёт новые первыми).
# sed 's/.*"tag_name"/…/' жадно захватил бы ПОСЛЕДНИЙ тег в однострочном JSON,
# то есть самый старый — поэтому вырезаем все вхождения через grep -o и берём
# первое.
latest="$(printf '%s' "$json" \
          | grep -o '"tag_name":[[:space:]]*"[^"]*"' \
          | head -1 \
          | sed 's/.*"\([^"]*\)"$/\1/')"
[ -n "$latest" ] || die "в репозитории нет ни одного релиза.
Создайте релиз с тегом вида v1.0.1 и приложите архив uspevaika-release.tar.gz."

info "на GitHub: $latest"

if [ "$MODE" = "check" ]; then
  if is_newer "$latest" "$CURRENT"; then
    printf '\n%sДоступно обновление: %s → %s%s\n' "$Y" "$CURRENT" "$latest" "$N"
    exit 1          # код 1 = есть новее (удобно для cron/мониторинга)
  fi
  printf '\n%sУстановлена последняя версия.%s\n' "$G" "$N"
  exit 0
fi

if [ "$MODE" = "update" ] && ! is_newer "$latest" "$CURRENT"; then
  printf '\n%sОбновление не требуется: %s — последняя.%s\n' "$G" "$CURRENT" "$N"
  info "переустановить принудительно: sudo ./autoupdate.sh --force"
  exit 0
fi

# ---------- скачивание ----------
printf '\n%sСкачиваю %s%s\n' "$B" "$latest" "$N"
mkdir -p "$WORK"
dl_url="$(printf '%s' "$json" | grep -o '"browser_download_url":[[:space:]]*"[^"]*\.tar\.gz"' \
          | head -1 | sed 's/.*"\(http[^"]*\)"$/\1/')"
[ -n "$dl_url" ] || die "в релизе $latest нет файла .tar.gz.
Приложите к релизу архив uspevaika-release.tar.gz."

archive="$WORK/uspevaika-${latest#v}.tar.gz"
info "$(basename "$dl_url")"
curl -fsSL -m 180 -o "$archive" "$dl_url" || die "не удалось скачать архив"
ok "скачано: $(du -h "$archive" | cut -f1)"

# ---------- проверка целостности ----------
sum_url="$(printf '%s' "$json" | grep -o '"browser_download_url":[[:space:]]*"[^"]*\.sha256"' \
           | head -1 | sed 's/.*"\(http[^"]*\)"$/\1/')"
if [ -n "$sum_url" ]; then
  expected="$(curl -fsSL -m 30 "$sum_url" | awk '{print $1}' | head -1)"
  actual="$(sha256sum "$archive" | awk '{print $1}')"
  if [ "$expected" = "$actual" ]; then
    ok "контрольная сумма совпала"
  else
    die "контрольная сумма НЕ совпала — файл повреждён или подменён.
ожидалось: $expected
получено:  $actual"
  fi
else
  warn "в релизе нет файла .sha256 — проверяю только распаковку"
fi

# ---------- распаковка ----------
stage="$WORK/stage-${latest#v}"
rm -rf "$stage"; mkdir -p "$stage"
tar -xzf "$archive" -C "$stage" || die "архив не распаковывается"
[ -f "$stage/install.sh" ] || die "в архиве нет install.sh — это не релиз «Успевайки»"
new_version="$(tr -d '[:space:]' < "$stage/app/VERSION" 2>/dev/null || echo "$latest")"
ok "архив распакован, версия внутри: $new_version"

# ---------- резерв и обновление ----------
printf '\n%sУстанавливаю %s%s\n' "$B" "$new_version" "$N"
backup="$WORK/backup-$(date +%Y%m%d-%H%M%S).tar.gz"
files=()
for f in .env orders.db menu.json VERSION; do
  [ -f "$APP_DIR/$f" ] && files+=("$f")
done
[ ${#files[@]} -gt 0 ] && tar -czf "$backup" -C "$APP_DIR" "${files[@]}" 2>/dev/null \
  && ok "резерв данных: $backup"

stamp="$(date +%Y%m%d-%H%M%S)"
cp -a "$APP_DIR" "$WORK/prev-$stamp"     # чтобы можно было откатить код
info "прежняя версия сохранена: $WORK/prev-$stamp"

if ! "$stage/install.sh" --update >>"$WORK/install-$stamp.log" 2>&1; then
  warn "install.sh сообщил об ошибке — смотрите $WORK/install-$stamp.log"
  info "откатываюсь на прежнюю версию"
  systemctl stop "$SERVICE" 2>/dev/null || true
  rm -rf "$APP_DIR"
  cp -a "$WORK/prev-$stamp" "$APP_DIR"
  systemctl start "$SERVICE" 2>/dev/null || true
  die "обновление не удалось, вернул $CURRENT"
fi

# ---------- проверка, что бот жив ----------
printf '\n%sПроверяю, что бот поднялся%s\n' "$B" "$N"
alive=0
for i in $(seq 1 15); do
  sleep 2
  if curl -fsS -m 3 http://127.0.0.1:8080/healthz >/dev/null 2>&1; then alive=1; break; fi
done

if [ "$alive" = "1" ]; then
  # aiohttp отдаёт JSON с пробелом после двоеточия: "version": "1.0.0"
  running="$(curl -fsS http://127.0.0.1:8080/healthz \
             | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  if [ -n "$running" ] && [ "$running" = "$new_version" ]; then
    ok "бот работает, версия $running"
  else
    ok "бот отвечает (версия в ответе: ${running:-не указана})"
  fi
else
  warn "бот не отвечает после обновления — откатываюсь"
  tail -20 "$WORK/install-$stamp.log" 2>/dev/null | sed 's/^/    /'
  systemctl stop "$SERVICE" 2>/dev/null || true
  rm -rf "$APP_DIR"
  cp -a "$WORK/prev-$stamp" "$APP_DIR"
  systemctl start "$SERVICE" 2>/dev/null || true
  sleep 3
  die "откат выполнен, работает прежняя версия $CURRENT"
fi

# ---------- уборка ----------
rm -rf "$stage"
# держим только последние резервы, иначе диск забьётся
for pattern in "prev-*" "backup-*" "install-*.log"; do
  ls -1dt "$WORK"/$pattern 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) | xargs -r rm -rf
done
ls -1t "$WORK"/*.tar.gz 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) | xargs -r rm -f

printf '\n%s════════════════════════════════════════%s\n' "$B" "$N"
printf '%sОбновлено: %s → %s%s\n\n' "$G$B" "$CURRENT" "$new_version" "$N"
echo "  Логи:        journalctl -u $SERVICE -f"
echo "  Откатиться:  sudo ./autoupdate.sh --to $CURRENT"
printf '%s════════════════════════════════════════%s\n' "$B" "$N"
