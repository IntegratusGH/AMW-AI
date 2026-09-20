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
        // Persist the active tab in the URL so a reload (incl. the status
        // poll's reload) or a Back navigation keeps the same tab.
        var param = root.getAttribute("data-tabs-url-param");
        if (param) {
          var key = btn.getAttribute("data-tab-key") || target.replace(/^tab-/, "");
          try {
            var url = new URL(window.location.href);
            url.searchParams.set(param, key);
            window.history.replaceState(null, "", url);
          } catch (e) {}
        }
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

  // In-app confirmation dialog (never use the browser's native confirm/alert).
  //   appConfirm(message, {okText, danger}) -> Promise<boolean>
  //   appNotify(message, {okText})          -> Promise<true>  (single button)
  var dlg = document.querySelector("[data-confirm-dialog]");
  if (dlg) {
    var msgEl = dlg.querySelector("[data-confirm-msg]");
    var okBtn = dlg.querySelector("[data-confirm-ok]");
    var cancelBtn = dlg.querySelector(".confirm-dialog__actions [data-confirm-cancel]");
    var cancelEls = dlg.querySelectorAll("[data-confirm-cancel]");
    var pending = null;
    function settle(val) {
      if (!pending) return;
      var resolve = pending; pending = null;
      dlg.hidden = true;
      document.removeEventListener("keydown", onKey, true);
      resolve(val);
    }
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); settle(false); }
      else if (e.key === "Enter") { e.preventDefault(); settle(true); }
    }
    okBtn.addEventListener("click", function () { settle(true); });
    cancelEls.forEach(function (b) {
      b.addEventListener("click", function () { settle(false); });
    });
    window.appConfirm = function (message, opts) {
      opts = opts || {};
      return new Promise(function (resolve) {
        if (pending) settle(false);
        pending = resolve;
        msgEl.textContent = message || "Are you sure?";
        okBtn.textContent = opts.okText || "OK";
        okBtn.className = "btn " + (opts.danger ? "btn-danger" : "btn-primary");
        cancelBtn.style.display = opts.alert ? "none" : "";
        dlg.hidden = false;
        document.addEventListener("keydown", onKey, true);
        okBtn.focus();
      });
    };
    window.appNotify = function (message, opts) {
      opts = opts || {}; opts.alert = true;
      return window.appConfirm(message, opts);
    };

    // Generic guard: any <form data-confirm="..."> shows the dialog first and
    // only submits when confirmed (form.submit() bypasses this listener).
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-confirm")) {
        return;
      }
      e.preventDefault();
      window.appConfirm(form.getAttribute("data-confirm"), {
        danger: true, okText: form.getAttribute("data-confirm-ok") || "Delete"
      }).then(function (ok) { if (ok) form.submit(); });
    });
  }
})();
