"""Помощники тестов API: синтетический комплект, фейковый движок, заглушка автомата (CONTRACTS §8), клиент."""
from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from dms_api import security
from dms_api.app import create_app
from dms_api.service import Service
from dms_api.settings import Settings
from dms_api.store import AppStore
from dms_core.config import load_yaml
from dms_core.constants import AddClinicState, AnswerMode, Confidence, DraftTo, Scenario
from dms_core.schemas import Answer, AnswerContext, Automation, Draft
from dms_core.store import KitStore
from fixtures.synth_kit import make_group_docs, make_parsed


def kit_store() -> KitStore:
    st = KitStore(":memory:")
    st.save(make_parsed("c01"))
    st.save(make_parsed("c02"))
    g, docs, cls = make_group_docs()
    st.save_group(g, docs, cls)
    return st


class FakeEngine:
    """Вместо engine.answer: запоминает вызовы (вопрос, контекст, модель), отвечает фиксированным Answer."""

    def __init__(self, fail_with_llm: Exception | None = None):
        self.calls: list[tuple[str, object, object]] = []
        self.fail_with_llm = fail_with_llm

    def __call__(self, question, context, *, llm=None, store=None):
        self.calls.append((question, context, llm))
        if llm is not None and self.fail_with_llm is not None:
            raise self.fail_with_llm
        return Answer(scenario=Scenario.sc02_add_clinic,
                      context=AnswerContext(group_id=context.group_id, contract_id=context.contract_id),
                      summary="Клиники нет в перечне программы.", confidence=Confidence.confirmed,
                      mode=AnswerMode.live if llm is not None else AnswerMode.extractive)


def _draft(to: DraftTo, stage: AddClinicState, n: int) -> Draft:
    return Draft(id=f"df_{stage.value}_{n}", to=to, stage=stage, subject=f"Тема {stage.value}",
                 body="Текст черновика", created_at=dt.datetime.now(dt.UTC))


def fake_add_clinic() -> SimpleNamespace:
    """Заглушка dms_core.scenarios.add_clinic по интерфейсу CONTRACTS §8: переходы — из config/add_clinic.yaml."""
    cfg = load_yaml("add_clinic.yaml")

    def start(request, answer, *, clinic=None, store=None):
        state = AddClinicState.not_in_program
        request.drafts.append(_draft(DraftTo.hr, state, len(request.drafts)))
        return Automation(state=state, clinic=clinic, history=[{"state": state.value}])

    def step(request, action, *, insurer_reply=None, store=None):
        auto = request.automation
        nxt = cfg["transitions"].get(auto.state.value, {}).get(action)
        if nxt is None:
            raise ValueError("недопустимый переход")
        auto.state = AddClinicState(nxt)
        auto.history.append({"action": action, "state": nxt, "insurer_reply": insurer_reply})
        for _ in cfg["drafts"].get(nxt, []):
            request.drafts.append(_draft(DraftTo.insurer, auto.state, len(request.drafts)))
        return request

    return SimpleNamespace(start=start, step=step)


def make_client(tmp: Path | None = None, *, settings: Settings | None = None, engine=None, llm=None,
                gate: security.HeavyGate | None = None, **svc_kw) -> tuple[TestClient, Service, Path]:
    tmp = tmp or Path(tempfile.mkdtemp(prefix="dms-api-"))
    svc = Service(AppStore(tmp / "app" / "app.sqlite"), svc_kw.pop("kit", None) or kit_store(),
                  llm_factory=lambda: llm, answer_fn=engine, data_dir=tmp,
                  add_clinic=svc_kw.pop("add_clinic", None) or fake_add_clinic(), **svc_kw)
    app = create_app(settings or Settings(_env_file=None), service=svc, gate=gate)
    return TestClient(app), svc, tmp


def new_request(c: TestClient, context: dict | None = None) -> tuple[str, str]:
    r = c.post("/api/chats", json={"context": context or {"contract_id": "c01"}})
    assert r.status_code == 201, r.text
    chat_id = r.json()["id"]
    r = c.post(f"/api/chats/{chat_id}/requests", json={"title": "Клиника"})
    assert r.status_code == 201, r.text
    return chat_id, r.json()["id"]
