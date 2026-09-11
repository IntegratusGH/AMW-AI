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
  var UPDATE_URL = root.getAttribute("data-update-url");
  var PREVIEW_URL = root.getAttribute("data-preview-url");

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
    fieldlist: root.querySelector("[data-fieldlist]"),
    valName: root.querySelector("[data-val-name]"),
    valValue: root.querySelector("[data-val-value]"),
    valAdd: root.querySelector("[data-val-add]"),
    valSettings: root.querySelector("[data-val-settings]"),
    valResult: root.querySelector("[data-val-result]"),
    apply: root.querySelector("[data-apply]"),
    name: root.querySelector("[data-name]"),
    save: root.querySelector("[data-save]"),
    update: root.querySelector("[data-update]"),
    status: root.querySelector("[data-status]")
  };

  var md = {
    modal: root.querySelector("[data-modal]"),
    title: root.querySelector("[data-modal-title]"),
    img: root.querySelector("[data-modal-img]"),
    ocr: root.querySelector("[data-modal-ocr]"),
    reset: root.querySelector("[data-modal-reset]"),
    test: root.querySelector("[data-modal-test]"),
    apply: root.querySelector("[data-modal-apply]"),
    thvalWrap: root.querySelector("[data-thval-wrap]"),
    thval: root.querySelector("[data-thval]"),
    controls: Array.prototype.slice.call(root.querySelectorAll("[data-pp]")),
    closers: Array.prototype.slice.call(root.querySelectorAll("[data-modal-close]"))
  };

  var state = {
    uid: BATCHES.length ? BATCHES[0].id : null,
    page: 1,
    pages: BATCHES.length ? BATCHES[0].page_count : 1,
    rects: [],          // [{name, x0, y0, x1, y1}]
    validation: null,   // {name, value, x0, y0, x1, y1, pp}
    pp: {},             // {fieldName: {contrast, brightness, ...}}
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
    runApply(true);   // auto-run Apply / Check on the newly shown CR page
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
    renderFieldList();
  }

  function hasPp(name) {
    var p = state.pp[name];
    return !!(p && Object.keys(p).length);
  }

  function renderFieldList() {
    if (!el.fieldlist) return;
    el.fieldlist.innerHTML = "";
    if (!state.rects.length) {
      el.fieldlist.innerHTML =
        "<p class='muted small'>No boxes yet. Pick a field above and click \u201c+ Add box\u201d.</p>";
      return;
    }
    var table = document.createElement("table");
    table.className = "table tmpl-ftable";
    table.innerHTML =
      "<thead><tr><th>Field</th><th>Read</th><th>Conf</th><th></th></tr></thead>";
    var tb = document.createElement("tbody");
    state.rects.forEach(function (r, i) {
      var tr = document.createElement("tr");
      tr.className = (state.sel === i ? "is-sel" : "") +
        (r._valid === false ? " is-bad" : (r._valid === true ? " is-ok" : ""));

      var nm = document.createElement("td");
      nm.className = "tmpl-ftable__name";
      nm.title = "Select box";
      nm.textContent = FIELD_LABEL[r.name] || r.name;
      if (hasPp(r.name)) {
        var dot = document.createElement("span");
        dot.className = "tmpl-frow__dot";
        dot.title = "Has image settings";
        nm.appendChild(dot);
      }
      nm.addEventListener("click", function () { select(i); });
      tr.appendChild(nm);

      var read = document.createElement("td");
      read.className = "mono";
      read.textContent = r._read || "\u2014";
      tr.appendChild(read);

      var conf = document.createElement("td");
      conf.className = "tmpl-ftable__conf";
      conf.textContent = (r._conf != null)
        ? Math.round(r._conf * 100) + "%" : "\u2014";
      tr.appendChild(conf);

      var act = document.createElement("td");
      act.className = "tmpl-ftable__act";
      var gear = document.createElement("button");
      gear.type = "button";
      gear.className = "btn btn-icon tmpl-frow__btn";
      gear.title = "Image settings";
      gear.textContent = "\u2699";
      gear.addEventListener("click", function () { openModal(i); });
      act.appendChild(gear);
      var del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-icon tmpl-frow__btn";
      del.title = "Delete box";
      del.textContent = "\u2715";
      del.addEventListener("click", function () { state.sel = i; deleteSelected(); });
      act.appendChild(del);
      tr.appendChild(act);

      tb.appendChild(tr);
    });
    table.appendChild(tb);
    el.fieldlist.appendChild(table);
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
      var r = state.rects[state.sel];
      if (r) delete state.pp[r.name];
      state.rects.splice(state.sel, 1);
      state.sel = null;
      render();
    }
  }
  el.del.addEventListener("click", deleteSelected);
  el.valSettings && el.valSettings.addEventListener("click", function () {
    openModal("val");
  });

  el.valAdd.addEventListener("click", function () {
    state.validation = {
      name: (el.valName.value || "").trim(),
      value: (el.valValue.value || "").trim(),
      x0: 0.30, y0: 0.03, x1: 0.70, y1: 0.075
    };
    select("val");
  });
  el.valValue.addEventListener("input", function () {
    if (state.validation) state.validation.value = (el.valValue.value || "").trim();
  });
  el.valName.addEventListener("input", function () {
    if (state.validation) state.validation.name = (el.valName.value || "").trim();
  });

  function refreshUpdateBtn() {
    if (!el.update) return;
    el.update.hidden = !el.template.value;
  }

  el.template.addEventListener("change", function () {
    var id = parseInt(el.template.value, 10);
    var t = TEMPLATES.filter(function (x) { return x.id === id; })[0];
    if (!t) { refreshUpdateBtn(); return; }
    state.rects = Object.keys(t.rects).map(function (k) {
      var b = t.rects[k];
      return { name: k, x0: b[0], y0: b[1], x1: b[2], y1: b[3] };
    });
    state.pp = {};
    if (t.field_pp) {
      Object.keys(t.field_pp).forEach(function (k) {
        state.pp[k] = JSON.parse(JSON.stringify(t.field_pp[k]));
      });
    }
    if (t.validation && t.validation.rect) {
      var vr = t.validation.rect;
      state.validation = {
        name: t.validation.name || "", value: t.validation.value || "",
        x0: vr[0], y0: vr[1], x1: vr[2], y1: vr[3],
        pp: t.validation.pp ? JSON.parse(JSON.stringify(t.validation.pp)) : null
      };
      el.valValue.value = state.validation.value;
      el.valName.value = state.validation.name || "";
    } else {
      state.validation = null;
    }
    state.sel = null;
    render();
    refreshUpdateBtn();
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
    var out = { name: v.name, value: v.value, rect: [v.x0, v.y0, v.x1, v.y1] };
    if (v.pp && Object.keys(v.pp).length) out.pp = v.pp;
    return out;
  }
  function fieldPpPayload() {
    var out = {};
    state.rects.forEach(function (r) {
      var p = state.pp[r.name];
      if (p && Object.keys(p).length) out[r.name] = p;
    });
    return out;
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

  function runApply(auto) {
    if (!state.rects.length && !state.validation) {
      if (!auto) setStatus("Add at least one box first.", "bad");
      return;
    }
    setStatus("Reading crops\u2026", "");
    el.apply.disabled = true;
    postJSON(APPLY_URL, {
      upload_id: state.uid, page_no: state.page,
      rects: rectsPayload(), field_pp: fieldPpPayload(),
      validation: validationPayload()
    }).then(function (r) {
      el.apply.disabled = false;
      if (!r.ok) { setStatus((r.data && r.data.error) || "Apply failed.", "bad"); return; }
      showResults(r.data);
      setStatus("Done.", "ok");
    }).catch(function () {
      el.apply.disabled = false; setStatus("Network error.", "bad");
    });
  }

  el.apply.addEventListener("click", function () { runApply(false); });

  function showResults(data) {
    var res = data.results || {};
    state.rects.forEach(function (r) {
      var d = res[r.name] || {};
      r._read = d.value || "\u2014";
      r._conf = (d.confidence != null) ? d.confidence : null;
      r._valid = (d.valid === true) ? true : (d.valid === false ? false : null);
    });
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
      rects: rectsPayload(), field_pp: fieldPpPayload(),
      validation: validationPayload()
    }).then(function (r) {
      el.save.disabled = false;
      if (!r.ok) { setStatus((r.data && r.data.error) || "Save failed.", "bad"); return; }
      TEMPLATES.push({ id: r.data.id, name: r.data.name, rects: r.data.rects,
        validation: r.data.validation || null, field_pp: r.data.field_pp || {} });
      var opt = document.createElement("option");
      opt.value = r.data.id; opt.textContent = r.data.name;
      el.template.appendChild(opt);
      el.template.value = r.data.id;
      refreshUpdateBtn();
      setStatus("Saved template \u201c" + r.data.name + "\u201d.", "ok");
    }).catch(function () {
      el.save.disabled = false; setStatus("Network error.", "bad");
    });
  });

  el.update && el.update.addEventListener("click", function () {
    var id = parseInt(el.template.value, 10);
    if (!id) { setStatus("Select a template to update.", "bad"); return; }
    if (!state.rects.length) { setStatus("Draw at least one field box.", "bad"); return; }
    setStatus("Updating\u2026", "");
    el.update.disabled = true;
    postJSON(UPDATE_URL, {
      template_id: id, rects: rectsPayload(), field_pp: fieldPpPayload(),
      validation: validationPayload()
    }).then(function (r) {
      el.update.disabled = false;
      if (!r.ok) { setStatus((r.data && r.data.error) || "Update failed.", "bad"); return; }
      var t = TEMPLATES.filter(function (x) { return x.id === r.data.id; })[0];
      if (t) { t.rects = r.data.rects; t.validation = r.data.validation || null;
        t.field_pp = r.data.field_pp || {}; }
      setStatus("Updated template \u201c" + r.data.name + "\u201d.", "ok");
    }).catch(function () {
      el.update.disabled = false; setStatus("Network error.", "bad");
    });
  });

  // --- image-settings popup ------------------------------------------------
  var modalTarget = null;   // rects index, or "val"
  var refreshTimer = null;

  function modalRect(target) {
    var r = (target === "val") ? state.validation : state.rects[target];
    return [r.x0, r.y0, r.x1, r.y1];
  }
  function modalName(target) {
    return (target === "val") ? "validation" : state.rects[target].name;
  }
  function currentPp(target) {
    if (target === "val") return (state.validation && state.validation.pp) || {};
    var r = state.rects[target];
    return (r && state.pp[r.name]) || {};
  }

  function neutralFor(key) {
    if (key === "gamma" || key === "scale") return 1;
    if (key === "colorkeep_v") return 130;
    return 0;
  }

  function loadModalControls(pp) {
    md.controls.forEach(function (c) {
      var key = c.getAttribute("data-pp");
      if (c.type === "checkbox") {
        c.checked = !!pp[key];
      } else if (key === "threshold") {
        var v = pp.threshold;
        c.value = (v === undefined || v === null || v === "" || v === "off")
          ? "off" : (v === "auto" ? "auto" : "fixed");
      } else {
        c.value = (pp[key] !== undefined && pp[key] !== null)
          ? pp[key] : neutralFor(key);
      }
    });
    var thv = 128;
    if (typeof pp.threshold === "number") thv = pp.threshold;
    md.thval.value = thv;
    syncThresholdUI(); syncOutputs(); applyCssFilter();
  }

  function collectPp() {
    var pp = {};
    md.controls.forEach(function (c) {
      var key = c.getAttribute("data-pp");
      if (c.type === "checkbox") {
        if (c.checked) pp[key] = true;
      } else if (key === "threshold") {
        if (c.value === "auto") pp.threshold = "auto";
        else if (c.value === "fixed") pp.threshold = parseInt(md.thval.value, 10);
      } else {
        var num = parseFloat(c.value);
        if (!isNaN(num) && num !== neutralFor(key)) pp[key] = num;
      }
    });
    if (!pp.colorkeep) delete pp.colorkeep_v;   // level only applies with it
    return pp;
  }

  function syncOutputs() {
    root.querySelectorAll("[data-out]").forEach(function (s) {
      var key = s.getAttribute("data-out");
      if (key === "thval") { s.textContent = md.thval.value; return; }
      var c = root.querySelector('[data-pp="' + key + '"]');
      if (!c) return;
      if (key === "scale") s.textContent = parseFloat(c.value).toFixed(2) + "\u00d7";
      else if (key === "gamma") s.textContent = parseFloat(c.value).toFixed(2);
      else s.textContent = c.value;
    });
  }

  function syncThresholdUI() {
    var sel = root.querySelector('[data-pp="threshold"]');
    if (md.thvalWrap) md.thvalWrap.hidden = !(sel && sel.value === "fixed");
  }

  function applyCssFilter() {
    var pp = collectPp();
    var f = [];
    if (pp.brightness) f.push("brightness(" + (1 + pp.brightness / 100) + ")");
    if (pp.contrast) f.push("contrast(" + (1 + pp.contrast / 100) + ")");
    if (pp.grayscale || pp.removebg || pp.clahe || pp.autocontrast ||
        pp.morph || pp.colorkeep || pp.fft ||
        pp.threshold !== undefined) f.push("grayscale(1)");
    if (pp.removebg) f.push("brightness(1.08) contrast(1.15)");
    if (pp.invert) f.push("invert(1)");
    md.img.style.filter = f.join(" ");
  }

  function fetchPreview(runOcr) {
    if (modalTarget === null) return;
    md.img.style.filter = "";   // server returns the already-processed crop
    postJSON(PREVIEW_URL, {
      upload_id: state.uid, page_no: state.page,
      name: modalName(modalTarget), rect: modalRect(modalTarget),
      pp: collectPp(), ocr: !!runOcr
    }).then(function (r) {
      if (!r.ok || !r.data) return;
      if (r.data.crop) md.img.src = r.data.crop;
      if (runOcr && r.data.ocr) {
        var o = r.data.ocr;
        md.ocr.innerHTML = "Read: <span class='mono'>" + (o.value || "\u2014") +
          "</span> \u00b7 conf " +
          (o.confidence != null ? (o.confidence * 100).toFixed(0) + "%" : "\u2014") +
          (o.valid === true ? " <span class='badge badge-success'>valid</span>" :
            (o.valid === false ? " <span class='badge badge-warning'>check</span>" : ""));
      }
    });
  }

  function scheduleRefresh() {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () { fetchPreview(false); }, 450);
  }

  function openModal(target) {
    if (target === "val" && !state.validation) {
      setStatus("Set the validation box first.", "bad"); return;
    }
    modalTarget = target;
    md.title.textContent = "Image settings \u2014 " +
      (target === "val" ? "Validation"
        : (FIELD_LABEL[state.rects[target].name] || state.rects[target].name));
    md.ocr.innerHTML = "";
    loadModalControls(currentPp(target));
    md.modal.hidden = false;
    fetchPreview(false);
  }
  function closeModal() { md.modal.hidden = true; modalTarget = null; }

  md.controls.forEach(function (c) {
    var evt = (c.type === "range") ? "input" : "change";
    c.addEventListener(evt, function () {
      syncThresholdUI(); syncOutputs(); applyCssFilter(); scheduleRefresh();
    });
  });
  md.thval && md.thval.addEventListener("input", function () {
    syncOutputs(); scheduleRefresh();
  });
  md.closers.forEach(function (c) { c.addEventListener("click", closeModal); });
  md.test && md.test.addEventListener("click", function () { fetchPreview(true); });
  md.reset && md.reset.addEventListener("click", function () {
    loadModalControls({});
    md.ocr.innerHTML = "";
    fetchPreview(false);
  });
  md.apply && md.apply.addEventListener("click", function () {
    if (modalTarget === null) return;
    var pp = collectPp();
    if (modalTarget === "val") {
      if (state.validation) state.validation.pp = pp;
    } else {
      state.pp[state.rects[modalTarget].name] = pp;
    }
    closeModal();
    render();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && md.modal && !md.modal.hidden) closeModal();
  });

  // --- init ----------------------------------------------------------------
  loadPage();
  render();
  refreshUpdateBtn();
})();
