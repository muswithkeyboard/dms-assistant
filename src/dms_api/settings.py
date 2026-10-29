"""Настройки веб-сервиса из переменных окружения и `.env` (префикс DMS_; по образцу Justmedit settings.py).

  DMS_HOST, DMS_PORT       — адрес и порт (127.0.0.1:8080; в Docker — 0.0.0.0, наружу публикуется только 127.0.0.1);
  DMS_DATA_DIR             — каталог данных (по умолчанию <app>/data): raw/ комплект, derived/ разбор, app/ SQLite;
  DMS_RATE_PER_MIN         — лимит запросов /api/* с одного адреса в минуту (по умолчанию 120);
  DMS_MAX_UPLOAD_MB        — предел размера загружаемого документа, МБ (по умолчанию 25: программы Ренессанса до 2 МБ,
                             каталог ЛПУ ГК 1 — 8,5 МБ);
  DMS_MAX_JSON_KB          — предел тела JSON-запросов, КБ (по умолчанию 256);
  DMS_HTTPS                — 1: сайт за HTTPS, заголовок Strict-Transport-Security;
  DMS_TRUSTED_PROXIES      — адреса и сети обратных прокси через запятую; пусто — не доверять никому;
  DMS_LLM_PROVIDER         — anthropic | openai_compat | gigachat | mock; пусто — без модели (demo_saved / extractive).
Ключи моделей читает только src/dms_core/llm/ (ADR 0002); здесь их нет.
"""
from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DMS_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: str = ""
    rate_per_min: int = 120
    max_upload_mb: int = 25
    max_json_kb: int = 256
    https: bool = False
    trusted_proxies: str = ""
    llm_provider: str = ""
    # прогрев: пункты и поисковые индексы договоров строятся в фоне при старте (круг 2 ревизии w16) — первый
    # ответ по ГК не ждёт чтения всех пунктов; 0 — строить при первом вопросе
    warm: bool = True

    @field_validator("trusted_proxies")
    @classmethod
    def _check_proxies(cls, v: str) -> str:
        """Опечатка в настройке безопасности останавливает запуск (ValueError), а не выключает проверку молча."""
        from .security import parse_networks

        parse_networks(tuple(x.strip() for x in v.split(",") if x.strip()))
        return v

    @property
    def proxies(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.trusted_proxies.split(",") if x.strip())

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024
