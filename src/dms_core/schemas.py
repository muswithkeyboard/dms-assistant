"""Контракты данных (docs/CONTRACTS.md, план §5). Заморожены после волны 0: добавлять можно только новые
необязательные поля. Правит только автор.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .constants import (
    AddClinicState,
    AnswerMode,
    Confidence,
    DocStatus,
    DocType,
    DraftTo,
    Insurer,
    Label,
    RequestStatus,
    Scenario,
    StatementKind,
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# ---------------------------------------------------------------- комплект: группы, договоры, документы

class Group(_Model):
    id: str                      # "gk1"
    name: str                    # "ГК 1"


class Program(_Model):
    name: str
    premium_per_person: float | None = None
    persons: int | None = None
    regions: list[str] = Field(default_factory=list)
    notes: str | None = None


class Payment(_Model):
    n: int
    due: dt.date | None = None
    period: str | None = None
    amount: float | None = None


class Franchise(_Model):
    declared: bool = False                         # франшиза в договоре предусмотрена
    mechanics_clause_ids: list[str] = Field(default_factory=list)


class Contract(_Model):
    """Карточка договора. Одно юрлицо = один договор (ТЗ п. 1, A2)."""
    id: str                      # "c01" … "c13"
    group_id: str
    legal_entity: str            # "Компания 1"
    number: str | None = None
    insurer: Insurer = Insurer.unknown
    date: dt.date | None = None
    period_from: dt.date | None = None
    period_to: dt.date | None = None
    headcount: int | None = None
    programs: list[Program] = Field(default_factory=list)
    payments: list[Payment] = Field(default_factory=list)
    franchise: Franchise = Field(default_factory=Franchise)
    missing_docs: list[str] = Field(default_factory=list)   # «Правила ДМС», «Прил. 1» — не предоставлены


class Document(_Model):
    id: str
    contract_id: str | None = None     # документ договора …
    group_id: str | None = None        # … или общий для ГК (правила, каталог)
    title: str
    doc_type: DocType
    precedence_level: int | None = None   # из config/precedence.yaml
    source_path: str                      # путь относительно data/raw
    original_format: Literal["docx", "xlsx", "pdf", "txt"]
    sha256: str
    uploaded_by: str = "operator"
    uploaded_at: dt.datetime
    status: DocStatus = DocStatus.received
    warnings: list[str] = Field(default_factory=list)


class Clause(_Model):
    """Пункт документа с восстановленным номером (numbering.xml) и путём в структуре."""
    id: str
    doc_id: str
    contract_id: str | None
    number: str | None = None          # "2.6.1"; None — абзац без номера
    path: str = ""                     # "Программа «Москва» › Бизнес › 1.4"
    program: str | None = None
    text: str
    footnotes: list[str] = Field(default_factory=list)
    page: int | None = None
    text_hash: str = ""


class ClinicRow(_Model):
    """Строка перечня клиник договора: программа × вид помощи × клиника × адрес."""
    id: str
    contract_id: str
    doc_id: str
    program: str
    care_type: str
    clinic_name: str
    legal_name: str | None = None
    address: str | None = None
    city: str | None = None
    code: str | None = None
    # None — в документе не указано; "—" — не применяется (это не 0 %); число — процент
    franchise_pct: float | Literal["—"] | None = None
    by_referral: bool = False
    access_note: str | None = None
    source_ref: str                    # строка таблицы / ячейка: "t11:r1234" или "Лист1!F57"


class CareMatrix(_Model):
    contract_id: str
    program: str
    care_type: str
    present: bool
    source_refs: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- ответ

class Citation(_Model):
    doc_id: str
    doc_title: str
    contract_id: str | None
    clause_number: str | None = None
    table_ref: str | None = None
    page: int | None = None
    quote: str
    verified: bool = False
    offsets: tuple[int, int] | None = None
    ref: str | None = None                       # ссылка, по которой проверена (DraftCitation.ref; ADR 0014)


class Statement(_Model):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    kind: StatementKind = StatementKind.fact


class AnswerContext(_Model):
    group_id: str | None = None
    contract_id: str | None = None
    program: str | None = None
    date: dt.date | None = None


class Alternative(_Model):
    clinic: ClinicRow
    reason: str


class ClinicChoice(_Model):
    """Подтверждение клиники менеджером (ADR 0010): клиника из вопроса не угадывается. Кандидаты — строки перечня
    программы одного договора (нестрогий подбор tools/clinics); метки «можно» и «нет в перечне» — только после
    выбора менеджера (правила covered / clinic_absent с clinic_confirmed: yes в config/verdict_rules.yaml)."""
    contract_id: str
    program: str
    asked: str                                   # название клиники из вопроса, как его выделил разбор
    candidates: list[ClinicRow] = Field(default_factory=list)   # ≤ CLINIC_CHOICE_MAX, лучшие первыми
    selected: str | None = None                  # id строки ClinicRow | "none" (нет в перечне) | None — не выбрано


class ReplyPoint(_Model):
    """Пункт ответа в чате: что сказано в документе — словами (текст кода + точная цитата), со сноской на источник."""
    text: str                                    # формулировка кода (шаблон texts_ru/reply.yaml), без кодов строк
    quote: str | None = None                     # точная цитата из документа (≤ REPLY_QUOTE_CHARS, проверена verify)
    refs: list[str] = Field(default_factory=list)   # Citation.ref (сноски [n] — по порядку первого упоминания)
    contract_id: str | None = None               # ГК: к какому договору пункт


class ChatReply(_Model):
    """Ответ ассистента в виде сообщения чата (ADR 0014): короткий ответ, что сказано в договоре, что уточнить.
    Собирается КОДОМ из проверенных утверждений и цитат (compose.reply); знаний модели нет (`docs/rules.md` п. 2)."""
    lead: str                                    # 1–2 фразы: прямой ответ на вопрос («Да, входит…», «Зависит от…»)
    points: list[ReplyPoint] = Field(default_factory=list)   # «Что сказано в договоре» — ≤ REPLY_POINTS_MAX
    ask: list[str] = Field(default_factory=list)  # «Что уточнить» — вопросы к менеджеру/сотруднику, без дублей
    caution: str | None = None                   # одна строка: «Ответ — по документам; решение СК …», допущения


class PerContract(_Model):
    contract_id: str
    summary: str
    label: Label | None = None
    statements: list[Statement] = Field(default_factory=list)


class LlmInfo(_Model):
    provider: str
    model: str
    prompt_version: str
    tool_calls: int = 0


class Answer(_Model):
    scenario: Scenario
    context: AnswerContext
    summary: str
    label: Label | None = None
    label_note: str | None = None          # «есть альтернатива», «нужно подтверждение СК»
    confidence: Confidence
    per_contract: list[PerContract] = Field(default_factory=list)
    statements: list[Statement] = Field(default_factory=list)
    comparisons: list[Statement] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    clinic_choices: list[ClinicChoice] = Field(default_factory=list)   # по договору; пусто — выбор не нужен
    reply: ChatReply | None = None               # ответ в чате обычным текстом (ADR 0014); None — старый вид
    suggested_automation: str | None = None
    mode: AnswerMode = AnswerMode.live
    llm: LlmInfo | None = None
    processing_ms: int | None = None


# ---------------------------------------------------------------- чаты, запросы, черновики

class ChatContext(_Model):
    group_id: str | None = None
    contract_id: str | None = None
    program: str | None = None
    date: dt.date | None = None


class Chat(_Model):
    """Контекст фиксирован при создании (ТЗ п. 9, ADR 0006)."""
    id: str
    title: str
    context: ChatContext
    created_at: dt.datetime


class Draft(_Model):
    id: str
    to: DraftTo
    stage: AddClinicState
    subject: str
    body: str
    fields: dict[str, str] = Field(default_factory=dict)
    unknown_fields: list[str] = Field(default_factory=list)   # поля «[ЗАПОЛНИТЬ]»
    created_at: dt.datetime
    edited_at: dt.datetime | None = None
    auto_send: Literal[False] = False                         # письма не отправляются никогда


class Automation(_Model):
    kind: Literal["add_clinic"] = "add_clinic"
    state: AddClinicState
    clinic: str | None = None
    history: list[dict] = Field(default_factory=list)


class Message(_Model):
    role: Literal["user", "assistant"]
    text: str | None = None
    answer: Answer | None = None
    created_at: dt.datetime


class Request(_Model):
    id: str
    chat_id: str
    title: str
    scenario: Scenario | None = None
    status: RequestStatus = RequestStatus.open
    rating: int | None = Field(default=None, ge=1, le=5)
    rating_comment: str | None = None
    slots: dict[str, str] = Field(default_factory=dict)
    automation: Automation | None = None
    drafts: list[Draft] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)


# ---------------------------------------------------------------- волна 2: черновик ответа агента (CONTRACTS §7)
# Модель собирает проверки и утверждения со ссылками на то, что вернули инструменты; метку SC 01 ставит код
# по config/verdict_rules.yaml (имена проверок совпадают с ключами правил), ссылки проверяет citations.verify.

Check3 = Literal["yes", "absent", "not_found"]


class DraftCitation(_Model):
    ref: str            # clause_id пункта, id строки клиник или source_ref матрицы — ровно как вернул инструмент
    quote: str = ""     # дословный фрагмент (для пунктов обязателен; для строк клиник — название клиники)


class DraftStatement(_Model):
    text: str
    kind: StatementKind = StatementKind.fact
    citations: list[DraftCitation] = Field(default_factory=list)
    # Какие проверки Checks подтверждает утверждение (exclusion, care_in_program, clinic_in_program, conditions,
    # documents). Проверка yes/absent без живого подтверждающего утверждения → not_found (ревизия w2, дефект 1).
    supports: list[str] = Field(default_factory=list)


class Checks(_Model):
    """Проверки SC 01 (ТЗ п. 5.1). not_found — «не нашли», это не «нет в документах» (`docs/rules.md` п. 3а)."""
    exclusion: Check3 | None = None            # yes — услуга в исключениях; absent — исключения просмотрены, её нет
    care_in_program: Check3 | None = None      # вид помощи / услуга входит в программу
    clinic_in_program: Check3 | None = None    # клиника есть в перечне программы для этого вида помощи
    conditions: list[str] = Field(default_factory=list)   # franchise | referral | approval | zone | limit | other
    documents: Literal["consistent", "contradictory"] = "consistent"


class DraftAlternative(_Model):
    row_id: str         # id строки ClinicRow из clinics_query
    reason: str


class DraftContradiction(_Model):
    a: DraftCitation
    b: DraftCitation
    note: str = ""


class DraftAnswer(_Model):
    contract_id: str
    summary: str
    checks: Checks | None = None
    statements: list[DraftStatement] = Field(default_factory=list)
    alternatives: list[DraftAlternative] = Field(default_factory=list)
    contradictions: list[DraftContradiction] = Field(default_factory=list)
    looked_at: list[str] = Field(default_factory=list)    # что просмотрено (для «нет в документах»)
    missing: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    clinic_choice: ClinicChoice | None = None            # guided: кандидаты клиники и учтённый выбор (ADR 0010)


# ---------------------------------------------------------------- подключение клиента (ADR 0011)

class ClientContract(_Model):
    """Договор, подключённый клиентом через интерфейс. id — "uNN" (не пересекается с cNN комплекта)."""
    id: str
    group_id: str                                 # "ugN"
    legal_entity: str
    insurer_hint: Insurer | None = None           # выбор клиента; None — определить по документам
    number: str | None = None
    created_at: dt.datetime
    insurer_name: str | None = None               # название СК свободным текстом (для неизвестных СК; ADR 0012)
    force_generic: bool = False                   # оператор: «не распознавать формат» — разбор запасным адаптером


class ClientFile(_Model):
    name: str                                     # имя файла в папке договора (без пути)
    doc_type: str                                 # по config/doc_types.yaml или выбор клиента
    scope: Literal["contract", "group"] = "contract"
    fmt: str
    size: int
    sha256: str
    uploaded_at: dt.datetime


class ClientGroup(_Model):
    id: str
    name: str
    created_at: dt.datetime
    contracts: list[ClientContract] = Field(default_factory=list)


class OnboardingReport(_Model):
    """Отчёт о готовности договора после обработки (ТЗ п. 7.1; без текстов документов)."""
    contract_id: str
    insurer: Insurer
    adapter: str                                  # zetta | renaissance | sogaz | generic
    readiness: Literal["ready", "limited", "failed"]   # limited — нет перечня клиник или матрицы видов помощи
    documents: list[dict[str, Any]] = Field(default_factory=list)   # {doc_id, title, doc_type, status, warnings}
    counts: dict[str, int] = Field(default_factory=dict)            # clauses, numbered, programs, clinics, care_rows
    warnings: list[str] = Field(default_factory=list)
    selfcheck: dict[str, int] = Field(default_factory=dict)         # asked, cited (самопроверка по программам)


class OnboardingJob(_Model):
    id: str
    contract_id: str
    state: Literal["queued", "running", "done", "error"]
    stage: str | None = None                      # received | extracted | checked | indexed | selfcheck
    created_at: dt.datetime
    finished_at: dt.datetime | None = None
    report: OnboardingReport | None = None
    error: str | None = None                      # по-русски, без текстов документов


# ---------------------------------------------------------------- разметка таблиц оператором (ADR 0012)

ColumnRole = Literal["clinic_name", "legal_name", "address", "city", "care_type", "program", "franchise_pct",
                     "code", "by_referral", "access_note", "present", "ignore"]


class TableRef(_Model):
    """Таблица в документе договора: docx — номер таблицы в документе (с 0), xlsx — имя листа."""
    doc_id: str
    kind: Literal["docx_table", "xlsx_sheet"]
    index: int | None = None
    sheet: str | None = None


class TableMapping(_Model):
    """Разметка таблицы перечня клиник: строка заголовка, роли колонок, постоянные значения для всей таблицы."""
    role: Literal["clinic_list", "care_matrix"] = "clinic_list"
    header_row: int | None = 0                    # строка заголовка (с 0); данные — ниже; None — заголовка нет
    columns: dict[int, ColumnRole]                # номер колонки (с 0) → роль; clinic_list: clinic_name обязателен;
                                                  # care_matrix long: care_type обязателен (+ program, present)
    program: str | None = None                    # программа для всей таблицы, если колонки программы нет
    care_type: str | None = None                  # вид помощи для всей таблицы, если колонки нет
    program_from_title: bool = False              # программа — строка-название над данными (одна непустая ячейка,
                                                  # первая строка таблицы): «ВИП», «Барнаул» (перечни по программам)
    # --- разметка матрицы «программа × вид помощи» (ADR 0013)
    derive_care: bool = False                     # clinic_list: виды помощи строк перечня → матрица (present=True)
    matrix_layout: Literal["wide", "long"] | None = None   # care_matrix: wide — строки = виды помощи, колонки =
                                                  # программы; long — колонки program / care_type / present
    program_cols: dict[int, str] = Field(default_factory=dict)   # wide: колонка → программа (по умолчанию —
                                                  # текст ячейки заголовка)


class TablePreview(_Model):
    ref: TableRef
    doc_title: str
    rows: list[list[str]] = Field(default_factory=list)   # первые TABLE_PREVIEW_ROWS строк (текст ячеек)
    n_rows: int = 0
    n_cols: int = 0
    signature: str | None = None                  # подпись заголовка (по header_row предложенной/сохранённой разметки)
    mapping: TableMapping | None = None           # действующая разметка договора
    suggested: TableMapping | None = None         # предложение: профиль СК по подписи или угадка по заголовкам
    profile_id: str | None = None                 # профиль СК, давший mapping / suggested


class InsurerProfile(_Model):
    """Сохранённая разметка для СК: применяется к таблице с той же подписью заголовка в следующих договорах."""
    id: str
    insurer_name: str
    signature: str
    mapping: TableMapping
    created_at: dt.datetime
    updated_at: dt.datetime
    used: int = 0                                 # сколько раз применена автоматически
