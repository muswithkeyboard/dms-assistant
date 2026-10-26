"""Подключение клиента (ADR 0011, CONTRACTS §9) на своих маленьких docx: полный цикл, готовность ready / limited /
failed, сбой без порчи базы, повторная обработка, сброс кэшей перечня, импорт комплекта сохраняет клиента."""
from __future__ import annotations

import importlib.util
import json

from dms_core import adapters, onboarding
from dms_core.constants import SELFCHECK_MAX, Insurer
from dms_core.schemas import ChatContext
from fixtures import client_kit as ck
from fixtures import ren_fixture
from helpers import ROOT, patched


def _client(tmp, name="Клиент Р", legal="ООО Ромашка", **kw):
    reg = ck.registry(tmp)
    g = reg.create_client_group(name)
    cc = reg.add_client_contract(g.id, legal, **kw)
    return reg, g, cc


def _ren_client(tmp, **kw):
    reg, g, cc = _client(tmp, **kw)
    files = ck.ren_files(tmp)
    for f in files:
        ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, f)
    return reg, g, cc, files


def test_ids_do_not_overlap_kit():
    tmp = ck.workdir()
    reg = ck.registry(tmp)
    g1, g2 = reg.create_client_group("А"), reg.create_client_group("Б")
    a = reg.add_client_contract(g1.id, "Юрлицо 1")
    b = reg.add_client_contract(g2.id, "Юрлицо 2")
    assert (g1.id, g2.id, a.id, b.id) == ("ug1", "ug2", "u01", "u02")
    assert [c.id for c in reg.client_group("ug1").contracts] == ["u01"]


def test_kit_contract_paths_and_group_files():
    tmp = ck.workdir()
    reg, g, cc, _ = _ren_client(tmp)
    rules = ck.generic_docx(tmp / "x" / "Правила страхования.docx", ["1. Общие положения"])
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, rules, scope="group", doc_type="rules")
    kc = onboarding.kit_contract(reg.client_group(g.id), cc, reg.client_files(cc.id))
    assert [f.path for f in kc.files] == ["ug1/u01/Договор № 99.docx", "ug1/u01/Приложение 1. Программа ДМС.docx"]
    assert [f.doc_type for f in kc.files] == ["contract", "program"] and kc.legal_entity == "ООО Ромашка"
    kg = onboarding.kit_group(reg.client_group(g.id), reg.client_files(g.id))
    assert [f.path for f in kg.files] == ["ug1/_group/Правила страхования.docx"] and kg.name == "Клиент Р"


def test_full_cycle_ready_and_visible_as_group():
    tmp = ck.workdir()
    reg, _g, cc, _ = _ren_client(tmp)
    st = ck.kit_store(tmp)
    stages: list[str] = []
    rep = onboarding.process(cc.id, store=st, registry=reg, on_stage=stages.append, root=ck.clients_root(tmp))
    assert stages == ["received", "extracted", "checked", "indexed", "selfcheck"]
    assert rep.readiness == "ready" and rep.adapter == "renaissance" and rep.insurer is Insurer.renaissance
    assert rep.counts == {"clauses": 48, "numbered": 21, "programs": 2, "clinics": 5, "care_rows": 6,
                          "mapped_tables": 0, "mapped_matrices": 0}
    assert {d["status"] for d in rep.documents} == {"ready"}
    assert 0 < rep.selfcheck["asked"] <= SELFCHECK_MAX and rep.selfcheck["cited"] == rep.selfcheck["asked"]
    # группа клиента — в KitStore.groups() с именем клиента; договор — с юрлицом клиента и страховой по документам
    assert ("ug1", "Клиент Р") in [(x.id, x.name) for x in st.groups()]
    c = st.contract("u01")
    assert c.group_id == "ug1" and c.legal_entity == "ООО Ромашка" and c.insurer is Insurer.renaissance
    assert all(cl.contract_id == "u01" for cl in st.clauses("u01"))


def test_selfcheck_questions_cover_programs_and_limit():
    tmp = ck.workdir()
    reg, _g, cc, _ = _ren_client(tmp)
    st = ck.kit_store(tmp)
    onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    qs = onboarding.selfcheck_questions("u01", st)
    assert {p for p, _ in qs} == {"Альфа", "Бета"}
    assert any("в клинике «Клиника А" in q for _, q in qs) and any(q.startswith("Входит ли") for _, q in qs)
    with patched(onboarding, "SELFCHECK_MAX", 3):
        assert len(onboarding.selfcheck_questions("u01", st)) == 3
        calls = []
        out = onboarding.selfcheck("u01", st, answer_fn=lambda q, ctx, **kw: calls.append(ctx) or _raise())
        assert out == {"asked": 3, "cited": 0} and all(x.contract_id == "u01" for x in calls)


def _raise():
    raise RuntimeError("сбой ответа")


def test_unknown_insurer_is_generic_limited_and_hint_mismatch_warns():
    tmp = ck.workdir()
    reg, g, cc = _client(tmp, insurer_hint=Insurer.sogaz)
    doc = ck.generic_docx(tmp / "src" / "Договор 7.docx", ["ДОГОВОР № 7 страхования", "1. Предмет договора",
                                                            "Страховщик оплачивает медицинские услуги."])
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, doc)
    st = ck.kit_store(tmp)
    rep = onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    assert rep.readiness == "limited" and rep.adapter == "generic" and rep.insurer is Insurer.unknown
    assert rep.counts["clauses"] == 3 and rep.counts["clinics"] == 0
    assert any("указана «sogaz»" in w for w in rep.warnings)
    assert st.contract(cc.id).insurer is Insurer.unknown        # подсказка клиента не подменяет определение


def test_empty_contract_failed_and_no_files_error():
    tmp = ck.workdir()
    reg, g, cc = _client(tmp)
    st = ck.kit_store(tmp)
    try:
        onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp))
        raise AssertionError("нет файлов — ошибка no_files")
    except onboarding.OnboardingError as e:
        assert e.code == "no_files"
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, ck.generic_docx(tmp / "src" / "Договор.docx", []))
    rep = onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp))
    assert rep.readiness == "failed" and rep.counts["clauses"] == 0
    assert cc.id not in {c.id for c in st.contracts()}          # в поиск не подключён


def test_crash_keeps_previous_version_and_reprocess_replaces():
    tmp = ck.workdir()
    reg, _g, cc, files = _ren_client(tmp)
    st, root = ck.kit_store(tmp), ck.clients_root(tmp)
    onboarding.process(cc.id, store=st, registry=reg, root=root, run_selfcheck=False)
    before = (len(st.clauses("u01")), len(st.clinics("u01")), st.revision("u01"))

    def boom(kc, r):
        raise ValueError("битый файл")
    with patched(adapters, "parse_contract", boom):
        rep = onboarding.process(cc.id, store=st, registry=reg, root=root)
    assert rep.readiness == "failed" and rep.warnings
    assert (len(st.clauses("u01")), len(st.clinics("u01")), st.revision("u01")) == before   # база цела

    # повторная обработка с другим перечнем: договор заменён целиком, строк с прежним названием нет
    ck.rename_clinic(root / "ug1" / "u01" / files[1].name, "Клиника А", "Клиника Верба")
    rep = onboarding.process(cc.id, store=st, registry=reg, root=root, run_selfcheck=False)
    names = {r.clinic_name for r in st.clinics("u01")}
    assert rep.readiness == "ready" and st.revision("u01") != before[2]
    assert any("Верба" in n for n in names) and not any("Клиника А" in n for n in names)
    assert len(st.contracts()) == 1


def test_reprocess_invalidates_clinic_caches_without_restart():
    """Кэши перечня (route._LIST_CACHE, _CLINIC_TOKEN_CACHE) — по хранилищу и договору, не по ревизии: без
    onboarding.invalidate слот «клиника» и кандидаты выбора видели бы прежний перечень до перезапуска."""
    from dms_core.scenarios import route

    tmp = ck.workdir()
    reg, _g, cc, files = _ren_client(tmp)
    st, root = ck.kit_store(tmp), ck.clients_root(tmp)
    onboarding.process(cc.id, store=st, registry=reg, root=root, run_selfcheck=False)
    for ctx in (ChatContext(contract_id="u01"), ChatContext(group_id="ug1")):
        assert any("Клиника А" in r[0] for r in route._list_index(ctx, st).rows)
        assert "верба" not in route._clinic_tokens(ctx, st)
    ck.rename_clinic(root / "ug1" / "u01" / files[1].name, "Клиника А", "Клиника Верба")
    onboarding.process(cc.id, store=st, registry=reg, root=root, run_selfcheck=False)
    for ctx in (ChatContext(contract_id="u01"), ChatContext(group_id="ug1")):
        rows = [r[0] for r in route._list_index(ctx, st).rows]
        assert any("Верба" in r for r in rows) and not any("Клиника А" in r for r in rows)
        assert "верба" in route._clinic_tokens(ctx, st)
    # явный сброс по договору не трогает кэши других договоров
    route._list_index(ChatContext(contract_id="u01"), st)
    assert route.invalidate("u99") == 0 and route.invalidate("u01") > 0


def _ingest_module():
    spec = importlib.util.spec_from_file_location("ingest_kit_w8", ROOT / "scripts" / "ingest_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_kit_import_keeps_client_contracts():
    """Пересборка базы импортом комплекта (scripts/ingest_kit.py --db) с реестром клиентов: договор клиента
    разобран заново и есть в новой базе рядом с договором комплекта (ADR 0011 п. 5)."""
    tmp = ck.workdir()
    reg, _g, _cc, _ = _ren_client(tmp)
    kit_root = tmp / "kit"
    ren_fixture.build(kit_root)                         # комплект: «ГК 9/Компания 99/…»
    db, report_path = tmp / "derived" / "kit.sqlite", tmp / "derived" / "ingest_report.json"
    # договор клиента обработан (снимок файлов в реестре) — импорт разбирает именно эту версию (ревизия w8 п. 6)
    with ck.kit_store(tmp) as st0:
        onboarding.process(_cc.id, store=st0, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    reg.close()
    mod = _ingest_module()
    import os

    old = os.environ.get("DMS_DATA_DIR")
    os.environ["DMS_DATA_DIR"] = str(tmp)               # client_root() = <tmp>/derived/clients
    try:
        rc = mod.main(["--root", str(kit_root), "--db", str(db), "--report", str(report_path),
                       "--app-db", str(tmp / "app" / "app.sqlite")])
    finally:
        if old is None:
            os.environ.pop("DMS_DATA_DIR", None)
        else:
            os.environ["DMS_DATA_DIR"] = old
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert rc == 0 and report["clients"] == {"u01": "ready"}
    st = ck.kit_store(tmp)
    assert {c.id for c in st.contracts()} == {"c99", "u01"}
    assert {x.id: x.name for x in st.groups()}["ug1"] == "Клиент Р"
    assert len(st.clauses("u01")) == len(st.clauses("c99"))
