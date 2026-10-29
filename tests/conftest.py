"""pytest: помощники тестов загружаются до любого теста — в том числе отключение чтения .env разработчика."""
import pytest

import helpers  # noqa: F401


@pytest.fixture(autouse=True)
def _assist_pause_reset():
    """Пауза модели-помощника после сбоя (assist.py) — на процесс; между тестами не переносится."""
    from dms_core.assist import reset_pause
    reset_pause()
    yield
    reset_pause()
