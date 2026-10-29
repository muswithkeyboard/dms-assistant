/* Страница «Чат» (план §8, ТЗ п. 8, 8.1, 9; docs/ui_style.md; ADR 0014). Чат как мессенджер: слева чаты (и обращения,
   если их несколько), в центре лента — реплика менеджера справа, ответ ассистента слева (Answer.reply: lead, «Что
   сказано в договоре», «Что уточнить»; служебное — под «Подробнее»); источник по сноске — шторка справа. API — docs/CONTRACTS.md §8; ответы — модели schemas.py. Данные сервера — только textContent.
   Письма не отправляются: черновик можно скопировать, скачать .txt или открыть в почте — mailto: собирается
   только в обработчике клика по кнопке «Открыть в почте» (openMail), заранее ссылки mailto: в странице нет. */
(function () {
  "use strict";
  var D = window.DMS, T = D.T, el = D.el, $ = D.$, clear = D.clear;

  var state = {
    groups: [], groupById: {}, contractById: {},
    chats: [], chat: null, requests: [], req: null,
    examples: [], busy: false,
    sources: [], sourceSel: null, clauseCache: {}, draftTab: "hr",
    docTitles: {}, stickyError: null, offerOpen: false, lastCite: null,
    clinicFind: {}, clinicRows: {},
    rateOpen: false, detailsOpen: {},
    att: { items: [], active: null }, attFile: null
  };

  // ---------- контекст: названия групп и договоров ----------
  function indexGroups(groups) {
    state.groups = groups || [];
    state.groupById = {}; state.contractById = {};
    state.groups.forEach(function (g) {
      state.groupById[g.id] = g;
      (g.contracts || []).forEach(function (c) { c.group_id = g.id; state.contractById[c.id] = c; });
    });
  }
  function contractName(id) {
    var c = state.contractById[id];
    if (!c) { return id || T("common.none"); }
    return c.legal_entity + (c.number ? " · № " + c.number : "");
  }
  // Текст пункта для показа: без маркера списка «- » / «• » в начале (так пункты приходят из разбора docx)
  // и без разметки сносок документа «[^7]» (ревизия w16: «томография[^7]» в пузыре и в панели источников).
  var FOOTNOTE_RE = /\[\^\d+\]/g;
  function noFootnotes(s) { return String(s || "").replace(FOOTNOTE_RE, ""); }
  function cleanText(s) { return noFootnotes(s).replace(/^\s*[-–—•·]\s+/, ""); }
  // Укоротить до n символов по границе слова и поставить «…» (полный текст — в правой панели).
  function shorten(s, n) {
    s = cleanText(s);
    if (s.length <= n) { return s; }
    var cut = s.slice(0, n), sp = cut.lastIndexOf(" ");
    return (sp > n * 0.6 ? cut.slice(0, sp) : cut).replace(/[\s,;:.–-]+$/, "") + "…";
  }
  function norm(s) { return cleanText(s).toLowerCase().replace(/[«»"“”„]/g, "").replace(/\s+/g, " ").trim(); }
  // Название документа: doc_title из ответа; если там пусто или служебный id — название из /api/documents.
  function docTitle(c) {
    var t = c.doc_title;
    if (t && t !== c.doc_id && !/^[\w.-]+$/.test(t)) { return t; }
    return state.docTitles[c.doc_id] || t || c.doc_id || T("common.none");
  }
  function loadDocTitles(ctx) {
    ctx = ctx || {};
    var ids = ctx.contract_id ? [ctx.contract_id] : ((state.groupById[ctx.group_id] || {}).contracts || []).map(function (c) { return c.id; });
    var qs = ids.map(function (id) { return "contract_id=" + encodeURIComponent(id); });
    if (ctx.group_id) { qs.push("group_id=" + encodeURIComponent(ctx.group_id)); }
    return Promise.all(qs.map(function (q) {
      return D.request("GET", "/api/documents?" + q).then(function (r) {
        (r.ok && Array.isArray(r.data) ? r.data : []).forEach(function (d) { if (d.id && d.title) { state.docTitles[d.id] = d.title; } });
      });
    }));
  }
  // В контексте группы у каждого источника указываем договор (ADR 0005: условия договоров не смешиваются).
  function citeContract(c) {
    var ctx = (state.chat && state.chat.context) || {};
    if (!c.contract_id || (ctx.contract_id && ctx.contract_id === c.contract_id)) { return null; }
    return T("answer.source_contract", { name: contractName(c.contract_id) });
  }
  function groupName(id) { var g = state.groupById[id]; return g ? g.name : (id || T("common.none")); }
  function contextTitle(ctx) {
    ctx = ctx || {};
    return ctx.contract_id ? contractName(ctx.contract_id) : groupName(ctx.group_id);
  }
  // Шапка чата и подпись в списке — одной строкой: «Договор № N · программа» / «ГК … · программа».
  function contractShort(id) {
    var c = state.contractById[id];
    return c && c.number ? T("chat.head_contract", { number: c.number }) : contractName(id);
  }
  function headLine(ctx) {
    ctx = ctx || {};
    return [ctx.contract_id ? contractShort(ctx.contract_id) : groupName(ctx.group_id), ctx.program].filter(Boolean).join(" · ");
  }
  // Подпись чата в списке: договор (или число договоров группы) и программа, если задана — два чата по одному
  // договору с разными программами различимы (ревизия w16).
  function chatSub(ctx) {
    ctx = ctx || {};
    var g = state.groupById[ctx.group_id];
    var base = ctx.contract_id ? contractShort(ctx.contract_id) : g ? T("chat.head_contracts_n", { n: (g.contracts || []).length }) : "";
    return [base, ctx.program].filter(Boolean).join(" · ");
  }
  function contextKv(ctx, opts) {
    ctx = ctx || {}; opts = opts || {};
    var rows = [];
    var gid = ctx.group_id || (state.contractById[ctx.contract_id] || {}).group_id;
    if (gid) { rows.push([T("chat.ctx_group"), groupName(gid)]); }
    if (ctx.contract_id) {
      var c = state.contractById[ctx.contract_id];
      rows.push([T("chat.ctx_contract"), contractName(ctx.contract_id) + (c && c.insurer ? " · " + T("insurers." + c.insurer) : "")]);
    } else if (gid && state.groupById[gid]) {
      rows.push([T("chat.ctx_contract"), T("chat.ctx_contracts_n", { n: (state.groupById[gid].contracts || []).length })]);
    }
    rows.push([T("chat.ctx_program"), ctx.program || T("chat.ctx_all_programs")]);
    if (ctx.date || opts.withDate) { rows.push([T("chat.ctx_date"), D.fmtDate(ctx.date)]); }
    var dl = el("dl", { class: "kv" + (opts.cls ? " " + opts.cls : "") });
    rows.forEach(function (r) {
      // В шапке пара «ключ: значение» — один неразрывный блок (div внутри dl допустим), чтобы не рвалась по строкам.
      if (opts.pairs) { dl.appendChild(el("div", { class: "ctx__pair", title: r[0] + ": " + r[1] }, [el("dt", { text: r[0] }), el("dd", { text: r[1] })])); }
      else { dl.appendChild(el("dt", { text: r[0] })); dl.appendChild(el("dd", { text: r[1] })); }
    });
    return dl;
  }

  // ---------- ошибки ----------
  // showAlert("") не стирает ошибку загрузки групп (state.stickyError): без групп названия договоров неверны.
  function showAlert(text, retry) {
    var a = $("alert");
    if (!text && state.stickyError) { text = state.stickyError.text; retry = state.stickyError.retry; }
    clear(a);
    a.hidden = !text;
    if (!text) { return; }
    a.appendChild(el("span", { text: text }));
    if (retry) { a.appendChild(el("button", { class: "btn btn--small", type: "button", text: T("common.retry"), on: { click: retry } })); }
    var r = a.getBoundingClientRect();
    if (r.top < 0 || r.bottom > window.innerHeight) { a.scrollIntoView({ block: "center" }); }
  }

  // ---------- чаты ----------
  function loadChats() {
    var list = $("chat-list");
    list.setAttribute("aria-busy", "true");
    return D.request("GET", "/api/chats").then(function (r) {
      list.removeAttribute("aria-busy");
      if (!r.ok) { clear(list); list.appendChild(el("li", { class: "navlist__empty alert", text: T("chat.chats_error") + " " + D.errorText(r) })); return; }
      state.chats = (r.data || []).slice().sort(function (a, b) { return String(b.created_at).localeCompare(String(a.created_at)); });
      renderChats();
    });
  }
  function renderChats() {
    var list = $("chat-list");
    clear(list);
    if (!state.chats.length) { list.appendChild(el("li", { class: "navlist__empty muted", text: T("chat.chats_empty") })); return; }
    state.chats.forEach(function (c) {
      // Компактно: название и серым — договор (или число договоров группы); дата создания — в подсказке.
      var btn = el("button", { type: "button", class: "navlist__item navlist__item--ico", "aria-current": state.chat && state.chat.id === c.id ? "true" : null,
        title: c.created_at ? T("chat.chat_created", { at: D.fmtDateTime(c.created_at) }) : null,
        on: { click: function () { closeSide(); selectChat(c.id); } } }, [
        // Иконка контекста: группа компаний — здание, один договор — человек (сотрудники компании).
        el("span", { class: "ico ico--sm ico--" + (c.context && c.context.contract_id ? "person" : "building"), "aria-hidden": "true" }),
        el("span", { class: "navlist__title", text: chatTitle(c) }),
        el("span", { class: "navlist__sub", text: chatSub(c.context) })
      ]);
      list.appendChild(el("li", null, [btn]));
    });
  }
  // Одинаковые названия чатов (один контекст) нумеруются по времени создания: «… · № 2».
  function chatTitle(c) {
    var base = c.title || contextTitle(c.context);
    var same = state.chats.filter(function (x) { return (x.title || contextTitle(x.context)) === base; });
    if (same.length < 2) { return base; }
    same.sort(function (a, b) { return String(a.created_at).localeCompare(String(b.created_at)); });
    return base + " · " + T("chat.chat_n", { n: same.indexOf(c) + 1 });
  }
  function selectChat(id, reqId) {
    var c = state.chats.filter(function (x) { return x.id === id; })[0];
    if (!c) { return Promise.resolve(); }
    state.chat = c; state.req = null; state.requests = []; state.sources = []; state.sourceSel = null; state.offerOpen = false; state.rateOpen = false;
    renderChats(); renderHead(); renderSources();
    $("new-req-btn").disabled = false;
    setAskEnabled(true);
    D.writeHash({ chat: c.id, req: reqId || null });
    loadDocTitles(c.context).then(function () { if (state.chat === c && state.req) { renderRequest(); } });
    state.att = { items: [], active: null }; renderAttBar(); loadAttachments();
    // Пока грузился список, адрес мог смениться («Назад», правка) — открываем запрос из текущего адреса.
    return loadRequests().then(function () {
      if (state.chat !== c) { return; }
      var h = D.readHash(), want = h.chat === c.id ? (h.req || null) : reqId;
      if (want) { return openRequestById(want); }
      // Без &req= открываем последнее открытое обращение (список — новые сверху), иначе последнее вообще:
      // при одном обращении список слева скрыт, и пустой чат выглядел бы как потерянная история (ревизия w16).
      var open = state.requests.filter(function (x) { return x.status !== "closed"; })[0] || state.requests[0];
      if (open) { return openRequestById(open.id); }
      return D.request("GET", "/api/chats/" + encodeURIComponent(c.id) + "/requests?status=all").then(function (r) {
        if (state.chat !== c || state.req) { return; }
        var last = r.ok && Array.isArray(r.data) ? r.data[0] : null;
        return openRequestById(last ? last.id : null);
      });
    });
  }
  // Запрос из адреса (#req=): есть в списке — выбираем; закрыт и скрыт фильтром — открываем без выделения в списке
  // и без включения «Показать закрытые» (фильтр — выбор менеджера); не нашёлся — сбрасываем req в адресе.
  function openRequestById(reqId) {
    var target = reqId && state.requests.filter(function (r) { return r.id === reqId; })[0];
    if (target || !reqId) { selectRequest(target ? target.id : null); return Promise.resolve(); }
    var chatId = state.chat.id;
    return D.request("GET", "/api/chats/" + encodeURIComponent(chatId) + "/requests?status=all").then(function (r) {
      if (!state.chat || state.chat.id !== chatId) { return; }
      var hidden = r.ok ? (r.data || []).filter(function (x) { return x.id === reqId; })[0] : null;
      if (!hidden) { selectRequest(null); return; }
      state.req = hidden; state.sources = []; state.sourceSel = null; state.offerOpen = false;
      D.writeHash({ chat: chatId, req: hidden.id });
      renderRequests(); renderRequest(); renderSources(); scrollToLast();
    });
  }
  // «Назад» / правка адреса: hashchange → тот же выбор, что после обновления страницы.
  function applyHash() {
    var h = D.readHash();
    if (!h.chat) { return; }
    if (!state.chat || state.chat.id !== h.chat) { selectChat(h.chat, h.req); return; }
    if ((h.req || null) !== ((state.req && state.req.id) || null)) { openRequestById(h.req || null); }
  }
  function renderHead() {
    // Одна строка «Договор № N · программа»; юрлицо и дата расчёта — в подсказке (полный контекст — под «Подробнее»).
    var h = $("chat-h");
    $("chat-menu-box").hidden = !state.chat;
    if (!state.chat) { h.textContent = T("chat.intro_title"); h.removeAttribute("title"); return; }
    var ctx = state.chat.context || {};
    h.textContent = headLine(ctx);
    h.setAttribute("title", [contextTitle(ctx), ctx.date ? T("chat.ctx_date") + ": " + D.fmtDate(ctx.date) : ""].filter(Boolean).join(" · "));
  }

  // ---------- запросы ----------
  function loadRequests() {
    if (!state.chat) { return Promise.resolve(); }
    var status = $("show-closed").checked ? "all" : "open";
    return D.request("GET", "/api/chats/" + encodeURIComponent(state.chat.id) + "/requests?status=" + status).then(function (r) {
      if (!r.ok) { showAlert(D.errorText(r), loadRequests); return; }
      state.requests = r.data || [];
      if (state.req) {
        var fresh = state.requests.filter(function (x) { return x.id === state.req.id; })[0];
        if (fresh) { state.req = fresh; }
      }
      renderRequests();
    });
  }
  function renderRequests() {
    var list = $("req-list");
    clear(list);
    // Обращения менеджеру по умолчанию не нужны: одно открытое — список скрыт (новый вопрос и закрытие — в меню «⋯»).
    var many = state.requests.length > 1 || $("show-closed").checked ||
      !!(state.req && !state.requests.some(function (x) { return x.id === state.req.id; }));
    $("req-box").hidden = !state.chat || !many;
    renderMenu();
    if (!state.chat) { return; }
    if (!state.requests.length) { list.appendChild(el("li", { class: "navlist__empty muted", text: T("chat.requests_empty") })); return; }
    state.requests.forEach(function (q) {
      var closed = q.status === "closed";
      var btn = el("button", { type: "button", class: "navlist__item" + (closed ? " is-closed" : ""),
        "aria-current": state.req && state.req.id === q.id ? "true" : null, on: { click: function () { selectRequest(q.id); } } }, [
        el("span", { class: "navlist__title" }, [el("span", { class: "dot", "aria-hidden": "true", text: closed ? "○" : "●" }), " " + q.title]),
        el("span", { class: "navlist__sub", text: (closed ? T("chat.status_closed") : T("chat.status_open")) +
          (q.scenario ? " · " + T("scenarios." + q.scenario) : "") + (q.rating ? " · " + T("chat.star", { n: q.rating }) : "") })
      ]);
      list.appendChild(el("li", null, [btn]));
    });
  }
  function selectRequest(id) {
    state.req = id ? state.requests.filter(function (x) { return x.id === id; })[0] || null : null;
    state.sources = []; state.sourceSel = null; state.offerOpen = false; state.rateOpen = false;
    D.writeHash({ chat: state.chat && state.chat.id, req: state.req && state.req.id });
    renderRequests(); renderRequest(); renderSources(); scrollToLast();
  }
  function replaceRequest(updated) {
    if (!updated || !updated.id) { return; }
    var found = false;
    state.requests = state.requests.map(function (x) { if (x.id === updated.id) { found = true; return updated; } return x; });
    if (!found) { state.requests.unshift(updated); }
    if (!$("show-closed").checked && updated.status === "closed") {
      state.requests = state.requests.filter(function (x) { return x.id !== updated.id || x.id === (state.req && state.req.id); });
    }
    state.req = updated;
    renderRequests(); renderRequest();
  }
  // Фокус после перерисовки запроса (ревизия w3-ui-fix): первый видимый и активный элемент из списка селекторов —
  // кнопка следующего шага, статус, сноска. Не-интерактивному элементу ставится tabindex="-1".
  function focusAfter(selectors) {
    for (var i = 0; i < selectors.length; i += 1) {
      var n = document.querySelector(selectors[i]);
      if (!n || n.disabled || n.offsetParent === null) { continue; }
      if (!/^(BUTTON|A|INPUT|TEXTAREA|SELECT)$/.test(n.tagName) && !n.hasAttribute("tabindex")) { n.setAttribute("tabindex", "-1"); }
      n.focus();
      return;
    }
  }

  // ---------- лента сообщений ----------
  function renderRequest() {
    var box = $("messages"), tools = $("request-tools");
    hidePreview(false);   // ссылка, к которой привязано превью, сейчас исчезнет вместе с лентой
    clear(box); clear(tools);
    showAlert("");
    renderMenu();
    if (!state.chat) {
      box.appendChild(hero());
      appendSuggestions(box);
      return;
    }
    var q = state.req;
    if (!q) {
      // Пустой чат: приветствие ассистента и подсказки-примеры; первый вопрос создаст обращение сам.
      box.appendChild(el("div", { class: "msg msg--bot intro" }, [el("p", { text: T("chat.intro_text") })]));
      appendSuggestions(box);
      setAskEnabled(true);
      return;
    }
    if (!state.requests.some(function (x) { return x.id === q.id; })) { box.appendChild(el("p", { class: "hint", text: T("chat.closed_hidden") })); }
    var lastAnswerIdx = -1;
    (q.messages || []).forEach(function (m, i) { if (m.answer) { lastAnswerIdx = i; } });
    (q.messages || []).forEach(function (m, i) {
      if (m.role === "user") { box.appendChild(userBubble(m.text, m.created_at)); }
      // Ответ словами (ADR 0014) — если сервер прислал reply; иначе прежняя карточка (обратная совместимость).
      else if (m.answer) {
        var node = m.answer.reply ? replyBubble(m.answer, i, i === lastAnswerIdx) : answerCard(m.answer, i, i === lastAnswerIdx);
        box.appendChild(supersededByPick(q.messages, i) ? prevFold(node, i) : node);
      }
      else if (m.text) { box.appendChild(el("article", { class: "msg msg--bot" }, [el("p", { text: m.text })])); }
    });
    if (q.automation) { box.appendChild(automationCard(q)); }
    if ((q.drafts || []).length) { box.appendChild(draftsCard(q)); }
    tools.appendChild(requestTools(q));
    setAskEnabled(q.status !== "closed");
    // Шторка источников по меню «Все источники» — сноски последнего ответа.
    if (lastAnswerIdx >= 0 && !state.sources.length) { state.sources = orderedSources(q.messages[lastAnswerIdx].answer); }
  }
  // Выбор клиники пересчитывает ответ (POST …/clinic): сервер добавляет реплику «Клиника: …» и новый ответ, прежний
  // остаётся в истории. Ответ, ждавший выбора клиники, за которым следует ответ с учтённым выбором, — сворачиваем
  // под «Подробнее» (не удаляя), чтобы лента не удваивалась (ревизия w16).
  function pendingPick(a) { return (a.clinic_choices || []).some(function (ch) { return ch.selected === null || ch.selected === undefined; }); }
  function supersededByPick(msgs, i) {
    if (!pendingPick(msgs[i].answer)) { return false; }
    for (var j = i + 1; j < msgs.length; j += 1) {
      if (msgs[j].answer) { return (msgs[j].answer.clinic_choices || []).some(function (ch) { return ch.selected !== null && ch.selected !== undefined; }); }
    }
    return false;
  }
  function prevFold(node, msgIdx) {
    var key = "prev|" + detailsKey(msgIdx), open = !!state.detailsOpen[key];
    var id = "prev-" + String(msgIdx) + "-" + Math.floor(Math.random() * 1e6);
    var body = el("div", { class: "prev__body", id: id, hidden: !open }, [node]);
    var btn = el("button", { type: "button", class: "btn btn--small btn--link prev__btn", "aria-expanded": open ? "true" : "false", "aria-controls": id,
      text: T(open ? "reply.prev_hide" : "reply.prev_show"), on: { click: function () {
        var now = body.hidden; body.hidden = !now; state.detailsOpen[key] = now;
        btn.setAttribute("aria-expanded", now ? "true" : "false"); btn.textContent = T(now ? "reply.prev_hide" : "reply.prev_show");
      } } });
    return el("div", { class: "prev" }, [el("p", { class: "prev__bar" }, [el("span", { class: "prev__t", text: T("reply.prev_before_pick") }), " ", btn]), body]);
  }
  // Лента прокручивается к началу последнего ответа (длинный ответ читается сверху), без ответа — в конец.
  // Вызывается при открытии обращения и после нового ответа, но не при перерисовке (вкладки черновиков и т. п.).
  function scrollToLast() {
    var box = $("messages"), list = box.querySelectorAll(".answer"), last = list[list.length - 1];
    // Вопрос менеджера над ответом тоже виден — так читается пара «вопрос → ответ».
    var prev = last && last.previousElementSibling, top = prev && prev.classList.contains("msg--user") ? prev : last;
    box.scrollTop = top ? Math.max(0, top.offsetTop - 8) : box.scrollHeight;
  }
  function userBubble(text, at) {
    return el("div", { class: "msg msg--user" }, [
      el("p", { class: "msg__text", text: text || "" }),
      at ? el("p", { class: "msg__who", text: D.fmtDateTime(at) }) : null
    ]);
  }
  // Приветствие без выбранного чата: ASCII-арт темы (CSS --art-hero), заголовок, «Новый чат» и три шага.
  function hero() {
    var steps = el("ol", { class: "hero__steps", "aria-label": T("chat.hero_steps_title") });
    [["building", "chat.hero_step1"], ["laptop", "chat.hero_step2"], ["chart", "chat.hero_step3"]].forEach(function (s, i) {
      steps.appendChild(el("li", { class: "hero__step" }, [
        el("span", { class: "ico-tile" }, [el("span", { class: "ico ico--" + s[0], "aria-hidden": "true" })]),
        el("span", { class: "hero__n", text: "0" + String(i + 1) }),
        el("span", { text: T(s[1]) })
      ]));
    });
    return el("div", { class: "hero" }, [
      el("div", { class: "hero__art", "aria-hidden": "true" }),
      el("span", { class: "ico-tile ico-tile--lg" }, [el("span", { class: "ico ico--md ico--doctor", "aria-hidden": "true" })]),
      el("p", { class: "hero__t", text: T("chat.hero_title") }),
      el("p", { class: "hero__p", text: T("chat.hero_text") }),
      el("button", { type: "button", class: "btn btn--primary", text: "+ " + T("chat.new_chat"), on: { click: function () { $("new-chat-btn").click(); } } }),
      steps
    ]);
  }
  // Иконка примера по теме вопроса (только оформление): правила — ui.yaml chat.example_icons, список по порядку
  // ({icon, words}: основы слов); первое совпавшее правило, иначе — врач. Список, а не словарь: tojson сортирует ключи.
  function exampleIcon(q) {
    var s = String(q || "").toLowerCase(), hit = "doctor";
    (D.cfg("chat.example_icons") || []).some(function (r) {
      if ((r.words || []).some(function (w) { return s.indexOf(w) >= 0; })) { hit = r.icon; return true; }
      return false;
    });
    return hit;
  }
  // Примеров нет — блок подсказок не добавляется (appendChild(null) бросал ошибку на каждой отрисовке пустого чата).
  function appendSuggestions(box) { var s = suggestions(); if (s) { box.appendChild(s); } }
  // Подсказки-примеры в пустом чате: до 4 первых примеров кнопками; остальные — «Примеры вопросов» над полем ввода.
  function suggestions() {
    if (!state.examples.length) { return null; }
    var ul = el("ul", { class: "suggest__list" });
    state.examples.slice(0, 4).forEach(function (ex) {
      ul.appendChild(el("li", null, [el("button", { type: "button", class: "suggest__btn", on: { click: function () { runExample(ex); } } }, [
        el("span", { class: "ico ico--sm ico--" + exampleIcon(ex.question || ex.title), "aria-hidden": "true" }),
        el("span", { class: "suggest__body" }, [
          el("span", { text: ex.question || ex.title || "" }),
          ex.context ? el("span", { class: "suggest__ctx", text: T("chat.example_ctx", { ctx: ex.context.contract_id ? contractShort(ex.context.contract_id) : groupName(ex.context.group_id) }) }) : null
        ])
      ])]));
    });
    return el("div", { class: "suggest" }, [el("p", { class: "hint", text: T("chat.suggest_title") }), ul]);
  }

  // Сноски: одинаковые ссылки (документ + пункт/строка + цитата) получают один номер в пределах ответа.
  function citeKey(c) { return [c.doc_id, c.clause_number || "", c.table_ref || "", c.quote || ""].join("|"); }
  function collectSources(answer) {
    var list = [], seen = {};
    function add(st) { (st.citations || []).forEach(function (c) { var k = citeKey(c); if (!seen[k]) { seen[k] = true; list.push(c); } }); }
    (answer.per_contract || []).forEach(function (pc) { (pc.statements || []).forEach(add); });
    (answer.statements || []).forEach(add);
    (answer.comparisons || []).forEach(add);
    return list;
  }
  // Номера сносок в ответе словами: сначала источники пунктов «Что сказано в договоре» по порядку, затем остальные.
  function orderedSources(a) {
    var all = collectSources(a);
    if (!a.reply) { return all; }
    var out = [];
    (a.reply.points || []).forEach(function (p) { pointCites(p, all).forEach(function (c) { if (out.indexOf(c) < 0) { out.push(c); } }); });
    all.forEach(function (c) { if (out.indexOf(c) < 0) { out.push(c); } });
    return out;
  }
  // Ссылка пункта (ReplyPoint.refs, CONTRACTS §12) → проверенная Citation ответа. Ссылка — ref пункта или строки
  // таблицы: совпадение с Citation.ref (если сервер его отдаёт), с table_ref или «документ:пункт». Если ни одна не
  // нашлась — по цитате: quote пункта — часть проверенной цитаты (так его собирает compose.chat_reply).
  function refMatch(c, r) {
    if (!r) { return false; }
    if (c.ref && c.ref === r) { return true; }
    if (c.table_ref && c.table_ref === r) { return true; }
    if (c.clause_number && c.doc_id && r.indexOf(c.doc_id) === 0) { return r.slice(c.doc_id.length).replace(/^[:#./]+/, "") === c.clause_number; }
    return false;
  }
  function pointCites(p, all) {
    var out = [];
    function own(c) { return !p.contract_id || !c.contract_id || c.contract_id === p.contract_id; }
    var q = norm(p.quote);
    function quoted(c) { var cq = norm(c.quote); return !!(q && cq && (cq.indexOf(q) >= 0 || q.indexOf(cq) >= 0)); }
    (p.refs || []).forEach(function (r) {
      // У одного пункта бывает несколько ссылок с разными цитатами — берём ту, чья цитата совпадает с цитатой пункта.
      var hit = all.filter(function (c) { return own(c) && refMatch(c, r); });
      var best = hit.filter(quoted);
      (best.length ? best : hit).slice(0, 1).forEach(function (c) { if (out.indexOf(c) < 0) { out.push(c); } });
    });
    if (!out.length && q) { all.forEach(function (c) { if (!out.length && own(c) && quoted(c)) { out.push(c); } }); }
    return out;
  }
  // Ссылка на строку таблицы — словами («табл. 5, стр. 250», «лист «Лист1», стр. 57»); служебный id не показываем.
  function humanRef(ref) {
    if (!ref) { return ""; }
    var m = /(?:^|#)t(\d+)r(\d+)$/.exec(ref);
    if (m) { return T("reply.table_row", { t: m[1], r: m[2] }); }
    m = /^(.+)!\$?[A-Za-z]{1,3}\$?(\d+)$/.exec(ref);
    if (m) { return T("reply.sheet_row", { sheet: m[1].replace(/^'|'$/g, ""), r: m[2] }); }
    if (/[#:!]/.test(ref) || /^[\w.-]+$/.test(ref)) { return ""; }
    return ref;
  }
  function citeWhere(c) {
    var parts = [];
    if (c.clause_number) { parts.push(T("answer.clause", { n: c.clause_number })); }
    var tr = humanRef(c.table_ref);
    if (tr) { parts.push(tr); }
    if (c.page && !/(?:^|#)t\d+r\d+$/.test(c.table_ref || "")) { parts.push(T("answer.page", { n: c.page })); }
    return parts.join(", ");
  }
  function citeButtons(st, sources) {
    return (st.citations || []).map(function (c) {
      var n = 0;
      sources.forEach(function (s, i) { if (citeKey(s) === citeKey(c)) { n = i + 1; } });
      // Без title: подсказку браузера заменяет превью источника (sourceLink).
      return sourceLink("cite", c, sources, n - 1, { "data-n": String(n),
        "aria-label": T("answer.open_source") + " " + n + ": " + docTitle(c) + (citeWhere(c) ? ", " + citeWhere(c) : "") }, ["[" + n + "]"]);
    });
  }
  function statementList(stmts, sources) {
    var ul = el("ul", { class: "list stmts" });
    stmts.forEach(function (st) {
      var kind = st.kind && st.kind !== "fact" ? T("statement_kinds." + st.kind) : "";
      var text = (st.citations || []).length ? shorten(st.text, 260) : cleanText(st.text);
      var li = el("li", null, [kind ? el("span", { class: "tag", text: kind }) : null, kind ? " " : null, text, " "]);
      citeButtons(st, sources).forEach(function (b) { li.appendChild(b); li.appendChild(document.createTextNode(" ")); });
      ul.appendChild(li);
    });
    return ul;
  }
  function card(title, children, cls) {
    return el("section", { class: "card" + (cls ? " " + cls : "") }, [el("h3", { text: title })].concat(children));
  }

  // ---------- ответ словами (ADR 0014, CONTRACTS §12: Answer.reply) ----------
  // Метка — одной строкой «иконка + слово» цветами слайда 12 (зелёный / жёлтый / красный / серый, через токены):
  // «Нужно уточнить» — пока не выбрана клиника или ответ ждёт уточнения; у ГК без общей метки — по договорам.
  var VERDICT_CHIP = { allowed: "chip--ok", conditional: "chip--warn chip--cond", denied: "chip--danger chip--x",
                       insufficient: "chip--q", clarify: "chip--q", mixed: "chip--cond" };
  function verdictOf(a) {
    var pending = (a.clinic_choices || []).some(function (ch) { return ch.selected === null || ch.selected === undefined; });
    if (pending) { return "clarify"; }
    if (a.label) {
      var asks = ((a.reply && a.reply.ask) || []).length;
      return a.label === "insufficient" && a.confidence === "need_clarification" && asks ? "clarify" : a.label;
    }
    var labels = (a.per_contract || []).map(function (pc) { return pc.label; }).filter(Boolean);
    if (labels.length && labels.every(function (l) { return l === labels[0]; })) { return labels[0]; }
    // Метки нет (сценарий без вердикта, напр. подбор клиники) — чип не рисуем, отвечает lead.
    return labels.length ? "mixed" : null;
  }
  function verdictChip(v) { return v ? el("span", { class: "chip reply__verdict " + (VERDICT_CHIP[v] || "chip--q"), text: T("reply.verdict." + v) }) : null; }
  function detailsKey(msgIdx) { return (state.req ? state.req.id : "") + "|" + msgIdx; }

  function replyBubble(a, msgIdx, isLast) {
    var r = a.reply, sources = orderedSources(a);
    var group = !(a.context || {}).contract_id;
    var art = el("article", { class: "msg msg--bot answer reply", "aria-label": T("chat.assistant") });
    // 0. Как модель-помощник поняла вопрос своими словами (ADR 0015) — до ответа, чтобы менеджер видел трактовку
    if (r.understood) {
      art.appendChild(el("p", { class: "reply__understood" }, [el("span", { class: "reply__understood-k", text: T("reply.understood") + " " }),
        el("q", { text: r.understood })]));
    }
    // 1. Прямой ответ: метка и lead (1–2 фразы).
    // lead уже начинается с метки словами («Недостаточно данных: …», слайд 13) — чип не дублирует её, а метка
    // в начале lead выделяется цветом метки
    var lead = noFootnotes(r.lead || a.summary || ""), chip = verdictChip(verdictOf(a)), m = byAttachment(a) ? null : /^([^:]{3,40}):\s*/.exec(lead);
    var leadNodes = m ? [el("strong", { class: "reply__verdict reply__verdict--" + verdictOf(a), text: m[1] + ": " }), lead.slice(m[0].length)]
                      : [chip, " ", lead];
    art.appendChild(el("p", { class: "reply__lead" }, [el("span", { class: "reply__lead-t" }, leadNodes)]));
    // 1а. Варианты ответа на уточняющий вопрос (reply.options, напр. программы): клик сразу отправляет вариант
    // как реплику менеджера — как Enter в поле ввода; сервер сам склеит его с исходным вопросом.
    var opts = (r.options || []).filter(function (o) { return typeof o === "string" && o.trim(); });
    if (opts.length) {
      var live = isLast && state.req && state.req.status !== "closed";
      art.appendChild(el("ul", { class: "reply__opts", "aria-label": T("reply.options_label") }, opts.map(function (o) {
        return el("li", null, [live
          ? el("button", { type: "button", class: "reply__opt", disabled: state.busy, on: { click: function () { if (!state.busy) { ask(o.trim()); } } } }, [o.trim()])
          : el("span", { class: "reply__opt is-off", text: o.trim() })]);
      })));
    }
    // 2. Выбор клиники (ADR 0010) — компактно внутри сообщения: от него зависит метка.
    if ((a.clinic_choices || []).length) { art.appendChild(clinicPickCard(a, isLast, msgIdx, true)); }
    // 3. «Что сказано в договоре»: формулировка, цитата курсивом, строка источника со сноской [n].
    var points = r.points || [];
    if (points.length) {
      art.appendChild(el("h3", { class: "reply__h", text: T(byAttachment(a) ? "reply.points_title_attachment" : group ? "reply.points_title_group" : "reply.points_title") }));
      var ol = el("ul", { class: "reply__points" }), lastCid = null;
      // Видны первые POINTS_SHOWN пунктов (в ГК — все: по пункту на договор), остальные — под «Ещё N»: главное
      // читается сразу, текст не давит (упрощение интерфейса 29.10.2026).
      var showAll = group || points.length <= POINTS_SHOWN + 1 || !!state.detailsOpen["pts|" + detailsKey(msgIdx)];
      points.forEach(function (p, i) {
        // ГК: пункты одного договора идут подряд под подписью «Договор № N» (ADR 0005: условия не смешиваются).
        if (group && p.contract_id && p.contract_id !== lastCid) {
          ol.appendChild(el("li", { class: "reply__ctr", text: contractShort(p.contract_id) }));
          lastCid = p.contract_id;
        }
        var item = pointItem(p, sources);
        if (!showAll && i >= POINTS_SHOWN) { item.hidden = true; item.classList.add("reply__pt--extra"); }
        ol.appendChild(item);
      });
      art.appendChild(ol);
      if (!showAll) {
        var extra = points.length - POINTS_SHOWN;
        art.appendChild(el("button", { type: "button", class: "btn btn--small btn--link reply__morepts", text: T("reply.more_points", { n: extra }),
          on: { click: function (e) {
            state.detailsOpen["pts|" + detailsKey(msgIdx)] = true;
            ol.querySelectorAll(".reply__pt--extra").forEach(function (x) { x.hidden = false; });
            e.currentTarget.remove();
          } } }));
      }
    }
    // 4. «Что уточнить»: в последнем ответе кнопкой (клик — заготовка в поле ввода) — только формулировки-вопросы
    // (кончаются на «?»); утверждения («Клиника … не найдена», «Не названа услуга…») — обычным текстом (ревизия w16).
    var asks = mergeAsks((r.ask || []).map(function (q) { return String(q || "").trim(); }).filter(Boolean));
    if (asks.length) {
      var canUse = isLast && state.req && state.req.status !== "closed";
      // «Что уточнить» — отдельным заметным блоком: то, что менеджеру нужно добавить, чтобы получить ответ. В последнем
      // ответе у каждого вопроса — готовые варианты (да / нет, «А или Б») и поле «свой ответ»: ответ сразу уходит
      // ассистенту вместе с вопросом (clarify) и склеивается с исходным вопросом (followup.clarify_reply).
      var askBox = el("div", { class: "reply__askbox" }, [el("h3", { class: "reply__h reply__h--ask", text: T("reply.ask_title") })]);
      art.appendChild(askBox);
      if (canUse && asks.some(function (q) { return q.indexOf("?") > 0; })) { askBox.appendChild(el("p", { class: "reply__askhint", text: T("reply.clarify_hint") })); }
      // уже отвеченные в этом обращении вопросы не спрашиваются снова: строка «✓ вопрос — ответ»
      var done = canUse ? answeredClarify(state.req) : {};
      // Один главный вопрос (первый неотвеченный: сервер ставит вперёд то, от чего зависит вывод) раскрыт, остальные
      // вопросы — под «Ещё N вопросов»: менеджер отвечает по одному, а не на всё сразу
      var shownQ = 0, hiddenQ = [];
      var askList = el("ul", { class: "reply__ask" }, asks.map(function (q) {
        var qi = q.indexOf("?"), isQ = qi > 0;
        if (!isQ) { return el("li", { class: "reply__note" }, [el("span", { text: q })]); }
        var question = q.slice(0, qi + 1).trim(), why = q.slice(qi + 1).trim();
        if (!canUse) { return el("li", null, [el("span", { text: q })]); }
        if (done[question] !== undefined) { return el("li", { class: "clar clar--done" }, [el("span", { text: "\u2713 " + question + " \u2014 " + done[question] })]); }
        var li = el("li", { class: "clar" }, clarifyItem(question, why));
        shownQ += 1;
        if (shownQ > 1 && !state.detailsOpen["asks|" + detailsKey(msgIdx)]) { li.hidden = true; hiddenQ.push(li); }
        return li;
      }));
      askBox.appendChild(askList);
      if (hiddenQ.length) {
        askBox.appendChild(el("button", { type: "button", class: "btn btn--small btn--link reply__moreasks", text: T("reply.more_asks", { n: hiddenQ.length }),
          on: { click: function (e) {
            state.detailsOpen["asks|" + detailsKey(msgIdx)] = true;
            hiddenQ.forEach(function (x) { x.hidden = false; });
            e.currentTarget.remove();
          } } }));
      }
    }
    // Кнопка «Добавить клинику» по answer.suggested_automation (движок) — в последнем ответе.
    if (isLast && (a.suggested_automation || state.offerOpen) && state.req && !state.req.automation && state.req.status !== "closed") {
      art.appendChild(automationOffer());
    }
    // 5. Мелко: статус ТЗ п. 8.1 (без процентов) и ограничения; «Скопировать ответ» — текст для HR (ревью сайта
    // 29.10.2026): вывод, пункты с цитатами и источниками, что уточнить; без служебного.
    var copyStatus = el("span", { class: "hint reply__copied", role: "status" });
    var foot = el("div", { class: "reply__foot" }, [
      a.confidence && !r.service ? el("p", { class: "reply__status status--" + a.confidence }, [T("reply.status") + ": ", T("confidence." + a.confidence)]) : null,
      // оговорка «решение — за СК» одна и та же у каждого ответа — показываем у последнего
      r.caution && isLast ? el("p", { class: "reply__caution", text: r.caution }) : null
    ]);
    art.appendChild(foot);
    var copyBtn = r.service ? null : el("button", { type: "button", class: "btn btn--small btn--link reply__copybtn", text: T("reply.copy"), on: { click: function () {
      copyText(replyText(a, lead, sources), art).then(function (ok) { copyStatus.textContent = ok ? T("reply.copied") : T("errors.copy_failed"); });
    } } });
    // 6. «Подробнее»: прежние блоки (основания, контекст, альтернативы, источники, технические детали) — скрыты.
    var key = detailsKey(msgIdx), did = "more-" + String(msgIdx) + "-" + Math.floor(Math.random() * 1e6);
    var open = !!state.detailsOpen[key];
    var more = el("div", { class: "reply__more", id: did, hidden: !open }, detailBlocks(a, sources, { reply: true }));
    var btn = el("button", { type: "button", class: "btn btn--small btn--link reply__morebtn", "aria-expanded": open ? "true" : "false", "aria-controls": did,
      text: T(open ? "reply.less" : "reply.more"), on: { click: function () {
        var now = more.hidden; more.hidden = !now; state.detailsOpen[key] = now;
        btn.setAttribute("aria-expanded", now ? "true" : "false"); btn.textContent = T(now ? "reply.less" : "reply.more");
      } } });
    // письмо HR / запрос в СК из этого ответа (Service.letter): шаблон конфига, основание — проверенные пункты
    var letterBtns = r.service ? [] : [["hr", "letters.btn_hr"], ["insurer", "letters.btn_insurer"]].map(function (x) {
      return el("button", { type: "button", class: "btn btn--small btn--link reply__letter", text: T(x[1]), on: { click: function () { openLetter(x[0], msgIdx); } } });
    });
    art.appendChild(el("div", { class: "reply__morebar" }, [btn, copyBtn].concat(letterBtns).concat([copyStatus])));
    art.appendChild(more);
    return art;
  }
  function pointItem(p, sources) {
    var cites = pointCites(p, sources);
    var li = el("li", { class: "reply__pt" }, [el("p", { class: "reply__text", text: cleanText(p.text) })]);
    // цитата, которая слово в слово повторяет формулировку (строка таблицы), не дублируется: проверенный фрагмент —
    // в превью и шторке источника по ссылке ниже
    if (p.quote && !sameWords(p.text, p.quote)) { li.appendChild(el("p", { class: "reply__quote", text: "«" + shorten(p.quote, 260) + "»" })); }
    if (cites.length) {
      var src = el("p", { class: "reply__src" }, [el("span", { class: "reply__srck", text: T("reply.source") + ": " })]);
      cites.forEach(function (c, i) {
        var n = sources.indexOf(c) + 1, where = citeWhere(c);
        var b = sourceLink("reply__ref", c, sources, n - 1, { "aria-label": T("answer.open_source") + " " + n + ": " + docTitle(c) + (where ? ", " + where : "") },
          [el("span", { class: "cite", "aria-hidden": "true", text: "[" + n + "]" }), " ", el("span", { class: "reply__reft", text: docTitle(c) + (where ? ", " + where : "") })]);
        if (i) { src.appendChild(document.createTextNode("; ")); }
        src.appendChild(b);
      });
      li.appendChild(src);
    }
    return li;
  }
  // Ответ простым текстом для письма или мессенджера: первая фраза, «Что сказано в договоре» (формулировка, цитата,
  // источник: документ и пункт), «Что уточнить» и оговорка о решении СК.
  function replyText(a, lead, sources) {
    var r = a.reply || {}, lines = [lead.trim()], group = !(a.context || {}).contract_id, lastCid = null;
    var points = r.points || [];
    if (points.length) {
      lines.push("", T(byAttachment(a) ? "reply.points_title_attachment" : group ? "reply.points_title_group" : "reply.points_title") + ":");
      points.forEach(function (p) {
        if (group && p.contract_id && p.contract_id !== lastCid) { lines.push(contractShort(p.contract_id) + ":"); lastCid = p.contract_id; }
        lines.push("— " + cleanText(p.text));
        if (p.quote) { lines.push("  «" + p.quote + "»"); }
        var cites = pointCites(p, sources).map(function (c) { var w = citeWhere(c); return docTitle(c) + (w ? ", " + w : ""); });
        if (cites.length) { lines.push("  " + T("reply.source") + ": " + cites.join("; ")); }
      });
    }
    var asks = (r.ask || []).map(function (q) { return String(q || "").trim(); }).filter(Boolean);
    if (asks.length) { lines.push("", T("reply.ask_title") + ":"); asks.forEach(function (q) { lines.push("— " + q); }); }
    if (r.caution) { lines.push("", r.caution); }
    return lines.join("\n");
  }
  // Формулировка и цитата из одних и тех же слов (≥ 85 % слов цитаты есть в формулировке)?
  function sameWords(text, quote) {
    function words(x) { return String(x || "").toLowerCase().split(/[^0-9a-z\u0430-\u044f\u0451]+/i).filter(function (w) { return w.length > 1; }); }
    var t = {}, q = words(quote), hit = 0;
    words(text).forEach(function (w) { t[w] = true; });
    q.forEach(function (w) { if (t[w]) { hit += 1; } });
    return q.length > 0 && hit / q.length >= 0.85;
  }
  // В чате ГК один и тот же вопрос приходит по каждому договору («Договор № 1: Какая программа…?», «Договор № 2: …»).
  // Застрахованный один — такой вопрос показывается один раз, без подписи договора; ответ склеится с исходным
  // вопросом и пойдёт по всем договорам группы. Вопрос только одного договора остаётся с подписью.
  function mergeAsks(list) {
    var out = [], at = {};
    list.forEach(function (q) {
      var m = /^[^:?]{2,40}:\s+(.+)$/.exec(q), core = m ? m[1].trim() : q, key = core.toLowerCase();
      if (at[key] !== undefined) { out[at[key]] = core; return; }
      at[key] = out.length; out.push(q);
    });
    return out;
  }
  // Вопрос из «Что уточнить»: формулировка, пояснение, кнопки готовых ответов и поле «свой ответ».
  function clarifyItem(question, why) {
    var opts = clarifyOptions(question), progs = askProgramOptions(question), listId = null, datalist = null;
    // вопрос о программе: программы договоров чата — кнопками (до PROGRAM_BUTTONS_MAX), больше — подсказками в поле
    if (progs.length && progs.length <= PROGRAM_BUTTONS_MAX) { opts = progs; }
    else if (progs.length) {
      listId = "clar-progs-" + Math.floor(Math.random() * 1e6);
      datalist = el("datalist", { id: listId }, progs.map(function (p) { return el("option", { value: p }); }));
    }
    var input = el("input", { type: "text", class: "clar__input", maxlength: "300", list: listId, placeholder: T(listId ? "reply.clarify_program" : "reply.clarify_own"), "aria-label": question });
    var send = el("button", { type: "submit", class: "btn btn--small btn--primary", text: T("reply.clarify_send") });
    var form = el("form", { class: "clar__own", novalidate: true, on: { submit: function (e) {
      e.preventDefault();
      var v = input.value.trim();
      if (!v) { input.focus(); return; }
      ask(v, question);
    } } }, [input, send]);
    var row = el("div", { class: "clar__row" }, opts.map(function (o) {
      return el("button", { type: "button", class: "reply__opt", disabled: state.busy, text: o, on: { click: function () { if (!state.busy) { ask(o, question); } } } });
    }).concat([form, datalist]));
    return [el("p", { class: "clar__q", text: question }), why ? el("p", { class: "clar__why", text: why }) : null, row];
  }
  // Вопрос о программе (основа из ui.yaml reply.clarify_program_stem) → названия программ договоров чата без повторов.
  function askProgramOptions(question) {
    var stem = T("reply.clarify_program_stem"), ctx = (state.chat && state.chat.context) || {}, names = {}, out = [];
    if (question.toLowerCase().indexOf(stem) < 0) { return []; }
    var contracts = ctx.contract_id ? [state.contractById[ctx.contract_id]] :
      ((state.groups.filter(function (g) { return g.id === ctx.group_id; })[0] || {}).contracts || []);
    contracts.forEach(function (c) {
      ((c && c.programs) || []).forEach(function (p) { var n = typeof p === "string" ? p : p.name; if (n && !names[n]) { names[n] = true; out.push(n); } });
    });
    return out;
  }
  // Ответы менеджера на вопросы «Что уточнить» в этом обращении: реплика «вопрос? — ответ» → {вопрос?: ответ}.
  function answeredClarify(req) {
    var out = {};
    ((req && req.messages) || []).forEach(function (m) {
      var t = m.role === "user" ? String(m.text || "") : "", i = t.indexOf("? \u2014 ");
      if (i > 0) { out[t.slice(0, i + 1)] = t.slice(i + 4); }
    });
    return out;
  }
  // Готовые ответы на вопрос: «А или Б?» — варианты (если вопрос короткий); вопрос «да/нет» (начинается с основы из
  // ui.yaml reply.clarify_yesno) — «Да» и «Нет»; иначе — только свой ответ.
  function clarifyOptions(question) {
    var s = question.replace(/[?\uff1f]\s*$/, "").trim(), low = s.toLowerCase(), or = T("reply.clarify_or");
    if (low.indexOf(or) > 0 && s.split(/\s+/).length <= 8) {
      return s.split(new RegExp(or, "i")).map(function (x) { x = x.trim(); return x.charAt(0).toUpperCase() + x.slice(1); }).filter(Boolean).slice(0, 4);
    }
    var yesno = D.cfg("reply.clarify_yesno") || [];
    if (yesno.some(function (st) { return low.indexOf(st) === 0; })) { return [T("reply.yes"), T("reply.no")]; }
    return [];
  }
  function useQuestion(q) {
    var t = $("question");
    if (t.disabled) { return; }
    t.value = String(q || "").trim(); autoGrow(); countInput(); t.focus(); t.setSelectionRange(t.value.length, t.value.length);
  }

  // ---------- блоки подробностей (прежняя карточка ответа) ----------
  // opts.reply — блоки под «Подробнее» ответа словами: без вопросов (они в «Что уточнить») и без выбора клиники.
  function detailBlocks(a, sources, opts) {
    opts = opts || {};
    var out = [];
    if (opts.reply) {
      var tech = [D.modeChip(a.mode), a.scenario ? el("span", { class: "answer__scn", text: T("scenarios." + a.scenario) }) : null].filter(Boolean);
      if (a.summary) { out.push(card(T("answer.summary"), [el("p", { text: noFootnotes(a.summary) }), a.label_note ? el("p", { class: "muted", text: a.label_note }) : null])); }
      if (tech.length) { out.push(el("div", { class: "answer__chips" }, tech)); }
    }
    // Применённый контекст. В чате по одному договору он уже в шапке — свёрнут; у группы — раскрыт.
    out.push(el("details", { class: "card card--flat ctxfold", open: !opts.reply && !(a.context || {}).contract_id }, [
      el("summary", null, [el("h3", { text: T("answer.context") }), el("span", { class: "ctxfold__brief", text: contextTitle(a.context) })]),
      contextKv(a.context)]));
    // Для ГК — отдельный блок по каждому договору (ADR 0005: условия не смешиваются).
    if ((a.per_contract || []).length) {
      var grid = el("div", { class: "pc" });
      a.per_contract.forEach(function (pc) {
        grid.appendChild(el("section", { class: "pc__item" }, [
          el("h4", null, [el("span", { text: contractName(pc.contract_id) }), " ", D.labelChip(pc.label)]),
          el("p", { text: cleanText(pc.summary) }),
          (pc.statements || []).length ? statementList(pc.statements, sources) : null
        ]));
      });
      out.push(card(T("answer.per_contract"), [grid]));
    }
    var basis = (a.statements || []).filter(function (s) { return s.kind !== "limitation"; });
    var limits = (a.statements || []).filter(function (s) { return s.kind === "limitation"; });
    if (basis.length) { out.push(card(T("answer.basis"), [statementList(basis, sources)])); }
    if ((a.comparisons || []).length) { out.push(card(T("answer.comparisons"), [statementList(a.comparisons, sources)], "card--cmp")); }
    if ((a.alternatives || []).length) { out.push(alternativesCard(a.alternatives)); }
    var qs = opts.reply ? [] : (a.questions || []);
    if ((a.missing || []).length || qs.length) {
      var kids = [];
      if ((a.missing || []).length) { kids.push(el("ul", { class: "list" }, a.missing.map(function (m) { return el("li", { text: m }); }))); }
      if (qs.length) {
        kids.push(el("ul", { class: "list qs" }, qs.map(function (q) {
          return el("li", null, [q, " ", opts.isLast ? el("button", { type: "button", class: "btn btn--small btn--link", text: T("answer.use_question"),
            on: { click: function () { useQuestion(q); } } }) : null]);
        })));
      }
      out.push(card(T("answer.missing"), kids, "card--q"));
    }
    if (limits.length || (a.limitations || []).length) {
      var lim = limits.length ? statementList(limits, sources) : el("ul", { class: "list" });
      (a.limitations || []).forEach(function (l) { lim.appendChild(el("li", { text: l })); });
      out.push(card(T("answer.limitations"), [lim]));
    }
    // Источники: документ, пункт/страница/строка, короткий фрагмент; клик — фрагмент в шторке.
    var ol = el("ol", { class: "srclist" });
    var shown = allStatements(a).map(function (st) { return norm(st.text); });
    sources.forEach(function (c, i) {
      var q = norm(c.quote);
      var dup = !q || shown.some(function (t) { return t.indexOf(q) >= 0 || (t.length > 40 && q.indexOf(t) >= 0); });
      var b = sourceLink("srclist__btn", c, sources, i, null, [
        el("span", { class: "cite", text: "[" + (i + 1) + "]" }), " ",
        el("span", { class: "srclist__doc", text: docTitle(c) }), citeWhere(c) ? el("span", { class: "source", text: " · " + citeWhere(c) }) : null,
        citeContract(c) ? el("span", { class: "srclist__ctr", text: citeContract(c) }) : null,
        dup ? null : el("span", { class: "srclist__q", text: "«" + shorten(c.quote, 160) + "»" })
      ]);
      ol.appendChild(el("li", null, [b]));
    });
    out.push(card(T("answer.sources"), [sources.length ? ol : el("p", { class: "muted", text: T("answer.no_sources") })]));
    if (a.llm) {
      out.push(el("p", { class: "source answer__meta", text: T("answer.meta", { model: a.llm.model, prompt: a.llm.prompt_version, ms: a.processing_ms || 0 }) }));
    }
    return out;
  }
  // Прежний вид ответа — когда сервер не прислал reply (обратная совместимость, CONTRACTS §12).
  function answerCard(a, msgIdx, isLast) {
    var sources = collectSources(a);
    var art = el("article", { class: "msg msg--bot answer", "aria-label": T("chat.assistant") });
    art.appendChild(el("div", { class: "answer__chips" }, [D.labelChip(a.label), D.modeChip(a.mode),
      a.scenario ? el("span", { class: "answer__scn", text: T("scenarios." + a.scenario) }) : null]));
    art.appendChild(el("div", { class: "headline headline--" + (a.label || "none") }, [
      el("p", { text: noFootnotes(a.summary) }), a.label_note ? el("p", { class: "headline__note", text: a.label_note }) : null]));
    art.appendChild(el("p", { class: "answer__status status--" + a.confidence }, [
      el("span", { class: "answer__k", text: T("answer.status") + ": " }), T("confidence." + a.confidence)]));
    if ((a.clinic_choices || []).length) { art.appendChild(clinicPickCard(a, isLast, msgIdx, false)); }
    detailBlocks(a, sources, { isLast: isLast }).forEach(function (n) { art.appendChild(n); });
    if (isLast && (a.suggested_automation || state.offerOpen) && state.req && !state.req.automation && state.req.status !== "closed") {
      art.appendChild(automationOffer());
    }
    return art;
  }
  function allStatements(a) {
    var out = [];
    (a.per_contract || []).forEach(function (pc) { out = out.concat(pc.statements || []); });
    return out.concat(a.statements || [], a.comparisons || []);
  }
  // Альтернативные клиники — список «название — адрес — вид помощи»; строка перечня — по кнопке «Источник».
  function alternativesCard(alts) {
    var ul = el("ul", { class: "list alts" });
    alts.forEach(function (alt) {
      var c = alt.clinic || {};
      var srcBtn = el("button", { type: "button", class: "btn btn--small btn--link", text: T("clinic_pick.source"),
        "aria-label": T("clinic_pick.source_aria", { name: c.clinic_name || "" }), on: { click: function () { openClinic(c, srcBtn); } } });
      var parts = [[c.city, c.address].filter(Boolean).join(", "), c.care_type].filter(Boolean);
      ul.appendChild(el("li", null, [el("strong", { text: c.clinic_name || "" }), parts.length ? " — " + parts.join(" — ") : "", " ", srcBtn]));
    });
    return card(T("answer.alternatives"), [ul]);
  }

  // ---------- подтверждение клиники менеджером (ADR 0010, CONTRACTS §8.1) ----------
  // Клиника из вопроса не угадывается: guided отдаёт кандидатов из перечня программы договора (clinic_choices),
  // менеджер выбирает строку или «Нет в перечне программы». Выбор — POST /api/requests/{id}/clinic, сервер
  // пересчитывает последний вопрос. Выбор действует только в последнем ответе; в контексте ГК — по каждому договору.
  var CLINIC_PICK_NONE = "none";   // constants.CLINIC_PICK_NONE
  function clinicMeta(row) {
    var care = [row.care_type];
    if (typeof row.franchise_pct === "number") { care.push(T("clinic_pick.franchise", { pct: row.franchise_pct })); }
    else if (row.franchise_pct === "—") { care.push(T("clinic_pick.franchise_na")); }
    if (row.by_referral) { care.push(T("clinic_pick.by_referral")); }
    return { place: [row.city, row.address].filter(Boolean).join(", "), care: care.filter(Boolean).join(" · ") };
  }
  function clinicSourceBtn(row) {
    var b = el("button", { type: "button", class: "btn btn--small btn--link pick__src", text: T("clinic_pick.source"),
      "aria-label": T("clinic_pick.source_aria", { name: row.clinic_name || row.id }), on: { click: function () { openClinic(row, b); } } });
    return b;
  }
  // compact — внутри ответа словами (ADR 0014): вопрос «Какую клинику имел в виду сотрудник?», до 4 кандидатов,
  // «Другая — найти» (поиск раскрывается по кнопке) и «Нет в перечне»; без длинного пояснения.
  var PICK_COMPACT_MAX = 4;
  var POINTS_SHOWN = 3;
  var PROGRAM_BUTTONS_MAX = 6;   // программ на вопрос «Какая программа?» кнопками; больше — подсказками в поле   // пунктов ответа видно сразу; остальные — под «Ещё N»
  function clinicPickCard(a, isLast, msgIdx, compact) {
    var group = !(a.context || {}).contract_id;
    var closed = !state.req || state.req.status === "closed";
    var pending = a.clinic_choices.some(function (ch) { return ch.selected === null || ch.selected === undefined; });
    var hid = compact ? null : "pick-h-" + Date.now() + "-" + Math.floor(Math.random() * 1e6);
    var kids = [];
    a.clinic_choices.forEach(function (ch) { kids.push(clinicChoiceBlock(ch, { group: group, live: isLast && !closed, isLast: isLast, closed: closed, hid: hid, msgIdx: msgIdx, compact: compact })); });
    if (compact) {
      return el("section", { class: "pick pick--compact", "aria-label": T("clinic_pick.title") },
        [pending ? el("h3", { class: "reply__h pick__title", text: T("clinic_pick.question") }) : null].concat(kids));
    }
    // Пояснение мелким текстом — один раз на карточку (кнопки ссылаются на него через aria-describedby).
    if (pending) { kids.push(el("p", { class: "hint", id: hid, text: T("clinic_pick.hint") })); }
    return card(T("clinic_pick.title"), kids, "pick");
  }
  function clinicChoiceBlock(ch, o) {
    var box = el("section", { class: "pick__item", "data-contract": ch.contract_id });
    var head = o.group ? T("clinic_pick.contract", { name: o.compact ? contractShort(ch.contract_id) : contractName(ch.contract_id), program: ch.program || T("common.none") })
                       : o.compact ? "" : T("clinic_pick.program", { name: ch.program || T("common.none") });
    if (head) { box.appendChild(el("h4", { class: "pick__head", text: head })); }
    if (ch.asked) { box.appendChild(el("p", { class: "pick__asked", text: T("clinic_pick.asked", { name: ch.asked }) })); }
    var rows = ch.candidates || [];
    // Выбор уже учтён (selected не null) — показываем его без кнопок.
    if (ch.selected !== null && ch.selected !== undefined) {
      if (ch.selected === CLINIC_PICK_NONE) {
        box.appendChild(el("p", { class: "pick__done" }, [D.chip(T("clinic_pick.chip_none"), "chip--warn chip--bang"), " ", T("clinic_pick.selected_none")]));
        return box;
      }
      // Строка могла быть найдена поиском по перечню (круг w7) — её нет среди кандидатов, берём из кэша поиска.
      var row = rows.filter(function (r) { return r.id === ch.selected; })[0] || state.clinicRows[ch.selected];
      if (!row) { box.appendChild(el("p", { class: "pick__done" }, [D.chip(T("clinic_pick.chip_selected"), "chip--ok"), " ", T("clinic_pick.selected_unknown")])); return box; }
      var mt = clinicMeta(row);
      box.appendChild(el("div", { class: "pick__done" }, [D.chip(T("clinic_pick.chip_selected"), "chip--ok"),
        el("span", { class: "pick__txt" }, [el("strong", { text: row.clinic_name || "" }), mt.place ? el("span", { class: "pick__meta", text: mt.place }) : null,
          mt.care ? el("span", { class: "pick__meta", text: mt.care }) : null]), clinicSourceBtn(row)]));
      return box;
    }
    var off = !o.live || state.busy;
    if (!rows.length) { box.appendChild(el("p", { class: "muted pick__empty", text: T("clinic_pick.empty") })); }
    if (o.compact) { rows = rows.slice(0, PICK_COMPACT_MAX); }
    var ul = el("ul", { class: "pick__list" });
    rows.forEach(function (r) { ul.appendChild(clinicOptRow(ch, r, box, off, o.hid)); });
    if (rows.length) { box.appendChild(ul); }
    // Поиск по всему перечню программы — только в активном блоке (последний ответ, запрос открыт).
    // В компактном виде он раскрывается кнопкой «Другая — найти»; раскрытость хранится в состоянии поиска.
    var find = o.live ? clinicFindBlock(ch, box, o) : null, other = null;
    if (find && o.compact) {
      var fst = clinicFindState(ch, o);
      find.hidden = !fst.open;
      other = el("button", { type: "button", class: "btn btn--small pick__other", disabled: off, "aria-expanded": fst.open ? "true" : "false",
        text: T(fst.open ? "clinic_pick.other_hide" : "clinic_pick.other"), on: { click: function () {
          fst.open = !fst.open; find.hidden = !fst.open;
          other.setAttribute("aria-expanded", fst.open ? "true" : "false");
          other.textContent = T(fst.open ? "clinic_pick.other_hide" : "clinic_pick.other");
          if (fst.open) { var inp = find.querySelector("input"); if (inp) { inp.focus(); } }
        } } });
    }
    if (find && !o.compact) { box.appendChild(find); }
    box.appendChild(el("div", { class: "pick__actions" }, [other, el("button", { type: "button", class: "btn btn--small pick__none", disabled: off, "aria-describedby": o.hid,
      text: T("clinic_pick.none"), on: { click: function () { pickClinic(ch.contract_id, CLINIC_PICK_NONE, box); } } })]));
    if (find && o.compact) { box.appendChild(find); }
    if (!o.isLast) { box.appendChild(el("p", { class: "hint", text: T("clinic_pick.stale") })); }
    else if (o.closed) { box.appendChild(el("p", { class: "hint", text: T("clinic_pick.closed") })); }
    return box;
  }
  // Строка перечня с кнопкой выбора и «Источник» — одинаково для кандидатов и для результатов поиска.
  function clinicOptRow(ch, r, box, off, hid) {
    var m = clinicMeta(r);
    return el("li", { class: "pick__row" }, [
      el("button", { type: "button", class: "pick__opt", disabled: off, "aria-describedby": hid, "aria-label": T("clinic_pick.pick_aria", { name: [r.clinic_name, m.place, m.care].filter(Boolean).join(", ") }),
        on: { click: function () { pickClinic(ch.contract_id, r.id, box); } } }, [
        el("span", { class: "pick__name", text: r.clinic_name || "" }),
        m.place ? el("span", { class: "pick__meta", text: m.place }) : null,
        m.care ? el("span", { class: "pick__meta", text: m.care }) : null
      ]),
      clinicSourceBtn(r)
    ]);
  }

  // ---------- поиск по перечню программы (круг w7, CONTRACTS §8: GET …/clinic-candidates) ----------
  // У сетей бывает 40+ адресов, а кандидатов — не больше CLINIC_CHOICE_MAX. Менеджер ищет строку по названию,
  // улице или городу; выбранная строка уходит тем же POST …/clinic. Состояние поиска — в state.clinicFind
  // по ключу «запрос | ответ | договор», чтобы перерисовка ленты (вкладки черновиков и т. п.) его не теряла.
  // Запросы: по Enter / «Найти» сразу, при вводе — с задержкой CLINIC_FIND_DELAY; ответ устаревшего запроса
  // отбрасывается по номеру seq (побеждает последний). Страница — CLINIC_SEARCH_PAGE строк (её задаёт сервер).
  var CLINIC_FIND_DELAY = 300;   // мс задержки ввода
  var CLINIC_FIND_MIN = 2;       // символов, с которых поиск запускается при вводе
  function clinicFindState(ch, o) {
    var key = [state.req ? state.req.id : "", o.msgIdx, ch.contract_id].join("|");
    var st = state.clinicFind[key];
    if (!st) { st = state.clinicFind[key] = { q: "", loaded: null, rows: [], total: 0, page: 0, seq: 0, timer: null, loading: false, error: "", open: false }; }
    return st;
  }
  function clinicFindBlock(ch, box, o) {
    var st = clinicFindState(ch, o);
    var fid = "pick-find-" + Math.floor(Math.random() * 1e9);
    var input = el("input", { type: "search", id: fid, maxlength: "200", autocomplete: "off", value: st.q, disabled: state.busy,
      class: "pick__q", "aria-describedby": fid + "-n" });
    var go = el("button", { type: "submit", class: "btn btn--small pick__go", disabled: state.busy, text: T("clinic_pick.find_btn") });
    var all = el("button", { type: "button", class: "btn btn--small btn--link pick__all", disabled: state.busy, text: T("clinic_pick.find_all") });
    var count = el("p", { class: "pick__count", id: fid + "-n", "aria-live": "polite" });
    var list = el("ul", { class: "pick__list pick__found", "aria-label": T("clinic_pick.find_results") });
    var more = el("button", { type: "button", class: "btn btn--small pick__more", hidden: true });
    var form = el("form", { class: "pick__find", role: "search", "aria-label": T("clinic_pick.find_region"), novalidate: true }, [
      el("label", { for: fid, text: T("clinic_pick.find_label") }),
      el("div", { class: "pick__findrow" }, [input, go, all]),
      count, list, more
    ]);

    // Отрисовка результатов из st; focusFrom — индекс первой новой строки (фокус после «Показать ещё»).
    function paint(focusFrom) {
      clear(list); count.classList.remove("pick__count--err");
      st.rows.forEach(function (r) { list.appendChild(clinicOptRow(ch, r, box, state.busy, o.hid)); });
      list.hidden = !st.rows.length;
      var left = st.total - st.rows.length;
      more.hidden = st.loaded === null || left <= 0;
      more.disabled = st.loading || state.busy;
      more.textContent = T("clinic_pick.find_more", { n: Math.min(left, st.page || left) });
      if (st.error) { count.textContent = st.error; count.classList.add("pick__count--err"); }
      else if (st.loading) { count.textContent = T("clinic_pick.find_loading"); }
      else if (st.loaded === null) { count.textContent = ""; }
      else if (!st.total) { count.textContent = st.loaded ? T("clinic_pick.find_none", { q: st.loaded }) : T("clinic_pick.find_none_all"); }
      else { count.textContent = T("clinic_pick.find_count", { shown: st.rows.length, total: st.total }); }
      if (typeof focusFrom === "number") {
        var btn = list.querySelectorAll(".pick__opt")[focusFrom];
        if (btn) { btn.focus(); }
      }
    }
    // Запрос страницы: reset — новый поиск с offset 0, иначе следующая страница того же запроса.
    function run(q, reset) {
      clearTimeout(st.timer); st.timer = null;
      if (!state.req || state.busy) { return; }
      if (reset && st.loaded === q && !st.error && !st.loading) { paint(); return; }   // этот запрос уже показан — без лишнего запроса
      var reqId = state.req.id, offset = reset ? 0 : st.rows.length;
      var my = ++st.seq;
      st.loading = true; st.error = "";
      if (reset) { st.rows = []; st.total = 0; st.loaded = null; }
      paint();
      var url = "/api/requests/" + encodeURIComponent(reqId) + "/clinic-candidates?contract_id=" + encodeURIComponent(ch.contract_id) +
        "&q=" + encodeURIComponent(q) + "&offset=" + offset;
      D.request("GET", url).then(function (r) {
        if (my !== st.seq) { return; }                               // ответ устаревшего запроса — отбрасываем
        st.loading = false;
        if (!state.req || state.req.id !== reqId) { return; }        // менеджер ушёл в другой запрос
        if (!r.ok) {
          st.error = clinicFindError(r);
          // 409 — выбор уже не в последнем ответе: перечитать запрос и показать ошибку над лентой.
          if (r.status === 409) { var msg = st.error; return loadRequests().then(function () { renderRequest(); showAlert(msg); }); }
          paint(); return;
        }
        var d = r.data || {}, got = Array.isArray(d.rows) ? d.rows : [];
        got.forEach(function (row) { if (row && row.id) { state.clinicRows[row.id] = row; } });
        var from = st.rows.length;
        st.rows = reset ? got : st.rows.concat(got);
        st.total = typeof d.total === "number" ? d.total : st.rows.length;
        st.page = st.page || got.length;
        st.loaded = q;
        // Фокус — только после «Показать ещё» (на первой новой строке); при новом поиске остаётся в поле.
        paint(reset ? undefined : from);
      });
    }
    input.addEventListener("input", function () {
      st.q = input.value;
      var q = input.value.trim();
      clearTimeout(st.timer); st.timer = null;
      if (q.length < CLINIC_FIND_MIN) {
        // Пустой запрос: результаты прошлого поиска убираем, ответ запроса в полёте забываем.
        if (!q && (st.loaded !== null || st.loading)) { st.seq++; st.loading = false; st.rows = []; st.total = 0; st.loaded = null; st.error = ""; paint(); }
        return;
      }
      st.timer = setTimeout(function () { run(q, true); }, CLINIC_FIND_DELAY);
    });
    input.addEventListener("keydown", function (e) {
      // Escape в непустом поле — очистить поиск (не закрывает панель источников).
      if (e.key === "Escape" && input.value) { e.preventDefault(); e.stopPropagation(); input.value = ""; input.dispatchEvent(new Event("input")); }
    });
    // Enter / «Найти»: сразу, без задержки; пустой запрос — первая страница всего перечня.
    form.addEventListener("submit", function (e) { e.preventDefault(); run(input.value.trim(), true); });
    all.addEventListener("click", function () { input.value = ""; st.q = ""; run("", true); });
    more.addEventListener("click", function () { if (st.loaded !== null) { run(st.loaded, false); } });
    paint();
    return form;
  }
  // Ошибки поиска — по коду ответа (CONTRACTS §8: 409 no_clinic_choice, 422 contract_not_in_context).
  function clinicFindError(r) {
    var code = r && r.data && r.data.error ? r.data.error.code : null;
    if (code === "no_clinic_choice" || (!code && r.status === 409)) { return T("clinic_pick.find_err_no_choice"); }
    if (code === "contract_not_in_context" || (!code && r.status === 422)) { return T("clinic_pick.find_err_contract"); }
    return T("clinic_pick.find_err", { reason: D.errorText(r) });
  }
  // Ошибки выбора — по-русски из ui.yaml по коду ответа (CONTRACTS §8: 409 no_clinic_choice, 422 clinic_row_unknown).
  function clinicError(r) {
    var code = r && r.data && r.data.error ? r.data.error.code : null;
    if (code === "no_clinic_choice" || (!code && r.status === 409)) { return T("errors.clinic_no_choice"); }
    if (code === "clinic_row_unknown" || (!code && r.status === 422)) { return T("errors.clinic_row_unknown"); }
    return D.errorText(r);
  }
  function setPickBusy(box, on) {
    var cardNode = box.closest(".pick") || box;
    Array.prototype.forEach.call(cardNode.querySelectorAll(".pick__opt, .pick__none, .pick__q, .pick__go, .pick__all, .pick__more"), function (b) { b.disabled = on; });
    var old = cardNode.querySelector(".pick__busy"); if (old) { old.parentNode.removeChild(old); }
    cardNode.setAttribute("aria-busy", on ? "true" : "false");
    if (on) { box.appendChild(el("p", { class: "pick__busy", role: "status", "aria-live": "polite" }, [el("span", { class: "spinner", "aria-hidden": "true" }), T("clinic_pick.busy")])); }
  }
  // Фокус — на новом ответе (последняя карточка ленты), как после вопроса; карточке ставится tabindex="-1".
  function focusLastAnswer() {
    var list = $("messages").querySelectorAll(".answer"), last = list[list.length - 1];
    if (!last) { return; }
    last.setAttribute("tabindex", "-1");
    last.focus({ preventScroll: true });
    last.scrollIntoView({ block: "start" });
  }
  function pickClinic(contractId, choice, box) {
    if (!state.req || state.busy) { return; }
    var reqId = state.req.id;
    state.busy = true; setAskEnabled(false); showAlert("");
    setPickBusy(box, true);
    D.request("POST", "/api/requests/" + encodeURIComponent(reqId) + "/clinic", { contract_id: contractId, choice: choice }).then(function (r) {
      state.busy = false;
      if (!state.req || state.req.id !== reqId) { setAskEnabled(true); return; }   // менеджер ушёл в другой запрос
      if (!r.ok) {
        var msg = clinicError(r);
        // 409 — ответ устарел (выбор уже не в последнем ответе): перечитать запрос, затем показать ошибку.
        if (r.status === 409) { return loadRequests().then(function () { renderRequest(); showAlert(msg); }); }
        setPickBusy(box, false); setAskEnabled(state.req.status !== "closed");
        showAlert(msg);
        return;
      }
      state.sources = []; state.sourceSel = null;
      return loadRequests().then(function () { renderRequest(); focusLastAnswer(); });
    });
  }

  // ---------- шторка «Источники» (по сноске [n]; по умолчанию скрыта) ----------
  // trigger — кнопка, открывшая источник: на неё возвращается фокус при закрытии панели (Escape, «Скрыть», подложка).
  function openSource(sources, idx, trigger) {
    state.sources = sources; state.sourceSel = idx; state.lastCite = trigger || null;
    renderSources();
    openDrawer();
  }
  function openClinic(row, trigger) {
    state.sources = [{ doc_id: row.doc_id, doc_title: state.docTitles[row.doc_id] || row.program || "", contract_id: row.contract_id, table_ref: row.source_ref,
                       quote: row.clinic_name, verified: true, _clinic: row }];
    state.sourceSel = 0; state.lastCite = trigger || null;
    renderSources(); openDrawer();
  }
  // Шторка выезжает справа на любой ширине; фокус — в шторку, при закрытии — обратно на сноску.
  function openDrawer() {
    hidePreview(false);
    var s = $("sources");
    s.classList.add("is-open");
    $("sources-backdrop").classList.add("is-open");
    s.focus();
  }
  function closeDrawer() {
    var s = $("sources"), wasOpen = s.classList.contains("is-open");
    s.classList.remove("is-open");
    $("sources-backdrop").classList.remove("is-open");
    var back = state.lastCite;
    if (wasOpen && back && document.body.contains(back)) { back.focus(); }
  }
  // Левая колонка на узком экране (≤ 900 px) — выезжает слева по кнопке «Чаты».
  function openSide() { $("side").classList.add("is-open"); $("side-backdrop").classList.add("is-open"); $("side-toggle").setAttribute("aria-expanded", "true"); $("side").focus(); }
  function closeSide() {
    var s = $("side"), was = s.classList.contains("is-open");
    s.classList.remove("is-open"); $("side-backdrop").classList.remove("is-open"); $("side-toggle").setAttribute("aria-expanded", "false");
    return was;
  }
  function renderSources() {
    var body = $("sources-body");
    clear(body);
    if (!state.sources.length) { body.appendChild(el("p", { class: "muted", text: T("sources.empty") })); return; }
    var ol = el("ol", { class: "srcpanel" });
    state.sources.forEach(function (c, i) {
      var sel = i === state.sourceSel;
      var li = el("li", { class: "srcpanel__item" + (sel ? " is-sel" : "") }, [
        el("button", { type: "button", class: "srcpanel__head", "aria-expanded": sel ? "true" : "false", on: { click: function () { state.sourceSel = sel ? null : i; renderSources(); } } }, [
          el("span", { class: "cite", text: "[" + (i + 1) + "]" }), " ", el("span", { text: docTitle(c) }),
          citeWhere(c) ? el("span", { class: "source", text: " · " + citeWhere(c) }) : null,
          citeContract(c) ? el("span", { class: "srcpanel__ctr", text: citeContract(c) }) : null
        ])
      ]);
      // Выбранный источник — полный текст пункта с подсветкой; остальные — короткий фрагмент цитаты.
      if (sel) { li.appendChild(fragment(c)); }
      else if (c.quote) { li.appendChild(el("p", { class: "srcpanel__q", text: "«" + shorten(c.quote, 140) + "»" })); }
      ol.appendChild(li);
    });
    body.appendChild(ol);
  }
  // Где в тексте пункта цитата: точное совпадение, затем — без учёта регистра и пробелов, затем offsets.
  function findQuote(text, quote, offsets) {
    if (quote) {
      var i = text.indexOf(quote);
      if (i >= 0) { return [i, i + quote.length]; }
      var words = quote.trim().split(/\s+/).map(function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); });
      var m = words.length ? new RegExp(words.join("\\s+"), "i").exec(text) : null;
      if (m) { return [m.index, m.index + m[0].length]; }
    }
    if (offsets && offsets.length === 2 && offsets[1] <= text.length) { return [offsets[0], offsets[1]]; }
    return null;
  }
  // Подсветка цитаты в тексте пункта (mark); не нашлась — текст без подсветки.
  function highlight(text, quote, offsets) {
    var box = el("div", { class: "quote" });
    var r = findQuote(text, quote, offsets);
    if (!r) { box.appendChild(document.createTextNode(text)); return box; }
    box.appendChild(document.createTextNode(text.slice(0, r[0])));
    box.appendChild(el("mark", { text: text.slice(r[0], r[1]) }));
    box.appendChild(document.createTextNode(text.slice(r[1])));
    return box;
  }
  // Текст пункта для показа и смещения цитаты в нём. Маркер «- » в начале убирается и в тексте, и в смещениях
  // (offsets сдвигаются на его длину). Сноски «[^N]» убираются из текста — смещения после этого неверны,
  // тогда подсветка только по цитате.
  function clauseText(cl, c) {
    var raw = cl.text || "", bare = noFootnotes(raw), txt = cleanText(raw), cut = bare.length - txt.length;
    var offs = bare === raw && c.offsets && c.offsets[0] >= cut ? [c.offsets[0] - cut, c.offsets[1] - cut] : null;
    return { text: txt, offsets: offs };
  }
  // Запрос полного текста пункта (GET /api/documents/{id}/clause). Пункт ищется по ссылке цитаты (c.ref — id пункта
  // или строки таблицы): номер пункта неоднозначен — в документе бывает несколько «2.1» (ревизия w16: панель
  // открывала чужой пункт). Нет ссылки — по номеру; не нашлось по ссылке — повтор по номеру.
  // chat_id: сервер проверит, что документ из договора(ов) контекста чата (ADR 0005).
  var clausePending = {};
  function loadClause(c) {
    if (c._clinic) { return Promise.resolve({ clinic: c._clinic }); }
    var byNumber = c.clause_number ? "number=" + encodeURIComponent(c.clause_number) : "";
    var refId = c.ref || c.table_ref || "";
    var qs = refId ? "ref=" + encodeURIComponent(refId) : byNumber;
    if (!c.doc_id || !qs) { return Promise.resolve(null); }
    var chatId = state.req ? state.req.chat_id : state.chat ? state.chat.id : null;
    var chatQs = chatId ? "&chat_id=" + encodeURIComponent(chatId) : "";
    var key = c.doc_id + "?" + qs + chatQs;
    if (state.clauseCache[key]) { return Promise.resolve(state.clauseCache[key]); }
    if (clausePending[key]) { return clausePending[key]; }
    var base = "/api/documents/" + encodeURIComponent(c.doc_id) + "/clause?";
    var p = D.request("GET", base + qs + chatQs).then(function (r) {
      if (r.ok || !refId || !byNumber || r.status !== 404) { return r; }
      return D.request("GET", base + byNumber + chatQs);
    }).then(function (r) {
      delete clausePending[key];
      if (r.ok && r.data) { state.clauseCache[key] = r.data; }
      return r.ok ? r.data : null;
    });
    clausePending[key] = p;
    return p;
  }
  function fragment(c) {
    var wrap = el("div", { class: "srcpanel__body" }, [el("p", { class: "muted", text: T("sources.loading") })]);
    if (c.verified === false) { wrap.appendChild(D.chip(T("sources.not_verified"), "chip--warn chip--bang")); }
    var done = function (data) {
      clear(wrap);
      if (data && data.clause) {
        var cl = data.clause, ct = clauseText(cl, c);
        wrap.appendChild(el("p", { class: "source", text: [cl.path, cl.number ? T("answer.clause", { n: cl.number }) : "", cl.page ? T("answer.page", { n: cl.page }) : ""].filter(Boolean).join(" · ") }));
        wrap.appendChild(highlight(ct.text, cleanText(c.quote), ct.offsets));
      } else if (data && data.clinic) {
        wrap.appendChild(clinicRow(data.clinic));
      } else {
        wrap.appendChild(el("p", { class: "muted", text: T("sources.error") }));
        wrap.appendChild(el("p", { class: "source", text: T("sources.quote_only") }));
        wrap.appendChild(el("div", { class: "quote" }, [el("mark", { text: cleanText(c.quote) })]));
      }
      if (c.doc_id && !isAttachmentId(c.doc_id)) {
        var hash = "#doc=" + encodeURIComponent(c.doc_id) + (c.clause_number ? "&clause=" + encodeURIComponent(c.clause_number) : "") +
          (c.ref ? "&ref=" + encodeURIComponent(c.ref) : "");
        wrap.appendChild(el("p", { class: "srcpanel__open" }, [el("a", { class: "btn btn--small", href: D.link("/documents" + hash), text: T("sources.open_doc") })]));
      }
    };
    loadClause(c).then(done);
    return wrap;
  }
  function clinicRow(row) {
    var dl = el("dl", { class: "kv quote" });
    [["answer.alt_clinic", row.clinic_name], ["answer.alt_address", [row.city, row.address].filter(Boolean).join(", ")],
     ["answer.alt_care", [row.program, row.care_type].filter(Boolean).join(" · ")], ["answer.alt_source", humanRef(row.source_ref)]].forEach(function (p) {
      if (!p[1]) { return; }
      dl.appendChild(el("dt", { text: T(p[0]) }));
      dl.appendChild(el("dd", null, [p[0] === "answer.alt_clinic" ? el("mark", { text: p[1] }) : p[1]]));
    });
    if (row.access_note) { dl.appendChild(el("dt", { text: T("answer.alt_reason") })); dl.appendChild(el("dd", { text: row.access_note })); }
    return el("div", null, [el("p", { class: "source", text: T("sources.clinic_row") }), dl]);
  }

  // ---------- превью источника у ссылки (этап w17-preview) ----------
  // Ссылка «Источник: [n] документ, п. …» в ответе, сноска [n] и строка списка «Источники» — гиперссылки: клик
  // открывает шторку с фрагментом (как раньше), наведение мышью или фокус с клавиатуры — всплывающее превью:
  // документ, пункт/страница, договор, проверенная цитата (Citation.quote) и, если загрузится, кусок полного текста
  // пункта вокруг цитаты с подсветкой (loadClause: тот же API и кэш, что у шторки). Запрос — только когда превью
  // показано (после PREVIEW_DELAY), не на каждое движение мыши. Ошибка загрузки — остаётся только цитата.
  // На тач-экране первый тап показывает превью с кнопкой «Открыть источник», второй тап по ссылке — шторку.
  // WCAG 1.4.13: превью закрывается по Esc, не исчезает, пока на нём мышь, не перекрывает ссылку.
  var PREVIEW_DELAY = 250, PREVIEW_HIDE_DELAY = 200, PREVIEW_RADIUS = 220, PREVIEW_GAP = 6, PREVIEW_MARGIN = 8;
  var pv = { node: null, trigger: null, showTimer: null, hideTimer: null, tap: false, token: 0, pointer: null };
  function previewNode() {
    if (pv.node) { return pv.node; }
    pv.node = el("div", { id: "src-preview", class: "srcprev", hidden: true });
    pv.node.addEventListener("pointerenter", function () { clearTimeout(pv.hideTimer); });
    pv.node.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse" && !pv.tap) { schedulePreviewHide(); } });
    pv.node.addEventListener("focusout", function (e) {
      if (!pv.node.contains(e.relatedTarget) && e.relatedTarget !== pv.trigger) { schedulePreviewHide(); }
    });
    document.body.appendChild(pv.node);
    return pv.node;
  }
  function previewOpen() { return !!(pv.node && !pv.node.hidden); }
  // Ссылка на источник: кнопка (действие — открыть шторку), оформлена как гиперссылка; превью по наведению/фокусу.
  function sourceLink(cls, c, sources, idx, attrs, children) {
    var a = attrs || {};
    a.type = "button"; a.class = cls;
    a.on = { click: function (e) { sourceClick(e, b, c, sources, idx); } };
    var b = el("button", a, children);
    b.addEventListener("pointerdown", function (e) { pv.pointer = e.pointerType; });
    b.addEventListener("pointerenter", function (e) { if (e.pointerType === "mouse") { schedulePreview(b, c, sources, idx); } });
    b.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse" && pv.trigger === b && !pv.tap) { schedulePreviewHide(); } else if (e.pointerType === "mouse") { clearTimeout(pv.showTimer); } });
    b.addEventListener("focus", function () {
      // Только фокус с клавиатуры: при клике мышью фокус тоже приходит, но превью уже показано наведением.
      var kb = false;
      try { kb = b.matches(":focus-visible"); } catch (err) { kb = false; }
      if (kb) { schedulePreview(b, c, sources, idx); }
    });
    b.addEventListener("blur", function (e) {
      if (pv.trigger === b && !(pv.node && pv.node.contains(e.relatedTarget))) { schedulePreviewHide(); }
    });
    return b;
  }
  function sourceClick(e, b, c, sources, idx) {
    var touch = pv.pointer === "touch" || pv.pointer === "pen";
    pv.pointer = null;
    // Тап: сначала превью; повторный тап по той же ссылке (или «Открыть источник») — шторка.
    if (touch && !(previewOpen() && pv.trigger === b)) { e.preventDefault(); showPreview(b, c, sources, idx, true); return; }
    hidePreview(false);
    openSource(sources, idx, b);
  }
  function schedulePreview(b, c, sources, idx) {
    clearTimeout(pv.hideTimer); clearTimeout(pv.showTimer);
    if (previewOpen() && pv.trigger === b) { return; }
    pv.showTimer = setTimeout(function () { showPreview(b, c, sources, idx, false); }, PREVIEW_DELAY);
  }
  function schedulePreviewHide() {
    clearTimeout(pv.showTimer); clearTimeout(pv.hideTimer);
    pv.hideTimer = setTimeout(function () { hidePreview(false); }, PREVIEW_HIDE_DELAY);
  }
  // refocus — вернуть фокус на ссылку (Esc, «Закрыть»), иначе — только если фокус был внутри превью.
  function hidePreview(refocus) {
    clearTimeout(pv.showTimer); clearTimeout(pv.hideTimer);
    pv.token++;
    if (!previewOpen()) { pv.trigger = null; return; }
    var t = pv.trigger, inside = pv.node.contains(document.activeElement);
    pv.node.hidden = true;
    clear(pv.node);
    if (t) { t.removeAttribute("aria-describedby"); t.removeAttribute("aria-expanded"); }
    pv.trigger = null; pv.tap = false;
    if (t && (refocus || inside) && document.body.contains(t)) { t.focus(); }
  }
  // Кусок текста пункта вокруг цитаты (±PREVIEW_RADIUS символов по границе слова), цитата — mark.
  // Цитата в тексте не нашлась — null (тогда остаётся одна цитата).
  function excerpt(text, quote, offsets) {
    var r = findQuote(text, quote, offsets);
    if (!r) { return null; }
    var s = Math.max(0, r[0] - PREVIEW_RADIUS), e = Math.min(text.length, r[1] + PREVIEW_RADIUS);
    if (s > 0) { var sp = text.indexOf(" ", s); if (sp >= 0 && sp < r[0]) { s = sp + 1; } }
    if (e < text.length) { var sp2 = text.lastIndexOf(" ", e); if (sp2 > r[1]) { e = sp2; } }
    return el("div", { class: "quote srcprev__ctx" }, [
      (s > 0 ? "…" : "") + text.slice(s, r[0]), el("mark", { text: text.slice(r[0], r[1]) }), text.slice(r[1], e) + (e < text.length ? "…" : "")
    ]);
  }
  function showPreview(b, c, sources, idx, tap) {
    if (!document.body.contains(b)) { return; }
    hidePreview(false);
    var node = previewNode(), tok = ++pv.token, where = citeWhere(c);
    pv.trigger = b; pv.tap = tap;
    // Наведение/фокус — подсказка (role="tooltip", у ссылки aria-describedby); тап — немодальный диалог с кнопками.
    node.setAttribute("role", tap ? "dialog" : "tooltip");
    if (tap) { node.setAttribute("aria-label", T("preview.label")); } else { node.removeAttribute("aria-label"); }
    var ctr = c.contract_id ? T("answer.source_contract", { name: contractName(c.contract_id) }) : "";
    var quoteBox = el("blockquote", { class: "srcprev__quote" }, ["«" + shorten(c.quote, 400) + "»"]);
    var ctxBox = el("div", { class: "srcprev__ctxbox" }, [el("p", { class: "srcprev__loading", text: T("preview.loading") })]);
    node.appendChild(el("p", { class: "srcprev__doc" }, [el("span", { class: "cite", text: "[" + (idx + 1) + "]" }), " ", docTitle(c)]));
    var meta = [where, ctr].filter(Boolean).join(" · ");
    if (meta) { node.appendChild(el("p", { class: "srcprev__meta", text: meta })); }
    node.appendChild(el("p", { class: "srcprev__status" }, [c.verified === false
      ? D.chip(T("sources.not_verified"), "chip--warn chip--bang") : D.chip(T("preview.verified"), "chip--ok")]));
    node.appendChild(quoteBox);
    node.appendChild(ctxBox);
    if (tap) {
      node.appendChild(el("p", { class: "srcprev__actions" }, [
        el("button", { type: "button", class: "btn btn--small btn--primary", text: T("preview.open"), on: { click: function () { hidePreview(false); openSource(sources, idx, b); } } }),
        el("button", { type: "button", class: "btn btn--small", text: T("preview.close"), on: { click: function () { hidePreview(true); } } })
      ]));
      b.setAttribute("aria-expanded", "true");
    } else {
      node.appendChild(el("p", { class: "srcprev__hint", text: T("preview.hint") }));
      b.setAttribute("aria-describedby", "src-preview");
    }
    node.hidden = false;
    placePreview();
    // Полный текст пункта — лениво: тот же кэш, что у шторки; превью закрылось или сменилось — результат не показываем.
    loadClause(c).then(function (data) {
      if (tok !== pv.token) { return; }
      clear(ctxBox);
      var ex = null;
      if (data && data.clause) {
        var ct = clauseText(data.clause, c);
        ex = excerpt(ct.text, cleanText(c.quote), ct.offsets);
      }
      if (ex) {
        // Цитата нашлась в тексте пункта — показываем её в контексте (подсветка), отдельный блок цитаты не нужен.
        quoteBox.hidden = true;
        ctxBox.appendChild(el("p", { class: "srcprev__k", text: T("preview.context") }));
        ctxBox.appendChild(ex);
      }
      placePreview();
    });
  }
  // Позиция: под ссылкой, по её левому краю; не влезает вниз — над ссылкой; не влезает вправо — по правому краю ссылки.
  function placePreview() {
    if (!previewOpen() || !pv.trigger) { return; }
    var node = pv.node, r = pv.trigger.getBoundingClientRect();
    var vw = document.documentElement.clientWidth, vh = window.innerHeight, M = PREVIEW_MARGIN, G = PREVIEW_GAP;
    node.style.maxHeight = "none";
    var w = node.offsetWidth, h = node.offsetHeight;
    var below = vh - r.bottom - G - M, above = r.top - G - M;
    var up = h > below && above > below;
    var room = Math.max(120, up ? above : below);
    node.style.maxHeight = room + "px";
    h = Math.min(h, room);
    node.style.top = Math.round(up ? r.top - G - h : r.bottom + G) + "px";
    var left = r.left;
    if (left + w > vw - M) { left = r.right - w; }
    node.style.left = Math.round(Math.max(M, Math.min(left, vw - M - w))) + "px";
    node.classList.toggle("is-up", up);
  }

  // ---------- автоматизация: добавление клиники (ТЗ п. 5.2.2, ADR 0004) ----------
  function automationOffer() {
    var id = "auto-clinic-" + Date.now();
    var input = el("input", { type: "text", id: id, maxlength: "200", value: (state.req.slots || {}).clinic || "" });
    var btn = el("button", { type: "button", class: "btn btn--primary", text: T("answer.automation_start"), on: { click: function () {
      btn.disabled = true;
      D.request("POST", "/api/requests/" + encodeURIComponent(state.req.id) + "/automation/add-clinic", { clinic: input.value.trim() || null }).then(function (r) {
        btn.disabled = false;
        if (!r.ok) { showAlert(D.errorText(r)); return; }
        state.offerOpen = false;
        replaceRequest(r.data);
        focusAfter([".auto .auto__actions .btn--primary", ".auto .auto__actions .btn", ".auto .auto__head .chip"]);
      });
    } } });
    return el("div", { class: "offer" }, [el("p", { text: T("answer.automation_offer") }),
      el("div", { class: "offer__row" }, [el("div", { class: "f" }, [el("label", { for: id, text: T("answer.automation_clinic") }), input]), btn])]);
  }
  // Пройденные состояния — из history (любое значение, совпадающее с именем состояния) и текущего state.
  function visitedStates(auto) {
    var names = Object.keys(D.cfg("automation.states") || {}), seen = {};
    (auto.history || []).forEach(function (h) {
      Object.keys(h || {}).forEach(function (k) { if (typeof h[k] === "string" && names.indexOf(h[k]) >= 0) { seen[h[k]] = true; } });
    });
    seen[auto.state] = true;
    return seen;
  }
  function stepPath(auto) {
    var s = auto.state, seen = visitedStates(auto);
    if (s === "alt_found" || s === "insufficient" || (s === "closed" && (seen.alt_found || seen.insufficient) && !seen.not_in_program)) {
      return [seen.alt_found ? "alt_found" : "insufficient"].concat(s === "closed" ? ["closed"] : []);
    }
    var path = ["not_in_program", "hr_wants_add", "awaiting_insurer"];
    if (seen.insurer_free) { path.push("insurer_free"); }
    else if (seen.insurer_paid) {
      // «платно»: согласие HR → подтверждение СК (только оно даёт «добавлена», ADR 0004); отказ HR — конец ветки
      path.push("insurer_paid");
      if (seen.hr_declined) { path.push("hr_declined"); } else { path.push("hr_agreed"); path.push("insurer_confirmed"); }
    }
    else { path.push("_branch"); }
    path.push("closed");
    return path;
  }
  function automationCard(q) {
    var auto = q.automation, path = stepPath(auto), cur = path.indexOf(auto.state);
    var ol = el("ol", { class: "steps", "aria-label": T("automation.steps_label") });
    path.forEach(function (st, i) {
      var cls = i < cur ? "is-done" : i === cur ? "is-current" : "is-next";
      var label = st === "_branch" ? T("automation.branch_placeholder") : T("automation.states." + st);
      ol.appendChild(el("li", { class: "steps__item " + cls, "aria-current": i === cur ? "step" : null }, [
        el("span", { class: "steps__mark", "aria-hidden": "true", text: i < cur ? "✓" : String(i + 1) }),
        el("span", { class: "steps__label", text: label }),
        i === cur ? el("span", { class: "vh", text: " (" + T("automation.current") + ")" }) : null
      ]));
    });
    var kids = [
      el("div", { class: "auto__head" }, [D.stateChip(auto.state), auto.clinic ? el("span", { class: "muted", text: T("automation.clinic", { name: auto.clinic }) }) : null]),
      ol
    ];
    if (auto.state === "alt_found") { kids.push(el("p", { class: "hint", text: T("automation.alt_found_hint") })); }
    if (auto.state === "insufficient") { kids.push(el("p", { class: "hint", text: T("automation.insufficient_hint") })); }
    var actions = (D.cfg("automation.flow") || {})[auto.state] || [];
    // Поле «Ответ СК» — только у действий, которые фиксируют ответ СК (automation.reply_actions).
    var replyActs = D.cfg("automation.reply_actions") || [];
    // Ответы СК, сохранённые как источник (history.insurer_reply), — дословно, с шагом, к которому относятся.
    // Шаг ответа СК без вставленного текста тоже виден: ветку выбрал менеджер, текста ответа нет.
    (auto.history || []).forEach(function (h) {
      if (!h) { return; }
      var ir = h.insurer_reply, stepName = T("automation.states." + (h.to || h.action || ""));
      if (ir && ir.text) {
        kids.push(el("div", { class: "f auto__reply" }, [el("p", { class: "source", text: h.action ? T("automation.reply_for", { step: stepName, at: D.fmtDateTime(h.at) }) : T("automation.reply_source", { at: D.fmtDateTime(h.at) }) }),
          el("div", { class: "quote", text: ir.text })]));
      } else if (h.action && replyActs.indexOf(h.action) >= 0) {
        kids.push(el("p", { class: "hint auto__reply", text: T("automation.reply_missing", { step: stepName }) }));
      }
    });
    var withReply = actions.filter(function (a) { return replyActs.indexOf(a) >= 0; });
    var reply = null;
    if (withReply.length) {
      var rid = "insurer-reply-" + q.id;
      var hint = withReply.indexOf("insurer_confirmed") >= 0 ? T("automation.confirm_hint") : T("automation.insurer_reply_hint");
      reply = el("textarea", { id: rid, rows: "3", maxlength: "8000", "aria-describedby": rid + "-h" });
      kids.push(el("div", { class: "f" }, [el("label", { for: rid, text: T("automation.insurer_reply") }), reply,
        el("p", { class: "hint", id: rid + "-h", text: hint })]));
    }
    var row = el("div", { class: "auto__actions" });
    actions.forEach(function (act, i) {
      var cls = act === "close" || act === "hr_declined" ? "btn" : i === 0 ? "btn btn--primary" : "btn";
      var b = el("button", { type: "button", class: cls, text: T("automation.actions." + act), disabled: q.status === "closed",
        on: { click: function () { step(act, reply && replyActs.indexOf(act) >= 0 ? reply.value.trim() : null, row); } } });
      row.appendChild(b);
    });
    if (!actions.length && auto.state !== "alt_found" && auto.state !== "insufficient") { row.appendChild(el("p", { class: "muted", text: T("automation.no_actions") })); }
    kids.push(row);
    return card(T("automation.title"), kids, "auto");
  }
  function step(action, reply, row) {
    Array.prototype.forEach.call(row.querySelectorAll("button"), function (b) { b.disabled = true; });
    var body = { action: action };
    if (reply) { body.insurer_reply = reply; }
    D.request("POST", "/api/requests/" + encodeURIComponent(state.req.id) + "/automation/step", body).then(function (r) {
      if (!r.ok) {
        Array.prototype.forEach.call(row.querySelectorAll("button"), function (b) { b.disabled = false; });
        showAlert(D.errorText(r));
        return;
      }
      state.draftTab = (r.data.drafts || []).length ? r.data.drafts[r.data.drafts.length - 1].to : state.draftTab;
      replaceRequest(r.data);
      focusAfter([".auto .auto__actions .btn--primary", ".auto .auto__actions .btn", ".auto .auto__head .chip"]);
    });
  }

  // ---------- черновики ----------
  function draftsCard(q) {
    var tabs = ["hr", "insurer"];
    var tablist = el("div", { class: "tabs", role: "tablist", "aria-label": T("drafts.title") });
    var panels = el("div");
    tabs.forEach(function (to) {
      var list = (q.drafts || []).filter(function (d) { return d.to === to; }).slice().reverse();
      var sel = state.draftTab === to;
      var tab = el("button", { type: "button", role: "tab", id: "dtab-" + to, "aria-controls": "dpanel-" + to,
        "aria-selected": sel ? "true" : "false", tabindex: sel ? "0" : "-1", text: T("drafts.tab_" + to) + " (" + list.length + ")",
        on: { click: function () { state.draftTab = to; renderRequest(); var t = $("dtab-" + to); if (t) { t.focus(); } },
              keydown: function (e) {
                if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
                  e.preventDefault(); state.draftTab = to === "hr" ? "insurer" : "hr"; renderRequest();
                  var t = $("dtab-" + state.draftTab); if (t) { t.focus(); }
                }
              } } });
      tablist.appendChild(tab);
      var panel = el("div", { role: "tabpanel", id: "dpanel-" + to, "aria-labelledby": "dtab-" + to, hidden: !sel, class: "drafts" });
      if (!list.length) { panel.appendChild(el("p", { class: "muted", text: T("drafts.empty_" + to) })); }
      list.forEach(function (d) { panel.appendChild(draftCard(d)); });
      panels.appendChild(panel);
    });
    return card(T("drafts.title"), [tablist, panels], "drafts-card");
  }
  function draftText(subject, body) { return subject + "\n\n" + body; }
  // ---------- письмо из ответа ----------
  // msgIdx — номер сообщения в обращении; серверу нужен номер ответа среди ответов по документам.
  function openLetter(to, msgIdx) {
    var q = state.req; if (!q) { return; }
    var n = (q.messages || []).slice(0, msgIdx + 1).filter(function (m) { return m.answer && !(m.answer.reply && m.answer.reply.service); }).length - 1;
    D.request("POST", "/api/requests/" + encodeURIComponent(q.id) + "/letter", { to: to, message: n < 0 ? null : n }).then(function (r) {
      if (!r.ok) { showAlert(D.errorText(r)); return; }
      state.letter = r.data;
      $("letter-h").textContent = T(to === "hr" ? "letters.title_hr" : "letters.title_insurer");
      $("letter-subject").value = r.data.subject; $("letter-body").value = r.data.body;
      $("letter-status").textContent = r.data.unknown_fields.length ? T("letters.unfilled", { n: r.data.unknown_fields.length }) : "";
      var dlg = $("letter-dlg");
      if (typeof dlg.showModal === "function") { dlg.showModal(); } else { dlg.setAttribute("open", ""); }
      $("letter-body").focus();
    });
  }
  // Перед копированием и «Открыть в почте» — проверка текста после правки: обещание — не даём, «[ЗАПОЛНИТЬ]» — предупреждаем.
  function checkLetter() {
    var subj = $("letter-subject").value, body = $("letter-body").value, st = $("letter-status");
    return D.request("POST", "/api/letters/check", { subject: subj, body: body, template: state.letter && state.letter.template }).then(function (r) {
      if (!r.ok) { st.textContent = D.errorText(r); return false; }
      if (r.data.promises.length) { st.textContent = T("letters.promises", { phrases: r.data.promises.join(", ") }); return false; }
      st.textContent = r.data.unfilled ? T("letters.unfilled", { n: r.data.unfilled }) : "";
      return true;
    });
  }
  function draftCard(d) {
    var sid = "ds-" + d.id, bid = "db-" + d.id;
    var subj = el("input", { type: "text", id: sid, value: d.subject, maxlength: "300" });
    var body = el("textarea", { id: bid, rows: String(Math.min(18, Math.max(6, (d.body || "").split("\n").length + 1))), maxlength: "20000" });
    body.value = d.body || "";
    var status = el("span", { class: "draft__status", role: "status" });
    var save = el("button", { type: "button", class: "btn btn--small btn--primary", text: T("common.save"), disabled: true });
    function dirty() { save.disabled = subj.value === d.subject && body.value === d.body; status.textContent = ""; }
    subj.addEventListener("input", dirty); body.addEventListener("input", dirty);
    save.addEventListener("click", function () {
      save.disabled = true;
      D.request("PATCH", "/api/drafts/" + encodeURIComponent(d.id), { subject: subj.value, body: body.value }).then(function (r) {
        if (!r.ok) { save.disabled = false; showAlert(D.errorText(r)); return; }
        d.subject = r.data.subject; d.body = r.data.body; d.edited_at = r.data.edited_at;
        status.textContent = T("common.saved");
      });
    });
    var copy = el("button", { type: "button", class: "btn btn--small", text: T("drafts.copy"), on: { click: function () {
      copyText(draftText(subj.value, body.value), body).then(function (ok) { status.textContent = ok ? T("drafts.copied") : T("errors.copy_failed"); });
    } } });
    var dl = el("button", { type: "button", class: "btn btn--small", text: T("drafts.download"), on: { click: function () {
      download(T("drafts.file_prefix") + "-" + d.to + "-" + d.stage + ".txt", draftText(subj.value, body.value));
    } } });
    var mail = el("button", { type: "button", class: "btn btn--small", text: T("drafts.mail"), "aria-describedby": "mh-" + d.id,
      on: { click: function () { openMail(subj.value, body.value); } } });
    var unknown = (d.unknown_fields || []).length ? el("p", { class: "draft__unknown" }, [D.chip(T("drafts.unknown", { fields: d.unknown_fields.map(function (f) { var t = fieldName(f); return /^.[\u0430-\u044f\u0451]/.test(t) ? t.charAt(0).toLowerCase() + t.slice(1) : t; }).join(", ") }), "chip--warn chip--bang")]) : null;
    var flagged = promiseNotes(d);
    return el("article", { class: "draft" }, [
      el("div", { class: "draft__bar" }, [el("strong", { text: T("drafts.not_sent") }), el("span", { text: T("drafts.stage", { name: T("automation.states." + d.stage) }) }),
        d.edited_at ? el("span", { text: T("drafts.edited", { at: D.fmtDateTime(d.edited_at) }) }) : null]),
      el("div", { class: "draft__form" }, [
        el("div", { class: "f" }, [el("label", { for: sid, text: T("drafts.subject") }), subj]),
        el("div", { class: "f" }, [el("label", { for: bid, text: T("drafts.body") }), body]),
        unknown,
        flagged,
        el("div", { class: "draft__actions" }, [save, copy, dl, mail, status]),
        el("p", { class: "hint", id: "mh-" + d.id, text: T("drafts.mail_hint") })
      ])
    ]);
  }
  // Имя поля шаблона письма (address, clinic…) — по-русски из automation.field_names; неизвестное — как есть.
  function fieldName(f) { var t = T("automation.field_names." + f); return t === "automation.field_names." + f ? f : t; }
  // Поля письма, очищенные автоматом из-за обещания в подставленном тексте (history.promise_fields).
  function promiseNotes(d) {
    var auto = state.req && state.req.automation, notes = [];
    ((auto && auto.history) || []).forEach(function (h) {
      ((h && h.promise_fields) || []).forEach(function (f) {
        if (f.draft !== d.id) { return; }
        notes.push(el("p", { class: "draft__unknown" }, [D.chip(T("automation.promise_field", {
          field: fieldName(f.field), phrases: (f.phrases || []).join("; ") }), "chip--warn chip--bang")]));
      });
    });
    return notes.length ? el("div", null, notes) : null;
  }
  function copyText(text, fallbackNode) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return legacyCopy(fallbackNode); });
    }
    return Promise.resolve(legacyCopy(fallbackNode));
  }
  function legacyCopy(node) {
    try { node.focus(); node.select(); return document.execCommand("copy"); } catch (e) { return false; }
  }
  function download(name, text) {
    var url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    var a = el("a", { href: url, download: name });
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  // Письмо не уходит само: адрес mailto: собирается только здесь, по клику менеджера; получателя вписывает он.
  // Отправляет почтовая программа менеджера, сервер о письме не знает (`docs/rules.md` п. 3).
  function openMail(subject, body) {
    window.location.href = "mailto:?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(body);
  }

  // ---------- меню «⋯»: новый вопрос, закрыть / открыть обращение, оценка, закрытые, источники ----------
  function answeredReq(q) { return !!q && (q.messages || []).some(function (m) { return m.answer; }); }
  function lastAnswerOf(q) { var a = null; ((q && q.messages) || []).forEach(function (m) { if (m.answer) { a = m.answer; } }); return a; }
  function renderMenu() {
    var ul = $("chat-menu");
    clear(ul);
    $("chat-menu-box").hidden = !state.chat;
    if (!state.chat) { return; }
    var q = state.req, items = [];
    items.push(["menu_new", function () { selectRequest(null); $("question").focus(); }]);
    if (q) { items.push([q.status === "closed" ? "menu_reopen" : "menu_close", function () { toggleRequest(q); }]); }
    if (answeredReq(q)) { items.push(["menu_rate", function () { state.rateOpen = true; renderRequest(); focusAfter([".rate input:checked", ".rate input"]); }]); }
    var la = lastAnswerOf(q), laSources = la ? orderedSources(la) : [];
    if (laSources.length) { items.push(["menu_sources", function () { openSource(laSources, null, $("chat-menu-btn")); }]); }
    if (answeredReq(q) && q.status !== "closed" && !q.automation && !(la && la.suggested_automation) && !state.offerOpen && !state.att.active) {
      items.push(["menu_add_clinic", function () { state.offerOpen = true; renderRequest(); focusAfter([".offer input"]); }]);
    }
    items.push([$("show-closed").checked ? "menu_closed_off" : "menu_closed_on", function () {
      var c = $("show-closed"); c.checked = !c.checked; loadRequests().then(renderRequest);
    }]);
    items.push(["attach.menu", openAttList]);
    items.push(["menu_delete_chat", deleteChat]);
    items.forEach(function (it) {
      var label = it[0].indexOf(".") > 0 ? T("attach.menu") : T("chat." + it[0]);
      ul.appendChild(el("li", null, [el("button", { type: "button", class: "menu__item" + (it[0] === "menu_delete_chat" ? " menu__item--danger" : ""), text: label,
        on: { click: function () { closeMenu(false); it[1](); } } })]));
    });
  }
  // Удалить чат целиком (обращения, черновики, вложения) — после подтверждения; затем — экран без выбранного чата.
  function deleteChat() {
    var c = state.chat;
    if (!c || !window.confirm(T("chat.delete_confirm", { title: chatTitle(c) }))) { return; }
    D.request("DELETE", "/api/chats/" + encodeURIComponent(c.id)).then(function (r) {
      if (!r.ok) { showAlert(D.errorText(r)); return; }
      var title = chatTitle(c);   // до удаления из списка: номер «· № 2» считается по соседям
      state.chats = state.chats.filter(function (x) { return x.id !== c.id; });
      state.chat = null; state.req = null; state.requests = []; state.sources = []; state.sourceSel = null;
      state.att = { items: [], active: null };
      D.writeHash({ chat: null, req: null });
      renderChats(); renderHead(); renderSources(); renderAttBar(); renderRequests(); renderRequest();
      setAskEnabled(true);
      $("announce").textContent = T("chat.deleted", { title: title });
      $("new-chat-btn").focus();
    });
  }
  function openMenu() {
    var ul = $("chat-menu"), b = $("chat-menu-btn");
    ul.hidden = false; b.setAttribute("aria-expanded", "true");
    var first = ul.querySelector("button"); if (first) { first.focus(); }
  }
  function closeMenu(focusBtn) {
    var ul = $("chat-menu"), b = $("chat-menu-btn");
    if (ul.hidden) { return false; }
    ul.hidden = true; b.setAttribute("aria-expanded", "false");
    if (focusBtn) { b.focus(); }
    return true;
  }
  // Стрелки вверх/вниз — по пунктам меню, Escape — закрыть и вернуть фокус на «⋯».
  function menuKeys(e) {
    var items = Array.prototype.slice.call($("chat-menu").querySelectorAll("button"));
    var i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      var n = items.length; if (!n) { return; }
      items[(i + (e.key === "ArrowDown" ? 1 : n - 1)) % n].focus();
    } else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
  }
  function toggleRequest(q) {
    var closed = q.status === "closed";
    D.request("POST", "/api/requests/" + encodeURIComponent(q.id) + (closed ? "/reopen" : "/close")).then(function (r) {
      if (!r.ok) { showAlert(D.errorText(r)); return; }
      replaceRequest(r.data);
      focusAfter(closed ? ["#question"] : ["#req-reopen", "#chat-menu-btn"]);
    });
  }

  // ---------- под лентой: «Обращение закрыто» и оценка (по пункту меню) ----------
  function requestTools(q) {
    var closed = q.status === "closed";
    var out = el("div", { class: "tools" });
    if (closed) {
      out.appendChild(el("p", { class: "closedbar" }, [el("span", { text: T("chat.closed_bar") }), " ",
        el("button", { type: "button", id: "req-reopen", class: "btn btn--small", text: T("chat.reopen_request"), on: { click: function () { toggleRequest(q); } } })]));
    }
    if (state.rateOpen && answeredReq(q)) { out.appendChild(rateBox(q)); }
    if (!closed && answeredReq(q)) { var fb = factsBar(q); if (fb) { out.appendChild(fb); } }
    return out;
  }
  // Карточка фактов обращения: что ассистент знает об этом случае (факты копятся по обращению — Request.slots, и
  // программа чата). Факт можно изменить: ответ уходит как уточнение (clarify) и склеивается с исходным вопросом;
  // вопросы — те же формулировки, что в «Что уточнить» (ui.yaml chat.facts), поэтому отвеченное не спрашивается снова.
  function factsBar(q) {
    var sl = q.slots || {}, ctx = (state.chat && state.chat.context) || {}, items = [];
    function add(key, value, question, opts, editable) { if (value) { items.push({ key: key, value: value, question: question, opts: opts || [], edit: editable }); } }
    add("service", sl.service || sl.care_type, null, null, false);
    add("clinic", sl.clinic, T("chat.facts.q_clinic"), null, true);
    add("program", ctx.program || sl.program, T("chat.facts.q_program"), null, !ctx.program);
    add("fact_mode", sl.fact_mode ? T("chat.facts.mode." + sl.fact_mode) : null, T("chat.facts.q_mode"),
      [T("chat.facts.opt_emergency"), T("chat.facts.opt_planned")], true);
    add("fact_referral", sl.fact_referral ? T("chat.facts.referral." + sl.fact_referral) : null, T("chat.facts.q_referral"),
      [T("reply.yes"), T("reply.no")], true);
    if (!items.length) { return null; }
    var bar = el("div", { class: "facts", role: "group", "aria-label": T("chat.facts.title") }, [el("span", { class: "facts__t", text: T("chat.facts.title") })]);
    items.forEach(function (it) {
      var chip = el("span", { class: "facts__chip" }, [el("span", { class: "facts__k", text: T("chat.facts." + it.key) + ": " }), el("span", { class: "facts__v", text: it.value })]);
      if (it.edit && state.req && state.req.status !== "closed") {
        chip.appendChild(el("button", { type: "button", class: "facts__edit", title: T("chat.facts.edit"), "aria-label": T("chat.facts.edit") + ": " + T("chat.facts." + it.key),
          text: "\u270E", on: { click: function () { editFact(bar, it); } } }));
      }
      bar.appendChild(chip);
    });
    return bar;
  }
  function editFact(bar, it) {
    var old = bar.querySelector(".facts__form"); if (old) { old.remove(); }
    var input = el("input", { type: "text", class: "clar__input", maxlength: "300", placeholder: T("reply.clarify_own"), "aria-label": it.question });
    var form = el("form", { class: "facts__form clar__own", novalidate: true, on: { submit: function (e) {
      e.preventDefault(); var v = input.value.trim(); if (!v) { input.focus(); return; } ask(v, it.question);
    } } }, [input, el("button", { type: "submit", class: "btn btn--small btn--primary", text: T("reply.clarify_send") })]);
    var row = el("div", { class: "facts__form-row" }, it.opts.map(function (o) {
      return el("button", { type: "button", class: "reply__opt", text: o, on: { click: function () { ask(o, it.question); } } });
    }).concat([form]));
    bar.appendChild(el("div", { class: "facts__form-wrap" }, [el("p", { class: "clar__q", text: it.question }), row]));
    (it.opts.length ? row.querySelector(".reply__opt") : input).focus();
  }
  function rateBox(q) {
    var stars = el("div", { class: "seg__row stars" });
    for (var n = 1; n <= 5; n += 1) {
      stars.appendChild(el("label", { title: T("chat.star", { n: n }) }, [
        el("input", { type: "radio", name: "rate-" + q.id, value: String(n), checked: q.rating === n, "aria-label": T("chat.star", { n: n }) }),
        el("span", { text: String(n) + " ★" })
      ]));
    }
    var cid = "rate-c-" + q.id;
    var comment = el("input", { type: "text", id: cid, maxlength: "1000", value: q.rating_comment || "" });
    var status = el("p", { class: "hint rate__status", id: "rate-status", role: "status", text: q.rating ? T("chat.rated", { n: q.rating }) : "" });
    var send = el("button", { type: "button", class: "btn btn--small", text: T("chat.rate_send"), on: { click: function () {
      var picked = stars.querySelector("input:checked");
      if (!picked) { status.textContent = T("errors.rating_required"); return; }
      send.disabled = true;
      D.request("POST", "/api/requests/" + encodeURIComponent(q.id) + "/rate", { rating: Number(picked.value), comment: comment.value.trim() || null }).then(function (r) {
        send.disabled = false;
        if (!r.ok) { showAlert(D.errorText(r)); return; }
        replaceRequest(r.data);
        focusAfter(["#rate-status"]);
      });
    } } });
    var hide = el("button", { type: "button", class: "btn btn--small btn--link", text: T("common.close"), on: { click: function () {
      state.rateOpen = false; renderRequest(); $("chat-menu-btn").focus();
    } } });
    return el("section", { class: "rate", "aria-label": T("chat.rate_title") }, [
      el("fieldset", { class: "rate__stars" }, [el("legend", { text: T("chat.rate_legend") }), stars]),
      el("div", { class: "f rate__comment" }, [el("label", { for: cid, text: T("chat.rate_comment") }), comment]),
      el("div", { class: "rate__btns" }, [send, hide]),
      status
    ]);
  }

  // ---------- документ в чат (ADR 0016): кнопка «Прикрепить», перетаскивание, плашка «По документу», «Вложения» ----------
  // Вложение принадлежит чату (в других чатах не видно). Пока активно — вопросы чата сервер отвечает по нему
  // (answer_message → attachment.answer); ✕ на плашке — снова по договору чата. Настройка «также в документы
  // договора» (GET /api/settings) — диалог с выбором договора (в чате ГК) и типа документа.
  var ATT_TYPES = ["program", "contract", "amendment", "memo", "other"];
  function isAttachmentId(id) { return String(id || "").indexOf("att_") === 0; }
  function byAttachment(a) { return collectSources(a).some(function (c) { return isAttachmentId(c.doc_id); }); }
  function activeAttachment() {
    var id = state.att.active;
    return id ? state.att.items.filter(function (x) { return x.id === id; })[0] || null : null;
  }
  function loadAttachments() {
    if (!state.chat) { state.att = { items: [], active: null }; renderAttBar(); return Promise.resolve(); }
    var chatId = state.chat.id;
    return D.request("GET", "/api/chats/" + encodeURIComponent(chatId) + "/attachments").then(function (r) {
      if (!state.chat || state.chat.id !== chatId) { return; }
      state.att = r.ok && r.data ? { items: r.data.items || [], active: r.data.active || null } : { items: [], active: null };
      (state.att.items || []).forEach(function (x) { if (x.id && x.title) { state.docTitles[x.id] = x.title; } });
      renderAttBar(); renderMenu();
    });
  }
  function renderAttBar() {
    var a = activeAttachment(), bar = $("attbar");
    bar.hidden = !a;
    $("attbar-name").textContent = a ? a.title : "";
    $("attach-btn").disabled = !state.chat || state.busy;
  }
  function setActiveAttachment(id) {
    if (!state.chat) { return Promise.resolve(); }
    return D.request("PUT", "/api/chats/" + encodeURIComponent(state.chat.id) + "/attachments/active", { attachment_id: id }).then(function (r) {
      if (!r.ok) { showAlert(D.errorText(r)); return; }
      state.att = { items: r.data.items || [], active: r.data.active || null };
      renderAttBar(); renderMenu();
      if ($("att-list-dlg").open) { renderAttList(); }
    });
  }
  function pickFile() {
    if (state.busy) { return; }
    if (!state.chat) { state.pending = { attach: true }; openNewChat(); return; }
    var inp = $("attach-input"); inp.value = ""; inp.click();
  }
  // Файл выбран или брошен в чат: при настройке «только в этом чате» — сразу загрузка, иначе — диалог договора и типа.
  function handleFile(file) {
    if (!file || !state.chat || state.busy) { return; }
    D.request("GET", "/api/settings").then(function (r) {
      var toContract = r.ok && r.data && r.data.chat_upload_target === "contract";
      if (toContract) { openAttDialog(file); } else { uploadFile(file, {}); }
    });
  }
  function openAttDialog(file) {
    state.attFile = file;
    $("att-file").textContent = file.name;
    var ctx = state.chat.context || {}, sel = $("att-contract"), types = $("att-type");
    clear(sel); clear(types);
    var ids = ctx.contract_id ? [ctx.contract_id] : ((state.groupById[ctx.group_id] || {}).contracts || []).map(function (c) { return c.id; });
    if (!ctx.contract_id) { sel.appendChild(el("option", { value: "", text: T("attach.dlg_contract_pick") })); }
    ids.forEach(function (id) { sel.appendChild(el("option", { value: id, text: contractName(id) })); });
    sel.disabled = !!ctx.contract_id;
    types.appendChild(el("option", { value: "", text: T("attach.dlg_type_auto") }));
    ATT_TYPES.forEach(function (t) { types.appendChild(el("option", { value: t, text: T("attach.types." + t) })); });
    var al = $("att-alert"); al.hidden = true; al.textContent = "";
    var dlg = $("att-dlg");
    if (dlg.showModal) { dlg.showModal(); } else { dlg.setAttribute("open", ""); }
    (sel.disabled ? types : sel).focus();
  }
  function closeAttDialog() { var dlg = $("att-dlg"); if (dlg.close) { dlg.close(); } else { dlg.removeAttribute("open"); } $("attach-btn").focus(); }
  function sendAttDialog(e) {
    e.preventDefault();
    var ctx = state.chat.context || {}, cid = $("att-contract").value;
    if (!ctx.contract_id && !cid) { var al = $("att-alert"); al.textContent = T("attach.dlg_contract_pick"); al.hidden = false; $("att-contract").focus(); return; }
    var file = state.attFile; state.attFile = null;
    closeAttDialog();
    uploadFile(file, { contract_id: ctx.contract_id ? "" : cid, doc_type: $("att-type").value });
  }
  function uploadFile(file, extra) {
    if (!file || !state.chat) { return; }
    state.busy = true; setAskEnabled(false); showAlert("");
    var fd = new FormData();
    fd.append("file", file, file.name);
    if (state.req && state.req.status !== "closed") { fd.append("request_id", state.req.id); }
    Object.keys(extra || {}).forEach(function (k) { if (extra[k]) { fd.append(k, extra[k]); } });
    var box = $("messages"), intro = box.querySelector(".intro"); if (intro) { box.removeChild(intro); }
    var prog = progressNode(); prog.label.textContent = T("attach.uploading", { name: file.name });
    box.appendChild(prog.node); box.scrollTop = box.scrollHeight;
    var chatId = state.chat.id;
    D.request("POST", "/api/chats/" + encodeURIComponent(chatId) + "/attachments", fd).then(function (r) {
      if (prog.node.parentNode) { prog.node.parentNode.removeChild(prog.node); }
      state.busy = false;
      if (!r.ok) { setAskEnabled(true); renderAttBar(); showAlert(D.errorText(r)); return; }
      if (!state.chat || state.chat.id !== chatId) { return; }
      var att = r.data.attachment || {};
      if (att.id && att.title) { state.docTitles[att.id] = att.title; }
      return Promise.all([loadAttachments(), loadRequests()]).then(function () {
        selectRequest(r.data.request_id);
        announceLast();
        if (!$("question").disabled) { $("question").focus(); }
      });
    });
  }
  function openAttList() {
    renderAttList();
    var dlg = $("att-list-dlg");
    if (dlg.showModal) { dlg.showModal(); } else { dlg.setAttribute("open", ""); }
    var b = dlg.querySelector(".attlist button") || $("att-list-close"); b.focus();
  }
  function closeAttList() { var dlg = $("att-list-dlg"); if (dlg.close) { dlg.close(); } else { dlg.removeAttribute("open"); } $("chat-menu-btn").focus(); }
  function renderAttList() {
    var ul = $("att-list");
    clear(ul);
    if (!state.att.items.length) { ul.appendChild(el("li", { class: "muted", text: T("attach.list_empty") })); return; }
    state.att.items.slice().reverse().forEach(function (x) {
      var on = x.id === state.att.active;
      ul.appendChild(el("li", { class: "attlist__item" + (on ? " is-active" : "") }, [
        el("div", { class: "attlist__main" }, [
          el("strong", { class: "attlist__title", text: x.title }),
          el("span", { class: "attlist__meta", text: [String(x.format || "").toUpperCase(), T("attach.clauses_n", { n: x.clauses || 0 }),
            x.uploaded_at ? D.fmtDateTime(x.uploaded_at) : ""].filter(Boolean).join(" · ") }),
          x.added_to ? el("span", { class: "attlist__meta", text: T("attach.added", { contract: contractName(x.added_to) }) }) : null
        ]),
        el("div", { class: "attlist__act" }, [
          on ? D.chip(T("attach.active"), "chip--ok") : null,
          el("button", { type: "button", class: "btn btn--small" + (on ? "" : " btn--primary"), text: T(on ? "attach.use_off" : "attach.use"),
            on: { click: function () { setActiveAttachment(on ? null : x.id); } } }),
          el("button", { type: "button", class: "btn btn--small", text: T("attach.delete"),
            "aria-label": T("attach.delete_aria", { name: x.title }), on: { click: function () { deleteAttachment(x); } } })
        ])
      ]));
    });
  }
  // Удаление вложения: подтверждение, затем список и плашка «По документу» перерисовываются; документ, добавленный к
  // договору, у договора остаётся (сообщение об этом — после удаления).
  function deleteAttachment(x) {
    if (!window.confirm(T(x.added_to ? "attach.delete_confirm_added" : "attach.delete_confirm", { name: x.title }))) { return; }
    D.request("DELETE", "/api/chats/" + encodeURIComponent(state.chat.id) + "/attachments/" + encodeURIComponent(x.id)).then(function (r) {
      if (!r.ok) { showAlert(D.errorText(r)); return; }
      state.att = { items: r.data.items || [], active: r.data.active || null };
      renderAttList(); renderAttBar(); renderMenu();
    });
  }
  // Перетаскивание файла в чат: подсветка зоны, первый файл — как по кнопке «Прикрепить».
  function wireDrop() {
    var zone = document.querySelector(".chat"), mark = $("dropzone"), depth = 0;
    function hasFiles(e) { return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0); }
    zone.addEventListener("dragenter", function (e) { if (!hasFiles(e) || !state.chat) { return; } e.preventDefault(); depth += 1; mark.hidden = false; });
    zone.addEventListener("dragover", function (e) { if (!hasFiles(e) || !state.chat) { return; } e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
    zone.addEventListener("dragleave", function () { depth = Math.max(0, depth - 1); if (!depth) { mark.hidden = true; } });
    zone.addEventListener("drop", function (e) {
      if (!hasFiles(e)) { return; }
      e.preventDefault(); depth = 0; mark.hidden = true;
      var f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) { handleFile(f); }
    });
  }

  // ---------- вопрос и индикатор «печатает…» ----------
  function setAskEnabled(on) {
    var t = $("question"), b = $("ask-btn");
    var closed = state.req && state.req.status === "closed";
    // без выбранного чата поле и кнопки работают: отправка или «Прикрепить» открывают выбор договора (pendingAsk)
    t.disabled = !on || state.busy;
    b.disabled = t.disabled;
    $("attach-btn").disabled = state.busy;
    $("ask-hint").textContent = closed ? T("chat.request_closed_hint") : T("chat.ask_hint");
  }
  // Пузырь «печатает…»: три точки и одна короткая строка по стадии SSE (route → «Читаю вопрос…», contract →
  // «Ищу в договоре…», verify → «Проверяю пункты…», compose → «Пишу ответ…»); неизвестная стадия — «Думаю…».
  function progressNode() {
    var label = el("span", { class: "typing__text", text: T("chat.typing.start") });
    var node = el("div", { class: "msg msg--bot typing progress", role: "status", "aria-label": T("chat.typing_aria") }, [
      el("span", { class: "typing__dots", "aria-hidden": "true" }, [el("span"), el("span"), el("span")]), label]);
    return { node: node, label: label, stage: function (data) {
      var name = data && typeof data === "object" ? (data.stage || data.name || "") : data;
      name = String(name || "").split(/[\s:_-]/)[0];
      var t = T("chat.typing." + name);
      label.textContent = t === "chat.typing." + name ? T("chat.typing.other") : t;
    } };
  }
  // Счётчик символов вопроса (ревизия w16): поле обрезает ввод по maxlength молча — с INPUT_WARN символов
  // показываем «N из 4000», на пределе — подсказку; вставка длиннее предела — отдельное предупреждение.
  var INPUT_WARN = 3500;
  function inputMax() { var t = $("question"); return t.maxLength > 0 ? t.maxLength : 4000; }
  function countInput(cut) {
    var t = $("question"), c = $("ask-count"), n = t.value.length, max = inputMax();
    if (!c) { return; }
    var show = cut || n >= INPUT_WARN;
    c.hidden = !show;
    c.classList.toggle("ask__count--max", !!cut || n >= max);
    c.textContent = !show ? "" : cut ? T("chat.input_cut", { max: max }) : n >= max ? T("chat.input_limit", { max: max }) : T("chat.input_count", { n: n, max: max });
  }
  function onPaste(e) {
    var t = $("question"), data = e.clipboardData && e.clipboardData.getData("text");
    if (!data) { return; }
    var room = inputMax() - (t.value.length - (t.selectionEnd - t.selectionStart));
    if (data.length > room) { setTimeout(function () { countInput(true); }, 0); }
  }
  // Поле ввода растёт по тексту до 6 строк (дальше — прокрутка внутри поля).
  function autoGrow() {
    var t = $("question");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight + 2, 168) + "px";
  }
  // Новый ответ — короткой фразой в aria-live (вся лента не перечитывается).
  function announceLast() {
    var q = state.req, a = lastAnswerOf(q);
    if (!a) { return; }
    var vk = a.reply ? verdictOf(a) : null;
    var v = vk ? T("reply.verdict." + vk) : (!a.reply && a.label ? T("labels." + a.label) : "");
    $("announce").textContent = T("chat.new_answer", { text: [v, a.reply ? a.reply.lead : a.summary].filter(Boolean).join(": ") });
  }
  // clarify — вопрос из «Что уточнить», на который это ответ (сервер склеит ответ с исходным вопросом)
  function ask(text, clarify) {
    text = (text || "").trim();
    if (!text) { showAlert(T("errors.empty_question")); $("question").focus(); return Promise.resolve(); }
    // чат не выбран: сначала выбор договора или группы, после создания чата вопрос уйдёт сам (createChat)
    if (!state.chat) { state.pending = { text: text, clarify: clarify || null }; openNewChat(); return Promise.resolve(); }
    if (state.busy) { return Promise.resolve(); }
    state.busy = true; setAskEnabled(false); showAlert("");
    var ensure = state.req && state.req.status !== "closed" ? Promise.resolve({ ok: true, data: state.req })
      : D.request("POST", "/api/chats/" + encodeURIComponent(state.chat.id) + "/requests", { title: text.length > 80 ? shorten(text, 79) : text });
    return ensure.then(function (r) {
      if (!r.ok) { throw r; }
      if (!state.req || state.req.id !== r.data.id) {
        state.req = r.data; state.requests.unshift(r.data); renderRequests(); renderRequest();
        D.writeHash({ chat: state.chat.id, req: r.data.id });
      }
      var box = $("messages");
      var intro = box.querySelector(".intro"); if (intro) { box.removeChild(intro); }
      box.appendChild(userBubble(clarify ? clarify + " — " + text : text, new Date().toISOString()));
      var prog = progressNode();
      box.appendChild(prog.node);
      box.scrollTop = box.scrollHeight;
      if (!clarify) { $("question").value = ""; autoGrow(); countInput(); }
      return D.postStream("/api/requests/" + encodeURIComponent(state.req.id) + "/messages", clarify ? { text: text, clarify: clarify } : { text: text }, prog.stage).then(function (res) {
        if (prog.node.parentNode) { prog.node.parentNode.removeChild(prog.node); }
        if (!res.ok) { throw { res: res, text: text }; }
        state.sources = []; state.sourceSel = null;
        // занятость снимается до перерисовки: иначе кнопки выбора клиники в новом ответе рисуются неактивными
        return loadRequests().then(function () { state.busy = false; renderRequest(); scrollToLast(); announceLast(); });
      });
    }).catch(function (err) {
      var res = err && err.res ? err.res : err;
      var p = $("messages").querySelector(".progress"); if (p) { p.parentNode.removeChild(p); }
      state.busy = false;
      if (state.req) { renderRequest(); }   // убрать неотправленную реплику: история — только то, что сохранил сервер
      showAlert(D.errorText(res), function () { showAlert(""); ask(err && err.text ? err.text : text, clarify); });
      if (!clarify) { $("question").value = text; autoGrow(); countInput(); }
    }).then(function () {
      state.busy = false; setAskEnabled(!(state.req && state.req.status === "closed"));
      if (!$("question").disabled) { $("question").focus(); }
    });
  }

  // ---------- примеры: кнопка «Примеры вопросов» над полем ввода и подсказки в пустом чате ----------
  function toggleExamples(open) {
    var box = $("examples"), b = $("examples-btn");
    if (open === undefined) { open = box.hidden; }
    box.hidden = !open; b.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { var f = box.querySelector("button"); if (f) { f.focus(); } }
  }
  function loadExamples() {
    return D.request("GET", "/api/examples").then(function (r) {
      var list = $("examples-list");
      clear(list);
      state.examples = r.ok ? (r.data || []) : [];
      $("examples-n").textContent = "(" + state.examples.length + ")";
      // 0 примеров — кнопка «Примеры вопросов (0)» не показывается (ревизия w16).
      $("examples-btn").hidden = !state.examples.length;
      if (!state.examples.length) { list.appendChild(el("li", { class: "muted", text: T("chat.examples_empty") })); return; }
      state.examples.forEach(function (ex, i) {
        list.appendChild(el("li", null, [el("button", { type: "button", class: "ex", on: { click: function () { toggleExamples(false); runExample(ex); } } }, [
          el("span", { class: "ex__n", text: String(i + 1) }),
          el("span", { class: "ex__t" }, [el("strong", { text: ex.title || ex.id }), el("span", { class: "ex__q", text: ex.question || "" })])
        ])]));
      });
    });
  }
  function sameCtx(a, b) {
    a = a || {}; b = b || {};
    return (a.group_id || null) === (b.group_id || null) && (a.contract_id || null) === (b.contract_id || null) && (a.program || null) === (b.program || null);
  }
  function runExample(ex) {
    var ctx = Object.assign({}, ex.context || {});
    if (!ctx.date) { ctx.date = D.todayIso(); }
    if (ctx.contract_id && !ctx.group_id && state.contractById[ctx.contract_id]) { ctx.group_id = state.contractById[ctx.contract_id].group_id; }
    var existing = state.chats.filter(function (c) { return sameCtx(c.context, ctx); })[0];
    var p = existing ? Promise.resolve(existing) : D.request("POST", "/api/chats", { context: ctx, title: null }).then(function (r) {
      if (!r.ok) { throw r; }
      state.chats.unshift(r.data); return r.data;
    });
    // Пример открывает чат своего договора и ставит вопрос в поле ввода — отправляет менеджер сам (ревью сайта
    // 29.10.2026: раньше вопрос уходил сразу, и ответ приходил по договору, который менеджер не выбирал).
    p.then(function (chat) {
      return selectChat(chat.id).then(function () { state.req = null; renderRequests(); renderRequest(); useQuestion(ex.question); });
    }).catch(function (r) { showAlert(D.errorText(r)); });
  }

  // ---------- новый чат ----------
  function kind() { return document.querySelector('input[name="nc-kind"]:checked').value; }
  function fillContracts() {
    var sel = $("nc-contract"), q = $("nc-search").value.trim().toLowerCase();
    clear(sel);
    var n = 0;
    state.groups.forEach(function (g) {
      var og = el("optgroup", { label: g.name });
      (g.contracts || []).forEach(function (c) {
        var text = T("new_chat.contract_option", { entity: c.legal_entity, number: c.number || T("common.none"), insurer: T("insurers." + (c.insurer || "unknown")) });
        if (q && text.toLowerCase().indexOf(q) < 0 && g.name.toLowerCase().indexOf(q) < 0) { return; }
        og.appendChild(el("option", { value: c.id, text: text })); n += 1;
      });
      if (og.children.length) { sel.appendChild(og); }
    });
    if (n === 1) { sel.selectedIndex = 0; }
    $("nc-contract-none").hidden = n > 0;
    fillPrograms();
  }
  // Программы выбранного в диалоге договора или группы (подсказки поля «Программа» и проверка при создании чата).
  function dialogPrograms() {
    var names = {}, contracts = [];
    if (kind() === "group") { var g = state.groupById[$("nc-group").value]; contracts = g ? g.contracts || [] : []; }
    else if (state.contractById[$("nc-contract").value]) { contracts = [state.contractById[$("nc-contract").value]]; }
    contracts.forEach(function (c) { (c.programs || []).forEach(function (p) { names[typeof p === "string" ? p : p.name] = true; }); });
    return Object.keys(names).sort();
  }
  function fillPrograms() {
    var dl = $("nc-programs");
    clear(dl);
    dialogPrograms().forEach(function (p) { dl.appendChild(el("option", { value: p })); });
  }
  function openNewChat() {
    var gs = $("nc-group");
    clear(gs);
    state.groups.forEach(function (g) { gs.appendChild(el("option", { value: g.id, text: g.name })); });
    $("nc-search").value = ""; $("nc-program").value = ""; $("nc-title").value = ""; $("nc-date").value = D.todayIso();
    $("nc-alert").hidden = true;
    var pend = $("nc-pending");
    pend.hidden = !state.pending;
    pend.textContent = state.pending ? T(state.pending.attach ? "new_chat.pending_attach" : "new_chat.pending_question") : "";
    fillContracts(); syncKind();
    var dlg = $("new-chat-dlg");
    if (typeof dlg.showModal === "function") { dlg.showModal(); } else { dlg.setAttribute("open", ""); }
    (kind() === "group" ? gs : $("nc-search")).focus();
  }
  function syncKind() {
    var g = kind() === "group";
    $("nc-group-box").hidden = !g; $("nc-contract-box").hidden = g;
    fillPrograms();
  }
  function closeNewChat() { var dlg = $("new-chat-dlg"); if (dlg.close) { dlg.close(); } else { dlg.removeAttribute("open"); } $("new-chat-btn").focus(); }
  function createChat(e) {
    e.preventDefault();
    var ctx = { program: $("nc-program").value.trim() || null, date: $("nc-date").value || null };
    if (kind() === "group") { ctx.group_id = $("nc-group").value || null; ctx.contract_id = null; }
    else {
      var c = state.contractById[$("nc-contract").value];
      ctx.contract_id = c ? c.id : null; ctx.group_id = c ? c.group_id : null;
    }
    var al = $("nc-alert");
    if (!ctx.group_id && !ctx.contract_id) { al.textContent = T("errors.choose_context"); al.hidden = false; return; }
    // программы нет ни в договоре, ни в группе — иначе ассистент спрашивал бы её снова в каждом ответе
    if (ctx.program) {
      var known = dialogPrograms(), want = ctx.program.toLowerCase().replace(/\s+/g, " ");
      var hit = known.filter(function (p) { return p.toLowerCase().replace(/\s+/g, " ") === want; })[0];
      if (known.length && !hit) { al.textContent = T("new_chat.program_unknown", { program: ctx.program }); al.hidden = false; $("nc-program").focus(); return; }
      if (hit) { ctx.program = hit; }
    }
    $("nc-create").disabled = true;
    D.request("POST", "/api/chats", { context: ctx, title: $("nc-title").value.trim() || null }).then(function (r) {
      $("nc-create").disabled = false;
      if (!r.ok) { al.textContent = D.errorText(r); al.hidden = false; return; }
      state.chats.unshift(r.data);
      var pending = state.pending; state.pending = null;
      closeNewChat();
      // Фокус — в поле вопроса: следующий шаг после создания чата — вопрос (ревизия w16). Вопрос, написанный до
      // выбора договора, отправляется сразу; «Прикрепить» до выбора — открывается выбор файла.
      selectChat(r.data.id).then(function () {
        if (pending && pending.text) { return ask(pending.text, pending.clarify); }
        if (pending && pending.attach) { pickFile(); return; }
        if (!$("question").disabled) { $("question").focus(); }
      });
    });
  }

  // ---------- запуск ----------
  function wire() {
    $("new-chat-btn").addEventListener("click", openNewChat);
    $("nc-cancel").addEventListener("click", function () { state.pending = null; closeNewChat(); });
    $("letter-close").addEventListener("click", function () { var d = $("letter-dlg"); if (d.close) { d.close(); } else { d.removeAttribute("open"); } });
    $("letter-copy").addEventListener("click", function () {
      checkLetter().then(function (ok) {
        if (!ok) { return; }
        copyText(draftText($("letter-subject").value, $("letter-body").value), $("letter-body")).then(function (done) {
          var st = $("letter-status"); st.textContent = (st.textContent ? st.textContent + " " : "") + T(done ? "letters.copied" : "errors.copy_failed");
        });
      });
    });
    $("letter-download").addEventListener("click", function () {
      checkLetter().then(function (ok) { if (ok) { download(T("drafts.file_prefix") + "-" + (state.letter ? state.letter.to : "letter") + ".txt", draftText($("letter-subject").value, $("letter-body").value)); } });
    });
    // mailto: — только по клику (openMail на одной строке с обработчиком click — тест test_mailto_only_in_click_handler)
    $("letter-mail").addEventListener("click", function () { checkLetter().then(function (ok) { if (ok) { openMail($("letter-subject").value, $("letter-body").value); } }); });
    $("new-chat-form").addEventListener("submit", createChat);
    Array.prototype.forEach.call(document.querySelectorAll('input[name="nc-kind"]'), function (r) { r.addEventListener("change", syncKind); });
    $("nc-search").addEventListener("input", fillContracts);
    $("nc-group").addEventListener("change", fillPrograms);
    $("nc-contract").addEventListener("change", fillPrograms);
    $("new-req-btn").addEventListener("click", function () { closeSide(); selectRequest(null); $("question").focus(); });
    $("show-closed").addEventListener("change", function () { loadRequests().then(renderRequest); });
    $("ask-form").addEventListener("submit", function (e) { e.preventDefault(); ask($("question").value); });
    // Enter — отправить, Shift+Enter — новая строка; Ctrl/Cmd+Enter — тоже отправить. Во время набора IME — не трогаем.
    $("question").addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || e.isComposing || e.shiftKey) { return; }
      e.preventDefault(); ask($("question").value);
    });
    $("question").addEventListener("input", function () { autoGrow(); countInput(); });
    $("question").addEventListener("paste", onPaste);
    $("examples-btn").addEventListener("click", function () { toggleExamples(); });
    $("chat-menu-btn").addEventListener("click", function () { if (!closeMenu(false)) { openMenu(); } });
    $("chat-menu").addEventListener("keydown", menuKeys);
    $("side-toggle").addEventListener("click", function () { if (!closeSide()) { openSide(); } });
    $("side-backdrop").addEventListener("click", function () { closeSide(); $("side-toggle").focus(); });
    document.addEventListener("click", function (e) {
      if (!$("chat-menu-box").contains(e.target)) { closeMenu(false); }
      if (!$("examples").hidden && !$("ask-form").contains(e.target)) { toggleExamples(false); }
    });
    $("sources-close").addEventListener("click", closeDrawer);
    $("sources-backdrop").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || $("new-chat-dlg").open || $("att-dlg").open || $("att-list-dlg").open) { return; }
      // Сначала закрывается превью источника (WCAG 1.4.13), шторка и прочее — следующим Esc.
      if (previewOpen()) { e.preventDefault(); hidePreview(true); return; }
      if ($("sources").classList.contains("is-open")) { closeDrawer(); return; }
      if (!$("examples").hidden) { toggleExamples(false); $("examples-btn").focus(); return; }
      if (closeSide()) { $("side-toggle").focus(); }
    });
    $("attach-btn").addEventListener("click", pickFile);
    $("attach-input").addEventListener("change", function () { var f = this.files && this.files[0]; if (f) { handleFile(f); } });
    $("attbar-off").addEventListener("click", function () { setActiveAttachment(null).then(function () { $("question").focus(); }); });
    $("att-form").addEventListener("submit", sendAttDialog);
    $("att-cancel").addEventListener("click", function () { state.attFile = null; closeAttDialog(); });
    $("att-list-close").addEventListener("click", closeAttList);
    wireDrop();
    window.addEventListener("hashchange", applyHash);
    // Превью источника привязано к ссылке: прокрутка ленты или окна, смена размера — закрыть (кроме прокрутки
    // внутри самого превью); тап или клик вне превью и ссылки — закрыть.
    window.addEventListener("scroll", function (e) { if (previewOpen() && !pv.node.contains(e.target)) { hidePreview(false); } }, true);
    window.addEventListener("resize", function () { hidePreview(false); });
    document.addEventListener("pointerdown", function (e) {
      if (previewOpen() && !pv.node.contains(e.target) && !(pv.trigger && pv.trigger.contains(e.target))) { hidePreview(false); }
    });
  }
  // Чат занимает окно до низа: верх раскладки зависит от переносов шапки и плашки — меряем и кладём в --chat-top.
  function fitChat() {
    var top = $("chatlay").getBoundingClientRect().top + window.scrollY;
    document.documentElement.style.setProperty("--chat-top", Math.round(top) + "px");
  }
  function init() {
    fitChat();
    if (document.fonts && document.fonts.ready) { document.fonts.ready.then(fitChat); }
    window.addEventListener("resize", fitChat);
    wire();
    setAskEnabled(true);
    D.request("GET", "/api/groups").then(function (r) {
      indexGroups(r.ok ? r.data : []);
      if (!r.ok) { state.stickyError = { text: D.errorText(r), retry: function () { location.reload(); } }; showAlert(state.stickyError.text, state.stickyError.retry); }
      return Promise.all([loadChats(), loadExamples()]);
    }).then(function () {
      var h = D.readHash();
      if (h.chat) { selectChat(h.chat, h.req); }
      else { renderRequest(); }
    });
  }
  D.ready.then(init);
})();
