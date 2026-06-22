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

  // ── iridescent ramp ────────────────────────
  const STOPS = [[94,234,212],[125,211,252],[196,181,253],[253,230,138],[252,165,165],[240,171,252],[255,255,255]];
  const RAMP = [];
  for (let i = 0; i < 96; i++) { const f = i/95*(STOPS.length-1), a = Math.floor(f), b = Math.min(a+1, STOPS.length-1), t = f-a; RAMP.push(STOPS[a].map((c,k)=>Math.round(c+(STOPS[b][k]-c)*t))); }
  const cssHue = (code) => { const h = HUE[code]; return h ? `rgb(${h[0]},${h[1]},${h[2]})` : "var(--accent)"; };

  // ── language data (display only; codes drive the API) ──
  const INDIAN = [["bn-IN","বাংলা","Bengali"],["en-IN","English","English"],["gu-IN","ગુજરાતી","Gujarati"],["hi-IN","हिन्दी","Hindi"],["kn-IN","ಕನ್ನಡ","Kannada"],["ml-IN","മലയാളം","Malayalam"],["mr-IN","मराठी","Marathi"],["od-IN","ଓଡ଼ିଆ","Odia"],["pa-IN","ਪੰਜਾਬੀ","Punjabi"],["ta-IN","தமிழ்","Tamil"],["te-IN","తెలుగు","Telugu"]];
  const INTL = [["fr","Français","French"],["de","Deutsch","German"],["es","Español","Spanish"],["ru","Русский","Russian"],["pt","Português","Portuguese"]];
  const ALL = [...INDIAN, ...INTL];
  const SEL = new Set(["hi-IN"]);   // default selection
  const HUE = {}, POSX = {};

  // ── DOM refs ───────────────────────────────
  const eq = $("#eq"), cfgEl = $("#cfg"), dotled = $(".dotled"), sessSeg = $(".hstat .seg");
  const drop = $("#drop"), fileInput = $("#file"), dropIc = $("#dropIc"), dropMain = $("#dropMain"), dropSub = $("#dropSub");
  const teach = $("#teach"), teachSt = $("#teachSt");
  const append = $("#append"), appendSt = $("#appendSt");
  const mosaic = $("#mosaic"), selN = $("#selN"), echoN = $("#echoN");
  const runBtn = $("#run"), echoEl = $(".run-echo");
  const feedN = $("#feedN"), logN = $("#logN");
  const sumEmpty = $("#sumEmpty"), sumData = $("#sumData"), resList = $("#resList"), feed = $("#feed"), logEl = $("#logs");

  // ── mosaic build (English ↔ native morph) ──
  function grp(t) { const g = document.createElement("div"); g.className = "grp"; g.innerHTML = `${t}<span class="r"></span>`; return g; }
  function tile([code, nat, en], idx) {
    const el = document.createElement("button"); el.type = "button"; el.dataset.code = code;
    el.className = "tile" + (SEL.has(code) ? " on" : "");
    const rgb = RAMP[Math.floor(idx/(ALL.length-1)*95)];
    HUE[code] = rgb; POSX[code] = 0.1 + 0.8 * idx/(ALL.length-1);
    el.style.setProperty("--tile-hue", `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`);
    el.innerHTML = `<span class="dots"></span><span class="code">${code}</span><span class="morph"><span class="en">${en}</span><span class="nat">${nat}</span></span>`;
    el.addEventListener("mouseenter", () => ping(POSX[code], rgb, 0.4, 650));
    el.addEventListener("click", () => {
      const on = !el.classList.contains("on"); el.classList.toggle("on");
      on ? SEL.add(code) : SEL.delete(code);
      ping(POSX[code], rgb, on ? 1 : 0.5, 900); if (on) note(idx); syncSel();
    });
    return el;
  }
  mosaic.appendChild(grp("indian · sarvam"));
  INDIAN.forEach((l, i) => mosaic.appendChild(tile(l, i)));
  mosaic.appendChild(grp("international · in-process"));
  INTL.forEach((l, i) => mosaic.appendChild(tile(l, INDIAN.length + i)));
  function syncSel() { const n = SEL.size; selN.textContent = `${n} / 16`; echoN.textContent = `${n} target${n === 1 ? "" : "s"}`; }
  document.querySelectorAll(".qs button").forEach((b) => b.addEventListener("click", () => {
    const q = b.dataset.q; SEL.clear();
    if (q === "all") ALL.forEach((l) => SEL.add(l[0]));
    else if (q === "indian") INDIAN.forEach((l) => SEL.add(l[0]));
    else if (q === "intl") INTL.forEach((l) => SEL.add(l[0]));
    document.querySelectorAll(".tile").forEach((t) => t.classList.toggle("on", SEL.has(t.dataset.code)));
    syncSel();
    [...SEL].forEach((code, i) => setTimeout(() => ping(POSX[code], HUE[code], 0.7, 800), i * 55));
  }));

  // ── teaching toggle ────────────────────────
  teach.addEventListener("click", () => { teach.classList.toggle("on"); teachSt.textContent = teach.classList.contains("on") ? "on" : "off"; });
  if (append) append.addEventListener("click", () => { append.classList.toggle("on"); appendSt.textContent = append.classList.contains("on") ? "on" : "off"; });

  // ── tabs ───────────────────────────────────
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("on")); t.classList.add("on");
    document.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== t.dataset.tab; });
  }));
  const showTab = (tab) => $(`.tab[data-tab="${tab}"]`).click();

  // ── dithered wave background ───────────────
  const cv = $("#wave"), ctx = cv.getContext("2d");
  let W = 0, H = 0, DPR = 1, energy = 0, energyTarget = 0, playhead = -1; const CELL = 9;
  const pulses = [];
  function ping(x, c, s = 1, dur = 900) { if (reduce) return; pulses.push({ x, c, s, t0: performance.now(), dur }); if (pulses.length > 48) pulses.shift(); }
  function resize() { const r = cv.getBoundingClientRect(); DPR = Math.min(devicePixelRatio || 1, 2); W = r.width; H = r.height; cv.width = W*DPR; cv.height = H*DPR; ctx.setTransform(DPR,0,0,DPR,0,0); }
  addEventListener("resize", resize);
  const BAYER = [[0,8,2,10],[12,4,14,6],[3,11,1,9],[15,7,13,5]].map((r) => r.map((v) => v/16));
  function field(x, t, e) { const nx = x/W; let w = Math.sin(nx*9+t*1.1)*0.5 + Math.sin(nx*23-t*0.7)*0.28 + Math.sin(nx*41+t*1.9)*0.16; w = Math.abs(w); const base = 0.10 + 0.05*Math.sin(nx*5-t*0.5); return base + w*(0.30+0.66*e); }
  function frame(ts) {
    const t = ts/1000; energy += (energyTarget - energy) * 0.05;
    for (let i = pulses.length-1; i >= 0; i--) { if ((ts - pulses[i].t0)/pulses[i].dur >= 1) pulses.splice(i, 1); }
    ctx.clearRect(0, 0, W, H);
    const mid = H*0.30;
    for (let cx = 0; cx < W; cx += CELL) {
      const x = cx + CELL/2, nx = x/W;
      let pAdd = 0, pc = null, pcw = 0;
      for (const p of pulses) { const age = (ts-p.t0)/p.dur, env = Math.sin(age*Math.PI), reach = 0.05+age*0.14, d = Math.abs(nx-p.x), g = Math.max(0,1-d/reach), amt = g*env*p.s; if (amt > 0) { pAdd += amt; if (amt > pcw) { pcw = amt; pc = p.c; } } }
      const amp = (field(x, t, energy) + pAdd*0.55) * H*0.32;
      let sweep = 0; if (playhead >= 0) { const d = Math.abs(nx-playhead); sweep = Math.max(0,1-d*7); }
      const idleSweep = Math.max(0, 1-Math.abs(nx-(((t*0.06)%1.3)-0.15))*9) * 0.18 * (1-energy);
      for (let cy = 0; cy < H; cy += CELL) {
        const y = cy + CELL/2, dist = Math.abs(y-mid);
        let I = 1 - dist/amp; if (I <= 0) continue;
        I = Math.pow(I, 0.7)*(0.5+0.5*energy) + sweep*0.5 + idleSweep + pAdd*0.45;
        const th = BAYER[(cx/CELL|0)%4][(cy/CELL|0)%4];
        if (I < th*0.9) continue;
        const ci = Math.min(95, Math.floor((nx*0.7 + I*0.5 + energy*0.15)*95)), base = RAMP[ci];
        let c = base;
        if (pc && pcw > 0.22) { const m = Math.min(1, pcw); c = [base[0]+(pc[0]-base[0])*m|0, base[1]+(pc[1]-base[1])*m|0, base[2]+(pc[2]-base[2])*m|0]; }
        const a = Math.min(0.72, (0.08 + I*0.36)*(1+energy*0.7));
        ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${a})`;
        const s = Math.max(1, 1.3 + I*2.1);
        ctx.fillRect(x-s, y-0.7, s*2, 1.4); ctx.fillRect(x-0.7, y-s, 1.4, s*2);
      }
    }
    if (!reduce) requestAnimationFrame(frame);
  }
  function startWave() { resize(); requestAnimationFrame(frame); }
  if (document.fonts) document.fonts.ready.then(resize);
  startWave();
  // idle: selected locales softly sing
  setInterval(() => {
    if (reduce || running) return;
    const codes = [...SEL];
    if (codes.length && Math.random() < 0.75) { const c = codes[Math.floor(Math.random()*codes.length)]; ping(POSX[c], HUE[c], 0.5, 1200); }
    else ping(Math.random()*0.7+0.15, RAMP[Math.floor(Math.random()*96)], 0.32, 1400);
  }, 1500);

  // ── optional sound ─────────────────────────
  let audioOn = false, actx = null;
  const snd = $("#snd");
  if (snd) snd.addEventListener("click", () => {
    audioOn = !audioOn; snd.classList.toggle("on", audioOn); snd.textContent = audioOn ? "♪ on" : "♪ off";
    if (audioOn) { try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); actx.resume(); note(4); } catch (e) {} }
  });
  const PENTA = [261.63,293.66,329.63,392.0,440.0,523.25,587.33,659.25,783.99,880.0,1046.5];
  function tone(freq, dur, type, gain) { if (!audioOn) return; try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); const o = actx.createOscillator(), g = actx.createGain(); o.type = type; o.frequency.value = freq; o.connect(g); g.connect(actx.destination); const now = actx.currentTime; g.gain.setValueAtTime(gain, now); g.gain.exponentialRampToValueAtTime(0.0001, now+dur); o.start(now); o.stop(now+dur); } catch (e) {} }
  function note(i) { tone(PENTA[i % PENTA.length], 0.17, "sine", 0.05); }
  function chord() { [0,2,4].forEach((n, k) => setTimeout(() => tone(PENTA[n+4], 0.55, "sine", 0.04), k*95)); }

  // ── config status ──────────────────────────
  const REQUIRED_ENV_KEYS = ["SARVAM_API_KEY","GEMINI_API_KEY","ELEVEN_LABS","DESI_VOCAL_VOICE","AWS_ACCESS_KEY","AWS_SECRET_KEY","AWS_BUCKET","AWS_REGION","AWS_ENDPOINT_URL","BATCH_ENABLE_S3_UPLOAD","BATCH_ENABLE_QC","GEMINI_QC_MODELS"];
  let envConfigured = false;
  async function refreshEnvStatus() {
    try {
      const r = await fetch("/config/session-env/status");
      if (!r.ok) throw new Error("status " + r.status);
      const p = await r.json();
      const missing = Array.isArray(p.missing_keys) ? p.missing_keys : [];
      const total = REQUIRED_ENV_KEYS.length;
      cfgEl.textContent = `${total - missing.length}/${total}`;
      envConfigured = Boolean(p.configured);
      if (dotled) { dotled.style.background = envConfigured ? "var(--ok)" : "var(--accent)"; dotled.style.boxShadow = `0 0 7px ${envConfigured ? "var(--ok)" : "var(--accent)"}`; dotled.title = envConfigured ? "runtime config ready" : "missing: " + missing.join(", "); }
      if (sessSeg) sessSeg.lastChild.textContent = envConfigured ? " session ready" : " config incomplete";
    } catch (_) { cfgEl.textContent = "—"; envConfigured = false; }
  }
  refreshEnvStatus();
  setInterval(refreshEnvStatus, 20000);

  // ── file drop + preview ────────────────────
  let selectedFile = null;
  drop.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) onFile(fileInput.files[0]); });
  ["dragover", "dragenter"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (ev) => { const f = ev.dataTransfer.files[0]; if (f) onFile(f); });
  async function onFile(file) {
    if (!file.name.toLowerCase().endsWith(".xlsx")) { dropSub.textContent = "only .xlsx allowed"; return; }
    selectedFile = file;
    drop.classList.add("loaded"); dropIc.textContent = "✓";
    dropMain.textContent = file.name; dropSub.textContent = `${(file.size/1024).toFixed(0)} KB · reading…`;
    if (!drop.querySelector(".x")) { const x = document.createElement("button"); x.className = "x"; x.textContent = "×"; x.title = "remove"; x.addEventListener("click", (ev) => { ev.stopPropagation(); clearFile(); }); drop.appendChild(x); }
    try {
      const fd = new FormData(); fd.append("file", file);
      const r = await fetchTimeout("/batch/preview-excel", { method: "POST", body: fd }, 10000);
      if (r.ok) { const d = await r.json(); const rows = d.rows || []; if (rows.length) { const cols = (rows[0] || []).length; dropSub.textContent = `${Math.max(0, rows.length-1)} rows × ${cols} cols · ready`; ping(0.5, [255,181,112], 0.9, 900); return; } }
      dropSub.textContent = `${(file.size/1024).toFixed(0)} KB · ready`;
    } catch (_) { dropSub.textContent = `${(file.size/1024).toFixed(0)} KB · ready`; }
  }
  function clearFile() { selectedFile = null; drop.classList.remove("loaded"); dropIc.textContent = "▤"; dropMain.textContent = "Drop .xlsx or click to browse"; dropSub.textContent = "source rows + target columns"; const x = drop.querySelector(".x"); if (x) x.remove(); fileInput.value = ""; }

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
  function setRunLabel(text, mode) { runBtn.childNodes[0].textContent = text + " "; const ret = runBtn.querySelector(".ret"); if (ret) ret.textContent = mode === "run" ? "⏎" : mode === "cancel" ? "✕" : ""; }
  function flash(msg) { setRunLabel(msg, ""); setTimeout(() => { if (!running) setRunLabel("Run pipeline", "run"); }, 1300); }
  function setM(k, v) { const el = $(`[data-m="${k}"]`); if (el) el.textContent = v; }

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
  function updateFeed(s) {
    s = s || {}; const proc = s.rows_processed || 0, total = s.total_rows || 0;
    for (let i = 1; i <= Math.min(proc, total); i++) {
      const id = "r" + i; if (feedSeen.has(id)) continue; feedSeen.add(id);
      const el = document.createElement("div"); el.className = "fr";
      el.innerHTML = `<span class="fc">row ${i}</span><span class="ft">processed across ${currentTargets.length} locales</span><span class="fs" style="color:var(--ok)">done</span>`;
      feed.appendChild(el);
    }
    feedN.textContent = feedSeen.size; feed.scrollTop = feed.scrollHeight;
  }

  runBtn.addEventListener("click", () => { if (running) cancelJob(); else startRun(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); if (!running) startRun(); } });

  async function startRun() {
    if (SEL.size === 0) { flash("Pick a target first"); return; }
    if (!selectedFile) { flash("Drop an .xlsx first"); return; }
    if (!envConfigured) { flash("Config not ready"); return; }

    running = true; isCancelling = false; currentTargets = [...SEL];
    setRunLabel("Running…", "cancel"); energyTarget = 1; if (eq) eq.classList.add("run"); showTab("logs");
    buildResRows(currentTargets);
    sumEmpty.hidden = true; sumData.hidden = false; setM("rows", 0); setM("targets", currentTargets.length); setM("done", 0); setM("failed", 0);
    clearFeed(); startLogs();
    pingTimer = setInterval(() => { const c = currentTargets[Math.floor(Math.random()*currentTargets.length)]; ping(POSX[c], HUE[c], 0.8, 800); }, 700);

    try {
      const fd = new FormData();
      fd.append("file", selectedFile);
      fd.append("max_language_parallelism", "3");
      if (teach.classList.contains("on")) fd.append("teaching_mode", "true");
      fd.append("mode", append && append.classList.contains("on") ? "append" : "create");
      currentTargets.forEach((l) => fd.append("target_languages", l));

      const cr = await fetch("/batch/excel-jobs", { method: "POST", body: fd });
      if (!cr.ok) { finishRun("failed", null, await safeErr(cr, "Failed to create batch job.")); return; }
      const cp = await cr.json();
      currentJobId = cp.job_id;
      echoEl.innerHTML = `<span class="p">$</span> job ${escapeHtml(currentJobId || "")}`;
      const final = await poll(currentJobId);
      finishRun(final.status, final.summary, final.error || "");
    } catch (e) { finishRun("failed", null, String((e && e.message) || e)); }
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
        if (tot > 0) { playhead = Math.min(1, proc/tot); if (!isCancelling) setRunLabel(`${proc}/${tot} rows`, "cancel"); }
        if (terminal.has(p.status)) return p;
        await wait(2000);
      } catch (_) { await wait(2000); }
    }
  }

  function finishRun(status, summary, err) {
    running = false; currentJobId = null; if (pingTimer) clearInterval(pingTimer);
    energyTarget = 0; playhead = -1; if (eq) eq.classList.remove("run");
    setRunLabel("Run pipeline", "run");
    // echoEl.innerHTML = `<span class="p">$</span> POST /batch/excel-jobs`;
    stopLogs(); pullLogs();
    if (summary) renderSummary(summary, status);
    if (err) addLogLine({ level: "ERROR", message: err });
    if (status === "completed") chord();
  }

  async function cancelJob() {
    if (!currentJobId) return;
    isCancelling = true; setRunLabel("Cancelling…", "");
    try { await fetchTimeout(`/batch/excel-jobs/${currentJobId}/cancel`, { method: "POST" }); } catch (_) {}
  }

  syncSel();
})();
