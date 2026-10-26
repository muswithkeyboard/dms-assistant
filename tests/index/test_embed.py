"""Энкодер rubert-tiny2 в ONNX и маршрутизация kNN (ADR 0009). Без файлов модели — SKIP (модель вне git)."""
from __future__ import annotations

from helpers import skip


def _encoder():
    from dms_core.index.embed import get_encoder

    enc = get_encoder()
    if enc is None:
        skip("нет энкодера: scripts/export_encoder.py → data/models/rubert-tiny2-onnx/")
    return enc


def test_onnx_matches_torch_reference():
    # Квантованная int8-модель близка к исходной torch-модели (эталонные векторы — meta.json экспорта)
    import numpy as np

    from dms_core.index.embed import meta

    enc = _encoder()
    m = meta()
    v = enc.encode(m["ref_texts"])
    assert v.shape == (2, 312)
    assert all(float(a @ b) > 0.9 for a, b in zip(v, np.array(m["ref_vectors"]), strict=True))


def test_knn_routes_typical_questions():
    # Свои формулировки, не из эталонов и не из route_examples.yaml
    from dms_core.constants import Scenario
    from dms_core.scenarios.knn_route import knn_scenario

    _encoder()
    assert knn_scenario("Нужно ли платить за приём, если клиника говорит, что полис это не покрывает?").scenario \
        == Scenario.sc01
    # неуверенный голос (малый отрыв) не перебивает правила: embedding_scenario → None или верный сценарий
    from dms_core.scenarios.route import embedding_scenario, rule_scenario

    q = "Подскажите, где пройти УЗИ по программе сотрудника"
    assert embedding_scenario(q) in (None, Scenario.sc02_clinic)
    assert rule_scenario(q) == Scenario.sc02_clinic


def test_route_works_without_encoder():
    # Нет энкодера — маршрутизация на правилах, сервис не падает
    import dms_core.scenarios.knn_route as kr

    kr._bank.cache_clear()
    orig = kr.get_encoder
    kr.get_encoder = lambda: None
    try:
        assert kr.knn_scenario("Клиника отказала в МРТ") is None
    finally:
        kr.get_encoder = orig
        kr._bank.cache_clear()
