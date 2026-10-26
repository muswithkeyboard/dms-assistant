"""Инструменты по клиникам и видам помощи: clinics_query, care_matrix (план §6.3). Только строки одного договора.

Нечёткое имя клиники: нормализация (регистр, «ё», кавычки, организационно-правовые формы, пунктуация), затем
вхождение строки или rapidfuzz WRatio ≥ NAME_THRESHOLD по названию клиники и юрлицу.
Вид помощи: код из config/care_types.yaml («dental») или слова («стоматология») — сравнение по леммам с названием
и синонимами вида помощи; плюс простое вхождение текста.
"""
from __future__ import annotations

import re
from typing import Any

from rapidfuzz import fuzz

from ..config import load_yaml
from ..constants import FRANCHISE_NOT_APPLICABLE
from ..index.morph import lemmas
from ..schemas import ClinicRow
from ..store.db import KitStore
from ._common import care_out, clinic_out, resolve

NAME_THRESHOLD = 85        # порог сходства имени клиники (rapidfuzz WRatio, 0–100)
ADDRESS_THRESHOLD = 85     # порог для адреса, если слова запроса не нашлись по леммам
FRANCHISE_UNKNOWN = "не указано"   # значение фильтра franchise: в строке размер франшизы не указан (None)

# Организационно-правовые формы и типовые префиксы учреждений: не различают клиники и мешают сравнению.
_OPF = frozenset(
    ["ооо", "оао", "зао", "пао", "ао", "нао", "ип", "ано", "чуз", "чу", "нуз", "гбуз", "гауз", "гбу", "гку", "фгбу", "фгбуз", "фгуп", "гуп", "муп", "мбуз", "ну", "лпу",
     "огауз", "огбуз", "буз", "кгбуз", "кгбу", "огбу", "фгбну", "фгау", "гбуз", "чуз", "нчуз", "ауз", "бу", "гау"]
)
_QUOTES_RE = re.compile(r"[«»\"'“”„‘’`]")
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_name(name: str) -> str:
    """«ООО «Медси-2»» → «медси 2»: регистр, ё→е, без кавычек, пунктуации и ОПФ."""
    s = _QUOTES_RE.sub(" ", name.casefold().replace("ё", "е"))
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(w for w in s.split() if w not in _OPF)


# Общие слова названий медорганизаций: есть почти в каждом названии и клиник не различают. Совпадение только по ним
# давало «Несуществующая клиника Альфа» ≈ настоящая клиника — ложное «клиника в программе» (ревизия w3).
# Слова сравниваются и по форме, и по лемме (pymorphy): «в Семейном докторе» → «семейный доктор» (этап w5-recall2).
# Сокращения типа учреждения (КДЦ, ГКБ, МСЧ…) тоже общие: «КДЦ Южный» = «Медико-оздоровительный центр Южный».
_GENERIC = frozenset(["клиника", "клиники", "клиник", "клинический", "клиническая", "медицинский", "медицинская", "медицинские", "медицина", "медцентр", "центр", "центра", "мц", "лечебный", "лечебно", "диагностический", "диагностическая", "многопрофильный", "многопрофильная", "поликлиника", "поликлиники", "больница", "госпиталь", "стоматология", "стоматологическая", "стоматологический", "филиал", "сеть", "городская", "городской", "детская", "детский", "семейный", "семейная", "частная", "частный", "здоровье", "здоровья", "доктор", "врач", "дом", "лаборатория",
                      "кдц", "лдц", "мдц", "дц", "кб", "гкб", "окб", "цкб", "дгкб", "гп", "дгп", "мсч", "омс", "им", "имени",
                      "clinic", "clinics", "medical", "center", "centre", "dental", "медико"])
# Описательные слова типа учреждения (леммы): общие для подбора, но «Семейная поликлиника №1» и «Поликлиника №1» —
# разные клиники (strict_name_match без сильного слова, этап w7-fix)
_TYPE_DESCRIPTORS = frozenset(["семейный", "городской", "детский", "частный", "стоматологический", "клинический",
                               "диагностический", "многопрофильный", "лечебный"])
DISTINCT_MATCH = 88     # сходство отличительного слова запроса со словом названия (опечатки, падежи)


def _lemma(word: str) -> str:
    from ..index.morph import lemma

    return lemma(word)


def _generic(word: str) -> bool:
    return word in _GENERIC or _lemma(word) in _GENERIC


def _distinct(words: list[str]) -> list[str]:
    return [w for w in words if not _generic(w) and (len(w) >= 3 or w.isdigit())]


def distinct_words(query: str) -> list[str]:
    """Отличительные слова названия клиники из запроса (не общие слова названий, ≥ 3 букв или число) — в
    нормализованной форме. Пусто — название из одних общих слов («Семейный доктор»): по нему «нет в перечне»
    не ставится."""
    return _distinct(normalize_name(query).split())


_FUNCTION_WORDS = frozenset(["на", "в", "во", "по", "из", "от", "до", "за", "для", "при", "со", "и", "или", "им",
                             "не", "без", "к", "ко", "у", "с", "о", "об"])


def _initials(words: list[str]) -> str:
    return "".join(w[0] for w in words if w)


def _acronym_covered(qw: list[str], nw: list[str]) -> set[int]:
    """Номера слов запроса, совпавших через сокращение: слово названия — первые буквы подряд идущих слов запроса
    («Томская ОКБ» ↔ «областная клиническая больница»), или слово запроса — первые буквы слов названия
    («БСМП» ↔ «Больница скорой медицинской помощи»)."""
    out: set[int] = set()
    # служебные слова названия («КДЦ МЕДСИ на Белорусской») — не сокращения: «на» ≠ «Несуществующая Альфа» (w7-fix)
    abbrs = {w for w in nw if 2 <= len(w) <= 6 and w.isalpha() and w not in _FUNCTION_WORDS}
    for i in range(len(qw)):
        for j in range(i + 2, min(len(qw), i + 6) + 1):
            if _initials(qw[i:j]) in abbrs:
                out |= set(range(i, j))
    for k, w in enumerate(qw):
        if 2 <= len(w) <= 6 and w.isalpha() and w not in _FUNCTION_WORDS and any(
                _initials(nw[i:i + len(w)]) == w for i in range(len(nw) - len(w) + 1)):
            out.add(k)
    return out


# Описательные слова названий (леммы): встречаются в названиях многих разных клиник («Центральная», «Областная»,
# «Инновационная», «Мой»). Отличительные, но не подтверждают личность клиники сами: совпадение только по ним — не
# «клиника в перечне» (ревизия 4: «клиника Центральная» → «Центральная клиническая больница …»), и «нет в перечне» по
# такому названию не ставится (этап w5-strict).
_WEAK = frozenset(["центральный", "областной", "районный", "республиканский", "краевой", "региональный", "окружной",
                   "национальный", "федеральный", "научный", "новый", "первый", "второй", "современный",
                   "международный", "европейский", "российский", "народный", "профессорский", "университетский",
                   "инновационный", "семейный", "добрый", "мой", "наш", "ваш", "твой", "лучший", "главный", "умный",
                   "хороший", "премиум", "плюс", "эксперт", "профи", "мед", "медик", "медика", "здоровый", "жизнь",
                   "консультативный", "специализированный", "ведомственный", "клинический", "столичный", "лечебный",
                   "оздоровительный", "реабилитационный", "диагностика", "консультация", "профилактика",
                   "health", "med", "plus", "family", "expert", "smart", "best", "new"])
_NUM = re.compile(r"\d+")
JOIN_MATCH = 88         # сходство склеенного написания («АВС Медицину» ↔ «АВСМЕДИЦИНА»): падеж последнего слова


def _weak(word: str) -> bool:
    return word in _WEAK or _lemma(word) in _WEAK


def _nums(words: list[str]) -> set[str]:
    return {m.lstrip("0") or "0" for w in words for m in _NUM.findall(w)}


def _runs(words: list[str], longest: int = 4) -> list[tuple[int, int, set[str]]]:
    """Склейки подряд идущих слов (i, j, {по формам, по леммам}) — для слитного / раздельного / дефисного написания."""
    out = []
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + longest) + 1):
            out.append((i, j, {"".join(words[i:j]), "".join(_lemma(x) for x in words[i:j])}))
    return out


def _joined_cover(qw: list[str], nw: list[str]) -> set[int]:
    """Номера слов запроса, совпавших с названием в другом написании: склейка ≥ 2 слов запроса = склейка слов
    названия («авс медицину» ↔ «авсмедицина») или одно слово запроса = склейка ≥ 2 слов названия («авсмедицина» ↔
    «авс медицина»). Строки с цифрами — только точно; без цифр (≥ 6 букв) — сходство ≥ JOIN_MATCH (падеж)."""
    names = [(i, j, v) for i, j, v in _runs(nw)]
    out: set[int] = set()
    for i, j, qv in _runs(qw):
        for a, b, nv in names:
            if (j - i) + (b - a) < 3:
                continue                      # одно слово против одного — обычное сравнение слов
            ok = bool(qv & nv) or any(
                len(x) >= 6 and len(y) >= 6 and x.isalpha() and y.isalpha() and fuzz.ratio(x, y) >= JOIN_MATCH
                for x in qv for y in nv)
            if ok and min(len(x) for x in qv) >= 4:
                out |= set(range(i, j))
                break
    return out


def strict_name_match(query: str, *names: str | None) -> bool:
    """Строгая проверка личности клиники (этап w5-strict, ревизия 4): решение «клиника в перечне» (yes) — только по
    ней; нечёткое сходство (name_score, сокращения) — лишь подбор кандидатов.

    Название запроса и название строки (или юрлицо) — одна клиника, если:
    1) после нормализации (регистр, ё, кавычки, ОПФ, пунктуация, дефис) склеенное написание совпало целиком
       («АВС-Медицина» = «АВС Медицина» = «АВСМЕДИЦИНА»); иначе
    2) числа запроса есть в названии («52 КДЦ» ≠ «12 КДЦ»), и
    3) каждое отличительное слово запроса (не общее слово названий) нашлось в названии — по форме, лемме, склейке
       или сходству ≥ DISTINCT_MATCH (слово ≥ 5 букв без цифр); сокращения («ИМ» ↔ «Инновационная Медицина») —
       НЕ подтверждение, и
    4) среди найденных есть сильное слово (≥ 4 букв или склейка, не число и не описательное слово _WEAK); если
       сильного нет («Семейная поликлиника №3», «Клиника Центральная», «Мой доктор»), — найдены все слова запроса,
       включая общие, и у названия нет отличительных слов сверх запроса («МЦК-3» ≠ «Семейная поликлиника №3»,
       «Центральная клиническая больница РЖД» ≠ «Клиника Центральная»)."""
    q = normalize_name(query)
    qw = q.split()
    if not qw:
        return False
    for cand in names:
        n = normalize_name(cand or "")
        if n and (q.replace(" ", "") == n.replace(" ", "") or _strict_words(qw, n.split())):
            return True
    return False


def _word_found(w: str, k: int, join: set[int], nw: list[str], nl: list[str]) -> bool:
    """Слово запроса (номер k) нашлось в названии: склейка, форма, лемма или сходство (≥ 5 букв, без цифр)."""
    if k in join or w in nw or _lemma(w) in nl:
        return True
    return w.isalpha() and len(w) >= 5 and _word_match(w, nw, nl) >= DISTINCT_MATCH


def _strict_words(qw: list[str], nw: list[str]) -> bool:
    """Шаги 2–4 strict_name_match для одного названия (слова после normalize_name)."""
    qn = _nums(qw)
    if not qn <= _nums(nw):
        return False
    nl = [_lemma(x) for x in nw]
    join = _joined_cover(qw, nw)
    dist = [k for k, w in enumerate(qw) if not _generic(w) and (len(w) >= 3 or w.isdigit())]
    if not all(_word_found(qw[k], k, join, nw, nl) for k in dist):
        return False
    if any(not qw[k].isdigit() and not _weak(qw[k]) and (len(qw[k]) >= 4 or k in join) for k in dist):
        return True
    # сильного слова нет: найдены все слова запроса, и у названия нет отличительных слов сверх запроса
    ql = [_lemma(w) for w in qw]
    q_forms = set(qw) | set(ql)
    # описательные слова типа учреждения тоже различают: «Поликлиника № 1» ≠ «Семейная поликлиника №1» (w7-fix)
    name_dist = [x for x in nw if (not _generic(x) or _lemma(x) in _TYPE_DESCRIPTORS) and (len(x) >= 3 or x.isdigit())]
    return len(qw) >= 2 and all(_word_found(w, k, join, nw, nl) for k, w in enumerate(qw)) and all(
        x in q_forms or _lemma(x) in q_forms or _word_found(x, -1, set(), qw, ql) for x in name_dist)


def confident_name(query: str, cities: set[str] | None = None) -> bool:
    """Название извлечено уверенно — можно ставить «нет в перечне» (этап w5-strict): есть отличительное слово
    ≥ 4 букв или с цифрой, не город (леммы cities), не общее и не описательное слово (_GENERIC, _WEAK)."""
    cities = cities or set()
    for w in normalize_name(query).split():
        if _generic(w) or _weak(w) or _lemma(w) in cities or w in cities:
            continue
        if len(w) >= 4 or any(ch.isdigit() for ch in w):
            return True
    return False


def _word_match(w: str, nw: list[str], nl: list[str]) -> float:
    """Лучшее сходство слова запроса со словами названия: по форме и по лемме (падежи: «Вероне» ↔ «Верона»)."""
    lw = _lemma(w)
    return max([fuzz.ratio(w, x) for x in nw] + [fuzz.ratio(lw, x) for x in nl] + [0])


def name_score(query: str, row: ClinicRow) -> float:
    """Сходство запроса с названием клиники или юрлицом (0–100).

    Вхождение нормализованной строки — 100. Иначе каждое отличительное слово запроса (не из _GENERIC) обязано
    найтись в названии — по форме или по лемме (fuzz.ratio ≥ DISTINCT_MATCH) или через сокращение; оценка — среднее
    по ним. Подстановки чужой клиники нет: «Чудо Доктор» ≠ «Мой доктор» (не нашлось «чудо»), «Мед.Ком» ≠ «МЕДСИ».
    Запрос только из общих слов («Семейный доктор») — все его леммы есть среди лемм названия. Нет совпадения — 0:
    лучше «не нашли», чем чужая клиника."""
    q = normalize_name(query)
    if not q:
        return 0.0
    qw = q.split()
    qd = [i for i, w in enumerate(qw) if w in _distinct([w])]
    best = 0.0
    for cand in (row.clinic_name, row.legal_name or ""):
        n = normalize_name(cand)
        if not n:
            continue
        if q in n:
            return 100.0
        nw = n.split()
        nl = [_lemma(x) for x in nw]
        acr = _acronym_covered(qw, nw) | _joined_cover(qw, nw)
        if not qd:
            # одни общие слова: все леммы запроса — среди лемм названия (или через сокращение)
            if all(k in acr or _lemma(w) in nl or w in nw for k, w in enumerate(qw)):
                best = max(best, 95.0)
            continue
        per = [100.0 if i in acr else _word_match(qw[i], nw, nl) for i in qd]
        if min(per) >= DISTINCT_MATCH:
            best = max(best, sum(per) / len(per))
    return best


def distinct_overlap(query: str, row: ClinicRow) -> bool:
    """Хотя бы одно отличительное слово запроса похоже на слово названия строки (или совпало сокращение). Для
    «нет в перечне»: строка с частичным совпадением — сомнение («не нашли»), а не «клиники нет»."""
    q = normalize_name(query)
    qw = q.split()
    qd = [i for i, w in enumerate(qw) if w in _distinct([w])]
    for cand in (row.clinic_name, row.legal_name or ""):
        n = normalize_name(cand)
        if not n or not qd:
            continue
        nw = n.split()
        nl = [_lemma(x) for x in nw]
        acr = _acronym_covered(qw, nw) | _joined_cover(qw, nw)
        for i in qd:
            w = qw[i]
            # короткое слово («мед») — только точное совпадение: «Медси» ≠ «МЕД+»
            if i in acr or (w in nw or _lemma(w) in nl if len(w) <= 3 else _word_match(w, nw, nl) >= OVERLAP_MATCH):
                return True
    return False


OVERLAP_MATCH = 80      # порог «похоже» для сомнения в «нет в перечне» (ниже порога совпадения — осторожнее)


def _care_phrase_sets(care_type: str) -> list[set[str]]:
    """Фразы для сравнения вида помощи: сам запрос; если это код или он совпал с синонимом вида —
    название и все синонимы этого вида (config/care_types.yaml) — в виде множеств лемм."""
    types = (load_yaml("care_types.yaml") or {}).get("care_types", {})
    q = set(lemmas(care_type))
    phrases: list[set[str]] = [q] if q else []

    def own(spec: dict[str, Any]) -> list[set[str]]:
        got = [set(lemmas(str(p))) for p in [spec.get("name", ""), *spec.get("synonyms", [])]]
        return [p for p in got if p]

    for code, spec in types.items():
        mine = own(spec)
        if care_type.strip().casefold() == code or any(p <= q for p in mine):
            phrases.extend(mine)
            # вид помощи в рамках другого (config part_of: диагностика — в поликлинике): и фразы вида-родителя
            parent = types.get(str(spec.get("part_of") or ""))
            if parent:
                phrases.extend(p for p in own(parent) if p not in phrases)
    return phrases


def _care_matches(care_type: str, row_care: str, phrase_sets: list[set[str]]) -> bool:
    if care_type.strip().casefold() in row_care.casefold():
        return True
    have = set(lemmas(row_care))
    return any(p <= have for p in phrase_sets)


def _text_contains(query: str, value: str | None, threshold: int) -> bool:
    """Город/адрес: вхождение строки, все леммы запроса в значении, или нечёткое сходство ≥ threshold."""
    if not value:
        return False
    q, v = query.casefold().replace("ё", "е").strip(), value.casefold().replace("ё", "е")
    if q in v:
        return True
    ql = set(lemmas(q))
    if ql and ql <= set(lemmas(v)):
        return True
    return fuzz.partial_ratio(q, v) >= threshold


def _franchise_matches(want: float | str, have: float | str | None) -> bool:
    """franchise: число — процент; «—» — не применяется (это не 0 %); «не указано» — в строке не указан."""
    if want == FRANCHISE_UNKNOWN:
        return have is None
    if want == FRANCHISE_NOT_APPLICABLE:
        return have == FRANCHISE_NOT_APPLICABLE
    if isinstance(have, int | float) and not isinstance(want, str):
        return abs(float(have) - float(want)) < 1e-9
    return False


def clinics_query(contract_id: str, program: str | None = None, care_type: str | None = None,
                  name: str | None = None, city: str | None = None, address: str | None = None,
                  franchise: float | str | None = None, by_referral: bool | None = None, *, limit: int = 50,
                  store: KitStore | None = None) -> dict[str, Any]:
    """Строки перечня клиник договора по фильтрам. При поиске по имени — сортировка по сходству (`name_score`)."""
    cid, st = resolve(contract_id, store)
    rows = st.clinics(cid, program=program, by_referral=by_referral)
    if care_type:
        phrases = _care_phrase_sets(care_type)
        rows = [r for r in rows if _care_matches(care_type, r.care_type, phrases)]
    if city:
        rows = [r for r in rows if _text_contains(city, r.city, NAME_THRESHOLD)]
    if address:
        rows = [r for r in rows if _text_contains(address, r.address, ADDRESS_THRESHOLD)]
    if franchise is not None:
        rows = [r for r in rows if _franchise_matches(franchise, r.franchise_pct)]
    scored: list[tuple[ClinicRow, float | None]] = [(r, None) for r in rows]
    if name:
        scored = [(r, name_score(name, r)) for r in rows]
        scored = [(r, s) for r, s in scored if s is not None and s >= NAME_THRESHOLD]
        scored.sort(key=lambda rs: -(rs[1] or 0))
    out = [clinic_out(r, **({"name_score": round(s, 1)} if s is not None else {})) for r, s in scored[:limit]]
    return {"rows": out, "total": len(scored), "truncated": len(scored) > limit}


# ---------------------------------------------------------------- поиск по перечню программы (w7-core, w7-fix)
_SEARCH_WORD = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
SEARCH_PREFIX_MIN = 3      # слово запроса не короче — ищется и как начало слова строки («марьин» → «Марьино»)
SEARCH_FUZZ_MIN = 5        # слово запроса не короче — ищется и с опечаткой (расстояние Левенштейна, SEARCH_FUZZ_DIST)
SEARCH_FUZZ_DIST = ((8, 2), (5, 1))     # (длина слова от, допустимое расстояние): «медсы» ↔ «медси», «здоровь»
# Слова запроса, которые строку не различают (w7-fix): предлоги, типы улиц и дома, «клиника / поликлиника / медцентр»
_SEARCH_SKIP = frozenset(["в", "во", "на", "по", "у", "с", "со", "к", "ко", "из", "от", "до", "и", "или", "около",
                          "возле", "улица", "ул", "проспект", "пр", "т", "пр-т", "просп", "переулок", "пер",
                          "шоссе", "ш", "бульвар", "б", "р", "проезд", "д", "дом", "корпус", "корп", "к", "стр",
                          "строение", "клиника", "поликлиника", "медцентр", "г", "город"])


def _search_keys(*texts: str | None) -> tuple[set[str], set[str]]:
    """(слова и леммы, числа) названия / юрлица / города / адреса строки: «ул. 1905 года» → {«ул», «1905», «год»…};
    латиница строки — ещё и кириллицей («Medikus» → «медикус», guided.translit_name)."""
    from ..guided import translit_name

    words: set[str] = set()
    for t in texts:
        low = (t or "").casefold().replace("ё", "е")
        for w in _SEARCH_WORD.findall(low):
            words.add(w)
            if not w.isdigit():
                words.add(_lemma(w))
                if re.fullmatch(r"[a-z]{3,}", w):
                    words.add(translit_name(w))
        # склейка через дефис: «РЖД-Медицина» ищется и как «рждмедицина»
        words |= {m.replace("-", "") for m in re.findall(r"[0-9a-zа-яё]+(?:-[0-9a-zа-яё]+)+", low)}
    return words, {w.lstrip("0") or "0" for w in words if w.isdigit()}


def _fuzzy_dist(tok: str) -> int:
    return next((d for n, d in SEARCH_FUZZ_DIST if len(tok) >= n), 0)


def _token_hit(tok: str, words: set[str], nums: set[str]) -> bool:
    """Слово запроса нашлось среди слов строки: число — точно; слово — форма, лемма, начало слова (≥ 3 букв) или
    опечатка (≥ 5 букв: расстояние Левенштейна ≤ 1, от 8 букв ≤ 2) по форме или лемме."""
    from rapidfuzz.distance import Levenshtein

    if tok.isdigit():
        return (tok.lstrip("0") or "0") in nums
    lt = _lemma(tok)
    if tok in words or lt in words:
        return True
    if len(tok) >= SEARCH_PREFIX_MIN and any(w.startswith(tok) for w in words):
        return True
    d = _fuzzy_dist(tok)
    return d > 0 and any(not w.isdigit() and abs(len(w) - len(x)) <= d and Levenshtein.distance(x, w) <= d
                         for w in words for x in {tok, lt})


def search_clinics(rows: list[ClinicRow], q: str | None, *, care: str | None = None) -> list[ClinicRow]:
    """Поиск менеджера по перечню программы (GET …/clinic-candidates, ADR 0010).

    Слова запроса — без предлогов, типов улиц и дома, «клиника / поликлиника / медцентр» (_SEARCH_SKIP); «3-й», «1-м»
    — число. Числа обязательны: строка без числа запроса не подходит («медси на 1905 года»). Слово ищется в
    названии, юрлице, городе и адресе — по форме, лемме, началу слова (≥ 3 букв), с опечаткой (≥ 5 букв:
    «медсы», «будь здоровь») и латиницей кириллицей («zdorovie», «medsi»). Строка подходит, если нашлось хотя бы
    одно слово (кроме чисел); порядок — по числу найденных слов, затем строки, где слова нашлись в названии или
    юрлице, затем порядок перечня. Одна клиника по одному адресу (строки разных видов помощи) — одна строка выдачи:
    строка с видом помощи care (если задан и есть), иначе первая по перечню. Пустой запрос — весь перечень по
    порядку (тоже без повторов). Решения здесь нет — выбирает менеджер."""
    from ..guided import translit_name

    low = re.sub(r"(\d+)-?[а-яё]{1,3}\b", r"\1", (q or "").casefold().replace("ё", "е"))   # «3-й» → «3»
    toks = [t for t in _SEARCH_WORD.findall(low) if t not in _SEARCH_SKIP]
    nums = [t for t in toks if t.isdigit()]
    words = [[t, translit_name(t)] if re.search(r"[a-z]", t) else [t] for t in toks if not t.isdigit()]
    if not toks:
        return _unique(list(rows), care)
    scored: list[tuple[int, int, int, ClinicRow]] = []
    for i, r in enumerate(rows):
        nw, nn = _search_keys(r.clinic_name, r.legal_name)
        aw, an = _search_keys(r.city, r.address)
        aw |= nw
        an |= nn
        if not all(_token_hit(n, aw, an) for n in nums):
            continue
        found = sum(1 for vs in words if any(_token_hit(v, aw, an) for v in vs))
        if words and not found:
            continue
        name_hit = any(_token_hit(v, nw, nn) for vs in words for v in vs)
        scored.append((-found, 0 if name_hit else 1, i, r))
    scored.sort(key=lambda x: x[:3])
    return _unique([r for *_k, r in scored], care)


def _unique(rows: list[ClinicRow], care: str | None) -> list[ClinicRow]:
    """Строки одной клиники по одному адресу (разные виды помощи) — одна, на месте первой; из них — строка с видом
    помощи care, если есть."""
    groups: dict[tuple[str, str], list[ClinicRow]] = {}
    order: list[tuple[str, str]] = []
    for r in rows:
        k = (normalize_name(r.clinic_name), normalize_name(r.address or ""))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    out = []
    for k in order:
        g = groups[k]
        out.append(next((r for r in g if care and r.care_type == care), g[0]))
    return out


def care_matrix(contract_id: str, program: str | None = None, *, store: KitStore | None = None) -> dict[str, Any]:
    """Матрица «программа × вид помощи» договора (present=False — вида помощи в программе нет, с источником)."""
    cid, st = resolve(contract_id, store)
    return {"rows": [care_out(m) for m in st.care(cid, program)]}
