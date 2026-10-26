"""Веб-сервис волны 0: страницы, служебные маршруты, заголовки безопасности."""
from __future__ import annotations

from helpers import client


def test_pages_render():
    c = client()
    for path in ("/", "/documents", "/about"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "Ассистент ДМС" in r.text
        assert "http://" not in r.text and "https://" not in r.text   # без внешних ресурсов


def test_healthz_and_version():
    c = client()
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.headers["cache-control"] == "no-store"
    assert set(c.get("/version").json()) == {"engine", "prompt", "llm"}


def test_security_headers():
    r = client().get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_not_found_is_russian():
    c = client()
    assert c.get("/нет-такой").status_code == 404
    r = c.get("/api/нет")
    assert r.status_code == 404 and "Страница не найдена" in r.json()["error"]["message"]


def test_static_css_has_both_themes():
    css = client().get("/static/app.css").text
    assert ':root[data-theme="dark"]' in css and "prefers-color-scheme: dark" in css
