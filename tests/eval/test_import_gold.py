"""scripts/import_gold.py: разбор меток и дат, нормализация цитат, проверка ссылок разметки (на подставном «комплекте»)
и полный прогон проверки на настоящем комплекте (нет data/raw или разметки — SKIP)."""
from __future__ import annotations

import importlib.util

from helpers import ROOT, kit_or_skip, skip


def _mod():
    spec = importlib.util.spec_from_file_location("import_gold", ROOT / "scripts" / "import_gold.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_parse_label_with_note():
    m = _mod()
    assert m.parse_label("Можно") == ("allowed", None)
    assert m.parse_label("Нельзя (есть альтернатива)") == ("denied", "есть альтернатива")
    assert m.parse_label("Можно при условиях (нужно подтверждение СК)") == ("conditional", "нужно подтверждение СК")
    assert m.parse_label("Можно (списание правомерно)") == ("allowed", "списание правомерно")
    assert m.parse_label("Недостаточно данных") == ("insufficient", None)
    assert m.parse_label("—") == (None, None) and m.parse_label(None) == (None, None)
    try:
        m.parse_label("Наверное")
    except ValueError:
        pass
    else:
        raise AssertionError("неизвестная метка должна давать ошибку")


def test_parse_date_and_norm():
    m = _mod()
    assert m.parse_date("(HR клиента, 19.10.2026)") == "2026-10-19"
    assert m.parse_date("(HR клиента, октябрь 2026)") is None
    assert m.norm("Помощь на дому[^16] в пределах  30 км") == m.norm("помощь на дому в пределах 30 км")
    assert m.norm("60–64 лет – коэффициент") == m.norm("60-64 лет - коэффициент")
    assert m.norm("«Мед.Ком»") == m.norm('"Мед.Ком"')


class _FakeKit:
    """Подставной комплект: блоки задаются словарём (договор, тип, адрес) → (текст, автономер)."""

    def __init__(self, blocks):
        self.blocks = blocks

    def block(self, contract_id, doc_type, block):
        key = (contract_id, doc_type, block)
        if key not in self.blocks:
            raise KeyError(f"{contract_id}.{doc_type}: нет блока {block}")
        text, number = self.blocks[key]
        return text, number, f"{contract_id}.{doc_type}#{block}"


def test_verify_citation_catches_wrong_number_missing_quote_and_block():
    m = _mod()
    kit = _FakeKit({
        ("c01", "contract", "p40"): ("Сумма второго платежа уточняется Дополнительным соглашением", "2.6.1."),
        ("c09", "program", "p290"): ("при необоснованном отказе в оказании услуг", "-"),
        ("c05", "program", "t4r0"): ("… 1.3. Пломба. 1.4. Снятие твердых зубных отложений однократно", None),
    })
    ok = {"contract_id": "c01", "doc_type": "contract", "block": "p40", "clause": "2.6.1",
          "quote_hint": "уточняется Дополнительным соглашением"}
    assert m.verify_citation(kit, ok) == ("c01.contract#p40", [])
    _, probs = m.verify_citation(kit, dict(ok, clause="2.6"))
    assert any("номер абзаца" in p for p in probs)
    _, probs = m.verify_citation(kit, dict(ok, quote_hint="уточняется протоколом"))
    assert probs == ["quote_hint не найден в блоке"]
    _, probs = m.verify_citation(kit, dict(ok, also=["15 дней"]))
    assert probs == ["also[0] не найден в блоке"]
    ref, probs = m.verify_citation(kit, dict(ok, block="p41"))
    assert ref is None and probs and probs[0].startswith("блок не найден")
    # маркер списка «-» — не номер пункта: ссылка на 6.7 по абзацу-перечислению допустима
    bullet = {"contract_id": "c09", "doc_type": "program", "block": "p290", "clause": "6.7",
              "quote_hint": "необоснованном отказе"}
    assert m.verify_citation(kit, bullet)[1] == []
    # программа Ренессанса: номер «1.4.» обязан стоять прямо перед цитатой
    ren = {"contract_id": "c05", "doc_type": "program", "block": "t4r0", "clause": "1.4", "path": "… › 1.4",
           "quote_hint": "Снятие твердых зубных отложений"}
    assert m.verify_citation(kit, ren)[1] == []
    assert m.verify_citation(kit, dict(ren, clause="1.5"))[1] == ["перед цитатой нет номера '1.5'"]


def test_all_manual_citations_verified_on_kit():
    """Каждая ссылка ручной разметки находится в документе комплекта (номер + цитата)."""
    import yaml

    from dms_core.config import derived_dir, gold_xlsx

    kit = kit_or_skip()
    manual = derived_dir() / "gold" / "k10_k19_manual.yaml"
    if not manual.exists() or not gold_xlsx().exists():
        skip("нет ручной разметки или xlsx кейсов (вне git)")
    m = _mod()
    records, acc, errors = m.build(m.read_cases(gold_xlsx()), yaml.safe_load(manual.read_text(encoding="utf-8")),
                                   m.Kit(kit))
    assert errors == []
    assert [r["id"] for r in records] == [f"K{i}" for i in range(10, 20)]
    assert all(i["verified"] for items in acc["cases"].values() for i in items)
