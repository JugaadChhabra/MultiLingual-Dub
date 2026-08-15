/* Video pane — the video editor's screen.
   An avatar and a script in, rendered video out, filed to the NAS. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$;

  // Shipped preset. localStorage holds only the operator's changes on top of
  // this, so editing here ships new copy immediately unless they edited it too.
  const SEED = [{
    id: "sunsign", label: "Horoscope",
    video_prompt: "The character is speaking directly to the camera in a calm, soothing, and divine tone, delivering an astrological and spiritual message as if guided by a higher power. Visuals: Soft cinematic lighting with a warm, divine glow on the face. Movements: Natural, graceful human-like head movements, including gentle nods of affirmation and subtle lateral head tilts. Expressions: A serene, compassionate, and divine smile with micro-expressions of wisdom. Eyes: Soft, warm, and prolonged eye contact with the camera, natural blinking, and occasional soft gaze shifts to avoid a dead stare. Lip-Sync & Jaw: Extremely accurate Hindi lip-sync with realistic mouth shape, jaw tension, and natural teeth visibility matching the phonemes. Body Language: Add a rhythmic, visible breathing pattern for a natural life-like presence, and incorporate slow, subtle, open-palm hand gestures to convey peace and avoid stiffness.",
    motion_prompt: "A serene spiritual guide delivering a divine message. Movement style: Graceful, slow, and highly empathetic. Maintain a gentle, compassionate smile throughout with micro-expressions of wisdom. Head movements should be fluid—incorporating soft lateral tilts, slow meditative nods of affirmation, and a subtle, rhythmic breathing pattern. Eyes should remain warm and focused on the camera with natural blinking and 'smiling eyes' (Duchenne smile). Mouth movements must be highly articulated for Hindi phonemes, showing realistic jaw tension and natural teeth visibility. Hands should move in a fluid, non-repetitive 'open-palm' gesture near the chest area to convey openness and peace. The overall presence should be calm, avoiding any sudden jerks, reflecting a meditative state of being.",
  }];
  const SEED_IDS = new Set(SEED.map((c) => c.id));
  // A retired id is no longer in SEED_IDS, so without this the stored `user`
  // array would resurrect it as an operator-created category.
  const RETIRED = new Set(["birthday"]);
  const FIELDS = ["label", "video_prompt", "motion_prompt"];
  const STORE = "autodub_video_cat_store_v1", ACTIVE = "autodub_video_active_cat";
  const CHAR = "autodub_video_character", JOB_KEY = "autodub_video_job";

  let cats = [], activeId = null;
  const seedById = (id) => SEED.find((c) => c.id === id);

  function loadStore() {
    try { const s = JSON.parse(localStorage.getItem(STORE)); if (s && typeof s === "object" && !Array.isArray(s)) return s; }
    catch (_) {}
    return {};
  }
  function loadCats() {
    const s = loadStore();
    const deleted = new Set(Array.isArray(s.deleted) ? s.deleted : []);
    const ov = s.overrides && typeof s.overrides === "object" ? s.overrides : {};
    const out = [];
    SEED.forEach((c) => { if (!deleted.has(c.id)) out.push({ ...c, ...(ov[c.id] || {}) }); });
    if (Array.isArray(s.user)) s.user.forEach((c) => { if (c && c.id && !RETIRED.has(c.id)) out.push({ ...c }); });
    return out;
  }
  function persist() {
    const overrides = {}, user = [], present = new Set();
    cats.forEach((c) => {
      if (SEED_IDS.has(c.id)) {
        present.add(c.id);
        const base = seedById(c.id), diff = {};
        FIELDS.forEach((k) => { if (c[k] !== base[k]) diff[k] = c[k]; });
        if (Object.keys(diff).length) overrides[c.id] = diff;
      } else user.push({ id: c.id, label: c.label, video_prompt: c.video_prompt, motion_prompt: c.motion_prompt });
    });
    const deleted = SEED.map((c) => c.id).filter((id) => !present.has(id));
    try {
      localStorage.setItem(STORE, JSON.stringify({ overrides, deleted, user }));
      localStorage.setItem(ACTIVE, activeId || "");
    } catch (_) {}
  }
  const activeCat = () => cats.find((c) => c.id === activeId) || cats[0] || null;
  const newId = (label) => (label || "cat").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "").slice(0, 24) + "-" + Math.floor(Math.random() * 1e6).toString(36);

  const els = {
    banners: $("#videoBanners"), monitor: $("#vMonitor"), body: $("#vBody"), tag: $("#vTag"),
    file: $("#vFile"), fileName: $("#vFileName"), fileSize: $("#vFileSize"), img: $("#vImg"),
    cats: $("#vCats"), mode: $("#vMode"), voice: $("#vVoice"), date: $("#vDate"),
    single: $("#vSingle"), batch: $("#vBatch"), script: $("#vScript"), count: $("#vCount"),
    title: $("#vTitle"), drop: $("#vDrop"), xls: $("#vXls"),
    dropTitle: $("#vDropTitle"), dropSub: $("#vDropSub"), queue: $("#vQueue"),
    progress: $("#videoProgress"), pCount: $("#vpCount"), pDetail: $("#vpDetail"),
    up: $("#vpUp"), done: $("#vpDone"), bad: $("#vpBad"),
    upN: $("#vpUpN"), doneN: $("#vpDoneN"), badN: $("#vpBadN"), elapsed: $("#vpElapsed"),
    run: $("#vRun"), cancel: $("#vCancel"), msg: $("#vMsg"), live: $("#videoLive"),
  };

  const state = { image: null, photoId: null, stillURL: null, excel: null, excelProblem: null,
                  batch: false, character: localStorage.getItem(CHAR) || "indian",
                  running: false, jobId: null, kind: null, startedAt: null };

  // ── categories ───────────────────────────────────────────────────────────
  // Chip and edit button are one visual control split by a hairline — the edit
  // affordance as its own floating card read as a mystery third button.
  function renderCats() {
    els.cats.innerHTML = "";
    cats.forEach((c) => {
      const wrap = document.createElement("div");
      wrap.className = "chipwrap" + (c.id === activeId ? " on" : "");
      const chip = document.createElement("button");
      chip.type = "button"; chip.className = "token chip"; chip.dataset.id = c.id;
      chip.setAttribute("aria-pressed", String(c.id === activeId));
      chip.textContent = c.label;
      chip.addEventListener("click", () => { activeId = c.id; persist(); renderCats(); refreshRun(); });
      const pen = document.createElement("button");
      pen.type = "button"; pen.className = "pen";
      pen.setAttribute("aria-label", `Edit ${c.label} prompts`);
      pen.title = `Edit ${c.label} prompts`;
      pen.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
      pen.addEventListener("click", () => openEditor(c.id));
      wrap.append(chip, pen);
      els.cats.appendChild(wrap);
    });
    const add = document.createElement("button");
    add.type = "button"; add.className = "token add"; add.textContent = "+ New";
    add.setAttribute("aria-label", "New prompt category");
    add.addEventListener("click", () => openEditor(null));
    els.cats.appendChild(add);
    refreshRun();
  }

  // ── monitor ──────────────────────────────────────────────────────────────
  // mtag names what the monitor is SHOWING; the run message names what the job
  // is doing. Never tinted or animated — a render has to be judged honestly.
  function showPlaceholder() {
    els.body.innerHTML = `<div class="ph"><div class="t">No avatar yet</div>
      <div class="s">Drag an image here, or pick one</div>
      <div class="row"><button type="button" class="btn" id="vBrowse">Upload image</button>
      <button type="button" class="btn" id="vReuse">Reuse an avatar</button></div></div>`;
    $("#vBrowse").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) els.img.click(); });
    $("#vReuse").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) openAvatars(); });
    els.tag.textContent = "No input"; els.file.hidden = true;
  }
  function showStill(tag) {
    els.body.innerHTML = state.stillURL ? `<img src="${state.stillURL}" alt="Selected reference avatar" />` : "";
    els.tag.textContent = tag || "Reference";
    els.file.hidden = !(state.image || state.photoId);
  }
  function showVideo(url) {
    els.body.innerHTML = `<video src="${url}" controls autoplay playsinline></video>`;
    els.tag.textContent = "Playback"; els.file.hidden = true;
    const v = els.body.querySelector("video");
    if (!v) return;
    // A truncated download does not reliably fire `error` — Chrome sits at
    // networkState=LOADING forever — so give up on a metadata timeout too.
    let settled = false;
    const giveUp = (why) => { if (settled) return; settled = true; clearTimeout(t);
      showStill("Clip unavailable");
      U.banner(els.banners, { kind: "warn", title: "The clip could not be played",
        detail: `It rendered, but this machine could not read the file (${why}).` }); };
    const t = setTimeout(() => giveUp("no metadata after 12s"), 12000);
    v.addEventListener("loadedmetadata", () => { settled = true; clearTimeout(t); }, { once: true });
    v.addEventListener("error", () => giveUp("media error"), { once: true });
  }

  els.monitor.addEventListener("click", (e) => {
    if (state.running || e.target.closest(".mfile") || e.target.closest("video")) return;
    els.img.click();
  });
  ["dragover", "dragenter"].forEach((e) => els.monitor.addEventListener(e, (ev) => {
    ev.preventDefault(); if (!state.running) els.monitor.classList.add("over"); }));
  ["dragleave", "drop"].forEach((e) => els.monitor.addEventListener(e, (ev) => {
    ev.preventDefault(); els.monitor.classList.remove("over"); }));
  els.monitor.addEventListener("drop", (ev) => {
    if (state.running) return; const f = ev.dataTransfer.files[0]; if (f) onImage(f); });
  els.img.addEventListener("change", () => { if (els.img.files[0]) onImage(els.img.files[0]); });
  $("#vReplace").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) els.img.click(); });
  $("#vRemove").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) clearImage(); });

  function onImage(file) {
    if (!/^image\//.test(file.type)) return;
    state.image = file; state.photoId = null;
    if (state.stillURL) URL.revokeObjectURL(state.stillURL);
    state.stillURL = URL.createObjectURL(file);
    els.fileName.textContent = file.name; els.fileSize.textContent = U.kb(file.size);
    if (!state.running) showStill("Reference");
    refreshRun();
  }
  function clearImage() {
    state.image = null; state.photoId = null;
    if (state.stillURL) { URL.revokeObjectURL(state.stillURL); state.stillURL = null; }
    els.img.value = "";
    if (!state.running) showPlaceholder();
    refreshRun();
  }

  // ── avatar picker ────────────────────────────────────────────────────────
  const avOv = U.overlay($("#avDrawer"), $("#avScrim"), { fallbackFocus: () => $("#vBrowse") });
  $("#avClose").addEventListener("click", avOv.close);
  async function openAvatars() {
    avOv.open();
    const body = $("#avBody");
    body.innerHTML = `<div class="empty"><div class="s">Loading avatars…</div></div>`;
    try {
      const r = await U.fetchT("/video/heygen/talking-photos", {}, 25000);
      if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
      const items = (await r.json()).items || [];
      if (!items.length) {
        body.innerHTML = `<div class="empty"><div class="t">No avatars yet</div>
          <div class="s">Upload an image once and it appears here.</div></div>`; return;
      }
      const grid = document.createElement("div"); grid.className = "avgrid";
      items.forEach((it) => {
        // HeyGen returns talking_photo_id / name / preview_url
        const id = it.talking_photo_id || it.id || "";
        const name = it.name || id.slice(0, 8);
        const url = it.preview_url || it.preview_image_url || "";
        const card = document.createElement("button");
        card.type = "button"; card.className = "avcard";
        card.setAttribute("aria-label", `Use avatar ${name}`);
        card.innerHTML = (url ? `<img src="${U.esc(url)}" alt="" />` : `<span class="noimg">No preview</span>`)
          + `<span class="nm">${U.esc(name)}</span>`;
        card.addEventListener("click", () => {
          state.photoId = id; state.image = null; els.img.value = "";
          if (state.stillURL) URL.revokeObjectURL(state.stillURL);
          state.stillURL = url || null;
          els.fileName.textContent = name; els.fileSize.textContent = "Reused";
          if (!state.running) showStill("Reference");
          avOv.close(); refreshRun();
        });
        grid.appendChild(card);
      });
      body.innerHTML = ""; body.appendChild(grid);
    } catch (e) {
      body.innerHTML = `<div class="empty"><div class="t">Could not load avatars</div>
        <div class="s">${U.esc(e.message)}</div></div>`;
    }
  }

  // ── mode, voice, script, title ───────────────────────────────────────────
  U.bindSeg(els.mode, ({ mode }) => {
    state.batch = mode === "batch";
    els.single.hidden = state.batch; els.batch.hidden = !state.batch;
    els.run.textContent = state.batch ? "Generate batch" : "Generate video";
    refreshRun();
  });
  U.bindSeg(els.voice, ({ char }) => {
    state.character = char;
    try { localStorage.setItem(CHAR, char); } catch (_) {}
  });
  els.script.addEventListener("input", () => {
    els.count.textContent = `${els.script.value.length} / 1000`; refreshRun();
  });
  els.title.addEventListener("input", refreshRun);
  // the title becomes the NAS filename, so it has to be filesystem-safe
  const safeTitle = () => els.title.value.trim().replace(/[\\/:*?"<>|]+/g, "_").replace(/\.mp4$/i, "");
  try {
    const d = new Date();
    els.date.value = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  } catch (_) {}

  U.dropzone(els.drop, els.xls, onExcel);
  async function onExcel(file) {
    if (!file.name.toLowerCase().endsWith(".xlsx")) {
      els.drop.dataset.state = "bad"; els.dropSub.textContent = "Only .xlsx files are accepted"; return;
    }
    state.excel = file; state.excelProblem = null;
    els.drop.classList.remove("empty"); els.drop.dataset.state = "ok";
    els.drop.querySelector(".ic").textContent = "✓";
    els.dropTitle.textContent = file.name; els.dropSub.textContent = "Reading…";
    refreshRun();
    try {
      const fd = new FormData(); fd.append("file", file);
      const r = await U.fetchT("/batch/preview-excel", { method: "POST", body: fd }, 15000);
      if (!r.ok) throw new Error("preview failed");
      const rows = (await r.json()).rows || [];
      const headers = (rows[0] || []).map((h) => String(h).trim().toLowerCase());
      if (!headers.includes("script")) {
        state.excelProblem = "Missing column: script";
        els.drop.dataset.state = "bad"; els.drop.querySelector(".ic").textContent = "!";
        els.dropSub.textContent = state.excelProblem;
      } else {
        els.dropSub.textContent = `${Math.max(0, rows.length - 1)} rows · headers ok`;
      }
    } catch (_) { els.dropSub.textContent = `${U.kb(file.size)} · could not read the sheet`; }
    refreshRun();
  }

  // ── validity ─────────────────────────────────────────────────────────────
  function blocker() {
    if (!cats.length) return "Create a prompt category to start";
    if (!activeCat()) return "Pick a category";
    if (state.batch) {
      // the batch endpoint takes an image upload only — no talking_photo_id
      if (!state.image) return "Batch needs an uploaded image, not a reused avatar";
      if (!state.excel) return "Drop a spreadsheet of scripts";
      if (state.excelProblem) return state.excelProblem;
    } else {
      if (!state.image && !state.photoId) return "Add an avatar to start";
      if (!els.script.value.trim()) return "Write the line the avatar should speak";
      if (!safeTitle()) return "Name the file so it does not overwrite an earlier render";
    }
    if (!window.Shell.configured()) return "Runtime config is not ready";
    return null;
  }
  function refreshRun() {
    if (state.running) { els.run.disabled = true; els.cancel.hidden = true; return; }
    const why = blocker();
    els.run.disabled = Boolean(why);
    if (!els.msg.classList.contains("err")) els.msg.textContent = why || "";
  }
  const say = (t, bad) => { els.msg.textContent = t || ""; els.msg.classList.toggle("err", !!bad); };

  // ── run ──────────────────────────────────────────────────────────────────
  els.run.addEventListener("click", submit);
  async function submit() {
    const why = blocker();
    if (why) { say(why, true); return; }
    say(""); U.clearBanners(els.banners);
    U.askNotify();   // not awaited — see pane-audio.js

    const c = activeCat();
    state.running = true; state.kind = state.batch ? "batch" : "single";
    state.startedAt = new Date().toISOString();
    els.live.hidden = false; refreshRun();
    window.Shell.setRunning("video", true, "Video rendering");
    els.tag.textContent = "Rendering"; els.progress.hidden = false;

    const fd = new FormData();
    if (state.image) fd.append("image", state.image);
    else fd.append("talking_photo_id", state.photoId);
    fd.append("character", state.character);
    if (els.date.value) fd.append("publish_date", els.date.value);
    if (c.video_prompt) fd.append("video_prompt", c.video_prompt);
    if (c.motion_prompt) fd.append("motion_prompt", c.motion_prompt);

    try {
      let url, id;
      if (state.batch) {
        fd.append("excel", state.excel);
        const r = await fetch("/video/heygen/batch", { method: "POST", body: fd });
        if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
        id = (await r.json()).batch_id; url = `/video/heygen/batch/${id}`;
      } else {
        fd.append("script", els.script.value.trim());
        fd.append("video_title", safeTitle());
        const r = await fetch("/video/heygen", { method: "POST", body: fd });
        if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
        id = (await r.json()).job_id; url = `/video/heygen/${id}`;
      }
      state.jobId = id;
      try { localStorage.setItem(JOB_KEY, JSON.stringify({ kind: state.kind, id })); } catch (_) {}
      poll(url);
    } catch (e) {
      U.banner(els.banners, { kind: "error", title: "Could not start the render",
        detail: "The request never reached the render service.", raw: String(e && e.message || e) });
      finish("failed", null);
    }
  }

  async function poll(url) {
    while (state.running) {
      try {
        const r = await U.fetchT(url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const p = await r.json();
        if (state.kind === "batch") renderBatch(p); else renderSingle(p);
        if (["completed", "failed", "partial", "cancelled"].includes(p.status)) { finish(p.status, p); return; }
      } catch (_) {}
      await new Promise((res) => setTimeout(res, 4000));
    }
  }

  function renderSingle(p) {
    const done = p.status === "completed" ? 1 : 0;
    const failed = p.status === "failed" ? 1 : 0;
    els.pCount.innerHTML = `${done} <small>of 1 video</small>`;
    els.pDetail.textContent = p.stage_message || p.status || "";
    els.upN.textContent = done; els.doneN.textContent = done ? 0 : 1 - failed; els.badN.textContent = failed;
    U.setBar(els, { total: 1, uploaded: done, rendered: done || failed ? 0 : 1, failed, live: !done && !failed });
    els.elapsed.textContent = state.startedAt ? `Started ${U.clock(state.startedAt)}` : "";
  }
  function renderBatch(p) {
    const rows = p.rows || [], total = p.total || rows.length || 1;
    const uploaded = rows.filter((r) => r.status === "completed" && r.nas_path).length;
    const doneNoNas = rows.filter((r) => r.status === "completed" && !r.nas_path).length;
    const failed = rows.filter((r) => r.status === "failed").length;
    els.pCount.innerHTML = `${uploaded + doneNoNas} <small>of ${total} videos</small>`;
    els.upN.textContent = uploaded; els.doneN.textContent = doneNoNas; els.badN.textContent = failed;
    U.setBar(els, { total, uploaded, rendered: doneNoNas, failed, live: p.status === "rendering" });
    const left = U.eta(state.startedAt, uploaded + doneNoNas, total);
    els.pDetail.textContent = [`${uploaded + doneNoNas} of ${total} rows`, left].filter(Boolean).join(" · ");
    els.elapsed.textContent = state.startedAt ? `Started ${U.clock(state.startedAt)}` : "";
    renderQueue(rows);
  }
  function renderQueue(rows) {
    if (!rows.length) return;
    els.queue.innerHTML = "";
    rows.forEach((row) => {
      const s = row.status || "pending";
      const badge = s === "completed" ? "g" : s === "failed" ? "r" : s === "rendering" ? "a" : "";
      const el = document.createElement("div");
      el.className = "qrow";
      el.innerHTML = `<span class="i">row ${row.row_index}</span>` +
        `<span class="nm">${U.esc(row.video_title || "untitled")}</span>` +
        `<span class="path">${U.esc(row.nas_path || "—")}</span>` +
        `<span class="badge ${badge}">${U.esc(s[0].toUpperCase() + s.slice(1))}</span>` +
        (row.video_local_url ? `<a class="btn q" href="${row.video_local_url}" download>Open</a>` : `<span></span>`);
      els.queue.appendChild(el);
      if (s === "completed" && row.video_local_url) showVideo(row.video_local_url);
    });
  }

  function finish(status, p) {
    state.running = false; state.jobId = null;
    try { localStorage.removeItem(JOB_KEY); } catch (_) {}
    els.live.hidden = true;
    window.Shell.setRunning("video", false);
    refreshRun();
    window.Shell.refreshRecoverable();

    const cause = p && p.cause;
    if (cause) window.Shell.showCause(els.banners, cause);

    if (status === "completed") {
      const url = p && (p.video_local_url || (p.summary && p.summary.video_url));
      if (url) showVideo(url); else showStill("Complete");
      const nas = p && p.summary && p.summary.nas_path;
      U.banner(els.banners, { kind: "ok", title: "Render finished",
        detail: nas ? `Filed to <code>${U.esc(nas)}</code>` : "Finished." });
      U.notify("Video render finished", nas ? `Filed to ${nas}` : "Finished", false);
    } else if (status === "partial") {
      const failed = (p.rows || []).filter((r) => r.status === "failed").length;
      U.notify(`Video batch finished with ${failed} failures`,
        cause ? cause.title : `${(p.done || 0)} of ${p.total || 0} filed to the NAS`, true);
    } else {
      showStill("Failed");
      if (!cause) U.banner(els.banners, { kind: "error", title: "The render did not finish",
        detail: "No cause was reported by the server." });
      U.notify("Video render failed", cause ? cause.title : "See the app for details", true);
    }
  }

  // ── category editor ──────────────────────────────────────────────────────
  const drawer = $("#catDrawer");
  const name = $("#catName"), vprompt = $("#catVideo"), mprompt = $("#catMotion");
  const errEl = $("#catErr"), dirtyEl = $("#catDirty");
  let editingId = null, snapshot = null;
  const fields = () => ({ label: name.value.trim(), video_prompt: vprompt.value.trim(), motion_prompt: mprompt.value.trim() });
  const dirty = () => snapshot !== null && JSON.stringify(fields()) !== JSON.stringify(snapshot);

  const catOv = U.overlay(drawer, $("#catScrim"), {
    focus: () => name,
    fallbackFocus: () => els.cats.querySelector("button"),
    beforeClose() {
      if (!dirty()) return true;
      U.confirm({ title: "Discard your changes?",
        message: "This category has edits that have not been saved yet.",
        confirmLabel: "Discard", onOk: () => { snapshot = null; catOv.close(); } });
      return false;
    },
  });
  function refreshEditor() {
    dirtyEl.hidden = !dirty();
    $("#catVideoN").textContent = vprompt.value.length || "";
    $("#catMotionN").textContent = mprompt.value.length || "";
    const seed = editingId ? seedById(editingId) : null;
    const cur = editingId ? cats.find((c) => c.id === editingId) : null;
    $("#catReset").hidden = !(seed && cur && FIELDS.some((k) => cur[k] !== seed[k]));
  }
  [name, vprompt, mprompt].forEach((el) => el.addEventListener("input", refreshEditor));

  function openEditor(id, from) {
    editingId = id;
    const c = id ? cats.find((x) => x.id === id) : from || null;
    $("#catTitle").textContent = id ? "Edit category" : "New category";
    $("#catSub").textContent = id ? c.label : from ? `Copy of ${from.label}` : "Video and motion prompt preset";
    name.value = c ? (id ? c.label : `${c.label} copy`) : "";
    vprompt.value = c ? c.video_prompt || "" : "";
    mprompt.value = c ? c.motion_prompt || "" : "";
    $("#catDelete").hidden = !id; $("#catDuplicate").hidden = !id;
    errEl.hidden = true;
    snapshot = fields(); refreshEditor();
    catOv.open();
  }
  $("#catClose").addEventListener("click", catOv.close);
  $("#catSave").addEventListener("click", save);
  drawer.addEventListener("keydown", (e) => {
    // textareas, so plain Return must stay a newline
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
  });
  function save() {
    const label = name.value.trim();
    if (!label) { errEl.textContent = "A name is required."; errEl.hidden = false; name.focus(); return; }
    if (cats.some((c) => c.id !== editingId && c.label.toLowerCase() === label.toLowerCase())) {
      errEl.textContent = "Another category already uses that name."; errEl.hidden = false; name.focus(); return;
    }
    errEl.hidden = true;
    const data = fields();
    if (editingId) Object.assign(cats.find((x) => x.id === editingId), data);
    else { const id = newId(label); cats.push({ id, ...data }); activeId = id; }
    persist(); renderCats();
    snapshot = null;              // saving means done — the drawer closes
    catOv.close();
  }
  $("#catDuplicate").addEventListener("click", () => {
    const c = cats.find((x) => x.id === editingId); if (!c) return;
    snapshot = null; openEditor(null, c);
  });
  $("#catReset").addEventListener("click", () => {
    const seed = seedById(editingId); if (!seed) return;
    U.confirm({ title: "Reset to the shipped default?",
      message: `“${seed.label}” goes back to the prompts this build ships with. Your edits to it are lost.`,
      confirmLabel: "Reset",
      onOk: () => {
        name.value = seed.label; vprompt.value = seed.video_prompt; mprompt.value = seed.motion_prompt;
        Object.assign(cats.find((x) => x.id === editingId), { ...seed });
        persist(); renderCats(); snapshot = fields(); refreshEditor();
      } });
  });
  $("#catDelete").addEventListener("click", () => {
    if (!editingId) return;
    const id = editingId, c = cats.find((x) => x.id === id);
    U.confirm({ title: "Delete this category?",
      message: `“${c ? c.label : "This category"}” and its prompts will be removed. This cannot be undone.`,
      confirmLabel: "Delete",
      onOk: () => {
        cats = cats.filter((x) => x.id !== id);
        if (activeId === id) activeId = cats[0] ? cats[0].id : null;
        persist(); renderCats(); snapshot = null; catOv.close();
      } });
  });

  // ── survive a reload ─────────────────────────────────────────────────────
  async function reattach() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(JOB_KEY)); } catch (_) {}
    if (!saved || !saved.id) return;
    const url = saved.kind === "batch" ? `/video/heygen/batch/${saved.id}` : `/video/heygen/${saved.id}`;
    try {
      const r = await U.fetchT(url);
      if (!r.ok) { localStorage.removeItem(JOB_KEY); return; }
      const p = await r.json();
      if (["completed", "failed", "partial", "cancelled"].includes(p.status)) { localStorage.removeItem(JOB_KEY); return; }
      state.running = true; state.kind = saved.kind; state.jobId = saved.id;
      state.startedAt = state.startedAt || new Date().toISOString();
      els.live.hidden = false; els.progress.hidden = false; refreshRun();
      U.banner(els.banners, { kind: "info", title: "Reattached to a render already in progress",
        detail: "Nothing was lost when the page reloaded." });
      poll(url);
    } catch (_) {}
  }

  // ── boot ─────────────────────────────────────────────────────────────────
  cats = loadCats();
  const savedActive = localStorage.getItem(ACTIVE);
  activeId = (savedActive && cats.some((c) => c.id === savedActive)) ? savedActive : (cats[0] ? cats[0].id : null);
  renderCats();
  els.voice.querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.char === state.character)));
  showPlaceholder();
  reattach();

  window.PaneVideo = { refreshRun, isRunning: () => state.running };
})();
