/* AutoDub — shared UI primitives.
   Both panes build from these, so a banner, an alert or a progress bar behaves
   the same wherever it appears. Spec: docs/component-sheet.html */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ── fetch with a deadline ───────────────────────────────────────────────
  async function fetchT(url, opts = {}, ms = 25000) {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), ms);
    try { return await fetch(url, { ...opts, signal: c.signal }); }
    finally { clearTimeout(t); }
  }

  // ── banners ─────────────────────────────────────────────────────────────
  // A banner always states a cause, an impact and a way out. `raw` is the
  // provider's own words, kept behind a disclosure so it is available for
  // debugging without being the thing the operator reads first.
  const ICONS = { error: "!", warn: "!", info: "i", ok: "✓" };
  function banner(host, { kind = "error", title, detail, raw, actions = [], id }) {
    const el = document.createElement("div");
    el.className = "banner" + (kind === "error" ? "" : " " + kind);
    if (id) el.dataset.id = id;
    el.innerHTML =
      `<span class="ic" aria-hidden="true">${ICONS[kind] || "!"}</span>` +
      `<span class="tx"><div class="h">${esc(title)}</div>` +
      (detail ? `<div class="d">${detail}</div>` : "") +
      (raw ? `<details class="raw"><summary>Show what the server said</summary><pre>${esc(raw)}</pre></details>` : "") +
      `</span><span class="a"></span>`;
    const slot = el.querySelector(".a");
    actions.forEach((a) => {
      const b = document.createElement(a.href ? "a" : "button");
      b.className = "btn" + (a.primary ? " p" : a.quiet ? " q" : "");
      b.textContent = a.label;
      if (a.href) { b.href = a.href; b.target = "_blank"; b.rel = "noopener"; }
      else { b.type = "button"; b.addEventListener("click", () => a.onClick && a.onClick(el)); }
      slot.appendChild(b);
    });
    // one banner per id — a repeated failure updates rather than stacking
    if (id) { const old = host.querySelector(`.banner[data-id="${id}"]`); if (old) old.remove(); }
    host.appendChild(el);
    return el;
  }
  const clearBanners = (host, kind) => {
    host.querySelectorAll(kind ? `.banner.${kind}` : ".banner").forEach((b) => b.remove());
  };

  // ── toasts: events, which are over ──────────────────────────────────────
  // A banner sits in the flow and stays until something clears it, which is
  // right for a condition the operator is still in. It is wrong for a thing
  // that merely happened: those accumulate above the work and push it down the
  // page. Anything carrying actions or a server response stays a banner — a
  // notice you might need to click must not time out from under you.
  const TOAST_MS = 4500, TOAST_MS_DETAIL = 7000, TOAST_MAX = 4;
  // CSS.escape is not in older WebKit; the fallback only has to survive being
  // put inside an attribute selector.
  const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&"));
  let toastHost = null;
  function toasts() {
    if (!toastHost || !toastHost.isConnected) {
      toastHost = document.querySelector(".toasts");
      if (!toastHost) {
        toastHost = document.createElement("div");
        toastHost.className = "toasts";
        // polite, not assertive: a finished render should not interrupt a
        // screen reader mid-sentence.
        toastHost.setAttribute("role", "status");
        toastHost.setAttribute("aria-live", "polite");
        document.body.appendChild(toastHost);
      }
    }
    return toastHost;
  }
  function toast(title, { detail, kind = "info", ms, id } = {}) {
    const host = toasts();
    const life = ms ?? (detail ? TOAST_MS_DETAIL : TOAST_MS);

    // A repeat refreshes the toast that is already there rather than swapping in
    // a new node. Replacing it destroyed whatever the operator was pointing at
    // or had focused — the click landed on the queue row underneath, and a
    // keyboard user lost their tab position to document.body.
    if (id) {
      const live = host.querySelector(`.toast[data-id="${cssEscape(id)}"]:not([data-going])`);
      if (live) { live.querySelector(".h").textContent = title;
        const d = live.querySelector(".d"); if (d && detail) d.innerHTML = detail;
        live.restart(life); return live; }
    }

    // A div, not a button: the close button nests inside, and a button inside a
    // button is invalid and does not receive its own clicks. The body stays
    // click-to-dismiss for the mouse; the close button is the affordance that
    // is focusable and reachable by keyboard.
    const el = document.createElement("div");
    el.className = "toast " + kind;
    if (id) el.dataset.id = id;
    el.innerHTML =
      `<span class="ic" aria-hidden="true">${ICONS[kind] || "i"}</span>` +
      `<span class="tx"><span class="h">${esc(title)}</span>` +
      (detail ? `<span class="d">${detail}</span>` : "") + `</span>` +
      `<button type="button" class="iconbtn x" aria-label="Dismiss">✕</button>`;
    host.appendChild(el);
    // Two frames: adding the class in the same frame as the insert can skip
    // the transition entirely, since no style recalc separates them.
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("in")));

    // Oldest first, so a burst does not build a column taller than the window.
    // Departing toasts linger in the DOM for a frame or two and must not count,
    // or a burst drops a live one while only three are visible.
    [...host.querySelectorAll(".toast:not([data-going])")]
      .slice(0, -TOAST_MAX).forEach((o) => dismiss(o));

    let timer = setTimeout(() => dismiss(el), life);
    el.restart = (next) => { clearTimeout(timer); timer = setTimeout(() => dismiss(el), next); };
    // Reading it should not race the clock.
    const hold = () => clearTimeout(timer);
    const resume = () => { timer = setTimeout(() => dismiss(el), TOAST_MS); };
    el.addEventListener("mouseenter", hold);
    el.addEventListener("mouseleave", resume);
    // focus/blur do not bubble; focusin/focusout do, and the focusable child is
    // the close button, so the toast has to listen for the bubbling pair or a
    // keyboard user would watch it vanish mid-read.
    el.addEventListener("focusin", hold);
    el.addEventListener("focusout", resume);
    el.addEventListener("click", () => { clearTimeout(timer); dismiss(el); });
    return el;
  }

  // Escape clears the toasts — but only when nothing else owns the key. An open
  // alert or drawer traps Escape for its own close, and stealing it there would
  // dismiss notices the operator cannot even see behind the scrim.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    if (document.querySelector(".alert.open,.drawer.open")) return;
    const host = document.querySelector(".toasts");
    const live = host ? [...host.querySelectorAll(".toast:not([data-going])")] : [];
    if (!live.length) return;
    e.preventDefault();
    live.forEach((el) => dismiss(el));
  });
  function dismiss(el, now) {
    if (!el || el.dataset.going) return;
    el.dataset.going = "1";
    if (now) { el.remove(); return; }
    el.classList.add("out");
    el.addEventListener("transitionend", () => el.remove(), { once: true });
    // transitionend does not fire if the element is already hidden.
    setTimeout(() => el.remove(), 400);
  }

  // ── overlays: inert when closed, focus trapped when open ────────────────
  function focusables(panel) {
    return [...panel.querySelectorAll('button,input,textarea,select,a[href],[tabindex]:not([tabindex="-1"])')]
      .filter((el) => !el.disabled && el.offsetParent !== null);
  }
  function overlay(panel, scrim, opts = {}) {
    let lastFocus = null;
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); api.close(); return; }
      if (e.key !== "Tab") return;
      const f = focusables(panel);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    const api = {
      open() {
        lastFocus = document.activeElement;
        panel.removeAttribute("inert");
        panel.classList.add("open"); scrim.classList.add("open");
        panel.addEventListener("keydown", onKey);
        setTimeout(() => { const f = focusables(panel); if (f.length) (opts.focus ? opts.focus() : f[0]).focus(); }, 60);
      },
      close() {
        if (opts.beforeClose && opts.beforeClose() === false) return;
        panel.classList.remove("open"); scrim.classList.remove("open");
        panel.removeEventListener("keydown", onKey);
        panel.setAttribute("inert", "");
        // the trigger can be gone if a re-render replaced it, hence the fallback
        if (lastFocus && document.contains(lastFocus) && lastFocus.offsetParent !== null) lastFocus.focus();
        else if (opts.fallbackFocus) { const f = opts.fallbackFocus(); if (f) f.focus(); }
        lastFocus = null;
      },
      isOpen: () => panel.classList.contains("open"),
    };
    scrim.addEventListener("click", api.close);
    return api;
  }

  // ── alert ───────────────────────────────────────────────────────────────
  // Focus lands on the safe button, so Return never destroys anything.
  const alertEl = $("#alert"), alertScrim = $("#alertScrim");
  const alertOk = $("#alertOk"), alertCancel = $("#alertCancel");
  let onConfirm = null;
  const alertOv = overlay(alertEl, alertScrim, { focus: () => alertCancel });
  function confirm({ title, message, confirmLabel = "OK", cancelLabel = "Cancel", danger = true, onOk }) {
    $("#alertTitle").textContent = title;
    $("#alertMsg").textContent = message;
    alertOk.textContent = confirmLabel;
    alertCancel.textContent = cancelLabel;
    alertOk.className = "btn " + (danger ? "d" : "p");
    onConfirm = onOk;
    alertOv.open();
  }
  alertCancel.addEventListener("click", () => alertOv.close());
  alertOk.addEventListener("click", () => { const fn = onConfirm; alertOv.close(); if (fn) fn(); });

  // ── progress ────────────────────────────────────────────────────────────
  // Three states the old UI conflated: uploaded and safe, rendered but still
  // uploading, failed. Widths are of the whole, so they sum to <= 100.
  function setBar(els, { total, uploaded, rendered, failed, live = true }) {
    const t = Math.max(1, total || 0);
    const pct = (n) => (Math.max(0, n || 0) / t * 100).toFixed(2) + "%";
    els.up.style.width = pct(uploaded);
    els.done.style.width = pct(rendered);
    els.bad.style.width = pct(failed);
    els.done.classList.toggle("live", Boolean(live) && (rendered || 0) > 0);
  }

  // Honest remaining time: measured rate over the run so far, never a guess
  // before there is enough signal to make one.
  function eta(startedAt, doneUnits, totalUnits) {
    if (!startedAt || !doneUnits || doneUnits < 2 || doneUnits >= totalUnits) return null;
    const elapsed = (Date.now() - new Date(startedAt).getTime()) / 1000;
    if (!(elapsed > 0)) return null;
    const remaining = (totalUnits - doneUnits) * (elapsed / doneUnits);
    if (!isFinite(remaining) || remaining <= 0) return null;
    if (remaining < 90) return "less than 2 minutes left";
    const m = Math.round(remaining / 60);
    return m < 60 ? `about ${m} minutes left` : `about ${(remaining / 3600).toFixed(1)} hours left`;
  }
  const clock = (iso) => {
    try { return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); }
    catch (_) { return ""; }
  };

  // ── notification + sound ────────────────────────────────────────────────
  // Permission is requested on the first run rather than at page load, so the
  // prompt arrives attached to an action the operator just took.
  let actx = null;
  function chime(bad) {
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      const notes = bad ? [392.0, 311.13] : [523.25, 659.25, 783.99];
      notes.forEach((f, i) => setTimeout(() => {
        const o = actx.createOscillator(), g = actx.createGain();
        o.type = "sine"; o.frequency.value = f;
        o.connect(g); g.connect(actx.destination);
        const now = actx.currentTime;
        g.gain.setValueAtTime(0.075, now);
        g.gain.exponentialRampToValueAtTime(0.0001, now + 0.5);
        o.start(now); o.stop(now + 0.5);
      }, i * 130));
    } catch (_) {}
  }
  async function askNotify() {
    if (!("Notification" in window)) return false;
    if (Notification.permission === "granted") return true;
    if (Notification.permission === "denied") return false;
    try { return (await Notification.requestPermission()) === "granted"; } catch (_) { return false; }
  }
  function notify(title, body, bad) {
    chime(bad);
    try {
      if ("Notification" in window && Notification.permission === "granted") {
        const n = new Notification(title, { body, tag: "autodub", renotify: true });
        n.onclick = () => { window.focus(); n.close(); };
      }
    } catch (_) {}
  }

  // ── small helpers ───────────────────────────────────────────────────────
  const kb = (b) => `${(b / 1024).toFixed(0)} KB`;
  function bindSwitch(btn, onChange) {
    btn.addEventListener("click", () => {
      const on = btn.getAttribute("aria-checked") !== "true";
      btn.setAttribute("aria-checked", String(on));
      if (onChange) onChange(on);
    });
  }
  const isOn = (btn) => btn.getAttribute("aria-checked") === "true";
  function bindSeg(seg, onPick) {
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      [...seg.querySelectorAll("button")].forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      onPick(b.dataset);
    });
  }

  // A dropzone that is a real button, so it works from the keyboard.
  function dropzone(btn, input, onFile) {
    btn.addEventListener("click", () => input.click());
    input.addEventListener("change", () => { if (input.files[0]) onFile(input.files[0]); });
    ["dragover", "dragenter"].forEach((e) =>
      btn.addEventListener(e, (ev) => { ev.preventDefault(); btn.classList.add("over"); }));
    ["dragleave", "drop"].forEach((e) =>
      btn.addEventListener(e, (ev) => { ev.preventDefault(); btn.classList.remove("over"); }));
    btn.addEventListener("drop", (ev) => { const f = ev.dataTransfer.files[0]; if (f) onFile(f); });
  }

  window.UI = { $, esc, fetchT, banner, clearBanners, toast, overlay, confirm, setBar, eta, clock,
                notify, askNotify, chime, kb, bindSwitch, isOn, bindSeg, dropzone };
})();
