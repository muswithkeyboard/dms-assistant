# Ассистент ДМС — два образа из одного файла (по образцу Justmedit):
#   target app    — веб-сервис и командная строка `dms`;
#   target verify — тот же образ плюс тесты, скрипты и dev-зависимости: `bash scripts/verify.sh`.
# Сборка и запуск: docker compose up --build. Документов организаторов в образе нет (.dockerignore):
# комплект монтируется томом только для чтения (ADR 0007).
ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_VERSION=0.12.19

# builder: окружение /opt/venv строго по uv.lock
FROM ${PYTHON_IMAGE} AS builder
ARG UV_VERSION
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1
RUN pip install "uv==${UV_VERSION}"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

FROM builder AS builder-dev
RUN uv sync --frozen --no-install-project --extra dev

# app: без uv, без root, файловая система только для чтения (docker-compose.yml)
FROM ${PYTHON_IMAGE} AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH PYTHONPATH=/app/src HOME=/tmp \
    DMS_HOST=0.0.0.0 DMS_PORT=8080 DMS_DATA_DIR=/data DMS_TRUSTED_PROXIES=""
# tini — PID 1: убирает процессы-«сироты» (дочерний процесс разбора PDF, убитый по таймауту)
RUN apt-get update && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app \
 && printf '#!/bin/sh\nexec python -m dms_core.cli "$@"\n' > /usr/local/bin/dms && chmod 0755 /usr/local/bin/dms \
 && mkdir -p /data/raw /data/derived /data/app && chown -R 10001:10001 /data/derived /data/app
RUN python -m compileall -q -j 0 --invalidation-mode unchecked-hash \
      "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY src ./src
RUN python -m compileall -q --invalidation-mode unchecked-hash /app/src
COPY config ./config
# scripts/ingest_kit.py — импорт комплекта из интерфейса оператора (POST /api/documents/import-kit)
COPY scripts ./scripts
# Права из рабочей копии бывают 600 (файлы, скопированные из reference/) — пользователь 10001 их бы не прочитал
# и сервис упал бы на старте: код и конфиги — только чтение для всех (найдено при проверке в Docker 24.10.2026).
RUN chmod -R a+rX /app/src /app/config /app/scripts
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]
# --no-access-log: журнал uvicorn выключен (строка запроса и адрес не пишутся), сервис ведёт свой журнал.
# --no-proxy-headers: X-Forwarded-* разбирает сам сервис и только от DMS_TRUSTED_PROXIES.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "dms_api.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-server-header", "--no-proxy-headers"]

# verify: образ app + тесты и скрипты. Без комплекта тесты на data/raw — SKIP.
FROM app AS verify
COPY --from=builder-dev /opt/venv /opt/venv
COPY tests ./tests
COPY eval ./eval
COPY docs/CONTRACTS.md ./docs/CONTRACTS.md
HEALTHCHECK NONE
CMD ["bash", "scripts/verify.sh"]
