"""eval/runner.py, eval/automation.py, eval/report.py и метрики уровня ответа — на своих маленьких кейсах
и фейковом движке (без сети, без документов организаторов)."""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

from helpers import ROOT

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dms_core.schemas import Answer, AnswerContext, ChatContext, Citation, Statement
from eval import automation as A
from eval import metrics as M
from eval import report as R
from eval import runner as RN

# ------------------------------------------------------------------ свои кейсы

def _case(cid, scenario="sc01", contract="c01", group="gk1", label="allowed", confidence="confirmed",
          facts=(), cites=(), questions=(), automation=None, date=None):
    return {"id": cid, "source": "own", "scenario": scenario, "question": f"вопрос {cid}",
            "context": {"group_id": group, "contract_id": contract, "program": None, "date": date},
            "expected": {"label": label, "label_note": None, "confidence": confidence, "key_facts": list(facts),
                         "required_citations": list(cites), "must_not": [], "questions": list(questions),
                         "automation": automation}}


def _cit(contract, doc="contract", clause=None, table_ref=None):
    return Citation(doc_id=f"{contract}.{doc}", doc_title="Документ", contract_id=contract, clause_number=clause,
                    table_ref=table_ref, quote="фрагмент", verified=True)


def _answer(ctx: ChatContext, *, label=None, confidence="confirmed", text="", cites=(), questions=(),
            alternatives=(), suggested=None) -> Answer:
    return Answer(scenario="sc01", context=AnswerContext(**ctx.model_dump()), summary=text, label=label,
                  confidence=confidence, statements=[Statement(text=text, citations=list(cites))] if cites else [],
                  questions=list(questions), suggested_automation=suggested)


def _write(dirpath: Path, name: str, cases) -> Path:
    p = dirpath / name
    p.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n", encoding="utf-8")
    return p


# ------------------------------------------------------------------ holdout

def test_holdout_never_loaded_without_flag():
    with tempfile.TemporaryDirectory() as d:
        g = Path(d)
        _write(g, "k10_k19.jsonl", [_case("K10")])
        _write(g, "own_gk1.jsonl", [_case("own-1")])
        _write(g, "holdout.jsonl", [_case("H1")])
        _write(g, "own_gk1_holdout.jsonl", [_case("H2")])     # «holdout» в имени тоже исключается
        files = RN.gold_files(g)
        assert [p.name for p in files] == ["k10_k19.jsonl", "own_gk1.jsonl"]
        assert {c["id"] for c in RN.load_cases(files)} == {"K10", "own-1"}
        # явный путь к holdout без флага — отказ до чтения файла
        try:
            RN.load_cases([g / "holdout.jsonl"])
            raise AssertionError("holdout прочитан без флага")
        except RN.HoldoutError:
            pass
        # с флагом — только holdout, настроечные файлы туда не подмешиваются
        assert [p.name for p in RN.gold_files(g, holdout=True)] == ["holdout.jsonl"]
        try:
            RN.load_cases([g / "own_gk1.jsonl"], holdout=True)
            raise AssertionError("в прогон holdout подмешан настроечный файл")
        except RN.HoldoutError:
            pass


def test_holdout_guard_by_resolved_path_and_any_segment():
    """Ссылка с другим именем на holdout.jsonl, путь через «..», каталог «holdout/» — отказ до чтения файла."""
    with tempfile.TemporaryDirectory() as d:
        g = Path(d)
        _write(g, "holdout.jsonl", [_case("H1")])
        (g / "cases.jsonl").symlink_to(g / "holdout.jsonl")
        (g / "sub").mkdir()
        (g / "Holdout").mkdir()
        _write(g / "Holdout", "own_gk1.jsonl", [_case("H3")])
        for bad in (g / "cases.jsonl", g / "sub" / ".." / "holdout.jsonl", g / "Holdout" / "own_gk1.jsonl"):
            assert RN.is_holdout(bad, g), bad
            try:
                RN.load_cases([bad], gold_dir=g)
                raise AssertionError(f"holdout прочитан без флага: {bad}")
            except RN.HoldoutError:
                pass
        assert not RN.is_holdout(_write(g, "own_gk1.jsonl", [_case("ok")]), g)


def test_duplicate_case_id_rejected():
    with tempfile.TemporaryDirectory() as d:
        p = _write(Path(d), "own_gk1.jsonl", [_case("X"), _case("X")])
        try:
            RN.load_cases([p])
            raise AssertionError("повтор id пропущен")
        except ValueError as e:
            assert "повтор id" in str(e)


def test_holdout_summary_has_no_rows_and_report_refuses():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "holdout-x"
        s = RN.run([_case("H1")], _fake_oracle({}), out, holdout=True)
        assert s["holdout"] is True and "rows" not in s
        assert "rows" not in json.loads((out / "summary.json").read_text(encoding="utf-8"))
        try:
            R.render(s)
            raise AssertionError("отчёт по holdout без флага")
        except R.ReportError:
            pass
        text = R.render(s, allow_holdout=True)
        assert "H1" not in text and "не выводится" in text


# ------------------------------------------------------------------ прогон: дата, кэш, параллельность, сбой, токены

class _Turn:
    def __init__(self):
        self.usage = type("U", (), {"input_tokens": 100, "output_tokens": 20})()
        self.text, self.tool_calls, self.model = "", [], "m"


class _FakeLLM:
    provider, model = "fake", "fake-1"

    def chat(self, messages, tools=None, **kw):
        return _Turn()


def _fake_oracle(answers: dict, calls: list | None = None):
    """Фейковый движок: ответ по id кейса из вопроса; два вызова модели на кейс; заполняет след."""
    def answer(question, context, *, llm=None, store=None, trace=None):
        cid = question.split()[-1]
        if calls is not None:
            calls.append((cid, context.date))
        if llm is not None:
            llm.chat([]), llm.chat([])
        spec = answers.get(cid, {})
        if spec.get("raise"):
            raise RuntimeError("движок упал")
        if trace is not None:
            trace.update(spec.get("trace", {}))
        return _answer(context, **{k: v for k, v in spec.items() if k != "trace"})
    return answer


def test_run_fixed_date_cache_parallel_and_usage():
    cases = [_case("A", date="2026-10-19"), _case("B"), _case("C")]
    calls: list = []
    eng = _fake_oracle({"A": {"label": "allowed"}, "C": {"raise": True}}, calls)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "r1"
        s = RN.run(cases, eng, out, llm=_FakeLLM(), workers=3, date_fallback=dt.date(2026, 7, 1))
        dates = dict(calls)
        assert dates["A"] == dt.date(2026, 10, 19) and dates["B"] == dt.date(2026, 7, 1)   # дата кейса / общая
        assert {p.name for p in out.iterdir()} == {"A.json", "B.json", "C.json", "summary.json"}
        rows = {r["id"]: r for r in s["rows"]}
        assert rows["C"]["error"].startswith("RuntimeError") and rows["C"]["causes"] == ["сбой"]
        assert rows["A"]["tokens"] == {"input_tokens": 200, "output_tokens": 40, "calls": 2}
        assert s["metrics"]["overall"]["tokens"] == {"input_tokens": 600, "output_tokens": 120}
        assert s["metrics"]["overall"]["errors"] == 1
        # повторный прогон: A и B из кэша, упавший C — заново
        calls.clear()
        RN.run(cases, eng, out, llm=_FakeLLM(), date_fallback=dt.date(2026, 7, 1))
        assert [c for c, _ in calls] == ["C"]
        # другая дата для кейсов без даты — другой ключ кэша, B перезапускается
        calls.clear()
        RN.run(cases, eng, out, llm=_FakeLLM(), date_fallback=dt.date(2026, 8, 1))
        assert sorted(c for c, _ in calls) == ["A", "B", "C"]


def test_engine_without_optional_kwargs_still_runs():
    def plain(question, context):     # интерфейс заглушки engine.answer в main
        raise NotImplementedError("Движок ответов — волна 2")
    with tempfile.TemporaryDirectory() as d:
        s = RN.run([_case("A")], plain, Path(d) / "r", llm=_FakeLLM())
        r = s["rows"][0]
        assert r["error"].startswith("NotImplementedError") and r["tokens"]["calls"] == 0


# ------------------------------------------------------------------ метрики кейса

def test_evaluate_case_all_metrics():
    case = _case("K", label="denied", facts=["Лимит 3 000 руб.", "Оплатить до 31.10.2026"],
                 cites=[{"contract_id": "c01", "doc_type": "contract", "clause": "2.8"},
                        {"contract_id": "c01", "doc_type": "contract", "clause": None,
                         "table_ref": "c01.contract#t6r4"}])
    ctx = ChatContext(contract_id="c01", group_id="gk1")
    ans = _answer(ctx, label="denied", text="Лимит 3 000 руб.; оплатить до 31.10.2026.",
                  cites=[_cit("c01", clause="2.8.1"), _cit("c01", table_ref="t6r4#v1"), _cit("c02", clause="2.8")])
    rec = {"answer": ans.model_dump(mode="json"), "ms": 1200,
           "trace": {"kept": 3, "dropped": 1, "draft_refs": ["c01.contract#p43", "c02.contract#p43", "gk1.rules#p1"]}}
    r = M.evaluate_case(case, rec)
    assert r["label"] == {"gold": "denied", "pred": "denied", "ok": True, "false_refusal": False,
                          "false_promise": False}
    assert (r["citations"]["found"], r["citations"]["required"]) == (2, 2)   # потомок пункта и версия строки
    assert r["facts"]["found"] == 2
    assert r["dropped"]["share"] == 0.25
    assert r["foreign"] == {"after": 1, "before": 1}     # документ ГК (gk1.rules) — не чужой
    assert r["causes"] == ["правило"] and not r["passed"]   # чужой договор прошёл фильтр — ошибка правила


def test_failure_causes_parse_search_model():
    cites = [{"contract_id": "c01", "doc_type": "contract", "clause": "1.1", "clause_id": "c01.contract#p1"},
             {"contract_id": "c01", "doc_type": "contract", "clause": "2.2", "clause_id": "c01.contract#p2"},
             {"contract_id": "c01", "doc_type": "contract", "clause": "3.3", "clause_id": "c01.contract#p3"}]
    case = _case("K", label="allowed", cites=cites)
    ctx = ChatContext(contract_id="c01")
    rec = {"answer": _answer(ctx, label="denied").model_dump(mode="json"),
           "trace": {"run_refs": ["c01.contract#p3"]}}
    in_base = {"c01.contract#p2", "c01.contract#p3"}
    r = M.evaluate_case(case, rec, ref_resolver=lambda g: {g["clause_id"]} & in_base)
    # p1 нет в базе → разбор; p2 есть, не вернули → поиск; p3 вернули, не процитировали → модель;
    # неверная метка при пропущенных ссылках — модель (не правило); ложный отказ
    assert r["causes"] == ["разбор", "поиск", "модель"]
    assert r["label"]["false_refusal"] is True
    # все ссылки на месте, метка неверна → правило
    full = _answer(ctx, label="denied", cites=[_cit("c01", clause=c) for c in ("1.1", "2.2", "3.3")])
    r2 = M.evaluate_case(case, {"answer": full.model_dump(mode="json")})
    assert r2["causes"] == ["правило"]


def test_clarification_expected_and_unexpected():
    case = _case("Q", label=None, confidence="need_clarification", questions=["Какая программа?"])
    ctx = ChatContext(contract_id="c01")
    asked = _answer(ctx, confidence="need_clarification", questions=["Уточните программу"])
    assert M.evaluate_case(case, {"answer": asked.model_dump(mode="json")})["clarify"]["ok"] is True
    silent = _answer(ctx, confidence="confirmed")
    r = M.evaluate_case(case, {"answer": silent.model_dump(mode="json")})
    assert r["clarify"] == {"expected": True, "asked": False, "ok": False} and "модель" in r["causes"]
    extra = M.evaluate_case(_case("N", label=None), {"answer": asked.model_dump(mode="json")})
    assert extra["clarify"]["ok"] is False                   # лишнее уточнение тоже ошибка
    assert M.evaluate_case(case, {"answer": None, "error": "x"})["clarify"]["ok"] is False


def test_percentile_nearest_rank():
    xs = list(range(1, 21))                                   # 1..20
    assert M.percentile(xs, 0.5) == 10 and M.percentile(xs, 0.95) == 19
    assert M.percentile([5], 0.95) == 5 and M.percentile([], 0.5) is None
    assert M.percentile([None, 3, 1], 0.5) == 1


def test_summarize_by_scenario_and_contract_gk_case_counts_in_each():
    gk = _case("G", scenario="sc02_info", contract=None, label=None,
               cites=[{"contract_id": "c09", "doc_type": "program", "clause": "1"},
                      {"contract_id": "c10", "doc_type": "program", "clause": "1"}])
    rows = [M.evaluate_case(_case("A"), {"answer": None, "error": "x"}),
            M.evaluate_case(gk, {"answer": _answer(ChatContext(group_id="gk3")).model_dump(mode="json"),
                                 "ms": 10})]
    s = M.summarize(rows)
    assert set(s["by_scenario"]) == {"sc01", "sc02"}
    assert set(s["by_contract"]) == {"c01", "c09", "c10"}
    assert s["by_contract"]["c09"]["cases"] == 1 and s["overall"]["cases"] == 2
    assert s["overall"]["dropped"]["share"] is None          # без следа движка — null, не 0
    assert s["overall"]["latency_ms"] == {"p50": 10, "p95": 10}   # сбой во время не входит


# ------------------------------------------------------------------ автомат

def test_automation_states_normalized_to_add_clinic_state():
    assert A.normalize_states(["not_in_program", "hr_wants_add", "insurer_free", "hr_notified_added"]) == \
        ["not_in_program", "hr_wants_add", "awaiting_insurer", "insurer_free", "closed"]
    assert A.normalize_states(["not_in_program", "hr_wants_add", "insurer_paid", "hr_approval", "insurer_confirm",
                               "clinic_added"]) == \
        ["not_in_program", "hr_wants_add", "awaiting_insurer", "insurer_paid", "hr_agreed", "closed"]
    assert A.normalize_states(["not_in_program", "hr_wants_add", "awaiting_insurer", "insurer_free"]) == \
        ["not_in_program", "hr_wants_add", "awaiting_insurer", "insurer_free"]
    try:
        A.normalize_states(["unknown_step"])
        raise AssertionError("неизвестное состояние пропущено")
    except ValueError:
        pass


def test_automation_check_initial_and_full():
    exp = {"branch": "insurer_free", "states": ["not_in_program", "hr_wants_add", "insurer_free"]}
    assert A.check(exp, {"suggested_automation": "not_in_program"}, {})["ok"] is True
    assert A.check(exp, {"alternatives": [{"x": 1}]}, {})["ok"] is False          # alt_found ≠ not_in_program
    full = A.check(exp, None, {"automation_states": ["not_in_program", "hr_wants_add", "awaiting_insurer",
                                                     "insurer_paid"]})
    assert full["mode"] == "full" and full["ok"] is False                        # ветка не та
    assert A.check(None, {}, {}) is None


def test_gold_automation_states_all_mappable():
    """Все шаги автомата в своих эталонах сводятся к AddClinicState (holdout не читается)."""
    from dms_core.config import derived_dir

    files = RN.gold_files(derived_dir() / "gold")
    if not files:
        from helpers import skip
        skip("нет data/derived/gold")
    for c in RN.load_cases(files):
        a = (c.get("expected") or {}).get("automation")
        if a and a.get("states"):
            assert A.normalize_states(a["states"]), c["id"]


# ------------------------------------------------------------------ отчёт

def test_report_has_ids_and_no_texts():
    secret_q = "СЕКРЕТНЫЙ ВОПРОС МЕНЕДЖЕРА"
    secret_fact = "Уникальный факт 987654"
    cases = [_case("A", facts=[secret_fact], cites=[{"contract_id": "c01", "doc_type": "contract",
                                                     "clause": "2.8", "quote_hint": "ЦИТАТА ИЗ ДОГОВОРА"}]),
             _case("B", label="denied")]
    cases[0]["question"] = secret_q + " A"
    eng = _fake_oracle({"A": {"label": "allowed", "text": "ТЕКСТ ОТВЕТА"}, "B": {"label": "allowed"}})
    with tempfile.TemporaryDirectory() as d:
        s = RN.run(cases, eng, Path(d) / "r")
        text = R.render(s)
    for leak in (secret_q, secret_fact, "ЦИТАТА ИЗ ДОГОВОРА", "ТЕКСТ ОТВЕТА"):
        assert leak not in text
    assert "| A |" in text and "| B |" in text and "c01 · contract · п. 2.8" in text
    assert "ложн" in text.casefold() and "SC 01" in text
    assert "нельзя → можно" in text                          # B: ложное обещание видно в строке
