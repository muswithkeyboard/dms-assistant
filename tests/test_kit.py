"""Опись комплекта организаторов (нужен data/raw/, иначе SKIP)."""
from __future__ import annotations

from helpers import kit_or_skip


def test_kit_has_13_contracts_in_3_groups():
    from dms_core.kit import scan

    groups = scan(kit_or_skip())
    assert [g.id for g in groups] == ["gk1", "gk2", "gk3"]
    contracts = [c for g in groups for c in g.contracts]
    assert [c.id for c in contracts] == [f"c{i:02d}" for i in range(1, 14)]
    assert {c.id: c.group_id for c in contracts}["c09"] == "gk3"


def test_every_contract_has_contract_doc_and_no_catalog():
    from dms_core.kit import scan

    for g in scan(kit_or_skip()):
        for c in g.contracts:
            types = [f.doc_type for f in c.files]
            assert types.count("contract") == 1, c.id
            assert "catalog" not in types and "rules" not in types, c.id   # они — уровня ГК


def test_group_level_documents():
    from dms_core.kit import scan

    gk1 = scan(kit_or_skip())[0]
    assert sorted(f.doc_type for f in gk1.files) == ["catalog", "rules"]
