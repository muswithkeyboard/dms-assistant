"""ВРЕМЕННЫЙ клиент OpenAI-совместимого API для ручной проверки агента на Ollama (этап w2-agent).

Не часть продукта: настоящий провайдер — src/dms_core/llm/openai_compat.py (этап w2-llm). Здесь — минимальная
реализация протокола LLM из llm/base.py на SDK `openai`. Тесты его не импортируют (сеть).

Ручной прогон (Ollama qwen3:8b на 127.0.0.1:11434):
    uv run python tests/agent/ollama_client.py K11 K12
Печатает: вызванные инструменты, шаги, валидность DraftAnswer, проверки и предупреждения кода — без текстов
документов (`docs/rules.md` п. 4).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dms_core.llm.base import LlmError, LlmTurn, Message, ToolCall, ToolSpec, Usage


class OpenAICompatTemp:
    provider = "openai_compat_temp"

    def __init__(self, model: str = "qwen3:8b", base_url: str = "http://127.0.0.1:11434/v1",
                 api_key: str = "ollama", timeout: float = 600.0) -> None:
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            elif m.role == "assistant" and m.tool_calls:
                out.append({"role": "assistant", "content": m.content or None, "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}}
                    for c in m.tool_calls]})
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    def chat(self, messages: list[Message], tools: list[ToolSpec] | None = None, *,
             json_schema: dict[str, Any] | None = None, max_tokens: int = 4096) -> LlmTurn:
        kwargs: dict[str, Any] = {"model": self.model, "messages": self._messages(messages), "temperature": 0,
                                  "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                  "parameters": t.parameters}} for t in tools]
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": "answer", "schema": json_schema}}
        try:
            r = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LlmError(f"сбой OpenAI-совместимого API: {type(exc).__name__}") from exc
        ch = r.choices[0]
        calls = []
        for i, c in enumerate(ch.message.tool_calls or []):
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": c.function.arguments}
            calls.append(ToolCall(id=c.id or f"call_{i}", name=c.function.name, args=args))
        u = r.usage
        return LlmTurn(text=ch.message.content or "", tool_calls=calls,
                       usage=Usage(u.prompt_tokens if u else 0, u.completion_tokens if u else 0),
                       model=r.model or self.model, stop_reason=ch.finish_reason or "")


def _manual(case_ids: list[str]) -> None:
    import datetime as dt

    from dms_core.agent import run_agent
    from dms_core.config import derived_dir
    from dms_core.scenarios.route import route
    from dms_core.schemas import ChatContext

    cases = {}
    for line in (derived_dir() / "gold" / "k10_k19.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        cases[d["id"]] = d
    llm = OpenAICompatTemp()
    for cid in case_ids:
        case = cases[cid]
        ctx = case["context"]
        context = ChatContext(group_id=ctx.get("group_id"), contract_id=ctx.get("contract_id"),
                              program=ctx.get("program"),
                              date=dt.date.fromisoformat(ctx["date"]) if ctx.get("date") else None)
        t0 = time.time()
        r = route(case["question"], context, llm)
        t1 = time.time()
        run = run_agent(llm, context.contract_id, case["question"], scenario=r.scenario, slots=r.slots,
                        context=context)
        t2 = time.time()
        d = run.draft
        print(json.dumps({
            "case": cid, "route": {"scenario": r.scenario.value, "source": r.source, "slots": sorted(r.slots),
                                   "questions": len(r.questions), "sec": round(t1 - t0, 1)},
            "agent": {"steps": run.steps, "sec": round(t2 - t1, 1), "error": run.error,
                      "tools": [f"{t.name}({','.join(sorted(t.args))}){'!' if t.error else ''}:{len(t.refs)}"
                                for t in run.tools],
                      "draft_valid": run.error is None, "checks": d.checks.model_dump() if d.checks else None,
                      "statements": len(d.statements),
                      "citations": sum(len(s.citations) for s in d.statements),
                      "citations_in_run_refs": sum(c.ref in run.refs for s in d.statements for c in s.citations),
                      "alternatives": len(d.alternatives), "looked_at": len(d.looked_at),
                      "questions": len(d.questions), "warnings": run.warnings,
                      "tokens": [run.usage.input_tokens, run.usage.output_tokens]},
        }, ensure_ascii=False))
        dump = os.environ.get("DMS_AGENT_DUMP")   # полный черновик — только во временную папку (не в репозиторий)
        if dump:
            Path(dump, f"{cid}.json").write_text(json.dumps({"route": r.slots, "draft": d.model_dump(mode="json"),
                                                             "tools": [[t.name, t.args] for t in run.tools]},
                                                            ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    _manual(sys.argv[1:] or ["K11", "K12"])
