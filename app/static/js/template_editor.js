// AMW-AI - CR-book positional template editor (Trainer tab).
// Draw/move/resize labelled value rectangles over a rendered CR page, apply
// existing templates, run OCR on the crops, and save the layout as a new
// template.  No external dependencies.
(function () {
  "use strict";

  var root = document.querySelector("[data-tmpl]");
  if (!root) return;

  function parse(attr, dflt) {
    try { return JSON.parse(root.getAttribute(attr)); } catch (e) { return dflt; }
  }
  var BATCHES = parse("data-batches", []);
  var TEMPLATES = parse("data-templates", []);
  var FIELDS = parse("data-fields", []);
  var CSRF = root.getAttribute("data-csrf") || "";
  var IMG_TPL = root.getAttribute("data-img-tpl") || "";
  var APPLY_URL = root.getAttribute("data-apply-url");
  var SAVE_URL = root.getAttribute("data-save-url");

  var FIELD_LABEL = {};
  FIELDS.forEach(function (f) { FIELD_LABEL[f.key] = f.label; });

  var el = {
    batch: root.querySelector("[data-batch]"),
    prev: root.querySelector("[data-prev]"),
    next: root.querySelector("[data-next]"),
    pageinfo: root.querySelector("[data-pageinfo]"),
    template: root.querySelector("[data-template]"),
    field: root.querySelector("[data-field]"),
    add: root.querySelector("[data-add]"),
    del: root.querySelector("[data-del]"),
    stage: root.querySelector("[data-stage]"),
    img: root.querySelector("[data-img]"),
    boxes: root.querySelector("[data-boxes]"),
    valField: root.querySelector("[data-val-field]"),
    valValue: root.querySelector("[data-val-value]"),
    valAdd: root.querySelector("[data-val-add]"),
    valResult: root.querySelector("[data-val-result]"),
    apply: root.querySelector("[data-apply]"),
    results: root.querySelector("[data-results]"),
    name: root.querySelector("[data-name]"),
    save: root.querySelector("[data-save]"),
    status: root.querySelector("[data-status]")
  };

  var state = {
    uid: BATCHES.length ? BATCHES[0].id : null,
    page: 1,
    pages: BATCHES.length ? BATCHES[0].page_count : 1,
    rects: [],          // [{name, x0, y0, x1, y1}]
    validation: null,   // {name, value, x0, y0, x1, y1}
    sel: null           // index into rects, or "val", or null
  };

  // --- helpers -------------------------------------------------------------
  function clamp01(v) { return Math.max(0, Math.min(1, v)); }
  function pct(v) { return (v * 100).toFixed(3) + "%"; }

  function imgUrl(uid, page) {
    return IMG_TPL.replace(/\/cr\/0\/page\/0\.png$/,
      "/cr/" + uid + "/page/" + page + ".png");
  }

  function setStatus(msg, kind) {
    el.status.textContent = msg || "";
    el.status.className = "tmpl-status" + (kind ? " is-" + kind : "");
  }

  function loadPage() {
    el.img.src = imgUrl(state.uid, state.page);
    el.pageinfo.textContent = "Page " + state.page + " / " + state.pages;
    el.prev.disabled = state.page <= 1;
    el.next.disabled = state.page >= state.pages;
  }

  // --- rendering -----------------------------------------------------------
  function render() {
    el.boxes.innerHTML = "";
    state.rects.forEach(function (r, i) {
      el.boxes.appendChild(makeBox(r, i, false));
    });
    if (state.validation) {
      el.boxes.appendChild(makeBox(state.validation, "val", true));
    }
  }

  function makeBox(r, idx, isVal) {
    var box = document.createElement("div");
    box.className = "tmpl-rect" + (isVal ? " is-val" : "") +
      (state.sel === idx ? " is-sel" : "") +
      (r._valid === false ? " is-bad" : (r._valid === true ? " is-ok" : ""));
    box.style.left = pct(r.x0);
    box.style.top = pct(r.y0);
    box.style.width = pct(r.x1 - r.x0);
    box.style.height = pct(r.y1 - r.y0);
    box.dataset.idx = idx;

    var label = document.createElement("span");
    label.className = "tmpl-rect__label";
    var name = isVal ? ("\u2713 " + (FIELD_LABEL[r.name] || r.name || "validation"))
      : (FIELD_LABEL[r.name] || r.name);
    label.textContent = name + (r._read ? "  \u2192 " + r._read : "");
    box.appendChild(label);

    ["nw", "ne", "sw", "se"].forEach(function (h) {
      var handle = document.createElement("span");
      handle.className = "tmpl-rect__h tmpl-rect__h--" + h;
      handle.dataset.handle = h;
      box.appendChild(handle);
    });

    box.addEventListener("pointerdown", function (ev) { onPointerDown(ev, idx); });
    return box;
  }

  // --- selection + keyboard ------------------------------------------------
  function select(idx) { state.sel = idx; render(); }

  function selectedRect() {
    if (state.sel === "val") return state.validation;
    if (typeof state.sel === "number") return state.rects[state.sel];
    return null;
  }

  el.stage.addEventListener("keydown", function (ev) {
    var r = selectedRect();
    if (!r) return;
    var step = ev.shiftKey ? 0.02 : 0.004;
    var dx = 0, dy = 0;
    if (ev.key === "ArrowLeft") dx = -step;
    else if (ev.key === "ArrowRight") dx = step;
    else if (ev.key === "ArrowUp") dy = -step;
    else if (ev.key === "ArrowDown") dy = step;
    else if (ev.key === "Delete" || ev.key === "Backspace") { deleteSelected(); ev.preventDefault(); return; }
    else return;
    ev.preventDefault();
    var w = r.x1 - r.x0, h = r.y1 - r.y0;
    r.x0 = clamp01(r.x0 + dx); r.y0 = clamp01(r.y0 + dy);
    r.x1 = clamp01(r.x0 + w); r.y1 = clamp01(r.y0 + h);
    render();
  });

  // --- drag / resize -------------------------------------------------------
  var drag = null;
  function onPointerDown(ev, idx) {
    ev.preventDefault();
    select(idx);
    var r = selectedRect();
    if (!r) return;
    var rect = el.stage.getBoundingClientRect();
    drag = {
      r: r,
      handle: ev.target.dataset.handle || null,
      startX: ev.clientX, startY: ev.clientY,
      ox0: r.x0, oy0: r.y0, ox1: r.x1, oy1: r.y1,
      sw: rect.width, sh: rect.height
    };
    el.stage.setPointerCapture(ev.pointerId);
  }

  el.stage.addEventListener("pointermove", function (ev) {
    if (!drag) return;
    var dx = (ev.clientX - drag.startX) / drag.sw;
    var dy = (ev.clientY - drag.startY) / drag.sh;
    var r = drag.r;
    if (!drag.handle) {                       // move whole box
      var w = drag.ox1 - drag.ox0, h = drag.oy1 - drag.oy0;
      r.x0 = clamp01(drag.ox0 + dx); r.y0 = clamp01(drag.oy0 + dy);
      r.x1 = clamp01(r.x0 + w); r.y1 = clamp01(r.y0 + h);
    } else {                                  // resize by corner
      var x0 = drag.ox0, y0 = drag.oy0, x1 = drag.ox1, y1 = drag.oy1;
      if (drag.handle[1] === "w") x0 = clamp01(x0 + dx); else x1 = clamp01(x1 + dx);
      if (drag.handle[0] === "n") y0 = clamp01(y0 + dy); else y1 = clamp01(y1 + dy);
      r.x0 = Math.min(x0, x1); r.x1 = Math.max(x0, x1);
      r.y0 = Math.min(y0, y1); r.y1 = Math.max(y0, y1);
    }
    render();
  });

  function endDrag(ev) {
    if (!drag) return;
    try { el.stage.releasePointerCapture(ev.pointerId); } catch (e) {}
    drag = null;
  }
  el.stage.addEventListener("pointerup", endDrag);
  el.stage.addEventListener("pointercancel", endDrag);

  // --- toolbar actions -----------------------------------------------------
  el.batch.addEventListener("change", function () {
    state.uid = parseInt(el.batch.value, 10);
    var b = BATCHES.filter(function (x) { return x.id === state.uid; })[0];
    state.pages = b ? b.page_count : 1;
    state.page = 1;
    loadPage();
  });

  el.prev.addEventListener("click", function () {
    if (state.page > 1) { state.page--; loadPage(); }
  });
  el.next.addEventListener("click", function () {
    if (state.page < state.pages) { state.page++; loadPage(); }
  });

  el.add.addEventListener("click", function () {
    var name = el.field.value;
    // default box near the centre; trainer positions it precisely
    state.rects.push({ name: name, x0: 0.40, y0: 0.10, x1: 0.62, y1: 0.135 });
    select(state.rects.length - 1);
  });

  function deleteSelected() {
    if (state.sel === "val") { state.validation = null; state.sel = null; render(); return; }
    if (typeof state.sel === "number") {
      state.rects.splice(state.sel, 1);
      state.sel = null;
      render();
    }
  }
  el.del.addEventListener("click", deleteSelected);

  el.valAdd.addEventListener("click", function () {
    state.validation = {
      name: el.valField.value,
      value: (el.valValue.value || "").trim(),
      x0: 0.30, y0: 0.03, x1: 0.70, y1: 0.075
    };
    select("val");
  });
  el.valValue.addEventListener("input", function () {
    if (state.validation) state.validation.value = (el.valValue.value || "").trim();
  });

  el.template.addEventListener("change", function () {
    var id = parseInt(el.template.value, 10);
    var t = TEMPLATES.filter(function (x) { return x.id === id; })[0];
    if (!t) { return; }
    state.rects = Object.keys(t.rects).map(function (k) {
      var b = t.rects[k];
      return { name: k, x0: b[0], y0: b[1], x1: b[2], y1: b[3] };
    });
    if (t.validation && t.validation.rect) {
      var vr = t.validation.rect;
      state.validation = {
        name: t.validation.name || "", value: t.validation.value || "",
        x0: vr[0], y0: vr[1], x1: vr[2], y1: vr[3]
      };
      el.valValue.value = state.validation.value;
      if (state.validation.name) el.valField.value = state.validation.name;
    } else {
      state.validation = null;
    }
    state.sel = null;
    render();
  });

  // --- server calls --------------------------------------------------------
  function rectsPayload() {
    var out = {};
    state.rects.forEach(function (r) {
      out[r.name] = [r.x0, r.y0, r.x1, r.y1];
    });
    return out;
  }
  function validationPayload() {
    if (!state.validation) return null;
    var v = state.validation;
    return { name: v.name, value: v.value, rect: [v.x0, v.y0, v.x1, v.y1] };
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    });
  }

  el.apply.addEventListener("click", function () {
    if (!state.rects.length && !state.validation) {
      setStatus("Add at least one box first.", "bad"); return;
    }
    setStatus("Reading crops\u2026", "");
    el.apply.disabled = true;
    postJSON(APPLY_URL, {
      upload_id: state.uid, page_no: state.page,
      rects: rectsPayload(), validation: validationPayload()
    }).then(function (r) {
      el.apply.disabled = false;
      if (!r.ok) { setStatus((r.data && r.data.error) || "Apply failed.", "bad"); return; }
      showResults(r.data);
      setStatus("Done.", "ok");
    }).catch(function () {
      el.apply.disabled = false; setStatus("Network error.", "bad");
    });
  });

  function showResults(data) {
    var res = data.results || {};
    var rows = "";
    state.rects.forEach(function (r) {
      var d = res[r.name] || {};
      r._read = d.value || "\u2014";
      r._valid = (d.valid === true) ? true : (d.valid === false ? false : null);
      var cls = r._valid === true ? "badge-success" :
        (r._valid === false ? "badge-warning" : "");
      rows += "<tr><td>" + (FIELD_LABEL[r.name] || r.name) +
        "</td><td class='mono'>" + (d.value || "\u2014") + "</td><td>" +
        (d.confidence != null ? (d.confidence * 100).toFixed(0) + "%" : "\u2014") +
        "</td><td>" + (cls ? "<span class='badge " + cls + "'>" +
          (r._valid ? "valid" : "check") + "</span>" : "\u2014") + "</td></tr>";
    });
    el.results.innerHTML = rows
      ? "<table class='table'><thead><tr><th>Field</th><th>Read</th><th>Conf</th><th></th></tr></thead><tbody>" + rows + "</tbody></table>"
      : "<p class='muted small'>No field boxes.</p>";
    var v = data.validation;
    if (v) {
      el.valResult.innerHTML = "<span class='badge " +
        (v.ok ? "badge-success'>match" : "badge-warning'>no match") + "</span> read: <span class='mono'>" +
        (v.value || "\u2014") + "</span>";
    } else {
      el.valResult.innerHTML = "";
    }
    render();
  }

  el.save && el.save.addEventListener("click", function () {
    var name = (el.name.value || "").trim();
    if (!name) { setStatus("Enter a template name.", "bad"); el.name.focus(); return; }
    if (!state.rects.length) { setStatus("Draw at least one field box.", "bad"); return; }
    setStatus("Saving\u2026", "");
    el.save.disabled = true;
    postJSON(SAVE_URL, {
      name: name, upload_id: state.uid, page_no: state.page,
      rects: rectsPayload(), validation: validationPayload()
    }).then(function (r) {
      el.save.disabled = false;
      if (!r.ok) { setStatus((r.data && r.data.error) || "Save failed.", "bad"); return; }
      TEMPLATES.push({ id: r.data.id, name: r.data.name, rects: r.data.rects,
        validation: r.data.validation || null });
      var opt = document.createElement("option");
      opt.value = r.data.id; opt.textContent = r.data.name;
      el.template.appendChild(opt);
      el.template.value = r.data.id;
      setStatus("Saved template \u201c" + r.data.name + "\u201d.", "ok");
    }).catch(function () {
      el.save.disabled = false; setStatus("Network error.", "bad");
    });
  });

  // --- init ----------------------------------------------------------------
  loadPage();
  render();
})();
