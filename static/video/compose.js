/* Video pane — what a run is made of: mode, voice, the single script and its
   title, and the spreadsheet a batch reads from. */
(() => {
  "use strict";
  const U = window.UI, V = window.Video;
  const { els, state } = V;
  const { MODE, CHAR } = V.keys;

  // ── mode, voice, script, title ───────────────────────────────────────────
  U.bindSwitch(els.overlay);
  U.bindSwitch(els.music);
  U.bindSeg(els.mode, ({ mode }) => applyMode(mode));
  function applyMode(mode) {
    state.mode = mode;
    try { localStorage.setItem(MODE, mode); } catch (_) {}
    els.mode.querySelectorAll("button").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    els.auto.hidden = mode !== "auto";
    els.single.hidden = mode !== "single";
    els.batch.hidden = mode !== "batch";
    // The work column holds whatever this mode produces: drafts in Auto, a queue
    // in Batch. Single produces one script, which lives in the controls, so the
    // column has nothing to show and goes away rather than sitting empty.
    els.draftsWrap.hidden = mode !== "auto";
    els.queueWrap.hidden = mode !== "batch";
    els.work.hidden = mode === "single";
    // The layout has to know, so it can collapse to one column rather than
    // leaving an empty half beside the controls.
    if (els.inner) els.inner.dataset.work = mode === "single" ? "off" : "on";
    V.refreshRunLabel();
    if (mode === "auto") V.refreshGen();
    V.refreshRun();
  }
  U.bindSeg(els.voice, ({ char }) => {
    state.character = char;
    try { localStorage.setItem(CHAR, char); } catch (_) {}
  });
  els.script.addEventListener("input", () => {
    els.count.textContent = `${els.script.value.length} / 1000`; V.refreshRun();
  });
  els.title.addEventListener("input", () => V.refreshRun());
  // the title becomes the NAS filename, so it has to be filesystem-safe
  const safeTitle = () => els.title.value.trim().replace(/[\\/:*?"<>|]+/g, "_").replace(/\.mp4$/i, "");
  // The date is no longer defaulted: it prints on the video card and files the
  // render, so it must be chosen deliberately rather than silently assumed today.
  const dateSet = () => {
    if (els.date.value) return true;
    U.toast("Set a publish date first", { kind: "error", id: "no-date",
      detail: "The date appears on the video card and names the NAS folder, so it can't default to today anymore." });
    return false;
  };

  U.dropzone(els.drop, els.xls, onExcel);
  async function onExcel(file) {
    if (!file.name.toLowerCase().endsWith(".xlsx")) {
      els.drop.dataset.state = "bad"; els.dropSub.textContent = "Only .xlsx files are accepted"; return;
    }
    state.excel = file; state.excelProblem = null;
    els.drop.classList.remove("empty"); els.drop.dataset.state = "ok";
    els.drop.querySelector(".ic").innerHTML = U.icon("check");
    els.dropTitle.textContent = file.name; els.dropSub.textContent = "Reading…";
    V.refreshRun();
    try {
      const fd = new FormData(); fd.append("file", file);
      const r = await U.fetchT("/batch/preview-excel", { method: "POST", body: fd }, 15000);
      if (!r.ok) throw new Error("preview failed");
      const rows = (await r.json()).rows || [];
      const headers = (rows[0] || []).map((h) => String(h).trim().toLowerCase());
      if (!headers.includes("script")) {
        state.excelProblem = "Missing column: script";
        els.drop.dataset.state = "bad"; els.drop.querySelector(".ic").innerHTML = U.icon("warning");
        els.dropSub.textContent = state.excelProblem;
      } else {
        els.dropSub.textContent = `${Math.max(0, rows.length - 1)} rows · headers ok`;
      }
    } catch (_) { els.dropSub.textContent = `${U.kb(file.size)} · could not read the sheet`; }
    V.refreshRun();
  }

  Object.assign(V, { applyMode, safeTitle, dateSet });
})();
