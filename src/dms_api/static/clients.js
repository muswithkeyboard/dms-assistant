/* Вкладка «Подключение клиента» на странице «Документы» (ADR 0011, docs/CONTRACTS.md §9).
   Мастер «Новый клиент» → загрузка файлов по договорам и общих файлов группы → обработка (фоновое задание,
   опрос раз в секунду) → отчёт о готовности → чат по договору. Ход обработки — шаги ТЗ п. 7.1 «иконка + слово».
   Тексты — config/texts_ru/ui.yaml (раздел clients), данные сервера — только textContent.
   Общие файлы группы приходят в files каждого договора со scope "group"; загружаются через первый договор группы
   (POST …/contracts/{uNN}/files, scope=group), правятся и удаляются через тот договор, в списке которого пришли. */
(function () {
  "use strict";
  var D = window.DMS, T = D.T, el = D.el, $ = D.$, clear = D.clear;
  // Стадии задания (OnboardingJob.stage) и итоговый шаг «Готов» — порядок ТЗ п. 7.1.
  var STAGES = ["received", "extracted", "checked", "indexed", "selfcheck", "done"];
  var INSURERS = ["zetta", "renaissance", "sogaz"];
  var EXT = /\.(docx|xlsx|pdf|txt)$/i;
  var POLL_MS = 1000;            // опрос задания раз в секунду (бриф w8-ui)
  var POLL_MAX_MISSES = 60;      // столько неудачных опросов подряд — и опрос прекращается с пояснением
  var STATE_CHIP = { "new": "chip--none", running: "chip--cond", ready: "chip--ok", limited: "chip--warn chip--bang",
                     failed: "chip--danger chip--x", error: "chip--danger chip--x" };
  var HEADLINE = { ready: "headline--allowed", limited: "headline--conditional", failed: "headline--denied" };
  var state = { clients: [], current: null, loaded: false, polls: {}, lost: {}, lastStage: {}, dirty: {}, msg: {},
                rowSeq: 0, del: null, focusAfter: null, liveTimer: null, chat: null, map: null,
                profiles: { loaded: false, loading: false, error: null, list: [] } };

  function enc(s) { return encodeURIComponent(s); }
  function live(text) {
    var n = $("cl-live");
    if (state.liveTimer) { clearTimeout(state.liveTimer); }
    n.textContent = "";
    state.liveTimer = setTimeout(function () { state.liveTimer = null; n.textContent = text; }, 30);
  }
  // Смена клиента: прежнее объявление (ход обработки другого клиента) не зачитывается в новом контексте.
  function resetLive() {
    if (state.liveTimer) { clearTimeout(state.liveTimer); state.liveTimer = null; }
    var n = $("cl-live"); if (n) { n.textContent = ""; }
  }
  function groupById(id) { return state.clients.filter(function (g) { return g.id === id; })[0] || null; }
  function findContract(cid) {
    var out = null;
    state.clients.forEach(function (g) { (g.contracts || []).forEach(function (c) { if (c.id === cid) { out = { g: g, c: c }; } }); });
    return out;
  }
  function errCode(r) { return r && r.data && r.data.error ? r.data.error.code : null; }
  // Ошибка по коду из ui.yaml (clients.errors.*), иначе — сообщение сервера (по-русски) или общий текст.
  function errText(r) {
    var code = errCode(r);
    if (code && typeof D.cfg("clients.errors." + code) === "string") { return T("clients.errors." + code); }
    return D.errorText(r);
  }
  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) { return T("clients.size_b", { n: n }); }
    if (n < 1024 * 1024) { return T("clients.size_kb", { n: Math.round(n / 1024) }); }
    return T("clients.size_mb", { n: (n / 1048576).toFixed(1).replace(".", ",") });
  }

  // ---------- состояние договора: по последнему заданию и его отчёту ----------
  function isActive(job) { return job && (job.state === "queued" || job.state === "running"); }
  function contractState(c) {
    var j = c.job;
    if (!j) { return "new"; }
    if (isActive(j)) { return "running"; }
    if (j.state === "error") { return "error"; }
    return (j.report && j.report.readiness) || "failed";
  }
  function stateChip(c) { var s = contractState(c); return D.chip(T("clients.states." + s), STATE_CHIP[s]); }
  // Файлы изменены после обработки. Источник — сервер (files_changed: файлы отличаются от снимка последней
  // успешной обработки); без признака (демо-API) — по времени загрузки и правкам в этом окне.
  function isStale(c) {
    var j = c.job;
    if (j && isActive(j)) { return false; }
    if (typeof c.files_changed === "boolean") { return c.files_changed; }
    if (!j) { return false; }
    if (state.dirty[c.id]) { return true; }
    var t = Date.parse(j.finished_at || j.created_at);
    return (c.files || []).some(function (f) { return Date.parse(f.uploaded_at) > t; });
  }
  function contractFiles(c) {
    return (c.files || []).filter(function (f) { return f.scope !== "group"; }).map(function (f) { return Object.assign({ _owner: c.id }, f); });
  }
  function groupFiles(g) {
    var seen = {}, out = [];
    (g.contracts || []).forEach(function (c) {
      (c.files || []).forEach(function (f) {
        if (f.scope === "group" && !seen[f.name]) { seen[f.name] = true; out.push(Object.assign({ _owner: c.id }, f)); }
      });
    });
    return out;
  }
  function markDirty(g, scope, cid) {
    (g.contracts || []).forEach(function (c) { if (c.job && (scope === "group" || c.id === cid)) { state.dirty[c.id] = true; } });
  }

  // ---------- вкладки страницы ----------
  function setTab(name, focus) {
    ["docs", "clients"].forEach(function (n) {
      var sel = n === name, tab = $("tab-" + n);
      tab.setAttribute("aria-selected", sel ? "true" : "false");
      tab.tabIndex = sel ? 0 : -1;
      $("panel-" + n).hidden = !sel;
    });
    if (focus) { $("tab-" + name).focus(); }
    if (name === "clients") {
      writeState();
      if (!state.loaded) { loadClients(); }
    } else if (D.readHash().tab) { D.writeHash({}); }
  }
  function writeState() { D.writeHash({ tab: "clients", client: state.current }); }
  function tabKeys(e) {
    var keys = { ArrowRight: 1, ArrowLeft: 1, Home: 1, End: 1 };
    if (!keys[e.key]) { return; }
    e.preventDefault();
    var next = e.key === "Home" ? "docs" : e.key === "End" ? "clients" : e.currentTarget.id === "tab-docs" ? "clients" : "docs";
    setTab(next, true);
  }

  // ---------- список клиентов ----------
  function loadClients() {
    var list = $("cl-list");
    list.setAttribute("aria-busy", "true");
    return D.request("GET", "/api/clients").then(function (r) {
      list.removeAttribute("aria-busy");
      state.loaded = true;
      if (!r.ok) {
        clear(list);
        list.appendChild(el("li", { class: "alert", text: T("clients.list_error") + " " + D.errorText(r) }));
        return;
      }
      state.clients = r.data || [];
      // Задание, начатое раньше (другая вкладка, обновление страницы), — продолжаем опрос.
      state.clients.forEach(function (g) {
        (g.contracts || []).forEach(function (c) { if (isActive(c.job)) { startPoll(c.job.id, c.id); } });
      });
      if (state.current && state.current !== "new" && !groupById(state.current)) { state.current = null; writeState(); }
      renderList();
      // Мастер с введёнными данными не перерисовываем (завершилась обработка другого договора и т. п.).
      if (state.current === "new" && $("cl-form")) { return; }
      renderMain();
    });
  }
  function renderList() {
    var list = $("cl-list");
    clear(list);
    if (!state.clients.length) { list.appendChild(el("li", { class: "navlist__empty muted", text: T("clients.list_empty") })); return; }
    state.clients.forEach(function (g) {
      var cs = g.contracts || [];
      list.appendChild(el("li", null, [el("button", {
        type: "button", class: "navlist__item cl-item", id: "cl-item-" + g.id, "aria-current": state.current === g.id ? "true" : "false",
        on: { click: function () { openGroup(g.id); } }
      }, [
        el("span", { class: "navlist__title", text: g.name }),
        el("span", { class: "navlist__sub", text: T("clients.contracts_n", { n: cs.length }) }),
        el("span", { class: "cl-item__rows" }, cs.map(function (c) {
          return el("span", { class: "cl-item__row" }, [el("span", { class: "cl-item__le", text: c.legal_entity }), stateChip(c),
            isStale(c) ? D.chip(T("clients.stale_chip"), "chip--warn chip--q") : null]);
        }))
      ])]));
    });
  }
  function openGroup(id) {
    if (state.current !== id) { resetLive(); state.map = null; }
    state.current = id;
    state.msg["top"] = null;
    writeState();
    renderList();
    renderMain();
    var h = $("cl-group-h");
    if (h) { h.focus(); }
  }
  function openNew() {
    resetLive();
    state.current = "new";
    writeState();
    renderList();
    renderMain();
    $("cl-name").focus();
  }
  // Перерисовка правой части с сохранением фокуса: у элементов стабильные id, фокус возвращается на тот же id.
  function renderMain() {
    var keep = state.focusAfter || (document.activeElement && document.activeElement.id);
    state.focusAfter = null;
    var box = $("cl-main");
    clear(box);
    if (state.current === "new") { box.appendChild(wizard()); }
    else {
      var g = groupById(state.current);
      box.appendChild(g ? groupView(g) : el("p", { class: "muted", text: T("clients.pick") }));
    }
    if (keep) { var n = $(keep); if (n && n !== document.activeElement && !$("cl-del-dlg").open) { n.focus(); } }
  }

  // ---------- шаги мастера ----------
  function wizardSteps(cur) {
    var ol = el("ol", { class: "steps steps--compact cl-steps", "aria-label": T("clients.steps_aria") });
    ["client", "files", "process"].forEach(function (s, i) {
      var cls = i < cur ? "is-done" : i === cur ? "is-current" : "is-next";
      ol.appendChild(el("li", { class: "steps__item " + cls, "aria-current": i === cur ? "step" : null }, [
        el("span", { class: "steps__mark", "aria-hidden": "true", text: i < cur ? "✓" : String(i + 1) }),
        el("span", { class: "steps__label", text: T("clients.steps." + s) })]));
    });
    return ol;
  }
  function stepOf(g) {
    var cs = g.contracts || [];
    return cs.some(function (c) { return c.job; }) ? 2 : 1;
  }

  // ---------- мастер «Новый клиент» ----------
  function insurerSelect(id) {
    var s = el("select", { id: id });
    s.appendChild(el("option", { value: "", text: T("clients.insurer_auto") }));
    INSURERS.forEach(function (i) { s.appendChild(el("option", { value: i, text: T("insurers." + i) })); });
    return s;
  }
  function field(id, label, input) { return el("div", { class: "f" }, [el("label", { for: id, text: label }), input]); }
  // Неизвестная СК (ADR 0012): название СК текстом и «не распознавать формат» — разбор запасным адаптером.
  function insurerExtra(nameId, fgId) {
    return el("div", { class: "cl-row__more" }, [
      field(nameId, T("clients.insurer_name"), el("input", { type: "text", id: nameId, maxlength: "120", placeholder: T("clients.insurer_name_ph") })),
      el("div", { class: "f cl-fg" }, [
        el("label", { class: "check", for: fgId }, [el("input", { type: "checkbox", id: fgId, "aria-describedby": fgId + "-hint" }),
          el("span", { text: T("clients.force_generic") })]),
        el("p", { class: "hint", id: fgId + "-hint", text: T("clients.force_generic_hint") })])]);
  }
  function wizard() {
    var form = el("form", { class: "cl-wizard", id: "cl-form", novalidate: true, "aria-labelledby": "cl-form-h" });
    form.appendChild(wizardSteps(0));
    form.appendChild(el("h2", { id: "cl-form-h", tabindex: "-1", text: T("clients.form_title") }));
    form.appendChild(el("p", { class: "hint", text: T("clients.form_hint") }));
    form.appendChild(field("cl-name", T("clients.group_name"), el("input", { type: "text", id: "cl-name", maxlength: "160",
      autocomplete: "organization", placeholder: T("clients.group_name_ph"), required: true })));
    var rows = el("div", { class: "cl-rows", id: "cl-rows" });
    form.appendChild(el("fieldset", { class: "cl-entities" }, [el("legend", { class: "sub", text: T("clients.entities") }), rows,
      el("button", { type: "button", class: "btn btn--small", id: "cl-add-row", text: "+ " + T("clients.add_entity"),
        on: { click: function () { addRow(rows, true); } } })]));
    form.appendChild(el("p", { class: "alert", id: "cl-form-alert", role: "alert", hidden: true }));
    form.appendChild(el("div", { class: "cl-form__actions" }, [
      el("button", { type: "button", class: "btn", id: "cl-form-cancel", text: T("common.cancel"),
        on: { click: function () { state.current = null; writeState(); renderList(); renderMain(); $("cl-new").focus(); } } }),
      el("button", { type: "submit", class: "btn btn--primary", id: "cl-create", text: T("clients.create") })]));
    addRow(rows, false);
    form.addEventListener("submit", submitWizard);
    return form;
  }
  function addRow(rows, focus) {
    state.rowSeq += 1;
    var n = state.rowSeq;
    var row = el("div", { class: "cl-row", role: "group", "data-row": String(n) }, [
      field("cl-le-" + n, T("clients.legal_entity"), el("input", { type: "text", id: "cl-le-" + n, class: "cl-le", maxlength: "200",
        placeholder: T("clients.legal_entity_ph"), required: true })),
      field("cl-ins-" + n, T("clients.insurer"), insurerSelect("cl-ins-" + n)),
      field("cl-num-" + n, T("clients.number"), el("input", { type: "text", id: "cl-num-" + n, maxlength: "60", placeholder: T("clients.number_ph") })),
      el("button", { type: "button", class: "btn btn--small btn--link cl-row__rm", text: T("clients.remove"),
        on: { click: function () { removeRow(rows, row); } } }),
      insurerExtra("cl-iname-" + n, "cl-fg-" + n)
    ]);
    rows.appendChild(row);
    renumber(rows);
    if (focus) { $("cl-le-" + n).focus(); }
  }
  function removeRow(rows, row) {
    var next = row.nextElementSibling || row.previousElementSibling;
    rows.removeChild(row);
    renumber(rows);
    if (next) { next.querySelector(".cl-le").focus(); } else { $("cl-add-row").focus(); }
  }
  function renumber(rows) {
    var all = rows.querySelectorAll(".cl-row");
    Array.prototype.forEach.call(all, function (r, i) {
      r.setAttribute("aria-label", T("clients.entity_n", { n: i + 1 }));
      var b = r.querySelector(".cl-row__rm");
      b.setAttribute("aria-label", T("clients.remove_entity", { n: i + 1 }));
      b.disabled = all.length === 1;
    });
  }
  function formError(text, input) {
    var al = $("cl-form-alert");
    al.textContent = text; al.hidden = !text;
    if (input) { input.setAttribute("aria-invalid", "true"); input.focus(); }
  }
  function submitWizard(e) {
    e.preventDefault();
    Array.prototype.forEach.call(document.querySelectorAll("#cl-form [aria-invalid]"), function (n) { n.removeAttribute("aria-invalid"); });
    formError("");
    var name = $("cl-name").value.trim();
    if (!name) { formError(T("clients.err_name"), $("cl-name")); return; }
    var contracts = [], bad = null;
    Array.prototype.forEach.call(document.querySelectorAll("#cl-rows .cl-row"), function (r, i) {
      var n = r.getAttribute("data-row"), le = $("cl-le-" + n);
      if (!le.value.trim() && !bad) { bad = { n: i + 1, input: le }; }
      contracts.push({ legal_entity: le.value.trim(), insurer: $("cl-ins-" + n).value || null, number: $("cl-num-" + n).value.trim() || null,
        insurer_name: $("cl-iname-" + n).value.trim() || null, force_generic: $("cl-fg-" + n).checked });
    });
    if (bad) { formError(T("clients.err_entity", { n: bad.n }), bad.input); return; }
    var btn = $("cl-create");
    btn.disabled = true; btn.textContent = T("clients.creating");
    D.request("POST", "/api/clients", { name: name, contracts: contracts }).then(function (r) {
      btn.disabled = false; btn.textContent = T("clients.create");
      if (!r.ok) { formError(D.errorText(r)); return; }
      state.current = r.data.id;
      state.msg["top"] = { ok: T("clients.created", { name: r.data.name }), errs: [] };
      state.focusAfter = "cl-group-h";
      writeState();
      return loadClients().then(function () { live(T("clients.created", { name: r.data.name })); });
    });
  }

  // ---------- карточка группы ----------
  function groupView(g) {
    var cs = g.contracts || [];
    var wrap = el("div", { class: "cl-group" });
    wrap.appendChild(wizardSteps(stepOf(g)));
    wrap.appendChild(el("h2", { id: "cl-group-h", tabindex: "-1", text: g.name }));
    wrap.appendChild(msgBlock("top"));
    wrap.appendChild(el("p", { class: "hint", text: T("clients.process_hint") }));
    cs.forEach(function (c) { wrap.appendChild(contractCard(g, c)); });
    var key = "g-" + g.id;
    var gcard = el("section", { class: "card cl-card", "aria-labelledby": "cl-gf-h" }, [
      el("h3", { id: "cl-gf-h", text: T("clients.group_files_title") }), el("p", { class: "hint", text: T("clients.group_files_hint") })]);
    if (cs.length) {
      gcard.appendChild(dropZone(key, T("clients.group_files_for"), g, cs[0].id, "group"));
      gcard.appendChild(fileTable(groupFiles(g), key, g, "group"));
    } else {
      gcard.appendChild(el("p", { class: "muted", text: T("clients.group_files_need_contract") }));
    }
    wrap.appendChild(gcard);
    wrap.appendChild(profilesCard());
    wrap.appendChild(addContractForm(g));
    return wrap;
  }
  function msgBlock(key) {
    var m = state.msg[key], box = el("div", { class: "cl-msg", id: "cl-msg-" + key });
    if (!m) { return box; }
    if (m.ok) { box.appendChild(el("p", { class: "hint cl-ok", text: m.ok })); }
    if ((m.errs || []).length) { box.appendChild(el("ul", { class: "alert cl-errs" }, m.errs.map(function (t) { return el("li", { text: t }); }))); }
    return box;
  }
  function contractCard(g, c) {
    var k = c.id, st = contractState(c), stale = isStale(c);
    var meta = [c.insurer_hint ? T("clients.insurer_hint", { name: T("insurers." + c.insurer_hint) })
      : c.insurer_name ? T("clients.insurer_name_label", { name: c.insurer_name }) : T("clients.insurer_hint_auto"),
      c.number ? T("clients.number_label", { n: c.number }) : null, c.id].filter(Boolean).join(" · ");
    var card = el("section", { class: "card cl-card", id: "cl-c-" + k, "aria-labelledby": "cl-h-" + k }, [
      el("div", { class: "cl-card__head" }, [
        el("h3", { class: "cl-card__title", id: "cl-h-" + k, tabindex: "-1", text: c.legal_entity }),
        el("span", { class: "cl-card__chips" }, [stateChip(c), stale ? D.chip(T("clients.stale_chip"), "chip--warn chip--q") : null,
          c.force_generic ? D.chip(T("clients.force_generic_chip"), "chip--mode") : null])]),
      el("p", { class: "source", text: meta })
    ]);
    card.appendChild(dropZone(k, c.legal_entity, g, k, "contract"));
    card.appendChild(fileTable(contractFiles(c), k, g, "contract"));
    var running = st === "running";
    card.appendChild(el("div", { class: "cl-actions" }, [
      el("button", { type: "button", class: "btn btn--primary", id: "cl-proc-" + k, disabled: running,
        text: c.job ? T("clients.reprocess") : T("clients.process"), on: { click: function () { processContract(c.id); } } }),
      stale ? el("p", { class: "hint cl-stale", text: T(contractFiles(c).length ? "clients.stale" : "clients.stale_no_files") }) : null]));
    card.appendChild(msgBlock("p-" + k));
    var prog = el("div", { id: "cl-prog-" + k, class: "cl-prog" });
    card.appendChild(prog);
    if (c.job) { renderProgress(prog, c.job); }
    if (c.job && c.job.state === "done" && c.job.report) { card.appendChild(report(g, c, c.job.report)); }
    if (state.map && state.map.cid === c.id) { card.appendChild(mappingPanel(c)); }
    return card;
  }

  // ---------- зона загрузки ----------
  // Кнопка «Выбрать файлы» — настоящая кнопка (клавиатура: Tab, Enter, Пробел); перетаскивание — на всю зону.
  function dropZone(key, name, g, ownerCid, scope) {
    var input = el("input", { type: "file", class: "vh", id: "cl-file-" + key, multiple: true, accept: ".docx,.xlsx,.pdf,.txt",
      tabindex: "-1", "aria-hidden": "true" });
    input.addEventListener("change", function () { var fs = Array.prototype.slice.call(input.files || []); input.value = ""; uploadFiles(fs, g, ownerCid, scope, key); });
    var zone = el("div", { class: "drop", id: "cl-drop-" + key }, [
      el("p", { class: "drop__title" }, [el("span", { class: "drop__icon", "aria-hidden": "true", text: "⇪" }),
        el("span", { text: T("clients.drop_title") }), el("span", { class: "muted", text: T("clients.drop_or") }),
        el("button", { type: "button", class: "btn btn--small", id: "cl-pick-" + key, "aria-label": T("clients.pick_files_for", { name: name }),
          "aria-describedby": "cl-drop-hint-" + key, text: T("clients.pick_files"), on: { click: function () { input.click(); } } })]),
      el("p", { class: "hint", id: "cl-drop-hint-" + key, text: T("clients.drop_hint") }),
      el("p", { class: "hint cl-upstatus", id: "cl-up-" + key }),
      input
    ]);
    function hasFiles(e) { var t = e.dataTransfer && e.dataTransfer.types; return t && Array.prototype.indexOf.call(t, "Files") >= 0; }
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; zone.classList.add("is-over"); } });
    });
    zone.addEventListener("dragleave", function (e) { if (!zone.contains(e.relatedTarget)) { zone.classList.remove("is-over"); } });
    zone.addEventListener("drop", function (e) {
      e.preventDefault(); zone.classList.remove("is-over");
      uploadFiles(Array.prototype.slice.call(e.dataTransfer.files || []), g, ownerCid, scope, key);
    });
    return el("div", { class: "cl-zone" }, [zone, msgBlock(key)]);
  }
  // Файлы уходят по одному (сервер 2–4 ГБ): ход — в строке зоны и в области объявлений; ошибки — списком по файлам.
  function uploadFiles(files, g, ownerCid, scope, key) {
    if (!files.length) { return; }
    var errs = [], done = 0, p = Promise.resolve();
    files.forEach(function (f, i) {
      p = p.then(function () {
        if (!EXT.test(f.name)) { errs.push(T("clients.upload_failed", { name: f.name, msg: T("clients.bad_ext") })); return; }
        var line = T("clients.uploading", { i: i + 1, n: files.length, name: f.name }), st = $("cl-up-" + key);
        if (st) { st.textContent = line; }
        live(line);
        var fd = new FormData();
        fd.append("file", f);
        fd.append("scope", scope);
        return D.request("POST", "/api/clients/contracts/" + enc(ownerCid) + "/files", fd).then(function (r) {
          if (r.ok) { done += 1; } else { errs.push(T("clients.upload_failed", { name: f.name, msg: D.errorText(r) })); }
        });
      });
    });
    return p.then(function () {
      state.msg[key] = { ok: done ? T("clients.uploaded", { n: done }) : null, errs: errs };
      live([done ? T("clients.uploaded", { n: done }) : null].concat(errs).filter(Boolean).join(" "));
      state.focusAfter = "cl-pick-" + key;
      return loadClients();
    });
  }

  // ---------- список файлов: тип (PATCH), удаление (DELETE с подтверждением) ----------
  function fileTable(files, key, g, scope) {
    if (!files.length) { return el("p", { class: "muted cl-nofiles", text: T("clients.files_empty") }); }
    var cols = ["name", "doc_type", "size", "uploaded_at", "actions"];
    var types = D.cfg("doc_types") || {};
    var tbody = el("tbody");
    files.forEach(function (f, i) {
      var sid = "cl-type-" + key + "-" + i;
      var sel = el("select", { id: sid, "aria-label": T("clients.doc_type_for", { name: f.name }) });
      Object.keys(types).forEach(function (t) { sel.appendChild(el("option", { value: t, text: T("doc_types." + t), selected: t === f.doc_type })); });
      sel.addEventListener("change", function () { patchType(f, sel.value, g, scope, key, sid); });
      var th = function (c) { return T("clients.file_cols." + c); };
      tbody.appendChild(el("tr", null, [
        el("td", { "data-th": th("name") }, [el("div", { class: "cl-fname" }, [el("span", { text: f.name }),
          el("span", { class: "source", text: String(f.fmt || "").toUpperCase() })])]),
        el("td", { "data-th": th("doc_type") }, [sel]),
        el("td", { class: "num nowrap", "data-th": th("size") }, [fmtSize(f.size)]),
        el("td", { class: "nowrap", "data-th": th("uploaded_at") }, [D.fmtDateTime(f.uploaded_at)]),
        el("td", { "data-th": th("actions") }, [el("button", { type: "button", class: "btn btn--small btn--link", id: "cl-del-" + key + "-" + i,
          "aria-label": T("clients.delete_for", { name: f.name }), text: T("clients.delete"),
          on: { click: function (e) { askDelete(f, g, scope, key, e.currentTarget); } } })])
      ]));
    });
    return el("div", { class: "tbl-wrap" }, [el("table", { class: "cl-files" }, [
      el("caption", { class: "vh", text: T("clients.files_title") }),
      el("thead", null, [el("tr", null, cols.map(function (c) {
        return el("th", { scope: "col", class: c === "size" ? "num" : null, text: T("clients.file_cols." + c) });
      }))]), tbody])]);
  }
  function fileUrl(f) { return "/api/clients/contracts/" + enc(f._owner) + "/files/" + enc(f.name); }
  function patchType(f, docType, g, scope, key, sid) {
    D.request("PATCH", fileUrl(f), { doc_type: docType }).then(function (r) {
      if (r.ok) {
        state.msg[key] = { ok: T("clients.type_saved", { name: f.name }), errs: [] };
        markDirty(g, scope, f._owner);
        live(state.msg[key].ok);
      } else {
        state.msg[key] = { ok: null, errs: [D.errorText(r)] };
      }
      state.focusAfter = sid;
      return loadClients();
    });
  }
  function askDelete(f, g, scope, key, btn) {
    state.del = { f: f, g: g, scope: scope, key: key, btn: btn };
    $("cl-del-h").textContent = T("clients.delete_title");
    $("cl-del-ok").textContent = T("clients.delete_confirm");
    $("cl-del-text").textContent = T("clients.delete_text", { name: f.name });
    var dlg = $("cl-del-dlg");
    if (typeof dlg.showModal === "function") { dlg.showModal(); } else { dlg.setAttribute("open", ""); }
    $("cl-del-cancel").focus();
  }
  function closeDelete(refocus) {
    var dlg = $("cl-del-dlg"), d = state.del;
    if (dlg.close) { dlg.close(); } else { dlg.removeAttribute("open"); }
    if (refocus && d && d.btn && document.body.contains(d.btn)) { d.btn.focus(); }
  }
  function confirmDelete() {
    var d = state.del;
    if (!d) { closeDelete(false); return; }
    if (d.profile) { confirmDeleteProfile(d); return; }
    $("cl-del-ok").disabled = true;
    D.request("DELETE", fileUrl(d.f)).then(function (r) {
      $("cl-del-ok").disabled = false;
      state.del = null;
      closeDelete(false);
      if (r.ok) {
        state.msg[d.key] = { ok: T("clients.deleted", { name: d.f.name }), errs: [] };
        markDirty(d.g, d.scope, d.f._owner);
        live(state.msg[d.key].ok);
      } else {
        state.msg[d.key] = { ok: null, errs: [D.errorText(r)] };
      }
      state.focusAfter = "cl-pick-" + d.key;
      return loadClients();
    });
  }

  // ---------- обработка и опрос задания ----------
  function processContract(cid) {
    var btn = $("cl-proc-" + cid);
    if (btn) { btn.disabled = true; }
    state.msg["p-" + cid] = null;
    D.request("POST", "/api/clients/contracts/" + enc(cid) + "/process").then(function (r) {
      var hit = findContract(cid);
      if (!r.ok) {
        // 409 job_running — задание уже идёт: показываем пояснение и подхватываем его ход; 422 no_files — нет файлов.
        state.msg["p-" + cid] = { ok: null, errs: [errText(r)] };
        state.focusAfter = "cl-proc-" + cid;
        if (errCode(r) === "job_running") { loadClients(); } else { renderMain(); }
        return;
      }
      delete state.dirty[cid];
      state.lastStage[cid] = null;
      if (hit) { hit.c.job = r.data; live(T("clients.live_started", { name: hit.c.legal_entity })); }
      state.focusAfter = "cl-h-" + cid;
      renderList();
      renderMain();
      startPoll(r.data.id, cid);
    });
  }
  function startPoll(jobId, cid) {
    if (state.polls[cid] && state.polls[cid].id === jobId) { return; }
    stopPoll(cid);
    state.polls[cid] = { id: jobId, misses: 0, timer: null };
    schedule(cid);
  }
  function stopPoll(cid) { var p = state.polls[cid]; if (p && p.timer) { clearTimeout(p.timer); } delete state.polls[cid]; }
  function schedule(cid) { var p = state.polls[cid]; if (p) { p.timer = setTimeout(function () { tick(cid); }, POLL_MS); } }
  function tick(cid) {
    var p = state.polls[cid];
    if (!p) { return; }
    D.request("GET", "/api/clients/jobs/" + enc(p.id)).then(function (r) {
      if (state.polls[cid] !== p) { return; }
      if (!r.ok) {
        if (r.status === 404) { stopPoll(cid); state.msg["p-" + cid] = { ok: null, errs: [T("clients.poll_gone")] }; renderMain(); return; }
        p.misses += 1;
        state.lost[cid] = true;
        repaintProgress(cid);
        if (p.misses >= POLL_MAX_MISSES) { stopPoll(cid); return; }
        schedule(cid);
        return;
      }
      p.misses = 0;
      state.lost[cid] = false;
      var job = r.data, hit = findContract(cid);
      if (hit) { hit.c.job = job; }
      announceStage(cid, job, hit);
      if (job.state === "done" || job.state === "error") { stopPoll(cid); finish(cid, job, hit); return; }
      repaintProgress(cid);
      schedule(cid);
    });
  }
  // Объявляем только смену стадии (не каждый опрос), чтобы экранный диктор не повторял одно и то же.
  function announceStage(cid, job, hit) {
    var key = job.state + ":" + (job.stage || "");
    if (state.lastStage[cid] === key || job.state === "done" || job.state === "error") { state.lastStage[cid] = key; return; }
    state.lastStage[cid] = key;
    var stage = job.state === "queued" ? T("clients.queued") : T("clients.stages." + (job.stage || "received"));
    live(T("clients.live_stage", { name: hit ? hit.c.legal_entity : cid, stage: stage }));
  }
  function repaintProgress(cid) {
    var box = $("cl-prog-" + cid), hit = findContract(cid);
    if (box && hit && hit.c.job) { renderProgress(box, hit.c.job); }
  }
  function finish(cid, job, hit) {
    var name = hit ? hit.c.legal_entity : cid;
    if (job.state === "error") { live(T("clients.live_error", { name: name })); }
    else {
      var rd = job.report ? job.report.readiness : "failed";
      live(T("clients.live_done", { name: name, state: T("clients.states." + rd) }));
      // Договор в поиске — обновляем дерево вкладки «Документы» (documents.js слушает это событие).
      if (rd === "ready" || rd === "limited") { document.dispatchEvent(new CustomEvent("dms:groups-changed")); }
    }
    // Открытая разметка этого договора — таблицы по новой обработке; профили — счётчик «применён».
    if (state.map && state.map.cid === cid) { loadTables(cid); }
    loadProfiles();
    loadClients();
  }
  function renderProgress(box, job) {
    clear(box);
    var cur = job.state === "done" ? STAGES.length - 1 : job.stage ? STAGES.indexOf(job.stage) : job.state === "error" ? 0 : -1;
    var failed = job.state === "done" && job.report && job.report.readiness === "failed";
    var ol = el("ol", { class: "steps steps--compact", "aria-label": T("clients.progress_aria") });
    STAGES.forEach(function (s, i) {
      var cls, mark, label = T("clients.stages." + s);
      if (job.state === "done" && i === cur && failed) { cls = "is-error"; mark = "✗"; label = T("clients.states.failed"); }
      else if (job.state === "done" || i < cur) { cls = "is-done"; mark = "✓"; }
      else if (i === cur && job.state === "error") { cls = "is-error"; mark = "✗"; }
      else if (i === cur) { cls = "is-current"; mark = "◐"; }
      else { cls = "is-next"; mark = String(i + 1); }
      ol.appendChild(el("li", { class: "steps__item " + cls, "aria-current": i === cur && job.state !== "done" ? "step" : null }, [
        el("span", { class: "steps__mark", "aria-hidden": "true", text: mark }), el("span", { class: "steps__label", text: label })]));
    });
    box.appendChild(el("h4", { class: "sub", text: T("clients.progress_title") }));
    box.appendChild(ol);
    var note = null, cls2 = "hint";
    if (job.state === "queued") { note = T("clients.queued"); }
    else if (job.state === "running") { note = T("clients.stage_now", { stage: T("clients.stages." + (job.stage || "received")) }); }
    else if (job.state === "error") { note = T("clients.job_error", { msg: job.error || T("errors.internal") }); cls2 = "alert"; }
    if (note) { box.appendChild(el("p", { class: cls2 + " cl-prog__note", text: note })); }
    if (state.lost[job.contract_id]) { box.appendChild(el("p", { class: "hint", text: T("clients.poll_lost") })); }
  }

  // ---------- отчёт о готовности ----------
  function readinessText(rep) {
    if (rep.readiness === "ready") { return T("clients.readiness_ready"); }
    if (rep.readiness === "failed") { return T("clients.readiness_failed"); }
    var c = rep.counts || {}, miss = [];
    if (!c.clinics) { miss.push(T("clients.missing_clinics")); }
    if (!c.care_rows) { miss.push(T("clients.missing_care")); }
    return T("clients.readiness_limited", { missing: miss.length ? miss.join(T("clients.missing_join")) : T("clients.readiness_limited_any") });
  }
  function report(g, c, rep) {
    var r = rep.readiness, sec = el("section", { class: "cl-report", "aria-labelledby": "cl-rep-h-" + c.id });
    sec.appendChild(el("h4", { class: "sub", id: "cl-rep-h-" + c.id, text: T("clients.report_title") }));
    sec.appendChild(el("div", { class: "headline " + (HEADLINE[r] || "") }, [
      el("p", { class: "cl-report__state" }, [D.chip(T("clients.states." + r), STATE_CHIP[r])]),
      el("p", { class: "headline__note", text: readinessText(rep) })]));
    var dl = el("dl", { class: "kv cl-report__kv" });
    [[T("clients.report_insurer"), T("insurers." + (rep.insurer || "unknown"))], [T("clients.report_adapter"), T("clients.adapters." + (rep.adapter || "generic"))]]
      .forEach(function (row) { dl.appendChild(el("dt", { text: row[0] })); dl.appendChild(el("dd", { text: row[1] })); });
    sec.appendChild(dl);
    // Счётчики разбора: пункты, с номером, программы, строки перечня, виды помощи (OnboardingReport.counts).
    var counts = rep.counts || {};
    sec.appendChild(el("h4", { class: "sub", text: T("clients.counts_title") }));
    var countKeys = ["clauses", "numbered", "programs", "clinics", "care_rows"];
    if (counts.mapped_tables !== undefined) { countKeys.push("mapped_tables"); }
    if (counts.mapped_matrices !== undefined) { countKeys.push("mapped_matrices"); }
    sec.appendChild(el("dl", { class: "cl-counts" }, countKeys.map(function (k) {
      var v = Number(counts[k] || 0);
      return el("div", { class: "cl-count" + (v ? "" : " is-zero") }, [el("dt", { text: T("clients.counts." + k) }), el("dd", { text: v.toLocaleString("ru-RU") })]);
    })));
    var sc = rep.selfcheck || {};
    sec.appendChild(el("h4", { class: "sub", text: T("clients.selfcheck_title") }));
    sec.appendChild(el("p", { class: "cl-selfcheck" }, [sc.asked ? D.chip(T("clients.selfcheck", { cited: sc.cited || 0, asked: sc.asked }),
      sc.cited === sc.asked ? "chip--ok" : "chip--warn chip--bang") : el("span", { class: "muted", text: T("clients.selfcheck_none") })]));
    sec.appendChild(el("p", { class: "hint", text: T("clients.selfcheck_hint") }));
    if ((rep.documents || []).length) {
      sec.appendChild(el("h4", { class: "sub", text: T("clients.docs_title") }));
      sec.appendChild(el("ul", { class: "cl-docs" }, rep.documents.map(function (d) {
        return el("li", { class: "cl-doc" }, [
          el("div", { class: "cl-doc__head" }, [el("span", { class: "cl-doc__title", text: d.title || T("clients.doc_untitled") }),
            D.chip(T("doc_types." + (d.doc_type || "other"))), d.scope === "group" ? D.chip(T("clients.scope_group")) : null,
            D.docStatusChip(d.status || "ready")]),
          (d.warnings || []).length ? el("ul", { class: "warns" }, d.warnings.map(function (w) {
            return el("li", null, [D.chip("!", "chip--warn chip--bang chip--icon"), " ", w]);
          })) : null]);
      })));
    }
    if ((rep.warnings || []).length) {
      sec.appendChild(el("h4", { class: "sub", text: T("clients.warnings_title") }));
      sec.appendChild(el("ul", { class: "warns" }, rep.warnings.map(function (w) { return el("li", null, [D.chip("!", "chip--warn chip--bang chip--icon"), " ", w]); })));
    }
    // Чат по договору — только при ready / limited (ADR 0011 п. 3).
    if (r === "ready" || r === "limited") {
      sec.appendChild(el("div", { class: "cl-actions" }, [el("button", { type: "button", class: "btn btn--primary", id: "cl-chat-" + c.id,
        text: T("clients.open_chat"), on: { click: function (e) { openChat(g, c, e.currentTarget); } } })]));
      sec.appendChild(msgBlock("chat-" + c.id));
    }
    // Нет перечня клиник (или он из разметки) — оператор размечает таблицы документов (ADR 0012); договор, готовый по
    // разметке (перечень и матрица, ADR 0013), — тоже: разметку можно изменить.
    var byMapping = counts.mapped_tables || counts.mapped_matrices;
    if ((r === "limited" && (!counts.clinics || byMapping)) || (r === "ready" && byMapping)) {
      var open = state.map && state.map.cid === c.id;
      sec.appendChild(el("div", { class: "cl-actions" }, [el("button", { type: "button", class: "btn", id: "cl-map-open-" + c.id,
        "aria-expanded": open ? "true" : "false", "aria-controls": "cl-map-" + c.id,
        text: open ? T("clients.mapping.close") : T(byMapping ? "clients.mapping.open_edit" : "clients.mapping.open"),
        on: { click: function () { toggleMapping(c); } } })]));
    }
    return sec;
  }
  // Чат по договору: программа влияет на ответ. Одна программа — чат сразу с ней; несколько — выбор в диалоге
  // перед созданием чата; нет программ (неизвестная СК, ответы — поиском по пунктам) — без программы.
  // Список программ — из GET /api/groups (карточка договора в поиске).
  function openChat(g, c, btn) {
    btn.disabled = true; btn.textContent = T("clients.opening_chat");
    D.request("GET", "/api/groups").then(function (r) {
      var programs = [];
      if (r.ok) {
        (r.data || []).forEach(function (gg) {
          (gg.contracts || []).forEach(function (cc) { if (cc.id === c.id) { programs = cc.programs || []; } });
        });
      }
      if (programs.length > 1) {
        btn.disabled = false; btn.textContent = T("clients.open_chat");
        askProgram(g, c, programs, btn);
        return;
      }
      createChat(g, c, programs.length === 1 ? programs[0] : null, btn);
    });
  }
  function chatDialog() {
    var dlg = $("cl-chat-dlg");
    if (dlg) { return dlg; }
    var sel = el("select", { id: "cl-chat-prog" });
    dlg = el("dialog", { class: "dlg", id: "cl-chat-dlg", "aria-labelledby": "cl-chat-h", "aria-describedby": "cl-chat-text" }, [
      el("h2", { id: "cl-chat-h", text: T("clients.chat_program_title") }),
      el("p", { id: "cl-chat-text", class: "hint", text: T("clients.chat_program_hint") }),
      field("cl-chat-prog", T("clients.chat_program_label"), sel),
      el("div", { class: "dlg__actions" }, [
        el("button", { class: "btn", type: "button", id: "cl-chat-cancel", text: T("common.cancel"),
          on: { click: function () { closeChatDialog(true); } } }),
        el("button", { class: "btn btn--primary", type: "button", id: "cl-chat-ok", text: T("clients.chat_program_open"),
          on: { click: function () {
            var ch = state.chat; if (!ch) { closeChatDialog(false); return; }
            closeChatDialog(false);
            createChat(ch.g, ch.c, $("cl-chat-prog").value, ch.btn);
          } } })])]);
    dlg.addEventListener("cancel", function () { setTimeout(function () { closeChatDialog(true); }, 0); });
    document.body.appendChild(dlg);
    return dlg;
  }
  function askProgram(g, c, programs, btn) {
    var dlg = chatDialog(), sel = $("cl-chat-prog");
    clear(sel);
    programs.forEach(function (p) { sel.appendChild(el("option", { value: p, text: p })); });
    state.chat = { g: g, c: c, btn: btn };
    if (typeof dlg.showModal === "function") { dlg.showModal(); } else { dlg.setAttribute("open", ""); }
    sel.focus();
  }
  function closeChatDialog(refocus) {
    var dlg = $("cl-chat-dlg"), ch = state.chat;
    state.chat = null;
    if (dlg) { if (dlg.close && dlg.open) { dlg.close(); } else { dlg.removeAttribute("open"); } }
    if (refocus && ch && ch.btn && document.body.contains(ch.btn)) { ch.btn.focus(); }
  }
  function createChat(g, c, program, btn) {
    btn.disabled = true; btn.textContent = T("clients.opening_chat");
    D.request("POST", "/api/chats", { context: { group_id: g.id, contract_id: c.id, program: program || null, date: D.todayIso() }, title: null }).then(function (r) {
      if (!r.ok) {
        btn.disabled = false; btn.textContent = T("clients.open_chat");
        state.msg["chat-" + c.id] = { ok: null, errs: [D.errorText(r)] };
        state.focusAfter = btn.id;
        renderMain();
        return;
      }
      location.href = D.link("/#chat=" + enc(r.data.id));
    });
  }

  // ---------- добавить юрлицо в существующую группу ----------
  function addContractForm(g) {
    var p = "cl-ac-" + g.id;
    var form = el("form", { class: "card cl-card cl-add", novalidate: true, "aria-labelledby": p + "-h" }, [
      el("h3", { id: p + "-h", text: T("clients.add_contract_title") }),
      el("div", { class: "cl-row cl-row--add" }, [
        field(p + "-le", T("clients.legal_entity"), el("input", { type: "text", id: p + "-le", maxlength: "200", placeholder: T("clients.legal_entity_ph") })),
        field(p + "-ins", T("clients.insurer"), insurerSelect(p + "-ins")),
        field(p + "-num", T("clients.number"), el("input", { type: "text", id: p + "-num", maxlength: "60", placeholder: T("clients.number_ph") })),
        el("button", { type: "submit", class: "btn btn--small", id: p + "-go", text: T("clients.add_contract") }),
        insurerExtra(p + "-iname", p + "-fg")]),
      msgBlock("add-" + g.id)
    ]);
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var le = $(p + "-le");
      if (!le.value.trim()) {
        state.msg["add-" + g.id] = { ok: null, errs: [T("clients.err_entity", { n: 1 })] };
        state.focusAfter = p + "-le"; renderMain(); return;
      }
      $(p + "-go").disabled = true;
      D.request("POST", "/api/clients/" + enc(g.id) + "/contracts", { legal_entity: le.value.trim(), insurer: $(p + "-ins").value || null,
        number: $(p + "-num").value.trim() || null, insurer_name: $(p + "-iname").value.trim() || null,
        force_generic: $(p + "-fg").checked }).then(function (r) {
        if (!r.ok) { state.msg["add-" + g.id] = { ok: null, errs: [D.errorText(r)] }; state.focusAfter = p + "-le"; renderMain(); return; }
        state.msg["add-" + g.id] = null;
        state.msg["top"] = { ok: T("clients.contract_added", { name: r.data.legal_entity }), errs: [] };
        live(state.msg["top"].ok);
        state.focusAfter = "cl-h-" + r.data.id;
        return loadClients();
      });
    });
    return form;
  }

  // ---------- разметка таблиц перечня клиник оператором (ADR 0012, docs/CONTRACTS.md §10) ----------
  // Оператор выбирает таблицу документа договора, строку заголовка и роль каждой колонки; «Проверить разбор» —
  // пробный разбор без сохранения (POST …/tables/try); «Сохранить разметку» — разметка договора (PUT …/tables/mapping),
  // по флажкам — профиль СК и обработка сразу; «Убрать разметку» — DELETE. Предложение сервера (suggested: профиль СК
  // по подписи заголовка или угадка по заголовкам) подставляется с пометкой «предложено», пока роль колонки не
  // изменена. Черновик — в state.map, а не в DOM: перерисовка карточки его не теряет. Без колонки «Название
  // клиники» строки перечня не собрать — ошибка у кнопок до запроса (сервер ответит тем же mapping_no_name).
  var ROLES = ["clinic_name", "legal_name", "address", "city", "care_type", "program", "franchise_pct", "code", "by_referral", "access_note", "ignore"];
  var TRY_COLS = ["clinic_name", "address", "city", "care_type", "program", "franchise_pct"];
  // Матрица «программа × вид помощи» (ADR 0013, CONTRACTS §11): draft.kind — "list" (перечень) или "matrix";
  // matrix: layout "wide" (колонка видов помощи care_col + над остальными колонками — программа: текст заголовка по
  // умолчанию или «не программа») или "long" (роли колонок MATRIX_ROLES в draft.mcols).
  var MATRIX_ROLES = ["program", "care_type", "present", "ignore"];
  var ALL_PROGRAMS = "*";            // TableMapping.program «все программы договора» (CONTRACTS §11)
  var SIG_SHOW = 60;             // символов подписи заголовка в списке профилей (полная — во всплывающей подсказке)

  function tablesUrl(cid) { return "/api/clients/contracts/" + enc(cid) + "/tables"; }
  function refKey(ref) { return [ref.doc_id, ref.kind, ref.index === null || ref.index === undefined ? "" : ref.index, ref.sheet || ""].join("|"); }
  function tableName(t) {
    return t.ref.kind === "xlsx_sheet" ? T("clients.mapping.table_xlsx", { name: t.ref.sheet || "" })
      : T("clients.mapping.table_docx", { n: (Number(t.ref.index) || 0) + 1 });
  }
  function tableFull(t) { return (t.doc_title || T("clients.doc_untitled")) + " — " + tableName(t); }
  function profileById(id) { return state.profiles.list.filter(function (p) { return p.id === id; })[0] || null; }
  function hasRole(d, role) { return Object.keys(d.columns).some(function (k) { return d.columns[k] === role; }); }
  function hasMRole(d, role) { return Object.keys(d.mcols).some(function (k) { return d.mcols[k] === role; }); }
  function isMatrix(d) { return d.kind === "matrix"; }
  function curTable() {
    var m = state.map;
    return m && m.sel ? m.tables.filter(function (t) { return refKey(t.ref) === m.sel; })[0] || null : null;
  }
  // Замена узла по id с возвратом фокуса на тот же id (как renderMain, но без перерисовки всей карточки).
  function swapNode(id, node) {
    var old = $(id);
    if (!old) { return; }
    var keep = state.focusAfter || (document.activeElement && document.activeElement.id);
    state.focusAfter = null;
    old.parentNode.replaceChild(node, old);
    if (keep) { var n = $(keep); if (n && n !== document.activeElement && !$("cl-del-dlg").open) { n.focus(); } }
  }
  function repaintMap() {
    var m = state.map, hit = m && findContract(m.cid);
    if (hit) { swapNode("cl-map-" + m.cid, mappingPanel(hit.c)); }
  }

  function toggleMapping(c) {
    if (state.map && state.map.cid === c.id) { state.map = null; state.focusAfter = "cl-map-open-" + c.id; renderMain(); return; }
    state.map = { cid: c.id, loading: true, error: null, tables: [], sel: null, draft: null, marks: {}, source: null, tryRes: null,
                  err: null, busy: null, saveProfile: false, profileName: c.insurer_name || "", processNow: true, sameLayout: false, scrollX: 0,
                  suspicious: null, confirm: false };
    state.msg["map-" + c.id] = null;
    state.focusAfter = "cl-map-h-" + c.id;
    renderMain();
    loadTables(c.id);
    if (!state.profiles.loaded) { loadProfiles(); }
  }
  // Таблицы договора (по файлам последней обработки). Выбранная таблица и черновик остаются, если таблица на месте;
  // reselect — черновик заново из ответа сервера (после «Убрать разметку»). Ничего не выбрано — сохранённая
  // разметка, иначе предложение профиля, иначе любое предложение.
  function loadTables(cid, reselect) {
    var m = state.map;
    if (!m || m.cid !== cid) { return Promise.resolve(); }
    m.loading = true; m.error = null;
    return D.request("GET", tablesUrl(cid)).then(function (r) {
      if (state.map !== m) { return; }
      m.loading = false;
      if (!r.ok) { m.error = T("clients.mapping.load_error", { msg: errText(r) }); repaintMap(); return; }
      m.tables = r.data || [];
      var cur = curTable();
      if (!cur) { m.sel = null; m.draft = null; m.tryRes = null; }
      else if (reselect) { selectTable(cur); }
      if (!m.sel) {
        var best = m.tables.filter(function (t) { return t.mapping || t.applied_profile; })[0] || m.tables.filter(function (t) { return t.suggested && t.profile_id; })[0]
          || m.tables.filter(function (t) { return t.suggested; })[0];
        if (best) { selectTable(best); }
      }
      repaintMap();
    });
  }
  function selectTable(t) {
    var m = state.map, base = t.mapping || t.suggested;
    m.sel = refKey(t.ref); m.tryRes = null; m.err = null; m.marks = {}; m.scrollX = 0; m.confirm = false;
    // w9-fix: профиль, который размечает таблицу сам при обработке (applied_profile), — «размечено», а не «предложено»
    m.source = t.mapping ? "saved" : t.applied_profile ? "applied" : t.suggested ? (t.profile_id ? "profile" : "guess") : null;
    m.suspicious = (t.suspicious || []).length ? t.suspicious : null;
    // Дополнение §10: header_row = null — строки заголовка нет; program_from_title — программа из строки-названия
    var noHead = !!base && base.header_row === null;
    var mx = !!base && base.role === "care_matrix";
    m.draft = { header_row: base && !noHead ? Number(base.header_row) || 0 : 0, no_header: noHead, from_title: !!(base && base.program_from_title),
      columns: {}, program: (base && base.program) || "", care_type: (base && base.care_type) || "",
      kind: mx ? "matrix" : "list", layout: (mx && base.matrix_layout) || "wide", derive: !!(base && base.derive_care),
      care_col: null, prog: {}, mcols: {} };
    initMatrix(t, mx ? base : null);
    // «Применить ко всем таблицам с такой же раскладкой» — включён, если предложена разметка без заголовка и таких таблиц несколько
    m.sameLayout = !t.mapping && noHead && sameLayoutN(t) > 1;
    for (var i = 0; i < (t.n_cols || 0); i++) {
      var role = base && base.columns && !mx ? base.columns[i] : null;     // ключи колонок в JSON — строки "0", "1", …
      m.draft.columns[i] = ROLES.indexOf(role) >= 0 ? role : "ignore";
      if (!t.mapping && role && role !== "ignore") { m.marks[i] = true; }
    }
    var pr = t.profile_id ? profileById(t.profile_id) : null;
    if (pr && !m.profileName) { m.profileName = pr.insurer_name; }
  }

  function sameLayoutN(t) { return Number(t.same_layout) || 1; }
  // Черновик матрицы из разметки (сохранённой / предложенной) или по умолчанию: колонка видов помощи — первая
  // колонка, программы — остальные колонки (название — текст заголовка; prog[i].name = null — «как в заголовке»).
  function initMatrix(t, base) {
    var d = state.map.draft, n = t.n_cols || 0, pc = base && base.program_cols ? base.program_cols : null;
    if (pc && !Object.keys(pc).length) { pc = null; }
    d.care_col = null;
    for (var i = 0; i < n; i++) {
      var role = base && base.columns ? base.columns[i] : null;
      if (role === "care_type" && d.care_col === null) { d.care_col = i; }
      d.mcols[i] = base && base.matrix_layout === "long" && MATRIX_ROLES.indexOf(role) >= 0 ? role : "ignore";
      var nm = pc ? pc[i] : undefined;
      d.prog[i] = { skip: !!pc && nm === undefined, name: nm ? String(nm) : null };
    }
    if (d.care_col === null && n) { d.care_col = 0; }
  }

  function mappingPanel(c) {
    var m = state.map, k = c.id;
    var sec = el("section", { class: "cl-map", id: "cl-map-" + k, "aria-labelledby": "cl-map-h-" + k }, [
      el("h4", { class: "cl-map__title", id: "cl-map-h-" + k, tabindex: "-1", text: T("clients.mapping.title") }),
      el("p", { class: "hint", text: T("clients.mapping.hint") }), msgBlock("map-" + k)]);
    if (m.loading && !m.tables.length) { sec.appendChild(el("p", { class: "muted", role: "status", text: T("clients.mapping.loading") })); return sec; }
    if (m.error) { sec.appendChild(el("p", { class: "alert", role: "alert", text: m.error })); return sec; }
    if (!m.tables.length) { sec.appendChild(el("p", { class: "muted", text: T("clients.mapping.tables_empty") })); return sec; }
    sec.appendChild(tablesList(c));
    var t = curTable();
    if (t && m.draft) { sec.appendChild(mappingEditor(c, t)); }
    return sec;
  }
  function tableState(t) {
    var main = tableMainState(t), extra = [], use = t.mapping || (t.applied_profile ? null : t.suggested);
    if (use && use.role === "care_matrix" && t.mapping) { extra.push(D.chip(T("clients.mapping.state_matrix"), "chip--none")); }
    if (t.mapping && t.mapping.derive_care) { extra.push(D.chip(T("clients.mapping.state_derive"), "chip--none")); }
    if ((t.suspicious || []).length) { extra.push(D.chip(T("clients.mapping.state_suspicious"), "chip--warn")); }
    if (t.similar_unmapped) { extra.push(D.chip(T("clients.mapping.state_similar"), "chip--warn")); }
    return extra.length ? el("span", { class: "cl-map__states" }, [main].concat(extra)) : main;
  }
  function tableMainState(t) {
    if (t.mapping) { return D.chip(T("clients.mapping.state_saved"), "chip--ok"); }
    if (t.applied_profile) {
      var ap = profileById(t.applied_profile);
      return D.chip(ap ? T("clients.mapping.state_profile_applied", { name: ap.insurer_name }) : T("clients.mapping.state_profile_applied_unknown"), "chip--ok");
    }
    if (t.suggested && t.profile_id) {
      var p = profileById(t.profile_id);
      return D.chip(p ? T("clients.mapping.state_profile", { name: p.insurer_name }) : T("clients.mapping.state_profile_unknown"), "chip--cond");
    }
    if (t.suggested && t.suggested.role === "care_matrix") { return D.chip(T("clients.mapping.state_matrix_guess"), "chip--q"); }
    if (t.suggested && t.suggested.header_row === null) { return D.chip(T("clients.mapping.state_layout", { n: sameLayoutN(t) }), "chip--q"); }
    if (t.suggested) { return D.chip(T("clients.mapping.state_guess"), "chip--q"); }
    return D.chip(T("clients.mapping.state_none"), "chip--none");
  }
  function tablesList(c) {
    var m = state.map, k = c.id, th = function (x) { return T("clients.mapping.cols." + x); };
    var tbody = el("tbody");
    m.tables.forEach(function (t, i) {
      var sel = refKey(t.ref) === m.sel, size = { rows: t.n_rows || 0, cols: t.n_cols || 0 };
      tbody.appendChild(el("tr", { class: sel ? "is-selected" : null }, [
        el("td", { "data-th": th("doc") }, [el("div", { class: "cl-fname" }, [el("span", { text: t.doc_title || T("clients.doc_untitled") }),
          el("span", { class: "source", text: tableName(t) })])]),
        el("td", { class: "num nowrap", "data-th": th("size") }, [el("span", { "aria-hidden": "true", text: T("clients.mapping.table_size", size) }),
          el("span", { class: "vh", text: T("clients.mapping.table_size_aria", size) })]),
        el("td", { "data-th": th("state") }, [tableState(t)]),
        el("td", { "data-th": th("actions") }, [el("button", { type: "button", class: "btn btn--small" + (sel ? " btn--primary" : ""),
          id: "cl-map-pick-" + k + "-" + i, "aria-pressed": sel ? "true" : "false", "aria-label": T("clients.mapping.pick_for", { name: tableFull(t) }),
          text: sel ? T("clients.mapping.picked") : T("clients.mapping.pick_table"),
          on: { click: function () { selectTable(t); state.focusAfter = "cl-map-pv-" + k; repaintMap(); } } })])]));
    });
    return el("div", { class: "tbl-wrap" }, [el("table", { class: "cl-files cl-map__list" }, [
      el("caption", { class: "sub cl-map__cap", text: T("clients.mapping.tables_title") }),
      el("thead", null, [el("tr", null, ["doc", "size", "state", "actions"].map(function (x) {
        return el("th", { scope: "col", class: x === "size" ? "num" : null, text: th(x) });
      }))]), tbody])]);
  }

  function mappingEditor(c, t) {
    var m = state.map, k = c.id, pr = t.profile_id ? profileById(t.profile_id) : null;
    // пометка «предложено» — только пока вид таблицы (перечень / матрица) тот же, что в предложении
    var sgMx = !!t.suggested && t.suggested.role === "care_matrix";
    var guessNote = sgMx !== isMatrix(m.draft) ? null : sgMx ? "clients.mapping.suggested_note_matrix"
      : m.draft.no_header ? "clients.mapping.suggested_note_layout" : "clients.mapping.suggested_note_guess";
    var note = m.source === "saved" ? T("clients.mapping.saved_note")
      : m.source === "applied" ? T("clients.mapping.applied_note_profile", { name: pr ? pr.insurer_name : T("clients.mapping.state_profile_unknown") })
      : m.source === "profile" ? T("clients.mapping.suggested_note_profile", { name: pr ? pr.insurer_name : T("clients.mapping.state_profile_unknown") })
      : m.source === "guess" && guessNote ? T(guessNote) : null;
    return el("div", { class: "cl-map__editor", id: "cl-map-ed-" + k }, [
      el("h5", { class: "cl-map__pv", id: "cl-map-pv-" + k, tabindex: "-1", text: T("clients.mapping.preview_title", { name: tableFull(t) }) }),
      el("p", { class: "hint", text: T("clients.mapping.preview_hint", { shown: (t.rows || []).length, total: t.n_rows || 0 }) }),
      note ? el("p", { class: "hint cl-map__note", text: note }) : null,
      kindBlock(c, t), isMatrix(m.draft) ? layoutBlock(c, t) : null,
      headerBlock(c), previewGrid(c, t), constsBlock(c), optionsBlock(c, t), actionsBlock(c, t),
      // Итог проверки — в постоянную область aria-live: узел не пересоздаётся при ответе, диктор читает итог.
      el("div", { class: "cl-map__try", id: "cl-map-try-" + k, "aria-live": "polite" }, m.tryRes ? tryContent(m.tryRes) : [])]);
  }
  function headText(t, h, i) { var row = (h === null ? null : (t.rows || [])[h]) || []; return String(row[i] === null || row[i] === undefined ? "" : row[i]).trim(); }
  // Радиогруппа (fieldset + legend): стрелки переключают выбор внутри группы, фокус возвращается на выбранный пункт.
  function radioGroup(name, legend, value, items, onPick) {
    return el("fieldset", { class: "cl-map__kind" }, [el("legend", { class: "sub", text: legend }),
      el("div", { class: "cl-map__opts" }, items.map(function (it) {
        var id = name + "-" + it.value;
        var inp = el("input", { type: "radio", name: name, id: id, value: it.value, checked: value === it.value, "aria-label": it.label, "aria-describedby": it.hint ? id + "-hint" : null });
        inp.addEventListener("change", function () { if (inp.checked) { onPick(it.value, id); } });
        return el("div", { class: "cl-map__opt" }, [el("label", { class: "check", for: id }, [inp, el("span", { text: it.label })]),
          it.hint ? el("p", { class: "hint", id: id + "-hint", text: it.hint }) : null]);
      }))]);
  }
  // Что в таблице (ADR 0013): перечень клиник или матрица видов помощи
  function kindBlock(c, t) {
    var m = state.map, d = m.draft;
    return radioGroup("cl-map-kind-" + c.id, T("clients.mapping.kind_title"), d.kind, [
      { value: "list", label: T("clients.mapping.kind_list") },
      { value: "matrix", label: T("clients.mapping.kind_matrix"), hint: T("clients.mapping.kind_matrix_hint") }], function (v, id) {
      d.kind = v; m.tryRes = null; m.err = null; m.marks = {}; state.focusAfter = id; repaintMap();
    });
  }
  // Вид матрицы и (wide) колонка видов помощи
  function layoutBlock(c, t) {
    var m = state.map, d = m.draft, k = c.id, sid = "cl-map-cc-" + k;
    var sel = el("select", { id: sid, "aria-describedby": sid + "-hint" });
    sel.appendChild(el("option", { value: "", text: T("clients.mapping.care_col_none"), selected: d.care_col === null }));
    for (var i = 0; i < (t.n_cols || 0); i++) {
      sel.appendChild(el("option", { value: String(i), text: colLabel(t, d.no_header ? null : d.header_row, i), selected: d.care_col === i }));
    }
    sel.addEventListener("change", function () {
      d.care_col = sel.value === "" ? null : Number(sel.value); m.tryRes = null; m.err = null; state.focusAfter = sid; repaintMap();
    });
    return el("div", { class: "cl-map__matrix" }, [
      radioGroup("cl-map-lay-" + k, T("clients.mapping.layout_title"), d.layout, [
        { value: "wide", label: T("clients.mapping.layout_wide"), hint: T("clients.mapping.layout_wide_hint") },
        { value: "long", label: T("clients.mapping.layout_long"), hint: T("clients.mapping.layout_long_hint") }], function (v, id) {
        d.layout = v; m.tryRes = null; m.err = null; state.focusAfter = id; repaintMap();
      }),
      d.layout === "wide" ? el("div", { class: "f cl-map__cc" }, [el("label", { for: sid, text: T("clients.mapping.care_col") }), sel,
        el("p", { class: "hint", id: sid + "-hint", text: T("clients.mapping.prog_hint") })]) : null]);
  }
  // Таблица без строки заголовка (перечни «по таблице на программу»): «Строки заголовка нет» выключает выбор строки
  // заголовка (данные — с первой строки), «Программа — из названия таблицы» — программа строк = первая строка таблицы.
  function headerBlock(c) {
    var m = state.map, d = m.draft, k = c.id;
    function box(id, key, label, hint) {
      var inp = el("input", { type: "checkbox", id: id, checked: !!d[key], "aria-label": label, "aria-describedby": id + "-hint" });
      inp.addEventListener("change", function () {
        d[key] = inp.checked; m.tryRes = null; m.err = null;
        if (key === "no_header" && !inp.checked) { m.sameLayout = false; }
        state.focusAfter = id; repaintMap();
      });
      return el("div", { class: "cl-map__opt" }, [el("label", { class: "check", for: id }, [inp, el("span", { text: label })]),
        el("p", { class: "hint", id: id + "-hint", text: hint })]);
    }
    return el("fieldset", { class: "cl-map__head" }, [el("legend", { class: "sub", text: T("clients.mapping.head_title") }),
      el("div", { class: "cl-map__opts" }, [
        box("cl-map-nh-" + k, "no_header", T("clients.mapping.no_header"), T("clients.mapping.no_header_hint")),
        box("cl-map-ft-" + k, "from_title", T("clients.mapping.from_title"), T("clients.mapping.from_title_hint"))])]);
  }
  function colLabel(t, h, i) {
    var head = headText(t, h, i);
    return head ? T("clients.mapping.col_label", { n: i + 1, head: head }) : T("clients.mapping.col_label_empty", { n: i + 1 });
  }
  // Предпросмотр: прокрутка по горизонтали — внутри блока (страница не уезжает). Первая колонка — выбор строки
  // заголовка (радио, стрелки внутри группы), над каждой колонкой — роль (select с подписью «Колонка N: заголовок»).
  function previewGrid(c, t) {
    var m = state.map, k = c.id, d = m.draft, n = t.n_cols || 0, hr = d.no_header ? -1 : d.header_row;
    var headRow = el("tr", null, [el("th", { scope: "col", class: "cl-map__hdrcol", text: T("clients.mapping.header_col") })]);
    for (var i = 0; i < n; i++) { headRow.appendChild(colHead(c, t, i)); }
    var tbody = el("tbody");
    (t.rows || []).forEach(function (row, ri) {
      var rid = "cl-hdr-" + k + "-" + ri;
      var radio = el("input", { type: "radio", name: "cl-hdr-" + k, id: rid, value: String(ri), checked: ri === hr, disabled: d.no_header,
        "aria-label": T("clients.mapping.header_pick", { n: ri + 1 }) });
      radio.addEventListener("change", function () { d.header_row = ri; m.tryRes = null; m.err = null; state.focusAfter = rid; repaintMap(); });
      var title = d.no_header && d.from_title && ri === 0;
      var tr = el("tr", { class: ri === hr ? "is-header" : ri < hr || title ? "is-above" : null }, [
        el("th", { scope: "row", class: "cl-map__hdrcol" }, [el("label", { class: "cl-map__rowpick", for: rid }, [radio,
          el("span", { "aria-hidden": "true", text: String(ri + 1) })]),
          ri < hr ? el("span", { class: "vh", text: T("clients.mapping.above_header") }) : null,
          title ? el("span", { class: "vh", text: T("clients.mapping.title_row") }) : null])]);
      for (var ci = 0; ci < n; ci++) { tr.appendChild(el("td", { text: row[ci] === null || row[ci] === undefined ? "" : String(row[ci]) })); }
      tbody.appendChild(tr);
    });
    var grid = el("div", { class: "cl-map__grid", role: "region", tabindex: "0", "aria-labelledby": "cl-map-pv-" + k }, [
      el("table", { class: "cl-map__tbl" }, [el("caption", { class: "vh", text: T("clients.mapping.preview_caption") }), el("thead", null, [headRow]), tbody])]);
    grid.addEventListener("scroll", function () { m.scrollX = grid.scrollLeft; });
    if (m.scrollX) { var x = m.scrollX; setTimeout(function () { grid.scrollLeft = x; }, 0); }
    return grid;
  }
  function colHead(c, t, i) {
    var dd = state.map.draft;
    if (isMatrix(dd)) { return dd.layout === "wide" ? wideHead(c, t, i) : longHead(c, t, i); }
    return listHead(c, t, i);
  }
  // wide: над колонкой видов помощи — пометка, над остальными — название программы (по умолчанию — текст заголовка)
  // и флажок «не программа»
  function wideHead(c, t, i) {
    var d = state.map.draft, m = state.map, k = c.id, pid = "cl-pg-" + k + "-" + i, nid = "cl-np-" + k + "-" + i;
    var head = el("span", { class: "cl-map__coln", "aria-hidden": "true", text: T("clients.mapping.col_n", { n: i + 1 }) });
    if (d.care_col === i) {
      return el("th", { scope: "col", class: "cl-map__colh is-used" }, [head, el("span", { class: "chip chip--none", text: T("clients.mapping.care_col_chip") })]);
    }
    var pg = d.prog[i] || (d.prog[i] = { skip: false, name: null });
    var hdr = headText(t, d.no_header ? null : d.header_row, i);
    var inp = el("input", { type: "text", id: pid, class: "cl-map__pname", maxlength: "120", value: pg.name !== null ? pg.name : hdr, disabled: pg.skip,
      "aria-label": T("clients.mapping.prog_name", { n: i + 1 }) });
    inp.addEventListener("input", function () { pg.name = inp.value; m.tryRes = null; });
    var cb = el("input", { type: "checkbox", id: nid, checked: pg.skip, "aria-label": T("clients.mapping.not_program_for", { n: i + 1 }) });
    cb.addEventListener("change", function () { pg.skip = cb.checked; m.tryRes = null; m.err = null; state.focusAfter = nid; repaintMap(); });
    return el("th", { scope: "col", class: "cl-map__colh" + (pg.skip ? "" : " is-used") }, [head, inp,
      el("label", { class: "check cl-map__np", for: nid }, [cb, el("span", { "aria-hidden": "true", text: T("clients.mapping.not_program") })])]);
  }
  function longHead(c, t, i) {
    var m = state.map, d = m.draft, sid = "cl-col-" + c.id + "-" + i;
    var sel = el("select", { id: sid, class: "cl-map__role", "aria-label": colLabel(t, d.no_header ? null : d.header_row, i) });
    MATRIX_ROLES.forEach(function (r) { sel.appendChild(el("option", { value: r, text: T("clients.mapping.matrix_roles." + r), selected: d.mcols[i] === r })); });
    sel.addEventListener("change", function () { d.mcols[i] = sel.value; m.tryRes = null; m.err = null; state.focusAfter = sid; repaintMap(); });
    var used = d.mcols[i] && d.mcols[i] !== "ignore";
    return el("th", { scope: "col", class: "cl-map__colh" + (used ? " is-used" : "") }, [
      el("span", { class: "cl-map__coln", "aria-hidden": "true", text: T("clients.mapping.col_n", { n: i + 1 }) }), sel]);
  }
  function listHead(c, t, i) {
    var m = state.map, d = m.draft, sid = "cl-col-" + c.id + "-" + i, sugg = m.marks[i];
    var sel = el("select", { id: sid, class: "cl-map__role", "aria-label": colLabel(t, d.no_header ? null : d.header_row, i), "aria-describedby": sugg ? sid + "-s" : null });
    ROLES.forEach(function (r) { sel.appendChild(el("option", { value: r, text: T("clients.mapping.roles." + r), selected: d.columns[i] === r })); });
    sel.addEventListener("change", function () { d.columns[i] = sel.value; delete m.marks[i]; m.tryRes = null; m.err = null; state.focusAfter = sid; repaintMap(); });
    var used = d.columns[i] && d.columns[i] !== "ignore";
    return el("th", { scope: "col", class: "cl-map__colh" + (used ? " is-used" : "") }, [
      el("span", { class: "cl-map__coln", "aria-hidden": "true", text: T("clients.mapping.col_n", { n: i + 1 }) }), sel,
      sugg ? el("span", { class: "chip chip--cond cl-map__sugg", id: sid + "-s", text: T("clients.mapping.suggested_chip") }) : null]);
  }
  // Постоянные значения — только если такой колонки нет (иначе поле выключено с пояснением).
  function constsBlock(c) {
    var d = state.map.draft, k = c.id, mx = isMatrix(d);
    if (mx && d.layout === "wide") { return null; }        // у широкой матрицы программы — над колонками
    function has(role) { return mx ? hasMRole(d, role) : hasRole(d, role); }
    function one(key, role, label, ph) {
      var id = "cl-map-" + key + "-" + k, byTitle = key === "program" && d.from_title && !has(role), dis = has(role) || byTitle;
      // «Все программы договора» (программа «*», CONTRACTS §11): строки таблицы — каждой программе договора
      var all = key === "program" && !dis && d.program === ALL_PROGRAMS;
      var inp = el("input", { type: "text", id: id, maxlength: "120", placeholder: ph, value: dis || all ? "" : d[key] || "", disabled: dis || all,
        "aria-describedby": dis ? id + "-why" : (key === "program" ? id + "-all-hint" : null) });
      inp.addEventListener("input", function () { d[key] = inp.value; });
      var allBox = null;
      if (key === "program" && !dis) {
        var cb = el("input", { type: "checkbox", id: id + "-all", checked: all, "aria-label": T("clients.mapping.const_program_all"), "aria-describedby": id + "-all-hint" });
        cb.addEventListener("change", function () { d.program = cb.checked ? ALL_PROGRAMS : ""; state.map.tryRes = null; state.focusAfter = cb.id; repaintMap(); });
        allBox = el("div", {}, [el("label", { class: "check", for: cb.id }, [cb, el("span", { text: T("clients.mapping.const_program_all") })]),
          el("p", { class: "hint", id: id + "-all-hint", text: T("clients.mapping.const_program_all_hint") })]);
      }
      return el("div", { class: "f" }, [el("label", { for: id, text: label }), inp, allBox,
        dis ? el("p", { class: "hint", id: id + "-why", text: byTitle ? T("clients.mapping.const_by_title")
          : T("clients.mapping.const_disabled", { role: T("clients.mapping.roles." + role) }) }) : null]);
    }
    return el("fieldset", { class: "cl-map__consts" }, [el("legend", { class: "sub", text: T("clients.mapping.consts_title") }),
      el("div", { class: "f2" }, [one("program", "program", T("clients.mapping.const_program"), T("clients.mapping.const_program_ph")),
        mx ? null : one("care_type", "care_type", T("clients.mapping.const_care"), T("clients.mapping.const_care_ph"))])]);
  }
  function optionsBlock(c, t) {
    var m = state.map, k = c.id, nSame = sameLayoutN(t), showSame = m.draft.no_header && nSame > 1;
    var sl = el("input", { type: "checkbox", id: "cl-map-sl-" + k, checked: !!m.sameLayout, "aria-label": T("clients.mapping.same_layout", { n: nSame }), "aria-describedby": "cl-map-sl-hint-" + k });
    sl.addEventListener("change", function () { m.sameLayout = sl.checked; });
    var sp = el("input", { type: "checkbox", id: "cl-map-sp-" + k, checked: m.saveProfile, "aria-label": T("clients.mapping.save_profile"), "aria-describedby": "cl-map-sp-hint-" + k });
    sp.addEventListener("change", function () { m.saveProfile = sp.checked; m.err = null; state.focusAfter = sp.id; repaintMap(); });
    var name = el("input", { type: "text", id: "cl-map-pn-" + k, maxlength: "120", value: m.profileName || "", placeholder: T("clients.mapping.profile_name_ph") });
    name.addEventListener("input", function () { m.profileName = name.value; });
    var pr = el("input", { type: "checkbox", id: "cl-map-pr-" + k, checked: m.processNow, "aria-label": T("clients.mapping.process_now"), "aria-describedby": "cl-map-pr-hint-" + k });
    pr.addEventListener("change", function () { m.processNow = pr.checked; });
    var dv = el("input", { type: "checkbox", id: "cl-map-dv-" + k, checked: !!m.draft.derive, "aria-label": T("clients.mapping.derive"), "aria-describedby": "cl-map-dv-hint-" + k });
    dv.addEventListener("change", function () { m.draft.derive = dv.checked; m.tryRes = null; });
    return el("div", { class: "cl-map__opts" }, [
      isMatrix(m.draft) ? null : el("div", { class: "cl-map__opt" }, [el("label", { class: "check", for: dv.id }, [dv, el("span", { text: T("clients.mapping.derive") })]),
        el("p", { class: "hint", id: "cl-map-dv-hint-" + k, text: T("clients.mapping.derive_hint") })]),
      showSame ? el("div", { class: "cl-map__opt" }, [el("label", { class: "check", for: sl.id }, [sl,
        el("span", { text: T("clients.mapping.same_layout", { n: nSame }) })]),
        el("p", { class: "hint", id: "cl-map-sl-hint-" + k, text: T("clients.mapping.same_layout_hint") })]) : null,
      el("div", { class: "cl-map__opt" }, [el("label", { class: "check", for: sp.id }, [sp, el("span", { text: T("clients.mapping.save_profile") })]),
        el("p", { class: "hint", id: "cl-map-sp-hint-" + k, text: T("clients.mapping.save_profile_hint") }),
        m.saveProfile ? field(name.id, T("clients.mapping.profile_name"), name) : null]),
      el("div", { class: "cl-map__opt" }, [el("label", { class: "check", for: pr.id }, [pr, el("span", { text: T("clients.mapping.process_now") })]),
        el("p", { class: "hint", id: "cl-map-pr-hint-" + k, text: T("clients.mapping.process_now_hint") })])]);
  }
  // Подозрительная разметка (w9-fix п. 1б): сервер сохраняет её только с confirm_suspicious — флажок подтверждения.
  function confirmBlock(c) {
    var m = state.map, k = c.id, id = "cl-map-cs-" + k;
    if (!m.suspicious) { return null; }
    var inp = el("input", { type: "checkbox", id: id, checked: !!m.confirm, "aria-describedby": id + "-hint",
      "aria-label": T(isMatrix(m.draft) ? "clients.mapping.confirm_suspicious_matrix" : "clients.mapping.confirm_suspicious") });
    inp.addEventListener("change", function () { m.confirm = inp.checked; });
    return el("div", { class: "cl-map__opt cl-map__confirm" }, [
      el("p", { class: "sub", text: T("clients.mapping.suspicious_title") }),
      el("ul", { class: "warns" }, m.suspicious.map(function (w) { return el("li", null, [D.chip("!", "chip--warn chip--bang chip--icon"), " ", w]); })),
      el("label", { class: "check", for: id }, [inp, el("span", { text: T(isMatrix(m.draft) ? "clients.mapping.confirm_suspicious_matrix" : "clients.mapping.confirm_suspicious") })]),
      el("p", { class: "hint", id: id + "-hint", text: T("clients.mapping.confirm_suspicious_hint") })]);
  }
  function actionsBlock(c, t) {
    var m = state.map, k = c.id, errId = "cl-map-err-" + k, desc = m.err ? errId : null;
    return el("div", { class: "cl-map__bar", id: "cl-map-bar-" + k }, [confirmBlock(c),
      el("div", { class: "cl-actions", role: "group", "aria-label": T("clients.mapping.actions_aria") }, [
        el("button", { type: "button", class: "btn", id: "cl-map-trybtn-" + k, disabled: !!m.busy, "aria-describedby": desc,
          text: m.busy === "try" ? T("clients.mapping.trying") : T("clients.mapping.try"), on: { click: function () { tryMapping(c, t); } } }),
        el("button", { type: "button", class: "btn btn--primary", id: "cl-map-save-" + k, disabled: !!m.busy, "aria-describedby": desc,
          text: m.busy === "save" ? T("clients.mapping.saving") : T("clients.mapping.save"), on: { click: function () { saveMapping(c, t); } } }),
        t.mapping ? el("button", { type: "button", class: "btn btn--danger", id: "cl-map-del-" + k, disabled: !!m.busy,
          text: m.busy === "del" ? T("clients.mapping.removing") : T("clients.mapping.remove"), on: { click: function () { removeMapping(c, t); } } }) : null]),
      el("p", { class: "alert", id: errId, role: "alert", hidden: !m.err, text: m.err || "" })]);
  }
  // Тело разметки (TableMapping): только колонки с ролью; постоянные значения — если такой колонки нет.
  function mappingBody(t) {
    var d = state.map.draft, cols = {};
    Object.keys(d.columns).forEach(function (k) { if (d.columns[k] !== "ignore" && Number(k) < (t.n_cols || 0)) { cols[k] = d.columns[k]; } });
    function val(v) { v = String(v || "").trim(); return v || null; }
    if (isMatrix(d)) { return matrixBody(t, val); }
    var byTitle = d.from_title && !hasRole(d, "program");
    return { role: "clinic_list", derive_care: !!d.derive, header_row: d.no_header ? null : d.header_row, columns: cols, program_from_title: !!d.from_title,
      program: hasRole(d, "program") || byTitle ? null : val(d.program), care_type: hasRole(d, "care_type") ? null : val(d.care_type) };
  }
  // Тело разметки матрицы (TableMapping role=care_matrix): wide — колонка видов помощи и program_cols (название —
  // введённое или текст заголовка), long — роли колонок и программа для всей таблицы / из названия.
  function matrixBody(t, val) {
    var d = state.map.draft, n = t.n_cols || 0, hr = d.no_header ? null : d.header_row, cols = {}, pc = {};
    if (d.layout === "wide") {
      if (d.care_col !== null && d.care_col < n) { cols[d.care_col] = "care_type"; }
      for (var i = 0; i < n; i++) {
        var pg = d.prog[i];
        if (i === d.care_col || (pg && pg.skip)) { continue; }
        var name = val(pg && pg.name !== null ? pg.name : headText(t, hr, i));
        if (name) { pc[i] = name; }
      }
      return { role: "care_matrix", matrix_layout: "wide", header_row: hr, columns: cols, program_cols: pc };
    }
    Object.keys(d.mcols).forEach(function (k) { if (d.mcols[k] !== "ignore" && Number(k) < n) { cols[k] = d.mcols[k]; } });
    var byTitle = d.from_title && !hasMRole(d, "program");
    return { role: "care_matrix", matrix_layout: "long", header_row: hr, columns: cols, program_from_title: !!d.from_title,
      program: hasMRole(d, "program") || byTitle ? null : val(d.program) };
  }
  // Ошибка разметки до запроса: перечень — нет названия клиники; матрица — нет колонки видов помощи или программ
  // (длинная таблица без источника программы — сервер решит сам: у договора с одной программой это не ошибка).
  function draftError(t) {
    var d = state.map.draft;
    if (!isMatrix(d)) { return hasRole(d, "clinic_name") ? null : T("clients.mapping.err_no_name"); }
    var b = matrixBody(t, function (v) { v = String(v || "").trim(); return v || null; });
    if (!Object.keys(b.columns).some(function (k) { return b.columns[k] === "care_type"; })) { return T("clients.mapping.err_no_care"); }
    if (b.matrix_layout === "wide" && !Object.keys(b.program_cols).length) { return T("clients.mapping.err_no_program_matrix"); }
    return null;
  }
  // Источник программы строк (w9-fix п. 1г): колонка программы, программа для всей таблицы или из названия таблицы
  function hasProgramSource(d) { return hasRole(d, "program") || !!d.from_title || !!String(d.program || "").trim(); }
  function mapError(text, focusId) { state.map.err = text; state.focusAfter = focusId; repaintMap(); }
  function tryMapping(c, t) {
    var m = state.map, k = c.id, de = draftError(t);
    if (de) { mapError(de, "cl-map-trybtn-" + k); return; }
    m.err = null; m.busy = "try"; m.tryRes = null; state.focusAfter = "cl-map-trybtn-" + k; repaintMap();
    D.request("POST", tablesUrl(k) + "/try", { ref: t.ref, mapping: mappingBody(t) }).then(function (r) {
      if (state.map !== m) { return; }
      m.busy = null;
      if (r.ok) {
        m.tryRes = r.data || { rows: [], total: 0, warnings: [] };
        m.suspicious = (m.tryRes.suspicious || []).length ? m.tryRes.suspicious : null;
        if (!m.suspicious) { m.confirm = false; }
      } else { m.err = errText(r); }
      state.focusAfter = "cl-map-trybtn-" + k;
      swapNode("cl-map-bar-" + k, actionsBlock(c, t));
      var box = $("cl-map-try-" + k);
      if (box) { clear(box); if (m.tryRes) { tryContent(m.tryRes).forEach(function (n) { box.appendChild(n); }); } }
    });
  }
  function fmtFranchise(v) {
    if (v === null || v === undefined || v === "") { return T("clients.mapping.franchise_none"); }
    if (v === "—") { return T("clients.mapping.franchise_na"); }
    return T("clients.mapping.franchise_pct", { n: String(v).replace(".", ",") });
  }
  // Пробный разбор матрицы: таблица программа × вид помощи (виды помощи — строки, программы — колонки), статус
  // «значок + слово»; ниже — пропущенные ячейки (не «да» / «нет»).
  function matrixGrid(rows) {
    var progs = [], cares = [], val = {};
    rows.forEach(function (r) {
      if (progs.indexOf(r.program) < 0) { progs.push(r.program); }
      if (cares.indexOf(r.care_type) < 0) { cares.push(r.care_type); }
      val[r.program + "\u0000" + r.care_type] = r.present;
    });
    function mark(v) {
      if (v === true) { return D.chip(T("clients.mapping.yes_mark"), "chip--ok"); }
      if (v === false) { return D.chip(T("clients.mapping.no_mark"), "chip--danger chip--x"); }
      return el("span", { class: "muted", text: "— " + T("clients.mapping.unknown_mark") });
    }
    return el("div", { class: "tbl-wrap cl-map__mgrid", role: "region", tabindex: "0", "aria-label": T("clients.mapping.try_matrix_caption") }, [
      el("table", { class: "cl-map__res cl-map__mx" }, [el("caption", { class: "vh", text: T("clients.mapping.try_matrix_caption") }),
        el("thead", null, [el("tr", null, [el("th", { scope: "col", text: T("clients.mapping.try_matrix_care") })].concat(progs.map(function (p) {
          return el("th", { scope: "col", text: p });
        })))]),
        el("tbody", null, cares.map(function (care) {
          return el("tr", null, [el("th", { scope: "row", text: care })].concat(progs.map(function (p) {
            return el("td", { "data-th": p }, [mark(val[p + "\u0000" + care])]);
          })));
        }))])]);
  }
  function skippedBlock(res) {
    var sk = res.skipped || [], n = Number(res.skipped_total || sk.length || 0);
    if (!n) { return []; }
    return [el("h5", { class: "sub", text: T("clients.mapping.skipped_title", { n: n.toLocaleString("ru-RU") }) }),
      el("div", { class: "tbl-wrap" }, [el("table", { class: "cl-map__res" }, [
        el("thead", null, [el("tr", null, ["cell", "text"].map(function (x) { return el("th", { scope: "col", text: T("clients.mapping.skipped_cols." + x) }); }))]),
        el("tbody", null, sk.map(function (s) {
          return el("tr", null, [el("td", { "data-th": T("clients.mapping.skipped_cols.cell"), class: "nowrap", text: s.cell }),
            el("td", { "data-th": T("clients.mapping.skipped_cols.text"), text: s.text })]);
        }))])])];
  }
  function warnList(warns) {
    return warns.length ? [el("h5", { class: "sub", text: T("clients.mapping.try_warnings") }),
      el("ul", { class: "warns" }, warns.map(function (w) { return el("li", null, [D.chip("!", "chip--warn chip--bang chip--icon"), " ", w]); }))] : [];
  }
  function tryContent(res) {
    if (res.rows === undefined && res.matrix) {
      var mrows = res.matrix || [], mo = [el("h5", { class: "sub", text: T("clients.mapping.try_title") }),
        el("p", { class: "cl-map__total" }, [el("strong", { text: T("clients.mapping.try_matrix_total", { n: Number(res.total || 0).toLocaleString("ru-RU") }) }),
          mrows.length < Number(res.total || 0) ? " " + T("clients.mapping.try_shown", { n: mrows.length }) : null])];
      mo.push(mrows.length ? matrixGrid(mrows) : el("p", { class: "alert", text: T("clients.mapping.try_matrix_empty") }));
      return mo.concat(skippedBlock(res), warnList(res.warnings || []));
    }
    var rows = res.rows || [], warns = res.warnings || [], out = [el("h5", { class: "sub", text: T("clients.mapping.try_title") }),
      el("p", { class: "cl-map__total" }, [el("strong", { text: T("clients.mapping.try_total", { n: Number(res.total || 0).toLocaleString("ru-RU") }) }),
        rows.length ? " " + T("clients.mapping.try_shown", { n: rows.length }) : null])];
    if (!rows.length) { out.push(el("p", { class: "alert", text: T("clients.mapping.try_empty") })); }
    else {
      out.push(el("div", { class: "tbl-wrap" }, [el("table", { class: "cl-map__res" }, [
        el("caption", { class: "vh", text: T("clients.mapping.try_caption") }),
        el("thead", null, [el("tr", null, TRY_COLS.map(function (x) { return el("th", { scope: "col", text: T("clients.mapping.try_cols." + x) }); }))]),
        el("tbody", null, rows.map(function (row) {
          return el("tr", null, TRY_COLS.map(function (x) {
            var v = x === "franchise_pct" ? fmtFranchise(row[x]) : row[x] || T("common.none");
            return el("td", { "data-th": T("clients.mapping.try_cols." + x), class: row[x] || x === "franchise_pct" ? null : "muted", text: v });
          }));
        }))])]));
    }
    if (res.matrix) {
      // «Виды помощи из этого перечня → матрица»: какие пары даст перечень
      out.push(el("h5", { class: "sub", text: T("clients.mapping.try_matrix_derive", { n: Number(res.matrix_total || res.matrix.length).toLocaleString("ru-RU") }) }));
      if (res.matrix.length) { out.push(matrixGrid(res.matrix)); }
    }
    return out.concat(warnList(warns));
  }
  function replaceTable(preview) {
    var m = state.map, key = refKey(preview.ref);
    m.tables = m.tables.map(function (t) { return refKey(t.ref) === key ? preview : t; });
  }
  // Сохранение: PUT …/tables/mapping. С process: true сервер сразу запускает обработку — ход, как у «Обработать»
  // (ответ — {preview, profile, job} или само задание OnboardingJob; учитываются оба вида).
  function saveMapping(c, t) {
    var m = state.map, k = c.id, name = String(m.profileName || "").trim(), de = draftError(t);
    if (de) { mapError(de, "cl-map-save-" + k); return; }
    if (m.saveProfile && !name) { mapError(T("clients.mapping.err_profile_name"), "cl-map-pn-" + k); return; }
    if (!isMatrix(m.draft) && !hasProgramSource(m.draft)) { mapError(T("clients.mapping.err_no_program"), "cl-map-save-" + k); return; }
    m.err = null; m.busy = "save"; state.focusAfter = "cl-map-save-" + k; repaintMap();
    var same = !!m.sameLayout && m.draft.no_header && sameLayoutN(t) > 1;
    var body = { ref: t.ref, mapping: mappingBody(t), save_profile: !!m.saveProfile, insurer_name: name || null, process: !!m.processNow,
      apply_same_layout: same, confirm_suspicious: !!(m.suspicious && m.confirm) };
    D.request("PUT", tablesUrl(k) + "/mapping", body).then(function (r) {
      if (state.map !== m) { return; }
      m.busy = null;
      if (!r.ok) {
        m.err = errText(r); state.focusAfter = "cl-map-save-" + k;
        // подозрительная разметка — сообщение сервера с причинами и флажок подтверждения у кнопок
        if (errCode(r) === "mapping_suspicious" && !m.suspicious) { m.suspicious = [m.err]; }
        repaintMap();
        if (errCode(r) === "job_running") { loadClients(); }
        return;
      }
      var data = r.data || {}, job = data.job || (data.state && data.contract_id ? data : null), nT = Number(data.applied_tables) || 1;
      var label = nT > 1 ? T("clients.mapping.tables_n", { name: tableFull(t), n: nT - 1 }) : tableFull(t);
      if (data.preview) { replaceTable(data.preview); }
      if (nT > 1) { loadTables(k); }             // другие таблицы раскладки тоже размечены — список заново
      if (data.profile) { loadProfiles(); }
      var text = job ? T("clients.mapping.saved_process", { name: label })
        : (data.profile ? T("clients.mapping.saved_profile", { name: label, insurer: data.profile.insurer_name }) : T("clients.mapping.saved", { name: label }))
          + " " + T("clients.mapping.saved_need_process");
      live(text);
      if (job) {
        var hit = findContract(k);
        state.map = null;
        delete state.dirty[k];
        state.lastStage[k] = null;
        if (hit) { hit.c.job = job; }
        state.msg["p-" + k] = { ok: text, errs: [] };
        state.focusAfter = "cl-h-" + k;
        renderList();
        renderMain();
        startPoll(job.id, k);
        return;
      }
      state.msg["map-" + k] = { ok: text, errs: [] };
      m.source = "saved"; m.marks = {};
      state.focusAfter = "cl-map-save-" + k;
      repaintMap();
    });
  }
  function removeMapping(c, t) {
    var m = state.map, k = c.id, ref = t.ref;
    var q = "?doc_id=" + enc(ref.doc_id) + "&kind=" + enc(ref.kind) + (ref.index !== null && ref.index !== undefined ? "&index=" + enc(ref.index) : "")
      + (ref.sheet ? "&sheet=" + enc(ref.sheet) : "");
    m.err = null; m.busy = "del"; state.focusAfter = "cl-map-trybtn-" + k; repaintMap();
    D.request("DELETE", tablesUrl(k) + "/mapping" + q).then(function (r) {
      if (state.map !== m) { return; }
      m.busy = null;
      if (!r.ok) { m.err = errText(r); state.focusAfter = "cl-map-trybtn-" + k; repaintMap(); return; }
      var text = T("clients.mapping.removed", { name: tableFull(t) });
      state.msg["map-" + k] = { ok: text, errs: [] };
      live(text);
      state.focusAfter = "cl-map-trybtn-" + k;
      return loadTables(k, true);
    });
  }

  // ---------- профили разметки СК (GET /api/insurer-profiles, DELETE /api/insurer-profiles/{ipN}) ----------
  function loadProfiles() {
    var P = state.profiles;
    if (P.loading) { return Promise.resolve(); }
    P.loading = true;
    return D.request("GET", "/api/insurer-profiles").then(function (r) {
      P.loading = false; P.loaded = true;
      if (r.ok) { P.error = null; P.list = r.data || []; } else { P.error = T("clients.profiles.load_error", { msg: errText(r) }); }
      swapNode("cl-prof", profilesCard());
      if (state.map) { repaintMap(); }       // подпись «предложено профилем СК «…»» берётся из списка профилей
    });
  }
  function shortSig(sig) {
    var lay = /^layout:(\d+):([\d,]*)$/.exec(String(sig || ""));
    if (lay) {
      var cols = lay[2] ? lay[2].split(",").map(function (x) { return String(Number(x) + 1); }).join(", ") : "—";
      return T("clients.profiles.layout_sig", { n: lay[1], cols: cols });
    }
    var s = String(sig || "").split("|").filter(Boolean).join(" · ");
    return s.length > SIG_SHOW ? s.slice(0, SIG_SHOW - 1) + "…" : s;
  }
  function profilesCard() {
    var P = state.profiles, th = function (x) { return T("clients.profiles.cols." + x); };
    if (!P.loaded && !P.loading) { setTimeout(loadProfiles, 0); }
    var card = el("section", { class: "card cl-card", id: "cl-prof", "aria-labelledby": "cl-prof-h" }, [
      el("h3", { id: "cl-prof-h", tabindex: "-1", text: T("clients.profiles.title") }), el("p", { class: "hint", text: T("clients.profiles.hint") }), msgBlock("prof")]);
    if (!P.loaded) { card.appendChild(el("p", { class: "muted", role: "status", text: T("clients.profiles.loading") })); return card; }
    if (P.error) { card.appendChild(el("p", { class: "alert", text: P.error })); return card; }
    if (!P.list.length) { card.appendChild(el("p", { class: "muted", text: T("clients.profiles.empty") })); return card; }
    card.appendChild(el("div", { class: "tbl-wrap" }, [el("table", { class: "cl-files cl-prof" }, [
      el("caption", { class: "vh", text: T("clients.profiles.title") }),
      el("thead", null, [el("tr", null, ["insurer", "signature", "used", "updated", "actions"].map(function (x) {
        return el("th", { scope: "col", class: x === "used" ? "num" : null, text: th(x) });
      }))]),
      el("tbody", null, P.list.map(function (p) {
        return el("tr", null, [
          el("td", { "data-th": th("insurer") }, [el("strong", { text: p.insurer_name })]),
          el("td", { "data-th": th("signature") }, [el("span", { class: "cl-sig", title: p.signature, text: shortSig(p.signature) })]),
          el("td", { class: "num", "data-th": th("used") }, [String(p.used || 0)]),
          el("td", { class: "nowrap", "data-th": th("updated") }, [D.fmtDateTime(p.updated_at || p.created_at)]),
          el("td", { "data-th": th("actions") }, [el("button", { type: "button", class: "btn btn--small btn--link", id: "cl-prof-del-" + p.id,
            "aria-label": T("clients.profiles.delete_for", { name: p.insurer_name }), text: T("clients.profiles.delete"),
            on: { click: function (e) { askDeleteProfile(p, e.currentTarget); } } })])]);
      }))])]));
    return card;
  }
  function askDeleteProfile(p, btn) {
    state.del = { profile: p, btn: btn };
    $("cl-del-h").textContent = T("clients.profiles.delete_title");
    $("cl-del-ok").textContent = T("clients.profiles.delete_confirm");
    $("cl-del-text").textContent = T("clients.profiles.delete_text", { name: p.insurer_name });
    var dlg = $("cl-del-dlg");
    if (typeof dlg.showModal === "function") { dlg.showModal(); } else { dlg.setAttribute("open", ""); }
    $("cl-del-cancel").focus();
  }
  function confirmDeleteProfile(d) {
    $("cl-del-ok").disabled = true;
    D.request("DELETE", "/api/insurer-profiles/" + enc(d.profile.id)).then(function (r) {
      $("cl-del-ok").disabled = false;
      state.del = null;
      closeDelete(false);
      if (r.ok) {
        state.msg["prof"] = { ok: T("clients.profiles.deleted", { name: d.profile.insurer_name }), errs: [] };
        live(state.msg["prof"].ok);
      } else {
        state.msg["prof"] = { ok: null, errs: [errText(r)] };
      }
      state.focusAfter = "cl-prof-h";
      // Предложения в открытой разметке могли измениться (профиль больше не подставляется).
      return loadProfiles().then(function () { if (state.map) { loadTables(state.map.cid); } });
    });
  }

  function init() {
    if (!$("panel-clients")) { return; }
    ["tab-docs", "tab-clients"].forEach(function (id) {
      $(id).addEventListener("click", function () { setTab(id === "tab-docs" ? "docs" : "clients"); });
      $(id).addEventListener("keydown", tabKeys);
    });
    $("cl-new").addEventListener("click", openNew);
    $("cl-del-cancel").addEventListener("click", function () { state.del = null; closeDelete(true); });
    $("cl-del-ok").addEventListener("click", confirmDelete);
    $("cl-del-dlg").addEventListener("cancel", function () { var d = state.del; state.del = null; setTimeout(function () { if (d && d.btn) { d.btn.focus(); } }, 0); });
    // Файл, брошенный мимо зоны, браузер не открывает вместо страницы.
    ["dragover", "drop"].forEach(function (ev) {
      document.addEventListener(ev, function (e) { if (!$("panel-clients").hidden && !(e.target.closest && e.target.closest(".drop"))) { e.preventDefault(); } });
    });
    var h = D.readHash();
    if (h.tab === "clients") { state.current = h.client || null; setTab("clients"); }
    // Ссылка вида #tab=clients&client=ugN, открытая на уже загруженной странице, — тот же клиент.
    window.addEventListener("hashchange", function () {
      var hh = D.readHash();
      if (hh.tab !== "clients") { return; }
      state.current = hh.client || null;
      if ($("panel-clients").hidden) { setTab("clients"); }
      if (state.loaded) { renderList(); renderMain(); }
    });
  }
  D.ready.then(init);
})();
