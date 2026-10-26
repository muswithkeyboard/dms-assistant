"""Скриншоты интерфейса на демо-данных (?mock=1) — docs/ui_style.md «Проверка», бриф w3-ui.

Запуск: `uv run python scripts/screenshots.py [--out docs/screenshots] [--only 03,05]`.
Сервис поднимается в этом же процессе на 127.0.0.1 (свободный порт), страницы открываются в Chromium через
Playwright: сначала установленный Google Chrome (channel="chrome", без загрузки), затем браузер Playwright, если
он установлен. Нет ни того, ни другого — SKIP (код 0) с подсказкой. Данные — выдуманные (static/mock_api.js),
текстов документов комплекта на снимках нет.
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SIZES = [(1366, 768), (1920, 1080)]
# Тёмная тема — только для ключевых экранов (на обоих размерах), чтобы снимков не было слишком много.
DARK = {"01", "03", "05", "10"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int):
    import uvicorn

    from dms_api.app import create_app

    app = create_app()
    logging.getLogger("dms").setLevel(logging.WARNING)   # журнал доступа на каждый файл снимкам не нужен
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("сервис не запустился")


# Сцены: (код, имя, путь, действие со страницей, снимать всю страницу)
def _scenes():
    def click_cite(page):
        page.locator(".answer button.cite").first.click()
        page.wait_for_selector(".srcpanel__body mark", timeout=5000)

    def new_chat(page):
        page.click("#new-chat-btn")
        page.check('input[name="nc-kind"][value="contract"]')
        page.fill("#nc-search", "ветер")
        page.select_option("#nc-contract", "c1")
        page.fill("#nc-program", "Стандарт")

    def open_examples(page):
        # w15-ui: примеры свёрнуты в кнопку «Примеры вопросов» над полем ввода
        if page.locator("#examples-btn").count() and not page.locator("#examples-list .ex").first.is_visible():
            page.click("#examples-btn")

    def progress(page):
        open_examples(page)
        page.locator("#examples-list .ex").first.click()
        page.wait_for_selector(".typing", timeout=10000)

    def error(page):
        open_examples(page)
        page.locator("#examples-list .ex").last.click()
        page.wait_for_selector("#alert:not([hidden])", timeout=15000)

    def insurer_tab(page):
        page.click("#dtab-insurer")

    def closed(page):
        page.check("#show-closed")
        page.wait_for_selector("#req-list .is-closed")
        page.locator("#req-list .is-closed").first.click()

    def wait_answer(page):
        page.wait_for_selector(".answer", timeout=5000)

    return [
        ("01", "chat_empty", "/?mock=1&seed=empty", None, False),
        ("02", "new_chat_dialog", "/?mock=1&seed=empty", new_chat, False),
        ("03", "answer_conditional_sources", "/?mock=1#chat=ch1&req=r1", click_cite, True),
        ("04", "group_per_contract", "/?mock=1#chat=ch2&req=r4", wait_answer, True),
        ("05", "automation_paid_drafts_hr", "/?mock=1#chat=ch1&req=r2", wait_answer, True),
        ("06", "drafts_insurer", "/?mock=1#chat=ch1&req=r2", insurer_tab, True),
        ("07", "progress_stages", "/?mock=1&seed=empty&slow=1", progress, False),
        ("08", "error_ru", "/?mock=1&seed=empty", error, False),
        ("09", "closed_requests", "/?mock=1#chat=ch1", closed, True),
        ("10", "documents_clause", "/documents?mock=1#doc=d2&clause=4.2", None, True),
        ("11", "documents_error_doc", "/documents?mock=1#doc=d9", None, False),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    ap.add_argument("--only", default="", help="коды сцен через запятую")
    args = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: нет пакета playwright (uv sync --extra dev)")
        return 0
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    server = _serve(port)
    base = f"http://127.0.0.1:{port}"
    n = 0
    with sync_playwright() as p:
        browser = None
        for kw in ({"channel": "chrome"}, {}):
            try:
                browser = p.chromium.launch(**kw)
                break
            except Exception:  # noqa: BLE001,S112 — пробуем следующий вариант браузера
                continue
        if browser is None:
            print("SKIP: нет Chrome и браузера Playwright (uv run playwright install chromium — это загрузка)")
            server.should_exit = True
            return 0
        for code, name, path, action, full in _scenes():
            if only and code not in only:
                continue
            for w, h in SIZES:
                for theme in ("light", "dark") if code in DARK else ("light",):
                    ctx = browser.new_context(viewport={"width": w, "height": h}, locale="ru-RU",
                                              color_scheme=theme)
                    ctx.add_init_script(f"try {{ localStorage.setItem('dms-theme', '{theme}'); }} catch (e) {{}}")
                    page = ctx.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                    page.goto(base + path)
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(300)
                    if action:
                        action(page)
                    page.wait_for_timeout(400)
                    if full:
                        # Снимок всей страницы: липкие колонки рисуются посреди склейки — на время снимка
                        # открепляем их через CSSOM (CSP запрещает вставку <style>).
                        page.evaluate("document.querySelectorAll('.side,.layout3>.sources')"
                                      ".forEach(function(n){n.style.position='static';n.style.maxHeight='none';})"
                                      if w > 1365 else
                                      "document.querySelectorAll('.side')"
                                      ".forEach(function(n){n.style.position='static';n.style.maxHeight='none';})")
                    fname = out / f"{code}_{name}_{w}x{h}_{theme}.png"
                    page.screenshot(path=str(fname), full_page=full)
                    if errors:
                        print(f"  ошибки JS на {fname.name}: {errors}")
                    n += 1
                    ctx.close()
        browser.close()
    server.should_exit = True
    print(f"снимков: {n} → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
