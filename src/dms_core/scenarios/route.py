"""Маршрутизация: сценарий и слоты обращения (docs/CONTRACTS.md §7, план §6.6–6.8).

С моделью — один вызов по config/prompts/route.md (JSON {scenario, slots}); пропуски слотов дополняются правилами.
Без модели или при сбое модели — только правила по словам (ниже). Затем `missing_facts` по
config/required_facts.yaml даёт уточняющие вопросы. Маршрутизатор ничего не решает о покрытии — только тема и факты.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import weakref
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from ..config import load_yaml, prompt
from ..constants import Scenario
from ..index.morph import _analyzer, lemmas, tokens
from ..llm.base import LLM, LlmError, Message
from ..schemas import ChatContext
from ..store.db import KitStore
from .clarify import missing_facts

SLOTS = ("service", "care_type", "clinic", "program", "city", "date", "relative", "refusal", "contract")
# rule_slots ставит и служебные слоты: clinic_exact (название в кавычках), street (улица филиала после названия)


@dataclass
class Route:
    scenario: Scenario
    slots: dict[str, str]
    questions: list[str]
    source: str = "rules"          # rules | llm — кто определил сценарий
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- правила по словам (без модели)
# Порядок важен: добавление клиники → отказ / «положено ли» → подбор клиники → условия → общее.

def _markers(key: str) -> re.Pattern[str]:
    """Маркеры маршрута config/route_markers.yaml[key]: список регулярных выражений, склеенный через «|»
    (без учёта регистра). Пустой список — шаблон, который ничего не находит."""
    items = [str(x) for x in (load_yaml("route_markers.yaml") or {}).get(key) or []]
    return re.compile("|".join(f"(?:{x})" for x in items) if items else r"(?!x)x", re.IGNORECASE)


# Маркеры — config/route_markers.yaml (порядок проверки и смысл групп — в шапке файла). Отказ или требование оплаты
# — один класс (ТЗ п. 5.1, ADR 0004); найденный маркер отказа — слот refusal (при нём правило «можно по программе
# без клиники» не срабатывает, config/verdict_rules.yaml, refusal: no).
_ADD_CLINIC = _markers("add_clinic")
_REFUSAL = _markers("refusal")
_COMPARE = _markers("compare")
_INFO_FIRST = _markers("info_first")
_PICK_FIRST = _markers("pick_first")
_COVERAGE = _markers("coverage")            # вопрос о покрытии услуги: SC 01, kNN не перебивает
_COVERAGE_SOFT = _markers("coverage_soft")  # «можно ли / может ли»: SC 01 по правилам, kNN может перебить
_PICK = _markers("pick")
_INFO = _markers("info")

_DATE_RU = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_QUOTED = re.compile(r"[«\"“]([^»\"”]{2,80})[»\"”]")
_PROGRAM_AFTER = re.compile(r"программ\w*\s+[«\"“]?([А-ЯЁA-Z][\w\-\. ]{1,40}?)[»\"”]?(?=[\s,.?!;:)]|$)")
_CLINIC_AFTER = re.compile(r"(?:клиник\w*|медцентр\w*|медицинск\w+\s+центр\w*|центр\w*)\s+[«\"“]?"
                           r"([А-ЯЁA-Z][\w\-\.]*(?:\s+[А-ЯЁA-Z0-9][\w\-\.]*){0,3})")
_RELATIVES = {
    "супруг": "супруг(а)", "жена": "супруг(а)", "муж": "супруг(а)", "ребенок": "ребёнок", "ребёнок": "ребёнок",
    "сын": "ребёнок", "дочь": "ребёнок", "дети": "ребёнок", "мать": "родитель", "мама": "родитель",
    "отец": "родитель", "папа": "родитель", "родитель": "родитель", "родственник": "родственник",
}


_QUESTION_END = re.compile(r"[^?]*\?")


def _ordered(q: str) -> Scenario | None:
    for rx, sc in ((_PICK_FIRST, Scenario.sc02_clinic), (_INFO_FIRST, Scenario.sc02_info),
                   (_COVERAGE, Scenario.sc01), (_COVERAGE_SOFT, Scenario.sc01), (_PICK, Scenario.sc02_clinic),
                   (_INFO, Scenario.sc02_info)):
        if rx.search(q):
            return sc
    return None


def rule_scenario(question: str) -> Scenario:
    """Сценарий по маркерам config/route_markers.yaml: первое совпадение по порядку групп (шапка файла).
    Добавление клиники, отказ и сравнение программ ищутся во всём тексте; остальные группы — сначала в ПЕРВОМ вопросе обращения из
    нескольких («Входит ли клиника в программу? Если нет — какие клиники доступны?» — вопрос о покрытии, а
    уточнение после него — продолжение), потом во всём тексте."""
    q = question or ""
    if _ADD_CLINIC.search(q):
        return Scenario.sc02_add_clinic
    if refusal_marker(q):
        return Scenario.sc01
    if _COMPARE.search(q):
        return Scenario.sc02_info
    first = _QUESTION_END.match(q)
    if first and first.end() < len(q.rstrip()):
        got = _ordered(first.group())
        if got is not None:
            return got
    return _ordered(q) or Scenario.general


def firm_sc01(question: str) -> bool:
    """Явный отказ или явный вопрос о покрытии услуги (rule_scenario → sc01 по группам refusal / coverage):
    kNN по эмбеддингам такой сценарий не перебивает."""
    q = question or ""
    if refusal_marker(q):
        return True
    first = _QUESTION_END.match(q)
    return rule_scenario(q) == Scenario.sc01 and bool(_COVERAGE.search(first.group() if first else q))


def refusal_marker(question: str) -> str | None:
    """Маркер отказа или требования оплаты в обращении (config/route_markers.yaml refusal) — тот же, что задаёт
    сценарий SC 01 и слот refusal; им же guided.allowed_blocker запрещает «можно» (этап w5-strict: один список).
    Этап w14-refusal: ещё отказ в процедуре — глагол словаря процедур после «не» («не стали оперировать», «не
    госпитализировали», «не загипсовали») или перед «не будем / не стали» («резать не будем», этап w14-fix) и
    шаблоны config/service_classes.yaml procedures.refusal («не положили в больницу»). Опечатки («атказали») —
    по словарю procedures.typo (fix_typos), если без исправления маркера нет."""
    q = question or ""
    m = _REFUSAL.search(q)
    if m:
        return m.group(0)
    got = procedure_refusal(q)
    if got:
        return got
    fixed = fix_typos(q)
    if fixed != q:
        m = _REFUSAL.search(fixed)
        return m.group(0) if m else procedure_refusal(fixed)
    return None


@dataclass(frozen=True)
class _ProcCfg:
    verbs: dict[str, str]              # лемма глагола → название процедуры
    fuzzy: int
    fuzzy_min: int
    refusal: re.Pattern[str] | None
    after_negation: re.Pattern[str] | None = None   # «резать не будем»: глагол словаря + это
    not_done: re.Pattern[str] | None = None         # «не сделали / не поставили» при слове класса процедуры
    typo_ratio: int = 101
    typo_min: int = 7
    typo_words: tuple[str, ...] = ()
    not_service: frozenset[str] = frozenset()
    refusal_service: str = ""                       # процедура шаблонов refusal («не положили в больницу»)


@cache
def procedures_cfg() -> _ProcCfg:
    """Словарь процедур config/service_classes.yaml procedures (этап w14-refusal, w14-fix)."""
    raw = (load_yaml("service_classes.yaml") or {}).get("procedures") or {}
    pats = [str(x) for x in raw.get("refusal") or []]
    typo = raw.get("typo") or {}
    after = str(raw.get("after_negation") or "")
    return _ProcCfg(verbs={str(k).replace("ё", "е"): str(v) for k, v in (raw.get("verbs") or {}).items()},
                    fuzzy=int(raw.get("fuzzy") or 101), fuzzy_min=int(raw.get("fuzzy_min") or 6),
                    refusal=re.compile("|".join(f"(?:{x})" for x in pats), re.IGNORECASE) if pats else None,
                    after_negation=re.compile(r"([а-яё-]+)" + after, re.IGNORECASE) if after else None,
                    not_done=re.compile(str(raw["not_done"]), re.IGNORECASE) if raw.get("not_done") else None,
                    typo_ratio=int(typo.get("ratio") or 101), typo_min=int(typo.get("min") or 7),
                    typo_words=tuple(str(x) for x in typo.get("words") or []),
                    not_service=frozenset(lem for x in raw.get("not_service") or [] for lem in lemmas(str(x))),
                    refusal_service=str(raw.get("refusal_service") or ""))


@cache
def _verb_forms() -> dict[str, str]:
    """Формы глаголов словаря (pymorphy lexeme: «прооперировали», «отнимать») → инфинитив словаря — для
    опечатки в форме глагола («прооперирвали»): сходство с формой точнее, чем с инфинитивом."""
    out: dict[str, str] = {}
    for v in procedures_cfg().verbs:
        for p in _analyzer().parse(v)[:2]:
            if str(p.tag.POS) == "INFN" and p.normal_form.replace("ё", "е") == v:
                for f in p.lexeme:
                    out.setdefault(f.word.replace("ё", "е"), v)
                break
        out.setdefault(v, v)
    return out


def procedure_verb(word: str) -> str | None:
    """Название процедуры по глаголу (config procedures.verbs): «отрезать» → «ампутация». Лемма — pymorphy
    (глагол или инфинитив); опечатка («атрезать», «прооперирвали») — сходство с формой глагола словаря не ниже
    procedures.fuzzy, слово не короче fuzzy_min и неизвестно pymorphy."""
    from rapidfuzz import fuzz, process

    cfg = procedures_cfg()
    if not cfg.verbs or not re.fullmatch(r"[А-Яа-яЁё-]+", word or ""):
        return None
    low = word.casefold().replace("ё", "е")
    for p in _analyzer().parse(low)[:3]:
        lem = p.normal_form.replace("ё", "е")
        if str(p.tag.POS) in ("INFN", "VERB", "PRTS", "GRND") and lem in cfg.verbs:
            return cfg.verbs[lem]
    if low in cfg.verbs:
        return cfg.verbs[low]
    verbal = any(str(p.tag.POS) in ("INFN", "VERB", "PRTS", "GRND") for p in _analyzer().parse(low)[:2])
    if len(low) >= cfg.fuzzy_min and verbal and not _analyzer().word_is_known(low):
        # только слово, похожее на глагол (догадка pymorphy по окончанию): «перелечивание» — не «переливать»
        forms = _verb_forms()
        got = process.extractOne(low, list(forms), scorer=fuzz.ratio)
        if got is not None and got[1] >= cfg.fuzzy:
            return cfg.verbs[forms[got[0]]]
    return None


_NEG_VERB = re.compile(r"(?<![\wа-яё])не((?:\s+[а-яё-]+){1,3})", re.IGNORECASE)


def procedure_refusal(question: str) -> str | None:
    """Отказ в процедуре без общего маркера отказа: «не» + до двух слов + глагол словаря процедур («не стали
    удалять», «не хочет оперировать»), глагол словаря + «не будем / не стали / не хотят» (procedures.after_negation:
    «резать не будем»), или шаблон config procedures.refusal («не положили в больницу», «не перевели в
    реанимацию»). Возвращается найденный фрагмент (значение слота refusal) или None."""
    q = (question or "").replace("ё", "е").replace("Ё", "Е")
    cfg = procedures_cfg()
    for m in _NEG_VERB.finditer(q):
        if any(procedure_verb(w) for w in m.group(1).split()):
            return m.group(0)
    if cfg.after_negation is not None:
        for m in cfg.after_negation.finditer(q):
            if procedure_verb(m.group(1)):
                return m.group(0)
    rx = cfg.refusal
    got = rx.search(q.casefold()) if rx is not None else None
    if got:
        return got.group(0)
    done = cfg.not_done.search(q) if cfg.not_done is not None else None
    if done and procedure_class_word(q):
        return done.group(0)
    return None


@cache
def _class_phrases() -> tuple[frozenset[str], ...]:
    """Слова и фразы классов процедур config/service_classes.yaml procedure_classes (леммы)."""
    raw = load_yaml("service_classes.yaml") or {}
    out: list[frozenset[str]] = []
    for spec in (raw.get("procedure_classes") or {}).values():
        for w in spec.get("words") or []:
            got = frozenset(x.replace("ё", "е") for x in lemmas(str(w)))
            if got:
                out.append(got)
    return tuple(out)


def _class_lemma(lem: str) -> bool:
    """Лемма — одиночное слово класса процедуры (config procedure_classes words)."""
    low = (lem or "").casefold().replace("ё", "е")
    return any(len(w) == 1 and low in w for w in _class_phrases())


def procedure_class_word(question: str) -> bool:
    """В вопросе есть слово или фраза класса процедуры (config procedure_classes words)."""
    have = {x.replace("ё", "е") for x in lemmas(question or "")}
    return any(w <= have for w in _class_phrases())


@cache
def _typo_vocab() -> dict[str, str]:
    """Словарь исправления опечаток (config procedures.typo): все формы pymorphy глаголов словаря процедур, слов
    классов процедур (procedure_classes words — одиночные слова) и typo.words. Форма → она же."""
    raw = load_yaml("service_classes.yaml") or {}
    base = [*procedures_cfg().verbs, *procedures_cfg().typo_words]
    for spec in (raw.get("procedure_classes") or {}).values():
        base += [str(w) for w in spec.get("words") or [] if " " not in str(w) and not is_abbreviation(str(w))]
    out: dict[str, str] = {}
    for w in base:
        low = w.casefold().replace("ё", "е")
        for p in _analyzer().parse(low)[:1]:
            for f in p.lexeme:
                out.setdefault(f.word.replace("ё", "е"), f.word)
        out.setdefault(low, low)
    return {k: v for k, v in out.items() if len(k) >= 4}


_TYPO_WORD = re.compile(r"(?<![\wА-Яа-яЁё-])[а-яё]+(?![\wА-Яа-яЁё-])")


@cache
def fix_typos(question: str) -> str:
    """Вопрос с исправленными опечатками в словах словаря процедур (config procedures.typo): слово со строчной
    буквы, не короче typo.min букв, которого нет в словаре pymorphy, заменяется формой словаря со сходством не
    ниже typo.ratio (rapidfuzz ratio): «апперации» → «операции», «ампутацыю» → «ампутацию», «больнитсу» →
    «больницу», «атказали» → «отказали». Слова с заглавной буквы (названия клиник) не трогаются. Только для разбора
    слотов: текст обращения в ответе — как есть."""
    from rapidfuzz import fuzz, process

    cfg = procedures_cfg()
    q = question or ""
    if cfg.typo_ratio > 100:
        return q
    vocab = _typo_vocab()
    keys = list(vocab)

    def repl(m: re.Match[str]) -> str:
        w = m.group(0)
        low = w.replace("ё", "е")
        if len(low) < cfg.typo_min or low in vocab or _analyzer().word_is_known(low):
            return w
        got = process.extractOne(low, keys, scorer=fuzz.ratio, score_cutoff=cfg.typo_ratio)
        # длина почти та же (опечатка — одна буква лишняя или пропущена): не «видеоэндоскопию» → «эндоскопию»,
        # не «перелечивание» → «переливание»
        return vocab[got[0]] if got is not None and abs(len(got[0]) - len(low)) <= 1 else w

    return _TYPO_WORD.sub(repl, q)


def embedding_scenario(question: str) -> Scenario | None:
    """Сценарий kNN, если энкодер доступен и голос уверенный; иначе None (решают правила)."""
    from ..config import load_yaml
    from .knn_route import knn_scenario

    vote = knn_scenario(question)
    if vote is None:
        return None
    return vote.scenario if vote.margin >= float(load_yaml("route_examples.yaml").get("min_margin", 0.5)) else None


def _programs(context: ChatContext, store: KitStore | None) -> list[str]:
    """Названия программ договора чата (или всех договоров ГК) — для распознавания слота program."""
    try:
        from ..tools._common import default_store

        st = store or default_store()
        if context.contract_id:
            return [p.name for p in st.contract(context.contract_id).programs]
        if context.group_id:
            return sorted({p.name for c in st.contracts(context.group_id) for p in c.programs})
    except (KeyError, ValueError, OSError, sqlite3.Error):   # нет базы — программы не распознаём, вопрос задаст clarify
        return []
    return []


def _match_program(text: str, programs: list[str]) -> str | None:
    t = text.strip().casefold().replace("ё", "е")
    for p in programs:
        if p.casefold().replace("ё", "е") == t:
            return p
    tl = lemmas(text)
    for p in programs:
        pl = lemmas(p)
        if pl and pl == tl:
            return p
    return None


def _squeeze(text: str) -> str:
    return re.sub(r"[\s\-]+", "", text.casefold().replace("ё", "е"))


def _care_types(question: str) -> tuple[str | None, str | None]:
    """(вид помощи — название из config/care_types.yaml, совпавшая фраза вопроса) по леммам синонимов."""
    have = set(lemmas(question))
    low = question.casefold()
    squeezed = _squeeze(question)
    low = low.replace("ё", "е")
    specs = list(((load_yaml("care_types.yaml") or {}).get("care_types", {}) or {}).values())
    # первый проход — названия и синонимы всех видов помощи; второй — route_words (специалисты, разговорные
    # формы: «невролог» → поликлиника, «терапевт на дом» — вызов на дом, а не поликлиника)
    passes = [[(s, p) for s in specs for p in [*s.get("synonyms", []), s.get("name", "")]],
              [(s, p) for s in specs for p in s.get("route_words", []) or []]]
    for k, ps in enumerate(passes):
        got = [(spec, phrase) for spec, phrase in ps if _phrase_in(str(phrase), have, low, squeezed)]
        if got:
            # route_words — самая длинная фраза («мануальный терапевт» — восстановительное лечение, а не «терапевт»)
            spec, phrase = max(got, key=lambda x: len(str(x[1]))) if k else got[0]
            return spec.get("name"), str(phrase)
    return None, None


def _phrase_in(phrase: str, have: set[str], low: str, squeezed: str) -> bool:
    """Фраза вида помощи есть в вопросе: леммы, вхождение по границе слова или слитное написание."""
    pl = set(lemmas(str(phrase)))
    # леммы фразы среди лемм вопроса, вхождение строки ПО ГРАНИЦЕ СЛОВА («КТ» не находится в «катаракты»)
    # или вхождение без пробелов и дефисов («AirFlow» = «AIR FLOW», «онлайн консультация» = «онлайн-…»)
    sq = _squeeze(str(phrase))
    whole = bool(phrase) and re.search(rf"(?<![\wё]){re.escape(str(phrase).casefold())}(?![\wё])", low)
    # слитное написание — тоже по границе слова: «терапевт» не находится в «психотерапевта»
    glued = len(sq) >= 5 and sq in squeezed and re.search(
        r"(?<![\wё])" + r"[\s\-]*".join(re.escape(ch) for ch in sq) + r"(?![\wё])", low.replace("ё", "е"))
    return bool((pl and pl <= have) or whole or glued)


def _route_word(phrase: str | None) -> bool:
    """Фраза — из route_words config/care_types.yaml (специалист, разговорная форма), а не название/синоним."""
    specs = ((load_yaml("care_types.yaml") or {}).get("care_types", {}) or {}).values()
    return bool(phrase) and any(str(phrase) in (s.get("route_words") or []) for s in specs)


@cache
def _route_tokens() -> frozenset[str]:
    from ..index.morph import lemma

    specs = ((load_yaml("care_types.yaml") or {}).get("care_types", {}) or {}).values()
    return frozenset(lemma(str(p).casefold()) for s in specs for p in s.get("route_words") or [] if " " not in str(p))


def _route_word_token(word: str) -> bool:
    from ..index.morph import lemma

    return lemma(word.casefold().replace("ё", "е")) in _route_tokens()


def _city(question: str) -> str | None:
    """Первое географическое название (pymorphy3: тег Geox) в нормальной форме."""
    morph = _analyzer()
    for w in tokens(question):
        if not re.search("[а-я]", w) or len(w) < 3:
            continue
        p = morph.parse(w)[0]
        if "Geox" in p.tag:
            return p.normal_form.replace("ё", "е").title()
    return None


# Общие слова обращения менеджера: не называют услугу (леммы pymorphy). Пополнять по новым формулировкам.
_GENERIC_LEMMAS = frozenset(["сотрудник", "сотрудница", "застрахованный", "застраховать", "застрахованная", "клиника", "клиник", "страховой", "страховая", "страховщик", "компания", "программа", "дмс", "полис", "договор", "отказать", "отказ", "отказывать", "записывать", "записать", "принять", "принимать", "говорить", "сказать", "правый", "правомерно", "обоснованно", "обоснованный", "правильно", "положить", "положенный", "покрываться", "покрывать", "оплачивать", "оплатить", "можно", "ли", "это", "что", "как", "какой", "который", "весь", "свой", "его", "её", "наш", "ваш", "врач", "приём", "прием", "год", "раз", "этот", "тот", "уже", "ещё", "ответить", "отвечать", "предлагать", "предложить", "делать", "сделать", "мочь", "хотеть", "нужно", "надо", "право", "иметь", "случай", "вопрос", "жена", "муж", "сын", "дочь", "ребёнок", "ребенок", "родственник", "родственница", "супруг", "супруга", "работник", "лечиться", "медицинский", "центр",
    # слова обстоятельств отказа / требования оплаты (не услуга): «выставили счёт», «за деньги», «регистратура»
    "счёт", "счет", "деньги", "деньга", "медцентр", "регистратура", "пациент", "запись", "сам", "самый", "гарантийный",
    "письмо", "оплата", "доплата", "стоимость", "страховка", "правомерный", "полиса",
    # сокращения обстоятельств (не услуга): «ДМС», «ЛПУ», «МКАД», «КАД», «км», «HR», «СК»
    "мкад", "кад", "лпу", "км", "hr", "ск", "гп", "жалоба", "возможный", "доступный", "нормальный", "законный",
    # w5-recall2: обстоятельства требования оплаты — «отправили на кассу», «выписали чек», «предложили прайс»
    "касса", "чек", "прайс", "наличка", "наличные", "карта", "рубль", "процент"])


_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


def is_abbreviation(word: str) -> bool:
    """Сокращение или латинское название услуги: «ЭКГ», «ПЭТ-КТ», «ЭхоКГ», «ВПЧ», «ПСА», «COVID-19», «AirFlow».
    Признак сокращения — не меньше двух заглавных букв и нет трёх строчных подряд. Такие слова pymorphy не знает
    (UNKN/LATN) или знает неверно («ПСА» → «пёс»): в услуге они остаются как есть, без лемматизации."""
    if _LATIN.fullmatch(word):
        return True
    return sum(ch.isupper() for ch in word) >= 2 and not re.search(r"[a-zа-яё]{3}", word)


def _sentence_start(question: str, pos: int) -> bool:
    """Слово в начале вопроса или после конца предложения («. », «? », «! », перевод строки)."""
    before = question[:pos].rstrip(" «\"“(")
    if before.endswith(".") and re.search(r"(?:^|\s)[а-яё]{1,3}\.$", before):
        return False                    # «в г. Нижний Новгород», «ул. Ленина» — сокращение, не конец предложения
    return not before or before[-1] in ".!?…\n" and pos > len(before)


def proper_name(word: str, question: str, pos: int, parse: Any) -> bool:
    """Собственное имя внутри вопроса — клиника, программа, город, марка прибора («в Медси», «в Леконсе»,
    «на Тонзиллоре», «Мед.Коме»): с заглавной буквы не в начале предложения и не сокращение, либо имя по
    pymorphy (Name/Surn/Patr/Orgn/Trad). Услугой не становится (название клиники проверяет check_clinic)."""
    if is_abbreviation(word):
        return False
    if any(g in parse.tag for g in ("Name", "Surn", "Patr", "Orgn", "Trad")) and not parse.is_known:
        return True
    return word[:1].isupper() and not _sentence_start(question, pos)


# Слова, после которых имя собственное — часть названия болезни / метода («болезнь Крона», «синдром Иценко»)
_EPONYM_HEADS = frozenset({"болезнь", "синдром", "метод", "аппарат", "операция"})


def service_phrase(question: str, *, exclude: list[str], skip_places: bool = False) -> str | None:
    """Содержательные слова вопроса (существительные, прилагательные, причастия, сокращения и латинские
    названия) без общих слов ДМС, без собственных имён и без уже распознанных клиники/программы/города; до 6 слов
    в исходном порядке. Нет таких слов — None (вопрос задаст clarify).
    skip_places — без обстоятельств места («на цветном бульваре», «в клинике»: предлог «в / во / на» + слова в
    предложном падеже) и без прилагательного при общем слове («Очный приём» — «очный» относится к «приёму»)."""
    from ..index.morph import _analyzer

    morph = _analyzer()
    q = question or ""
    skip = {w.casefold() for e in exclude for w in re.findall(r"[\wё-]+", e or "")}
    out: list[str] = []
    words = list(re.finditer(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9-]*", q))
    place = False
    proc: str | None = None          # процедура по глаголу (config procedures.verbs) и место её дополнения
    proc_at = 0
    for k, m in enumerate(words):
        w = m.group()
        if skip_places:
            low = w.casefold()
            if low in ("в", "во", "на"):
                place = True
                continue
            pw = morph.parse(w)[0] if re.match(r"[А-Яа-яЁё]", w) else None
            if place and pw is not None and ("loct" in pw.tag or "loc2" in pw.tag):
                continue                     # «на цветном бульваре», «в клинике» — место, не услуга
            place = False
            nxt = words[k + 1].group() if k + 1 < len(words) else ""
            if pw is not None and str(pw.tag.POS) == "ADJF" and nxt and \
                    morph.parse(nxt)[0].normal_form.replace("ё", "е") in _GENERIC_LEMMAS:
                continue                     # «Очный приём»: прилагательное при общем слове
        if w.casefold() in skip or w.casefold() in {x.casefold() for x in out}:
            continue
        if is_abbreviation(w):
            # сокращение — как есть, без лемматизации; общие («ДМС», «МКАД», «ЛПУ») — нет
            if len(w) >= 2 and w.casefold().replace("ё", "е") not in _GENERIC_LEMMAS:
                out.append(w)
            continue
        if len(w) < 3:
            continue
        if proc is None and (named := procedure_verb(w)):
            # этап w14-refusal: глагол-процедура («отрезать», «вырезать», «прооперировать») — главное слово услуги
            # по словарю config/service_classes.yaml procedures.verbs; дополнение после него — вторым словом
            proc, proc_at = named, len(out)
            continue
        p = morph.parse(w)[0]
        if p.normal_form in _GENERIC_LEMMAS or p.normal_form.replace("ё", "е") in _GENERIC_LEMMAS:
            continue
        if "Geox" in p.tag or p.normal_form in skip:      # город в любой форме («в Москве») — не услуга
            continue
        if proper_name(w, q, m.start(), p) and not _class_lemma(p.normal_form):
            # слово класса процедуры («реанимашку», которое pymorphy не знает) — услуга, а не имя
            # имя в названии болезни или метода («болезнь Крона», «синдром Дауна») — часть услуги
            prev = morph.parse(words[k - 1].group())[0].normal_form if k else ""
            if prev not in _EPONYM_HEADS:
                continue
        if {"NOUN", "ADJF", "PRTF"} & {str(p.tag.POS)}:
            out.append(w)
    if proc is not None and procedure_named_by_verb(q, out[proc_at:]):
        out = with_procedure(proc, out, proc_at)
    return " ".join(out[:6]) or None


@cache
def _specialist_suffixes() -> tuple[str, ...]:
    svc = (load_yaml("coverage_markers.yaml") or {}).get("service") or {}
    return tuple(str(x).casefold() for x in svc.get("specialist_suffixes") or [])


def actor_word(word: str) -> bool:
    """Кто или где отказал, а не услуга (config/service_classes.yaml procedures.not_service: «хирург», «больница»,
    «роддом», «травмпункт»; специалист по окончанию леммы config/coverage_markers.yaml specialist_suffixes:
    «гинеколог», «офтальмолог»)."""
    lems = lemmas(word)[:1] or [word.casefold()]
    lem = lems[0].replace("ё", "е")
    if lem in procedures_cfg().not_service or word.casefold().replace("ё", "е") in procedures_cfg().not_service:
        return True
    if lem in _RELATIVES or lem in ("бабушка", "дедушка", "внук", "внучка"):
        return True                      # «госпитализировать бабушку» — для кого, а не услуга
    return len(lem) > 6 and lem.endswith(_specialist_suffixes())


def procedure_service(question: str, service: str | None, *, exclude: list[str]) -> str | None:
    """Слот услуги при отказе или процедуре в вопросе (этап w14-fix, ревизия 8, дефект 5): без «кто / где
    отказал» («ампутация хирург стопы» → «ампутация стопы», «роддом роды» → «роды», «удаление гинеколог полипа»
    → «удаление полипа»), если в слоте остаётся другое слово. Глагол-процедура вопроса («отказался вскрывать
    фурункул»), которого нет в слоте вида помощи («хирург в поликлинике»), — услуга по глаголу и дополнению
    (service_phrase). Без отказа и без глагола словаря слот прежний."""
    words = [m.group() for m in re.finditer(r"[A-Za-zА-Яа-яЁё][\w-]*", question or "")]
    named = {p for w in words if (p := procedure_verb(w))}
    if not named and not refusal_marker(question):
        return service
    svc = service
    have = set(lemmas(svc or ""))
    if named and not any(set(lemmas(n)) <= have for n in named):
        alt = service_phrase(question, exclude=exclude)
        if alt and any(set(lemmas(n)) <= set(lemmas(alt)) for n in named):
            svc = alt
    if not svc or not (named or procedure_class_word(svc)):
        return svc                       # в слоте нет процедуры («вызов врача на дом») — слот прежний
    kept = [w for w in svc.split() if not actor_word(w)]
    # предлог, после которого слово убрано («инфузионной терапии в [поликлинике]»), — тоже
    kept = [w for i, w in enumerate(kept)
            if w.casefold() not in STOP_PREPS or (i + 1 < len(kept) and kept[i + 1].casefold() not in STOP_PREPS)]
    content = [w for w in kept if len(w) >= 3 or is_abbreviation(w)]
    if not content and procedures_cfg().refusal_service and procedures_cfg().refusal is not None and \
            procedures_cfg().refusal.search((question or "").casefold().replace("ё", "е")):
        # «не положили в больницу»: в слоте только место — процедура шаблона отказа (procedures.refusal_service)
        return procedures_cfg().refusal_service
    return " ".join(kept) if content else svc


def procedure_named_by_verb(question: str, objects: list[str]) -> bool:
    """Глагол-процедура задаёт главное слово услуги, когда без него услуги нет: в обращении отказ (разбор отказа
    в процедуре, guided.refusal_analysis), у глагола нет дополнения или дополнение — только часть тела
    (config/coverage_markers.yaml service.anatomy_words: «отрезать ногу» — без глагола осталась бы «нога»).
    Дополнение само называет услугу («удалить папиллому», «удалить аппендикс») — услуга прежняя: исключения
    программы называют её этими словами («удаление … папиллом»), лишнее слово сузило бы совпадение."""
    if refusal_marker(question):
        return True
    anatomy = _anatomy_words()
    return all(lem in anatomy for w in objects for lem in lemmas(w)[:1])


def with_procedure(proc: str, words: list[str], at: int) -> list[str]:
    """Услуга с названием процедуры первым словом: «отрезать ногу» → «ампутация ноги», «не прооперировали
    мениск» → «операция мениска». Прямое дополнение (слова после глагола в винительном / именительном падеже)
    ставится в родительный падеж; остальные слова — как в вопросе. Процедура уже названа существительным
    («операцию ... оперировать») — второй раз не добавляется."""
    from ..index.morph import _analyzer

    morph = _analyzer()
    have = {lem for w in words for lem in lemmas(w)}
    if set(lemmas(proc)) <= have:
        return words
    out: list[str] = []
    for i, w in enumerate(words):
        parses = morph.parse(w)[:3] if i >= at and re.match(r"[А-Яа-яЁё]", w) and not is_abbreviation(w) else []
        # разбор с винительным падежом — среди первых трёх («катаракту»: дательный «катаракт» и винительный)
        p = next((x for x in parses if str(x.tag.POS) in ("NOUN", "ADJF", "PRTF") and "accs" in x.tag), None) or \
            next((x for x in parses if str(x.tag.POS) in ("NOUN", "ADJF", "PRTF") and "nomn" in x.tag), None)
        if p is not None:
            g = p.inflect({"gent"})
            w = g.word if g is not None else w
        out.append(w)
    return [proc, *out]


# Предлоги, которыми к услуге присоединяется уточнение: «анализы на ВИЧ», «УЗИ при беременности», «госпитализация
# для лечения алкоголизма», «иммуноглобулин против энцефалита», «лечение под наркозом». «в» и «по» — нет:
# «в клинике», «по полису».
_QUALIFIER_PREPS = frozenset({"на", "при", "для", "от", "против", "после", "под"})
# Предлоги между услугой и видом помощи слева от него: «криосауна в физиотерапии», «пост в стационаре»,
# «герметизация фиссур в стоматологии» — услуга названа слева, вид помощи — место её оказания.
_HEAD_PREPS = frozenset({"в", "во", "на", "у"})
_TOKEN = re.compile(r"[A-Za-zА-Яа-яЁё0-9][\w-]*|[^\w\s]")
_QUALIFIER_MAX = 5


def service_with_care(question: str, phrase: str, *, exclude: list[str]) -> str:
    """Слот услуги при найденном виде помощи: синоним вида помощи в вопросе и его уточнения — именная группа
    вокруг него (слева — прилагательные и ГЛАВНОЕ СУЩЕСТВИТЕЛЬНОЕ услуги, у которого синоним — зависимое слово:
    «имплантация зубов», «отбеливание зубов», «пост в стационаре»; справа — слова в родительном падеже и группы
    с предлогом «на / при / для / от / против / после / под»): «генетические анализы», «анализы на ВИЧ»,
    «имплантация зубов после удаления», «МРТ головного мозга при рассеянном склерозе». Слова обстоятельств
    («в 20 км за МКАД», «по полису», «требует оплатить») в уточнение не входят: граница — знак препинания,
    глагол, союз, другой предлог, общее слово ДМС, клиника/программа/город, собственное имя.
    Слова синонима в вопросе не найдены по отдельности (слитное «AirFlow») — синоним как есть."""
    morph = _analyzer()
    q = question or ""
    matches = list(_TOKEN.finditer(q))
    toks = [m.group() for m in matches]
    want = {x for x in lemmas(phrase)} | {w.casefold() for w in re.findall(r"[\wё-]+", phrase)}
    skip = {w.casefold() for e in exclude for w in re.findall(r"[\wё-]+", e or "")}
    parsed = [morph.parse(t)[0] if re.match(r"[А-Яа-яЁё]", t) and not is_abbreviation(t) else None for t in toks]

    def lem(i: int) -> str:
        p = parsed[i]
        return (p.normal_form if p is not None else toks[i].casefold()).replace("ё", "е")

    # слово вопроса — синоним или его часть через дефис («ПЭТ-КТ» при синониме «КТ»)
    hits = [i for i, t in enumerate(toks) if t.casefold() in want or lem(i) in want
            or ("-" in t and any(part in want for part in t.casefold().split("-")))]
    if not hits:
        return phrase
    a = b = hits[0]
    while b + 1 < len(toks) and (b + 1 in hits or (b + 2 in hits and toks[b + 1].casefold() in ("на", "и"))):
        b += 1 + (b + 1 not in hits)

    def content(i: int, *, cases: tuple[str, ...] = (), pos: tuple[str, ...] = ("NOUN", "ADJF", "PRTF")) -> bool:
        """Слово-уточнение: существительное/прилагательное/причастие (в нужном падеже) или сокращение, не общее
        слово, не город и не собственное имя."""
        p = parsed[i]
        if toks[i].casefold() in skip:
            return False
        if p is None:
            # сокращение / латиница с буквой («ВПЧ», «витамин D», «COVID-19»); число («20 км») — не уточнение
            return is_abbreviation(toks[i]) and lem(i) not in _GENERIC_LEMMAS and "NOUN" in pos
        if lem(i) in _GENERIC_LEMMAS or "Geox" in p.tag or proper_name(toks[i], q, matches[i].start(), p):
            return False
        if str(p.tag.POS) not in pos:
            return False
        return not cases or any(c in p.tag for c in cases)

    added = 0
    # слева: прилагательные; существительное — главное слово услуги, если синоним при нём в косвенном падеже
    # («имплантация зубов», «отбеливание зубов»), или через предлог места («пост в стационаре»)
    while a - 1 >= 0 and added < _QUALIFIER_MAX:
        if content(a - 1, pos=("ADJF", "PRTF")):
            a, added = a - 1, added + 1
            continue
        lead = parsed[a]
        oblique = lead is not None and str(lead.tag.POS) == "NOUN" and "nomn" not in lead.tag
        if oblique and content(a - 1, pos=("NOUN",)):
            a, added = a - 1, added + 1
            continue
        if a - 2 >= 0 and toks[a - 1].casefold() in _HEAD_PREPS and content(a - 2, pos=("NOUN",)):
            a, added = a - 2, added + 1
            continue
        break
    i, in_group = b + 1, False
    while i < len(toks) and added < _QUALIFIER_MAX:
        if toks[i].casefold() in _QUALIFIER_PREPS and i + 1 < len(toks) and content(i + 1):
            i, in_group = i + 1, True          # предлог: следующее уточнение — в любом падеже («при беременности»)
        elif not (content(i) if in_group else content(i, cases=("gent",))):
            break
        b, added, i = i, added + 1, i + 1
    words = [t for t in toks[a:b + 1] if re.match(r"[\wё]", t)]
    return " ".join(words) if words else phrase


def _clinic_tokens(context: ChatContext, store: KitStore | None) -> frozenset[str]:
    """Слова названий клиник перечня договора чата (или договоров ГК): название и юрлицо — формы и леммы."""
    return _clinic_vocab(context, store)[0]


def _clinic_cities(context: ChatContext, store: KitStore | None) -> frozenset[str]:
    """Города строк перечня клиник (леммы): город в начале названия — не клиника («в Москве»), а «Верона» — да."""
    return _clinic_vocab(context, store)[1]


def _clinic_vocab(context: ChatContext, store: KitStore | None) -> tuple[frozenset[str], frozenset[str]]:
    try:
        from ..tools._common import default_store

        st = store or default_store()
        cids = [context.contract_id] if context.contract_id else \
            [c.id for c in st.contracts(context.group_id)] if context.group_id else []
        return _clinic_tokens_cached(st, tuple(cids))
    except (KeyError, ValueError, OSError, sqlite3.Error):
        return frozenset(), frozenset()


_CLINIC_TOKEN_CACHE: dict[tuple[int, tuple[str, ...]], tuple[frozenset[str], frozenset[str]]] = {}
_KNOWN_STORES: set[int] = set()


def _store_key(st: KitStore) -> int:
    """Ключ хранилища в кэшах словаря перечня: id(st); когда хранилище удаляется, его записи из кэшей убираются
    (w7-fix: новое хранилище с тем же id получало чужой кэш — нестабильные тесты на синтетике)."""
    k = id(st)
    if k not in _KNOWN_STORES:
        _KNOWN_STORES.add(k)
        try:
            weakref.finalize(st, _forget_store, k)
        except TypeError:
            pass
    return k


def _forget_store(k: int) -> None:
    _KNOWN_STORES.discard(k)
    for store_cache in (_CLINIC_TOKEN_CACHE, _LIST_CACHE):
        for key in [x for x in store_cache if x[0] == k]:
            store_cache.pop(key, None)


def _clinic_tokens_cached(st: KitStore, cids: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    from ..index.morph import lemma

    key = (_store_key(st), cids)
    if key not in _CLINIC_TOKEN_CACHE:
        words: set[str] = set()
        cities: set[str] = set()
        for cid in cids:
            for r in st.clinics(cid):
                for name in (r.clinic_name, r.legal_name or ""):
                    for w in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", name):
                        k = w.casefold().replace("ё", "е")
                        words |= {k, lemma(k)}
                for w in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", r.city or ""):
                    cities.add(lemma(w.casefold().replace("ё", "е")))
        _CLINIC_TOKEN_CACHE[key] = (frozenset(words), frozenset(cities))
    return _CLINIC_TOKEN_CACHE[key]


@cache
def _service_vocabulary() -> frozenset[str]:
    """Слова, которые называют услугу или вид помощи, а не клинику (синонимы config/care_types.yaml и сокращения
    config/coverage_markers.yaml): «МРТ» в вопросе — услуга, даже если в перечне есть клиника «МРТ-Эксперт»."""
    out: set[str] = set()
    for spec in ((load_yaml("care_types.yaml") or {}).get("care_types", {}) or {}).values():
        for phrase in [*spec.get("synonyms", []), spec.get("name", "")]:
            out |= {w.casefold().replace("ё", "е") for w in re.findall(r"[\wё-]+", str(phrase))}
    svc = (load_yaml("coverage_markers.yaml") or {}).get("service") or {}
    out |= {str(k).casefold() for k in (svc.get("abbreviations") or {})}
    # этап w14-fix (ревизия 8): слова классов процедур config/service_classes.yaml («ОРИТ», «ЭКО», «реанимашка»,
    # «гемодиализ») — услуга, а не клиника («отказали в ОРИТ», «в реанимашку не взяли»); слова-начала названий
    # клиник («больница», «роддом» — _CLINIC_HEADS) сюда не входят
    for spec in ((load_yaml("service_classes.yaml") or {}).get("procedure_classes") or {}).values():
        for phrase in spec.get("words") or []:
            for w in re.findall(r"[\wё-]+", str(phrase)):
                low = w.casefold().replace("ё", "е")
                noun = is_abbreviation(w) or str(_analyzer().parse(low)[0].tag.POS) == "NOUN"
                if noun and low not in _CLINIC_HEADS and low not in ("роддом", "стационар"):
                    out.add(low)
    return frozenset(out)


# Предлоги перед названием клиники («в Медси», «у доктора Шейха», «из Клиники МЕД+», «от Чудо доктора»)
_CLINIC_PREPS = frozenset({"в", "во", "у", "из", "от"})
# Нарицательные слова, с которых начинается название клиники в строчном написании («в стоматологии Галеевой»,
# «в клинике врачей Никитиных», «у доктора Шейха»): начало названия, если дальше идёт имя
_CLINIC_HEADS = frozenset({"клиника", "стоматология", "поликлиника", "больница", "центр", "медцентр", "доктор",
                           "госпиталь", "лаборатория"})
# Слово названия: буквы/цифры, внутри — дефис, точка, «+» («Мед.Ком», «РЖД-Медицина», «Кардио+», «Поликлиника.ру»)
_NAME_TOKEN = re.compile(r"[A-Za-zА-Яа-яЁё0-9№][A-Za-zА-Яа-яЁё0-9-]*(?:\.[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9-]*)*\+?"
                         r"|[^\w\s]")


def _in_vocab(word: str, vocab: frozenset[str]) -> bool:
    """Слово (или часть составного слова не короче 3 букв) — среди слов/лемм названий клиник перечня."""
    from ..index.morph import lemma

    parts = [p for p in re.split(r"[.\-+]", word.casefold().replace("ё", "е")) if len(p) >= 3]
    return any(p in vocab or lemma(p) in vocab for p in parts)


def clinic_phrase(question: str, context: ChatContext, store: KitStore | None) -> tuple[str | None, str | None]:
    """(название клиники как в тексте, город при названии) — без кавычек и без слова «клиника» впереди.

    Начало названия: собственное имя внутри предложения (с заглавной буквы, не в начале), сокращение или латиница
    после предлога «в / у / из / от» — или в начале предложения, если слово есть в названиях клиник перечня договора
    («Чудо Доктор на Школьной…», «Семейный доктор развернул…»); строчное нарицательное («в стоматологии Галеевой»,
    «у доктора Шейха») — если за ним имя. Продолжение: имена, сокращения, числа («№ 3», «им.»), строчные слова,
    которые есть в названиях клиник перечня («Гармонии здоровья», «Семейном докторе», «Немецком медицинском центре»).
    Граница — знак препинания, предлог, глагол, слово услуги. Географическое название после начала — город, не часть
    названия («РЖД-Медицине Пензы» → «РЖД-Медицине» + «Пенза»); в начале — часть названия, если это не город
    перечня («в Вероне», «в Томской областной больнице»), иначе это город («в Москве»). Сравнение с перечнем —
    по леммам (tools/clinics.name_score); здесь только границы названия."""
    from ..index.morph import lemma

    vocab, cities = _clinic_vocab(context, store)
    if not vocab:
        return None, None
    morph = _analyzer()
    q = question or ""
    toks = [(m.group(), m.start()) for m in _NAME_TOKEN.finditer(q)]
    svc = _service_vocabulary()
    programs = {w.casefold().replace("ё", "е") for p in _programs(context, store) for w in re.findall(r"[\wё]+", p)}

    def parse(i: int) -> Any:
        w = toks[i][0]
        return morph.parse(w)[0] if re.match(r"[А-Яа-яЁё]", w) and not is_abbreviation(w) else None

    def low(i: int) -> str:
        return toks[i][0].casefold().replace("ё", "е")

    def word(i: int) -> bool:
        return bool(re.match(r"[\wё№]", toks[i][0]))

    def geox(i: int) -> bool:
        p = parse(i)
        return p is not None and "Geox" in p.tag

    def excluded(i: int) -> bool:
        """Не может быть словом названия: слово услуги, общее слово обращения, программа договора."""
        p = parse(i)
        lem = (p.normal_form if p is not None else low(i)).replace("ё", "е")
        base = low(i).rstrip("+")
        if lem in _CLINIC_HEADS:
            return False                           # «стоматология», «центр» внутри названия — часть названия
        return base in svc or lem in svc or base in programs or (lem in _GENERIC_LEMMAS and lem not in vocab)

    def named(i: int) -> bool:
        """Имя/сокращение/латиница: начало или продолжение названия."""
        w, pos = toks[i]
        if not word(i) or excluded(i) or len(w) < 2:
            return False
        if is_abbreviation(w) or re.match(r"[A-Za-z]", w):
            return low(i) not in _GENERIC_LEMMAS
        p = parse(i)
        return p is not None and proper_name(w, q, pos, p)

    def uninflected(i: int) -> bool:
        """Географическое имя с заглавной буквы после «в / во / у / из / от», но не в падеже, которого требует
        предлог («в Верона», «в Медина (г.Электросталь)»): несклоняемое название клиники, а не город (w7-fix).
        «в Москве», «в Сочи» (несклоняемое — есть разбор в предложном падеже) — город."""
        w = toks[i][0]
        prev = low(i - 1) if i else ""
        if prev not in _CLINIC_PREPS or not w[:1].isupper():
            return False
        want = ("loct", "loc2", "accs") if prev in ("в", "во") else ("gent", "gen2")
        return not any("Geox" in p.tag and any(c in p.tag for c in want) for p in morph.parse(w))

    def start_ok(i: int) -> bool:
        w, pos = toks[i]
        if not word(i) or excluded(i) or w[:1].isdigit():
            return False
        p_i = parse(i)
        lem_i = (p_i.normal_form if p_i is not None else low(i)).replace("ё", "е")
        if lem_i in _GENERIC_LEMMAS and lem_i not in _CLINIC_HEADS:
            return False                           # «Компании 9», «Сотруднику…» — общее слово, не начало названия
        if not_clinic_word(w):
            return False                           # «в СОГАЗе сказали…» — страховщик, не клиника
        prev = low(i - 1) if i else ""
        if prev and street_type(prev):
            return False                           # «на переулке Здоровья», «улица Западная» — улица (w7-fix)
        if lemma(low(i).rstrip("+")) in cities or (geox(i) and not _in_vocab(w, vocab) and not uninflected(i)):
            return False                           # «в Москве», «в Турции» — город / страна, не клиника
        if _route_word_token(low(i)):
            return False                           # «Невролог …», «ЭКГ …» — услуга, не клиника
        if w[:1].isupper() and _sentence_start(q, pos):
            # начало предложения: слово из названий клиник перечня; всё название проверяется по перечню договора
            # после продолжения («Чудо Доктор на Школьной…», «ИММА взяли…», «Поликлиника.ру требует…»)
            return _in_vocab(w, vocab)
        if named(i):
            # «в Медси», «у Доктора Шейха»; без предлога — если слово есть в названиях клиник перечня («Входит ли
            # ММЦ МЕДСИ…»); после «на / по» — улица или программа («на Сущевском», «по ВИП»)
            return prev in _CLINIC_PREPS or (prev not in ("на", "по", "к", "ко") and _in_vocab(w, vocab) and
                                             len(w) >= 3)
        p = parse(i)
        if p is not None and p.normal_form.replace("ё", "е") in _CLINIC_HEADS and prev in _CLINIC_PREPS:
            nxt = i + 1 + (i + 1 < len(toks) and parse(i + 1) is not None and
                           str(parse(i + 1).tag.POS) == "NOUN" and not named(i + 1))
            return nxt < len(toks) and named(nxt)
        return False

    def cont(i: int) -> bool:
        w, _pos = toks[i]
        if w == "№" or re.fullmatch(r"№?\d{1,4}", w):
            return True
        if low(i) in ("им", "им.", "имени"):
            return True
        if not word(i) or excluded(i) or low(i) in STOP_PREPS:
            return False
        if geox(i) or lemma(low(i).rstrip("+")) in cities:
            return False                           # город после начала названия — отдельно
        if named(i):
            return True
        p = parse(i)
        if p is None:
            return False
        if str(p.tag.POS) not in ("NOUN", "ADJF", "PRTF", "ADJS"):
            return False                           # ADJS — «Будь здоров»
        return _in_vocab(w, vocab)

    for i in range(len(toks)):
        if not start_ok(i):
            continue
        j = i + 1
        while j < len(toks) and (cont(j) or (toks[j][0] == "." and j + 1 < len(toks) and low(j - 1) == "им")):
            j += 1
        words = [t for t, _ in toks[i:j] if t != "."]
        # число без имени («№ 3» в конце) остаётся; одно нарицательное слово без имени — не название
        if not words or (len(words) == 1 and not named(i) and parse(i) is not None and
                         parse(i).normal_form.replace("ё", "е") in _CLINIC_HEADS):
            continue
        name = " ".join(words).replace(" .", ".")
        w0, pos0 = toks[i]
        if _sentence_start(q, pos0) and w0[:1].isupper():
            # начало предложения: одно слово — только сокращение / латиница / «Поликлиника.ру» («ИММА взяли…»);
            # несколько слов — первое не глагол, есть отличительное слово, и название есть в перечне договора
            # («Чудо Доктор на Школьной…»); иначе это обычное начало фразы («Профессиональный хоккеист…»)
            p0 = parse(i)
            brand = is_abbreviation(w0) or bool(re.match(r"[A-Za-z]", w0)) or "." in w0
            if len(words) == 1 and not brand:
                continue
            if not brand and (p0 is None or str(p0.tag.POS) not in ("NOUN", "ADJF", "PRTF")):
                continue
            if not _contract_has_name(name, context, store):
                continue
        # границы по перечню: номер перед названием и слова, которые есть в строке перечня (w7-core)
        i2, j = extend_by_list(toks, i, j, _list_index(context, store),
                               place=lambda k: geox(k) or lemma(low(k).rstrip("+")) in cities)
        name = " ".join(t for t, _ in toks[i2:j] if t != ".").replace(" .", ".")
        city = None
        if j < len(toks) and (geox(j) or lemma(low(j)) in cities) and parse(j) is not None:
            city = parse(j).normal_form.replace("ё", "е").title()
        return name, city
    return None, None


def _contract_has_name(name: str, context: ChatContext, store: KitStore | None) -> bool:
    """Название совпало с клиникой перечня договора чата (любой программы) — tools/clinics.name_score."""
    from ..tools._common import default_store
    from ..tools.clinics import NAME_THRESHOLD, name_score

    try:
        st = store or default_store()
        cids = [context.contract_id] if context.contract_id else \
            [c.id for c in st.contracts(context.group_id)] if context.group_id else []
        return any(name_score(name, r) >= NAME_THRESHOLD for cid in cids for r in st.clinics(cid))
    except (KeyError, ValueError, OSError, sqlite3.Error):
        return False


# ---------------------------------------------------------------- название клиники по перечню договора (w7-core)
# Ревизия 6: слот клиники терял номер и второе слово («12 КДЦ» → «КДЦ», «Доктор рядом» → «Доктор», «Он Клиник
# Рязань» → «Он Клиник»), а строчные названия без кавычек («в медэкспресс», «в тонус премиум») не находились вовсе.
# Границы названия уточняются по строкам перечня клиник договора чата (ГК — всех договоров группы): слово перечня —
# не догадка модели, а текст документа. Решения о клинике здесь нет — его делает менеджер выбором строки (ADR 0010).

@dataclass(frozen=True)
class _ListIndex:
    rows: tuple[tuple[str, str], ...]          # (название, юрлицо) строк перечня — для strict_name_match
    words: tuple[frozenset[str], ...]          # слова строки: формы, леммы, склейки через дефис
    anchors: dict[str, frozenset[int]]         # отличительное слово перечня → номера строк
    # w7-fix: («тип учреждения» лемма, номер) → строки: «поликлиника №1», «12 КДЦ», «центры-7»
    numbered: dict[tuple[str, str], frozenset[int]] = field(default_factory=dict)
    # w7-fix: название целиком (леммы слов части названия) → строки — для названий без отличительного слова
    whole: dict[tuple[str, ...], frozenset[int]] = field(default_factory=dict)


_LIST_CACHE: dict[tuple[int, tuple[str, ...]], _ListIndex] = {}


def invalidate(contract_id: str | None = None) -> int:
    """Сброс кэшей перечня клиник (словарь названий и городов, индекс строк для слота «клиника») по договору —
    все ключи, в набор договоров которых он входит (контекст договора и контексты ГК с ним); None — всё целиком.
    Нужен после повторной обработки договора клиента (ADR 0011): ключ кэша — хранилище и договоры, а не ревизия,
    поэтому без сброса слот и кандидаты видели бы прежний перечень до перезапуска. Возвращает число сброшенных."""
    n = 0
    for store_cache in (_CLINIC_TOKEN_CACHE, _LIST_CACHE):
        for key in [x for x in store_cache if contract_id is None or contract_id in x[1]]:
            store_cache.pop(key, None)
            n += 1
    return n


_HYPHENED = re.compile(r"[a-zа-яё0-9]+(?:-[a-zа-яё0-9]+)+")


def _name_keys(text: str) -> set[str]:
    """Слова названия для сравнения: формы и леммы слов normalize_name и склейки слов через дефис («мед-арт» →
    «медарт»)."""
    from ..guided import translit_name
    from ..index.morph import lemma
    from ..tools.clinics import normalize_name

    words = normalize_name(text).split()
    out = set(words) | {lemma(w) for w in words}
    out |= {m.replace("-", "") for m in _HYPHENED.findall(text.casefold().replace("ё", "е"))}
    # латиница строки — ещё и кириллицей («Medikus» ↔ «в медикус», guided.translit_name; w7-fix)
    out |= {translit_name(w) for w in words if re.fullmatch(r"[a-z]{4,}", w)}
    return out


@cache
def _list_cfg(key: str) -> frozenset[str]:
    """Список слов config/route_markers.yaml (clinic_type_words, clinic_name_descriptors, clinic_legal_words,
    street_types)."""
    return frozenset(str(x).casefold().replace("ё", "е") for x in (load_yaml("route_markers.yaml") or {}).get(key)
                     or [])


def _num(w: str) -> str:
    return w.lstrip("0") or "0"


def _numbered_keys(words: list[str]) -> set[tuple[str, str]]:
    """Пары (тип учреждения, номер) нормализованного названия: тип и число рядом («поликлиника 1», «12 кдц»)."""
    from ..index.morph import lemma

    types = _list_cfg("clinic_type_words")
    out: set[tuple[str, str]] = set()
    for k, w in enumerate(words):
        t = lemma(w)
        if t not in types and w not in types:
            continue
        nums = [_num(words[x]) for x in (k - 1, k + 1) if 0 <= x < len(words) and words[x].isdigit()]
        for n in nums:
            out.add((t if t in types else w, n))
        # этап w14-fix (ревизия 8, r8-170): сокращение типа по первым буквам слов перед ним — «городская
        # клиническая больница № 11» ↔ «ГКБ 11», «детская городская поликлиника» ↔ «ДГП» (если сокращение — тоже
        # тип учреждения config clinic_type_words)
        for span in range(2, 5):
            if k - span + 1 < 0:
                break
            abbr = "".join(x[:1] for x in words[k - span + 1:k + 1])
            if abbr in types:
                out |= {(abbr, n) for n in nums}
    return out


def _name_parts(name: str) -> list[str]:
    """Части названия строки: целиком, части между кавычками, текст в скобках и без скобок («"Клиника Будь Здоров"
    ООО (Будь здоров)» → «Клиника Будь Здоров», «Будь здоров»)."""
    out = [name, re.sub(r"\([^)]*\)", " ", name), *re.findall(r"\(([^)]*)\)", name)]
    out += re.split(r"[«»\"“”„]", name)
    return [x for x in out if x and x.strip()]


def _whole_key(part: str) -> tuple[str, ...] | None:
    """Ключ «название целиком» (леммы слов) — если в части названия есть слово вне типов учреждений и описательных
    слов (clinic_type_words, clinic_name_descriptors, tools/clinics._WEAK): «Семейный доктор», «Будь здоров»,
    «Здоровье», «Медицинские услуги», «КДФ-Воронеж», «Скорая помощь 072», но не «Семейный медицинский центр». Одно
    слово — только существительное ≥ 5 букв, не общее слово обращения, не услуга, не специалист."""
    from ..index.morph import lemma
    from ..tools.clinics import _weak, normalize_name

    words = normalize_name(part).split()
    if not 1 <= len(words) <= 5:
        return None
    key = tuple(lemma(w) if not w.isdigit() else _num(w) for w in words)
    plain = _list_cfg("clinic_type_words") | _list_cfg("clinic_name_descriptors") | _list_cfg("clinic_legal_words") | \
        STOP_PREPS
    if not any(k not in plain and w not in plain and not _weak(w) for k, w in zip(key, words, strict=True)):
        return None
    if len(key) == 1:
        w = key[0]
        if len(w) < 5 or w.isdigit() or w in _GENERIC_LEMMAS or _route_word_token(w) or \
                w in _service_vocabulary():
            return None
        p = _analyzer().parse(w)[0]
        if str(p.tag.POS) not in ("NOUN", "None") or "Geox" in p.tag:
            return None
    return key


def _anchor_cfg() -> tuple[frozenset[str], float]:
    cfg = load_yaml("route_markers.yaml") or {}
    stop = frozenset(str(x).casefold().replace("ё", "е") for x in cfg.get("clinic_anchor_stop") or [])
    return stop, float(cfg.get("clinic_anchor_max_share") or 0.1)


def _anchor_word(w: str, *, cities: frozenset[str], stop: frozenset[str]) -> bool:
    """Отличительное слово перечня: ≥ 4 букв, не общее и не описательное слово названий (tools/clinics), не город,
    не слово услуги / специалиста, не общее слово обращения, не из clinic_anchor_stop (config/route_markers.yaml)."""
    from ..index.morph import lemma
    from ..tools.clinics import _generic, _weak

    if len(w) < 4 or w.isdigit() or _generic(w) or _weak(w):
        return False
    lem = lemma(w)
    if lem in cities or w in stop or lem in stop or lem in _GENERIC_LEMMAS or _route_word_token(w):
        return False
    if w in _service_vocabulary() or lem in _service_vocabulary():
        return False
    p = _analyzer().parse(w)[0]
    return "Geox" not in p.tag


def _list_index(context: ChatContext, store: KitStore | None) -> _ListIndex | None:
    from ..index.morph import lemma

    try:
        from ..tools._common import default_store

        st = store or default_store()
        cids = tuple([context.contract_id] if context.contract_id else
                     [c.id for c in st.contracts(context.group_id)] if context.group_id else [])
        key = (_store_key(st), cids)
        if key in _LIST_CACHE:
            return _LIST_CACHE[key]
        stop, share = _anchor_cfg()
        cities = _clinic_vocab(context, st)[1]
        from ..tools.clinics import normalize_name

        rows: list[tuple[str, str]] = []
        words: list[frozenset[str]] = []
        numbered: dict[tuple[str, str], set[int]] = {}
        whole: dict[tuple[str, ...], set[int]] = {}
        seen: set[tuple[str, str]] = set()
        for cid in cids:
            for r in st.clinics(cid):
                k = (r.clinic_name, r.legal_name or "")
                if k in seen:
                    continue
                seen.add(k)
                n = len(rows)
                rows.append(k)
                words.append(frozenset(_name_keys(f"{k[0]} {k[1]}")))
                for x in {k[0], k[1]}:
                    for pair in _numbered_keys(normalize_name(x).split()):
                        numbered.setdefault(pair, set()).add(n)
                    for part in _name_parts(x):
                        if (wk := _whole_key(part)) is not None:
                            whole.setdefault(wk, set()).add(n)
        # частота слова по разным названиям: слово из каждого десятого названия («общество», «россии») — общее
        df: dict[str, int] = {}
        for ws in words:
            for w in {lemma(x) for x in ws}:
                df[w] = df.get(w, 0) + 1
        limit = max(8, int(share * len(rows)))
        anchors: dict[str, set[int]] = {}
        for i, ws in enumerate(words):
            for w in ws:
                if df.get(lemma(w), 0) <= limit and _anchor_word(w, cities=cities, stop=stop):
                    anchors.setdefault(w, set()).add(i)
        idx = _ListIndex(tuple(rows), tuple(words), {w: frozenset(v) for w, v in anchors.items()},
                         {k: frozenset(v) for k, v in numbered.items()}, {k: frozenset(v) for k, v in whole.items()})
        _LIST_CACHE[key] = idx
        return idx
    except (KeyError, ValueError, OSError, sqlite3.Error):
        return None


def _span_contained(span: str, words: frozenset[str]) -> bool:
    """Все слова фрагмента вопроса есть среди слов строки перечня (форма, лемма, склейка через дефис)."""
    from ..index.morph import lemma
    from ..tools.clinics import normalize_name

    ws = normalize_name(span).split()
    joined = {m.replace("-", "") for m in _HYPHENED.findall(span.casefold().replace("ё", "е"))}
    return bool(ws) and all(w in words or lemma(w) in words or
                            any(w in j and j in words for j in joined) for w in ws)


def listed_full_match(name: str, context: ChatContext, store: KitStore | None,
                      *, program: str | None = None) -> bool:
    """Название совпало со строкой перечня договора целиком: те же слова (леммы, без ОПФ, регистра и кавычек) или
    то же слитное написание, что у названия (и названия без пояснения в скобках) или юрлица строки. «Ренессанс Здоровье» — клиника, хотя «Ренессанс» —
    страховщик (not_clinic_words); «Ренессанс» само по себе — не клиника «Ренессанс Здоровье»."""
    from ..index.morph import lemma
    from ..tools.clinics import normalize_name

    types = _list_cfg("clinic_type_words")

    def key(x: str) -> tuple[frozenset[str], str]:
        # тип учреждения («клиника», «поликлиника») не различает названия: «Ренессанс» = «Клиника Ренессанс» (w7-fix)
        n = normalize_name(x)
        ws = frozenset(lemma(w) for w in n.split())
        return (ws - types) or ws, n.replace(" ", "")

    want = key(name)
    if not want[1]:
        return False
    try:
        from ..tools._common import default_store

        st = store or default_store()
        cids = [context.contract_id] if context.contract_id else \
            [c.id for c in st.contracts(context.group_id)] if context.group_id else []
        for cid in cids:
            for r in st.clinics(cid, program=program):
                # название без пояснения в скобках: «"Медицинские Центры" ООО (ИММА)» = «Медицинские Центры»
                # и части в кавычках («Общество с ограниченной ответственностью "Клиника Ренессанс"», w7-fix)
                names = (r.clinic_name, r.legal_name or "", *_name_parts(r.clinic_name))
                for x in names:
                    k = key(x)
                    if x and (k[0] == want[0] or k[1] == want[1]):
                        return True
        return False
    except (KeyError, ValueError, OSError, sqlite3.Error):
        return False


_EXT_BACK, _EXT_FWD = 2, 3      # на сколько слов название расширяется по перечню назад / вперёд


def extend_by_list(toks: list[tuple[str, int]], i: int, j: int, idx: _ListIndex | None,
                   place: Any = None) -> tuple[int, int]:
    """Границы названия [i, j) по перечню: число сразу перед названием после предлога («в 12 КДЦ», «в 52 КДЦ
    Минобороны») — часть названия всегда; затем самый длинный фрагмент [i', j') ⊇ [i, j) (назад ≤ 2 слов, вперёд
    ≤ 3, без знаков препинания и предлогов на краях), все слова которого есть в одной строке перечня («Он Клиник
    Рязань», «Доктор рядом», «Новая медицина-2000», «ЦЕНТР СЕМЕЙНОЙ МЕДИЦИНЫ», «Клиника семейного врача+»).
    Город (place(k) — географическое слово) входит в название, только если без него в названии нет уверенного
    отличительного слова (tools/clinics.confident_name): «Он Клиник Рязань», но «ТрансМедицине» + город «Самара»."""
    from ..tools.clinics import confident_name
    def wordy(k: int) -> bool:
        return 0 <= k < len(toks) and bool(re.match(r"[\wё№]", toks[k][0]))

    def edge_ok(k: int) -> bool:
        return wordy(k) and toks[k][0].casefold() not in STOP_PREPS

    if i >= 1 and re.fullmatch(r"№?\d{1,4}", toks[i - 1][0]) and (
            i < 2 or toks[i - 2][0].casefold() in _CLINIC_PREPS):
        i -= 1                                     # «в 12 КДЦ»: номер — часть названия
    if idx is None:
        return i, j
    best = (i, j)
    base_sure = confident_name(" ".join(t for t, _ in toks[i:j]))
    for a in range(i, max(-1, i - _EXT_BACK - 1), -1):
        if a < i and not all(wordy(k) for k in range(a, i)):
            break
        if not edge_ok(a):
            continue
        for b in range(j, min(len(toks), j + _EXT_FWD) + 1):
            if b > j and not all(wordy(k) for k in range(j, b)):
                break
            if b > j and base_sure and place is not None and place(b - 1):
                break                              # «ТрансМедицине Самары»: город — отдельно
            if (a, b) == (i, j) or not edge_ok(b - 1) or (b - a) <= (best[1] - best[0]):
                continue
            span = " ".join(t for t, _ in toks[a:b])
            shorts = {t.casefold() for t, _ in toks[a:b] if len(t) <= 2 and not t[:1].isdigit() and t != "№"}
            if any(shorts <= ws and _span_contained(span, ws) for ws in idx.words):
                best = (a, b)
    return best


# Предлоги перед строчным названием: «в / во / у / из» («от гриппа», «от бешенства» — не клиника)
_LISTED_PREPS = frozenset({"в", "во", "у", "из"})
# Нарицательное слово перед строчным названием, которое в слот не входит: «в клинике тонус премиум» → «тонус премиум»
_LEAD_HEADS = frozenset({"клиника", "поликлиника", "медцентр"})
_ANCHOR_BACK = 4     # слов между предлогом «в / у / из / от» (или «клиника») и началом строчного названия


@cache
def _anatomy_words() -> frozenset[str]:
    """Части тела (config/coverage_markers.yaml service.anatomy_words, леммы): слово услуги, не название клиники."""
    from ..index.morph import lemma

    svc = (load_yaml("coverage_markers.yaml") or {}).get("service") or {}
    return frozenset(lemma(str(x).casefold().replace("ё", "е")) for x in svc.get("anatomy_words") or [])


def _row_names(row: tuple[str, str]) -> tuple[str, ...]:
    """Название и юрлицо строки; латиница — ещё и кириллицей (guided.translit_name: «Medikus» ↔ «медикус»)."""
    from ..guided import translit_name

    return (*row, *(translit_name(x) for x in row if re.search(r"[A-Za-z]{3}", x)))


def listed_clinic(question: str, context: ChatContext, store: KitStore | None) -> str | None:
    """Название клиники без кавычек и без заглавной буквы — по отличительным словам перечня договора («к урологу в
    медэкспресс», «в тонус премиум», «в группа компаний персона», «в общество с ограниченной … мед-арт»).

    Слово вопроса — отличительное слово строк перечня (_ListIndex.anchors); вокруг него — самый длинный фрагмент
    (назад ≤ 3, вперёд ≤ 4 слов, без знаков препинания), строго совпавший со строкой (tools/clinics.strict_name_match);
    перед фрагментом (через ≤ 4 общих слов названия: «клинике», «общество с ограниченной …») — предлог «в / у / из»;
    одно слово — только существительное, не известное словарю или всё отличительное в названии строки (single_ok). Фрагмент только из слов страховщика / органа (not_clinic_words) — лишь при совпадении со строкой целиком
    в обе стороны (listed_full_match)."""
    from ..guided import translit_name
    from ..index.morph import lemma
    from ..tools.clinics import _generic, normalize_name, strict_name_match

    idx = _list_index(context, store)
    if idx is None or not idx.anchors:
        return None
    q = question or ""
    toks = [(m.group(), m.start()) for m in _NAME_TOKEN.finditer(q)]
    stop, _share = _anchor_cfg()

    def wordy(k: int) -> bool:
        return 0 <= k < len(toks) and bool(re.match(r"[\wё№]", toks[k][0]))

    def filler(k: int) -> bool:
        """Общее слово названия между предлогом и отличительным словом: «клинике», «общество», «с», «ограниченной»."""
        w = toks[k][0].casefold().replace("ё", "е")
        lem = lemma(w)
        return w == "с" or _generic(w) or lem in _CLINIC_HEADS or w in stop or lem in stop or \
            lem in ("ограниченный", "ответственность") or w.startswith("ответствен")

    def after_prep(a: int) -> bool:
        k = a - 1
        for _ in range(_ANCHOR_BACK + 1):
            if not wordy(k):
                # начало предложения после «клиника …»: «клиника будь здоров на …» (w7-fix)
                return k < a - 1 and lemma(toks[k + 1][0].casefold()) in _LEAD_HEADS and \
                    _sentence_start(q, toks[k + 1][1])
            if toks[k][0].casefold() in _LISTED_PREPS:
                return True
            if not filler(k):
                return False
            k -= 1
        return False

    def single_ok(a: int, rows: set[int]) -> bool:
        """Одно строчное слово — название, только если это не прилагательное / глагол («в московском роддоме»)
        и либо слово словарю не известно («медэкспресс»), либо оно — всё отличительное в названии строки
        («клиника сова», «преамбула»), а не часть длинного названия («от гриппа» ≠ «НИИ гриппа»)."""
        w = toks[a][0]
        p = _analyzer().parse(w)[0]
        if not p.is_known:
            # словарю не известно: часть речи — догадка анализатора («медси» → глагол «медсить»), не довод
            return True
        if str(p.tag.POS) in ("ADJF", "ADJS", "PRTF", "PRTS", "VERB", "INFN", "ADVB", "GRND"):
            return False
        keys = _name_keys(w)
        for r in rows:
            # название строки целиком или его часть (без скобок, в кавычках; латиница — кириллицей): «Medikus
            # (ООО "МЕДАВТО")» — «медикус» (w7-fix)
            for part in _name_parts(idx.rows[r][0]):
                for x0 in {part, translit_name(part)}:
                    dist = [x for x in normalize_name(x0).split() if not _generic(x) and len(x) >= 3]
                    if dist and all(x in keys or lemma(x) in keys for x in dist):
                        return True
        return False

    anatomy = _anatomy_words()

    def nominal_anchor(a: int, b: int) -> bool:
        """Во фрагменте есть отличительное слово-имя: существительное, слово не из словаря, латиница, сокращение,
        склейка через дефис — не прилагательное («московской», «женской»), не наречие («рядом») и не часть тела
        («глаз»: «клиника здоровья глаз» — не «Микрохирургия глаза»)."""
        for x in range(a, b):
            w = toks[x][0]
            keys = _name_keys(w)
            if not any(y in idx.anchors for y in keys):
                continue
            lw = lemma(w.casefold().replace("ё", "е"))
            if lw in anatomy:
                continue
            p = _analyzer().parse(w)[0]
            if "-" in w or not p.is_known or re.match(r"[A-Za-z]", w) or is_abbreviation(w) or \
                    str(p.tag.POS) == "NOUN":
                return True
        return False

    best: tuple[int, int] | None = None
    for k, (w, _pos) in enumerate(toks):
        if not wordy(k):
            continue
        keys = _name_keys(w)
        rows = set().union(*(idx.anchors.get(x, frozenset()) for x in keys))
        if not rows:
            continue
        for a in range(k, max(-1, k - 4), -1):
            if a < k and not wordy(a):
                break
            for b in range(min(len(toks), k + 5), k, -1):
                if not all(wordy(x) for x in range(a, b)):
                    continue
                if toks[a][0].casefold() in STOP_PREPS or toks[b - 1][0].casefold() in STOP_PREPS:
                    continue
                if best is not None and b - a <= best[1] - best[0]:
                    continue
                span = " ".join(t for t, _ in toks[a:b])
                # короткие слова фрагмента («в», «г», «н») — только если они есть в той же строке перечня
                # («кдц медси в марьино», но не «экология здоровья в г»)
                shorts = {t.casefold() for t, _ in toks[a:b] if len(t) <= 2 and not t[:1].isdigit() and t != "№"}
                if not any(shorts <= idx.words[r] and strict_name_match(span, *_row_names(idx.rows[r])) for r in rows):
                    continue
                # начало предложения — два слова и больше, среди них тип учреждения или число («кдц медси на
                # белорусской — можно …», w7-fix); «тонус премиум — это клиника?» — не название
                types = _list_cfg("clinic_type_words")
                start = _sentence_start(q, toks[a][1]) and b - a >= 2 and any(
                    t[:1].isdigit() or lemma(t.casefold()) in types for t, _ in toks[a:b])
                if not (start or after_prep(a) or (a > 0 and toks[a - 1][0].casefold() in _LISTED_PREPS)):
                    continue
                if b - a == 1 and not single_ok(a, rows):
                    continue
                if not nominal_anchor(a, b) and not listed_full_match(span, context, store):
                    continue                    # «в московской клинике», «в клинике рядом» — описание (w7-fix)
                if all(not_clinic_word(x) for x in span.split() if len(x) >= 3) and \
                        not listed_full_match(span, context, store):
                    continue
                best = (a, b)
    if best is None:
        return None
    a, b = best
    # «в клинике тонус премиум» → «тонус премиум»: общее слово впереди отбрасывается, если без него совпадение то же
    while b - a > 1 and lemma(toks[a][0].casefold()) in _LEAD_HEADS and any(
            strict_name_match(" ".join(t for t, _ in toks[a + 1:b]), *row) for row in idx.rows):
        a += 1
    return " ".join(t for t, _ in toks[a:b])


# ---------------------------------------------------------------- «тип учреждения + номер», название целиком (w7-fix)
# Ревизия 7: «Поликлиника №1», «поликлинику № 5», «Медицинские центры-7», «12 кдц», «мсч № 152 фмба россии»,
# «семейная поликлиника 4» не становились слотом: у таких названий нет отличительного слова (тип учреждения и
# описательные слова — общие, число якорем не было). Номер с типом учреждения — название, если в перечне договора
# есть строка с тем же типом и тем же номером; номер остаётся в слоте (по нему strict_name_match отличает «52 КДЦ»
# от «12 КДЦ»). Решения о клинике здесь нет — его делает менеджер выбором строки (ADR 0010).
_NUM_TOKEN = re.compile(r"[A-Za-zА-Яа-яЁё]+|\d{1,4}|№|[^\w\s]")
_NUM_MARKS = frozenset({"№", "-", "–", "N"})
# слово после числа — мера, а не номер учреждения: «в поликлинике 2 раза», «5 дней»
_MEASURE = frozenset({"раз", "день", "неделя", "месяц", "год", "час", "минута", "км", "рубль", "руб", "процент",
                      "тысяча", "приём", "прием", "визит", "сеанс", "процедура", "штука", "шт", "р", "человек", "лет"})
TYPE_FUZZ = 85        # опечатка в типе учреждения («поликилника»): сходство ≥ порога, слово ≥ 6 букв
_NUM_EXT = 3          # слов строки перечня перед типом и после номера («спб городская …», «… фмба россии»)


def numbered_clinic(question: str, context: ChatContext, store: KitStore | None) -> str | None:
    """Название «тип учреждения + номер» по перечню договора: тип (config clinic_type_words, по лемме; опечатка —
    по сходству) и число рядом («№ 5», «- 2», «12 кдц»); строка перечня с той же парой (тип, номер) обязательна.
    Границы — слова этих строк перед типом и после номера (≤ 3: «спб городская поликлиника № 40», «мсч № 152 фмба
    россии»). После «на / по» — улица, не клиника. Возвращает фрагмент вопроса как есть (с «№»)."""
    from rapidfuzz import fuzz

    from ..index.morph import lemma
    from ..tools.clinics import normalize_name

    idx = _list_index(context, store)
    if idx is None or not idx.numbered:
        return None
    q = question or ""
    toks = [(m.group(), m.start(), m.end()) for m in _NUM_TOKEN.finditer(q)]
    types = _list_cfg("clinic_type_words")
    long_types = [t for t in types if len(t) >= 6]

    def low(k: int) -> str:
        return toks[k][0].casefold().replace("ё", "е")

    def alpha(k: int) -> bool:
        return 0 <= k < len(toks) and toks[k][0].isalpha() and toks[k][0] not in _NUM_MARKS

    def type_of(k: int) -> str | None:
        w = low(k)
        lw = lemma(w)
        if lw in types or w in types:
            return lw if lw in types else w
        if len(w) >= 6:
            best = max(long_types, key=lambda t: fuzz.ratio(lw, t), default=None)
            if best is not None and fuzz.ratio(lw, best) >= TYPE_FUZZ:
                return best
        return None

    for k in range(len(toks)):
        if not alpha(k) or (t := type_of(k)) is None:
            continue
        nums: list[int] = []
        x = k + 1 + (k + 1 < len(toks) and toks[k + 1][0] in _NUM_MARKS)
        if x < len(toks) and toks[x][0].isdigit():
            nums.append(x)
        if k >= 1 and toks[k - 1][0].isdigit():
            nums.append(k - 1)
        for x in nums:
            if x > k and alpha(x + 1) and lemma(low(x + 1)) in _MEASURE:
                continue                        # «в поликлинике 2 раза» — мера, не номер
            if x < k and x >= 1 and low(x - 1) not in _CLINIC_PREPS:
                continue                        # номер перед типом — только сразу после «в / у / из» («в 12 кдц»)
            rows = idx.numbered.get((t, _num(toks[x][0])))
            if not rows:
                # такого номера в перечне нет, но тип с номерами есть («Медицинские центры-9» при «…-2», «…-7»):
                # это название клиники (её нет в перечне — решит выбор менеджера «нет в перечне»)
                rows = frozenset().union(*(v for (tt, _n), v in idx.numbered.items() if tt == t))
            if not rows:
                continue
            words: set[str] = set().union(*(idx.words[r] for r in rows))

            def in_row(j: int, back: bool, words: set[str] = words) -> bool:
                """Слово строки перечня (форма, лемма, опечатка ≥ 5 букв), форма собственности («ОГАУЗ»); перед
                типом — ещё и прилагательное («городской поликлинике № 3», «клиинческая больница»)."""
                if not alpha(j) or low(j) in STOP_PREPS:
                    return False
                w = low(j)
                if w in words or lemma(w) in words or not normalize_name(w):
                    return True
                if len(w) >= 5 and any(len(y) >= 5 and fuzz.ratio(w, y) >= TYPE_FUZZ for y in words):
                    return True
                p = _analyzer().parse(toks[j][0])[0]
                return back and str(p.tag.POS) in ("ADJF", "PRTF")

            a, b = min(k, x), max(k, x)
            for _ in range(_NUM_EXT):
                # «консультативно-диагностический»: дефис между словами — одно слово
                step = 2 if a >= 3 and toks[a - 1][0] == "-" and alpha(a - 2) else 1
                if not in_row(a - step, True):
                    break
                a -= step
            for _ in range(_NUM_EXT):
                if not in_row(b + 1, False):
                    break
                b += 1
            if a >= 1 and low(a - 1) in ("на", "по"):
                continue                        # «на 2-й Поликлинической» — улица
            return q[toks[a][1]:toks[b][2]]
    return None


def whole_name_clinic(question: str, context: ChatContext, store: KitStore | None) -> str | None:
    """Название строки перечня без отличительного слова — целиком (_ListIndex.whole: «семейный доктор», «будь
    здоров», «медицинские услуги», «кдф-воронеж», «скорая помощь 072», «здоровье»): все слова фрагмента (леммы) —
    это часть названия строки, ни больше ни меньше. Перед фрагментом — «в / у / из» (через «клинику / клинике»);
    в начале предложения — если в названии есть число или фрагмент начинается с «клиника». Одно слово — только
    сразу после «в / у / из» («в здоровье в лобне»)."""
    from ..index.morph import lemma
    from ..tools.clinics import normalize_name

    idx = _list_index(context, store)
    if idx is None or not idx.whole:
        return None
    q = question or ""
    toks = [(m.group(), m.start()) for m in _NAME_TOKEN.finditer(q)]

    def wordy(k: int) -> bool:
        return 0 <= k < len(toks) and bool(re.match(r"[\wё№]", toks[k][0]))

    def low(k: int) -> str:
        return toks[k][0].casefold().replace("ё", "е")

    def boundary(b: int) -> bool:
        """После названия — граница: конец, знак, предлог / союз, глагол или наречие, но не продолжение именной
        группы («в клинике здоровья глаз» — не «Клиника здоровья»)."""
        if b >= len(toks) or not wordy(b) or low(b) in STOP_PREPS or toks[b][0][:1].isdigit():
            return True
        p = _analyzer().parse(toks[b][0])[0]
        return str(p.tag.POS) not in ("NOUN", "ADJF", "PRTF", "NUMR")

    def key(a: int, b: int) -> tuple[str, ...]:
        ws = normalize_name(" ".join(t for t, _ in toks[a:b])).split()
        return tuple(_num(w) if w.isdigit() else lemma(w) for w in ws)

    best: tuple[int, int] | None = None
    for a in range(len(toks)):
        if not wordy(a) or low(a) in STOP_PREPS:
            continue
        prev = low(a - 1) if a >= 1 else ""
        lead = a >= 2 and lemma(prev) in _LEAD_HEADS and low(a - 2) in _LISTED_PREPS
        for b in range(min(len(toks), a + 6), a, -1):
            if not all(wordy(x) for x in range(a, b)) or low(b - 1) in STOP_PREPS:
                continue
            if best is not None and b - a <= best[1] - best[0]:
                break
            k = key(a, b)
            if k not in idx.whole or not boundary(b):
                continue
            if len(k) == 1:
                ok = prev in _LISTED_PREPS
            else:
                start = _sentence_start(q, toks[a][1]) and (any(x.isdigit() for x in k) or k[0] in _LEAD_HEADS)
                ok = prev in _LISTED_PREPS or lead or start
            if ok:
                best = (a, b)
                break
    if best is None:
        return None
    a, b = best
    while b - a > 1 and lemma(low(a)) in _LEAD_HEADS and key(a + 1, b) in idx.whole:
        a += 1                                  # «клиника будь здоров» → «будь здоров»
    return " ".join(t for t, _ in toks[a:b])


def list_name_words(context: ChatContext, store: KitStore | None) -> frozenset[str]:
    """Отличительные слова перечня договора и слова названий-целиком (не типы учреждений и не описательные): после
    «в / у / из» такое слово — название клиники, а не услуга («к кардиологу в здоровье» — не услуга «здоровье»)."""
    idx = _list_index(context, store)
    if idx is None:
        return frozenset()
    plain = _list_cfg("clinic_type_words") | _list_cfg("clinic_name_descriptors")
    return frozenset(idx.anchors) | frozenset(w for k in idx.whole for w in k if w not in plain and not w.isdigit())


# Предлоги и союзы — граница названия
STOP_PREPS = frozenset({"в", "во", "на", "по", "с", "со", "к", "ко", "о", "об", "от", "до", "из", "за", "для", "при",
                        "без", "у", "и", "или", "а", "но", "не", "что", "как", "ли"})


@cache
def _not_clinic() -> tuple[str, ...]:
    return tuple(str(x).casefold() for x in (load_yaml("route_markers.yaml") or {}).get("not_clinic_words") or [])


def not_clinic_word(word: str) -> bool:
    """Слово — страховщик / орган / общее слово (config/route_markers.yaml not_clinic_words), не название клиники.
    «поликлиника» в списке — только как первое слово названия без имени («в поликлинике сказали…»)."""
    w = word.casefold().replace("ё", "е")
    return any(w.startswith(x) for x in _not_clinic() if x != "поликлиника") or \
        (w.startswith("поликлиник") and "." not in w)


def clinic_in_question(question: str, context: ChatContext, store: KitStore | None) -> str | None:
    """Название клиники без кавычек (clinic_phrase); None — клинику спросит clarify, а не угадает код."""
    return clinic_phrase(question, context, store)[0]


# Улица после названия клиники: «на» + слово с заглавной буквы (+ «валу / шоссе / бульваре / проспекте …»)
# Улица с числом (w7-core): «на 1905 года», «на 8 Марта», «на 3-я Парковая», «на 3-й Хорошёвский»
_STREET = re.compile(r"[»\"”]?\s*,?\s*на\s+((?:\d{4}\s+года|(?:\d{1,2}(?:-?[а-яё]{1,3})?\s+)?[А-ЯЁ][а-яё\-]+"
                     r"(?:\s+[А-ЯЁ][а-яё\-]+)?)"
                     r"(?:\s+(?:вал\w*|шоссе|бульвар\w*|проспект\w*|улиц\w*|переул\w*|набережн\w*|площад\w*|"
                     r"проезд\w*))?)")


@cache
def _street_forms() -> re.Pattern[str]:
    """Улица филиала после названия (w7-fix, ревизия 7; типы улиц — config/route_markers.yaml street_types):
    «на улице Кулакова», «на переулке Сивцев Вражек», «на 8 марта» (строчными — с числом или после типа улицы),
    «в 1-м Смоленском переулке», «в Горловом тупике» («в» — только с типом улицы: «в Лобне» — город),
    «по адресу улица Плющиха», «по адресу: ул. Маршала Катукова». Группа name — сама улица без типа."""
    alts = []
    for t in sorted(_list_cfg("street_types"), key=len, reverse=True):
        e = re.escape(t)
        alts.append(e + r"[а-яё]*\.?" if len(t) >= 4 else e + r"\.?(?![а-яё])")
    typ = "(?i:" + "|".join(alts) + ")"           # без учёта регистра — только тип улицы, не имя
    num = r"(?:\d{1,2}(?:-?[а-яё]{1,3})?\s+)"
    word = r"[а-яё]*(?:-[А-ЯЁа-яё][а-яё]+)*"           # «Садовой-Каретной», «Яблочкина»
    cap = rf"[А-ЯЁ]{word}(?:\s+[А-ЯЁ]{word})?"
    anyw = rf"[А-ЯЁа-яё][а-яё]{word}(?:\s+[А-ЯЁ]{word})?"
    lead = r"[»\"”]?\s*,?\s*"
    forms = [
        rf"(?i:на)\s+{typ}\s+(?P<a>{num}?{anyw})",                  # «на улице Кулакова», «на улице кулакова»
        r"на\s+(?P<b>\d{1,2}\s+[а-яё]{3,})",                # «на 8 марта»
        rf"(?i:в|во)\s+(?P<c>{num}?{cap})\s+{typ}",                # «в 1-м Смоленском переулке»
        rf"(?i:по\s+адресу):?\s+(?:{typ}\s+)?(?P<d>{num}?{anyw})",   # «по адресу улица Плющиха»
    ]
    return re.compile(lead + "(?:" + "|".join(forms) + ")")


def street_type(word: str) -> bool:
    """Слово — тип улицы (config street_types): длинные — по началу слова («улице», «переулке»), короткие — точно."""
    w = word.casefold().replace("ё", "е").rstrip(".")
    return any(w.startswith(t) if len(t) >= 4 else w == t for t in _list_cfg("street_types"))


def street_after(question: str, pos: int) -> tuple[str, str] | None:
    """Улица сразу после позиции pos (конец названия клиники): (улица, весь фрагмент «на улице …»); формы
    _street_forms, затем _STREET."""
    q = question or ""
    if m := _street_forms().match(q, pos):
        name = next(g for g in m.groups() if g)
        # строчные слова после типа улицы — только до предлога / союза («на улице кулакова и …»)
        words = []
        for w in name.split():
            if w.casefold() in STOP_PREPS:
                break
            words.append(w)
        return (" ".join(words[:3]).strip(), m.group(0)) if words else None
    if m := _STREET.match(q, pos):
        return m.group(1).strip(), m.group(0)
    return None


def streets_in(question: str) -> list[str]:
    """Фрагменты-улицы вопроса в формах _street_forms («на улице …», «в … переулке», «по адресу …») и «на» + имя
    с заглавной буквы — для города («на Сивцевом Вражке», «на Плющихе» — улица, а не город)."""
    q = question or ""
    out = [m.group(0) for m in _street_forms().finditer(q)]
    out += [m.group(0) for m in re.finditer(r"\bна\s+([А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ][а-яё\-]+)?)", q)]
    return out


# Первое слово названия документа или раздела в кавычках — не клиника («Списке медицинских организаций»)
_DOC_WORDS = frozenset({"список", "перечень", "программа", "договор", "исключение", "памятка", "правило",
                        "приложение", "раздел", "пункт", "условие", "порядок", "положение", "полис", "не"})


def _doc_or_generic(name: str, context: ChatContext, store: KitStore | None) -> bool:
    """Название документа / раздела («Список медицинских организаций», «Исключения») или одни общие слова
    обращения («не по ДМС») — не клиника: слот клиники дал бы «выберите клинику» (clinic_unresolved) вместо
    вывода по услуге (эталоны К17, own-gk1-16). Название, совпавшее со строкой перечня целиком («Медицинские
    Центры»), — клиника."""
    words = lemmas(name, keep_stopwords=True)
    if words[:1] and words[0] in _DOC_WORDS:
        return True
    # все слова — общие слова обращения или служебные («не по ДМС»); «Он Клиник», «Центры-2» — не такие
    return bool(words) and not re.search(r"\d", name) and \
        all(x in _GENERIC_LEMMAS or x in _FUNCTION_WORDS for x in words) and \
        not listed_full_match(name, context, store)


_FUNCTION_WORDS = frozenset({"не", "по", "в", "на", "для", "за", "с", "и"})


def _list_word(word: str, place: frozenset[str]) -> bool:
    """Слово — из названий перечня (list_name_words), но не слово услуги / вида помощи / специалиста (у них общее
    начало ≥ 6 букв со словарём услуг: «кардиологии» ↔ «кардиолог» остаётся услугой)."""
    from ..index.morph import lemma

    w = word.casefold().replace("ё", "е")
    lw = lemma(w)
    if not (w in place or lw in place) or _route_word_token(w) or lw in _GENERIC_LEMMAS:
        return False
    svc = _service_vocabulary() | _route_tokens()
    return not any(len(x) >= 6 and (lw[:6] == x[:6]) for x in svc)


def rule_slots(question: str, context: ChatContext, store: KitStore | None = None) -> dict[str, str]:
    """Слоты по словам. Только то, что прямо есть в тексте; ничего не выводится. Опечатки в словах словаря
    процедур («апперации», «больнитсу», «атказали») исправляются до разбора (fix_typos, этап w14-fix)."""
    q = fix_typos(question or "")
    slots: dict[str, str] = {}
    programs = _programs(context, store)
    quoted = [m.group(1).strip() for m in _QUOTED.finditer(q)]
    for cand in quoted + [m.group(1).strip() for m in _PROGRAM_AFTER.finditer(q)]:
        p = _match_program(cand, programs)
        if p:
            slots["program"] = p
            break
    for cand in quoted:
        if _doc_or_generic(cand, context, store):
            continue                               # «Список медицинских организаций», «не по ДМС» — не клиника
        if _match_program(cand, programs) is None and cand != slots.get("program"):
            slots["clinic"] = cand
            slots["clinic_exact"] = "1"        # название в кавычках — точное (основание для «нет в перечне»)
            break
    clinic_city = None
    if "clinic" not in slots and (numbered := numbered_clinic(q, context, store)):
        # «тип учреждения + номер» по перечню договора: «Поликлиника №1», «12 кдц», «мсч № 152 фмба россии» (w7-fix)
        slots["clinic"] = numbered
        # название, продолженное после номера («ГКБ №3 им. Альперовича»: номер — по перечню, имя — clinic_phrase),
        # — целиком (этап w14-fix: тип по сокращению «ГКБ» нашёл номер раньше, чем имя)
        longer = clinic_phrase(q, context, store)[0]
        if longer and len(longer) > len(numbered) and _squeeze(longer.replace(".", "")).startswith(
                _squeeze(numbered.replace(".", ""))):
            slots["clinic"] = longer
    if "clinic" not in slots:
        # название без кавычек (clinic_phrase) и «клиника X» — что названо раньше в вопросе
        m = _CLINIC_AFTER.search(q)
        after = m.group(1).strip() if m and _match_program(m.group(1), programs) is None else None
        name, clinic_city = clinic_phrase(q, context, store)
        if name and (_match_program(name, programs) is not None or _doc_or_generic(name, context, store)):
            name, clinic_city = None, None         # «в Списке медицинских организаций» — документ, не клиника
        if name and (after is None or q.find(name) <= (m.start() if m else len(q))):
            slots["clinic"] = name
        elif after:
            slots["clinic"] = after
    if "clinic" not in slots and (listed := listed_clinic(q, context, store)):
        # строчное название без кавычек — по отличительным словам перечня договора (w7-core)
        slots["clinic"] = listed
    if "clinic" not in slots and (whole := whole_name_clinic(q, context, store)):
        # название строки без отличительного слова — целиком: «в семейный доктор», «в здоровье» (w7-fix)
        slots["clinic"] = whole
    # Услуга — содержательные слова вопроса после удаления общих слов ДМС и уже найденных клиники/программы/
    # города (услуг в программах сотни: «герметизация фиссур», «чек-ап»). Вид помощи (care_type) — отдельный
    # слот: синоним вида помощи услугу не заменяет, иначе теряется уточнение («анализы на ВИЧ» → «анализы»).
    # Это только запрос к документам договора — вывод о покрытии делает guided по найденному тексту.
    # город — не из названия клиники («в Вероне» — клиника «Верона»; «РЖД-Медицине Пензы» — город «Пенза»)
    # улица филиала после названия клиники («Семейный доктор на Дежнева», «Будь здоров на Сущевском валу»)
    street_text = ""
    if slots.get("clinic") and (st := street_after(q, q.find(slots["clinic"]) + len(slots["clinic"]))):
        slots["street"], street_text = st
    # город — не из названия клиники и не из улицы («на Куйбышева» — не город Куйбышев; «на Сивцевом Вражке»,
    # «на Плющихе» — улица: после «на» город — только город перечня клиник договора, w7-fix)
    rest = q
    for part in (slots.get("clinic"), street_text or slots.get("street")):
        if part:
            rest = rest.replace(part, " ")
    cities = _clinic_cities(context, store)
    streets = [x for x in streets_in(rest) if cities and
               not any(w and lemmas(w) and lemmas(w)[0] in cities for w in re.findall(r"[А-ЯЁ][а-яё\-]+", x)[:1])]
    for part in streets:
        rest = rest.replace(part, " ")
    city = clinic_city or _city(rest)
    # услуга и вид помощи — без названия клиники и улицы (w7-fix: «поликлиника» в «Семейная поликлиника №1» — не
    # вид помощи вопроса, «улица» в «по адресу улица …» — не услуга); слова названий перечня сразу после
    # «в / у / из» — тоже не услуга («к кардиологу в здоровье»)
    q_svc = rest if slots.get("clinic") else q
    place = list_name_words(context, store)
    after = [w for _p, w in re.findall(r"(?i)\b(в|во|у|из)\s+([а-яёa-z][\w-]*)", q) if _list_word(w, place)]
    names = [slots.get("clinic", ""), slots.get("program", ""), city or "", slots.get("street", ""), *after]
    care, phrase = _care_types(q_svc)
    if care is None and q_svc != q:
        care, phrase = _care_types(q)
        q_svc = q
    if care and _route_word(phrase):
        # вид помощи по слову-специалисту / разговорной форме (config route_words): услуга — именная группа слова
        # («имплантация зубов», «наркоз при колоноскопии»); если это одно слово («стоматолог»), — все содержательные
        # слова вопроса без обстоятельств места («стоматолог … герметизацию фиссур»; специалист — необязательное)
        slots["care_type"] = care
        group = service_with_care(q_svc, str(phrase), exclude=names)
        slots["service"] = group if len(group.split()) > 1 else \
            (service_phrase(q_svc, exclude=names, skip_places=True) or group)
    elif care:
        slots["care_type"] = care
        slots["service"] = service_with_care(q_svc, phrase or care, exclude=names)
    elif (svc := service_phrase(q_svc, exclude=names)):
        slots["service"] = svc
    if slots.get("service"):
        # отказ / процедура в вопросе: услуга — сама процедура, без «кто / где отказал» (этап w14-fix)
        got = procedure_service(q_svc, slots["service"], exclude=names)
        if got:
            slots["service"] = got
    if city and city.casefold() != slots.get("program", "").casefold():   # «программа «Москва»» — не город
        slots["city"] = city
    m = _DATE_RU.search(q)
    if m:
        try:
            slots["date"] = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass
    elif (m := _DATE_ISO.search(q)):
        slots["date"] = m.group(0)
    for lem in lemmas(q):
        if lem in _RELATIVES:
            slots["relative"] = _RELATIVES[lem]
            break
    if (refusal := refusal_marker(q)):
        slots["refusal"] = refusal
    return slots


# ---------------------------------------------------------------- модель

ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scenario": {"type": "string", "enum": [s.value for s in Scenario]},
        "slots": {"type": "object", "properties": {s: {"type": "string"} for s in SLOTS},
                  "additionalProperties": False},
    },
    "required": ["scenario", "slots"],
    "additionalProperties": False,
}


def _llm_route(question: str, context: ChatContext, llm: LLM) -> tuple[Scenario, dict[str, str]]:
    from ..agent import extract_json

    ctx = (f"Контекст чата: {'договор' if context.contract_id else 'группа компаний'}; программа: "
           f"{context.program or 'не указана'}.")
    turn = llm.chat([Message("system", prompt("route")),
                     Message("user", f"{ctx}\n<question>\n{question}\n</question>")],
                    None, json_schema=ROUTE_SCHEMA, max_tokens=1024)
    data = extract_json(turn.text)
    scenario = Scenario(str(data.get("scenario")))
    raw = data.get("slots") or {}
    slots = {k: str(v).strip() for k, v in raw.items() if k in SLOTS and v not in (None, "") and str(v).strip()}
    return scenario, slots


def grounded(slot: str, value: str, question: str, rules: dict[str, str]) -> bool:
    """Слот модели опирается на текст вопроса (проверка кодом, а не просьбой из route.md):
    refusal / relative / date — только если их нашли и правила (маркеры отказа, слова родства, дата в тексте);
    care_type — если правила нашли вид помощи или слова значения есть в вопросе; остальные — общая лемма
    со словами вопроса или вхождение без пробелов («AirFlow»)."""
    if slot in ("refusal", "relative", "date"):
        return slot in rules
    if slot == "care_type" and "care_type" in rules:
        return True
    have = set(lemmas(question))
    return bool(set(lemmas(value)) & have) or (len(_squeeze(value)) >= 3 and _squeeze(value) in _squeeze(question))


def route(question: str, context: ChatContext, llm: LLM | None = None, *, store: KitStore | None = None) -> Route:
    """Сценарий + слоты + вопросы по недостающим фактам. Сбой модели — откат на правила (с пометкой).

    Сценарий модели принимается, кроме одного детерминированного правила: явные маркеры отказа или вопроса
    «положено ли / можно ли» (rule_scenario → sc01) важнее ответа модели — SC 01 даёт метку и включает все
    проверки SC 02 по услуге, а пропуск SC 01 лишает менеджера вывода по отказу.
    """
    rules = rule_slots(question, context, store)
    warnings: list[str] = []
    rule_sc = rule_scenario(question)
    scenario, slots, source = rule_sc, dict(rules), "rules"
    # явный отказ / требование оплаты и явный вопрос о покрытии услуги («…входит?», «оплатят ли», «по полису?»)
    # kNN не перебивает: это SC 01 (config/route_markers.yaml, группы refusal и coverage)
    knn_sc = None if firm_sc01(question) else embedding_scenario(question)
    if knn_sc is not None:
        # kNN по эмбеддингам, когда он уверен (отрыв победителя ≥ min_margin, config/route_examples.yaml);
        # иначе — правила по словам. Замер на эталонах без holdout: правила 0,68 → сочетание 0,81 (ADR 0009).
        scenario, source = knn_sc, "knn"
    if llm is not None:
        try:
            scenario, raw = _llm_route(question, context, llm)
            source = "llm"
            if rule_sc == Scenario.sc01 and scenario != Scenario.sc01:
                warnings.append(f"сценарий модели {scenario.value} заменён на sc01 (маркеры отказа / «положено ли»)")
                scenario = Scenario.sc01
            slots = {}
            for k, v in raw.items():
                if grounded(k, v, question, rules):
                    slots[k] = v
                else:
                    warnings.append(f"слот модели {k} отброшен: нет опоры в тексте вопроса")
            programs = _programs(context, store)
            if slots.get("program"):
                # название программы — каноническое из договора; не нашлось — оставляем как в вопросе
                slots["program"] = _match_program(slots["program"], programs) or slots["program"]
            for k in ("refusal", "relative", "date"):     # значения этих слотов — нормализованные правилами
                if k in rules:
                    slots[k] = rules[k]
            for k, v in rules.items():
                slots.setdefault(k, v)
        except (LlmError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"маршрутизация моделью не удалась, правила по словам: {type(exc).__name__}")
    questions = missing_facts(scenario, slots, context, store=store)
    return Route(scenario=scenario, slots=slots, questions=questions, source=source, warnings=warnings)
