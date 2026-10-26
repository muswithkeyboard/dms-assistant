/* Тема: светлая «Кубики» / тёмная Mains (docs/ui_style.md). Выбор хранится в localStorage (в try/catch: в приватном
   окне хранилище может быть недоступно); без выбора — по prefers-color-scheme. Подключается в <head>, чтобы тема
   применялась до отрисовки, без мигания. */
(function () {
  "use strict";
  var KEY = "dms-theme";
  var root = document.documentElement;
  function saved() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function store(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* без хранилища — до перезагрузки */ } }
  function current() {
    var t = root.getAttribute("data-theme");
    if (t) { return t; }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  var t = saved();
  if (t === "dark" || t === "light") { root.setAttribute("data-theme", t); }
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) { return; }
    function label() { btn.textContent = current() === "dark" ? "☀" : "☾"; }
    label();
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      store(next);
      label();
    });
  });
})();
