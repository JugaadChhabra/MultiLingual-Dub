/* Audio dubbing pane — the sound engineer's screen.
   Spreadsheet in, voiced clips out, uploaded to S3. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$;

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
                  jobId: null, targets: [] };
  const JOB_KEY = "autodub_audio_job";

  // ── language tokens ──────────────────────────────────────────────────────
  const SEL = U.langPicker({ tokens: els.langs, groups: els.groups, count: els.selN,
                             onChange: () => refreshRun() });

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
  function renderProgress(s, status) {
    U.jobProgress(els, s, status, state.targets.length || SEL.size);
  }

  // ── run ──────────────────────────────────────────────────────────────────
  els.run.addEventListener("click", start);
  U.bindJobCancel(els.cancel, () => state.jobId);

  async function start() {
    const why = blocker();
    if (why) { say(why, true); return; }
    say("");
    U.clearBanners(els.banners);
    // Not awaited: requestPermission() blocks on a browser prompt, and the run
    // must not wait on someone noticing it. Permission lands before the job ends.
    U.askNotify();

    state.running = true; state.targets = [...SEL];
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
    const end = await U.pollJob(id, (summary, status) => {
      renderProgress(summary, status);
      window.Shell.setRunning("audio", true, shortProgress(summary));
    });
    finish(end.status, end.summary, end.cause);
  }
  function shortProgress(s) {
    s = s || {};
    return s.total_rows ? `Audio ${s.rows_processed || 0}/${s.total_rows}` : "Audio running";
  }

  function finish(status, summary, cause) {
    state.running = false; state.jobId = null;
    try { localStorage.removeItem(JOB_KEY); } catch (_) {}
    els.live.hidden = true;
    U.resetJobCancel(els.cancel);
    window.Shell.setRunning("audio", false);
    if (summary) renderProgress(summary, status);
    refreshRun();
    U.jobEnded({ banners: els.banners, status, summary, cause, name: "Audio dubbing" });
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
      if (U.JOB_ENDED.includes(p.status)) { localStorage.removeItem(JOB_KEY); return; }
      state.running = true; state.jobId = id; state.targets = [...SEL];
      els.live.hidden = false; refreshRun();
      U.toast("Reattached to a run already in progress", { kind: "info",
        detail: "Nothing was lost when the page reloaded." });
      poll(id);
    } catch (_) {}
  }

  refreshRun(); reattach();
  window.PaneAudio = { refreshRun, isRunning: () => state.running };
})();
