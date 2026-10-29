"""Помощники тестов (по образцу Justmedit tests_helpers.py). Тесты — обычные функции test_* с assert, без фикстур
pytest: их запускает и pytest, и scripts/run_tests.py."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# .env разработчика (модель-помощник Ollama и т. п.) тестами не читается: проверка не зависит от машины (w17)
import dms_core.llm._common as _llm_common

_llm_common.DOTENV_PATH = Path("/nonexistent/.env")


class SkipTest(Exception):
    """Тест пропущен (нет данных или программы). scripts/run_tests.py считает это SKIP, а не FAIL."""


def skip(reason: str):
    try:
        import pytest  # type: ignore
    except ImportError:
        raise SkipTest(reason)
    pytest.skip(reason)


def kit_or_skip() -> Path:
    """Папка «Документы» комплекта или SKIP: в репозитории документов организаторов нет (`docs/rules.md` п. 4)."""
    from dms_core.config import kit_dir

    p = kit_dir()
    if not p.is_dir():
        skip(f"нет комплекта организаторов: {p} (data/raw/ или DMS_DATA_DIR)")
    return p


def kit_db_or_skip():
    """Хранилище разобранного комплекта (data/derived/kit.sqlite) или SKIP. Файл не создаётся: KitStore() на
    отсутствующем пути создал бы пустую базу, и тесты комплекта дальше падали бы вместо SKIP (Docker verify)."""
    from dms_core.store import KitStore, default_path

    if not default_path().is_file():
        skip("нет базы разбора комплекта (data/derived/kit.sqlite)")
    return KitStore(default_path())


def soffice_or_skip() -> str:
    """Путь к LibreOffice (soffice) или SKIP — нужен только для сверки нумерации пунктов."""
    for name in ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        found = shutil.which(name) or (name if Path(name).is_file() else None)
        if found:
            return found
    skip("нет LibreOffice (soffice): сверка нумерации пропущена")
    return ""


def client():
    """TestClient сервиса с настройками по умолчанию."""
    from starlette.testclient import TestClient

    from dms_api.app import create_app

    return TestClient(create_app(), base_url="http://127.0.0.1")


from contextlib import contextmanager


@contextmanager
def env(**values):
    """Переменные окружения на время блока (None — удалить); замена фикстуры monkeypatch для run_tests.py."""
    import os

    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def patched(obj, name: str, value):
    """Атрибут объекта/модуля на время блока."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


class MonkeyPatch:
    """Минимальная замена фикстуры pytest monkeypatch (setenv / delenv / setattr с откатом) — тесты остаются обычными
    функциями без фикстур (`docs/rules.md` п. 9) и запускаются и pytest, и scripts/run_tests.py."""

    def __init__(self):
        self._undo: list = []

    def setenv(self, name: str, value: str) -> None:
        import os

        old = os.environ.get(name)
        os.environ[name] = value
        self._undo.append(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))

    def delenv(self, name: str, raising: bool = True) -> None:
        import os

        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        old = os.environ.pop(name)
        self._undo.append(lambda: os.environ.__setitem__(name, old))

    def setattr(self, target, name, value=None) -> None:
        import importlib

        if isinstance(target, str):                       # «пакет.модуль.атрибут», значение — второй аргумент
            mod, _, attr = target.rpartition(".")
            target, name, value = importlib.import_module(mod), attr, name
        old = getattr(target, name)
        setattr(target, name, value)
        self._undo.append(lambda: setattr(target, name, old))

    def undo(self) -> None:
        while self._undo:
            self._undo.pop()()


def with_monkeypatch(fn):
    """Декоратор: передаёт в тест MonkeyPatch и откатывает изменения. Без functools.wraps — иначе pytest увидит
    параметр monkeypatch через __wrapped__ и подставит свою фикстуру в функцию без аргументов."""
    def wrapper():
        mp = MonkeyPatch()
        try:
            return fn(mp)
        finally:
            mp.undo()

    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def skip_unless_rule(rule_id: str) -> None:
    """SKIP, если правило config/verdict_rules.yaml выключено автором (предохранитель по ревизии)."""
    from dms_core.config import load_yaml

    if rule_id not in {r["id"] for r in load_yaml("verdict_rules.yaml")["rules"]}:
        skip(f"правило {rule_id} выключено автором (предохранитель, ревизия 4)")


def skip_unless_limit_deny() -> None:
    from dms_core.config import load_yaml

    if not (load_yaml("coverage_markers.yaml").get("limit_exceeded") or {}).get("deny", True):
        skip("«лимит превышен → нельзя» выключено автором (предохранитель, ревизия 4)")


def label_as_engine(verified, pick: str | None = None, *, refusal: bool = False):
    """Метка SC 01 так, как её ставит engine._one_contract (ADR 0010): clinic_confirmed — выбор менеджера передан
    и guided его учёл (draft.clinic_choice.selected == выбор). Возвращает (label, note, rule)."""
    from dms_core.constants import Label
    from dms_core.scenarios.sc01_refusal import decide_label

    if not verified.citations:
        return Label.insufficient, None, "no_evidence"
    choice = verified.draft.clinic_choice
    confirmed = bool(pick) and choice is not None and choice.selected == pick
    return decide_label(verified.draft.checks, has_alternative=bool(verified.draft.alternatives),
                        facts={"refusal": refusal, "clinic_confirmed": confirmed})
