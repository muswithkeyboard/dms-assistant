"""Ответ по документу, прикреплённому к чату (ADR 0016): разбор файла на пункты, поиск по пунктам, ответ цитатами.

Вложение — НЕ документ договора: оно принадлежит одному чату, в поиск договора не попадает (ADR 0005 — условия
не переносятся), меток «можно / нельзя» по нему нет (документ не разобран как договор: нет программ, перечня,
матрицы). Ответ собирает код, без модели (`docs/rules.md` п. 1–2):
  * разбор — тот же, что у запасного адаптера (adapters/generic.READERS): docx с автонумерацией Word
    (numbering.xml) и таблицами строками, txt / pdf — абзацы с номером из начала строки, xlsx — строка листа;
  * поиск — BM25 по леммам пунктов вложения (index/bm25, index/morph) с расширением запроса синонимами видов помощи
    (index/search.expand_query); пункт берётся, только если в нём есть хотя бы одна значимая основа вопроса
    (guided.key_stems без вопросительных слов texts_ru/attachment.yaml question_words) — иначе BM25 находил бы
    пункты по одному служебному слову;
  * вопрос о разделе («Что сказано в разделе «X»?» — кнопки после загрузки) — пункты этого раздела по порядку;
  * цитата — дословный фрагмент текста пункта (guided.fragment); проверка — citations.locate_quote по тексту
    пункта: непроверенная цитата не показывается, пункт без цитаты в ответ не попадает (`docs/rules.md` п. 1).
Тексты — config/texts_ru/attachment.yaml.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

from .adapters.base import text_hash
from .adapters.generic import READERS, _is_heading
from .citations import locate_quote
from .config import load_yaml
from .constants import REPLY_POINTS_MAX, REPLY_QUOTE_CHARS, AnswerMode, Confidence, Scenario
from .index.bm25 import BM25
from .index.morph import lemmas
from .index.search import clause_terms, expand_query
from .schemas import Answer, AnswerContext, ChatReply, Citation, Clause, Document, ReplyPoint, Statement

POINTS_MAX = min(5, REPLY_POINTS_MAX)   # пунктов вложения в ответе (бриф w18: до 5)
SEARCH_K = 30                           # кандидатов BM25 до проверки основ вопроса
SECTIONS_SHOWN = 4                      # разделов в сообщении о загрузке
OPTIONS_MAX = 3                         # кнопок-примеров вопросов
SECTION_TITLE_MAX = 120                 # длиннее — это пункт, а не заголовок раздела
_TOP_NUMBER = re.compile(r"^(?:\d{1,2}|[IVXLC]{1,6})\.?$")   # «2», «2.», «IV.» (римские — разделы программ)
_LETTERS = re.compile(r"[a-zа-яё]", re.IGNORECASE)
_FOOTNOTE = re.compile(r"\[\^\d+\]")
_ROMAN_TEXT = re.compile(r"^\s*[IVXLC]{1,6}\.\s+(?=\S)")
_GUILLEMETS = re.compile(r"«([^»]{2,200})»")
_WORD = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)


@cache
def texts() -> dict[str, Any]:
    return dict(load_yaml("texts_ru/attachment.yaml") or {})


def _t(key: str, **kw: Any) -> str:
    return str(texts().get(key, key)).format(**kw)


def plural(n: int, forms: Sequence[str]) -> str:
    """1 пункт, 2 пункта, 5 пунктов (формы — texts_ru/attachment.yaml items)."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


# ---------------------------------------------------------------- разбор файла

def parse_file(path: Path, fmt: str, title: str, doc_id: str, contract_id: str | None = None) -> list[Clause]:
    """Файл → пункты тем же кодом, что у запасного адаптера (generic.parse): id «<doc_id>#p<i>» у абзацев,
    «<doc_id>#t<N>r<R>…» у строк таблиц, путь «<название> › <номер>». Ошибка чтения — InputError читателя
    (или исключение разбора) наружу: сервис превращает её в сообщение по-русски."""
    reader = READERS.get(fmt)
    if reader is None:
        raise ValueError(f"формат {fmt} не разбирается")
    out: list[Clause] = []
    for i, (number, text, footnotes, page, suffix, cpath) in enumerate(reader(path, title)):
        if not text.strip():
            continue
        out.append(Clause(
            id=f"{doc_id}{suffix}" if suffix else f"{doc_id}#p{i}", doc_id=doc_id, contract_id=contract_id,
            number=number, path=cpath or (f"{title} › {number}" if number else title), text=text,
            footnotes=footnotes, page=page, text_hash=text_hash(text)))
    return out


def rebind(clauses: list[Clause], doc_id: str, contract_id: str) -> list[Clause]:
    """Пункты вложения → пункты документа договора (настройка «добавлять в документы договора»): тот же текст,
    свой id документа и договор. Проверка изоляции — KitStore.add_documents."""
    out = []
    for c in clauses:
        suffix = c.id[len(c.doc_id):] if c.id.startswith(c.doc_id) else "#" + c.id
        out.append(c.model_copy(update={"id": f"{doc_id}{suffix}", "doc_id": doc_id, "contract_id": contract_id}))
    return out


# ---------------------------------------------------------------- разделы документа

def _is_table(c: Clause) -> bool:
    return "#t" in c.id[len(c.doc_id):] or "!" in c.id[len(c.doc_id):]


def sections(clauses: list[Clause]) -> list[tuple[str, int, int]]:
    """Разделы документа: (название, индекс заголовка, конец раздела). Заголовок раздела — пункт верхнего уровня
    с коротким текстом («2. Амбулаторная помощь»); если таких меньше двух — короткие абзацы-заголовки без номера
    (generic._is_heading: «Исключения из программы»). Раздел — до следующего заголовка. Строки таблиц —
    не заголовки."""
    def numbered(c: Clause) -> bool:
        # заголовок — со словами: «8 (800) 333-44-19» (телефон с номером в начале строки) — не раздел;
        # «IV. ИСКЛЮЧЕНИЯ ИЗ ПРОГРАММЫ:» без автонумерации — римский номер набран текстом
        if _is_table(c) or len(c.text) > SECTION_TITLE_MAX or len(_LETTERS.findall(c.text)) < 3:
            return False
        return bool(c.number and _TOP_NUMBER.match(c.number)) or (c.number is None and bool(_ROMAN_TEXT.match(c.text)))

    def heading(c: Clause) -> bool:
        return c.number is None and not _is_table(c) and len(c.text) <= 80 and _is_heading(c.text) \
            and bool(_WORD.search(c.text))

    starts = [i for i, c in enumerate(clauses) if numbered(c)]
    if len(starts) < 2:
        starts = [i for i, c in enumerate(clauses) if heading(c)]
    out = []
    for k, i in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(clauses)
        text = _ROMAN_TEXT.sub("", clauses[i].text) if clauses[i].number is None else clauses[i].text
        name = re.sub(r"\s+", " ", _FOOTNOTE.sub("", text)).strip().rstrip(":.").strip()
        if name.isupper():
            name = name[0] + name[1:].lower()        # «ОБЩИЕ ПОЛОЖЕНИЯ» → «Общие положения» (сравнение — без регистра)
        if name:
            out.append((name, i, end))
    return out


def _short(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------- поиск по пунктам вложения

_INDEX: OrderedDict[tuple[str, int], BM25] = OrderedDict()
_INDEX_MAX = 8                          # вложений в кэше индексов (вложение — один документ, индекс маленький)
_LOCK = threading.Lock()


def _index(key: str, clauses: list[Clause]) -> BM25:
    k = (key, len(clauses))
    with _LOCK:
        got = _INDEX.get(k)
        if got is not None:
            _INDEX.move_to_end(k)
            return got
    bm = BM25([clause_terms(c) for c in clauses])
    with _LOCK:
        _INDEX[k] = bm
        while len(_INDEX) > _INDEX_MAX:
            _INDEX.popitem(last=False)
    return bm


@cache
def _ignored() -> frozenset[str]:
    return frozenset(lemmas(" ".join(str(w) for w in texts().get("question_words", [])), keep_stopwords=True))


def question_stems(question: str) -> list[str]:
    """Значимые основы вопроса: guided.key_stems без вопросительных и служебных слов (attachment.yaml)."""
    from .guided import key_stems

    ign = _ignored()
    kept = " ".join(lm for lm in lemmas(question) if lm not in ign)
    return key_stems(kept)


def _words(s: str) -> list[str]:
    return [w.casefold().replace("ё", "е") for w in _WORD.findall(s or "")]


def _hits(stems: list[str], s: str) -> int:
    words = _words(s)
    return sum(1 for st in stems if any(w.startswith(st) for w in words))


def search(question: str, clauses: list[Clause], key: str, k: int = POINTS_MAX) -> list[Clause]:
    """Пункты вложения по вопросу: кандидаты BM25 (слова вопроса и синонимы вида помощи), из них — только с
    основами вопроса; порядок — больше основ вопроса, затем оценка BM25."""
    if not clauses:
        return []
    stems = question_stems(question)
    if not stems:
        return []
    ign = _ignored()
    query = {lm: w for lm, w in expand_query(question).items() if lm not in ign}
    ranked = []
    for i, score in _index(key, clauses).top(query, SEARCH_K):
        c = clauses[i]
        h = _hits(stems, f"{c.number or ''} {c.path} {c.text}")
        if h:
            ranked.append((-h, -score, i))
    # пункты о самой услуге вопроса (по леммам, как в ответе по договору — compose._q_lemmas / _lemma_hit): «удалить
    # зуб» — без «чистки зубов»; ни одного такого — все найденные (ревизия точности w18)
    from .compose import _lemma_hit, _q_lemmas
    from .index.morph import lemmas

    want = _q_lemmas(question)
    if want:
        def rel(i: int) -> int:
            have = set(lemmas(clauses[i].text))
            return sum(_lemma_hit(w, have) for w in want)
        need = min(2, len(want))
        if any(rel(i) >= need for _a, _b, i in ranked):
            ranked = [x for x in ranked if rel(x[2]) >= need]
    ranked.sort()
    # одинаковый текст в документе повторяется (у каждой программы — своя копия общих пунктов): в ответе — один раз
    out: list[Clause] = []
    seen: set[str] = set()
    for _, _, i in ranked:
        c = clauses[i]
        if c.text_hash in seen:
            continue
        seen.add(c.text_hash)
        out.append(c)
        if len(out) >= k:
            break
    return out


def section_asked(question: str, clauses: list[Clause]) -> list[Clause] | None:
    """Вопрос о разделе («…в разделе «X»?»): пункты раздела X по порядку (до POINTS_MAX; раздел из одного
    заголовка — сам заголовок). None — в вопросе нет названия раздела этого документа."""
    names = [m.group(1).strip().casefold() for m in _GUILLEMETS.finditer(question)]
    if not names:
        return None
    for name, start, end in sections(clauses):
        if name.casefold() in names or _short(name).casefold() in names:
            body = clauses[start + 1:end] or clauses[start:start + 1]
            return body[:POINTS_MAX]
    return None


# ---------------------------------------------------------------- цитаты и ответ

def _where(c: Clause, title: str) -> str:
    """Где пункт в документе — словами, без служебных id: путь без названия документа и без номера."""
    p = c.path or ""
    for prefix in (f"{title} › ", title):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    if c.number and p.endswith(c.number):
        p = p[: -len(c.number)].rstrip(" ›")
    return p.strip(" ›")


def number_of(c: Clause) -> str | None:
    """Номер пункта для показа: маркер списка Word («•», «–») — не номер."""
    return c.number if c.number and _WORD.search(c.number) else None


def point_text(c: Clause, title: str) -> str:
    where, number = _where(c, title), number_of(c)
    if number:
        return _t("point_number_in", number=number, where=where) if where else _t("point_number", number=number)
    if _is_table(c):
        return _t("point_table", where=where) if where else _t("point_plain")
    return _t("point_plain_in", where=where) if where else _t("point_plain")


def _cut(quote: str, limit: int = REPLY_QUOTE_CHARS) -> str:
    """Цитата для пункта ответа: не длиннее limit, по границе слова (остаётся подстрокой проверенной цитаты)."""
    if len(quote) <= limit:
        return quote
    cut = quote.rfind(" ", 0, limit)
    return quote[: cut if cut > limit // 2 else limit].rstrip(" ,;:")


def cite(c: Clause, doc: Document, stems: list[str]) -> Citation | None:
    """Дословный фрагмент пункта, проверенный по тексту пункта (locate_quote). Не нашёлся — None: такой пункт
    в ответ не попадает (`docs/rules.md` п. 1)."""
    from .guided import fragment

    quote = fragment(c.text, stems).strip()
    if not quote:
        return None
    at = locate_quote(quote, c.text)
    if at is None:
        return None
    # строка таблицы — без table_ref: где она в документе, говорит текст пункта ответа («Строка таблицы (табл. 1…)»)
    return Citation(doc_id=doc.id, doc_title=doc.title, contract_id=None, clause_number=number_of(c), page=c.page,
                    quote=quote, verified=True, offsets=at, ref=c.id)


def _context(ctx: Any) -> AnswerContext:
    if ctx is None:
        return AnswerContext()
    return AnswerContext(group_id=getattr(ctx, "group_id", None), contract_id=getattr(ctx, "contract_id", None),
                         program=getattr(ctx, "program", None), date=getattr(ctx, "date", None))


def answer(question: str, doc: Document, clauses: list[Clause], context: Any = None) -> Answer:
    """Ответ на вопрос только по пунктам вложения. Метки нет; статус — «подтверждено» (есть проверенные цитаты)
    или «данных недостаточно» (ничего не нашлось)."""
    stems = question_stems(question)
    picked = section_asked(question, clauses)
    if picked is None:
        picked = search(question, clauses, doc.id)
    statements: list[Statement] = []
    points: list[ReplyPoint] = []
    for c in picked:
        cit = cite(c, doc, stems)
        if cit is None:
            continue
        text = point_text(c, doc.title)
        statements.append(Statement(text=text, citations=[cit]))
        points.append(ReplyPoint(text=text, quote=_cut(cit.quote), refs=[c.id]))
    lead = _t("found" if points else "not_found", title=doc.title)
    caution = _t("caution")
    return Answer(
        scenario=Scenario.general, context=_context(context), summary=lead, label=None,
        confidence=Confidence.confirmed if points else Confidence.insufficient,
        statements=statements, limitations=[caution],
        reply=ChatReply(lead=lead, points=points, caution=caution),
        mode=AnswerMode.extractive)


def intro(doc: Document, clauses: list[Clause], context: Any = None, added_to: str | None = None) -> Answer:
    """Сообщение ассистента сразу после загрузки: сколько пунктов, разделы, 2–3 кнопки-примера вопросов по
    названиям разделов. Без модели. Утверждение «Разделы документа» — со ссылками на пункты-заголовки
    (проверенные цитаты): по ним интерфейс открывает пункт вложения."""
    secs = sections(clauses)
    # разделы повторяются (у каждой программы документа — свои «I. Общие положения»): название — один раз
    names = list(dict.fromkeys(_short(name) for name, _, _ in secs))
    shown = ", ".join(names[:SECTIONS_SHOWN])
    if len(names) > SECTIONS_SHOWN:
        shown += " " + _t("loaded_more", n=len(names) - SECTIONS_SHOWN)
    lead = _t("loaded", title=doc.title, n=len(clauses), items=plural(len(clauses), texts()["items"]),
              sections=_t("loaded_sections", sections=shown) if names else "")
    cites = [cit for _, start, _ in secs[:SECTIONS_SHOWN] if (cit := cite(clauses[start], doc, [])) is not None]
    statements = [Statement(text=_t("sections_statement", title=doc.title), citations=cites)] if cites else []
    caution = _t("loaded_hint") + (" " + _t("added", contract=added_to) if added_to else "")
    options = [_t("section_question", section=n) for n in names[:OPTIONS_MAX]]
    return Answer(
        scenario=Scenario.general, context=_context(context), summary=lead, label=None,
        confidence=Confidence.confirmed, statements=statements, limitations=[caution],
        reply=ChatReply(lead=lead, caution=caution, options=options, service=True), mode=AnswerMode.extractive)
