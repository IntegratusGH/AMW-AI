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

  // Auto-dismiss flash messages after a short delay.
  window.setTimeout(function () {
    document.querySelectorAll(".flash").forEach(function (el) {
      el.style.transition = "opacity .4s";
      el.style.opacity = "0";
      window.setTimeout(function () { el.remove(); }, 400);
    });
  }, 5000);
})();
