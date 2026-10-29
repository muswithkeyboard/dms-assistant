/* Общие помощники страниц ассистента ДМС: тексты, DOM, запросы к API, поток SSE, демо-режим ?mock=1.
   Чистый JS без сборки и библиотек. Тексты — только из config/texts_ru/ui.yaml (атрибут data-ui у <body>);
   всё, что пришло от сервера, вставляется только через textContent (никакого innerHTML). */
(function () {
  "use strict";

  var UI = {};
  try { UI = JSON.parse(document.body.getAttribute("data-ui") || "{}"); } catch (e) { UI = {}; }

  // Текст по пути "chat.ask"; нет такого — сам путь (видно в интерфейсе и ловится тестом tests/ui).
  function cfg(path) {
    return path.split(".").reduce(function (o, k) { return o && typeof o === "object" ? o[k] : undefined; }, UI);
  }
  function T(path, vars) {
    var v = cfg(path);
    if (typeof v !== "string") { return path; }
    return vars ? fmt(v, vars) : v;
  }
  function fmt(s, vars) {
    return String(s).replace(/\{(\w+)\}/g, function (m, k) {
      return vars[k] === undefined || vars[k] === null ? m : String(vars[k]);
    });
  }

  // ---------- DOM ----------
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) { return; }
        if (k === "class") { node.className = v; }
        else if (k === "text") { node.textContent = v; }
        else if (k === "on") { Object.keys(v).forEach(function (ev) { node.addEventListener(ev, v[ev]); }); }
        else { node.setAttribute(k, v === true ? "" : String(v)); }
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) { return; }
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }
  function clear(node) { while (node && node.firstChild) { node.removeChild(node.firstChild); } }
  function $(id) { return document.getElementById(id); }
  function chip(text, cls) { return el("span", { class: "chip" + (cls ? " " + cls : ""), text: text }); }

  // Чипы «иконка + слово» (docs/ui_style.md, «Чипы вердиктов и веток»): класс задаёт иконку, слово — ui.yaml.
  var LABEL_CHIP = { allowed: "chip--ok", conditional: "chip--warn chip--cond", denied: "chip--danger chip--x",
                     insufficient: "chip--q" };
  function labelChip(label) { return label ? chip(T("labels." + label), LABEL_CHIP[label] || "") : null; }
  var STATE_CHIP = { alt_found: "chip--ok", not_in_program: "chip--warn chip--bang", hr_wants_add: "chip--cond",
                     awaiting_insurer: "chip--warn chip--q", insurer_free: "chip--ok", insurer_paid: "chip--warn chip--rub",
                     hr_agreed: "chip--ok", hr_declined: "chip--none", closed: "chip--none", insufficient: "chip--q" };
  function stateChip(state) { return chip(T("automation.states." + state), STATE_CHIP[state] || ""); }
  var DOC_CHIP = { ready: "chip--ok", error: "chip--danger chip--x" };
  function docStatusChip(st) { return chip(T("doc_status." + st), DOC_CHIP[st] || "chip--q"); }
  function modeChip(mode) { return mode && mode !== "live" ? chip(T("modes." + mode), "chip--mode") : null; }

  var dateFmt = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
  var dtFmt = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  function fmtDate(iso) {
    if (!iso) { return T("common.none"); }
    var d = new Date(String(iso).length === 10 ? iso + "T00:00:00" : iso);
    return isNaN(d) ? String(iso) : dateFmt.format(d);
  }
  function fmtDateTime(iso) { var d = new Date(iso); return iso && !isNaN(d) ? dtFmt.format(d) : T("common.none"); }
  function todayIso() {
    var d = new Date(), m = d.getMonth() + 1, day = d.getDate();
    return d.getFullYear() + "-" + (m < 10 ? "0" : "") + m + "-" + (day < 10 ? "0" : "") + day;
  }

  // ---------- демо-режим ----------
  // mock_api.js подключается только флагом ?mock=1 (бриф w3-ui): без флага страница работает с настоящим API.
  var MOCK = /(^|[?&])mock=1(&|$)/.test(location.search.slice(1));
  var ready = new Promise(function (resolve) {
    if (!MOCK) { resolve(); return; }
    var s = document.createElement("script");
    s.src = "/static/mock_api.js";
    s.onload = function () { resolve(); };
    s.onerror = function () { resolve(); };
    document.head.appendChild(s);
  });
  ready.then(function () { var b = $("mock-banner"); if (b && MOCK && window.DMS_MOCK) { b.hidden = false; $("notice").hidden = false; } });
  // Ссылки внутри приложения сохраняют флаг демо-режима.
  function link(path) {
    if (!MOCK) { return path; }
    var hashAt = path.indexOf("#"), base = hashAt < 0 ? path : path.slice(0, hashAt), hash = hashAt < 0 ? "" : path.slice(hashAt);
    return base + (base.indexOf("?") < 0 ? "?" : "&") + "mock=1" + hash;
  }

  // ---------- запросы ----------
  // Итог любого запроса: {ok, status, data}; ошибки сервера — {"error": {code, message, field}} (CONTRACTS §8).
  function request(method, url, body) {
    if (MOCK && window.DMS_MOCK) { return window.DMS_MOCK.request(method, url, body); }
    var opts = { method: method, headers: { "Accept": "application/json" }, credentials: "same-origin", cache: "no-store" };
    if (body instanceof FormData) { opts.body = body; }
    else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(url, opts).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        return { status: resp.status, ok: resp.ok, data: data };
      });
    }, function () { return { status: 0, ok: false, data: null }; });
  }
  function errorText(r) {
    if (r && r.data && r.data.error && r.data.error.message) { return r.data.error.message; }
    if (!r || r.status === 0) { return T("errors.network"); }
    if (r.status === 429) { return T("errors.rate_limited"); }
    if (r.status === 413) { return T("errors.too_large"); }
    if (r.status === 404) { return T("errors.not_found"); }
    return T("errors.http", { code: r.status });
  }

  // POST с потоком SSE (план §7, FR 17): event: stage — этап подготовки (onStage), event: answer — итог.
  // Сервер может ответить обычным JSON (без поддержки потока) — тогда итог берётся из тела.
  function postStream(url, body, onStage) {
    if (MOCK && window.DMS_MOCK) { return window.DMS_MOCK.stream(url, body, onStage); }
    return fetch(url, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Accept": "text/event-stream", "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (resp) {
      var ct = resp.headers.get("Content-Type") || "";
      if (!resp.ok || ct.indexOf("text/event-stream") < 0 || !resp.body) {
        return resp.text().then(function (text) {
          var data = null;
          try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
          return { status: resp.status, ok: resp.ok, data: data };
        });
      }
      var reader = resp.body.getReader(), decoder = new TextDecoder("utf-8"), buf = "", result = null;
      function handle(block) {
        var ev = "message", data = [];
        block.split("\n").forEach(function (line) {
          if (line.indexOf("event:") === 0) { ev = line.slice(6).trim(); }
          else if (line.indexOf("data:") === 0) { data.push(line.slice(5).replace(/^ /, "")); }
        });
        var raw = data.join("\n"), parsed = raw;
        try { parsed = JSON.parse(raw); } catch (e) { parsed = raw; }
        if (ev === "stage") { onStage(parsed); }
        else if (ev === "answer") { result = { status: 200, ok: true, data: parsed }; }
        else if (ev === "error") { result = { status: 500, ok: false, data: typeof parsed === "object" ? parsed : null }; }
      }
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            if (buf.trim()) { handle(buf); }
            return result || { status: 500, ok: false, data: { error: { message: T("errors.stream") } } };
          }
          buf += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, "\n");
          var i;
          while ((i = buf.indexOf("\n\n")) >= 0) { handle(buf.slice(0, i)); buf = buf.slice(i + 2); }
          return pump();
        });
      }
      return pump();
    }, function () { return { status: 0, ok: false, data: null }; });
  }

  // Состояние в адресе (#chat=…&req=…): после обновления страницы открывается та же история (ТЗ п. 9).
  function readHash() {
    var out = {};
    location.hash.replace(/^#/, "").split("&").forEach(function (p) {
      var kv = p.split("=");
      if (kv[0]) { out[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || ""); }
    });
    return out;
  }
  function writeHash(obj) {
    var parts = Object.keys(obj).filter(function (k) { return obj[k]; }).map(function (k) {
      return encodeURIComponent(k) + "=" + encodeURIComponent(obj[k]);
    });
    var h = parts.length ? "#" + parts.join("&") : "";
    if (location.hash !== h) { history.replaceState(null, "", location.pathname + location.search + h); }
  }

  window.DMS = {
    T: T, cfg: cfg, fmt: fmt, el: el, clear: clear, $: $, chip: chip, labelChip: labelChip, stateChip: stateChip,
    docStatusChip: docStatusChip, modeChip: modeChip, fmtDate: fmtDate, fmtDateTime: fmtDateTime, todayIso: todayIso,
    request: request, postStream: postStream, errorText: errorText, ready: ready, mock: MOCK, link: link,
    readHash: readHash, writeHash: writeHash
  };
})();
