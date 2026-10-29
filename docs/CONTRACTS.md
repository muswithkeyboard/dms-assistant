# Контракты (заморожены после волны 0, 23.10.2026)

Правит только автор. Можно добавлять новые **необязательные** поля и новые функции; менять смысл,
переименовывать и удалять — только через ADR. Код — `src/dms_core/schemas.py`, `constants.py`.

## 1. Данные (план §5) — `schemas.py`

`Group`, `Contract` (карточка: программы, премии, численность, сроки, график взносов, франшиза, `missing_docs`),
`Document`, `Clause`, `ClinicRow`, `CareMatrix`, `Citation`, `Statement`, `Answer`, `Chat`, `Request`, `Draft`.
Перечисления — `constants.py`: `DocType`, `DocStatus` (ТЗ п. 7.1), `Insurer`, `Scenario`, `Label` (ТЗ п. 5.1),
`Confidence` (ТЗ п. 8.1), `AddClinicState` (ADR 0004).

Идентификаторы (стабильны между прогонами импорта):

| Объект | Формат | Пример |
|---|---|---|
| группа | `gk<N>` по папке «ГК N» | `gk3` |
| договор | `c<MM>` по папке «Компания M» | `c09` |
| документ | `<contract_id>.<doc_type>[.<n>]`; общий для ГК — `<group_id>.<doc_type>[.<n>]` | `c09.program`, `gk1.rules` |
| пункт | `<doc_id>#p<индекс абзаца в теле документа>` | `c09.contract#p143` |
| пункт из таблицы | `<doc_id>#t<таблица>r<строка>`; текст ячейки по строкам — `…c<ячейка>l<строка ячейки>`; абзацы текста условий в ячейке — `…p<абзац>` | `c09.memo#t0r3c1l2`, `c05.program#t4r0p243` |
| строка клиник | `<doc_id>#t<таблица>r<строка>` (docx) или `<doc_id>#<лист>!<строка>` (xlsx); одна строка с несколькими видами помощи → `<source_ref>#<k>` | `c01.contract#t11r1234`, `c10.clinics#Лист1!57#2` |

Правила, которые проверяются тестами:
- у каждого `Clause` и `ClinicRow` `contract_id` = договор документа; у общих документов ГК (`rules`, `catalog`)
  `contract_id = None`, и в инструментах договора они не участвуют (план §1 п. 4);
- `ClinicRow.franchise_pct`: `None` — не указано, `"—"` — не применяется, число — процент; «—» ≠ 0;
- `Clause.number` — номер, как его показывает Word/LibreOffice (автонумерация из `numbering.xml` + текстовый номер);
  абзац без номера — `None`; `Clause.path` — путь «Документ › раздел › подпрограмма › номер» для ссылок, где номера
  повторяются (программы Ренессанса);
- подпункт, нумерованный внутри пункта («7)»), — `number="3.4.5, подп. 7"`: родитель для сверки ссылок — «3.4.5»;
- `ClinicRow.care_type` — заголовок вида помощи как в документе («Поликлиника | Врач на дом»); `CareMatrix.care_type` —
  отдельные виды помощи; сопоставление — через `config/care_types.yaml` в инструментах (волна 2);
- `Clause.program` — одна программа или перечень через «, » для общего блока нескольких программ
  («Стандарт, Бизнес»); фильтр по программе делит значение по «, » (дополнение 23.10.2026, этап w1-sogaz);
- сноски вклеиваются в `Clause.footnotes` того абзаца, где стоит ссылка; скрытый текст (`w:vanish`) пропускается.

## 2. Чтение файлов — `dms_core/io/` (волна 1, этап docx-reader)

```python
# io/docx_reader.py
@dataclass Para:  index: int; number: str | None; level: int | None; text: str; style: str | None;
                  footnotes: list[str]; in_table: bool = False
@dataclass Cell:  text: str; icons: list[str]          # картинка в ячейке (VML и DrawingML) → "[icon:<sha1 8>]"
                  paras: list[Para] = []; footnotes: list[str] = []   # абзацы ячейки с номерами; сноски из ячейки
@dataclass Table: index: int; rows: list[list[Cell]]; para_before: int   # индекс абзаца перед таблицей
@dataclass DocxDoc: paragraphs: list[Para]; tables: list[Table]; blocks: list[tuple[str, int]]  # ("p"|"t", index) — порядок тела
                  skipped_hidden: list[tuple[int, str]] = []   # пропущенные скрытые абзацы: позиция и метка (для lo_check)
def read_docx(path: Path) -> DocxDoc
def to_text(doc: DocxDoc) -> str                       # «номер\tтекст» построчно — для сверки с LibreOffice
def iter_all_paras(doc: DocxDoc) -> Iterator[Para]     # абзацы тела и ячеек в порядке документа
# ошибки: DocxError (повреждённый docx), io/errors.InputError (сообщение по-русски, для API — 422)
# сверка с LibreOffice: python -m dms_core.io.lo_check → data/derived/numbering_check.json

# io/xlsx_reader.py — openpyxl read-only + защита от zip-бомбы (из Justmedit table.py: _check_xlsx)
def read_xlsx(path: Path, max_unpacked: int = 200 * 2**20) -> dict[str, list[list[str | None]]]   # лист → строки

# io/pdf_reader.py — по образцу Justmedit pdf_text.py (дочерний процесс с таймаутом), без OCR
def read_pdf(path: Path) -> list[str]                 # текст по страницам; нет текстового слоя — InputError
```

## 3. Адаптеры — `dms_core/adapters/` (волна 1, этап adapters)

```python
@dataclass ParsedContract:
    contract: Contract; documents: list[Document]; clauses: list[Clause]
    clinics: list[ClinicRow]; care: list[CareMatrix]; warnings: list[str]
    errors: list[str] = []          # ошибки сверки: документ не получает «ready» (дополнение после ревизии w1)
def detect(kit_contract: KitContract) -> Insurer              # по преамбуле и структуре таблиц, не по имени папки
def parse_contract(kit_contract: KitContract, root: Path) -> ParsedContract   # zetta | renaissance | sogaz | generic
def parse_group_docs(group: KitGroup, root: Path) -> tuple[list[Document], list[Clause]]   # rules, catalog (contract_id=None)
```

## 4. Хранилище и поиск — `dms_core/store/`, `dms_core/index/` (волна 1, этап store-index)

```python
# store/db.py — SQLite в data/derived/kit.sqlite (разбор) ; data/app/app.sqlite (чаты — волна 3)
class KitStore:
    def __init__(self, path: Path)
    def save(self, parsed: ParsedContract) -> None          # идемпотентно: повторный импорт заменяет договор
    def contracts(self) -> list[Contract];  def contract(self, contract_id) -> Contract
    def documents(self, contract_id) -> list[Document]
    def clauses(self, contract_id, doc_types=None) -> list[Clause]
    def clause(self, contract_id, number: str | None = None, path: str | None = None) -> list[Clause]
    def clinics(self, contract_id, **filters) -> list[ClinicRow]
    def care(self, contract_id, program=None) -> list[CareMatrix]
# store/dedup.py — одинаковый текст хранится один раз (text_hash), ссылки остаются на документ своего договора
# index/search.py — BM25 + pymorphy3 по пунктам ОДНОГО договора
def search_clauses(store, contract_id: str, query: str, k: int = 10, program=None, doc_types=None) -> list[tuple[Clause, float]]
```

`contract_id` обязателен во всех функциях чтения; без него — `ValueError` (ADR 0005).

## 5. Импорт комплекта — `dms ingest` / `scripts/ingest_kit.py`

`kit.scan()` → для каждого договора `parse_contract` → `KitStore.save` → статусы документов
`received → extracted → checked → indexed → ready` (ТЗ п. 7.1); ошибка — `error` + текст в `warnings`.
Отчёт — `data/derived/ingest_report.json`: по договору — СК, документы и статусы, число пунктов с номером, клиник,
программ, сумма премий и сверка с итогом договора, предупреждения.

## 6. Инструменты агента (план §6.3) — волна 2; первая версия поверх `KitStore` — волна 1 (для приёмки К10–К19)

Все принимают `contract_id` первым аргументом и возвращают данные с источником (`doc_id` + номер пункта
или `table_ref`).

## 7. Волна 2 — ответы (зафиксировано 24.10.2026)

```python
# llm/base.py (в main): Message, ToolCall, ToolSpec, Usage, LlmTurn, LlmError, LLM(Protocol).chat(...)
# llm/__init__.py: get_llm(provider=None, model=None, base_url=None) -> LLM | None   # None — ключа/провайдера нет
#   провайдеры: anthropic.py, openai_compat.py (OpenAI, YandexGPT, Ollama /v1), gigachat.py, mock.py (кассеты)
#   mock: ответ по ключу sha256(канонический JSON запроса); нет кассеты → LlmError; DMS_LLM_RECORD=1 + живой
#   провайдер под ним — записывает eval/cassettes/<sha>.json (без ключей; тексты документов — вне git:
#   кассеты с текстами комплекта лежат в data/derived/cassettes/, в git — только кассеты на своих фикстурах)
# llm/pricing.py: cost_usd(model, usage) ; llm/prompts.py: load(name) -> (text, version) из config/prompts/

# tools/registry.py
def bind_tools(contract_id: str, store=None) -> tuple[list[ToolSpec], Callable[[str, dict], dict]]
#   ToolSpec без параметра contract_id: договор привязан кодом, модель не может выбрать чужой (ADR 0005).
#   Исполнитель возвращает JSON-совместимый результат инструмента; ошибки — {"error": "..."} по-русски.
#   ref карточки договора — doc_id документа «договор» (c01.contract); ref матрицы — её source_ref.

# agent.py
@dataclass ToolRecord: name: str; args: dict; refs: set[str]      # refs — все clause_id / row_id / source_ref ответа
@dataclass AgentRun:  draft: DraftAnswer; tools: list[ToolRecord]; steps: int; usage: Usage; model: str; error: str|None
                      warnings: list[str] = []; prompt_version: str = ''   # что поправил код (без текстов)
def run_agent(llm, contract_id: str, question: str, *, scenario: Scenario, slots: dict, context: ChatContext,
              store=None, max_steps: int = 8) -> AgentRun        # итог — DraftAnswer по JSON-схеме (schemas.py)

# scenarios/route.py
@dataclass Route: scenario: Scenario; slots: dict[str, str]; questions: list[str]   # нехватка обязательных фактов
def route(question: str, context: ChatContext, llm=None) -> Route    # 1 вызов модели; без модели — правила по словам
# scenarios/clarify.py: missing_facts(scenario, slots, context) -> list[str]  — по config/required_facts.yaml

# citations.py
@dataclass Verified: draft: DraftAnswer; citations: dict[str, Citation]; dropped: list[tuple[str, str]]  # (ref, причина)
def verify(draft: DraftAnswer, run_refs: set[str], store, contract_id: str) -> Verified
#   ссылка жива, только если ref вернул инструмент в этом прогоне (run_refs), он принадлежит договору,
#   и цитата (≥ 25 знаков и 4 слов) найдена в тексте пункта: точно, затем rapidfuzz ≥ 92; строки клиник —
#   по id и названию клиники. Утверждение без живых ссылок выбрасывается. absent в checks допустим только
#   с looked_at и при разобранном документе нужного типа (иначе → not_found).

# scenarios/sc01_refusal.py
def decide_label(checks: Checks, has_alternative: bool) -> tuple[Label, str | None, str]   # (метка, уточнение, id правила)
# scenarios/clinics_pick.py: pick(contract_id, program, care_type, near=None, k=5, store=None) -> list[Alternative]
# precedence.py / conflicts.py: старший документ по config/precedence.yaml; противоречие → обе цитаты (A3)
# promises.py: find_promises(text, template_id=None) -> list[str] по config/forbidden_promises.yaml

# compose.py
def compose(scenario, context: AnswerContext, verified: dict[str, Verified], *, label=None, ...) -> Answer
#   форма ТЗ п. 8; статус ТЗ п. 8.1: contradictory > need_clarification (есть вопросы) > insufficient
#   (нет живых ссылок по сути) > confirmed. Контекст ГК — per_contract по каждому договору, без смешивания ссылок.

# engine.py (автор)
def answer(question: str, context: ChatContext, *, llm=None, store=None, trace: dict | None = None) -> Answer
#   trace (eval): dropped, kept, draft_refs, run_refs, automation_states, per_contract{rule, steps, error} — без текстов
#   route → clarify → для каждого договора контекста run_agent → verify → decide_label → compose.
#   Без модели — mode=extractive: найденные пункты и строки клиник без вывода, confidence=insufficient.
```

## 8. Волна 3 — сервис и интерфейс (зафиксировано 24.10.2026)

```python
# scenarios/drafts.py — черновики писем (ADR 0004, план §6.9); шаблоны config/texts_ru/drafts.yaml
def render(template_id: str, fields: dict[str, str], *, stage: AddClinicState) -> Draft
#   неизвестное поле → «[ЗАПОЛНИТЬ]» и в unknown_fields; итоговый текст проверяет promises.find_promises
#   (с template_id для allow_in); найдено обещание → ValueError (шаблон испорчен) — тест на всех шаблонах.
def draft_fields(answer: Answer, request: Request, store) -> dict[str, str]   # юрлицо, договор, программа, клиника…

# scenarios/add_clinic.py — автомат ТЗ п. 5.2.2 по config/add_clinic.yaml
def start(request: Request, answer: Answer, *, clinic: str | None, store=None) -> Automation
#   начальное состояние: alt_found | not_in_program | insufficient (по answer.alternatives и наличию клиники
#   в перечне программы — инструментом clinics_query, не моделью); черновики начального шага — в request.drafts
def step(request: Request, action: str, *, insurer_reply: str | None = None, store=None) -> Request
#   action — ключ перехода из add_clinic.yaml (hr_wants_add, insurer_request_ready, insurer_free, insurer_paid,
#   hr_agreed, hr_declined, close); недопустимый переход → ValueError; повтор того же шага — без дублей
#   (идемпотентность по request_id + clinic + state); ветку задаёт только action менеджера (ответ СК),
#   insurer_reply сохраняется в history как источник; клиника «добавлена» только в запросе, не в документах.

# dms_api/store.py — SQLite data/app/app.sqlite (вне git): chats, requests, messages, drafts — JSON моделей schemas
class AppStore:
    create_chat(context: ChatContext, title: str | None) -> Chat ; chats() -> list[Chat] ; chat(id) -> Chat
    create_request(chat_id, title) -> Request ; requests(chat_id, status: open|closed|all) -> list[Request]
    request(id) -> Request ; save_request(r: Request) -> None ; draft(id) -> (Request, Draft) ; save_draft(...)
```

HTTP (план §7). Ответы — JSON моделей `schemas.py` (`model_dump(mode="json")`); ошибки — `{"error": {code, message,
field}}` по-русски (ServiceError). Тела ≤ DMS_MAX_JSON_KB, файлы ≤ DMS_MAX_UPLOAD_MB.

| Маршрут | Тело → ответ |
|---|---|
| `GET /api/groups` | → `[{id, name, contracts: [{id, legal_entity, number, insurer, programs: [name]}]}]` |
| `GET /api/contracts/{id}` | → `Contract` + `documents: [Document]` |
| `GET /api/documents?contract_id=&group_id=` · `GET /api/documents/{id}` | → `[Document]` / `Document` |
| `GET /api/documents/{id}/clauses` | → `{clauses: [Clause], total, truncated}` — пункты документа по порядку (оператор) |
| `GET /api/documents/{id}/clause?number=&path=&ref=` | → `{clause: Clause}` или `{clinic: ClinicRow}` (по ref) — для панели «Источники» |
| ↳ параметр `ref` (w16-sec) | точная ссылка цитаты `Citation.ref` (`c08.program#t14r0p77`, с `@vN` или без) → ровно этот пункт; без `ref` и при нескольких разных пунктах с номером `number` — 409 `ambiguous_clause` |
| `POST /api/documents` (multipart: file, contract_id, group_id, doc_type?, uploaded_by) | → `Document` (разбор и статусы ТЗ п. 7.1) |
| `POST /api/documents/import-kit` | → отчёт импорта (`ingest_report`, без текстов) |
| `POST /api/chats` `{context: ChatContext, title?}` · `GET /api/chats` · `GET /api/chats/{id}` | → `Chat` (+ `requests` в GET по id) |
| `POST /api/chats/{id}/requests` `{title}` · `GET /api/chats/{id}/requests?status=open|closed|all` | → `Request` / `[Request]` |
| `POST /api/requests/{id}/messages` `{text}` | → `{message: Message, answer: Answer}`; `Accept: text/event-stream` — поток `event: stage` `{stage, label, contract_id?}` (received, route, clarify, contract, verify, compose) и `event: answer` (FR 17) |
| `POST /api/requests/{id}/automation/add-clinic` `{clinic?}` · `POST …/automation/step` `{action, insurer_reply?}` | → `Request` |
| `GET /api/requests/{id}/clinic-candidates?contract_id=&q=&offset=` | → `{rows: [ClinicRow], total, offset}` — поиск по перечню программы выбора последнего ответа (название, юрлицо, город, адрес; леммы и латиница), по `CLINIC_SEARCH_PAGE`; пустой `q` — весь перечень по порядку; 409 `no_clinic_choice`, 422 `contract_not_in_context` (ADR 0010) |
| `POST /api/requests/{id}/clinic` `{contract_id, choice: row_id \| "none"}` | → `{message: Message, answer: Answer}` — выбор клиники менеджером и пересчёт последнего вопроса (ADR 0010); 409 `no_clinic_choice` — в последнем ответе нет выбора по этому договору; 422 `clinic_row_unknown` — строка не из перечня программы договора |
| `PATCH /api/drafts/{id}` `{subject?, body?}` | → `Draft` (edited_at) |
| `POST /api/requests/{id}/close` · `/reopen` · `/rate` `{rating 1–5, comment?}` | → `Request` |
| `GET /api/examples` | → `[{id, title, question, context}]` — вопросы К10–К19 без эталонных ответов |
| `GET /healthz` · `/version` | служебные (есть) |

Режимы ответа: модель есть → `live`; нет модели и вопрос совпал с демо-примером (`data/app/demo_saved/<id>.json`,
формируется скриптом на живой модели, вне git) → `demo_saved` с пометкой; иначе → `extractive` (engine).

### 8.1. Подтверждение клиники менеджером (ADR 0010, зафиксировано 25.10.2026)

```python
class ClinicChoice(_Model):       # schemas.py
    contract_id: str; program: str; asked: str
    candidates: list[ClinicRow]   # ≤ CLINIC_CHOICE_MAX (constants), лучшие первыми; пусто — кандидатов нет
    selected: str | None          # id строки | CLINIC_PICK_NONE ("none") | None — выбор ещё не сделан
Answer.clinic_choices: list[ClinicChoice]      # по договорам; пусто — клиника не названа или выбор не нужен
DraftAnswer.clinic_choice: ClinicChoice | None # guided заполняет, если клиника названа (SC 01 и SC 02 clinic)

engine.answer(question, context, *, ..., clinic_picks: dict[str, str] | None = None) -> Answer
#   clinic_picks[cid] → slots["clinic_pick"] в guided; decide_label(..., facts={"refusal", "clinic_confirmed"}),
#   clinic_confirmed = выбор передан и draft.clinic_choice.selected == выбор (guided его учёл)
```
guided при `slots["clinic_pick"]`: строка с этим id должна быть из `store.clinics(cid, program=program)`, иначе
выбор не учитывается (`selected=None`, «не нашли»). Строка выбрана → `clinic_in_program=yes` по ней (ссылка —
`ref` строки), если вид помощи вопроса подходит к строке или к другой строке той же клиники и адреса; не подходит —
`not_found` с фразой о видах помощи строки. «none» → `absent` (ссылка на перечень, «нет в перечне программы»),
если перечень программы получен; без перечня — `not_found`. `selected` — выбор, если он учтён.
Сервис: `choose_clinic(request_id, body)` — последнее сообщение ассистента запроса с `clinic_choices` этого
договора, вопрос — предыдущее сообщение пользователя; сообщение пользователя «Клиника: <название, адрес>» или
«Клиники нет в перечне программы», затем ответ; выборы по другим договорам того же ответа сохраняются
(`request.slots["clinic_pick:<cid>"]`), новый вопрос (`POST …/messages`) их сбрасывает. demo_saved не применяется.

Дополнение 25.10.2026 (круг w7): менеджер может выбрать ЛЮБУЮ строку перечня программы — найденную поиском
`GET …/clinic-candidates` («Показать ещё» / поиск по адресу), не только из `candidates`. `decide_label` получает
факт `clinic_named`; правило `clinic_unresolved` (клиника названа, `clinic_in_program` ∈ {absent, not_found}) →
«недостаточно данных» с пометкой «выберите клинику из перечня» вместо «при условиях». Значение условия правила
списком — «любое из».
Выбранная строка, которой нет среди кандидатов (найдена поиском), после учёта выбора идёт первой в
`ClinicChoice.candidates` (всего ≤ CLINIC_CHOICE_MAX) — интерфейс показывает её в «Выбрано» и после перезагрузки.


## 9. Подключение клиента (ADR 0011, зафиксировано 25.10.2026)

```python
# schemas.py — ClientGroup, ClientContract, ClientFile, OnboardingReport, OnboardingJob (см. файл)
# constants.py — CLIENT_CONTRACT_PREFIX "u", CLIENT_GROUP_PREFIX "ug", CLIENT_MAX_FILES, SELFCHECK_MAX

# dms_core/onboarding.py (новый модуль)
def client_root() -> Path                     # data/derived/clients (DMS_DATA_DIR учитывается)
def kit_contract(group: ClientGroup, contract: ClientContract, files: list[ClientFile]) -> KitContract
def kit_group(group: ClientGroup, files: list[ClientFile]) -> KitGroup            # общие файлы группы
def process(contract_id: str, *, store: KitStore, registry, on_stage=None) -> OnboardingReport
#   адаптер (insurer_hint не подменяет определение по документам: расхождение — предупреждение) → store.save →
#   статусы ТЗ 7.1 → invalidate(contract_id) → selfcheck → отчёт. Сбой — report.readiness="failed", база не портится.
def invalidate(contract_id: str) -> None     # сброс всех кэшей движка по договору (слоты, поиск, перечень, kNN нет)
def selfcheck(contract_id: str, store) -> dict[str, int]    # {asked, cited}, ≤ SELFCHECK_MAX вопросов
def reparse_all(store, registry) -> dict     # для scripts/ingest_kit.py: договоры клиентов после пересборки базы
```

| Маршрут | Тело → ответ |
|---|---|
| `POST /api/clients` `{name, contracts: [{legal_entity, insurer?, number?}]}` | → `ClientGroup` (201) |
| `GET /api/clients` | → `[ClientGroup + {contracts: [... + files: [ClientFile], job: OnboardingJob\|null]}]` |
| `POST /api/clients/{ugN}/contracts` `{legal_entity, insurer?, number?}` | → `ClientContract` |
| `POST /api/clients/contracts/{uNN}/files` (multipart: file, doc_type?, scope?) | → `ClientFile` (201); формат по сигнатуре (docx, xlsx, pdf, txt), ≤ DMS_MAX_UPLOAD_MB, ≤ CLIENT_MAX_FILES; имя файла очищается от пути |
| `PATCH /api/clients/contracts/{uNN}/files/{name}` `{doc_type?, scope?}` · `DELETE …/files/{name}` | → `ClientFile` / 204 |
| `POST /api/clients/contracts/{uNN}/process` | → `OnboardingJob` (202); задание уже идёт — 409 `job_running`; нет файлов — 422 `no_files` |
| `GET /api/clients/jobs/{id}` | → `OnboardingJob` (опрос; стадии received → extracted → checked → indexed → selfcheck) |

После `done` договор есть в `GET /api/groups` (группа `ugN`), по нему создаются чаты (`POST /api/chats`), работают
документы, источники, выбор клиники. Ошибки — `{"error": {code, message, field}}` по-русски. В логах нет имён
файлов клиента и текстов документов (только id договора и счётчики).
Общие файлы группы (уточнение 25.10.2026): загрузка — `POST /api/clients/contracts/{uNN}/files` со `scope=group`
через любой договор группы (файл — в `clients/<ugN>/_group/`); в `GET /api/clients` они приходят в `files` каждого
договора группы со `scope: "group"`; PATCH / DELETE — через любой договор группы по имени; то же имя заменяет файл;
коды: `job_running`, `import_running`, `no_files`, `too_many_files`, `bad_name`, 413 (размер), 415 (формат). Обработка договора берёт его файлы и
общие файлы группы.
Дополнение по ревизии w8 (25.10.2026): `GET /api/clients` — у договора ещё ключи `files_changed` (файлы отличаются
от снимка последней успешной обработки) и `in_search`; `OnboardingReport.documents[*].scope` (contract | group);
коды `write_failed` (500), `payload_too_large` (413, в т.ч. xlsx со слишком большой распаковкой); `reparse_all`
разбирает только снимок файлов последней успешной обработки: `kept` (файлы изменены — прежняя версия),
`not_processed` (ни разу не обработан — пропуск), `no_files`. Договор без программ (generic) — ответ поиском по
пунктам без вопроса о программе, метка «недостаточно данных».


## 10. Разметка таблиц оператором (ADR 0012, зафиксировано 25.10.2026)

```python
# schemas.py — ColumnRole, TableRef, TableMapping, TablePreview, InsurerProfile;
#   ClientContract.insurer_name (СК текстом), ClientContract.force_generic
# constants.py — TABLE_PREVIEW_ROWS, MAPPING_SAMPLE_ROWS, PROFILE_PREFIX "ip"

# dms_core/table_mapping.py (новый модуль, без dms_api)
def list_tables(kc: KitContract, root: Path, doc_ids: dict[str, str]) -> list[tuple[TableRef, str, list[list[str]]]]
def signature(rows: list[list[str]], header_row: int) -> str          # нормализованный заголовок
def suggest(rows, profiles: list[InsurerProfile]) -> tuple[TableMapping | None, str | None]  # профиль, иначе угадка
def apply(contract_id: str, ref: TableRef, rows, mapping: TableMapping, doc_id: str) -> tuple[list[ClinicRow], list[str]]
#   строки без названия клиники пропускаются; повтор заголовка внутри таблицы пропускается; предупреждения — по-русски
```
`onboarding.process`: generic (или force_generic) → разметки договора → профили по подписи (used += 1) → строки
перечня и программы в договор → `KitStore.save` → отчёт: `counts.clinics`, `counts.mapped_tables`, предупреждения
«применена разметка СК «…» к таблице …».

| Маршрут | Тело → ответ |
|---|---|
| `GET /api/clients/contracts/{uNN}/tables` | → `[TablePreview]` (по файлам последней обработки) |
| `POST /api/clients/contracts/{uNN}/tables/try` `{ref, mapping}` | → `{rows: [ClinicRow] (≤ MAPPING_SAMPLE_ROWS), total, warnings}` — пробный разбор, ничего не сохраняет |
| `PUT /api/clients/contracts/{uNN}/tables/mapping` `{ref, mapping, save_profile?: bool, insurer_name?}` | → `{preview: TablePreview, profile: InsurerProfile \| null}`; затем менеджер жмёт «Обработать» (или `process` сразу — `{process: true}` → `OnboardingJob`) |
| `DELETE /api/clients/contracts/{uNN}/tables/mapping?doc_id=&kind=&index=&sheet=` | → 204 |
| `GET /api/insurer-profiles` · `DELETE /api/insurer-profiles/{ipN}` | → `[InsurerProfile]` / 204 |
| `POST /api/clients` и `…/contracts` — дополнительно `insurer_name?`, `force_generic?` | как в §9 |

Ошибки: 422 `mapping_no_name` (нет колонки названия клиники), `mapping_bad_column` (колонка вне таблицы),
`table_not_found`; 409 `job_running`. Разметки и профили — в AppStore (`data/app`), без данных документов.
Уточнение §10: PUT mapping с `process: true` → `{preview, profile, job}`; DELETE mapping — `doc_id`, `kind` и только
`index` (docx) или только `sheet` (xlsx); колонки с ролью `ignore` можно не передавать (нет колонки = ignore).
Дополнение §10 (25.10.2026, проверка автора на c06): перечни «по таблице на программу» без строки заголовка
(Ренессанс: 1-я строка — название программы, далее подзаголовки видов помощи) — `TableMapping.header_row = None`
(заголовка нет) и `program_from_title = true` (программа — строка-название, одна непустая ячейка). Подпись такой
таблицы — по раскладке, не по тексту: `layout:<число колонок>:<номера колонок, непустых в большинстве строк данных>`
(название программы в подпись не входит), чтобы одна разметка (и профиль СК) применялась ко всем таблицам программ
договора и к следующим договорам СК. `suggest` предлагает это сам, если первая строка — одна ячейка и среди таблиц
договора есть несколько с той же раскладкой. В интерфейсе — «Строки заголовка нет», «Программа — из названия
таблицы» и «Применить ко всем таблицам с такой же раскладкой» (PUT mapping с `apply_same_layout: true` сохраняет
разметку для всех таких таблиц договора).
Уточнение по w9-layout: GET tables — дополнительно `layout` (подпись раскладки) и `same_layout` (число таблиц
договора с той же раскладкой); PUT mapping → `applied_tables`. Колонка входит в раскладку, если заполнена в ≥ 25 %
строк данных; одноячеечный текст длиннее 80 знаков над строкой-названием пропускается (описание программы, не название).
Уточнение по ревизии w9 (w9-fix): коды 422 `mapping_suspicious` (содержимое не похоже на перечень; PUT с
`confirm_suspicious: true` — сохранить всё же), 422 `mapping_no_program` (не указано, откуда программа, а у договора
не ровно одна программа), 409 `mapping_not_generic` (разметка у распознанной СК не сохраняется); try → `suspicious`;
GET tables → `applied_profile`, `suspicious`, `similar_unmapped`. `program_from_title` — также строки-варианты внутри
таблицы («Вариант № N …», «Общие для вариантов …») задают программу следующих строк. Профиль применяется сам только
при той же СК (insurer_name), сильной подписи (≥ 3 колонок и адрес / ≥ 3 ячеек заголовка) и прошедшей проверке
содержимого; иначе — только предложение. Строка перечня без вида помощи не даёт «клиника в перечне» для метки.


## 11. Разметка матрицы «программа × вид помощи» (ADR 0013, зафиксировано 26.10.2026)

`TableMapping.role = "care_matrix"` с `matrix_layout` `wide` (колонка `care_type` + `program_cols {колонка: программа}`)
или `long` (колонки `program`, `care_type`, `present`); `TableMapping.derive_care` у перечня клиник — матрица из видов
помощи строк перечня. Новая роль колонки `present`. Константа `MATRIX_SAMPLE_ROWS`.

```python
# dms_core/table_mapping.py
def apply_matrix(contract_id: str, ref: TableRef, rows, mapping: TableMapping, doc_id: str,
                 programs: list[str]) -> tuple[list[CareMatrix], list[str]]     # (строки матрицы, предупреждения)
def matrix_from_clinics(contract_id: str, clinics: list[ClinicRow]) -> list[CareMatrix]   # derive_care
def cell_value(text: str) -> bool | None          # True — входит, False — явное «нет», None — пропуск
```
`onboarding.process`: перечни → программы → матрицы (разметки договора, затем профили той же СК) → `derive_care` →
`Contract.programs` и `KitStore.save` (care) → readiness `ready`, если есть пункты, программы, перечень и матрица;
`counts.care_rows`, `counts.mapped_matrices`; предупреждения о пропущенных ячейках (сводно).

API — те же маршруты §10: `try` для `role=care_matrix` → `{matrix: [{program, care_type, present, source_ref}] (≤
MATRIX_SAMPLE_ROWS), total, skipped: [{cell, text}] (≤ 20), warnings, suspicious}`; `PUT mapping` принимает разметку
матрицы (`mapping_no_care` 422 — нет колонки видов помощи; `mapping_no_program` 422 — у wide нет колонок программ /
у long нет источника программы); `suggest` предлагает wide, если в заголовке ≥ 2 колонок, совпавших с программами
договора / перечня, а в первой колонке — виды помощи. Профиль СК хранит и разметки матриц.
Уточнение §11 (w10-matrix): try матрицы → `skipped_total`; try перечня с `derive_care` → `matrix`, `matrix_total`;
`counts.mapped_matrices` — таблицы матриц + перечни с derive_care; ссылки строк матрицы — `…r<R>c<C>` (wide) и
`<строка перечня>#care` (derive_care, открывается строкой перечня); пустая строка в `program_cols` — «текст
заголовка». Пара «программа × вид», где есть и «да», и «нет», не учитывается; «нет» матрицы убирает пару, выведенную
из перечня.
Дополнение §11 (26.10.2026): `TableMapping.program = "*"` у long-матрицы и перечня — «все программы договора»
(таблица услуг, общая для программ, как у СОГАЗ t7). Запасной разборщик (generic) читает и таблицы документов как
пункты (строка таблицы — пункт с путём «<документ> › табл. N, стр. M», ячейки через « | »), чтобы проверка покрытия
видела условия, исключения и услуги, набранные в таблицах (Ренессанс t6, СОГАЗ t7).


## 12. Ответ в чате (ADR 0014, зафиксировано 27.10.2026)

```python
class ReplyPoint(_Model): text: str; quote: str | None; refs: list[str]; contract_id: str | None
class ChatReply(_Model):  lead: str; points: list[ReplyPoint]; ask: list[str]; caution: str | None
Answer.reply: ChatReply | None
# constants: REPLY_POINTS_MAX = 6, REPLY_QUOTE_CHARS = 220

# dms_core/compose.py
def chat_reply(answer: Answer) -> ChatReply    # из answer.statements / per_contract / missing / questions / label
```
`engine.answer` заполняет `reply` во всех режимах (guided, agent, extractive, уточнение). `quote` — подстрока
проверенной цитаты (`Citation.quote`), обрезанная по границе фразы; `refs` — `Citation.ref` проверенных ссылок.
Тексты — `config/texts_ru/reply.yaml` (новый, в CONFIG_FILES). В тексте ответа нет id строк и служебных слов
(«supports», «guided», коды договоров кроме ГК-подписи «Договор № N»).
