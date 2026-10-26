"""Подключение клиента (ADR 0011, docs/CONTRACTS.md §9): файлы клиента → договор в базе и в поиске без перезапуска.

Здесь же — общий конвейер разбора договора, которым пользуются и импорт комплекта (scripts/ingest_kit.py), и
подключение клиента: адаптер (определение СК по документам) → KitStore.save (транзакция по договору) → статусы
ТЗ п. 7.1 с проверкой полноты по типу документа и сверкой карточки. Код конвейера один — расхождений между
импортом и подключением быть не может.

Реестр клиентов и заданий хранит сервис (dms_api/store.py, AppStore); ядро зависит только от протокола Registry
и не импортирует dms_api. Файлы клиента — вне git: data/derived/clients/<ugN>/<uNN>/ и <ugN>/_group/.
В журнал и отчёт тексты документов не пишутся (`docs/rules.md` п. 4); имена файлов клиента в журнал не пишутся.
"""
from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from . import adapters
from .config import derived_dir
from .constants import SELFCHECK_MAX, DocStatus, Insurer
from .kit import KitContract, KitFile, KitGroup, nfc
from .schemas import ChatContext, ClientContract, ClientFile, ClientGroup, Group, OnboardingReport

log = logging.getLogger("dms.onboarding")

GROUP_DIR = "_group"        # общие файлы группы клиента: clients/<ugN>/_group/

# ------------------------------------------------------------------ общий конвейер (импорт и подключение)

PREMIUM_TOLERANCE = 1.0     # рубль: расхождение суммы премий и графика взносов из-за округления


def premium_check(contract: Any) -> dict[str, Any]:
    """Сумма премий по программам (премия на человека × численность) и сверка с суммой графика взносов."""
    parts = [p.premium_per_person * p.persons for p in contract.programs
             if p.premium_per_person is not None and p.persons is not None]
    premium = round(sum(parts), 2) if parts else None
    complete = bool(contract.programs) and len(parts) == len(contract.programs)
    amounts = [p.amount for p in contract.payments if p.amount is not None]
    payments = round(sum(amounts), 2) if amounts else None
    match = None if premium is None or payments is None or not complete else abs(premium - payments) <= PREMIUM_TOLERANCE
    return {"premium_sum": premium, "premium_sum_complete": complete, "payments_sum": payments, "match": match}


# Предупреждения адаптера, означающие ошибку сверки (а не находку в тексте документа вроде «битая ссылка» или
# «два варианта»): несошедшиеся суммы (≠), не найденная обязательная таблица, нет договора, дубли идентификаторов.
# У ParsedContract нет признака «ошибка» у предупреждения — пока распознаём по формулировкам адаптеров
# (adapters/renaissance.py:553–562, zetta.py:406, sogaz.py:474, 508); нужен структурный признак — см. отчёт этапа.
ERROR_MARKERS = re.compile(r"≠|не найден|нет договора|нет перечня клиник|нет текста условий|повторяющиеся идентификаторы")
# id документа в тексте предупреждения: договоры комплекта cNN и договоры клиентов uNN (ADR 0011)
_DOC_REF = re.compile(r"\b([cu]\d{2,}\.[a-z_]+(?:\.\d+)?)(?=[#:\s,)]|$)")

# Чем подтверждается полнота документа по типу (ТЗ п. 7.1 «полнота проверена»)
COMPLETENESS = {
    "contract": "есть пункты с номером",
    "program": "есть пункты",
    "memo": "есть пункты",
    "clinics": "есть строки клиник",
}


def adapter_errors(parsed: Any) -> dict[str | None, list[str]]:
    """Ошибки сверки адаптера по документам: doc_id из текста, иначе None (карточка договора).

    Основной канал — ParsedContract.errors; ERROR_MARKERS по warnings — страховка для адаптеров, которые ещё пишут
    ошибки сверки в предупреждения (перевести их на errors — волна 2)."""
    own = {d.id for d in parsed.documents}
    out: dict[str | None, list[str]] = {}
    explicit = list(getattr(parsed, "errors", []))
    for w in explicit + [w for w in parsed.warnings if ERROR_MARKERS.search(w)]:
        refs: list[str | None] = [r for r in _DOC_REF.findall(w) if r in own] or [None]
        for r in dict.fromkeys(refs):
            out.setdefault(r, []).append(w)
    return out


def completeness(d: Any, parsed: Any) -> str | None:
    """Полнота документа по типу; None — подтверждена, иначе текст предупреждения."""
    clauses = [c for c in parsed.clauses if c.doc_id == d.id]
    t = d.doc_type.value
    if t == "contract":
        ok = any(c.number for c in clauses)
    elif t in ("program", "memo"):
        ok = bool(clauses)
    elif t == "clinics":
        ok = any(r.doc_id == d.id for r in parsed.clinics)
    else:                                   # amendment, insurer_reply, other: есть хоть что-то разобранное
        ok = bool(clauses) or any(r.doc_id == d.id for r in parsed.clinics)
    if ok:
        return None
    return f"полнота не подтверждена: нужно «{COMPLETENESS.get(t, 'есть пункты или строки таблиц')}» — нет"


def card_problems(contract: Any, premium: dict[str, Any]) -> list[str]:
    """Сверка карточки договора: номер, программы, сумма премий = сумма графика взносов."""
    out = []
    if not contract.number:
        out.append("карточка: не найден номер договора")
    if not contract.programs:
        out.append("карточка: не найдены программы")
    if premium["match"] is False:
        out.append(f"карточка: сумма премий {premium['premium_sum']} ≠ сумма взносов {premium['payments_sum']}")
    elif premium["match"] is None:
        out.append("карточка: сверка премий не выполнена (нет премий, численности или графика взносов)")
    return out


def mark_statuses(store: Any, parsed: Any, premium: dict[str, Any]) -> None:
    """Статусы ТЗ п. 7.1 по каждому документу договора:
      * error — адаптер пометил документ ошибкой (остаётся как есть);
      * extracted + предупреждение — полнота по типу не подтверждена (COMPLETENESS);
      * checked + предупреждение — полнота есть, но не сошлась сверка: карточка договора (номер, программы,
        премии) или ошибка сверки адаптера (ERROR_MARKERS) — документ не «ready», оператор видит причину;
      * иначе checked → indexed → ready (индекс договора строится сразу)."""
    from .index.search import get_index

    errors = adapter_errors(parsed)
    contract_docs = [d for d in parsed.documents if d.doc_type.value == "contract"]
    card = card_problems(parsed.contract, premium) + errors.get(None, [])
    if not contract_docs:
        card.append("нет документа договора: карточка не проверена")
    # карточка и общие ошибки сверки относятся к документу договора (нет его — к первому документу)
    card_doc = contract_docs[0].id if contract_docs else (parsed.documents[0].id if parsed.documents else None)
    get_index(store, parsed.contract.id)
    for d in parsed.documents:
        if d.status is DocStatus.error:
            continue
        gap = completeness(d, parsed)
        if gap:
            store.set_doc_status(d.id, DocStatus.extracted, gap)
            continue
        problems = list(errors.get(d.id, [])) + (card if d.id == card_doc else [])
        if problems:
            for w in problems:
                store.set_doc_status(d.id, DocStatus.checked, w)
            continue
        for st in (DocStatus.checked, DocStatus.indexed, DocStatus.ready):
            store.set_doc_status(d.id, st)


def mark_group_doc(d: Any, clauses: list[Any]) -> None:
    """Общие документы ГК: rules — есть текст (пункты); catalog — прочитан. Это не условия договора: ready
    означает «загружен и прочитан», предупреждение «не часть договора» остаётся в warnings."""
    if d.status is DocStatus.error:
        return
    t = d.doc_type.value
    if t == "rules" and not any(c.doc_id == d.id and c.text.strip() for c in clauses):
        d.status = DocStatus.extracted
        d.warnings.append("полнота не подтверждена: в правилах нет текста")
    elif t in ("rules", "catalog"):
        d.status = DocStatus.ready
    else:
        d.status = DocStatus.extracted


def doc_entry(d: Any) -> dict[str, Any]:
    return {"doc_id": d.id, "doc_type": d.doc_type.value, "status": d.status.value, "warnings": list(d.warnings)}


def ingest_group_docs(g: KitGroup, root: Path, store: Any) -> dict[str, Any]:
    """Общие документы группы (adapters.parse_group_docs → KitStore.save_group). Сбой не останавливает договоры:
    файлы группы получают статус error в отчёте. Без файлов — только имя группы (save_group без документов)."""
    greport: dict[str, Any] = {"files": [f.path for f in g.files], "documents": []}
    parse_group = getattr(adapters, "parse_group_docs", None)
    if parse_group is not None and g.files:
        try:
            docs, clauses = parse_group(g, root)
            for d in docs:
                mark_group_doc(d, clauses)
            store.save_group(Group(id=g.id, name=g.name), docs, clauses)
            greport["documents"] = [doc_entry(d) | {"clauses": sum(1 for c in clauses if c.doc_id == d.id)}
                                    for d in store.group_documents(g.id)]
        except Exception as e:  # noqa: BLE001 — сбой общих документов ГК не останавливает импорт договоров
            greport["error"] = f"{type(e).__name__}: {e}"
            greport["documents"] = [{"doc_id": None, "path": f.path, "doc_type": f.doc_type,
                                     "status": DocStatus.error.value, "warnings": [greport["error"]]}
                                    for f in g.files]
    elif g.files:
        greport["warning"] = "общие документы ГК не разобраны: нет adapters.parse_group_docs"
    else:
        store.save_group(Group(id=g.id, name=g.name), [], [])
    return greport


def parse_contract(kc: KitContract, root: Path, *, force_generic: bool = False) -> tuple[Any | None, dict[str, Any]]:
    """Шаг 1 конвейера: разбор договора адаптером. (ParsedContract | None, запись отчёта).

    force_generic — выбор оператора «не распознавать формат» (ADR 0012 п. 5, ClientContract.force_generic):
    договор разбирается запасным адаптером generic, даже если формат СК узнаётся.
    Нет адаптера — status=skipped; сбой разбора — status=error, все файлы договора в отчёте со статусом error
    (ТЗ п. 7.1). База на этом шаге не трогается."""
    entry: dict[str, Any] = {"group_id": kc.group_id, "files": len(kc.files)}
    try:
        if force_generic:
            from .adapters import generic

            return generic.parse(kc, root), entry
        return adapters.parse_contract(kc, root), entry
    except RuntimeError as e:
        if "нет адаптера" not in str(e):
            raise
        entry.update(status="skipped", warnings=[str(e)])
        print(f"ПРЕДУПРЕЖДЕНИЕ {kc.id}: нет адаптера", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — ошибка разбора одного договора видна в отчёте (ТЗ п. 7.1)
        msg = f"{type(e).__name__}: {e}"
        entry.update(status="error", warnings=[msg],
                     documents=[{"doc_id": None, "path": f.path, "doc_type": f.doc_type,
                                 "status": DocStatus.error.value, "warnings": [msg]} for f in kc.files])
        print(f"ОШИБКА {kc.id}: {type(e).__name__}", file=sys.stderr)
    return None, entry


def save_contract(kc: KitContract, parsed: Any, store: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Шаг 2 конвейера: KitStore.save (одна транзакция, договор заменяется целиком) → статусы ТЗ п. 7.1 →
    счётчики в записи отчёта."""
    store.save(parsed)
    premium = premium_check(parsed.contract)
    mark_statuses(store, parsed, premium)
    c = parsed.contract
    docs = store.documents(c.id)
    parsed_paths = {d.source_path for d in docs}
    entry.update(
        status="ok",
        insurer=c.insurer.value,
        documents=[doc_entry(d) for d in docs],
        files_not_parsed=[f.path for f in kc.files if f.path not in parsed_paths],
        clauses=len(parsed.clauses),
        clauses_numbered=sum(1 for cl in parsed.clauses if cl.number),
        clinics=len(parsed.clinics),
        programs=[p.name for p in c.programs],
        care_rows=len(parsed.care),
        premium=premium,
        missing_docs=list(c.missing_docs),
        warnings=list(parsed.warnings),
    )
    return entry


def ingest_contract(kc: KitContract, root: Path, store: Any) -> dict[str, Any]:
    """Разбор и запись одного договора (импорт комплекта): parse_contract → save_contract."""
    parsed, entry = parse_contract(kc, root)
    return entry if parsed is None else save_contract(kc, parsed, store, entry)


# ------------------------------------------------------------------ реестр клиентов (протокол)

class Registry(Protocol):
    """Реестр клиентов (реализация — dms_api.store.AppStore). Ядро читает его и пишет только снимок обработанных
    файлов (save_processed: какие файлы и sha256 дали версию договора в базе) — по нему reparse_all повторяет
    именно обработанную версию, а сервис показывает «файлы изменены» (ревизия w8, пп. 6–7). Реестр без этих
    методов допустим: снимок тогда не ведётся (_snapshot_api)."""

    def client_groups(self) -> list[ClientGroup]: ...
    def client_group(self, group_id: str) -> ClientGroup: ...          # KeyError — нет группы
    def client_contract(self, contract_id: str) -> ClientContract: ...  # KeyError — нет договора
    def client_files(self, owner_id: str) -> list[ClientFile]: ...      # owner — uNN (файлы договора) или ugN


class OnboardingError(Exception):
    """Ошибка подключения, которую видит клиент: код и сообщение по-русски (без текстов документов)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def client_root() -> Path:
    """data/derived/clients (DMS_DATA_DIR учитывается через config.derived_dir)."""
    return derived_dir() / "clients"


def file_relpath(group_id: str, owner_id: str, f: ClientFile) -> str:
    """Путь файла клиента относительно client_root: <ugN>/<uNN>/<имя> или <ugN>/_group/<имя>."""
    folder = GROUP_DIR if f.scope == "group" else owner_id
    return f"{group_id}/{folder}/{f.name}"


def _kit_file(group_id: str, owner_id: str, f: ClientFile) -> KitFile:
    return KitFile(path=nfc(file_relpath(group_id, owner_id, f)), title=nfc(Path(f.name).stem), fmt=f.fmt,
                   doc_type=f.doc_type, scope=f.scope, size=f.size, sha256=f.sha256)


# Типы общих файлов группы, которые не условия договора (`docs/rules.md` п. 0а): разбираются как документы ГК
GROUP_ONLY_TYPES = frozenset({"rules", "catalog"})


def kit_contract(group: ClientGroup, contract: ClientContract, files: list[ClientFile]) -> KitContract:
    """Опись договора клиента для адаптера; пути — от client_root. Файлы договора (scope=contract) и общие файлы
    группы (scope=group, папка _group/) кроме правил СК и каталога: общая программа или перечень группы разбирается
    в КАЖДЫЙ договор отдельно, под его contract_id (ADR 0005: условия не переносятся — у договора своя копия).
    Тип документа — выбор клиента или config/doc_types.yaml (classify при загрузке)."""
    own = [_kit_file(group.id, contract.id, f) for f in files
           if f.scope != "group" or f.doc_type not in GROUP_ONLY_TYPES]
    return KitContract(id=contract.id, group_id=group.id, legal_entity=contract.legal_entity, files=own)


def kit_group(group: ClientGroup, files: list[ClientFile]) -> KitGroup:
    """Опись общих документов группы клиента — правила СК и каталог (не условия договора, `docs/rules.md` п. 0а)."""
    shared = [_kit_file(group.id, group.id, f) for f in files if f.scope == "group" and f.doc_type in GROUP_ONLY_TYPES]
    return KitGroup(id=group.id, name=group.name, files=shared)


# ------------------------------------------------------------------ кэши движка

def invalidate(contract_id: str) -> None:
    """Сброс всех кэшей движка по договору: перечень клиник для слота «клиника» и кандидатов выбора
    (scenarios/route.py: _CLINIC_TOKEN_CACHE, _LIST_CACHE), индекс поиска по пунктам (index/search.py).
    Остальные кэши движка (@cache в guided, route, compose, promises, llm) — конфиги и словари, от договора не
    зависят; банк kNN маршрутизации — примеры вопросов, не договоры."""
    from .index import search
    from .scenarios import route

    route.invalidate(contract_id)
    search.invalidate(contract_id)


# ------------------------------------------------------------------ самопроверка

def _cited(answer: Any) -> bool:
    """В ответе есть хотя бы одна проверенная ссылка (citations.verify) — в утверждениях ответа или договора."""
    statements = list(answer.statements) + [s for pc in answer.per_contract for s in pc.statements]
    return any(c.verified for s in statements for c in s.citations)


def selfcheck_questions(contract_id: str, store: Any) -> list[tuple[str, str]]:
    """(программа, вопрос): по каждой программе — «Входит ли <вид помощи> в программу?» по строкам матрицы и,
    если у программы есть перечень, вопрос о первой клинике перечня. Не больше SELFCHECK_MAX; вопросы
    распределяются по программам по очереди, чтобы при пределе проверялись все программы."""
    c = store.contract(contract_id)
    per_program: list[list[tuple[str, str]]] = []
    for p in c.programs:
        qs: list[tuple[str, str]] = []
        rows = store.clinics(contract_id, program=p.name)
        if rows:
            r = rows[0]
            # ячейка перечня с несколькими видами («Поликлиника | Врач на дом») — вопрос об одном, первом
            care = str(r.care_type).split("|")[0].strip() or r.care_type
            qs.append((p.name, f"Можно ли получить «{care}» в клинике «{r.clinic_name}»?"))
        seen: set[str] = set()
        for m in store.care(contract_id, program=p.name):
            if m.care_type in seen:
                continue
            seen.add(m.care_type)
            qs.append((p.name, f"Входит ли «{m.care_type}» в программу?"))
        per_program.append(qs)
    out: list[tuple[str, str]] = []
    i = 0
    while len(out) < SELFCHECK_MAX and any(i < len(q) for q in per_program):
        for q in per_program:
            if i < len(q) and len(out) < SELFCHECK_MAX:
                out.append(q[i])
        i += 1
    return out


def selfcheck(contract_id: str, store: Any, *, answer_fn: Callable[..., Any] | None = None) -> dict[str, int]:
    """Самопроверка договора (ADR 0011 п. 4): прогон engine.answer без модели по типовым вопросам; cited — ответы
    хотя бы с одной проверенной ссылкой. Индикатор готовности, не эталон."""
    if answer_fn is None:
        from .engine import answer as answer_fn
    c = store.contract(contract_id)
    asked = cited = 0
    for program, q in selfcheck_questions(contract_id, store):
        ctx = ChatContext(group_id=c.group_id, contract_id=contract_id, program=program)
        asked += 1
        try:
            a = answer_fn(q, ctx, llm=None, store=store)
        except Exception as e:  # noqa: BLE001 — сбой одного вопроса самопроверки не роняет подключение
            log.warning("selfcheck %s: ошибка %s", contract_id, type(e).__name__)
            continue
        cited += _cited(a)
    return {"asked": asked, "cited": cited}


# ------------------------------------------------------------------ битые файлы и понятные предупреждения

FILE_UNREADABLE = "Файл «{name}» не читается: {msg}"


def probe_file(path: Path, fmt: str) -> str | None:
    """Файл открывается своим читателем (io/): None — да, иначе причина по-русски (без текста документа).
    Зовётся только после сбоя адаптера — найти, какой файл договора битый (ревизия w8 п. 5)."""
    from .io.errors import InputError

    try:
        if fmt == "docx":
            from .io.docx_reader import read_docx

            read_docx(path)
        elif fmt == "xlsx":
            from .io.xlsx_reader import read_xlsx

            read_xlsx(path)
        elif fmt == "pdf":
            from .io.pdf_reader import read_pdf

            read_pdf(path)
        elif fmt == "txt":
            from .io.txt_reader import read_txt

            read_txt(path)
    except InputError as e:
        return e.message
    except Exception:  # noqa: BLE001 — любой сбой читателя = файл не читается; подробности могут содержать данные
        return f"файл повреждён или это не {fmt.upper()}"
    return None


def _without(kc: KitContract, bad: set[str]) -> KitContract:
    return KitContract(id=kc.id, group_id=kc.group_id, legal_entity=kc.legal_entity,
                       files=[f for f in kc.files if f.path not in bad])


def parse_client(kc: KitContract, root: Path, *, force_generic: bool = False
                 ) -> tuple[Any | None, dict[str, Any], dict[str, str]]:
    """Разбор договора клиента с изоляцией битых файлов: (ParsedContract | None, запись отчёта, {путь: причина}).

    Адаптеры Зетты / Ренессанса / СОГАЗ разбирают договор целиком (карточка, программы и перечни связаны), поэтому
    по отдельности файлы не разобрать. При сбое каждый файл проверяется своим читателем (probe_file); нечитаемые
    исключаются и разбор повторяется по остальным — договор подключается без битого файла, у файла в отчёте —
    ошибка. Если битых файлов не нашли (сбой в связке документов) или без них разбор тоже не удался — failed,
    битые файлы названы в отчёте. Запасной адаптер generic сам разбирает файлы по одному (adapters/generic.py)."""
    parsed, entry = parse_contract(kc, root, force_generic=force_generic)
    if parsed is not None or entry.get("status") != "error":
        return parsed, entry, {}
    bad: dict[str, str] = {}
    for f in kc.files:
        from .adapters.base import abs_path

        why = probe_file(abs_path(root, f), f.fmt)
        if why is not None:
            bad[f.path] = why
    if not bad or len(bad) == len(kc.files):
        return None, entry, bad
    parsed, entry2 = parse_contract(_without(kc, set(bad)), root, force_generic=force_generic)
    return parsed, (entry2 if parsed is not None else entry), bad


# id в тексте предупреждения адаптера: документ (c05.program.2, u01.contract, ug1.rules) с необязательной ссылкой
# на пункт или строку (#p12, #t0r5), id договора (u01:) и путь файла клиента (ug1/u01/имя.docx)
_ID_REF = re.compile(r"\b((?:ug\d+|[cu]\d{2,})\.[a-z_]+(?:\.\d+)?)(#[\w.\-]+)?")
_CID_REF = re.compile(r"\b[cu]\d{2,}\b(?![./#])")
_PATH_REF = re.compile(r"\bug\d+/(?:u\d{2,}|" + GROUP_DIR + r")/")


def humanize(text: str, titles: dict[str, str], numbers: dict[str, str | None]) -> str:
    """Технические id в предупреждении → «документ → пункт» (ревизия w8 п. 8): «u01.program#p12» → «Программа» →
    п. 1.2; id без известного документа остаются как есть; id договора → «договор»; путь файла → имя файла."""
    def doc(m: re.Match[str]) -> str:
        did, frag = m.group(1), m.group(2)
        if did not in titles:
            return m.group(0)
        out = f"«{titles[did]}»"
        num = numbers.get(did + frag) if frag else None
        return out + (f" → п. {num}" if num else "")

    text = _PATH_REF.sub("", text)
    text = _ID_REF.sub(doc, text)
    return _CID_REF.sub("договор", text)


def _doc_view(d: Any, scope: str, titles: dict[str, str], numbers: dict[str, str | None]) -> dict[str, Any]:
    return {"doc_id": d.id, "title": d.title, "doc_type": d.doc_type.value, "status": d.status.value,
            "scope": scope, "warnings": [humanize(w, titles, numbers) for w in d.warnings]}


def _bad_views(bad: dict[str, str], kc: KitContract) -> list[dict[str, Any]]:
    """Нечитаемые файлы — в отчёт со статусом error и причиной (договор разобран по остальным)."""
    out = []
    for f in kc.files:
        if f.path in bad:
            name = Path(f.path).name
            out.append({"doc_id": None, "title": Path(f.path).stem, "doc_type": f.doc_type,
                        "status": DocStatus.error.value, "scope": f.scope,
                        "warnings": [FILE_UNREADABLE.format(name=name, msg=bad[f.path])]})
    return out


# ------------------------------------------------------------------ снимок обработанных файлов

def _snapshot_api(registry: Any) -> bool:
    return callable(getattr(registry, "save_processed", None)) and callable(getattr(registry, "processed_files",
                                                                                      None))


def file_key(f: ClientFile) -> tuple[str, str, str, str]:
    """Что влияет на разбор: область, имя, содержимое (sha256) и тип документа."""
    return (f.scope, f.name, f.sha256, f.doc_type)


def files_changed(current: list[ClientFile], processed: list[ClientFile] | None) -> bool:
    """Файлы договора отличаются от обработанных (снимок при done). Снимка нет — договор не обрабатывался: False."""
    if processed is None:
        return False
    return {file_key(f) for f in current} != {file_key(f) for f in processed}


def _sha256(path: Path) -> str | None:
    import hashlib

    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _files_on_disk(group_id: str, contract_id: str, files: list[ClientFile], root: Path) -> bool:
    """Все файлы снимка лежат на диске с тем же содержимым (sha256) — разбор повторит ту же версию."""
    owner = {f.name: (group_id if f.scope == "group" else contract_id) for f in files}
    return all(_sha256(root / file_relpath(group_id, owner[f.name], f)) == f.sha256 for f in files)


# ------------------------------------------------------------------ разметка таблиц оператором (ADR 0012)

def _mapping_api(registry: Any) -> bool:
    return callable(getattr(registry, "table_mappings", None)) and callable(getattr(registry, "insurer_profiles",
                                                                                    None))


GENERIC_NO_LIST = "формат СК не распознан"     # начало предупреждения adapters/generic.py о перечне


def _match_program(name: str, known: list[str]) -> str:
    """Программа строки перечня → программа договора, если она видна в названии (w9-fix п. 4): то же название без
    регистра и знаков, или название программы договора — целиком внутри строки-заголовка («Вариант № 2 Рязань
    Стандарт» → «Рязань Стандарт»). Единственное совпадение — его название, иначе как есть."""
    from .table_mapping import norm_cell

    n = norm_cell(name)
    same = [k for k in known if norm_cell(k) == n]
    if same:
        return same[0]
    inside = [k for k in known if norm_cell(k) and f" {norm_cell(k)} " in f" {n} "]
    return inside[0] if len(inside) == 1 else name


def _tables_list(labels: list[str], limit: int = 3) -> str:
    return ", ".join(f"«{x}»" for x in labels[:limit]) + (f" и ещё {len(labels) - limit}" if len(labels) > limit
                                                          else "")


def apply_mappings(cc: ClientContract, kc: KitContract, parsed: Any, root: Path, registry: Any
                   ) -> tuple[int, list[str], list[str], int]:
    """Разметки таблиц → строки перечня и программы договора (только разбор запасным адаптером, ADR 0012 п. 4).

    Порядок: (1) разметки договора (registry.table_mappings) — для своих таблиц; таблицы больше нет — предупреждение;
    (2) профили СК (registry.insurer_profiles) — для остальных таблиц, только если профиль применяется сам
    (table_mapping.auto_profile, w9-fix п. 2: СК договора совпала с СК профиля, подпись сильная, содержимое похоже на
    перечень); подошедший по подписи профиль без этих условий — только предложение на экране разметки (сводное
    предупреждение с причиной). Разметка без источника программы (нет колонки, программы для таблицы и программы из
    названия) применяется, только если у договора по разбору ровно одна программа, иначе не применяется (w9-fix
    п. 1г: строки получали программу «Перечень клиник договора»). Строки перечня дописываются в parsed.clinics (id
    уникальны: ссылка на строку таблицы), программы строк — в parsed.contract.programs (название программы договора,
    если оно видно в программе строки — _match_program). Предупреждения — сводные по источнику разметки (w9-fix
    п. 6), плюс «похожие неразмеченные таблицы» (w9-fix п. 5).

    Матрица «программа × вид помощи» (ADR 0013, CONTRACTS §11) — порядок: перечни → программы договора → матрицы
    (разметки role=care_matrix договора, затем профили той же СК; программы матрицы сопоставляются с программами
    договора и перечня) → derive_care (виды помощи строк перечней с флажком → present=True) → parsed.care
    (_merge_care: явная матрица важнее; пара, где матрица «нет», а перечень даёт клиники этого вида, — противоречие,
    не учитывается). Возвращает (размечено таблиц перечня, предупреждения, id применённых профилей — по одному на
    договор, размечено матриц — таблиц матриц и перечней с derive_care)."""
    from . import table_mapping as tm
    from .schemas import Program

    saved = list(registry.table_mappings(cc.id))
    own = {tm.ref_key(ref): m for ref, m in saved}
    profiles = list(registry.insurer_profiles())
    if not own and not profiles:
        return 0, [], [], 0
    doc_ids = {d.source_path: d.id for d in parsed.documents if d.status is not DocStatus.error}
    titles = {d.id: d.title for d in parsed.documents}
    tables = tm.list_tables(kc, root, doc_ids)
    known = [p.name for p in parsed.contract.programs]
    single = known[0] if len(known) == 1 else None
    warnings: list[str] = []
    used: list[str] = []
    mapped = 0
    seen: set[str] = {r.id for r in parsed.clinics}
    found = {tm.ref_key(ref) for ref, _, _ in tables}
    for ref, _m in saved:
        if tm.ref_key(ref) not in found:
            warnings.append(tm.warn_text("mapping_lost",
                                         table=tm.table_label(ref, titles.get(ref.doc_id, ref.doc_id))))
    # сводка: источник («договор» / id профиля) → [таблиц, строк]; ключ счётчика → [строк, таблицы]
    by_source: dict[str, list[int]] = {}
    sums: dict[str, tuple[list[int], list[str]]] = {}
    suggested: dict[tuple[str, str], int] = {}       # (СК профиля, причина) → таблиц
    names: dict[str, str] = {p.id: p.insurer_name for p in profiles}
    applied_rows: list[tm.Rows] = []
    unmapped: list[tuple[str, tm.Rows]] = []
    # матрицы — после перечней и программ (CONTRACTS §11): (ref, название, строки, разметка, профиль)
    matrices: list[tuple[Any, str, tm.Rows, Any, Any]] = []
    derived: list[tuple[str, list[Any]]] = []      # перечни с derive_care: (название таблицы, строки перечня)
    # перечень → (программы строк, программа из названия таблицы)
    list_tables: dict[tuple[str, str, int | None, str | None], tuple[dict[int, list[str]], bool]] = {}
    matrix_tables: list[Any] = []                  # применённые матрицы (TableRef)
    star: list[str] = []                           # перечни с программой «*» (все программы договора)
    for ref, title, rows in tables:
        label = tm.table_label(ref, titles.get(ref.doc_id, title))
        mapping = own.get(tm.ref_key(ref))
        profile = None
        if mapping is None:
            profile, why = tm.auto_profile(rows, profiles, cc.insurer_name)
            if profile is None and why:
                hint = tm.profile_for(rows, profiles, cc.insurer_name)
                key = (hint.insurer_name if hint else "", why)
                suggested[key] = suggested.get(key, 0) + 1
        use = mapping if mapping is not None else (profile.mapping if profile is not None else None)
        if use is None:
            unmapped.append((label, rows))
            continue
        if use.role == "care_matrix":
            matrices.append((ref, label, rows, use, profile))
            continue
        if not tm.has_program_source(use) and single is None:
            warnings.append(tm.warn_text("no_program_skip", table=label))
            continue
        rp: dict[int, list[str]] = {}
        try:
            got, stats = tm.apply_rows(cc.id, ref, rows, use, ref.doc_id, fallback_program=single, row_programs=rp)
        except tm.MappingError:
            if profile is None:
                warnings.append(tm.warn_text("empty", table=label))
            continue                          # профиль к этой таблице не подошёл — таблица остаётся без разметки
        if stats.get("no_title"):
            warnings.append(tm.warn_text("no_title", table=label))
            continue
        if profile is not None and not got:
            continue
        if not got:
            warnings.append(tm.warn_text("empty", table=label))
            continue
        got = [r for r in got if r.id not in seen]
        for r in got:
            if r.program != tm.ALL_PROGRAMS:
                r.program = _match_program(r.program, known)
        seen |= {r.id for r in got}
        list_tables[tm.ref_key(ref)] = ({i: [_match_program(p, known) for p in ps if p] for i, ps in rp.items()},
                                        use.program_from_title)
        parsed.clinics += got
        if use.derive_care:
            derived.append((label, got))
        if (use.program or "").strip() == tm.ALL_PROGRAMS:
            star.append(label)
        mapped += 1
        applied_rows.append(rows)
        src = profile.id if profile is not None else ""
        acc = by_source.setdefault(src, [0, 0])
        acc[0] += 1
        acc[1] += len(got)
        for k in tm.STAT_KEYS:
            if stats.get(k):
                cnt, labels = sums.setdefault(k, ([0], []))
                cnt[0] += stats[k]
                labels.append(label)
        if profile is not None and profile.id not in used:
            used.append(profile.id)          # счётчик used — по договорам: один раз на договор (w9-fix п. 2)
    for src, (k, n) in by_source.items():
        warnings.insert(0, tm.warn_text("sum_applied_profile", insurer=names.get(src, src), k=k, n=n) if src else
                        tm.warn_text("sum_applied_contract", k=k, n=n))
    for key in tm.STAT_KEYS:
        if key in sums:
            cnt, labels = sums[key]
            warnings.append(tm.warn_text("sum_rows", what=tm.warn_text(f"sum_{key}"), n=cnt[0], k=len(labels),
                                         tables=_tables_list(labels)))
    for (insurer, why), k in suggested.items():
        warnings.append(tm.warn_text("profile_suggested_only", insurer=insurer, n=k, reason=tm.warn_text(why)))
    # похожие на размеченные, но неразмеченные таблицы (w9-fix п. 5) — чтобы оператор их увидел
    similar = [label for label, rows in unmapped if any(tm.similar(rows, a) for a in applied_rows)]
    if similar:
        warnings.append(tm.warn_text("similar_unmapped", n=len(similar), tables=_tables_list(similar)))
    if mapped:
        have = {p.name for p in parsed.contract.programs}
        for name in dict.fromkeys(r.program for r in parsed.clinics):
            if name and name not in have and name != tm.ALL_PROGRAMS:
                parsed.contract.programs.append(Program(name=name))
                have.add(name)
        # «перечня клиник нет» запасного адаптера больше не верно — перечень есть по разметке оператора
        parsed.warnings = [w for w in parsed.warnings if not w.startswith(GENERIC_NO_LIST)]
        parsed.warnings.append(tm.warn_text("generic_mapped"))
    if mapped:
        parsed.clinics = tm.expand_all_programs(parsed.clinics, [p.name for p in parsed.contract.programs])
        derived = [(label, tm.expand_all_programs(got, [p.name for p in parsed.contract.programs]))
                   for label, got in derived]
        if star and not parsed.contract.programs:
            warnings.append(tm.warn_text("all_no_programs", table=_tables_list(star)))
    n_matrices = _apply_matrices(cc, parsed, matrices, derived, profiles, warnings, used, matrix_tables)
    _table_clauses(parsed, list_tables, matrix_tables)
    return mapped, warnings, used, n_matrices


# id пункта-строки таблицы запасного адаптера: docx «<doc>#t<N>r<R>[p<K>]», xlsx «<doc>#<лист>!<строка>»
_ROW_CLAUSE = re.compile(r"^(?P<row>(?P<table>.+?#t\d+)r(?P<r>\d+))(?:p\d+)?$|^(?P<xrow>(?P<sheet>.+?#[^#!]+)!(?P<xr>\d+))$")


def _table_clauses(parsed: Any,
                   lists: dict[tuple[str, str, int | None, str | None], tuple[dict[int, list[str]], bool]],
                   matrices: list[Any]) -> None:
    """Пункты-строки размеченных таблиц (adapters/generic.py читает таблицы как пункты, этап w10-fix).

    * строка, ставшая строкой перечня (source_ref строки клиники), и строки таблиц матрицы — не пункты (их видно как
      перечень и матрицу; id пункта не перекрывает ссылку строки в citations);
    * остальные строки перечня (название программы, подзаголовки видов помощи, условия) — программа, действующая в
      этой строке по разметке (table_mapping.apply_rows row_programs); строка блока нескольких программ («Общие
      для вариантов …») — пункт на каждую программу, id «…@v<N>» (как у адаптера «Зетты»);
    * «программа — из названия таблицы» (program_from_title): название программы действует и дальше по документу —
      на строки выше первого названия в следующей размеченной таблице и на неразмеченные таблицы до следующей
      размеченной (текст программы, набранный в таблице сразу после перечня программы — так у «Ренессанса»:
      без этого тексты всех программ договора становились общими пунктами, и вопрос о программе «Саратов» читал
      исключения программы «ВИП»);
    * неразмеченная таблица, в названии которой (абзац прямо перед таблицей или первая строка из одной ячейки)
      названа ровно одна программа договора («Услуги программы «Москва»», этап w10-fix2), — строки получают эту
      программу (_title_programs): без этого строки «Массаж | да» таблицы одной программы были общими пунктами и
      давали «можно» другой программе;
    * строки прочих таблиц — без программы (общие пункты договора; guided не даёт по ним «входит», если программ у
      договора ≥ 2)."""
    from . import table_mapping as tm

    if not lists and not matrices:
        return
    clinic_src = {r.source_ref for r in parsed.clinics}
    by_table = {_table_id(key): v for key, v in lists.items()}
    matrix_ids = {_table_id(tm.ref_key(ref)) for ref in matrices}
    # по документу: номера размеченных docx-таблиц перечня по порядку → программа «дальше по документу»
    mapped: dict[str, list[int]] = {}
    for doc_id, kind, index, _sheet in lists:
        if kind == "docx_table" and index is not None:
            mapped.setdefault(doc_id, []).append(index)
    for v in mapped.values():
        v.sort()

    def carried(doc_id: str, index: int) -> list[str]:
        """Программа из названия ближайшей размеченной таблицы выше (её последняя строка)."""
        prev = [i for i in mapped.get(doc_id, []) if i < index]
        if not prev:
            return []
        progs, from_title = by_table[f"{doc_id}#t{prev[-1]}"]
        return progs[max(progs)] if from_title and progs else []

    titled = _title_programs(parsed.clauses, [p.name for p in parsed.contract.programs])
    out = []
    for cl in parsed.clauses:
        m = _ROW_CLAUSE.match(cl.id)
        if m is None:
            out.append(cl)
            continue
        row = m.group("row") or m.group("xrow")
        table = m.group("table") or m.group("sheet")
        r = int(m.group("r")) if m.group("r") is not None else int(m.group("xr")) - 1
        if row in clinic_src or table in matrix_ids:
            continue
        got = by_table.get(table)
        index = int(table.rsplit("#t", 1)[1]) if m.group("table") else None
        names: list[str] = []
        if got is not None:
            progs, from_title = got
            if r in progs:
                names = progs[r]
            elif from_title and index is not None and (not progs or r < min(progs)):
                names = carried(cl.doc_id, index)
        elif table in titled:
            names = [titled[table]]
        elif index is not None:
            names = carried(cl.doc_id, index)
        if len(names) <= 1:
            cl.program = names[0] if names else None
            out.append(cl)
            continue
        out += [cl.model_copy(update={"id": f"{cl.id}@v{k + 1}", "program": p}) for k, p in enumerate(names)]
    parsed.clauses = out


def _named_program(text: str, known: list[str]) -> str | None:
    """Ровно одна программа договора, названная в тексте (словами, без регистра; «Москва» внутри «Москва ВИП» не
    считается отдельной программой). Ни одной или несколько — None."""
    from .table_mapping import norm_cell

    n = f" {norm_cell(text)} "
    hits = [k for k in dict.fromkeys(known) if norm_cell(k) and f" {norm_cell(k)} " in n]
    hits = [k for k in hits if not any(o != k and f" {norm_cell(k)} " in f" {norm_cell(o)} " for o in hits)]
    return hits[0] if len(hits) == 1 else None


# Название таблицы: абзац перед ней не длиннее этого (длиннее — текст условий, а не название)
TITLE_PARA_CHARS = 200


def _title_programs(clauses: list[Any], known: list[str]) -> dict[str, str]:
    """id таблицы запасного адаптера → программа из её названия (этап w10-fix2): абзац прямо перед первой строкой
    таблицы (не длиннее TITLE_PARA_CHARS) или первая строка таблицы из одной ячейки; программа — _named_program."""
    out: dict[str, str] = {}
    if not known:
        return out
    prev: Any = None
    seen: set[str] = set()
    for cl in clauses:
        m = _ROW_CLAUSE.match(cl.id)
        if m is None:
            prev = cl
            continue
        table = m.group("table") or m.group("sheet")
        if table not in seen:
            seen.add(table)
            texts = [cl.text] if " | " not in cl.text else []
            if prev is not None and prev.doc_id == cl.doc_id and len(prev.text or "") <= TITLE_PARA_CHARS:
                texts.insert(0, prev.text)
            got = next((p for t in texts if (p := _named_program(t, known))), None)
            if got:
                out[table] = got
        prev = None                      # строка таблицы — не название следующей таблицы
    return out


def _table_id(key: tuple[str, str, int | None, str | None]) -> str:
    """id таблицы по ключу TableRef: docx «<doc>#t<N>», xlsx «<doc>#<лист>» (как citations.list_table_ref)."""
    doc_id, kind, index, sheet = key
    return f"{doc_id}#{sheet}" if kind == "xlsx_sheet" else f"{doc_id}#t{index}"


def _apply_matrices(cc: ClientContract, parsed: Any, matrices: list[tuple[Any, str, Any, Any, Any]],
                    derived: list[tuple[str, list[Any]]], profiles: list[Any], warnings: list[str],
                    used: list[str], applied: list[Any] | None = None) -> int:
    """Матрицы по разметке и derive_care → parsed.care (ADR 0013). Программы матрицы сопоставляются с программами
    договора и перечня (уже в parsed.contract.programs); профиль матрицы применяется сам, только если содержимое
    по программам договора похоже на матрицу (matrix_check). Предупреждения — сводные. Возвращает число таблиц."""
    from . import table_mapping as tm
    from .schemas import Program

    programs = [p.name for p in parsed.contract.programs]
    names = {p.id: p.insurer_name for p in profiles}
    explicit: list[Any] = []
    by_source: dict[str, list[int]] = {}
    skipped: tuple[list[int], list[str]] = ([0], [])
    conflicts = 0
    unknown: list[str] = []
    n_tables = 0
    for ref, label, rows, mapping, profile in matrices:
        if profile is not None and tm.check_any(rows, mapping, programs):
            warnings.append(tm.warn_text("profile_suggested_only", insurer=profile.insurer_name, n=1,
                                         reason=tm.warn_text("why_suspicious")))
            continue
        try:
            res = tm.apply_matrix_rows(cc.id, ref, rows, mapping, programs)
        except tm.MappingError:
            if profile is None:
                warnings.append(tm.mwarn("no_program_source", table=label))
            continue
        if res.skipped_total:
            skipped[0][0] += res.skipped_total
            skipped[1].append(label)
        conflicts += res.conflicts
        unknown += [p for p in res.unknown if p not in unknown]
        if not res.rows:
            if profile is None:
                warnings.append(tm.mwarn("empty", table=label))
            continue
        explicit += res.rows
        n_tables += 1
        if applied is not None:
            applied.append(ref)
        acc = by_source.setdefault(profile.id if profile is not None else "", [0, 0])
        acc[0] += 1
        acc[1] += len(res.rows)
        if profile is not None and profile.id not in used:
            used.append(profile.id)
    derived_rows = tm.matrix_from_clinics(cc.id, [r for _l, got in derived for r in got])
    n_tables += len([1 for _l, got in derived if got])
    # «нет» матрицы при клиниках этого вида в перечне той же программы (этап w10-fix2, `docs/rules.md` п. 2) — с флажком
    # derive_care и без него: противоречие документов, а не «нельзя» и не «пары нет»
    contra = tm.list_conflicts(explicit, list(parsed.clinics))
    care, dropped = _merge_care(list(parsed.care), explicit, derived_rows, contra)
    parsed.care = care
    for src, (k, n) in by_source.items():
        warnings.append(tm.mwarn("applied_profile", insurer=names.get(src, src), k=k, n=n) if src else
                        tm.mwarn("sum_applied", k=k, n=n))
    if derived_rows:
        warnings.append(tm.mwarn("sum_derived", n=len(derived_rows), k=len(derived)))
    if skipped[0][0]:
        warnings.append(tm.mwarn("sum_skipped", n=skipped[0][0], k=len(skipped[1]), tables=_tables_list(skipped[1])))
    if conflicts:
        warnings.append(tm.mwarn("sum_conflicts", n=conflicts))
    if unknown:
        warnings.append(tm.mwarn("sum_unknown", programs=", ".join(f"«{p}»" for p in unknown[:5])))
    if dropped:
        warnings.append(tm.mwarn("derived_conflict", n=dropped))
    if contra:
        warnings.append(tm.list_conflict_warning(contra))
    have = {p.name for p in parsed.contract.programs}
    for m in explicit:
        if m.program not in have:
            parsed.contract.programs.append(Program(name=m.program))
            have.add(m.program)
    return n_tables


def _merge_care(base: list[Any], explicit: list[Any], derived: list[Any],
                contra: list[tuple[Any, list[str]]] | None = None) -> tuple[list[Any], int]:
    """Матрица договора: строки адаптера (base) → явная матрица разметки → derive_care. Пара «программа × вид помощи»
    уже есть — ссылки добавляются, если значение то же; разные значения — противоречие: пара убирается (ни «можно»,
    ни «нельзя» из неё; `docs/rules.md` п. 2). Возвращает (строки, сколько пар убрано).

    contra (этап w10-fix2, table_mapping.list_conflicts) — «нет» матрицы при клиниках этого вида в перечне: пара
    остаётся двумя строками — «не входит» (ячейка матрицы) и «входит» (строки перечня, ссылки «…#care») с тем же
    названием вида помощи; guided.check_care видит обе и показывает противоречие (оба фрагмента, documents =
    contradictory → «при условиях, нужно подтверждение СК»). Строки derive_care этой пары в неё не сливаются."""
    from .schemas import CareMatrix

    pair = {(m.program, m.care_type) for m, _refs in contra or []}
    out: dict[tuple[str, str], Any] = {}
    bad: set[tuple[str, str]] = set()
    for m in [*base, *explicit, *derived]:
        key = (m.program, m.care_type)
        if key in bad:
            continue
        if key in pair and m.present:
            continue                         # «входит» по перечню этой пары — отдельной строкой ниже
        cur = out.get(key)
        if cur is None:
            out[key] = m.model_copy(deep=True)
        elif cur.present == m.present:
            cur.source_refs += [r for r in m.source_refs if r not in cur.source_refs]
        else:
            bad.add(key)
            out.pop(key)
    rows = list(out.values())
    for m, refs in contra or []:
        if (m.program, m.care_type) in out:
            rows.append(CareMatrix(contract_id=m.contract_id, program=m.program, care_type=m.care_type, present=True,
                                   source_refs=list(refs)))
    return rows, len(bad)


# ------------------------------------------------------------------ обработка договора клиента

def _readiness(clauses: int, programs: int, clinics: int, care: int) -> str:
    """ADR 0011 п. 3: ready — пункты, программы, перечень и матрица; limited — в поиске без перечня или матрицы;
    failed — нет разобранных пунктов."""
    if clauses <= 0:
        return "failed"
    return "ready" if programs and clinics and care else "limited"


def _adapter_name(insurer: Insurer) -> str:
    return insurer.value if insurer is not Insurer.unknown else "generic"


def process(contract_id: str, *, store: Any, registry: Registry, on_stage: Callable[[str], None] | None = None,
            root: Path | None = None, run_selfcheck: bool = True,
            files: list[ClientFile] | None = None) -> OnboardingReport:
    """Обработка договора клиента: опись → адаптер → store.save → статусы ТЗ 7.1 → invalidate → selfcheck → отчёт.

    insurer_hint не подменяет определение СК по документам: расхождение — предупреждение. Сбой разбора или нет
    пунктов — readiness="failed", прежняя версия договора в базе (если была) не трогается. Нет файлов договора —
    OnboardingError("no_files"). root — папка клиентов (по умолчанию client_root()); run_selfcheck=False — для
    reparse_all после импорта комплекта (самопроверка — индикатор, её не повторяем на каждый импорт).
    files — явный набор файлов (файлы договора и общие файлы группы; по умолчанию — текущие из реестра): сервис
    передаёт набор на момент запуска, reparse_all — снимок последней обработки. Успех (не failed) записывает
    снимок в реестр (registry.save_processed)."""
    def stage(name: str) -> None:
        if on_stage is not None:
            on_stage(name)

    root = root or client_root()
    stage("received")
    cc = registry.client_contract(contract_id)
    group = registry.client_group(cc.group_id)
    if files is None:
        files = registry.client_files(cc.id) + registry.client_files(group.id)
    shared = [f for f in files if f.scope == "group"]
    kc = kit_contract(group, cc, files)
    kg = kit_group(group, shared)
    if not kc.files:
        raise OnboardingError("no_files", "Загрузите хотя бы один файл договора.")

    parsed, entry, bad = parse_client(kc, root, force_generic=cc.force_generic)
    stage("extracted")
    report = OnboardingReport(contract_id=cc.id, insurer=Insurer.unknown, adapter="generic", readiness="failed")
    if parsed is None:
        if bad:
            names = ", ".join(f"«{Path(p).name}»" for p in bad)
            report.warnings.append(f"Не читаются файлы: {names}. Замените их (лучше DOCX) и запустите обработку "
                                   "снова." + ("" if len(bad) == len(kc.files) else
                                               " Без них договор разобрать не удалось."))
        report.warnings.append("Документы не удалось разобрать: проверьте, что файлы открываются, и загрузите "
                               "их снова." if entry.get("status") == "error" else
                               "Для документов нет подходящего разборщика.")
        report.documents = _bad_views(bad, kc) + [
            {"doc_id": None, "title": Path(f.path).stem, "doc_type": f.doc_type, "status": DocStatus.error.value,
             "scope": f.scope, "warnings": []} for f in kc.files if f.path not in bad]
        log.warning("onboarding %s: разбор не удался (%s, битых файлов %d)", cc.id, entry.get("status"), len(bad))
        return report
    c = parsed.contract
    titles = {d.id: d.title for d in parsed.documents}
    numbers = {cl.id: cl.number for cl in parsed.clauses}
    report.insurer, report.adapter = c.insurer, _adapter_name(c.insurer)
    if cc.insurer_hint is not None and cc.insurer_hint is not Insurer.unknown and cc.insurer_hint != c.insurer:
        report.warnings.append(f"Страховая по документам — «{report.adapter}», указана «{cc.insurer_hint.value}»: "
                               "используется определённая по документам.")
    if not c.number and cc.number:
        c.number = cc.number                 # номер из карточки клиента — только если в документах его нет
    mapped, map_warnings, used_profiles, mapped_matrices = 0, [], [], 0
    if _mapping_api(registry):
        if c.insurer is Insurer.unknown:
            # разметка таблиц оператором — только для разбора запасным адаптером (ADR 0012 п. 4)
            mapped, map_warnings, used_profiles, mapped_matrices = apply_mappings(cc, kc, parsed, root, registry)
        elif registry.table_mappings(cc.id):     # type: ignore[attr-defined]
            from .table_mapping import warn_text

            map_warnings = [warn_text("not_generic")]
    counts = {"clauses": len(parsed.clauses), "numbered": sum(1 for x in parsed.clauses if x.number),
              "programs": len(c.programs), "clinics": len(parsed.clinics), "care_rows": len(parsed.care),
              "mapped_tables": mapped, "mapped_matrices": mapped_matrices}
    report.counts = counts
    report.readiness = _readiness(counts["clauses"], counts["programs"], counts["clinics"], counts["care_rows"])  # type: ignore[assignment]
    if report.readiness == "failed":
        report.warnings.append("В документах не найдено ни одного пункта: договор в поиск не подключён.")
        # предупреждения разборщика (формат не распознан, PDF без текста…) — чтобы было видно, почему пусто
        report.warnings += [humanize(w, titles, numbers) for w in parsed.warnings]
        report.documents = _bad_views(bad, kc) + [
            _doc_view(d, "contract", titles, numbers) | {
                "status": d.status.value if d.status is DocStatus.error else DocStatus.extracted.value}
            for d in parsed.documents]
        log.warning("onboarding %s: нет пунктов", cc.id)
        return report

    greport = ingest_group_docs(kg, root, store)       # имя группы = имя клиента (Group ugN в KitStore.groups())
    save_contract(kc, parsed, store, entry)
    stage("checked")
    invalidate(cc.id)
    from .index.search import get_index

    get_index(store, cc.id)
    stage("indexed")
    group_docs = store.group_documents(group.id) if kg.files else []
    titles |= {d.id: d.title for d in group_docs}
    report.documents = ([_doc_view(d, "contract", titles, numbers) for d in store.documents(cc.id)] +
                        [_doc_view(d, "group", titles, numbers) for d in group_docs] + _bad_views(bad, kc))
    if greport.get("error"):
        report.warnings.append("Общие документы группы (правила, каталог) не разобраны: проверьте, что файлы "
                               "открываются.")
    report.warnings += [humanize(w, titles, numbers) for w in parsed.warnings]
    report.warnings += map_warnings
    if bad:
        report.warnings.append("Договор разобран без нечитаемых файлов: " +
                               ", ".join(f"«{Path(p).name}»" for p in bad) + ".")
    if report.readiness == "limited" and counts["clinics"] and not counts["care_rows"]:
        from .table_mapping import warn_text

        report.warnings.append(warn_text("limited_mapped", n=counts["clinics"]))
    elif report.readiness == "limited":
        report.warnings.append("Договор подключён к поиску по пунктам, но перечень клиник или матрица видов помощи "
                               "не распознаны: ответы о клиниках будут «недостаточно данных».")
    if _snapshot_api(registry):
        registry.save_processed(cc.id, list(files))      # type: ignore[attr-defined]
    if used_profiles and callable(getattr(registry, "profile_used", None)):
        for pid in used_profiles:
            registry.profile_used(pid, cc.id)              # type: ignore[attr-defined]  # +1 за договор
    if run_selfcheck:
        stage("selfcheck")
        report.selfcheck = selfcheck(cc.id, store)
    log.info("onboarding %s: %s adapter=%s clauses=%d clinics=%d mapped_tables=%d", cc.id, report.readiness,
             report.adapter, counts["clauses"], counts["clinics"], mapped)
    return report


def copy_contract(src: Any, dst: Any, contract_id: str) -> None:
    """Договор целиком из одной базы в другую (документы со статусами, пункты, клиники, матрица, предупреждения)
    и общие документы его группы, если в новой базе их нет. Нужен scripts/ingest_kit.py: база собирается заново,
    а договор клиента, файлы которого изменились после обработки, переносится в прежней версии (ревизия w8 п. 6)."""
    from .adapters.base import ParsedContract

    c = src.contract(contract_id)
    dst.save(ParsedContract(contract=c, documents=src.documents(contract_id), clauses=src.clauses(contract_id),
                            clinics=src.clinics(contract_id), care=src.care(contract_id),
                            warnings=src.contract_warnings(contract_id)))
    names = {g.id: g.name for g in src.groups()}
    if c.group_id in names and not dst.group_documents(c.group_id):
        docs = src.group_documents(c.group_id)
        # общие пункты группы публичным чтением не отдаются (условия договора их не выбирают, ADR 0005) —
        # читаем той же выборкой KitStore, что и clauses(), но по group_id
        rows = src._rows(src._CLAUSE_SQL + "WHERE c.contract_id IS NULL AND c.group_id = ? ORDER BY c.ord",
                         (c.group_id,))
        dst.save_group(Group(id=c.group_id, name=names[c.group_id]), docs, [src._clause(r) for r in rows])


def _has_contract(store: Any, contract_id: str) -> bool:
    try:
        store.contract(contract_id)
    except KeyError:
        return False
    return True


def reparse_all(store: Any, registry: Registry, *, root: Path | None = None,
                previous: Any = None) -> dict[str, Any]:
    """Договоры клиентов заново в базу (после пересборки базы импортом комплекта, ADR 0011 п. 5).

    Разбираются только файлы последней успешной обработки (снимок registry.processed_files с sha256) — загрузки,
    которые клиент ещё не обработал, импорт не подхватывает. Снимка нет (договор не обрабатывался) — договор
    пропускается («not_processed»). Файлы снимка изменились или удалены — повторить ту же версию нельзя: договор
    остаётся в прежней версии («kept»): в базе на месте (импорт в работающую базу) или переносится из previous —
    прежней базы (scripts/ingest_kit.py собирает новую). Реестр без снимков — прежнее поведение: текущие файлы.
    {contract_id: readiness | kept | not_processed | no_files | "error: <тип>"}; сбой одного договора остальные
    не останавливает."""
    root = root or client_root()
    snapshots = _snapshot_api(registry)
    out: dict[str, Any] = {}
    for g in registry.client_groups():
        for cc in g.contracts:
            try:
                files = registry.processed_files(cc.id) if snapshots else None  # type: ignore[attr-defined]
                if snapshots and files is None:
                    out[cc.id] = "not_processed"
                    continue
                if files is not None and not _files_on_disk(g.id, cc.id, files, root):
                    if not _has_contract(store, cc.id) and previous is not None and _has_contract(previous, cc.id):
                        copy_contract(previous, store, cc.id)
                        invalidate(cc.id)
                    out[cc.id] = "kept" if _has_contract(store, cc.id) else "no_files"
                    continue
                rd = process(cc.id, store=store, registry=registry, root=root, run_selfcheck=False,
                             files=files).readiness
                if rd == "failed":
                    # разбор не удался — прежняя версия не теряется: в базе на месте или из прежней базы
                    if not _has_contract(store, cc.id) and previous is not None and _has_contract(previous, cc.id):
                        copy_contract(previous, store, cc.id)
                        invalidate(cc.id)
                    rd = "kept" if _has_contract(store, cc.id) else rd
                out[cc.id] = rd
            except OnboardingError as e:
                out[cc.id] = e.code
            except Exception as e:  # noqa: BLE001 — договор клиента со сбоем виден в отчёте импорта
                log.warning("reparse %s: ошибка %s", cc.id, type(e).__name__)
                out[cc.id] = f"error: {type(e).__name__}"
    return out
