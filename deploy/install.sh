#!/usr/bin/env bash
# Установка и обновление ассистента ДМС на сервере (Docker + Caddy с HTTPS). Описание — deploy/README.md.
#   первый раз:  bash deploy/install.sh <домен> [архив данных — необязательно]
#   обновление:  git pull && bash deploy/install.sh
set -euo pipefail
domain="${1:-}"; archive="${2:-}"
# путь к архиву — от папки, где запущен скрипт (дальше работаем из deploy/)
if [[ -n "$archive" && -f "$archive" ]]; then archive="$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")"; fi
cd "$(dirname "$0")"
DC=(docker compose -f docker-compose.server.yml)
say() { printf '\n== %s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "нет Docker. Установка: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "нет docker compose (пакет docker-compose-plugin)."

if [[ -n "$domain" ]]; then
  [[ "$domain" =~ ^[A-Za-z0-9.-]+$ ]] || die "домен без http:// и без пути, например dms.example.ru"
  touch .env; grep -v '^DMS_DOMAIN=' .env > .env.tmp || true; { echo "DMS_DOMAIN=$domain"; cat .env.tmp; } > .env; rm -f .env.tmp
elif [[ ! -f .env ]]; then
  die "первый запуск: bash deploy/install.sh <домен>"
fi
domain="$(sed -n 's/^DMS_DOMAIN=//p' .env)"
data_home="$(sed -n 's/^DMS_DATA_HOME=//p' .env)"; data_home="${data_home:-../data}"

# Документы договоров: тестовый комплект лежит в репозитории (kit/, ADR 0020). Архив — необязательный:
# свой комплект или энкодер rubert-tiny2 (без энкодера маршрутизация вопросов работает на правилах).
kit_target="$data_home/raw/Кейсы_обезличенные/Документы"
if [[ -n "$archive" ]]; then
  [[ -f "$archive" ]] || die "нет файла архива: $archive"
  say "Распаковка данных в $data_home"
  mkdir -p "$data_home"
  tar -xzf "$archive" -C "$data_home"
  kit_changed=1
elif [[ ! -d "$kit_target" ]] || ! diff -rq ../kit/Документы "$kit_target" >/dev/null 2>&1; then
  [[ -d ../kit/Документы ]] || die "нет документов: ни kit/ в репозитории, ни архива вторым аргументом."
  say "Документы договоров из репозитория (kit/) → $kit_target"
  mkdir -p "$kit_target"
  cp -R ../kit/Документы/. "$kit_target/"
  kit_changed=1
fi
[[ -n "$(ls -A "$data_home/raw" 2>/dev/null)" ]] || die "нет документов в $data_home/raw."
mkdir -p "$data_home/models"
chmod -R a+rX "$data_home/raw" "$data_home/models"   # контейнер работает не от root

say "Сборка образа (первый раз — несколько минут)"
"${DC[@]}" build app

# Импорт комплекта: только если база ещё пустая или передан новый архив. Сервис на это время остановлен.
if [[ -n "${kit_changed:-}" ]] || ! "${DC[@]}" run --rm --no-deps -T app test -s /data/derived/kit.sqlite 2>/dev/null; then
  say "Импорт комплекта (до нескольких минут)"
  "${DC[@]}" stop app >/dev/null 2>&1 || true
  "${DC[@]}" run --rm --no-deps -T app python scripts/ingest_kit.py
fi

say "Запуск"
"${DC[@]}" up -d
for _ in $(seq 1 60); do
  if "${DC[@]}" exec -T app python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status != 200)" 2>/dev/null; then
    sleep 3
    [[ "$("${DC[@]}" ps caddy --format '{{.State}}')" == running ]] \
      || { "${DC[@]}" logs --tail 20 caddy; die "Caddy не запустился — журнал выше."; }
    say "Готово: https://$domain"
    echo "Сертификат выпускается при первом заходе (нужны открытые порты 80 и 443 и A-запись домена на этот сервер)."
    echo "Журнал: docker compose -f deploy/docker-compose.server.yml logs -f"
    exit 0
  fi
  sleep 2
done
"${DC[@]}" logs --tail 50 app
die "сервис не ответил за 2 минуты — журнал выше."
