"""Сборка ответа по форме ТЗ п. 8 и статус уверенности ТЗ п. 8.1 (план §6.10, CONTRACTS §7).

Вход — проверенные черновики по договорам ({contract_id: Verified}); на экран попадает только то, что прошло
citations.verify. Форма (ТЗ п. 8): 1) краткий вывод / метка SC 01 → `summary`, `label`; 2) контекст → `context`;
3) условия и ограничения → `statements` (kind condition/limitation) и `limitations`; 4) источники → `Citation`
у каждого утверждения (документ, пункт/строка таблицы, короткий фрагмент); 5) выводы из сопоставления —
отдельно, `comparisons`; 6) недостающие данные, противоречия, вопрос → `missing`, предупреждения, `questions`.

Краткий вывод модели (`summary`) проверяется так же строго, как утверждения (`checked_summary`): без живых
утверждений, с числом/датой или названием клиники не из живых ссылок, вопроса и контекста — заменяется первым
живым утверждением или нейтральной формулировкой config/texts_ru/ui.yaml (summary.*).

Контекст ГК (несколько договоров) — блок `per_contract` на каждый договор; утверждение никогда не смешивает ссылки
разных договоров (ссылка чужого договора в блоке выбрасывается, ADR 0005).

Статус (ТЗ п. 8.1), по каждому договору, затем худший по ответу — в порядке приоритета:
  contradictory (противоречие документов равного уровня) > need_clarification (есть уточняющие вопросы) >
  insufficient (нет ни одного утверждения с живой ссылкой; режим extractive; метка SC 01 «недостаточно данных») >
  confirmed.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
from collections.abc import Iterable
from functools import cache, lru_cache

from .citations import Verified, normalize
from .config import load_yaml
from .conflicts import analyze
from .constants import REPLY_POINTS_MAX, REPLY_QUOTE_CHARS, AnswerMode, Confidence, Label, Scenario, StatementKind
from .numtext import known_numbers, numbers
from .schemas import (
    Alternative,
    Answer,
    AnswerContext,
    ChatReply,
    Citation,
    LlmInfo,
    PerContract,
    ReplyPoint,
    Statement,
)
from .store.db import KitStore

# Приоритет статусов: меньше — «хуже», побеждает при сведении договоров ГК (CONTRACTS §7)
STATUS_ORDER = (Confidence.contradictory, Confidence.need_clarification, Confidence.insufficient,
                Confidence.confirmed)
GROUP_SUMMARY = "Ответ по каждому договору группы отдельно: условия одного договора на другой не переносятся."
PRECEDENCE_NOTE = "Позиция занята по старшему документу; противоречие стоит подтвердить письмом в страховую компанию."


def _dedup(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        x = (x or "").strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def worst(statuses: Iterable[Confidence]) -> Confidence:
    """Худший статус по порядку STATUS_ORDER; пусто — данных недостаточно."""
    got = list(statuses)
    if not got:
        return Confidence.insufficient
    return min(got, key=STATUS_ORDER.index)


def status_of(*, contradictory: bool, questions: bool, has_evidence: bool) -> Confidence:
    """ТЗ п. 8.1 для одного договора: порядок проверок = порядок приоритета."""
    if contradictory:
        return Confidence.contradictory
    if questions:
        return Confidence.need_clarification
    if not has_evidence:
        return Confidence.insufficient
    return Confidence.confirmed


class _Block:
    """Собранное по одному договору."""

    def __init__(self, cid: str, v: Verified) -> None:
        self.cid = cid
        self.v = v
        self.statements: list[Statement] = []
        self.comparisons: list[Statement] = []
        for st in v.draft.statements:
            # только проверенные ссылки этого договора; чужой contract_id — выбросить (защита ADR 0005)
            cits = [c for dc in st.citations if (c := v.citation(dc)) is not None and c.contract_id == cid]
            if not cits:
                continue
            s = Statement(text=st.text, citations=cits, kind=st.kind)
            (self.comparisons if st.kind is StatementKind.comparison else self.statements).append(s)
        self.has_evidence = bool(self.statements or self.comparisons)
        report = analyze(v)
        warnings = [w for w in report.warnings()
                    if all(c.contract_id == cid for c in w.citations)]
        self.statements.extend(warnings)
        self.contradictory = report.contradictory
        self.limitations = [PRECEDENCE_NOTE] if report.resolved_by_precedence else []
        self.questions = list(v.draft.questions)
        self.missing = list(v.draft.missing)


_QUOTED = re.compile(r"[«\"“]([^»\"”]{2,120})[»\"”]")


@cache
def _summary_texts() -> dict[str, str]:
    return dict((load_yaml("texts_ru/ui.yaml") or {}).get("summary") or {})


def _evidence_texts(b: _Block) -> list[str]:
    """Тексты, которыми может пользоваться краткий вывод: живые утверждения и фрагменты их документов."""
    out: list[str] = []
    for s in [*b.statements, *b.comparisons]:
        out.append(s.text)
        out.extend(f"{c.quote} {c.doc_title} {c.clause_number or ''}" for c in s.citations)
    return out


def checked_summary(summary: str, b: _Block, *, question: str | None, context: AnswerContext,
                    store: KitStore | None) -> str:
    """Краткий вывод модели — только если всё проверяемое в нём подтверждено (дефект 3 ревизии w2).

    Проверяется: каждое число и дата (numtext.numbers) есть в живых утверждениях/ссылках, вопросе или контексте;
    каждое название в кавычках «…» и каждое название клиники договора (store) встречается там же. Иначе вывод
    заменяется первым живым утверждением, а без них — нейтральной формулировкой config/texts_ru/ui.yaml.
    """
    if not b.has_evidence:
        # ни одного утверждения с живой ссылкой: вывод модели ничем не подтверждён (`docs/rules.md` п. 1)
        return _summary_texts().get("empty", "")
    texts = _evidence_texts(b)
    ctx =[question or "", context.program or "", context.date.isoformat() if context.date else ""]
    pool = normalize(" \n ".join([*texts, *ctx]))
    year = context.date.year if context.date else None
    ok = bool(summary.strip())
    if ok and numbers(summary, default_year=year) - known_numbers([*texts, *ctx], default_year=year):
        ok = False
    if ok:
        for m in _QUOTED.finditer(summary):
            if normalize(m.group(1)) not in pool:
                ok = False
                break
    if ok and store is not None:
        norm_summary = normalize(summary)
        for r in store.clinics(b.cid):
            name = normalize(r.clinic_name)
            if len(name) >= 4 and name in norm_summary and name not in pool:
                ok = False
                break
    if ok:
        return summary
    first = next((s.text for s in [*b.statements, *b.comparisons] if s.kind is not StatementKind.limitation), None)
    if first:
        return first
    return _summary_texts().get("neutral", "")


def _alternatives(blocks: list[_Block], given: list[Alternative] | None, store: KitStore | None) -> list[Alternative]:
    """Альтернативы: переданные движком (clinics_pick) + выбранные моделью и прошедшие verify (по строкам хранилища)."""
    out = list(given or [])
    seen = {a.clinic.id for a in out}
    if store is None:
        return out
    for b in blocks:
        rows = {r.id: r for r in store.clinics(b.cid)} if b.v.draft.alternatives else {}
        for alt in b.v.draft.alternatives:
            row = rows.get(alt.row_id)
            if row is not None and row.id not in seen:
                seen.add(row.id)
                out.append(Alternative(clinic=row, reason=alt.reason))
    return out


def compose(scenario: Scenario, context: AnswerContext, verified: dict[str, Verified], *,
            label: Label | None = None, label_note: str | None = None,
            labels: dict[str, Label | None] | None = None, label_notes: dict[str, str | None] | None = None,
            alternatives: list[Alternative] | None = None, questions: list[str] | None = None,
            missing: list[str] | None = None, limitations: list[str] | None = None,
            summary: str | None = None, mode: AnswerMode = AnswerMode.live, llm: LlmInfo | None = None,
            suggested_automation: str | None = None, store: KitStore | None = None,
            processing_ms: int | None = None, question: str | None = None) -> Answer:
    """Answer по форме ТЗ п. 8. label/label_note — общий вывод (один договор); labels — метка каждого договора
    в per_contract (контекст ГК)."""
    blocks = [_Block(cid, v) for cid, v in verified.items()]
    group = len(blocks) > 1 or (context.group_id is not None and context.contract_id is None and bool(blocks))
    labels = labels or {}
    label_notes = label_notes or {}
    # краткий вывод модели каждого договора — только проверенный (дефект 3): числа, даты, клиники — из ссылок
    summaries = {b.cid: checked_summary(b.v.draft.summary, b, question=question, context=context, store=store)
                 for b in blocks}

    statuses = [status_of(contradictory=b.contradictory, questions=bool(b.questions), has_evidence=b.has_evidence)
                for b in blocks]
    confidence = worst(statuses)
    if questions and STATUS_ORDER.index(confidence) > STATUS_ORDER.index(Confidence.need_clarification):
        confidence = Confidence.need_clarification
    # без модели (extractive) и при метке «недостаточно данных» подтверждённого вывода нет (план §6.4, ТЗ п. 5.1)
    if confidence is Confidence.confirmed and (mode is AnswerMode.extractive or label is Label.insufficient):
        confidence = Confidence.insufficient

    def pref(b: _Block, items: list[str]) -> list[str]:
        return [f"{b.cid}: {x}" for x in items] if group else items

    all_questions = _dedup([*(questions or []), *(q for b in blocks for q in pref(b, b.questions))])
    # этап w14-fix (ревизия 8, дефект 9): уточнение, одинаковое во ВСЕХ договорах группы («Какую причину отказа
    # назвала клиника?»), — один раз и без кода договора; различающиеся — с кодом, как прежде
    common = set.intersection(*(set(b.missing) for b in blocks)) if group and len(blocks) > 1 else set()
    all_missing = _dedup([*(missing or []), *(m if m in common else pref(b, [m])[0]
                                              for b in blocks for m in b.missing)])
    all_limits = _dedup([*(limitations or []), *(x for b in blocks for x in b.limitations)])

    answer = Answer(
        scenario=scenario, context=context, summary="", label=label, label_note=label_note, confidence=confidence,
        limitations=all_limits, missing=all_missing, questions=all_questions,
        alternatives=_alternatives(blocks, alternatives, store), suggested_automation=suggested_automation,
        mode=mode, llm=llm, processing_ms=processing_ms,
    )
    if group:
        answer.summary = summary or GROUP_SUMMARY
        answer.per_contract = [
            PerContract(contract_id=b.cid, summary=summaries[b.cid], label=labels.get(b.cid),
                        statements=[*b.statements, *b.comparisons])
            for b in blocks
        ]
        notes = {b.cid: label_notes.get(b.cid) for b in blocks if label_notes.get(b.cid)}
        if notes and not answer.label_note:
            answer.label_note = "; ".join(f"{cid}: {n}" for cid, n in notes.items())
    elif blocks:
        b = blocks[0]
        answer.summary = summary or summaries[b.cid]
        answer.statements = b.statements
        answer.comparisons = b.comparisons
        if answer.label is None and labels.get(b.cid) is not None:
            answer.label = labels[b.cid]
            answer.label_note = answer.label_note or label_notes.get(b.cid)
    else:
        answer.summary = summary or ""
    # ответ в чате (ADR 0014); движок пересобирает его после выбора клиники и неблокирующих уточнений
    answer.reply = chat_reply(answer, question=question, store=store)
    return answer


# ------------------------------------------------------------------ ответ в чате (ADR 0014, CONTRACTS §12)
# Сообщение ассистента собирает КОД из уже проверенных утверждений ответа (только Citation.verified): короткий
# ответ (lead), «что сказано в договоре» (points: формулировка словами + точная цитата пункта), «что уточнить» (ask)
# и одна строка ограничений (caution). Тексты и словари — config/texts_ru/reply.yaml; знаний модели нет
# (`docs/rules.md` п. 1–2): всё содержание пунктов берётся из цитаты документа, а не придумывается.

# служебные id в тексте: ссылки строк («c06.program#t5r250»), коды договоров («c06»), id строк таблиц («t5r250»)
SERVICE_ID = re.compile(r"\b[a-z]\d{2}\.[\w.]+#[\w.]+|\bc\d{2}\b|\bt\d+r\d+(?:p\d+)?\b")
_SENT_END = re.compile(r"(?<=[.!?…])\s+(?=[А-ЯЁA-Z«\"])")
# маркеры начала пункта в сыром тексте документа: «- », «•», «1.5. », «2)», «VII. »
_ITEM_MARK = re.compile(r"^\s*(?:[-–—•*·]\s*|\d+(?:\.\d+)*[.)]\s*|[IVXLC]+\.\s+)+")
_FOOTNOTE = re.compile(r"\[\^\d+\]")
_YESNO = {"да", "нет", "входит", "не входит", "+", "-", "включено", "не включено"}


def sentences(text: str) -> list[str]:
    """Деление на фразы по «. » + заглавная, но не внутри «…» (название программы «Обслуживание. Программа …»)
    и не после номера пункта («3. Терапевтические …», «п. 2.8. настоящего …»)."""
    out: list[str] = []
    start = depth = 0
    for m in re.finditer(r"[«»]|" + _SENT_END.pattern, text):
        if m.group(0) == "«":
            depth += 1
        elif m.group(0) == "»":
            depth = max(0, depth - 1)
        elif depth == 0 and not re.search(r"(?:^|\s)(?:п|пп|ст|г|т|ч|т\.\s?ч|т\.\s?д|т\.\s?п|др|руб|коп|мес|им|ул|д|\d+(?:\.\d+)*)\.$", text[start:m.start()]):
            out.append(text[start:m.start()].strip())
            start = m.end()
    out.append(text[start:].strip())
    return [s for s in out if s]
_SUPERSCRIPT = re.compile(r"[¹²³⁰-₟]")
_EDGE_MARKS = re.compile(r"^[\s\-–—•*·;:,.)\]]+|^\(?\d{1,2}[.)]\s+|[\s\-–—•*·;:,(\[]+$")
_GK_PREFIX = re.compile(r"^(c\d{2}):\s*")
POINT_TEXT_CHARS = 200        # формулировка пункта из сырого текста документа — коротко; целиком — в цитате


@cache
def _reply_texts() -> dict:
    return dict(load_yaml("texts_ru/reply.yaml") or {})


@cache
def _guided_texts() -> dict[str, str]:
    """Шаблоны формулировок guided (ui.yaml и clinic_pick.yaml) — чтобы узнавать готовые фразы кода."""
    out = dict((load_yaml("texts_ru/ui.yaml") or {}).get("guided") or {})
    out.update((load_yaml("texts_ru/clinic_pick.yaml") or {}).get("guided") or {})
    return out


# утверждения «клиника есть в перечне программы» (guided): для lead «Можно: клиника … есть в перечне …»
CLINIC_IN_LIST = ("clinic_yes", "clinic_yes_short", "clinic_pick_yes", "clinic_pick_yes_short")


def cite_ref(c: Citation) -> str:
    """Ссылка для `ReplyPoint.refs`: Citation.ref — ref, по которому цитата проверена (verify, ADR 0014). Без него
    (ответы, собранные не через verify: extractive, сохранённые) — строка таблицы или «документ:пункт»; интерфейс
    связывает пункт с Citation по любому из этих ключей (chat.js refMatch)."""
    return c.ref or c.table_ref or f"{c.doc_id}:{c.clause_number or ''}"


def _template_rx(key: str) -> re.Pattern[str] | None:
    """Шаблон guided (ui.yaml) → регулярка: подстановки {x} — любые символы. Нужна, чтобы узнать готовую
    фразу кода (основания отказа) в кратком выводе и не разбирать её заново."""
    tpl = _guided_texts().get(key)
    if not tpl:
        return None
    parts = re.split(r"\{(\w+)\}", tpl)
    rx = "".join(re.escape(p) if i % 2 == 0 else f"(?P<{p}>.+?)" for i, p in enumerate(parts))
    # точка в конце шаблона — конец фразы (а не «п. 5» внутри основания)
    if rx.endswith(r"\."):
        rx = rx[:-2] + r"\.(?=\s+[А-ЯЁ«]|\s*$)"
    return re.compile(rx)


def _humanize(text: str) -> str:
    """Служебные слова → человеческие (reply.yaml replace), без id строк и кодов договоров."""
    for pat, rep in _reply_texts().get("replace") or []:
        text = re.sub(pat, rep, text, flags=re.IGNORECASE)
    text = SERVICE_ID.sub("", text)
    return re.sub(r"\s{2,}", " ", re.sub(r"\s+([,.;:])", r"\1", text)).strip()


def _first_sentence(text: str) -> str:
    got = sentences(text)
    return got[0] if got else ""


def _shorten(text: str, limit: int) -> str:
    """Не длиннее limit: по границе фразы, затем «;»/«,», затем слова — с «…»."""
    if len(text) <= limit:
        return text
    first = _first_sentence(text)
    if len(first) <= limit:
        return first
    head = text[:limit - 1]
    cut = max(head.rfind("; "), head.rfind(", "))
    if cut < limit // 2:
        cut = head.rfind(" ")
    return head[:cut if cut > 0 else limit - 1].rstrip(" ,;:—-") + "…"


def _is_raw(text: str, quote: str) -> bool:
    """Утверждение — сам текст документа (найденный пункт), а не формулировка кода: начало текста есть в цитате
    или начало цитаты — в тексте."""
    t, q = normalize(text), normalize(quote)
    # вторая проверка — только когда цитата сопоставима по длине с текстом (название клиники в формулировке кода
    # «Клиника «X» есть в перечне…» не делает её сырым текстом)
    return bool(t and q) and (t[:50] in q or (q[:50] in t and len(q) >= 0.6 * len(t)))


def _clean_raw(text: str) -> str:
    """Сырой текст пункта/строки таблицы → читаемая фраза: без маркеров списка и сносок, строка таблицы
    «приёмы специалистов | терапевт | да» → «Приёмы специалистов: терапевт — да», ЗАГЛАВНЫЕ → обычные."""
    text = _FOOTNOTE.sub("", _SUPERSCRIPT.sub("", text)).replace("\t", " ")
    text = _ITEM_MARK.sub("", text).strip()
    cells = [c.strip() for c in text.split(" | ") if c.strip()]
    if len(cells) >= 2:
        # ячейка «да/нет» — значение строки; после неё — примечание («доступно в телемедицине»)
        yn = next((i for i in range(1, len(cells)) if cells[i].casefold() in _YESNO), None)
        if yn is not None:
            head = cells[0] + (": " + ", ".join(cells[1:yn]) if yn > 1 else "")
            text = f"{head} — {cells[yn]}" + ("; " + "; ".join(cells[yn + 1:]) if yn + 1 < len(cells) else "")
        else:
            text = f"{cells[0]}: " + "; ".join(cells[1:])
    if text.isupper():
        text = text.capitalize()
    return _upper_first(re.sub(r"\s{2,}", " ", text).strip(" :;,"))


def _lower_first(text: str) -> str:
    """«Вид помощи…» → «вид помощи…» для подстановки после «Метка: »; аббревиатуры и «…» не трогаем."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def reply_quote(quote: str, limit: int = REPLY_QUOTE_CHARS) -> str:
    """Цитата для пункта ответа: подстрока проверенной цитаты без маркеров списка и сносок по краям, обрезанная по
    границе фразы (точка / точка с запятой), а если такой нет — по границе слова; ≤ limit знаков."""
    q = _SUPERSCRIPT.sub("", quote or "").strip()
    prev = None
    while prev != q:                         # маркеры по краям: «- », «1) », «•», хвостовые «;», «,»
        prev = q
        q = _EDGE_MARKS.sub("", q).strip()
    if len(q) <= limit:
        return q
    head = q[:limit]
    cut = max(head.rfind(". "), head.rfind("; "), head.rfind("! "), head.rfind("? "))
    if cut >= limit // 3:
        return head[:cut + 1].strip()
    cut = head.rfind(" ")
    return _EDGE_MARKS.sub("", head[:cut if cut > 0 else limit]).strip()


def _markers(quote: str) -> list[str]:
    """Условия, названные в цитате (reply.yaml quote_markers): «по направлению врача», «по жизненным показаниям»."""
    out: list[str] = []
    for pat, phrase in _reply_texts().get("quote_markers") or []:
        if re.search(pat, quote, flags=re.IGNORECASE) and phrase not in out:
            out.append(phrase)
    return out


def point_text(text: str, quote: str) -> str:
    """Формулировка пункта словами. Утверждение-отсылка («… по порядку этого пункта: сверьте с этим пунктом»)
    заменяется темой + содержанием пункта: условия из цитаты по словарю reply.yaml, а без них — началом цитаты."""
    t = _reply_texts()
    rx = re.compile(t["this_clause"], flags=re.IGNORECASE)
    if rx.search(text):
        topic = rx.sub("", text).strip(" :—–-,.")
        topic = _upper_first(re.sub(t["topic_lead_in"], "", topic, flags=re.IGNORECASE).strip())
        found = _markers(quote)
        if found:
            text = t["point_topic"].format(topic=topic, content=", ".join(found))
        else:
            text = t["point_topic_quote"].format(topic=topic, content=_lower_first(_shorten(_clean_raw(quote), 140)))
    elif _is_raw(text, quote):
        text = _shorten(_clean_raw(text), POINT_TEXT_CHARS)
    return _humanize(text)


def _verified(st: Statement) -> list[Citation]:
    return [c for c in st.citations if c.verified]


# порядок пунктов: основание метки → условия → исключения/ограничения → справка (факты, сравнения, выводы)
_BASIS = {Label.denied: StatementKind.limitation, Label.conditional: StatementKind.condition,
          Label.allowed: StatementKind.fact}
_ORDER = (StatementKind.condition, StatementKind.limitation, StatementKind.fact, StatementKind.comparison,
          StatementKind.inference)


def _rank(st: Statement, label: Label | None) -> tuple[int, int]:
    """(вид, сырой текст): основание метки первым, внутри вида — формулировки кода раньше сырого текста пункта."""
    cits = _verified(st)
    raw = int(bool(cits) and _is_raw(st.text, cits[0].quote))
    if _template_rx("exclusion_absent_st") and _template_rx("exclusion_absent_st").match(st.text):
        return len(_ORDER) + 1, raw          # «исключения просмотрены» — вывод кода о просмотре, не текст: последним
    if label in _BASIS and st.kind is _BASIS[label]:
        return 0, raw
    return 1 + _ORDER.index(st.kind), raw


# названия программ контекста ответа: их слова в вопросе — не услуга (ставит chat_reply на время сборки ответа)
_CONTEXT_NAMES: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("dms_context_names", default=())
# все слова вопроса, включая обстоятельства («после операции») — только для выбора между равными пунктами
_QUESTION_ALL: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("dms_question_all", default=())
SHORT_QUOTE = 40  # короче — обрывок перечня («25. для лечения сахарного диабета;»): полный пункт главнее
SAME_STEM = 7     # леммы длиннее — сравнение по началу («стоматология» ↔ «стоматологический»; «гастроскопия» ≠
                  # «гастроэнтеролог» — 6 знаков было мало), короче — целиком


def _q_lemmas(question: str | None) -> list[str]:
    """Леммы услуги из вопроса для отбора пунктов: без слов самого вопроса (reply.yaml question_noise) и служебных;
    глагол процедуры — её названием (config service_classes procedures.verbs: «вырвать» → «удаление»)."""
    if not question:
        return []
    from .index.morph import STOPWORDS, lemmas
    from .scenarios.route import procedures_cfg

    noise = tuple(_reply_texts().get("question_noise") or [])
    verbs = procedures_cfg().verbs or {}
    # название с большой буквы не в начале вопроса («в Медси», «программа Бизнес») — не услуга; аббревиатура
    # («МРТ», «ЭКГ») — услуга
    t = _reply_texts()
    # условие обращения, а не услуга: «без направления терапевта», «по назначению невролога» (reply.yaml)
    if t.get("qualifier_rx"):
        question = re.sub(t["qualifier_rx"], " ", question, flags=re.IGNORECASE)
    # название программы контекста («Входит ли в ВИП …») — не услуга
    for name in _CONTEXT_NAMES.get() or ():
        question = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", " ", question, flags=re.IGNORECASE)
    words = re.findall(r"[A-Za-zА-Яа-яЁё][\w-]*", question)
    # «Медси», «Москва» — название; «ВИЧ-инфекция», «МРТ» — нет (после первой буквы не одни строчные)
    names = {w.casefold() for i, w in enumerate(words) if i and w[:1].isupper() and w[1:].islower()}
    out: list[str] = []
    for w in words:
        if w.casefold() in names:
            continue
        for lem in lemmas(w):
            if lem in STOPWORDS or len(lem) < 3 or lem.startswith(noise):
                continue
            out += lemmas(verbs[lem]) if lem in verbs else [lem]
    return _dedup(out)


def _key_lemmas(question: str | None) -> list[str]:
    """Ключевые слова услуги вопроса — существительные (и сокращения), без общих слов процедуры и обращения
    (config service_classes procedures.generic, reply.yaml key_skip): «удалить родинку по косметическим показаниям»
    → [«родинка»]; «протезирование зубов — коронка» → [«протезирование», «зуб», «коронка»]. Существительных нет —
    прилагательные («медикаментозное обеспечение» → [«медикаментозный»]). По ним выбирается главный пункт ответа."""
    from .guided import _stem, coverage_cfg, procedure_cfg
    from .index.morph import _analyzer

    skip = set(procedure_cfg().generic or ()) | set(_reply_texts().get("key_skip") or [])
    anatomy = {_stem(x) for x in coverage_cfg().anatomy}
    nouns, adjs, body = [], [], []
    for lem in _q_lemmas(question):
        if lem in skip:
            continue
        pos = str(_analyzer().parse(lem)[0].tag.POS) if re.search("[а-яё]", lem) else "NOUN"
        if pos in ("NOUN", "None"):
            # часть тела («МРТ коленного сустава») — уточнение услуги, не её название: ключ, только если другого нет
            (body if _stem(lem) in anatomy else nouns).append(lem)
        elif pos in ("ADJF", "PRTF"):
            adjs.append(lem)
    return nouns or adjs or body


def _doc_rank(c: Citation) -> int:
    """Договор и доп. соглашение, затем программа, затем прочее (памятка — её оглавление не ответ, w18)."""
    kind = (c.doc_id or "").split(".", 1)[-1].split("_", 1)[0]
    return {"contract": 0, "amendment": 0, "program": 1}.get(kind, 2)


def _is_anatomy(lem: str) -> bool:
    from .guided import _stem, coverage_cfg

    return _stem(lem) in {_stem(x) for x in coverage_cfg().anatomy}


def _key_hit(st: Statement, keys: list[str]) -> bool:
    """В цитате пункта есть ключевые слова услуги: все (их ≤ 2) или все, кроме одного (их ≥ 3)."""
    from .index.morph import lemmas

    cits = _verified(st)
    if not cits or not keys:
        return False
    have = set(lemmas(cits[0].quote))
    hits = sum(any(_phrase_hit(v, have) for v in _synonyms(w)) for w in keys)
    return hits >= (min(len(keys), 2))


@cache
def _synonym_groups() -> tuple[tuple[tuple[str, ...], ...], ...]:
    """Равнозначные названия (config route_markers.yaml key_synonyms и assist_synonyms, синонимы процедур) — каждое
    как фраза из лемм: «прививка» — «вакцинация», «МРТ» — «магнитно-резонансная томография»."""
    from .index.morph import lemmas

    # только явные равнозначные названия: группы видов помощи («психолог» ↔ «амбулаторно-поликлиническая помощь») и
    # глаголы процедур сюда не входят — иначе «помощь», «приём» совпадали бы с любым пунктом
    cfg = load_yaml("route_markers.yaml") or {}
    proc = ((load_yaml("service_classes.yaml") or {}).get("procedures") or {}).get("synonyms") or {}
    groups = [*(cfg.get("key_synonyms") or []), *(cfg.get("assist_synonyms") or []),
              *[[k, *(v or [])] for k, v in proc.items()]]
    out = []
    for g in groups:
        phrases = tuple(t for p in g if (t := tuple(lemmas(str(p)))))
        if phrases:
            out.append(phrases)
    return tuple(out)


def _synonyms(word: str) -> list[tuple[str, ...]]:
    """Названия, равнозначные слову: само слово и фразы групп, где слово — отдельный член группы (а не часть фразы:
    «зуб» из «имплантация зуба» — не синоним «имплантации», ревизия w18)."""
    out: list[tuple[str, ...]] = [(word,)]
    for g in _synonym_groups():
        if (word,) in g and len(g) <= 12:
            out += [p for p in g if p not in out]
    return out


def _phrase_hit(phrase: tuple[str, ...], have: set[str]) -> bool:
    return all(_lemma_hit(w, have) for w in phrase)


def _lemma_hit(want: str, have: set[str]) -> bool:
    """Лемма есть среди лемм текста: та же; длинная — то же начало («стоматология» ↔ «стоматологический»); короткая —
    сложное существительное с той же основой («грыжа» ↔ «грыжесечение»), но не прилагательное («зуб» ≠ «зубной»)."""
    if want in have or (len(want) >= SAME_STEM and any(h.startswith(want[:SAME_STEM]) for h in have)):
        return True
    # короткое слово — начало сложного («грыжа» ↔ «грыжесечение»); длинное так не сравнивается («гастроскопия» ≠
    # «гастроэнтерология» — общее «гастро» лишь корень)
    st = want.rstrip("аяоеиыуюь")
    if len(want) <= 6 and len(st) >= 3 and \
            any(len(h) > len(want) + 3 and h.startswith(st) and _is_noun(h) for h in have):
        return True
    # вторая часть сложного слова: «протезирование» ↔ «зубопротезирование»
    st = want[:-3] if len(want) > 8 else want
    return len(st) >= 6 and any(len(h) > len(st) + 2 and st in h[3:] and _is_noun(h) for h in have)


@lru_cache(maxsize=20_000)
def _is_noun(lem: str) -> bool:
    from .index.morph import _analyzer

    return str(_analyzer().parse(lem)[0].tag.POS) == "NOUN"


def _relevant(st: Statement, lems: list[str]) -> bool:
    """Пункт о самой услуге вопроса: в цитате есть леммы услуги (не меньше двух, если их ≥ 2). По леммам, а не по
    началу слова: «удаление зубных отложений» (чистка) — не «удаление зуба» (ревизия точности w18)."""
    from .index.morph import lemmas

    cits = _verified(st)
    if not cits or not lems:
        return False
    have = set(lemmas(cits[0].quote))
    return sum(any(_phrase_hit(v, have) for v in _synonyms(w)) for w in lems) >= min(2, len(lems))


def _keep_unrelated(st: Statement) -> bool:
    """Пункты, нужные и без слов услуги: строка таблицы видов помощи (основание «входит») и «исключения
    просмотрены, услуги в них нет»."""
    if _care_fact(st) is not None:
        return True
    rx = _template_rx("exclusion_absent_st")
    return bool(rx and rx.match(st.text))


def _focus(statements: list[Statement], lems: list[str] | None, keys: list[str] | None = None) -> list[Statement]:
    """Только пункты о самой услуге вопроса, если такие есть («вырвать зуб» — без пломб и чистки; «коронка» — без
    протезов сосудов); нет ни одного — все (тогда lead говорит, что прямо услуга не названа)."""
    def ok(s: Statement) -> bool:
        return _relevant(s, lems or keys or []) and (not keys or _key_hit(s, keys))
    if not (lems or keys) or not any(ok(s) for s in statements):
        return statements
    return [s for s in statements if ok(s) or _keep_unrelated(s)]


def _quote_key(quote: str) -> str:
    """Ключ одинаковой цитаты: без приставки «Программа: » строки таблицы и кавычек, первые 80 знаков."""
    q = _clean_raw(quote).strip(" «»\"")
    for _ in range(4):                      # «Муром Стандарт: Расширение программы "Стандарт": …» — все рубрики
        q = re.sub(r"^[^:«»]{1,60}:\s*", "", q).strip(" «»\"")
    return normalize(q)[:80]


def _points(statements: list[Statement], label: Label | None, contract_id: str | None = None,
            stems: list[str] | None = None, lems: list[str] | None = None,
            keys: list[str] | None = None) -> list[ReplyPoint]:
    """Пункты «что сказано в договоре»: только утверждения с проверенной ссылкой (`docs/rules.md` п. 1), по важности,
    без дублей (одинаковый текст или та же цитата того же источника). stems — основы услуги из вопроса: пункты
    о самой услуге идут сразу после основания метки, раньше общих фактов о виде помощи."""
    out: list[ReplyPoint] = []
    seen: set[str] = set()
    stems = stems or []

    def key(s: Statement) -> tuple[int, int, int]:
        kind, raw = _rank(s, label)
        return (0 if kind == 0 else 1 if _about_service(s, stems) else 2), kind, raw

    for st in sorted(_focus(statements, lems, keys), key=key):     # sorted устойчив: внутри вида — как было
        cits = _verified(st)
        if not cits:
            continue
        if _dangling(cits[0].quote) and _is_raw(st.text, cits[0].quote):
            continue                         # обрывок «Стационарное лечение предусматривает» — без содержания
        quote = reply_quote(cits[0].quote)
        text = point_text(st.text, cits[0].quote)
        # одинаковая цитата из разных мест (строка таблицы и пункт с тем же текстом) — один пункт (ревизия w18)
        keys = {normalize(text), normalize(quote) + re.sub(r"@v\d+$", "", cite_ref(cits[0])),
                "q:" + _quote_key(cits[0].quote)}
        if not text or keys & seen:
            continue
        seen |= keys
        out.append(ReplyPoint(text=text, quote=quote or None, refs=_dedup(cite_ref(c) for c in cits),
                              contract_id=contract_id))
    return out


def _q_stems(question: str | None) -> list[str]:
    """Основы слов услуги из вопроса: без служебных, общих слов ДМС и слов самого вопроса («входит ли», «можно»,
    «сотруднику» — reply.yaml question_noise)."""
    if not question:
        return []
    from .guided import key_stems  # поздний импорт: guided тяжёлый и сам не зависит от compose
    noise = tuple(_reply_texts().get("question_noise") or [])
    return [s for s in key_stems(question) if not s.startswith(noise)]


def _service_hits(st: Statement, stems: list[str]) -> int:
    """Сколько основ услуги из вопроса в тексте утверждения и его цитате (0 — пункт не про саму услугу)."""
    if not stems:
        return 0
    from .guided import _hits
    cits = _verified(st)
    # только текст документа (цитата): формулировки кода сами повторяют название услуги из вопроса
    return _hits(stems, cits[0].quote) if cits else 0


def _dangling(quote: str) -> bool:
    """Обрывок перед перечнем: короткая фраза без точки, кончается глаголом или двоеточием («… предусматривает»,
    «… являются:») — содержание пункта в следующих строках, в цитате его нет."""
    q = _SUPERSCRIPT.sub("", quote or "").strip()
    if " | " in q or " — " in q or len(q.split()) > 5:
        return False                         # строка таблицы «МРТ — входит» и обычная фраза — не обрывок
    return q.endswith(":") or bool(re.search(r"(?:ает|яет|ются|ется|ит|ят)$", q.casefold()))


def _yes_row(st: Statement) -> bool:
    """Строка таблицы со значением «да/нет» в ячейке (прямой ответ «входит / не входит»)."""
    cits = _verified(st)
    return bool(cits) and any(c.strip().casefold() in _YESNO for c in cits[0].quote.split(" | ")[1:])


def _about_service(st: Statement, stems: list[str]) -> bool:
    """Пункт о самой услуге из вопроса, а не о виде помощи в целом: совпали основы услуги (две, если их ≥ 2)."""
    return bool(stems) and _service_hits(st, stems) >= min(2, len(stems)) and st.kind is not StatementKind.limitation


def _main_statement(statements: list[Statement], label: Label | None) -> Statement | None:
    """Главное утверждение для lead: формулировка кода раньше сырого текста пункта, затем по важности."""
    live = [s for s in statements if _verified(s) and not _dangling(_verified(s)[0].quote)]
    return min(live, key=lambda s: _rank(s, label)[::-1]) if live else None


def _refusal_reason(question: str | None) -> str | None:
    """Причина отказа, названная в вопросе («говорит не по полису», «сослались на исключения»), — слайд 12:
    в SC 01 выделить услугу, клинику и причину. Только слова самого вопроса."""
    if not question:
        return None
    m = re.search(_reply_texts()["reason_rx"], question, flags=re.IGNORECASE)
    if not m:
        return None
    reason = m.group("reason").strip(" ,.;:—-")
    return reason if len(reason) >= 3 else None


def _refusal_core(summary: str, label: Label | None) -> str | None:
    """Разбор отказа (w14): основания из краткого вывода кода → «отказ может быть обоснован, если …; если случай
    экстренный — …». Краткий вывод уже прошёл проверку (guided — код, agent — checked_summary). Для меток
    «можно»/«нельзя» основание — главное утверждение, а не перечень возможных причин отказа."""
    if label in (Label.allowed, Label.denied):
        return None
    summary = summary or ""
    t = _reply_texts()
    # факты со слов менеджера дали итог (guided.refusal_facts): «отказ обоснован: …» / «основание не подтверждается: …»
    for key, lead_key in (("refusal_sum_justified", "lead_refusal_justified"),
                          ("refusal_sum_cleared", "lead_refusal_cleared")):
        got = _template_rx(key)
        m = got.search(summary) if got else None
        if m and t.get(lead_key):
            return t[lead_key].format(why=m.group("why").strip())
    rx = _template_rx("refusal_sum_grounds")
    m = rx.search(summary or "") if rx else None
    if m:
        key = "lead_refusal_conditional" if label is Label.conditional else "lead_refusal"
        core = t[key].format(grounds=m.group("grounds").strip())
        emergency = _template_rx("refusal_sum_emergency")
        if emergency and emergency.search(summary or ""):
            core += t["lead_refusal_emergency"]
        return core
    no = _guided_texts().get("refusal_sum_no_grounds")
    if no and no in summary and label in (None, Label.insufficient):
        return t["lead_refusal_no_grounds"]
    return None


LEAD_CORE_CHARS = 160      # длиннее — у пункта-перечня в lead берётся часть со словами услуги
LEAD_CORE_MIN = 30         # короче — первое предложение пункта лишь рубрика («Программа "Стандарт".»)
_LIST_SPLIT = re.compile(r";\s+|,\s+(?![^()]*\))")


def _around(text: str, lems: list[str] | None, limit: int = LEAD_CORE_CHARS) -> str:
    """Длинный пункт-перечень → «начало пункта … часть перечня с услугой» («Зубопротезирование и подготовка к нему …
    удаление и депульпирование зубов»), а не начало, обрезанное до «удаление кист…» (ревизия w18). Без слов услуги
    в перечне — начало пункта. Полная цитата — в пунктах ниже."""
    from .index.morph import lemmas

    text = _clean_raw(text)
    if len(text) <= limit or not lems:
        return _shorten(text, limit)
    parts = [p.strip() for p in _LIST_SPLIT.split(text) if p.strip()]
    hits = [sum(_lemma_hit(w, set(lemmas(p))) for w in lems) for p in parts]
    best = max(range(len(parts)), key=lambda i: (hits[i], -i)) if parts else 0
    if not parts or hits[best] == 0 or best == 0:
        return _shorten(text, limit)
    # часть с услугой близко к началу — начало пункта до неё целиком, без «…» («Услуги … оказываемые» — нет)
    upto = text[:text.find(parts[best]) + len(parts[best])] if parts[best] in text else ""
    if upto and len(upto) <= limit:
        return upto.rstrip(" ,;:")
    # начало пункта без оговорок в скобках («(за исключением случаев …)»): иначе скобка обрывается на середине
    head = _shorten(re.sub(r"\s*\([^)]*\)?", "", parts[0]), limit // 2).rstrip("…").rstrip(" ,;:")
    return _shorten(f"{head} … {parts[best]}", limit)


def _service_statements(statements: list[Statement], stems: list[str], lems: list[str] | None,
                        keys: list[str] | None) -> list[Statement]:
    """Пункты о самой услуге вопроса — кандидаты в главный пункт. С ключевыми словами (compose._key_lemmas): в цитате
    есть ключевые слова услуги и слова вопроса («родинка» — не косметическая стоматология, «коронка зуба» — не
    протезы сосудов, ревизия выбора главного пункта w18); без них — прежнее правило по основам вопроса."""
    # строка таблицы видов помощи — не пункт об услуге (для неё — шаблон lead_insufficient_care)
    live = [s for s in statements if _verified(s) and s.kind is not StatementKind.limitation and _care_fact(s) is None]
    if keys:
        # часть тела («коленного сустава») — уточнение: пункт «МРТ — да» об услуге и без неё
        # общие слова («анализ на витамин D») — не обязательны: пункт «исследование уровня витамина D» о той же услуге
        rel = [w for w in (lems or keys) if w in keys or not (_is_anatomy(w) or w in _generic_words())] or keys
        got = [s for s in live if _key_hit(s, keys) and (_relevant(s, rel) or _relevant(s, lems or keys))]
    else:
        got = [s for s in live if _about_service(s, stems)]
    from .index.morph import lemmas

    full = [w for w in _QUESTION_ALL.get() if w not in (lems or [])]

    def hits(words: list[str], have: set[str]) -> int:
        return sum(any(_phrase_hit(v, have) for v in _synonyms(w)) for w in words)

    def rich(words: list[str], have: set[str]) -> int:
        # слово названо и сокращением, и полностью («МРТ» и «магнитно-резонансная томография» — строка о МРТ, а не
        # «ПЭТ/МРТ» в перечне)
        return sum(sum(_phrase_hit(v, have) for v in _synonyms(w)) for w in words)

    def score(s: Statement) -> tuple:
        q = _verified(s)[0].quote
        have = set(lemmas(q))
        low = q.casefold()
        pos = min((low.find(k[:SAME_STEM]) for k in (keys or []) if low.find(k[:SAME_STEM]) >= 0), default=999)
        words = lems or keys or []
        # больше слов вопроса; строка таблицы «да / нет» (прямой ответ); полнота названия; слова обстоятельств
        # вопроса («после операции» — «после оперативного лечения»); факт раньше оговорки; где
        # ключевое слово раньше (пункт о нём, а не конец перечня)
        return (-hits(words, have), len(q) < SHORT_QUOTE, not _yes_row(s), -rich(words, have), -hits(full, have),
                s.kind is StatementKind.condition, _doc_rank(_verified(s)[0]), pos, -_service_hits(s, stems))
    return sorted(got, key=score)


def _core(scenario: Scenario, label: Label | None, summary: str, statements: list[Statement],
          stems: list[str] | None = None, lems: list[str] | None = None,
          keys: list[str] | None = None) -> str | None:
    """Одна фраза по существу: основания отказа (SC 01) или главное проверенное утверждение. Есть пункт о самой
    услуге из вопроса (строка таблицы «вызов врача на дом — да») — он главнее общего «вид помощи входит»
    (ревизия 9: lead отвечал про вид помощи, а не про услугу). Для «нельзя» основание — исключение."""
    core = _refusal_core(summary, label) if scenario is Scenario.sc01 else None
    if core is None:
        stems = stems or []
        service = _service_statements(statements, stems, lems, keys) if label is not Label.denied else []
        # «недостаточно данных», а услуга названа в исключении: это главное, что нужно знать менеджеру
        excl = [s for s in statements if label is Label.insufficient and s.kind is StatementKind.limitation
                and _verified(s) and (_key_hit(s, keys) and _relevant(s, lems or keys) if keys
                                      else stems and _service_hits(s, stems) >= min(2, len(stems)))]
        if excl and not service:
            frag = _around(_verified(excl[0])[0].quote, lems)
            return _reply_texts()["lead_insufficient_exclusion"].format(text=frag.rstrip("."))
        # «Можно при условиях»: главное — что помощь входит (факт), условия — дальше словами из цитат
        pool = [s for s in statements if s.kind is not StatementKind.condition] if label is Label.conditional else []
        # пункта об услуге нет, но есть строка таблицы видов помощи — она главнее случайного пункта («коронка» —
        # не «протезы сосудов»)
        rows = [s for s in statements if _verified(s) and _care_fact(s) is not None]
        care_row = rows[0] if rows and keys and not service and label in (Label.allowed, Label.conditional) else None
        # вопрос о самом виде помощи («Входит ли стоматология?»): все слова услуги — в названии строки таблицы видов
        # помощи (без синонимов: «психотерапевт» — не «амбулаторно-поликлиническая помощь») — строка главная
        from .index.morph import lemmas as _lm
        rel = [w for w in (lems or keys or []) if not _is_anatomy(w)]
        level = next((r for r in rows if rel and all(_lemma_hit(w, set(_lm(_care_fact(r)["care"]))) for w in rel)), None)
        if level is not None and label in (Label.allowed, Label.conditional):
            service, care_row = [], level
        # ответ без метки: формулировка кода (ближайший взнос, срок из карточки) — прямой ответ, главнее цитат
        code = [s for s in statements if label is None and _verified(s) and s.kind is StatementKind.fact
                and _care_fact(s) is None and not _is_raw(s.text, _verified(s)[0].quote)]
        main = (code[0] if code else None) or (service[0] if service else None) or care_row or \
            _main_statement(pool, None) or _main_statement(statements, label)
        if main is not None:
            core = _first_sentence(point_text(main.text, _verified(main)[0].quote))
            short = len(core) < LEAD_CORE_MIN and _is_raw(main.text, _verified(main)[0].quote)
            if short or (len(_verified(main)[0].quote) > LEAD_CORE_CHARS and
                         _is_raw(main.text, _verified(main)[0].quote)):
                # длинный перечень — часть с самой услугой; обрывок-рубрика («Программа "Стандарт".») — тоже
                core = _around(_verified(main)[0].quote, lems)   # длинный перечень — часть с самой услугой
            # рубрика программы в начале («Программа "Стандарт". Стоматологическое обслуживание») — не нужна
            core = re.sub(_reply_texts().get("care_name_strip") or r"(?!)", "", core, count=1).strip() or core
            # строка таблицы «раздел: услуга — да; условие» — словами: «услуга входит в программу (условие)»
            row_core = _row_lead(core)
            if row_core:
                core = row_core
            clinic = _clinic_in_list(statements) if label is Label.allowed else None
            if clinic and normalize(clinic) not in normalize(core):
                # «Можно: клиника «X» есть в перечне клиник программы «P»; «вид помощи» — входит …» (слайд 12)
                core = f"{clinic}; {_lower_first(core)}"
            if label is Label.conditional:
                # «Можно при условиях: … входит; условия: по направлению врача» — слова условий из цитат пунктов
                # условий (словарь reply.yaml quote_markers), без них — отсылка к пунктам ниже
                found = _dedup(m for s in [main, *statements] if s is main or s.kind is StatementKind.condition
                               for c in _verified(s)[:1] for m in _markers(c.quote))
                if not found:
                    # условие лежит в строке таблицы или пункте-факте («…; да; по направлению Страховщика»):
                    # слова условий из цитат первых пунктов ответа, кроме исключений
                    # только пункты о самой услуге (если такие есть): условие пункта о другой услуге («аутогемотерапия
                    # не более 5 сеансов» к вопросу о гастроэнтерологе) — не её условие
                    pool_c = [s for s in _focus(statements, lems, keys)
                              if s.kind is not StatementKind.limitation and _verified(s)]
                    if keys and not any(_key_hit(s, keys) for s in pool_c):
                        pool_c = [s for s in pool_c if _care_fact(s) is not None]
                    ranked = sorted(pool_c, key=lambda s: _rank(s, label))[:4]
                    found = _dedup(m for s in ranked for m in _markers(_verified(s)[0].quote))
                found = [m for m in found if m.casefold() not in core.casefold()]
                tail = ", ".join(found[:3]) if found else _reply_texts()["lead_conditions_below"]
                # условие уже сказано в строке таблицы («входит в программу (по направлению …)») — не повторяем
                if (main.kind is not StatementKind.condition or found) and not (row_core and "(" in row_core and not found):
                    core = _reply_texts()["lead_conditions"].format(text=core.rstrip("."), conditions=tail)
            elif label is Label.insufficient:
                # факт — не ответ на вопрос: метка «недостаточно данных» объясняется словами
                care = None if service else _care_fact(main)
                if care:
                    # пункта о самой услуге нет, есть только строка таблицы видов помощи (ревизия 9, круг 2):
                    # «прямо об услуге не сказано; вид помощи «…» входит — относится ли услуга к нему, решает СК»
                    core = _reply_texts()["lead_insufficient_care"].format(**care)
                elif keys and not service:
                    # пункта со словами услуги нет: не выдавать «ближайший» пункт о другом за ответ
                    core = _reply_texts()["lead_not_named"].format(what=", ".join(keys))
                else:
                    key = "lead_insufficient_service" if service else "lead_insufficient_fact"
                    core = _reply_texts()[key].format(fact=core.rstrip("."))
            elif label is None and keys and not service and _care_fact(main) is None and \
                    _is_raw(main.text, _verified(main)[0].quote) and not any(_key_hit(s, keys[:1]) for s in statements):
                # главный пункт — сырая цитата не об услуге; формулировка кода (срок договора из карточки,
                # ближайший взнос) — уже прямой ответ, её не заменять
                core = _upper_first(_reply_texts()["lead_not_named"].format(what=", ".join(keys))) + ". " + \
                    _reply_texts()["lead_not_named_below"]
            care = _care_fact(main) if label is not Label.insufficient else None
            if care and core.startswith(f"«{care['raw']}»"):
                # длинное название строки таблицы («Программа "Стандарт". Стационарное обслуживание») — короче
                core = f"«{care['care']}»" + core[len(care["raw"]) + 2:]
    return core


def _row_lead(core: str) -> str | None:
    """Строка таблицы видов помощи / услуг «раздел: услуга — да; условие» → «услуга входит в программу (условие)»
    (шаблоны reply.yaml lead_row_*); не строка таблицы — None. Слова — только из самой строки (проверенной цитаты)."""
    t = _reply_texts()
    rx = t.get("lead_row_rx")
    m = re.match(rx, core.strip()) if rx else None
    if not m:
        return None
    service = m.group("service").strip().strip("«»\"")
    if m.group("value") == "нет":
        return t["lead_row_no"].format(service=service)
    rest = (m.group("rest") or "").strip().rstrip(".")
    return t["lead_row_yes_rest"].format(service=service, rest=rest) if rest else t["lead_row_yes"].format(service=service)


def _care_fact(st: Statement) -> dict[str, str] | None:
    """Утверждение guided «вид помощи «X» входит в программу «P»» (шаблоны care_yes, care_matrix_yes) →
    {care, raw, program}; care — название строки таблицы без повтора программы («Программа "Стандарт". …»,
    reply.yaml care_name_strip). Иное утверждение — None."""
    for key in ("care_matrix_yes", "care_yes"):
        rx = _template_rx(key)
        m = rx.match(st.text) if rx else None
        if m:
            raw = m["care"]
            care = re.sub(_reply_texts().get("care_name_strip") or r"(?!)", "", raw, flags=re.IGNORECASE).strip()
            where = _reply_texts().get("lead_care_where_matrix", "") if key == "care_matrix_yes" else ""
            return {"care": care or raw, "raw": raw, "program": m["program"], "where": where}
    return None


def _is_clinic_in_list(st: Statement) -> bool:
    """Утверждение guided «клиника «X» есть в перечне клиник программы «P»» (шаблоны CLINIC_IN_LIST)."""
    return any((rx := _template_rx(key)) is not None and rx.match(st.text) for key in CLINIC_IN_LIST)


def _clinic_in_list(statements: list[Statement]) -> str | None:
    """Короткая фраза «клиника «X» есть в перечне клиник программы «P»» из проверенного утверждения guided
    о строке перечня (шаблоны CLINIC_IN_LIST); нет такого утверждения — None."""
    for st in statements:
        if not _verified(st):
            continue
        for key in CLINIC_IN_LIST:
            rx = _template_rx(key)
            m = rx.match(st.text) if rx else None
            if m and {"clinic", "program"} <= set(m.groupdict()):
                return _reply_texts()["lead_clinic_in_list"].format(clinic=m["clinic"], program=m["program"])
    return None


def _labelled(label: Label, core: str | None) -> str:
    """«<Метка словами>: <фраза>» (образ ответа заказчика)."""
    t = _reply_texts()
    text = _lower_first(core) if core else t["lead_no_points"]
    return t["lead_label"].format(label=t["label_words"][label.value], text=text.rstrip(".")) + "."


def _limit(lead: str, max_chars: int = 300) -> str:
    """lead — не больше двух фраз и max_chars знаков (обрезка по границе фразы, затем слова)."""
    sents = sentences(lead)
    lead = " ".join(sents[:2])
    if len(lead) > max_chars and len(sents) > 1:
        lead = sents[0]
    if len(lead) > max_chars:
        cut = lead[:max_chars - 1].rfind(" ")
        lead = lead[:cut].rstrip(" ,;:—-") + "…"
    return lead


def _number(cid: str, store: KitStore | None) -> str:
    """Номер договора для подписи (из хранилища; без хранилища — из кода договора)."""
    number = None
    if store is not None:
        with contextlib.suppress(Exception):
            number = store.contract(cid).number
    if not number:
        number = str(int(cid[1:])) if re.fullmatch(r"c\d+", cid) else "?"
    return str(number)


def _caption(cids: list[str], store: KitStore | None) -> str:
    """«Договор № N» или, когда пункт одинаков в нескольких договорах, «Договоры № 5, 6, 7»."""
    t = _reply_texts()
    nums = ", ".join(_number(c, store) for c in cids)
    return t["contract_caption" if len(cids) == 1 else "contracts_caption"].format(number=nums)


def _ask(answer: Answer, store: KitStore | None, question: str | None) -> list[str]:
    """«Что уточнить»: вопросы, затем недостающее; префикс «c01: » → «Договор № 1: »; без дублей и вложенных
    повторов (пункт, целиком входящий в другой, отбрасывается); причина отказа, уже названная в вопросе, не
    переспрашивается; не больше 4."""
    g = _guided_texts()
    known = set()
    if answer.scenario is Scenario.sc01 and _refusal_reason(question):
        known = {normalize(g.get("refusal_missing_reason", "")), normalize(g.get("refusal_ask_reason", ""))}
    items: list[str] = []
    for x in [*answer.questions, *answer.missing]:
        m = _GK_PREFIX.match(x)
        if m:
            x = f"{_caption([m.group(1)], store)}: {x[m.end():]}"
        x = _humanize(x)
        if normalize(x) not in known:
            items.append(x)
    if not items and answer.label is Label.insufficient:
        items = list(_reply_texts().get("ask_insufficient") or [])   # правило 2: что сделать дальше
    items = _dedup(items)
    norm = [normalize(x) for x in items]
    out = [x for i, x in enumerate(items)
           if not any(j != i and norm[i] in norm[j] and len(norm[j]) > len(norm[i]) for j in range(len(items)))]
    return out[:4]


def _single_lead(answer: Answer, question: str | None) -> str:
    """lead одного договора: «<Метка словами>: <одна фраза>» (SC 01) или главный факт (SC 02) + вторая фраза —
    причина отказа из вопроса или просьба выбрать клинику."""
    t = _reply_texts()
    second: str | None = None
    if answer.scenario is Scenario.sc01:
        reason = _refusal_reason(question)
        if reason:
            second = t["lead_reason"].format(reason=reason)
    if any(c.selected is None and c.candidates for c in answer.clinic_choices):
        second = second or t["lead_clinic_choice"]
    core = _core(answer.scenario, answer.label, answer.summary, answer.statements, _q_stems(question),
                 _q_lemmas(question), _key_lemmas(question))
    if answer.label is not None:
        lead = _labelled(answer.label, core)
    elif core:
        lead = core
    elif second:
        lead, second = second, None
    else:
        lead = t["lead_info_empty"]
    return _limit(f"{lead} {second}" if second else lead)


def _group_points(answer: Answer, store: KitStore | None, stems: list[str] | None = None,
                  lems: list[str] | None = None, keys: list[str] | None = None) -> list[ReplyPoint]:
    """ГК: лучшие пункты каждого договора по очереди (первый каждого, затем второй…). Пункт, одинаковый по тексту
    в нескольких договорах, показывается один раз с подписью «Договоры № 5, 6» и ссылками КАЖДОГО договора —
    условие не переносится: у каждого договора своя проверенная ссылка (ADR 0005)."""
    per = [_points(pc.statements, pc.label, pc.contract_id, stems, lems, keys) for pc in answer.per_contract]
    merged: dict[str, tuple[ReplyPoint, list[str]]] = {}
    depth = 0
    while any(depth < len(p) for p in per):
        for p in per:
            if depth >= len(p):
                continue
            pt = p[depth]
            key = normalize(pt.text)
            if key in merged:
                first, cids = merged[key]
                if pt.contract_id not in cids:
                    cids.append(str(pt.contract_id))
                    first.refs = _dedup([*first.refs, *pt.refs])
            elif len(merged) < REPLY_POINTS_MAX:
                merged[key] = (pt.model_copy(), [str(pt.contract_id)])
        depth += 1
    out: list[ReplyPoint] = []
    fmt = _reply_texts()["point_with_contract"]
    order = {pc.contract_id: i for i, pc in enumerate(answer.per_contract)}
    for pt, cids in merged.values():
        cids.sort(key=lambda c: order.get(c, 0))
        pt.text = fmt.format(caption=_caption(cids, store), text=pt.text)
        pt.contract_id = cids[0] if len(cids) == 1 else None
        out.append(pt)
    return out


LEAD_GROUP_MAX = 300


def _group_lead(answer: Answer, points: list[ReplyPoint], store: KitStore | None = None,
                stems: list[str] | None = None, lems: list[str] | None = None,
                keys: list[str] | None = None) -> str:
    """ГК, сводно: одна метка во всех договорах — «<Метка> — по всем договорам группы (N): <фраза>»; разные —
    «По договорам группы: по 3 из 5 — можно; по 2 из 5 — недостаточно данных»; без меток (SC 02) — общий пункт
    всех договоров или «ответ по каждому договору ниже»."""
    t = _reply_texts()
    pcs = answer.per_contract
    total = len(pcs)
    if not points:
        return t["lead_group_empty"]
    counts: dict[Label, int] = {}
    for pc in pcs:
        if pc.label is not None:
            counts[pc.label] = counts.get(pc.label, 0) + 1
    if len(counts) == 1 and counts[next(iter(counts))] == total:
        label = next(iter(counts))
        words = t["label_words"][label.value]
        # фраза — по договорам, где есть проверенные пункты (ревизия w17: бралась из первого договора, а у него пунктов
        # не было — «не найдено подтверждённых условий» над показанным ниже пунктом другого договора)
        cores = [(pc.contract_id, core) for pc in pcs if pc.statements
                 and (core := _core(answer.scenario, label, pc.summary, pc.statements, stems, lems, keys))]
        if not cores:
            text = t["lead_no_points"] if not points else t["lead_group_points_below"]
            return _limit(t["lead_group_same"].format(label=words, n=total, text=text) + ".")
        texts = {normalize(c) for _cid, c in cores}
        if len(cores) == total and len(texts) == 1:
            text = _lower_first(cores[0][1]).rstrip(".")
            return _limit(t["lead_group_same"].format(label=words, n=total, text=text) + ".")
        # пункт есть не во всех договорах или разный — фраза с подписью договора, а не «по всем»
        cid, core = cores[0]
        head = t["lead_group_same_some"].format(label=words, n=total, caption=_caption([cid], store), text="")
        # вторая фраза укорачивается сама: _limit при превышении оставил бы одну первую — без пункта договора
        text = _shorten(_lower_first(core).rstrip("."), max(60, LEAD_GROUP_MAX - len(head) - 1)).rstrip(".")
        return _limit(head + text + ("" if text.endswith("…") else "."), LEAD_GROUP_MAX)
    if counts:
        parts = [t["lead_group_part"].format(n=counts[lab], total=total, label=t["group_label_lower"][lab.value])
                 for lab in sorted(counts, key=lambda lab: -counts[lab])]
        return _limit(t["lead_group"].format(parts="; ".join(parts)))
    if points[0].contract_id is None and total > 1 and len(points[0].refs) >= total:
        text = points[0].text.split(": ", 1)[-1]
        return _limit(t["lead_group_all"].format(n=total, text=_lower_first(text).rstrip(".")) + ".")
    return _limit(t["lead_group_nolabel"])


def _caution(answer: Answer, points: list[ReplyPoint], question: str | None = None) -> str:
    t = _reply_texts()
    if answer.mode is AnswerMode.extractive:
        return t["caution_extractive"]
    out = t["caution"]
    if question and re.search(t.get("oms_rx") or r"(?!)", question, flags=re.IGNORECASE):
        out += t["caution_oms"]            # вопрос об ОМС: документы ДМС об ОМС не говорят (ответ — только по ДМС)
    if any(t["assumption_marker"] in p.text.casefold() for p in points):
        out += t["caution_assumption"]
    elif answer.per_contract:
        out += t["caution_group"]
    return out


def chat_reply(answer: Answer, *, question: str | None = None, store: KitStore | None = None) -> ChatReply:
    """Ответ в чате; названия программ контекста на время сборки — не слова услуги (_CONTEXT_NAMES)."""
    names = [answer.context.program] if answer.context.program else []
    # названия всех программ договоров ответа («Входит ли в ВИП …» в чате без программы) — тоже не услуга
    for cid in [answer.context.contract_id, *(pc.contract_id for pc in answer.per_contract)]:
        if cid and store is not None:
            with contextlib.suppress(Exception):
                names += [p.name for p in store.contract(cid).programs]
    names = tuple(sorted({n for n in names if n}, key=len, reverse=True))   # длинные раньше: «Рязань Стандарт» до «Стандарт»
    token = _CONTEXT_NAMES.set(names)
    token2 = _QUESTION_ALL.set(tuple(_all_lemmas(question)))
    try:
        return _chat_reply(answer, question=question, store=store)
    finally:
        _CONTEXT_NAMES.reset(token)
        _QUESTION_ALL.reset(token2)


def _all_lemmas(question: str | None) -> list[str]:
    """Значимые слова вопроса вместе с обстоятельствами («реабилитация после операции» → и «операция»)."""
    from .index.morph import STOPWORDS, lemmas

    noise = tuple(_reply_texts().get("question_noise") or [])
    return _dedup(w for w in lemmas(question or "") if w not in STOPWORDS and len(w) >= 3 and not w.startswith(noise))


@cache
def _generic_words() -> frozenset[str]:
    """Общие слова процедур и классов услуг (config service_classes: procedures.generic, service_classes.*.generic):
    «анализ», «приём», «лечение» — не признак того, что пункт о самой услуге."""
    from .guided import procedure_cfg

    out = set(procedure_cfg().generic or ())
    for spec in ((load_yaml("service_classes.yaml") or {}).get("service_classes") or {}).values():
        out |= {str(w).casefold() for w in spec.get("generic") or []}
    return frozenset(out)


def _chat_reply(answer: Answer, *, question: str | None = None, store: KitStore | None = None) -> ChatReply:
    """Ответ в чате (ADR 0014) из готового Answer: всё — из проверенных утверждений и цитат этого ответа.

    question — текст вопроса (только чтобы назвать в lead причину отказа из самого вопроса, слайд 12);
    store — для подписи «Договор № N» в контексте ГК. Пункт без проверенной ссылки в ответ не попадает."""
    t = _reply_texts()
    ask = _ask(answer, store, question)
    first_q = _humanize(answer.questions[0]) if answer.questions else ""
    if answer.per_contract:
        points = _group_points(answer, store, _q_stems(question), _q_lemmas(question), _key_lemmas(question))
        lead = _group_lead(answer, points, store, _q_stems(question), _q_lemmas(question), _key_lemmas(question))
        reason = _refusal_reason(question) if answer.scenario is Scenario.sc01 else None
        if reason:
            lead = _limit(f"{lead} {t['lead_reason'].format(reason=reason)}")
    else:
        statements = answer.statements
        if any(c.selected is None and c.candidates for c in answer.clinic_choices):
            # клиника ещё не выбрана менеджером (ADR 0010): «клиника X есть в перечне» по первому кандидату
            # противоречит просьбе выбрать клинику — такие пункты до выбора не показываются (ревизия w16)
            statements = [s for s in statements if not _is_clinic_in_list(s)]
        points = _points(statements, answer.label, stems=_q_stems(question),
                         lems=_q_lemmas(question), keys=_key_lemmas(question))[:REPLY_POINTS_MAX]
        if answer.mode is AnswerMode.extractive:
            lead = t["lead_extractive"]
        elif not points and first_q and answer.label is None:
            # уточняющий вопрос вместо ответа (нет обязательного факта): lead — сам вопрос
            lead = _limit(t["lead_clarify"].format(question=first_q))
            ask = [x for x in ask if normalize(x) != normalize(first_q)]
        else:
            lead = _single_lead(answer, question)
    reply = ChatReply(lead=_humanize(lead), points=points, ask=ask, caution=_caution(answer, points, question))
    _narrow(reply, answer, question)
    return reply


def _narrow(reply: ChatReply, answer: Answer, question: str | None) -> None:
    """Вопрос назван одним видом помощи («а зубы входят?») — к ответу по виду помощи варианты уточнения кнопками
    (reply.yaml narrow_options): менеджер выбирает услугу, ответ по ней точнее. Только если вариантов ещё нет и
    это не уточнение программы / клиники."""
    if reply.options or answer.confidence is Confidence.need_clarification and not reply.points:
        return
    # общие слова («лечение зубов», «сдать анализы») — не уточнение услуги
    lems = set(_q_lemmas(question)) - set(_reply_texts().get("narrow_ignore") or [])
    if not lems:
        return
    t = _reply_texts()
    for group in t.get("narrow_options") or []:
        when = {w.casefold() for w in group.get("when") or []}
        if lems <= when:
            reply.options = [str(x) for x in group.get("options") or []]
            if not reply.points or answer.label is None:
                # пунктов нет или ответ без метки («а анализы?» — случайный пункт об анализе на кальций): первая
                # фраза просит уточнить услугу, найденные пункты остаются ниже
                reply.lead = t["narrow_lead"]
            elif t.get("narrow_ask") and t["narrow_ask"] not in reply.ask:
                reply.ask = [t["narrow_ask"], *reply.ask][:4]
            return
