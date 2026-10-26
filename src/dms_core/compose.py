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
import re
from collections.abc import Iterable
from functools import cache

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
        elif depth == 0 and not re.search(r"(?:^|\s)(?:п|пп|ст|г|\d+(?:\.\d+)*)\.$", text[start:m.start()]):
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
        last = cells[-1]
        if last.casefold() in _YESNO:
            head = cells[0] + (": " + ", ".join(cells[1:-1]) if len(cells) > 2 else "")
            text = f"{head} — {last}"
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
    if label in _BASIS and st.kind is _BASIS[label]:
        return 0, raw
    return 1 + _ORDER.index(st.kind), raw


def _points(statements: list[Statement], label: Label | None, contract_id: str | None = None) -> list[ReplyPoint]:
    """Пункты «что сказано в договоре»: только утверждения с проверенной ссылкой (`docs/rules.md` п. 1), по важности,
    без дублей (одинаковый текст или та же цитата того же источника)."""
    out: list[ReplyPoint] = []
    seen: set[str] = set()
    for st in sorted(statements, key=lambda s: _rank(s, label)):     # sorted устойчив: внутри вида — как было
        cits = _verified(st)
        if not cits:
            continue
        quote = reply_quote(cits[0].quote)
        text = point_text(st.text, cits[0].quote)
        keys = {normalize(text), normalize(quote) + cite_ref(cits[0])}
        if not text or keys & seen:
            continue
        seen |= keys
        out.append(ReplyPoint(text=text, quote=quote or None, refs=_dedup(cite_ref(c) for c in cits),
                              contract_id=contract_id))
    return out


def _main_statement(statements: list[Statement], label: Label | None) -> Statement | None:
    """Главное утверждение для lead: формулировка кода раньше сырого текста пункта, затем по важности."""
    live = [s for s in statements if _verified(s)]
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


def _core(scenario: Scenario, label: Label | None, summary: str, statements: list[Statement]) -> str | None:
    """Одна фраза по существу: основания отказа (SC 01) или главное проверенное утверждение."""
    core = _refusal_core(summary, label) if scenario is Scenario.sc01 else None
    if core is None:
        # «Можно при условиях»: главное — что помощь входит (факт), условия — дальше словами из цитат
        pool = [s for s in statements if s.kind is not StatementKind.condition] if label is Label.conditional else []
        main = _main_statement(pool, None) or _main_statement(statements, label)
        if main is not None:
            core = _first_sentence(point_text(main.text, _verified(main)[0].quote))
            clinic = _clinic_in_list(statements) if label is Label.allowed else None
            if clinic and normalize(clinic) not in normalize(core):
                # «Можно: клиника «X» есть в перечне клиник программы «P»; «вид помощи» — входит …» (слайд 12)
                core = f"{clinic}; {_lower_first(core)}"
            if label is Label.conditional:
                # «Можно при условиях: … входит; условия: по направлению врача» — слова условий из цитат пунктов
                # условий (словарь reply.yaml quote_markers), без них — отсылка к пунктам ниже
                found = _dedup(m for s in statements if s.kind is StatementKind.condition
                               for c in _verified(s)[:1] for m in _markers(c.quote))
                found = [m for m in found if m not in core]
                tail = ", ".join(found[:3]) if found else _reply_texts()["lead_conditions_below"]
                if main.kind is not StatementKind.condition or found:
                    core = _reply_texts()["lead_conditions"].format(text=core.rstrip("."), conditions=tail)
    return core


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
    core = _core(answer.scenario, answer.label, answer.summary, answer.statements)
    if answer.label is not None:
        lead = _labelled(answer.label, core)
    elif core:
        lead = core
    elif second:
        lead, second = second, None
    else:
        lead = t["lead_info_empty"]
    return _limit(f"{lead} {second}" if second else lead)


def _group_points(answer: Answer, store: KitStore | None) -> list[ReplyPoint]:
    """ГК: лучшие пункты каждого договора по очереди (первый каждого, затем второй…). Пункт, одинаковый по тексту
    в нескольких договорах, показывается один раз с подписью «Договоры № 5, 6» и ссылками КАЖДОГО договора —
    условие не переносится: у каждого договора своя проверенная ссылка (ADR 0005)."""
    per = [_points(pc.statements, pc.label, pc.contract_id) for pc in answer.per_contract]
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


def _group_lead(answer: Answer, points: list[ReplyPoint]) -> str:
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
        core = _core(answer.scenario, label, pcs[0].summary, pcs[0].statements)
        text = _lower_first(core).rstrip(".") if core else t["lead_no_points"]
        return _limit(t["lead_group_same"].format(label=t["label_words"][label.value], n=total, text=text) + ".")
    if counts:
        parts = [t["lead_group_part"].format(n=counts[lab], total=total, label=t["group_label_lower"][lab.value])
                 for lab in sorted(counts, key=lambda lab: -counts[lab])]
        return _limit(t["lead_group"].format(parts="; ".join(parts)))
    if points[0].contract_id is None and total > 1 and len(points[0].refs) >= total:
        text = points[0].text.split(": ", 1)[-1]
        return _limit(t["lead_group_all"].format(n=total, text=_lower_first(text).rstrip(".")) + ".")
    return _limit(t["lead_group_nolabel"])


def _caution(answer: Answer, points: list[ReplyPoint]) -> str:
    t = _reply_texts()
    if answer.mode is AnswerMode.extractive:
        return t["caution_extractive"]
    out = t["caution"]
    if any(t["assumption_marker"] in p.text.casefold() for p in points):
        out += t["caution_assumption"]
    elif answer.per_contract:
        out += t["caution_group"]
    return out


def chat_reply(answer: Answer, *, question: str | None = None, store: KitStore | None = None) -> ChatReply:
    """Ответ в чате (ADR 0014) из готового Answer: всё — из проверенных утверждений и цитат этого ответа.

    question — текст вопроса (только чтобы назвать в lead причину отказа из самого вопроса, слайд 12);
    store — для подписи «Договор № N» в контексте ГК. Пункт без проверенной ссылки в ответ не попадает."""
    t = _reply_texts()
    ask = _ask(answer, store, question)
    first_q = _humanize(answer.questions[0]) if answer.questions else ""
    if answer.per_contract:
        points = _group_points(answer, store)
        lead = _group_lead(answer, points)
        reason = _refusal_reason(question) if answer.scenario is Scenario.sc01 else None
        if reason:
            lead = _limit(f"{lead} {t['lead_reason'].format(reason=reason)}")
    else:
        points = _points(answer.statements, answer.label)[:REPLY_POINTS_MAX]
        if answer.mode is AnswerMode.extractive:
            lead = t["lead_extractive"]
        elif not points and first_q and answer.label is None:
            # уточняющий вопрос вместо ответа (нет обязательного факта): lead — сам вопрос
            lead = _limit(t["lead_clarify"].format(question=first_q))
            ask = [x for x in ask if normalize(x) != normalize(first_q)]
        else:
            lead = _single_lead(answer, question)
    return ChatReply(lead=_humanize(lead), points=points, ask=ask, caution=_caution(answer, points))
