"""Ревизия безопасности w16-sec: Host / Origin / Content-Type (п. 2), точный пункт по ref и 409 при одинаковых
номерах (п. 3), один файл в двух договорах (п. 4), demo_saved — программа, дата и перепроверка ссылок (п. 5),
блокировка запроса при вопросе и устаревший выбор клиники (п. 6). Синтетика, без сети."""
from __future__ import annotations

import json
import threading
import time

from starlette.testclient import TestClient

from api.api_kit import FakeEngine, make_client, new_request
from dms_api import security
from dms_api.app import create_app
from dms_api.settings import Settings
from dms_core.constants import Confidence, Scenario
from dms_core.schemas import Answer, AnswerContext, ChatReply, Citation, Clause, ReplyPoint, Statement
from dms_core.store import KitStore
from fixtures.synth_kit import make_group_docs, make_parsed
from helpers import env


def _ru(r) -> str:
    msg = r.json()["error"]["message"]
    assert any("а" <= ch <= "я" for ch in msg.lower()), msg
    return msg


# ------------------------------------------------------------------ п. 2: CSRF и DNS-rebinding

def test_text_plain_json_body_is_415():
    c, _svc, _ = make_client(engine=FakeEngine())
    r = c.post("/api/chats", content=json.dumps({"context": {"contract_id": "c01"}}),
               headers={"Content-Type": "text/plain"})
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_media_type"
    _ru(r)
    assert c.get("/api/chats").json() == []                          # чат не создан
    # кнопки без тела (закрыть, импорт) шлют POST без Content-Type — пустое тело допустимо
    _chat, rid = new_request(c)
    assert c.post(f"/api/requests/{rid}/close").status_code == 200


def test_cross_origin_unsafe_api_is_403():
    c, _svc, _ = make_client(engine=FakeEngine())
    body = {"context": {"contract_id": "c01"}}
    for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                    {"Origin": "http://127.0.0.1:9999"}):
        r = c.post("/api/chats", json=body, headers=headers)
        assert r.status_code == 403 and r.json()["error"]["code"] == "cross_origin", headers
        assert "стороннего сайта" in _ru(r)
    assert c.get("/api/chats").json() == []
    # свой сайт: Origin совпадает с Host, Sec-Fetch-Site same-origin
    r = c.post("/api/chats", json=body, headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 201, r.text
    # чтение с чужим Origin не меняет данных — не отклоняется (браузер сам не отдаст ответ чужой странице)
    assert c.get("/api/chats", headers={"Origin": "https://evil.example"}).status_code == 200


def test_host_header_checked():
    c, _svc, _ = make_client(engine=FakeEngine())
    for path in ("/api/chats", "/", "/healthz"):
        r = c.get(path, headers={"Host": "evil.example"})
        assert r.status_code == 403, path
    r = c.get("/api/chats", headers={"Host": "evil.example"})
    assert r.json()["error"]["code"] == "host_not_allowed" and "DMS_ALLOWED_HOSTS" in _ru(r)
    for host in ("127.0.0.1:8080", "localhost", "LOCALHOST:8080", "[::1]:8080"):
        assert c.get("/healthz", headers={"Host": host}).status_code == 200, host
    # TestClient по умолчанию шлёт Host: testserver — в проде не разрешён
    plain = TestClient(create_app(Settings(_env_file=None), service=c.app.state.service, allowed_hosts=[]))
    assert plain.get("/healthz").status_code == 403


def test_allowed_hosts_from_param_and_env():
    svc = make_client(engine=FakeEngine())[1]
    app = create_app(Settings(_env_file=None), service=svc, allowed_hosts=["dms.example.ru"])
    c = TestClient(app, base_url="http://dms.example.ru:8080")
    assert c.get("/healthz").status_code == 200
    assert c.post("/api/chats", json={"context": {"contract_id": "c01"}},
                  headers={"Origin": "http://dms.example.ru:8080"}).status_code == 201
    assert c.get("/healthz", headers={"Host": "other.example.ru"}).status_code == 403
    with env(DMS_ALLOWED_HOSTS=" Dms.Example.RU:8443 , 10.0.0.5,,bad host"):
        hosts = security.allowed_hosts_from_env()
    assert {"dms.example.ru", "10.0.0.5", "127.0.0.1", "localhost", "[::1]"} <= hosts and "bad host" not in hosts
    assert security.origin_matches("https://dms.example.ru", "dms.example.ru:443", "https")
    assert not security.origin_matches("https://dms.example.ru.evil.example", "dms.example.ru", "https")


# ------------------------------------------------------------------ п. 3: пункт по точной ссылке

def _dup_kit() -> KitStore:
    """c01: в программе два разных пункта «3.1» (Стандарт / Премиум) и пункт общей части с вариантами @v1/@v2."""
    p = make_parsed("c01")
    for k, prog in ((1, "Премиум"), (2, "Стандарт")):
        p.clauses.append(Clause(id=f"c01.program#p90@v{k}", doc_id="c01.program", contract_id="c01", number="7.7",
                                path="Общие › 7.7", program=prog, text="Общий пункт программы для всех вариантов."))
    st = KitStore(":memory:")
    st.save(p)
    st.save(make_parsed("c02"))
    g, docs, cls = make_group_docs()
    st.save_group(g, docs, cls)
    return st


def test_clause_by_exact_ref_and_409_on_duplicate_number():
    c, _svc, _ = make_client(engine=FakeEngine(), kit=_dup_kit())
    r = c.get("/api/documents/c01.program/clause?number=3.1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ambiguous_clause"
    assert _ru(r) == "Пункт с этим номером встречается несколько раз — откройте его по ссылке из ответа."
    ids = [cl.id for cl in _dup_kit().clauses("c01") if cl.number == "3.1" and cl.doc_id == "c01.program"]
    assert len(ids) == 2
    for ref in ids:
        got = c.get("/api/documents/c01.program/clause", params={"ref": ref, "number": "3.1"}).json()["clause"]
        assert got["id"] == ref
    # варианты одного пункта (@vN) — один пункт: по номеру не 409
    assert c.get("/api/documents/c01.program/clause?number=7.7").status_code == 200
    # ссылка с суффиксом — ровно вариант; без суффикса — вариант программы чата
    v1 = c.get("/api/documents/c01.program/clause", params={"ref": "c01.program#p90@v1"}).json()["clause"]
    assert v1["program"] == "Премиум"
    first = c.get("/api/documents/c01.program/clause", params={"ref": "c01.program#p90"}).json()["clause"]
    assert first["id"] == "c01.program#p90@v1"
    chat = c.post("/api/chats", json={"context": {"contract_id": "c01", "program": "Стандарт"}}).json()["id"]
    base = c.get("/api/documents/c01.program/clause", params={"ref": "c01.program#p90", "chat_id": chat})
    assert base.json()["clause"]["id"] == "c01.program#p90@v2"
    # ссылка другого договора в документе c01 не открывается (ADR 0005)
    assert c.get("/api/documents/c01.program/clause", params={"ref": "c02.program#p0"}).status_code == 404


# ------------------------------------------------------------------ п. 4: один файл в двух договорах

def test_same_file_in_two_contracts_kept_in_both():
    c, svc, tmp = make_client(engine=FakeEngine())
    data = "Памятка застрахованному: телефон пульта.".encode()
    docs = {}
    for cid in ("c01", "c02"):
        r = c.post("/api/documents", files={"file": ("Памятка.txt", data)}, data={"contract_id": cid})
        assert r.status_code == 201, r.text
        docs[cid] = r.json()
    assert docs["c01"]["id"] != docs["c02"]["id"]
    assert docs["c01"]["source_path"] != docs["c02"]["source_path"]
    for cid, d in docs.items():
        listed = [x["id"] for x in c.get(f"/api/documents?contract_id={cid}").json()]
        assert d["id"] in listed, cid
        assert (tmp / d["source_path"]).is_file()
    # повторная загрузка в тот же договор — та же запись, не дубль
    again = c.post("/api/documents", files={"file": ("Памятка.txt", data)}, data={"contract_id": "c01"}).json()
    assert again["id"] == docs["c01"]["id"]
    assert [x["id"] for x in c.get("/api/documents?contract_id=c01").json()].count(again["id"]) == 1
    # прежняя запись со старым id (up_<sha[:12]>) читается как есть
    from dms_core.schemas import Document

    old = Document.model_validate({**docs["c01"], "id": "up_0123456789ab"})
    svc.app.save_upload(old)
    assert c.get("/api/documents/up_0123456789ab").json()["id"] == "up_0123456789ab"


# ------------------------------------------------------------------ п. 5: demo_saved

def _demo_setup(tmp, ctx: dict, answer: Answer, eid: str = "K10") -> None:
    gold = tmp / "derived" / "gold"
    gold.mkdir(parents=True, exist_ok=True)
    ex = {"id": eid, "question": "Какая франшиза?", "context": ctx}
    (gold / "k10_k19.jsonl").write_text(json.dumps(ex, ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp / "app" / "demo_saved").mkdir(parents=True, exist_ok=True)
    (tmp / "app" / "demo_saved" / f"{eid}.json").write_text(answer.model_dump_json(), encoding="utf-8")


def _saved_answer(good_ref: str, quote: str) -> Answer:
    good = Citation(doc_id="c01.program", doc_title="program c01", contract_id="c01", clause_number="4.1",
                    quote=quote, verified=True, ref=good_ref)
    gone = Citation(doc_id="c01.program", doc_title="program c01", contract_id="c01", clause_number="99",
                    quote="Пункт, которого больше нет в базе договора после пересборки.", verified=True,
                    ref="c01.program#p999")
    foreign = good.model_copy(update={"contract_id": "c02", "ref": "c02.program#p0"})
    return Answer(scenario=Scenario.sc01, context=AnswerContext(group_id="gk9", contract_id="c01", program="Стандарт"),
                  summary="Сохранённый ответ", confidence=Confidence.confirmed,
                  statements=[Statement(text="Франшиза по программе составляет 10 процентов.", citations=[good, gone]),
                              Statement(text="Пункт, которого больше нет.", citations=[gone]),
                              Statement(text="Франшиза другого договора.", citations=[foreign])],
                  reply=ChatReply(lead="Франшиза 10 %.", points=[
                      ReplyPoint(text="Франшиза", quote=quote, refs=[good_ref]),
                      ReplyPoint(text="Старый пункт", refs=["c01.program#p999"])]))


def test_demo_saved_matches_program_and_date_and_reverifies_refs():
    c, svc, tmp = make_client(engine=FakeEngine(), llm=None)
    fr = next(cl for cl in svc.kit.clauses("c01") if cl.number == "4.1")
    _demo_setup(tmp, {"group_id": "gk9", "contract_id": "c01", "program": "Стандарт", "date": "2026-10-19"},
                _saved_answer(fr.id, fr.text))

    def ask(ctx: dict) -> dict:
        _chat, rid = new_request(c, ctx)
        r = c.post(f"/api/requests/{rid}/messages", json={"text": "какая франшиза"})
        assert r.status_code == 200, r.text
        return r.json()["answer"]

    # другая программа или другая дата — не демо
    assert ask({"contract_id": "c01", "date": "2026-10-19"})["mode"] != "demo_saved"
    assert ask({"contract_id": "c01", "program": "Стандарт", "date": "2026-10-20"})["mode"] != "demo_saved"
    assert ask({"contract_id": "c01", "program": "Стандарт"})["mode"] != "demo_saved"
    ans = ask({"contract_id": "c01", "program": "Стандарт", "date": "2026-10-19"})
    assert ans["mode"] == "demo_saved"
    refs = [cit["ref"] for st in ans["statements"] for cit in st["citations"]]
    assert refs == [fr.id]                                   # мёртвая и чужая ссылки выброшены
    assert len(ans["statements"]) == 1
    assert [p["refs"] for p in ans["reply"]["points"]] == [[fr.id]]


def test_demo_saved_without_live_refs_falls_back():
    c, _svc, tmp = make_client(engine=FakeEngine(), llm=None)
    ans = _saved_answer("c01.program#p998", "Цитата пункта, которого нет в текущей базе договора.")
    _demo_setup(tmp, {"group_id": "gk9", "contract_id": "c01", "program": "Стандарт"}, ans)
    _chat, rid = new_request(c, {"contract_id": "c01", "program": "Стандарт", "date": "2026-10-27"})
    out = c.post(f"/api/requests/{rid}/messages", json={"text": "Какая франшиза?"}).json()["answer"]
    assert out["mode"] == "extractive"                       # пример без даты — дата чата любая, но ссылок нет


# ------------------------------------------------------------------ п. 6: блокировка запроса

def test_answer_message_appends_user_message_under_request_lock():
    c, svc, _ = make_client(engine=FakeEngine())
    _chat, rid = new_request(c)
    req, chat, text = svc.prepare_message(rid, {"text": "Вопрос"})
    done = threading.Event()

    def run() -> None:
        svc.answer_message(req, chat, text)
        done.set()

    with svc._locked(rid):
        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.3)
        assert svc.app.request(rid).messages == []            # ждёт блокировку, ничего не дописал
    t.join(5)
    assert done.is_set()
    assert [m.role for m in svc.app.request(rid).messages] == ["user", "assistant"]


def test_choose_clinic_stale_after_new_question_is_409():
    from dms_core.schemas import ClinicChoice, Message

    c, svc, _ = make_client(engine=FakeEngine())
    _chat, rid = new_request(c, {"contract_id": "c01", "program": "Стандарт"})
    import datetime as dt

    row = next(iter(svc.kit.clinics("c01", program="Стандарт")))
    now = dt.datetime.now(dt.UTC)
    ans = Answer(scenario=Scenario.sc01, context=AnswerContext(contract_id="c01", program="Стандарт"), summary="",
                 confidence=Confidence.confirmed,
                 clinic_choices=[ClinicChoice(contract_id="c01", program="Стандарт", asked="Клиника", candidates=[row])])
    svc.app.add_message(rid, Message(role="user", text="Клиника в программе?", created_at=now))
    svc.app.add_message(rid, Message(role="assistant", answer=ans, created_at=now))
    # новый вопрос уже записан (ответ на него ещё считается) — выбор по прошлому ответу устарел
    svc.app.add_message(rid, Message(role="user", text="Новый вопрос", created_at=now))
    r = c.post(f"/api/requests/{rid}/clinic", json={"contract_id": "c01", "choice": row.id})
    assert r.status_code == 409 and r.json()["error"]["code"] == "clinic_choice_stale"
    _ru(r)
    assert len(svc.app.request(rid).messages) == 3            # ничего не дописано



def test_short_reply_to_program_question_continues_original_question():
    """«Стандарт» в ответ на «Какая программа?» — движку уходит исходный вопрос с программой (followup.py, w16);
    на экране у сообщения менеджера остаётся его собственный текст."""
    from dms_core.constants import Confidence, Scenario
    from dms_core.schemas import Answer, AnswerContext

    asked: list[str] = []

    def engine(question, context, *, llm=None, store=None):
        asked.append(question)
        conf = Confidence.need_clarification if len(asked) == 1 else Confidence.confirmed
        return Answer(scenario=Scenario.sc01, context=AnswerContext(contract_id="c01"), summary="s",
                      confidence=conf, questions=["Какая программа у застрахованного?"] if len(asked) == 1 else [])

    c, _svc, _tmp = make_client(engine=engine, llm=object())
    _chat, rid = new_request(c)
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Отказали в МРТ — правомерно?"}).status_code == 200
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Стандарт"}).status_code == 200
    assert asked == ["Отказали в МРТ — правомерно?", "Отказали в МРТ — правомерно? По программе «Стандарт»."]
    msgs = c.get(f"/api/requests/{rid}").json()["messages"]
    assert msgs[2]["text"] == "Стандарт" and msgs[2]["asked"].endswith("«Стандарт».")
