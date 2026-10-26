"""Сервис без Starlette (по образцу Justmedit service.py): чаты, запросы, ответы, автомат, документы.
Маршруты — app.py, они только читают тело, зовут сюда и отдают JSON.

Ошибки для клиента — ServiceError(status, code, message, field): сообщение по-русски, с тем, что исправить.
Правила сервиса:
  * ответ — только engine.answer по контексту чата (контекст фиксирован при создании, ТЗ п. 9, ADR 0006);
  * без модели: demo_saved — только если вопрос и контекст совпали с примером и есть сохранённый ответ;
    иначе — extractive от движка (план §6.4). Сбой модели → тот же extractive и понятная пометка по-русски;
  * автомат добавления клиники и черновики — dms_core.scenarios.add_clinic (CONTRACTS §8); ветку «бесплатно /
    платно» задаёт только действие менеджера (ADR 0004); письма сервер не отправляет и mailto не формирует;
  * в журнал не попадают тексты вопросов, ответов, документов — только идентификаторы и типы ошибок.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import importlib.util
import inspect
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from dms_core import __version__
from dms_core.config import load_yaml, prompt_version

log = logging.getLogger("dms.service")


class ServiceError(Exception):
    """Ошибка, которую нужно вернуть клиенту: код HTTP, код ошибки, сообщение по-русски, поле ввода (если есть)."""

    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "field": self.field}}


def versions(llm_provider: str = "") -> dict:
    """Версии для подвала и /version: движок, промпт, провайдер модели (или режим без модели)."""
    return {
        "engine": __version__,
        "prompt": prompt_version("system"),
        "llm": llm_provider or "без модели",
    }


def health(llm_provider: str = "") -> dict:
    return {"status": "ok", **versions(llm_provider)}


def ui_texts() -> dict:
    return load_yaml("texts_ru/ui.yaml")


# ------------------------------------------------------------------ сообщения и пределы

MAX_QUESTION_CHARS = 4000
MAX_TITLE_CHARS = 200
MAX_DRAFT_CHARS = 20000
MAX_REPLY_CHARS = 20000
MAX_COMMENT_CHARS = 2000
CLINIC_PICK_SLOT = "clinic_pick:"   # request.slots["clinic_pick:<cid>"] — выбор клиники менеджером (ADR 0010)
MAX_ROW_ID_CHARS = 300
MAX_SEARCH_CHARS = 200      # строка поиска по перечню клиник (GET …/clinic-candidates)

# Стадии подготовки ответа для потока SSE (FR 17). Движок пока не принимает обратный вызов, поэтому сервис
# стадии сообщает engine.answer через on_stage (route, clarify, contract:cNN, verify:cNN, compose).
STAGES = {
    "received": "Вопрос получен",
    "route": "Определяю сценарий и проверяю обязательные факты",
    "clarify": "Проверяю, хватает ли фактов для ответа",
    "contracts": "Ищу в документах договоров контекста",
    "contract": "Ищу в документах договора {cid}",
    "verify": "Проверяю ссылки на пункты договора {cid}",
    "compose": "Собираю ответ",
    "demo_saved": "Показываю сохранённый ответ примера",
}


def stage_event(name: str) -> dict:
    """Стадия движка («contract:c05», «verify:c05», «compose») → данные события SSE для индикатора (FR 17)."""
    stage, _, cid = name.partition(":")
    data: dict = {"stage": stage, "label": STAGES.get(stage, stage).format(cid=cid)}
    if cid:
        data["contract_id"] = cid
    return data

ERR_MODEL = ("Модель недоступна или вернула ошибку. Показаны найденные пункты документов без вывода — "
             "проверьте их вручную или повторите позже.")


def _err_not_found(what: str) -> ServiceError:
    return ServiceError(404, "not_found", f"{what} не найден. Обновите страницу.")


def _text(body: dict, key: str, *, required: bool, limit: int, label: str) -> str | None:
    """Строковое поле тела: тип, пустота, длина — с сообщением по-русски и именем поля."""
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ServiceError(422, "required", f"Заполните поле «{label}».", key)
        return None
    if not isinstance(value, str):
        raise ServiceError(422, "invalid", f"Поле «{label}» должно быть текстом.", key)
    value = value.strip()
    if len(value) > limit:
        raise ServiceError(422, "too_long", f"Поле «{label}»: не больше {limit} знаков.", key)
    return value


def _dump(model: Any) -> dict:
    return model.model_dump(mode="json")


def _norm_q(s: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (s or "").casefold().replace("ё", "е")).split())


# ------------------------------------------------------------------ загрузка файла: сигнатуры

# Нужные части пакета OOXML: без них это не документ Word / книга Excel, даже если расширение верное.
_OOXML_PARTS = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml"}
# Частей пакета — как у читателя xlsx (io/xlsx_reader.py MAX_XLSX_PARTS = 10 000): каталог ЛПУ ГК 1 — 2181 часть
# (лист на регион), прежний предел 2000 его отклонял. Защита от zip-бомбы — суммарный объявленный распакованный
# размер MAX_UNPACKED (zipfile не распакует больше объявленного размера части).
MAX_ZIP_ENTRIES = 10_000
MAX_UNPACKED = 200 * 2**20        # распакованный размер пакета (защита от zip-бомбы)


def sniff_format(filename: str, data: bytes) -> str:
    """Формат по расширению, подтверждённый содержимым: docx/xlsx — zip с [Content_Types].xml и главной частью,
    pdf — начинается с %PDF, txt — декодируется как текст без нулевых байтов. Иначе ServiceError 415."""
    ext = Path(filename or "").suffix.lower().lstrip(".")
    bad = ServiceError(415, "bad_file", "Файл не подходит: допустимы DOCX, XLSX, PDF и TXT, и содержимое должно "
                                        "соответствовать расширению.", "file")
    if ext in _OOXML_PARTS:
        if not data.startswith(b"PK\x03\x04"):
            raise bad
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos = z.infolist()
                names = {i.filename for i in infos}
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError):
            raise bad from None
        if "[Content_Types].xml" not in names or _OOXML_PARTS[ext] not in names:
            raise bad
        # формат верный, но пакет слишком велик — это «размер» (413), а не «не тот формат» (415)
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ServiceError(413, "payload_too_large", f"В файле больше {MAX_ZIP_ENTRIES} частей: разбейте его на "
                                                 "несколько файлов.", "file")
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise ServiceError(413, "payload_too_large", f"Файл в распакованном виде больше {MAX_UNPACKED // 2**20} МБ: "
                                                 "разбейте его на несколько файлов.", "file")
        return ext
    if ext == "pdf":
        if not data[:1024].lstrip().startswith(b"%PDF-"):
            raise bad
        return ext
    if ext == "txt":
        if b"\x00" in data[:65536]:
            raise bad
        return ext
    raise bad


def extract_text_stats(path: Path, fmt: str) -> dict:
    """Извлечение текста (ТЗ п. 7.1: «извлечение текста → проверка полноты»): только счётчики, сам текст наружу
    и в журнал не уходит (`docs/rules.md` п. 4). InputError читателя — сообщение по-русски."""
    if fmt == "docx":
        from dms_core.io.docx_reader import read_docx

        doc = read_docx(path)
        return {"paragraphs": len(doc.paragraphs), "numbered": sum(1 for p in doc.paragraphs if p.number),
                "tables": len(doc.tables), "chars": sum(len(p.text) for p in doc.paragraphs)}
    if fmt == "xlsx":
        from dms_core.io.xlsx_reader import read_xlsx

        sheets = read_xlsx(path)
        rows = sum(len(r) for r in sheets.values())
        return {"sheets": len(sheets), "rows": rows, "chars": rows}
    if fmt == "pdf":
        from dms_core.io.pdf_reader import read_pdf

        pages = read_pdf(path)
        return {"pages": len(pages), "chars": sum(len(p) for p in pages)}
    from dms_core.io.txt_reader import read_txt

    text = read_txt(path)
    return {"lines": text.count("\n") + 1, "chars": len(text.strip())}


# ------------------------------------------------------------------ подключение клиента: имена файлов и задания

MAX_CLIENT_CONTRACTS = 50          # договоров в одном запросе создания клиента
MAX_FILENAME_CHARS = 150
# Предел имени в байтах UTF-8: файловые системы ограничивают имя 255 байтами (APFS, ext4), кириллица — 2 байта на
# букву; запас — на временное имя и NFD на macOS не нужен (пишем NFC), но 15 байт оставляем под «.part»-варианты
MAX_FILENAME_BYTES = 240
# Опасные символы имени файла: управляющие, разделители пути, символы, запрещённые в Windows
_BAD_NAME = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
ERR_JOB_FAILED = ("Договор не удалось обработать: в документах не найдено пунктов или файлы не читаются. "
                  "Проверьте файлы и тип документов и запустите обработку снова.")
ERR_JOB_LOST = "Обработка прервана: сервис перезапускался. Запустите обработку снова."


def clean_filename(filename: str) -> str:
    """Имя файла клиента без пути и опасных символов (NFC, ≤ MAX_FILENAME_CHARS с сохранением расширения). Пустое
    или скрытое имя — ServiceError 422: файл с таким именем нельзя показать менеджеру и сохранить в папку."""
    name = unicodedata.normalize("NFC", filename or "")
    name = re.split(r"[\\/]", name)[-1]
    name = " ".join(_BAD_NAME.sub("_", name).split()).lstrip(". ")
    stem, suffix = Path(name).stem, Path(name).suffix
    if len(name) > MAX_FILENAME_CHARS:
        stem = stem[:MAX_FILENAME_CHARS - len(suffix)].rstrip()
    # длина — и в байтах UTF-8: обрезка основы по байтам без порчи многобайтовой буквы, расширение сохраняется
    room = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
    if len(stem.encode("utf-8")) > room:
        stem = stem.encode("utf-8")[:max(room, 0)].decode("utf-8", errors="ignore").rstrip()
    name = stem + suffix
    if not stem or not suffix or name.startswith("~$"):
        raise ServiceError(422, "bad_name", "Имя файла не подходит: нужно имя с расширением (DOCX, XLSX, PDF, TXT).",
                           "file")
    return name


TABLES_CACHE_MAX = 4        # договоров в кэше таблиц экрана разметки (большой docx читается секунды)


def _map_error(key: str, **kw: Any) -> str:
    """Сообщение ошибки разметки таблиц (config/texts_ru/mapping.yaml errors)."""
    from dms_core.table_mapping import error_text

    return error_text(key, **kw)


def _matrix_row(m: Any) -> dict:
    """Строка матрицы для пробного разбора (CONTRACTS §11): программа, вид помощи, present, первая ссылка."""
    return {"program": m.program, "care_type": m.care_type, "present": m.present,
            "source_ref": m.source_refs[0] if m.source_refs else None}


def new_job_id() -> str:
    return f"job_{secrets.token_hex(6)}"


# ------------------------------------------------------------------ сервис

class Service:
    """Зависимости сервиса: хранилище приложения (AppStore), разобранный комплект (KitStore), модель, движок.
    В тестах всё подменяется: answer_fn — фейк engine.answer, llm_factory — фейк get_llm, kit — синтетика."""

    def __init__(self, app_store: Any = None, kit_store: Any = None, *, llm_factory: Callable[[], Any] | None = None,
                 answer_fn: Callable[..., Any] | None = None, data_dir: Path | None = None,
                 kit_root: Path | None = None, add_clinic: Any = None,
                 ingest_fn: Callable[..., dict] | None = None):
        from dms_core import config

        self._app = app_store
        self._app_lock = threading.Lock()
        self._kit = kit_store
        self.llm_factory = llm_factory
        self.answer_fn = answer_fn
        self.data_dir = Path(data_dir) if data_dir else config.data_dir()
        self.kit_root = kit_root
        self.add_clinic_mod = add_clinic          # модуль с start/step (CONTRACTS §8); None — dms_core.scenarios
        self.ingest_fn = ingest_fn                # None — scripts/ingest_kit.py::ingest
        self._import_lock = threading.Lock()
        self.import_job: dict = {"state": "idle"}
        # Подключение клиента (ADR 0011): одно задание обработки за раз на процесс; id и договор идущего задания
        self._onboard_lock = threading.Lock()
        self._job_running: str | None = None
        self._job_contract: str | None = None
        # Блокировка на запрос (ревизия w3: потеря обновлений). Все изменяющие операции запроса — ответ, запуск и шаг
        # автомата, правка черновика, закрыть / вернуть / оценить — идут под ней и перечитывают запрос из базы уже
        # под блокировкой: save_request пишет карточку и черновики целиком, без этого последний писатель затирал
        # изменения соседнего потока. Словарь растёт по числу запросов процесса — это байты на запрос.
        self._req_locks: dict[str, threading.Lock] = {}
        self._req_locks_guard = threading.Lock()
        # Таблицы документов договора для экрана разметки (ADR 0012): последние TABLES_CACHE_MAX договоров
        self._tables_cache: dict[str, tuple] = {}
        self._tables_lock = threading.Lock()

    @contextmanager
    def _locked(self, request_id: str) -> Iterator[None]:
        with self._req_locks_guard:
            lock = self._req_locks.setdefault(request_id, threading.Lock())
        with lock:
            yield

    # ---------------------------------------------------------------- зависимости

    @property
    def app(self) -> Any:
        """AppStore открывается при старте сервиса (lifespan) или при первом обращении; открытие = миграция."""
        if self._app is None:
            with self._app_lock:
                if self._app is None:
                    from .store import AppStore

                    self._app = AppStore(self.data_dir / "app" / "app.sqlite")
        return self._app

    @property
    def kit(self) -> Any:
        if self._kit is None:
            from dms_core.tools._common import default_store

            self._kit = default_store()
        return self._kit

    def get_llm(self) -> Any:
        if self.llm_factory is not None:
            return self.llm_factory()
        from dms_core.llm import get_llm

        return get_llm()

    def _engine_answer(self, question: str, context: Any, llm: Any,
                       on_stage: Callable[[str], None] | None = None, trace: dict | None = None,
                       clinic_picks: dict[str, str] | None = None) -> Any:
        if self.answer_fn is not None:
            # подменённый движок (тесты) стадий не сообщает — «до» и «после» вызова
            stage = on_stage or (lambda _s: None)
            stage("route")
            params = inspect.signature(self.answer_fn).parameters
            kw: dict[str, Any] = {"trace": trace} if trace is not None and "trace" in params else {}
            if clinic_picks and "clinic_picks" in params:
                kw["clinic_picks"] = clinic_picks
            out = self.answer_fn(question, context, llm=llm, store=self.kit, **kw)
            stage("compose")
            return out
        from dms_core import engine

        return engine.answer(question, context, llm=llm, store=self.kit, on_stage=on_stage, trace=trace,
                             clinic_picks=clinic_picks)

    # ---------------------------------------------------------------- группы, договоры, документы

    def groups(self) -> list[dict]:
        out = []
        for g in self.kit.groups():
            out.append({"id": g.id, "name": g.name, "contracts": [
                {"id": c.id, "legal_entity": c.legal_entity, "number": c.number, "insurer": c.insurer.value,
                 "programs": [p.name for p in c.programs]} for c in self.kit.contracts(g.id)]})
        return out

    def contract(self, contract_id: str) -> dict:
        try:
            c = self.kit.contract(contract_id)
            docs = self.kit.documents(contract_id)
        except (KeyError, ValueError):
            raise _err_not_found("Договор") from None
        return {**_dump(c), "documents": [_dump(d) for d in docs + self.app.uploads(contract_id=contract_id)]}

    def documents(self, contract_id: str | None, group_id: str | None) -> list[dict]:
        """Документы договора; для ГК — общие документы ГК и документы всех её договоров (дерево ГК → юрлица)."""
        if contract_id:
            try:
                docs = self.kit.documents(contract_id)
            except (KeyError, ValueError):
                raise _err_not_found("Договор") from None
            return [_dump(d) for d in docs + self.app.uploads(contract_id=contract_id)]
        if group_id:
            contracts = self.kit.contracts(group_id)
            if not contracts and group_id not in {g.id for g in self.kit.groups()}:
                raise _err_not_found("Группа компаний")
            docs = self.kit.group_documents(group_id)
            for c in contracts:
                docs += self.kit.documents(c.id)
            return [_dump(d) for d in docs + self.app.uploads(group_id=group_id)]
        raise ServiceError(422, "required", "Укажите договор или группу компаний.", "contract_id")

    def _find_document(self, doc_id: str) -> Any:
        up = self.app.upload(doc_id)
        if up is not None:
            return up
        for g in self.kit.groups():
            for d in self.kit.group_documents(g.id):
                if d.id == doc_id:
                    return d
            for c in self.kit.contracts(g.id):
                for d in self.kit.documents(c.id):
                    if d.id == doc_id:
                        return d
        raise _err_not_found("Документ")

    def document(self, doc_id: str) -> dict:
        return _dump(self._find_document(doc_id))

    def _chat_contracts(self, chat_id: str) -> set[str]:
        """Договоры контекста чата: один договор или все договоры ГК (контекст фиксирован, ADR 0006)."""
        ctx = self._chat(chat_id).context
        if ctx.contract_id:
            return {ctx.contract_id}
        return {c.id for c in self.kit.contracts(ctx.group_id)} if ctx.group_id else set()

    def _parsed_document(self, doc_id: str, chat_id: str | None) -> Any:
        """Разобранный документ договора. chat_id задан — документ обязан принадлежать договору(ам) контекста
        этого чата, иначе 404 (ADR 0005: из чата одного договора пункты другого не открываются)."""
        doc = self._find_document(doc_id)
        if not doc.contract_id or self.app.upload(doc_id) is not None:
            raise ServiceError(404, "not_found", "Для этого документа просмотр пунктов недоступен: показываются "
                                                 "только пункты разобранных документов договора.")
        if chat_id is not None and doc.contract_id not in self._chat_contracts(chat_id):
            raise ServiceError(404, "not_found", "Документ не относится к договорам этого чата.")
        return doc

    def clause(self, doc_id: str, number: str | None, path: str | None, ref: str | None,
               chat_id: str | None = None) -> dict:
        """Пункт или строка клиник для панели «Источники». Поиск — только в договоре этого документа (ADR 0005)."""
        doc = self._parsed_document(doc_id, chat_id)
        cid = doc.contract_id
        if ref:
            for cl in self.kit.clauses(cid):
                if cl.id == ref and cl.doc_id == doc_id:
                    return {"clause": _dump(cl)}
            from dms_core.table_mapping import DERIVED_SUFFIX

            # строка матрицы «виды помощи из перечня» (ADR 0013): ссылка — строка перечня + «#care»
            base = ref.removesuffix(DERIVED_SUFFIX)
            for row in self.kit.clinics(cid, doc_id=doc_id):
                if base in (row.id, row.source_ref):
                    return {"clinic": _dump(row)}
            raise _err_not_found("Пункт")
        if not number and not path:
            raise ServiceError(422, "required", "Укажите номер пункта, путь или ссылку.", "number")
        hits = [c for c in self.kit.clause(cid, number=number, path=path) if c.doc_id == doc_id]
        if not hits:
            raise _err_not_found("Пункт")
        return {"clause": _dump(hits[0])}

    def document_clauses(self, doc_id: str, limit: int = 2000, chat_id: str | None = None) -> dict:
        """Пункты документа по порядку — для просмотра на странице оператора (без строк клиник)."""
        doc = self._parsed_document(doc_id, chat_id)
        rows = [c for c in self.kit.clauses(doc.contract_id) if c.doc_id == doc_id]
        return {"clauses": [_dump(c) for c in rows[:limit]], "total": len(rows), "truncated": len(rows) > limit}

    # ---------------------------------------------------------------- загрузка документа

    def upload(self, *, filename: str, data: bytes, contract_id: str | None, group_id: str | None,
               doc_type: str | None, uploaded_by: str | None) -> dict:
        """Загрузка файла оператором: сигнатура → сохранение в data/derived/uploads (вне git) → извлечение текста →
        проверка полноты → метаданные (кто, когда, источник, формат) и статус ТЗ п. 7.1.

        Индексация отдельного файла в поиск договора не выполняется: разбор договора — целиком адаптером
        (import-kit), поэтому статус останавливается на «Полнота проверена» с предупреждением."""
        from dms_core.constants import DocStatus, DocType
        from dms_core.io.errors import InputError
        from dms_core.kit import classify
        from dms_core.schemas import Document

        if not data:
            raise ServiceError(422, "required", "Выберите файл.", "file")
        fmt = sniff_format(filename, data)
        if contract_id:
            try:
                c = self.kit.contract(contract_id)
            except (KeyError, ValueError):
                raise ServiceError(422, "invalid", "Договор не найден: выберите договор из списка.",
                                   "contract_id") from None
            if group_id and group_id != c.group_id:
                raise ServiceError(422, "invalid", "Договор не относится к выбранной группе компаний.", "group_id")
            group_id = c.group_id
        elif not group_id or group_id not in {g.id for g in self.kit.groups()}:
            raise ServiceError(422, "required", "Выберите договор или группу компаний.", "contract_id")
        if doc_type:
            try:
                dtype = DocType(doc_type)
            except ValueError:
                raise ServiceError(422, "invalid", "Неизвестный тип документа.", "doc_type") from None
        else:
            dtype = DocType(classify(filename)[0])
        who = _text({"u": uploaded_by}, "u", required=False, limit=100, label="Кто загрузил") or "operator"
        sha = hashlib.sha256(data).hexdigest()
        doc_id = f"up_{sha[:12]}"
        target = self.data_dir / "derived" / "uploads" / f"{doc_id}.{fmt}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        title = Path(filename).stem[:MAX_TITLE_CHARS] or doc_id
        doc = Document(id=doc_id, contract_id=contract_id or None, group_id=group_id, title=title, doc_type=dtype,
                       source_path=f"derived/uploads/{target.name}", original_format=fmt, sha256=sha,
                       uploaded_by=who, uploaded_at=dt.datetime.now(dt.UTC), status=DocStatus.received)
        try:
            stats = extract_text_stats(target, fmt)
            doc.status = DocStatus.extracted
            if stats.get("chars", 0) <= 0:
                doc.warnings.append("Текст не найден: файл пустой или это скан без текстового слоя.")
            else:
                doc.status = DocStatus.checked
                if fmt == "docx" and dtype in (DocType.contract, DocType.program) and not stats.get("numbered"):
                    doc.warnings.append("В документе нет автонумерации пунктов: ссылки будут без номеров.")
                doc.warnings.append("Файл сохранён и проверен, но в ответах не участвует: ответы строятся по "
                                    "документам комплекта. Чтобы файл учитывался, положите его в папку договора "
                                    "комплекта и выполните «Импорт комплекта».")
        except InputError as e:
            doc.status = DocStatus.error
            doc.warnings.append(e.message)
        except Exception as e:  # noqa: BLE001 — сбой читателя не роняет загрузку: статус «Ошибка» (ТЗ п. 7.1)
            log.warning("upload %s: ошибка чтения %s", doc_id, type(e).__name__)
            doc.status = DocStatus.error
            doc.warnings.append("Файл не удалось прочитать. Проверьте, что он открывается, и загрузите снова.")
        self.app.save_upload(doc)
        log.info("upload %s fmt=%s status=%s", doc_id, fmt, doc.status.value)
        return _dump(doc)

    # ---------------------------------------------------------------- импорт комплекта (в фоне)

    def _ingest_fn(self) -> Callable[..., dict]:
        """scripts/ingest_kit.py::ingest. Скрипт не пакет — грузим по пути; в образе без scripts/ — ошибка 501."""
        from dms_core.config import REPO_ROOT

        path = Path(REPO_ROOT) / "scripts" / "ingest_kit.py"
        if not path.is_file():
            raise ServiceError(501, "not_available", "Импорт комплекта недоступен в этой сборке: запустите "
                                                     "`uv run python scripts/ingest_kit.py`.")
        spec = importlib.util.spec_from_file_location("dms_ingest_kit", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod.ingest

    def start_import(self, gate: Any, ingest_fn: Callable[..., dict] | None = None) -> dict:
        """Запуск импорта в фоновом потоке. Одновременно — один импорт; он занимает один слот HeavyGate на всё
        время работы (нет слота — 503). Ответ — состояние задачи; отчёт без текстов документов."""
        from dms_core.config import kit_dir

        ingest = ingest_fn or self.ingest_fn or self._ingest_fn()
        root = self.kit_root or kit_dir()
        if not root.is_dir():
            raise ServiceError(422, "no_kit", "Комплект документов не найден: положите его в data/raw/ "
                                              "и повторите.")
        if self._onboard_lock.locked():
            raise ServiceError(409, "job_running", "Идёт обработка договора клиента. Дождитесь окончания.")
        if not self._import_lock.acquire(blocking=False):
            raise ServiceError(409, "import_running", "Импорт уже идёт. Дождитесь окончания.")
        try:
            slot = gate.slot(client="__import__")
            slot.__enter__()
        except BaseException:
            self._import_lock.release()
            raise
        self.import_job = {"state": "running", "started_at": dt.datetime.now(dt.UTC).isoformat()}
        kit = self.kit
        # договоры клиентов после импорта комплекта — заново из реестра (ADR 0011 п. 5), если импорт это умеет
        params = inspect.signature(ingest).parameters
        kw: dict[str, Any] = {}
        if "registry" in params:
            kw["registry"] = self.app
        if "clients_root" in params:
            kw["clients_root"] = self.clients_root

        def run() -> None:
            t0 = time.perf_counter()
            try:
                report = ingest(root, kit, **kw)
                self.import_job = {"state": "done", "seconds": round(time.perf_counter() - t0, 1),
                                   "report": _safe_report(report)}
                log.info("import-kit: готово за %.1f с", time.perf_counter() - t0)
            except Exception as e:  # noqa: BLE001 — сбой фоновой задачи виден в состоянии задачи
                log.warning("import-kit: ошибка %s", type(e).__name__)
                self.import_job = {"state": "error", "message": "Импорт не удался. Проверьте комплект и "
                                                                "запустите скрипт импорта вручную."}
            finally:
                slot.__exit__(None, None, None)
                self._import_lock.release()

        threading.Thread(target=run, name="dms-import-kit", daemon=True).start()
        return dict(self.import_job)

    def import_status(self) -> dict:
        return dict(self.import_job)

    # ---------------------------------------------------------------- подключение клиента (ADR 0011, CONTRACTS §9)

    @property
    def clients_root(self) -> Path:
        """Папка файлов клиентов: <data_dir>/derived/clients (вне git; в Docker — том dms-derived)."""
        return self.data_dir / "derived" / "clients"

    def _client_contract(self, contract_id: str) -> Any:
        try:
            return self.app.client_contract(contract_id)
        except KeyError:
            raise _err_not_found("Договор клиента") from None

    def _contract_fields(self, body: Any, prefix: str = "") -> dict:
        """{legal_entity, insurer?, number?} договора клиента: проверка с сообщениями по-русски."""
        from dms_core.constants import Insurer

        if not isinstance(body, dict):
            raise ServiceError(422, "invalid", "Договор заполнен неверно: укажите юрлицо.", f"{prefix}legal_entity")
        legal = _text(body, "legal_entity", required=True, limit=MAX_TITLE_CHARS, label="Юрлицо")
        number = _text(body, "number", required=False, limit=100, label="Номер договора")
        raw = _text(body, "insurer", required=False, limit=40, label="Страховая компания")
        insurer = None
        if raw:
            try:
                insurer = Insurer(raw)
            except ValueError:
                raise ServiceError(422, "invalid", "Неизвестная страховая компания: выберите из списка или оставьте "
                                                   "пустым — она определится по документам.",
                                   f"{prefix}insurer") from None
            insurer = None if insurer is Insurer.unknown else insurer
        insurer_name = _text(body, "insurer_name", required=False, limit=MAX_TITLE_CHARS,
                             label="Название страховой компании")
        force = body.get("force_generic", False)
        if not isinstance(force, bool):
            raise ServiceError(422, "invalid", "«Не распознавать формат»: да или нет.", f"{prefix}force_generic")
        return {"legal_entity": legal, "insurer_hint": insurer, "number": number, "insurer_name": insurer_name,
                "force_generic": force}

    def create_client(self, body: dict) -> dict:
        name = _text(body, "name", required=True, limit=MAX_TITLE_CHARS, label="Название клиента")
        raw = body.get("contracts") or []
        if not isinstance(raw, list):
            raise ServiceError(422, "invalid", "Список договоров заполнен неверно.", "contracts")
        if len(raw) > MAX_CLIENT_CONTRACTS:
            raise ServiceError(422, "too_many", f"Не больше {MAX_CLIENT_CONTRACTS} договоров за раз.", "contracts")
        fields = [self._contract_fields(c, prefix=f"contracts.{i}.") for i, c in enumerate(raw)]
        g = self.app.create_client_group(name)
        for f in fields:
            self.app.add_client_contract(g.id, **f)
        log.info("client %s: создан, договоров %d", g.id, len(fields))
        return _dump(self.app.client_group(g.id))

    def add_client_contract(self, group_id: str, body: dict) -> dict:
        fields = self._contract_fields(body)
        try:
            c = self.app.add_client_contract(group_id, **fields)
        except KeyError:
            raise _err_not_found("Клиент") from None
        log.info("client %s: добавлен договор %s", group_id, c.id)
        return _dump(c)

    def _job_view(self, job: Any) -> Any:
        """Задание из реестра; «идёт» в базе, но не в этом процессе — прервано перезапуском сервиса."""
        if job is not None and job.state in ("queued", "running") and job.id != self._job_running:
            job = job.model_copy(update={"state": "error", "error": ERR_JOB_LOST})
        return job

    def clients(self) -> list[dict]:
        from dms_core.onboarding import files_changed

        try:
            searchable = {c.id for c in self.kit.contracts()}
        except Exception:  # noqa: BLE001 — нет базы комплекта: список клиентов всё равно показывается
            searchable = set()
        out = []
        for g in self.app.client_groups():
            item = _dump(g)
            item["files"] = [_dump(f) for f in self.app.client_files(g.id)]
            item["contracts"] = []
            for c in g.contracts:
                job = self._job_view(self.app.last_job(c.id))
                # файлы договора и общие файлы группы (scope="group") — в files каждого договора группы
                files = self.app.client_files(c.id) + self.app.client_files(g.id)
                # files_changed — файлы (имя, область, тип, sha256) отличаются от обработанных при последнем done:
                # договор в поиске остаётся в обработанной версии, пока его не обработают заново (ревизия w8 п. 7)
                item["contracts"].append({**_dump(c), "files": [_dump(f) for f in files],
                                          "job": _dump(job) if job is not None else None,
                                          "files_changed": files_changed(files, self.app.processed_files(c.id)),
                                          "in_search": c.id in searchable})
            out.append(item)
        return out

    def _client_dir(self, group_id: str, owner_dir: str) -> Path:
        """Папка файлов клиента внутри clients_root; ссылки на пути не допускаются (ADR 0011, правило данных)."""
        root = self.clients_root
        folder = root / group_id / owner_dir
        for p in (root, root / group_id, folder):
            if p.is_symlink():
                raise ServiceError(500, "internal", "Папка файлов клиента недоступна.")
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.resolve().is_relative_to(root.resolve()):
            raise ServiceError(500, "internal", "Папка файлов клиента недоступна.")
        return folder

    def _locate_file(self, cc: Any, name: str) -> tuple[str, Any]:
        """(владелец, ClientFile) по имени: сначала файлы договора, затем общие файлы группы."""
        for owner in (cc.id, cc.group_id):
            for f in self.app.client_files(owner):
                if f.name == name:
                    return owner, f
        raise _err_not_found("Файл")

    def _owner_dir(self, cc: Any, scope: str) -> tuple[str, Path]:
        from dms_core.onboarding import GROUP_DIR

        if scope == "group":
            return cc.group_id, self._client_dir(cc.group_id, GROUP_DIR)
        return cc.id, self._client_dir(cc.group_id, cc.id)

    def _require_idle(self, cc: Any, scope: str | None = None) -> None:
        """Файлы нельзя менять, пока их читают: идёт импорт комплекта (он заново разбирает договоры клиентов —
        409 import_running) или обработка этого договора; общие файлы группы (scope=group) — и обработка любого
        договора группы (он читает их тоже) — 409 job_running."""
        if self._import_lock.locked():
            raise ServiceError(409, "import_running", "Идёт импорт комплекта. Дождитесь окончания и повторите.")
        running = self._job_contract if self._job_running is not None else None
        if running is None:
            return
        if running == cc.id:
            raise ServiceError(409, "job_running", "Договор сейчас обрабатывается: дождитесь окончания и повторите.")
        if scope == "group":
            try:
                same_group = self.app.client_contract(running).group_id == cc.group_id
            except KeyError:
                same_group = False
            if same_group:
                raise ServiceError(409, "job_running", "Идёт обработка договора этой группы: общие файлы группы "
                                                       "можно менять после окончания.")

    @staticmethod
    def _doc_type_scope(name: str, doc_type: str | None, scope: str | None) -> tuple[str, str]:
        from dms_core.constants import DocType
        from dms_core.kit import classify

        auto_type, auto_scope = classify(name)
        if doc_type:
            try:
                DocType(doc_type)
            except ValueError:
                raise ServiceError(422, "invalid", "Неизвестный тип документа.", "doc_type") from None
        if scope and scope not in ("contract", "group"):
            raise ServiceError(422, "invalid", "Область документа: «договор» или «вся группа».", "scope")
        return doc_type or auto_type, scope or auto_scope

    def upload_client_file(self, contract_id: str, *, filename: str, data: bytes, doc_type: str | None = None,
                           scope: str | None = None) -> dict:
        """Файл клиента: имя очищается от пути и опасных символов, формат — по сигнатуре (sniff_format), не больше
        CLIENT_MAX_FILES на договор; то же имя — замена файла (новый sha256). В журнал — только id и формат."""
        from dms_core.constants import CLIENT_MAX_FILES
        from dms_core.schemas import ClientFile

        cc = self._client_contract(contract_id)
        self._require_idle(cc)
        if not data:
            raise ServiceError(422, "required", "Выберите файл.", "file")
        name = clean_filename(filename)
        fmt = sniff_format(name, data)
        dtype, dscope = self._doc_type_scope(name, doc_type, scope)
        old = next((f for o in (cc.id, cc.group_id) for f in self.app.client_files(o) if f.name == name), None)
        self._require_idle(cc, "group" if "group" in (dscope, getattr(old, "scope", None)) else None)
        owner, folder = self._owner_dir(cc, dscope)
        existing = {f.name: o for o in (cc.id, cc.group_id) for f in self.app.client_files(o)}
        own = [f for f in self.app.client_files(cc.id)] + [f for f in self.app.client_files(cc.group_id)]
        if name not in existing and len(own) >= CLIENT_MAX_FILES:
            raise ServiceError(422, "too_many_files", f"У договора уже {CLIENT_MAX_FILES} файлов: удалите лишние.",
                               "file")
        target = folder / name
        tmp = folder / f".{secrets.token_hex(6)}.part"
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        except OSError:
            # недописанный .part не остаётся в папке клиента ни при какой ошибке записи (место, права, имя)
            with contextlib.suppress(OSError):
                tmp.unlink()
            log.warning("client file %s: ошибка записи", cc.id)
            raise ServiceError(500, "write_failed", "Файл не удалось сохранить на сервере. Повторите позже.",
                               "file") from None
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        old_owner = existing.get(name)
        if old_owner is not None and old_owner != owner:        # тот же файл в другой области — перенос
            self._remove_file(cc, old_owner, name)
        f = ClientFile(name=name, doc_type=dtype, scope=dscope, fmt=fmt, size=len(data),
                       sha256=hashlib.sha256(data).hexdigest(), uploaded_at=dt.datetime.now(dt.UTC))
        self.app.save_client_file(owner, f)
        log.info("client file %s: загружен fmt=%s scope=%s", cc.id, fmt, dscope)
        return _dump(f)

    def _remove_file(self, cc: Any, owner: str, name: str) -> None:
        from dms_core.onboarding import GROUP_DIR

        folder = self._client_dir(cc.group_id, GROUP_DIR if owner == cc.group_id else cc.id)
        p = folder / name
        if p.is_file() and not p.is_symlink():
            p.unlink()
        self.app.delete_client_file(owner, name)

    def patch_client_file(self, contract_id: str, name: str, body: dict) -> dict:
        """Тип и область файла (выбор клиента). Смена области переносит файл между папкой договора и _group/."""
        cc = self._client_contract(contract_id)
        self._require_idle(cc)
        owner, f = self._locate_file(cc, name)
        self._require_idle(cc, f.scope if body.get("scope") in (None, f.scope) else "group")
        doc_type = _text(body, "doc_type", required=False, limit=40, label="Тип документа")
        scope = _text(body, "scope", required=False, limit=20, label="Область")
        dtype, dscope = self._doc_type_scope(f.name, doc_type or f.doc_type, scope or f.scope)
        new_owner, new_dir = self._owner_dir(cc, dscope)
        if new_owner != owner:
            _, old_dir = self._owner_dir(cc, f.scope)
            src = old_dir / f.name
            if src.is_file() and not src.is_symlink():
                os.replace(src, new_dir / f.name)
            self.app.delete_client_file(owner, f.name)
        f = f.model_copy(update={"doc_type": dtype, "scope": dscope})
        self.app.save_client_file(new_owner, f)
        log.info("client file %s: тип или область изменены", cc.id)
        return _dump(f)

    def delete_client_file(self, contract_id: str, name: str) -> None:
        cc = self._client_contract(contract_id)
        self._require_idle(cc)
        owner, f = self._locate_file(cc, name)
        self._require_idle(cc, f.scope)
        self._remove_file(cc, owner, f.name)
        log.info("client file %s: удалён", cc.id)

    def client_job(self, job_id: str) -> dict:
        try:
            return _dump(self._job_view(self.app.job(job_id)))
        except KeyError:
            raise _err_not_found("Задание") from None

    def start_onboarding(self, contract_id: str, gate: Any) -> dict:
        """Обработка договора клиента в фоновом потоке (onboarding.process). Одно задание за раз на процесс
        (сервер 2–4 ГБ): идёт другое задание или импорт комплекта — 409; задание занимает слот HeavyGate."""
        from dms_core.schemas import OnboardingJob

        cc = self._client_contract(contract_id)
        from dms_core.onboarding import GROUP_ONLY_TYPES

        files = self.app.client_files(cc.id) + self.app.client_files(cc.group_id)
        if not [f for f in files if f.scope != "group" or f.doc_type not in GROUP_ONLY_TYPES]:
            raise ServiceError(422, "no_files", "Загрузите хотя бы один файл договора.", "file")
        if self._import_lock.locked():
            raise ServiceError(409, "import_running", "Идёт импорт комплекта. Дождитесь окончания и повторите.")
        if not self._onboard_lock.acquire(blocking=False):
            raise ServiceError(409, "job_running", "Уже идёт обработка договора. Дождитесь окончания и повторите.")
        try:
            slot = gate.slot(client="__onboarding__")
            slot.__enter__()
        except BaseException:
            self._onboard_lock.release()
            raise
        job = OnboardingJob(id=new_job_id(), contract_id=cc.id, state="queued", stage="received",
                            created_at=dt.datetime.now(dt.UTC))
        self._job_running, self._job_contract = job.id, cc.id
        self.app.save_job(job)
        kit, registry, root = self.kit, self.app, self.clients_root

        def on_stage(name: str) -> None:
            job.state, job.stage = "running", name
            self.app.save_job(job)

        def run() -> None:
            from dms_core import onboarding

            t0 = time.perf_counter()
            try:
                report = onboarding.process(cc.id, store=kit, registry=registry, on_stage=on_stage, root=root,
                                            files=files)
                job.report = report
                if report.readiness == "failed":
                    job.state, job.error = "error", ERR_JOB_FAILED
                else:
                    job.state = "done"
            except onboarding.OnboardingError as e:
                job.state, job.error = "error", e.message
            except Exception as e:  # noqa: BLE001 — сбой фонового задания виден в состоянии задания
                log.warning("onboarding %s: ошибка %s", cc.id, type(e).__name__)
                job.state, job.error = "error", ERR_JOB_FAILED
            finally:
                job.finished_at = dt.datetime.now(dt.UTC)
                try:
                    self.app.save_job(job)
                finally:
                    self._job_running = self._job_contract = None
                    slot.__exit__(None, None, None)
                    self._onboard_lock.release()
                log.info("onboarding %s: %s за %.1f с", cc.id, job.state, time.perf_counter() - t0)

        threading.Thread(target=run, name="dms-onboarding", daemon=True).start()
        return _dump(job)

    # ---------------------------------------------------------------- разметка таблиц оператором (ADR 0012, §10)

    def _processed_kc(self, cc: Any) -> Any:
        """Опись договора по снимку файлов последней успешной обработки (таблицы — те, что разобраны в базе)."""
        from dms_core.onboarding import kit_contract

        files = self.app.processed_files(cc.id)
        if files is None:
            return None                     # договор не обрабатывался: таблиц для разметки нет
        return kit_contract(self.app.client_group(cc.group_id), cc, files)

    def _tables(self, cc: Any) -> list:
        """Таблицы документов договора (table_mapping.list_tables) по снимку обработки; кэш — по набору файлов
        (sha256) и версии документов в базе: чтение больших docx не повторяется на каждый запрос экрана."""
        from dms_core import table_mapping as tm

        kc = self._processed_kc(cc)
        if kc is None:
            return []
        try:
            docs = self.kit.documents(cc.id)
        except KeyError:
            docs = []
        doc_ids = {d.source_path: d.id for d in docs}
        key = (cc.id, tuple(sorted((f.path, f.sha256) for f in kc.files)), tuple(sorted(doc_ids.items())))
        with self._tables_lock:
            hit = self._tables_cache.get(cc.id)
            if hit is not None and hit[0] == key:
                return hit[1]
        tables = tm.list_tables(kc, self.clients_root, doc_ids)
        with self._tables_lock:
            self._tables_cache[cc.id] = (key, tables)
            while len(self._tables_cache) > TABLES_CACHE_MAX:
                self._tables_cache.pop(next(iter(self._tables_cache)))
        return tables

    @staticmethod
    def _table_ref(raw: Any) -> Any:
        from dms_core.schemas import TableRef

        try:
            return TableRef.model_validate(raw)
        except ValidationError:
            raise ServiceError(422, "invalid", _map_error("table_not_found"), "ref") from None

    @staticmethod
    def _mapping(raw: Any) -> Any:
        from dms_core.schemas import TableMapping

        if isinstance(raw, dict) and isinstance(raw.get("columns"), dict):
            # номера колонок в JSON — строки ключей объекта
            raw = {**raw, "columns": {str(k): v for k, v in raw["columns"].items()}}
        try:
            return TableMapping.model_validate(raw)
        except ValidationError:
            raise ServiceError(422, "invalid", _map_error("mapping_invalid"), "mapping") from None

    def _table(self, cc: Any, ref: Any) -> tuple:
        from dms_core import table_mapping as tm

        found = tm.find_table(self._tables(cc), ref)
        if found is None:
            raise ServiceError(422, "table_not_found", _map_error("table_not_found"), "ref")
        return found

    def _programs(self, cc: Any) -> list[str]:
        """Программы договора по последней обработке и программы строк перечня (ADR 0013: программа матрицы должна
        совпасть с одной из них)."""
        try:
            names = [p.name for p in self.kit.contract(cc.id).programs]
            names += [r.program for r in self.kit.clinics(cc.id)]
        except (KeyError, ValueError):
            return []
        return [p for p in dict.fromkeys(names) if p]

    @staticmethod
    def _check_mapping(rows: list, mapping: Any, programs: list[str] | None = None) -> None:
        """Разметка подходит к таблице (перечень — validate, матрица — validate_matrix: mapping_no_care,
        mapping_no_program; единственная программа договора — источник программы длинной матрицы)."""
        from dms_core import table_mapping as tm

        try:
            tm.validate_any(rows, mapping, single_program=len(programs or []) == 1)
        except tm.MappingError as e:
            raise ServiceError(422, e.code if e.code != "mapping_bad_header" else "mapping_bad_column", e.message,
                               f"mapping.{e.field}" if e.field else "mapping") from None

    @staticmethod
    def _preview(ref: Any, title: str, rows: list, own: dict, profiles: list, counts: dict[str, int],
                 insurer_name: str | None = None, mapped_rows: list | None = None,
                 programs: list[str] | None = None) -> dict:
        """TablePreview + поля Дополнения §10 сверх схемы: layout — подпись раскладки таблицы, same_layout — сколько
        таблиц договора (с этой) с той же раскладкой (предложение «строки заголовка нет» и флажок «Применить ко всем
        таблицам с такой же раскладкой (N)»); w9-fix: applied_profile — id профиля СК, который размечает таблицу сам
        при обработке (table_mapping.auto_profile: та же СК, сильная подпись, содержимое похоже на перечень; на
        экране «размечено профилем СК «…»», а не «предложено»), suspicious — причины, почему разметка таблицы (своя
        или профиля) не похожа на перечень, similar_unmapped — таблица не размечена, но похожа по раскладке на
        размеченную (mapped_rows — строки размеченных таблиц договора)."""
        from dms_core import table_mapping as tm

        layout = tm.layout_signature(rows)
        same = counts.get(layout, 1) if layout else 1
        mapping = own.get(tm.ref_key(ref))
        pv = _dump(tm.preview(ref, title, rows, mapping, profiles, same, insurer_name, programs))
        auto = None if mapping is not None else tm.auto_profile(rows, profiles, insurer_name, programs)[0]
        if auto is not None:
            pv["profile_id"] = auto.id
        use = mapping or (auto.mapping if auto else None)
        suspicious: list[str] = []
        if use is not None:
            try:
                tm.validate_any(rows, use, single_program=len(programs or []) == 1)
                suspicious = tm.check_any(rows, use, programs)
            except tm.MappingError:
                suspicious = []
        similar = use is None and any(tm.similar(rows, r) for r in mapped_rows or [])
        return pv | {"layout": layout or None, "same_layout": same, "applied_profile": auto.id if auto else None,
                     "suspicious": suspicious, "similar_unmapped": similar}

    def contract_tables(self, contract_id: str) -> list[dict]:
        """GET …/tables: TablePreview по каждой таблице документов последней обработки (разметка договора,
        предложение — профиль СК по подписи или угадка; профиль, который применится сам, — applied_profile)."""
        from dms_core import table_mapping as tm

        cc = self._client_contract(contract_id)
        own = {tm.ref_key(r): m for r, m in self.app.table_mappings(cc.id)}
        profiles = self.app.insurer_profiles()
        tables = self._tables(cc)
        counts = tm.layout_counts(tables)
        programs = self._programs(cc)
        mapped_rows = [rows for ref, _t, rows in tables
                       if tm.ref_key(ref) in own or tm.auto_profile(rows, profiles, cc.insurer_name, programs)[0]
                       is not None]
        out = [self._preview(ref, title, rows, own, profiles, counts, cc.insurer_name, mapped_rows, programs)
               for ref, title, rows in tables]
        log.info("tables %s: таблиц %d, размечено %d", cc.id, len(out), len(own))
        return out

    def _require_generic(self, cc: Any) -> None:
        """Разметка таблиц — только договорам, разобранным запасным адаптером (ADR 0012 п. 4): у распознанной СК
        разметка не применяется, хранить её незачем (w9-fix п. 6) — 409 mapping_not_generic."""
        from dms_core.constants import Insurer

        try:
            insurer = self.kit.contract(cc.id).insurer
        except (KeyError, ValueError):
            return
        if insurer is not Insurer.unknown:
            raise ServiceError(409, "mapping_not_generic", _map_error("mapping_not_generic"))

    def try_mapping(self, contract_id: str, body: dict) -> dict:
        """POST …/tables/try: пробный разбор таблицы по разметке — ничего не сохраняется."""
        from dms_core import table_mapping as tm
        from dms_core.constants import MAPPING_SAMPLE_ROWS, MATRIX_SAMPLE_ROWS

        cc = self._client_contract(contract_id)
        ref, mapping = self._table_ref(body.get("ref")), self._mapping(body.get("mapping"))
        ref, title, rows = self._table(cc, ref)
        programs = self._programs(cc)
        self._check_mapping(rows, mapping, programs)
        label = tm.table_label(ref, title)
        # проверка содержимого (w9-fix п. 1б, ADR 0013): подозрительная разметка — предупреждение (сохранить — с
        # подтверждением)
        suspicious = tm.check_any(rows, mapping, programs)
        if mapping.role == "care_matrix":
            # CONTRACTS §11: пробный разбор матрицы — пары программа × вид помощи и пропущенные ячейки
            res = tm.apply_matrix_rows(cc.id, ref, rows, mapping, programs)
            warnings = tm.matrix_warnings(label, res)
            # «нет» матрицы при клиниках этого вида в перечне последней обработки (этап w10-fix2, `docs/rules.md` п. 2):
            # в ответах это противоречие документов — оператор видит его до сохранения
            try:
                contra = tm.list_conflicts(res.rows, list(self.kit.clinics(cc.id)))
            except (KeyError, ValueError):
                contra = []
            if contra:
                warnings.append(tm.list_conflict_warning(contra))
            if suspicious:
                warnings = [tm.mwarn("suspicious", table=label, reasons="; ".join(suspicious)), *warnings]
            return {"matrix": [_matrix_row(m) for m in res.rows[:MATRIX_SAMPLE_ROWS]], "total": len(res.rows),
                    "skipped": res.skipped, "skipped_total": res.skipped_total, "warnings": warnings,
                    "suspicious": suspicious}
        got, warnings = tm.apply(cc.id, ref, rows, mapping, ref.doc_id, title=label)
        if (mapping.program or "").strip() == tm.ALL_PROGRAMS and "program" not in mapping.columns.values():
            # «Все программы договора»: строки — каждой программе договора по последней обработке
            if not programs:
                warnings.append(tm.warn_text("all_no_programs", table=label))
            got = tm.expand_all_programs(got, programs)
        if suspicious:
            warnings = [tm.warn_text("suspicious", table=label, reasons="; ".join(suspicious)), *warnings]
        out = {"rows": [_dump(r) for r in got[:MAPPING_SAMPLE_ROWS]], "total": len(got), "warnings": warnings,
               "suspicious": suspicious}
        if mapping.derive_care:
            # «Виды помощи из этого перечня → матрица» (ADR 0013 п. 3): какие пары даст перечень
            derived = tm.matrix_from_clinics(cc.id, got)
            out |= {"matrix": [_matrix_row(m) for m in derived[:MATRIX_SAMPLE_ROWS]], "matrix_total": len(derived)}
        return out

    def save_mapping(self, contract_id: str, body: dict, gate: Any = None) -> tuple[int, dict]:
        """PUT …/tables/mapping: разметка договора; apply_same_layout — та же разметка всем таблицам договора с той же
        подписью (ответ: applied_tables — сколько таблиц размечено); save_profile — профиль СК по подписи (название СК —
        insurer_name тела или карточки договора; новое название сохраняется в карточку); process: true — сразу
        обработка договора (202, {preview, profile, job: OnboardingJob}). Пока договор обрабатывается — 409
        job_running. Колонки без роли (ignore) клиент может не передавать: нет колонки в разметке — ignore."""
        from dms_core import table_mapping as tm

        cc = self._client_contract(contract_id)
        self._require_idle(cc)
        self._require_generic(cc)
        ref, mapping = self._table_ref(body.get("ref")), self._mapping(body.get("mapping"))
        ref, title, rows = self._table(cc, ref)
        programs = self._programs(cc)
        self._check_mapping(rows, mapping, programs)
        save_profile, process = body.get("save_profile", False), body.get("process", False)
        same_layout, confirm = body.get("apply_same_layout", False), body.get("confirm_suspicious", False)
        if not all(isinstance(x, bool) for x in (save_profile, process, same_layout, confirm)):
            raise ServiceError(422, "invalid", _map_error("mapping_invalid"), "save_profile")
        if mapping.role != "care_matrix" and not tm.has_program_source(mapping):
            # w9-fix п. 1г: без источника программы строки получали «программу договора» «Перечень клиник договора»
            # (у матрицы источник программы проверяет validate_matrix — mapping_no_program)
            raise ServiceError(422, "mapping_no_program", _map_error("mapping_no_program"), "mapping.program")
        suspicious = tm.check_any(rows, mapping, programs)
        if suspicious and not confirm:
            # w9-fix п. 1б: таблица премий («Итого», суммы) вместо перечня — сохранение только с подтверждением;
            # ADR 0013: колонка видов помощи / программ не похожа на матрицу — так же
            key = "mapping_matrix_suspicious" if mapping.role == "care_matrix" else "mapping_suspicious"
            raise ServiceError(422, "mapping_suspicious", _map_error(key, reasons="; ".join(suspicious)), "mapping")
        insurer_name = _text(body, "insurer_name", required=False, limit=MAX_TITLE_CHARS,
                             label="Название страховой компании") or cc.insurer_name
        profile = None
        if save_profile:
            sig = tm.mapping_signature(rows, mapping)
            if not insurer_name:
                raise ServiceError(422, "required", _map_error("insurer_name"), "insurer_name")
            if not sig:
                raise ServiceError(422, "mapping_bad_column", _map_error("mapping_invalid"), "mapping.header_row")
            profile = self.app.save_insurer_profile(insurer_name, sig, tm.profile_mapping(rows, mapping))
        if insurer_name and insurer_name != cc.insurer_name:
            cc = cc.model_copy(update={"insurer_name": insurer_name})
            self.app.save_client_contract(cc)
        # apply_same_layout (Дополнение §10): та же разметка — всем таблицам договора с той же подписью (по раскладке
        # или по строке заголовка), к которым она подходит; выбранная таблица — всегда
        targets = [ref]
        if same_layout:
            key = tm.ref_key(ref)
            targets += [t[0] for t in tm.same_signature_tables(self._tables(cc), rows, mapping)
                        if tm.ref_key(t[0]) != key]
        for t_ref in targets:
            self.app.save_table_mapping(cc.id, t_ref, mapping)
        log.info("mapping %s: сохранена разметка таблиц %d, профиль %s", cc.id, len(targets),
                 profile.id if profile else "нет")
        own = {tm.ref_key(r): m for r, m in self.app.table_mappings(cc.id)}
        pv = self._preview(ref, title, rows, own, self.app.insurer_profiles(), tm.layout_counts(self._tables(cc)),
                           cc.insurer_name, programs=programs)
        out = {"preview": pv, "profile": _dump(profile) if profile else None, "applied_tables": len(targets)}
        if process:
            # уточнение автора к §10: с process: true — то же тело и задание обработки (202)
            return 202, out | {"job": self.start_onboarding(cc.id, gate)}
        return 200, out

    def delete_mapping(self, contract_id: str, query: dict) -> None:
        """DELETE …/tables/mapping?doc_id=&kind=&index=&sheet= — разметка таблицы договора (нет её — тоже 204)."""
        cc = self._client_contract(contract_id)
        self._require_idle(cc)
        raw: dict[str, Any] = {k: query.get(k) or None for k in ("doc_id", "kind", "sheet")}
        idx = query.get("index")
        if idx not in (None, ""):
            try:
                raw["index"] = int(str(idx))
            except ValueError:
                raise ServiceError(422, "invalid", _map_error("table_not_found"), "index") from None
        self.app.delete_table_mapping(cc.id, self._table_ref(raw))
        log.info("mapping %s: разметка таблицы удалена", cc.id)

    def insurer_profiles(self) -> list[dict]:
        return [_dump(p) for p in self.app.insurer_profiles()]

    def delete_insurer_profile(self, profile_id: str) -> None:
        if not self.app.delete_insurer_profile(profile_id):
            raise ServiceError(404, "not_found", _map_error("profile_not_found"))
        log.info("profile %s: удалён", profile_id)

    # ---------------------------------------------------------------- чаты и запросы

    def _validate_context(self, raw: Any) -> Any:
        from dms_core.schemas import ChatContext

        if not isinstance(raw, dict):
            raise ServiceError(422, "required", "Выберите группу компаний или договор.", "context")
        try:
            ctx = ChatContext.model_validate(raw)
        except ValidationError:
            raise ServiceError(422, "invalid", "Контекст чата заполнен неверно: проверьте группу, договор, "
                                               "программу и дату (ГГГГ-ММ-ДД).", "context") from None
        if ctx.contract_id:
            try:
                c = self.kit.contract(ctx.contract_id)
            except (KeyError, ValueError):
                raise ServiceError(422, "invalid", "Договор не найден: выберите договор из списка.",
                                   "contract_id") from None
            if ctx.group_id and ctx.group_id != c.group_id:
                raise ServiceError(422, "invalid", "Договор не относится к выбранной группе компаний.", "group_id")
            ctx.group_id = c.group_id
            if ctx.program and ctx.program not in {p.name for p in c.programs}:
                raise ServiceError(422, "invalid", "В договоре нет такой программы: выберите из списка.",
                                   "program")
        elif ctx.group_id:
            if ctx.group_id not in {g.id for g in self.kit.groups()}:
                raise ServiceError(422, "invalid", "Группа компаний не найдена.", "group_id")
            # Программа в контексте ГК допустима, только если она есть хотя бы у одного договора группы;
            # на договоры без неё движок её не переносит (engine.program_for, ADR 0005).
            if ctx.program and not any(ctx.program in {p.name for p in c.programs}
                                       for c in self.kit.contracts(ctx.group_id)):
                raise ServiceError(422, "invalid", "В договорах группы нет такой программы.", "program")
        else:
            raise ServiceError(422, "required", "Выберите группу компаний или договор.", "context")
        return ctx

    def _context_title(self, ctx: Any) -> str:
        if ctx.contract_id:
            c = self.kit.contract(ctx.contract_id)
            title = c.legal_entity + (f" · договор № {c.number}" if c.number else "")
        else:
            names = {g.id: g.name for g in self.kit.groups()}
            title = names.get(ctx.group_id, ctx.group_id)
        if ctx.program:
            title += f" · {ctx.program}"
        if ctx.date:
            title += f" · на {ctx.date.strftime('%d.%m.%Y')}"
        return title

    def create_chat(self, body: dict) -> dict:
        ctx = self._validate_context(body.get("context"))
        title = _text(body, "title", required=False, limit=MAX_TITLE_CHARS, label="Название")
        chat = self.app.create_chat(ctx, title or self._context_title(ctx))
        return _dump(chat)

    def chats(self) -> list[dict]:
        return [_dump(c) for c in self.app.chats()]

    def _chat(self, chat_id: str) -> Any:
        try:
            return self.app.chat(chat_id)
        except KeyError:
            raise _err_not_found("Чат") from None

    def chat(self, chat_id: str) -> dict:
        chat = self._chat(chat_id)
        return {**_dump(chat), "requests": [_dump(r) for r in self.app.requests(chat_id, "all")]}

    def create_request(self, chat_id: str, body: dict) -> dict:
        self._chat(chat_id)
        title = _text(body, "title", required=False, limit=MAX_TITLE_CHARS, label="Название запроса")
        n = len(self.app.requests(chat_id, "all")) + 1
        return _dump(self.app.create_request(chat_id, title or f"Запрос {n}"))

    def requests(self, chat_id: str, status: str) -> list[dict]:
        self._chat(chat_id)
        if status not in ("open", "closed", "all"):
            raise ServiceError(422, "invalid", "Статус: open, closed или all.", "status")
        return [_dump(r) for r in self.app.requests(chat_id, status)]

    def _request(self, request_id: str) -> Any:
        try:
            return self.app.request(request_id)
        except KeyError:
            raise _err_not_found("Запрос") from None

    def request(self, request_id: str) -> dict:
        return _dump(self._request(request_id))

    def _open_request(self, request_id: str) -> Any:
        from dms_core.constants import RequestStatus

        req = self._request(request_id)
        if req.status != RequestStatus.open:
            raise ServiceError(409, "request_closed", "Запрос закрыт. Верните его в работу, чтобы продолжить.")
        return req

    # ---------------------------------------------------------------- вопрос → ответ

    def prepare_message(self, request_id: str, body: dict) -> tuple[Any, Any, str]:
        """Проверки до тяжёлого расчёта (их ошибки уходят обычным JSON даже для потока SSE)."""
        req = self._open_request(request_id)
        text = _text(body, "text", required=True, limit=MAX_QUESTION_CHARS, label="Вопрос")
        chat = self._chat(req.chat_id)
        return req, chat, text

    def answer_message(self, req: Any, chat: Any, text: str,
                       on_stage: Callable[[str], None] | None = None) -> dict:
        """Синхронно (вызывается в потоке): сообщение пользователя → engine.answer → сообщение ассистента.
        Оба сообщения дописываются в базу здесь же: обрыв соединения не теряет ответ."""
        from dms_core.constants import AnswerMode
        from dms_core.schemas import Message

        stage = on_stage or (lambda _s: None)
        now = dt.datetime.now(dt.UTC)
        self.app.add_message(req.id, Message(role="user", text=text, created_at=now))
        t0 = time.perf_counter()
        llm = None
        try:
            llm = self.get_llm()
        except Exception as e:  # noqa: BLE001 — неверная настройка провайдера: работаем без модели
            log.warning("llm: не создана (%s)", type(e).__name__)
        answer = self.demo_saved(text, chat.context) if llm is None else None
        slots: dict[str, str] = {}
        if answer is None:
            # стадии сообщает сам движок (engine.answer on_stage): route, clarify, contract:cNN, verify:cNN, compose
            trace: dict = {}
            answer = self._answer_safely(text, chat.context, llm, stage, trace)
            slots = {k: str(v) for k, v in (trace.get("slots") or {}).items() if v}
        else:
            stage("demo_saved")
        if answer.processing_ms is None:
            answer.processing_ms = int(1000 * (time.perf_counter() - t0))
        msg = Message(role="assistant", answer=answer, created_at=dt.datetime.now(dt.UTC))
        self.app.add_message(req.id, msg)
        with self._locked(req.id):
            # перечитать под блокировкой: пока считался ответ, менеджер мог сделать шаг автомата или оценить запрос
            fresh = self.app.request(req.id)
            changed = False
            picks = [k for k in fresh.slots if k.startswith(CLINIC_PICK_SLOT)]
            if picks:
                # новый вопрос — выбор клиники по прошлому ответу больше не действует (ADR 0010 п. 5)
                fresh.slots = {k: v for k, v in fresh.slots.items() if not k.startswith(CLINIC_PICK_SLOT)}
                changed = True
            if fresh.scenario is None:
                fresh.scenario = answer.scenario
                changed = True
            if answer.mode != AnswerMode.demo_saved and slots:
                # факты обращения копятся по запросу: новое значение слота заменяет старое (уточнение менеджера)
                fresh.slots = {**fresh.slots, **slots}
                changed = True
            if changed:
                self.app.save_request(fresh)
        log.info("answer request=%s mode=%s scenario=%s ms=%s", req.id, answer.mode.value
                 if isinstance(answer.mode, AnswerMode) else answer.mode, answer.scenario.value,
                 answer.processing_ms)
        return {"message": _dump(msg), "answer": _dump(answer)}

    def _answer_safely(self, text: str, context: Any, llm: Any,
                       on_stage: Callable[[str], None] | None = None, trace: dict | None = None,
                       clinic_picks: dict[str, str] | None = None) -> Any:
        """engine.answer с моделью; сбой модели/агента → extractive (без модели) с понятной пометкой.
        clinic_picks — выбор клиники менеджером по договорам (ADR 0010), и в повторе без модели тоже."""
        from dms_core.llm.base import LlmError

        try:
            return self._engine_answer(text, context, llm, on_stage, trace, clinic_picks)
        except KeyError:
            raise ServiceError(422, "contract_not_loaded", "Договор контекста не загружен: выполните импорт "
                                                           "комплекта и создайте чат заново.") from None
        except LlmError as e:
            failure: Exception = e
        except Exception as e:
            if llm is None:
                raise
            failure = e
        log.warning("answer: сбой с моделью (%s) — режим без модели", type(failure).__name__)
        out = self._engine_answer(text, context, None, None, trace, clinic_picks)
        out.limitations = [ERR_MODEL, *out.limitations]
        return out

    # ---------------------------------------------------------------- выбор клиники менеджером (ADR 0010)

    def choose_clinic(self, request_id: str, body: dict) -> dict:
        """Менеджер выбрал строку перечня или «нет в перечне программы» → пересчёт последнего вопроса запроса с
        engine.answer(..., clinic_picks) (CONTRACTS §8.1). Проверки: договор — из контекста чата (иначе 422), в
        последнем ответе ассистента есть выбор по этому договору (иначе 409 no_clinic_choice), строка — из перечня
        программы этого выбора в этом договоре (иначе 422 clinic_row_unknown: чужой договор или программа). Выборы
        по другим договорам того же ответа (request.slots) сохраняются; demo_saved не применяется. Синхронно, под
        блокировкой запроса: два клика подряд не пересчитывают один ответ параллельно."""
        from dms_core.constants import CLINIC_PICK_NONE
        from dms_core.schemas import Message

        t = _pick_texts()
        cid = _text(body, "contract_id", required=True, limit=64, label="Договор")
        choice = _text(body, "choice", required=True, limit=MAX_ROW_ID_CHARS, label="Клиника")
        assert cid is not None and choice is not None
        with self._locked(request_id):
            req = self._open_request(request_id)
            chat = self._chat(req.chat_id)
            if cid not in self._chat_contracts(req.chat_id):
                raise ServiceError(422, "contract_not_in_context", t["error_contract"], "contract_id")
            idx, offer = _clinic_offer(req, cid, t)
            question = _pick_question(req, idx, t)
            row = None
            if choice != CLINIC_PICK_NONE:
                # только строка перечня программы выбора в ЭТОМ договоре (ADR 0005): id другого договора не найдётся
                row = next((r for r in self.kit.clinics(cid, program=offer.program) if r.id == choice), None)
                if row is None:
                    raise ServiceError(422, "clinic_row_unknown", t["error_row_unknown"], "choice")
            offered = {c.contract_id for c in req.messages[idx].answer.clinic_choices}
            picks = {k[len(CLINIC_PICK_SLOT):]: v for k, v in req.slots.items() if k.startswith(CLINIC_PICK_SLOT)}
            picks = {c: v for c, v in picks.items() if c in offered}
            picks[cid] = choice
            t0 = time.perf_counter()
            llm = None
            try:
                llm = self.get_llm()
            except Exception as e:  # noqa: BLE001 — неверная настройка провайдера: работаем без модели
                log.warning("llm: не создана (%s)", type(e).__name__)
            answer = self._answer_safely(question, chat.context, llm, None, None, picks)
            if answer.processing_ms is None:
                answer.processing_ms = int(1000 * (time.perf_counter() - t0))
            if row is None:
                said = t["user_pick_none"]
            elif row.address:
                said = t["user_pick_row"].format(name=row.clinic_name, address=row.address)
            else:
                said = t["user_pick_row_no_address"].format(name=row.clinic_name)
            self.app.add_message(req.id, Message(role="user", text=said, created_at=dt.datetime.now(dt.UTC)))
            msg = Message(role="assistant", answer=answer, created_at=dt.datetime.now(dt.UTC))
            self.app.add_message(req.id, msg)
            fresh = self.app.request(req.id)
            fresh.slots = {**fresh.slots, f"{CLINIC_PICK_SLOT}{cid}": choice}
            self.app.save_request(fresh)
        log.info("clinic request=%s contract=%s choice=%s mode=%s", req.id, cid, "none" if row is None else "row",
                 getattr(answer.mode, "value", answer.mode))
        return {"message": _dump(msg), "answer": _dump(answer)}

    def clinic_candidates(self, request_id: str, contract_id: str | None, q: str | None,
                          offset: str | int | None) -> dict:
        """Поиск по перечню программы выбора клиники последнего ответа (CONTRACTS §8 дополнение w7): строки
        перечня программы этого договора (не кандидаты — весь перечень), подходящие к запросу по названию, юрлицу,
        городу, адресу (tools/clinics.search_clinics), страницами по CLINIC_SEARCH_PAGE → {rows, total, offset}.
        Договор не из контекста чата — 422 contract_not_in_context; в последнем ответе нет выбора по договору —
        409 no_clinic_choice. Только чтение: без HeavyGate и без блокировки запроса."""
        from dms_core.constants import CLINIC_SEARCH_PAGE
        from dms_core.tools.clinics import search_clinics

        t = _pick_texts()
        cid = _text({"contract_id": contract_id}, "contract_id", required=True, limit=64, label="Договор")
        query = _text({"q": q}, "q", required=False, limit=MAX_SEARCH_CHARS, label="Поиск") or ""
        try:
            start = int(offset or 0)
        except (TypeError, ValueError):
            raise ServiceError(422, "invalid", t["error_offset"], "offset") from None
        if start < 0:
            raise ServiceError(422, "invalid", t["error_offset"], "offset")
        req = self._request(request_id)
        if cid not in self._chat_contracts(req.chat_id):
            raise ServiceError(422, "contract_not_in_context", t["error_contract"], "contract_id")
        _idx, offer = _clinic_offer(req, str(cid), t)
        # одна клиника по одному адресу — одна строка; из строк разных видов помощи — вида помощи лучшего кандидата
        # (кандидаты guided упорядочены: строгое совпадение, улица, вид помощи вопроса); total — по уникальным
        care = offer.candidates[0].care_type if offer.candidates else None
        found = search_clinics(self.kit.clinics(str(cid), program=offer.program), query, care=care)
        page = found[start:start + CLINIC_SEARCH_PAGE]
        return {"rows": [_dump(r) for r in page], "total": len(found), "offset": start}

    def demo_saved(self, text: str, context: Any) -> Any:
        """Сохранённый ответ демо-примера (data/app/demo_saved/<id>.json, формируется на живой модели): только
        если вопрос совпал с примером дословно (без регистра и знаков) и контекст чата — тот же договор/группа.
        Иначе None — дальше extractive. Чужой контекст не подставляется (ADR 0005)."""
        from dms_core.constants import AnswerMode
        from dms_core.schemas import Answer

        q = _norm_q(text)
        for ex in self._examples_raw():
            if _norm_q(ex.get("question", "")) != q:
                continue
            ectx = ex.get("context") or {}
            same = (ectx.get("contract_id") or None) == context.contract_id and (
                context.contract_id or (ectx.get("group_id") or None) == context.group_id)
            path = self.data_dir / "app" / "demo_saved" / f"{ex.get('id')}.json"
            if not same or not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", str(ex.get("id"))) or not path.is_file():
                continue
            try:
                ans = Answer.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError):
                log.warning("demo_saved %s: файл не читается", ex.get("id"))
                return None
            ans.mode = AnswerMode.demo_saved
            note = "Сохранённый ответ демо-примера (модель сейчас не подключена)."
            if note not in ans.limitations:
                ans.limitations = [note, *ans.limitations]
            if ans.reply is None:
                # сохранённый до ADR 0014 ответ — ответ словами собирается кодом из тех же утверждений
                from dms_core.compose import chat_reply

                with contextlib.suppress(Exception):
                    ans.reply = chat_reply(ans, question=text)
            return ans
        return None

    # ---------------------------------------------------------------- примеры

    def _examples_raw(self) -> list[dict]:
        path = self.data_dir / "derived" / "gold" / "k10_k19.jsonl"
        if not path.is_file():
            return []
        out = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
        except (OSError, ValueError):
            log.warning("examples: файл примеров не читается")
            return []
        return out

    def examples(self) -> list[dict]:
        """Примеры для блока «Примеры» нового чата: id, короткий заголовок, вопрос, контекст. Эталонных ответов,
        ссылок и проверок здесь нет — только то, что менеджер мог бы спросить сам."""
        out = []
        for ex in self._examples_raw():
            q = str(ex.get("question") or "").strip()
            if not q or not ex.get("id"):
                continue
            ctx = ex.get("context") or {}
            out.append({"id": str(ex["id"]), "title": _short_title(q), "question": q,
                        "context": {k: ctx.get(k) for k in ("group_id", "contract_id", "program", "date")}})
        return out

    # ---------------------------------------------------------------- автомат и черновики

    def _add_clinic(self) -> Any:
        if self.add_clinic_mod is not None:
            return self.add_clinic_mod
        try:
            from dms_core.scenarios import add_clinic
        except ImportError:
            raise ServiceError(501, "not_available", "Автомат добавления клиники ещё не подключён.") from None
        return add_clinic

    @staticmethod
    def _last_answer(req: Any) -> Any:
        for m in reversed(req.messages):
            if m.role == "assistant" and m.answer is not None:
                return m.answer
        raise ServiceError(409, "no_answer", "Сначала задайте вопрос ассистенту: автомат запускается по ответу.")

    def start_add_clinic(self, request_id: str, body: dict) -> dict:
        clinic = _text(body, "clinic", required=False, limit=MAX_TITLE_CHARS, label="Клиника")
        with self._locked(request_id):
            return self._start_add_clinic(self._open_request(request_id), clinic)

    def _start_add_clinic(self, req: Any, clinic: str | None) -> dict:
        if req.automation is not None:
            # повторный запуск с той же клиникой — без дублей (идемпотентность CONTRACTS §8)
            same = (req.automation.clinic or None) == clinic
            if same and req.automation.state.value != "insufficient":
                return _dump(req)
            if not same and req.automation.state.value != "insufficient":
                raise ServiceError(409, "automation_running", "Автомат уже запущен для другой клиники. Закройте "
                                                              "запрос и создайте новый.")
            # из «недостаточно данных» — перезапуск: факты могли появиться в следующем вопросе
            req.automation = None
        mod = self._add_clinic()
        answer = self._last_answer(req)
        try:
            req.automation = mod.start(req, answer, clinic=clinic, store=self.kit)
        except ValueError:
            raise ServiceError(422, "automation_invalid", "Автомат нельзя запустить по этому ответу.") from None
        self.app.save_request(req)
        return _dump(self.app.request(req.id))

    def automation_step(self, request_id: str, body: dict) -> dict:
        action = _text(body, "action", required=True, limit=64, label="Действие")
        reply = _text(body, "insurer_reply", required=False, limit=MAX_REPLY_CHARS, label="Ответ СК")
        with self._locked(request_id):
            return self._automation_step(self._open_request(request_id), action, reply)

    def _automation_step(self, req: Any, action: str | None, reply: str | None) -> dict:
        if req.automation is None:
            raise ServiceError(409, "no_automation", "Автомат не запущен: сначала нажмите «Добавить клинику».")
        mod = self._add_clinic()
        try:
            new = mod.step(req, action, insurer_reply=reply, store=self.kit)
        except ValueError:
            raise ServiceError(422, "bad_transition", "Это действие сейчас недоступно: обновите страницу и "
                                                      "выберите один из предложенных шагов.", "action") from None
        self.app.save_request(new)
        log.info("automation request=%s action=%s state=%s", req.id, action, new.automation.state.value
                 if new.automation else None)
        return _dump(self.app.request(req.id))

    def _draft(self, draft_id: str) -> tuple[Any, Any]:
        try:
            return self.app.draft(draft_id)
        except KeyError:
            raise _err_not_found("Черновик") from None

    def edit_draft(self, draft_id: str, body: dict) -> dict:
        """Правка черновика менеджером: запрос должен быть открыт (409); итоговый текст (тема + тело) проходит
        фильтр config/forbidden_promises.yaml — найдено обещание → 422 с перечнем фраз, черновик не меняется."""
        from dms_core.constants import RequestStatus

        req, _ = self._draft(draft_id)
        with self._locked(req.id):
            req, draft = self._draft(draft_id)              # перечитать под блокировкой запроса
            if req.status != RequestStatus.open:
                raise ServiceError(409, "request_closed", "Запрос закрыт: черновик не изменить. Верните запрос "
                                                          "в работу, чтобы править письмо.")
            return self._edit_draft(draft, body)

    def _edit_draft(self, draft: Any, body: dict) -> dict:
        from dms_core.promises import find_promises
        from dms_core.scenarios.add_clinic import template_of

        subject = _text(body, "subject", required=False, limit=MAX_TITLE_CHARS, label="Тема")
        text = body.get("body")
        if text is not None and not isinstance(text, str):
            raise ServiceError(422, "invalid", "Текст письма должен быть текстом.", "body")
        if text is not None and len(text) > MAX_DRAFT_CHARS:
            raise ServiceError(422, "too_long", f"Текст письма: не больше {MAX_DRAFT_CHARS} знаков.", "body")
        if subject is None and text is None:
            raise ServiceError(422, "required", "Нечего сохранять: измените тему или текст.", "body")
        # Письмо уходит от брокера: обещаний (сроки, гарантии, «будет добавлена») в нём быть не должно и после
        # правки. allow_in — по шаблону этого черновика («клиника добавлена» — только после подтверждения СК).
        new_subject = subject if subject is not None else draft.subject
        new_body = text if text is not None else draft.body
        hits = find_promises(new_subject + "\n" + new_body, template_of(draft))
        if hits:
            phrases = ", ".join("«" + h + "»" for h in dict.fromkeys(hits))
            raise ServiceError(422, "promise", "Не сохранено: в письме есть обещание — " + phrases + ". Перепишите "
                                               "без сроков, гарантий и «будет добавлена/согласовано».", "body")
        if subject is not None:
            draft.subject = subject
        if text is not None:
            draft.body = text
        draft.edited_at = dt.datetime.now(dt.UTC)
        self.app.save_draft(draft)
        return _dump(draft)

    # ---------------------------------------------------------------- закрыть / вернуть / оценить

    def close(self, request_id: str) -> dict:
        from dms_core.constants import RequestStatus

        with self._locked(request_id):
            req = self._request(request_id)
            req.status = RequestStatus.closed
            self.app.save_request(req)
        return _dump(req)

    def reopen(self, request_id: str) -> dict:
        from dms_core.constants import RequestStatus

        with self._locked(request_id):
            req = self._request(request_id)
            req.status = RequestStatus.open
            self.app.save_request(req)
        return _dump(req)

    def rate(self, request_id: str, body: dict) -> dict:
        rating = body.get("rating")
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ServiceError(422, "invalid", "Оценка — целое число от 1 до 5.", "rating")
        comment = _text(body, "comment", required=False, limit=MAX_COMMENT_CHARS, label="Комментарий")
        with self._locked(request_id):
            req = self._request(request_id)
            req.rating = rating
            req.rating_comment = comment
            self.app.save_request(req)
        return _dump(req)


def _pick_texts() -> dict[str, str]:
    """Сообщения выбора клиники (config/texts_ru/clinic_pick.yaml service): ошибки и реплика менеджера."""
    return dict((load_yaml("texts_ru/clinic_pick.yaml") or {}).get("service") or {})


def _clinic_offer(req: Any, cid: str, t: dict[str, str]) -> tuple[int, Any]:
    """(номер последнего сообщения ассистента, ClinicChoice по договору cid) или 409 no_clinic_choice: выбор
    относится только к последнему ответу, по более старому ответу клиника не пересчитывается."""
    for i in range(len(req.messages) - 1, -1, -1):
        m = req.messages[i]
        if m.role != "assistant":
            continue
        offer = next((c for c in (m.answer.clinic_choices if m.answer else []) if c.contract_id == cid), None)
        if offer is None:
            break
        return i, offer
    raise ServiceError(409, "no_clinic_choice", t["error_no_choice"], "contract_id")


def _pick_question(req: Any, idx: int, t: dict[str, str]) -> str:
    """Вопрос, на который отвечает сообщение ассистента idx: предыдущее сообщение пользователя. Если сам ответ —
    пересчёт по выбору клиники (в нём учтён выбор), то предыдущая реплика — «Клиника: …», и вопрос ищется перед
    исходным ответом: цепочка выборов пересчитывает один и тот же вопрос менеджера."""
    i = idx
    while True:
        j = next((k for k in range(i - 1, -1, -1) if req.messages[k].role == "user"), None)
        if j is None:
            raise ServiceError(409, "no_clinic_choice", t["error_no_choice"], "contract_id")
        ans = req.messages[i].answer
        recomputed = ans is not None and any(c.selected for c in ans.clinic_choices)
        prev = next((k for k in range(j - 1, -1, -1) if req.messages[k].role == "assistant"), None)
        if not recomputed or prev is None:
            return str(req.messages[j].text or "")
        i = prev


def _short_title(question: str, limit: int = 60) -> str:
    """Короткий заголовок примера: начало вопроса по границе слова."""
    q = " ".join(question.split())
    if len(q) <= limit:
        return q
    cut = q[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:—- ") + "…"


def _safe_report(report: dict) -> dict:
    """Отчёт импорта для ответа API: только статусы и счётчики по договорам и ГК (без путей и текстов ошибок)."""
    contracts = {cid: {k: e.get(k) for k in ("group_id", "status", "insurer", "clauses", "clinics", "programs")
                       if k in e} | {"documents": len(e.get("documents") or []),
                                     "warnings": len(e.get("warnings") or [])}
                 for cid, e in (report.get("contracts") or {}).items()}
    groups = {gid: {"documents": len(g.get("documents") or []), "error": bool(g.get("error"))}
              for gid, g in (report.get("groups") or {}).items()}
    return {"contracts": contracts, "groups": groups, "statuses": report.get("statuses"),
            "clients": report.get("clients") or {}, "seconds": report.get("seconds")}
