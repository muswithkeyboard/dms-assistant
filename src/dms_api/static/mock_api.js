/* Демо-API для разработки интерфейса и скриншотов. Подключается ТОЛЬКО флагом ?mock=1 (common.js); без флага
   страница ходит в настоящий API (docs/CONTRACTS.md §8). Ответы — по схемам src/dms_core/schemas.py.
   Все данные выдуманы: группы, договоры, пункты и клиники — не из комплекта организаторов (`docs/rules.md` п. 4).
   Состояние — в sessionStorage (в try/catch), чтобы обновление страницы показывало ту же историю.
   Флаги: &seed=empty — без готовых чатов; &slow=1 — медленные стадии (для снимка индикатора);
   &fail=clinic — выбор клиники отвечает 422, &fail=find — поиск по перечню отвечает 422 (снимки ошибок);
   &noreply=1 — ответы без Answer.reply (прежний вид карточки, обратная совместимость ADR 0014). */
(function () {
  "use strict";
  var params = location.search.slice(1);
  var NOREPLY = /(^|&)noreply=1(&|$)/.test(params);
  var KEY = "dms-mock-v3" + (NOREPLY ? "-noreply" : "");
  var SLOW = /(^|&)slow=1(&|$)/.test(params) ? 1600 : 380;
  var EMPTY = /(^|&)seed=empty(&|$)/.test(params);
  var NOW = "2026-10-24T10:00:00";
  var seq = 100;
  function id(p) { seq += 1; return p + seq; }
  function now() { return new Date().toISOString(); }
  function clone(x) { return JSON.parse(JSON.stringify(x)); }

  // ---------- справочник: группы, договоры, документы, пункты, клиники ----------
  var GROUPS = [
    { id: "g1", name: "ГК «Северный ветер»", contracts: [
      { id: "c1", legal_entity: "ООО «Северный ветер — Логистика»", number: "ДМС-24/101", insurer: "zetta", programs: ["Стандарт", "Москва VIP"] },
      { id: "c2", legal_entity: "АО «Северный ветер — Торг»", number: "ДМС-24/102", insurer: "zetta", programs: ["Стандарт"] }
    ] },
    { id: "g2", name: "ГК «Пример»", contracts: [
      { id: "c3", legal_entity: "ООО «Пример-Сервис»", number: "7700-ДМС-15", insurer: "sogaz", programs: ["Базовая", "Расширенная"] }
    ] }
  ];
  function contractOf(cid) {
    var out = null;
    GROUPS.forEach(function (g) { g.contracts.forEach(function (c) { if (c.id === cid) { out = { g: g, c: c }; } }); });
    return out;
  }
  function doc(id, cid, gid, title, type, status, warnings, fmt) {
    return { id: id, contract_id: cid, group_id: gid, title: title, doc_type: type, precedence_level: { contract: 1, amendment: 0, program: 2, clinics: 3, memo: 4, rules: 5, catalog: null }[type],
      source_path: "demo/" + id + "." + (fmt || "docx"), original_format: fmt || "docx", sha256: "9f2c4e1ab07d33c58e6f1a2b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f",
      uploaded_by: "operator", uploaded_at: "2026-10-23T18:20:00", status: status, warnings: warnings || [] };
  }
  var DOCS = [
    doc("d1", "c1", "g1", "Договор ДМС-24/101", "contract", "ready"),
    doc("d2", "c1", "g1", "Программа ДМС «Стандарт»", "program", "ready"),
    doc("d3", "c1", "g1", "Перечень клиник программ", "clinics", "ready", ["В 3 строках не указан адрес филиала"], "xlsx"),
    doc("d4", "c1", "g1", "Памятка застрахованному", "memo", "indexed"),
    doc("d5", "c2", "g1", "Договор ДМС-24/102", "contract", "ready"),
    doc("d6", "c2", "g1", "Программа ДМС «Стандарт»", "program", "checked", ["Приложение 1 упомянуто в договоре, но не предоставлено"]),
    doc("d7", null, "g1", "Правила страхования СК", "rules", "ready", [], "pdf"),
    doc("d8", null, "g1", "Каталог клиник страховщика", "catalog", "ready", ["Каталог страховщика — не условия договора"], "xlsx"),
    doc("d9", "c3", "g2", "Договор 7700-ДМС-15", "contract", "error", ["Файл повреждён: не удалось извлечь текст. Загрузите файл заново."])
  ];
  var CLAUSES = {
    d2: [
      { number: "1.1", path: "Программа «Стандарт» › Общие положения", text: "Программа определяет объём медицинской помощи застрахованным по договору и порядок её получения." },
      { number: "2.1", path: "Программа «Стандарт» › Амбулаторная помощь", text: "Приём врачей-специалистов, лабораторная и инструментальная диагностика в медицинских организациях из перечня программы." },
      { number: "4.2", path: "Программа «Стандарт» › Диагностика", text: "Магнитно-резонансная томография (МРТ) и компьютерная томография оказываются по направлению лечащего врача медицинской организации из перечня программы при наличии медицинских показаний." },
      { number: "4.3", path: "Программа «Стандарт» › Диагностика", text: "Повторное исследование той же области в течение 30 дней оплачивается при согласовании со страховщиком." },
      { number: "6.1", path: "Программа «Стандарт» › Исключения", text: "Не оплачиваются услуги, оказанные без направления врача, если направление предусмотрено программой." },
      { number: "6.4", path: "Программа «Стандарт» › Исключения", text: "Ортопедическая стоматология и имплантация зубов не входят в программу." }
    ],
    d6: [
      { number: "4.2", path: "Программа «Стандарт» › Диагностика", text: "Магнитно-резонансная томография оказывается по направлению врача и по согласованию со страховщиком." }
    ]
  };
  var CLINICS = {
    cr1: { id: "cr1", contract_id: "c1", doc_id: "d3", program: "Стандарт", care_type: "Диагностика (МРТ, КТ)", clinic_name: "Клиника «Диагност-Центр»", legal_name: "ООО «Диагност-Центр»", address: "ул. Примерная, 12", city: "Москва", code: "DC-01", franchise_pct: null, by_referral: true, access_note: "По направлению врача", source_ref: "Лист1!B57" },
    cr2: { id: "cr2", contract_id: "c1", doc_id: "d3", program: "Стандарт", care_type: "Диагностика (МРТ, КТ)", clinic_name: "Медицинский центр «Озеро»", legal_name: "АО «Озеро-Мед»", address: "пр-т Тестовый, 4, стр. 2", city: "Москва", code: "OZ-02", franchise_pct: "—", by_referral: true, access_note: null, source_ref: "Лист1!B61" },
    // Похожие названия — для карточки «Уточните клинику» (ADR 0010): менеджер различает строки по адресу и виду помощи.
    cr3: { id: "cr3", contract_id: "c1", doc_id: "d3", program: "Стандарт", care_type: "Поликлиника | Диагностика", clinic_name: "Клиника «Диагност-Центр Плюс»", legal_name: "ООО «ДЦ Плюс»", address: "ш. Учебное, 88", city: "Москва", code: "DC-07", franchise_pct: 10, by_referral: false, access_note: null, source_ref: "Лист1!B63" },
    cr4: { id: "cr4", contract_id: "c1", doc_id: "d3", program: "Стандарт", care_type: "Стоматология", clinic_name: "Диагност-Дент", legal_name: null, address: "ул. Образцовая, 3", city: "Москва", code: null, franchise_pct: null, by_referral: false, access_note: null, source_ref: "Лист1!B70" }
  };
  // Сеть с 44 филиалами (круг w7: поиск по перечню, «Показать ещё») — выдуманные улицы и города; строки
  // cn1…cn44 — в той же программе «Стандарт» договора c1, их можно выбрать тем же POST …/clinic.
  (function () {
    var streets = ["ул. 1905 года, 7", "ул. Лесная, 5", "пр-т Мира, 101", "ул. Садовая, 14", "наб. Речная, 2", "ул. Школьная, 33",
      "пер. Тихий, 9", "ул. Полевая, 18", "б-р Зелёный, 6", "ул. Новая, 40", "ш. Северное, 77"];
    var cities = ["Москва", "Москва", "Москва", "Санкт-Петербург", "Казань", "Тверь", "Москва", "Екатеринбург"];
    for (var i = 1; i <= 44; i++) {
      CLINICS["cn" + i] = { id: "cn" + i, contract_id: "c1", doc_id: "d3", program: "Стандарт", care_type: i % 3 ? "Поликлиника | Диагностика" : "Диагностика (МРТ, КТ)",
        clinic_name: "Клиника «Диагност-Центр»", legal_name: "ООО «Диагност-Центр Сеть»", address: streets[i % streets.length].replace(/\d+$/, function (n) { return String(Number(n) + Math.floor(i / streets.length)); }),
        city: cities[i % cities.length], code: "DCN-" + i, franchise_pct: i % 4 ? null : 10, by_referral: i % 2 === 0, access_note: null, source_ref: "Лист1!B" + (200 + i) };
    }
  })();
  var CLINIC_SEARCH_PAGE = 20;   // constants.CLINIC_SEARCH_PAGE
  // Поиск по перечню программы (CONTRACTS §8): все слова запроса должны встретиться в названии, юрлице, городе
  // или адресе (без учёта регистра и кавычек); пустой запрос — весь перечень по порядку.
  function searchClinics(cid, program, q, offset) {
    var words = String(q || "").toLowerCase().replace(/[«»"“”]/g, " ").split(/[\s,]+/).filter(Boolean);
    var all = Object.keys(CLINICS).map(function (k) { return CLINICS[k]; }).filter(function (r) {
      if (r.contract_id !== cid || r.program !== program) { return false; }
      var hay = [r.clinic_name, r.legal_name, r.city, r.address].join(" ").toLowerCase().replace(/[«»"“”]/g, " ");
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    });
    return { rows: all.slice(offset, offset + CLINIC_SEARCH_PAGE), total: all.length, offset: offset };
  }
  function clauseObj(docId, c) {
    var d = DOCS.filter(function (x) { return x.id === docId; })[0] || {};
    return { id: docId + ":" + c.number, doc_id: docId, contract_id: d.contract_id || null, number: c.number, path: c.path, program: "Стандарт", text: c.text, footnotes: [], page: null, text_hash: "" };
  }
  function cite(docId, number, quote) {
    var d = DOCS.filter(function (x) { return x.id === docId; })[0];
    return { doc_id: docId, doc_title: d.title, contract_id: d.contract_id, clause_number: number, table_ref: null, page: null, quote: quote, verified: true, offsets: null };
  }
  function citeRow(row) {
    return { doc_id: row.doc_id, doc_title: "Перечень клиник программ", contract_id: row.contract_id, clause_number: null, table_ref: row.source_ref, page: null, quote: row.clinic_name, verified: true, offsets: null };
  }

  // ---------- ответы (выдуманные, по ключевым словам вопроса) ----------
  function ctxOf(chat) { var c = chat.context; return { group_id: c.group_id, contract_id: c.contract_id, program: c.program, date: c.date }; }
  function llm() { return { provider: "mock", model: "demo", prompt_version: "system v1", tool_calls: 4 }; }
  // Ответ с выбором клиники (CONTRACTS §8.1): по каждому договору контекста — кандидаты из перечня программы;
  // picks[cid] — выбор менеджера (id строки | "none"). Без выбора метки «можно» / «нет в перечне» нет.
  var ASKED = "Диагност Центр";
  function clinicAnswer(chat, base, picks) {
    picks = picks || {};
    var cids = chat.context.contract_id ? [chat.context.contract_id] : (GROUPS.filter(function (g) { return g.id === chat.context.group_id; })[0] || { contracts: [] }).contracts.map(function (c) { return c.id; });
    var choices = cids.map(function (cid) {
      return { contract_id: cid, program: chat.context.program || "Стандарт", asked: ASKED,
        candidates: cid === "c1" ? [CLINICS.cr1, CLINICS.cr3, CLINICS.cr2, CLINICS.cr4] : [], selected: picks[cid] || null };
    });
    function verdict(cid) {
      var pick = picks[cid], row = pick && pick !== "none" ? CLINICS[pick] : null;
      if (row) {
        return { label: "conditional", summary: "Можно при условиях: клиника «" + row.clinic_name.replace(/^Клиника\s+/, "").replace(/[«»]/g, "") + "» (" + row.address + ") есть в перечне программы; МРТ — по направлению врача.",
          statements: [{ text: "Клиника есть в перечне программы: " + row.care_type + ".", kind: "fact", citations: [citeRow(row)] },
            { text: "МРТ оказывается по направлению лечащего врача медицинской организации из перечня программы.", kind: "condition", citations: [cite("d2", "4.2", "по направлению лечащего врача медицинской организации из перечня программы")] }] };
      }
      if (pick === "none") {
        return { label: "denied", summary: "Клиники нет в перечне программы — по договору она не оплачивается.",
          statements: [{ text: "Перечень клиник программы не содержит клинику из вопроса (отмечено менеджером).", kind: "fact", citations: [cite("d2", "2.1", "в медицинских организациях из перечня программы")] }] };
      }
      return { label: "insufficient", summary: "Нужно подтвердить клинику: в перечне программы есть похожие строки.", statements: [] };
    }
    var out = Object.assign(base, { scenario: "sc01", clinic_choices: choices, confidence: "need_clarification" });
    if (chat.context.contract_id) {
      var v = verdict(chat.context.contract_id);
      out.label = v.label; out.summary = v.summary; out.statements = v.statements;
      if (v.label !== "insufficient") { out.confidence = "confirmed"; }
      if (v.label === "denied") { out.alternatives = [{ clinic: CLINICS.cr1, reason: "В перечне программы «Стандарт», вид помощи — диагностика." }]; out.suggested_automation = "add_clinic"; }
      if (v.label === "insufficient") { out.missing = ["Какая строка перечня — та клиника, о которой спрашивают"]; }
      return out;
    }
    out.per_contract = cids.map(function (cid) { var v = verdict(cid); return { contract_id: cid, label: v.label, summary: v.summary, statements: v.statements }; });
    out.label = null;
    out.summary = "Клинику нужно подтвердить по каждому договору группы: условия договоров не смешиваются.";
    return out;
  }
  // ---------- ответ словами (ADR 0014, CONTRACTS §12: Answer.reply) ----------
  // Сервер собирает reply кодом из проверенных утверждений; мок — так же: пункты из statements (без «ограничений»),
  // цитата — проверенная цитата ссылки, refs — «документ:пункт» или ссылка на строку таблицы; lead — по метке.
  function refOf(c) { return c.table_ref || (c.doc_id + ":" + c.clause_number); }
  var CAUTION = "Ответ — по документам договора. Решение по конкретному случаю принимает страховая компания.";
  function replyFor(text, a) {
    var q = text.toLowerCase(), points = [];
    function add(st, cid) {
      var cs = st.citations || [];
      if (st.kind === "limitation" || !cs.length) { return; }
      points.push({ text: st.text, quote: cs[0].quote, refs: cs.map(refOf), contract_id: cid || cs[0].contract_id || null });
    }
    (a.per_contract || []).forEach(function (pc) { (pc.statements || []).forEach(function (st) { add(st, pc.contract_id); }); });
    (a.statements || []).forEach(function (st) { add(st); });
    var pending = (a.clinic_choices || []).some(function (ch) { return !ch.selected; });
    var ask = (a.questions || []).slice();
    var lead;
    if (pending) {
      lead = "Нужно уточнить клинику: в перечне программы есть несколько похожих. Выберите, о какой спрашивает сотрудник, — и я проверю условия.";
    } else if (!a.context.contract_id && (a.per_contract || []).length && !(a.clinic_choices || []).length) {
      lead = "Зависит от договора: по ДМС-24/101 МРТ — по направлению лечащего врача, по ДМС-24/102 — ещё и по согласованию со страховщиком.";
    } else if (!a.context.contract_id && (a.clinic_choices || []).length) {
      lead = "По договорам группы по-разному: в каждом договоре клиника проверяется по своему перечню.";
    } else if (a.scenario === "sc02_clinic") {
      lead = "Нет: клиники «Медцентр Плюс» нет в перечне программы «Стандарт». В перечне есть две клиники, где МРТ делают по направлению врача.";
    } else if (a.label === "denied" && /имплант|зуб|стомат/.test(q)) {
      lead = "Нет, не входит: имплантация зубов — в исключениях программы «Стандарт». Отказ клиники обоснован.";
    } else if (a.label === "denied") {
      lead = "Нет: клиники нет в перечне программы, по договору её услуги не оплачиваются. Ниже — клиники из перечня.";
    } else if (a.label === "conditional" && /отказ/.test(q)) {
      lead = "Отказ обоснован, только если у сотрудника не было направления врача: МРТ входит в программу «Стандарт» по направлению лечащего врача клиники из перечня.";
    } else if (a.label === "conditional") {
      lead = "Да, при условии: " + (a.summary || "").replace(/^Можно при условиях:\s*/, "");
    } else if (a.label === "allowed") {
      lead = "Да, входит: " + (a.summary || "").replace(/^Можно:\s*/, "");
    } else {
      lead = "Пока не могу ответить: в вопросе нет услуги и клиники, а условия договора зависят от них.";
      if (!ask.length) { ask.push("О какой услуге и какой клинике идёт речь?"); }
      ask.push("Что именно написала клиника в отказе?");
    }
    var caution = (a.limitations || [])[0] || CAUTION;
    return { lead: lead, points: points.slice(0, 6), ask: ask, caution: caution };
  }
  function answerFor(text, chat, picks) {
    var a = answerCore(text, chat, picks);
    a.reply = NOREPLY ? null : replyFor(text, a);
    return a;
  }
  function answerCore(text, chat, picks) {
    var q = text.toLowerCase(), base = { context: ctxOf(chat), mode: "live", llm: llm(), processing_ms: 2140,
      per_contract: [], statements: [], comparisons: [], limitations: [], missing: [], questions: [], alternatives: [], clinic_choices: [], suggested_automation: null, label: null, label_note: null };
    if (/диагност/.test(q)) { return clinicAnswer(chat, base, picks); }
    if (!chat.context.contract_id) {
      return Object.assign(base, {
        scenario: "sc02_info", confidence: "contradictory",
        summary: "Условия МРТ в договорах группы различаются: в одном — по направлению врача, в другом — ещё и по согласованию со страховщиком.",
        per_contract: [
          { contract_id: "c1", label: "conditional", summary: "МРТ — по направлению лечащего врача из перечня программы.", statements: [
            { text: "МРТ оказывается по направлению лечащего врача при наличии медицинских показаний.", kind: "condition", citations: [cite("d2", "4.2", "по направлению лечащего врача медицинской организации из перечня программы")] }] },
          { contract_id: "c2", label: "conditional", summary: "МРТ — по направлению врача и по согласованию со страховщиком.", statements: [
            { text: "Требуется согласование со страховщиком.", kind: "condition", citations: [cite("d6", "4.2", "по согласованию со страховщиком")] }] }
        ],
        comparisons: [{ text: "Согласование со страховщиком требуется только по договору ДМС-24/102; по ДМС-24/101 достаточно направления.", kind: "comparison",
          citations: [cite("d2", "4.2", "по направлению лечащего врача"), cite("d6", "4.2", "по согласованию со страховщиком")] }],
        limitations: ["Приложение 1 к договору ДМС-24/102 не предоставлено — его условия не учтены."]
      });
    }
    if (/имплант|зуб|стомат/.test(q)) {
      return Object.assign(base, { scenario: "sc01", label: "denied", confidence: "confirmed",
        summary: "Отказ обоснован: имплантация зубов — в исключениях программы «Стандарт».",
        statements: [{ text: "Ортопедическая стоматология и имплантация в программу не входят.", kind: "fact", citations: [cite("d2", "6.4", "Ортопедическая стоматология и имплантация зубов не входят в программу")] }],
        limitations: ["Проверены программа «Стандарт» и исключения; дополнительные соглашения к договору не загружены."] });
    }
    if (/медцентр|прикреп|в программе\?/.test(q)) {
      return Object.assign(base, { scenario: "sc02_clinic", confidence: "confirmed", label_note: "есть альтернатива",
        summary: "Клиники «Медцентр Плюс» нет в перечне программы «Стандарт» для диагностики. В программе есть две клиники, где МРТ доступна по направлению врача.",
        statements: [{ text: "МРТ входит в программу по направлению врача.", kind: "condition", citations: [cite("d2", "4.2", "по направлению лечащего врача")] },
          { text: "Клиника «Диагност-Центр» есть в перечне программы для диагностики.", kind: "fact", citations: [citeRow(CLINICS.cr1)] }],
        alternatives: [{ clinic: CLINICS.cr1, reason: "В перечне программы «Стандарт», вид помощи — диагностика; по направлению врача." },
          { clinic: CLINICS.cr2, reason: "В перечне программы «Стандарт», вид помощи — диагностика." }],
        suggested_automation: "add_clinic" });
    }
    if (/узи|анализ|при[её]м врача|кардиолог/.test(q)) {
      return Object.assign(base, { scenario: "sc01", label: "allowed", confidence: "confirmed",
        summary: "Можно: приём врачей-специалистов и лабораторная и инструментальная диагностика входят в амбулаторную помощь программы «Стандарт» в клиниках из перечня.",
        statements: [{ text: "Амбулаторная помощь включает приём врачей-специалистов и диагностику в клиниках из перечня программы.", kind: "fact",
          citations: [cite("d2", "2.1", "Приём врачей-специалистов, лабораторная и инструментальная диагностика в медицинских организациях из перечня программы")] }] });
    }
    if (/мрт|томограф/.test(q)) {
      return Object.assign(base, { scenario: "sc01", label: "conditional", confidence: "confirmed",
        summary: "Можно при условиях: МРТ входит в программу «Стандарт» по направлению лечащего врача клиники из перечня; исключений для МРТ нет.",
        statements: [
          { text: "МРТ оказывается по направлению лечащего врача медицинской организации из перечня программы.", kind: "condition", citations: [cite("d2", "4.2", "по направлению лечащего врача медицинской организации из перечня программы")] },
          { text: "Без направления, если оно предусмотрено программой, услуга не оплачивается.", kind: "limitation", citations: [cite("d2", "6.1", "Не оплачиваются услуги, оказанные без направления врача")] },
          { text: "Повторное МРТ той же области в течение 30 дней — по согласованию со страховщиком.", kind: "condition", citations: [cite("d2", "4.3", "оплачивается при согласовании со страховщиком")] }
        ],
        questions: ["Было ли у сотрудника направление врача клиники из перечня?"] });
    }
    return Object.assign(base, { scenario: "general", confidence: "need_clarification", label: "insufficient", mode: "extractive", llm: null,
      summary: "Недостаточно данных: не указаны услуга и клиника. Найденные пункты показаны без вывода.",
      missing: ["Услуга или вид обращения", "Клиника"], questions: ["О какой услуге и какой клинике идёт речь?"] });
  }

  // ---------- черновики (выдуманные тексты; настоящие — config/texts_ru/drafts.yaml на сервере) ----------
  var TPL = {
    hr_no_clinic: { to: "hr", subject: "Клиника {clinic} в программе {program}", body: "Добрый день!\n\nПо договору {contract} ({legal_entity}) клиника {clinic} не входит в программу {program} для обращения «{care_type}».\nВ программе есть клиники, где можно получить эту помощь:\n— Клиника «Диагност-Центр», Москва, ул. Примерная, 12\n— Медицинский центр «Озеро», Москва, пр-т Тестовый, 4, стр. 2\n\nЕсли сотруднику важна именно эта клиника, мы можем запросить у страховой компании её добавление в программу. Сообщите, пожалуйста, нужно ли это сделать." },
    insurer_add_request: { to: "insurer", subject: "Запрос о добавлении клиники {clinic} в программу {program}", body: "Добрый день!\n\nПросим рассмотреть возможность добавления клиники {clinic} ({address}) в программу {program} по договору {contract} ({legal_entity}) для обращения «{care_type}».\nПросим сообщить, возможно ли добавление и на каких условиях." },
    hr_clinic_added: { to: "hr", subject: "Клиника {clinic} добавлена в программу {program}", body: "Добрый день!\n\nСтраховая компания подтвердила: клиника {clinic} добавлена в программу {program} по договору {contract}." },
    hr_paid_terms: { to: "hr", subject: "Условия добавления клиники {clinic}", body: "Добрый день!\n\nСтраховая компания готова добавить клинику {clinic} в программу {program} на следующих условиях:\n{insurer_terms}\n\nСообщите, пожалуйста, согласны ли вы с этими условиями." },
    insurer_confirm_paid: { to: "insurer", subject: "Подтверждение условий добавления клиники {clinic}", body: "Добрый день!\n\nСтрахователь {legal_entity} согласен с условиями добавления клиники {clinic} в программу {program} по договору {contract}. Просим подтвердить добавление." }
  };
  // Копия transitions и drafts из config/add_clinic.yaml (JSON между метками; tests/ui сверяет с конфигом).
  // «Клиника добавлена» — только insurer_free и insurer_confirmed (после подтверждения СК, ADR 0004).
  var FLOW = /* flow:begin */{"alt_found": {"close": "closed"}, "insufficient": {"close": "closed"}, "not_in_program": {"hr_wants_add": "hr_wants_add", "close": "closed"}, "hr_wants_add": {"insurer_request_ready": "awaiting_insurer", "close": "closed"}, "awaiting_insurer": {"insurer_free": "insurer_free", "insurer_paid": "insurer_paid", "close": "closed"}, "insurer_free": {"close": "closed"}, "insurer_paid": {"hr_agreed": "hr_agreed", "hr_declined": "hr_declined", "close": "closed"}, "hr_agreed": {"insurer_confirmed": "insurer_confirmed", "close": "closed"}, "insurer_confirmed": {"close": "closed"}, "hr_declined": {"close": "closed"}}/* flow:end */;
  var STAGE_DRAFTS = /* drafts:begin */{"not_in_program": ["hr_no_clinic"], "hr_wants_add": ["insurer_add_request"], "insurer_free": ["hr_clinic_added"], "insurer_paid": ["hr_paid_terms"], "hr_agreed": ["insurer_confirm_paid"], "insurer_confirmed": ["hr_clinic_added"]}/* drafts:end */;
  function makeDraft(tplId, stage, fields) {
    var t = TPL[tplId], unknown = [];
    function fill(s) { return s.replace(/\{(\w+)\}/g, function (m, k) { if (fields[k]) { return fields[k]; } if (unknown.indexOf(k) < 0) { unknown.push(k); } return "[ЗАПОЛНИТЬ]"; }); }
    return { id: id("dr"), to: t.to, stage: stage, subject: fill(t.subject), body: fill(t.body), fields: fields, unknown_fields: unknown, created_at: now(), edited_at: null, auto_send: false };
  }
  function draftFields(req, chat) {
    var cc = contractOf(chat.context.contract_id) || { c: {} };
    return { clinic: req.automation && req.automation.clinic ? "«" + req.automation.clinic + "»" : "", program: "«" + (chat.context.program || "Стандарт") + "»",
      contract: cc.c.number ? "№ " + cc.c.number : "", legal_entity: cc.c.legal_entity || "", care_type: "диагностика (МРТ)",
      insurer_terms: req._insurer_terms || "" };
  }

  // ---------- состояние ----------
  function seed() {
    var db = { chats: [], requests: [] };
    if (EMPTY) { return db; }
    var ch1 = { id: "ch1", title: "ООО «Северный ветер — Логистика» · Стандарт", context: { group_id: "g1", contract_id: "c1", program: "Стандарт", date: "2026-10-24" }, created_at: "2026-10-24T09:00:00" };
    var ch2 = { id: "ch2", title: "ГК «Северный ветер»", context: { group_id: "g1", contract_id: null, program: null, date: "2026-10-24" }, created_at: "2026-10-24T09:30:00" };
    db.chats.push(ch2, ch1);
    function req(rid, chat, title, q, extra) {
      var a = answerFor(q, chat);
      var r = Object.assign({ id: rid, chat_id: chat.id, title: title, scenario: a.scenario, status: "open", rating: null, rating_comment: null, slots: {}, automation: null, drafts: [],
        messages: [{ role: "user", text: q, answer: null, created_at: NOW }, { role: "assistant", text: null, answer: a, created_at: NOW }] }, extra || {});
      db.requests.push(r);
      return r;
    }
    req("r1", ch1, "Отказ в МРТ колена", "Клиника отказала сотруднику в МРТ колена. Обоснованно?");
    var r2 = req("r2", ch1, "Клиника «Медцентр Плюс»", "Сотрудник хочет делать МРТ в клинике «Медцентр Плюс». Она есть в программе?", { slots: { clinic: "Медцентр Плюс" } });
    r2.automation = { kind: "add_clinic", state: "not_in_program", clinic: "Медцентр Плюс", history: [{ to: "not_in_program", at: NOW }] };
    r2._insurer_terms = "доплата 4 500 ₽ за каждого застрахованного, прикреплённого к клинике";
    r2.drafts.push(makeDraft("hr_no_clinic", "not_in_program", draftFields(r2, ch1)));
    ["hr_wants_add", "insurer_request_ready", "insurer_paid"].forEach(function (act) { applyStep(r2, ch1, act, act === "insurer_paid" ? "Добавление возможно, доплата 4 500 ₽ за каждого прикреплённого." : null); });
    req("r3", ch1, "Имплантация зуба", "Сотруднику отказали в имплантации зуба. Это законно?", { status: "closed", rating: 4, rating_comment: "Верно, пункт найден." });
    req("r4", ch2, "МРТ по договорам группы", "На каких условиях МРТ в договорах группы?");
    req("r5", ch1, "МРТ в «Диагност Центре»", "Можно ли сотруднику сделать МРТ в «Диагност Центре»?", { _clinic_q: "Можно ли сотруднику сделать МРТ в «Диагност Центре»?" });
    req("r6", ch2, "«Диагност Центр» по группе", "Входит ли «Диагност Центр» в программы договоров группы?", { _clinic_q: "Входит ли «Диагност Центр» в программы договоров группы?" });
    return db;
  }
  function applyStep(req, chat, action, reply) {
    var from = req.automation.state, to = (FLOW[from] || {})[action];
    if (!to) { return false; }
    if (reply) { req.automation.history.push({ action: action, from: from, to: to, insurer_reply: { text: reply, kind: "insurer_reply" }, at: now() }); }
    else { req.automation.history.push({ action: action, from: from, to: to, at: now() }); }
    req.automation.state = to;
    (STAGE_DRAFTS[to] || []).forEach(function (tpl) { req.drafts.push(makeDraft(tpl, to, draftFields(req, chat))); });
    if (to === "closed") { req.status = "closed"; }
    return true;
  }
  var db;
  try { db = JSON.parse(sessionStorage.getItem(KEY + (EMPTY ? "-empty" : "")) || "null"); } catch (e) { db = null; }
  if (!db) { db = seed(); }
  function save() { try { sessionStorage.setItem(KEY + (EMPTY ? "-empty" : ""), JSON.stringify(db)); } catch (e) { /* без хранилища — до перезагрузки */ } }
  save();


  // ---------- подключение клиента (ADR 0011, CONTRACTS §9) ----------
  // Группы ugN и договоры uNN клиентов, их файлы и задания обработки — в том же sessionStorage. Задание идёт по
  // времени: received → extracted → checked → indexed → selfcheck → done (по ONB_STEP мс на стадию), договоры —
  // по одному (следующее задание ждёт в очереди). Флаг &fail=onboard — задание падает на проверке полноты.
  // Готовность: нет текста договора/программы — failed; есть перечень клиник и матрица (известная СК) — ready;
  // иначе limited. После done с ready/limited договор появляется в GET /api/groups и по нему создаётся чат.
  var ONB_STAGES = ["received", "extracted", "checked", "indexed", "selfcheck"];
  var ONB_STEP = SLOW > 1000 ? 2600 : 1300, ONB_QUEUE = 400;
  var FAIL_ONB = /(^|&)fail=onboard(&|$)/.test(params);
  var INSURER_RU = { zetta: "Зетта", renaissance: "Ренессанс", sogaz: "СОГАЗ", unknown: "не определена" };
  var DOC_TYPES = ["contract", "amendment", "program", "clinics", "memo", "rules", "catalog", "insurer_reply", "other"];
  function iso(ms) { return new Date(ms).toISOString(); }
  function guessType(name) {
    var n = name.toLowerCase();
    if (/перечен|клиник|лпу/.test(n)) { return "clinics"; }
    if (/программ/.test(n)) { return "program"; }
    if (/доп\S*\s*соглаш|^дс[\s_-]/.test(n)) { return "amendment"; }
    if (/памятк/.test(n)) { return "memo"; }
    if (/правил/.test(n)) { return "rules"; }
    if (/каталог/.test(n)) { return "catalog"; }
    if (/договор/.test(n)) { return "contract"; }
    return "other";
  }
  function fakeHash(s) {
    var h = 0, out = "";
    for (var i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) >>> 0; }
    while (out.length < 64) { out += h.toString(16); h = (h * 1103515245 + 12345) >>> 0; }
    return out.slice(0, 64);
  }
  function cfile(name, scope, size, at) {
    var ext = (name.split(".").pop() || "").toLowerCase();
    return { name: name, doc_type: guessType(name), scope: scope, fmt: ext, size: size, sha256: fakeHash(name + size), uploaded_at: at };
  }
  function detectInsurer(files) {
    var n = files.map(function (f) { return f.name.toLowerCase(); }).join(" ");
    return /зетт|zetta/.test(n) ? "zetta" : /ренессанс|renaissance/.test(n) ? "renaissance" : /согаз|sogaz/.test(n) ? "sogaz" : null;
  }
  function buildReport(j) {
    var files = j._files, types = files.map(function (f) { return f.doc_type; });
    var detected = detectInsurer(files), insurer = detected || j._hint || "unknown";
    var adapter = j._fg ? "generic" : { zetta: "zetta", renaissance: "renaissance", sogaz: "sogaz" }[insurer] || "generic";
    var textual = ["contract", "program", "amendment"];
    var hasText = types.some(function (t) { return textual.indexOf(t) >= 0; });
    var hasClinics = adapter !== "generic" && types.indexOf("clinics") >= 0, hasCare = adapter !== "generic" && types.indexOf("program") >= 0;
    var docs = files.map(function (f, i) { return { doc_id: j.contract_id + "-d" + (i + 1), title: f.name.replace(/\.[^.]+$/, ""), doc_type: f.doc_type, fmt: f.fmt }; });
    // Разбор без адаптера (generic): разметки договора, затем профили СК по подписи заголовка (ADR 0012 п. 4).
    var mapped = adapter === "generic" && hasText ? applyMappings(j.contract_id, docs) : { clinics: 0, tables: 0, care: 0, matrices: 0, warnings: [] };
    var readiness = !hasText ? "failed" : (hasClinics && hasCare) || (mapped.clinics && mapped.care) ? "ready" : "limited";
    var nProg = types.filter(function (t) { return t === "program"; }).length || 1;
    var warnings = [];
    if (j._hint && detected && j._hint !== detected) { warnings.push("Выбрана СК «" + INSURER_RU[j._hint] + "», по документам — «" + INSURER_RU[detected] + "». Используется определение по документам."); }
    if (j._fg && hasText) { warnings.push("Формат не распознавался по выбору оператора — договор разобран как неизвестная СК."); }
    else if (adapter === "generic" && hasText) { warnings.push("Страховая компания не распознана — разобраны только пункты договора (общий разбор), без матрицы видов помощи."); }
    warnings = warnings.concat(mapped.warnings);
    if (!hasClinics && !mapped.clinics && hasText) { warnings.push("Перечень клиник не распознан: ответы о клиниках будут «недостаточно данных». Разметьте таблицу перечня вручную."); }
    return {
      contract_id: j.contract_id, insurer: insurer, adapter: adapter, readiness: readiness,
      documents: files.map(function (f, i) {
        var w = f.doc_type === "other" ? ["Тип документа не определён по имени файла — выберите тип вручную и обработайте заново."]
          : f.doc_type === "catalog" ? ["Каталог страховщика — не условия договора, в ответах не используется."] : [];
        if (f.fmt === "pdf") { w.push("PDF: номера пунктов взяты из текста, проверьте нумерацию."); }
        return { doc_id: docs[i].doc_id, title: docs[i].title, doc_type: f.doc_type, fmt: f.fmt,
          status: readiness === "failed" ? (textual.indexOf(f.doc_type) < 0 ? "checked" : "error") : "ready", warnings: w };
      }),
      counts: readiness === "failed" ? { clauses: 0, numbered: 0, programs: 0, clinics: 0, care_rows: 0 }
        : { clauses: 140 + 41 * files.length, numbered: 128 + 39 * files.length, programs: nProg, clinics: hasClinics ? 214 : mapped.clinics,
            care_rows: hasCare ? 23 * nProg : mapped.care || 0, mapped_tables: mapped.tables, mapped_matrices: mapped.matrices || 0 },
      warnings: warnings,
      selfcheck: readiness === "failed" ? { asked: 0, cited: 0 } : readiness === "ready" ? { asked: 12, cited: 11 } : { asked: 6, cited: 4 }
    };
  }
  function seedClients() {
    var at = "2026-10-24T08:40:00.000Z", t0 = Date.parse("2026-10-24T08:45:00.000Z");
    var g = { id: "ug1", name: "ГК «Ромашка-Групп»", created_at: "2026-10-24T08:30:00.000Z", contracts: [
      { id: "u01", group_id: "ug1", legal_entity: "ООО «Ромашка-Производство»", insurer_hint: "zetta", number: "Z-26/0412", created_at: "2026-10-24T08:30:00.000Z" },
      { id: "u02", group_id: "ug1", legal_entity: "ООО «Ромашка-Ритейл»", insurer_hint: null, number: null, created_at: "2026-10-24T08:30:00.000Z" },
      // Неизвестная СК с перечнем в XLSX: подпись листа совпадает с профилем «Альфа-Страхование» — профиль применён.
      { id: "u03", group_id: "ug1", legal_entity: "ООО «Ромашка-Логистика»", insurer_hint: null, number: "АС-26/77", created_at: "2026-10-24T08:30:00.000Z",
        insurer_name: "Альфа-Страхование", force_generic: false }
    ] };
    var files = {
      u01: [cfile("Договор ДМС Зетта Z-26-0412.docx", "contract", 184320, at), cfile("Программа Стандарт.docx", "contract", 96256, at),
            cfile("Перечень клиник Москва.xlsx", "contract", 41984, at)],
      u02: [cfile("Договор_Ритейл_2026.pdf", "contract", 1258291, at), cfile("Программа ДМС.pdf", "contract", 402432, at)],
      u03: [cfile("Договор ДМС Логистика.docx", "contract", 152576, at), cfile("ЛПУ к договору.xlsx", "contract", 88064, at)]
    };
    var gfiles = { ug1: [cfile("Памятка застрахованному.pdf", "group", 220160, at)] };
    var jobs = ["u01", "u02", "u03"].map(function (cid, i) {
      return { id: "oj" + (i + 1), contract_id: cid, state: "running", stage: null, created_at: iso(t0), finished_at: null, report: null, error: null,
        _t0: t0, _fail: false, _hint: g.contracts[i].insurer_hint, _files: files[cid].concat(gfiles.ug1) };
    });
    return { groups: [g], files: files, gfiles: gfiles, jobs: jobs, seq: { g: 1, c: 3, j: 3 } };
  }
  if (!db.onb) { db.onb = EMPTY ? { groups: [], files: {}, gfiles: {}, jobs: [], seq: { g: 0, c: 0, j: 0 } } : seedClients(); save(); }
  var ONB = db.onb;
  function jobView(j) {
    if (j.state === "done" || j.state === "error") { return j; }
    var dt = Date.now() - j._t0, i = Math.floor((dt - ONB_QUEUE) / ONB_STEP);
    if (dt < ONB_QUEUE) { j.state = "queued"; j.stage = null; }
    else if (j._fail && i >= 2) {
      j.state = "error"; j.stage = "checked"; j.finished_at = iso(j._t0 + ONB_QUEUE + 2 * ONB_STEP);
      j.error = "Не удалось проверить полноту: в файлах не найден текст договора (возможно, скан без текстового слоя). Загрузите договор в DOCX или PDF с текстом и обработайте заново.";
    }
    else if (i >= ONB_STAGES.length) { j.state = "done"; j.stage = "selfcheck"; j.finished_at = iso(j._t0 + ONB_QUEUE + ONB_STAGES.length * ONB_STEP); j.report = buildReport(j); }
    else { j.state = "running"; j.stage = ONB_STAGES[i]; }
    save();
    return j;
  }
  function publicJob(j) { var c = clone(j); Object.keys(c).forEach(function (k) { if (k.charAt(0) === "_") { delete c[k]; } }); return c; }
  function lastJob(cid) { var js = ONB.jobs.filter(function (j) { return j.contract_id === cid; }); return js.length ? jobView(js[js.length - 1]) : null; }
  function onbContract(cid) {
    var out = null;
    ONB.groups.forEach(function (g) { g.contracts.forEach(function (c) { if (c.id === cid) { out = { g: g, c: c }; } }); });
    return out;
  }
  function allFiles(g, cid) { return (ONB.files[cid] || []).concat(ONB.gfiles[g.id] || []); }
  function publicGroup(g) {
    var out = clone(g);
    out.contracts.forEach(function (c) { c.files = clone(allFiles(g, c.id)); var j = lastJob(c.id); c.job = j ? publicJob(j) : null; });
    return out;
  }
  function newContract(g, b) {
    ONB.seq.c += 1;
    var c = { id: "u" + (ONB.seq.c < 10 ? "0" : "") + ONB.seq.c, group_id: g.id, legal_entity: String(b.legal_entity).trim(),
      insurer_hint: ["zetta", "renaissance", "sogaz"].indexOf(b.insurer) >= 0 ? b.insurer : null, number: b.number ? String(b.number).trim() : null, created_at: now(),
      insurer_name: b.insurer_name ? String(b.insurer_name).trim().slice(0, 120) : null, force_generic: !!b.force_generic };
    g.contracts.push(c); ONB.files[c.id] = [];
    return c;
  }
  // Подключённые договоры клиентов (done + ready/limited) — в справочнике групп, как договоры комплекта.
  function syncGroups() {
    GROUPS = GROUPS.filter(function (g) { return !/^ug/.test(g.id); });
    ONB.groups.forEach(function (g) {
      var cs = g.contracts.map(function (c) {
        var j = lastJob(c.id), rep = j && j.state === "done" ? j.report : null;
        if (!rep || (rep.readiness !== "ready" && rep.readiness !== "limited")) { return null; }
        return { id: c.id, legal_entity: c.legal_entity, number: c.number, insurer: rep.insurer, programs: rep.counts.programs > 1 ? ["Стандарт", "Расширенная"] : ["Стандарт"] };
      }).filter(Boolean);
      if (cs.length) { GROUPS.push({ id: g.id, name: g.name, contracts: cs }); }
    });
  }
  // ---------- разметка таблиц оператором (ADR 0012, CONTRACTS §10) ----------
  // Таблицы договора выдуманы и строятся по документам последней обработки: реквизиты (не перечень), перечень
  // в DOCX (строка заголовка — вторая, внутри повтор заголовка и строка без названия) и широкий лист XLSX
  // (10 колонок — для горизонтальной прокрутки). Подпись заголовка — нормализованные ячейки (регистр, ё, знаки).
  // Профиль СК «Альфа-Страхование» в начальных данных совпадает с подписью листа XLSX — «предложено профилем».
  // Обработка generic-договора применяет разметку договора, иначе профиль по подписи (used += 1).
  var PREVIEW_ROWS = 15, SAMPLE_ROWS = 10, MATRIX_ROWS = 20;   // TABLE_PREVIEW_ROWS, MAPPING_SAMPLE_ROWS, MATRIX_SAMPLE_ROWS (constants.py)
  var ROLES = ["clinic_name", "legal_name", "address", "city", "care_type", "program", "franchise_pct", "code", "by_referral", "access_note", "present", "ignore"];
  // Матрица «программа × вид помощи» (ADR 0013): широкая таблица (виды помощи × программы «ВИП», «Москва»), ячейки
  // «да» / «+» / «нет» / «—» и текст лимита (пропускается); значение ячейки — как table_mapping.cell_value.
  var M_YES = ["да", "+", "входит", "включено", "✓", "v"], M_NO = ["нет", "—", "-", "–", "не входит", "не включено", "x", "х"];
  function cellValue(v) {
    var s = String(v || "").trim().toLowerCase().replace(/ё/g, "е").replace(/[.;,]+$/, "");
    if (!s) { return null; }
    if (M_NO.indexOf(s) >= 0) { return false; }
    if (M_YES.indexOf(s) >= 0) { return true; }
    return null;
  }
  function mRowsMatrix() {
    return [["Вид медицинской помощи", "ВИП", "Москва", "Примечание"],
      ["Амбулаторно-поликлиническая помощь", "да", "да", ""], ["Стоматологическая помощь", "+", "нет", ""],
      ["Помощь на дому", "да", "—", "в пределах МКАД"], ["Стационарная помощь", "да", "до 300 000 руб. в год", ""],
      ["Скорая медицинская помощь", "входит", "да", ""], ["Итого", "5", "3", ""]];
  }
  var M_CLIN = ["Медцентр «Альфа-Здоровье»", "Клиника «Береника»", "Поликлиника «Вектор»", "Диагностический центр «Гамма»",
    "Медицинский центр «Дельта-Мед»", "Стоматология «Жемчуг»", "Клиника «Здравница»", "Центр семейной медицины «Исток»", "Клиника «Кедр»"];
  var M_CITY = ["Москва", "Москва", "Санкт-Петербург", "Казань", "Екатеринбург"];
  var M_STREET = ["ул. Лесная, д. ", "пр-т Мира, д. ", "Садовая ул., д. ", "ул. Луговая, д. ", "наб. Речная, д. "];
  var M_CARE = ["Амбулаторно-поликлиническая помощь", "Стоматологическая помощь", "Стационарная помощь", "Помощь на дому"];
  var HEAD_B = ["№ п/п", "Наименование ЛПУ", "Адрес", "Город", "Вид помощи", "Примечание"];
  var HEAD_C = ["Код", "Наименование медицинской организации", "Юридическое лицо", "Город", "Адрес", "Программа", "Вид помощи",
    "Франшиза, %", "По направлению", "Примечание"];
  function mRowsA() { return [["Страхователь", "ООО «…» (по договору)"], ["Срок страхования", "01.02.2026 – 31.01.2027"],
    ["Количество застрахованных", "240"], ["Страховая премия, руб.", "4 128 000,00"]]; }
  function mRowsB() {
    var rows = [["Приложение 3. Перечень медицинских организаций", "", "", "", "", ""], HEAD_B.slice()];
    for (var i = 0; i < 38; i++) {
      if (i === 12 || i === 26) { rows.push(HEAD_B.slice()); }   // повтор заголовка на новой странице
      rows.push([String(i + 1), i === 20 ? "" : M_CLIN[i % M_CLIN.length] + (i >= M_CLIN.length ? " (филиал " + (Math.floor(i / M_CLIN.length) + 1) + ")" : ""),
        M_STREET[i % M_STREET.length] + (3 + i * 2), M_CITY[i % M_CITY.length], M_CARE[i % 3], i % 7 === 3 ? "по направлению врача" : ""]);
    }
    return rows;
  }
  function mRowsC() {
    var rows = [HEAD_C.slice()];
    for (var i = 0; i < 120; i++) {
      var name = M_CLIN[i % M_CLIN.length] + (i >= M_CLIN.length ? " № " + (Math.floor(i / M_CLIN.length) + 1) : "");
      rows.push(["МО-" + (1001 + i), name, "ООО «" + name.replace(/^[^«]*«|».*$/g, "") + "»", M_CITY[i % M_CITY.length],
        M_STREET[(i + 2) % M_STREET.length] + (10 + i), i % 4 === 3 ? "Расширенная" : "Стандарт", M_CARE[i % M_CARE.length],
        i % 5 === 0 ? "10" : i % 9 === 0 ? "—" : "", i % 6 === 0 ? "да" : "", i % 8 === 0 ? "только взрослые" : ""]);
    }
    return rows;
  }
  // Перечень «по таблице на программу» без строки заголовка (Дополнение §10): строка-название, подзаголовки видов
  // помощи, строки клиник «название | | адрес | код»; пустое название — следующий адрес клиники выше.
  function mRowsProg(title, shift) {
    var rows = [[title, "", "", ""], ["Поликлиника | Врач на дом", "", "", ""]];
    for (var i = 0; i < 9; i++) {
      if (i === 6) { rows.push(["Стоматология", "", "", ""]); }
      rows.push([i === 2 ? "" : M_CLIN[(i + shift) % M_CLIN.length], "", "г. " + M_CITY[(i + shift) % M_CITY.length] + ", " + M_STREET[i % M_STREET.length] + (5 + i), "П" + (i + 1) + "фВ"]);
    }
    return rows;
  }
  // Таблицы договора по документам обработки: [{ref, doc_title, rows}].
  function tableSpecs(docs) {
    var out = [], text = docs.filter(function (d) { return d.fmt !== "xlsx"; });
    if (text[0]) {
      out.push({ ref: { doc_id: text[0].doc_id, kind: "docx_table", index: 0, sheet: null }, doc_title: text[0].title, rows: mRowsA() });
      var tb = text[1] || text[0];
      out.push({ ref: { doc_id: tb.doc_id, kind: "docx_table", index: tb === text[0] ? 2 : 1, sheet: null }, doc_title: tb.title, rows: mRowsB() });
      ["ВИП", "Москва"].forEach(function (title, j) {
        out.push({ ref: { doc_id: tb.doc_id, kind: "docx_table", index: (tb === text[0] ? 3 : 2) + j, sheet: null }, doc_title: tb.title, rows: mRowsProg(title, j * 3) });
      });
      out.push({ ref: { doc_id: tb.doc_id, kind: "docx_table", index: (tb === text[0] ? 5 : 4), sheet: null }, doc_title: tb.title, rows: mRowsMatrix() });
    }
    var x = docs.filter(function (d) { return d.fmt === "xlsx"; })[0];
    if (x) { out.push({ ref: { doc_id: x.doc_id, kind: "xlsx_sheet", index: null, sheet: "ЛПУ" }, doc_title: x.title, rows: mRowsC() }); }
    return out;
  }
  function refKey(r) { return [r.doc_id, r.kind, r.index === null || r.index === undefined ? "" : r.index, r.sheet || ""].join("|"); }
  function normCell(v) { return String(v || "").toLowerCase().replace(/ё/g, "е").replace(/[^a-zа-я0-9%]+/g, " ").trim(); }
  function mSignature(rows, h) { return h !== null && rows[h] ? rows[h].map(normCell).join("|") : null; }
  // Подпись по раскладке (table_mapping.layout_signature): колонки, заполненные хотя бы в четверти строк данных.
  function mLayout(rows) {
    var data = rows.filter(function (r) { return r.filter(function (c) { return String(c || "").trim(); }).length > 1; });
    if (!data.length) { return null; }
    var n = rows.reduce(function (a, r) { return Math.max(a, r.length); }, 0), cols = [];
    for (var c = 0; c < n; c++) {
      var hit = data.filter(function (r) { return String(r[c] || "").trim(); }).length;
      if (hit / data.length >= 0.25) { cols.push(c); }
    }
    return "layout:" + n + ":" + cols.join(",");
  }
  function mMapSig(rows, mp) { return mp.header_row === null ? mLayout(rows) : mSignature(rows, mp.header_row); }
  function titleOf(rows) {
    var r = rows[0] || [], cells = r.filter(function (c) { return String(c || "").trim(); });
    return cells.length === 1 ? String(cells[0]).trim() : null;
  }
  var GUESS = [[/наимен|клиник|лпу|медицинск|организац/, "clinic_name"], [/юр/, "legal_name"], [/адрес/, "address"], [/город/, "city"],
    [/вид|помощ/, "care_type"], [/программ/, "program"], [/франш/, "franchise_pct"], [/код/, "code"], [/направлен/, "by_referral"], [/примеч/, "access_note"]];
  function guessRole(cell) {
    var n = normCell(cell);
    for (var i = 0; i < GUESS.length; i++) { if (n && GUESS[i][0].test(n)) { return GUESS[i][1]; } }
    return "ignore";
  }
  // Предложение: профиль СК по подписи одной из первых строк, иначе угадка по заголовкам (≥ 2 знакомых колонки).
  // Программы договора по разметкам перечней (для предложения матрицы): программы из названий таблиц и колонки программы.
  function knownPrograms(cid) {
    var out = [];
    if (!cid) { return out; }
    tablesOf(cid).forEach(function (t) {
      var mp = mapsOf(cid)[refKey(t.ref)];
      if (!mp || mp.role === "care_matrix") { return; }
      mApply(cid, t, mp).rows.forEach(function (r) { if (r.program && out.indexOf(r.program) < 0) { out.push(r.program); } });
    });
    return out;
  }
  // Как table_mapping._matrix_guess / _wide_by_headers (w10-fix2): программы договора известны — совпавшие колонки;
  // нет — колонки заголовка, не похожие на роль колонки («Примечание»), программа = текст заголовка («»).
  function matrixGuess(rows, progs) {
    var head = rows[0] || [], pc = {}, n = 0;
    head.forEach(function (c, i) { if (i > 0 && progs.indexOf(String(c || "").trim()) >= 0) { pc[i] = String(c).trim(); n += 1; } });
    if (!/помощ|услуг/i.test(String(head[0] || ""))) { return null; }
    if (n < 2) {
      pc = {}; n = 0;
      head.forEach(function (c, i) { if (i > 0 && String(c || "").trim() && guessRole(c) === "ignore") { pc[i] = ""; n += 1; } });
    }
    if (n < 2) { return null; }
    return { role: "care_matrix", matrix_layout: "wide", header_row: 0, columns: { 0: "care_type" }, program_cols: pc, program: null, care_type: null };
  }
  function mSuggest(rows, same, cid) {
    var lp = ONB.profiles.filter(function (pp) { return pp.mapping.header_row === null && pp.signature === mLayout(rows); })[0];
    if (lp) { return { mapping: clone(lp.mapping), profile: lp }; }
    for (var h = 0; h < Math.min(5, rows.length); h++) {
      var sig = mSignature(rows, h), prof = ONB.profiles.filter(function (pp) { return pp.signature === sig; })[0];
      if (prof) { return { mapping: clone(prof.mapping), profile: prof }; }
    }
    var mg = matrixGuess(rows, knownPrograms(cid));
    if (mg) { return { mapping: mg, profile: null }; }
    for (var h2 = 0; h2 < Math.min(5, rows.length); h2++) {
      var cols = {}, hits = 0;
      rows[h2].forEach(function (cell, ci) { var r = guessRole(cell); if (r !== "ignore") { hits += 1; } cols[ci] = r; });
      var hasName = Object.keys(cols).some(function (k) { return cols[k] === "clinic_name"; });
      if (hits >= 2 && hasName) { return { mapping: { role: "clinic_list", header_row: h2, columns: cols, program: null, care_type: null }, profile: null }; }
    }
    // таблица программ без заголовка: первая строка — одна ячейка, таких таблиц в договоре несколько
    if ((same || 1) > 1 && titleOf(rows) && mLayout(rows) === "layout:4:0,2,3") {
      return { mapping: { role: "clinic_list", header_row: null, program_from_title: true, columns: { 0: "clinic_name", 2: "address", 3: "code" },
        program: null, care_type: null }, profile: null };
    }
    return null;
  }
  function mapsOf(cid) { ONB.maps[cid] = ONB.maps[cid] || {}; return ONB.maps[cid]; }
  function tablesOf(cid) {
    var j = lastJob(cid);
    if (!j || j.state !== "done" || !j.report || j.report.readiness === "failed") { return []; }
    var docs = j.report.documents.map(function (d) { return { doc_id: d.doc_id, title: d.title, doc_type: d.doc_type, fmt: d.fmt || "docx" }; });
    return tableSpecs(docs);
  }
  function publicMapping(m) { var c = clone(m); delete c._profile; return c; }
  function sameLayout(cid, t) {
    var lay = mLayout(t.rows);
    return lay ? tablesOf(cid).filter(function (x) { return mLayout(x.rows) === lay; }).length : 1;
  }
  // w9-fix п. 2: профиль применяется сам только к договору той же СК (без регистра, кавычек, «страхование», ОПФ)
  function insurerKey(v) {
    return normCell(v).split(" ").filter(function (w) { return w && ["ск", "страхование", "страховая", "компания", "ооо", "ао", "пао", "зао"].indexOf(w) < 0; }).join(" ");
  }
  function autoProfile(cid, rows, same) {
    var sg = mSuggest(rows, same), hit = onbContract(cid), key = insurerKey(hit && hit.c.insurer_name);
    return sg && sg.profile && key && insurerKey(sg.profile.insurer_name) === key && !suspiciousOf(rows, sg.mapping).length ? sg.profile : null;
  }
  // w9-fix п. 1б: проверка содержимого — в колонке названия суммы, числа, «Итого» вместо названий организаций
  function suspiciousOf(rows, mp) {
    if (!mp || !mp.columns) { return []; }
    var c = Object.keys(mp.columns).filter(function (k) { return mp.columns[k] === "clinic_name"; })[0];
    if (c === undefined) { return []; }
    var start = mp.header_row === null ? 0 : mp.header_row + 1, data = [], good = 0;
    rows.slice(start).forEach(function (r) {
      var filled = r.filter(function (x) { return String(x || "").trim(); }), v = String(r[c] || "").trim();
      if (filled.length > 1 && v) { data.push(v); if (/[A-Za-zА-Яа-яЁё]{2}/.test(v) && !/^(итого|всего|страховая премия|программа)(?![а-яё])/i.test(v)) { good += 1; } }
    });
    if (!data.length || good / data.length >= 0.6) { return []; }
    return ["в колонке названия похожих на название организации строк " + Math.round(good * 100 / data.length) + " % (нужно не меньше 60 %)"];
  }
  function hasProgram(mp) {
    return !!mp.program_from_title || !!String(mp.program || "").trim() || Object.keys(mp.columns || {}).some(function (k) { return mp.columns[k] === "program"; });
  }
  function previewOf(cid, t) {
    var same = sameLayout(cid, t), m = mapsOf(cid)[refKey(t.ref)] || null, sg = m ? null : mSuggest(t.rows, same, cid), cur = m || (sg && sg.mapping);
    var ncols = t.rows.reduce(function (a, r) { return Math.max(a, r.length); }, 0), auto = m ? null : autoProfile(cid, t.rows, same);
    return { ref: clone(t.ref), doc_title: t.doc_title, rows: clone(t.rows.slice(0, PREVIEW_ROWS)), n_rows: t.rows.length, n_cols: ncols,
      signature: cur ? mMapSig(t.rows, cur) : null, mapping: m ? publicMapping(m) : null,
      suggested: sg ? sg.mapping : null, profile_id: m ? (m._profile || null) : sg && sg.profile ? sg.profile.id : null,
      layout: mLayout(t.rows), same_layout: same, applied_profile: auto ? auto.id : null,
      suspicious: m || auto ? suspiciousOf(t.rows, m || auto.mapping) : [], similar_unmapped: false };
  }
  function tableLabel(t) { return "«" + t.doc_title + "», " + (t.ref.kind === "xlsx_sheet" ? "лист «" + t.ref.sheet + "»" : "таблица " + (t.ref.index + 1)); }
  function franchise(v) {
    var s = String(v || "").trim();
    if (!s) { return null; }
    if (/^(—|-|нет|не применяется)$/i.test(s)) { return "—"; }
    var n = parseFloat(s.replace(",", "."));
    return isNaN(n) ? null : n;
  }
  // Применение разметки: строки ниже заголовка; без названия и повтор заголовка — пропуск с предупреждением.
  function mApply(cid, t, mp) {
    var out = [], noName = 0, again = 0, head = mSignature(t.rows, mp.header_row), by = {}, prev = null, section = "";
    var start = mp.header_row === null ? 0 : mp.header_row + 1, prog = null;
    if (mp.program_from_title) {
      prog = titleOf(t.rows);
      if (!prog) { return { rows: [], warnings: ["Таблица " + tableLabel(t) + ": программа — из названия таблицы, но первая строка — не одна ячейка: таблица не разобрана."] }; }
      start = Math.max(start, 1);
    }
    Object.keys(mp.columns).forEach(function (k) { if (mp.columns[k] !== "ignore") { by[mp.columns[k]] = Number(k); } });
    function cell(r, role) { return by[role] === undefined ? null : String(r[by[role]] || "").trim() || null; }
    for (var i = start; i < t.rows.length; i++) {
      var r = t.rows[i], filled = r.filter(function (c) { return String(c || "").trim(); });
      if (!filled.length) { continue; }
      if (head && mSignature(t.rows, i) === head) { again += 1; continue; }
      if (filled.length === 1 && r.length > 1) { section = String(filled[0]).trim(); prev = null; continue; }   // подзаголовок
      var name = cell(r, "clinic_name") || (mp.header_row === null ? prev : null);   // без заголовка: пустое название — адрес клиники выше
      if (!name) { noName += 1; continue; }
      prev = name;
      out.push({ id: cid + "-mr" + i, contract_id: cid, doc_id: t.ref.doc_id, program: cell(r, "program") || prog || mp.program || "",
        care_type: cell(r, "care_type") || mp.care_type || section, clinic_name: name, legal_name: cell(r, "legal_name"), address: cell(r, "address"),
        city: cell(r, "city"), code: cell(r, "code"), franchise_pct: franchise(cell(r, "franchise_pct")),
        by_referral: /^(да|\+|по направлению)/i.test(cell(r, "by_referral") || ""), access_note: cell(r, "access_note"),
        source_ref: t.ref.kind === "xlsx_sheet" ? t.ref.sheet + "!B" + (i + 1) : "t" + t.ref.index + ":r" + i });
    }
    var w = [];
    if (noName) { w.push("Пропущено строк без названия клиники: " + noName + "."); }
    if (again) { w.push("Пропущен повтор строки заголовка внутри таблицы: " + again + "."); }
    if (by.program === undefined && !mp.program && !prog) { w.push("Программа не указана — нет колонки «Программа» и значения для всей таблицы: строки перечня без программы."); }
    if (by.care_type === undefined && !mp.care_type && !section) { w.push("Вид помощи не указан — нет колонки «Вид помощи» и значения для всей таблицы."); }
    return { rows: out, warnings: w };
  }
  // Матрица по разметке (ADR 0013): wide — колонка видов помощи + program_cols, long — program / care_type / present.
  function mApplyMatrix(cid, t, mp) {
    var by = {}, out = [], skipped = [], nSkip = 0, seen = {};
    Object.keys(mp.columns || {}).forEach(function (k) { by[mp.columns[k]] = Number(k); });
    var start = mp.header_row === null ? 0 : mp.header_row + 1, head = mp.header_row === null ? [] : t.rows[mp.header_row] || [];
    function add(prog, care, val, ref) { var key = prog + "\u0000" + care; if (!seen[key]) { seen[key] = true; out.push({ program: prog, care_type: care, present: val, source_ref: ref }); } }
    function skip(i, c, v) { nSkip += 1; if (skipped.length < 20) { skipped.push({ cell: "строка " + (i + 1) + ", колонка " + (c + 1), text: String(v).slice(0, 120) }); } }
    for (var i = start; i < t.rows.length; i++) {
      var r = t.rows[i], care = String(r[by.care_type] || "").trim();
      if (!care || !/[А-Яа-яЁёA-Za-z]{2}/.test(care) || /^(итого|всего)/i.test(care)) { continue; }
      if (mp.matrix_layout === "wide") {
        Object.keys(mp.program_cols || {}).forEach(function (k) {
          var c = Number(k), prog = String(mp.program_cols[k] || head[c] || "").trim(), raw = String(r[c] || "").trim(), v = cellValue(raw);
          if (!prog || c === by.care_type) { return; }
          if (v === null) { if (raw) { skip(i, c, raw); } return; }
          add(prog, care, v, t.ref.doc_id + "#t" + t.ref.index + "r" + i + "c" + c);
        });
      } else {
        var prog = by.program === undefined ? String(mp.program || "").trim() : String(r[by.program] || "").trim();
        var raw = by.present === undefined ? "да" : String(r[by.present] || "").trim(), v = cellValue(raw);
        if (!prog) { continue; }
        if (v === null) { if (raw) { skip(i, by.present, raw); } continue; }
        add(prog, care, v, t.ref.doc_id + "#t" + t.ref.index + "r" + i);
      }
    }
    var w = [];
    if (nSkip) { w.push("Таблица " + tableLabel(t) + ": ячеек матрицы с текстом не «да» / «нет» (суммы, лимиты, примечания) — " + nSkip + ": они не учтены."); }
    if (!out.length) { w.push("Таблица " + tableLabel(t) + ": по разметке матрицы не найдено ни одной пары «программа × вид помощи»."); }
    return { matrix: out, skipped: skipped, skipped_total: nSkip, warnings: w };
  }
  // derive_care: виды помощи строк перечня → пары present=true («Поликлиника | Врач на дом» — два вида)
  function deriveCare(rows) {
    var out = [], seen = {};
    rows.forEach(function (r) {
      String(r.care_type || "").split("|").forEach(function (part) {
        var care = part.trim(), key = r.program + "\u0000" + care;
        if (!care || !r.program || seen[key]) { return; }
        seen[key] = true; out.push({ program: r.program, care_type: care, present: true, source_ref: r.source_ref + "#care" });
      });
    });
    return out;
  }
  // Сводные предупреждения (w9-fix п. 6): одно на источник разметки; профиль — только своей СК, used — раз на договор.
  function applyMappings(cid, docs) {
    var res = { clinics: 0, tables: 0, care: 0, matrices: 0, warnings: [] }, maps = mapsOf(cid), by = {}, used = {};
    tableSpecs(docs).forEach(function (t) {
      var m = maps[refKey(t.ref)], prof = null;
      if (!m) { prof = autoProfile(cid, t.rows, 1); m = prof ? prof.mapping : null; }
      if (m && m.role === "care_matrix") {             // ADR 0013: матрица — после перечней
        var mx = mApplyMatrix(cid, t, m);
        if (mx.matrix.length) { res.care += mx.matrix.length; res.matrices += 1;
          res.warnings.push("Применена разметка матрицы видов помощи к таблицам: 1, строк матрицы — " + mx.matrix.length + "."); }
        res.warnings = res.warnings.concat(mx.warnings);
        return;
      }
      if (!m || !hasProgram(m)) { return; }
      var a = mApply(cid, t, m), key = prof ? prof.id : "";
      if (m.derive_care) {
        var dc = deriveCare(a.rows);
        res.care += dc.length; res.matrices += 1;
        res.warnings.push("Виды помощи из перечня клиник → матрица: строк матрицы — " + dc.length + " (таблиц перечня: 1).");
      }
      res.clinics += a.rows.length; res.tables += 1;
      by[key] = by[key] || { k: 0, n: 0, name: prof ? prof.insurer_name : null }; by[key].k += 1; by[key].n += a.rows.length;
      if (prof && !used[prof.id]) { used[prof.id] = true; prof.used += 1; }
    });
    Object.keys(by).forEach(function (k) {
      var b = by[k];
      res.warnings.push(b.name ? "Применена разметка СК «" + b.name + "» к таблицам: " + b.k + ", строк перечня — " + b.n + "."
        : "Применена разметка договора к таблицам: " + b.k + ", строк перечня — " + b.n + ".");
    });
    return res;
  }
  function seedProfiles() {
    var at = "2026-10-20T09:00:00.000Z", cols = {};
    ["code", "clinic_name", "legal_name", "city", "address", "program", "care_type", "franchise_pct", "by_referral", "access_note"].forEach(function (r, i) { cols[i] = r; });
    return [{ id: "ip1", insurer_name: "Альфа-Страхование", signature: HEAD_C.map(normCell).join("|"),
      mapping: { role: "clinic_list", header_row: 0, columns: cols, program: null, care_type: null }, created_at: at, updated_at: at, used: 2 }];
  }
  function findTable(cid, ref) {
    if (!ref || !ref.doc_id) { return null; }
    var idx = ref.index === undefined || ref.index === "" || ref.index === null ? null : Number(ref.index);
    var key = refKey({ doc_id: ref.doc_id, kind: ref.kind, index: idx, sheet: ref.sheet || null });
    return tablesOf(cid).filter(function (t) { return refKey(t.ref) === key; })[0] || null;
  }
  // Проверка разметки (коды CONTRACTS §10): mapping_no_name, mapping_bad_column.
  function checkMapping(t, mp) {
    if (!mp || !mp.columns) { return err(422, "Разметка не передана.", "mapping", "validation"); }
    var n = t.rows.reduce(function (a, r) { return Math.max(a, r.length); }, 0), bad = false, name = false, care = false;
    Object.keys(mp.columns).forEach(function (k) {
      if (!(Number(k) >= 0 && Number(k) < n) || ROLES.indexOf(mp.columns[k]) < 0) { bad = true; }
      if (mp.columns[k] === "clinic_name") { name = true; }
      if (mp.columns[k] === "care_type") { care = true; }
    });
    Object.keys(mp.program_cols || {}).forEach(function (k) { if (!(Number(k) >= 0 && Number(k) < n)) { bad = true; } });
    if (bad || !(mp.header_row === null || (mp.header_row >= 0 && mp.header_row < t.rows.length))) { return err(422, "Колонка или строка заголовка вне таблицы.", "mapping.columns", "mapping_bad_column"); }
    if (mp.role === "care_matrix") {                  // CONTRACTS §11: mapping_no_care, mapping_no_program
      if (!care) { return err(422, "Отметьте колонку с видами помощи.", "mapping.columns", "mapping_no_care"); }
      var progs = mp.matrix_layout === "wide" ? Object.keys(mp.program_cols || {}).length
        : Object.keys(mp.columns).some(function (k) { return mp.columns[k] === "program"; }) || !!String(mp.program || "").trim() || !!mp.program_from_title;
      if (!progs) { return err(422, "Укажите программы матрицы: колонки программ (строки — виды помощи) или колонку программы / программу для всей таблицы (длинная таблица).", "mapping.program_cols", "mapping_no_program"); }
      return null;
    }
    if (!name) { return err(422, "Не отмечена колонка названия клиники.", "mapping.columns", "mapping_no_name"); }
    return null;
  }
  function cleanMapping(mp) {
    var cols = {};
    Object.keys(mp.columns).forEach(function (k) { if (mp.columns[k] !== "ignore") { cols[k] = mp.columns[k]; } });
    var s = function (v) { v = String(v || "").trim(); return v ? v.slice(0, 120) : null; };
    if (mp.role === "care_matrix") {
      var pc = {};
      Object.keys(mp.program_cols || {}).forEach(function (k) { pc[k] = String(mp.program_cols[k] || "").slice(0, 120); });
      return { role: "care_matrix", matrix_layout: mp.matrix_layout === "long" ? "long" : "wide", header_row: mp.header_row === null ? null : Number(mp.header_row) || 0,
        columns: cols, program_cols: pc, program_from_title: !!mp.program_from_title, program: s(mp.program), care_type: null };
    }
    return { role: "clinic_list", header_row: mp.header_row === null ? null : Number(mp.header_row) || 0, columns: cols, derive_care: !!mp.derive_care,
      program_from_title: !!mp.program_from_title, program: mp.program_from_title ? null : s(mp.program), care_type: s(mp.care_type) };
  }
  function mapRoute(method, p, body, qs) {
    var m, hit, t, bad;
    if (p === "/api/insurer-profiles" && method === "GET") { return ok(ONB.profiles); }
    if ((m = /^\/api\/insurer-profiles\/(ip\d+)$/.exec(p)) && method === "DELETE") {
      var pi = ONB.profiles.filter(function (x) { return x.id === m[1]; })[0];
      if (!pi) { return err(404, "Профиль СК не найден.", null, "not_found"); }
      ONB.profiles.splice(ONB.profiles.indexOf(pi), 1);
      Object.keys(ONB.maps).forEach(function (cid) { Object.keys(ONB.maps[cid]).forEach(function (k) { if (ONB.maps[cid][k]._profile === pi.id) { delete ONB.maps[cid][k]._profile; } }); });
      save();
      return { ok: true, status: 204, data: null };
    }
    if (!(m = /^\/api\/clients\/contracts\/(u\d+)\/tables(\/try|\/mapping)?$/.exec(p))) { return null; }
    hit = onbContract(m[1]);
    if (!hit) { return err(404, "Договор не найден.", null, "not_found"); }
    var cid = hit.c.id;
    if (!m[2] && method === "GET") { return ok(tablesOf(cid).map(function (tt) { return previewOf(cid, tt); })); }
    if (m[2] === "/try" && method === "POST") {
      if (!(t = findTable(cid, body && body.ref))) { return err(404, "Таблица не найдена.", "ref", "table_not_found"); }
      if ((bad = checkMapping(t, body.mapping))) { return bad; }
      var cm = cleanMapping(body.mapping);
      if (cm.role === "care_matrix") {
        var mx = mApplyMatrix(cid, t, cm);
        return ok({ matrix: mx.matrix.slice(0, MATRIX_ROWS), total: mx.matrix.length, skipped: mx.skipped, skipped_total: mx.skipped_total,
          warnings: mx.warnings, suspicious: [] });
      }
      var a = mApply(cid, t, cm), su = suspiciousOf(t.rows, body.mapping);
      var res = { rows: a.rows.slice(0, SAMPLE_ROWS), total: a.rows.length, suspicious: su,
        warnings: (su.length ? ["Таблица " + tableLabel(t) + ": разметка похожа на ошибочную — " + su.join("; ") + "."] : []).concat(a.warnings) };
      if (cm.derive_care) { var dc = deriveCare(a.rows); res.matrix = dc.slice(0, MATRIX_ROWS); res.matrix_total = dc.length; }
      return ok(res);
    }
    if (m[2] === "/mapping" && method === "PUT") {
      if (!(t = findTable(cid, body && body.ref))) { return err(404, "Таблица не найдена.", "ref", "table_not_found"); }
      if ((bad = checkMapping(t, body.mapping))) { return bad; }
      var cur = lastJob(cid);
      if (body.process && cur && (cur.state === "queued" || cur.state === "running")) { return err(409, "Обработка этого договора уже идёт.", null, "job_running"); }
      var mp = cleanMapping(body.mapping), prof = null, nm = String(body.insurer_name || "").trim(), su = mp.role === "care_matrix" ? [] : suspiciousOf(t.rows, mp);
      if (mp.role !== "care_matrix" && !hasProgram(mp)) { return err(422, "Укажите, откуда программа строк: колонка программы, программа для всей таблицы или «Программа — из названия таблицы».", "mapping.program", "mapping_no_program"); }
      if (su.length && body.confirm_suspicious !== true) {
        return err(422, "Разметка похожа на ошибочную: " + su.join("; ") + ". Проверьте таблицу; если это всё же перечень клиник, сохраните с подтверждением.", "mapping", "mapping_suspicious");
      }
      if (body.save_profile) {
        if (!nm) { return err(422, "Укажите название СК для профиля.", "insurer_name", "validation"); }
        var sig = mMapSig(t.rows, mp), at = now();
        prof = ONB.profiles.filter(function (x) { return x.signature === sig; })[0];
        if (prof) { prof.insurer_name = nm.slice(0, 120); prof.mapping = clone(mp); prof.updated_at = at; }
        else { ONB.seq.ip += 1; prof = { id: "ip" + ONB.seq.ip, insurer_name: nm.slice(0, 120), signature: sig, mapping: clone(mp), created_at: at, updated_at: at, used: 0 }; ONB.profiles.push(prof); }
        mp._profile = prof.id;
      }
      mapsOf(cid)[refKey(t.ref)] = mp;
      var applied = 1;
      if (body.apply_same_layout === true) {          // Дополнение §10: та же разметка всем таблицам с той же подписью
        var sig0 = mMapSig(t.rows, mp);
        tablesOf(cid).forEach(function (x) {
          if (refKey(x.ref) !== refKey(t.ref) && sig0 && mMapSig(x.rows, mp) === sig0 && !checkMapping(x, mp)) { mapsOf(cid)[refKey(x.ref)] = clone(mp); applied += 1; }
        });
      }
      if (nm && !hit.c.insurer_name) { hit.c.insurer_name = nm.slice(0, 120); }
      save();
      var out = { preview: previewOf(cid, t), profile: prof ? clone(prof) : null, applied_tables: applied };
      if (body.process) { out.job = publicJob(jobView(startJob(hit))); }
      return ok(out);
    }
    if (m[2] === "/mapping" && method === "DELETE") {
      var ref = { doc_id: qs.get("doc_id"), kind: qs.get("kind"), index: qs.get("index"), sheet: qs.get("sheet") };
      if (!(t = findTable(cid, ref))) { return err(404, "Таблица не найдена.", "ref", "table_not_found"); }
      delete mapsOf(cid)[refKey(t.ref)];
      save();
      return { ok: true, status: 204, data: null };
    }
    return null;
  }
  // Разметки и профили — в том же состоянии (прежнее состояние sessionStorage дополняется).
  if (!ONB.maps) { ONB.maps = {}; ONB.profiles = EMPTY ? [] : seedProfiles(); ONB.seq.ip = ONB.profiles.length; save(); }
  // Очередь по одному договору (HeavyGate): начало — после конца последнего незавершённого задания.
  function startJob(hit) {
    var start = Date.now();
    ONB.jobs.forEach(function (j) {
      var v = jobView(j);
      if (v.state === "queued" || v.state === "running") { start = Math.max(start, j._t0 + ONB_QUEUE + ONB_STAGES.length * ONB_STEP); }
    });
    ONB.seq.j += 1;
    var job = { id: "oj" + ONB.seq.j, contract_id: hit.c.id, state: "queued", stage: null, created_at: now(), finished_at: null, report: null, error: null,
      _t0: start, _fail: FAIL_ONB, _hint: hit.c.insurer_hint, _fg: !!hit.c.force_generic, _files: clone(allFiles(hit.g, hit.c.id)) };
    ONB.jobs.push(job); save();
    return job;
  }
  function created(data, status) { return { ok: true, status: status || 201, data: clone(data) }; }
  function onbRoute(method, p, body, qs) {
    var m, hit, list, mr = mapRoute(method, p, body, qs);
    if (mr) { return mr; }
    if (p === "/api/clients" && method === "GET") { return ok(ONB.groups.map(publicGroup)); }
    if (p === "/api/clients" && method === "POST") {
      var name = String((body && body.name) || "").trim(), cs = (body && body.contracts) || [];
      if (!name) { return err(422, "Укажите название группы компаний.", "name", "validation"); }
      if (!cs.length) { return err(422, "Добавьте хотя бы одно юрлицо.", "contracts", "validation"); }
      for (var i = 0; i < cs.length; i++) {
        if (!String(cs[i].legal_entity || "").trim()) { return err(422, "Укажите название юрлица в строке " + (i + 1) + ".", "contracts." + i + ".legal_entity", "validation"); }
      }
      ONB.seq.g += 1;
      var g = { id: "ug" + ONB.seq.g, name: name, created_at: now(), contracts: [] };
      ONB.groups.push(g); ONB.gfiles[g.id] = [];
      cs.forEach(function (c) { newContract(g, c); });
      save();
      return created(g);
    }
    if ((m = /^\/api\/clients\/(ug\d+)\/contracts$/.exec(p)) && method === "POST") {
      var gg = ONB.groups.filter(function (x) { return x.id === m[1]; })[0];
      if (!gg) { return err(404, "Группа компаний не найдена.", null, "not_found"); }
      if (!String((body && body.legal_entity) || "").trim()) { return err(422, "Укажите название юрлица.", "legal_entity", "validation"); }
      var nc = newContract(gg, body); save();
      return created(nc);
    }
    if ((m = /^\/api\/clients\/contracts\/(u\d+)\/files$/.exec(p)) && method === "POST") {
      hit = onbContract(m[1]);
      if (!hit) { return err(404, "Договор не найден.", null, "not_found"); }
      var f = body && body.get ? body.get("file") : null;
      if (!f) { return err(422, "Файл не выбран.", "file", "validation"); }
      var fname = String(f.name).replace(/^.*[\\/]/, ""), ext = (fname.split(".").pop() || "").toLowerCase();
      if (["docx", "xlsx", "pdf", "txt"].indexOf(ext) < 0) { return err(415, "Формат файла не поддерживается: нужен DOCX, XLSX, PDF или TXT.", "file", "unsupported_format"); }
      if (f.size > 25 * 1048576) { return err(413, "Файл больше 25 МБ. Разделите документ или уменьшите файл.", "file", "too_large"); }
      var scope = body.get("scope") === "group" ? "group" : "contract";
      list = scope === "group" ? (ONB.gfiles[hit.g.id] = ONB.gfiles[hit.g.id] || []) : (ONB.files[hit.c.id] = ONB.files[hit.c.id] || []);
      var same = list.filter(function (x) { return x.name === fname; })[0];
      if (!same && list.length >= 40) { return err(422, "Не больше 40 файлов на договор. Удалите лишние файлы.", "file", "too_many_files"); }
      var rec = cfile(fname, scope, f.size, now());
      if (body.get("doc_type") && DOC_TYPES.indexOf(body.get("doc_type")) >= 0) { rec.doc_type = body.get("doc_type"); }
      if (same) { list[list.indexOf(same)] = rec; } else { list.push(rec); }
      save();
      return created(rec);
    }
    if ((m = /^\/api\/clients\/contracts\/(u\d+)\/files\/([^/]+)$/.exec(p))) {
      hit = onbContract(m[1]);
      if (!hit) { return err(404, "Договор не найден.", null, "not_found"); }
      var nm = decodeURIComponent(m[2]), lists = [ONB.files[hit.c.id] || [], ONB.gfiles[hit.g.id] || []], rec2 = null, own = null;
      lists.forEach(function (l) { l.forEach(function (x) { if (x.name === nm) { rec2 = x; own = l; } }); });
      if (!rec2) { return err(404, "Файл не найден. Обновите список файлов.", null, "not_found"); }
      if (method === "DELETE") { own.splice(own.indexOf(rec2), 1); save(); return { ok: true, status: 204, data: null }; }
      if (method === "PATCH") {
        if (body.doc_type !== undefined) {
          if (DOC_TYPES.indexOf(body.doc_type) < 0) { return err(422, "Неизвестный тип документа.", "doc_type", "validation"); }
          rec2.doc_type = body.doc_type;
        }
        if (body.scope && body.scope !== rec2.scope) {
          own.splice(own.indexOf(rec2), 1);
          rec2.scope = body.scope === "group" ? "group" : "contract";
          (rec2.scope === "group" ? ONB.gfiles[hit.g.id] : ONB.files[hit.c.id]).push(rec2);
        }
        save();
        return ok(rec2);
      }
    }
    if ((m = /^\/api\/clients\/contracts\/(u\d+)\/process$/.exec(p)) && method === "POST") {
      hit = onbContract(m[1]);
      if (!hit) { return err(404, "Договор не найден.", null, "not_found"); }
      var cur = lastJob(hit.c.id);
      if (cur && (cur.state === "queued" || cur.state === "running")) { return err(409, "Обработка этого договора уже идёт.", null, "job_running"); }
      if (!allFiles(hit.g, hit.c.id).length) { return err(422, "Нет файлов для обработки. Загрузите документы договора.", null, "no_files"); }
      return created(publicJob(jobView(startJob(hit))), 202);
    }
    if ((m = /^\/api\/clients\/jobs\/([^/]+)$/.exec(p)) && method === "GET") {
      var jj = ONB.jobs.filter(function (x) { return x.id === m[1]; })[0];
      return jj ? ok(publicJob(jobView(jj))) : err(404, "Задание не найдено.", null, "not_found");
    }
    return null;
  }

  function ok(data) { return { ok: true, status: 200, data: clone(data) }; }
  function err(status, message, field, code) { return { ok: false, status: status, data: { error: { code: code || String(status), message: message, field: field || null } } }; }
  function publicReq(r) { var c = clone(r); delete c._insurer_terms; delete c._clinic_q; return c; }
  function findChat(cid) { return db.chats.filter(function (c) { return c.id === cid; })[0]; }
  function findReq(rid) { return db.requests.filter(function (r) { return r.id === rid; })[0]; }

  var EXAMPLES = [
    { id: "ex1", title: "МРТ: при условиях", question: "Клиника отказала сотруднику в МРТ колена. Обоснованно?", context: { group_id: "g1", contract_id: "c1", program: "Стандарт" } },
    { id: "ex2", title: "Имплантация: нельзя", question: "Сотруднику отказали в имплантации зуба. Это законно?", context: { group_id: "g1", contract_id: "c1", program: "Стандарт" } },
    { id: "ex3", title: "Клиники нет → HR и СК", question: "Сотрудник хочет в клинику «Медцентр Плюс». Она есть в программе?", context: { group_id: "g1", contract_id: "c1", program: "Стандарт" } },
    { id: "ex4", title: "МРТ по группе", question: "На каких условиях МРТ в договорах группы?", context: { group_id: "g1", contract_id: null, program: null } },
    { id: "ex5", title: "Мало данных", question: "Почему отказали?", context: { group_id: "g2", contract_id: "c3", program: null } },
    { id: "ex8", title: "Приём кардиолога: можно", question: "Входит ли приём кардиолога и УЗИ сердца?", context: { group_id: "g1", contract_id: "c1", program: "Стандарт" } },
    { id: "ex7", title: "Уточнить клинику", question: "Можно ли сотруднику сделать МРТ в «Диагност Центре»?", context: { group_id: "g1", contract_id: "c1", program: "Стандарт" } },
    { id: "ex6", title: "Ошибка сервиса", question: "Покажи ошибку сервиса", context: { group_id: "g2", contract_id: "c3", program: null } }
  ];

  // ---------- маршруты ----------
  function route(method, url, body) {
    var u = new URL(url, location.origin), p = u.pathname, qs = u.searchParams, m;
    syncGroups();
    if (/^\/api\/(clients|insurer-profiles)(\/|$)/.test(p)) { var onb = onbRoute(method, p, body, qs); if (onb) { return onb; } }
    if (method === "GET" && p === "/api/groups") { return ok(GROUPS); }
    if (method === "GET" && p === "/api/examples") { return ok(EXAMPLES); }
    if ((m = /^\/api\/contracts\/([^/]+)$/.exec(p))) {
      var cc = contractOf(m[1]);
      if (!cc) { return err(404, "Договор не найден. Выберите договор из списка."); }
      return ok({ id: cc.c.id, group_id: cc.g.id, legal_entity: cc.c.legal_entity, number: cc.c.number, insurer: cc.c.insurer, date: "2026-01-15",
        period_from: "2026-02-01", period_to: "2027-01-31", headcount: 240, programs: cc.c.programs.map(function (n) { return { name: n, premium_per_person: null, persons: null, regions: ["Москва"], notes: null }; }),
        payments: [], franchise: { declared: false, mechanics_clause_ids: [] }, missing_docs: cc.c.id === "c2" ? ["Приложение 1"] : [],
        documents: DOCS.filter(function (d) { return d.contract_id === cc.c.id; }) });
    }
    if (method === "GET" && p === "/api/documents") {
      return ok(DOCS.filter(function (d) { return qs.get("contract_id") ? d.contract_id === qs.get("contract_id") : d.group_id === qs.get("group_id"); }));
    }
    if (method === "POST" && p === "/api/documents") {
      var f = body && body.get ? body.get("file") : null;
      if (!f) { return err(400, "Файл не выбран.", "file"); }
      var nd = doc(id("d"), body.get("contract_id") || null, body.get("group_id") || null, f.name.replace(/\.[^.]+$/, ""), body.get("doc_type") || "other", "ready", [], (f.name.split(".").pop() || "docx").toLowerCase());
      nd.uploaded_by = body.get("uploaded_by") || "operator"; nd.uploaded_at = now();
      DOCS.push(nd);
      return ok(nd);
    }
    if (method === "POST" && p === "/api/documents/import-kit") { return ok({ contracts: 3, documents: DOCS.length, errors: 1, warnings: 4 }); }
    if ((m = /^\/api\/documents\/([^/]+)\/clauses$/.exec(p))) {
      return ok((CLAUSES[m[1]] || []).map(function (c) { return clauseObj(m[1], c); }));
    }
    if ((m = /^\/api\/documents\/([^/]+)\/clause$/.exec(p))) {
      if (qs.get("ref")) {
        var row = Object.keys(CLINICS).map(function (k) { return CLINICS[k]; }).filter(function (r) { return r.source_ref === qs.get("ref") || r.id === qs.get("ref"); })[0];
        return row ? ok({ clinic: row }) : err(404, "Строка перечня не найдена.");
      }
      var hit = (CLAUSES[m[1]] || []).filter(function (c) { return c.number === qs.get("number"); })[0];
      return hit ? ok({ clause: clauseObj(m[1], hit) }) : err(404, "Пункт не найден в документе.");
    }
    if ((m = /^\/api\/documents\/([^/]+)$/.exec(p))) {
      var d = DOCS.filter(function (x) { return x.id === m[1]; })[0];
      return d ? ok(d) : err(404, "Документ не найден.");
    }
    if (p === "/api/chats" && method === "GET") { return ok(db.chats); }
    if (p === "/api/chats" && method === "POST") {
      var ctx = body.context || {};
      if (!ctx.group_id && !ctx.contract_id) { return err(422, "Выберите группу компаний или договор.", "context"); }
      var cc2 = contractOf(ctx.contract_id), g = GROUPS.filter(function (x) { return x.id === ctx.group_id; })[0];
      var title = body.title || ((cc2 ? cc2.c.legal_entity : g ? g.name : "") + (ctx.program ? " · " + ctx.program : ""));
      var ch = { id: id("ch"), title: title, context: { group_id: ctx.group_id || (cc2 && cc2.g.id) || null, contract_id: ctx.contract_id || null, program: ctx.program || null, date: ctx.date || null }, created_at: now() };
      db.chats.unshift(ch); save();
      return ok(ch);
    }
    if ((m = /^\/api\/chats\/([^/]+)\/requests$/.exec(p))) {
      if (!findChat(m[1])) { return err(404, "Чат не найден."); }
      if (method === "POST") {
        var nr = { id: id("r"), chat_id: m[1], title: body.title, scenario: null, status: "open", rating: null, rating_comment: null, slots: {}, automation: null, drafts: [], messages: [] };
        db.requests.unshift(nr); save();
        return ok(publicReq(nr));
      }
      var st = qs.get("status") || "open";
      return ok(db.requests.filter(function (r) { return r.chat_id === m[1] && (st === "all" || r.status === st); }).map(publicReq));
    }
    if ((m = /^\/api\/chats\/([^/]+)$/.exec(p))) {
      var c = findChat(m[1]);
      return c ? ok(Object.assign(clone(c), { requests: db.requests.filter(function (r) { return r.chat_id === c.id; }).map(publicReq) })) : err(404, "Чат не найден.");
    }
    if ((m = /^\/api\/requests\/([^/]+)\/(close|reopen|rate)$/.exec(p))) {
      var r = findReq(m[1]);
      if (!r) { return err(404, "Запрос не найден."); }
      if (m[2] === "close") { r.status = "closed"; }
      if (m[2] === "reopen") { r.status = "open"; }
      if (m[2] === "rate") {
        if (!(body.rating >= 1 && body.rating <= 5)) { return err(422, "Оценка — число от 1 до 5.", "rating"); }
        r.rating = body.rating; r.rating_comment = body.comment || null;
      }
      save();
      return ok(publicReq(r));
    }
    if ((m = /^\/api\/requests\/([^/]+)\/automation\/(add-clinic|step)$/.exec(p))) {
      var rq = findReq(m[1]), chat = rq && findChat(rq.chat_id);
      if (!rq) { return err(404, "Запрос не найден."); }
      if (m[2] === "add-clinic") {
        if (rq.automation) { return ok(publicReq(rq)); }   // повторный запуск — без дублей
        var clinic = body.clinic || null;
        var last = rq.messages.filter(function (x) { return x.answer; }).pop();
        var stateName = clinic ? "not_in_program" : last && last.answer.alternatives.length ? "alt_found" : "insufficient";
        rq.automation = { kind: "add_clinic", state: stateName, clinic: clinic, history: [{ to: stateName, at: now() }] };
        (STAGE_DRAFTS[stateName] || []).forEach(function (tpl) { rq.drafts.push(makeDraft(tpl, stateName, draftFields(rq, chat))); });
      } else {
        if (body.insurer_reply && body.action === "insurer_paid") { rq._insurer_terms = body.insurer_reply; }
        if (!rq.automation || !applyStep(rq, chat, body.action, body.insurer_reply)) {
          return err(409, "Это действие недоступно на текущем шаге. Обновите страницу.", "action");
        }
      }
      save();
      return ok(publicReq(rq));
    }
    // Поиск по перечню программы выбора последнего ответа (CONTRACTS §8, круг w7): 409 no_clinic_choice — в
    // последнем ответе нет выбора по договору; 422 contract_not_in_context — договор не из контекста чата.
    // &fail=find — поиск отвечает 422 (снимок ошибки).
    if ((m = /^\/api\/requests\/([^/]+)\/clinic-candidates$/.exec(p)) && method === "GET") {
      var rs = findReq(m[1]), chs = rs && findChat(rs.chat_id), cidS = qs.get("contract_id") || "";
      if (!rs) { return err(404, "Запрос не найден."); }
      var ctxIds = chs.context.contract_id ? [chs.context.contract_id] : (GROUPS.filter(function (g) { return g.id === chs.context.group_id; })[0] || { contracts: [] }).contracts.map(function (c) { return c.id; });
      if (ctxIds.indexOf(cidS) < 0 || /(^|&)fail=find(&|$)/.test(params)) { return err(422, "Договор не входит в контекст чата.", "contract_id", "contract_not_in_context"); }
      var lastS = rs.messages.filter(function (x) { return x.answer; }).pop();
      var chS = lastS && (lastS.answer.clinic_choices || []).filter(function (x) { return x.contract_id === cidS; })[0];
      if (!chS) { return err(409, "В последнем ответе нет выбора клиники по этому договору.", "contract_id", "no_clinic_choice"); }
      return ok(searchClinics(cidS, chS.program, qs.get("q"), Math.max(0, parseInt(qs.get("offset"), 10) || 0)));
    }
    // Выбор клиники менеджером (CONTRACTS §8, ADR 0010): только в последнем ответе запроса, строка — из перечня
    // программы этого договора; ответ пересчитывается с выборами по договорам (slots clinic_pick:<cid>).
    if ((m = /^\/api\/requests\/([^/]+)\/clinic$/.exec(p)) && method === "POST") {
      var rc = findReq(m[1]), chc = rc && findChat(rc.chat_id);
      if (!rc) { return err(404, "Запрос не найден."); }
      if (rc.status !== "open") { return err(409, "Запрос закрыт. Верните его в работу, чтобы продолжить."); }
      var lastA = rc.messages.filter(function (x) { return x.answer; }).pop();
      var chA = lastA && (lastA.answer.clinic_choices || []).filter(function (x) { return x.contract_id === body.contract_id; })[0];
      if (!chA) { return err(409, "В последнем ответе нет выбора клиники по этому договору.", "contract_id", "no_clinic_choice"); }
      var rowC = body.choice === "none" ? null : CLINICS[body.choice];
      if (body.choice !== "none" && (!rowC || rowC.contract_id !== body.contract_id || rowC.program !== chA.program)) {
        return err(422, "Строка не из перечня программы договора.", "choice", "clinic_row_unknown");
      }
      if (/(^|&)fail=clinic(&|$)/.test(params)) { return err(422, "Строка не из перечня программы договора.", "choice", "clinic_row_unknown"); }
      rc.slots["clinic_pick:" + body.contract_id] = body.choice;
      var picks = {};
      Object.keys(rc.slots).forEach(function (k) { if (k.indexOf("clinic_pick:") === 0) { picks[k.slice(12)] = rc.slots[k]; } });
      var ac = answerFor(rc._clinic_q || ASKED.toLowerCase(), chc, picks);
      var umc = { role: "user", text: rowC ? "Клиника: " + rowC.clinic_name + ", " + [rowC.city, rowC.address].filter(Boolean).join(", ") : "Клиники нет в перечне программы", answer: null, created_at: now() };
      var amc = { role: "assistant", text: null, answer: ac, created_at: now() };
      rc.messages.push(umc, amc);
      save();
      return ok({ message: amc, answer: ac });
    }
    if ((m = /^\/api\/drafts\/([^/]+)$/.exec(p)) && method === "PATCH") {
      var found = null;
      db.requests.forEach(function (x) { x.drafts.forEach(function (dd) { if (dd.id === m[1]) { found = dd; } }); });
      if (!found) { return err(404, "Черновик не найден."); }
      var owner = db.requests.filter(function (x) { return x.drafts.indexOf(found) >= 0; })[0];
      if (owner && owner.status !== "open") { return err(409, "Запрос закрыт. Верните его в работу, чтобы продолжить."); }
      if (typeof body.subject === "string") { found.subject = body.subject; }
      if (typeof body.body === "string") { found.body = body.body; }
      found.edited_at = now(); save();
      return ok(found);
    }
    return err(404, "Страница не найдена. Вернитесь в чат.");
  }
  function delay(ms) { return new Promise(function (res) { setTimeout(res, ms); }); }

  window.DMS_MOCK = {
    request: function (method, url, body) { return delay(/\/clinic$/.test(url) ? SLOW * 2 : /clinic-candidates/.test(url) ? 250 + Math.floor(Math.random() * 300)
      : /\/tables\/try$/.test(url) ? 450 : /^\/api\/clients\/contracts\/[^/]+\/files$/.test(url) ? 300 + Math.floor(Math.random() * 500) : /^\/api\/clients/.test(url) ? 120 + Math.floor(Math.random() * 120) : 60).then(function () { return route(method, url, body); }); },
    // Поток SSE: стадии route → contract cN (по каждому договору контекста) → verify → compose, затем итог.
    stream: function (url, body, onStage) {
      var m = /^\/api\/requests\/([^/]+)\/messages$/.exec(url), r = m && findReq(m[1]), chat = r && findChat(r.chat_id);
      if (!r) { return Promise.resolve(err(404, "Запрос не найден.")); }
      var contracts = chat.context.contract_id ? [chat.context.contract_id] : (GROUPS.filter(function (g) { return g.id === chat.context.group_id; })[0] || { contracts: [] }).contracts.map(function (c) { return c.id; });
      var stages = [{ stage: "route" }].concat(contracts.map(function (cid) { return { stage: "contract", contract_id: cid }; })).concat([{ stage: "verify" }, { stage: "compose" }]);
      var p = Promise.resolve();
      stages.forEach(function (s) { p = p.then(function () { return delay(SLOW); }).then(function () { onStage(s); }); });
      return p.then(function () { return delay(SLOW / 2); }).then(function () {
        if (/ошибк/.test(body.text.toLowerCase())) { return err(503, "Модель не ответила вовремя. Повторите вопрос через минуту — ответ без модели покажет найденные пункты."); }
        var a = answerFor(body.text, chat);
        var um = { role: "user", text: body.text, answer: null, created_at: now() }, am = { role: "assistant", text: null, answer: a, created_at: now() };
        r.messages.push(um, am); r.scenario = r.scenario || a.scenario;
        // Новый вопрос сбрасывает выборы клиники (CONTRACTS §8.1); вопрос запоминается для пересчёта.
        Object.keys(r.slots).forEach(function (k) { if (k.indexOf("clinic_pick:") === 0) { delete r.slots[k]; } });
        r._clinic_q = body.text;
        if (a.alternatives.length) { r.slots.clinic = r.slots.clinic || "Медцентр Плюс"; }
        save();
        return ok({ message: am, answer: a });
      });
    }
  };
})();
