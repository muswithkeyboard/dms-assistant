"""Пределы и журнал: 413 на большое тело, 429 на частые запросы, 503 при занятых слотах, расчёт не держит цикл
событий, в журнале нет текста вопроса."""
from __future__ import annotations

import logging
import threading
import time

from api.api_kit import FakeEngine, make_client, new_request
from dms_api import security
from dms_api.settings import Settings


def test_json_body_too_large_is_413():
    c, _, _ = make_client(engine=FakeEngine(), settings=Settings(_env_file=None, max_json_kb=1))
    _, rid = new_request(c)
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "х" * 3000})
    assert r.status_code == 413 and "Слишком большой запрос" in r.json()["error"]["message"]


def test_question_validation():
    c, _, _ = make_client(engine=FakeEngine())
    _, rid = new_request(c)
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "  "})
    assert r.status_code == 422 and r.json()["error"]["field"] == "text"
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "х" * 4001}).status_code == 422
    assert c.post("/api/requests/нет/messages", json={"text": "вопрос"}).status_code == 404


def test_rate_limit_is_429_with_retry_after():
    c, _, _ = make_client(engine=FakeEngine(), settings=Settings(_env_file=None, rate_per_min=3))
    codes = [c.get("/api/groups").status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    r = c.get("/api/groups")
    assert int(r.headers["retry-after"]) >= 1 and "Слишком много" in r.json()["error"]["message"]


def test_busy_gate_is_503_even_for_sse():
    c, _, _ = make_client(engine=FakeEngine(), llm=object(), gate=security.HeavyGate(slots=0))
    _, rid = new_request(c)
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "вопрос"}, headers={"Accept": "text/event-stream"})
    assert r.status_code == 503 and r.headers["retry-after"] and "занят" in r.json()["error"]["message"]


def test_slow_answer_does_not_block_other_requests_and_slot_is_released():
    gate = security.HeavyGate(slots=2, per_client=1, client_wait=0.1)
    started = threading.Event()

    class Slow(FakeEngine):
        def __call__(self, *a, **kw):
            started.set()
            time.sleep(0.6)
            return super().__call__(*a, **kw)

    c, _, _ = make_client(engine=Slow(), llm=object(), gate=gate)
    _, rid = new_request(c)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", c.post(f"/api/requests/{rid}/messages",
                                                                    json={"text": "вопрос"})))
    t.start()
    assert started.wait(2)
    t0 = time.perf_counter()
    assert c.get("/api/groups").status_code == 200            # цикл событий свободен, пока идёт расчёт
    assert time.perf_counter() - t0 < 0.5
    # второй тяжёлый запрос того же адреса — 503 «ваш предыдущий запрос ещё обрабатывается»
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "второй"})
    assert r.status_code == 503 and "предыдущий" in r.json()["error"]["message"]
    t.join(5)
    assert out["r"].status_code == 200
    assert gate._active == {} and gate._sem.acquire(blocking=False)   # слот освобождён потоком расчёта
    gate._sem.release()


def test_access_log_has_no_question_text():
    records: list[str] = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = H()
    logging.getLogger("dms").addHandler(h)
    try:
        c, _, _ = make_client(engine=FakeEngine(), llm=object())
        _, rid = new_request(c)
        secret = "СекретныйВопросПроКлиникуЗубная"
        c.post(f"/api/requests/{rid}/messages", json={"text": secret})
        c.post(f"/api/requests/{rid}/messages?q={secret}", json={"text": secret},
               headers={"Accept": "text/event-stream"})
    finally:
        logging.getLogger("dms").removeHandler(h)
    assert records and not any(secret in m for m in records)
    assert any("/messages" in m for m in records) and any("answer request=" in m for m in records)


def test_static_version_changes_with_content():
    # Метка ?v= статики — по содержимому: правка JS/CSS меняет адрес, иначе браузер держит старый файл (immutable)
    import tempfile
    from pathlib import Path

    from dms_api.app import static_version

    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.js"
        f.write_text("var a = 1;", encoding="utf-8")
        v1 = static_version(Path(d))
        f.write_text("var a = 2;", encoding="utf-8")
        assert static_version(Path(d)) != v1
