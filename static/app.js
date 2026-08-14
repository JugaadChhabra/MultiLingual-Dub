/* AutoDub — front-end controller for the dithered-wave UI.
   Talks to the real backend:
     GET  /config/session-env/status      -> {configured, missing_keys[]}
     POST /batch/preview-excel  (file)     -> {rows: [[...],...]}
     POST /batch/excel-jobs     (FormData) -> {job_id}
     GET  /batch/excel-jobs/{id}           -> {job_id, status, summary, error}
     POST /batch/excel-jobs/{id}/cancel
     GET  /logs/important?since_id=&limit= -> {logs:[{timestamp,level,logger,message}], latest_id}
*/
(() => {
  "use strict";

  // ── helpers ────────────────────────────────
  const $ = (s) => document.querySelector(s);
  const reduce = matchMedia("(prefers-reduced-motion:reduce)").matches;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  async function fetchTimeout(url, opts = {}, ms = 25000) {
    const c = new AbortController(); const t = setTimeout(() => c.abort(), ms);
    try { return await fetch(url, { ...opts, signal: c.signal }); } finally { clearTimeout(t); }
  }
  async function safeErr(resp, fallback = "Request failed.") {
    try { const d = await resp.json(); return d.detail || d.error || d.message || fallback; } catch (_) { return fallback; }
  }

  // ── shared background + sound (static/wave.js, static/audio.js) ──
  const wave = AutoDubWave.create($("#wave"), "linear");
  const RAMP = AutoDubWave.RAMP;
  const sfx = AutoDubAudio.create({ enabled: false });
  const ping = (pos, c, s, dur) => wave.ping(c, s, dur, pos);
  const cssHue = (code) => { const h = HUE[code]; return h ? `rgb(${h[0]},${h[1]},${h[2]})` : "var(--accent)"; };

  // ── language data (display only; codes drive the API) ──
  const INDIAN = [["bn-IN","বাংলা","Bengali"],["en-IN","English","English"],["gu-IN","ગુજરાતી","Gujarati"],["hi-IN","हिन्दी","Hindi"],["kn-IN","ಕನ್ನಡ","Kannada"],["ml-IN","മലയാളം","Malayalam"],["mr-IN","मराठी","Marathi"],["od-IN","ଓଡ଼ିଆ","Odia"],["pa-IN","ਪੰਜਾਬੀ","Punjabi"],["ta-IN","தமிழ்","Tamil"],["te-IN","తెలుగు","Telugu"]];
  const INTL = [["fr","Français","French"],["de","Deutsch","German"],["es","Español","Spanish"],["ru","Русский","Russian"],["pt","Português","Portuguese"]];
  const ALL = [...INDIAN, ...INTL];
  const SEL = new Set(["hi-IN"]);   // default selection
  const HUE = {}, POSX = {};

  // ── DOM refs ───────────────────────────────
  const eq = $("#eq"), cfgEl = $("#cfg"), dotled = $("#dot"), sessTxt = $("#sessTxt");
  const drop = $("#drop"), fileInput = $("#file"), dropIc = $("#dropIc"), dropMain = $("#dropMain"), dropSub = $("#dropSub"), dropClear = $("#dropClear");
  const teach = $("#teach"), append = $("#append");
  const mosaic = $("#mosaic"), selN = $("#selN");
  const runBtn = $("#run"), echoEl = $("#runEcho"), runMsg = $("#runMsg");
  const cancelBtn = $("#cancel");
  const feedN = $("#feedN"), logN = $("#logN");
  const sumEmpty = $("#sumEmpty"), sumData = $("#sumData"), resList = $("#resList"), feed = $("#feed"), logEl = $("#logs");

  // ── mosaic build (English ↔ native morph) ──
  function grp(t) { const g = document.createElement("div"); g.className = "grp"; g.innerHTML = `${t}<span class="r"></span>`; return g; }
  function tile([code, nat, en], idx) {
    const el = document.createElement("button"); el.type = "button"; el.dataset.code = code;
    el.className = "tile";
    // aria-pressed is both the a11y contract and the styling hook — one source of truth
    el.setAttribute("aria-pressed", SEL.has(code) ? "true" : "false");
    el.setAttribute("aria-label", `${en} (${code})`);
    const rgb = RAMP[Math.floor(idx/(ALL.length-1)*95)];
    HUE[code] = rgb; POSX[code] = 0.1 + 0.8 * idx/(ALL.length-1);
    el.innerHTML = `<span class="dots" aria-hidden="true"></span><span class="code" aria-hidden="true">${code}</span><span class="morph" aria-hidden="true"><span class="en">${en}</span><span class="nat">${nat}</span></span>`;
    el.addEventListener("mouseenter", () => ping(POSX[code], rgb, 0.4, 650));
    el.addEventListener("click", () => {
      const on = el.getAttribute("aria-pressed") !== "true";
      el.setAttribute("aria-pressed", on ? "true" : "false");
      on ? SEL.add(code) : SEL.delete(code);
      ping(POSX[code], rgb, on ? 1 : 0.5, 900); if (on) note(idx);
      syncSel();   // re-derives the group toggles from the actual selection
    });
    return el;
  }
  // group labels name the translation provider: Sarvam for Indian locales,
  // Google Translate for the rest. TTS is ElevenLabs for both.
  mosaic.appendChild(grp("indian · sarvam"));
  INDIAN.forEach((l, i) => mosaic.appendChild(tile(l, i)));
  mosaic.appendChild(grp("international · google"));
  INTL.forEach((l, i) => mosaic.appendChild(tile(l, INDIAN.length + i)));
  // `selN` reads "n / 16"; the run column no longer repeats the same count, so the
  // echo line carries the job endpoint instead of a duplicate target tally.
  // Each group button is a toggle: on adds its languages, off removes them. That
  // subsumes what the separate `clear` button did (deselect everything = turn the
  // active groups off), so there is no fourth destructive button in the row.
  const GROUPS = { all: ALL, indian: INDIAN, intl: INTL };
  function groupIsOn(codes) { return codes.length > 0 && codes.every((l) => SEL.has(l[0])); }
  function syncSel() {
    selN.textContent = `${SEL.size} / ${ALL.length}`;
    document.querySelectorAll(".qs button").forEach((b) => {
      b.setAttribute("aria-pressed", groupIsOn(GROUPS[b.dataset.q] || []) ? "true" : "false");
    });
    refreshRunState();
  }
  document.querySelectorAll(".qs button").forEach((b) => b.addEventListener("click", () => {
    const codes = GROUPS[b.dataset.q] || [];
    const turnOff = groupIsOn(codes);
    codes.forEach((l) => (turnOff ? SEL.delete(l[0]) : SEL.add(l[0])));
    document.querySelectorAll(".tile").forEach((t) => t.setAttribute("aria-pressed", SEL.has(t.dataset.code) ? "true" : "false"));
    syncSel();
    if (!turnOff) codes.forEach((l, i) => setTimeout(() => ping(POSX[l[0]], HUE[l[0]], 0.7, 800), i * 55));
  }));

  // ── mode switches (role="switch"; aria-checked drives the visual) ──
  // The separate "OFF"/"ON" caption is gone — the switch itself already says that,
  // and the accessible state lives on aria-checked.
  const isOn = (btn) => Boolean(btn) && btn.getAttribute("aria-checked") === "true";
  [teach, append].forEach((btn) => {
    if (!btn) return;
    btn.addEventListener("click", () => btn.setAttribute("aria-checked", isOn(btn) ? "false" : "true"));
  });

  // ── tabs ───────────────────────────────────
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.setAttribute("aria-selected", String(x === t)));
    document.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== t.dataset.tab; });
  }));
  const showTab = (tab) => $(`.tab[data-tab="${tab}"]`).click();

  // ── idle: selected locales softly sing through the wave ──
  setInterval(() => {
    if (reduce || running) return;
    const codes = [...SEL];
    if (codes.length && Math.random() < 0.75) { const c = codes[Math.floor(Math.random()*codes.length)]; ping(POSX[c], HUE[c], 0.5, 1200); }
    else ping(Math.random()*0.7+0.15, RAMP[Math.floor(Math.random()*96)], 0.32, 1400);
  }, 1500);

  // ── optional sound ─────────────────────────
  const snd = $("#snd");
  if (snd) snd.addEventListener("click", () => {
    const on = !sfx.enabled();
    sfx.setEnabled(on);
    snd.classList.toggle("on", on); snd.setAttribute("aria-pressed", String(on));
    snd.textContent = on ? "♪ on" : "♪ off";
    if (on) note(4);
  });
  const note = (i) => sfx.note(i);
  const chord = () => sfx.chord();

  // ── config status ──────────────────────────
  // The required-key list comes from the server, which derives it from the
  // settings classes. Keeping a copy here is how the old counter drifted out of
  // sync with what the backend actually reads.
  // One function owns the LED, the label and the config count, so the three can
  // never disagree. The old catch block left a mint "session ready" on screen
  // while the status call was failing — the indicator lied exactly when it mattered.
  let envConfigured = false;
  function setSession(state, label, tip) {
    envConfigured = state === "ok";
    if (dotled) { dotled.className = "dotled" + (state === "ok" ? "" : state === "unknown" ? " bad" : " warn"); dotled.title = tip || label; }
    if (sessTxt) sessTxt.textContent = label;
    refreshRunState();
  }
  async function refreshEnvStatus() {
    try {
      const r = await fetch("/config/session-env/status");
      if (!r.ok) throw new Error("status " + r.status);
      const p = await r.json();
      const missing = Array.isArray(p.missing_keys) ? p.missing_keys : [];
      const required = Array.isArray(p.required_keys) ? p.required_keys : [];
      const total = required.length;
      cfgEl.textContent = total ? `${total - missing.length}/${total}` : "—";
      if (p.configured) setSession("ok", "session ready", "runtime config ready");
      else setSession("incomplete", "config incomplete", "missing: " + missing.join(", "));
    } catch (_) {
      cfgEl.textContent = "—";
      setSession("unknown", "config unreachable", "could not read /config/session-env/status");
    }
  }
  // kicked off from boot() at the bottom — setSession() touches run state, which
  // must not be reachable before the run-state bindings below are initialised

  // ── file drop + preview ────────────────────
  let selectedFile = null;
  drop.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) onFile(fileInput.files[0]); });
  ["dragover", "dragenter"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (ev) => { const f = ev.dataTransfer.files[0]; if (f) onFile(f); });
  dropClear.addEventListener("click", clearFile);

  // batch/excel.py requires these four headers. /batch/preview-excel doesn't check
  // them — it just dumps the first rows — so a wrong sheet used to read as "ready"
  // and only blow up after the operator committed the run. Check it here, at drop.
  const REQUIRED_HEADERS = ["voiceover_text", "emotion", "activity_name", "voiceover_title"];
  let sheetProblem = null, sheetRows = 0;

  async function onFile(file) {
    if (!file.name.toLowerCase().endsWith(".xlsx")) { dropSub.textContent = "only .xlsx allowed"; return; }
    selectedFile = file; sheetProblem = null; sheetRows = 0;
    drop.classList.add("loaded"); dropIc.textContent = "✓"; dropClear.hidden = false;
    dropMain.textContent = file.name; dropSub.textContent = `${(file.size/1024).toFixed(0)} KB · reading…`;
    refreshRunState();
    try {
      const fd = new FormData(); fd.append("file", file);
      const r = await fetchTimeout("/batch/preview-excel", { method: "POST", body: fd }, 10000);
      if (!r.ok) throw new Error("preview failed");
      const rows = (await r.json()).rows || [];
      const headers = (rows[0] || []).map((h) => String(h).trim().toLowerCase());
      const missing = REQUIRED_HEADERS.filter((h) => !headers.includes(h));
      if (missing.length) {
        sheetProblem = `sheet is missing column${missing.length > 1 ? "s" : ""}: ${missing.join(", ")}`;
        drop.classList.remove("loaded"); dropIc.textContent = "!";
        dropSub.textContent = sheetProblem;
      } else {
        sheetRows = Math.max(0, rows.length - 1);
        dropSub.textContent = `${sheetRows} row${sheetRows === 1 ? "" : "s"} · headers ok`;
        ping(0.5, [255,181,112], 0.9, 900);
      }
    } catch (_) {
      dropSub.textContent = `${(file.size/1024).toFixed(0)} KB · could not read the sheet`;
    }
    refreshRunState();
  }
  function clearFile() {
    selectedFile = null; sheetProblem = null; sheetRows = 0;
    drop.classList.remove("loaded"); dropIc.textContent = "▤";
    dropMain.textContent = "Drop .xlsx or click to browse";
    dropSub.textContent = "columns: voiceover_text, emotion, activity_name, voiceover_title";
    dropClear.hidden = true; fileInput.value = ""; drop.focus(); refreshRunState();
  }

  // ── logs polling ───────────────────────────
  let lastLogId = 0, logTimer = null;
  async function primeLog() { try { const r = await fetchTimeout("/logs/important?since_id=0&limit=1"); if (!r.ok) return; const p = await r.json(); if (typeof p.latest_id === "number") lastLogId = p.latest_id; } catch (_) {} }
  async function pullLogs() {
    try {
      const r = await fetchTimeout(`/logs/important?since_id=${lastLogId}&limit=200`);
      if (!r.ok) return; const p = await r.json();
      const logs = Array.isArray(p.logs) ? p.logs : []; logs.forEach(addLogLine);
      if (typeof p.latest_id === "number") lastLogId = Math.max(lastLogId, p.latest_id);
      if (logs.length) logEl.scrollTop = logEl.scrollHeight;
    } catch (_) {}
  }
  function addLogLine(item) {
    const ts = item.timestamp ? new Date(item.timestamp).toLocaleTimeString("en-GB", { hour12: false }) : "--:--:--";
    const lvl = (item.level || "INFO").toUpperCase();
    const msg = (item.logger ? item.logger + " — " : "") + (item.message || "");
    const cls = lvl === "ERROR" ? "er" : "mut";
    const pfx = lvl === "ERROR" ? "✗" : lvl === "WARNING" ? "!" : "·";
    const row = document.createElement("div"); row.className = "row";
    row.innerHTML = `<span class="t">${ts}</span><span class="p ${lvl === "ERROR" ? "er" : ""}">${pfx}</span><span class="${cls}">${escapeHtml(msg)}</span>`;
    logEl.appendChild(row); logN.textContent = logEl.children.length;
    while (logEl.children.length > 400) logEl.removeChild(logEl.firstChild);
  }
  function startLogs() { stopLogs(); lastLogId = 0; logEl.innerHTML = ""; logN.textContent = 0; primeLog().then(pullLogs); logTimer = setInterval(pullLogs, 2000); }
  function stopLogs() { if (logTimer) { clearInterval(logTimer); logTimer = null; } }

  // ── run / poll / render ────────────────────
  let running = false, currentJobId = null, isCancelling = false, currentTargets = [], pingTimer = null;
  function setRunLabel(text, mode) { runBtn.childNodes[0].textContent = text + " "; const ret = runBtn.querySelector(".ret"); if (ret) ret.textContent = mode === "run" ? "⏎" : ""; }
  function setM(k, v) { const el = $(`[data-m="${k}"]`); if (el) el.textContent = v; }

  // Errors and blocking reasons live in a persistent aria-live line, not inside the
  // button label. Overwriting the label hid the CTA's own name, vanished before a
  // screen reader could read it, and put the message far from the field at fault.
  function say(msg, isErr) { runMsg.textContent = msg || ""; runMsg.classList.toggle("err", Boolean(isErr)); }

  // What blocks a run, in the order the operator should fix it. Returns null when ready.
  function blockingReason() {
    if (!selectedFile) return "drop an .xlsx to enable the run";
    if (sheetProblem) return sheetProblem;
    if (SEL.size === 0) return "pick at least one target language";
    if (!envConfigured) return "runtime config is not ready";
    return null;
  }
  function refreshRunState() {
    if (running) { runBtn.disabled = true; cancelBtn.hidden = false; return; }
    cancelBtn.hidden = true;
    const why = blockingReason();
    runBtn.disabled = Boolean(why);
    if (!runMsg.classList.contains("err")) say(why || "");
    // what this run will actually produce, instead of the endpoint name
    if (sheetRows > 0 && SEL.size > 0) {
      echoEl.innerHTML = `<b>${sheetRows}</b> rows × <b>${SEL.size}</b> targets = <b>${sheetRows * SEL.size}</b> clips`;
    } else {
      echoEl.textContent = "Ready when you are";
    }
  }

  function buildResRows(targets) {
    resList.innerHTML = "";
    targets.forEach((code) => {
      const l = ALL.find((x) => x[0] === code);
      const r = document.createElement("div"); r.className = "resrow"; r.dataset.code = code;
      r.innerHTML = `<span class="rc">${code}</span><span class="rn">${l ? l[1] : code}</span><span class="bar"><i style="background:${cssHue(code)}"></i></span><span class="st" style="color:var(--faint)">queued</span>`;
      resList.appendChild(r);
    });
  }
  function renderSummary(s, status) {
    s = s || {};
    sumEmpty.hidden = true; sumData.hidden = false;
    setM("rows", s.total_rows || 0);
    setM("targets", currentTargets.length || SEL.size);
    setM("done", s.rows_succeeded || 0);
    setM("failed", s.rows_failed || 0);
    const total = s.total_rows || 0, proc = s.rows_processed || 0, frac = total ? proc/total : 0;
    document.querySelectorAll("#resList .resrow").forEach((row) => {
      const bar = row.querySelector("i"), st = row.querySelector(".st");
      if (status === "completed") { if (bar) bar.style.width = "100%"; st.textContent = "done"; st.style.color = "var(--ok)"; }
      else if (status === "failed" || status === "cancelled") { st.textContent = status === "failed" ? "failed" : "cancelled"; st.style.color = "var(--err)"; }
      else { if (bar) bar.style.width = (frac*100).toFixed(0) + "%"; st.textContent = "running"; st.style.color = "var(--muted)"; }
    });
  }
  const feedSeen = new Set();
  function clearFeed() { feed.innerHTML = ""; feedSeen.clear(); feedN.textContent = 0; }
  // The feed used to restate the summary ("processed across 11 locales"). It now
  // carries what the summary can't: when each row landed and how long it took.
  let lastRowAt = 0;
  function updateFeed(s) {
    s = s || {}; const proc = s.rows_processed || 0, total = s.total_rows || 0;
    for (let i = 1; i <= Math.min(proc, total); i++) {
      const id = "r" + i; if (feedSeen.has(id)) continue; feedSeen.add(id);
      const now = performance.now();
      const took = lastRowAt ? `${((now - lastRowAt)/1000).toFixed(1)}s` : "—";
      lastRowAt = now;
      const clock = new Date().toLocaleTimeString("en-GB", { hour12: false });
      const el = document.createElement("div"); el.className = "fr";
      el.innerHTML = `<span class="fc">row ${i}</span>`
        + `<span class="ft">${currentTargets.length} clips · ${clock}</span>`
        + `<span class="fs" style="color:var(--faint)">${took}</span>`;
      feed.appendChild(el);
    }
    feedN.textContent = feedSeen.size; feed.scrollTop = feed.scrollHeight;
  }

  runBtn.addEventListener("click", startRun);
  // Cancelling used to be a click on the same button that displayed progress, so
  // reflexively clicking a progress readout killed the job. Now it is a separate
  // control that arms first — two deliberate clicks, no blocking modal.
  let cancelArmed = null;
  cancelBtn.addEventListener("click", () => {
    if (!running || isCancelling) return;
    if (cancelArmed) { clearTimeout(cancelArmed); cancelArmed = null; cancelJob(); return; }
    cancelBtn.textContent = "Click again to cancel";
    cancelBtn.classList.add("armed");
    cancelArmed = setTimeout(() => {
      cancelArmed = null; cancelBtn.textContent = "Cancel run"; cancelBtn.classList.remove("armed");
    }, 4000);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); if (!running) startRun(); } });

  async function startRun() {
    const why = blockingReason();
    if (why) { say(why, true); refreshRunState(); return; }
    say("");

    running = true; isCancelling = false; currentTargets = [...SEL]; lastRowAt = 0;
    setRunLabel("Running…", ""); refreshRunState();
    // Summary is the run view — it carries the metrics and the per-language rows.
    // Jumping to the log firehose on submit buried the thing the operator watches.
    wave.setEnergy(1); if (eq) eq.classList.add("run"); showTab("summary");
    buildResRows(currentTargets);
    sumEmpty.hidden = true; sumData.hidden = false; setM("rows", 0); setM("targets", currentTargets.length); setM("done", 0); setM("failed", 0);
    clearFeed(); startLogs();
    pingTimer = setInterval(() => { const c = currentTargets[Math.floor(Math.random()*currentTargets.length)]; ping(POSX[c], HUE[c], 0.8, 800); }, 700);

    try {
      const fd = new FormData();
      fd.append("file", selectedFile);
      fd.append("max_language_parallelism", "3");
      if (isOn(teach)) fd.append("teaching_mode", "true");
      fd.append("mode", isOn(append) ? "append" : "create");
      currentTargets.forEach((l) => fd.append("target_languages", l));

      const cr = await fetch("/batch/excel-jobs", { method: "POST", body: fd });
      if (!cr.ok) { finishRun("failed", null, await safeErr(cr, "Failed to create batch job.")); return; }
      const cp = await cr.json();
      currentJobId = cp.job_id;
      rememberJob(currentJobId);
      const final = await poll(currentJobId);
      finishRun(final.status, final.summary, final.error || "");
    } catch (e) { finishRun("failed", null, String((e && e.message) || e)); }
  }

  // ── surviving a reload ──
  // The job persists server-side; the id used to live only in this closure, so a
  // refresh mid-run left the operator with no way to see whether it was still going.
  const JOB_KEY = "autodub_active_batch_job";
  function rememberJob(id) { try { localStorage.setItem(JOB_KEY, id); } catch (_) {} }
  function forgetJob() { try { localStorage.removeItem(JOB_KEY); } catch (_) {} }
  async function reattach() {
    let id = null;
    try { id = localStorage.getItem(JOB_KEY); } catch (_) {}
    if (!id) return;
    try {
      const r = await fetchTimeout(`/batch/excel-jobs/${id}`);
      if (!r.ok) { forgetJob(); return; }
      const p = await r.json();
      if (["completed", "failed", "cancelled"].includes(p.status)) { forgetJob(); return; }
      running = true; isCancelling = false; currentJobId = id; lastRowAt = 0;
      currentTargets = [...SEL];
      setRunLabel("Running…", ""); refreshRunState();
      wave.setEnergy(1); if (eq) eq.classList.add("run"); showTab("summary");
      buildResRows(currentTargets);
      sumEmpty.hidden = true; sumData.hidden = false;
      clearFeed(); startLogs();
      addLogLine({ level: "INFO", message: `reattached to job ${id}` });
      const final = await poll(id);
      finishRun(final.status, final.summary, final.error || "");
    } catch (_) { /* offline — keep the record for the next load */ }
  }

  async function poll(id) {
    const terminal = new Set(["completed", "failed", "cancelled"]); let notFound = 0;
    while (true) {
      try {
        const r = await fetchTimeout(`/batch/excel-jobs/${id}`);
        if (r.status === 404) { if (++notFound >= 3) return { job_id: id, status: "failed", error: "Job not found repeatedly (server may have restarted).", summary: null }; await wait(2000); continue; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        const p = await r.json(); notFound = 0;
        renderSummary(p.summary, p.status); updateFeed(p.summary);
        const s = p.summary || {}, proc = s.rows_processed || 0, tot = s.total_rows || 0;
        // progress reads out below the button; the button itself stays labelled
        if (tot > 0) {
          wave.setPlayhead(Math.min(1, proc/tot));
          if (!isCancelling) echoEl.innerHTML = `<b>${proc}</b> of <b>${tot}</b> rows <span class="jid">${escapeHtml(id)}</span>`;
        }
        if (terminal.has(p.status)) return p;
        await wait(2000);
      } catch (_) { await wait(2000); }
    }
  }

  function finishRun(status, summary, err) {
    running = false; currentJobId = null; if (pingTimer) clearInterval(pingTimer);
    forgetJob();
    wave.setEnergy(0); wave.setPlayhead(-1); if (eq) eq.classList.remove("run");
    setRunLabel("Run pipeline", "run");
    cancelBtn.textContent = "Cancel run"; cancelBtn.classList.remove("armed"); cancelBtn.disabled = false;
    stopLogs(); pullLogs();
    if (summary) renderSummary(summary, status);
    if (err) addLogLine({ level: "ERROR", message: err });
    say(err ? err : status === "completed" ? "" : `job ${status}`, Boolean(err) || status === "failed");
    refreshRunState();
    if (status === "completed") chord();
  }

  async function cancelJob() {
    if (!currentJobId) return;
    isCancelling = true;
    cancelBtn.textContent = "Cancelling…"; cancelBtn.disabled = true; cancelBtn.classList.remove("armed");
    echoEl.textContent = "Cancelling — finishing the row in flight";
    try { await fetchTimeout(`/batch/excel-jobs/${currentJobId}/cancel`, { method: "POST" }); } catch (_) {}
  }

  // ── boot ───────────────────────────────────
  syncSel();
  refreshEnvStatus();
  setInterval(refreshEnvStatus, 20000);
  reattach();
})();
