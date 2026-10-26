"""Прогон эталонов через движок (план §10.1, бриф w2-eval). Без сети и документов — на фейковом движке в тестах.

Что делает:
  1) load_cases — читает data/derived/gold/k10_k19.jsonl и own_gk*.jsonl. holdout.jsonl — только с holdout=True
     (флаг --holdout запускает автор); без флага файл holdout не открывается даже по явному пути (правило 6).
  2) run — для каждого кейса вызывает engine.answer(question, ChatContext(...)) с фиксированной датой кейса
     (context.date; если пусто — date_fallback, одна на весь прогон), с моделью, обёрнутой счётчиком токенов
     (UsageMeter: стоимость считает dms_core.llm.pricing), параллельно (workers) и с кэшем
     data/derived/eval/<run_id>/<case>.json: кейс с тем же вопросом, контекстом, моделью и датой не перезапускается.
  3) metrics.evaluate_case / summarize — строки и свод → summary.json рядом с кэшем.

След движка (trace). Answer не несёт отброшенных ссылок и ссылок до фильтра, поэтому, если engine.answer принимает
именованный параметр trace (dict), runner передаёт пустой словарь, а движок может заполнить:
  dropped: int, kept: int — ссылки, отброшенные / принятые citations.verify;
  draft_refs: list[str] — все ref из DraftAnswer до фильтра (ссылки на чужой договор «до фильтра», план §10.1);
  run_refs: list[str] — ref, которые вернули инструменты (причина ошибки «поиск» против «модель»);
  automation_states: list[str] — путь автомата добавления клиники.
Без trace эти метрики в summary — null, а не 0.

В журнал и summary не пишутся тексты документов и обращений: только id кейсов, метки, числа, адреса пунктов.
Кэш кейса (ответ движка) содержит цитаты — он лежит в data/derived/ (вне git, `docs/rules.md` п. 4).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import automation
from . import metrics as M

GOLD_FILES = ("k10_k19.jsonl", "own_gk*.jsonl")
HOLDOUT_FILE = "holdout.jsonl"
CACHE_VERSION = 1


class HoldoutError(RuntimeError):
    """Попытка прочитать holdout без флага --holdout (правило 6: при настройке holdout не открывается)."""


# ------------------------------------------------------------------ эталоны

def default_gold_dir() -> Path:
    from dms_core.config import derived_dir
    return derived_dir() / "gold"


def is_holdout(path: Path | str, gold_dir: Path | str | None = None) -> bool:
    """Файл — holdout (правило 6), без чтения содержимого:
    - «holdout» в любом сегменте пути — как написан и после resolve() (каталог «holdout/», «holdout.jsonl.bak»);
    - путь после resolve() (ссылки, «..», регистр не важен) совпадает с resolve(<gold_dir>/holdout.jsonl) —
      и для переданного gold_dir, и для каталога эталонов по умолчанию (символическая ссылка с другим именем
      на holdout.jsonl тоже отказ)."""
    p = Path(path)
    try:
        real = p.resolve()
    except OSError:
        real = p.absolute()
    for q in (p, real):
        if any("holdout" in part.casefold() for part in q.parts):
            return True
    dirs = [Path(gold_dir)] if gold_dir is not None else []
    dirs.append(default_gold_dir())
    for d in dirs:
        try:
            target = (d / HOLDOUT_FILE).resolve()
        except OSError:
            continue
        if str(real).casefold() == str(target).casefold():
            return True
        try:
            if real.exists() and target.exists() and os.path.samefile(real, target):
                return True
        except OSError:
            pass
    return False


def gold_files(gold_dir: Path, *, holdout: bool = False) -> list[Path]:
    """Файлы эталонов прогона. Обычный прогон — k10_k19 + own_gk*, всё с «holdout» в имени отбрасывается;
    holdout=True — ровно holdout.jsonl и ничего больше (чтобы его метрики не смешались с настроечными)."""
    if holdout:
        p = gold_dir / HOLDOUT_FILE
        return [p] if p.is_file() else []
    out: list[Path] = []
    for pattern in GOLD_FILES:
        out += sorted(p for p in gold_dir.glob(pattern) if p.is_file() and not is_holdout(p, gold_dir))
    return out


def load_cases(paths: Iterable[Path], *, holdout: bool = False, ids: Iterable[str] | None = None,
               gold_dir: Path | None = None) -> list[dict]:
    """Кейсы из JSONL. Файл holdout без holdout=True — HoldoutError ДО открытия файла. Повтор id — ошибка
    (кэш и отчёт адресуются по id)."""
    want = set(ids) if ids else None
    cases: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        path = Path(path)
        if is_holdout(path, gold_dir) and not holdout:
            raise HoldoutError(f"{path.name}: holdout читается только с флагом --holdout (правило 6)")
        if holdout and not is_holdout(path, gold_dir):
            raise HoldoutError(f"{path.name}: прогон --holdout берёт только {HOLDOUT_FILE}")
        with path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                case = json.loads(line)
                cid = case.get("id")
                if not cid:
                    raise ValueError(f"{path.name}:{n}: нет id")
                if cid in seen:
                    raise ValueError(f"{path.name}:{n}: повтор id {cid}")
                seen.add(cid)
                if want is None or cid in want:
                    case["_file"] = path.name
                    cases.append(case)
    return cases


# ------------------------------------------------------------------ модель: счётчик токенов

class UsageMeter:
    """Обёртка LLM на один кейс: считает токены всех ходов (Answer токенов не несёт). Модель общая, счётчик — свой."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self.provider = getattr(llm, "provider", "?")
        self.model = getattr(llm, "model", "?")
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self._lock = threading.Lock()

    def chat(self, messages, tools=None, **kw):
        turn = self._llm.chat(messages, tools, **kw)
        u = getattr(turn, "usage", None)
        with self._lock:
            self.calls += 1
            self.input_tokens += int(getattr(u, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(u, "output_tokens", 0) or 0)
        return turn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)

    def usage(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "calls": self.calls}


def cost_usd(model: str, usage: Mapping) -> float | None:
    """Стоимость по dms_core.llm.pricing.cost_usd (этап llm). Нет модуля или цены модели → None, не 0."""
    try:
        from dms_core.llm import pricing  # type: ignore[attr-defined]
        from dms_core.llm.base import Usage
    except ImportError:
        return None
    try:
        return pricing.cost_usd(model, Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))))
    except Exception:  # noqa: BLE001   # неизвестная модель — цены нет
        return None


# ------------------------------------------------------------------ один кейс

def _context(case: Mapping, date_fallback: dt.date | None):
    from dms_core.schemas import ChatContext

    ctx = dict(case.get("context") or {})
    d = ctx.get("date")
    ctx["date"] = dt.date.fromisoformat(d) if isinstance(d, str) and d else (d or date_fallback)
    return ChatContext(**ctx)


def _accepts(fn: Callable, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return name in sig.parameters or any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values())


def case_key(case: Mapping, meta: Mapping) -> str:
    """Ключ кэша: вопрос, контекст, модель, дата, версии промптов/движка. Меняется любое — кейс прогоняется заново."""
    payload = {"q": case.get("question"), "ctx": case.get("context"), "meta": meta, "v": CACHE_VERSION}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _safe_name(case_id: str) -> str:
    return re.sub(r"[^\w.-]+", "_", case_id)


def _error_text(e: BaseException) -> str:
    """Сообщение об ошибке для кэша и отчёта: класс + первые 200 знаков (ошибки движка по-русски, без текстов)."""
    return f"{type(e).__name__}: {str(e)[:200]}"


def run_case(case: Mapping, engine_fn: Callable, *, llm: Any = None, store: Any = None,
             date_fallback: dt.date | None = None) -> dict:
    """Один кейс → запись кэша: answer (JSON) | None, trace, ms, usage, cost_usd, error."""
    meter = UsageMeter(llm) if llm is not None else None
    kwargs: dict[str, Any] = {}
    if _accepts(engine_fn, "llm"):
        kwargs["llm"] = meter
    if _accepts(engine_fn, "store") and store is not None:
        kwargs["store"] = store
    trace: dict = {}
    if _accepts(engine_fn, "trace"):
        kwargs["trace"] = trace
    rec: dict[str, Any] = {"case_id": case["id"], "answer": None, "trace": trace, "error": None}
    t0 = time.perf_counter()
    try:
        ans = engine_fn(case["question"], _context(case, date_fallback), **kwargs)
        rec["answer"] = ans.model_dump(mode="json") if hasattr(ans, "model_dump") else ans
    except Exception as e:  # noqa: BLE001   # сбой кейса не роняет прогон: он попадёт в отчёт с причиной «сбой»
        rec["error"] = _error_text(e)
    rec["ms"] = int((time.perf_counter() - t0) * 1000)
    rec["usage"] = meter.usage() if meter else {}
    rec["cost_usd"] = cost_usd(meter.model, rec["usage"]) if meter and meter.calls else None
    return rec


# ------------------------------------------------------------------ прогон

def _prompt_versions() -> dict[str, str]:
    try:
        from dms_core.config import CONFIG_DIR, prompt_version
    except ImportError:
        return {}
    return {p.stem: prompt_version(p.stem) for p in sorted((CONFIG_DIR / "prompts").glob("*.md"))}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def default_run_id(provider: str | None, model: str | None, *, holdout: bool, today: dt.date | None = None) -> str:
    rid = f"{(today or dt.date.today()).isoformat()}-{provider or 'none'}-{model or 'none'}"
    rid = re.sub(r"[^\w.-]+", "_", rid)
    return f"holdout-{rid}" if holdout else rid


def run(cases: list[dict], engine_fn: Callable, out_dir: Path, *, llm: Any = None, store: Any = None,
        workers: int = 1, resume: bool = True, date_fallback: dt.date | None = None, holdout: bool = False,
        run_meta: Mapping | None = None, gold_paths: Iterable[Path] = (), judge: Callable | None = None,
        log: Callable[[str], None] | None = None) -> dict:
    """Прогон кейсов, кэш в out_dir/<case>.json, итог out_dir/summary.json. Возвращает summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"provider": getattr(llm, "provider", None), "model": getattr(llm, "model", None),
            "date_fallback": date_fallback.isoformat() if date_fallback else None,
            "prompts": _prompt_versions(), "engine_version": _engine_version()}
    log = log or (lambda _m: None)

    def one(case: dict) -> dict:
        key = case_key(case, meta)
        path = out_dir / f"{_safe_name(case['id'])}.json"
        if resume and path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("key") == key and not cached.get("error"):
                    log(f"{case['id']}: из кэша")
                    return cached
            except (OSError, json.JSONDecodeError):
                pass
        rec = run_case(case, engine_fn, llm=llm, store=store, date_fallback=date_fallback)
        rec["key"] = key
        if judge is not None and rec["answer"] is not None and (case.get("expected") or {}).get("key_facts"):
            try:
                rec["judge"] = judge(case["expected"]["key_facts"], M.answer_text(rec["answer"]))
            except Exception as e:  # noqa: BLE001
                rec["judge"] = {"error": _error_text(e)}
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"{case['id']}: {'сбой' if rec['error'] else 'ок'} за {rec['ms']} мс")
        return rec

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            records = list(ex.map(one, cases))
    else:
        records = [one(c) for c in cases]

    group_contracts = _group_contracts(store)
    resolver = store_ref_resolver(store) if store is not None else None
    meta_of = store_clause_meta(store) if store is not None else None
    rows = []
    for case, rec in zip(cases, records, strict=True):
        allowed = M.case_contracts(case)
        gid = (case.get("context") or {}).get("group_id")
        if not (case.get("context") or {}).get("contract_id") and gid in group_contracts:
            allowed = group_contracts[gid]   # контекст ГК: допустим любой договор этой ГК, но не чужой ГК
        rows.append(M.evaluate_case(case, rec, allowed_contracts=allowed, automation_check=automation.check,
                                    ref_resolver=resolver, clause_meta=meta_of))

    summary = {
        "format": "dms-eval-summary/1",
        "run_id": out_dir.name,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "holdout": holdout,
        **meta, **dict(run_meta or {}),
        "gold_files": [{"name": p.name, "sha256_16": _sha(p)} for p in gold_paths],
        "cases": len(cases),
        "metrics": M.summarize(rows),
    }
    if not holdout:
        # По holdout — только свод: построчные ожидания/итоги holdout не должны попадать туда, где их читают
        # при настройке промптов (правило 6).
        summary["rows"] = rows
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def _engine_version() -> str | None:
    try:
        from dms_core.constants import ENGINE_VERSION
    except ImportError:
        return None
    return ENGINE_VERSION


def _group_contracts(store: Any) -> dict[str, list[str]]:
    if store is None:
        return {}
    try:
        out: dict[str, list[str]] = {}
        for c in store.contracts():
            out.setdefault(c.group_id, []).append(c.id)
        return out
    except Exception:  # noqa: BLE001
        return {}


def store_ref_resolver(store: Any) -> Callable[[Mapping], set[str] | None]:
    """id блоков базы для обязательной ссылки эталона — чтобы отличить «разбор» (блока нет) от «поиск» / «модель».
    Адрес (clause_id / table_ref с «#») — проверяется наличием id (или его версии «…#v1») среди пунктов, строк
    клиник и source_refs матрицы видов помощи договора; номер пункта — store.clause(номер) с тем же типом документа. Иначе — None (не проверить)."""
    cache: dict[str, set[str]] = {}
    lock = threading.Lock()

    def ids_of(cid: str) -> set[str]:
        with lock:
            if cid not in cache:
                try:
                    ids = {c.id for c in store.clauses(cid)}
                    for r in store.clinics(cid):
                        ids.add(r.id)
                        ids.add(f"{r.doc_id}#{r.source_ref}")
                    for m in store.care(cid):          # строки-заголовки видов помощи (Ренессанс)
                        ids.update(m.source_refs)
                except Exception:  # noqa: BLE001
                    ids = set()
                cache[cid] = ids
            return cache[cid]

    def resolve(g: Mapping) -> set[str] | None:
        cid = g.get("contract_id")
        if not cid:
            return None
        for key in ("clause_id", "table_ref"):
            ref = g.get(key)
            if ref and "#" in ref:
                hit = {i for i in ids_of(cid) if M._id_related(ref, i)}
                if hit:
                    return hit
        if g.get("clause"):
            try:
                found = store.clause(cid, number=str(g["clause"]))
            except Exception:  # noqa: BLE001
                return None
            dt_ = g.get("doc_type")
            return {c.id for c in found if not dt_ or M.doc_type_of(c.doc_id) == dt_}
        has_addr = any(g.get(k) and "#" in str(g.get(k)) for k in ("clause_id", "table_ref"))
        return set() if has_addr else None

    return resolve


def store_clause_meta(store: Any) -> Callable[[Mapping], dict | None]:
    """Путь и программа пункта ссылки ответа по базе (в Citation их нет): пункт документа doc_id с номером
    clause_number, в тексте которого есть показанный фрагмент quote (после verify это текст документа). Без
    однозначного пункта — None (эталон с путём/программой тогда не засчитывается)."""
    lock = threading.Lock()
    cache: dict[str, list] = {}

    def clauses_of(cid: str) -> list:
        with lock:
            if cid not in cache:
                try:
                    cache[cid] = list(store.clauses(cid))
                except Exception:  # noqa: BLE001
                    cache[cid] = []
            return cache[cid]

    def meta(c: Mapping) -> dict | None:
        doc_id, number = c.get("doc_id") or "", c.get("clause_number")
        cid = c.get("contract_id") or M.ref_contract(doc_id)
        if not cid or not doc_id:
            return None
        quote = M._norm_text((c.get("quote") or "").rstrip("…"))
        found = [x for x in clauses_of(cid) if x.doc_id == doc_id and (not number or x.number == number)
                 and (not quote or quote in M._norm_text(x.text))]
        if not found or len({(x.path, x.program) for x in found}) > 1:
            return None
        return {"path": found[0].path, "program": found[0].program}

    return meta
