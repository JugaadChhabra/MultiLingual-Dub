/* Audio dubbing pane — the sound engineer's screen.
   Spreadsheet in, voiced clips out, uploaded to S3. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$;

  // display only; the codes drive the API
  const INDIAN = [["bn-IN","বাংলা","Bengali"],["en-IN","English","English"],["gu-IN","ગુજરાતી","Gujarati"],
    ["hi-IN","हिन्दी","Hindi"],["kn-IN","ಕನ್ನಡ","Kannada"],["ml-IN","മലയാളം","Malayalam"],
    ["mr-IN","मराठी","Marathi"],["od-IN","ଓଡ଼ିଆ","Odia"],["pa-IN","ਪੰਜਾਬੀ","Punjabi"],
    ["ta-IN","தமிழ்","Tamil"],["te-IN","తెలుగు","Telugu"]];
  const INTL = [["fr","Français","French"],["de","Deutsch","German"],["es","Español","Spanish"],
    ["ru","Русский","Russian"],["pt","Português","Portuguese"]];
  const ALL = [...INDIAN, ...INTL];
  const GROUPS = { all: ALL, indian: INDIAN, intl: INTL };
  const SEL = new Set(["hi-IN"]);

  // batch/excel.py requires these four; checking at drop time means a wrong
  // sheet is caught before the operator commits the run
  const REQUIRED = ["voiceover_text", "emotion", "activity_name", "voiceover_title"];

  const els = {
    banners: $("#audioBanners"), progress: $("#audioProgress"),
    clips: $("#apClips"), detail: $("#apDetail"), started: $("#apStarted"),
    up: $("#apUp"), done: $("#apDone"), bad: $("#apBad"),
    upN: $("#apUpN"), doneN: $("#apDoneN"), badN: $("#apBadN"), queueN: $("#apQueueN"),
    drop: $("#aDrop"), file: $("#aFile"), dropTitle: $("#aDropTitle"), dropSub: $("#aDropSub"),
    teach: $("#aTeach"), append: $("#aAppend"), langs: $("#aLangs"), selN: $("#aSelN"),
    groups: $("#aGroups"), run: $("#aRun"), cancel: $("#aCancel"), msg: $("#aMsg"),
    live: $("#audioLive"),
  };

  const state = { file: null, rows: 0, sheetProblem: null, running: false,
                  jobId: null, cancelling: false, targets: [] };
  const JOB_KEY = "autodub_audio_job";

  // ── language tokens ──────────────────────────────────────────────────────
  function renderLangs() {
    els.langs.innerHTML = "";
    ALL.forEach(([code, native, english]) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "token"; b.dataset.code = code;
      b.setAttribute("aria-pressed", String(SEL.has(code)));
      b.setAttribute("aria-label", `${english} (${code})`);
      // English until it is picked, then it morphs into the native script —
      // the two share one grid cell so the pill never changes width
      b.innerHTML = `<span class="morph"><span class="en">${U.esc(english)}</span>` +
        `<span class="nat">${U.esc(native)}</span></span>`;
      b.addEventListener("click", () => {
        SEL.has(code) ? SEL.delete(code) : SEL.add(code);
        b.setAttribute("aria-pressed", String(SEL.has(code)));
        syncSel();
      });
      els.langs.appendChild(b);
    });
  }
  const groupOn = (list) => list.length > 0 && list.every(([c]) => SEL.has(c));
  function syncSel() {
    els.selN.textContent = `· ${SEL.size} of ${ALL.length}`;
    els.groups.querySelectorAll("button").forEach((b) =>
      b.setAttribute("aria-pressed", String(groupOn(GROUPS[b.dataset.group] || []))));
    refreshRun();
  }
  // group buttons toggle: turning one off is what a separate "clear" used to do
  els.groups.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const list = GROUPS[b.dataset.group] || [];
    const off = groupOn(list);
    list.forEach(([c]) => (off ? SEL.delete(c) : SEL.add(c)));
    els.langs.querySelectorAll(".token").forEach((t) =>
      t.setAttribute("aria-pressed", String(SEL.has(t.dataset.code))));
    syncSel();
  });

  U.bindSwitch(els.teach); U.bindSwitch(els.append);

  // ── source sheet ─────────────────────────────────────────────────────────
  U.dropzone(els.drop, els.file, onFile);
  async function onFile(file) {
    if (!file.name.toLowerCase().endsWith(".xlsx")) {
      setDrop("bad", file.name, "Only .xlsx files are accepted"); return;
    }
    state.file = file; state.sheetProblem = null; state.rows = 0;
    setDrop("ok", file.name, "Reading…");
    refreshRun();
    try {
      const fd = new FormData(); fd.append("file", file);
      const r = await U.fetchT("/batch/preview-excel", { method: "POST", body: fd }, 15000);
      if (!r.ok) throw new Error("preview failed");
      const rows = (await r.json()).rows || [];
      const headers = (rows[0] || []).map((h) => String(h).trim().toLowerCase());
      const missing = REQUIRED.filter((h) => !headers.includes(h));
      if (missing.length) {
        state.sheetProblem = `Missing column${missing.length > 1 ? "s" : ""}: ${missing.join(", ")}`;
        setDrop("bad", file.name, state.sheetProblem);
      } else {
        state.rows = Math.max(0, rows.length - 1);
        setDrop("ok", file.name, `${state.rows} rows · headers ok`);
      }
    } catch (_) {
      setDrop("ok", file.name, `${U.kb(file.size)} · could not read the sheet`);
    }
    refreshRun();
  }
  function setDrop(kind, title, sub) {
    els.drop.dataset.state = kind;
    els.drop.classList.toggle("empty", kind === "empty");
    els.dropTitle.textContent = title;
    els.dropSub.textContent = sub;
    els.drop.querySelector(".ic").textContent = kind === "ok" ? "✓" : kind === "bad" ? "!" : "▤";
  }

  // ── what blocks a run, in the order it should be fixed ───────────────────
  function blocker() {
    if (!state.file) return "Drop a spreadsheet to start";
    if (state.sheetProblem) return state.sheetProblem;
    if (!SEL.size) return "Pick at least one target language";
    if (!window.Shell.configured()) return "Runtime config is not ready";
    return null;
  }
  function refreshRun() {
    if (state.running) { els.run.disabled = true; els.cancel.hidden = false; return; }
    els.cancel.hidden = true;
    const why = blocker();
    els.run.disabled = Boolean(why);
    els.run.textContent = "Run pipeline";
    if (!els.msg.classList.contains("err")) {
      els.msg.textContent = why || (state.rows && SEL.size
        ? `${state.rows} rows × ${SEL.size} languages = ${state.rows * SEL.size} clips`
        : "");
    }
  }
  const say = (text, bad) => { els.msg.textContent = text || ""; els.msg.classList.toggle("err", !!bad); };

  // ── progress ─────────────────────────────────────────────────────────────
  // Everything here comes from fields JobSummary already collected; the old UI
  // showed four of them and threw the rest away.
  function renderProgress(s, status) {
    s = s || {};
    els.progress.hidden = false;
    const total = s.language_tasks_total || (s.total_rows || 0) * (state.targets.length || SEL.size) || 0;
    const uploaded = s.uploads_succeeded || 0;
    const succeeded = s.language_tasks_succeeded || 0;
    const failed = (s.language_tasks_failed || 0) + (s.uploads_failed || 0);
    // succeeded includes uploaded, so the middle band is the difference
    const rendered = Math.max(0, succeeded - uploaded);

    els.clips.innerHTML = `${uploaded + rendered} <small>of ${total} clips</small>`;
    els.upN.textContent = uploaded;
    els.doneN.textContent = rendered;
    els.badN.textContent = failed;
    els.queueN.textContent = Math.max(0, total - uploaded - rendered - failed);
    U.setBar(els, { total, uploaded, rendered, failed, live: status === "running" });

    const rows = s.total_rows || 0, processed = s.rows_processed || 0;
    const left = status === "running" ? U.eta(s.started_at, processed, rows) : null;
    els.detail.textContent = status === "running"
      ? [`Row ${processed} of ${rows}`, left].filter(Boolean).join(" · ")
      : `Finished ${rows} rows`;
    els.started.textContent = s.started_at ? `Started ${U.clock(s.started_at)}` : "";
  }

  // ── run ──────────────────────────────────────────────────────────────────
  els.run.addEventListener("click", start);
  els.cancel.addEventListener("click", () => {
    U.confirm({
      title: "Cancel this run?",
      message: "Rows already finished stay uploaded. The row in flight completes first.",
      confirmLabel: "Cancel run", cancelLabel: "Keep running",
      onOk: async () => {
        state.cancelling = true;
        els.cancel.disabled = true; els.cancel.textContent = "Cancelling…";
        try { await U.fetchT(`/batch/excel-jobs/${state.jobId}/cancel`, { method: "POST" }); } catch (_) {}
      },
    });
  });

  async function start() {
    const why = blocker();
    if (why) { say(why, true); return; }
    say("");
    U.clearBanners(els.banners);
    // Not awaited: requestPermission() blocks on a browser prompt, and the run
    // must not wait on someone noticing it. Permission lands before the job ends.
    U.askNotify();

    state.running = true; state.cancelling = false; state.targets = [...SEL];
    els.live.hidden = false; refreshRun();
    window.Shell.setRunning("audio", true);

    try {
      const fd = new FormData();
      fd.append("file", state.file);
      fd.append("max_language_parallelism", "3");
      if (U.isOn(els.teach)) fd.append("teaching_mode", "true");
      fd.append("mode", U.isOn(els.append) ? "append" : "create");
      state.targets.forEach((l) => fd.append("target_languages", l));

      const r = await fetch("/batch/excel-jobs", { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
      const { job_id } = await r.json();
      state.jobId = job_id;
      try { localStorage.setItem(JOB_KEY, job_id); } catch (_) {}
      await poll(job_id);
    } catch (e) {
      U.banner(els.banners, { kind: "error", title: "Could not start the run",
        detail: "The request never reached the server.", raw: String(e && e.message || e) });
      finish("failed", null, null);
    }
  }

  async function poll(id) {
    let notFound = 0;
    while (true) {
      try {
        const r = await U.fetchT(`/batch/excel-jobs/${id}`);
        if (r.status === 404) {
          if (++notFound >= 3) { finish("failed", null, null); return; }
          await new Promise((res) => setTimeout(res, 2000)); continue;
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        const p = await r.json(); notFound = 0;
        renderProgress(p.summary, p.status);
        window.Shell.setRunning("audio", true, shortProgress(p.summary));
        if (["completed", "failed", "cancelled"].includes(p.status)) {
          finish(p.status, p.summary, p.cause); return;
        }
      } catch (_) { /* keep polling through a blip */ }
      await new Promise((res) => setTimeout(res, 2000));
    }
  }
  function shortProgress(s) {
    s = s || {};
    return s.total_rows ? `Audio ${s.rows_processed || 0}/${s.total_rows}` : "Audio running";
  }

  function finish(status, summary, cause) {
    state.running = false; state.jobId = null;
    try { localStorage.removeItem(JOB_KEY); } catch (_) {}
    els.live.hidden = true;
    els.cancel.disabled = false; els.cancel.textContent = "Cancel run";
    window.Shell.setRunning("audio", false);
    if (summary) renderProgress(summary, status);
    refreshRun();

    const s = summary || {};
    const uploaded = s.uploads_succeeded || 0;
    const failed = (s.language_tasks_failed || 0) + (s.uploads_failed || 0);

    // the classified cause, if the backend produced one
    if (cause) window.Shell.showCause(els.banners, cause);

    if (status === "completed" && !failed) {
      U.toast(`All ${uploaded} clips uploaded`, { kind: "ok",
        detail: `Finished ${U.clock(new Date().toISOString())}.` });
      U.notify("Audio dubbing finished", `${uploaded} clips uploaded to S3`, false);
    } else if (status === "cancelled") {
      U.toast("Run cancelled", { kind: "info",
        detail: `${uploaded} clips finished and stayed uploaded.` });
      U.notify("Audio dubbing cancelled", `${uploaded} clips were uploaded before stopping`, true);
    } else {
      const head = cause ? cause.title : "The run did not finish";
      U.notify(`Audio dubbing finished with ${failed} failures`, `${head} · ${uploaded} uploaded`, true);
      if (!cause) {
        U.banner(els.banners, { kind: "error", title: "The run did not finish",
          detail: "No cause was reported by the server." });
      }
    }
  }

  // ── survive a reload ─────────────────────────────────────────────────────
  async function reattach() {
    let id = null;
    try { id = localStorage.getItem(JOB_KEY); } catch (_) {}
    if (!id) return;
    try {
      const r = await U.fetchT(`/batch/excel-jobs/${id}`);
      if (!r.ok) { localStorage.removeItem(JOB_KEY); return; }
      const p = await r.json();
      if (["completed", "failed", "cancelled"].includes(p.status)) { localStorage.removeItem(JOB_KEY); return; }
      state.running = true; state.jobId = id; state.targets = [...SEL];
      els.live.hidden = false; refreshRun();
      U.toast("Reattached to a run already in progress", { kind: "info",
        detail: "Nothing was lost when the page reloaded." });
      poll(id);
    } catch (_) {}
  }

  renderLangs(); syncSel(); reattach();
  window.PaneAudio = { refreshRun, isRunning: () => state.running };
})();
