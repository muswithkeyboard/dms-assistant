"""Веб-сервис ассистента ДМС (план §7–8, CONTRACTS §8): страницы, HTTP API, безопасность.

Маршрут = прочитать тело (с пределом размера) → вызвать Service → отдать JSON модели schemas.py. Логика — service.py.
Тяжёлые маршруты (вопрос, загрузка документа, импорт, обработка договора клиента) занимают слот HeavyGate; расчёт идёт в отдельном потоке,
цикл событий не блокируется. Журнал — только метод, путь, код и время (security.SecurityMiddleware).

Запуск: `uv run dms serve` или `uv run uvicorn dms_api.app:app --no-proxy-headers` (адрес клиента для лимитов
исправляет только ProxyHeadersMiddleware и только для доверенных прокси).
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import security
from .service import STAGES, Service, ServiceError, health, stage_event, ui_texts, versions
from .settings import Settings

HERE = Path(__file__).resolve().parent
log = logging.getLogger("dms.app")

SSE_HEARTBEAT_SECONDS = 10.0     # комментарий «: ping» в потоке — прокси не рвёт тихое соединение
MULTIPART_OVERHEAD = 64 * 1024   # заголовки частей multipart поверх предела размера файла


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def static_version(root: Path = HERE / "static") -> str:
    """Метка ?v= для статики — хэш содержимого файлов, а не версия движка: статика с меткой кешируется браузером
    на год (immutable, security.cache_control_for), и после правки JS/CSS при той же версии движка браузер
    держал бы старые файлы (найдено при проверке демо 24.10.2026)."""
    h = hashlib.sha1()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(f.relative_to(root).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


_CLIENT_FILE_PATH = re.compile(r"^(/api/clients/contracts/[^/]+/files/).+$")


class _ClientFileNameFilter(logging.Filter):
    """Журнал доступа без имён файлов клиента (CONTRACTS §9): путь …/files/<имя> пишется как …/files/…"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > 1 and isinstance(args[1], str):
            masked = _CLIENT_FILE_PATH.sub(r"\1…", args[1])
            if masked != args[1]:
                record.args = (args[0], masked, *args[2:])
        return True


def _mask_client_file_names() -> None:
    access = logging.getLogger(security.LOGGER_NAME)
    if not any(isinstance(f, _ClientFileNameFilter) for f in access.filters):
        access.addFilter(_ClientFileNameFilter())


def create_app(settings: Settings | None = None, *, service: Service | None = None,
               gate: security.HeavyGate | None = None, allowed_hosts: Iterable[str] | None = None) -> Starlette:
    """allowed_hosts — имена для проверки Host сверх 127.0.0.1 / localhost / [::1]; None — DMS_ALLOWED_HOSTS."""
    settings = settings or Settings()
    hosts = security.allowed_hosts_from_env() if allowed_hosts is None else security.parse_allowed_hosts(
        allowed_hosts)
    svc = service or Service(data_dir=Path(settings.data_dir) if settings.data_dir else None)
    gate = gate or security.HeavyGate()
    json_limit = settings.max_json_kb * 1024
    security.setup_logging()
    _mask_client_file_names()
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    limiter = security.RateLimiter(settings.rate_per_min)
    ver = versions(settings.llm_provider)
    static_v = static_version()

    def page(name: str, active: str, status: int = 200) -> HTMLResponse:
        html = env.get_template(name).render(t=ui_texts(), versions=ver, active=active, v=static_v)
        return HTMLResponse(html, status_code=status)

    async def index(_: Request) -> Response:
        return page("index.html", "chat")

    async def documents(_: Request) -> Response:
        return page("documents.html", "documents")

    async def about(_: Request) -> Response:
        return page("about.html", "about")

    async def healthz(_: Request) -> Response:
        return JSONResponse(health(settings.llm_provider))

    async def version(_: Request) -> Response:
        return JSONResponse(ver)

    # ------------------------------------------------------------------ API: помощники

    async def json_body(request: Request) -> dict:
        """Тело JSON не больше DMS_MAX_JSON_KB (413), корректный UTF-8 JSON-объект (422). Пустое тело — {}.
        Непустое тело — только с Content-Type application/json, иначе 415 (простой запрос text/plain со стороннего
        сайта браузер шлёт без preflight; security.RequestGuardMiddleware)."""
        raw = await security.read_body(request, json_limit)
        if raw.strip() and not security.is_json_content_type(request.headers.get("content-type")):
            raise ServiceError(415, "unsupported_media_type", security.ERR_CONTENT_TYPE)
        data = security.parse_json_body(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ServiceError(422, "invalid_json", "Тело запроса должно быть объектом JSON.")
        return data

    async def acquire(client: str):
        """Слот HeavyGate, освобождаемый потоком расчёта (security.HeavyGate.acquire)."""
        return await gate.acquire(client)

    def in_thread(fn, release, out: queue.SimpleQueue) -> None:
        """Расчёт в отдельном потоке: стадии и итог — в очередь; слот освобождается в конце потока."""
        def run() -> None:
            try:
                out.put(("answer", fn(lambda st: out.put(("stage", st)))))
            except ServiceError as e:
                out.put(("error", e))
            except Exception:
                log.exception("ошибка расчёта ответа")
                out.put(("error", ServiceError(500, "internal", ui_texts()["errors"]["internal"])))
            finally:
                release()
        threading.Thread(target=run, name="dms-answer", daemon=True).start()

    async def wait_result(out: queue.SimpleQueue):
        """Ждать итог расчёта, не блокируя цикл событий; стадии выдавать по мере поступления."""
        last = time.monotonic()
        while True:
            try:
                kind, value = out.get_nowait()
            except queue.Empty:
                await anyio.sleep(0.05)
                if time.monotonic() - last >= SSE_HEARTBEAT_SECONDS:
                    last = time.monotonic()
                    yield "ping", None
                continue
            yield kind, value
            if kind in ("answer", "error"):
                return

    # ------------------------------------------------------------------ API: контексты и документы

    async def api_groups(_: Request) -> Response:
        return JSONResponse(svc.groups())

    async def api_contract(request: Request) -> Response:
        return JSONResponse(svc.contract(request.path_params["cid"]))

    async def api_documents(request: Request) -> Response:
        if request.method == "POST":
            return await api_upload(request)
        q = request.query_params
        return JSONResponse(svc.documents(q.get("contract_id") or None, q.get("group_id") or None))

    async def api_document(request: Request) -> Response:
        return JSONResponse(svc.document(request.path_params["did"]))

    async def api_clause(request: Request) -> Response:
        q = request.query_params
        return JSONResponse(svc.clause(request.path_params["did"], q.get("number") or None, q.get("path") or None,
                                       q.get("ref") or None, chat_id=q.get("chat_id") or None))

    async def api_clauses(request: Request) -> Response:
        return JSONResponse(svc.document_clauses(request.path_params["did"],
                                                 chat_id=request.query_params.get("chat_id") or None))

    async def api_upload(request: Request) -> Response:
        """multipart: file, contract_id | group_id, doc_type?, uploaded_by. Тело читается потоком с пределом
        DMS_MAX_UPLOAD_MB (413); разбор файла — в потоке под слотом HeavyGate."""
        limit = settings.max_upload_bytes
        raw = await security.read_body(request, limit + MULTIPART_OVERHEAD)

        async def receive() -> dict:
            return {"type": "http.request", "body": raw, "more_body": False}

        try:
            form = await Request(request.scope, receive).form(max_files=1, max_fields=10)
        except Exception:  # noqa: BLE001 — битый multipart: понятная ошибка вместо 500
            raise ServiceError(422, "bad_form", "Форма загрузки заполнена неверно: выберите файл и договор.") \
                from None
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise ServiceError(422, "required", "Выберите файл.", "file")
        data = await upload.read()
        if len(data) > limit:
            raise ServiceError(413, "payload_too_large", ui_texts()["errors"]["too_large"], "file")

        def field(name: str) -> str | None:
            v = form.get(name)
            return (v.strip() or None) if isinstance(v, str) else None

        async with gate.aslot(security.request_client(request)):
            doc = await anyio.to_thread.run_sync(lambda: svc.upload(
                filename=upload.filename or "", data=data, contract_id=field("contract_id"),
                group_id=field("group_id"), doc_type=field("doc_type"), uploaded_by=field("uploaded_by")))
        return JSONResponse(doc, status_code=201)

    async def api_import_kit(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse(svc.import_status())
        await json_body(request)
        return JSONResponse(svc.start_import(gate), status_code=202)

    # ------------------------------------------------------------------ API: подключение клиента (ADR 0011)

    async def api_clients(request: Request) -> Response:
        if request.method == "POST":
            return JSONResponse(svc.create_client(await json_body(request)), status_code=201)
        return JSONResponse(svc.clients())

    async def api_client_contracts(request: Request) -> Response:
        return JSONResponse(svc.add_client_contract(request.path_params["gid"], await json_body(request)),
                            status_code=201)

    async def api_client_files(request: Request) -> Response:
        """multipart: file, doc_type?, scope? — тело читается потоком с пределом DMS_MAX_UPLOAD_MB (413)."""
        limit = settings.max_upload_bytes
        raw = await security.read_body(request, limit + MULTIPART_OVERHEAD)

        async def receive() -> dict:
            return {"type": "http.request", "body": raw, "more_body": False}

        try:
            form = await Request(request.scope, receive).form(max_files=1, max_fields=10)
        except Exception:  # noqa: BLE001 — битый multipart: понятная ошибка вместо 500
            raise ServiceError(422, "bad_form", "Форма загрузки заполнена неверно: выберите файл.") from None
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise ServiceError(422, "required", "Выберите файл.", "file")
        data = await upload.read()
        if len(data) > limit:
            raise ServiceError(413, "payload_too_large", ui_texts()["errors"]["too_large"], "file")

        def field(name: str) -> str | None:
            v = form.get(name)
            return (v.strip() or None) if isinstance(v, str) else None

        cid = request.path_params["cid"]
        async with gate.aslot(security.request_client(request)):
            f = await anyio.to_thread.run_sync(lambda: svc.upload_client_file(
                cid, filename=upload.filename or "", data=data, doc_type=field("doc_type"), scope=field("scope")))
        return JSONResponse(f, status_code=201)

    async def api_client_file(request: Request) -> Response:
        cid, name = request.path_params["cid"], request.path_params["name"]
        if request.method == "DELETE":
            svc.delete_client_file(cid, name)
            return Response(status_code=204)
        return JSONResponse(svc.patch_client_file(cid, name, await json_body(request)))

    async def api_client_process(request: Request) -> Response:
        await json_body(request)
        return JSONResponse(svc.start_onboarding(request.path_params["cid"], gate), status_code=202)

    async def api_client_job(request: Request) -> Response:
        return JSONResponse(svc.client_job(request.path_params["jid"]))

    # ------------------------------------------------------------------ API: разметка таблиц (ADR 0012, §10)

    async def api_tables(request: Request) -> Response:
        cid = request.path_params["cid"]
        async with gate.aslot(security.request_client(request)):
            out = await anyio.to_thread.run_sync(lambda: svc.contract_tables(cid))
        return JSONResponse(out)

    async def api_tables_try(request: Request) -> Response:
        cid, body = request.path_params["cid"], await json_body(request)
        async with gate.aslot(security.request_client(request)):
            out = await anyio.to_thread.run_sync(lambda: svc.try_mapping(cid, body))
        return JSONResponse(out)

    async def api_tables_mapping(request: Request) -> Response:
        cid = request.path_params["cid"]
        if request.method == "DELETE":
            svc.delete_mapping(cid, dict(request.query_params))
            return Response(status_code=204)
        body = await json_body(request)
        # без слота HeavyGate: process: true сам занимает слот обработки (start_onboarding), а таблицы договора
        # к этому моменту уже прочитаны экраном разметки (кэш сервиса)
        status, out = await anyio.to_thread.run_sync(lambda: svc.save_mapping(cid, body, gate))
        return JSONResponse(out, status_code=status)

    async def api_profiles(_: Request) -> Response:
        return JSONResponse(svc.insurer_profiles())

    async def api_profile(request: Request) -> Response:
        svc.delete_insurer_profile(request.path_params["pid"])
        return Response(status_code=204)

    # ------------------------------------------------------------------ API: вложения чата и настройки (ADR 0016)

    async def api_attachments(request: Request) -> Response:
        """GET — вложения чата; POST multipart: file, request_id?, contract_id?, doc_type? — тело читается потоком
        с пределом DMS_MAX_UPLOAD_MB (413); разбор — в потоке под слотом HeavyGate."""
        chat_id = request.path_params["chat_id"]
        if request.method == "GET":
            return JSONResponse(svc.attachments(chat_id))
        limit = settings.max_upload_bytes
        raw = await security.read_body(request, limit + MULTIPART_OVERHEAD)

        async def receive() -> dict:
            return {"type": "http.request", "body": raw, "more_body": False}

        try:
            form = await Request(request.scope, receive).form(max_files=1, max_fields=10)
        except Exception:  # noqa: BLE001 — битый multipart: понятная ошибка вместо 500
            raise ServiceError(422, "bad_form", "Форма загрузки заполнена неверно: выберите файл.") from None
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise ServiceError(422, "required", "Выберите файл.", "file")
        data = await upload.read()
        if len(data) > limit:
            raise ServiceError(413, "payload_too_large", ui_texts()["errors"]["too_large"], "file")

        def field(name: str) -> str | None:
            v = form.get(name)
            return (v.strip() or None) if isinstance(v, str) else None

        async with gate.aslot(security.request_client(request)):
            out = await anyio.to_thread.run_sync(lambda: svc.attach(
                chat_id, filename=upload.filename or "", data=data, request_id=field("request_id"),
                contract_id=field("contract_id"), doc_type=field("doc_type")))
        return JSONResponse(out, status_code=201)

    async def api_attachment(request: Request) -> Response:
        """DELETE — удалить вложение чата (файл и пункты; у договора добавленный документ остаётся)."""
        return JSONResponse(svc.delete_attachment(request.path_params["chat_id"], request.path_params["aid"]))

    async def api_attachment_active(request: Request) -> Response:
        return JSONResponse(svc.set_active_attachment(request.path_params["chat_id"], await json_body(request)))

    async def api_settings(request: Request) -> Response:
        if request.method == "PUT":
            return JSONResponse(svc.save_settings(await json_body(request)))
        return JSONResponse(svc.settings())

    async def settings_page(_: Request) -> Response:
        return page("settings.html", "settings")

    async def api_examples(_: Request) -> Response:
        return JSONResponse(svc.examples())

    # ------------------------------------------------------------------ API: чаты и запросы

    async def api_chats(request: Request) -> Response:
        if request.method == "POST":
            return JSONResponse(svc.create_chat(await json_body(request)), status_code=201)
        return JSONResponse(svc.chats())

    async def api_chat(request: Request) -> Response:
        if request.method == "DELETE":
            return JSONResponse(svc.delete_chat(request.path_params["chat_id"]))
        return JSONResponse(svc.chat(request.path_params["chat_id"]))

    async def api_chat_requests(request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        if request.method == "POST":
            return JSONResponse(svc.create_request(chat_id, await json_body(request)), status_code=201)
        return JSONResponse(svc.requests(chat_id, request.query_params.get("status", "open")))

    async def api_request(request: Request) -> Response:
        return JSONResponse(svc.request(request.path_params["rid"]))

    async def api_message(request: Request) -> Response:
        """Вопрос → Answer. Accept: text/event-stream — поток стадий (event: stage) и итог (event: answer);
        иначе обычный JSON {message, answer}. Ошибки до начала расчёта — обычным JSON с кодом HTTP."""
        body = await json_body(request)
        clarify = svc.clarify_of(body)        # ответ на вопрос из «Что уточнить» (необязательно)
        req, chat, text = svc.prepare_message(request.path_params["rid"], body)   # помечает обращение занятым
        try:
            release_slot = await acquire(security.request_client(request))
        except BaseException:
            svc.release_busy(req.id)
            raise

        def release() -> None:
            release_slot()
            svc.release_busy(req.id)

        out: queue.SimpleQueue = queue.SimpleQueue()
        try:
            in_thread(lambda on_stage: svc.answer_message(req, chat, text, on_stage, clarify), release, out)
        except BaseException:
            release()
            raise
        if "text/event-stream" not in request.headers.get("accept", ""):
            async for kind, value in wait_result(out):
                if kind == "answer":
                    return JSONResponse(value)
                if kind == "error":
                    raise value
            raise ServiceError(500, "internal", ui_texts()["errors"]["internal"])

        async def stream():
            yield _sse("stage", {"stage": "received", "label": STAGES["received"]})
            async for kind, value in wait_result(out):
                if kind == "ping":
                    yield b": ping\n\n"
                elif kind == "stage":
                    yield _sse("stage", stage_event(value))
                elif kind == "answer":
                    yield _sse("answer", value)
                else:
                    yield _sse("error", {**value.body(), "status": value.status})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    async def api_clinic(request: Request) -> Response:
        """Выбор клиники менеджером (ADR 0010): {contract_id, choice: row_id | "none"} → {message, answer} —
        пересчёт последнего вопроса запроса с выбором; синхронно JSON, расчёт — в потоке под слотом HeavyGate."""
        body = await json_body(request)
        rid = request.path_params["rid"]
        async with gate.aslot(security.request_client(request)):
            out = await anyio.to_thread.run_sync(lambda: svc.choose_clinic(rid, body))
        return JSONResponse(out)

    async def api_clinic_candidates(request: Request) -> Response:
        """Поиск по перечню программы выбора клиники (CONTRACTS §8, круг w7): ?contract_id=&q=&offset= →
        {rows, total, offset}. Лёгкий маршрут — без слота HeavyGate (только чтение перечня)."""
        p = request.query_params
        return JSONResponse(svc.clinic_candidates(request.path_params["rid"], p.get("contract_id"), p.get("q"),
                                                  p.get("offset")))

    async def api_add_clinic(request: Request) -> Response:
        return JSONResponse(svc.start_add_clinic(request.path_params["rid"], await json_body(request)))

    async def api_step(request: Request) -> Response:
        return JSONResponse(svc.automation_step(request.path_params["rid"], await json_body(request)))

    async def api_letter(request: Request) -> Response:
        return JSONResponse(svc.letter(request.path_params["rid"], await json_body(request)))

    async def api_letter_check(request: Request) -> Response:
        return JSONResponse(svc.check_letter(await json_body(request)))

    async def api_draft(request: Request) -> Response:
        return JSONResponse(svc.edit_draft(request.path_params["did"], await json_body(request)))

    async def api_close(request: Request) -> Response:
        await json_body(request)
        return JSONResponse(svc.close(request.path_params["rid"]))

    async def api_reopen(request: Request) -> Response:
        await json_body(request)
        return JSONResponse(svc.reopen(request.path_params["rid"]))

    async def api_rate(request: Request) -> Response:
        return JSONResponse(svc.rate(request.path_params["rid"], await json_body(request)))

    # ------------------------------------------------------------------ ошибки

    async def on_service_error(request: Request, exc: ServiceError) -> Response:
        headers = {"Retry-After": str(exc.retry_after)} if hasattr(exc, "retry_after") else None
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)

    async def on_http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            if exc.status_code == 404:
                msg = ui_texts()["errors"]["not_found"]
            elif exc.status_code == 405:
                msg = "Этот метод для адреса не поддерживается."
            else:
                msg = exc.detail
            return JSONResponse({"error": {"code": str(exc.status_code), "message": msg, "field": None}},
                                status_code=exc.status_code)
        return page("404.html", "", status=exc.status_code)

    async def on_error(request: Request, exc: Exception) -> Response:
        # Трассировка — только в журнал сервера (без тела запроса); наружу — понятное сообщение по-русски.
        log.exception("ошибка обработки %s %r", request.method, request.url.path[:200])
        return JSONResponse({"error": {"code": "internal", "message": ui_texts()["errors"]["internal"],
                                       "field": None}}, status_code=500)

    async def rate_limit(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            retry = limiter.check(security.request_client(request))
            if retry:
                return await on_service_error(request, security.rate_limit_error(retry))
        return await call_next(request)

    @asynccontextmanager
    async def lifespan(_app):
        _ = svc.app            # открыть AppStore при старте: миграция схемы до первого запроса
        if settings.warm:
            threading.Thread(target=svc.warm, name="dms-warm", daemon=True).start()
        yield

    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/documents", documents),
            Route("/about", about),
            Route("/settings", settings_page),
            Route("/api/settings", api_settings, methods=["GET", "PUT"]),
            Route("/healthz", healthz),
            Route("/version", version),
            Route("/api/groups", api_groups),
            Route("/api/contracts/{cid}", api_contract),
            Route("/api/examples", api_examples),
            Route("/api/documents", api_documents, methods=["GET", "POST"]),
            Route("/api/documents/import-kit", api_import_kit, methods=["GET", "POST"]),
            Route("/api/documents/{did}", api_document),
            Route("/api/documents/{did}/clause", api_clause),
            Route("/api/documents/{did}/clauses", api_clauses),
            Route("/api/clients", api_clients, methods=["GET", "POST"]),
            Route("/api/clients/jobs/{jid}", api_client_job),
            Route("/api/clients/contracts/{cid}/files", api_client_files, methods=["POST"]),
            Route("/api/clients/contracts/{cid}/files/{name}", api_client_file, methods=["PATCH", "DELETE"]),
            Route("/api/clients/contracts/{cid}/process", api_client_process, methods=["POST"]),
            Route("/api/clients/contracts/{cid}/tables", api_tables),
            Route("/api/clients/contracts/{cid}/tables/try", api_tables_try, methods=["POST"]),
            Route("/api/clients/contracts/{cid}/tables/mapping", api_tables_mapping, methods=["PUT", "DELETE"]),
            Route("/api/insurer-profiles", api_profiles),
            Route("/api/insurer-profiles/{pid}", api_profile, methods=["DELETE"]),
            Route("/api/clients/{gid}/contracts", api_client_contracts, methods=["POST"]),
            Route("/api/chats", api_chats, methods=["GET", "POST"]),
            Route("/api/chats/{chat_id}", api_chat, methods=["GET", "DELETE"]),
            Route("/api/chats/{chat_id}/requests", api_chat_requests, methods=["GET", "POST"]),
            Route("/api/chats/{chat_id}/attachments", api_attachments, methods=["GET", "POST"]),
            Route("/api/chats/{chat_id}/attachments/active", api_attachment_active, methods=["PUT"]),
            Route("/api/chats/{chat_id}/attachments/{aid}", api_attachment, methods=["DELETE"]),
            Route("/api/requests/{rid}", api_request),
            Route("/api/requests/{rid}/messages", api_message, methods=["POST"]),
            Route("/api/requests/{rid}/clinic", api_clinic, methods=["POST"]),
            Route("/api/requests/{rid}/clinic-candidates", api_clinic_candidates),
            Route("/api/requests/{rid}/automation/add-clinic", api_add_clinic, methods=["POST"]),
            Route("/api/requests/{rid}/automation/step", api_step, methods=["POST"]),
            Route("/api/requests/{rid}/close", api_close, methods=["POST"]),
            Route("/api/requests/{rid}/reopen", api_reopen, methods=["POST"]),
            Route("/api/requests/{rid}/rate", api_rate, methods=["POST"]),
            Route("/api/drafts/{did}", api_draft, methods=["PATCH"]),
            Route("/api/requests/{rid}/letter", api_letter, methods=["POST"]),
            Route("/api/letters/check", api_letter_check, methods=["POST"]),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ],
        middleware=[
            Middleware(security.ProxyHeadersMiddleware, trusted=security.parse_networks(settings.proxies)),
            Middleware(security.SecurityMiddleware, hsts=settings.https),
            Middleware(security.RequestGuardMiddleware, allowed_hosts=hosts),
            Middleware(security.GZipByPathMiddleware),
            Middleware(BaseHTTPMiddleware, dispatch=rate_limit),
        ],
        exception_handlers={ServiceError: on_service_error, HTTPException: on_http_error, Exception: on_error},
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = svc
    app.state.gate = gate
    return app


app = create_app()
