/* Страница «Настройки» (ADR 0016): куда сохранять документы, загруженные в чат. GET/PUT /api/settings.
   Тексты — config/texts_ru/ui.yaml (settings.*), данные сервера — только textContent. */
(function () {
  "use strict";
  var D = window.DMS, T = D.T, $ = D.$;

  function current() {
    var r = document.querySelector('input[name="chat_upload_target"]:checked');
    return r ? r.value : null;
  }
  function apply(data) {
    var v = (data && data.chat_upload_target) || "chat";
    Array.prototype.forEach.call(document.querySelectorAll('input[name="chat_upload_target"]'), function (r) { r.checked = r.value === v; });
  }
  function alertText(text) { var a = $("settings-alert"); a.textContent = text || ""; a.hidden = !text; }
  function setBusy(on) { $("upload-target").disabled = on; $("settings-save").disabled = on; }

  function load() {
    $("settings-status").textContent = T("common.loading");
    return D.request("GET", "/api/settings").then(function (r) {
      $("settings-status").textContent = "";
      if (!r.ok) { alertText(D.errorText(r)); return; }
      apply(r.data); setBusy(false);
    });
  }
  function save(e) {
    e.preventDefault();
    var v = current();
    if (!v) { return; }
    setBusy(true); alertText(""); $("settings-status").textContent = "";
    D.request("PUT", "/api/settings", { chat_upload_target: v }).then(function (r) {
      setBusy(false);
      if (!r.ok) { alertText(D.errorText(r)); return; }
      apply(r.data);
      $("settings-status").textContent = T("settings.saved");
    });
  }
  function init() {
    $("settings-form").addEventListener("submit", save);
    load();
  }
  D.ready.then(init);
})();
