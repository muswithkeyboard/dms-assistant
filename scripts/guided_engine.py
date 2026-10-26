"""Движок для прогонов eval в режиме guided (ADR 0009), пока engine.py заморожен.

Копия порядка engine.answer с выбором режима (guided.engine_mode, DMS_ENGINE_MODE; здесь по умолчанию — guided)
и вызовом run_guided вместо run_agent. Предлагаемая правка engine.py — та же логика (docs/status/stage-w5-guided.md).

  uv run python scripts/eval.py --engine scripts.guided_engine:answer --out <своя папка> --run-id guided-norules
Печать — только id кейсов и числа (scripts/eval.py). Пиковая память процесса — guided_engine.peak_rss_mb().
"""
from __future__ import annotations

import contextlib
import os
import resource
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dms_core import engine as E
from dms_core.constants import Confidence, Label, Scenario
from dms_core.schemas import Answer, ChatContext, LlmInfo


def peak_rss_mb() -> float:
    """Пиковая память процесса (ru_maxrss: байты на macOS, килобайты на Linux)."""
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024


def _one_contract(llm: Any, cid: str, question: str, route: Any, context: ChatContext, store: Any,
                  mode: str) -> dict:
    from dms_core.agent import run_agent
    from dms_core.citations import verify
    from dms_core.guided import run_guided
    from dms_core.scenarios.sc01_refusal import decide_label

    program = E.program_for(cid, context.program or route.slots.get("program"), store)
    slots = {k: v for k, v in route.slots.items() if k not in ("program", "contract")}
    if program:
        slots["program"] = program
    ctx = context.model_copy(update={"contract_id": cid, "program": program})
    if mode == "guided":
        run = run_guided(cid, question, scenario=route.scenario, slots=slots, context=ctx, store=store, llm=llm)
    else:
        run = run_agent(llm, cid, question, scenario=route.scenario, slots=slots, context=ctx, store=store)
    verified = verify(run.draft, run.refs, store, cid, program=program)
    label = note = rule = None
    if route.scenario == Scenario.sc01 and verified.draft.checks is not None:
        if not verified.citations:
            label, note, rule = Label.insufficient, None, "no_evidence"
        else:
            label, note, rule = decide_label(verified.draft.checks, has_alternative=bool(verified.draft.alternatives))
    return {"contract_id": cid, "run": run, "verified": verified, "label": label, "label_note": note, "rule": rule}


def answer(question: str, context: ChatContext, *, llm: Any = None, store: Any = None,
           trace: dict | None = None, on_stage: Callable[[str], None] | None = None,
           classifier: Callable[[str], Any] | None = None) -> Answer:
    """classifier — необязательный выбор сценария (точка расширения guided.choose_scenario)."""
    def stage(name: str) -> None:
        if on_stage is not None:
            with contextlib.suppress(Exception):
                on_stage(name)

    from dms_core.guided import choose_scenario, engine_mode, fill_slots

    os.environ.setdefault("DMS_ENGINE_MODE", "guided")
    t0 = time.perf_counter()
    stage("route")
    store = E._store(store)
    from dms_core.scenarios.clarify import missing_facts
    from dms_core.scenarios.route import route as do_route

    mode = engine_mode(llm)
    # guided: маршрут правилами; модель — только один короткий вызов на недостающие слоты (ADR 0009)
    if mode == "guided":
        # guided: сценарий и слоты — правила (точка расширения classifier); модель на слоты — только флагом
        r = choose_scenario(question, context, store=store, classifier=classifier)
        if llm is not None:
            r.slots = fill_slots(question, r.scenario, r.slots, context, llm, store=store)
    else:
        r = do_route(question, context, llm=llm, store=store)
    narrowed = E.resolve_contract_slot(r.slots.get("contract"), context, store)
    if context.group_id and not context.contract_id and not narrowed:
        r.slots.pop("contract", None)
    if narrowed:
        context = context.model_copy(update={"contract_id": narrowed})
    stage("clarify")
    if trace is not None:
        trace["scenario"] = r.scenario.value
        trace["slots"] = dict(r.slots)
        trace["engine_mode"] = mode
    questions = missing_facts(r.scenario, r.slots, context, store=store)
    if mode == "extractive":
        out = E.extractive(question, context, r.scenario, store, slots=r.slots)
        out.questions = questions
        out.processing_ms = int(1000 * (time.perf_counter() - t0))
        return out
    if questions:
        return Answer(scenario=r.scenario, context=E._answer_context(context), summary="Нужно уточнение.",
                      confidence=Confidence.need_clarification, questions=questions, mode=E.AnswerMode.live,
                      processing_ms=int(1000 * (time.perf_counter() - t0)))
    cids = E.contracts_of(context, store)
    with ThreadPoolExecutor(max_workers=min(E.MAX_PARALLEL_CONTRACTS, len(cids))) as pool:
        def run_one(c: str) -> dict:
            stage(f"contract:{c}")
            res = _one_contract(llm, c, question, r, context, store, mode)
            stage(f"verify:{c}")
            return res

        results = list(pool.map(run_one, cids))

    from dms_core.compose import compose
    from dms_core.config import prompt_version

    label = note = None
    if len(results) == 1:
        label, note = results[0]["label"], results[0]["label_note"]
    stage("compose")
    out = compose(r.scenario, E._answer_context(context), {x["contract_id"]: x["verified"] for x in results},
                  question=question, store=store, suggested_automation=E.suggest_automation(r.scenario, r.slots),
                  label=label, label_note=note, labels={x["contract_id"]: x["label"] for x in results},
                  label_notes={x["contract_id"]: x["label_note"] for x in results})
    if trace is not None:
        E._fill_trace(trace, results)
        trace["llm_tokens"] = {"input": sum(x["run"].usage.input_tokens for x in results),
                               "output": sum(x["run"].usage.output_tokens for x in results),
                               "calls": sum(x["run"].steps for x in results)}
        trace["peak_rss_mb"] = round(peak_rss_mb(), 1)
    calls = sum(len(x["run"].tools) for x in results)
    pv = results[0]["run"].prompt_version if mode == "guided" and results else prompt_version("agent_system")
    out.llm = LlmInfo(provider=getattr(llm, "provider", "none") if llm is not None else "none",
                      model=getattr(llm, "model", "?") if llm is not None else mode,
                      prompt_version=pv, tool_calls=calls)
    out.mode = E.AnswerMode.live
    out.processing_ms = int(1000 * (time.perf_counter() - t0))
    return out
