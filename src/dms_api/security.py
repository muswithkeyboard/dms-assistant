"""Безопасность веб-слоя (план §11; перенесено из Justmedit `reference/justmedit-code/security.py`).

  * лимит запросов — скользящее окно 60 с на адрес клиента, в памяти процесса; превышение — 429 и Retry-After;
  * тяжёлые маршруты (вопрос к ассистенту, загрузка документа, импорт комплекта): не больше HEAVY_CONCURRENCY
    расчётов одновременно на процесс и не больше HEAVY_PER_CLIENT на адрес — иначе 503 service_busy с Retry-After;
  * тело запроса читается потоком с пределом размера: больше предела — 413;
  * на каждом ответе — заголовки безопасности (CSP без inline и без внешних источников, nosniff, no-referrer;
    HSTS — при DMS_HTTPS=1), на ответах с данными — `Cache-Control: no-store`; статика с меткой версии (?v=) —
    на год (immutable), шрифты — на неделю, остальное — no-cache;
  * сжатие gzip — только для статики и страниц: ответы с текстами документов и обращений не сжимаются (BREACH);
  * журнал: время, метод, путь, код, длительность. Тела запросов, строка запроса, тексты документов и обращений
    в журнал не попадают (`docs/rules.md` п. 4); трассировки наружу не отдаются;
  * адрес клиента для лимитов: адрес соединения; X-Forwarded-For / -Proto / -Host — только от доверенного прокси
    из DMS_TRUSTED_PROXIES (по умолчанию никому не доверяем). IPv6 считается по сети /64.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import anyio

from .service import ServiceError

LOGGER_NAME = "dms.access"
log = logging.getLogger(LOGGER_NAME)

# Скрипты, стили, шрифты и картинки — только свои ('self'): без CDN и внешних ресурсов (`docs/rules.md` п. 11).
CSP = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}
NO_STORE_PREFIXES = ("/api/", "/healthz", "/version")
# Кеш статики: адрес с меткой версии (/static/app.js?v=…) меняется вместе с файлом — его можно хранить год
# и не перепроверять; шрифты (их адреса задаёт app.css, без метки) — неделю; остальное — no-cache (сверка ETag).
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_FONTS = "public, max-age=604800"
HSTS = "max-age=31536000"
_VERSION_QUERY = re.compile(rb"(?:^|&)v=[^&]+")


def cache_control_for(path: str, query: bytes, status: int) -> str:
    """Cache-Control по умолчанию (если ответ не задал свой): no-store для данных (/api, /healthz, /version);
    долгий кеш — только для успешных ответов статики; остальное — no-cache."""
    if path.startswith(NO_STORE_PREFIXES):
        return "no-store"
    if path.startswith("/static/") and status in (200, 304):
        if path.startswith("/static/fonts/"):
            return CACHE_FONTS
        if _VERSION_QUERY.search(query or b""):
            return CACHE_IMMUTABLE
    return "no-cache"


def setup_logging() -> None:
    """Журнал доступа в stderr: «время метод путь код длительность». Настраивается один раз."""
    logger = logging.getLogger("dms")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


# --------------------------------------------------------------------------------------------
# Лимит запросов: скользящее окно в памяти
# --------------------------------------------------------------------------------------------
class RateLimiter:
    """Не больше `limit` запросов за `window` секунд с одного адреса. Хранит только адреса и отметки времени."""

    def __init__(self, limit: int, window: float = 60.0, max_clients: int = 20000):
        self.limit, self.window, self.max_clients = int(limit), float(window), max_clients
        self._hits: OrderedDict[str, deque] = OrderedDict()     # порядок — от давно не обращавшихся к недавним
        self._lock = threading.Lock()

    def check(self, client: str, now: float | None = None) -> int | None:
        """None — запрос разрешён и учтён; иначе — через сколько секунд повторить (для Retry-After)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.get(client)
            if q is None:
                if len(self._hits) >= self.max_clients:
                    self._purge(now)
                q = self._hits[client] = deque()
            else:
                self._hits.move_to_end(client)
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, int(self.window - (now - q[0])) + 1)
            q.append(now)
            return None

    def _purge(self, now: float) -> None:
        """Под наплывом новых адресов: сначала удаляются ключи без отметок в окне (устаревшие). Если и так места
        нет — самые давно не обращавшиеся (LRU) до 90 % предела. Счётчики активных адресов не сбрасываются
        (раньше при переполнении сбрасывался весь лимитер — и с ним окна тех, кто уже упёрся в лимит)."""
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window]:
            del self._hits[key]
        target = max(0, int(self.max_clients * 0.9))
        while len(self._hits) > target:
            self._hits.popitem(last=False)


# --------------------------------------------------------------------------------------------
# Адрес клиента и доверенный прокси
# --------------------------------------------------------------------------------------------
def client_key(host: str | None) -> str:
    """Ключ лимитера по адресу: IPv4 — как есть; IPv6 — сеть /64 (провайдер выдаёт абоненту целую /64, и без
    этого каждый адрес из неё считался бы отдельным клиентом); IPv4 в IPv6 (::ffff:a.b.c.d) — как IPv4."""
    if not host:
        return "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return str(host)[:64]
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def request_client(request) -> str:
    """Ключ лимитера для запроса Starlette (адрес уже исправлен ProxyHeadersMiddleware, если прокси доверенный)."""
    return client_key(request.client.host if request.client else None)


def parse_networks(items: Iterable[str]) -> tuple:
    """«10.0.0.1, 172.16.0.0/12, ::1» -> сети ipaddress. Неверная запись — ValueError (опечатка в настройке
    безопасности должна остановить запуск, а не молча выключить проверку)."""
    out = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"DMS_TRUSTED_PROXIES: «{item}» — не адрес и не сеть (пример: 127.0.0.1, 10.0.0.0/8)") \
                from None
    return tuple(out)


_HOST_RE = re.compile(r"[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](:[0-9]{1,5})?")


def _strip_port(value: str) -> str:
    """Запись адреса из X-Forwarded-For без порта и скобок: «[2001:db8::1]:443» -> «2001:db8::1»,
    «203.0.113.5:1234» -> «203.0.113.5»."""
    value = value.strip()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


class ProxyHeadersMiddleware:
    """X-Forwarded-For / -Proto / -Host принимаются, только если соединение пришло от доверенного прокси
    (DMS_TRUSTED_PROXIES; по умолчанию список пуст — заголовки игнорируются: подделать адрес для лимитов нельзя).

    Адрес клиента — первый справа адрес X-Forwarded-For, который сам не доверенный прокси (правые записи дописаны
    нашими прокси, левые мог прислать кто угодно). Proto и Host — последняя запись (её поставил ближайший прокси).
    Proto — только http или https; Host — только имя или адрес с портом. Uvicorn свои заголовки прокси не
    разбирает (запуск с --no-proxy-headers): иначе он доверял бы X-Forwarded-For от 127.0.0.1."""

    def __init__(self, app, trusted: Iterable = ()):
        self.app = app
        self.networks = tuple(trusted)

    def trusted(self, host: str | None) -> bool:
        if not host or not self.networks:
            return False
        try:
            ip = ipaddress.ip_address(_strip_port(host))
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip.version == n.version and ip in n for n in self.networks)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and self.networks:
            client = scope.get("client")
            if client and self.trusted(client[0]):
                scope = self._apply(scope)
        await self.app(scope, receive, send)

    def _apply(self, scope: dict) -> dict:
        values: dict[bytes, list[str]] = {}
        for k, v in scope.get("headers", []):
            values.setdefault(k.lower(), []).append(v.decode("latin-1"))

        def items(name: bytes) -> list[str]:
            return [x.strip() for x in ",".join(values.get(name, [])).split(",") if x.strip()]

        scope = dict(scope)
        chain = items(b"x-forwarded-for")
        if chain:
            real = None
            for entry in reversed(chain):
                if not self.trusted(entry):
                    real = entry
                    break
            real = _strip_port(real if real is not None else chain[0])
            try:
                ipaddress.ip_address(real)
            except ValueError:
                real = None                                  # мусор вместо адреса — остаётся адрес соединения
            if real is not None:
                scope["client"] = (real, 0)
        proto = items(b"x-forwarded-proto")
        if proto and proto[-1].lower() in ("http", "https"):
            scope["scheme"] = proto[-1].lower()
        host = items(b"x-forwarded-host")
        if host and _HOST_RE.fullmatch(host[-1]):
            headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"host"]
            scope["headers"] = [(b"host", host[-1].encode("latin-1"))] + headers
        return scope


# --------------------------------------------------------------------------------------------
# Разбор JSON тела
# --------------------------------------------------------------------------------------------
MAX_JSON_DEPTH = 32


def _depth_ok(value: Any, limit: int) -> bool:
    """Вложенность объектов и списков не глубже limit (обход без рекурсии)."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return False
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return True


def parse_json_body(body: bytes) -> Any:
    """Тело -> JSON. Не UTF-8, не JSON, вложенность глубже MAX_JSON_DEPTH (в том числе такая, на которой
    разборщик падает с RecursionError) — 422 invalid_json, а не 500."""
    err = ServiceError(422, "invalid_json", "Тело запроса должно быть корректным JSON в кодировке UTF-8 "
                                            f"с вложенностью не глубже {MAX_JSON_DEPTH}.")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise err from None
    if not _depth_ok(data, MAX_JSON_DEPTH):
        raise err
    return data


def rate_limit_error(retry_after: int, message: str | None = None) -> ServiceError:
    err = ServiceError(429, "rate_limited",
                       message or f"Слишком много запросов с вашего адреса. Повторите через {retry_after} с.")
    err.retry_after = retry_after  # type: ignore[attr-defined]
    return err


# --------------------------------------------------------------------------------------------
# Тяжёлые расчёты: не больше N одновременно на процесс
# --------------------------------------------------------------------------------------------
HEAVY_CONCURRENCY = 2
HEAVY_PER_CLIENT = 1          # тяжёлых расчётов одновременно с одного адреса (IPv6 — сети /64)
CLIENT_WAIT_SECONDS = 6.0     # сколько второй запрос того же адреса ждёт, пока закончится его первый
BUSY_RETRY_AFTER = 5



def _busy(own: bool = False) -> ServiceError:
    if own:
        err = ServiceError(503, "service_busy", "Ваш предыдущий запрос ещё обрабатывается: дождитесь ответа "
                                                "и повторите.")
    else:
        err = ServiceError(503, "service_busy",
                           "Сервис занят обработкой других файлов. Повторите через несколько секунд.")
    err.retry_after = BUSY_RETRY_AFTER  # type: ignore[attr-defined]
    return err


class HeavyGate:
    """Слоты для тяжёлых расчётов (файл, пакет, PDF): не больше `slots` на процесс и не больше `per_client` на адрес
    клиента. Общий слот берётся без ожидания: свободного нет — 503 service_busy с Retry-After. Слот занимается
    только на время расчёта (после чтения тела): медленная загрузка не держит слот.

    Один клиент не может занять все слоты (например, двумя битыми PDF подряд): второй его запрос в slot() сразу
    получает 503, а в aslot() сначала ждёт до `client_wait` секунд, пока закончится первый, — отказ получает только
    он сам, остальные клиенты работают. В памяти — только адреса с идущими сейчас расчётами."""

    def __init__(self, slots: int = HEAVY_CONCURRENCY, per_client: int = HEAVY_PER_CLIENT,
                 client_wait: float = CLIENT_WAIT_SECONDS):
        self.slots = int(slots)
        self.per_client = max(1, int(per_client))
        self.client_wait = float(client_wait)
        self._sem = threading.BoundedSemaphore(self.slots)
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}

    def _take_client(self, client: str | None) -> bool:
        if client is None:
            return True
        with self._lock:
            n = self._active.get(client, 0)
            if n >= self.per_client:
                return False
            self._active[client] = n + 1
            return True

    def _release_client(self, client: str | None) -> None:
        if client is None:
            return
        with self._lock:
            n = self._active.get(client, 0) - 1
            if n > 0:
                self._active[client] = n
            else:
                self._active.pop(client, None)

    def _take_slot(self, client: str | None) -> None:
        if not self._sem.acquire(blocking=False):
            self._release_client(client)
            raise _busy()

    def _release(self, client: str | None) -> None:
        self._sem.release()
        self._release_client(client)

    @contextmanager
    def slot(self, client: str | None = None) -> Iterator[None]:
        """Без ожидания. client — адрес клиента (security.request_client) или имя фоновой задачи; None — без учёта
        адреса. Все вызовы передают адрес явно (ревью 29.10.2026 п. 24: неявный адрес из contextvar не ставился)."""
        if not self._take_client(client):
            raise _busy(own=True)
        self._take_slot(client)
        try:
            yield
        finally:
            self._release(client)

    @asynccontextmanager
    async def aslot(self, client: str | None = None) -> AsyncIterator[None]:
        """То же, но второй запрос того же адреса ждёт (не дольше client_wait), пока закончится первый."""
        deadline = time.monotonic() + self.client_wait
        while not self._take_client(client):
            if time.monotonic() >= deadline:
                raise _busy(own=True)
            await anyio.sleep(0.05)
        self._take_slot(client)
        try:
            yield
        finally:
            self._release(client)

    async def acquire(self, client: str | None = None) -> Callable[[], None]:
        """Слот, который освобождает тот, кто закончил расчёт (поток), а не обработчик запроса: обрыв соединения
        клиента не освобождает слот раньше конца расчёта — предел одновременных расчётов не превышается.
        Ожидание адреса — как у aslot; возвращает функцию освобождения (вызвать ровно один раз)."""
        deadline = time.monotonic() + self.client_wait
        while not self._take_client(client):
            if time.monotonic() >= deadline:
                raise _busy(own=True)
            await anyio.sleep(0.05)
        self._take_slot(client)
        released = threading.Event()

        def release() -> None:
            if not released.is_set():
                released.set()
                self._release(client)

        return release


# --------------------------------------------------------------------------------------------
# Предел размера тела
# --------------------------------------------------------------------------------------------
async def read_body(request, limit: int) -> bytes:
    """Читает тело запроса потоком; больше `limit` байт — 413 (проверяется и Content-Length, и фактический размер)."""
    too_large = ServiceError(413, "payload_too_large",
                             f"Слишком большой запрос: допустимо не больше {_human(limit)}.")
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                raise too_large
        except ValueError:
            raise ServiceError(400, "bad_request", "Некорректный заголовок Content-Length.") from None
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


def _human(n: int) -> str:
    return f"{n // (1024 * 1024)} МБ" if n >= 1024 * 1024 else f"{max(1, n // 1024)} КБ"


# --------------------------------------------------------------------------------------------
# Host, Origin и Content-Type: защита от DNS-rebinding и CSRF (ревизия w16-sec)
# --------------------------------------------------------------------------------------------
# Сервис без входа по паролю: любой сайт, открытый у менеджера в браузере, мог бы слать POST на 127.0.0.1:8080
# (простой запрос text/plain без preflight) или, подменив DNS своего имени на 127.0.0.1, читать ответы API.
#   * Host — только 127.0.0.1, localhost, [::1] и имена из DMS_ALLOWED_HOSTS (через запятую, без порта);
#     иначе 403 на любой путь (за доверенным прокси Host берётся из X-Forwarded-Host — ProxyHeadersMiddleware);
#   * небезопасный метод на /api/*: Sec-Fetch-Site: cross-site — 403; Origin есть и не совпадает с Host — 403
#     (Origin «null» — тоже 403);
#   * тело JSON-маршрутов — только с Content-Type application/json (app.json_body, 415); пустое тело без
#     Content-Type допустимо (кнопки «Закрыть», «Обработать» шлют POST без тела; его защищает проверка Origin).
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ERR_HOST = "Адрес сайта не разрешён: откройте ассистента по адресу 127.0.0.1 или localhost, либо добавьте имя " \
           "сервера в DMS_ALLOWED_HOSTS."
ERR_ORIGIN = "Запрос со стороннего сайта отклонён: действия выполняются только со страниц самого ассистента."
ERR_CONTENT_TYPE = "Тело запроса должно быть JSON: укажите заголовок Content-Type: application/json."


def parse_allowed_hosts(value: str | Iterable[str] | None) -> frozenset[str]:
    """DMS_ALLOWED_HOSTS → множество имён в нижнем регистре (порт и пробелы отбрасываются) вместе с LOCAL_HOSTS."""
    items = value.split(",") if isinstance(value, str) else list(value or ())
    out = set(LOCAL_HOSTS)
    for raw in items:
        h = _host_name(raw.strip())
        if h:
            out.add(h)
    return frozenset(out)


def allowed_hosts_from_env() -> frozenset[str]:
    """DMS_ALLOWED_HOSTS из окружения или `.env` (как Settings: префикс DMS_)."""
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class _Hosts(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="DMS_", env_file=".env", extra="ignore")
        allowed_hosts: str = ""

    return parse_allowed_hosts(_Hosts().allowed_hosts)


def _host_name(value: str) -> str:
    """«Example.ORG:8080» → «example.org»; «[::1]:8080» → «[::1]»; мусор → ""."""
    value = value.strip().lower()
    if not value or not _HOST_RE.fullmatch(value):
        return ""
    if value.startswith("["):
        return value[:value.index("]") + 1]
    return value.rsplit(":", 1)[0] if ":" in value else value


_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _netloc(host: str, scheme: str) -> str:
    """Имя и порт для сравнения Origin с Host: порт по умолчанию схемы отбрасывается."""
    host = host.strip().lower()
    port = _DEFAULT_PORTS.get(scheme)
    if port and host.endswith(":" + port) and not host.endswith("]"):
        host = host[: -len(port) - 1]
    return host


def origin_matches(origin: str, host: str, scheme: str) -> bool:
    """Origin («scheme://host[:port]») указывает на тот же сайт, что Host запроса (с учётом порта по умолчанию)."""
    m = re.fullmatch(r"(https?)://([^/?#]+)", origin.strip(), flags=re.IGNORECASE)
    if not m:
        return False
    o_scheme = m.group(1).lower()
    return _netloc(m.group(2), o_scheme) == _netloc(host, scheme if scheme in _DEFAULT_PORTS else o_scheme)


def is_json_content_type(value: str | None) -> bool:
    """application/json (с параметрами вроде charset) — да; text/plain, form и прочее — нет."""
    return (value or "").split(";", 1)[0].strip().lower() == "application/json"


class RequestGuardMiddleware:
    """Проверка Host (все пути) и Origin / Sec-Fetch-Site (небезопасные методы /api/*), см. блок выше.
    Ошибка /api/* — JSON {"error": {code, message, field}} как у остальных маршрутов; страницы — простой текст."""

    def __init__(self, app, allowed_hosts: Iterable[str] = LOCAL_HOSTS):
        self.app = app
        self.allowed = frozenset(_host_name(h) or h for h in allowed_hosts) | LOCAL_HOSTS

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers: dict[bytes, str] = {}
        for k, v in scope.get("headers", []):
            headers.setdefault(k.lower(), v.decode("latin-1"))
        host = headers.get(b"host", "")
        path = scope.get("path", "")
        api = path.startswith("/api/")
        if _host_name(host) not in self.allowed:
            await self._deny(send, api, "host_not_allowed", ERR_HOST)
            return
        if api and scope.get("method", "") in UNSAFE_METHODS:
            site = headers.get(b"sec-fetch-site", "").strip().lower()
            origin = headers.get(b"origin")
            if site == "cross-site" or (origin is not None
                                        and not origin_matches(origin, host, scope.get("scheme", "http"))):
                await self._deny(send, api, "cross_origin", ERR_ORIGIN)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send, api: bool, code: str, message: str) -> None:
        if api:
            body = json.dumps({"error": {"code": code, "message": message, "field": None}},
                              ensure_ascii=False).encode()
            ctype = b"application/json"
        else:
            body, ctype = message.encode(), b"text/plain; charset=utf-8"
        await _send_plain(send, 403, body, [(b"content-type", ctype)])


# --------------------------------------------------------------------------------------------
# Заголовки безопасности и журнал (ASGI-обёртка)
# --------------------------------------------------------------------------------------------
class SecurityMiddleware:
    """Добавляет заголовки безопасности к каждому ответу и пишет строку журнала без тел и значений.
    hsts=True (сайт за HTTPS, DMS_HTTPS=1) — ещё Strict-Transport-Security: браузер год ходит только по HTTPS."""

    def __init__(self, app, hsts: bool = False):
        self.app = app
        self.headers = [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
        if hsts:
            self.headers.append((b"strict-transport-security", HSTS.encode()))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        t0 = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                present = {k.lower() for k, _ in message.get("headers", [])}
                headers = list(message.get("headers", []))
                headers.extend((k, v) for k, v in self.headers if k not in present)
                if b"cache-control" not in present:
                    cache = cache_control_for(path, scope.get("query_string", b""), message["status"])
                    headers.append((b"cache-control", cache.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Только путь (без строки запроса): в журнале не должно быть ни текстов документов и обращений, ни ПДн. Путь — через
            # repr: перевод строки или управляющий символ в пути (%0a) не подделает отдельную строку журнала.
            log.info("%s %r %d %.1fms", method, path[:200], status["code"], 1000 * (time.perf_counter() - t0))

# --------------------------------------------------------------------------------------------
# Сжатие gzip — только для ответов без персональных данных (BREACH)
# --------------------------------------------------------------------------------------------
# Сжимаются только статика и HTML-страницы (в разметке нет данных): ответы /api/* с текстами документов
# и обращений рядом с тем, что может подставить чужая страница, при сжатии выдают содержимое по длине (BREACH).
GZIP_PREFIXES = ("/static/",)
GZIP_PATHS = frozenset({"/", "/documents", "/about"})
GZIP_MIN_SIZE = 1024
GZIP_LEVEL = 6                # уровень 9 медленнее в 2–3 раза, а файл меньше на 1–2 %


def gzip_allowed(path: str) -> bool:
    return path in GZIP_PATHS or path.startswith(GZIP_PREFIXES)


class GZipByPathMiddleware:
    """GZipMiddleware Starlette только для путей из списка gzip_allowed (картинки и woff2 он сам не сжимает)."""

    def __init__(self, app, minimum_size: int = GZIP_MIN_SIZE, compresslevel: int = GZIP_LEVEL):
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and gzip_allowed(scope.get("path", "")):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


async def _send_plain(send, status: int, body: bytes, headers: list) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
