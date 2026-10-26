"""Волна 0: каркас собирается, конфиги читаются, правила проекта соблюдаются кодом."""
from __future__ import annotations

import re

from helpers import ROOT


def test_configs_load():
    from dms_core.config import CONFIG_FILES, load_yaml

    for name in CONFIG_FILES:
        data = load_yaml(name)
        assert isinstance(data, dict) and data.get("version"), name


def test_prompt_has_version():
    from dms_core.config import prompt_version

    assert prompt_version("system") != "unknown"


def test_no_mail_sending_in_src():
    # `docs/rules.md` п. 3: сервер писем не отправляет — в src/ нет SMTP и почтовых API.
    pattern = re.compile(r"\b(smtplib|aiosmtplib|sendmail|SMTP\(|mailgun|sendgrid|yagmail)\b", re.IGNORECASE)
    for path in (ROOT / "src").rglob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path


def test_draft_templates_have_no_forbidden_promises():
    # План §6.9: шаблоны черновиков не содержат обещаний; исключения — allow_in конфига.
    from dms_core.config import load_yaml

    cfg = load_yaml("forbidden_promises.yaml")
    for tid, tpl in load_yaml("texts_ru/drafts.yaml")["templates"].items():
        text = f"{tpl['subject']}\n{tpl['body']}"
        for pat in cfg["patterns"]:
            if tid in cfg.get("allow_in", {}).get(pat, []):
                continue
            assert not re.search(pat, text, re.IGNORECASE), (tid, pat)


def test_add_clinic_transitions_reach_known_states():
    from dms_core.config import load_yaml
    from dms_core.constants import AddClinicState

    cfg = load_yaml("add_clinic.yaml")
    known = {s.value for s in AddClinicState}
    for src, moves in cfg["transitions"].items():
        assert src in known
        assert set(moves.values()) <= known
    for state, drafts in cfg["drafts"].items():
        assert state in known
        assert set(drafts) <= set(load_yaml("texts_ru/drafts.yaml")["templates"])


def test_precedence_covers_all_doc_types():
    from dms_core.config import load_yaml
    from dms_core.constants import DocType

    assert set(load_yaml("precedence.yaml")["levels"]) == {d.value for d in DocType}


def test_classify_kit_filenames():
    # Имена — как в комплекте (docs/facts/kit/); проверка правил config/doc_types.yaml без самих файлов.
    from dms_core.kit import classify

    assert classify("Договор № 1 (с приложениями).docx") == ("contract", "contract")
    assert classify("Приложение 1. Программа ДМС.docx") == ("program", "contract")
    assert classify("Приложение 2. Программы ДМС.docx") == ("program", "contract")
    assert classify("Приложение 4. Список медицинских организаций.xlsx") == ("clinics", "contract")
    assert classify("Приложение 6. Памятка.docx") == ("memo", "contract")
    assert classify("ЛПУ_Регионы Стандарт.xlsx") == ("catalog", "group")
    assert classify("GN. Правила страхования от несчастных случаев и болезней.pdf") == ("rules", "group")


def test_answer_schema_roundtrip():
    from dms_core.constants import Confidence, Scenario
    from dms_core.schemas import Answer, AnswerContext

    a = Answer(scenario=Scenario.sc01, context=AnswerContext(contract_id="c01"), summary="—",
               confidence=Confidence.insufficient)
    assert Answer.model_validate_json(a.model_dump_json()) == a


def test_draft_never_auto_send():
    from dms_core.schemas import Draft

    assert Draft.model_fields["auto_send"].default is False
