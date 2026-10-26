# Эталонные кейсы — схема записи (файлы — в `data/derived/gold/`, вне git, ADR 0008)

Одна строка JSONL = один кейс:

```json
{
  "id": "K12" | "own-gk3-07",
  "source": "organizers" | "own",
  "scenario": "sc01" | "sc02_info" | "sc02_clinic" | "sc02_add_clinic",
  "context": {"group_id": "gk3", "contract_id": "c09" | null, "program": "…" | null, "date": "2026-10-19" | null},
  "question": "текст обращения менеджера",
  "expected": {
    "label": "allowed" | "denied" | "conditional" | "insufficient" | null,
    "label_note": "есть альтернатива" | "нужно подтверждение СК" | null,
    "confidence": "confirmed" | "need_clarification" | "contradictory" | "insufficient",
    "key_facts": ["короткое проверяемое утверждение", "…"],
    "required_citations": [{"contract_id": "c09", "doc_type": "program", "clause": "3.1.4" | null,
                            "table_ref": null, "quote_hint": "до 15 слов"}],
    "must_not": ["утверждение, которого быть не должно (перенос из другого договора и т. п.)"],
    "questions": ["уточняющий вопрос, если ожидается"],
    "automation": {"branch": "insurer_free" | "insurer_paid" | null, "states": ["not_in_program", "…"]} | null
  },
  "reference_answer": "текст эталонного ответа (только для К10–К19)",
  "trap": "перенос | нет документа | противоречие | инструкция в документе | …" | null,
  "notes": "откуда взят ответ: документ → пункт"
}
```

Совпадение ссылок засчитывается по родителю/потомку пункта («3.1.4» ↔ «3.1.4.6», план §3.2).

Необязательные поля (пишет `scripts/import_gold.py`, этап w1-gold; схема проверяется `tests/eval/test_gold_schema.py`):
- в записи: `client_message` (обращение как от клиента), `difficulty`, `insurer`, `contracts` (договоры обязательных
  ссылок), `gold_refs` и `checks` (колонки эталона «Документы и пункты», «Что проверяет кейс»);
- в `expected`: `label_raw` (вывод как в эталоне), `per_contract` (кейс по ГК: итог по каждому договору);
- в ссылке: `clause_gold` (номер, как в эталоне — бывает грубее), `subitem` («7)» — подпункт, набранный текстом),
  `path` (путь для программ Ренессанса: нумерация заново в каждой подпрограмме), `program`, `clause_id`
  (`<doc_id>#p<абзац>`). `table_ref` — `<doc_id>#t<таблица>r<строка>` (docx) или `<doc_id>#<лист>!<строка>` (xlsx).

Рядом — `acceptance_k10_k19.json`: по каждому кейсу пункты/строки, которые инструменты волны 1 обязаны найти
(`ref`, `quote_hint`, `also`, `required`), все проверены по документам комплекта при импорте. Ручная разметка —
`k10_k19_manual.yaml` (там же, вне git).
