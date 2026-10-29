/* Страница «Документы» для оператора (план §8, ТЗ п. 7.1): дерево ГК → юрлица (договоры) → документы со статусом
   готовности и предупреждениями, карточка договора, просмотр пунктов документа, загрузка файла с метаданными,
   импорт комплекта. API — docs/CONTRACTS.md §8. Данные сервера — только textContent. */
(function () {
  "use strict";
  var D = window.DMS, T = D.T, el = D.el, $ = D.$, clear = D.clear;
  var state = { groups: [], contractById: {}, docsCache: {}, hitRef: null };
  // Порядок статусов готовности (ТЗ п. 7.1): получен → текст извлечён → полнота проверена → индексирован → готов.
  var PIPELINE = ["received", "extracted", "checked", "indexed", "ready"];

  function showAlert(text) { var a = $("alert"); a.textContent = text || ""; a.hidden = !text; }
  function kv(rows, cls) {
    var dl = el("dl", { class: "kv" + (cls ? " " + cls : "") });
    rows.forEach(function (r) {
      if (r[1] === null || r[1] === undefined || r[1] === "") { return; }
      dl.appendChild(el("dt", { text: r[0] }));
      dl.appendChild(el("dd", null, [r[1]]));
    });
    return dl;
  }

  // ---------- дерево ----------
  function loadTree() {
    var tree = $("tree");
    return D.request("GET", "/api/groups").then(function (r) {
      tree.removeAttribute("aria-busy");
      clear(tree);
      if (!r.ok) { tree.appendChild(el("p", { class: "alert", text: T("docs.tree_error") + " " + D.errorText(r) })); return; }
      state.groups = r.data || [];
      state.contractById = {};
      if (!state.groups.length) { tree.appendChild(el("p", { class: "muted", text: T("docs.tree_empty") })); }
      state.groups.forEach(function (g) {
        var inner = el("div", { class: "tree__in" });
        inner.appendChild(branch(T("docs.group_docs"), null, { group_id: g.id }, null));
        (g.contracts || []).forEach(function (c) {
          c.group_id = g.id; state.contractById[c.id] = c;
          var sub = [c.number ? "№ " + c.number : null, T("insurers." + (c.insurer || "unknown"))].filter(Boolean).join(" · ");
          inner.appendChild(branch(c.legal_entity, sub, { contract_id: c.id }, c.id));
        });
        tree.appendChild(el("details", { class: "tree__group", open: true }, [el("summary", null, [el("span", { class: "ico ico--sm ico--building tree__ico", "aria-hidden": "true" }), el("strong", { text: g.name })]), inner]));
      });
      fillUploadSelects();
    });
  }
  function branch(title, sub, query, contractId) {
    var list = el("ul", { class: "tree__docs" }, [el("li", { class: "muted", text: T("common.loading") })]);
    var d = el("details", { class: "tree__branch" }, [
      el("summary", null, [el("span", { text: title }), sub ? el("span", { class: "navlist__sub", text: sub }) : null]),
      contractId ? el("p", null, [el("button", { type: "button", class: "btn btn--small btn--link", text: T("docs.contract_card"),
        on: { click: function () { showContract(contractId); } } })]) : null,
      list
    ]);
    var loaded = false;
    d.addEventListener("toggle", function () { if (d.open && !loaded) { loaded = true; loadDocs(query, list); } });
    d._reload = function () { loaded = true; loadDocs(query, list); };
    d.setAttribute("data-key", contractId || "g:" + query.group_id);
    return d;
  }
  function loadDocs(query, list) {
    var qs = Object.keys(query).map(function (k) { return k + "=" + encodeURIComponent(query[k]); }).join("&");
    return D.request("GET", "/api/documents?" + qs).then(function (r) {
      clear(list);
      if (!r.ok) { list.appendChild(el("li", { class: "alert", text: D.errorText(r) })); return; }
      // Общие документы группы — только без contract_id (документы договоров живут в своих ветках).
      var docs = (r.data || []).filter(function (doc) { return query.contract_id || !doc.contract_id; });
      if (!docs.length) { list.appendChild(el("li", { class: "muted", text: T("docs.docs_empty") })); return; }
      docs.forEach(function (doc) {
        state.docsCache[doc.id] = doc;
        list.appendChild(el("li", null, [el("button", { type: "button", class: "tree__doc", "data-doc": doc.id, "aria-current": D.readHash().doc === doc.id ? "true" : null, on: { click: function () { showDoc(doc.id); } } }, [
          el("span", { class: "tree__title", text: doc.title }),
          el("span", { class: "tree__chips" }, [D.docStatusChip(doc.status),
            doc.uploaded_by === "chat" ? D.chip(T("attach.from_chat"), "chip--cond") : null,
            (doc.warnings || []).length ? D.chip(T("docs.warnings_n", { n: doc.warnings.length }), "chip--warn chip--bang") : null])
        ])]));
      });
    });
  }

  // ---------- карточка договора ----------
  function showContract(id) {
    var body = $("detail-body");
    clear(body); body.appendChild(el("p", { class: "muted", text: T("common.loading") }));
    D.request("GET", "/api/contracts/" + encodeURIComponent(id)).then(function (r) {
      clear(body);
      if (!r.ok) { body.appendChild(el("p", { class: "alert", text: D.errorText(r) })); return; }
      var c = r.data;
      $("detail-h").textContent = c.legal_entity + (c.number ? " · № " + c.number : "");
      var f = function (k) { return T("docs.contract_fields." + k); };
      var programs = el("ul", { class: "list" }, (c.programs || []).map(function (p) {
        return el("li", { text: p.name + ((p.regions || []).length ? " · " + p.regions.join(", ") : "") + (p.persons ? " · " + p.persons : "") });
      }));
      body.appendChild(kv([
        [f("legal_entity"), c.legal_entity], [f("number"), c.number], [f("insurer"), T("insurers." + (c.insurer || "unknown"))],
        [f("date"), c.date ? D.fmtDate(c.date) : null],
        [f("period"), c.period_from || c.period_to ? D.fmtDate(c.period_from) + " — " + D.fmtDate(c.period_to) : null],
        [f("headcount"), c.headcount ? String(c.headcount) : null],
        [f("franchise"), (c.franchise || {}).declared ? T("docs.franchise_yes") : T("docs.franchise_no")]
      ]));
      if ((c.programs || []).length) { body.appendChild(el("h3", { class: "sub", text: f("programs") })); body.appendChild(programs); }
      if ((c.missing_docs || []).length) {
        body.appendChild(el("h3", { class: "sub", text: f("missing_docs") }));
        body.appendChild(el("ul", { class: "list" }, c.missing_docs.map(function (m) { return el("li", null, [D.chip(m, "chip--warn chip--bang")]); })));
      }
      var docs = c.documents || [];
      if (docs.length) {
        body.appendChild(el("h3", { class: "sub", text: T("docs.title") }));
        body.appendChild(el("ul", { class: "tree__docs" }, docs.map(function (doc) {
          state.docsCache[doc.id] = doc;
          return el("li", null, [el("button", { type: "button", class: "tree__doc", on: { click: function () { showDoc(doc.id); } } }, [
            el("span", { class: "tree__title", text: doc.title }), el("span", { class: "tree__chips" }, [D.docStatusChip(doc.status)])])]);
        })));
      }
      $("detail").focus();
    });
  }

  // ---------- документ ----------
  function pipeline(status) {
    var cur = PIPELINE.indexOf(status);
    var ol = el("ol", { class: "steps steps--compact", "aria-label": T("docs.pipeline") });
    PIPELINE.forEach(function (st, i) {
      var cls = status === "error" ? (i === 0 ? "is-done" : "is-next") : i < cur ? "is-done" : i === cur ? "is-current" : "is-next";
      ol.appendChild(el("li", { class: "steps__item " + cls, "aria-current": i === cur ? "step" : null }, [
        el("span", { class: "steps__mark", "aria-hidden": "true", text: i <= cur ? "✓" : String(i + 1) }), el("span", { class: "steps__label", text: T("doc_status." + st) })]));
    });
    if (status === "error") { ol.appendChild(el("li", { class: "steps__item is-error", "aria-current": "step" }, [el("span", { class: "steps__mark", "aria-hidden": "true", text: "✗" }), el("span", { class: "steps__label", text: T("doc_status.error") })])); }
    return ol;
  }
  function showDoc(id, clauseNumber) {
    var body = $("detail-body");
    clear(body); body.appendChild(el("p", { class: "muted", text: T("common.loading") }));
    D.writeHash({ doc: id, clause: clauseNumber || null });
    Array.prototype.forEach.call(document.querySelectorAll(".tree__doc"), function (b) { b.setAttribute("aria-current", b.getAttribute("data-doc") === id ? "true" : "false"); });
    D.request("GET", "/api/documents/" + encodeURIComponent(id)).then(function (r) {
      clear(body);
      if (!r.ok) { body.appendChild(el("p", { class: "alert", text: D.errorText(r) })); return; }
      var doc = r.data, f = function (k) { return T("docs.doc_fields." + k); };
      // Переход по ссылке «Открыть документ»: раскрываем ветку дерева, где лежит документ.
      var br = document.querySelector('.tree__branch[data-key="' + String(doc.contract_id || "g:" + doc.group_id).replace(/"/g, "") + '"]');
      if (br && !br.open) { br.open = true; }
      $("detail-h").textContent = doc.title;
      var owner = doc.contract_id && state.contractById[doc.contract_id] ? state.contractById[doc.contract_id].legal_entity : null;
      body.appendChild(el("div", { class: "doc__chips" }, [D.docStatusChip(doc.status), D.chip(T("doc_types." + doc.doc_type)), owner ? el("span", { class: "muted", text: owner }) : null]));
      body.appendChild(el("h3", { class: "sub", text: T("docs.pipeline") }));
      body.appendChild(pipeline(doc.status));
      if ((doc.warnings || []).length) {
        body.appendChild(el("h3", { class: "sub", text: T("docs.warnings") }));
        body.appendChild(el("ul", { class: "warns" }, doc.warnings.map(function (w) { return el("li", null, [D.chip("!", "chip--warn chip--bang chip--icon"), " ", w]); })));
      }
      body.appendChild(kv([
        [f("doc_type"), T("doc_types." + doc.doc_type)], [f("original_format"), String(doc.original_format || "").toUpperCase()],
        [f("uploaded_by"), doc.uploaded_by === T("docs.upload_by_default") ? T("docs.upload_by_placeholder") : doc.uploaded_by], [f("uploaded_at"), D.fmtDateTime(doc.uploaded_at)],
        [f("precedence_level"), doc.precedence_level === null || doc.precedence_level === undefined ? null : String(doc.precedence_level)],
        [f("source_path"), doc.source_path], [f("sha256"), doc.sha256 ? el("code", { text: String(doc.sha256).slice(0, 16) + "…" }) : null]
      ], "doc__meta"));
      body.appendChild(el("h3", { class: "sub", text: T("docs.clauses") }));
      var box = el("div", { class: "clauses" });
      body.appendChild(box);
      loadClauses(doc, box, clauseNumber);
    });
  }
  // Список пунктов: GET /api/documents/{id}/clauses (если сервер его даёт); иначе — поиск пункта по номеру
  // через GET /api/documents/{id}/clause?number= (CONTRACTS §8).
  function loadClauses(doc, box, clauseNumber) {
    D.request("GET", "/api/documents/" + encodeURIComponent(doc.id) + "/clauses").then(function (r) {
      var list = r.ok && Array.isArray(r.data) ? r.data : r.ok && r.data && Array.isArray(r.data.clauses) ? r.data.clauses : null;
      if (!list) { lookupForm(doc, box, clauseNumber); return; }
      var fid = "cl-filter-" + doc.id;
      var filter = el("input", { type: "search", id: fid, autocomplete: "off" });
      var ol = el("ol", { class: "clauses__list" });
      // Переход из чата (#doc=…&clause=…&ref=…): подсвечиваем пункт по id ссылки цитаты — номер пункта в документе
      // бывает не один (ревизия w16); id не нашёлся — по номеру.
      var byRef = !!state.hitRef && list.some(function (c) { return c.id === state.hitRef; });
      function render() {
        var q = filter.value.trim().toLowerCase();
        clear(ol);
        list.filter(function (c) { return !q || (c.number || "").toLowerCase().indexOf(q) === 0 || (c.text || "").toLowerCase().indexOf(q) >= 0; }).slice(0, 400).forEach(function (c) {
          ol.appendChild(clauseItem(c, byRef ? c.id === state.hitRef : clauseNumber && c.number === clauseNumber));
        });
        if (!ol.children.length) { ol.appendChild(el("li", { class: "muted", text: T("docs.clause_not_found") })); }
      }
      filter.addEventListener("input", render);
      box.appendChild(el("div", { class: "f" }, [el("label", { for: fid, text: T("docs.clauses_filter") }), filter]));
      box.appendChild(ol);
      render();
      var hit = ol.querySelector(".is-hit");
      if (hit) { hit.scrollIntoView({ block: "center" }); }
    });
  }
  function clauseItem(c, hit) {
    return el("li", { class: "clause" + (hit ? " is-hit" : ""), id: c.number ? "cl-" + c.number : null }, [
      el("span", { class: "clause__n", text: c.number || "·" }),
      el("div", null, [c.path ? el("p", { class: "source", text: c.path }) : null, el("p", { class: "clause__t", text: c.text })])
    ]);
  }
  function lookupForm(doc, box, clauseNumber) {
    var nid = "cl-num-" + doc.id;
    var input = el("input", { type: "text", id: nid, value: clauseNumber || "", autocomplete: "off", maxlength: "40" });
    var out = el("div", { "aria-live": "polite" });
    var form = el("form", { class: "lookup" }, [el("p", { class: "muted", text: T("docs.clauses_empty") }),
      el("div", { class: "lookup__row" }, [el("div", { class: "f" }, [el("label", { for: nid, text: T("docs.clause_number") }), input]),
        el("button", { type: "submit", class: "btn btn--small", text: T("docs.clause_lookup") })])]);
    function go() {
      var n = input.value.trim();
      clear(out);
      if (!n) { return; }
      D.request("GET", "/api/documents/" + encodeURIComponent(doc.id) + "/clause?number=" + encodeURIComponent(n)).then(function (r) {
        clear(out);
        if (!r.ok || !r.data || !r.data.clause) { out.appendChild(el("p", { class: "alert", text: r.ok ? T("docs.clause_not_found") : D.errorText(r) })); return; }
        out.appendChild(el("ol", { class: "clauses__list" }, [clauseItem(r.data.clause, true)]));
      });
    }
    form.addEventListener("submit", function (e) { e.preventDefault(); D.writeHash({ doc: doc.id, clause: input.value.trim() || null }); go(); });
    box.appendChild(form); box.appendChild(out);
    if (clauseNumber) { go(); }
  }

  // ---------- загрузка и импорт ----------
  function fillUploadSelects() {
    var gs = $("up-group"), cs = $("up-contract");
    clear(gs);
    state.groups.forEach(function (g) { gs.appendChild(el("option", { value: g.id, text: g.name })); });
    function contracts() {
      clear(cs);
      cs.appendChild(el("option", { value: "", text: T("docs.group_docs") }));
      var g = state.groups.filter(function (x) { return x.id === gs.value; })[0];
      ((g && g.contracts) || []).forEach(function (c) { cs.appendChild(el("option", { value: c.id, text: c.legal_entity + (c.number ? " · № " + c.number : "") })); });
    }
    gs.onchange = contracts;
    contracts();
  }
  function upload(e) {
    e.preventDefault();
    var al = $("up-alert"), st = $("up-status"), file = $("up-file").files[0];
    al.hidden = true; st.textContent = "";
    if (!file) { al.textContent = T("errors.choose_file"); al.hidden = false; $("up-pick").focus(); return; }
    if (!$("up-group").value && !$("up-contract").value) { al.textContent = T("errors.choose_owner"); al.hidden = false; return; }
    var fd = new FormData();
    fd.append("file", file);
    fd.append("group_id", $("up-group").value);
    fd.append("contract_id", $("up-contract").value);
    if ($("up-type").value) { fd.append("doc_type", $("up-type").value); }
    // Пустое поле — в API уходит служебное значение «operator» (docs.upload_by_default), на экране — «оператор».
    fd.append("uploaded_by", $("up-by").value.trim() || T("docs.upload_by_default"));
    $("up-send").disabled = true; st.textContent = T("docs.upload_running");
    D.request("POST", "/api/documents", fd).then(function (r) {
      $("up-send").disabled = false;
      if (!r.ok) { st.textContent = ""; al.textContent = D.errorText(r); al.hidden = false; return; }
      st.textContent = T("docs.upload_done", { title: r.data.title });
      $("upload-form").reset();
      showFileName();
      fillUploadSelects();
      var key = r.data.contract_id || "g:" + r.data.group_id;
      var br = document.querySelector('.tree__branch[data-key="' + key.replace(/"/g, "") + '"]');
      if (br) { br.open = true; br._reload(); }
      showDoc(r.data.id);
    });
  }
  function importKit() {
    var btn = $("import-kit"), st = $("import-status");
    // Импорт пересобирает базу всех договоров — только после подтверждения (ревью сайта 29.10.2026)
    if (!window.confirm(T("docs.import_confirm"))) { return; }
    btn.disabled = true; st.textContent = T("docs.import_running"); showAlert("");
    D.request("POST", "/api/documents/import-kit").then(function (r) {
      btn.disabled = false;
      if (!r.ok) { st.textContent = ""; showAlert(D.errorText(r)); return; }
      // Отчёт импорта без текстов документов: показываем только числовые и короткие поля верхнего уровня.
      var parts = [];
      Object.keys(r.data || {}).forEach(function (k) {
        var v = r.data[k];
        if (typeof v === "number" || typeof v === "boolean" || (typeof v === "string" && v.length < 80)) { parts.push(k + ": " + v); }
        else if (Array.isArray(v)) { parts.push(k + ": " + v.length); }
      });
      st.textContent = T("docs.import_done") + (parts.length ? " · " + parts.join(" · ") : "");
      loadTree();
    });
  }

  // Своя кнопка выбора файла: подпись из ui.yaml, имя выбранного файла — рядом (системная подпись — на языке браузера).
  function showFileName() {
    var f = $("up-file").files[0];
    $("up-file-name").textContent = f ? f.name : T("docs.upload_no_file");
  }
  function init() {
    $("upload-form").addEventListener("submit", upload);
    $("up-pick").addEventListener("click", function () { $("up-file").click(); });
    $("up-file").addEventListener("change", showFileName);
    $("import-kit").addEventListener("click", importKit);
    // Договор клиента подключён к поиску (clients.js, ADR 0011) — дерево и списки загрузки обновляются.
    document.addEventListener("dms:groups-changed", function () { loadTree(); });
    loadTree().then(function () {
      var h = D.readHash();
      if (h.doc) {
        state.hitRef = h.ref || null;
        showDoc(h.doc, h.clause || null);
      }
    });
  }
  D.ready.then(init);
})();
