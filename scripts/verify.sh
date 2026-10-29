#!/usr/bin/env bash
# Полный чистый прогон ассистента ДМС без Docker (по образцу Justmedit scripts/verify.sh):
#   0) зависимости; 1) тесты; 2) конфиги; 3) гигиена репозитория (нет данных организаторов и .env в git, нет SMTP);
#   4) опись комплекта (если data/raw есть); 5) сервер на свободном порту: /healthz, /, заголовки; 6) в журнале нет
#   строки запроса; 7) остановка сервера.
# Печатает таблицу PASS / FAIL / SKIP. Код возврата 1, если есть хотя бы один FAIL.
# Запуск из app/:  uv run bash scripts/verify.sh   (или docker compose run --rm verify)
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONPATH="$ROOT/src"
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"
else PY="python3"; fi
TMP="$(mktemp -d)"
SERVER_PID=""
RESULTS="$TMP/results.tsv"
: > "$RESULTS"
cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; fi
  rm -rf "$TMP"
}
trap cleanup EXIT
record() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$RESULTS"; printf '%-5s %s — %s\n' "$1" "$2" "$3"; }

echo "== Ассистент ДМС: проверка без Docker =="
echo "Python: $PY ($($PY --version 2>&1)); каталог: $ROOT"
echo

# 0. Зависимости
if ! $PY -c "import yaml, starlette, jinja2, pydantic, docx, openpyxl" > "$TMP/deps.log" 2>&1; then
  record FAIL "зависимости" "нет пакетов ($(tail -1 "$TMP/deps.log")): uv sync --extra dev и uv run bash scripts/verify.sh"
  echo; echo "== Итог: PASS 0, FAIL 1, SKIP 0 =="; exit 1
fi

# 1. Тесты
echo "-- 1. Тесты"
$PY scripts/run_tests.py > "$TMP/tests.log" 2>&1
TEST_RC=$?
SUMMARY="$(grep -E '^Итог:' "$TMP/tests.log" | tail -1)"
if [ "$TEST_RC" = "0" ]; then record PASS "тесты" "${SUMMARY:-без ошибок}"
else record FAIL "тесты" "${SUMMARY:-run_tests.py завершился с ошибкой}"; grep -E '^FAIL' "$TMP/tests.log" | head -20; fi
if grep -q '^SKIP' "$TMP/tests.log"; then
  REASONS="$(grep '^SKIP' "$TMP/tests.log" | sed 's/.* — //' | sort | uniq -c | sed 's/^ *//' | paste -sd ';' -)"
  record SKIP "тесты, которым нужны данные или программы" "$(grep -c '^SKIP' "$TMP/tests.log") пропущено: $REASONS"
fi

# 2. Конфиги
echo "-- 2. Конфиги"
if $PY -m dms_core.cli config-check > "$TMP/cfg.log" 2>&1; then record PASS "конфиги" "$(grep -c '^ok' "$TMP/cfg.log") файлов читаются"
else record FAIL "конфиги" "$(tail -1 "$TMP/cfg.log")"; fi

# 3. Гигиена репозитория (`docs/rules.md` п. 3–4)
echo "-- 3. Репозиторий"
if git -C "$ROOT" rev-parse --git-dir > /dev/null 2>&1; then
  BAD="$(git -C "$ROOT" ls-files | grep -E '^(data|reference)(/|$)|(^|/)\.env$|\.(docx|xlsx|pdf|zip)$' | grep -v '^tests/fixtures/' | head -5)"
  if [ -z "$BAD" ]; then record PASS "git без данных" "нет data/, reference/, .env и документов вне tests/fixtures"
  else record FAIL "git без данных" "в индексе: $BAD"; fi
else
  record SKIP "git без данных" "не git-репозиторий (архив)"
fi
if grep -rEl '\b(smtplib|aiosmtplib|sendmail|sendgrid|mailgun)\b' src > /dev/null 2>&1; then
  record FAIL "нет отправки писем" "$(grep -rEl '\b(smtplib|aiosmtplib|sendmail|sendgrid|mailgun)\b' src | head -3)"
else record PASS "нет отправки писем" "в src/ нет SMTP и почтовых API"; fi

# 4. Комплект
echo "-- 4. Комплект"
if $PY -m dms_core.cli kit > "$TMP/kit.log" 2>&1; then record PASS "опись комплекта" "$(tail -1 "$TMP/kit.log")"
else record SKIP "опись комплекта" "$(tail -1 "$TMP/kit.log")"; fi

# 5–7. Сервер
echo "-- 5. Сервер"
PORT="$($PY -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
$PY -m uvicorn dms_api.app:app --host 127.0.0.1 --port "$PORT" --no-access-log --no-server-header --no-proxy-headers \
  > "$TMP/server.log" 2>&1 &
SERVER_PID=$!
UP=0
for _ in $(seq 1 60); do
  if $PY -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=2)" 2>/dev/null; then UP=1; break; fi
  kill -0 "$SERVER_PID" 2>/dev/null || break
  sleep 0.5
done
if [ "$UP" != "1" ]; then
  record FAIL "запуск сервера" "не ответил на /healthz за 30 с: $(tail -3 "$TMP/server.log" | tr '\n' ' ')"
else
  record PASS "запуск сервера" "порт $PORT"
  if BASE="http://127.0.0.1:$PORT" $PY - > "$TMP/http.log" 2>&1 <<'PYEOF'
import os, urllib.request
base = os.environ["BASE"]
r = urllib.request.urlopen(base + "/?canary=SECRET-QUERY-7391", timeout=5)
html = r.read().decode()
assert r.status == 200 and "Ассистент ДМС" in html, "страница / не отрисована"
assert "default-src 'self'" in r.headers["content-security-policy"], "нет CSP"
for p in ("/documents", "/about", "/static/app.css"):
    assert urllib.request.urlopen(base + p, timeout=5).status == 200, p
print("ok")
PYEOF
  then record PASS "страницы и заголовки" "/, /documents, /about, статика; CSP без внешних источников"
  else record FAIL "страницы и заголовки" "$(tail -2 "$TMP/http.log" | tr '\n' ' ')"; fi
  if grep -q "SECRET-QUERY-7391" "$TMP/server.log"; then record FAIL "журнал без строки запроса" "метка попала в журнал"
  else record PASS "журнал без строки запроса" "метка из строки запроса в журнал не попала"; fi
  kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null
  if kill -0 "$SERVER_PID" 2>/dev/null; then record FAIL "остановка сервера" "процесс не завершился"
  else record PASS "остановка сервера" "процесс завершён"; fi
  SERVER_PID=""
fi

N_PASS="$(grep -c '^PASS' "$RESULTS")"; N_FAIL="$(grep -c '^FAIL' "$RESULTS")"; N_SKIP="$(grep -c '^SKIP' "$RESULTS")"
echo
echo "== Итог: PASS $N_PASS, FAIL $N_FAIL, SKIP $N_SKIP =="
echo "Docker этим скриптом не проверяется: docker compose up --build и docker compose run --rm verify"
[ "$N_FAIL" = "0" ]
