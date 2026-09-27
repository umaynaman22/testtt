// Small enhancements only; every page works without JavaScript.
(function () {
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      var s = document.getElementById("global-search");
      if (s) { e.preventDefault(); s.focus(); s.select(); }
    }
    // Rent Day: Enter in a row saves that row.
    if (e.key === "Enter" && e.target.closest && e.target.closest("tr[data-row]")) {
      var btn = e.target.closest("tr[data-row]").querySelector("button[data-save]");
      if (btn) { e.preventDefault(); btn.click(); }
    }
  });
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); }
  }, true);
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-print]");
    if (t) { e.preventDefault(); window.print(); }
    var all = e.target.closest("[data-check-all]");
    if (all) {
      var boxes = document.querySelectorAll("input[name='" + all.getAttribute("data-check-all") + "']");
      boxes.forEach(function (b) { b.checked = all.checked; });
    }
  });
  // After HTMX saves a Rent Day row, move focus to the next row's amount field.
  var savingRow = null;
  document.addEventListener("htmx:beforeRequest", function (e) {
    var tr = e.detail.elt.closest && e.detail.elt.closest("tr[data-row]");
    savingRow = tr ? tr.id : null;
  });
  document.addEventListener("htmx:afterSettle", function () {
    var row = savingRow && document.getElementById(savingRow);
    if (!row) { return; }
    var target = row.classList.contains("saved") ? row.nextElementSibling : row;
    var input = target && target.querySelector("input.amt");
    if (input) { input.focus(); input.select(); }
  });
  // Picking "Other" as the payment method: jump to the box to type it in (CSS shows the box).
  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("select[data-method]") && e.target.value === "other") {
      var scope = e.target.closest("tr, form");
      var box = scope && scope.querySelector("input[name='method_other']");
      if (box) { box.focus(); box.select(); }
    }
  });
  // Select the whole amount when a field gets focus, so typing replaces it.
  document.addEventListener("focusin", function (e) {
    if (e.target.matches && e.target.matches("input.amt, input[name='method_other']")) { e.target.select(); }
  });
})();
