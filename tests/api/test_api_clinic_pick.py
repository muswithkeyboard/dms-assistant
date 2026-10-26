"""POST /api/requests/{id}/clinic — выбор клиники менеджером (ADR 0010, CONTRACTS §8, §8.1): настоящий движок в
режиме guided на синтетике, без сети. Кандидаты в ответе → выбор строки → «можно»; «нет в перечне» → «нельзя»;
строка чужого договора / программы → 422 и не доходит до движка; 409 без выбора; сброс выбора новым вопросом;
закрытый запрос; ГК — выборы по договорам хранятся отдельно."""
from __future__ import annotations

from api.api_kit import make_client, new_request
from guided.test_clinic_pick import Q_NONE, Q_OK, _gk_store
from guided.test_recall2 import EXCLUSIONS, INCLUDED, _kit
from helpers import skip_unless_rule


def _client(kit=None):
    c, svc, _tmp = make_client(kit=kit or _kit(INCLUDED + EXCLUSIONS))   # движок — engine.answer, модели нет
    return c, svc


def _ask(c, rid: str, text: str) -> dict:
    r = c.post(f"/api/requests/{rid}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()["answer"]


def test_pick_row_then_none_recompute_same_question():
    skip_unless_rule("covered")
    c, _svc = _client()
    _chat, rid = new_request(c, {"contract_id": "c01", "program": "Стандарт"})
    ans = _ask(c, rid, Q_OK)
    assert ans["label"] != "allowed"
    ch = ans["clinic_choices"][0]
    assert ch["contract_id"] == "c01" and ch["selected"] is None and ch["candidates"]
    row = ch["candidates"][0]
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": row["id"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["answer"]["label"] == "allowed" and out["answer"]["clinic_choices"][0]["selected"] == row["id"]
    assert out["message"]["role"] == "assistant"
    req = c.get(f"/api/requests/{rid}").json()
    assert [m["role"] for m in req["messages"]] == ["user", "assistant", "user", "assistant"]
    assert req["messages"][2]["text"].startswith("Клиника: ") and row["clinic_name"] in req["messages"][2]["text"]
    assert req["slots"]["clinic_pick:c01"] == row["id"]
    # повторный выбор по пересчитанному ответу пересчитывает тот же вопрос, а не реплику «Клиника: …»
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": "none"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"]["clinic_choices"][0]["asked"] == ch["asked"]
    assert r.json()["answer"]["clinic_choices"][0]["selected"] == "none"
    req = c.get(f"/api/requests/{rid}").json()
    assert req["messages"][4]["text"] == "Клиники нет в перечне программы"
    assert req["slots"]["clinic_pick:c01"] == "none"
    # новый вопрос сбрасывает выборы
    _ask(c, rid, "Положена ли стоматология?")
    assert not any(k.startswith("clinic_pick:") for k in c.get(f"/api/requests/{rid}").json()["slots"])


def test_pick_none_denied():
    skip_unless_rule("clinic_absent")
    c, _svc = _client()
    _chat, rid = new_request(c, {"contract_id": "c01", "program": "Стандарт"})
    ans = _ask(c, rid, Q_NONE)
    assert ans["label"] != "denied" and ans["clinic_choices"]
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": "none"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"]["label"] == "denied" and r.json()["answer"]["alternatives"]


def test_errors_foreign_row_contract_no_choice_closed():
    c, _svc = _client()
    _chat, rid = new_request(c, {"contract_id": "c01", "program": "Стандарт"})
    # нет ответа с выбором клиники — 409
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": "none"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_clinic_choice"
    _ask(c, rid, "Прием невролога по ДМС пойдет?")          # клиника не названа — выбора нет
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": "none"})
    assert r.status_code == 409
    _ask(c, rid, Q_OK)
    before = len(c.get(f"/api/requests/{rid}").json()["messages"])
    # строка другого договора, другой программы, выдуманная — 422, движок не вызывается, сообщений не прибавилось
    for bad in ("c02.clinics#t0r0", "c01.clinics#s12", "c01.clinics#нет"):
        r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": bad})
        assert r.status_code == 422 and r.json()["error"]["code"] == "clinic_row_unknown", bad
    # договор не из контекста чата
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c02", "choice": "none"})
    assert r.status_code == 422 and r.json()["error"]["field"] == "contract_id"
    # пустой выбор
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01"})
    assert r.status_code == 422 and r.json()["error"]["field"] == "choice"
    req = c.get(f"/api/requests/{rid}").json()
    assert len(req["messages"]) == before and not any(k.startswith("clinic_pick:") for k in req["slots"])
    # закрытый запрос — как у остальных методов
    c.post(f"/api/requests/{rid}/close")
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": "none"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "request_closed"
    assert c.post("/api/requests/нет/clinic", json={"contract_id": "c01", "choice": "none"}).status_code == 404


def test_group_context_picks_kept_per_contract():
    c, _svc = _client(_gk_store())
    _chat, rid = new_request(c, {"group_id": "gk9", "program": "Стандарт"})
    ans = _ask(c, rid, Q_OK)
    assert {x["contract_id"] for x in ans["clinic_choices"]} == {"c01", "c02"}
    # строка c01 в выборе по c02 — 422 (ADR 0005)
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c02", "choice": "c01.clinics#s0"})
    assert r.status_code == 422
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": "c01.clinics#s0"})
    by = {x["contract_id"]: x["selected"] for x in r.json()["answer"]["clinic_choices"]}
    assert by == {"c01": "c01.clinics#s0", "c02": None}
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c02", "choice": "c02.clinics#s0"})
    assert r.status_code == 200, r.text
    by = {x["contract_id"]: x["selected"] for x in r.json()["answer"]["clinic_choices"]}
    assert by == {"c01": "c01.clinics#s0", "c02": "c02.clinics#s0"}     # выбор по c01 сохранён
    slots = c.get(f"/api/requests/{rid}").json()["slots"]
    assert slots["clinic_pick:c01"] == "c01.clinics#s0" and slots["clinic_pick:c02"] == "c02.clinics#s0"


# ---------------------------------------------------------------- поиск по перечню (круг w7)


def test_clinic_candidates_search_pages_and_errors():
    from dms_core.constants import CLINIC_SEARCH_PAGE

    c, svc = _client()
    _chat, rid = new_request(c, {"contract_id": "c01", "program": "Стандарт"})
    url = f"/api/requests/{rid}/clinic-candidates"
    # выбора клиники ещё нет — 409
    r = c.get(url, params={"contract_id": "c01"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_clinic_choice"
    _ask(c, rid, Q_OK)
    # пустой запрос — весь перечень программы по порядку, без строк другой программы
    every = svc.kit.clinics("c01", program="Стандарт")
    r = c.get(url, params={"contract_id": "c01"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["total"] == len(every) and out["offset"] == 0
    assert [x["id"] for x in out["rows"]] == [x.id for x in every][:CLINIC_SEARCH_PAGE]
    assert all(x["program"] == "Стандарт" for x in out["rows"])
    # поиск по названию и городу; страница со сдвигом
    r = c.get(url, params={"contract_id": "c01", "q": "верба"})
    assert [x["clinic_name"] for x in r.json()["rows"]] == ["«Верба» ООО"]
    r = c.get(url, params={"contract_id": "c01", "q": "томск"})
    assert r.json()["total"] >= 2 and all(x["city"] == "Томск" for x in r.json()["rows"])
    r = c.get(url, params={"contract_id": "c01", "offset": "2"})
    assert r.json()["offset"] == 2 and r.json()["rows"][0]["id"] == every[2].id
    # чужой договор и неверный сдвиг — 422; запрос не найден — 404
    r = c.get(url, params={"contract_id": "c02"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "contract_not_in_context"
    assert c.get(url, params={"contract_id": "c01", "offset": "-1"}).status_code == 422
    assert c.get(url, params={"contract_id": "c01", "offset": "x"}).status_code == 422
    assert c.get(url).status_code == 422
    assert c.get("/api/requests/нет/clinic-candidates", params={"contract_id": "c01"}).status_code == 404


def test_pick_row_found_by_search_counts_and_goes_first():
    skip_unless_rule("covered")
    c, _svc = _client()
    _chat, rid = new_request(c, {"contract_id": "c01", "program": "Стандарт"})
    ans = _ask(c, rid, Q_OK)
    offered = {x["id"] for x in ans["clinic_choices"][0]["candidates"]}
    row = c.get(f"/api/requests/{rid}/clinic-candidates", params={"contract_id": "c01", "q": "верба"}).json()["rows"][0]
    assert row["id"] not in offered                         # не из кандидатов — найдена поиском
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": row["id"]})
    assert r.status_code == 200, r.text
    ch = r.json()["answer"]["clinic_choices"][0]
    assert ch["selected"] == row["id"] and ch["candidates"][0]["id"] == row["id"]
    assert r.json()["answer"]["label"] == "allowed"          # строка из поиска учтена как выбор менеджера
    # после выбора поиск идёт по тому же перечню (последний ответ — пересчёт с выбором)
    r = c.get(f"/api/requests/{rid}/clinic-candidates", params={"contract_id": "c01", "q": "ромашка"})
    assert r.status_code == 200 and r.json()["total"] == 1
