"""Режим guided: ответ ведёт код, модель необязательна (ADR 0009, план §6.4, CONTRACTS §7).

По сценарию и слотам код сам вызывает инструменты ОДНОГО договора (тот же исполнитель `bind_tools`, что у агента:
договор привязан замыканием, ADR 0005) и собирает DraftAnswer. Дальше движок идёт обычным путём
citations.verify → decide_label → compose, поэтому всё, что здесь собрано, ещё раз проверяется кодом.

Правила сборки (`docs/rules.md` п. 1–3а):
- каждое утверждение подтверждается своим фрагментом (claim-level grounding): цитата — дословный фрагмент пункта
  из результата инструмента (`fragment`), строки перечня клиник (название клиники) или строки матрицы; выводы из
  разных пунктов в одно утверждение не склеиваются;
- `supports` ставится только утверждению, которое и есть доказательство проверки;
- absent («нет в документах») — только когда инструмент вернул документ нужного типа: строка матрицы этой программы
  с present=False; перечень клиник программы получен, а клиники в нём нет; исключения программы найдены и
  просмотрены. Иначе not_found («не нашли»), из которого «нельзя» не бывает;
- нет данных → missing / questions, а не вывод. Тексты пунктов — данные: инструкции внутри них ни на что не влияют
  (код не читает их как команды, а только ищет в них слова услуги).

SC 01 (ТЗ п. 5.1): программа → care_matrix → вид помощи (config/care_types.yaml) → исключения (search_clauses) →
клиника (clinics_query) → условия из строки клиники (франшиза, «по направлению», примечание о доступе) →
альтернативы (clinics_pick, допущение A6). SC 02: взносы — payments_next по дате контекста; клиники —
clinics_query; сравнение программ — compare_programs; остальное — search_clauses (+ care_matrix при виде помощи).

Модель (необязательно, без инструментов, строгая маленькая схема, structured_override("on")):
`fill_slots` — один короткий вызов, если правила не нашли нужный слот; слот модели принимается, только если
опирается на текст вопроса (route.grounded). `summary` — один короткий вызов по уже собранным утверждениям;
compose всё равно проверяет его числа и названия (checked_summary). Без модели — шаблоны config/texts_ru/ui.yaml.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from .agent import CALC_TOOLS, AgentRun, ToolRecord, extract_json, summarize
from .config import load_yaml, prompt, prompt_version
from .constants import Scenario, StatementKind
from .index.morph import STOPWORDS, lemmas
from .llm.base import LLM, LlmError, Message, Usage
from .schemas import (
    ChatContext,
    Checks,
    Clause,
    ClinicChoice,
    ClinicRow,
    DraftAlternative,
    DraftAnswer,
    DraftCitation,
    DraftContradiction,
    DraftStatement,
)
from .store.db import KitStore, program_matches
from .tools import registry
from .tools._common import resolve
from .tools.clinics import _care_phrase_sets
from .tools.registry import collect_refs

QUOTE_MAX = 280            # длина фрагмента цитаты (citations: на экран идёт ≤ 300 знаков)
QUOTE_MIN_CHARS = 40       # короче — фрагмент расширяется соседним предложением (verify требует ≥ 25 знаков, 4 слова)
SEARCH_K = 5               # пунктов поиска на справочный вопрос
EXCL_K = 15                # пунктов поиска по исключениям
LISTING_LIMIT = 20         # строк перечня клиник на запрос
CLINIC_ROWS_MAX = 50       # строк одной клиники по названию (у сети — строка на каждый адрес; предел инструмента)
ALT_K = 5                  # альтернатив (clinics_pick)
SLOTS_MAX_TOKENS = 200     # бюджет вызова модели на слоты
SUMMARY_MAX_TOKENS = 120   # бюджет вызова модели на краткий вывод
PROMPT_SLOTS = "guided_slots"
# Тема «взносы»: условия оплаты по графику, уточнение суммы, последствия неуплаты (общий запрос, не под кейс)
PAYMENT_QUERY = "страховой взнос оплата срок уплаты неуплата очередного взноса сумма взноса уточняется"
PROMPT_SUMMARY = "guided_summary"

# Слова, после которых упоминание услуги — отрицание («без стоматологии», «кроме МРТ», «не входит»).
NEGATIONS = frozenset({"без", "кроме", "не", "исключением", "исключая"})
# Темы справочного вопроса SC 02
_PAY = re.compile(r"взнос\w*|плат[её]ж\w*|срок\w*\s+оплат\w*|оплатить\s+до", re.IGNORECASE)
_CLINIC_Q = re.compile(r"клиник\w*|медцентр\w*|\bлпу\b|\bгде\b|\bкуда\b", re.IGNORECASE)
_COMPARE = re.compile(r"сравн\w*|отлича\w*|различа\w*|разниц\w*|что\s+(?:пропад|изменит)\w*", re.IGNORECASE)
_FRANCHISE_Q = re.compile(r"франшиз\w*", re.IGNORECASE)
# Примечание о доступе в строке клиники → условие словаря Checks.conditions
_NOTE_KIND = (("franchise", re.compile(r"франшиз", re.IGNORECASE)),
              ("approval", re.compile(r"согласован", re.IGNORECASE)),
              ("zone", re.compile(r"\bзон\w*|мкад|\bкм\b|в\s+пределах", re.IGNORECASE)),
              ("referral", re.compile(r"направлени|по\s+записи", re.IGNORECASE)))
_SENT = re.compile(r"[^.;!?\n]+[.;!?]?")
_WORD = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
# Слова, которые есть почти в каждом пункте ДМС и услугу не отличают (по ним совпадение не считается).
_GENERIC = frozenset({"помощь", "медицинский", "услуга", "обслуживание", "программа", "договор", "страхование",
                      "застрахованный", "лечение", "проведение", "оказание", "прием", "врач", "консультация",
                      # слова самого вопроса («Есть ли франшиза?»): основа «есть» требовалась во фрагменте пункта
                      # и отсекала все пункты о франшизе (ревизия w18)
                      "есть", "быть", "иметься"})


@cache
def _t() -> dict[str, str]:
    # формулировки выбора клиники менеджером (ADR 0010) — отдельный файл: ui.yaml ведёт этап интерфейса
    picked = dict((load_yaml("texts_ru/clinic_pick.yaml") or {}).get("guided") or {})
    # справки выбора строки матрицы (этап w12-care) — config/texts_ru/care_pick.yaml
    care = dict((load_yaml("texts_ru/care_pick.yaml") or {}).get("guided") or {})
    return {**picked, **care, **dict((load_yaml("texts_ru/ui.yaml") or {}).get("guided") or {})}


def text(key: str, **kw: Any) -> str:
    """Шаблон config/texts_ru/ui.yaml guided.<key> (или clinic_pick.yaml guided.<key>) с подстановкой значений из
    инструментов и контекста."""
    return _t().get(key, key).format(**kw)


# ---------------------------------------------------------------- слова и фрагменты

def _stem(lem: str) -> str:
    """Основа для сравнения со словами текста: «стоматология» ↔ «стоматологии», «зубной» ↔ «зубного».
    У прилагательных окончание («-ой / -ый / -ий») отбрасывается целиком: «половой» → «полов» ↔ «половым»."""
    if len(lem) > 5 and lem.endswith(("ой", "ый", "ий")):
        return lem[:min(len(lem) - 2, 6)]
    if len(lem) > 7 and lem.endswith("олог"):
        # специалист ↔ область: «косметолог» ↔ «косметологии», но не «косметической целью»
        return lem[:-1]
    return lem if len(lem) <= 4 else lem[:min(len(lem) - 1, 6)]


def key_stems(phrase: str) -> list[str]:
    """Основы значимых слов фразы (без служебных и общих слов ДМС)."""
    out: list[str] = []
    for lem in lemmas(phrase):
        if lem in STOPWORDS or lem in _GENERIC or len(lem) < 3 and not lem.isdigit():
            continue
        s = _stem(lem)
        if s not in out:
            out.append(s)
    return out


def _words(s: str) -> list[str]:
    return [w.casefold().replace("ё", "е") for w in _WORD.findall(s or "")]


def phrase_hit(stems: list[str], s: str, *, negation_aware: bool = True) -> bool:
    """Все основы фразы есть среди слов текста; упоминание сразу после «без / кроме / не» не считается."""
    if not stems:
        return False
    words = _words(s)
    for st in stems:
        ok = False
        for i, w in enumerate(words):
            if w.startswith(st) and not (negation_aware and i and words[i - 1] in NEGATIONS):
                ok = True
                break
        if not ok:
            return False
    return True


def _hits(stems: list[str], s: str) -> int:
    words = _words(s)
    return sum(1 for st in stems if any(w.startswith(st) for w in words))


def fragment(full: str, stems: list[str]) -> str:
    """Дословный фрагмент пункта для цитаты: предложение с наибольшим числом основ, при нужде — соседние
    предложения; длиннее QUOTE_MAX — окно вокруг первого совпадения по границам слов. Всегда подстрока текста."""
    s = (full or "").strip()
    if len(s) <= QUOTE_MAX:
        return s
    spans = [(m.start(), m.end()) for m in _SENT.finditer(s) if m.group().strip()]
    if not spans:
        return s[:QUOTE_MAX].rsplit(" ", 1)[0]
    best = max(range(len(spans)), key=lambda i: (_hits(stems, s[spans[i][0]:spans[i][1]]), -i))
    a, b = spans[best]
    j = best
    while (b - a < QUOTE_MIN_CHARS or len(s[a:b].split()) < 5) and j + 1 < len(spans):
        j += 1
        b = spans[j][1]
    if b - a > QUOTE_MAX:
        low = s[a:b].casefold().replace("ё", "е")
        pos = min((low.find(st) for st in stems if low.find(st) >= 0), default=0)
        a = a + max(0, pos - 80)
        if a > 0 and s[a - 1] != " ":
            nxt = s.find(" ", a)
            a = nxt + 1 if 0 <= nxt < b else a
        b = min(b, a + QUOTE_MAX)
        if b < len(s) and s[b:b + 1] != " ":
            cut = s.rfind(" ", a, b)
            b = cut if cut > a else b
    return s[a:b].strip()


def _prog_eq(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.casefold() == b.casefold() or program_matches(a, b) or program_matches(b, a)


def _date_ru(iso: str | None) -> str:
    try:
        return dt.date.fromisoformat(str(iso)).strftime("%d.%m.%Y")
    except ValueError:
        return str(iso)


def _money(x: float | None) -> str:
    if x is None:
        return ""
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


# ---------------------------------------------------------------- прогон одного договора

def bound_executor(cid: str, st: KitStore) -> Any:
    """Исполнитель инструментов, привязанный к договору, как bind_tools (договор подставляет код, лишние
    параметры отвергаются), но без обрезки результата под контекст модели (`registry._fit`): в guided результат
    читает код, а обрезанный список строк матрицы дал бы ложное «не нашли»."""
    st.contract(cid)                       # KeyError, если договора нет

    def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
        problem = registry._check_args(name, args) if name in registry._TOOLS else f"нет инструмента {name}"
        if problem:
            return {"error": problem}
        try:
            return registry._TOOLS[name][0](cid, st, **args)
        except (ValueError, KeyError, TypeError) as exc:
            return {"error": f"{name}: {exc}"}

    return execute


@dataclass
class _Run:
    cid: str
    st: KitStore
    execute: Any
    records: list[ToolRecord] = field(default_factory=list)
    statements: list[DraftStatement] = field(default_factory=list)
    alternatives: list[DraftAlternative] = field(default_factory=list)
    looked_at: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    summary_parts: list[str] = field(default_factory=list)
    contradictions: list[DraftContradiction] = field(default_factory=list)
    clinic_choice: ClinicChoice | None = None     # кандидаты клиники для выбора менеджером (ADR 0010)
    _all: list[Clause] | None = None
    _clauses: dict[str, Clause] | None = None
    _sections: Any = None
    _scope: dict[str, list[Clause]] = field(default_factory=dict)

    def all_clauses(self, doc_types: list[str] | None = None) -> list[Clause]:
        """Пункты договора в порядке документа — читаются из хранилища один раз на прогон (круг 2 ревизии w16:
        в ГК из 100 договоров кэш хранилища вмещает не все договоры, и каждое чтение было новым разбором)."""
        if self._all is None:
            self._all = self.st.clauses(self.cid)
        if doc_types is None:
            return self._all
        docs = {d.id for d in self.st.documents(self.cid) if d.doc_type.value in set(doc_types)}
        return [c for c in self._all if c.doc_id in docs]

    def sections(self) -> _Sections:
        """Разделы пунктов договора (заголовки по пути, номеру и предшествующие) — строится один раз на прогон."""
        if self._sections is None:
            self._sections = _Sections(self.all_clauses())
        return self._sections

    def scope(self, program: str) -> list[Clause]:
        """Пункты, где ищется услуга: программа, договор и доп. соглашения (config/verdict_rules.yaml
        absent_sources.exclusion) — этой программы или общие (program=None). Чужие программы не участвуют."""
        if program not in self._scope:
            types = (load_yaml("verdict_rules.yaml") or {}).get("absent_sources", {}).get("exclusion") or \
                ["program", "contract", "amendment"]
            self._scope[program] = [c for c in self.all_clauses(list(types))
                                    if c.program is None or program_matches(c.program, program)]
        return self._scope[program]

    def seen_refs(self) -> set[str]:
        return {r for rec in self.records for r in rec.refs}

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        """Вызов инструмента договора и запись прогона (ToolRecord — вход citations.verify, как у агента)."""
        args = {k: v for k, v in args.items() if v not in (None, "")}
        res = self.execute(tool, args)
        err = res.get("error") if isinstance(res, dict) else "не JSON"
        self.records.append(ToolRecord(
            name=tool, args=dict(args), refs=set() if err else collect_refs(res), error=err,
            summary={} if err else summarize(tool, res),
            result=dict(res) if tool in CALC_TOOLS and not err else None))
        return {} if err else res

    def clause(self, ref: str) -> Clause | None:
        if self._clauses is None:
            self._clauses = {c.id: c for c in self.all_clauses()}
        return self._clauses.get(ref)

    def say(self, statement: str, cites: list[DraftCitation], *, kind: StatementKind = StatementKind.fact,
            supports: list[str] | None = None) -> None:
        if not cites:
            return
        if _is_heading(statement) and all(_is_heading(c.quote or statement) for c in cites):
            return                           # заголовок раздела («VII. … ФРАНШИЗЫ») — не условие и не факт (ревизия 9)
        key = (statement, tuple((c.ref, c.quote) for c in cites))
        for i, old in enumerate(self.statements):
            if (old.text, tuple((c.ref, c.quote) for c in old.citations)) == key:
                # то же утверждение с той же цитатой — одно; проверки, которые оно подтверждает, объединяются
                merged = list(dict.fromkeys([*old.supports, *(supports or [])]))
                self.statements[i] = old.model_copy(update={"supports": merged})
                return
        self.statements.append(DraftStatement(text=statement, kind=kind, citations=cites,
                                              supports=list(supports or [])))

    def cite_clause(self, ref: str, stems: list[str]) -> tuple[DraftCitation, str] | None:
        """(ссылка на пункт с дословным фрагментом, фрагмент) — фрагмент берётся из полного текста пункта."""
        cl = self.clause(ref)
        if cl is None:
            return None
        frag = fragment(cl.text, stems)
        return (DraftCitation(ref=ref, quote=frag), frag) if frag else None


def _is_heading(s: str) -> bool:
    """Заголовок раздела, а не условие: «II. ФРАНШИЗА», «ПОРЯДОК ОБРАЩЕНИЯ» — в основном заглавные буквы или
    короткая строка с римским номером. Из заголовка условие не выводится (ревизия 9: «с франшизой» из заголовка)."""
    t = s.strip()
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        return True
    return bool(re.match(r"^[IVXLC]+\.\s", t)) and len(t) < 120


def _clause_kind(s: str) -> StatementKind:
    """Вид найденного фрагмента. Условие — только явная оговорка о порядке оказания: франшиза, направление врача или
    страховщика, согласование со страховщиком, гарантийное письмо, «при условии». Слова «по согласованию» без
    страховщика (рассылка полисов, график работы) условием помощи не считаются (ревизия 9)."""
    low = s.casefold()
    if re.search(r"франшиз|по\s+направлени\w*\s+(?:лечащего\s+|врач|страховщик|застрахов)|"
                 r"согласовани\w*\s+(?:со?\s+)?страховщик|гарантийн\w*\s+письм|при\s+условии", low):
        return StatementKind.condition
    if re.search(r"не\s+более|лимит|не\s+оплачива|исключ|не\s+входит|не\s+является|ограничен", low):
        return StatementKind.limitation
    return StatementKind.fact


def _search_statements(run: _Run, query: str, program: str | None, stems: list[str], k: int = SEARCH_K,
                       need_hit: bool = True, doc_types: list[str] | None = None,
                       abroad: bool | None = None) -> int:
    """Найденные пункты → утверждения «текст = дословный фрагмент» (ничего не пересказывается). abroad задан —
    только пункты с тем же признаком «помощь за рубежом» (_Sections.abroad): вопросу о помощи в России справка
    из раздела о загранице (ГК 3, п. 3.5) не показывается, и наоборот."""
    res = run.call("search_clauses", query=query, program=program, k=k, doc_types=doc_types)
    n = 0
    for h in res.get("hits") or []:
        if abroad is not None and (cl := run.clause(h["ref"])) is not None and run.sections().abroad(cl) != abroad:
            continue
        got = run.cite_clause(h["ref"], stems)
        if got is None:
            continue
        dc, frag = got
        if need_hit and stems and _hits(stems, frag) < min(2, len(stems)):
            continue
        if _is_heading(frag):                # заголовок раздела — не содержание пункта
            continue
        run.say(frag, [dc], kind=_clause_kind(frag))
        n += 1
        if _LEADS_ON.search(frag):
            n += _continuation(run, h["ref"])
    return n


# Пункт обрывается перед содержанием («Страховая сумма … составляет:») — значение в следующих строках таблицы
_LEADS_ON = re.compile(r"(?::|\bсоставля\w+|\bследующ\w+)\s*$", re.IGNORECASE)
CONTINUATION_MAX = 3


def _continuation(run: _Run, ref: str) -> int:
    """Продолжение пункта-обрывка (круг 2 ревизии w16): следующие за ним строки без номера того же документа
    (строки таблицы «к п. 2.2», абзацы перечня) — до первого пункта с номером, не больше CONTINUATION_MAX. Строки
    берутся инструментом get_clause по пути — ссылки попадают в прогон и проходят citations.verify."""
    cls = run.all_clauses()
    ids = [c.id for c in cls]
    if ref not in ids:
        return 0
    i = ids.index(ref)
    n = 0
    for cl in cls[i + 1:i + 1 + CONTINUATION_MAX]:
        if cl.number is not None or cl.doc_id != cls[i].doc_id:
            break
        if cl.id not in collect_refs(run.call("get_clause", path=cl.path)):
            break
        got = run.cite_clause(cl.id, [])
        if got is None or _is_heading(got[1]):
            break
        run.say(got[1], [got[0]], kind=_clause_kind(got[1]))
        n += 1
    nxt = cls[i + 1 + CONTINUATION_MAX] if n == CONTINUATION_MAX and i + 1 + CONTINUATION_MAX < len(cls) else None
    if nxt is not None and nxt.number is None and nxt.doc_id == cls[i].doc_id:
        # перечень длиннее показанного — неполный список не выдаётся за полный (ревизия круга 2)
        run.missing.append(text("continuation_more", number=cls[i].number or cls[i].path))
    return n


# ---------------------------------------------------------------- матрица видов помощи

def _care_stem_sets(care: str | None, service: str | None) -> list[list[str]]:
    """Фразы вида помощи (config/care_types.yaml: название и синонимы) и услуги — в основах слов."""
    out: list[list[str]] = []
    for phrase in [care or "", service or ""]:
        if not phrase:
            continue
        for lem_set in _care_phrase_sets(phrase):
            stems = [_stem(x) for x in lem_set if x not in STOPWORDS and x not in _GENERIC] or \
                    [_stem(x) for x in lem_set]
            if stems and stems not in out:
                out.append(stems)
    return out


def care_route_stems(care: str | None) -> set[str]:
    """Основы route_words вида помощи (config/care_types.yaml: «пломба», «кариес» — стоматология): услуга из этих
    слов — сам вид помощи, а не уточнение сверх него (строка матрицы вида помощи даёт «входит»; «можно» всё равно
    только по тексту программы — guided.allowed_blocker)."""
    if not care:
        return set()
    out: set[str] = set()
    for code, spec in ((load_yaml("care_types.yaml") or {}).get("care_types") or {}).items():
        if _norm(care) in (code, _norm(str(spec.get("name") or ""))):
            for w in spec.get("route_words") or []:
                out |= {_stem(x) for x in lemmas(str(w)) if x not in STOPWORDS}
    return out


def care_label(row_care: str) -> str:
    """Название вида помощи для текста: код матрицы («dental») → название из config/care_types.yaml."""
    spec = ((load_yaml("care_types.yaml") or {}).get("care_types") or {}).get(row_care.strip().casefold())
    return str(spec.get("name")) if spec else row_care


def care_row_matches(row_care: str, stem_sets: list[list[str]]) -> bool:
    """Строка матрицы / перечня — нужный вид помощи: все основы одной из фраз есть в названии строки и
    упоминание не отрицательное («Поликлиническое обслуживание без стоматологии» — не стоматология).
    Строка с кодом вида помощи («dental») сравнивается по его названию из config/care_types.yaml."""
    cfg = load_yaml("care_types.yaml") or {}
    # строка-страховой риск («Страхование критических заболеваний») — не вид помощи (care_types.yaml not_care_rows)
    if any(re.search(rx, row_care, flags=re.IGNORECASE) for rx in cfg.get("not_care_rows") or []):
        return False
    spec = (cfg.get("care_types") or {}).get(row_care.strip().casefold())
    if spec:
        row_care = str(spec.get("name") or row_care)
    # составной заголовок ГК 2 «Поликлиника | Врач на дом» — несколько видов помощи: каждая часть отдельно
    parts = [p.strip() for p in row_care.split("|") if p.strip()] or [row_care]
    return any(phrase_hit(s, p) for s in stem_sets for p in parts)


def clinic_care_fits(row_care: str, stem_sets: list[list[str]]) -> bool:
    """Строка ПЕРЕЧНЯ клиник подходит к виду помощи вопроса. Пустой вид помощи у строки (разметка оператора без
    колонки и без постоянного вида помощи, ADR 0012) НЕ подходит (w9-fix п. 1а: пустой вид помощи подходил к любому,
    и «к терапевту в «Итого»» с выбором строки таблицы премий давало ложное «можно»): клиника в таком перечне —
    только сведение «есть в перечне, вид помощи не указан» (clinic_listed_no_care), clinic_in_program = not_found,
    правило covered не срабатывает."""
    return bool(row_care.strip()) and care_row_matches(row_care, stem_sets)


def _row_lead(row_care: str) -> str:
    """«О чём» строка матрицы ГК 1 («Расширение программы "Стандарт": Консультации и лечение у психотерапевта
    (амбулаторно-поликлиническое…) не более 5 раз в год»): после вводной «…:» и до первой скобки."""
    s = row_care
    m = re.match(r"^[^:]{0,60}:\s*", s)
    if m:
        s = s[m.end():]
    return s.split("(")[0]


def service_row_matches(row_care: str, variants: list[ServiceTerms]) -> bool:
    """Строка матрицы названа услугой из вопроса (а не только видом помощи): слова услуги — в начале названия
    строки (_row_lead, первые lead_words слов), без отрицания («без вызова врача на дом» — не совпадение)."""
    parts = [p.strip() for p in row_care.split("|") if p.strip()] or [row_care]
    return any(_any_hit(variants, _row_lead(p), lead=coverage_cfg().lead_words) for p in parts)


# ---------------------------------------------------------------- способ оказания и класс услуги (этап w12-care)
#
# Правила — config/service_classes.yaml. Строка матрицы, совпавшая с видом помощи по словам, может быть про другой
# способ оказания («Программа Телемедицина: … дежурного терапевта» на вопрос об очном приёме терапевта) или узкой
# по объёму («Поликлиническое обслуживание, в объёме лабораторных исследований» на вопрос об МРТ). Такая строка
# «входит» не подтверждает: выбирается строка, подходящая по способу и классу услуги, а если её нет —
# care_in_program = not_found и справка «найден вид помощи «…», но он не про …» (`docs/rules.md` п. 3а: not_found
# метку «нельзя» не даёт).

@dataclass(frozen=True)
class _SvcCfg:
    negation: re.Pattern[str] | None
    modes: tuple[tuple[str, str, tuple[re.Pattern[str], ...]], ...]
    in_person_name: str
    care_modes: dict[str, str]
    narrow: re.Pattern[str] | None
    scope_ignore: re.Pattern[str] | None
    classes: tuple[tuple[str, str, tuple[frozenset[str], ...]], ...]
    generic: dict[str, frozenset[frozenset[str]]]


@cache
def service_cfg() -> _SvcCfg:
    raw = load_yaml("service_classes.yaml") or {}
    care_types = (load_yaml("care_types.yaml") or {}).get("care_types") or {}

    def rx(s: Any) -> re.Pattern[str] | None:
        return re.compile(str(s), re.IGNORECASE) if s else None

    classes = []
    generic: dict[str, frozenset[frozenset[str]]] = {}
    for code, spec in (raw.get("service_classes") or {}).items():
        generic[str(code)] = frozenset(frozenset(lemmas(str(w))) for w in spec.get("generic") or [] if lemmas(str(w)))
        words = [str(w) for w in spec.get("words") or []]
        for ct in spec.get("include_route_words") or []:
            words += [str(w) for w in (care_types.get(ct) or {}).get("route_words") or []]
        phrases = tuple(dict.fromkeys(frozenset(lemmas(w)) for w in words if lemmas(w)))
        classes.append((str(code), str(spec.get("name") or code), phrases))
    return _SvcCfg(
        negation=rx(raw.get("negation")),
        modes=tuple((str(k), str(v.get("name") or k), tuple(re.compile(p, re.IGNORECASE) for p in v.get("patterns") or []))
                    for k, v in (raw.get("modes") or {}).items()),
        in_person_name=str(raw.get("in_person_name") or ""),
        care_modes={str(k): str(v) for k, v in (raw.get("care_modes") or {}).items()},
        narrow=rx(raw.get("narrow")), scope_ignore=rx(raw.get("scope_ignore")), classes=tuple(classes),
        generic=generic)


def _mode_text(s: str) -> str:
    """Текст для поиска способа: нижний регистр, «ё» → «е», без упоминаний после «без / кроме …» (config negation)."""
    t = (s or "").casefold().replace("ё", "е")
    neg = service_cfg().negation
    return neg.sub(" ", t) if neg else t


def text_modes(s: str) -> frozenset[str]:
    """Способы оказания помощи, названные в тексте (config/service_classes.yaml modes)."""
    t = _mode_text(s)
    return frozenset(code for code, _n, pats in service_cfg().modes if any(p.search(t) for p in pats))


def question_modes(question: str, care: str | None) -> frozenset[str]:
    """Способ, о котором вопрос: слова способа в вопросе; «онлайн / на дом» сильнее общего «приём» («онлайн-приём
    терапевта» — телемедицина). Слов нет — способ вида помощи слота care_type (config care_modes); пусто —
    вопрос об очной помощи без уточнения (in_person)."""
    got = set(text_modes(question))
    if got & {"telemed", "home"}:
        got.discard("outpatient")
    if not got:
        m = service_cfg().care_modes.get(care_code(care) or "")
        if m:
            got = {m}
    return frozenset(got)


def mode_fits(row_modes: frozenset[str], q_modes: frozenset[str]) -> bool:
    """Строка подходит по способу: у строки способа нет (общий вид помощи) — подходит; вопрос без уточнения
    (очная помощь) — у строки есть не только дистанционный способ; иначе — общий способ строки и вопроса."""
    if not row_modes:
        return True
    if not q_modes:
        return bool(row_modes - {"telemed"})
    return bool(row_modes & q_modes)


def class_phrases(s: str | None) -> dict[str, set[frozenset[str]]]:
    """Классы услуги в тексте (config/service_classes.yaml service_classes) и совпавшие фразы каждого класса:
    леммы фразы — среди лемм текста."""
    have = set(lemmas(s or ""))
    out: dict[str, set[frozenset[str]]] = {}
    for code, _n, phrases in service_cfg().classes:
        got = {p for p in phrases if p <= have}
        if got:
            out[code] = got
    return out


def service_classes(s: str | None) -> frozenset[str]:
    """Классы услуги (config/service_classes.yaml service_classes)."""
    return frozenset(class_phrases(s))


def narrow_scope(row_care: str) -> dict[str, set[frozenset[str]]] | None:
    """Классы услуг узкой строки матрицы и фразы каждого класса в её объёме («Поликлиническое обслуживание,
    в объёме лабораторных исследований…» → {lab: {лабораторный}}) или None — строка не узкая (нет маркера config
    narrow или после него не назван ни один класс). Составная строка «A | B» узкая, только если узки все части."""
    cfg = service_cfg()
    if cfg.narrow is None:
        return None
    out: dict[str, set[frozenset[str]]] = {}
    for part in [p.strip() for p in row_care.split("|") if p.strip()] or [row_care]:
        m = cfg.narrow.search(_mode_text(part))
        if not m:
            return None
        scope = _mode_text(part)[m.end():]
        if cfg.scope_ignore:
            scope = cfg.scope_ignore.sub(" ", scope)
        got = class_phrases(scope)
        if not got:
            return None
        for k, v in got.items():
            out.setdefault(k, set()).update(v)
    return out


def scope_fits(scope: dict[str, set[frozenset[str]]], svc: dict[str, set[frozenset[str]]]) -> bool:
    """Узкая строка подтверждает услугу: класс услуги в объёме строки, и либо слова самой услуги названы в объёме
    («5 приёмов специалистов (терапевт, …)» — терапевт), либо объём назван только общими словами класса
    (config generic: «в объёме лабораторных исследований» — любой анализ). Перечень («2 инструментальных
    исследования (флюорография, ЭКГ)») без услуги вопроса (МРТ) её не подтверждает."""
    gen = service_cfg().generic
    for c in set(scope) & set(svc):
        own = svc[c] - gen.get(c, frozenset())
        if own & scope[c] or scope[c] <= gen.get(c, frozenset()):
            return True
    return False


def _mode_name(modes: frozenset[str]) -> str:
    names = {code: name for code, name, _p in service_cfg().modes}
    return ", ".join(names[m] for m in sorted(modes) if m in names) or service_cfg().in_person_name


def care_misfit(row_care: str, q_modes: frozenset[str], s_classes: dict[str, set[frozenset[str]]]
                ) -> tuple[str, dict[str, str]] | None:
    """Почему строка матрицы не подтверждает услугу вопроса: (ключ текста config/texts_ru/care_pick.yaml,
    подстановки) или None — строка подходит. Сначала способ оказания, затем класс узкой строки."""
    rm = text_modes(care_label(row_care))
    if not mode_fits(rm, q_modes):
        return "care_misfit_mode", {"row_mode": _mode_name(rm), "question_mode": _mode_name(q_modes)}
    scope = narrow_scope(care_label(row_care))
    if scope is not None and s_classes and not scope_fits(scope, s_classes):
        names = {code: name for code, name, _p in service_cfg().classes}
        return "care_misfit_class", {"service_class": ", ".join(names[c] for c in sorted(s_classes))}
    return None


def pick_care_rows(rows: list[dict[str, Any]], question: str, care: str | None, service: str | None,
                   first: list[dict[str, Any]] | None = None
                   ) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str, dict[str, str]]]]:
    """Строки матрицы, совпавшие по словам, → (подходящие по способу и классу услуги — по порядку: названные
    услугой (first), общие строки, узкие строки нужного класса (у узкой строки свои рамки: чек-ап, «по
    направлению Пульта»); неподходящие с причиной)."""
    q_modes = question_modes(question or service or "", care)
    s_classes = class_phrases(service) or class_phrases(question)
    first = first or []
    fit: list[tuple[int, int, dict[str, Any]]] = []
    misfit: list[tuple[dict[str, Any], str, dict[str, str]]] = []
    for i, r in enumerate(rows):
        rc = str(r.get("care_type") or "")
        why = care_misfit(rc, q_modes, s_classes)
        if why:
            misfit.append((r, *why))
            continue
        rank = 0 if r in first else 2 if narrow_scope(care_label(rc)) is not None else 1
        fit.append((rank, i, r))
    return [r for _k, _i, r in sorted(fit, key=lambda x: (x[0], x[1]))], misfit


def _cite_matrix(run: _Run, row: dict[str, Any], stems: list[str]) -> list[DraftCitation]:
    """Ссылка строки матрицы: пункт программы, где назван этот вид помощи (дословный фрагмент); иначе — сама
    строка таблицы матрицы, как её вернул инструмент (на экран: «программа: вид помощи — входит / не входит»)."""
    refs = [r for r in row.get("refs") or [row.get("ref")] if r]
    for ref in refs[:50]:
        cl = run.clause(ref)
        if cl is not None and _hits(stems, cl.text):
            got = run.cite_clause(ref, stems)
            if got:
                return [got[0]]
    plain = next((r for r in refs if run.clause(r) is None), None)
    if plain:
        return [DraftCitation(ref=plain, quote="")]
    # пункта со словами услуги нет: пункт, где назван сам вид помощи строки, а не первый попавшийся (ревизия 9:
    # «скорая помощь входит» с цитатой о помощи на дому)
    care = key_stems(str(row.get("care_type") or ""))
    for ref in refs[:50]:
        cl = run.clause(ref)
        if cl is not None and care and _hits(care, cl.text) >= min(2, len(care)):
            got = run.cite_clause(ref, care)
            if got:
                return [got[0]]
    ref = refs[0] if refs else None
    return [DraftCitation(ref=ref, quote=fragment(run.clause(ref).text, stems))] if ref else []


@dataclass
class CareCheck:
    """Итог матрицы: значение care_in_program, название строки, услуга сама — строка матрицы, условия строки."""
    value: str = "not_found"
    name: str | None = None
    service_row: bool = False
    conditions: list[str] = field(default_factory=list)
    contradictory: bool = False     # у вида помощи в матрице и «входит», и «не входит» (этап w10-fix2)


def check_care(run: _Run, program: str, care: str | None, service: str | None, *,
               skip: list[str] | None = None, question: str = "") -> CareCheck:
    """care_in_program по матрице программы: строка вида помощи (config/care_types.yaml) или строка, названная
    услугой (ГК 1: заголовки T11 «Расширение программы…: Консультации и лечение у психотерапевта … не более 5 раз
    в год»). absent — только строка нужного вида с present=False. Условия из названия строки, совпавшей с услугой
    («не более 5 раз в год», «по направлению врача») — каждое своим утверждением со ссылкой на строку."""
    res = run.call("care_matrix", program=program)
    rows = [r for r in res.get("rows") or [] if _prog_eq(r.get("program"), program)]
    if rows:
        run.looked_at.append(f"матрица видов помощи программы «{program}» (care_matrix)")
    sets = _care_stem_sets(care, service)
    variants = service_terms(service, skip=skip)
    by_service = [r for r in rows if variants and service_row_matches(str(r.get("care_type") or ""), variants)]
    matched = by_service + [r for r in rows if r not in by_service and
                            care_row_matches(str(r.get("care_type") or ""), sets)]
    # способ оказания и класс услуги (config/service_classes.yaml, этап w12-care): строка телемедицины не отвечает
    # на вопрос об очном приёме, «в объёме лабораторных исследований» — на вопрос об МРТ
    matched, misfit = pick_care_rows(matched, question, care, service, first=by_service)
    stems = [s for x in sets for s in x]
    # уточнения услуги сверх слов вида помощи («анализы на ВИЧ», «УЗИ при беременности», «генетические
    # анализы»): строка вида помощи «Диагностика — входит» не значит, что входит услуга с уточнением, —
    # care_in_program=yes по ней не ставится (утверждение о строке матрицы остаётся справочным)
    care_stems = {s for x in _care_stem_sets(care, None) for s in x} | care_route_stems(care)
    extras = [s for s in (variants[0].required if variants else ())
              if not any(s.startswith(c) or c.startswith(s) for c in care_stems)]
    out = CareCheck()
    # этап w10-fix2 (`docs/rules.md` п. 2): строка вида помощи с тем же названием и «входит», и «не входит» — так onboarding
    # хранит «нет» матрицы разметки при клиниках этого вида в перечне (table_mapping.list_conflicts). Противоречие
    # документов: оба фрагмента, care_in_program не ставится, documents = contradictory («при условиях, нужно
    # подтверждение СК»), не «нельзя». У адаптеров СК таких пар нет (пара «программа × вид помощи» одна).
    both: dict[str, dict[bool, dict[str, Any]]] = {}
    for r in matched:
        both.setdefault(_norm(str(r.get("care_type") or "")), {}).setdefault(bool(r.get("present")), r)
    pair = next((v for v in both.values() if len(v) == 2), None)
    if pair is not None:
        name = care_label(str(pair[True]["care_type"]))
        yes, no = _cite_matrix(run, pair[True], stems), _cite_matrix(run, pair[False], stems)
        run.say(text("care_matrix_yes", care=name, program=program), yes, kind=StatementKind.fact, supports=[])
        run.say(text("care_matrix_absent", care=name, program=program), no, kind=StatementKind.limitation,
                supports=[])
        if yes and no:
            run.contradictions.append(DraftContradiction(a=yes[0], b=no[0],
                                                         note=text("matrix_contradiction", care=name,
                                                                   program=program)))
        return CareCheck(value="not_found", name=name, contradictory=True)
    for present, value, key in ((True, "yes", "care_matrix_yes"), (False, "absent", "care_matrix_absent")):
        hit = [r for r in matched if bool(r.get("present")) is present]
        if hit:
            r = hit[0]
            name = care_label(str(r["care_type"]))
            cites = _cite_matrix(run, r, stems)
            by_row = r in by_service
            partial = present and not by_row and bool(extras)
            run.say(text(key, care=name, program=program), cites,
                    kind=StatementKind.fact if present else StatementKind.limitation,
                    supports=[] if partial else ["care_in_program"])
            out = CareCheck(value="not_found" if partial else value, name=name, service_row=by_row)
            if present and r in by_service and cites:
                for kind in condition_kinds(name):
                    run.say(text("cond_matrix", care=name, program=program), cites, kind=StatementKind.condition,
                            supports=[f"conditions.{kind}"])
                    out.conditions.append(kind)
            break
    else:
        # подходящей строки нет, а совпавшая по словам строка другого способа / класса есть — честная справка
        # без «входит» (care_in_program остаётся not_found, метку «нельзя» это не даёт)
        hit_mis = next((m for m in misfit if m[0].get("present")), None)
        if hit_mis is not None:
            r, key, kw = hit_mis
            name = care_label(str(r["care_type"]))
            run.say(text(key, care=name, program=program, **kw), _cite_matrix(run, r, stems),
                    kind=StatementKind.fact, supports=[])
            out = CareCheck(value="not_found", name=name)
    # услуга названа словами целого вида помощи («госпитализация», «стационар», «вызов врача на дом»;
    # config/coverage_markers.yaml service.care_level_words), и этот вид в матрице программы есть
    if out.value == "yes" and variants and not out.service_row:
        level = {_stem(_norm(x)) for x in coverage_cfg().care_level}
        out.service_row = all(all(s in level for s in t.required) for t in variants[:1])
    return out


# ---------------------------------------------------------------- покрытие: услуга в разделах программы
#
# Правила разделов, видов условий и совпадения услуги — config/coverage_markers.yaml (устройство программ трёх СК —
# docs/facts/kit/gk1–3.md). Раздел пункта определяется по его ЗАГОЛОВКАМ (путь, пункт-родитель по номеру,
# ближайший предшествующий заголовок), а не по словам в тексте самого пункта: «не оплачивается» бывает и внутри
# перечня «входит», а «входит» — внутри исключений. Выводы (`docs/rules.md` п. 1–3а):
#   exclusion=yes   — услуга названа в пункте раздела «исключение», без оговорки («за исключением…», «если…»),
#                     пункт о ней (слова услуги в начале фрагмента), и в разделе «входит» её нет;
#   care=yes        — услуга названа в пункте раздела «входит» без отрицания в тексте («не входит»);
#   conditions      — виды условий из найденных пунктов и их заголовков (франшиза, направление, лимит, зона…);
#   exclusion=absent — «входит» установлено, исключения программы найдены инструментом и просмотрены, услуги в них
#                     нет; иначе not_found. Услуга и в «входит», и в «исключение» — противоречие (оба фрагмента),
#                     «нельзя» не ставится. Ложное «нельзя» — самая дорогая ошибка: при любом сомнении — not_found.

SECTIONS = ("exclusion", "condition", "included")
_HYPHEN = re.compile(r"(?<=[0-9a-zа-яё])[-/](?=[0-9a-zа-яё])")     # «чек-ап», «ПЭТ/КТ» → слитно
_SERVICE_WORD = re.compile(r"[0-9a-zа-яё]+(?:-[0-9a-zа-яё]+)*")
_NUMBER = re.compile(r"\d+(?:\.\d+)*\.?")
_ITEM_START = re.compile(r"^\s*[-–—•*]")


def _norm(s: str | None) -> str:
    return (s or "").casefold().replace("ё", "е")


@dataclass(frozen=True)
class _CovCfg:
    sections: dict[str, tuple[re.Pattern[str], ...]]
    care_headings: tuple[re.Pattern[str], ...]
    care_heading_words: int
    cells: dict[str, frozenset[str]]
    negation: tuple[re.Pattern[str], ...]
    caution: tuple[re.Pattern[str], ...]
    included_caution: tuple[re.Pattern[str], ...]
    kinds: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]
    noise: frozenset[str]
    grammemes: frozenset[str]
    context: frozenset[str]
    care_level: frozenset[str]
    abbreviations: tuple[tuple[str, str], ...]
    synonyms: tuple[tuple[str, ...], ...]
    procedure: frozenset[str]
    item_lead_words: int
    allow_missing_from: int
    lead_words: int
    exclusion_lead_words: int
    lookback: int
    max_statements: int
    anatomy: frozenset[str] = frozenset()
    specialist_suffixes: tuple[str, ...] = ()
    qualifier_preps: frozenset[str] = frozenset()
    actions: tuple[tuple[str, tuple[str, ...]], ...] = ()
    abroad_scope: tuple[re.Pattern[str], ...] = ()
    abroad_question: tuple[re.Pattern[str], ...] = ()
    abroad_reference: re.Pattern[str] | None = None
    reference_only: tuple[re.Pattern[str], ...] = ()
    reference_max_words: int = 40
    procedure_headings: tuple[re.Pattern[str], ...] = ()
    allowed_blockers: tuple[re.Pattern[str], ...] = ()
    abroad_countries: frozenset[str] = frozenset()
    care_sections: tuple[tuple[str, tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]], ...] = ()


def _rx(items: Any) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(str(x), re.IGNORECASE) for x in items or [])


@cache
def coverage_cfg() -> _CovCfg:
    """config/coverage_markers.yaml в скомпилированном виде."""
    raw = load_yaml("coverage_markers.yaml") or {}
    svc = raw.get("service") or {}
    cells = raw.get("table_cells") or {}
    abroad = raw.get("abroad") or {}
    ref_only = raw.get("reference_only") or {}
    return _CovCfg(
        sections={k: _rx((raw.get("sections") or {}).get(k)) for k in SECTIONS},
        care_headings=_rx(raw.get("included_care_headings")),
        care_heading_words=int(raw.get("care_heading_words") or 10),
        cells={k: frozenset(_norm(str(x)).strip() for x in cells.get(k) or []) for k in
               ("included", "limited", "negative")},
        negation=_rx(raw.get("text_negation")), caution=_rx(raw.get("caution")),
        included_caution=_rx(raw.get("included_caution")),
        kinds=tuple((str(k), _rx(v)) for k, v in (raw.get("condition_kinds") or {}).items()),
        noise=frozenset(_norm(str(x)) for x in svc.get("noise") or []),
        grammemes=frozenset(str(x) for x in svc.get("noise_grammemes") or []),
        # необязательные слова услуги: вид помощи / специалист (context_words) и место исследования (anatomy_words)
        context=frozenset(_norm(str(x)) for k in ("context_words", "anatomy_words") for x in svc.get(k) or []),
        care_level=frozenset(_norm(str(x)) for x in svc.get("care_level_words") or []),
        abbreviations=tuple((_norm(str(k)), _norm(str(v))) for k, v in (svc.get("abbreviations") or {}).items()),
        synonyms=tuple(tuple(_norm(str(x)) for x in g) for g in svc.get("synonyms") or [] if g),
        procedure=frozenset(_norm(str(x)) for x in svc.get("procedure_words") or []),
        item_lead_words=int(svc.get("item_lead_words") or 3),
        allow_missing_from=int(svc.get("allow_missing_from") or 4),
        lead_words=int(svc.get("lead_words") or 20),
        exclusion_lead_words=int(svc.get("exclusion_lead_words") or 6),
        lookback=int(raw.get("lookback") or 80), max_statements=int(raw.get("max_statements") or 3),
        anatomy=frozenset(_norm(str(x)) for x in svc.get("anatomy_words") or []),
        specialist_suffixes=tuple(_norm(str(x)) for x in svc.get("specialist_suffixes") or []),
        qualifier_preps=frozenset(_norm(str(x)) for x in svc.get("qualifier_preps") or []),
        actions=tuple((str(k), tuple(_norm(str(x)) for x in v or [])) for k, v in (svc.get("actions") or {}).items()),
        abroad_scope=_rx(abroad.get("scope")), abroad_question=_rx(abroad.get("question")),
        abroad_reference=re.compile(str(abroad["reference"]), re.IGNORECASE) if abroad.get("reference") else None,
        reference_only=_rx(ref_only.get("patterns")), reference_max_words=int(ref_only.get("max_words") or 40),
        procedure_headings=_rx(svc.get("procedure_headings")),
        allowed_blockers=_rx(raw.get("allowed_blockers")),
        abroad_countries=frozenset(_norm(str(x)) for x in abroad.get("countries") or []),
        care_sections=tuple((str(k), _rx(v.get("headings") if isinstance(v, dict) else v),
                             _rx(v.get("question") if isinstance(v, dict) else []))
                            for k, v in (raw.get("care_sections") or {}).items()),
    )


@dataclass(frozen=True)
class ServiceTerms:
    """Основы значимых слов одного варианта названия услуги. required — по ним решается совпадение (при
    n ≥ allow_missing_from допускается пропуск одного); optional — слова вида помощи/специалиста/анатомии, не
    обязательны; actions — вид действия услуги (config service.actions: лечение, реабилитация, диагностика,
    профилактика), если он назван в вопросе."""
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    procedure: tuple[str, ...] = ()     # из required: слова процедуры (config service.procedure_words)
    actions: tuple[str, ...] = ()
    adjectives: tuple[str, ...] = ()    # из required: прилагательные / причастия («плановая», «генетические»)
    lems: tuple[str, ...] = ()          # леммы обязательных слов (для строгого совпадения: «терапевт» ≠ «терапевтическая»)

    def need(self) -> int:
        n = len(self.required)
        return n - 1 if n >= coverage_cfg().allow_missing_from else n

    def reduced(self) -> ServiceTerms | None:
        """Услуга без слов процедуры («анализы на ВИЧ» → «ВИЧ») — для поиска в исключениях; совпадение по ней
        даёт только оговорку, не exclusion=yes. None — слов процедуры нет или кроме них ничего не остаётся."""
        keep = [k for k, x in enumerate(self.required) if x not in self.procedure]
        rest = tuple(self.required[k] for k in keep)
        # без существительного («плановую госпитализацию» → «плановая») услуга не названа: оговорки из пункта
        # о «плановой санации» к ней не относятся
        if not self.procedure or not rest or all(x in self.adjectives for x in rest):
            return None
        lems = tuple(self.lems[k] for k in keep) if len(self.lems) == len(self.required) else ()
        return ServiceTerms(rest, self.optional, actions=self.actions, adjectives=self.adjectives, lems=lems)


def _service_variants(service: str) -> list[str]:
    """Название услуги и его варианты по сокращениям config (МРТ ↔ магнитно-резонансная томография). Регистр
    исходного текста сохраняется: по нему распознаются сокращения («ПСА» — не «пёс»)."""
    s = (service or "").replace("ё", "е").replace("Ё", "Е")
    out = [s]
    for abbr, full in coverage_cfg().abbreviations:
        rx = re.compile(rf"(?<![0-9a-zа-я]){re.escape(abbr)}(?![0-9a-zа-я])", re.IGNORECASE)
        if rx.search(s):
            out.append(rx.sub(full, s))
        if full in s.casefold():
            out.append(re.sub(re.escape(full), abbr.upper(), s, flags=re.IGNORECASE))
    return list(dict.fromkeys(out))


@dataclass(frozen=True)
class _SvcWord:
    stem: str
    lemma: str
    qualifier: bool = False     # слово после предлога уточнения («после удаления», «на ВПЧ»)
    optional: bool = False      # вид помощи / специалист / анатомия (context_words, anatomy_words, specialist_suffixes)
    anatomy: bool = False
    noun: bool = True           # существительное, сокращение или латиница (не прилагательное / причастие)


def _service_words(phrase: str, skip: frozenset[str]) -> list[_SvcWord]:
    """Значимые слова услуги без служебных, общих слов ДМС (_GENERIC), слов ситуации (noise) и названий (география,
    имена, числительные — граммемы pymorphy), а также без слов программы/клиники/города (skip).
    Слово через дефис сравнивается слитно: «чек-апе» → «чекап». Сокращение («ЭКГ», «ПСА», «ВПЧ», «ПЭТ-КТ») и
    латиница («Lokomat») — как есть, без лемматизации и без усечения основы."""
    from .index.morph import _analyzer
    from .scenarios.route import is_abbreviation

    cfg = coverage_cfg()
    abbr = {a for a, _ in cfg.abbreviations}
    context = {_stem(x) for x in cfg.context}
    anatomy = {_stem(x) for x in cfg.anatomy}
    out: list[_SvcWord] = []
    qualifier = False
    for m in re.finditer(r"[0-9a-zа-яё]+(?:-[0-9a-zа-яё]+)*", phrase.replace("ё", "е").replace("Ё", "Е"),
                         re.IGNORECASE):
        orig = m.group()
        raw = _norm(orig)
        w = raw.replace("-", "")
        if raw in cfg.qualifier_preps:
            qualifier = bool(out)        # предлог в начале («на дому») — часть названия, а не уточнение
            continue
        if w in skip or raw in skip or w.isdigit():
            continue
        noun = True
        if is_abbreviation(orig):
            lem = st = w
        elif re.search("[а-я]", w):
            parses = _analyzer().parse(w)
            p = parses[0]
            # разбор, при котором слово — вид помощи / анатомия («вен» → «вена», а не «вено»), предпочтительнее
            p = next((x for x in parses if _stem(x.normal_form.replace("ё", "е")) in context | anatomy), p)
            eponym = bool(out) and out[-1].lemma in _EPONYM_HEADS
            if any(g in p.tag for g in cfg.grammemes) and not eponym:
                continue
            # имя в названии болезни («болезни Крона») — как есть, без разбора как нарицательного («крона»)
            lem = w if eponym and any(g in p.tag for g in cfg.grammemes) else p.normal_form.replace("ё", "е")
            st = _stem(lem)
            noun = str(p.tag.POS) not in ("ADJF", "ADJS", "PRTF", "PRTS")
        else:
            lem = st = w
        if lem in skip or lem in STOPWORDS or lem in _GENERIC or lem in cfg.noise:
            continue
        if len(lem) < 3 and raw not in abbr and not is_abbreviation(orig):
            continue
        if any(x.stem == st for x in out):
            continue
        specialist = len(lem) > 6 and lem.endswith(cfg.specialist_suffixes)
        out.append(_SvcWord(st, lem, qualifier=qualifier, optional=st in context or specialist,
                            anatomy=st in anatomy, noun=noun))
    return out


# Слова, после которых имя собственное — часть названия болезни / метода (route._EPONYM_HEADS)
_EPONYM_HEADS = frozenset({"болезнь", "синдром", "метод", "аппарат", "операция"})


def _service_stems(phrase: str, skip: frozenset[str]) -> list[str]:
    return [x.stem for x in _service_words(phrase, skip)]


def service_actions(service: str | None) -> tuple[str, ...]:
    """Вид действия услуги (config service.actions) по словам вопроса — до слов «после / при / …»: «реабилитация
    после ковида» — реабилитация, а не лечение; «лечение» не входит в основы услуги (общее слово), но вид задаёт."""
    head = re.split(r"\b(?:" + "|".join(sorted(coverage_cfg().qualifier_preps)) + r")\b", _norm(service), maxsplit=1)[0] \
        if coverage_cfg().qualifier_preps else _norm(service)
    words = _WORD.findall(head)
    lems = [lemma_of(w) for w in words]
    return tuple(k for k, stems in coverage_cfg().actions
                 if any(x.startswith(stems) or w.startswith(stems) for x, w in zip(lems, words, strict=True)))


def text_actions(words: list[str]) -> set[str]:
    """Виды действия, названные словами фрагмента (config service.actions, по началу слова)."""
    return {k for k, stems in coverage_cfg().actions if any(w.startswith(stems) for w in words)}


def service_terms(service: str | None, *, skip: list[str] | None = None) -> list[ServiceTerms]:
    """Варианты услуги для поиска в пунктах. skip — названия программ/клиники/города (их слова — не услуга).

    Услуга без главного слова не ищется (вариант отбрасывается → «не нашли» → «недостаточно данных»):
    обязательными остались одни уточнения («после удаления» без «имплантации») или услуга — одна анатомия
    («колено»). Если без необязательных слов остаются одни прилагательные («плановый терапевт» → «плановый»),
    необязательные слова становятся обязательными. Услуга из одних слов вида помощи или специалиста («стоматология», «приём психиатра») ищется по ним."""
    if not service:
        return []
    cfg = coverage_cfg()
    skip_set = frozenset(w for s in skip or [] for w in _norm(s).replace("-", " ").split() if w)
    procedure = {_stem(x) for x in cfg.procedure}
    actions = service_actions(service)
    word_sets: list[list[_SvcWord]] = []
    for v in _service_variants(service):
        ws = _service_words(v, skip_set)
        if ws and [x.stem for x in ws] not in [[y.stem for y in z] for z in word_sets]:
            word_sets.append(ws)
    # синонимы услуги (config service.synonyms): фраза группы среди слов услуги → варианты с остальными фразами
    for ws in list(word_sets):
        stems = [x.stem for x in ws]
        for group in cfg.synonyms:
            phrases = [_service_words(p, frozenset()) for p in group]
            for ph in phrases:
                ph_st = [x.stem for x in ph]
                if not ph or not set(ph_st) <= set(stems):
                    continue
                for other in phrases:
                    # слова фразы заменяются словами синонима целиком (общее слово фраз — «мануальный» — остаётся)
                    kept = [x for x in ws if x.stem not in ph_st]
                    new = kept + [x for x in other if x.stem not in {y.stem for y in kept}]
                    if other and other != ph and new and \
                            [x.stem for x in new] not in [[y.stem for y in z] for z in word_sets]:
                        word_sets.append(new)
                break
    out: list[ServiceTerms] = []
    for ws in word_sets:
        req = [x for x in ws if not x.optional]
        opt = [x for x in ws if x.optional]
        gen = procedure_cfg().generic
        if not req:
            if all(x.anatomy for x in opt):
                continue                         # одна анатомия («колено») — услуга не названа
            req, opt = ws, []
        elif opt and all(not x.noun for x in req):
            # без необязательных слов осталось одно второстепенное слово («плановый» от «планового терапевта»):
            # необязательные слова возвращаются в обязательные, иначе совпало бы любое «плановое …»
            req, opt = ws, []
        elif opt and len(req) == 1 and req[0].lemma in (load_yaml("coverage_markers.yaml") or {}).get(
                "ambiguous_alone", []) and any(x.optional and not x.anatomy and x.lemma not in gen for x in opt):
            # слово процедуры, неоднозначное без области («протезирование» — и сосудов, и зубов; config
            # coverage_markers.yaml ambiguous_alone): слово области обязательно — иначе «протезы сосудов» давали
            # ложное «входит» на вопрос о протезировании зубов (ревизия главного пункта w18)
            req, opt = req + [x for x in opt if x.optional and not x.anatomy and x.lemma not in gen], \
                [x for x in opt if not (x.optional and not x.anatomy and x.lemma not in gen)]
        elif opt and all(x.lemma in procedure_cfg().generic and not x.qualifier for x in req):
            # осталось одно общее слово процедуры («удаление» от «удаления зуба», procedures.generic): само по себе оно
            # услугу не называет — без «зуба» совпали бы «удаление ВМС», «удаление кист» (ревизия точности w18);
            # обязательным становится необщее слово («зуб»), общее («в стационаре») остаётся необязательным
            req, opt = req + [x for x in opt if x.lemma not in gen], [x for x in opt if x.lemma in gen]

        if all(x.qualifier for x in req):
            continue                             # остались одни уточнения — главное слово услуги потеряно
        t = ServiceTerms(tuple(x.stem for x in req), tuple(x.stem for x in opt),
                         tuple(x.stem for x in req if x.stem in procedure), actions,
                         tuple(x.stem for x in req if not x.noun), tuple(x.lemma for x in req))
        if t not in out:
            out.append(t)
    return out


def _word_lists(s: str) -> tuple[list[str], list[str]]:
    """Слова текста: как есть («магнитно», «резонансной») и со слитными словами через дефис («чекап»)."""
    n = _norm(s)
    return _WORD.findall(n), _WORD.findall(_HYPHEN.sub("", n))


# Слово текста — то же слово услуги, а не другое слово с той же основой (этап w7-fix, ревизия 7): основа _stem
# обрезана до 6 букв, и «диспансеризация» совпадала с «диспансерное наблюдение». Для исключений сравнение — по
# лемме: леммы равны или различаются только окончанием (общее начало ≥ длины более короткой леммы без 2 букв:
# «косметолог» ↔ «косметологический», «стоматология» ↔ «стоматологической», «терапевт» ↔ «терапия»), но не
# словообразовательным суффиксом («диспансеризация» ≠ «диспансерный»: общее начало 9 из 12).
LEMMA_TAIL = 2


def same_word(lem: str, word: str) -> bool:
    """Слово текста word — та же лемма lem услуги (с точностью до окончания), см. LEMMA_TAIL."""
    lem = lem.casefold().replace("ё", "е").replace("-", "")
    w = word.casefold().replace("ё", "е")
    if not lem or w == lem:
        return True
    lw = lemma_of(w).replace("ё", "е").replace("-", "")
    if lw == lem or not lem.isalpha() or not lw.isalpha():
        return lw == lem or w.startswith(lem)
    common = 0
    for a, b in zip(lem, lw, strict=False):
        if a != b:
            break
        common += 1
    return common >= min(len(lem), len(lw)) - LEMMA_TAIL


def terms_hit(terms: ServiceTerms, s: str, *, lead: int | None = None, actions: bool = True,
              by_lemma: bool = False) -> bool:
    """Услуга названа во фрагменте: найдено не меньше need() обязательных основ, ни одна — не сразу после
    «без / кроме / не / исключением»; lead — все найденные слова в первых lead словах фрагмента («о чём» он).
    Одно общее слово ДМС совпадения не даёт: такие слова не попадают в основы услуги (_GENERIC).
    actions — вид действия должен совпасть: у услуги он назван («лечение», «реабилитация», «вакцинация»), а во
    фрагменте до последнего найденного слова услуги назван только другой вид — совпадения нет (config
    service.actions: «лечение ковида» ≠ «реабилитация после COVID-19»).
    by_lemma — слово текста ещё и та же лемма слова услуги (same_word; для исключений, этап w7-fix)."""
    if not terms.required:
        return False
    lists = _word_lists(s)
    found: list[int] = []
    lems = terms.lems if by_lemma and len(terms.lems) == len(terms.required) else ()
    for n, st in enumerate(terms.required):
        pos = None
        for words in lists:
            for i, w in enumerate(words):
                if w.startswith(st) and not (i and words[i - 1] in NEGATIONS) and \
                        (not lems or same_word(lems[n], w)):
                    pos = i
                    break
            if pos is not None:
                break
        if pos is not None:
            found.append(pos)
    if len(found) < terms.need() or not found:
        return False
    if lead is not None and max(found) >= lead:
        return False
    if actions and terms.actions:
        # вид действия — только в своём пункте перечня (между «, ; : ( )»): «диагностика по методу Фоля, …,
        # гомеопатия» — «диагностика» относится к первому пункту, а не к «гомеопатии»
        words0 = lists[0]
        last = max(found)
        if last < len(words0):
            start = 0
            pos = 0
            for part in re.split(r"[,;:()]", _norm(s)):
                n = len(_WORD.findall(part))
                if pos + n > last:
                    start = pos
                    break
                pos += n
            named = text_actions(words0[start:last + 1])
        else:
            named = text_actions(words0[:last + 1])
        if named and not named & set(terms.actions):
            return False
    return True


def _any_hit(variants: list[ServiceTerms], s: str, *, lead: int | None = None, actions: bool = True,
             by_lemma: bool = False) -> bool:
    return any(terms_hit(t, s, lead=lead, actions=actions, by_lemma=by_lemma) for t in variants)


def strict_hit(variants: list[ServiceTerms], s: str) -> bool:
    """Услуга названа во фрагменте ЦЕЛИКОМ: все обязательные слова какого-либо варианта — по лемме (или сокращение
    как есть), а не по основе одного слова («терапевт» ≠ «терапевтическая санация», «стоматолог» ≠
    «стоматологии ортодонтической»). Для противоречия «входит» / «исключение» (ревизия w5-recall)."""
    words = _WORD.findall(_HYPHEN.sub("", _norm(s))) + _WORD.findall(_norm(s))
    have = {w for w in words} | {lemma_of(w) for w in words}
    for t in variants:
        if t.lems and len(t.lems) == len(t.required) and all(
                lem in have or lem.replace("-", "") in have for lem in t.lems):
            return True
    return False


_ITEM_SPLIT = re.compile(r"[,;:()\[\]–—]|\s-\s")
# Составное существительное через дефис («сурдологии-оториноларингологии») может быть отдельной специальностью:
# первая часть не на -о/-е (не прилагательная приставка «магнитно-», «медико-») и не «врача-» («врача-терапевта» —
# та же услуга). Ревизия w6: «приём оториноларинголога» → ложное «нельзя» по «сурдологии-оториноларингологии».
_NOUN_COMPOUND = re.compile(r"(?<![0-9a-zа-яё-])(?!врач)([а-яё]{4,}[^\Wоеё_\d])-(?=[а-яё]{4,})", re.IGNORECASE)


def exclusion_hit(variants: list[ServiceTerms], seg: str) -> bool:
    """Пункт исключений — о нашей услуге: слова услуги в начале фрагмента (exclusion_lead_words) или все в
    начале одной части перечня (между «, ; : ( ) –», item_lead_words): «…в косметических целях: герметизация
    фиссур, …, реставрация зубных рядов», «Приемы … по: гериатрии, диабетологии, …».
    Слова услуги — по лемме (same_word, этап w7-fix): «диспансеризация» не совпадает с «диспансерным наблюдением»."""
    cfg = coverage_cfg()
    if _any_hit(variants, seg, lead=cfg.exclusion_lead_words, by_lemma=True):
        return True
    return any(_any_hit(variants, part, lead=cfg.item_lead_words, by_lemma=True)
               for part in _ITEM_SPLIT.split(seg) if part.strip())


def scoped_only_hit(variants: list[ServiceTerms], seg: str, question: str = "") -> bool:
    """Услуга названа в пункте исключений только как часть другого («Зубопротезирование и подготовка к нему,
    включая … удаление и депульпирование зубов»): до слов «включая / в том числе»
    (config coverage_markers.yaml exclusion_scope) услуги нет, после — есть. Исключена услуга лишь в рамках
    главного («удаление зуба как подготовка к протезированию»), а не вообще — оговорка (условие other), не «нельзя»
    (ревизия точности w18: «удаление зуба» → ложное «нельзя»)."""
    rx = (load_yaml("coverage_markers.yaml") or {}).get("exclusion_scope")
    m = re.search(rx, _norm(seg)) if rx else None
    if m is None:
        return False
    head, tail = _norm(seg)[:m.start()], _norm(seg)[m.end():]
    if not (not _any_hit(variants, head, actions=False) and _any_hit(variants, tail, actions=False)):
        return False
    # вопрос сам о том, к чему подготовка («удаление зуба перед протезированием») — исключение действует
    purpose = [st for st in key_stems(head) if len(st) >= 5]
    return not (question and any(phrase_hit([st], question, negation_aware=False) for st in purpose))


def compound_only_hit(variants: list[ServiceTerms], seg: str) -> bool:
    """Исключение совпало только половиной составного слова через дефис: со склеенными составными словами
    (_NOUN_COMPOUND) совпадения нет. Это не «исключение» и не «в исключениях не названа», а оговорка — условие
    other с фрагментом (как caution): ни «нельзя», ни «можно» по такому пункту."""
    merged = _NOUN_COMPOUND.sub(r"\1", seg)
    return merged != seg and not exclusion_hit(variants, merged)


def _segments(cl: Clause) -> list[str]:
    """Фрагменты пункта, в которых ищется услуга: строка таблицы — целиком; иначе предложения и пункты перечня
    через «;» (как у fragment(), чтобы цитата была тем же фрагментом)."""
    if _is_table_row(cl):
        return [cl.text]
    return [m.group().strip() for m in _SENT.finditer(cl.text or "") if len(m.group().strip()) > 2]


def _is_table_row(cl: Clause) -> bool:
    """Строка таблицы (путь «… › табл. 8, стр. 3»): её раздел — по ячейкам, не по заголовкам (в ГК 3 таблица
    услуг приложения оказалась в пути под разделом VII «франшиза»)."""
    return "табл." in (cl.path or "")


def _cell_section(cl: Clause) -> tuple[str | None, bool]:
    """Строка таблицы услуг (ГК 3: «вид | услуга | да / с ограничениями | комментарий»): раздел — по ячейке.
    (раздел, с ограничениями)."""
    cfg = coverage_cfg()
    cells = [_norm(c).strip(" .") for c in cl.text.split("|")]
    if _mixed_row(cells):
        return "condition", False
    if any(c in cfg.cells["limited"] for c in cells):
        return "included", True
    if any(c in cfg.cells["included"] for c in cells):
        return "included", False
    if any(c in cfg.cells["negative"] for c in cells[1:]):
        return "negative", False
    return None, False


def _mixed_row(cells: list[str]) -> bool:
    """Строка таблицы, где есть и ячейка «да / входит», и ячейка «нет / не входит» (этап w10-fix2): неразмеченная
    матрица «вид помощи | программа 1 | программа 2» — к какой программе «да», по строке не понять. Такая строка —
    не «входит» и не «исключение»: только условие other со строкой целиком (раздел condition)."""
    cfg = coverage_cfg()
    return any(c in cfg.cells["included"] for c in cells) and any(c in cfg.cells["negative"] for c in cells[1:])


def _comment_after_yes(cells: list[str]) -> bool:
    """В строке таблицы услуг после ячейки «да» (config table_cells.included) есть непустая ячейка-комментарий."""
    inc = coverage_cfg().cells["included"]
    norm = [_norm(c).strip(" .") for c in cells]
    for i, c in enumerate(norm):
        if c in inc:
            return any(len(x) >= 3 and x not in inc for x in norm[i + 1:])
    return False


def heading_section(h: str, *, ancestor: bool = True) -> str | None:
    """Раздел по одному заголовку; в одном заголовке отрицание сильнее («не входит» ≠ «входит»).
    Заголовки видов помощи (config included_care_headings: «Поликлиническое обслуживание», «Инструментальные
    исследования») дают «входит» только заголовку-предку (ancestor) не длиннее care_heading_words слов; вводная
    самого пункта («Приемы, консультации и манипуляции врачей … по:») по общим словам раздел не получает."""
    cfg = coverage_cfg()
    n = _norm(h)
    for sec in SECTIONS:
        if any(p.search(n) for p in cfg.sections[sec]):
            return sec
    if ancestor and len(n.split()) <= cfg.care_heading_words and any(p.search(n) for p in cfg.care_headings):
        return "included"
    return None


def _lead(t: str) -> str:
    """Заголовочная часть пункта-родителя: до двоеточия или до конца первого предложения (≤ 200 знаков)."""
    t = (t or "").strip()
    m = re.search(r"[:.;]", t[:200])
    if m and m.start() > 3:
        return t[:m.end()]
    return t if len(t) <= 200 else ""


def _heading_like(t: str) -> bool:
    """Похоже на заголовок: вводная к перечню («…:») или короткая строка без запятых и точки в конце."""
    t = (t or "").strip()
    if not 2 < len(t) <= 160 or _ITEM_START.match(t):
        return False
    return t.endswith(":") or (len(t) <= 90 and "," not in t and not t.endswith((".", ";")))


def _heading_clause(cl: Clause) -> bool:
    """Пункт — заголовок блока: вводная к перечню («…:») или текст, с которого начинается сегмент его пути
    (ГК 2: «Вызов врача на дом в пределах МКАД»)."""
    t = (cl.text or "").strip()
    if t.endswith(":") and len(t) <= 200:
        return True
    last = _norm((cl.path or "").split(" › ")[-1]).rstrip("…").strip()
    return bool(last) and len(t) <= 200 and not _NUMBER.fullmatch(last) and _norm(t).startswith(last[:40])


@dataclass
class _Head:
    text: str
    clause: Clause | None = None      # пункт-заголовок (им можно сослаться), None — имя сегмента пути
    own: bool = False                 # вводная самого пункта («…:»), а не заголовок-предок


@dataclass
class _Hit:
    clause: Clause
    segment: str
    section: str | None
    heads: list[_Head]
    caution: bool = False
    negated: bool = False
    kinds: list[tuple[str, Clause, str]] = field(default_factory=list)   # (вид условия, пункт, фрагмент)


def condition_kinds(s: str) -> list[str]:
    """Виды условий (словарь Checks.conditions) по тексту: config condition_kinds."""
    n = _norm(s)
    return [k for k, pats in coverage_cfg().kinds if any(p.search(n) for p in pats)]


class _Sections:
    """Разделы пунктов одного договора: заголовки по пути, по номеру-родителю и ближайший предшествующий."""

    def __init__(self, clauses: list[Clause]) -> None:
        self.by_doc: dict[str, list[Clause]] = {}
        self.pos: dict[str, int] = {}
        self.first: dict[tuple[str, str], Clause] = {}
        self.by_number: dict[tuple[str, str | None, str], Clause] = {}
        for c in clauses:
            lst = self.by_doc.setdefault(c.doc_id, [])
            self.pos[c.id] = len(lst)
            lst.append(c)
            self.first.setdefault((c.doc_id, c.path), c)
            if c.number:
                self.by_number.setdefault((c.doc_id, c.program, c.number.rstrip(".")), c)
        self.cache: dict[str, tuple[str | None, list[_Head]]] = {}
        self.abroad_cache: dict[str, bool] = {}

    def _path_head(self, cl: Clause, path: str) -> _Head | None:
        h = self.first.get((cl.doc_id, path))
        if h is None or h.id == cl.id:
            return None
        lead = _lead(h.text)
        return _Head(lead, h) if lead else None

    def chain(self, cl: Clause) -> list[_Head]:
        """Заголовки пункта от ближайшего к дальнему: вводная часть самого пункта (до «:»), пункты-родители по
        номеру («4.1» → «4»), сегменты пути и пункты с этими путями."""
        out: list[_Head] = []
        own = (cl.text or "").strip()
        if ":" in own[:200]:
            out.append(_Head(own[:own.index(":") + 1], cl, own=True))
        segs = (cl.path or "").split(" › ")
        for i in range(len(segs), 0, -1):
            seg = segs[i - 1]
            base = seg.removesuffix(" (абзац)").strip()
            parent = " › ".join(segs[:i - 1])
            if _NUMBER.fullmatch(base):
                parts = base.rstrip(".").split(".")
                if base != seg:      # «6.1.3 (абзац)» — сам пункт 6.1.3 тоже заголовок
                    h = self._path_head(cl, f"{parent} › {base}" if parent else base)
                    out += [h] if h else []
                for k in range(len(parts) - 1, 0, -1):
                    num = ".".join(parts[:k])
                    for cand in (num, num + "."):
                        h = self._path_head(cl, f"{parent} › {cand}" if parent else cand)
                        if h:
                            out.append(h)
                            break
                continue
            if not re.fullmatch(r"раздел\s+[ivxlc\d]+", _norm(base)):
                out.append(_Head(base.rstrip("…")))
            h = self._path_head(cl, " › ".join(segs[:i]))
            out += [h] if h else []
        return out

    def previous(self, cl: Clause, limit: int) -> list[_Head]:
        """Предшествующие пункты-заголовки того же документа и программы (плоский путь ГК 2)."""
        lst = self.by_doc.get(cl.doc_id) or []
        i = self.pos.get(cl.id, 0)
        out: list[_Head] = []
        for c in reversed(lst[max(0, i - limit):i]):
            if c.program != cl.program:
                break
            if _heading_like(c.text):
                out.append(_Head(c.text.strip(), c))
        return out

    def abroad(self, cl: Clause) -> bool:
        """Пункт относится к помощи за рубежом (config abroad.scope): так назван заголовок-предок пункта (полный
        текст пункта-родителя: «3.5 Медицинские … услуги, предоставляемые вне территории Российской Федерации…»)
        или пункт, на который заголовок ссылается («4.3 В отношении … услуг, предусмотренных
        п. 3.5 …:»). Текст самого пункта не учитывается: исключение «услуги за рубежом» — о загранице, а не только
        для неё."""
        if cl.id in self.abroad_cache:
            return self.abroad_cache[cl.id]
        cfg = coverage_cfg()

        def marked(t: str) -> bool:
            return any(p.search(_norm(t)) for p in cfg.abroad_scope)

        got = False
        for h in self.chain(cl):
            if h.clause is not None and h.clause.id == cl.id and not h.own:
                continue
            text = h.clause.text if h.clause is not None else h.text
            if marked((text if not h.own else h.text)[:300]):
                got = True
                break
            m = cfg.abroad_reference.search(_norm(h.text if h.own else text)[:300]) if cfg.abroad_reference else None
            ref = self.by_number.get((cl.doc_id, cl.program, m.group(1).rstrip("."))) if m else None
            if ref is not None and ref.id != cl.id and marked(ref.text[:300]):
                got = True
                break
        if not got:
            # этап w5-strict (ревизия 4): длинный заголовок-родитель без знака препинания в первых 200 знаках
            # («3.5 Медицинские и иные услуги, предоставляемые вне территории Российской Федерации при …») не даёт
            # вводной (_lead пуст) и выпадает из chain — родители берутся напрямую: по номеру («3.5.1.1» → «3.5.1»
            # → «3.5») и по сегментам пути, полный текст родителя (первые 300 знаков)
            got = any(marked(p.text[:300]) for p in self._ancestors(cl))
        if not got and cl.number and marked((cl.text or "")[:300]) and \
                (cl.doc_id, cl.program, cl.number.rstrip(".") + ".1") in self.by_number:
            # сам заголовок раздела о загранице (у пункта есть подпункты «3.5.1…») — тоже раздел о загранице;
            # пункт без подпунктов с такими словами (исключение «услуги за рубежом») по-прежнему не считается
            got = True
        self.abroad_cache[cl.id] = got
        return got

    def _ancestors(self, cl: Clause) -> list[Clause]:
        """Пункты-родители: по номеру той же программы и документа и по префиксам пути (без самого пункта)."""
        out: list[Clause] = []
        if cl.number:
            parts = cl.number.rstrip(".").split(".")
            for k in range(len(parts) - 1, 0, -1):
                p = self.by_number.get((cl.doc_id, cl.program, ".".join(parts[:k])))
                if p is not None and p.id != cl.id:
                    out.append(p)
        segs = (cl.path or "").split(" › ")
        for i in range(len(segs) - 1, 0, -1):
            p = self.first.get((cl.doc_id, " › ".join(segs[:i])))
            if p is not None and p.id != cl.id and p not in out:
                out.append(p)
        return out

    def section(self, cl: Clause) -> tuple[str | None, list[_Head]]:
        """(раздел пункта, заголовки до определившего раздел включительно). Таблица услуг — по ячейке.

        Порядок (ревизия w5-coverage): 1) любой заголовок цепочки — раздел исключений (вводная самого пункта,
        пункт-родитель по номеру «4.2.2» → «4.2» → «4», сегмент пути) → пункт — исключение, что бы ни было в его
        вводной («Приемы, консультации … по:» под «4.2 … не оплачивается Страховщиком:»); 2) иначе — ближайший
        заголовок цепочки с разделом «входит» / «условие»; 3) цепочка ничего не дала (плоский путь ГК 2) —
        ближайший предшествующий заголовок того же документа и программы: заголовок исключений с ТЕМ ЖЕ путём
        делает пункт исключением, с другим путём — останавливает поиск (лучше «не нашли», чем чужое «нельзя»)."""
        if cl.id in self.cache:
            return self.cache[cl.id]
        table = _is_table_row(cl)
        got: tuple[str | None, list[_Head]] = (_cell_section(cl)[0], []) if table else (None, [])
        if not table or got[0] is None:
            # строка таблицы без ячейки «да / нет» (таблицы лимитов раздела III ГК 3: «Наименование услуги |
            # Ограничение объема…») — раздел по заголовкам, как у пункта; но «исключение» из заголовка строке
            # таблицы не присваивается (лучше «не нашли», чем «нельзя» по строке лимитов)
            heads = self.chain(cl)
            secs = [heading_section(h.text, ancestor=not h.own) for h in heads]
            if "exclusion" in secs:
                i = secs.index("exclusion")
                got = ("exclusion", heads[:i + 1])
            elif any(secs):
                i = next(k for k, x in enumerate(secs) if x)
                got = (secs[i], heads[:i + 1])
            else:
                for h in self.previous(cl, coverage_cfg().lookback):
                    sec = heading_section(h.text)
                    if sec == "exclusion":
                        if h.clause is not None and h.clause.path == cl.path:
                            got = ("exclusion", [*heads, h])
                        break
                    if sec:
                        got = (sec, [*heads, h])
                        break
            if table and got[0] == "exclusion":
                got = (None, [])
        self.cache[cl.id] = got
        return got


def _negated(seg: str) -> bool:
    n = _norm(seg)
    return any(p.search(n) for p in coverage_cfg().negation)


def _cautious(seg: str) -> bool:
    n = _norm(seg)
    return any(p.search(n) for p in coverage_cfg().caution)


def caution_scope(seg: str, variants: list[ServiceTerms]) -> str:
    """Часть фрагмента исключений, к которой относится оговорка для нашей услуги: пункты перечня (части между
    «,» и «;» вне скобок) с услугой — целиком, а скобки при ДРУГИХ пунктах перечня отбрасываются: «…аутогемотерапию,
    гирудотерапию, лечение методом ударно-волновой терапии (за исключением случаев, предусмотренных…)» — оговорка
    о ударно-волновой терапии, а не о гирудотерапии. Оговорка без скобок («…, за исключением случаев …») остаётся:
    к какому пункту она относится, по тексту не видно (осторожно — условие, а не «нельзя»)."""
    items: list[str] = []
    depth, cur = 0, []
    for ch in seg:
        # скобка, открытая в предыдущем фрагменте («…согласно п. 3.1.6 настоящей Программы), …» — фрагменты
        # режутся по точке), глубину ниже нуля не уводит
        depth = max(0, depth + (ch == "(") - (ch == ")"))
        if ch in ",;" and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    own = [_any_hit(variants, it, actions=False) for it in items]
    if not any(own):
        return seg
    def bare(it: str) -> str:
        # скобки чужого пункта, в том числе незакрытая в конце фрагмента и закрывающая из предыдущего
        it = re.sub(r"\([^()]*\)", " ", it)
        return re.sub(r"^[^()]*\)|\([^()]*$", " ", it)

    return ",".join(it if mine else bare(it) for it, mine in zip(items, own, strict=True))


def procedure_heading(cl: Clause, seg: str, heads: list[_Head]) -> bool:
    """Слово процедуры услуги («анализ», «исследование», «диагностика») названо рубрикой строки таблицы (первая
    ячейка) или заголовком пункта (config service.procedure_headings), а не в самом фрагменте."""
    pats = coverage_cfg().procedure_headings
    texts = [h.text for h in heads]
    if _is_table_row(cl):
        texts.append(seg.split("|")[0])
    return any(p.search(_norm(t)) for t in texts for p in pats)


def question_abroad(question: str) -> bool:
    """Вопрос о помощи за рубежом (config abroad.question и abroad.countries — страны по леммам: «в Турции»).
    Для вопроса о России пункты раздела «вне территории РФ» (ГК 3: 3.5.x и 4.3.x) не участвуют ни во «входит», ни
    в исключениях и противоречиях (coverage_hits, check_coverage)."""
    n = _norm(question)
    if any(p.search(n) for p in coverage_cfg().abroad_question):
        return True
    countries = coverage_cfg().abroad_countries
    return bool(countries) and any(lemma_of(w) in countries or w in countries for w in _WORD.findall(n))


def care_code(care: str | None) -> str | None:
    """Код вида помощи config/care_types.yaml по названию / коду слота care_type."""
    if not care:
        return None
    for code, spec in ((load_yaml("care_types.yaml") or {}).get("care_types") or {}).items():
        if _norm(care) in (code, _norm(str(spec.get("name") or ""))):
            return str(code)
    return None


def _care_family(code: str | None) -> set[str]:
    """Вид помощи и связанные с ним по part_of (диагностика — в поликлинике)."""
    if not code:
        return set()
    types = (load_yaml("care_types.yaml") or {}).get("care_types") or {}
    out = {code}
    parent = str((types.get(code) or {}).get("part_of") or "")
    if parent:
        out.add(parent)
    out |= {str(k) for k, v in types.items() if str((v or {}).get("part_of") or "") == code}
    return out


def section_care(sections: _Sections, cl: Clause) -> str | None:
    """Вид помощи раздела, в котором стоит пункт: ближайший заголовок цепочки, названный видом помощи
    (config coverage_markers.yaml care_sections: «Помощь на дому в пределах 30 км», «Программа «АПТЕКА»»,
    «Стационарное обслуживание»). None — общий раздел программы (исключения программы, общие условия)."""
    cfg = coverage_cfg()
    texts = [cl.text.split("|")[0]] if _is_table_row(cl) else []   # строка таблицы: рубрика — первая ячейка
    texts += [h.text for h in sections.chain(cl) if not h.own]
    for t in texts:
        n = _norm(t)[:200]
        for code, pats, _q in cfg.care_sections:
            if any(p.search(n) for p in pats):
                return code
    return None


def care_compatible(hit_care: str | None, question_care: str | None, question: str = "") -> bool:
    """Пункт раздела вида помощи hit_care относится к вопросу о виде question_care: раздел общий; вид вопроса не
    известен (кроме разделов с условием по вопросу — config care_sections.question: «АПТЕКА» — только к вопросу о
    лекарствах); тот же вид или связанный по part_of. Вопрос о приёме невролога не берёт «входит» / условия /
    исключения из разделов «Помощь на дому», «Телемедицина», «Стационар»; «лечение бесплодия» — из «АПТЕКИ»."""
    if hit_care is None:
        return True
    for code, _pats, qpats in coverage_cfg().care_sections:
        if code == hit_care and qpats:
            return any(p.search(_norm(question)) for p in qpats)
    if question_care is None:
        return True
    return hit_care in _care_family(question_care)


def coverage_hits(run: _Run, program: str, services: list[ServiceTerms],
                  cares: list[ServiceTerms] | None = None, *, abroad: bool = False,
                  care: str | None = None, question: str = "") -> list[_Hit]:
    """Пункты программы (и общие пункты договора/доп. соглашений), где названа услуга, с разделом каждого.
    services — варианты услуги (по ним — всё); cares — фразы вида помощи (только заголовки разделов «входит»
    и «условие», чтобы взять условие раздела вроде «Помощь на дому в пределах 30 км от МКАД:»).
    abroad — вопрос о помощи за рубежом: учитываются только пункты раздела о загранице (_Sections.abroad), иначе —
    только остальные (ГК 3: п. 3.5 и исключения 4.3 «…услуг, предусмотренных п. 3.5» к помощи в России не относятся)."""
    cfg = coverage_cfg()
    sections = run.sections()
    cares = cares or []
    # услуга без слов процедуры («анализы на ВИЧ» → «ВИЧ»): только для исключений и только как оговорка
    reduced = [r for t in services if (r := t.reduced()) is not None and r not in services]
    out: list[_Hit] = []
    # программ у договора ≥ 2: строка таблицы без программы не даёт «входит» (этап w10-fix2, ниже)
    multi = len(_program_names(run)) >= 2
    for cl in run.scope(program):
        low = _HYPHEN.sub("", _norm(cl.text))
        by_service = any(st in low or st in _norm(cl.text) for t in [*services, *reduced] for st in t.required[:1])
        by_care = bool(cares) and _heading_clause(cl)
        if not by_service and not by_care:
            continue
        table = _is_table_row(cl)
        if sections.abroad(cl) != abroad:
            continue                              # помощь за рубежом — только к вопросу о загранице, и наоборот
        if not care_compatible(section_care(sections, cl), care_code(care), question):
            continue                              # раздел другого вида помощи (на дому, телемедицина, аптека)
        for seg in _segments(cl):
            loose = _any_hit(services, seg, actions=False) if by_service else False
            svc = loose and _any_hit(services, seg)
            red = not svc and by_service and bool(reduced) and _any_hit(reduced, seg)
            care_head = not svc and not red and by_care and _any_hit(cares, seg, lead=cfg.lead_words)
            if loose and not svc and not red:
                # слова услуги есть, но вид действия другой («реабилитация после …» при вопросе о лечении): не
                # совпадение; в разделе исключений — упоминание (исключения «просмотренными» не считаются)
                sec, heads = sections.section(cl)
                if sec == "exclusion":
                    out.append(_Hit(clause=cl, segment=seg, section="exclusion_mention", heads=heads))
                    break
                continue
            if not svc and not red and not care_head:
                continue
            sec, heads = sections.section(cl)
            if sec is None:
                break
            if care_head and sec not in ("included", "condition"):
                break
            if red and sec in ("included", "condition") and procedure_heading(cl, seg, heads):
                # «анализ на ПСА» в строке «лабораторная диагностика | ПСА общий | да»: слово процедуры названо
                # рубрикой / заголовком (config service.procedure_headings) — услуга названа целиком
                red, svc = False, True
            if red:
                if sec != "exclusion":
                    continue          # без слов процедуры «входит» не устанавливается (вся услуга — не названа)
                # исключение названо без слов процедуры: оговорка (условие other с фрагментом), не exclusion=yes;
                # упоминание не в начале пункта — как прочие упоминания (absent не ставится)
                lead_ok = exclusion_hit(reduced, seg)
                out.append(_Hit(clause=cl, segment=seg, section="exclusion" if lead_ok else "exclusion_mention",
                                heads=heads, caution=lead_ok))
                break
            if sec == "exclusion" and not exclusion_hit(services, seg):
                # услуга лишь упомянута в пункте исключений (пункт о другой услуге): не «исключение», но и
                # «в исключениях не названа» сказать нельзя
                out.append(_Hit(clause=cl, segment=seg, section="exclusion_mention", heads=heads))
                break
            elif sec in ("included", "condition") and not table and svc and \
                    not _any_hit(services, seg, lead=cfg.lead_words):
                continue
            hit = _Hit(clause=cl, segment=seg, section=sec, heads=heads, caution=sec == "exclusion" and
                       (_cautious(caution_scope(seg, services)) or compound_only_hit(services, seg)
                        or scoped_only_hit(services, seg, question)),
                       negated=sec == "included" and (_negated(seg) or in_exception_only(services, seg)))
            if care_head:
                hit.section = "condition"         # заголовок вида помощи даёт только условие, не «входит»
            if table and (_mixed_row([_norm(c).strip(" .") for c in cl.text.split("|")]) or
                          (hit.section == "included" and cl.program is None and multi)):
                # этап w10-fix2: строка «да / нет» сразу нескольких программ или строка таблицы без программы при
                # договоре с ≥ 2 программами (неразмеченная таблица запасного адаптера: чья она — не известно) —
                # не «входит», а справка-условие other со строкой целиком (`docs/rules.md` п. 2)
                hit.section = "condition"
                hit.negated = False
                hit.kinds.append(("other", cl, seg))
            # условия: сам фрагмент (в таблице — только если услуга не в одной лишь первой ячейке-рубрике) …
            own_text = seg
            if table:
                cells = seg.split("|")
                # таблица услуг (первая ячейка — рубрика вида помощи): условие — только если услуга не в одной
                # рубрике; таблица лимитов (без ячейки «да / нет») — строка целиком: «Маммография | … При наличии
                # медицинских показаний», «Мануальная терапия | Не более 3 процедур…»
                cell_rubric = _cell_section(cl)[0] is not None
                own_text = seg if not cell_rubric or (svc and _any_hit(services, " | ".join(cells[1:]))) else ""
                if own_text and _cell_section(cl)[1]:
                    hit.kinds.append(("limit", cl, seg))
                elif own_text and cell_rubric and _comment_after_yes(cells):
                    # этап w5-strict (ревизия 4): ячейка-комментарий после «да» («онколог | да | До установления
                    # диагноза») — оговорка к услуге: условие other со строкой целиком, «можно» без него — обещание
                    hit.kinds.append(("other", cl, seg))
            own_kinds = condition_kinds(own_text)
            for k in own_kinds:
                hit.kinds.append((k, cl, seg))
            # оговорка внутри «входит» («за исключением…», «первичная…», «в объеме 2 тестов») — услуга входит не
            # целиком: условие other (config included_caution), если вид условия не распознан точнее
            if sec == "included" and not hit.negated and own_text and not own_kinds and \
                    any(p.search(_norm(own_text)) for p in cfg.included_caution):
                hit.kinds.append(("other", cl, seg))
            # … и заголовки раздела «входит» / «условие», у которых есть пункт для ссылки
            if sec in ("included", "condition") and not hit.negated:
                for h in heads:
                    if h.clause is not None and h.clause.id != cl.id:
                        for k in condition_kinds(h.text):
                            hit.kinds.append((k, h.clause, h.text))
            out.append(hit)
            break                                   # один фрагмент на пункт
    return out


@dataclass
class Coverage:
    """Итог проверки покрытия услуги по тексту программы."""
    care: str = "not_found"
    exclusion: str = "not_found"
    conditions: list[str] = field(default_factory=list)
    contradictory: bool = False
    text_included: bool = False     # услуга названа в разделе «входит» текста программы (не только матрица)


def _cite_fragment(run: _Run, cl: Clause, seg: str, stems: list[str]) -> DraftCitation | None:
    """Ссылка на пункт с дословным фрагментом; пункт, которого не возвращал поиск, читается get_clause (запись
    прогона — refs для citations.verify)."""
    if cl.id not in run.seen_refs():
        run.call("get_clause", path=cl.path)
        if cl.id not in run.seen_refs():
            return None
    # короткий пункт — целиком; длинный — найденный фрагмент (если он не обрывок), иначе fragment() по основам
    full = cl.text or ""
    ok_seg = seg in full and len(seg) >= QUOTE_MIN_CHARS and len(seg.split()) >= 5 and len(seg) <= QUOTE_MAX
    frag = seg if ok_seg and len(full) > QUOTE_MAX else fragment(full, stems)
    return DraftCitation(ref=cl.id, quote=frag) if frag else None


_EXCEPT = re.compile(r"(?:кроме|за\s+исключением|исключая|не\s+включая)\s+([^()]{3,200})")


def in_exception_only(variants: list[ServiceTerms], seg: str) -> bool:
    """Во фрагменте «входит» услуга названа только внутри оговорки «за исключением / кроме / исключая …»
    («Лечение кожных заболеваний, в т. ч. псориаза (за исключением … псориатического артрита …)»): это не «входит»,
    а ограничение — фрагмент показывается, «входит» из него не ставится."""
    n = _norm(seg)
    for m in _EXCEPT.finditer(n):
        tail = m.group(1)
        if _any_hit(variants, tail, actions=False) and not _any_hit(variants, n[:m.start()], actions=False):
            return True
    return False


def _agreed(incl_seg: str, excl_seg: str, variants: list[ServiceTerms]) -> bool:
    """Включённый пункт с оговоркой «кроме / за исключением / исключая X» и пункт исключений об X согласованы, а не
    противоречат: значимое слово исключения (не слово услуги) названо в оговорке включённого пункта («Удаление ВМС
    по показаниям (исключая … беременности)» и исключение «…беременность…»)."""
    svc = {st for t in variants for st in (*t.required, *t.optional)}
    for m in _EXCEPT.finditer(_norm(incl_seg)):
        tail = m.group(1)
        words = [st for st in key_stems(excl_seg) if st not in svc and len(st) >= 4]
        if any(phrase_hit([st], tail, negation_aware=False) for st in words):
            return True
    return False


def _generic_service(variants: list[ServiceTerms], care: str | None) -> bool:
    """Услуга названа одним словом вида помощи или процедуры («диагностика», «анализы», «обследование»): по такому
    слову пункт исключений о конкретной болезни с ним не сравнивается."""
    if not variants:
        return False
    t = variants[0]
    if len(t.required) != 1:
        return False
    st = t.required[0]
    care_stems = {x for ph in _care_stem_sets(care, None) for x in ph} if care else set()
    procedure = {_stem(x) for x in coverage_cfg().procedure if " " not in x}
    return st in care_stems or (st in procedure and st not in {_stem(_norm(a)) for a, _ in coverage_cfg().abbreviations})


def reference_only(cl: Clause) -> bool:
    """Пункт только отсылает к другому тексту («исключения указаны в п. 4 Правил», config reference_only): по нему
    нельзя сказать, что исключения просмотрены."""
    cfg = coverage_cfg()
    n = _norm(cl.text)
    return len(n.split()) <= cfg.reference_max_words and any(p.search(n) for p in cfg.reference_only)


def check_coverage(run: _Run, program: str, service: str | None, care_value: str, *, care: str | None = None,
                   care_row_service: bool = False, skip: list[str] | None = None,
                   abroad: bool = False, question: str = "") -> Coverage:
    """Услуга в разделах программы → care_in_program / exclusion / conditions (правила — в шапке раздела).

    care_value — итог матрицы (yes / absent / not_found): матрица сильнее текста, её «нет» текстом не меняется.
    care_row_service — услуга сама и есть строка матрицы этой программы («госпитализация», «стоматология»):
    тогда пункт исключений, где это слово лишь упомянуто («стационар на дому»), не делает весь вид помощи
    исключённым — показывается как ограничение, exclusion=yes из него не ставится."""
    cov = Coverage(care=care_value)
    variants = service_terms(service, skip=skip)
    if not variants:
        return cov
    stems = list(dict.fromkeys(s for t in variants for s in (*t.required, *t.optional)))
    cares = [t for t in service_terms(care, skip=skip)] if care else []
    for ph in _care_phrase_sets(care) if care else []:
        st = [_stem(x) for x in ph if x not in STOPWORDS and x not in _GENERIC]
        if st and ServiceTerms(tuple(st)) not in cares:
            cares.append(ServiceTerms(tuple(st)))
    # поиск исключений — запись прогона и подтверждение просмотра (citations._absent_confirmed)
    res = run.call("search_clauses", query=f"исключения не является страховым случаем не оплачивается {service}",
                   program=program, k=EXCL_K)
    sections = run.sections()
    # «исключения просмотрены» — только по пунктам исключений документов программы / договора / доп. соглашений
    # (config/verdict_rules.yaml absent_sources.exclusion — run.scope; не по Памятке и правилам СК), не по
    # пункту-отсылке и не по разделу о помощи за рубежом при вопросе о помощи в России (и наоборот)
    in_scope = {c.id for c in run.scope(program)}
    excl_seen = [cl for h in res.get("hits") or [] if (cl := run.clause(h["ref"])) is not None
                 and cl.id in in_scope and sections.section(cl)[0] == "exclusion" and not reference_only(cl)
                 and sections.abroad(cl) == abroad
                 and care_compatible(section_care(sections, cl), care_code(care), question)]
    hits = coverage_hits(run, program, variants, cares, abroad=abroad, care=care, question=question)
    cfg = coverage_cfg()
    excl = [h for h in hits if h.section == "exclusion" and not h.caution]
    cautious = [h for h in hits if h.section == "exclusion" and h.caution]
    incl = [h for h in hits if h.section == "included" and not h.negated]
    # услуга — одно слово вида помощи или процедуры («диагностика», «анализы»): пункт исключений о болезни, где это
    # слово лишь стоит («диагностика и лечение иммунодефицитов»), — не исключение услуги и не противоречие
    generic = _generic_service(variants, care)
    if incl and excl:
        # противоречие — только если и во «входит», и в исключении названа вся услуга (strict_hit по леммам); иначе
        # пункт исключений, где услуга названа частью слова («терапевт» ↔ «терапевтическая санация»), — упоминание;
        # включённый пункт с оговоркой «кроме X» и исключение «X» согласованы (оговорка — условие other)
        strict_in = [h for h in incl if strict_hit(variants, h.segment)]
        keep = []
        for h in excl:
            if not strict_hit(variants, h.segment) or any(_agreed(i.segment, h.segment, variants) for i in incl):
                h.section = "exclusion_mention"
            else:
                keep.append(h)
        excl = keep
        # в исключении услуга названа целиком, а во «входит» — нет: противоречие остаётся (осторожно — «при
        # условиях / нужна проверка СК», а не «нельзя» по одному пункту)
        del strict_in
    if generic:
        for h in excl:
            h.section = "exclusion_mention"
        excl = []
    # упоминание услуги в пункте исключений мешает «исключения просмотрены» (absent), только если услуга названа
    # в нём целиком (strict_hit): основа слова («электр» от «электрокардиографии» в «электропунктурной …») — не она
    mentions_extra = [h for h in hits if h.section == "exclusion_mention" and strict_hit(variants, h.segment)]
    negative = [h for h in hits if h.section == "negative" or (h.section == "included" and h.negated)]
    mentions = mentions_extra

    def cite(h: _Hit) -> DraftCitation | None:
        return _cite_fragment(run, h.clause, h.segment, stems)

    # услуга в разделе «входит»
    incl_cites = [(h, dc) for h in incl if (dc := cite(h)) is not None][:cfg.max_statements]
    for h, dc in incl_cites:
        sup = ["care_in_program"] if cov.care == "not_found" else []
        run.say(dc.quote, [dc], kind=StatementKind.fact, supports=sup)
    # «можно» опирается только на пункт «входит», где услуга названа целиком (strict_hit), а не по основе слова
    cov.text_included = any(strict_hit(variants, h.segment) for h, _dc in incl_cites)
    if cov.care == "not_found" and incl_cites:
        cov.care = "yes"
        run.summary_parts.append(text("service_yes", service=service, program=program))
    # отрицание в «входит» и «нет» в таблице — показать, вывода не делать
    for h in negative[:cfg.max_statements]:
        dc = cite(h)
        if dc:
            run.say(dc.quote, [dc], kind=StatementKind.limitation)
    # исключения
    excl_cites = [(h, dc) for h in excl if (dc := cite(h)) is not None][:cfg.max_statements]
    if excl_cites and care_row_service:
        for _h, dc in excl_cites:
            run.say(dc.quote, [dc], kind=StatementKind.limitation)
    elif excl_cites and incl_cites:
        # и «входит», и «исключение» — противоречие документов: оба фрагмента, «нельзя» не ставится
        cov.contradictory = True
        run.contradictions.append(DraftContradiction(a=incl_cites[0][1], b=excl_cites[0][1],
                                                     note=text("coverage_contradiction", program=program)))
        for _h, dc in excl_cites:
            run.say(dc.quote, [dc], kind=StatementKind.limitation)
    elif excl_cites:
        dc = excl_cites[0][1]
        run.say(dc.quote, [dc], kind=StatementKind.limitation, supports=["exclusion"])
        run.summary_parts.append(text("exclusion_yes", program=program))
        cov.exclusion = "yes"
    # условия: оговорка в исключениях («за исключением случаев…») — условие other, не исключение
    conds: list[tuple[str, DraftCitation]] = []
    # услуга названа словами вида помощи: оговорка в исключении о другой услуге этого вида — не её условие
    for h in ([] if care_row_service else cautious[:cfg.max_statements]):
        dc = cite(h)
        if dc:
            kinds = [k for k in condition_kinds(h.segment) if k != "other"] or ["other"]
            conds += [(k, dc) for k in kinds]
    for h in [x for x in hits if x.section in ("included", "condition") and not x.negated]:
        for kind, cl, frag in h.kinds:
            dc = _cite_fragment(run, cl, frag, stems)
            if dc:
                conds.append((kind, dc))
    seen: list[str] = []
    for kind, dc in conds:
        if kind in seen:
            continue
        seen.append(kind)
        run.say(dc.quote, [dc], kind=StatementKind.condition, supports=[f"conditions.{kind}"])
    cov.conditions = seen
    # исключения просмотрены, услуги в них нет — только когда «входит» установлено (матрица или текст)
    if cov.exclusion != "yes" and not cov.contradictory and not cautious and not excl_cites and not mentions \
            and cov.care == "yes" and excl_seen:
        run.looked_at.append(f"исключения программы «{program}» (search_clauses, пунктов раздела: "
                             f"{len(excl_seen)}; поиск услуги по пунктам программы)")
        cl = excl_seen[0]
        frag = fragment(cl.text, [])
        run.say(text("exclusion_absent_st", program=program, service=service, quote=frag),
                [DraftCitation(ref=cl.id, quote=frag)], kind=StatementKind.fact, supports=["exclusion"])
        cov.exclusion = "absent"
    return cov


# ---------------------------------------------------------------- клиника и условия

def _note_kind(note: str) -> str:
    for kind, rx in _NOTE_KIND:
        if rx.search(note):
            return kind
    return "other"


def _conditions_of(run: _Run, row: dict[str, Any], program: str) -> list[str]:
    """Условия доступа из строки перечня клиник (франшиза, «по направлению», примечание) — каждое своим
    утверждением со ссылкой на эту строку (config/verdict_rules.yaml: conditions present → «при условиях»)."""
    conds: list[str] = []
    name = str(row.get("clinic_name") or "")
    cite = [DraftCitation(ref=row["ref"], quote=name)]
    pct = row.get("franchise_pct")
    if isinstance(pct, int | float) and not isinstance(pct, bool) and pct > 0:
        run.say(text("cond_franchise", clinic=name, pct=f"{pct:g}", program=program), cite,
                kind=StatementKind.condition, supports=["conditions.franchise"])
        conds.append("franchise")
    if row.get("by_referral"):
        run.say(text("cond_referral", clinic=name, program=program), cite, kind=StatementKind.condition,
                supports=["conditions.referral"])
        conds.append("referral")
    note = str(row.get("access_note") or "").strip()
    if note:
        kind = _note_kind(note)
        if kind not in conds:
            run.say(text("cond_note", clinic=name, note=note), cite, kind=StatementKind.condition,
                    supports=[f"conditions.{kind}"])
            conds.append(kind)
    return conds


# Слова, из которых не состоит название клиники или услуги: участники договора, сам ДМС, служебные слова обращения.
_NOT_NAME = frozenset({"дмс", "не", "по", "сотрудник", "сотрудница", "застрахованный", "застрахованная", "компания",
                       "программа", "договор", "страховой", "страховая", "страховщик", "клиника", "отказ", "отказать",
                       "работник", "hr", "полис", "услуга", "это", "он", "она"})


def _norm_words(s: str) -> list[str]:
    return [w for w in _words(s) if w]


def plausible_name(value: str, *, others: list[str]) -> bool:
    """Значение слота «клиника» / «услуга» похоже на название, а не на программу, город, юрлицо договора или
    служебные слова («не по ДМС», «Компания 13», «Москва»). Иначе слот не используется: ложная «клиника» дала бы
    «клиники нет в перечне» → «нельзя» (вывод без опоры, `docs/rules.md` п. 1)."""
    words = _norm_words(value)
    if not words or len(words) > 8:
        return False
    key = " ".join(words)
    if any(key == " ".join(_norm_words(o)) for o in others if o):
        return False
    # все слова — из названия программы / юрлица / города («Стандарт» при программе «Рязань Стандарт»)
    lw = {lemma_of(w) for w in words}
    if any(lw <= {lemma_of(x) for x in _norm_words(o)} for o in others if o):
        return False
    distinct = [w for w in words if w not in _NOT_NAME and lemma_of(w) not in _NOT_NAME and not w.isdigit()]
    return bool(distinct)


def lemma_of(word: str) -> str:
    got = lemmas(word, keep_stopwords=True)
    return got[0] if got else word


def name_context(st: KitStore, cid: str, slots: dict[str, Any]) -> list[str]:
    """С чем название клиники/услуги совпадать не должно: программы, юрлица и номера договоров ГК, город слота."""
    c = st.contract(cid)
    out = [p.name for p in c.programs] + [m.program for m in st.care(cid)]
    for x in st.contracts(c.group_id) if c.group_id else [c]:
        out += [x.legal_entity, x.number or ""]
    return [*out, str(slots.get("city") or "")]


def check_clinic(run: _Run, program: str, clinic: str, care: str | None, service: str | None,
                 *, conditions: bool = True, city: str | None = None, exact: bool = False,
                 street: str | None = None, pick: str | None = None) -> tuple[str, list[str]]:
    """clinic_in_program и условия по перечню клиник программы.

    Кандидаты — нечёткий подбор (clinics_query → tools/clinics.name_score: леммы, сокращения, склейки); решение —
    только строгое (этап w5-strict, ревизия 4: подстановка чужой клиники по сокращению, общему слову и потерянному
    номеру):
    yes — строка программы, где личность клиники подтверждена tools/clinics.strict_name_match (числа совпали, все
    отличительные слова найдены по форме / лемме / склейке, сокращение — не подтверждение, одно описательное слово —
    не подтверждение), для вида помощи вопроса; строки с разными номерами в названии при запросе без номера («Медси»
    ↔ «Медси-2», «Медси-3») — неясно, какая клиника (not_found). Кандидаты есть, строгого совпадения нет — not_found
    («не нашли», уточнение). absent («нет в перечне» → «нельзя») — только если (1) название извлечено уверенно
    (tools/clinics.confident_name: отличительное слово ≥ 4 букв или с цифрой, не город, не общее и не описательное
    слово), (2) перечень программы получен, (3) ни одна строка программы по всем видам помощи не совпала строго и
    не похожа нестрого (tools/clinics.distinct_overlap — сокращения и склейки тоже сомнение). Затем — другие
    программы договора (_other_programs). Иначе not_found (`docs/rules.md` п. 3а). exact — название в кавычках;
    правила для него те же.

    ADR 0010: клиника, названная в вопросе, всегда получает выбор менеджера (run.clinic_choice — кандидаты из
    перечня программы, clinic_candidates). Метки «можно» / «нет в перечне» ставятся только по учтённому выбору
    (pick — id строки или CLINIC_PICK_NONE, из slots["clinic_pick"]); без выбора решение текстом — прежнее строгое,
    но правила covered / clinic_absent без clinic_confirmed не срабатывают."""
    from .scenarios.route import listed_full_match, not_clinic_word
    from .tools.clinics import confident_name, distinct_overlap, distinct_words, normalize_name, strict_name_match

    # название совпало со строкой перечня программы целиком в обе стороны («Ренессанс Здоровье», «Он Клиник
    # Рязань») — это клиника перечня, проверки «похоже ли на название» не отбрасывают её (w7-core, ревизия 6)
    listed = listed_full_match(clinic, ChatContext(contract_id=run.cid), run.st, program=program)
    if not listed and not plausible_name(clinic, others=name_context(run.st, run.cid, {"city": city})):
        run.missing.append(text("missing_clinic_unclear", clinic=clinic))
        return "not_found", []
    distinct = distinct_words(clinic)
    if not listed and distinct and all(not_clinic_word(w) for w in distinct):
        # «СОГАЗ», «Ренессанс» — страховщик, а не клиника (config/route_markers.yaml not_clinic_words)
        run.missing.append(text("missing_clinic_unclear", clinic=clinic))
        return "not_found", []
    if not listed and not distinct and len(normalize_name(clinic).split()) < 2:
        # одно общее слово («Клиник», «Поликлиника») — не название конкретной клиники
        run.missing.append(text("missing_clinic_unclear", clinic=clinic))
        return "not_found", []
    # Название похоже на клинику — менеджеру предлагаются строки перечня этой программы этого договора (ADR 0010)
    run.clinic_choice = ClinicChoice(
        contract_id=run.cid, program=program, asked=clinic,
        candidates=clinic_candidates(run.st, run.cid, program, clinic, care, service, city=city, street=street))
    if pick:
        return _picked_clinic(run, program, clinic, care, service, pick, conditions=conditions)
    # все строки клиники (у сети — строка на каждый адрес): филиал по городу / улице выбирается ниже
    res = run.call("clinics_query", program=program, name=clinic, limit=CLINIC_ROWS_MAX)
    cand = [r for r in res.get("rows") or [] if _prog_eq(r.get("program"), program)]
    if cand and street:
        # строки сети на названной улице — отдельным запросом по адресу (у сети строк больше предела выдачи)
        by_addr = run.call("clinics_query", program=program, name=clinic, address=street, limit=CLINIC_ROWS_MAX)
        cand = [r for r in by_addr.get("rows") or [] if _prog_eq(r.get("program"), program)] + cand
    # решение — только по строгой личности клиники; нечёткие кандидаты без неё — «не нашли», а не чужая клиника
    rows = [r for r in cand if strict_name_match(clinic, r.get("clinic_name"), r.get("legal_name"))]
    if cand and not rows:
        run.looked_at.append(f"перечень клиник программы «{program}» (clinics_query)")
        run.missing.append(text("missing_clinic_not_found", clinic=clinic, program=program))
        return "not_found", []
    if rows and not distinct and len({normalize_name(str(r.get("clinic_name") or "")) for r in rows}) > 1:
        # название из одних общих слов подошло к нескольким разным клиникам — какая из них, не ясно
        run.missing.append(text("missing_clinic_unclear", clinic=clinic))
        return "not_found", []
    sets = _care_stem_sets(care, service)
    if rows:
        run.looked_at.append(f"перечень клиник программы «{program}» (clinics_query)")
        fit = [r for r in rows if clinic_care_fits(str(r.get("care_type") or ""), sets)] if sets else rows
        if fit and city:
            # город обращения назван: строка того же названия в другом городе — другой филиал сети, не «есть»
            same = [r for r in fit if in_city(city, r.get("city"), r.get("address"), r.get("clinic_name"))]
            if not same and all(r.get("city") for r in fit):
                run.missing.append(text("missing_clinic_not_found", clinic=clinic, program=program))
                return "not_found", []
            fit = same or fit
        if fit and street:
            # улица филиала названа («Семейный доктор на Дежнева»): строки сети с другими адресами — другие
            # филиалы; сравнение по лемме улицы целиком (street_word_match: «Варшавке» ↔ «Варшавское ш»,
            # «Покровке» ↔ «Покровка», но «Краснопресненская» ≠ «Красногорск» — этап w5-strict)
            keys = [w for w in _words(street) if len(w) >= 4]
            same = [r for r in fit if on_street(street, r.get("address"), r.get("clinic_name"))]
            if not same and keys and all(r.get("address") for r in fit):
                run.missing.append(text("missing_clinic_street", clinic=clinic, street=street, program=program))
                return "not_found", []
            fit = same or fit
        if fit and not _NUM_IN.search(clinic) and len({_name_numbers(r) for r in fit}) > 1:
            # запрос без номера, а строки с разными номерами в названии («Медси-2», «Медси-3») — какая из них, не ясно
            run.missing.append(text("missing_clinic_unclear", clinic=clinic))
            return "not_found", []
        if not fit and all(not str(x.get("care_type") or "").strip() for x in rows):
            # строки перечня без вида помощи: клиника в перечне есть, но для вида помощи вопроса — не видно
            r = rows[0]
            run.say(text("clinic_listed_no_care", clinic=r.get("clinic_name"), program=program),
                    [DraftCitation(ref=r["ref"], quote=str(r.get("clinic_name") or ""))])
            return "not_found", []
        if not fit:
            r = rows[0]
            run.say(text("clinic_other_care", clinic=r.get("clinic_name"), program=program,
                         care="; ".join(sorted({str(x.get("care_type")) for x in rows}))[:200]),
                    [DraftCitation(ref=r["ref"], quote=str(r.get("clinic_name") or ""))])
            return "not_found", []
        r = fit[0]
        name = str(r.get("clinic_name") or clinic)
        statement = (text("clinic_yes", clinic=name, program=program, care=r.get("care_type"),
                          address=r.get("address")) if r.get("address") else
                     text("clinic_yes_short", clinic=name, program=program))
        run.say(statement, [DraftCitation(ref=r["ref"], quote=name)], supports=["clinic_in_program"])
        run.summary_parts.append(text("clinic_yes_short", clinic=name, program=program))
        return "yes", (_conditions_of(run, r, program) if conditions else [])
    # перечень программы целиком, по всем видам помощи: «нет в перечне» проверяется по всей программе
    listing = run.call("clinics_query", program=program, limit=LISTING_LIMIT)
    lrows = [r for r in listing.get("rows") or [] if _prog_eq(r.get("program"), program)]
    every = run.st.clinics(run.cid, program=program)
    cities = {lemma_of(w) for r in every for w in _words(r.city or "")} | {lemma_of(w) for w in _words(city or "")}
    confident = bool(distinct) and confident_name(clinic, cities)
    doubt = any(distinct_overlap(clinic, r) or strict_name_match(clinic, r.clinic_name, r.legal_name) for r in every)
    if not lrows or not every or not confident or doubt:
        run.missing.append(text("missing_clinic_not_found", clinic=clinic, program=program))
        return "not_found", []
    run.looked_at.append(f"перечень клиник программы «{program}» (clinics_query, строк: {len(every)})")
    said = text("clinic_absent", clinic=clinic, program=program, n=listing.get("total", len(lrows)))
    run.say(said, [listing_citation(run, program, lrows, said)], kind=StatementKind.limitation,
            supports=["clinic_in_program"])
    run.summary_parts.append(text("missing_clinic_not_found", clinic=clinic, program=program))
    _other_programs(run, program, clinic)
    return "absent", []


# Пункт, который называет сам перечень клиник («Приложение 4. Список медицинских организаций.»): короткий пункт
# договора / программы со словами «перечень / список» + «медицинских организаций / учреждений / ЛПУ / клиник»
_LISTING_TITLE = re.compile(r"(?:перечн\w*|списо?к\w*)\s+(?:медицинск\w+\s+(?:организац|учрежден)\w*|лпу\b|"
                            r"лечебн\w+|клиник\w*)", re.IGNORECASE)
LISTING_TITLE_MAX = 200        # знаков: длиннее — это не название перечня, а пункт о порядке обслуживания


def listing_citation(run: _Run, program: str, lrows: list[dict[str, Any]], statement: str) -> DraftCitation:
    """Ссылка утверждения «клиники нет в перечне программы» (w7-core, ревизия 6): не на строку чужой клиники, а на
    пункт, который называет перечень клиник (_LISTING_TITLE: этой программы или общий, не длиннее
    LISTING_TITLE_MAX, связанный с текстом утверждения по citations.related), — пункт читается get_clause, чтобы
    ссылка была из прогона (citations.verify). Такого пункта нет (w7-fix, ревизия 7: ГК 1 и ГК 2 — ссылка шла на
    строку чужой клиники или «ЛПУ по выбору Страховщика»):
    1) строка-заголовок программы в той же таблице перечня перед первой строкой программы («Вариант № 2 Рязань
       Стандарт», ГК 1) — пункт таблицы, в тексте которого названа программа;
    2) таблица перечня целиком (citations.list_table_ref: «c05.program#t6») с цитатой-заголовком таблицы — программой
       её строк (ГК 2: первая строка таблицы документа — название программы); citations.verify проверяет, что строки
       таблицы вернулись в прогоне и цитата — заголовок этой таблицы или название документа.
    Только если и таблицу назвать нельзя (id строки другого вида) — первая строка перечня программы, как раньше."""
    from .citations import list_table_ref, normalize, related

    for cl in run.all_clauses():
        if len(cl.text) > LISTING_TITLE_MAX or not _LISTING_TITLE.search(cl.text):
            continue
        if not related(statement, [cl.text, cl.path]):
            continue
        if cl.program is not None and not program_matches(cl.program, program):
            continue
        run.call("get_clause", path=cl.path)
        return DraftCitation(ref=cl.id, quote=cl.text.strip())
    r = lrows[0]
    table = list_table_ref(str(r["ref"]))
    m = re.match(r".*#t\d+r(\d+)", str(r["ref"]))
    if table and m:
        first = int(m.group(1))
        heads = []
        for cl in run.all_clauses():
            k = re.fullmatch(re.escape(table) + r"r(\d+)(?:[@#p].*)?", cl.id)
            if k is None or int(k.group(1)) >= first or len(cl.text) > LISTING_TITLE_MAX:
                continue
            if normalize(program) in normalize(cl.text) and (cl.program is None or
                                                             program_matches(cl.program, program)):
                heads.append((int(k.group(1)), cl))
        if heads:
            cl = max(heads, key=lambda x: x[0])[1]
            run.call("get_clause", path=cl.path)
            return DraftCitation(ref=cl.id, quote=cl.text.strip())
    if table:
        return DraftCitation(ref=table, quote=str(r.get("program") or program))
    return DraftCitation(ref=r["ref"], quote=str(r.get("clinic_name") or ""))


def in_city(city: str, *where: str | None) -> bool:
    """Строка перечня в городе обращения: лемма города есть среди слов города, адреса или названия строки."""
    want = {lemma_of(w) for w in _words(city)}
    return bool(want & {lemma_of(w) for w in _words(" ".join(str(x or "") for x in where))})


def on_street(street: str, *where: str | None) -> bool:
    """Строка перечня на улице обращения: каждое слово названия улицы (≥ 4 букв, кроме типа улицы — config
    route_markers.yaml street_types) совпало со словом адреса или названия (street_word_match). Все слова, а не
    первое: «Красной Пресне» ≠ «Красногорск» (общее начало «красн»; демо-прогон К13, 26.10.2026)."""
    types = tuple(str(t) for t in (load_yaml("route_markers.yaml").get("street_types") or []))
    # тип улицы: короткое сокращение («пл», «пер») — только слово целиком («Плющева» — не «пл.»), полное — по началу
    def street_type(w: str) -> bool:
        return any(w == t or (len(t) >= 4 and w.startswith(t)) for t in types)

    keys = [w for w in _words(street) if len(w) >= 4 and not w.isdigit() and not street_type(w)]
    addr = _words(" ".join(str(x or "") for x in where))
    # число в названии улицы («1905 года», «8 Марта», «3-я Парковая») — тоже в адресе (w7-core); улица из одного
    # числа с «года» — по числу
    nums = [w for w in _words(street) if w.isdigit()]
    if nums and not all(n in addr for n in nums):
        return False
    if not keys:
        return bool(nums)
    return all(any(street_word_match(k, a) for a in addr) for k in keys)


def _row_key(r: ClinicRow) -> tuple[str, str]:
    """Клиника и адрес строки — без регистра, кавычек, ОПФ и пунктуации: «одна клиника по одному адресу»."""
    from .tools.clinics import normalize_name

    return normalize_name(r.clinic_name), normalize_name(r.address or "")


_LAT2CYR = [("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ya", "я"), ("yu", "ю"),
            ("yo", "ё"), ("ph", "ф"), ("x", "кс"), ("j", "дж")] + list(zip("abcdefghiklmnopqrstuvwyz",
                                                                       "абкдефгхиклмнопкрстуввиз", strict=True))
_LATIN = re.compile(r"[a-z]", re.IGNORECASE)


def translit_name(name: str) -> str:
    """Латиница названия кириллицей для нестрогого подбора кандидатов («Medsi» → «медси»; ревизия w6: на латиницу
    кандидатов не было, и у менеджера оставалась одна кнопка «Нет в перечне»). Только для подбора, не для решения."""
    out = name.casefold()
    for lat, cyr in _LAT2CYR:
        out = out.replace(lat, cyr)
    return out


def clinic_candidates(st: KitStore, cid: str, program: str, clinic: str, care: str | None, service: str | None,
                      *, city: str | None = None, street: str | None = None) -> list[ClinicRow]:
    """Кандидаты клиники для выбора менеджером (ADR 0010) — строки перечня ЭТОЙ программы ЭТОГО договора.

    Подбор нестрогий (tools/clinics.name_score ≥ NAME_THRESHOLD или distinct_overlap — похожее отличительное
    слово), порядок: строгие совпадения (tools/clinics.strict_name_match) раньше нестрогих; внутри — строки с видом
    помощи вопроса, затем строки в городе / на улице обращения, затем по сходству названия и порядку перечня.
    Одна клиника по одному адресу с одним видом помощи — одна строка; не больше CLINIC_CHOICE_MAX. Решения здесь
    нет: личность клиники подтверждает менеджер."""
    from .constants import CLINIC_CHOICE_MAX
    from .tools.clinics import NAME_THRESHOLD, distinct_overlap, name_score, strict_name_match

    sets = _care_stem_sets(care, service)
    scored: list[tuple[tuple[int, int, int, int, float, int], ClinicRow]] = []
    names = [clinic] + ([translit_name(clinic)] if _LATIN.search(clinic) else [])
    for i, r in enumerate(st.clinics(cid, program=program)):
        # латиница строки — ещё и кириллицей: «в медикус» ↔ «Medikus (ООО …)» (w7-fix)
        rows = [r] + ([r.model_copy(update={"clinic_name": translit_name(r.clinic_name),
                                            "legal_name": translit_name(r.legal_name or "")})]
                      if _LATIN.search(f"{r.clinic_name} {r.legal_name or ''}") else [])
        score = max(name_score(n, x) for n in names for x in rows)
        if score < NAME_THRESHOLD and not any(distinct_overlap(n, x) for n in names for x in rows):
            continue
        strict = any(strict_name_match(n, x.clinic_name, x.legal_name) for n in names for x in rows)
        care_fit = clinic_care_fits(r.care_type, sets) if sets else True
        in_town = not city or in_city(city, r.city, r.address, r.clinic_name)
        on_st = not street or on_street(street, r.address, r.clinic_name)
        # улица обращения раньше вида помощи: у сети строк больше CLINIC_CHOICE_MAX, филиал на названной улице
        # должен попасть в кандидаты (ревизия 6); город — после вида помощи, как раньше
        scored.append(((0 if strict else 1, 0 if on_st else 1, 0 if care_fit else 1, 0 if in_town else 1, -score,
                        i), r))
    scored.sort(key=lambda x: x[0])
    out: list[ClinicRow] = []
    seen: set[tuple[str, str, str]] = set()
    for _k, r in scored:
        key = (*_row_key(r), r.care_type.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= CLINIC_CHOICE_MAX:
            break
    return out


def _picked_clinic(run: _Run, program: str, clinic: str, care: str | None, service: str | None, pick: str,
                   *, conditions: bool = True) -> tuple[str, list[str]]:
    """Клиника по выбору менеджера (ADR 0010, CONTRACTS §8.1). Строгие проверки имени не нужны — личность клиники
    подтвердил человек; проверяется только, что выбор относится к перечню этой программы этого договора.

    CLINIC_PICK_NONE — «нет в перечне программы»: absent со ссылкой на перечень программы, если перечень получен
    (clinics_query вернул строки этой программы), иначе not_found (`docs/rules.md` п. 3а); затем другие программы
    договора (_other_programs). id строки — клиника = эта строка: yes по ней или по строке той же клиники и адреса
    с видом помощи вопроса (ссылка — строка перечня, условия — _conditions_of); вид помощи не подходит ни к одной
    строке этой клиники по этому адресу — not_found с видами помощи строк. Строка не из перечня программы
    договора — выбор не учитывается (selected=None, «не нашли»)."""
    from .constants import CLINIC_PICK_NONE

    choice = run.clinic_choice
    assert choice is not None
    if pick == CLINIC_PICK_NONE:
        # запрос по названию — вход проверки absent в citations.verify (клиника названа, и среди строк её нет);
        # перечень целиком — подтверждение, что перечень этой программы получен
        run.call("clinics_query", program=program, name=clinic, limit=CLINIC_ROWS_MAX)
        listing = run.call("clinics_query", program=program, limit=LISTING_LIMIT)
        lrows = [r for r in listing.get("rows") or [] if _prog_eq(r.get("program"), program)]
        if not lrows:
            run.missing.append(text("missing_clinic_pick_no_listing", program=program))
            return "not_found", []
        choice.selected = CLINIC_PICK_NONE
        total = listing.get("total", len(lrows))
        run.looked_at.append(f"перечень клиник программы «{program}» (clinics_query, строк: {total})")
        said = text("clinic_pick_absent", clinic=clinic, program=program, n=total)
        run.say(said, [listing_citation(run, program, lrows, said)], kind=StatementKind.limitation,
                supports=["clinic_in_program"])
        run.summary_parts.append(text("missing_clinic_not_found", clinic=clinic, program=program))
        _other_programs(run, program, clinic)
        return "absent", []
    every = run.st.clinics(run.cid, program=program)
    row = next((r for r in every if r.id == pick), None)
    if row is None:
        # чужой договор, чужая программа или выдуманный id — выбор не учитывается (ADR 0005, ADR 0010)
        run.missing.append(text("missing_clinic_pick_unknown", program=program))
        return "not_found", []
    choice.selected = pick
    if all(c.id != row.id for c in choice.candidates):
        # строка найдена поиском по перечню (GET …/clinic-candidates), а не из кандидатов — она первой, чтобы
        # карточка показывала выбранную строку (дополнение автора w7)
        from .constants import CLINIC_CHOICE_MAX

        choice.candidates = [row, *choice.candidates][:CLINIC_CHOICE_MAX]
    # строки той же клиники по тому же адресу: у строки один вид помощи, у клиники их может быть несколько
    same = [r for r in every if _row_key(r) == _row_key(row)]
    res = run.call("clinics_query", program=program, name=row.clinic_name, address=row.address,
                   limit=CLINIC_ROWS_MAX)
    returned = {str(x.get("ref")) for x in res.get("rows") or []}
    group = [r for r in same if r.id in returned]
    if row.id not in returned:
        # нечёткий поиск инструмента строку не вернул (длинный адрес, сеть больше предела выдачи) — строки клиники
        # прочитаны из перечня программы напрямую; запись прогона нужна citations.verify (ссылка — на прочитанное)
        run.records.append(ToolRecord(name="clinics_query", args={"program": program, "row_id": pick},
                                      refs={r.id for r in same}))
        group = same
    group = [row, *[r for r in group if r.id != row.id]]
    sets = _care_stem_sets(care, service)
    fit = [r for r in group if clinic_care_fits(r.care_type, sets)] if sets else [row]
    run.looked_at.append(f"перечень клиник программы «{program}» (строка, выбранная менеджером)")
    if not fit and all(not r.care_type.strip() for r in group):
        # выбранная строка без вида помощи (разметка оператора): «есть в перечне» — сведение, не clinic_in_program
        run.say(text("clinic_pick_listed_no_care", clinic=row.clinic_name, program=program),
                [DraftCitation(ref=row.id, quote=row.clinic_name)])
        return "not_found", []
    if not fit:
        run.say(text("clinic_pick_other_care", clinic=row.clinic_name, program=program,
                     care="; ".join(sorted({r.care_type for r in group}))[:200]),
                [DraftCitation(ref=row.id, quote=row.clinic_name)])
        return "not_found", []
    r = fit[0]
    statement = (text("clinic_pick_yes", clinic=r.clinic_name, program=program, care=r.care_type, address=r.address)
                 if r.address else text("clinic_pick_yes_short", clinic=r.clinic_name, program=program))
    run.say(statement, [DraftCitation(ref=r.id, quote=r.clinic_name)], supports=["clinic_in_program"])
    run.summary_parts.append(text("clinic_yes_short", clinic=r.clinic_name, program=program))
    if not conditions:
        return "yes", []
    return "yes", _conditions_of(run, {**r.model_dump(mode="json"), "ref": r.id}, program)


_NUM_IN = re.compile(r"\d")


def _name_numbers(r: dict[str, Any]) -> frozenset[str]:
    """Числа в названии строки перечня («12 КДЦ» → {"12"}) — подпись для «какая из клиник сети»."""
    from .tools.clinics import normalize_name

    return frozenset(re.findall(r"\d+", normalize_name(str(r.get("clinic_name") or ""))))


def street_word_match(a: str, b: str) -> bool:
    """Слово улицы из вопроса и слово адреса — одна улица: равные леммы или общее начало лемм не короче
    max(5, короткая лемма − 2) («варшавка» ↔ «варшавский», «чистопрудный» ↔ «чистопрудном»), а не первые 5 букв
    («краснопресненский» ≠ «красногорск»)."""
    la, lb = lemma_of(_norm(a)), lemma_of(_norm(b))
    if la == lb or _norm(a) == _norm(b):
        return True
    n = 0
    for x, y in zip(la, lb, strict=False):
        if x != y:
            break
        n += 1
    return n >= max(5, min(len(la), len(lb)) - 2)


def _other_programs(run: _Run, program: str, clinic: str) -> None:
    """Клиника есть в перечне ДРУГОЙ программы этого договора — отдельное утверждение (не альтернатива и не
    «доступна по программе застрахованного»; config/prompts/agent_sc02.md, ADR 0005 — только этот договор)."""
    res = run.call("clinics_query", name=clinic, limit=LISTING_LIMIT)
    seen: set[str] = set()
    for r in res.get("rows") or []:
        other = str(r.get("program") or "")
        if not other or _prog_eq(other, program) or other in seen:
            continue
        seen.add(other)
        name = str(r.get("clinic_name") or clinic)
        run.say(text("clinic_other_program", clinic=name, other=other, program=program),
                [DraftCitation(ref=r["ref"], quote=name)], kind=StatementKind.limitation)
        if len(seen) >= 3:
            break


def add_alternatives(run: _Run, program: str, care: str, *, near: str | None, exclude: str | None) -> None:
    """Альтернативы той же программы и вида помощи (clinics_pick, A6). Запись прогона — refs строк перечня."""
    from .scenarios.clinics_pick import pick

    alts = pick(run.cid, program, care, near=near, k=ALT_K, store=run.st, exclude=exclude)
    run.records.append(ToolRecord(name="clinics_pick", args={"program": program, "care_type": care},
                                  refs={a.clinic.id for a in alts}))
    run.alternatives += [DraftAlternative(row_id=a.clinic.id, reason=a.reason) for a in alts]


def _franchise_clauses(run: _Run, program: str) -> list[str]:
    """Вопрос о франшизе, а в строке клиники размер не указан: пункты договора/программы о франшизе."""
    stems = key_stems("франшиза")
    before = len(run.statements)
    res = run.call("search_clauses", query="франшиза", program=program, k=SEARCH_K)
    for h in res.get("hits") or []:
        got = run.cite_clause(h["ref"], stems)
        if got and _hits(stems, got[1]):
            run.say(got[1], [got[0]], kind=StatementKind.condition, supports=["conditions.franchise"])
            break
    return ["franchise"] if len(run.statements) > before else []


# ---------------------------------------------------------------- сценарии

def _ask_program(run: _Run, slots: dict[str, Any]) -> None:
    """Вопрос о программе: заданной в чате программы у договора нет — так и сказано (с её названием)."""
    nf = slots.get("program_not_found")
    run.questions.append(text("question_program_not_found", program=nf) if nf else text("question_program"))
    run.missing.append(text("missing_program_not_found", program=nf) if nf else text("missing_program"))


def resolve_program(cid: str, st: KitStore, context: ChatContext, slots: dict[str, Any]) -> str | None:
    """Программа: контекст → слот (только если она есть у ЭТОГО договора) → единственная программа договора."""
    names = [p.name for p in st.contract(cid).programs]
    for cand in (context.program, slots.get("program")):
        if cand:
            for n in names:
                if n.casefold() == str(cand).casefold():
                    return n
    return names[0] if len(names) == 1 else None


@dataclass(frozen=True)
class _Count:
    """Число обращений из вопроса: n — какое по счёту, unit — класс единицы (config limit_exceeded.unit_classes)."""
    n: int
    unit: str


@cache
def _limit_cfg() -> dict[str, Any]:
    return dict((load_yaml("coverage_markers.yaml") or {}).get("limit_exceeded") or {})


def _unit_class(word: str, *, service_stems: tuple[str, ...] = ()) -> str | None:
    """Класс единицы счёта по слову (config limit_exceeded.unit_classes): any (раз), visit (приём, консультация),
    procedure (процедура, сеанс, занятие), objects (образование); слово услуги («4-ю папиллому», «11-я ванна») —
    service. None — не единица обращений («звонок», «вторник»)."""
    w = _norm(word)
    for cls, stems in (_limit_cfg().get("unit_classes") or {}).items():
        if any(w.startswith(_norm(str(x))) for x in stems or []):
            return str(cls)
    if service_stems and any(w.startswith(st) for st in service_stems if len(st) >= 4):
        return "service"
    return None


def asked_counts(question: str, service_stems: tuple[str, ...] = ()) -> list[_Count]:
    """Все упоминания счёта обращений в вопросе (config coverage_markers.yaml limit_exceeded): «шестой приём»,
    «6-й сеанс», «11-я жемчужная ванна», «второй раз» → n; «уже 5 раз была», «6 раз уже был» → n + 1. Счёт рядом со
    словами-звонками («уже 6 раз звонили, не записывают») — не обращения: пропускается."""
    cfg = _limit_cfg()
    n = _norm(question)
    calls = re.compile(str(cfg.get("not_visits") or r"(?!x)x"), re.IGNORECASE)
    ords = {_norm(str(k)): int(v) for k, v in (cfg.get("ordinals") or {}).items()}
    end = r"(?:ой|ий|ый|ая|ое|ую|ого|ом|ому|ей|ья|ье|ьего|ьей|ьему|ые|ых)"
    out: list[_Count] = []

    def unit_after(pos: int) -> str | None:
        # единица — первое или второе слово после числа («6-й сеанс», «11-я жемчужная ванна», «4-ю папиллому»)
        for w in _WORD.findall(n[pos:pos + 40])[:2]:
            cls = _unit_class(w, service_stems=service_stems)
            if cls:
                return cls
        return None

    def near_calls(a: int, b: int) -> bool:
        return bool(calls.search(n[max(0, a - 25):b + 30]))

    for word, num in ords.items():
        for m in re.finditer(rf"\b{word}{end}\b", n):
            u = unit_after(m.end())
            if u and not near_calls(m.start(), m.end()):
                out.append(_Count(num, u))
    for m in re.finditer(r"\b(\d{1,3})\s*-?\s*(?:й|ой|ий|ый|я|е|ю|го|му)\b", n):
        u = unit_after(m.end())
        if u and not near_calls(m.start(), m.end()):
            out.append(_Count(int(m.group(1)), u))
    for rx in (r"уже\s+(\d{1,3})\s+(\w+)", r"\b(\d{1,3})\s+(\w+)\s+уже\b"):
        for m in re.finditer(rx, n):
            u = _unit_class(m.group(2), service_stems=service_stems)
            if u and not near_calls(m.start(), m.end()):
                out.append(_Count(int(m.group(1)) + 1, u))
    return out


def asked_count(question: str, service_stems: tuple[str, ...] = ()) -> int | None:
    """Какое по счёту обращение в вопросе; None — счёта нет или он неоднозначен (разные числа: «уже 10 процедур
    грязелечения, теперь ванны, первая процедура»; «4-я — нормально, а 6-я?»)."""
    nums = {c.n for c in asked_counts(question, service_stems)}
    return nums.pop() if len(nums) == 1 else None


def _period(s: str, rules: dict[str, Any]) -> str:
    """Период по тексту (config limit_exceeded.periods): course / hospital / month / other / year; иначе none."""
    t = _norm(s)
    for name, pats in rules.items():
        if any(re.search(str(p), t) for p in pats or []):
            return str(name)
    return "none"


def _units_compatible(asked: str, limit: str) -> bool:
    """Единица вопроса и лимита — одного рода (config limit_exceeded.compatible): «раз» — с любой; приём — с приёмом;
    процедура / сеанс — с процедурой; слово услуги — с процедурой, образованием, «раз»."""
    if "any" in (asked, limit):
        return True
    comp = _limit_cfg().get("compatible") or {}
    return limit == asked or limit in (comp.get(asked) or [])


@dataclass(frozen=True)
class _Limit:
    n: int
    unit: str
    period: str
    start: int
    end: int


def limits_in(item: str) -> list[_Limit]:
    """Лимиты пункта перечня (config limit_exceeded.limits): «не более 5 сеансов в год», «1 раз в год», «2 раза»,
    «однократно», «не более 3 (трех) образований», «до 5 приемов» — число, класс единицы и период после числа."""
    cfg = _limit_cfg()
    n = _norm(item)
    out: list[_Limit] = []
    for pat in cfg.get("limits") or []:
        for m in re.finditer(str(pat), n):
            gd = m.groupdict()
            num = 1 if gd.get("one") else int(gd.get("n") or 0)
            if num <= 0:
                continue
            unit = _unit_class(gd.get("unit") or "") or "any" if gd.get("unit") else "any"
            tail = n[m.end():m.end() + 60].split(";")[0]
            tail = re.split(r"не\s+более|однократн", tail)[0]
            period = _period(m.group(0) + " " + tail, cfg.get("periods") or {})
            if any(x.start <= m.start() < x.end for x in out):
                continue
            out.append(_Limit(num, unit, period, m.start(), m.end()))
    return out


def _periods_compatible(asked: str, limit: str) -> bool:
    """Период вопроса и лимита: годовой (или без периода) — с годовым/без периода; курс — с курсом;
    госпитализация — с госпитализацией. Месяц, «раз в 3 года» и прочее — не сравниваются (условие)."""
    yearly = {"year", "none"}
    if asked in yearly and limit in yearly:
        return True
    return asked == limit and asked in ("course", "hospital")


def limit_exceeded(run: _Run, question: str, program: str, checks: Checks, service: str | None = None,
                   skip: list[str] | None = None) -> None:
    """Лимит исчерпан → exclusion=yes («нельзя») со ссылкой на фрагмент лимита. Число из вопроса сравнивается с
    лимитом, только если совпали (config coverage_markers.yaml limit_exceeded; ревизия w5-recall — 3 ложных «нельзя»):
    (а) услуга: лимит стоит в том же пункте перечня (часть между «;»), где названа услуга вопроса (все её слова);
    (б) единица: «приём / сеанс / процедура / образование», а не «звонок» («6 раз звонили» — не приёмы);
    (в) период: вопрос о курсе не сравнивается с годовым лимитом и наоборот; «второй курс», «в новом году»,
        «вторая госпитализация» — счёт заново (сброс) — не сравнивается;
    и счёт в вопросе однозначен. Иначе — условие «лимит» со ссылкой (вывод — за менеджером)."""
    variants = service_terms(service, skip=skip) if service else []
    stems = tuple(dict.fromkeys(st for t in variants for st in t.required))
    asked = asked_counts(question, stems)
    nums = {c.n for c in asked}
    if len(nums) != 1:
        return
    cfg = _limit_cfg()
    nq = _norm(question)
    if any(re.search(str(p), nq) for p in cfg.get("reset") or []):
        return
    q_period = _period(nq, cfg.get("periods") or {})
    n_asked = nums.pop()
    units = {c.unit for c in asked}
    for st in list(run.statements):
        if "conditions.limit" not in st.supports:
            continue
        for c in st.citations:
            cl = run.clause(c.ref)
            full = cl.text if cl is not None else c.quote
            pos = 0
            for item in re.split(r";", full):
                a = full.find(item, pos)
                pos = a + len(item)
                if not variants or not _any_hit(variants, item, actions=False):
                    continue
                for lim in limits_in(item):
                    if not all(_units_compatible(u, lim.unit) for u in units):
                        continue
                    if not _periods_compatible(q_period, lim.period) or n_asked <= lim.n:
                        continue
                    quote = _limit_quote(full, a + lim.start, a + lim.end, a, a + len(item))
                    if not quote:
                        continue
                    msg = text("limit_exceeded", asked=n_asked, limit=lim.n, program=program)
                    # Переключатель config coverage_markers.yaml limit_exceeded.deny (автор, ревизия 4: лимит
                    # «каждого вида» и лимит другого раздела давали ложное «нельзя»). false — лимит только условие.
                    if (load_yaml("coverage_markers.yaml").get("limit_exceeded") or {}).get("deny", True):
                        run.say(msg, [DraftCitation(ref=c.ref, quote=quote)], kind=StatementKind.limitation,
                                supports=["exclusion"])
                        checks.exclusion = "yes"
                    else:
                        run.say(msg, [DraftCitation(ref=c.ref, quote=quote)], kind=StatementKind.limitation,
                                supports=["conditions", "conditions.limit"])
                        if "limit" not in checks.conditions:
                            checks.conditions.append("limit")
                    run.summary_parts.append(msg)
                    return


def _limit_quote(full: str, a: int, b: int, lo: int, hi: int) -> str:
    """Дословный фрагмент вокруг лимита (в границах пункта перечня, по границам слов, ≤ QUOTE_MAX)."""
    start = max(lo, a - 160)
    end = min(hi, b + 60, start + QUOTE_MAX)
    if start > lo:
        sp = full.find(" ", start)
        start = sp + 1 if 0 <= sp < a else start
    if end < hi:
        sp = full.rfind(" ", b, end)
        end = sp if sp > b else end
    q = full[start:end].strip()
    return q if len(q) >= 25 else ""


def _program_names(run: _Run) -> list[str]:
    """Программы договора: карточка + программы матрицы видов помощи (разбор мог найти их только в приложении)."""
    return list(dict.fromkeys([p.name for p in run.st.contract(run.cid).programs] +
                              [m.program for m in run.st.care(run.cid)]))


def _sc01(run: _Run, question: str, program: str | None, slots: dict[str, Any], checks: Checks) -> None:
    service = slots.get("service") or slots.get("care_type")
    care = slots.get("care_type") or slots.get("service")
    clinic = slots.get("clinic")
    if not program and not _program_names(run):
        # У договора нет ни одной программы (разбор неизвестной СК — adapters/generic.py, ADR 0011 п. 3 «limited»):
        # спрашивать программу бессмысленно — выбрать не из чего. Проверок матрицы и перечня нет, поэтому
        # Checks остаются пустыми (метка — «недостаточно данных», без «можно» / «нельзя», `docs/rules.md` п. 3а);
        # показываются найденные пункты договора дословно, со ссылками (как справочный ответ).
        run.missing.append(text("missing_no_programs"))
        # основы слов услуги, если она выделена: слова вопроса («входит ли», «положено ли») в пункте не стоят
        stems = key_stems(str(service)) if service else key_stems(question)
        _search_statements(run, question, None, stems or key_stems(question), k=SEARCH_K,
                           abroad=question_abroad(question))
        if service and (slots.get("refusal") or _refusal_in(question)):
            # этап w14-fix (ревизия 8, дефект 7): отказ в процедуре и у договора без программ — разбор по пунктам
            # самого договора (порядок плановой / экстренной госпитализации, исключения); матрицы нет — вид помощи
            # не проверяется, Checks по-прежнему пустые
            refusal_analysis(run, question, None, slots, str(service))
        return
    if not program:
        _ask_program(run, slots)
        return
    conds: list[str] = []
    cov, cc = Coverage(), CareCheck()
    if not service:
        run.missing.append(text("missing_service"))
    else:
        # слова программ, клиники и города — не слова услуги (route.service_phrase их не всегда отсекает)
        skip = [*name_context(run.st, run.cid, slots), str(clinic or "")]
        cc = check_care(run, program, care, service, skip=skip, question=question)
        if cc.value == "yes":
            run.summary_parts.append(text("care_yes", care=cc.name, program=program))
        elif cc.value == "absent":
            run.summary_parts.append(text("care_absent", care=cc.name, program=program))
        cov = check_coverage(run, program, str(slots.get("service") or service), cc.value,
                             care=slots.get("care_type"), care_row_service=cc.service_row, skip=skip,
                             abroad=question_abroad(question), question=question)
        checks.care_in_program, checks.exclusion = cov.care, cov.exclusion
        if cov.contradictory or cc.contradictory:
            checks.documents = "contradictory"
        conds += cc.conditions + cov.conditions
        if checks.exclusion != "yes" and not cov.contradictory:
            limit_exceeded(run, question, program, checks, str(slots.get("service") or service), skip)
        # пункты программы об услуге (лимиты, порядок) — справочно, без влияния на метку
        _search_statements(run, str(service), program, key_stems(str(service)), k=3,
                           abroad=question_abroad(question))
        if checks.exclusion != "yes" and (slots.get("refusal") or _refusal_in(question)):
            # этап w14-refusal: отказ клиники в процедуре — разбор по существу (класс процедуры по словарю → виды
            # помощи матрицы, порядок плановой / экстренной госпитализации, исключения); Checks не меняются
            got = refusal_analysis(run, question, program, slots, str(slots.get("service") or service))
            if got:
                # этап w14-fix (ревизия 8, дефект 3): клиника проверяется по виду помощи процедуры из разбора
                # (класс по словарю → вид помощи матрицы: «ампутация» — стационар), а не по слову услуги:
                # иначе строка перечня «Госпитализация» не подходит «услуге» и даёт ложное «другой вид помощи»
                care = got
    if clinic:
        checks.clinic_in_program, clinic_conds = check_clinic(run, program, str(clinic), care, service,
                                                              exact=bool(slots.get("clinic_exact")),
                                                              city=slots.get("city"), street=slots.get("street"),
                                                              pick=slots.get("clinic_pick"))
        conds += clinic_conds
    if _FRANCHISE_Q.search(question) and "franchise" not in conds:
        conds += _franchise_clauses(run, program)
    checks.conditions = list(dict.fromkeys(conds))
    if service and checks.care_in_program == "yes" and checks.clinic_in_program == "yes" and \
            not checks.conditions and checks.exclusion != "yes" and checks.documents != "contradictory":
        reason = allowed_blocker(question, checks, cov, cc)
        clinic_cite = _clinic_citation(run)
        if reason == "reason_refusal_dms" and clinic_cite is not None:
            # этап w5-strict (ревизия 4): клиника отказала или требует оплату (маркеры config/route_markers.yaml
            # refusal) — «можно» недопустимо, пока причина отказа не проверена: условие other «клиника отказала —
            # уточните причину» со ссылкой на строку перечня (config/verdict_rules.yaml conditions → «при условиях»)
            run.say(text("clinic_refusal_check", clinic=clinic_cite.quote, program=program), [clinic_cite],
                    kind=StatementKind.condition, supports=["conditions.other"])
            checks.conditions.append("other")
            run.missing.append(text("missing_allowed_unverified", reason=text(reason)))
        elif reason:
            # «можно» (config/verdict_rules.yaml covered) — только при полной проверке: услуга названа в тексте
            # «входит» (или сама — строка матрицы), исключения просмотрены, отказа клиники в обращении нет
            checks.care_in_program = "not_found"
            run.missing.append(text("missing_allowed_unverified", reason=text(reason)))
    if care and checks.care_in_program != "absent" and checks.exclusion != "yes" and \
            (checks.clinic_in_program == "absent" or not clinic):
        add_alternatives(run, program, str(care), near=slots.get("city"), exclude=clinic)


# ---------------------------------------------------------------- разбор отказа в процедуре (этап w14-refusal)
#
# Менеджеру нужен ответ «оправдан ли отказ», даже когда процедура («ампутация», «капельница», «роды») в программе
# буквально не названа. Связь процедура → класс → вид помощи матрицы — из СЛОВАРЯ config/service_classes.yaml
# (procedure_classes) и показывается как допущение («по словарю видов помощи», kind=inference), а не как условие
# договора. Всё, что идёт на экран, — со ссылкой на строку матрицы или пункт программы (citations.verify). Checks
# и метка SC 01 не меняются: новых путей к «можно» / «нельзя» нет (`docs/rules.md` п. 1–3а).

@dataclass(frozen=True)
class ProcClass:
    code: str
    name: str
    words: tuple[frozenset[str], ...]    # леммы слов и фраз класса
    care: tuple[str, ...]                # коды config/care_types.yaml по порядку важности
    orders: tuple[str, ...]              # способы оказания (config orders): planned / emergency
    exclusion_words: tuple[frozenset[str], ...]   # леммы / фразы самого класса: по ним исключение — к случаю
    major: tuple[frozenset[str], ...] = ()        # процедура масштаба стационара → major_care
    major_care: tuple[str, ...] = ()
    minor: tuple[frozenset[str], ...] = ()        # амбулаторная процедура → minor_care
    minor_care: tuple[str, ...] = ()
    skip_care: tuple[str, ...] = ()               # вид помощи слота, при котором класс не применяется


@dataclass(frozen=True)
class _Order:
    code: str
    name: str
    query: str
    patterns: tuple[re.Pattern[str], ...]
    requires: tuple[str, ...]
    min_chars: int


@dataclass(frozen=True)
class _ProcCfg:
    classes: tuple[ProcClass, ...]
    orders: dict[str, _Order]
    synonyms: dict[frozenset[str], tuple[str, ...]]
    generic: frozenset[str] = frozenset()                  # общие слова процедуры: не называют её саму
    not_service: frozenset[str] = frozenset()              # кто / где отказал: не процедура
    order_skip: tuple[re.Pattern[str], ...] = ()           # раздел о премии / взносах / расторжении
    order_prefer: tuple[re.Pattern[str], ...] = ()         # раздел о порядке оказания помощи


def _lemma_set(s: str) -> frozenset[str]:
    return frozenset(x.replace("ё", "е") for x in lemmas(s))


def _phrases(items: Any) -> tuple[frozenset[str], ...]:
    return tuple(w for w in (_lemma_set(str(x)) for x in items or []) if w)


@cache
def procedure_cfg() -> _ProcCfg:
    """Классы процедур, способы оказания и синонимы — config/service_classes.yaml (procedure_classes, orders,
    order_skip / order_prefer, procedures.synonyms / generic / not_service)."""
    raw = load_yaml("service_classes.yaml") or {}
    proc = raw.get("procedures") or {}
    classes = tuple(
        ProcClass(code=str(code), name=str(spec.get("name") or code), words=_phrases(spec.get("words")),
                  care=tuple(str(x) for x in spec.get("care") or []),
                  orders=tuple(str(x) for x in spec.get("orders") or []),
                  exclusion_words=_phrases(spec.get("exclusion_words")),
                  major=_phrases(spec.get("major")), major_care=tuple(str(x) for x in spec.get("major_care") or []),
                  minor=_phrases(spec.get("minor")), minor_care=tuple(str(x) for x in spec.get("minor_care") or []),
                  skip_care=tuple(str(x) for x in spec.get("skip_care") or []))
        for code, spec in (raw.get("procedure_classes") or {}).items())
    orders = {str(code): _Order(code=str(code), name=str(o.get("name") or code), query=str(o.get("query") or code),
                                patterns=_rx(o.get("patterns")),
                                requires=tuple(str(x) for x in o.get("requires") or []),
                                min_chars=int(o.get("min_chars") or 0))
              for code, o in (raw.get("orders") or {}).items()}
    syn = {_lemma_set(str(k)): tuple(str(x) for x in v or [])
           for k, v in (proc.get("synonyms") or {}).items()}
    return _ProcCfg(classes=classes, orders=orders, synonyms=syn,
                    generic=frozenset(lem for x in proc.get("generic") or [] for lem in _lemma_set(str(x))),
                    not_service=frozenset(lem for x in proc.get("not_service") or [] for lem in _lemma_set(str(x))),
                    order_skip=_rx(raw.get("order_skip")), order_prefer=_rx(raw.get("order_prefer")))


def procedure_classes(service: str | None, question: str = "", care: str | None = None) -> list[ProcClass]:
    """Классы процедуры по словарю, основной — первым: сначала по словам услуги, затем — вопроса («отказали в
    капельнице на дому»: в слоте услуги «на дому»). Слово / фраза класса — подмножество лемм текста. Класс не
    применяется при виде помощи слота из его skip_care: «удаление зуба» при вопросе о стоматологии — не хирургия
    стационара, а стоматологическое вмешательство. Остальные совпавшие классы дают свои слова исключений
    («удалить папиллому, говорят косметология» — хирургия и эстетическая процедура)."""
    from .scenarios.route import fix_typos

    code = care_code(care) or care_code(_care_name(care))
    out: list[ProcClass] = []
    # опечатки вопроса («больнитсу») — как в слотах (route.fix_typos, config procedures.typo)
    for s in (service or "", fix_typos(question or "")):
        order = [x.replace("ё", "е") for x in lemmas(s)]
        have = frozenset(order)
        got: list[tuple[int, int, ProcClass]] = []
        for k, pc in enumerate(procedure_cfg().classes):
            if pc in out or (code and code in pc.skip_care):
                continue
            pos = [min(order.index(x) for x in w) for w in pc.words if w <= have]
            if pos:
                # основной класс — слово, которое в услуге стоит раньше («операция варикоза косметики» —
                # хирургия); при равенстве — порядок config
                got.append((min(pos), k, pc))
        out += [pc for _p, _k, pc in sorted(got, key=lambda x: x[:2])]
    return out


def procedure_class(service: str | None, question: str = "", care: str | None = None) -> ProcClass | None:
    """Основной класс процедуры по словарю (procedure_classes) или None."""
    got = procedure_classes(service, question, care)
    return got[0] if got else None


def class_care(pc: ProcClass, service: str, question: str = "", care: str | None = None) -> tuple[str, ...]:
    """Виды помощи процедуры по масштабу (config procedure_classes major / minor): процедура масштаба стационара
    (ампутация, холецистэктомия, эндопротезирование) — только major_care, даже если вопрос назвал поликлинику или
    специалиста («хирург отказался ампутировать»); амбулаторная (удаление папилломы, вскрытие абсцесса) —
    minor_care; иначе — care класса. Вид помощи слота вопроса из списка — первым (не для major)."""
    have = _lemma_set(service) | _lemma_set(question)
    if pc.major and any(w <= have for w in pc.major):
        return pc.major_care or pc.care
    codes = pc.minor_care or pc.care if pc.minor and any(w <= have for w in pc.minor) else pc.care
    slot = care_code(care) or care_code(_care_name(care))
    return (slot, *[c for c in codes if c != slot]) if slot in codes else codes


def _care_name(care: str | None) -> str | None:
    """Название вида помощи слота care_type по синониму («стационар» → «Стационарная помощь»)."""
    from .scenarios.route import _care_types

    return _care_types(care)[0] if care else None


def procedure_synonyms(service: str) -> list[str]:
    """Другие названия процедуры (config procedures.synonyms): «удаление аппендицита» → «аппендэктомия»."""
    have = _lemma_set(service)
    return [x for k, v in procedure_cfg().synonyms.items() if k and k <= have for x in v]


def _class_care_rows(run: _Run, program: str, codes: tuple[str, ...]) -> list[tuple[str, dict[str, Any]]]:
    """(код вида помощи, строка матрицы) по видам помощи процедуры — первая подходящая строка каждого вида:
    «входит» раньше «не входит», общая строка раньше узкой (pick_care_rows, как в check_care)."""
    res = run.call("care_matrix", program=program)
    rows = [r for r in res.get("rows") or [] if _prog_eq(r.get("program"), program)]
    types = (load_yaml("care_types.yaml") or {}).get("care_types") or {}
    out: list[tuple[str, dict[str, Any]]] = []
    for code in codes:
        name = str((types.get(code) or {}).get("name") or code)
        sets = _care_stem_sets(name, None)
        matched = [r for r in rows if care_row_matches(str(r.get("care_type") or ""), sets)]
        fit, _mis = pick_care_rows(matched, "", name, None)
        row = next((r for r in fit if r.get("present")), None) or next(iter(fit), None)
        if row is not None and all(row is not x for _c, x in out):
            out.append((code, row))
    return out


ORDER_K = 12         # пунктов поиска, среди которых выбирается пункт о порядке госпитализации


def _order_clause(run: _Run, program: str | None, order: _Order, abroad: bool
                  ) -> tuple[DraftCitation, Clause] | None:
    """Пункт о порядке способа оказания (плановая / экстренная госпитализация): найден поиском, способ назван
    (order.patterns), есть условие порядка (order.requires), не заголовок (min_chars), не раздел исключений и не
    раздел о помощи за рубежом. Пункт раздела об оплате премии / взносах / неуплате / расторжении (config
    order_skip — по заголовкам раздела и тексту пункта: «Страховщик оплачивает исключительно экстренную помощь»
    при просрочке взноса) не берётся; пункт раздела о порядке оказания помощи (order_prefer: приложение-программа,
    «объём и порядок получения медицинских услуг») выбирается раньше. Ссылка — дословное предложение со способом."""
    cfg = procedure_cfg()
    res = run.call("search_clauses", query=order.query, program=program, k=ORDER_K)
    stems = key_stems(order.name)
    sections = run.sections()
    best: tuple[int, int, DraftCitation, Clause] | None = None
    for rank, h in enumerate(res.get("hits") or []):
        cl = run.clause(h["ref"])
        if cl is None or len(cl.text or "") < order.min_chars or sections.abroad(cl) != abroad:
            continue
        if sections.section(cl)[0] == "exclusion":
            continue
        heads = _norm(" ".join([cl.path or "", *(x.text[:200] for x in sections.chain(cl) if not x.own)]))
        if any(p.search(heads) or p.search(_norm(cl.text)) for p in cfg.order_skip):
            continue
        prefer = 0 if any(p.search(heads) for p in cfg.order_prefer) else 1
        if best is not None and (prefer, rank) >= best[:2]:
            continue
        for seg in _segments(cl):
            low = _norm(seg)
            if any(p.search(low) for p in order.patterns) and any(r in low for r in order.requires):
                dc = _cite_fragment(run, cl, seg, stems)
                if dc is not None:
                    best = (prefer, rank, dc, cl)
                    break
    return (best[2], best[3]) if best is not None else None


EXCL_SHOW = 3        # исключений-оснований отказа на экран
# оговорка «кроме / за исключением / исключая / не включая …» внутри перечня исключений: названное в ней —
# НЕ исключение («Трансплантация тканей, кроме переливания крови» — переливание не исключено). Граница — запятая,
# «;» или скобка.
_EXCEPT_TAIL = re.compile(r"(?:кроме|за\s+исключением|исключая|не\s+включая)\s+[^,;()]{3,160}")


def _lemma_hits(targets: Any, seg_lemmas: list[str]) -> list[str]:
    """Слова / фразы (множества лемм), все леммы которых есть во фрагменте. Лемма совпадает с леммой фрагмента:
    равна ей; одна — начало другой (≥ 5 букв: «стент» ↔ «стентирование», «эндопротез» ↔ «эндопротезирование»);
    общее начало не короче max(6, длина короткой − 2) («щитовидка» ↔ «щитовидной»), но не «пластиковый» ↔
    «пластический» и не «иммунизация» ↔ «иммунный». Короткие слова («нос», «ухо») — только равенство."""
    def same(a: str, b: str) -> bool:
        if a == b:
            return True
        short = min(len(a), len(b))
        if short < 5:
            return False
        if a.startswith(b) or b.startswith(a):
            return True
        n = 0
        while n < short and a[n] == b[n]:
            n += 1
        return n >= max(6, short - 2)

    def one(lem: str) -> bool:
        return any(same(lem, x) for x in seg_lemmas)

    out: list[str] = []
    for t in targets:
        if t and all(one(x) for x in t):
            out.append(" ".join(sorted(t)))
    return out


def procedure_words(service: str, question: str = "") -> tuple[list[frozenset[str]], frozenset[str]]:
    """(слова и фразы самой процедуры, анатомия вопроса). Процедура — леммы услуги (каждая отдельно) и синонимы
    (config procedures.synonyms — фразой: «внутривенное капельное введение» → все её значимые слова вместе) без
    общих слов (procedures.generic: «операция», «удаление», предлоги), без «кто / где отказал»
    (procedures.not_service), служебных и общих слов ДМС и без анатомии; анатомия — леммы вопроса из
    config/coverage_markers.yaml service.anatomy_words («нога», «колено», «сердце»)."""
    cfg = procedure_cfg()
    anat_cfg = coverage_cfg().anatomy
    anatomy = frozenset(x for x in _lemma_set(f"{service} {question}") if x in anat_cfg)

    def keep(x: str) -> bool:
        return x not in cfg.generic and x not in cfg.not_service and x not in STOPWORDS and \
            x not in _GENERIC and len(x) >= 3 and not x.isdigit()

    proc: list[frozenset[str]] = [frozenset([x]) for x in sorted(_lemma_set(service)) if keep(x) and x not in anatomy]
    for syn in procedure_synonyms(service):
        ph = frozenset(x for x in _lemma_set(syn) if keep(x))
        if ph and ph not in proc:
            proc.append(ph)
    return proc, anatomy


def _class_exclusions(run: _Run, program: str | None, classes: list[ProcClass], service: str, question: str,
                      abroad: bool) -> list[tuple[DraftCitation, list[str], Clause]]:
    """Исключения программы, которые могут быть основанием отказа. Исключение относится к случаю, только если во
    фрагменте (вне оговорки «кроме / за исключением …») названа САМА процедура (леммы услуги и её синонимов, без
    общих слов «операция / удаление» — procedures.generic), слово самого класса (exclusion_words: «пластические
    операции» для ринопластики, «экстракорпоральные методы» для гемодиализа) или анатомия вопроса. Общие слова
    класса одни — недостаточны: «удаление щитовидки» не подпадает под «пластические операции». Порядок — самое
    конкретное первым: процедура, затем слово класса, затем анатомия. Раздел другого вида помощи (стоматология при
    хирургии стационара) пропускается."""
    sections = run.sections()
    proc_t, anatomy = procedure_words(service, question)
    anat_t = [frozenset([x]) for x in sorted(anatomy)]
    cls_t = list(dict.fromkeys(w for pc in classes for w in pc.exclusion_words))
    if not (proc_t or anat_t or cls_t):
        return []
    family = {c for pc in classes for code in pc.care for c in _care_family(code)}
    want = {_stem(x)[:5] for t in (*proc_t, *anat_t, *cls_t) for x in t}
    scope = run.scope(program) if program else _contract_scope(run)
    found: list[tuple[int, int, int, int, Clause, str, list[str]]] = []
    for i, cl in enumerate(scope):
        low = _norm(cl.text)
        if not any(st in low for st in want):
            continue
        if sections.abroad(cl) != abroad or sections.section(cl)[0] != "exclusion":
            continue
        sc = section_care(sections, cl)
        if sc is not None and family and sc not in family:
            continue
        for seg in _segments(cl):
            main = _EXCEPT_TAIL.sub(" ", _norm(seg))
            seg_l = [x.replace("ё", "е") for x in lemmas(main)]
            p_hit, c_hit, a_hit = _lemma_hits(proc_t, seg_l), _lemma_hits(cls_t, seg_l), _lemma_hits(anat_t, seg_l)
            if p_hit or c_hit or a_hit:
                found.append((-len(p_hit), -len(c_hit), -len(a_hit), i, cl, seg,
                              list(dict.fromkeys([*p_hit, *c_hit, *a_hit]))))
                break
    out: list[tuple[DraftCitation, list[str], Clause]] = []
    for *_k, cl, seg, words in sorted(found, key=lambda x: x[:4]):
        dc = _cite_fragment(run, cl, seg, [_stem(x) for w in words for x in w.split()])
        if dc is not None and all(dc.ref != o.ref for o, _w, _c in out):
            out.append((dc, words, cl))
        if len(out) >= EXCL_SHOW:
            break
    return out


def _contract_scope(run: _Run) -> list[Clause]:
    """Пункты договора без программ (разбор неизвестной СК, ADR 0011): договор, доп. соглашения, программа."""
    types = (load_yaml("verdict_rules.yaml") or {}).get("absent_sources", {}).get("exclusion") or \
        ["program", "contract", "amendment"]
    return list(run.all_clauses(list(types)))


def procedure_cfg_verbs() -> list[str]:
    """Названия процедур словаря глаголов (config procedures.verbs) — не анатомия услуги."""
    return [str(v) for v in ((load_yaml("service_classes.yaml") or {}).get("procedures") or {}).get("verbs", {})
            .values()]


def _refusal_in(question: str) -> bool:
    from .scenarios.route import refusal_marker

    return bool(refusal_marker(question))


_SHORT_NUM = re.compile(r"\d{1,2}\.?")
SECTION_MAX = 60     # знаков названия раздела при коротком номере пункта


def _num(run: _Run, cl: Clause) -> str:
    """« (п. 2.8.1)» — номер пункта; короткий номер («1», «10.») без раздела ничего не говорит менеджеру — с
    названием ближайшего раздела: « (Страховщик не оплачивает › п. 10)»."""
    if not cl.number:
        return ""
    number = cl.number.strip()
    if _SHORT_NUM.fullmatch(number):
        for h in run.sections().chain(cl):
            head = re.sub(r"\s+", " ", h.text or "").strip().rstrip(":").strip()
            if h.own or not head or _NUMBER.fullmatch(head):
                continue
            if len(head) > SECTION_MAX:
                head = head[:SECTION_MAX].rsplit(" ", 1)[0] + "…"
            return text("refusal_clause_ref", section=head, number=number.rstrip("."))
    return text("refusal_clause_num", number=number)


def _where(program: str | None) -> dict[str, str]:
    """Подстановки «программы «X»» / «по программе «X»» или — у договора без программ — «договора»."""
    if program:
        return {"program": program, "of_prog": text("refusal_of_program", program=program),
                "by_prog": text("refusal_by_program", program=program)}
    return {"program": "", "of_prog": text("refusal_of_contract"), "by_prog": text("refusal_by_contract")}


def _last_hit(patterns: list[str], text_: str) -> int:
    """Позиция конца последнего совпадения любого шаблона в тексте; нет совпадений — -1."""
    pos = -1
    for p in patterns:
        for m in re.finditer(p, text_):
            pos = max(pos, m.end())
    return pos


def refusal_facts(question: str) -> dict[str, Any]:
    """Факты обращения о госпитализации со слов менеджера (config/texts_ru/reply.yaml refusal_facts): mode —
    «emergency» / «planned» / None, referral — есть ли направление и согласование (True / False / None), reason —
    названа ли причина отказа (reply.yaml reason_rx, как у compose). Названы оба варианта — берётся более поздний в
    тексте: ответ на уточнение дописан в конец вопроса (клиника сказала «нет направления» → менеджер: «есть»).
    Это слова менеджера, а не условия договора: в выводе они так и названы («со слов менеджера»)."""
    cfg = load_yaml("texts_ru/reply.yaml") or {}
    rf = cfg.get("refusal_facts") or {}
    n = _norm(question)
    em, pl = _last_hit(rf.get("emergency") or [], n), _last_hit(rf.get("planned") or [], n)
    # «экстренно или планово» в одном месте — это вопрос, а не факт
    both = em >= 0 and pl >= 0 and abs(em - pl) < 40
    mode = None if both or (em < 0 and pl < 0) else ("emergency" if em > pl else "planned")
    yes, no = _last_hit(rf.get("referral_yes") or [], n), _last_hit(rf.get("referral_no") or [], n)
    referral = None if yes < 0 and no < 0 else yes > no
    reason = bool(cfg.get("reason_rx") and re.search(cfg["reason_rx"], question or "", flags=re.IGNORECASE))
    return {"mode": mode, "referral": referral, "reason": reason}


def refusal_analysis(run: _Run, question: str, program: str | None, slots: dict[str, Any], service: str
                     ) -> str | None:
    """Разбор отказа клиники в процедуре (этап w14-refusal) — дополняет run утверждениями, кратким выводом и
    уточнениями. Возвращает вид помощи процедуры (название config/care_types.yaml) для проверки клиники —
    «""» — вида помощи нет; None — класс процедуры по словарю не найден (разбора нет).

    (а) класс процедуры по словарю → виды помощи по масштабу (class_care) → матрица программы (входит / не
        входит), со ссылкой на строку; у договора без программ (program=None) матрицы нет — этот шаг пропускается;
    (б) исключения, где названа сама процедура, слово её класса или анатомия вопроса, — возможное основание отказа;
    (в) порядок плановой (направление / согласование) и экстренной госпитализации — пункты программы / договора;
    (г) краткий вывод: входит ли вид помощи, при каких условиях отказ может быть обоснован (список не
        исчерпывающий), что уточнить;
    (д) уточнения не блокирующие (missing): экстренно или планово, направление, причина отказа.
    Знание «как обычно бывает» сюда не попадает: каждое утверждение — строка матрицы или пункт документа."""
    classes = procedure_classes(service, question, slots.get("care_type"))
    if not classes:
        return None
    pc = classes[0]
    where = _where(program)
    abroad = question_abroad(question)
    codes = class_care(pc, service, question, slots.get("care_type"))
    rows = _class_care_rows(run, program, codes) if program else []
    shown: list[tuple[str, bool, str]] = []      # (вид помощи, входит, код) — виды процедуры, найденные в матрице
    for code, row in rows:
        name = care_label(str(row.get("care_type") or ""))
        present = bool(row.get("present"))
        cites = _cite_matrix(run, row, key_stems(name))
        if not cites:
            continue
        run.say(text("refusal_class_care" if present else "refusal_class_care_absent", service=service,
                     klass=pc.name, care=name, program=program), cites, kind=StatementKind.inference, supports=[])
        shown.append((name, present, code))
    # в краткий вывод — первый вид процедуры, который входит; ни один не входит — первый найденный
    main = next((x for x in shown if x[1]), shown[0] if shown else None)
    # другие названия процедуры (config procedures.synonyms): пункты, где она названа так
    for syn in procedure_synonyms(service):
        _search_statements(run, syn, program, key_stems(syn), k=2, abroad=abroad)
    # (в) порядок способов оказания — только для классов со стационаром
    orders: dict[str, Clause] = {}
    for code in pc.orders:
        order = procedure_cfg().orders.get(code)
        got = _order_clause(run, program, order, abroad) if order is not None else None
        if got is None:
            continue
        dc, cl = got
        run.say(text(f"refusal_order_{code}", **where), [dc], kind=StatementKind.condition, supports=[])
        orders[code] = cl
    # (б) исключения, которые могут быть основанием отказа
    excl = _class_exclusions(run, program, classes, service, question, abroad)
    for dc, words, cl in excl:
        run.say(text("refusal_exclusion", words=", ".join(words), num=_num(run, cl), **where), [dc],
                kind=StatementKind.limitation, supports=[])
    # (г) краткий вывод
    parts: list[str] = []
    if not program:
        parts.append(text("refusal_sum_no_matrix", service=service, klass=pc.name))
    elif main is not None:
        parts.append(text("refusal_sum_care" if main[1] else "refusal_sum_care_absent", care=main[0],
                          program=program, service=service, klass=pc.name))
    else:
        parts.append(text("refusal_sum_no_care", program=program, service=service, klass=pc.name))
    # факты со слов менеджера (ответы на «Что уточнить», followup.clarify_fact): экстренно / планово, есть ли
    # направление и согласование, названа ли причина — меняют основания и итог, уже известное не спрашивается
    facts = refusal_facts(question)
    mode, referral = facts["mode"], facts["referral"]
    planned_num = _num(run, orders["planned"]) if "planned" in orders else ""
    emergency_num = _num(run, orders["emergency"]) if "emergency" in orders else ""
    grounds: list[str] = []
    justified: str | None = None      # основание отказа подтверждено фактами
    cleared: list[str] = []           # основание порядка госпитализации снято фактами
    if "planned" in orders:
        if mode == "emergency" and "emergency" in orders:
            parts.append(text("refusal_fact_emergency", num=emergency_num))
            cleared.append(text("refusal_why_emergency", num=emergency_num))
        elif referral is True:
            parts.append(text("refusal_fact_referral_ok", num=planned_num))
            cleared.append(text("refusal_why_referral_ok", num=planned_num))
        elif referral is False and mode == "planned":
            parts.append(text("refusal_fact_referral_no", num=planned_num))
            justified = text("refusal_why_planned_no_referral", num=planned_num)
        elif referral is False:
            # плановая ли — неизвестно: отказ обоснован только для плановой (основание с фактом, без итога)
            grounds.append(text("refusal_ground_planned_no_referral", num=planned_num))
        else:
            grounds.append(text("refusal_ground_planned", num=planned_num))
    if excl:
        grounds.append(text("refusal_ground_exclusion"))
    if main is not None and not main[1]:
        grounds.append(text("refusal_ground_care_absent", care=main[0]))
    if justified:
        parts.append(text("refusal_sum_justified", why=justified))
    elif grounds:
        parts.append(text("refusal_sum_grounds", grounds="; ".join(grounds)))
    elif cleared:
        parts.append(text("refusal_sum_cleared", why="; ".join(cleared)))
    else:
        parts.append(text("refusal_sum_no_grounds"))
    if "emergency" in orders and mode is None and not justified:
        parts.append(text("refusal_sum_emergency", num=emergency_num, **where))
    # экстренно или планово — не спрашиваем, если направление и согласование есть: условие выполнено в обоих случаях
    mode_needed = bool(orders) and mode is None and referral is not True
    asks = [text("refusal_ask_mode")] if mode_needed else []
    if "planned" in orders and referral is None and mode != "emergency":
        asks.append(text("refusal_ask_referral"))
    if not slots.get("clinic"):
        asks.append(text("refusal_ask_clinic"))
    if not facts["reason"]:
        asks.append(text("refusal_ask_reason"))
    if asks:
        parts.append(text("refusal_sum_ask", asks=", ".join(asks)))
    run.summary_parts.append(" ".join(parts))
    # (д) уточнения — не блокирующие: ответ уже дан; известное со слов менеджера не спрашивается
    if mode_needed:
        run.missing.append(text("refusal_missing_mode"))
    if "planned" in orders and referral is None and mode != "emergency":
        run.missing.append(text("refusal_missing_referral"))
    if not facts["reason"]:
        run.missing.append(text("refusal_missing_reason"))
    # вид помощи для проверки клиники: вид из разбора (матрица), а не слово услуги (дефект 3 ревизии 8)
    types = (load_yaml("care_types.yaml") or {}).get("care_types") or {}
    code = main[2] if main is not None else (codes[0] if codes else "")
    return str((types.get(code) or {}).get("name") or "")


def allowed_blocker(question: str, checks: Checks, cov: Coverage, cc: CareCheck) -> str | None:
    """Почему «можно» не ставится, хотя клиника в перечне и вид помощи входит (ключ текста config/texts_ru), или
    None. Первым — отказ клиники: в обращении клиника отказала, требует оплату или сказала «не по ДМС / не по
    страховке / платите сами» (маркеры config/route_markers.yaml refusal — те же, что у маршрутизатора, плюс
    config/coverage_markers.yaml allowed_blockers): отказ может быть правомерным (правило договора «специалист не
    ведёт приём по ДМС»), «можно» — только после проверки причины. Затем: исключения не просмотрены
    (exclusion≠absent) — «можно» без проверки исключений было бы обещанием; услуга не названа в тексте «входит» и
    сама не строка матрицы — «входит» только по виду помощи (поликлиника есть — не значит, что есть МРТ)."""
    from .scenarios.route import refusal_marker

    n = _norm(question)
    # отказ клиники / требование оплаты — те же маркеры, что у маршрутизатора (config/route_markers.yaml refusal),
    # плюс дополнительные шаблоны config/coverage_markers.yaml allowed_blockers (без повторов маркеров отказа)
    if refusal_marker(question) or refusal_marker(n) or any(p.search(n) for p in coverage_cfg().allowed_blockers):
        return "reason_refusal_dms"
    if checks.exclusion != "absent":
        return "reason_exclusions_unseen"
    if not cov.text_included and not cc.service_row:
        return "reason_service_unnamed"
    return None


def _clinic_citation(run: _Run) -> DraftCitation | None:
    """Ссылка утверждения «клиника в перечне программы» (supports clinic_in_program) — строка перечня клиник."""
    for st in run.statements:
        if "clinic_in_program" in st.supports and st.citations:
            return st.citations[0]
    return None


def programs_in(question: str, names: list[str]) -> list[str]:
    """Программы договора, названные в вопросе (длинные названия раньше: «Рязань Стандарт старше 60»)."""
    q = " " + " ".join(_words(question)) + " "
    found: list[str] = []
    for n in sorted(names, key=len, reverse=True):
        key = " " + " ".join(_words(n)) + " "
        if key.strip() and key in q:
            found.append(n)
            q = q.replace(key, " ")
    return found


def _compare(run: _Run, a: str, b: str) -> None:
    res = run.call("compare_programs", a=a, b=b)
    for d in res.get("care_diff") or []:
        refs = d.get("refs") or []
        if not refs:
            continue
        care = str(d.get("care_type"))
        stems = key_stems(care)
        cites = []
        for ref in refs:
            if run.clause(ref) is not None:
                got = run.cite_clause(ref, stems)
                if got:
                    cites = [got[0]]
                    break
        cites = cites or [DraftCitation(ref=refs[0], quote="")]
        run.say(text("compare_care", care=care, a=a, b=b, va=d.get(a), vb=d.get(b)), cites,
                kind=StatementKind.comparison)
    for side, other, key in ((a, b, "clinics_only_in_a"), (b, a, "clinics_only_in_b")):
        rows = res.get(key) or []
        if rows:
            r = rows[0]
            run.say(text("compare_clinics", a=side, n=res.get(f"{key}_total", len(rows)), clinic=r["clinic_name"]),
                    [DraftCitation(ref=r["ref"], quote=str(r["clinic_name"]))], kind=StatementKind.comparison)


def _payments(run: _Run, on: dt.date) -> None:
    res = run.call("payments_next", date=on.isoformat())
    nxt = res.get("next") if res else None
    if not nxt:
        run.missing.append(text("missing_no_payment", date=on.strftime("%d.%m.%Y")))
        return
    period = text("payment_period", period=nxt["period"]) if nxt.get("period") else ""
    st = text("payment_next", n=nxt["n"], amount=_money(nxt.get("amount")), due=_date_ru(nxt.get("due")),
              period=period)
    run.say(st, [DraftCitation(ref=res["ref"], quote="")])
    run.summary_parts.append(st)


@cache
def _card_cfg() -> tuple[tuple[tuple[str, tuple[re.Pattern[str], ...]], ...], re.Pattern[str] | None, dict[str, str]]:
    """Темы справочных вопросов о договоре, исключения и слова страховщиков (config/route_markers.yaml)."""
    cfg = load_yaml("route_markers.yaml") or {}
    topics = tuple((t, tuple(re.compile(p, re.IGNORECASE) for p in pats))
                   for t, pats in (cfg.get("card_topics") or {}).items())
    excl = re.compile(cfg["card_exclude"], re.IGNORECASE) if cfg.get("card_exclude") else None
    return topics, excl, dict(cfg.get("card_insurer_words") or {})


def card_topics(question: str, *, within: str | None = None) -> list[str]:
    """Темы справочного вопроса о договоре (route_markers.yaml card_topics): срок, численность, страховщик; вопрос
    о другом сроке / числе (card_exclude: «срок гарантийного письма», «сколько можно прикрепить») — не справка.
    within — проверка слота: темы, в совпадение которых в тексте within входят все слова question."""
    topics, excl, _w = _card_cfg()
    text_ = within if within is not None else question
    if not text_ or (excl is not None and excl.search(text_)):
        return []
    out: list[str] = []
    # слова самого договора («договор», «полис») в слоте не мешают: «числа действует договор» — тоже тема срока
    words = [w[:4] for w in re.findall(r"\w+", (question or "").casefold()) if w[:4] not in ("дого", "поли")]
    for topic, pats in topics:
        for rx in pats:
            m = rx.search(text_)
            if m and (within is None or (words and all(w in m.group(0).casefold() for w in words))):
                out.append(topic)
                break
    return out


_MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
               "ноября", "декабря")


def _date_rx(iso: str) -> str:
    """Дата во всех записях договоров комплекта: «01.05.2026», «1 мая 2026», ««01» мая 2026»."""
    d = dt.date.fromisoformat(iso)
    return (rf"(?:(?<![\d.])0?{d.day}\.{d.month:02d}\.{d.year}|(?<!\d)«?(?<!\d)0?{d.day}»?\s+{_MONTHS_GEN[d.month - 1]}"
            rf"\s+{d.year})")


def _card_rx(topic: str, card: dict[str, Any]) -> tuple[str, re.Pattern[str], re.Pattern[str]] | None:
    """(запрос поиска, признак пункта договора с этим фактом карточки, где в пункте само значение) — пункт должен
    содержать значение карточки и слова темы; цитата — окно вокруг значения; значения в карточке нет — None."""
    v = card.get(topic if topic != "period" else "period_to")
    if v in (None, "", "unknown"):
        return None
    if topic == "period":
        if not card.get("period_from"):
            return None
        rx = rf"(?=.*{_date_rx(card['period_from'])})(?=.*{_date_rx(card['period_to'])})(?=.*(?:срок|действ|вступа))"
        return ("срок действия договора вступает в силу действует до", re.compile(rx, re.IGNORECASE | re.DOTALL),
                re.compile(_date_rx(card["period_to"]), re.IGNORECASE))
    if topic == "headcount":
        num = rf"(?<![\d,.]){int(v)}(?![\d,.])"
        rx = rf"(?=.*(?:численност|количеств\w*\s+застрахованн))(?=.*{num})"
        return ("общая численность застрахованных составляет", re.compile(rx, re.IGNORECASE | re.DOTALL),
                re.compile(num))
    if topic == "insurer" and v in _card_cfg()[2]:
        name = _card_cfg()[2][v]
        rx = re.compile(rf"страховщик.{{0,40}}?{name}|{name}.{{0,200}}?страховщик", re.IGNORECASE | re.DOTALL)
        return f"{name} именуемое в дальнейшем страховщик", rx, re.compile(name, re.IGNORECASE)
    return None


def _card(run: _Run, topics: list[str]) -> int:
    """Справочные факты карточки договора по темам вопроса (card_topics): срок, численность, страховщик. Значение
    берётся из карточки, а ссылка — пункт договора / доп. соглашения, где стоит это же значение и слова темы
    (search_clauses, дословный фрагмент); такого пункта нет — факт не утверждается, в «Что уточнить» — куда
    смотреть (`docs/rules.md` п. 1: утверждение без ссылки на экран не попадает). Возвращает число утверждений."""
    res = run.call("contract_card")
    card = (res or {}).get("contract") or {}
    n = 0
    for topic in topics:
        spec = _card_rx(topic, card)
        cite = None
        if spec is not None:
            query, rx, value = spec
            hits = run.call("search_clauses", query=query, k=20, doc_types=["contract", "amendment"]).get("hits")
            for h in hits or []:
                cl = run.clause(h["ref"])
                if cl is not None and rx.search(cl.text):
                    m = value.search(cl.text)
                    cite = DraftCitation(ref=cl.id, quote=_shorten_quote(cl.text, m.end() if m else 0))
                    break
        if cite is None:
            run.missing.append(text("missing_card_field", field=text(f"card_field_{topic}")))
            continue
        if topic == "period":
            st = text("card_period", date_from=_date_ru(card["period_from"]), date_to=_date_ru(card["period_to"]))
        elif topic == "insurer":
            st = text("card_insurer", value=((load_yaml("texts_ru/ui.yaml") or {}).get("insurers") or {})
                      .get(card["insurer"], card["insurer"]))
        else:
            st = text(f"card_{topic}", value=card[topic])
        run.say(st, [cite])
        run.summary_parts.append(st)
        n += 1
    return n


QUOTE_TAIL = 30


def _shorten_quote(text: str, end: int = 0, limit: int = 300) -> str:
    """Дословный фрагмент пункта не длиннее limit (по границам слов), в котором есть значение, кончающееся на end
    (ревизия круга 2: «…составляет 67 человек» стояло за первыми 300 знаками пункта)."""
    if len(text.strip()) <= limit:
        return text.strip()
    stop = max(min(end + QUOTE_TAIL, len(text)), min(limit, len(text)))   # значение и несколько слов после него
    stop = text.find(" ", stop) if text.find(" ", stop) > 0 else len(text)
    start = max(0, stop - limit)
    if start:
        start = text.find(" ", start) + 1
    return text[start:stop].strip(" ,;:")


def _sc02(run: _Run, question: str, scenario: Scenario, program: str | None, slots: dict[str, Any],
          context: ChatContext) -> None:
    # программы договора: карточка + программы матрицы видов помощи (разбор мог найти их только в приложении)
    names = _program_names(run)
    stems = key_stems(question)
    # справочный вопрос о самом договоре — без вида помощи, клиники и услуги («срок действия полиса на МРТ» — нет);
    # слот услуги из слов самой темы («срок действия», «числа» из «до какого числа») справку не отменяет
    topics = [] if any(slots.get(k) for k in ("care_type", "clinic")) or \
        (slots.get("service") and not card_topics(str(slots["service"]), within=question)) else card_topics(question)
    if topics and not _PAY.search(question):
        _card(run, topics)
        return                               # справка из карточки — без поиска по тексту (он находил чужие сроки)
    if _PAY.search(question):
        on = context.date or (dt.date.fromisoformat(slots["date"]) if slots.get("date") else dt.date.today())
        _payments(run, on)
        _search_statements(run, PAYMENT_QUERY, None, key_stems(PAYMENT_QUERY), k=3,
                           doc_types=["contract", "amendment"])
        return
    named = programs_in(question, names)
    if _COMPARE.search(question) and len(named) >= 2:
        for b in named[1:]:
            _compare(run, named[0], b)
    care = slots.get("care_type")
    clinic = slots.get("clinic")
    if program and clinic:
        check_clinic(run, program, str(clinic), care, slots.get("service"), city=slots.get("city"),
                     pick=slots.get("clinic_pick"))
    elif program and (scenario in (Scenario.sc02_clinic, Scenario.sc02_add_clinic) or _CLINIC_Q.search(question)) \
            and care:
        res = run.call("clinics_query", program=program, care_type=care, city=slots.get("city"),
                       limit=LISTING_LIMIT)
        rows = res.get("rows") or []
        if rows:
            run.looked_at.append(f"перечень клиник программы «{program}» (clinics_query)")
            run.say(text("clinics_found", program=program, n=res.get("total", len(rows))),
                    [DraftCitation(ref=rows[0]["ref"], quote=str(rows[0].get("clinic_name") or ""))])
            add_alternatives(run, program, str(care), near=slots.get("city"), exclude=None)
    elif not program and len(names) > 1 and (care or clinic):
        run.questions.append(text("question_program_not_found", program=slots["program_not_found"])
                             if slots.get("program_not_found") else text("question_program"))
    if program and care:
        res = run.call("care_matrix", program=program)
        sets = _care_stem_sets(care, slots.get("service"))
        same = [r for r in res.get("rows") or [] if _prog_eq(r.get("program"), program) and
                care_row_matches(str(r.get("care_type") or ""), sets)]
        # строка подходящего способа оказания и класса услуги (config/service_classes.yaml, этап w12-care)
        for r in pick_care_rows(same, question, care, slots.get("service"))[0]:
                key = "care_matrix_yes" if r.get("present") else "care_matrix_absent"
                run.say(text(key, care=r["care_type"], program=program),
                        _cite_matrix(run, r, [s for x in sets for s in x]))
                break
    _search_statements(run, question, program, stems, k=SEARCH_K, abroad=question_abroad(question))


# ---------------------------------------------------------------- модель (необязательно)

SLOT_FIELDS = ("service", "care_type", "clinic", "city", "program")
SLOTS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": list(SLOT_FIELDS),
    # null — «в тексте не указано»: без этого маленькая модель угадывает вместо пустого слота (тогда — вопрос)
    "properties": {k: {"type": ["string", "null"]} for k in SLOT_FIELDS},
}
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["summary"],
    "properties": {"summary": {"type": "string"}},
}
# Какие слоты нужны сценарию, чтобы guided смог проверить документы (иначе — уточняющий вопрос)
NEEDED = {Scenario.sc01: ("service", "clinic"), Scenario.sc02_clinic: ("care_type", "city"),
          Scenario.sc02_add_clinic: ("clinic", "care_type")}


def _chat_json(llm: LLM, system: str, user: str, schema: dict[str, Any], max_tokens: int,
               usage: Usage) -> dict[str, Any] | None:
    """Один короткий вызов без инструментов со строгой схемой (constrained decoding там, где провайдер умеет).
    Повтор при невалидном JSON — внутри провайдера (BaseProvider.chat: одна попытка исправления)."""
    from .llm._common import structured_override

    try:
        with structured_override("on"):
            turn = llm.chat([Message("system", system), Message("user", user)], None, json_schema=schema,
                            max_tokens=max_tokens)
    except LlmError:
        return None
    usage.input_tokens += turn.usage.input_tokens
    usage.output_tokens += turn.usage.output_tokens
    try:
        data = extract_json(turn.text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def needed_slots(scenario: Scenario | str, slots: dict[str, Any]) -> list[str]:
    """Слоты сценария, которых нет (по ним решается, нужен ли вызов модели на извлечение)."""
    sc = Scenario(str(getattr(scenario, "value", scenario)))
    return [k for k in NEEDED.get(sc, ()) if not slots.get(k)]


def fill_slots(question: str, scenario: Scenario | str, slots: dict[str, Any], context: ChatContext,
               llm: LLM | None, *, store: KitStore | None = None, usage: Usage | None = None) -> dict[str, Any]:
    """Слоты, которых не нашли правила, — один короткий вызов модели. Слот модели принимается, только если он
    опирается на текст вопроса (route.grounded); вид помощи приводится к названию config/care_types.yaml."""
    from .scenarios.route import _care_types, _match_program, _programs, grounded

    if llm is None or not llm_slots_enabled() or not needed_slots(scenario, slots):
        return dict(slots)
    usage = usage if usage is not None else Usage()
    # В промпт — только вопрос и короткие списки имён (программы договора/ГК, виды помощи); фрагменты документов
    # модели не передаются: длинный промпт на CPU — десятки секунд, и модель не должна «читать» договор.
    programs = _programs(context, store)[:40]
    cares = [str(v.get("name")) for v in ((load_yaml("care_types.yaml") or {}).get("care_types") or {}).values()]
    user = (f"Программы: {'; '.join(programs) or 'не известны'}\nВиды помощи: {'; '.join(cares)}\n"
            f"<question>\n{question}\n</question>")
    data = _chat_json(llm, prompt(PROMPT_SLOTS), user, SLOTS_SCHEMA, SLOTS_MAX_TOKENS, usage)
    out = dict(slots)
    if not data:
        return out
    others = list(_programs(context, store))
    try:
        from .tools._common import default_store

        st = store or default_store()
        cs = [st.contract(context.contract_id)] if context.contract_id else st.contracts(context.group_id)
        others += [x.legal_entity for x in cs] + [x.number or "" for x in cs]
    except (KeyError, ValueError, OSError):
        pass
    others.append(str(slots.get("city") or data.get("city") or ""))
    # город — первым: «клиника» = названию города не принимается
    for k in ("city", *[f for f in SLOT_FIELDS if f != "city"]):
        v = str(data.get(k) or "").strip()
        if not v or out.get(k) or not grounded(k, v, question, slots):
            continue
        if k == "care_type":
            v = _care_types(v)[0] or ""          # только вид помощи из config/care_types.yaml
            if not v:
                continue
        if k == "program":
            v = _match_program(v, _programs(context, store)) or ""
            if not v:
                continue
        if k in ("clinic", "service") and not plausible_name(v, others=others):
            continue                              # «Москва», «ДМС», «Компания 13» — не клиника и не услуга
        out[k] = v
        if k == "city":
            others.append(v)
    if out.get("service") and not out.get("care_type"):
        care = _care_types(str(out["service"]))[0]
        if care:
            out["care_type"] = care
    return out


def generated(st: DraftStatement) -> bool:
    """Утверждение собрано кодом по шаблону (не дословный фрагмент и без фрагмента внутри) — только такие
    уходят модели на краткий вывод: тексты документов в промпт не передаются (длина промпта на CPU)."""
    quotes = [c.quote for c in st.citations if len(c.quote) > 20]
    return not any(q in st.text for q in quotes)


def _llm_summary(llm: LLM, question: str, statements: list[DraftStatement], usage: Usage) -> str:
    body = "\n".join(f"{i + 1}. {s.text}" for i, s in enumerate([x for x in statements if generated(x)][:6]))
    if not body:
        return ""
    data = _chat_json(llm, prompt(PROMPT_SUMMARY),
                      f"<question>\n{question}\n</question>\n<statements>\n{body}\n</statements>",
                      SUMMARY_SCHEMA, SUMMARY_MAX_TOKENS, usage)
    return str((data or {}).get("summary") or "").strip()


# ---------------------------------------------------------------- вход

ENGINE_MODES = ("agent", "guided", "auto")


def _flag(name: str) -> bool:
    from .llm._common import env

    return env(name, "").strip().lower() in ("1", "true", "yes", "on")


def llm_summary_enabled() -> bool:
    """DMS_GUIDED_LLM_SUMMARY=1 — модель формулирует краткий вывод по живым утверждениям (по умолчанию выкл.)."""
    return _flag("DMS_GUIDED_LLM_SUMMARY")


def llm_slots_enabled() -> bool:
    """DMS_GUIDED_LLM_SLOTS=1 — модель дополняет слоты (по умолчанию выкл.: замер ADR 0009 — qwen3:0.6b выдумывает
    слоты в 30 из 57 вопросов, 1.7b — в 10 из 57 и 9,6 с на CPU; сценарий и слоты ведут правила)."""
    return _flag("DMS_GUIDED_LLM_SLOTS")


def choose_scenario(question: str, context: ChatContext, *, store: KitStore | None = None,
                    classifier: Any = None) -> Any:
    """Сценарий и слоты для guided — правилами route.py (модель не вызывается). Точка расширения: classifier —
    необязательная функция question → Scenario | None (например, kNN по эмбеддингам; зависимость не добавлена).
    Явные маркеры отказа / «положено ли» (правила → sc01) важнее классификатора, как и ответа модели в route()."""
    from .scenarios.clarify import missing_facts
    from .scenarios.route import route

    r = route(question, context, llm=None, store=store)
    if classifier is not None and r.scenario != Scenario.sc01:
        try:
            guess = classifier(question)
        except Exception:      # noqa: BLE001 — сбой классификатора не ломает ответ: остаются правила
            guess = None
        if guess is not None:
            r.scenario = Scenario(str(getattr(guess, "value", guess)))
            r.source = "classifier"
            r.questions = missing_facts(r.scenario, r.slots, context, store=store)
    return r



def engine_mode(llm: Any) -> str:
    """Режим движка (ADR 0009): DMS_ENGINE_MODE=agent|guided|auto (по умолчанию auto).
    auto — guided, если модели нет или она «малая» (DMS_LLM_SMALL=1); иначе agent.
    agent без модели — прежний extractive движка (возвращается "extractive")."""
    from .llm._common import env

    mode = env("DMS_ENGINE_MODE", "auto").strip().lower()
    mode = mode if mode in ENGINE_MODES else "auto"
    if mode == "auto":
        small = env("DMS_LLM_SMALL", "").strip().lower() in ("1", "true", "yes", "on")
        mode = "guided" if llm is None or small else "agent"
    if mode == "agent" and llm is None:
        return "extractive"
    return mode


def run_guided(contract_id: str, question: str, *, scenario: Scenario | str, slots: dict[str, Any],
               context: ChatContext, store: KitStore | None = None, llm: LLM | None = None) -> AgentRun:
    """Один договор — один прогон кодом. Тот же AgentRun, что у run_agent: дальше verify → decide_label → compose."""
    cid, st = resolve(contract_id, store)
    execute = bound_executor(cid, st)
    sc = Scenario(str(getattr(scenario, "value", scenario)))
    usage = Usage()
    steps = 0
    if llm is not None and llm_slots_enabled() and needed_slots(sc, slots):
        slots = fill_slots(question, sc, slots, context, llm, store=st, usage=usage)
        steps += 1
    run = _Run(cid=cid, st=st, execute=execute)
    program = resolve_program(cid, st, context, slots)
    # программа задана (в чате или в вопросе), но у этого договора такой нет — вопрос «какая программа?» говорит об
    # этом прямо, а не «не известна» (менеджер её выбирал; аудит уточнений 29.10.2026)
    given = context.program or slots.get("program")
    if program is None and given:
        slots = {**slots, "program_not_found": str(given)}
    checks: Checks | None = None
    if sc == Scenario.sc01:
        checks = Checks()
        _sc01(run, question, program, slots, checks)
    else:
        _sc02(run, question, sc, program, slots, context)
    summary = " ".join(dict.fromkeys(run.summary_parts))
    if llm is not None and llm_summary_enabled() and any(generated(x) for x in run.statements):
        got = _llm_summary(llm, question, run.statements, usage)
        steps += 1
        summary = got or summary
    draft = DraftAnswer(contract_id=cid, summary=summary, checks=checks, statements=run.statements,
                        alternatives=run.alternatives, contradictions=run.contradictions, looked_at=run.looked_at,
                        missing=list(dict.fromkeys(run.missing)), questions=list(dict.fromkeys(run.questions)),
                        clinic_choice=run.clinic_choice)
    version = f"guided@2+{PROMPT_SLOTS}@{prompt_version(PROMPT_SLOTS)}+{PROMPT_SUMMARY}@{prompt_version(PROMPT_SUMMARY)}"
    return AgentRun(draft=draft, tools=run.records, steps=steps, usage=usage,
                    model=getattr(llm, "model", "") if llm is not None else "guided", error=None,
                    prompt_version=version)


__all__ = ["fill_slots", "fragment", "needed_slots", "run_guided"]
