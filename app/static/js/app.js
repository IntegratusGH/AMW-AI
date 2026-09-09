// AMW-AI - small UI behaviours (no external dependencies)
(function () {
  "use strict";

  // Close the user dropdown when clicking outside of it.
  document.addEventListener("click", function (event) {
    document.querySelectorAll("details.user-menu[open]").forEach(function (menu) {
      if (!menu.contains(event.target)) {
        menu.removeAttribute("open");
      }
    });
  });

  // Tabs: a [data-tabs] container with .tab-btn[data-tab-target] buttons and
  // matching .tab-panel elements (by id).  Clicking a button shows its panel.
  document.querySelectorAll("[data-tabs]").forEach(function (root) {
    root.querySelectorAll(".tab-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var target = btn.getAttribute("data-tab-target");
        root.querySelectorAll(".tab-btn").forEach(function (b) {
          b.classList.toggle("is-active", b === btn);
        });
        root.querySelectorAll(".tab-panel").forEach(function (p) {
          p.classList.toggle("is-active", p.id === target);
        });
      });
    });
  });

  // Auto-dismiss flash messages after a short delay.
  window.setTimeout(function () {
    document.querySelectorAll(".flash").forEach(function (el) {
      el.style.transition = "opacity .4s";
      el.style.opacity = "0";
      window.setTimeout(function () { el.remove(); }, 400);
    });
  }, 5000);
})();
