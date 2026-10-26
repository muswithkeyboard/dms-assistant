"""adapters.parse_group_docs: правила СК (PDF) → пункты без договора, каталог (xlsx) → документ без пунктов.
Синтетика без комплекта; на комплекте (ГК 1) — SKIP без data/raw/."""
from __future__ import annotations

import tempfile
from pathlib import Path

from dms_core import adapters, kit
from dms_core.adapters.group_docs import CATALOG_WARNING, RULES_WARNING, split_rules
from dms_core.constants import DocStatus, DocType
from dms_core.kit import KitFile, KitGroup
from helpers import kit_or_skip


def _kf(path: str, doc_type: str, fmt: str) -> KitFile:
    return KitFile(path=path, title=Path(path).stem, fmt=fmt, doc_type=doc_type, scope="group", size=1, sha256="0")


def test_split_rules_numbers_pages_and_continuations():
    pages = ["ПРАВИЛА СТРАХОВАНИЯ\nот несчастных случаев\n1. ОБЩИЕ ПОЛОЖЕНИЯ\n1.1.Настоящие Правила\nпродолжение строки",
             "2\nпункта на второй странице\n1.2. Второй пункт\n2023. год не номер\n2.6.1. Глубокий пункт"]
    cls = split_rules(pages, "gk9.rules", "Правила")
    assert [(c.number, c.page) for c in cls] == [(None, 1), ("1", 1), ("1.1", 1), ("1.2", 2), ("2.6.1", 2)]
    assert cls[2].text == "Настоящие Правила продолжение строки пункта на второй странице"   # колонтитул «2» убран
    assert "2023. год не номер" in cls[3].text
    assert all(c.contract_id is None and c.doc_id == "gk9.rules" for c in cls)
    assert len({c.id for c in cls}) == len(cls)
    assert cls[2].path == "Правила › 1.1"


def test_parse_group_docs_catalog_and_errors():
    import openpyxl

    root = Path(tempfile.mkdtemp(prefix="dms_gd_"))
    wb = openpyxl.Workbook()
    wb.active.append(["Регион", "ЛПУ"])
    wb.active.append(["Москва", "Клиника"])
    wb.save(root / "ЛПУ_Регионы Стандарт.xlsx")
    (root / "rules.pdf").write_bytes(b"not a pdf")
    group = KitGroup(id="gk9", name="ГК 9", files=[_kf("ЛПУ_Регионы Стандарт.xlsx", "catalog", "xlsx"),
                                                   _kf("rules.pdf", "rules", "pdf")])
    docs, cls = adapters.parse_group_docs(group, root)
    cat, rules = docs
    assert cat.id == "gk9.catalog" and cat.doc_type is DocType.catalog
    assert cat.contract_id is None and cat.group_id == "gk9"
    assert cat.status is DocStatus.extracted and CATALOG_WARNING in cat.warnings
    assert rules.status is DocStatus.error and RULES_WARNING in rules.warnings
    assert any(w.startswith("ошибка чтения") for w in rules.warnings)
    assert cls == []                                    # каталог не даёт пунктов, битый PDF — тоже


def test_parse_group_docs_on_kit_gk1():
    root = kit_or_skip()
    g = next(x for x in kit.scan(root) if x.id == "gk1")
    docs, cls = adapters.parse_group_docs(g, root)
    by_type = {d.doc_type.value: d for d in docs}
    assert set(by_type) == {"rules", "catalog"}
    assert all(d.status is DocStatus.extracted and d.contract_id is None for d in docs)
    rules = [c for c in cls if c.doc_id == by_type["rules"].id]
    assert len(rules) > 300 and sum(1 for c in rules if c.number) > 300
    assert all(c.page for c in rules)
    assert not [c for c in cls if c.doc_id == by_type["catalog"].id]
