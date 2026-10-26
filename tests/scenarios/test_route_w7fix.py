"""Маркеры и улицы маршрутизатора (этап w7-fix, config/route_markers.yaml версия 5): «сказали платить», «платить
… самим» — требование оплаты; «договору и сколько платить» — нет (граница слова); типы улиц."""
from __future__ import annotations

from dms_core.scenarios.route import refusal_marker, street_type


def test_pay_demand_markers_with_word_boundary():
    assert refusal_marker("В клинике сказали платить за УЗИ самим. Это входит в программу?")
    assert refusal_marker("Клиника сказала платить самим за прием терапевта. Положено?")
    assert refusal_marker("Говорят, оплачивать придётся самому")
    assert refusal_marker("Когда ближайший взнос по договору и сколько платить?") is None


def test_street_types():
    for w in ("улице", "ул.", "переулке", "пер", "проспекте", "шоссе", "проезде", "тупике", "бульваре"):
        assert street_type(w), w
    for w in ("поликлинике", "перерва", "улыбка", "клинике"):
        assert not street_type(w), w
