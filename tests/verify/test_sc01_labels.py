"""SC 01: метка строго по config/verdict_rules.yaml — по тесту на каждое правило; not_found не даёт «нельзя»."""
from __future__ import annotations

import itertools

from dms_core.config import load_yaml
from dms_core.constants import Label
from dms_core.scenarios.sc01_refusal import decide_label
from dms_core.schemas import Checks
from helpers import skip_unless_rule


def test_rule_excluded():
    assert decide_label(Checks(exclusion="yes", care_in_program="yes", clinic_in_program="yes"), False) == (
        Label.denied, None, "excluded")


def test_rule_care_absent():
    assert decide_label(Checks(care_in_program="absent", clinic_in_program="yes"), False)[::2] == (
        Label.denied, "care_absent")


CONFIRMED = {"clinic_confirmed": True}     # клиника подтверждена выбором менеджера (ADR 0010)


def test_rule_clinic_absent():
    skip_unless_rule("clinic_absent")
    absent = Checks(care_in_program="yes", clinic_in_program="absent")
    assert decide_label(absent, False, CONFIRMED)[::2] == (Label.denied, "clinic_absent")
    # без выбора менеджера «нет в перечне» по тексту вопроса — не «нельзя»
    assert decide_label(absent, False)[0] is not Label.denied
    assert decide_label(absent, False, {"clinic_confirmed": False})[0] is not Label.denied


def test_denied_with_alternative_note():
    skip_unless_rule("clinic_absent")
    note = load_yaml("verdict_rules.yaml")["notes"]["denied_with_alternative"]
    assert decide_label(Checks(care_in_program="yes", clinic_in_program="absent"), True, CONFIRMED) == (
        Label.denied, note, "clinic_absent")


def test_rule_contradiction():
    label, note, rid = decide_label(Checks(care_in_program="yes", clinic_in_program="yes",
                                           documents="contradictory"), False)
    assert (label, rid) == (Label.conditional, "contradiction") and note == "нужно подтверждение СК"


def test_rule_conditions():
    assert decide_label(Checks(care_in_program="yes", clinic_in_program="yes", conditions=["franchise"]), False) == (
        Label.conditional, None, "conditions")


def test_rule_covered():
    skip_unless_rule("covered")
    ok = Checks(care_in_program="yes", clinic_in_program="yes", exclusion="absent")
    assert decide_label(ok, False, CONFIRMED) == (Label.allowed, None, "covered")
    # без выбора менеджера клиника по тексту не подтверждена — не «можно» (ADR 0010)
    assert decide_label(ok, False)[0] is not Label.allowed


def test_rule_clinic_unresolved():
    """Клиника названа, но не подтверждена (не нашли / «нет в перечне» без выбора) → «недостаточно данных» с
    пометкой «выберите клинику», а не «при условиях» (CONTRACTS §8 дополнение w7, ревизия 6)."""
    note = next(r["note"] for r in load_yaml("verdict_rules.yaml")["rules"] if r["id"] == "clinic_unresolved")
    named = {"clinic_named": True}
    for clinic in ("not_found", "absent"):
        cond = Checks(care_in_program="yes", clinic_in_program=clinic, conditions=["franchise"])
        assert decide_label(cond, False, named) == (Label.insufficient, note, "clinic_unresolved")
        # клиника не названа — прежнее правило условий
        assert decide_label(cond, False)[::2] == (Label.conditional, "conditions")
        assert decide_label(cond, False, {"clinic_named": False})[::2] == (Label.conditional, "conditions")
    # клиника подтверждена (yes) — условия, как раньше
    ok = Checks(care_in_program="yes", clinic_in_program="yes", conditions=["franchise"])
    assert decide_label(ok, False, named)[::2] == (Label.conditional, "conditions")
    # исключение и отсутствие вида помощи стоят раньше: «нельзя» не прячется за выбором клиники
    assert decide_label(Checks(exclusion="yes", care_in_program="yes", clinic_in_program="not_found"), False,
                        named)[::2] == (Label.denied, "excluded")
    assert decide_label(Checks(care_in_program="absent", clinic_in_program="not_found"), False,
                        named)[::2] == (Label.denied, "care_absent")
    # «нет в перечне» по выбору менеджера — «нельзя» (clinic_absent раньше clinic_unresolved)
    if any(r["id"] == "clinic_absent" for r in load_yaml("verdict_rules.yaml")["rules"]):
        absent = Checks(care_in_program="yes", clinic_in_program="absent")
        assert decide_label(absent, False, {**named, **CONFIRMED})[::2] == (Label.denied, "clinic_absent")


def test_rule_unknown():
    assert decide_label(Checks(care_in_program="yes", clinic_in_program="not_found"), True) == (
        Label.insufficient, None, "unknown")
    assert decide_label(None, False)[0] is Label.insufficient


def test_every_config_rule_has_a_test():
    ids = {r["id"] for r in load_yaml("verdict_rules.yaml")["rules"]}
    # clinic_absent и covered могут быть выключены автором (предохранитель) — их тесты тогда SKIP
    assert ids - {"clinic_absent", "covered"} == {"excluded", "care_absent", "contradiction", "clinic_unresolved",
                                                "conditions", "unknown"}


def test_not_found_never_denied():
    """Полный перебор: «нельзя» только при exclusion=yes или absent; одни not_found/None — никогда."""
    vals = [None, "yes", "not_found", "absent"]
    for ex, care, clinic, docs, cond in itertools.product(vals, vals, vals, ["consistent", "contradictory"],
                                                          [[], ["referral"]]):
        label, _, _ = decide_label(Checks(exclusion=ex, care_in_program=care, clinic_in_program=clinic,
                                          documents=docs, conditions=cond), False)
        if label is Label.denied:
            assert ex == "yes" or "absent" in (care, clinic), (ex, care, clinic)
        if "not_found" in (ex, care, clinic) and ex != "yes" and "absent" not in (care, clinic):
            assert label is not Label.denied
